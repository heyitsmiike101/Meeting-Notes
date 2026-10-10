"""macOS sleep and wake notifications for the client (NSWorkspace). Does nothing on other platforms.

Closing the laptop lid during a recording puts the Mac to sleep with a ScreenCaptureKit stream open. On wake
the controller swaps in a fresh stream and stops the old one while the Mac is awake, so it never lingers
(and keeps the purple screen-recording indicator on) after the recording or the app has ended.
"""

from __future__ import annotations

import logging
import sys
from typing import Callable, Dict

log = logging.getLogger(__name__)

_CALLBACKS: Dict[str, Callable[[], None]] = {}
_STATE: dict = {"observer": None, "cls": None}


def _call(kind: str) -> None:
    fn = _CALLBACKS.get(kind)
    if fn is None:
        return
    try:
        fn()
    except Exception:  # noqa: BLE001 - a notification handler must never raise into AppKit
        log.exception("%s handler failed", kind)


def _observer_class():
    if _STATE["cls"] is None:
        from Foundation import NSObject

        class MeetingNotesPowerObserver(NSObject):
            def willSleep_(self, _notification):
                _call("sleep")

            def didWake_(self, _notification):
                _call("wake")

        _STATE["cls"] = MeetingNotesPowerObserver
    return _STATE["cls"]


def install(on_sleep: Callable[[], None], on_wake: Callable[[], None]) -> bool:
    """Call ``on_sleep`` / ``on_wake`` (on the main thread) around system sleep. True if installed."""
    _CALLBACKS["sleep"] = on_sleep
    _CALLBACKS["wake"] = on_wake
    if sys.platform != "darwin":
        return False
    if _STATE["observer"] is not None:
        return True
    try:
        import AppKit

        observer = _observer_class().alloc().init()
        center = AppKit.NSWorkspace.sharedWorkspace().notificationCenter()
        will = getattr(AppKit, "NSWorkspaceWillSleepNotification", "NSWorkspaceWillSleepNotification")
        did = getattr(AppKit, "NSWorkspaceDidWakeNotification", "NSWorkspaceDidWakeNotification")
        center.addObserver_selector_name_object_(observer, "willSleep:", will, None)
        center.addObserver_selector_name_object_(observer, "didWake:", did, None)
    except Exception:  # noqa: BLE001 - optional: without it the 20 s stall check still reopens the stream
        log.info("sleep/wake notifications unavailable", exc_info=True)
        return False
    _STATE["observer"] = observer  # keep it alive for the life of the app
    log.info("listening for sleep/wake")
    return True
