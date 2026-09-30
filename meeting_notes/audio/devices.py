"""Platform-aware discovery of microphones and system-audio sources.

Nothing here imports ``soundcard`` directly -- that import lives solely in
``soundcard_source`` (see its docstring), and this module reaches it through
``soundcard_source.import_soundcard()`` so it still loads cleanly on a
machine without the backend installed.

"System audio" (what a meeting's remote participants are saying, as opposed
to what the local microphone hears) is not a single concept across
platforms, which is why ``list_system_sources`` is split by ``sys.platform``
rather than written once:

* Windows can loop back *any* output device through WASAPI, with no driver
  and no admin rights, by asking ``soundcard`` for microphones with
  ``include_loopback=True`` -- the output devices come back wrapped as
  loopback-capable "microphones".
* macOS 13+ can capture the system-output mix through ScreenCaptureKit, with
  no driver and no admin (``screencapture_source``). That is listed first
  whenever PyObjC's ScreenCaptureKit bindings are importable. It needs the
  "Screen & System Audio Recording" permission; when that has not been
  granted, and a BlackHole-style virtual driver is installed, the driver is
  offered first so recording still works.
* Older macOS, or a Mac that denied the permission, falls back to the driver
  route below. macOS has no OS-level loopback *device* API at all. Asking ``soundcard`` for
  ``include_loopback=True`` there does not fail -- it just emits a warning
  ("macOS does not support loopback recording functionality") and silently
  hands back the same ordinary input devices, which would make a caller
  believe loopback is working when it is not. So we never pass that flag on
  darwin. Instead we scan ordinary input devices for the handful of virtual
  audio drivers people install for exactly this purpose (BlackHole,
  Soundflower, ...): if one is installed, it shows up as a normal
  microphone whose name gives it away.
* Anywhere else, there is no implementation, and we say so rather than
  pretending.
"""

from __future__ import annotations

import os
import platform
import sys
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import List, Optional, Tuple

from . import screencapture_source, soundcard_source

# soundcard resamples to whatever rate the caller requests when opening a
# recorder -- devices don't expose a fixed "native" rate the way channel
# count is fixed by the hardware. This is the project-wide default used to
# populate DeviceInfo.samplerate and to open sources when none is specified.
_DEFAULT_SAMPLERATE = 48000

# Virtual loopback drivers people install on macOS specifically so
# soundcard's ordinary input-device enumeration also sees a capture of
# whatever the system is playing.
_MACOS_LOOPBACK_NAME_HINTS = (
    "blackhole",
    "soundflower",
    "loopback audio",
    "existential audio",
)


# Sentinel "raw device" for the ScreenCaptureKit entry (there is no soundcard
# object behind it), plus the id/name it is listed under.
_SCK_RAW = object()
SCK_DEVICE_ID = "screencapturekit"

_permission_requested = False


@dataclass
class DeviceInfo:
    id: str
    name: str
    kind: str  # "mic" | "system"
    channels: int
    samplerate: int
    is_default: bool
    note: str = ""


class DeviceNotFound(Exception):
    """Raised by resolve_source() when nothing matches, with a listing attached."""


class DeviceDiscoveryError(DeviceNotFound):
    """A backend/OS error while enumerating devices."""

    def __init__(self, kind: str, cause: BaseException):
        self.kind = kind
        self.cause = cause
        super().__init__(f"could not enumerate {kind} devices: {type(cause).__name__}: {cause}")


def _rdp_session_hint() -> str:
    """Return an actionable hint when Windows is running inside Remote Desktop."""
    if sys.platform != "win32":
        return ""
    session = os.environ.get("SESSIONNAME", "")
    client = os.environ.get("CLIENTNAME", "")
    if session.upper().startswith("RDP-") or client:
        return (
            " This appears to be a Remote Desktop session; reconnect with "
            "Remote audio > Settings > Record from this computer enabled, or "
            "run Meeting Notes from the physical Windows session."
        )
    return ""


def audio_diagnostic_report() -> str:
    """Collect a safe, detailed audio-backend report for support.

    The report contains device names/ids and exception types only. It never
    reads configuration, tokens, recordings, or audio content. Enumeration
    is intentionally performed directly so a packaged-backend failure is
    visible instead of being converted into an empty device list.
    """
    lines = [
        "Meeting Notes audio diagnostic",
        f"timestamp_utc={datetime.now(timezone.utc).isoformat()}",
        f"platform={platform.platform()}",
        f"python={platform.python_version()}",
        f"rdp_session={bool(os.environ.get('SESSIONNAME', '').upper().startswith('RDP-') or os.environ.get('CLIENTNAME'))}",
    ]
    try:
        sc = soundcard_source.import_soundcard()
    except Exception as exc:  # noqa: BLE001 - exact backend error is the report
        lines.append(f"soundcard_import=ERROR {type(exc).__name__}: {exc!r}")
        return "\n".join(lines) + "\n"

    try:
        try:
            import importlib.metadata
            version = importlib.metadata.version("soundcard")
        except Exception:
            version = "unknown"
        lines.append(f"soundcard_import=OK version={version}")
    except Exception as exc:  # pragma: no cover - defensive only
        lines.append(f"soundcard_version=ERROR {type(exc).__name__}: {exc!r}")

    if sys.platform == "darwin":
        ok, why = screencapture_source.available()
        lines.append(f"screencapturekit_available={ok}{'' if ok else ' (' + why + ')'}")
        lines.append(f"screen_recording_permission={screencapture_source.permission_granted()}")
    for label, call in (
        ("microphones", lambda: sc.all_microphones()),
        ("microphones_loopback", lambda: sc.all_microphones(include_loopback=True)),
        ("speakers", lambda: sc.all_speakers()),
        ("default_microphone", lambda: sc.default_microphone()),
        ("default_speaker", lambda: sc.default_speaker()),
    ):
        try:
            value = call()
            if isinstance(value, (list, tuple)):
                rendered = ", ".join(f"{getattr(item, 'name', item)!r}" for item in value)
            else:
                rendered = repr(getattr(value, "name", value))
            lines.append(f"{label}=OK {rendered}")
        except Exception as exc:  # noqa: BLE001 - exact backend error is the report
            lines.append(f"{label}=ERROR {type(exc).__name__}: {exc!r}")
    return "\n".join(lines) + "\n"


def _channel_count(dev) -> int:
    """Normalize soundcard's ``.channels`` (an int on most backends, but not
    guaranteed) into a plain count."""
    ch = getattr(dev, "channels", None)
    if isinstance(ch, int) and ch > 0:
        return ch
    try:
        n = len(ch)  # some backends report a channel-index list instead
        if n > 0:
            return n
    except TypeError:
        pass
    return 2  # unknown -- assume stereo rather than silently recording mono


def _to_info(dev, kind: str, *, is_default: bool, note: str = "") -> DeviceInfo:
    return DeviceInfo(
        id=str(getattr(dev, "id", getattr(dev, "name", ""))),
        name=str(dev.name),
        kind=kind,
        channels=_channel_count(dev),
        samplerate=_DEFAULT_SAMPLERATE,
        is_default=is_default,
        note=note,
    )


def _raw_microphones(*, raise_errors: bool = False) -> List[Tuple[object, DeviceInfo]]:
    """Ordinary input devices, paired with their DeviceInfo.

    Listing helpers retain soft-failure behavior. Source resolution asks for
    errors so COM/WASAPI failures are not misreported as missing hardware.
    """
    try:
        sc = soundcard_source.import_soundcard()
        mics = sc.all_microphones()
    except Exception as exc:
        if raise_errors:
            raise DeviceDiscoveryError("microphone", exc) from exc
        return []

    if not mics and raise_errors:
        if sys.platform == "win32":
            raise DeviceNotFound(
                "no microphones were returned by Windows WASAPI."
                + _rdp_session_hint()
                + " Check that a microphone is enabled and that Windows microphone "
                "privacy allows desktop apps."
            )
        raise DeviceNotFound("no microphones were found by the audio backend")

    try:
        default_id = sc.default_microphone().id
    except Exception:
        # A usable non-default endpoint is still better than no recording.
        # The exact default-endpoint error remains in the diagnostic report.
        default_id = None

    return [
        (m, _to_info(m, "mic", is_default=(getattr(m, "id", None) == default_id)))
        for m in mics
    ]


def _raw_system_sources(*, raise_errors: bool = False) -> List[Tuple[object, DeviceInfo]]:
    """Loopback-capable devices, paired with their DeviceInfo."""
    try:
        sc = soundcard_source.import_soundcard()
    except Exception as exc:
        if raise_errors:
            raise DeviceDiscoveryError("system-audio", exc) from exc
        return []

    if sys.platform == "win32":
        try:
            candidates = sc.all_microphones(include_loopback=True)
        except Exception as exc:
            if raise_errors:
                raise DeviceDiscoveryError("system-audio", exc) from exc
            return []
        # Loopback entries are the wrapped output devices among the
        # results; soundcard marks them with `.isloopback`. Fall back to
        # "everything returned" only if that attribute is absent entirely
        # (older soundcard), since asking with include_loopback=True at all
        # means the caller wants loopback devices specifically.
        loopback = [m for m in candidates if getattr(m, "isloopback", False)]
        if not loopback and candidates and not hasattr(candidates[0], "isloopback"):
            loopback = candidates
        try:
            default_speaker_name = sc.default_speaker().name
        except Exception:
            # Loopback candidates remain usable even when Windows has no
            # declared default speaker. Diagnostics retain the exact error.
            default_speaker_name = None
        return [
            (
                m,
                _to_info(
                    m,
                    "system",
                    is_default=(default_speaker_name is not None and m.name == default_speaker_name),
                    note="WASAPI loopback of an output device",
                ),
            )
            for m in loopback
        ]

    if sys.platform == "darwin":
        # NEVER pass include_loopback=True here -- see module docstring.
        try:
            mics = sc.all_microphones()
        except Exception:
            mics = []
        drivers = []
        for m in mics:
            name_lower = str(m.name).lower()
            if any(hint in name_lower for hint in _MACOS_LOOPBACK_NAME_HINTS):
                drivers.append(
                    (m, _to_info(m, "system", is_default=False, note="virtual loopback driver"))
                )
        return _with_screencapturekit(drivers)

    return []


def _with_screencapturekit(drivers: List[Tuple[object, DeviceInfo]]) -> List[Tuple[object, DeviceInfo]]:
    """Put the ScreenCaptureKit source in front of any virtual-driver sources."""
    ok, _why = screencapture_source.available()
    if not ok:
        return drivers
    info = DeviceInfo(
        id=SCK_DEVICE_ID,
        name=screencapture_source.SOURCE_NAME,
        kind="system",
        channels=screencapture_source.CHANNELS,
        samplerate=screencapture_source.SAMPLE_RATE,
        is_default=True,
        note="no driver needed; requires Screen & System Audio Recording permission",
    )
    entry = (_SCK_RAW, info)
    if drivers and screencapture_source.permission_granted() is False:
        # Not allowed (yet): a driver that is already installed still records.
        first = drivers[0]
        drivers[0] = (first[0], DeviceInfo(**{**first[1].__dict__, "is_default": True}))
        return drivers + [(_SCK_RAW, DeviceInfo(**{**info.__dict__, "is_default": False}))]
    return [entry] + drivers


def system_source_platform_note() -> str:
    """Why list_system_sources() is empty on a platform with no implementation.

    Empty string on Windows/macOS, where an empty result instead means "none
    detected" and gets its own message (see resolve_source / doctor).
    """
    if sys.platform in ("win32", "darwin"):
        return ""
    return (
        f"system-audio (loopback) capture is not implemented for platform "
        f"{sys.platform!r}; only Windows (WASAPI loopback) and macOS "
        f"(ScreenCaptureKit, or a virtual audio driver such as BlackHole) are supported"
    )


def list_microphones() -> List[DeviceInfo]:
    return [info for _, info in _raw_microphones()]


def list_system_sources() -> List[DeviceInfo]:
    return [info for _, info in _raw_system_sources()]


def _no_system_source_message() -> str:
    if sys.platform == "darwin":
        _ok, why = screencapture_source.available()
        return (
            "no system-audio source found. On macOS 13 or newer Meeting Notes "
            "captures system audio through ScreenCaptureKit, which is not "
            f"available here ({why or 'unknown reason'}). Older macOS has no "
            "built-in loopback API, so capturing what a meeting's other "
            "participants say needs a one-time (admin-required) install of a "
            "virtual audio driver: BlackHole 2ch -- https://existential.audio/blackhole/"
        )
    if sys.platform == "win32":
        return (
            "no loopback-capable output device found via "
            "soundcard.all_microphones(include_loopback=True). This is "
            "unexpected on Windows -- check that an output device is enabled."
            + _rdp_session_hint()
        )
    return system_source_platform_note()


def resolve_source(
    kind: str,
    requested: Optional[str] = None,
    samplerate: Optional[int] = None,
    *,
    interactive: bool = False,
):
    """Resolve a mic or system-audio device by id or (substring, case-insensitive) name.

    ``requested`` of ``None`` picks the platform default: the OS default
    microphone for ``kind="mic"``, or the first detected loopback/virtual
    device for ``kind="system"`` (there is no OS-level notion of a "default"
    system-audio source).
    """
    if kind not in ("mic", "system"):
        raise ValueError(f"kind must be 'mic' or 'system', got {kind!r}")

    raw_pairs = (
        _raw_microphones(raise_errors=True)
        if kind == "mic"
        else _raw_system_sources(raise_errors=True)
    )

    if not raw_pairs:
        if kind == "system":
            raise DeviceNotFound(_no_system_source_message())
        raise DeviceNotFound(
            "no microphones were found by soundcard.all_microphones() -- is a "
            "microphone connected and enabled?"
        )

    chosen: Optional[Tuple[object, DeviceInfo]] = None
    if requested is None:
        chosen = next((p for p in raw_pairs if p[1].is_default), raw_pairs[0])
    else:
        chosen = next((p for p in raw_pairs if p[1].id == requested), None)
        if chosen is None:
            needle = requested.lower()
            chosen = next((p for p in raw_pairs if needle in p[1].name.lower()), None)
        if chosen is None:
            available = "\n".join(f"  - {info.id!r}: {info.name!r}" for _, info in raw_pairs)
            raise DeviceNotFound(
                f"no {kind} device matching {requested!r}. Available {kind} devices:\n{available}"
            )

    raw, info = chosen
    if (
        raw is not _SCK_RAW
        and kind == "system"
        and interactive
        and sys.platform == "darwin"
        and not _permission_requested
        and screencapture_source.available()[0]
        and screencapture_source.permission_granted() is False
    ):
        # Recording proceeds via the installed driver this time; still show the
        # system prompt once so ScreenCaptureKit works from the next recording.
        globals()["_permission_requested"] = True
        screencapture_source.request_permission()
    if raw is _SCK_RAW:
        return _resolve_screencapturekit(info, samplerate, interactive=interactive)
    return soundcard_source.SoundcardSource(
        raw,
        name=info.name,
        channels=info.channels,
        samplerate=int(samplerate or info.samplerate),
    )


def _resolve_screencapturekit(info: DeviceInfo, samplerate: Optional[int], *, interactive: bool):
    """The ScreenCaptureKit source, or a clear permission error."""
    global _permission_requested
    if screencapture_source.permission_granted() is False:
        if interactive and not _permission_requested:
            _permission_requested = True
            screencapture_source.request_permission()
        raise DeviceNotFound(screencapture_source.PERMISSION_MESSAGE)
    return screencapture_source.ScreenCaptureKitSource(
        samplerate=int(samplerate or info.samplerate), channels=info.channels
    )
