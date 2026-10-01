"""Live input levels before a recording starts ("Preview"), with nothing written anywhere.

While idle, the window and the Recorders page can show what the selected microphone and system-audio
device are hearing, so a dead mic or the wrong output is noticed *before* the meeting. This module is the
metering half: one small thread per track opens that track's :class:`~meeting_notes.audio.source.AudioSource`
(the same source objects a recording opens, via the same ``source.open()`` / ``reader.read()`` protocol) and
keeps the latest block's peak, computed by the same :func:`~meeting_notes.audio.track_recorder.block_peak`
a recording uses. It never creates a file, a timing log or a session.

Rules it lives by:

* It only runs while something is looking (``idle_meter_wanted``): the window is visible, or a web viewer
  is watching this recorder. Otherwise every device stays closed (no "microphone in use" indicator, no CPU).
* A recording takes over cleanly: ``stop(join_timeout=...)`` signals every lane and waits (bounded) until
  the devices are released, and a lane stuck in a native call is abandoned like a wedged recorder thread is
  (``track_recorder``): it is a daemon, flagged stale, and can no longer publish a level.
* A device that cannot be opened or read shows level 0 and is retried slowly (an unplugged mic that comes
  back resumes by itself). Failures are never raised to the caller.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Dict, List, Optional, Tuple

from meeting_notes.audio.track_recorder import block_peak

log = logging.getLogger("meeting_notes.client.idle_meter")

# Short blocks so the bars feel live (a recording uses 0.5 s blocks; nothing is saved here).
BLOCK_SECONDS = 0.1
# After a failed open/read, wait this long before trying the device again.
RETRY_SECONDS = 2.0
# How long ``stop`` waits for the lanes to hand their devices back.
STOP_JOIN_SECONDS = 0.75


def idle_meter_wanted(*, enabled: bool, idle: bool, window_visible: bool, watched: bool) -> bool:
    """The one rule for "keep the devices open and meter": allowed by the setting, not recording, and
    someone is looking (the window is on screen, or a web viewer is watching this recorder)."""
    return bool(enabled and idle and (window_visible or watched))


class _Lane:
    """Meters one track on its own thread."""

    def __init__(self, track: str, source, block_seconds: float):
        self.track = track
        self.source = source
        self.key: Tuple[str, int] = source_key(source)
        self._block_frames = max(1, int(source.samplerate * block_seconds))
        self.stop_event = threading.Event()
        self.level = 0.0
        self.error: Optional[str] = None
        self.opens = 0
        self.thread = threading.Thread(target=self._run, name=f"idle-meter-{track}", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def signal_stop(self) -> None:
        self.stop_event.set()
        self.level = 0.0

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self._meter_once()
                return
            except Exception as exc:  # noqa: BLE001 - any failure means "device not usable right now"
                if self.stop_event.is_set():
                    return
                self.level = 0.0
                self.error = f"{type(exc).__name__}: {exc}"
                log.debug("idle meter %s: %s", self.track, self.error)
                if self.stop_event.wait(RETRY_SECONDS):
                    return

    def _meter_once(self) -> None:
        with self.source.open() as reader:
            self.opens += 1
            self.error = None
            empty = 0
            while not self.stop_event.is_set():
                block = reader.read(self._block_frames)
                if self.stop_event.is_set():
                    break
                if block is None or len(block) == 0:
                    empty += 1
                    if self.stop_event.wait(min(0.05 * empty, 0.5)):
                        break
                    continue
                empty = 0
                self.level = min(1.0, block_peak(block))


def source_key(source) -> Tuple[str, int]:
    """What identifies a device for retargeting: scans return fresh source objects each time."""
    return (str(getattr(source, "name", "")), int(getattr(source, "samplerate", 0) or 0))


class IdleMeter:
    """Meters up to one source per track; ``set_sources`` retargets, ``stop`` releases everything."""

    def __init__(self, *, block_seconds: float = BLOCK_SECONDS):
        self._block_seconds = block_seconds
        self._lock = threading.Lock()
        self._lanes: Dict[str, _Lane] = {}
        self._retiring: List[_Lane] = []   # signalled, maybe still closing their device

    # -- control ---------------------------------------------------------------

    def set_sources(self, sources: Dict[str, object]) -> None:
        """Meter exactly ``sources`` (track -> source): start new lanes, restart a lane whose device
        changed, stop lanes whose track is gone. Unchanged devices are left running. Never blocks."""
        with self._lock:
            for track in list(self._lanes):
                lane = self._lanes[track]
                source = sources.get(track)
                if source is None or source_key(source) != lane.key:
                    self._retire(track)
            for track, source in sources.items():
                if source is not None and track not in self._lanes:
                    lane = _Lane(track, source, self._block_seconds)
                    self._lanes[track] = lane
                    lane.start()

    def _retire(self, track: str) -> None:
        lane = self._lanes.pop(track, None)
        if lane is not None:
            lane.signal_stop()
            self._retiring.append(lane)

    def stop(self, join_timeout: float = 0.0) -> bool:
        """Signal every lane to stop; with ``join_timeout`` wait (bounded) until the devices are released.

        Returns True when nothing is left running. A lane that does not finish in time is abandoned: it
        is a daemon and its level is never read again.
        """
        with self._lock:
            for track in list(self._lanes):
                self._retire(track)
            lanes = list(self._retiring)
        deadline = time.monotonic() + max(0.0, join_timeout)
        for lane in lanes:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                lane.thread.join(timeout=remaining)
        with self._lock:
            self._retiring = [lane for lane in self._retiring if lane.thread.is_alive()]
            return not self._retiring

    # -- reading ---------------------------------------------------------------

    @property
    def active(self) -> bool:
        with self._lock:
            return bool(self._lanes)

    def tracks(self) -> List[str]:
        with self._lock:
            return sorted(self._lanes)

    def keys(self) -> Dict[str, Tuple[str, int]]:
        with self._lock:
            return {track: lane.key for track, lane in self._lanes.items()}

    def levels(self) -> Dict[str, float]:
        """Latest peak per metered track (0 for one whose device is failing)."""
        with self._lock:
            return {track: lane.level for track, lane in self._lanes.items()}

    def errors(self) -> Dict[str, str]:
        with self._lock:
            return {track: lane.error for track, lane in self._lanes.items() if lane.error}
