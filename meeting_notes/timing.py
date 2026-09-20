"""Mapping a track's frame positions onto real wall-clock time.

Why this exists: frame counts are not a clock. Two capture devices run off two
different crystals, so over an hour their "48000 frames" drift apart by a
noticeable fraction of a second. Worse, on Windows ``soundcard`` synthesises
zero-filled blocks sized from elapsed wall time whenever the output device goes
idle, so a track's frame count quietly becomes a wall-clock estimate rather than
a real sample count.

So each recorder periodically logs ``(cumulative_frames, time.monotonic())`` to
a sidecar JSONL file, and this module turns that log into a frame -> time
mapping by piecewise-linear interpolation. Both tracks are timed by the same
process clock, so their mappings land in one shared timeline.

Gaps (a stalled or dead device) are handled at *record* time by padding the raw
file with silence for the lost duration, so a track's frames stay wall-clock
continuous and no transcript segment can straddle a discontinuity. The gap is
still logged, so the merged transcript can mark that stretch as lost audio.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

import numpy as np


class TimingLogWriter:
    """Appends a track's timing events. Progress points are throttled."""

    def __init__(self, path: Path, *, progress_interval: float = 1.0):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a", encoding="utf-8")
        self._interval = progress_interval
        self._last_logged = float("-inf")
        self._segment = -1

    def _emit(self, **entry) -> None:
        self._fh.write(json.dumps(entry, separators=(",", ":")) + "\n")
        self._fh.flush()

    def open_segment(self, frames: int, samplerate: int, channels: int, device: str) -> int:
        """Record the start of a contiguous capture run. Returns the segment index."""
        self._segment += 1
        now = time.monotonic()
        self._emit(
            event="open",
            segment=self._segment,
            frames=int(frames),
            t=now,
            wall=time.time(),
            samplerate=int(samplerate),
            channels=int(channels),
            device=device,
        )
        self._last_logged = now
        return self._segment

    def progress(self, frames: int, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and (now - self._last_logged) < self._interval:
            return
        self._emit(event="progress", segment=self._segment, frames=int(frames), t=now)
        self._last_logged = now

    def gap(self, frames_before: int, frames_padded: int, seconds_lost: float, reason: str) -> None:
        self._emit(
            event="gap",
            segment=self._segment,
            frames_before=int(frames_before),
            frames_padded=int(frames_padded),
            seconds_lost=round(float(seconds_lost), 3),
            reason=reason,
            t=time.monotonic(),
        )

    def close_segment(self, frames: int) -> None:
        self._emit(event="close", segment=self._segment, frames=int(frames), t=time.monotonic())

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.flush()
            self._fh.close()

    def __enter__(self) -> "TimingLogWriter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


@dataclass
class Gap:
    frames_before: int
    frames_padded: int
    seconds_lost: float
    reason: str

    @property
    def frames_after(self) -> int:
        return self.frames_before + self.frames_padded


@dataclass
class Segment:
    index: int
    samplerate: int
    device: str = ""
    start_frame: int = 0
    end_frame: int = 0
    start_monotonic: float = 0.0
    end_monotonic: float = 0.0

    @property
    def drift_ppm(self) -> Optional[float]:
        """Observed vs nominal sample rate, in parts per million.

        Positive means the device produced frames faster than its nominal rate.
        ``None`` when the segment is too short to measure meaningfully.
        """
        span = self.end_monotonic - self.start_monotonic
        frames = self.end_frame - self.start_frame
        if span < 5.0 or frames <= 0 or not self.samplerate:
            return None
        observed = frames / span
        return round((observed / self.samplerate - 1.0) * 1e6, 1)


@dataclass
class FrameClock:
    """Maps frame index -> monotonic time for one track."""

    samplerate: int
    frames: np.ndarray
    times: np.ndarray
    segments: list = field(default_factory=list)
    gaps: list = field(default_factory=list)

    @property
    def start_monotonic(self) -> float:
        return float(self.times[0]) if len(self.times) else 0.0

    @property
    def total_frames(self) -> int:
        return int(self.frames[-1]) if len(self.frames) else 0

    def monotonic_at(self, frame: float) -> float:
        """Interpolate the monotonic time at which ``frame`` was captured.

        Outside the logged range, extrapolate using the nominal sample rate --
        that covers the final partial second of a recording, whose progress
        point may never have been written.
        """
        if len(self.frames) == 0:
            return 0.0
        if len(self.frames) == 1:
            return float(self.times[0] + (frame - self.frames[0]) / self.samplerate)
        if frame <= self.frames[0]:
            return float(self.times[0] - (self.frames[0] - frame) / self.samplerate)
        if frame >= self.frames[-1]:
            return float(self.times[-1] + (frame - self.frames[-1]) / self.samplerate)
        return float(np.interp(frame, self.frames, self.times))

    def monotonic_at_seconds(self, wav_seconds: float) -> float:
        """Same, for a position expressed in seconds into the finished WAV."""
        return self.monotonic_at(wav_seconds * self.samplerate)

    def in_gap(self, wav_seconds: float) -> bool:
        """True if this position falls in silence padded over a lost stretch."""
        frame = wav_seconds * self.samplerate
        return any(g.frames_before <= frame < g.frames_after for g in self.gaps)


def load_timing_log(path: Path) -> FrameClock:
    """Build a FrameClock from a track's timing JSONL."""
    entries = []
    # errors="replace": a SIGKILL can tear the final line mid-character, and a
    # UnicodeDecodeError here would abort finalize() before the audio is wrapped.
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            # A kill mid-write can leave one torn final line. Everything before
            # it is still a perfectly good clock, so keep it.
            continue
    return build_clock(entries)


def build_clock(entries: Iterable[dict]) -> FrameClock:
    points: list = []
    segments: dict = {}
    gaps: list = []
    samplerate = 48000

    for entry in entries:
        event = entry.get("event")
        idx = entry.get("segment", 0)
        if event == "open":
            samplerate = int(entry.get("samplerate") or samplerate)
            seg = Segment(
                index=idx,
                samplerate=samplerate,
                device=entry.get("device", ""),
                start_frame=int(entry["frames"]),
                end_frame=int(entry["frames"]),
                start_monotonic=float(entry["t"]),
                end_monotonic=float(entry["t"]),
            )
            segments[idx] = seg
            points.append((float(entry["frames"]), float(entry["t"])))
        elif event in ("progress", "close"):
            seg = segments.get(idx)
            if seg is not None:
                seg.end_frame = int(entry["frames"])
                seg.end_monotonic = float(entry["t"])
            points.append((float(entry["frames"]), float(entry["t"])))
        elif event == "gap":
            gaps.append(
                Gap(
                    frames_before=int(entry["frames_before"]),
                    frames_padded=int(entry["frames_padded"]),
                    seconds_lost=float(entry.get("seconds_lost", 0.0)),
                    reason=entry.get("reason", "unknown"),
                )
            )

    # np.interp requires strictly increasing x. Frames only ever advance, but a
    # forced progress write can repeat the previous count; keep the last time
    # seen for any repeated frame position.
    points.sort(key=lambda p: p[0])
    frames: list = []
    times: list = []
    for f, t in points:
        if frames and f == frames[-1]:
            times[-1] = t
            continue
        frames.append(f)
        times.append(t)

    return FrameClock(
        samplerate=samplerate,
        frames=np.asarray(frames, dtype=np.float64),
        times=np.asarray(times, dtype=np.float64),
        segments=[segments[k] for k in sorted(segments)],
        gaps=gaps,
    )
