"""Remote-control protocol shared by the server and the recorder (like ``wire.py``).

Every running recorder opens one authenticated websocket to the server
(``CONNECT``) and keeps it open. Over it the recorder pushes a compact *state
snapshot* (what the window shows) and the server forwards *commands* typed in
the web UI's Recorders page (the same actions as the window's buttons). Nothing
here is persisted: when the socket closes the recorder is gone from the server
until it reconnects.

Messages are JSON text frames.

recorder -> server
    ``{"type": "hello", "protocol": 1, "instance_id": "<32 hex>", "device": "...",
       "platform": "Windows 11", "version": "0.7.6", "state": {...}}``  (first frame)
    ``{"type": "state", "state": {...}}``                               (full snapshot)
    ``{"type": "ack", "command_id": "...", "ok": true, "code": null,
       "error": null, "state": {...}, "result": {...}?}``               (after a command)
    ``{"type": "levels", "mic": 0.12, "system": 0.3}``                  (idle level preview, 0.7.7+; see below)
server -> recorder
    ``{"type": "welcome", "protocol": 1, "server_version": "..."}``
    ``{"type": "command", "command_id": "<hex>", "command": "start", "args": {...}}``
    ``{"type": "watch", "levels": true}``                               (idle level preview wanted, 0.7.7+)

Idle level preview (0.7.7+). While nobody is recording, the window can show live input levels
("Preview"). The Recorders page shows them too, but a recorder only streams them while a web viewer
has the page open and visible: the page tells the server (``{"type": "watch", "visible": bool}`` on
its events socket, repeated every ``VIEWER_BEAT_EVERY`` seconds, valid for ``VIEWER_TTL``), and the
server tells each recorder that advertised the ``idle_levels`` capability in its hello
(``"caps": ["idle_levels"]``) with a ``watch`` frame. A ``watch`` is a lease: the recorder stops
streaming ``WATCH_LEASE`` seconds after the last one unless it is renewed (the server renews it every
``WATCH_REFRESH`` seconds), so a dead server or page can never leave a microphone open. ``levels``
frames are throttled to ``LEVELS_EVERY`` and only sent while idle; a recording's levels travel in the
state snapshot. Servers and recorders that predate this ignore the frames and the ``caps`` field, and
a recorder is never sent a ``watch`` unless it advertised the capability.

Note types. A recorder that advertises ``note_type`` has a per-meeting note type picker (the types come from
the server's ``GET /v1/note-templates``); the chosen id is in its snapshot (``note_type``) and the Recorders page
changes it with ``set_note_type``. The id ends up in the meeting's ``meta.note_type`` at finalize, and the server
generates notes of that type for the meeting. A recorder that advertises ``auto_end`` reports an automatic end of
a recording in ``auto_end`` (``{mode, label}``) and accepts ``disable_auto_end``. The page only offers a control
for a recorder that advertised the matching capability (the Recorders list carries ``caps`` per recorder).

Close codes: 4401 unauthorized, 4400 bad hello / protocol, 4408 idle too long,
4409 replaced by a newer connection with the same instance id. A server without
this endpoint answers the handshake with HTTP 403 or 404 (unmatched route): the recorder gives up quietly
and only retries rarely.

``result`` is only sent by the recordings commands (``list_recordings``, ``reupload``,
``delete_local``; see ``RECORDING_COMMANDS``). It may be large, so an ack frame may be up
to ``MAX_ACK_FRAME_BYTES`` (every other frame stays under ``MAX_MESSAGE_BYTES``).

Both sides whitelist commands (``COMMANDS``); ``clean_command`` validates a name
and its arguments and is the only way either side should read them.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

PROTOCOL_VERSION = 1

CONNECT = "/v1/recorders/connect"          # recorder websocket (Bearer = the server token)
LIST = "/v1/recorders"                     # GET, web auth
EVENTS = "/v1/recorders/events"            # websocket, web auth: live feed for the page
COMMAND = "/v1/recorders/{instance_id}/commands"  # POST, web auth


def command_path(instance_id: str) -> str:
    return COMMAND.format(instance_id=instance_id)


CLOSE_UNAUTHORIZED = 4401
CLOSE_BAD_HELLO = 4400
CLOSE_IDLE = 4408
CLOSE_REPLACED = 4409

# Recorder-side send cadence (seconds): a change goes out at once (not faster
# than MIN_SEND_GAP); otherwise a refresh at least this often.
MIN_SEND_GAP = 0.25
SEND_EVERY_RECORDING = 1.0
SEND_EVERY_IDLE = 5.0
# Server drops a recorder that has been silent this long (seconds).
STALE_AFTER = 30.0
# Asking the recorder to do something: how long the HTTP call waits for the ack.
COMMAND_TIMEOUT = 5.0

MAX_MESSAGE_BYTES = 16 * 1024        # any single frame either direction, except:
MAX_COMMAND_BYTES = 64 * 1024        # a command frame (recorder receives; carries up to MAX_IDS ids)
MAX_RESULT_BYTES = 512 * 1024        # the ``result`` of a recordings command (compact JSON)
MAX_ACK_FRAME_BYTES = MAX_RESULT_BYTES + MAX_MESSAGE_BYTES  # an ack frame (recorder -> server)
MAX_RECORDINGS = 1000                # recordings listed per recorder (server stops paging here)
MAX_IDS = 100                        # session ids in one reupload / delete_local command
MAX_ID_LEN = 128                     # same cap as the server's session ids
MAX_TEXT = 200                       # meeting / device / banner text fields
MAX_BANNERS = 8

# Optional features a recorder advertises in its hello (``caps``). The server only sends a recorder
# the frames of a capability it advertised.
CAP_IDLE_LEVELS = "idle_levels"
CAP_NOTE_TYPE = "note_type"   # per-meeting note type picker (``note_type`` in the state, ``set_note_type``)
CAP_AUTO_END = "auto_end"     # reports an automatic end of the recording (``auto_end``), ``disable_auto_end``
CAPS = (CAP_IDLE_LEVELS, CAP_NOTE_TYPE, CAP_AUTO_END)
MAX_CAPS = 8

# Idle level preview timings (seconds); see the module docstring.
LEVELS_EVERY = 0.2            # recorder: at most one ``levels`` frame this often (~5 Hz)
LEVELS_MIN_GAP = 0.08         # server: ignores ``levels`` frames closer together than this
WATCH_LEASE = 25.0            # recorder: a ``watch`` frame stays valid this long
WATCH_REFRESH = 8.0           # server: renews a recorder's ``watch`` at least this often
VIEWER_BEAT_EVERY = 10.0      # page: how often it repeats "I am watching"
VIEWER_TTL = 30.0             # server: a viewer that has not beaten for this long is not watching

STATUSES = ("idle", "recording", "finishing")
AUTO_END_MODES = ("hour", "silence")
TRACKS = ("mic", "system")
BANNER_LEVELS = ("error", "warn", "info", "ok")
# Banner ids the recorder may report (``text`` is always the human wording).
BANNER_IDS = (
    "no_mic",               # no microphone connected
    "no_system",            # can't hear the meeting (no system audio)
    "device_lost",          # a device was lost mid-recording
    "device_back",          # a device came back (ok)
    "token_rejected",       # server rejected the token
    "server_unreachable",   # can't reach the server, recordings waiting
    "recordings_in_app_folder",
    "update_available",
    "unsupported_version",  # server says this version is too old
)

NOTE_TYPE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")   # a note type id (the server's are lowercase, up to 40)
_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_COMMAND_ID_RE = re.compile(r"^[0-9a-f]{8,64}$")


def clean_caps(raw: Any) -> Tuple[str, ...]:
    """The known capabilities in a hello's ``caps`` list (anything else is ignored)."""
    if not isinstance(raw, list):
        return ()
    found = []
    for item in raw[:MAX_CAPS]:
        if isinstance(item, str) and item in CAPS and item not in found:
            found.append(item)
    return tuple(found)


def sanitize_levels(raw: Any) -> Dict[str, float]:
    """The ``mic`` / ``system`` values of a ``levels`` frame, clamped to 0..1 (missing tracks omitted)."""
    src = raw if isinstance(raw, dict) else {}
    out: Dict[str, float] = {}
    for track in TRACKS:
        value = src.get(track)
        if value is None or isinstance(value, bool):
            continue
        out[track] = _unit(value)
    return out


def valid_instance_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_ID_RE.match(value))


def valid_note_type(value: Any) -> bool:
    """A note type id as the recorder and server both know it (letters, digits, ``_`` and ``-``)."""
    return isinstance(value, str) and bool(NOTE_TYPE_RE.match(value.strip()))


def valid_command_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_COMMAND_ID_RE.match(value))


# -- commands ---------------------------------------------------------------

# name -> {arg: kind}. kinds: "name" (optional text <= MAX_TEXT), "track" (mic|system, required),
# "offset" (optional int >= 0), "ids" (required list of 1..MAX_IDS session ids),
# "note_type" (required note type id, ``NOTE_TYPE_RE``).
COMMANDS: Dict[str, Dict[str, str]] = {
    "start": {"name": "name"},
    "stop": {},
    "mute": {"track": "track"},
    "unmute": {"track": "track"},
    "refresh_devices": {},
    "accept_call_prompt": {"name": "name"},
    "dismiss_call_prompt": {},
    "keep_recording": {},        # dismiss the stop suggestion / countdown, keep going
    "stop_suggested": {},        # accept the stop suggestion / countdown
    "retry_uploads": {},
    "check_update": {},
    "install_update": {},        # only while idle
    "set_name": {"name": "name"},
    "set_note_type": {"note_type": "note_type"},   # pick the note type for the meeting (idle or recording)
    "disable_auto_end": {},                         # turn off the automatic end of this recording
    # Recordings saved on the recorder (0.7.6+): list them, send them again, delete the local copy.
    "list_recordings": {"offset": "offset"},
    "reupload": {"session_ids": "ids"},
    "delete_local": {"session_ids": "ids"},
}

# Commands whose ack carries a ``result``, and how many times the normal command timeout the
# server waits for them (scanning a big save folder / moving folders to the Recycle Bin is slow).
RECORDING_COMMANDS = ("list_recordings", "reupload", "delete_local")
TIMEOUT_FACTOR = {"list_recordings": 4, "reupload": 4, "delete_local": 12}

# Stable machine-readable refusal codes used in acks.
ERROR_CODES = (
    "remote_control_disabled",
    "unknown_command",
    "bad_args",
    "busy",
    "already_recording",
    "not_recording",
    "recording_in_progress",
    "no_prompt",
    "no_suggestion",
    "no_auto_end",
    "no_update",
    "no_such_track",
    "no_save_folder",
    "failed",
)


_SESSION_ID_BAD = re.compile(r"[\\/\x00]")


def valid_session_id(value: Any) -> bool:
    """A recording folder name as the recorder and server both know it (never a path)."""
    return (
        isinstance(value, str)
        and 0 < len(value) <= MAX_ID_LEN
        and value not in (".", "..")
        and not _SESSION_ID_BAD.search(value)
    )


def clean_command(name: Any, args: Any) -> Tuple[str, Dict[str, Any]]:
    """Validate a command name and its arguments; returns ``(name, clean_args)``.

    Raises ``ValueError`` (message suitable for a 400) for an unknown command,
    unexpected/invalid arguments. Optional text args are stripped and capped; a
    blank optional ``name`` is dropped, ``set_name`` requires a non-blank name and
    ``set_note_type`` a valid note type id.
    """
    if not isinstance(name, str) or name not in COMMANDS:
        raise ValueError("unknown command")
    spec = COMMANDS[name]
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ValueError("args must be an object")
    extra = sorted(set(args) - set(spec))
    if extra:
        raise ValueError(f"unexpected argument: {extra[0]}")
    clean: Dict[str, Any] = {}
    for key, kind in spec.items():
        value = args.get(key)
        if kind == "ids":
            if not isinstance(value, list) or not value:
                raise ValueError("session_ids must be a non-empty list")
            if len(value) > MAX_IDS:
                raise ValueError(f"at most {MAX_IDS} session ids per command")
            seen = []
            for item in value:
                if not valid_session_id(item):
                    raise ValueError("invalid session id")
                if item not in seen:
                    seen.append(item)
            clean[key] = seen
        elif kind == "offset":
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 10**6:
                raise ValueError("offset must be a non-negative integer")
            if value:
                clean[key] = value
        elif kind == "track":
            if value not in TRACKS:
                raise ValueError("track must be 'mic' or 'system'")
            clean[key] = value
        elif kind == "note_type":
            if not valid_note_type(value):
                raise ValueError("note_type must be a note type id")
            clean[key] = value.strip()
        else:
            if value is None:
                continue
            if not isinstance(value, str):
                raise ValueError(f"{key} must be text")
            text = " ".join(value.split())[:MAX_TEXT]
            if text:
                clean[key] = text
    if name == "set_name" and "name" not in clean:
        raise ValueError("name is required")
    return name, clean


# -- state snapshot ---------------------------------------------------------


def _text(value: Any, limit: int = MAX_TEXT) -> str:
    return " ".join(str(value).split())[:limit] if value is not None else ""


def _opt_text(value: Any, limit: int = MAX_TEXT) -> Optional[str]:
    text = _text(value, limit)
    return text or None


def _unit(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number:  # NaN
        return 0.0
    return round(min(1.0, max(0.0, number)), 3)


def _count(value: Any) -> int:
    try:
        return max(0, min(10**6, int(value)))
    except (TypeError, ValueError):
        return 0


def _seconds(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number < 0:
        return None
    return round(min(number, 10**7), 1)


def sanitize_state(raw: Any) -> Dict[str, Any]:
    """Normalize whatever a recorder sent into the documented snapshot shape.

    Unknown keys are dropped, strings capped, numbers clamped, so the server
    never stores or forwards more than the contract allows. Always returns a
    complete dict (missing parts take their idle defaults).

    Shape::

        status          "idle" | "recording" | "finishing"
        meeting         {name, session_id|None, elapsed_sec|None}
        tracks          {mic|system: {device|None, connected, muted, level 0..1, peak 0..1, degraded}}
        banners         [{id, level, text}]                (<= MAX_BANNERS)
        update          {available, version|None, installing}
        uploads         {pending, failed, awaiting_transcript, current_percent|None, state|None}
        call            {prompt: {label, name}|None, active_app|None}
        suggestion      {kind, title, seconds_left|None}|None   (stop suggestion / end countdown)
        control         {allowed}
        stream          live-preview state text or None
        note_type       the note type id picked for the meeting (``note_type`` capability), or None
        auto_end        {mode: "hour"|"silence"|None, label|None}   the automatic end of this recording
                        (``auto_end`` capability): ``mode`` is None, with no label, unless the recorder
                        shows its "Auto end at ..." strip; ``label`` is that strip's text
        preview         {supported, active, tracks}   idle level preview (0.7.7+): ``supported`` is false for
                        an older recorder or when the person turned it off; ``active`` while it is metering;
                        ``tracks`` the ones it can meter (macOS: the microphone only)
    """
    src = raw if isinstance(raw, dict) else {}
    status = src.get("status")
    meeting_src = src.get("meeting") if isinstance(src.get("meeting"), dict) else {}
    tracks_src = src.get("tracks") if isinstance(src.get("tracks"), dict) else {}
    tracks: Dict[str, Dict[str, Any]] = {}
    for track in TRACKS:
        t = tracks_src.get(track) if isinstance(tracks_src.get(track), dict) else {}
        tracks[track] = {
            "device": _opt_text(t.get("device")),
            "connected": bool(t.get("connected")),
            "muted": bool(t.get("muted")),
            "level": _unit(t.get("level")),
            "peak": _unit(t.get("peak")),
            "degraded": bool(t.get("degraded")),
        }
    banners = []
    banners_src = src.get("banners") if isinstance(src.get("banners"), list) else []
    for item in banners_src[:MAX_BANNERS]:
        if not isinstance(item, dict):
            continue
        banner_id = item.get("id")
        if banner_id not in BANNER_IDS:
            continue
        level = item.get("level") if item.get("level") in BANNER_LEVELS else "info"
        banners.append({"id": banner_id, "level": level, "text": _text(item.get("text"), 300)})
    update_src = src.get("update") if isinstance(src.get("update"), dict) else {}
    uploads_src = src.get("uploads") if isinstance(src.get("uploads"), dict) else {}
    percent = uploads_src.get("current_percent")
    try:
        percent = None if percent is None else round(min(100.0, max(0.0, float(percent))), 1)
    except (TypeError, ValueError):
        percent = None
    call_src = src.get("call") if isinstance(src.get("call"), dict) else {}
    prompt_src = call_src.get("prompt") if isinstance(call_src.get("prompt"), dict) else None
    suggestion_src = src.get("suggestion") if isinstance(src.get("suggestion"), dict) else None
    control_src = src.get("control") if isinstance(src.get("control"), dict) else {}
    preview_src = src.get("preview") if isinstance(src.get("preview"), dict) else {}
    preview_tracks = preview_src.get("tracks") if isinstance(preview_src.get("tracks"), list) else list(TRACKS)
    auto_src = src.get("auto_end") if isinstance(src.get("auto_end"), dict) else {}
    auto_mode = auto_src.get("mode") if auto_src.get("mode") in AUTO_END_MODES else None
    note_type = src.get("note_type")
    return {
        "status": status if status in STATUSES else "idle",
        "meeting": {
            "name": _text(meeting_src.get("name")),
            "session_id": _opt_text(meeting_src.get("session_id"), 120),
            "elapsed_sec": _seconds(meeting_src.get("elapsed_sec")),
        },
        "tracks": tracks,
        "banners": banners,
        "update": {
            "available": bool(update_src.get("available")),
            "version": _opt_text(update_src.get("version"), 32),
            "installing": bool(update_src.get("installing")),
        },
        "uploads": {
            "pending": _count(uploads_src.get("pending")),
            "failed": _count(uploads_src.get("failed")),
            "awaiting_transcript": _count(uploads_src.get("awaiting_transcript")),
            "current_percent": percent,
            "state": _opt_text(uploads_src.get("state"), 40),
        },
        "call": {
            "prompt": (
                {"label": _text(prompt_src.get("label"), 60), "name": _text(prompt_src.get("name"))}
                if prompt_src is not None
                else None
            ),
            "active_app": _opt_text(call_src.get("active_app"), 60),
        },
        "suggestion": (
            {
                "kind": _text(suggestion_src.get("kind"), 20),
                "title": _text(suggestion_src.get("title")),
                "seconds_left": (
                    _count(suggestion_src["seconds_left"])
                    if suggestion_src.get("seconds_left") is not None
                    else None
                ),
            }
            if suggestion_src is not None
            else None
        ),
        "control": {"allowed": bool(control_src.get("allowed", True))},
        "stream": _opt_text(src.get("stream"), 40),
        "note_type": note_type.strip() if valid_note_type(note_type) else None,
        "auto_end": {
            "mode": auto_mode,
            "label": _opt_text(auto_src.get("label"), 80) if auto_mode else None,
        },
        "preview": {
            "supported": preview_src.get("supported") is True,
            "active": preview_src.get("active") is True,
            "tracks": [t for t in TRACKS if t in preview_tracks],
        },
    }


def platform_family(text: Any) -> str:
    """``"Windows 11"`` -> ``"windows"``, ``"macOS 26"``/``"Darwin 25"`` -> ``"macos"``, else ``"other"``."""
    value = str(text or "").lower()
    if value.startswith("win"):
        return "windows"
    if value.startswith(("mac", "darwin")):
        return "macos"
    return "other"


# Darwin kernel major -> macOS marketing major (Apple jumped from 15 to 26).
_DARWIN_TO_MACOS = {20: "11", 21: "12", 22: "13", 23: "14", 24: "15", 25: "26"}


def friendly_platform(text: Any) -> str:
    """Show ``"Darwin 25.0.0"`` (what older macOS recorders send) as ``"macOS 26"``.

    Anything else (``"Windows 11"``, ``"macOS 26.6"``, ``"Linux 6.8"``) passes through. A Darwin
    major outside the table becomes plain ``"macOS"``.
    """
    value = str(text or "").strip()
    match = re.match(r"^darwin\b\s*(\d+)?", value, re.IGNORECASE)
    if not match:
        return value
    mapped = _DARWIN_TO_MACOS.get(int(match.group(1))) if match.group(1) else None
    return f"macOS {mapped}" if mapped else "macOS"


# -- recordings command results ---------------------------------------------

LOCAL_QUEUE_STATES = ("not_queued", "pending", "uploading", "failed", "awaiting_transcript", "recording")
RESULT_CODES = ("active_recording", "uploading", "not_found", "invalid", "failed")


def _epoch(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number < 0 or number > 10**11:
        return None
    return round(number, 1)


def _size(value: Any) -> int:
    try:
        return max(0, min(10**13, int(value)))
    except (TypeError, ValueError):
        return 0


def sanitize_recording(raw: Any) -> Optional[Dict[str, Any]]:
    """One row of a ``list_recordings`` result, normalized; ``None`` if it has no usable id.

    Shape: ``session_id, name, started (epoch), duration_sec|None, size_bytes, valid, reason|None,
    active, queue {state, percent|None, error|None, attempts}``.
    """
    src = raw if isinstance(raw, dict) else {}
    session_id = src.get("session_id")
    if not valid_session_id(session_id):
        return None
    queue_src = src.get("queue") if isinstance(src.get("queue"), dict) else {}
    qstate = queue_src.get("state")
    percent = queue_src.get("percent")
    try:
        percent = None if percent is None else round(min(100.0, max(0.0, float(percent))), 1)
    except (TypeError, ValueError):
        percent = None
    return {
        "session_id": session_id,
        "name": _text(src.get("name"), MAX_TEXT) or session_id,
        "started": _epoch(src.get("started")),
        "duration_sec": _seconds(src.get("duration_sec")),
        "size_bytes": _size(src.get("size_bytes")),
        "valid": bool(src.get("valid")),
        "reason": _opt_text(src.get("reason"), 200),
        "active": bool(src.get("active")),
        "queue": {
            "state": qstate if qstate in LOCAL_QUEUE_STATES else "not_queued",
            "percent": percent,
            "error": _opt_text(queue_src.get("error"), 200),
            "attempts": _count(queue_src.get("attempts")),
        },
    }


def _sanitize_outcomes(raw: Any, extra: Tuple[str, ...] = ()) -> list:
    out = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict) or not valid_session_id(item.get("session_id")):
            continue
        code = item.get("code")
        entry: Dict[str, Any] = {
            "session_id": item["session_id"],
            "ok": item.get("ok") is True,
            "code": code if code in RESULT_CODES else None,
            "error": _opt_text(item.get("error"), 300),
        }
        for key in extra:
            if key == "bytes":
                entry[key] = _size(item.get(key))
            else:
                entry[key] = _opt_text(item.get(key), 20)
        out.append(entry)
        if len(out) >= MAX_IDS:
            break
    return out


def sanitize_result(command: str, raw: Any) -> Optional[Dict[str, Any]]:
    """Normalize the ``result`` of a recordings command's ack; ``None`` for other commands.

    ``list_recordings`` -> ``{recordings: [...], total, offset, next_offset|None}``;
    ``reupload`` -> ``{results: [{session_id, ok, code, error}], queued, already_queued}``;
    ``delete_local`` -> ``{results: [{..., bytes, method}], deleted, freed_bytes}``.
    """
    if command not in RECORDING_COMMANDS:
        return None
    src = raw if isinstance(raw, dict) else {}
    if command == "list_recordings":
        rows = []
        for item in src.get("recordings") if isinstance(src.get("recordings"), list) else []:
            row = sanitize_recording(item)
            if row is not None:
                rows.append(row)
        next_offset = src.get("next_offset")
        return {
            "recordings": rows,
            "total": _count(src.get("total")),
            "offset": _count(src.get("offset")),
            "next_offset": _count(next_offset) if next_offset is not None else None,
        }
    if command == "reupload":
        return {
            "results": _sanitize_outcomes(src.get("results")),
            "queued": _count(src.get("queued")),
            "already_queued": _count(src.get("already_queued")),
        }
    return {
        "results": _sanitize_outcomes(src.get("results"), ("bytes", "method")),
        "deleted": _count(src.get("deleted")),
        "freed_bytes": _size(src.get("freed_bytes")),
    }
