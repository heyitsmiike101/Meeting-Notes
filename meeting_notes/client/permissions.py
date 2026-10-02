"""macOS privacy permissions the client needs, and what to tell the person about each.

Nothing here prompts unless a ``request_*`` function is called (and those honour
``MEETING_NOTES_NO_PERMISSION_PROMPT``). Every probe is cheap and never raises, so the window can
re-read them whenever it regains focus. On other platforms :func:`snapshot` returns an empty list.

Three permissions matter on a Mac:

* **Microphone** -- readable through AVFoundation.
* **Screen & System Audio Recording** -- ScreenCaptureKit system audio; readable with
  ``CGPreflightScreenCaptureAccess``; macOS only applies a new grant after the app restarts.
* **Local Network** (macOS 15 and newer) -- there is no API to ask. An app that has not been
  allowed gets ``EHOSTUNREACH`` ("No route to host") for hosts on the LAN, so it is inferred from
  that failure when the server's host is a LAN name or address.
"""

from __future__ import annotations

import ipaddress
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlsplit

GRANTED = "granted"
NOT_GRANTED = "not_granted"
UNKNOWN = "unknown"

MICROPHONE = "microphone"
SCREEN = "screen"
LOCAL_NETWORK = "local_network"

_PANE = "x-apple.systempreferences:com.apple.preference.security?"
SETTINGS_URLS = {
    MICROPHONE: _PANE + "Privacy_Microphone",
    SCREEN: _PANE + "Privacy_ScreenCapture",
    LOCAL_NETWORK: _PANE + "Privacy_LocalNetwork",
}

# AVAuthorizationStatus
_AV_NOT_DETERMINED = 0
_AV_AUTHORIZED = 3

_NO_ROUTE = re.compile(r"(?i)errno\s*65\b|no route to host|EHOSTUNREACH")
_PERMISSION_TEXT = re.compile(r"(?i)not allowed to record|permission|not authori[sz]ed|privacy")


@dataclass
class Permission:
    key: str
    title: str
    status: str  # GRANTED | NOT_GRANTED | UNKNOWN
    steps: List[str] = field(default_factory=list)
    note: str = ""
    settings_url: str = ""
    can_request: bool = False  # microphone not yet asked: a button can show the system prompt
    needs_restart: bool = False  # a grant only takes effect after quitting and reopening

    @property
    def needed(self) -> bool:
        return self.status == NOT_GRANTED

    @property
    def status_text(self) -> str:
        return {GRANTED: "Granted", NOT_GRANTED: "Not granted"}.get(self.status, "Unknown")


# -- probes ------------------------------------------------------------------------


def _darwin() -> bool:
    return sys.platform == "darwin"


def microphone_state() -> Optional[int]:
    """The raw AVAuthorizationStatus for audio capture (0 not asked, 1 restricted, 2 denied, 3 allowed)."""
    if not _darwin():
        return None
    try:
        import AVFoundation

        return int(AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(AVFoundation.AVMediaTypeAudio))
    except Exception:  # noqa: BLE001 - a missing framework just means "unknown"
        return None


def request_microphone(on_done=None) -> bool:
    """Show the system Microphone prompt (once, if never asked). A no-op under ``MEETING_NOTES_NO_PERMISSION_PROMPT``."""
    if not _darwin() or os.environ.get("MEETING_NOTES_NO_PERMISSION_PROMPT"):
        return False
    try:
        import AVFoundation

        def handler(granted):
            if on_done is not None:
                try:
                    on_done(bool(granted))
                except Exception:  # noqa: BLE001
                    pass

        AVFoundation.AVCaptureDevice.requestAccessForMediaType_completionHandler_(AVFoundation.AVMediaTypeAudio, handler)
        return True
    except Exception:  # noqa: BLE001
        return False


def screen_granted() -> Optional[bool]:
    from meeting_notes.audio import screencapture_source

    return screencapture_source.permission_granted()


def request_screen() -> bool:
    from meeting_notes.audio import screencapture_source

    return screencapture_source.request_permission()


def is_lan_host(url: str) -> bool:
    """True for a server URL whose host is a LAN name or address (where Local Network privacy applies)."""
    text = (url or "").strip()
    if not text:
        return False
    if "://" not in text:
        text = "http://" + text
    try:
        host = (urlsplit(text).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return "." not in host or host.endswith((".lan", ".local", ".home", ".internal", ".localdomain", ".home.arpa"))
    return ip.is_private or ip.is_link_local


def is_no_route(text) -> bool:
    """True when an error text is macOS answering EHOSTUNREACH ("No route to host")."""
    return bool(text) and bool(_NO_ROUTE.search(str(text)))


def is_permission_error(text) -> bool:
    """True when a recording-start error reads as a missing privacy permission."""
    return bool(text) and bool(_PERMISSION_TEXT.search(str(text)))


def bundle_path() -> Optional[Path]:
    """The ``.app`` this process runs from, or None when it is not the bundled app."""
    if not _darwin():
        return None
    for parent in Path(sys.executable).resolve().parents:
        if parent.suffix == ".app":
            return parent
    return None


# -- the list shown to the person ------------------------------------------------------


def snapshot(server_url: str = "", contact_error: str = "") -> List[Permission]:
    """Every permission with its current state; empty off macOS. Never prompts, never raises."""
    if not _darwin():
        return []
    items: List[Permission] = []

    state = microphone_state()
    mic_status = UNKNOWN if state is None else GRANTED if state == _AV_AUTHORIZED else NOT_GRANTED
    items.append(
        Permission(
            MICROPHONE,
            "Microphone",
            mic_status,
            steps=[
                "Open System Settings, then Privacy & Security, then Microphone.",
                "Switch Meeting Notes on.",
            ],
            note="Needed to record your side of the meeting.",
            settings_url=SETTINGS_URLS[MICROPHONE],
            can_request=state == _AV_NOT_DETERMINED,
        )
    )

    granted = screen_granted()
    items.append(
        Permission(
            SCREEN,
            "Screen & System Audio Recording",
            UNKNOWN if granted is None else GRANTED if granted else NOT_GRANTED,
            steps=[
                "Open System Settings, then Privacy & Security, then Screen & System Audio Recording.",
                "Switch Meeting Notes on.",
                "Quit and reopen Meeting Notes. macOS only applies this permission after a restart.",
            ],
            note=(
                "Needed to record the other people on a call (system audio). It never records your screen. "
                "Without it, Meeting Notes still records your microphone."
            ),
            settings_url=SETTINGS_URLS[SCREEN],
            needs_restart=True,
        )
    )

    blocked = is_no_route(contact_error) and is_lan_host(server_url)
    items.append(
        Permission(
            LOCAL_NETWORK,
            "Local Network",
            NOT_GRANTED if blocked else UNKNOWN,
            steps=[
                "Open System Settings, then Privacy & Security, then Local Network.",
                "Switch Meeting Notes on.",
            ],
            note=(
                "Needed to reach your Meeting Notes server on this network. "
                "macOS cannot report this one, so it shows as needed only when the server could not be reached."
            ),
            settings_url=SETTINGS_URLS[LOCAL_NETWORK],
        )
    )
    return items


def missing(items: List[Permission]) -> List[Permission]:
    return [p for p in items if p.needed]
