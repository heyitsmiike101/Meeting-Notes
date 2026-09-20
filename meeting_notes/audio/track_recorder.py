"""One capture thread per track, plus the machinery to survive a dead device.

The hard constraint shaping this module: a thread blocked inside ``soundcard``'s
``record()`` is inside a C call, and Python cannot interrupt or kill it. Not
with an Event, not with KeyboardInterrupt (which only ever lands in the main
thread). This matters most on macOS, where CoreAudio's recorder is documented to
block until the requested frames arrive, so a device that vanishes mid-meeting
hangs its thread forever. (Windows degrades more kindly: WASAPI synthesises
zero-filled blocks when the endpoint goes idle, so it returns rather than
hanging.)

So recovery never tries to stop a stuck thread. It *abandons* it -- flips the
active token so the zombie can never touch the output file again -- and starts a
fresh thread writing the next segment. The zombie dies with the process.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from meeting_notes.timing import TimingLogWriter
from meeting_notes.wav_io import RawTrackWriter


@dataclass
class TrackError:
    track: str
    message: str
    monotonic: float
    fatal: bool = False


class TrackRecorder:
    """Captures one source to one raw PCM file, restarting across failures."""

    def __init__(
        self,
        track: str,
        source,
        session_dir: Path,
        stop_event: threading.Event,
        errors: "queue.Queue[TrackError]",
        *,
        block_seconds: float = 0.5,
        progress_interval: float = 1.0,
    ):
        self.track = track
        self.source = source
        self.stop_event = stop_event
        self.errors = errors
        self.block_frames = max(1, int(source.samplerate * block_seconds))

        self.raw_path = Path(session_dir) / f"{track}.raw"
        self.writer = RawTrackWriter(self.raw_path)
        self.timing = TimingLogWriter(
            Path(session_dir) / f"{track}.timing.jsonl", progress_interval=progress_interval
        )

        # Guards every mutation of the output file and timing log. Also the
        # mechanism that makes abandoning a thread safe: a zombie must check its
        # token and write under the same lock, so it cannot slip a stale block
        # in between the new thread's writes.
        self._lock = threading.Lock()
        self._active_token: object = None
        self._threads: list = []

        self.last_progress = time.monotonic()
        self.last_peak = 0.0
        self.generation = 0
        self.degraded = False
        self.abandoned_threads = 0
        self.warnings: list = []
        self._started = False

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        self._spawn(reason="start")

    def restart(self, reason: str) -> None:
        """Abandon the current worker (it may be wedged) and start a new one."""
        with self._lock:
            if self._active_token is not None:
                self.abandoned_threads += 1
            self._active_token = None
            self.degraded = True
        self._spawn(reason=reason)

    def _spawn(self, reason: str) -> None:
        token = object()
        with self._lock:
            self._active_token = token
            self.generation += 1
        thread = threading.Thread(
            target=self._worker,
            args=(token, reason),
            name=f"track-{self.track}-{self.generation}",
            daemon=True,
        )
        self._threads.append(thread)
        self._started = True
        thread.start()

    @property
    def frames(self) -> int:
        return self.writer.frames

    @property
    def alive(self) -> bool:
        return any(t.is_alive() for t in self._threads)

    # -- worker --------------------------------------------------------------

    def _is_active(self, token: object) -> bool:
        return self._active_token is token and not self.stop_event.is_set()

    def _worker(self, token: object, reason: str) -> None:
        backoff = 1.0
        while not self.stop_event.is_set():
            if self._active_token is not token:
                return  # abandoned before we even opened the device
            try:
                self._capture_once(token, reason)
                return  # clean stop
            except Exception as exc:  # noqa: BLE001 - any failure means "device gone"
                if self._active_token is not token or self.stop_event.is_set():
                    return
                self.degraded = True
                self.errors.put(
                    TrackError(self.track, f"{type(exc).__name__}: {exc}", time.monotonic())
                )
                # Keep retrying for the life of the meeting: an unplugged USB
                # mic or a dropped Bluetooth headset often comes back, and
                # giving up would silently lose the rest of the recording.
                if self.stop_event.wait(backoff):
                    return
                backoff = min(backoff * 2, 30.0)
                reason = "reopen-after-error"

    def _capture_once(self, token: object, reason: str) -> None:
        with self.source.open() as reader:
            with self._lock:
                if not self._is_active(token):
                    return
                if reason != "start":
                    self._pad_gap_locked(reason)
                self.timing.open_segment(
                    frames=self.writer.frames,
                    samplerate=self.source.samplerate,
                    channels=self.source.channels,
                    device=self.source.name,
                )
                self.last_progress = time.monotonic()

            while not self.stop_event.is_set():
                block = reader.read(self.block_frames)
                now = time.monotonic()
                if block is None or len(block) == 0:
                    continue
                peak = float(np.abs(np.asarray(block, dtype=np.float32)).max(initial=0.0))
                with self._lock:
                    # Abandonment only, deliberately not _is_active(): if the
                    # stop flag flipped while this block was in flight we still
                    # want the audio, otherwise every Ctrl+C truncates the
                    # recording by up to one block.
                    if self._active_token is not token:
                        return
                    self.writer.write_float(block)
                    self.timing.progress(self.writer.frames)
                    self.last_progress = now
                    self.last_peak = peak
                self._drain_reader_warnings(reader)

            with self._lock:
                if self._active_token is token:
                    self.timing.progress(self.writer.frames, force=True)
                    self.timing.close_segment(self.writer.frames)

    def _pad_gap_locked(self, reason: str) -> None:
        """Fill the lost stretch with silence so frames stay wall-clock aligned.

        Without this, a 20 second stall would splice pre-stall and post-stall
        audio together in the WAV, and every timestamp after the gap would be 20
        seconds early. Padding keeps frame position meaning the same thing for
        the whole file, which is what lets the two tracks stay comparable.
        """
        lost = max(0.0, time.monotonic() - self.last_progress)
        padded = self.writer.write_silence(int(lost * self.source.samplerate))
        if padded:
            self.timing.gap(
                frames_before=self.writer.frames - padded,
                frames_padded=padded,
                seconds_lost=lost,
                reason=reason,
            )

    def _drain_reader_warnings(self, reader) -> None:
        drain = getattr(reader, "drain_warnings", None)
        if drain is None:
            return
        for message in drain():
            # soundcard raises "data discontinuity in recording" as a warning.
            # It means samples were dropped by the driver, which is worth
            # surfacing in session.json rather than letting it scroll past.
            self.warnings.append(message)

    # -- shutdown ------------------------------------------------------------

    def close(self, join_timeout: float = 2.0) -> None:
        """Stop, then finalize files without ever blocking on a wedged thread."""
        deadline = time.monotonic() + join_timeout
        for thread in self._threads:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                thread.join(timeout=remaining)
        with self._lock:
            self._active_token = None
            self.timing.close()
            self.writer.close()

    def stuck_for(self, now: Optional[float] = None) -> float:
        return (now or time.monotonic()) - self.last_progress
