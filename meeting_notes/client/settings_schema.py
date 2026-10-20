"""The Settings dialog, described as data, and the one place that saves it.

Two screens show these settings: the desktop app's Settings dialog (``ui/settings_dialog.py``) and the server's
Recorders page (the recorder window's Settings panel, 0.7.15). They must stay the same, so neither owns the
wording: both take their pages, section titles, field labels, help texts, choices, order and visibility rules from
:func:`build_schema`. ``tests/test_settings_schema.py`` walks the real dialog and fails when a field is in one and
not the other.

The schema is plain JSON (it travels to the server in the ``get_settings`` remote command, see ``remote.py``):

* ``{"pages": [{"key", "title", "local_only"?, "items": [...]}]}``. A page with ``local_only`` (Logs) exists only
  in the dialog; the remote copy leaves it out.
* an item is one of
  ``{"type": "section", "id", "title"}``,
  ``{"type": "note", "id", "text", "show_if"?, "variants"?: [{"if", "text"}]}`` (a muted paragraph; with
  ``variants`` the first variant whose conditions hold gives the text) or
  ``{"type": "field", "key", "kind", "label", ...}``.
* a field's ``key`` is its dotted config path (``meeting_detection.auto_record``); ``kind`` is one of ``bool``
  (``label`` is the checkbox text), ``choice`` (``choices``: ``[[value, label], ...]``), ``text``, ``path``,
  ``device`` (``mic`` / ``system`` list, filled from the computer's devices), ``note_type`` (filled from the
  server's note types), ``secret`` (never sent: the page only learns whether it is set) and ``readonly``.
  Optional: ``help`` (the dialog's tooltip), ``aria``, ``placeholder``, ``show_if`` / ``enabled_if`` (lists of
  conditions that must all hold: ``{"key", "eq"}`` or ``{"key", "ne"}``, evaluated on the form's current
  values) and ``confirm`` (what the remote page asks before sending that change).

Nothing here talks to the network, and the sign-in token is never read into any value that leaves this module:
``server.token`` is a ``secret`` field whose value is just ``True`` / ``False`` (set / not set).
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from meeting_notes import __version__
from meeting_notes import config as config_mod
from meeting_notes import remote

log = logging.getLogger("meeting_notes.client.settings_schema")

KINDS = ("bool", "choice", "text", "path", "device", "note_type", "secret", "readonly")
NOT_REMOTE = "This can only be changed on the computer itself."
MAX_URL = 500
MAX_PATH = 1000

AUTO_LABEL = "Automatic (system default)"
SERVER_DEFAULT_LABEL = "Server default"
GPU_UNSUPPORTED = "GPU helper is available on Windows PCs with an NVIDIA graphics card."

# (key, title, local_only)
PAGE_TITLES = (
    ("general", "General", False),
    ("audio", "Audio", False),
    ("recordings", "Recordings", False),
    ("server", "Server", False),
    ("gpu_helper", "GPU helper", False),
    ("remote", "Remote control", False),
    ("logs", "Logs", True),
    ("about", "About", False),
)

# Condition lists shared by several items.
_DETECT_ON = [{"key": "meeting_detection.enabled", "eq": True}]
_AUTO_ON = _DETECT_ON + [{"key": "meeting_detection.auto_record", "eq": True}]
_BYE_ON = _AUTO_ON + [{"key": "meeting_detection.auto_end", "eq": "bye"}]
_LIVE_ON = {"key": "server.live_preview", "eq": True}
_LIVE_OFF = {"key": "server.live_preview", "eq": False}


def _section(item_id: str, title: str) -> Dict[str, Any]:
    return {"type": "section", "id": f"section.{item_id}", "title": title}


def _note(item_id: str, text: str, **extra: Any) -> Dict[str, Any]:
    return {"type": "note", "id": f"note.{item_id}", "text": text, **extra}


def _field(key: str, kind: str, label: str, **extra: Any) -> Dict[str, Any]:
    item = {"type": "field", "key": key, "kind": kind, "label": label}
    item.update({k: v for k, v in extra.items() if v is not None})
    return item


def build_schema(
    *,
    platform: Optional[str] = None,
    note_types_known: bool = True,
    trash_name: Optional[str] = None,
    first_run: bool = False,
    server_url: str = "",
) -> Dict[str, Any]:
    """Every page of Settings, in the dialog's order.

    ``platform`` is ``sys.platform``; ``note_types_known`` is false when the app has not reached the server's note
    types (the dialog then says to connect); ``first_run`` + ``server_url`` word the password hint of the first-start
    prompt. Text is word-for-word what the dialog shows."""
    platform = sys.platform if platform is None else platform
    if trash_name is None:
        from meeting_notes.client import retention

        trash_name = retention.trash_name()
    if first_run and server_url:
        hint = f"Paste the same password you use to sign in at {server_url}."
    else:
        hint = "The same password you use to sign in on the server's web page."

    general = [
        _section("appearance", "Appearance"),
        _field(
            "appearance", "choice", "Theme", aria="Appearance",
            choices=[["system", "System"], ["light", "Light"], ["dark", "Dark"]],
        ),
        _section("detection", "Meeting detection"),
        _field("meeting_detection.enabled", "bool", "Offer to record Teams, Zoom and Google Meet calls"),
        _field(
            "meeting_detection.auto_record", "bool", "Start recording automatically when a call starts",
            enabled_if=_DETECT_ON,
        ),
        _field(
            "meeting_detection.auto_end", "choice", "Also end", show_if=_AUTO_ON,
            choices=[
                ["manual", "Nothing else"],
                ["hour", "On the hour"],
                ["silence", "After 30 seconds of silence"],
                ["bye", "When people say goodbye"],
            ],
        ),
        _note(
            "auto_end",
            "An extra rule on top of Stop when the meeting closes; whichever comes first ends the recording. On the "
            "hour stops at the end of the hour the call is in (a call joined in the last 10 minutes before the hour "
            "runs to the next one). You can turn auto end off for any recording.",
            show_if=_AUTO_ON,
        ),
        _note(
            "auto_end_bye", "",
            show_if=_BYE_ON,
            variants=[
                {
                    "if": [_LIVE_ON],
                    "text": "Goodbyes are heard in the server's live preview: after one, 20 seconds of quiet on both "
                            "sides ends the recording. Without a live transcript it never ends.",
                },
                {
                    "if": [_LIVE_OFF],
                    "text": "When people say goodbye needs the live preview, which is turned off (Server page, "
                            "Uploads). Until you turn it on this never ends the recording.",
                },
            ],
        ),
        _field(
            "meeting_detection.auto_stop", "bool", "Stop when the meeting closes",
            help="Applies to every detected meeting, recorded after a prompt or automatically. It waits for the "
                 "meeting audio to go quiet and shows a countdown first (Keep recording cancels it).",
        ),
        _field(
            "meeting_detection.suggest_stop", "bool", "Suggest stopping when a meeting seems over",
            help="Asks whether to stop when the call ends or when there has been no audio for five minutes. "
                 "It never stops a recording by itself.",
        ),
        _section("notes", "Notes"),
        _field(
            "default_note_type", "note_type", "Default note type",
            first_label=SERVER_DEFAULT_LABEL, first_label_named="Server default ({name})",
            missing_label="{id} (not found on the server)", missing_label_unknown="{id}",
        ),
    ]
    if not note_types_known:
        general.append(_note("note_type_offline", "Connect to the server to choose a note type."))
    general.append(_note(
        "note_type",
        "Meetings recorded here get notes of this type automatically, saved where that note type sends them "
        "(for example its Notion page). You can pick another type for a single meeting next to the meeting name.",
    ))

    audio = [
        _field("show_audio_levels", "bool", "Show audio levels before recording"),
        _note(
            "levels",
            "Shows live input from your microphone and speakers while the window is open, greyed out "
            "until you record. Nothing is saved. Turn it off to keep your microphone closed until you press "
            "Start recording.",
        ),
        _section("devices", "Devices"),
        _field(
            "audio_devices.mic", "device", "Microphone", device_kind="mic",
            first_label=AUTO_LABEL, missing_label="{name} (not connected)",
        ),
        _field(
            "audio_devices.system", "device", "Speakers", device_kind="system", aria="Speakers (what you hear)",
            help="Speakers: what you hear, recorded as the other side of the call",
            first_label=AUTO_LABEL, missing_label="{name} (not connected)",
            disable_if_empty=True if platform == "darwin" else None,
        ),
    ]
    if platform == "darwin":
        # macOS records "what you hear" through ScreenCaptureKit, which is not a device you can choose.
        # Only a virtual loopback driver (BlackHole, ...) shows up here, when one is installed.
        audio.append(_note(
            "devices_mac",
            "On a Mac, what you hear is captured automatically through macOS screen and system audio "
            "recording, so there is no speaker to pick. A virtual loopback driver such as BlackHole "
            "appears in the list when it is installed.",
        ))
    audio += [
        _note(
            "devices_auto",
            "Automatic follows the system default. If the device you chose is not connected, recording uses "
            "the automatic one instead. A change applies to the next recording (and to the level preview and "
            "the device names in the main window); a recording in progress keeps its devices.",
        ),
        _note(
            "devices_window",
            "The main window shows which microphone and speakers are in use; its menu has Refresh audio devices "
            "to re-scan them.",
        ),
    ]

    recordings = [
        _section("save_folder", "Save folder"),
        _field("save_dir", "path", "Save recordings to"),
        _section("local", "Local recordings"),
        _field(
            "local_retention_days", "choice", "Keep recordings", aria="Keep recordings on this computer",
            choices=[[0, "Forever"], [7, "7 days"], [30, "30 days"], [90, "90 days"]],
        ),
        _note(
            "retention",
            f"Only after the server has the finished transcript. Removed recordings go to the "
            f"{trash_name}; anything still waiting to upload is kept.",
        ),
        _field(
            "delete_after_upload", "bool", "Delete from this computer after the server confirms the upload",
            help="Once the server has confirmed it received the whole recording (not just started it), the "
            "recording's audio on this computer is removed. The transcript and notes stay on the server. A "
            "recording that fails to upload is never deleted. For recordings that upload successfully this "
            "overrides Keep recordings.",
        ),
        _field("recordings.stats", "readonly", "In this folder"),
    ]

    server = [
        _field("server.url", "text", "Server URL", placeholder="http://192.168.1.50:8000", max_len=MAX_URL, confirm={
            "on": "change",
            "title": "Change the server address?",
            "lead": "This computer will reconnect to {value}; if it is wrong the recorder will drop off this page.",
            "button": "Change address",
        }),
        _field(
            "server.token", "secret", "Server password", aria="Server password",
            placeholder="Server password (leave empty if the server has none)",
        ),
        _note("password_hint", hint),
        _section("uploads", "Uploads"),
        _field("server.live_preview", "bool", "Show the server's live preview while recording"),
        _field("server.auto_upload", "bool", "Upload finished recordings for transcription"),
        _section("updates", "Updates"),
        _field("server.check_updates", "bool", "Check the server for client updates"),
        _note(
            "transcription",
            "Transcription runs on the server only. The live preview is approximate "
            "and disposable; the transcript you keep is produced by the server from "
            "the complete recording after the meeting. A dropped connection can never "
            "lose the local audio.",
        ),
    ]

    if platform == "win32":
        gpu_helper = [
            _field("gpu_helper.enabled", "bool", "Let the server use this computer's graphics card"),
            _note(
                "gpu_helper",
                "Lets the server send transcription work to this computer's NVIDIA graphics card. Work pauses while "
                "you record here, and the app keeps running in the notification area when you close its window. "
                "GPU support must be installed on this computer first.",
            ),
            _field("gpu_helper.gpu", "readonly", "Graphics card"),
            _field("gpu_helper.runtime", "readonly", "GPU support"),
            _field(
                "gpu_helper.start_at_login", "bool", "Start when I sign in to Windows",
                enabled_if=[{"key": "gpu_helper.enabled", "eq": True}],
            ),
            _field("gpu_helper.status", "readonly", "Status"),
        ]
    else:
        gpu_helper = [_note("gpu_helper_unsupported", GPU_UNSUPPORTED)]

    remote_page = [
        _field("remote_control_allowed", "bool", "Allow control from the server", confirm={
            "on": "false",
            "title": "Turn off remote control?",
            "lead": "You will lose remote control of this computer. It can only be turned back on in the app's "
                    "own Settings on that computer.",
            "button": "Turn off",
        }),
        _note(
            "remote",
            "Lets the Recorders page on your server start and stop recordings, mute and more "
            "while this app is open. The app always shows a notice when it does.",
        ),
    ]

    about = [
        _field("about.version", "readonly", "Version"),
        _field("about.save_dir", "readonly", "Recordings folder"),
        _field("about.config", "readonly", "Settings file"),
        _field("about.logs", "readonly", "Logs folder"),
    ]

    contents = {
        "general": general, "audio": audio, "recordings": recordings, "server": server,
        "gpu_helper": gpu_helper, "remote": remote_page, "logs": [], "about": about,
    }
    pages = []
    for key, title, local_only in PAGE_TITLES:
        page: Dict[str, Any] = {"key": key, "title": title, "items": contents[key]}
        if local_only:
            page["local_only"] = True
        pages.append(page)
    return {"pages": pages}


# -- reading a schema --------------------------------------------------------------------------


def remote_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    """The pages the server's Recorders page shows (everything but the dialog-only ones)."""
    return {"pages": [p for p in schema["pages"] if not p.get("local_only")]}


def page_items(schema: Dict[str, Any], page: Optional[str] = None) -> List[Dict[str, Any]]:
    """The items of one page (or of every page, in order)."""
    out: List[Dict[str, Any]] = []
    for p in schema["pages"]:
        if page is None or p["key"] == page:
            out.extend(p["items"])
    return out


def item_id(item: Dict[str, Any]) -> str:
    """A field's key, or a section's / note's id (unique across the schema)."""
    return item["key"] if item["type"] == "field" else item["id"]


def items_by_id(schema: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {item_id(i): i for i in page_items(schema)}


def fields_by_key(schema: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {i["key"]: i for i in page_items(schema) if i["type"] == "field"}


def conditions_hold(conds: Optional[Iterable[Dict[str, Any]]], values: Dict[str, Any]) -> bool:
    """Whether every condition holds for ``values`` (an empty or missing list always holds)."""
    for cond in conds or ():
        have = values.get(cond["key"])
        if "eq" in cond and not (have == cond["eq"] and type(have) is type(cond["eq"])):
            return False
        if "ne" in cond and have == cond["ne"] and type(have) is type(cond["ne"]):
            return False
    return True


def is_shown(item: Dict[str, Any], values: Dict[str, Any]) -> bool:
    return conditions_hold(item.get("show_if"), values)


def is_enabled(item: Dict[str, Any], values: Dict[str, Any]) -> bool:
    return conditions_hold(item.get("enabled_if"), values)


def note_text(item: Dict[str, Any], values: Dict[str, Any]) -> str:
    """The wording of a note now: its first variant whose conditions hold, else its own text."""
    for variant in item.get("variants") or ():
        if conditions_hold(variant.get("if"), values):
            return variant["text"]
    return item.get("text", "")


# -- choices that depend on the computer ------------------------------------------------------------


def device_options(spec: Dict[str, Any], names: Iterable[str], saved: str) -> List[Tuple[str, str]]:
    """``(value, label)`` for a device field: Automatic first, then the connected devices; a saved device that
    is not connected stays, marked."""
    names = [str(n) for n in names]
    out = [("", spec["first_label"])]
    out += [(n, n) for n in names]
    if saved and saved not in names:
        out.append((saved, spec["missing_label"].format(name=saved)))
    return out


def note_type_options(
    spec: Dict[str, Any], types: List[Dict[str, str]], server_default: str, saved: str
) -> List[Tuple[str, str]]:
    """``(value, label)`` for the default note type: "Server default" (with the type's name when known), each
    known type; a saved id the list lacks stays so saving does not silently drop it."""
    names = {t["id"]: t["name"] for t in types}
    server_name = names.get(server_default)
    first = spec["first_label_named"].format(name=server_name) if server_name else spec["first_label"]
    out = [("", first)] + [(t["id"], t["name"]) for t in types]
    if saved and saved not in names:
        template = spec["missing_label"] if types else spec["missing_label_unknown"]
        out.append((saved, template.format(id=saved)))
    return out


def list_devices() -> Dict[str, List[str]]:
    """The connected devices, ``{"mic": [...], "system": [...]}`` (empty under tests and headless runs)."""
    if os.environ.get("MEETING_NOTES_NO_DEVICE_WATCH"):
        return {"mic": [], "system": []}  # tests and headless runs never touch the audio backend
    from meeting_notes.audio import devices as devices_mod

    return {kind: devices_mod.selectable_device_names(kind) for kind in ("mic", "system")}


# -- values ------------------------------------------------------------------------------------------


def get_values(data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Every config-backed field's current value, normalised the way the app reads it (flat, dotted keys).

    ``server.token`` is only ``True`` / ``False`` (is one set); the token itself never leaves the config."""
    data = config_mod.load_config() if data is None else data
    server = config_mod.server_settings(data)
    detection = config_mod.meeting_detection_settings(data)
    devices = config_mod.audio_device_settings(data)
    values = {
        "appearance": config_mod.appearance_setting(data),
        "meeting_detection.enabled": bool(detection["enabled"]),
        "meeting_detection.auto_record": bool(detection["auto_record"]),
        "meeting_detection.auto_end": detection["auto_end"],
        "meeting_detection.auto_stop": bool(detection["auto_stop"]),
        "meeting_detection.suggest_stop": bool(detection["suggest_stop"]),
        "default_note_type": config_mod.default_note_type_setting(data),
        "show_audio_levels": config_mod.idle_levels_enabled(data),
        "audio_devices.mic": devices["mic"],
        "audio_devices.system": devices["system"],
        "save_dir": str(config_mod.save_dir(data)),
        "local_retention_days": config_mod.local_retention_days(data),
        "delete_after_upload": config_mod.delete_after_upload(data),
        "server.url": str(server.get("url") or ""),
        "server.token": bool(server.get("token")),
        "server.live_preview": bool(server.get("live_preview", True)),
        "server.auto_upload": bool(server.get("auto_upload", True)),
        "server.check_updates": bool(server.get("check_updates", True)),
        "remote_control_allowed": config_mod.remote_control_allowed(data),
    }
    if sys.platform == "win32":
        helper = config_mod.gpu_helper_settings(data)
        values["gpu_helper.enabled"] = helper["enabled"]
        values["gpu_helper.start_at_login"] = helper["start_at_login"]
    return values


def stats_text(count: int, size_bytes: int) -> str:
    """The "In this folder" line: ``3 recordings, 1.2 GB``."""
    from meeting_notes.client.recordings import format_size

    return f"{count} recording{'s' if count != 1 else ''}, {format_size(size_bytes)}"


def read_only_values(data: Optional[Dict[str, Any]] = None, stats: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    """The values of the ``readonly`` fields (About, and the folder count when ``stats`` is given)."""
    from meeting_notes.client import logsetup

    data = config_mod.load_config() if data is None else data
    out = {
        "about.version": __version__,
        "about.save_dir": str(config_mod.save_dir(data)),
        "about.config": str(config_mod.config_path()),
        "about.logs": str(logsetup.log_dir()),
    }
    if sys.platform == "win32":
        from meeting_notes.client import gpu_helper, gpu_runtime

        out["gpu_helper.gpu"] = gpu_runtime.cached_gpu_text()
        out["gpu_helper.runtime"] = gpu_runtime.runtime_text()
        out["gpu_helper.status"] = gpu_helper.current_status_text()
    if stats is not None:
        out["recordings.stats"] = stats_text(int(stats.get("count", 0)), int(stats.get("bytes", 0)))
    return out


def merge_values(
    data: Dict[str, Any], values: Dict[str, Any], token: Optional[str] = None
) -> Dict[str, Any]:
    """A copy of config ``data`` with ``values`` (flat dotted keys, as :func:`get_values`) written the way the
    dialog saves them. Only the keys present are changed; ``token`` (not a ``values`` entry) replaces the saved
    sign-in token when given."""
    new = dict(data)
    present = set(values)

    server_keys = ("server.url", "server.live_preview", "server.auto_upload", "server.check_updates")
    if token is not None or present & set(server_keys):
        server = config_mod.server_settings(data)
        group = {
            "url": server["url"], "token": server["token"], "live_preview": server["live_preview"],
            "auto_upload": server["auto_upload"], "check_updates": server["check_updates"],
        }
        if "server.url" in values:
            group["url"] = str(values["server.url"]).strip().rstrip("/")
        for key in ("live_preview", "auto_upload", "check_updates"):
            if f"server.{key}" in values:
                group[key] = bool(values[f"server.{key}"])
        if token is not None:
            group["token"] = token
        new["server"] = group
        if group["token"]:
            new["password_prompted"] = True  # saved once: never open Settings on its own for this again

    detection_keys = [k for k in present if k.startswith("meeting_detection.")]
    if detection_keys:
        detection = config_mod.meeting_detection_settings(data)
        group = {
            "enabled": detection["enabled"], "auto_stop": detection["auto_stop"],
            "suggest_stop": detection["suggest_stop"], "auto_record": detection["auto_record"],
            "auto_end": detection["auto_end"], "end_grace_sec": detection["end_grace_sec"],
        }
        for key in detection_keys:
            name = key.split(".", 1)[1]
            if name == "auto_end":
                group[name] = values[key] or config_mod.DEFAULT_AUTO_END
            else:
                group[name] = bool(values[key])
        new["meeting_detection"] = group

    if "save_dir" in values:
        new["save_dir"] = str(values["save_dir"]).strip() or str(config_mod.DEFAULT_SAVE_DIR)
    if "default_note_type" in values:
        if values["default_note_type"]:
            new["default_note_type"] = values["default_note_type"]
        else:
            new.pop("default_note_type", None)  # "Server default"
    if "remote_control_allowed" in values:
        new["remote_control_allowed"] = bool(values["remote_control_allowed"])
    gpu_keys = [k for k in present if k.startswith("gpu_helper.")]
    if gpu_keys:
        group = dict(data.get("gpu_helper") if isinstance(data.get("gpu_helper"), dict) else {})
        for name, default in config_mod.GPU_HELPER_DEFAULTS.items():
            group.setdefault(name, default)
        for key in gpu_keys:
            group[key.split(".", 1)[1]] = bool(values[key])
        new["gpu_helper"] = group
    if "show_audio_levels" in values:
        new["show_audio_levels"] = bool(values["show_audio_levels"])
    if "audio_devices.mic" in values or "audio_devices.system" in values:
        current = config_mod.audio_device_settings(data)
        new["audio_devices"] = config_mod.audio_device_settings({"audio_devices": {
            "mic": values.get("audio_devices.mic", current["mic"]) or "",
            "system": values.get("audio_devices.system", current["system"]) or "",
        }})
    if "appearance" in values:
        new["appearance"] = values["appearance"] or "system"
    if "local_retention_days" in values:
        new["local_retention_days"] = int(values["local_retention_days"] or 0)
    if "delete_after_upload" in values:
        new["delete_after_upload"] = bool(values["delete_after_upload"])
    return new


def changed_keys(old: dict, new: dict) -> list:
    """Dotted names of settings that differ (never their values: some are secrets)."""
    changed = []
    for key in sorted(set(old) | set(new)):
        a, b = old.get(key), new.get(key)
        if isinstance(a, dict) or isinstance(b, dict):
            a, b = a or {}, b or {}
            for sub in sorted(set(a) | set(b)):
                if a.get(sub) != b.get(sub):
                    changed.append(f"{key}.{sub}")
        elif a != b:
            changed.append(key)
    return changed


def after_save(old: Dict[str, Any], data: Dict[str, Any], source: str = "dialog") -> None:
    """What every save does once the config is written: restyle the app, hide the token from the logs, log which
    settings changed and create the save folder."""
    try:
        from meeting_notes.client.ui import theme

        theme.apply_appearance(data.get("appearance") or "system")  # live, without a restart
    except Exception:  # noqa: BLE001 - a theme failure must never lose the save
        log.exception("could not apply the appearance setting")
    from meeting_notes.client import logsetup

    logsetup.register_secret(config_mod.server_settings(data).get("token") or "")
    log.info("settings saved (%s); changed: %s", source, ", ".join(changed_keys(old, data)) or "nothing")
    # Created now rather than at record time: a bad path should fail here, not thirty seconds into a meeting.
    try:
        config_mod.save_dir(data).mkdir(parents=True, exist_ok=True)
    except OSError:
        pass


# -- changes from the server ---------------------------------------------------------------------------


def save_dir_error(text: str, *, check_writable: bool = False) -> Optional[str]:
    """Why ``text`` may not hold recordings, else None. The dialog's rule (not inside the app folder, which an
    update replaces); with ``check_writable`` also that the folder exists or can be created and written."""
    from meeting_notes.client import paths

    text = text.strip()
    if not text:
        return None
    error = paths.validate_save_dir(text)
    if error or not check_writable:
        return error
    folder = Path(text).expanduser()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        if not folder.is_dir():
            return "That is not a folder."
        probe = folder / f".write-test-{os.getpid()}"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return f"Recordings can't be saved there: {exc.strerror or type(exc).__name__}."
    return None


def validate_changes(
    changes: Dict[str, Any],
    *,
    schema: Optional[Dict[str, Any]] = None,
    known_note_types: Optional[Iterable[str]] = None,
    current: Optional[Dict[str, Any]] = None,
    check_writable: bool = True,
) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """Check ``{key: value}`` the way the dialog's widgets constrain it; returns ``(clean, errors)``.

    A key the schema does not know, a ``secret`` or ``readonly`` field, a wrong type, a choice that is not offered, a
    note type this app does not know (``known_note_types``; the saved one is always allowed) or a save folder that may
    not hold recordings is an error for that key."""
    fields = fields_by_key(schema or build_schema())
    current = get_values() if current is None else current
    known = set(known_note_types or ())
    clean: Dict[str, Any] = {}
    errors: Dict[str, str] = {}
    for key, value in changes.items():
        spec = fields.get(key)
        if spec is None:
            errors[key] = "That is not a setting."
            continue
        kind = spec["kind"]
        if kind in ("secret", "readonly"):
            errors[key] = NOT_REMOTE
        elif kind == "bool":
            if isinstance(value, bool):
                clean[key] = value
            else:
                errors[key] = "Must be on or off."
        elif kind == "choice":
            if any(value == v and type(value) is type(v) for v, _label in spec["choices"]):
                clean[key] = value
            else:
                errors[key] = "That is not one of the choices."
        elif kind == "device":
            kind_name = spec["device_kind"]
            if not isinstance(value, str):
                errors[key] = "Must be a device name, or empty for Automatic."
                continue
            chosen = config_mod.audio_device_settings({"audio_devices": {kind_name: value}})[kind_name]
            if value.strip() and not chosen:
                errors[key] = "That is not a usable device name."
            else:
                clean[key] = chosen
        elif kind == "note_type":
            if not isinstance(value, str):
                errors[key] = "Must be a note type."
                continue
            value = value.strip()
            if value and not remote.valid_note_type(value):
                errors[key] = "That is not a note type."
            elif value and value not in known and value != current.get(key):
                errors[key] = "That note type is not known to this app."
            else:
                clean[key] = value
        elif kind == "path":
            if not isinstance(value, str) or "\n" in value or "\x00" in value or len(value) > MAX_PATH:
                errors[key] = "That is not a usable folder."
                continue
            problem = save_dir_error(value, check_writable=check_writable)
            if problem:
                errors[key] = problem
            else:
                clean[key] = value.strip() or str(config_mod.DEFAULT_SAVE_DIR)
        else:  # text
            limit = spec.get("max_len", 200)
            if not isinstance(value, str) or any(ord(c) < 32 for c in value) or len(value.strip()) > limit:
                errors[key] = "That is not usable text."
            else:
                clean[key] = value.strip().rstrip("/") if key == "server.url" else value.strip()
    return clean, errors


@dataclass
class ApplyResult:
    """What :func:`apply_changes` did. ``saved`` lists the keys that really changed; with any ``errors`` nothing is
    saved (like the dialog, which refuses to save a bad folder)."""

    saved: List[str] = field(default_factory=list)
    errors: Dict[str, str] = field(default_factory=dict)
    values: Dict[str, Any] = field(default_factory=dict)
    old: Dict[str, Any] = field(default_factory=dict)
    new: Dict[str, Any] = field(default_factory=dict)


def apply_changes(
    changes: Dict[str, Any],
    *,
    known_note_types: Optional[Iterable[str]] = None,
    check_writable: bool = True,
    side_effects: bool = True,
) -> ApplyResult:
    """Validate ``changes`` ({dotted key: value}) and write them to ``config.json`` exactly as the dialog's Save does
    (same normalisers, :func:`merge_values`, :func:`after_save`).

    The window then runs its own part of the dialog's accept (re-read detection, restart the uploader, ...); the
    caller does that when ``saved`` is not empty."""
    old = config_mod.load_config()
    current = get_values(old)
    clean, errors = validate_changes(
        changes, known_note_types=known_note_types, current=current, check_writable=check_writable
    )
    result = ApplyResult(errors=errors, values=current, old=old, new=old)
    if errors:
        return result
    differing = {k: v for k, v in clean.items() if current.get(k) != v}
    if not differing:
        return result
    new = merge_values(old, differing)
    config_mod.save_config(new)
    if side_effects:
        after_save(old, new, "server")
    result.saved = sorted(differing)
    result.new = new
    result.values = get_values(new)
    return result


def settings_payload(
    *,
    note_types: List[Dict[str, str]],
    server_default_note_type: str,
    devices: Dict[str, List[str]],
    stats: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    """The ``result`` of the ``get_settings`` remote command: the pages the server shows, every field's value
    (the sign-in token only as set / not set), the connected devices and the note types the app knows.

    Nothing secret is read into it: ``get_values`` reduces the token to a flag."""
    data = config_mod.load_config()
    if sys.platform == "win32":
        from meeting_notes.client import gpu_runtime

        gpu_runtime.cached_gpu(max_age=30.0)  # this runs on a worker thread; nvidia-smi may take a moment
    schema = remote_schema(build_schema(note_types_known=bool(note_types)))
    values = {**get_values(data), **read_only_values(data, stats or {"count": 0, "bytes": 0})}
    return {
        "schema": schema,
        "values": values,
        "devices": {kind: [str(n) for n in (devices or {}).get(kind) or []] for kind in ("mic", "system")},
        "note_types": [{"id": t["id"], "name": t["name"]} for t in note_types],
        "server_default_note_type": server_default_note_type or "",
    }
