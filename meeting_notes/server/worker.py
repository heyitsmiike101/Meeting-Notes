"""The thread, stop flag and wake flag every background worker of the server used to hand-roll.

``BackgroundWorker`` owns the lifecycle: a daemon thread started once (a second ``start`` while it runs does
nothing), a stop flag, a wake flag that cuts a wait short, and a ``stop`` that sets both and joins. A subclass
supplies the work:

* the usual timer shape -- override ``run_once`` (and set ``poll_seconds`` / ``startup_delay``); or
* its own loop -- override ``_run`` (a queue consumer, a worker that waits before its first pass).

``stop`` always sets the wake flag too, so a worker sleeping in ``_wait`` ends at once instead of after its nap.
The attribute names ``_stop``, ``_wake`` and ``_thread`` are kept: tests and subclasses read them.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)


class BackgroundWorker:
    thread_name = "meeting-notes-worker"  # shows in thread dumps (the test hang log names threads)
    stop_timeout = 2.0                    # default join timeout of ``stop``
    startup_delay = 0.0                   # seconds to wait before the first pass (cut short by ``stop`` only)
    poll_seconds = 1.0                    # nap between passes of the default loop (cut short by ``wake``)

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle -------------------------------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        if self._stop.is_set():
            self._wake.clear()  # restart: drop the wake that ``stop`` left behind
        self._stop.clear()
        self._on_start()
        self._thread = threading.Thread(target=self._run, name=self.thread_name, daemon=True)
        self._thread.start()

    def wake(self) -> None:
        """Run the next pass now (as soon as the thread notices) instead of after the current nap."""
        self._wake.set()

    def stop(self, timeout: Optional[float] = None) -> None:
        self._stop.set()
        self._wake.set()
        self._on_stop()
        if self._thread is not None:
            self._thread.join(timeout=self.stop_timeout if timeout is None else timeout)

    def _on_start(self) -> None:
        """Before the thread starts (a queue consumer scans what it must pick up)."""

    def _on_stop(self) -> None:
        """After the flags are set, before the join (a queue consumer unblocks its ``get``)."""

    # -- the work --------------------------------------------------------------------------------------------

    def run_once(self) -> None:
        """One pass of the default loop."""
        raise NotImplementedError

    def _poll_seconds(self) -> float:
        """How long to nap after a pass; read after each pass so a changed setting applies on the next one."""
        return self.poll_seconds

    def _wait(self, seconds: float) -> bool:
        """Sleep until ``wake`` or ``stop`` or the time is up. True when woken."""
        return self._wake.wait(timeout=max(0.0, seconds))

    def _run(self) -> None:
        if self.startup_delay and self._stop.wait(self.startup_delay):
            return
        while not self._stop.is_set():
            self._wake.clear()  # before the pass, so a wake that arrives during it is not lost
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 -- one bad pass must not end the worker
                logger.exception("%s: pass failed", self.thread_name)
            self._wait(self._poll_seconds())
