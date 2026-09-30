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
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from meeting_notes.timing import TimingLogWriter
from meeting_notes.wav_io import RawTrackWriter


def describe_error(exc: BaseException) -> str:
    """Format an exception as ``TypeName: message`` without ever landing on a
    bare, contentless ``TypeName: `` -- which is exactly what
    ``f"{type(exc).__name__}: {exc}"`` produces for an exception raised with
    no message (a bare ``assert`` is the case that motivated this: an
    ``AssertionError`` with an empty ``str(exc)``, seen for real coming out of
    ``soundcard``'s WASAPI layer). When the message is empty, fall back to
    the innermost traceback frame's ``file:line``, which at least points
    whoever reads session.json or a doctor report at the line that raised,
    instead of a name with nothing after the colon.
    """
    message = str(exc)
    if message:
        return f"{type(exc).__name__}: {message}"
    frames = traceback.extract_tb(exc.__traceback__)
    if not frames:
        return type(exc).__name__
    frame = frames[-1]
    # Two path components (parent dir + filename) is enough to tell soundcard's
    # mediafoundation.py from wav_io.py's, without the full absolute path.
    parts = Path(frame.filename).parts
    location = "/".join(parts[-2:]) if len(parts) >= 2 else parts[-1]
    return f"{type(exc).__name__} ({location}:{frame.lineno})"


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
        on_block=None,
        session_start: Optional[float] = None,
    ):
        self.track = track
        self.source = source
        self.stop_event = stop_event
        self.errors = errors
        # Optional mirror of each captured block, used to feed the live preview
        # stream. Deliberately called OUTSIDE the write lock and wrapped in a
        # try/except: the preview is a disposable side channel and must never be
        # able to slow down or break the recording, which is the real artifact.
        self.on_block = on_block
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
        # Separate from last_progress on purpose. last_progress means "when real
        # audio last arrived" and is what gap padding measures against, so it
        # must not be touched by a restart. This one means "when we last gave
        # the device a fresh chance", and is what the watchdog counts from --
        # without it, a device that cannot be reopened at all would be restarted
        # every supervisor tick forever, defeating the worker's own backoff.
        self._watchdog_mark = time.monotonic()
        self.last_peak = 0.0
        # Muting is a per-track output state, not a device stop.  The worker
        # continues reading so the other source and live preview stay alive,
        # while muted blocks are written/streamed as silence and timing keeps
        # advancing normally.
        self._muted = False
        self.generation = 0
        self.degraded = False
        self.abandoned_threads = 0
        self.warnings: list = []
        self._started = False
        # Set only for a track whose device showed up after the session began.
        # It is the session's monotonic start: the first thing such a recorder
        # does is fill the time before the device existed with silence, so this
        # track's frame 0 is the session's t=0 like the other track's.
        self.session_start = session_start
        self._prefix_done = session_start is None
        self.attached_late = session_start is not None
        # Health, read by the session/controller to notice a lost device (and
        # to decide it is safe to swap in a fresh one). ``consecutive_errors``
        # counts failed opens/reads since the last block that really arrived.
        self.consecutive_errors = 0
        self.ever_read = False
        self.last_replaced = float("-inf")
        self.attach_gap_seconds = 0.0

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

    @property
    def failing(self) -> bool:
        """True while the device is erroring and no audio has arrived since."""
        return self.consecutive_errors > 0

    def replace_source(self, source, reason: str = "device-changed") -> None:
        """Point this track at a different device and reopen it.

        Only the same sample rate is accepted, because the raw file and the
        session metadata describe exactly one rate per track. The abandoned
        worker (if any) can no longer touch the file, and the reopen pads the
        lost stretch with silence exactly like a stall restart does.
        """
        if int(source.samplerate) != int(self.source.samplerate):
            raise ValueError(
                f"cannot switch {self.track} from {self.source.samplerate} Hz to {source.samplerate} Hz mid-recording"
            )
        with self._lock:
            self.source = source
            self.last_replaced = time.monotonic()
            self.consecutive_errors = 0
        self.restart(reason)

    def _spawn(self, reason: str) -> None:
        token = object()
        with self._lock:
            self._active_token = token
            self.generation += 1
            self._watchdog_mark = time.monotonic()
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

    @property
    def muted(self) -> bool:
        with self._lock:
            return self._muted

    def set_muted(self, muted: bool) -> None:
        """Mute or unmute this source without closing/restarting its device."""
        with self._lock:
            self._muted = bool(muted)

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
                self.consecutive_errors += 1
                self.errors.put(
                    TrackError(self.track, describe_error(exc), time.monotonic())
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
                if not self._prefix_done:
                    self._write_attach_prefix_locked()
                else:
                    if reason != "start":
                        self._pad_gap_locked(reason)
                    self.timing.open_segment(
                        frames=self.writer.frames,
                        samplerate=self.source.samplerate,
                        channels=self.source.channels,
                        device=self.source.name,
                    )
                self.last_progress = time.monotonic()

            empty_reads = 0
            while not self.stop_event.is_set():
                block = reader.read(self.block_frames)
                now = time.monotonic()
                if block is None or len(block) == 0:
                    # A backend handing back empty blocks in a tight loop would
                    # otherwise burn a core and still trip the watchdog. Yield
                    # briefly so the stall path stays the thing that handles it.
                    empty_reads += 1
                    if self.stop_event.wait(min(0.05 * empty_reads, 0.5)):
                        break
                    continue
                empty_reads = 0
                peak = float(np.abs(np.asarray(block, dtype=np.float32)).max(initial=0.0))
                with self._lock:
                    # Abandonment only, deliberately not _is_active(): if the
                    # stop flag flipped while this block was in flight we still
                    # want the audio, otherwise every Ctrl+C truncates the
                    # recording by up to one block.
                    if self._active_token is not token:
                        return
                    if self._muted:
                        # Keep the original shape/dtype and preserve the
                        # block's duration.  This avoids a discontinuity in
                        # the shared timeline and sends silence to the live
                        # preview through _mirror_block below.
                        block = np.zeros_like(block)
                        peak = 0.0
                    self.writer.write_float(block)
                    self.timing.progress(self.writer.frames)
                    self.last_progress = now
                    self.last_peak = peak
                    self.consecutive_errors = 0
                    self.ever_read = True
                self._mirror_block(block)
                self._drain_reader_warnings(reader)

            with self._lock:
                if self._active_token is token:
                    self.timing.progress(self.writer.frames, force=True)
                    self.timing.close_segment(self.writer.frames)

    def _mirror_block(self, block) -> None:
        if self.on_block is None:
            return
        try:
            self.on_block(self.track, block)
        except Exception:  # noqa: BLE001 - a broken preview must not stop capture
            self.on_block = None

    def _write_attach_prefix_locked(self) -> None:
        """Silence from the session start up to now, for a late-attached device.

        The segment is opened at frame 0 *back-dated to the session start*, then
        the silence is written and logged as a gap (reason ``late-attach``).
        That is precisely the shape a device that dropped out at t=0 and came
        back at t=now would leave, so the frame clock, ``in_gap`` and the
        server's merge treat it with no special casing: frame f maps to
        session_start + f/rate through the gap, and everything captured after
        lines up with the other track.
        """
        now = time.monotonic()
        start = min(float(self.session_start), now)
        self.timing.open_segment(
            frames=0,
            samplerate=self.source.samplerate,
            channels=self.source.channels,
            device=self.source.name,
            t=start,
        )
        lost = now - start
        padded = self.writer.write_silence(int(lost * self.source.samplerate))
        if padded:
            self.timing.gap(
                frames_before=0,
                frames_padded=padded,
                seconds_lost=lost,
                reason="late-attach",
            )
            self._mirror_silence(padded)
        self.attach_gap_seconds = lost
        self._prefix_done = True

    def _mirror_silence(self, frames: int) -> None:
        """Feed the live preview the same silence so its clock matches the file."""
        if self.on_block is None:
            return
        chunk = max(1, int(self.source.samplerate * 5))
        remaining = int(frames)
        while remaining > 0 and self.on_block is not None:
            n = min(chunk, remaining)
            self._mirror_block(np.zeros((n, 1), dtype=np.float32))
            remaining -= n

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
        """How long this track has gone without audio, counting from the last
        restart rather than the last sample, so a device that cannot be reopened
        is retried once per stall_timeout instead of once per supervisor tick."""
        return (now or time.monotonic()) - max(self.last_progress, self._watchdog_mark)
