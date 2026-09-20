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
* macOS has no OS-level loopback API at all. Asking ``soundcard`` for
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

import sys
from dataclasses import dataclass
from typing import List, Optional, Tuple

from . import soundcard_source

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


def _raw_microphones() -> List[Tuple[object, DeviceInfo]]:
    """Ordinary input devices, paired with their DeviceInfo. Never raises."""
    try:
        sc = soundcard_source.import_soundcard()
        mics = sc.all_microphones()
        try:
            default_id = sc.default_microphone().id
        except Exception:
            default_id = None
    except Exception:
        return []

    return [
        (m, _to_info(m, "mic", is_default=(getattr(m, "id", None) == default_id)))
        for m in mics
    ]


def _raw_system_sources() -> List[Tuple[object, DeviceInfo]]:
    """Loopback-capable devices, paired with their DeviceInfo. Never raises."""
    try:
        sc = soundcard_source.import_soundcard()
    except Exception:
        return []

    if sys.platform == "win32":
        try:
            candidates = sc.all_microphones(include_loopback=True)
        except Exception:
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
            return []
        found = []
        for m in mics:
            name_lower = str(m.name).lower()
            if any(hint in name_lower for hint in _MACOS_LOOPBACK_NAME_HINTS):
                found.append(
                    (m, _to_info(m, "system", is_default=False, note="virtual loopback driver"))
                )
        return found

    return []


def system_source_platform_note() -> str:
    """Why list_system_sources() is empty on a platform with no implementation.

    Empty string on Windows/macOS, where an empty result instead means "none
    detected" and gets its own message (see resolve_source / doctor).
    """
    if sys.platform in ("win32", "darwin"):
        return ""
    return (
        f"system-audio (loopback) capture is not implemented for platform "
        f"{sys.platform!r}; only Windows (WASAPI loopback) and macOS (via a "
        f"virtual audio driver such as BlackHole) are supported"
    )


def list_microphones() -> List[DeviceInfo]:
    return [info for _, info in _raw_microphones()]


def list_system_sources() -> List[DeviceInfo]:
    return [info for _, info in _raw_system_sources()]


def _no_system_source_message() -> str:
    if sys.platform == "darwin":
        return (
            "no system-audio source found. macOS has no built-in loopback API, "
            "so capturing what a meeting's other participants say requires a "
            "one-time (admin-required) install of a virtual audio driver: "
            "BlackHole 2ch -- https://existential.audio/blackhole/"
        )
    if sys.platform == "win32":
        return (
            "no loopback-capable output device found via "
            "soundcard.all_microphones(include_loopback=True). This is "
            "unexpected on Windows -- check that an output device is enabled."
        )
    return system_source_platform_note()


def resolve_source(
    kind: str,
    requested: Optional[str] = None,
    samplerate: Optional[int] = None,
) -> soundcard_source.SoundcardSource:
    """Resolve a mic or system-audio device by id or (substring, case-insensitive) name.

    ``requested`` of ``None`` picks the platform default: the OS default
    microphone for ``kind="mic"``, or the first detected loopback/virtual
    device for ``kind="system"`` (there is no OS-level notion of a "default"
    system-audio source).
    """
    if kind not in ("mic", "system"):
        raise ValueError(f"kind must be 'mic' or 'system', got {kind!r}")

    raw_pairs = _raw_microphones() if kind == "mic" else _raw_system_sources()

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
    return soundcard_source.SoundcardSource(
        raw,
        name=info.name,
        channels=info.channels,
        samplerate=int(samplerate or info.samplerate),
    )
