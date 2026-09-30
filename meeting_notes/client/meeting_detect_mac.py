"""macOS probes for :mod:`meeting_notes.client.meeting_detect`.

The detector's state machine is platform-neutral and already takes injected
probes; this module supplies the two macOS ones (and their pure helpers):

* **Who is using the microphone.** Windows records that per app in the registry;
  macOS does not, but CoreAudio (14.2+) exposes one *process object* per client
  of the audio system, with ``kAudioProcessPropertyIsRunningInput`` and the
  process's bundle id. That is exact, and it lets us ignore Meeting Notes' own
  capture (which would otherwise keep "the mic in use" for the whole recording
  and the call could never be seen to end). On macOS 13 - 14.1 the process list
  does not exist; we fall back to the device-level
  ``kAudioDevicePropertyDeviceIsRunningSomewhere`` on input devices, which says
  *that* a mic is live but not who, so every running meeting app is reported as
  a candidate (and, while we are recording ourselves, the mic is always live so
  the call is never auto-ended on those old versions).
* **Window titles** via ``CGWindowListCopyWindowInfo``. Titles need the Screen &
  System Audio Recording permission (the same one system audio uses). Without
  it the windows still list, just with empty titles, so detection degrades to
  app-name-only names such as "Zoom call 2:30 PM" instead of failing.

Everything that touches CoreAudio / Quartz / AppKit is behind small callables
so the logic is testable on any OS with fakes. Nothing here is imported on
Windows or Linux unless a test asks for it.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

from meeting_notes.client.meeting_detect import MicUse, classify

log = logging.getLogger("meeting_notes.client.meeting_detect_mac")


@dataclass(frozen=True)
class AudioProcess:
    """One CoreAudio client process."""

    pid: int
    bundle_id: str
    running_input: bool


# -- pure logic ------------------------------------------------------------------------


def mic_usage_from_processes(
    processes: Iterable[AudioProcess], own_pid: Optional[int] = None
) -> List[MicUse]:
    """MicUse entries for processes that are recording, minus ourselves."""
    own = os.getpid() if own_pid is None else own_pid
    uses: List[MicUse] = []
    seen = set()
    for proc in processes:
        if proc.pid == own or not proc.bundle_id:
            continue
        key = proc.bundle_id.lower()
        if key in seen:
            continue
        seen.add(key)
        uses.append(MicUse(key=proc.bundle_id, exe_path="", exe_name=key, in_use=bool(proc.running_input)))
    return uses


def mic_usage_from_device_state(input_running: bool, running_bundle_ids: Iterable[str]) -> List[MicUse]:
    """Fallback (no per-process info): a live mic is attributed to every meeting app that is running."""
    uses: List[MicUse] = []
    seen = set()
    for bundle in running_bundle_ids:
        key = (bundle or "").lower()
        if not key or key in seen or classify(key) is None:
            continue
        seen.add(key)
        uses.append(MicUse(key=bundle, exe_path="", exe_name=key, in_use=input_running))
    return uses


def windows_to_titles(
    windows: Iterable[dict], bundle_for_pid: Callable[[int], str]
) -> List[Tuple[str, str]]:
    """(bundle id lowercased, title) for normal on-screen windows.

    ``windows`` are ``CGWindowListCopyWindowInfo`` dictionaries. Empty titles
    (Screen Recording not granted) are kept: the detector still needs to know an
    app has a window even when it cannot read what it says.
    """
    found: List[Tuple[str, str]] = []
    cache: dict = {}
    for window in windows:
        try:
            if int(window.get("kCGWindowLayer", 0) or 0) != 0:
                continue  # menu bar items, overlays, the Dock
            pid = int(window.get("kCGWindowOwnerPID", 0) or 0)
            if not pid:
                continue
            if pid not in cache:
                cache[pid] = (bundle_for_pid(pid) or "").lower()
            bundle = cache[pid]
            if not bundle:
                continue
            title = str(window.get("kCGWindowName", "") or "").strip()
            found.append((bundle, title))
        except Exception:  # noqa: BLE001 - one odd window must not break the walk
            continue
    return found


# -- CoreAudio via ctypes ---------------------------------------------------------------------


def _fourcc(text: str) -> int:
    return int.from_bytes(text.encode("ascii"), "big")


_SYSTEM_OBJECT = 1
_SCOPE_GLOBAL = _fourcc("glob")
_SCOPE_INPUT = _fourcc("inpt")
_ELEMENT_MAIN = 0
_SEL_PROCESS_LIST = _fourcc("prs#")  # kAudioHardwarePropertyProcessObjectList
_SEL_PROCESS_PID = _fourcc("ppid")  # kAudioProcessPropertyPID
_SEL_PROCESS_BUNDLE = _fourcc("pbid")  # kAudioProcessPropertyBundleID
_SEL_PROCESS_INPUT = _fourcc("piri")  # kAudioProcessPropertyIsRunningInput
_SEL_DEVICES = _fourcc("dev#")  # kAudioHardwarePropertyDevices
_SEL_STREAMS = _fourcc("stm#")  # kAudioDevicePropertyStreams
_SEL_RUNNING_SOMEWHERE = _fourcc("gone")  # kAudioDevicePropertyDeviceIsRunningSomewhere
_UTF8 = 0x08000100


class _Address(ctypes.Structure):
    _fields_ = [("selector", ctypes.c_uint32), ("scope", ctypes.c_uint32), ("element", ctypes.c_uint32)]


class CoreAudioReader:
    """Thin ctypes wrapper over the few CoreAudio property reads we need."""

    def __init__(self):
        self._ca = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
        self._cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self._ca.AudioObjectGetPropertyDataSize.argtypes = [
            ctypes.c_uint32, ctypes.POINTER(_Address), ctypes.c_uint32, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        self._ca.AudioObjectGetPropertyDataSize.restype = ctypes.c_int32
        self._ca.AudioObjectGetPropertyData.argtypes = [
            ctypes.c_uint32, ctypes.POINTER(_Address), ctypes.c_uint32, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p,
        ]
        self._ca.AudioObjectGetPropertyData.restype = ctypes.c_int32
        self._cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
        self._cf.CFStringGetCString.restype = ctypes.c_bool
        self._cf.CFRelease.argtypes = [ctypes.c_void_p]

    def _address(self, selector: int, scope: int = _SCOPE_GLOBAL) -> _Address:
        return _Address(selector, scope, _ELEMENT_MAIN)

    def object_ids(self, obj: int, selector: int, scope: int = _SCOPE_GLOBAL) -> Optional[List[int]]:
        """A property that is an array of UInt32 object ids; None if unsupported."""
        addr = self._address(selector, scope)
        size = ctypes.c_uint32(0)
        if self._ca.AudioObjectGetPropertyDataSize(obj, ctypes.byref(addr), 0, None, ctypes.byref(size)) != 0:
            return None
        count = size.value // 4
        if count == 0:
            return []
        buf = (ctypes.c_uint32 * count)()
        if self._ca.AudioObjectGetPropertyData(obj, ctypes.byref(addr), 0, None, ctypes.byref(size), buf) != 0:
            return None
        return list(buf)

    def uint32(self, obj: int, selector: int, scope: int = _SCOPE_GLOBAL) -> Optional[int]:
        addr = self._address(selector, scope)
        value = ctypes.c_uint32(0)
        size = ctypes.c_uint32(4)
        if self._ca.AudioObjectGetPropertyData(obj, ctypes.byref(addr), 0, None, ctypes.byref(size), ctypes.byref(value)) != 0:
            return None
        return int(value.value)

    def string(self, obj: int, selector: int) -> str:
        addr = self._address(selector)
        ref = ctypes.c_void_p(0)
        size = ctypes.c_uint32(ctypes.sizeof(ctypes.c_void_p))
        if self._ca.AudioObjectGetPropertyData(obj, ctypes.byref(addr), 0, None, ctypes.byref(size), ctypes.byref(ref)) != 0 or not ref.value:
            return ""
        try:
            buf = ctypes.create_string_buffer(512)
            if self._cf.CFStringGetCString(ref, buf, 512, _UTF8):
                return buf.value.decode("utf-8", "replace")
            return ""
        finally:
            self._cf.CFRelease(ref)


def list_audio_processes(reader: Optional[CoreAudioReader] = None) -> Optional[List[AudioProcess]]:
    """Every CoreAudio client process; None when the OS has no process list (< 14.2)."""
    if sys.platform != "darwin":
        return None
    try:
        reader = reader or CoreAudioReader()
        ids = reader.object_ids(_SYSTEM_OBJECT, _SEL_PROCESS_LIST)
        if ids is None:
            return None
        processes: List[AudioProcess] = []
        for obj in ids:
            pid = reader.uint32(obj, _SEL_PROCESS_PID)
            running = reader.uint32(obj, _SEL_PROCESS_INPUT)
            if pid is None or running is None:
                continue
            processes.append(AudioProcess(pid, reader.string(obj, _SEL_PROCESS_BUNDLE), bool(running)))
        return processes
    except Exception:  # noqa: BLE001
        log.debug("CoreAudio process list failed", exc_info=True)
        return None


def any_input_device_running(reader: Optional[CoreAudioReader] = None) -> bool:
    """Is any input-capable device running for some process (the pre-14.2 signal)?"""
    if sys.platform != "darwin":
        return False
    try:
        reader = reader or CoreAudioReader()
        for device in reader.object_ids(_SYSTEM_OBJECT, _SEL_DEVICES) or []:
            if not reader.object_ids(device, _SEL_STREAMS, _SCOPE_INPUT):
                continue  # output-only
            if reader.uint32(device, _SEL_RUNNING_SOMEWHERE):
                return True
    except Exception:  # noqa: BLE001
        log.debug("CoreAudio device probe failed", exc_info=True)
    return False


# -- AppKit / Quartz ------------------------------------------------------------------------------


def running_bundle_ids() -> List[str]:
    """Bundle ids of running GUI apps (NSWorkspace). [] off macOS or on error."""
    if sys.platform != "darwin":
        return []
    try:
        from AppKit import NSWorkspace

        return [
            str(app.bundleIdentifier())
            for app in NSWorkspace.sharedWorkspace().runningApplications()
            if app.bundleIdentifier()
        ]
    except Exception:  # noqa: BLE001
        return []


def bundle_for_pid(pid: int) -> str:
    if sys.platform != "darwin":
        return ""
    try:
        from AppKit import NSRunningApplication

        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(int(pid))
        return str(app.bundleIdentifier() or "") if app is not None else ""
    except Exception:  # noqa: BLE001
        return ""


def _cg_windows() -> List[dict]:
    import Quartz

    options = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
    return list(Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID) or [])


# -- the probes handed to MeetingDetector ---------------------------------------------------------------


def read_mic_usage(
    *,
    list_processes: Callable[[], Optional[Sequence[AudioProcess]]] = list_audio_processes,
    device_running: Callable[[], bool] = any_input_device_running,
    running_apps: Callable[[], Iterable[str]] = running_bundle_ids,
    own_pid: Optional[int] = None,
) -> List[MicUse]:
    """Per-app microphone use on macOS ([] off macOS or on error)."""
    if sys.platform != "darwin" and list_processes is list_audio_processes:
        return []
    try:
        processes = list_processes()
        if processes is not None:
            return mic_usage_from_processes(processes, own_pid)
        return mic_usage_from_device_state(bool(device_running()), running_apps())
    except Exception:  # noqa: BLE001 - detection is best-effort
        return []


def list_window_titles(
    *,
    windows: Callable[[], Iterable[dict]] = _cg_windows,
    bundle_lookup: Callable[[int], str] = bundle_for_pid,
) -> List[Tuple[str, str]]:
    """(bundle id lowercased, title) for visible windows; titles may be empty."""
    if sys.platform != "darwin" and windows is _cg_windows:
        return []
    try:
        return windows_to_titles(windows(), bundle_lookup)
    except Exception:  # noqa: BLE001
        return []
