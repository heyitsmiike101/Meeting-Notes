"""Detect when a Teams, Zoom or browser (Google Meet) call starts and ends.

macOS probes live in :mod:`meeting_notes.client.meeting_detect_mac`; this module
holds the platform-neutral classification, naming and state machine.

Windows records which apps are using the microphone right now in the current
user's registry (the "microphone in use" privacy indicator reads the same data):

    HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\CapabilityAccessManager\\
        ConsentStore\\microphone

Packaged apps (new Teams) are direct subkeys; desktop apps live under the
``NonPackaged`` subkey, named by exe path with ``\\`` replaced by ``#``.  An app
is using the mic when ``LastUsedTimeStop`` is 0 (or older than ``LastUsedTimeStart``).
No admin rights are needed.

The pure logic (classification, name suggestion, the :class:`MeetingDetector`
state machine) is kept apart from the two Windows probes so it can be tested on
any OS with injected fakes.
"""

from __future__ import annotations

import logging
import ntpath
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable, List, Optional, Sequence, Tuple, Union

log = logging.getLogger("meeting_notes.client.meeting_detect")

CONSENT_KEY = (
    r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager"
    r"\ConsentStore\microphone"
)

KIND_TEAMS = "teams"
KIND_ZOOM = "zoom"
KIND_BROWSER = "browser"

LABELS = {KIND_TEAMS: "Teams", KIND_ZOOM: "Zoom", KIND_BROWSER: "Browser"}
LABEL_MEET = "Google Meet"

_BROWSER_EXES = {"chrome.exe", "brave.exe", "msedge.exe", "firefox.exe"}
_TEAMS_EXES = {"ms-teams.exe", "teams.exe"}
_ZOOM_EXES = {"zoom.exe"}

# macOS identifies apps by bundle id (lowercased). Helper processes carry the
# app's id as a prefix ("com.google.Chrome.helper.Renderer"), and it is those
# helpers that actually hold the microphone, so match on prefixes.
_MAC_TEAMS_PREFIXES = ("com.microsoft.teams",)
_MAC_ZOOM_PREFIXES = ("us.zoom.xos", "us.zoom.zoom")
_MAC_BROWSER_PREFIXES = (
    "com.google.chrome",
    "com.brave.browser",
    "com.microsoft.edgemac",
    "org.mozilla.firefox",
    "company.thebrowser.browser",  # Arc
    "com.vivaldi.vivaldi",
    "com.operasoftware.opera",
    "org.chromium.chromium",
    "com.apple.safari",
    "com.apple.webkit.",  # Safari's web-content / GPU helpers
)


# -- data ------------------------------------------------------------------


@dataclass(frozen=True)
class MicUse:
    """One app entry from the microphone consent store."""

    key: str  # registry subkey name, e.g. "MSTeams_8wekyb3d8bbwe" or "C:#...#Zoom.exe"
    exe_path: str  # full exe path for desktop apps, "" for packaged apps
    exe_name: str  # lowercased basename for desktop apps; lowercased key for packaged
    in_use: bool


@dataclass(frozen=True)
class MeetingStarted:
    kind: str
    label: str
    suggested_name: str


@dataclass(frozen=True)
class MeetingEnded:
    kind: str
    label: str


MeetingEvent = Union[MeetingStarted, MeetingEnded]


# -- Windows probes --------------------------------------------------------


def _filetime_in_use(start: int, stop: int) -> bool:
    return start > 0 and (stop == 0 or stop < start)


def _entry_from_key(key: str, packaged: bool, start: int, stop: int) -> MicUse:
    if packaged:
        return MicUse(key=key, exe_path="", exe_name=key.lower(), in_use=_filetime_in_use(start, stop))
    exe_path = key.replace("#", "\\")
    return MicUse(
        key=key,
        exe_path=exe_path,
        exe_name=ntpath.basename(exe_path).lower(),
        in_use=_filetime_in_use(start, stop),
    )


def read_mic_usage() -> List[MicUse]:
    """Read per-app microphone use from the registry.  [] off Windows or on error."""
    if sys.platform != "win32":
        return []
    try:
        import winreg  # type: ignore[import-not-found]
    except ImportError:
        return []

    results: List[MicUse] = []

    def read_times(sub) -> Optional[Tuple[int, int]]:
        try:
            start = int(winreg.QueryValueEx(sub, "LastUsedTimeStart")[0])
            stop = int(winreg.QueryValueEx(sub, "LastUsedTimeStop")[0])
        except OSError:
            return None  # never used the microphone
        return start, stop

    def scan(parent, packaged: bool) -> None:
        index = 0
        while True:
            try:
                name = winreg.EnumKey(parent, index)
            except OSError:
                return
            index += 1
            if packaged and name == "NonPackaged":
                continue
            try:
                with winreg.OpenKey(parent, name) as sub:
                    times = read_times(sub)
            except OSError:
                continue
            if times is not None:
                results.append(_entry_from_key(name, packaged, times[0], times[1]))

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, CONSENT_KEY) as root:
            scan(root, True)
            try:
                with winreg.OpenKey(root, "NonPackaged") as nonpackaged:
                    scan(nonpackaged, False)
            except OSError:
                pass
    except OSError:
        return []
    return results


def list_window_titles() -> List[Tuple[str, str]]:
    """(exe basename lowercased, title) for visible top-level windows with titles.

    [] off Windows or on any failure.
    """
    if sys.platform != "win32":
        return []
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        enum_proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows.argtypes = [enum_proc_type, wintypes.LPARAM]
        user32.EnumWindows.restype = wintypes.BOOL
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        process_query_limited_information = 0x1000
        exe_cache = {}

        def exe_for_pid(pid: int) -> str:
            if pid in exe_cache:
                return exe_cache[pid]
            name = ""
            handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
            if handle:
                try:
                    size = wintypes.DWORD(1024)
                    buf = ctypes.create_unicode_buffer(size.value)
                    if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                        name = ntpath.basename(buf.value).lower()
                finally:
                    kernel32.CloseHandle(handle)
            exe_cache[pid] = name
            return name

        found: List[Tuple[str, str]] = []

        def callback(hwnd, _lparam):
            try:
                if not user32.IsWindowVisible(hwnd):
                    return True
                length = user32.GetWindowTextLengthW(hwnd)
                if length <= 0:
                    return True
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                title = buf.value.strip()
                if not title:
                    return True
                pid = wintypes.DWORD(0)
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if not pid.value:
                    return True
                exe = exe_for_pid(pid.value)
                if exe:
                    found.append((exe, title))
            except Exception:  # noqa: BLE001 - never let one window break the walk
                pass
            return True

        user32.EnumWindows(enum_proc_type(callback), 0)
        return found
    except Exception:  # noqa: BLE001 - detection is best-effort
        return []


# -- classification --------------------------------------------------------


def classify(exe_name: str) -> Optional[str]:
    """Meeting-app kind for an exe basename or packaged key, else None."""
    name = (exe_name or "").lower()
    if name.startswith("msteams_") or name in _TEAMS_EXES:
        return KIND_TEAMS
    if name in _ZOOM_EXES:
        return KIND_ZOOM
    if name in _BROWSER_EXES:
        return KIND_BROWSER
    if name.startswith(_MAC_TEAMS_PREFIXES):
        return KIND_TEAMS
    if name.startswith(_MAC_ZOOM_PREFIXES):
        return KIND_ZOOM
    if name.startswith(_MAC_BROWSER_PREFIXES):
        return KIND_BROWSER
    return None


def _normalize_path(path: str) -> str:
    return os.path.normcase(os.path.normpath(path)) if path else ""


# -- name suggestion -------------------------------------------------------

_TEAMS_SUFFIX = re.compile(r"\s*\|\s*Microsoft Teams(?:\s*\([^)]*\))?\s*$", re.IGNORECASE)
_TEAMS_MEETING_NOISE = re.compile(r"\s*(?:\((?:Meeting)\)|\|\s*Meeting)\s*$", re.IGNORECASE)
_GENERIC_TEAMS = {
    "chat", "activity", "calendar", "teams", "calls", "files", "apps", "people",
    "notifications", "search", "settings", "onedrive", "communities", "approvals",
    "copilot", "tasks", "viva engage", "microsoft teams", "meeting",
    "meeting compact view", "meeting controls", "sharing control bar",
    "new meeting", "join", "microsoft teams notification", "loading",
}
_BROWSER_SUFFIX = re.compile(
    r"(?:\s+[-–—]\s+(?:Google Chrome|Brave|Chromium|Mozilla Firefox)"
    r"|(?:\s+[-–—]\s+[^-–—]+)?\s+[-–—]\s+Microsoft\W*Edge)\s*$",
    re.IGNORECASE,
)
_MORE_PAGES = re.compile(r"\s+and \d+ more pages?\s*$", re.IGNORECASE)
_MEET_TITLE = re.compile(r"^Meet\s*[-–—]\s*(.+)$", re.IGNORECASE)
_MEET_TITLE_ALT = re.compile(r"^(.+?)\s+[-–—]\s+Google Meet$", re.IGNORECASE)
_MEET_CODE = re.compile(r"^[a-z]{3}-[a-z]{4}-[a-z]{3}$", re.IGNORECASE)
_GENERIC_MEET = {"meet", "google meet"}
_FIRST_SEGMENT = re.compile(r"\s*\|\s*")


def sanitize_name(name: str, limit: int = 120) -> str:
    text = re.sub(r"\s+", " ", name or "").strip()
    return text[:limit].strip()


def _teams_title(raw: str) -> Optional[str]:
    """Meeting name from a Teams window title, or None if it is generic."""
    text = _TEAMS_SUFFIX.sub("", raw).strip()
    text = _TEAMS_MEETING_NOISE.sub("", text).strip()
    if not text:
        return None
    first = _FIRST_SEGMENT.split(text)[0].strip().lower()
    if first in _GENERIC_TEAMS or text.lower() in _GENERIC_TEAMS:
        return None
    return sanitize_name(text) or None


def _strip_browser(raw: str) -> str:
    text = _BROWSER_SUFFIX.sub("", raw).strip()
    return _MORE_PAGES.sub("", text).strip()


def _meet_title(raw: str) -> Optional[str]:
    text = _strip_browser(raw)
    if text.lower() in _GENERIC_MEET:
        return None
    match = _MEET_TITLE.match(text) or _MEET_TITLE_ALT.match(text)
    if not match:
        return None
    detail = sanitize_name(match.group(1))
    if not detail or detail.lower() in _GENERIC_MEET:
        return None
    if _MEET_CODE.match(detail):
        return f"Meet {detail}"
    return detail


def _fallback_name(label: str, now: datetime) -> str:
    hour12 = now.hour % 12 or 12
    suffix = "AM" if now.hour < 12 else "PM"
    return f"{label} call {hour12}:{now.minute:02d} {suffix}"


def suggest_name(kind: str, titles: Iterable[str], now: datetime) -> Tuple[str, str]:
    """(label, meeting name) from the window titles of one app kind."""
    titles = [t for t in titles if t]
    label = LABELS.get(kind, "Meeting")
    name: Optional[str] = None

    if kind == KIND_TEAMS:
        for title in titles:
            name = _teams_title(title)
            if name:
                break
    elif kind == KIND_BROWSER:
        for title in titles:
            name = _meet_title(title)
            if name:
                label = LABEL_MEET
                break
        if not name:
            for title in titles:
                stripped = _strip_browser(title)
                if _TEAMS_SUFFIX.search(stripped):
                    name = _teams_title(stripped)
                    if name:
                        label = LABELS[KIND_TEAMS]
                        break
    # Zoom titles are generic ("Zoom Meeting", "Zoom Workplace"): always fall back.

    if not name:
        name = _fallback_name(label, now)
    return label, sanitize_name(name)


# -- "is a call window still open?" -----------------------------------------

_ZOOM_CALL_TITLE = re.compile(r"^\s*zoom\s+(meeting|webinar)\b", re.IGNORECASE)
_TEAMS_CALL_TITLES = ("meeting compact view", "meeting controls", "sharing control bar")


def has_call_window(kind: str, titles: Iterable[str]) -> bool:
    """True when one of an app kind's window titles looks like a live call.

    Deliberately errs towards "yes": a false positive only means a recording
    is not auto-stopped, a false negative could cut a live call short.
    Browsers only count for Google Meet / Teams-on-the-web tabs; other web
    calls (webinars) are covered by the system-audio check in the UI instead.
    """
    for title in titles:
        if not title:
            continue
        if kind == KIND_ZOOM:
            if _ZOOM_CALL_TITLE.match(title):
                return True
        elif kind == KIND_TEAMS:
            if title.strip().lower() in _TEAMS_CALL_TITLES or _teams_title(title):
                return True
        elif kind == KIND_BROWSER:
            stripped = _strip_browser(title)
            if _meet_title(title) or (
                _TEAMS_SUFFIX.search(stripped) and _teams_title(stripped)
            ):
                return True
    return False


# -- state machine ---------------------------------------------------------


class MeetingDetector:
    """Turns polled mic-use snapshots into MeetingStarted / MeetingEnded events."""

    def __init__(
        self,
        read_usage: Callable[[], Sequence[MicUse]] = read_mic_usage,
        read_titles: Callable[[], Sequence[Tuple[str, str]]] = list_window_titles,
        own_executable: Optional[str] = None,
        start_debounce_sec: float = 4.0,
        end_grace_sec: float = 60.0,
        wall_clock: Callable[[], datetime] = datetime.now,
    ):
        self._read_usage = read_usage
        self._read_titles = read_titles
        self._own = _normalize_path(own_executable if own_executable is not None else sys.executable)
        self.start_debounce_sec = float(start_debounce_sec)
        self.end_grace_sec = float(end_grace_sec)
        self._wall_clock = wall_clock
        self._pending_kind: Optional[str] = None
        self._pending_since = 0.0
        self._active_kind: Optional[str] = None
        self._active_label = ""
        self._last_seen = 0.0
        self._window_hold = False

    @property
    def active_kind(self) -> Optional[str]:
        return self._active_kind

    def _kinds_in_use(self) -> List[str]:
        try:
            usage = list(self._read_usage() or [])
        except Exception:  # noqa: BLE001 - a failed probe is "nothing in use"
            usage = []
        kinds: List[str] = []
        for use in usage:
            if not use.in_use:
                continue
            if use.exe_path and self._own and _normalize_path(use.exe_path) == self._own:
                continue
            kind = classify(use.exe_name)
            if kind and kind not in kinds:
                kinds.append(kind)
        return kinds

    def _titles_for(self, kind: str) -> List[str]:
        try:
            windows = list(self._read_titles() or [])
        except Exception:  # noqa: BLE001
            windows = []
        wanted = []
        for exe, title in windows:
            exe_kind = classify(exe)
            if exe_kind == kind:
                wanted.append(title)
        return wanted

    def _kinds_with_windows(self) -> set:
        try:
            windows = list(self._read_titles() or [])
        except Exception:  # noqa: BLE001
            windows = []
        return {classify(exe) for exe, _title in windows} - {None}

    def poll(self, now: float) -> List[MeetingEvent]:
        kinds = self._kinds_in_use()
        events: List[MeetingEvent] = []

        if self._active_kind is not None:
            if self._active_kind in kinds:
                self._last_seen = now
            elif has_call_window(self._active_kind, self._titles_for(self._active_kind)):
                # The mic was released (muted attendee, push-to-talk, listen-only
                # webinar) but the call window is still open: still in the call.
                if not self._window_hold:
                    log.info("meeting detection: %s mic released but call window still open; call continues", self._active_kind)
                    self._window_hold = True
                self._last_seen = now
            elif now - self._last_seen >= self.end_grace_sec:
                log.info(
                    "meeting detection: %s mic released for %.0fs and no call window; call ended",
                    self._active_kind, now - self._last_seen,
                )
                events.append(MeetingEnded(self._active_kind, self._active_label))
                self._active_kind = None
                self._active_label = ""
                self._pending_kind = None
                self._window_hold = False
            return events

        if not kinds:
            self._pending_kind = None
            return events
        # A crashed app can leave its registry entry marked "in use"; only
        # offer a call for apps that actually have a window open.  (Ending a
        # call never depends on windows, so a recording is not cut short.)
        live = self._kinds_with_windows()
        kinds = [kind for kind in kinds if kind in live]
        if not kinds:
            self._pending_kind = None
            return events
        if self._pending_kind not in kinds:
            self._pending_kind = kinds[0]
            self._pending_since = now
        if now - self._pending_since >= self.start_debounce_sec:
            kind = self._pending_kind
            label, name = suggest_name(kind, self._titles_for(kind), self._wall_clock())
            self._active_kind = kind
            self._active_label = label
            self._last_seen = now
            self._window_hold = False
            self._pending_kind = None
            events.append(MeetingStarted(kind, label, name))
        return events
