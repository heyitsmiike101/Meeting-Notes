"""Keeps an up-to-date picture of which microphone / system-audio devices exist.

A headset that is switched on three minutes into a meeting must still end up in
the recording, and the window must not need a manual "Refresh audio" click to
notice it. So a small daemon thread re-scans on a timer (and sooner when the OS
says the device set changed, see ``wake``), and publishes the result as a
:class:`DeviceSnapshot`.

Everything platform-specific stays behind the injected ``scan`` callable (the
controller passes one built on ``audio.devices.resolve_source``), so this file
is plain threading and works the same on every platform.

Rules the thread lives by:

* It never touches Qt. The UI reads ``snapshot`` from its own timer.
* A scan that raises, or wedges inside a native call, costs nothing but that
  thread: the recording and the window are unaffected.
* ``on_poll`` is called after every scan (used while recording to attach or
  replace a track); ``on_change`` only when a device name actually changed.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

log = logging.getLogger("meeting_notes.client.device_watch")

KINDS = ("mic", "system")
DEFAULT_INTERVAL = 3.0
# After an OS device-change notification the endpoint list needs a moment to
# settle (Bluetooth headsets register in stages), so poll shortly after it.
WAKE_SETTLE = 0.7


@dataclass
class DeviceSnapshot:
    """What one scan found: a source (or None) and an error text per kind."""

    sources: Dict[str, Optional[object]] = field(default_factory=lambda: {k: None for k in KINDS})
    errors: Dict[str, str] = field(default_factory=dict)
    # kind -> the chosen device that could not be found, so the automatic one was used instead
    fallbacks: Dict[str, str] = field(default_factory=dict)
    taken: float = 0.0

    def name(self, kind: str) -> Optional[str]:
        source = self.sources.get(kind)
        return getattr(source, "name", None) if source is not None else None

    def names(self) -> Dict[str, Optional[str]]:
        return {kind: self.name(kind) for kind in KINDS}

    def label(self, kind: str) -> str:
        """Text for the window: the device name, or why there is none."""
        name = self.name(kind)
        if name:
            return name
        reason = self.errors.get(kind) or "not found"
        return f"unavailable: {reason}"


class DeviceWatcher:
    def __init__(
        self,
        scan: Callable[[], DeviceSnapshot],
        *,
        on_change: Optional[Callable[[DeviceSnapshot, DeviceSnapshot], None]] = None,
        on_poll: Optional[Callable[[DeviceSnapshot], None]] = None,
        interval: float = DEFAULT_INTERVAL,
    ):
        self._scan = scan
        self.on_change = on_change
        self.on_poll = on_poll
        self.interval = interval
        self._snapshot = DeviceSnapshot()
        self._has_snapshot = False
        self._lock = threading.Lock()
        self._poll_lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.polls = 0

    # -- state ---------------------------------------------------------------

    @property
    def snapshot(self) -> DeviceSnapshot:
        with self._lock:
            return self._snapshot

    @property
    def has_snapshot(self) -> bool:
        with self._lock:
            return self._has_snapshot

    def publish(self, snapshot: DeviceSnapshot) -> None:
        """Adopt a snapshot taken elsewhere (a manual probe) without a 'change'."""
        with self._lock:
            self._snapshot = snapshot
            self._has_snapshot = True

    # -- polling -------------------------------------------------------------

    def poll_once(self) -> Optional[DeviceSnapshot]:
        """One scan + callbacks. Never raises. Safe to call from tests."""
        with self._poll_lock:
            try:
                fresh = self._scan()
            except Exception as exc:  # noqa: BLE001 - a scan must never kill the watcher
                log.warning("device scan failed: %s: %s", type(exc).__name__, exc)
                return None
            fresh.taken = time.monotonic()
            with self._lock:
                previous = self._snapshot
                had = self._has_snapshot
                self._snapshot = fresh
                self._has_snapshot = True
            self.polls += 1
            if had and previous.names() != fresh.names():
                log.info(
                    "audio devices changed: mic %r -> %r, system %r -> %r",
                    previous.name("mic"), fresh.name("mic"),
                    previous.name("system"), fresh.name("system"),
                )
                self._call(self.on_change, previous, fresh)
            self._call(self.on_poll, fresh)
            return fresh

    @staticmethod
    def _call(fn, *args) -> None:
        if fn is None:
            return
        try:
            fn(*args)
        except Exception:  # noqa: BLE001 - a broken callback must not stop polling
            log.exception("device watcher callback failed")

    def wake(self) -> None:
        """Poll soon (the OS just reported a device change)."""
        self._wake.set()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="device-watch")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            if self._stop.wait(0):
                return
            woke = self._wake.wait(self.interval)
            if self._stop.is_set():
                return
            if woke:
                self._wake.clear()
                self._stop.wait(WAKE_SETTLE)
