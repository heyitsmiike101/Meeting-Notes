"""Supervises the two track recorders and owns the session directory.

The supervisor runs on the main thread and does three things the capture
threads deliberately cannot do for themselves: surface their exceptions (an
exception inside a thread otherwise vanishes), notice a track that has stopped
making progress, and guarantee that one dead track never takes the session down
with it.
"""

from __future__ import annotations

import json
import platform
import queue
import re
import secrets
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Optional

from meeting_notes.audio.track_recorder import TrackError, TrackRecorder
from meeting_notes.timing import load_timing_log
from meeting_notes.wav_io import finalize_session

LABELS = {"mic": "You", "system": "Them"}


def create_session_dir(base: Path, name: Optional[str] = None) -> Path:
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") if name else ""
    # The directory name is also the session ID sent to the server. A timestamp
    # and meeting name alone collide when two client machines start the same
    # named meeting during the same second, causing their streams to share
    # files. Add a short host hint for operators and random entropy for actual
    # uniqueness; every character remains valid under the wire ID contract.
    host = re.sub(r"[^A-Za-z0-9._-]+", "-", socket.gethostname()).strip("-")[:24]
    unique = secrets.token_hex(4)
    parts = [stamp]
    if slug:
        parts.append(slug)
    if host:
        parts.append(host)
    parts.append(unique)
    directory = Path(base) / "_".join(parts)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


@dataclass
class SessionEvent:
    kind: str
    track: str
    detail: str
    elapsed: float


@dataclass
class RecordingSession:
    session_dir: Path
    sources: Dict[str, object]
    block_seconds: float = 0.5
    stall_timeout: float = 30.0
    progress_interval: float = 1.0
    on_block: object = None

    recorders: Dict[str, TrackRecorder] = field(default_factory=dict)
    events: list = field(default_factory=list)
    stop_event: threading.Event = field(default_factory=threading.Event)
    errors: "queue.Queue[TrackError]" = field(default_factory=queue.Queue)
    started_monotonic: float = 0.0
    started_wall: float = 0.0

    def start(self) -> None:
        self.session_dir = Path(self.session_dir)
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.started_monotonic = time.monotonic()
        self.started_wall = time.time()
        for track, source in self.sources.items():
            recorder = TrackRecorder(
                track,
                source,
                self.session_dir,
                self.stop_event,
                self.errors,
                block_seconds=self.block_seconds,
                progress_interval=self.progress_interval,
                on_block=self.on_block,
            )
            self.recorders[track] = recorder
            recorder.start()

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_monotonic if self.started_monotonic else 0.0

    def request_stop(self) -> None:
        self.stop_event.set()

    # -- supervision ---------------------------------------------------------

    def supervise(self, on_status: Optional[Callable[["RecordingSession"], None]] = None) -> None:
        """Block until stopped, watching for errors and stalls."""
        while not self.stop_event.is_set():
            self._drain_errors()
            self._check_stalls()
            if on_status:
                on_status(self)
            self.stop_event.wait(0.25)
        self._drain_errors()

    def _drain_errors(self) -> None:
        while True:
            try:
                err = self.errors.get_nowait()
            except queue.Empty:
                return
            self.events.append(
                SessionEvent("error", err.track, err.message, round(self.elapsed, 2))
            )

    def _check_stalls(self) -> None:
        now = time.monotonic()
        for track, recorder in self.recorders.items():
            if recorder.stuck_for(now) <= self.stall_timeout:
                continue
            # Nothing has arrived for stall_timeout seconds. The thread is very
            # likely wedged inside a blocking native read that will never
            # return, so restart() abandons it rather than waiting on it.
            self.events.append(
                SessionEvent(
                    "stall",
                    track,
                    f"no audio for {recorder.stuck_for(now):.0f}s, restarting device",
                    round(self.elapsed, 2),
                )
            )
            recorder.restart("stall")

    # -- shutdown ------------------------------------------------------------

    def finalize(self, join_timeout: float = 2.0) -> dict:
        """Stop capture, convert raw PCM to WAV, and write session.json."""
        self.stop_event.set()
        for recorder in self.recorders.values():
            recorder.close(join_timeout=join_timeout)
        self._drain_errors()

        meta = self._build_meta()
        self._write_meta(meta)

        # finalize_session reads sample rates back out of session.json, so the
        # metadata has to land on disk before the WAVs are wrapped.
        wrapped = finalize_session(self.session_dir, remove_raw=True)
        for track, info in wrapped.items():
            meta.setdefault("tracks", {}).setdefault(track, {}).update(
                {
                    "wav": Path(info["wav"]).name,
                    "frames": info["frames"],
                    "duration_sec": info["duration_sec"],
                }
            )
        meta["duration_sec"] = max(
            [t.get("duration_sec") or 0 for t in meta.get("tracks", {}).values()] or [0]
        )
        self._write_meta(meta)
        return meta

    def _build_meta(self) -> dict:
        tracks: dict = {}
        for track, recorder in self.recorders.items():
            timing_path = self.session_dir / f"{track}.timing.jsonl"
            segments: list = []
            gaps: list = []
            if timing_path.exists():
                clock = load_timing_log(timing_path)
                segments = [
                    {
                        "index": s.index,
                        "start_frame": s.start_frame,
                        "end_frame": s.end_frame,
                        "drift_ppm": s.drift_ppm,
                    }
                    for s in clock.segments
                ]
                gaps = [
                    {
                        "frames_before": g.frames_before,
                        "seconds_lost": g.seconds_lost,
                        "reason": g.reason,
                    }
                    for g in clock.gaps
                ]
            tracks[track] = {
                "device": recorder.source.name,
                "samplerate": recorder.source.samplerate,
                "channels": recorder.source.channels,
                "label": LABELS.get(track, track),
                "frames": recorder.frames,
                "degraded": recorder.degraded,
                "abandoned_threads": recorder.abandoned_threads,
                "segments": segments,
                "gaps": gaps,
                "warnings": recorder.warnings[:50],
            }

        return {
            "version": 1,
            "created": datetime.fromtimestamp(self.started_wall).isoformat(timespec="seconds"),
            "started_wall": self.started_wall,
            "platform": f"{platform.system()} {platform.release()}",
            "python": sys.version.split()[0],
            "block_seconds": self.block_seconds,
            "stall_timeout": self.stall_timeout,
            "tracks": tracks,
            "events": [
                {"kind": e.kind, "track": e.track, "detail": e.detail, "at": e.elapsed}
                for e in self.events
            ],
        }

    def _write_meta(self, meta: dict) -> None:
        (self.session_dir / "session.json").write_text(
            json.dumps(meta, indent=2, sort_keys=False), encoding="utf-8"
        )
