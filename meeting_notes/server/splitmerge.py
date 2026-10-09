"""Split one meeting into several, or combine several into one.

Everything here works on files the server already has; nothing is retranscribed.
A split slices each track's WAV at the chosen points and re-cuts the finished
transcript; a combine concatenates the tracks (filling the real-time gap between
recordings with silence) and shifts the transcripts onto one timeline. The
originals are moved to Recently deleted, never destroyed, so both operations are
reversible (``unsplit`` / ``uncombine``).

Timeline model. Transcript segment times are *session-relative* seconds (see
``transcribe.merge``): 0.0 is the earliest instant any track started. A track
WAV, though, starts at that track's own first frame, which can be a little after
session 0 and drifts against wall time. ``TrackAudio`` bridges the two using the
track's timing log (``timing.FrameClock``), so a session time maps to the right
WAV frame on each track, and a slice keeps the two tracks aligned.

No FastAPI imports here: ``splitmerge_api.py`` owns the HTTP surface.
"""

from __future__ import annotations

import json
import logging
import math
import os
import shutil
import threading
import time
import uuid
import wave
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

from .. import wire
from ..timing import FrameClock, load_timing_log
from ..transcribe.merge import render_json, render_markdown
from . import store as store_mod

logger = logging.getLogger("meeting_notes.server.splitmerge")

# -- tunables (documented in docs/architecture.md) -----------------------------------

MIN_PART_SEC = 10.0          # no part of a split may be shorter than this
MAX_PARTS = 50               # sanity cap on one split
SILENCE_MIN_SEC = 120.0      # quiet stretch worth suggesting a split for
GAP_MIN_SEC = 15.0           # audio-lost stretch worth suggesting a split for
SUGGESTION_MERGE_SEC = 60.0  # suggestions closer than this collapse to the best one
COMBINE_GAP_CAP_SEC = 600.0  # most silence inserted between two combined recordings
COMBINE_GAP_MARK_SEC = 1.0   # real gaps shorter than this are not filled or flagged
COMBINE_MAX = 20
CONTINUATION_WINDOW_SEC = 300.0  # "started within 5 minutes of the other ending"
RMS_WINDOW_SEC = 0.5
QUIET_RMS = 0.004            # ~ -48 dBFS; below this a window counts as silence
_CHUNK_FRAMES = 1 << 16

_CONF_RANK = {"high": 3, "medium": 2, "low": 1}


class SplitMergeError(Exception):
    """A request that cannot be honoured; ``status`` is the HTTP code to use."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# -- small helpers ----------------------------------------------------------------


def _num(value, default=None):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _fmt_dur(seconds: float) -> str:
    seconds = int(round(max(0.0, seconds)))
    if seconds >= 3600:
        return f"{seconds // 3600}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"
    if seconds >= 120:
        return f"{seconds // 60} min"
    return f"{seconds // 60}:{seconds % 60:02d}"


def _created_like(template, timestamp: float):
    """Format ``timestamp`` like the original meta's ``created`` (ISO text or epoch)."""
    if isinstance(template, str) and not template.strip().replace(".", "", 1).isdigit():
        return datetime.fromtimestamp(timestamp).isoformat(timespec="seconds")
    return timestamp


def _clean_track_meta(info: dict, *, duration: float, keep_late_attach: bool) -> dict:
    """A track's meta entry for a derived meeting: identity kept, per-recording detail dropped."""
    out = {k: v for k, v in info.items() if k not in ("segments", "gaps", "warnings", "abandoned_threads", "degraded")}
    out["duration_sec"] = round(duration, 3)
    rate = _num(info.get("samplerate") or info.get("sample_rate"))
    if rate:
        out["frames"] = int(round(duration * rate))
    if not keep_late_attach:
        out.pop("attached_late", None)
        out.pop("attach_gap_seconds", None)
    return out


# -- WAV access ---------------------------------------------------------------------


@dataclass
class TrackAudio:
    """One track of one meeting: its WAV, its clock, and session-time <-> frame maps."""

    track: str
    path: Path
    rate: int
    channels: int
    width: int
    nframes: int
    clock: Optional[FrameClock]
    earliest: float  # monotonic instant that is session time 0

    @property
    def duration(self) -> float:
        return self.nframes / self.rate if self.rate else 0.0

    @property
    def has_clock(self) -> bool:
        return self.clock is not None and len(self.clock.frames) > 0

    @property
    def offset_sec(self) -> float:
        """Session time at which this track's frame 0 lands (0 without a clock)."""
        if not self.has_clock:
            return 0.0
        return max(0.0, self.clock.start_monotonic - self.earliest)

    def _clock_frame_at(self, mono: float) -> float:
        clock = self.clock
        frames, times, sr = clock.frames, clock.times, clock.samplerate
        if len(frames) == 1 or mono <= times[0]:
            return float(frames[0] - (times[0] - mono) * sr)
        if mono >= times[-1]:
            return float(frames[-1] + (mono - times[-1]) * sr)
        return float(np.interp(mono, times, frames))

    def frame_at(self, session_s: float) -> int:
        """WAV frame index that plays at session time ``session_s`` (clamped to the file)."""
        if self.has_clock:
            sec = self._clock_frame_at(self.earliest + session_s) / self.clock.samplerate
        else:
            sec = session_s
        return int(min(max(round(sec * self.rate), 0), self.nframes))

    def session_at_frame(self, frame: float) -> float:
        if self.has_clock:
            return float(self.clock.monotonic_at(frame / self.rate * self.clock.samplerate) - self.earliest)
        return frame / self.rate

    def gap_intervals(self) -> List[Tuple[float, float, str]]:
        """Recorded audio-lost stretches as (start_s, end_s, reason) in session time."""
        if not self.has_clock:
            return []
        out = []
        sr = self.clock.samplerate
        for gap in self.clock.gaps:
            a = self.clock.monotonic_at(gap.frames_before) - self.earliest
            b = self.clock.monotonic_at(gap.frames_before + gap.frames_padded) - self.earliest
            if b > a:
                out.append((float(a), float(b), str(gap.reason or "")))
        return out


def _open_wav(path: Path) -> Tuple[int, int, int, int]:
    try:
        with wave.open(str(path), "rb") as fh:
            if fh.getcomptype() != "NONE":
                raise SplitMergeError(f"{path.name}: compressed WAV audio is not supported", 422)
            return fh.getframerate(), fh.getnchannels(), fh.getsampwidth(), fh.getnframes()
    except (wave.Error, EOFError, OSError) as exc:
        raise SplitMergeError(f"could not read audio file {path.name}: {exc}", 422) from exc


@dataclass
class Material:
    """Everything an operation needs to know about one finished meeting."""

    session_id: str
    meta: dict
    name: str
    started: float
    duration: float
    segments: List[dict]
    tracks: Dict[str, TrackAudio] = field(default_factory=dict)
    transcript_job_id: str = ""
    has_audio: bool = False

    @property
    def end(self) -> float:
        return self.started + self.duration


def _latest_job(store, session_id: str) -> Optional[dict]:
    jobs = store.jobs_for_session(session_id)
    return next((j for j in jobs if not store_mod.is_superseded_job(j)), jobs[0] if jobs else None)


def load_material(store, session_id: str, *, need_transcript: bool = True) -> Material:
    """Read a meeting into memory (metadata, transcript segments, audio handles).

    Raises ``SplitMergeError`` (404 unknown, 409 not finished) so callers can relay it.
    """
    if not store_mod.is_safe_id(session_id):
        raise SplitMergeError(f"invalid session id: {session_id!r}", 400)
    if not store.session_exists(session_id):
        raise SplitMergeError(
            "meeting is in Recently deleted" if store.is_trashed(session_id) else f"unknown meeting: {session_id}", 404
        )
    meta = store.read_session_meta(session_id)
    name = str(meta.get("name") or session_id)
    upload = meta.get("upload") if isinstance(meta.get("upload"), dict) else {}
    if upload.get("state") in ("pending", "uploading"):
        raise SplitMergeError(f"{name} is still uploading", 409)

    segments: List[dict] = []
    job_id = ""
    if need_transcript:
        latest = _latest_job(store, session_id)
        if latest is None or latest.get("state") != wire.JobState.DONE:
            raise SplitMergeError(f"{name} has not finished transcribing", 409)
        transcript = store.read_transcript(latest["job_id"])
        if transcript is None:
            raise SplitMergeError(f"{name} has no transcript", 409)
        try:
            payload = json.loads(transcript.get("json") or "{}")
        except json.JSONDecodeError as exc:
            raise SplitMergeError(f"{name} has an unreadable transcript", 409) from exc
        raw = payload.get("segments") if isinstance(payload, dict) else None
        for seg in raw if isinstance(raw, list) else []:
            if isinstance(seg, dict) and _num(seg.get("start")) is not None and _num(seg.get("end")) is not None:
                segments.append(seg)
        job_id = latest["job_id"]

    started = store_mod._parse_created_timestamp(meta.get("started_wall"))
    if started is None:
        started = store_mod._parse_created_timestamp(meta.get("created") or meta.get("date"))
    if started is None:
        row = store.session_index_row(session_id) or {}
        started = float(row.get("created") or 0.0)

    mat = Material(
        session_id=session_id, meta=meta, name=name, started=started, duration=0.0,
        segments=segments, transcript_job_id=job_id,
    )
    # Audio: only WAVs (raw files are the resumable-stream copy of the same audio).
    wavs: Dict[str, Tuple[Path, Tuple[int, int, int, int]]] = {}
    for track in wire.TRACKS:
        path = store.track_wav_path(session_id, track)
        if path.is_file():
            wavs[track] = (path, _open_wav(path))
    clocks: Dict[str, FrameClock] = {}
    for track in wavs:
        tpath = store.track_timing_path(session_id, track)
        if tpath.is_file():
            try:
                clock = load_timing_log(tpath)
            except (OSError, ValueError, KeyError):
                continue
            if len(clock.frames):
                clocks[track] = clock
    earliest = min((c.start_monotonic for c in clocks.values()), default=0.0)
    for track, (path, (rate, channels, width, nframes)) in wavs.items():
        mat.tracks[track] = TrackAudio(
            track=track, path=path, rate=rate, channels=channels, width=width, nframes=nframes,
            clock=clocks.get(track), earliest=earliest,
        )
    mat.has_audio = bool(mat.tracks)

    declared = _num(meta.get("duration_sec"))
    from_audio = max((t.offset_sec + t.duration for t in mat.tracks.values()), default=0.0)
    from_text = max((_num(s["end"], 0.0) for s in segments), default=0.0)
    mat.duration = max(declared or 0.0, from_audio, from_text)
    if mat.duration <= 0:
        raise SplitMergeError(f"{name} has no recorded length", 409)
    return mat


# -- RMS scan --------------------------------------------------------------------------

_PROFILE_CACHE: Dict[tuple, np.ndarray] = {}
_PROFILE_LOCK = threading.Lock()


def rms_profile(path: Path, window_sec: float = RMS_WINDOW_SEC) -> np.ndarray:
    """Normalized RMS (0..1) of consecutive ``window_sec`` windows of a mono/stereo int16 WAV."""
    stat = path.stat()
    key = (str(path), stat.st_mtime_ns, stat.st_size, window_sec)
    with _PROFILE_LOCK:
        cached = _PROFILE_CACHE.get(key)
    if cached is not None:
        return cached
    values: List[np.ndarray] = []
    with wave.open(str(path), "rb") as fh:
        rate, channels, width = fh.getframerate(), fh.getnchannels(), fh.getsampwidth()
        if width != 2:
            raise SplitMergeError(f"{path.name}: only 16-bit audio can be scanned", 422)
        win = max(1, int(rate * window_sec))
        block = win * 240  # ~2 minutes of windows per read
        leftover = np.zeros(0, dtype=np.float32)
        while True:
            data = fh.readframes(block)
            if not data:
                break
            samples = np.frombuffer(data[: len(data) - len(data) % (2 * channels)], dtype="<i2")
            if channels > 1:
                samples = samples.reshape(-1, channels).mean(axis=1)
            samples = np.concatenate([leftover, samples.astype(np.float32) / 32768.0])
            usable = (len(samples) // win) * win
            leftover = samples[usable:]
            if usable:
                frames = samples[:usable].reshape(-1, win)
                values.append(np.sqrt(np.mean(frames * frames, axis=1)))
        if len(leftover) >= max(1, win // 4):
            values.append(np.array([math.sqrt(float(np.mean(leftover * leftover)))], dtype=np.float32))
    result = np.concatenate(values) if values else np.zeros(0, dtype=np.float32)
    with _PROFILE_LOCK:
        if len(_PROFILE_CACHE) >= 8:
            _PROFILE_CACHE.pop(next(iter(_PROFILE_CACHE)))
        _PROFILE_CACHE[key] = result
    return result


def quiet_timeline(mat: Material, *, window_sec: float = RMS_WINDOW_SEC) -> Optional[np.ndarray]:
    """Boolean array over session time: True where every track is silent. None without audio."""
    if not mat.tracks:
        return None
    n = int(math.ceil(mat.duration / window_sec)) + 1
    quiet = np.ones(n, dtype=bool)
    times = np.arange(n) * window_sec
    for track in mat.tracks.values():
        profile = rms_profile(track.path)
        idx = np.floor((times - track.offset_sec) / window_sec).astype(int)
        inside = (idx >= 0) & (idx < len(profile))
        loud = np.zeros(n, dtype=bool)
        loud[inside] = profile[idx[inside]] >= QUIET_RMS
        quiet &= ~loud
    return quiet


def _runs(flags: np.ndarray, window_sec: float, min_sec: float) -> List[Tuple[float, float]]:
    out: List[Tuple[float, float]] = []
    start = None
    for i, flag in enumerate(list(flags) + [False]):
        if flag and start is None:
            start = i
        elif not flag and start is not None:
            a, b = start * window_sec, i * window_sec
            if b - a >= min_sec:
                out.append((a, b))
            start = None
    return out


# -- split suggestions ---------------------------------------------------------------------


def _overlap(a1: float, b1: float, a2: float, b2: float) -> float:
    return max(0.0, min(b1, b2) - max(a1, a2))


def _speech(segments: List[dict]) -> List[dict]:
    out = [
        s for s in segments
        if not s.get("in_gap") and str(s.get("text") or "").strip()
        and _num(s.get("end"), 0) >= _num(s.get("start"), 0)
    ]
    out.sort(key=lambda s: float(s["start"]))
    return out


def suggest_points(mat: Material, ai_suggestions: Optional[List[dict]] = None) -> dict:
    """Recommended split points for a meeting: long silences, lost audio, AI topic shifts."""
    duration = mat.duration
    cands: List[dict] = []

    # (b) recorded audio-lost stretches (computed first: silence that is one of these is reported as it) --------------------------------------------------------
    gaps: List[List] = []  # [start, end, set(tracks), reasons]
    for seg in mat.segments:
        if seg.get("in_gap"):
            gaps.append([float(seg["start"]), float(seg["end"]), {str(seg.get("track") or "")}, set()])
    for track in mat.tracks.values():
        for a, b, reason in track.gap_intervals():
            gaps.append([a, b, {track.track}, {reason} if reason else set()])
    gaps.sort(key=lambda g: g[0])
    merged: List[List] = []
    for g in gaps:
        if merged and g[0] <= merged[-1][1] + 2.0:
            merged[-1][1] = max(merged[-1][1], g[1])
            merged[-1][2] |= g[2]
            merged[-1][3] |= g[3]
        else:
            merged.append(g)
    # (a) silence on every track >= SILENCE_MIN_SEC ---------------------------------------
    quiet = quiet_timeline(mat)
    audio_runs = _runs(quiet, RMS_WINDOW_SEC, SILENCE_MIN_SEC) if quiet is not None else []
    speech = _speech(mat.segments)
    text_gaps: List[Tuple[float, float]] = []
    if speech:
        prev_end = float(speech[0]["end"])
        for seg in speech[1:]:
            start = float(seg["start"])
            if start - prev_end >= SILENCE_MIN_SEC:
                text_gaps.append((prev_end, start))
            prev_end = max(prev_end, float(seg["end"]))
    for a, b in audio_runs:
        if any(_overlap(a, b, ga, gb) >= 0.5 * (b - a) for ga, gb, _t, _r in merged):
            continue  # that silence is the recorded audio loss, reported below
        has_text = any(_overlap(a, b, float(s["start"]), float(s["end"])) > 0 for s in speech)
        cands.append({
            "time": (a + b) / 2, "kind": "silence", "confidence": "high", "weight": b - a,
            "label": f"Silence, {_fmt_dur(b - a)}",
            "reason": f"Every track was silent for {_fmt_dur(b - a)}"
            + (" (the transcript has text here, likely noise)." if has_text else " and nobody spoke."),
        })
    for a, b in text_gaps:
        if any(_overlap(a, b, ra, rb) >= 0.5 * (b - a) for ra, rb in audio_runs):
            continue
        if any(_overlap(a, b, ga, gb) >= 0.5 * (b - a) for ga, gb, _t, _r in merged):
            continue
        if quiet is None:
            cands.append({
                "time": (a + b) / 2, "kind": "silence", "confidence": "medium", "weight": b - a,
                "label": f"No speech, {_fmt_dur(b - a)}",
                "reason": f"Nobody spoke for {_fmt_dur(b - a)} (from the transcript; the audio is gone).",
            })
        else:
            cands.append({
                "time": (a + b) / 2, "kind": "silence", "confidence": "low", "weight": b - a,
                "label": f"No speech, {_fmt_dur(b - a)}",
                "reason": f"Nobody spoke for {_fmt_dur(b - a)}, but the audio is not silent (music or background noise).",
            })

    track_count = max(len(mat.tracks), 1)
    for a, b, tracks, reasons in merged:
        length = b - a
        if length < GAP_MIN_SEC:
            continue
        if "late-attach" in reasons and a <= 1.0:
            cands.append({
                "time": b, "kind": "late_attach", "confidence": "low", "weight": length,
                "label": "Device joined late",
                "reason": f"A device only started recording {_fmt_dur(length)} into the meeting; the start of it is silent.",
            })
            continue
        tracks.discard("")
        both = len(tracks) >= track_count
        cands.append({
            "time": (a + b) / 2, "kind": "gap",
            "confidence": "high" if (length >= 60 and both) else "medium", "weight": length,
            "label": f"Audio lost, {_fmt_dur(length)}",
            "reason": f"Recording lost audio for {_fmt_dur(length)}"
            + ("" if both or not tracks else f" on {', '.join(sorted(tracks))} only")
            + " (a device dropped out).",
        })

    # (c) AI topic shifts --------------------------------------------------------------------------
    for item in ai_suggestions or []:
        t = _num(item.get("time_sec"))
        title = str(item.get("title") or "").strip()
        if t is None or not title:
            continue
        cands.append({
            "time": t, "kind": "topic", "confidence": "medium", "weight": 0.0,
            "label": title, "reason": f"AI noticed the topic change to: {title}",
        })

    # Edge filter, collapse neighbours, sort ------------------------------------------------------
    cands = [c for c in cands if MIN_PART_SEC <= c["time"] <= duration - MIN_PART_SEC]
    cands.sort(key=lambda c: (-_CONF_RANK[c["confidence"]], -c["weight"]))
    kept: List[dict] = []
    for c in cands:
        if all(abs(c["time"] - k["time"]) >= SUGGESTION_MERGE_SEC for k in kept):
            kept.append(c)
    kept.sort(key=lambda c: c["time"])
    return {
        "duration_sec": round(duration, 3),
        "audio_scanned": quiet is not None,
        "suggestions": [
            {
                "time_sec": round(c["time"], 1), "reason": c["reason"], "confidence": c["confidence"],
                "label": c["label"], "kind": c["kind"],
            }
            for c in kept
        ],
    }


# -- AI suggestion queue ---------------------------------------------------------------------------


class SplitAiQueue:
    """Async, optional "find topic shifts" jobs for the bridge worker.

    Deliberately separate from the meeting-notes review queue: an older bridge
    that does not know this kind of work never sees these jobs (the claim
    endpoint only offers them to a bridge that asks for ``kinds=split_suggestions``).
    One small JSON file per job under ``<data>/split_ai/``.
    """

    STALE_SECONDS = 15 * 60
    MAX_SUGGESTIONS = 50

    def __init__(self, store):
        self.store = store
        self.dir = Path(store.root) / "split_ai"
        self._lock = threading.RLock()

    def _path(self, job_id: str) -> Path:
        store_mod._check_id(job_id, "split job")
        return self.dir / f"{job_id}.json"

    def _read(self, job_id: str) -> Optional[dict]:
        try:
            value = store_mod._read_json(self._path(job_id))
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def _write(self, job: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        store_mod._atomic_write_json(self._path(job["job_id"]), job)

    def read(self, job_id: str) -> Optional[dict]:
        with self._lock:
            return self._read(job_id)

    def _all(self) -> List[dict]:
        out = []
        if self.dir.exists():
            for p in self.dir.glob("*.json"):
                if store_mod.is_safe_id(p.stem):
                    job = self._read(p.stem)
                    if job:
                        out.append(job)
        return sorted(out, key=lambda j: j.get("created", 0), reverse=True)

    def latest(self, session_id: str, transcript_job_id: Optional[str] = None) -> Optional[dict]:
        with self._lock:
            for job in self._all():
                if job.get("session_id") != session_id:
                    continue
                if transcript_job_id and job.get("transcript_job_id") != transcript_job_id:
                    continue
                return job
        return None

    def enqueue(self, session_id: str, transcript_job_id: str, *, force: bool = False) -> dict:
        with self._lock:
            existing = self.latest(session_id, transcript_job_id)
            if existing and not force and existing.get("status") in ("queued", "running", "done"):
                return existing
            now = time.time()
            job = {
                "job_id": uuid.uuid4().hex, "session_id": session_id,
                "transcript_job_id": transcript_job_id, "status": "queued",
                "created": now, "updated": now, "claimed_at": None, "error": None, "suggestions": None,
            }
            self._write(job)
            return job

    def claim_next(self) -> Optional[dict]:
        now = time.time()
        with self._lock:
            jobs = self._all()
            for job in jobs:
                if job.get("status") == "running" and now - float(job.get("claimed_at") or 0) >= self.STALE_SECONDS:
                    job.update(status="queued", claimed_at=None, updated=now)
                    self._write(job)
            for job in reversed(self._all()):  # oldest first
                if job.get("status") != "queued":
                    continue
                if not self.store.session_exists(job.get("session_id", "")):
                    job.update(status="error", error="meeting no longer exists", updated=now)
                    self._write(job)
                    continue
                job.update(status="running", claimed_at=now, updated=now, error=None)
                self._write(job)
                return job
        return None

    @staticmethod
    def validate(value) -> List[dict]:
        """Shape-check a bridge result ``{"suggestions": [{time_sec, title}]}``."""
        if not isinstance(value, dict) or not isinstance(value.get("suggestions"), list):
            raise SplitMergeError("result must be an object with a suggestions list", 400)
        out = []
        for item in value["suggestions"][: SplitAiQueue.MAX_SUGGESTIONS]:
            if not isinstance(item, dict) or set(item) - {"time_sec", "title"}:
                raise SplitMergeError("each suggestion must have only time_sec and title", 400)
            t = _num(item.get("time_sec")) if not isinstance(item.get("time_sec"), bool) else None
            title = item.get("title")
            if t is None or t < 0 or not isinstance(title, str) or not title.strip():
                raise SplitMergeError("each suggestion needs a non-negative time_sec and a non-empty title", 400)
            out.append({"time_sec": round(t, 1), "title": title.strip()[:120]})
        return out

    def complete(self, job_id: str, result) -> dict:
        suggestions = self.validate(result)
        with self._lock:
            job = self._read(job_id)
            if job is None:
                raise SplitMergeError("unknown split-suggestions job", 404)
            if job.get("status") not in ("queued", "running"):
                raise SplitMergeError(f"job is not active: {job.get('status')}", 409)
            job.update(status="done", suggestions=suggestions, updated=time.time(), error=None)
            self._write(job)
            return job

    def fail(self, job_id: str, error: str) -> dict:
        with self._lock:
            job = self._read(job_id)
            if job is None:
                raise SplitMergeError("unknown split-suggestions job", 404)
            if job.get("status") not in ("queued", "running"):
                raise SplitMergeError(f"job is not active: {job.get('status')}", 409)
            job.update(status="error", error=str(error)[:500], updated=time.time())
            self._write(job)
            return job

    def public(self, job: Optional[dict]) -> dict:
        if not job:
            return {"status": "none", "suggestions": []}
        return {
            "status": job.get("status"), "job_id": job.get("job_id"),
            "error": job.get("error"), "suggestions": job.get("suggestions") or [],
        }


def ai_transcript_text(store, session_id: str, transcript_job_id: str) -> str:
    """Timestamped plain text for the topic-shift prompt: one line per segment."""
    transcript = store.read_transcript(transcript_job_id) or {}
    try:
        segments = json.loads(transcript.get("json") or "{}").get("segments") or []
    except (json.JSONDecodeError, AttributeError):
        segments = []
    lines = []
    for seg in segments:
        if not isinstance(seg, dict) or seg.get("in_gap") or not str(seg.get("text") or "").strip():
            continue
        start = _num(seg.get("start"), 0.0)
        label = str(seg.get("label") or seg.get("track") or "Speaker")
        lines.append(f"[{start:.1f}s] {label}: {str(seg['text']).strip()}")
    return "\n".join(lines) + "\n"


# -- building derived sessions ---------------------------------------------------------------------


def _new_ids(store, base_id: str, count: int) -> List[str]:
    """``<orig>_part1..N`` (or a uuid-based set when that would be unsafe or taken)."""
    def free(ids: List[str]) -> bool:
        return all(store_mod.is_safe_id(i) and not store.session_exists(i) and not store.is_trashed(i) for i in ids)

    for attempt in range(1, 20):
        prefix = base_id if attempt == 1 else f"{base_id}-s{attempt}"
        ids = [f"{prefix}_part{n}" for n in range(1, count + 1)]
        if free(ids):
            return ids
    prefix = uuid.uuid4().hex
    return [f"{prefix}_part{n}" for n in range(1, count + 1)]


def _write_timing(path: Path, entries: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry, separators=(",", ":")) + "\n")


def _slice_wav(src: TrackAudio, dst: Path, start: int, end: int) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(src.path), "rb") as fin, wave.open(str(dst), "wb") as out:
        out.setnchannels(src.channels)
        out.setsampwidth(src.width)
        out.setframerate(src.rate)
        fin.setpos(start)
        remaining = end - start
        while remaining > 0:
            data = fin.readframes(min(remaining, _CHUNK_FRAMES))
            if not data:
                break
            out.writeframes(data)
            remaining -= len(data) // (src.width * src.channels)


def _sliced_timing(src: TrackAudio, start: int, end: int) -> List[dict]:
    """A timing log for frames [start, end) of ``src`` that keeps original monotonic times."""
    clock = src.clock
    sr = clock.samplerate
    f0 = start / src.rate * sr
    f1 = end / src.rate * sr
    device = next((s.device for s in clock.segments if s.device), "")
    t0 = clock.monotonic_at(f0)
    t1 = clock.monotonic_at(f1)
    entries: List[dict] = [{
        "event": "open", "segment": 0, "frames": 0, "t": t0,
        "samplerate": int(sr), "channels": 1, "device": device,
    }]
    for f, t in zip(clock.frames.tolist(), clock.times.tolist()):
        if f0 < f < f1:
            entries.append({"event": "progress", "segment": 0, "frames": int(round(f - f0)), "t": t})
    for gap in clock.gaps:
        a, b = max(gap.frames_before, f0), min(gap.frames_after, f1)
        if b > a:
            entries.append({
                "event": "gap", "segment": 0, "frames_before": int(round(a - f0)),
                "frames_padded": int(round(b - a)), "seconds_lost": round((b - a) / sr, 3),
                "reason": gap.reason, "t": t0,
            })
    entries.append({"event": "progress", "segment": 0, "frames": int(round(f1 - f0)), "t": t1})
    entries.append({"event": "close", "segment": 0, "frames": int(round(f1 - f0)), "t": t1})
    return entries


def _finish_session(
    store, session_id: str, meta: dict, segments: List[dict], *, job_note: dict
) -> str:
    """Write meta + a finished transcript job so the new meeting reads as 'complete'."""
    store.write_session_meta(session_id, meta)
    md = render_markdown(segments, meta)
    js = render_json(segments, meta)
    job_id = store.create_job(session_id, {"derived": job_note})
    store.write_transcript(job_id, md, js)
    store.update_job(job_id, state=wire.JobState.DONE, progress=1.0, error=None)
    return job_id


def _cleanup_new(store, session_ids: List[str]) -> None:
    for sid in session_ids:
        try:
            store.delete_session(sid)
        except Exception:  # noqa: BLE001 - best-effort rollback
            logger.exception("splitmerge: could not clean up %s", sid)


def _mark_trash(store, session_id: str, **fields) -> None:
    entry = store._trash_entry(session_id)
    record = store._read_trash_record(session_id)
    if record is not None:
        record.update(fields)
        store_mod._atomic_write_json(entry / "trash.json", record)


def _queue_notes(store, ids: List[str]) -> List[str]:
    queued = []
    for sid in ids:
        try:
            store.create_review(sid)
            queued.append(sid)
        except Exception:  # noqa: BLE001 - notes are a convenience, never fail the operation
            logger.exception("splitmerge: could not queue notes for %s", sid)
    return queued


# -- split ---------------------------------------------------------------------------------------------


def validate_split_points(points, duration: float) -> List[float]:
    if not isinstance(points, list) or not points:
        raise SplitMergeError("points must be a non-empty list of seconds", 400)
    if len(points) > MAX_PARTS - 1:
        raise SplitMergeError(f"at most {MAX_PARTS - 1} split points are allowed", 400)
    cleaned: List[float] = []
    for p in points:
        value = _num(p) if not isinstance(p, bool) else None
        if value is None:
            raise SplitMergeError("every split point must be a number of seconds", 400)
        if not 0 < value < duration:
            raise SplitMergeError(
                f"split point {value:g}s is outside the meeting (it must be between 0 and {duration:.0f} seconds)", 400
            )
        cleaned.append(round(value, 3))
    cleaned.sort()
    bounds = [0.0] + cleaned + [duration]
    for lo, hi in zip(bounds, bounds[1:]):
        if hi - lo < MIN_PART_SEC:
            raise SplitMergeError(
                f"each part must be at least {MIN_PART_SEC:.0f} seconds; "
                f"the part from {lo:.0f}s to {hi:.0f}s is {hi - lo:.1f}s",
                400,
            )
    return cleaned


def split_session(
    store,
    session_id: str,
    points,
    *,
    names: Optional[list] = None,
    regenerate_notes: bool = False,
    ai_enabled: bool = False,
    is_live: Callable[[str], bool] = lambda _sid: False,
) -> dict:
    if is_live(session_id):
        raise SplitMergeError("this meeting is still recording; stop it before splitting", 409)
    mat = load_material(store, session_id)
    cuts = validate_split_points(points, mat.duration)
    count = len(cuts) + 1
    if names is not None:
        if not isinstance(names, list) or len(names) > count or not all(n is None or isinstance(n, str) for n in names):
            raise SplitMergeError("names must be a list of strings, one per part", 400)
        if any(len((n or "").strip()) > 200 for n in names):
            raise SplitMergeError("names must be 200 characters or fewer", 400)
    names = list(names or []) + [None] * (count - len(names or []))

    bounds = [0.0] + cuts + [mat.duration]
    ids = _new_ids(store, session_id, count)
    # One boundary frame per track and cut, so neighbouring parts never lose or repeat a sample.
    frame_bounds: Dict[str, List[int]] = {}
    for track, audio in mat.tracks.items():
        frame_bounds[track] = [0] + [audio.frame_at(c) for c in cuts] + [audio.nframes]

    created_ids: List[str] = []
    parts: List[dict] = []
    try:
        for n in range(count):
            pid, s0, s1 = ids[n], bounds[n], bounds[n + 1]
            last = n == count - 1
            created_ids.append(pid)
            track_meta: Dict[str, dict] = {}
            for track, audio in mat.tracks.items():
                a, b = frame_bounds[track][n], frame_bounds[track][n + 1]
                if b <= a:
                    continue  # this track had no audio in this part
                _slice_wav(audio, store.track_wav_path(pid, track), a, b)
                if audio.has_clock:
                    _write_timing(store.track_timing_path(pid, track), _sliced_timing(audio, a, b))
                info = (mat.meta.get("tracks") or {}).get(track)
                track_meta[track] = _clean_track_meta(
                    info if isinstance(info, dict) else {}, duration=(b - a) / audio.rate, keep_late_attach=(n == 0)
                )

            part_segments = []
            for seg in mat.segments:
                start, end = float(seg["start"]), float(seg["end"])
                mid = (start + end) / 2
                if s0 <= mid < s1 or (last and mid >= s1):
                    shifted = dict(seg)
                    shifted["start"] = round(min(max(start - s0, 0.0), s1 - s0), 3)
                    shifted["end"] = round(min(max(end - s0, 0.0), s1 - s0), 3)
                    part_segments.append(shifted)
            part_segments.sort(key=lambda d: (d["start"], str(d.get("track") or "")))

            base_name = mat.name
            custom = (names[n] or "").strip()
            part_name = custom or f"{base_name} (part {n + 1})"
            started = mat.started + s0
            meta = {k: v for k, v in mat.meta.items() if k not in ("tracks", "name_updated_at", "duration_sec")}
            meta.update({
                "name": part_name, "name_updated_at": time.time(),
                "started_wall": started,
                "created": _created_like(mat.meta.get("created"), started),
                "duration_sec": round(s1 - s0, 3), "tracks": track_meta,
                "split": {
                    "from": session_id, "from_name": mat.name, "part": n + 1, "of": count,
                    "offset_sec": round(s0, 3),
                },
            })
            _finish_session(
                store, pid, meta, part_segments, job_note={"op": "split", "from": session_id}
            )
            parts.append({
                "session_id": pid, "name": part_name, "started_wall": started,
                "duration_sec": round(s1 - s0, 3), "offset_sec": round(s0, 3),
                "segments": len(part_segments), "has_audio": bool(track_meta),
            })
        record = store.trash_session(session_id, source="split")
        if record is None:
            raise SplitMergeError("meeting disappeared while splitting", 409)
        _mark_trash(store, session_id, derived_kind="split", derived_ids=ids)
    except BaseException:
        _cleanup_new(store, created_ids)
        raise

    store.announce_derived("split", [session_id], ids)  # Notion: the original's copy goes, the parts follow it
    notes: List[str] = []
    if regenerate_notes and ai_enabled:
        notes = _queue_notes(store, ids)
    logger.info("split %s into %d parts", session_id, count)
    return {
        "original": {"session_id": session_id, "name": mat.name, "trashed": True},
        "parts": parts, "notes_queued": notes, "undo": f"/v1/sessions/{session_id}/unsplit",
    }


def unsplit(store, session_id: str) -> dict:
    """Undo a split: put the original back and remove the parts made from it."""
    record = store._read_trash_record(session_id)
    if record is None or record.get("derived_kind") != "split":
        raise SplitMergeError("this meeting was not split, or it is no longer in Recently deleted", 404)
    part_ids = [p for p in record.get("derived_ids") or [] if store_mod.is_safe_id(p)]
    try:
        store.restore_session(session_id)
    except store_mod.TrashConflict as exc:
        raise SplitMergeError(str(exc), 409) from exc
    removed = []
    for pid in part_ids:
        if store.session_exists(pid):
            store.trash_session(pid, source="unsplit")
        if store.purge_trashed(pid):
            removed.append(pid)
    store.announce_derived("unsplit", part_ids, [session_id])
    return {"restored": session_id, "removed": removed}


# -- combine -----------------------------------------------------------------------------------------------


def plan_combine(store, ids, *, is_live: Callable[[str], bool] = lambda _sid: False) -> List[Material]:
    """Validate a combine request and return the meetings in start-time order."""
    if not isinstance(ids, list) or len(ids) < 2:
        raise SplitMergeError("choose at least two meetings to combine", 400)
    if len(ids) > COMBINE_MAX:
        raise SplitMergeError(f"at most {COMBINE_MAX} meetings can be combined at once", 400)
    if len(set(ids)) != len(ids) or not all(isinstance(i, str) for i in ids):
        raise SplitMergeError("ids must be distinct meeting ids", 400)
    mats = []
    for sid in ids:
        if is_live(sid):
            raise SplitMergeError("a selected meeting is still recording; stop it before combining", 409)
        mats.append(load_material(store, sid))
    for mat in mats:
        if not mat.has_audio:
            raise SplitMergeError(f"audio was deleted for {mat.name}; combining needs audio", 409)
    mats.sort(key=lambda m: (m.started, m.session_id))
    # All parts of one track must share a format: no resampling is attempted.
    formats: Dict[str, Tuple[int, int, int]] = {}
    for mat in mats:
        for track, audio in mat.tracks.items():
            fmt = (audio.rate, audio.channels, audio.width)
            if formats.setdefault(track, fmt) != fmt:
                raise SplitMergeError(
                    f"{mat.name} has a different audio format than the other meetings; they cannot be combined", 422
                )
    return mats


def combine_timeline(mats: List[Material]) -> List[dict]:
    """Where each meeting lands on the combined timeline, with the gaps between them."""
    plan = []
    cursor = 0.0
    for i, mat in enumerate(mats):
        real_gap = 0.0
        inserted = 0.0
        if i:
            prev = mats[i - 1]
            real_gap = mat.started - prev.end
            if real_gap >= COMBINE_GAP_MARK_SEC:
                inserted = min(real_gap, COMBINE_GAP_CAP_SEC)
        start_at = cursor + inserted
        plan.append({
            "mat": mat, "offset": start_at, "gap_before": inserted, "real_gap": max(real_gap, 0.0),
            "capped": real_gap > COMBINE_GAP_CAP_SEC, "overlap": real_gap < 0,
        })
        cursor = start_at + mat.duration
    return plan


def combine_sessions(
    store,
    ids,
    *,
    name: Optional[str] = None,
    regenerate_notes: bool = False,
    ai_enabled: bool = False,
    is_live: Callable[[str], bool] = lambda _sid: False,
) -> dict:
    mats = plan_combine(store, ids, is_live=is_live)
    if name is not None:
        if not isinstance(name, str) or len(name.strip()) > 200:
            raise SplitMergeError("name must be a string of 200 characters or fewer", 400)
        name = name.strip() or None
    plan = combine_timeline(mats)
    total = plan[-1]["offset"] + plan[-1]["mat"].duration
    first = mats[0]
    new_name = name or first.name
    new_id = f"{first.session_id}_combined"
    if not (store_mod.is_safe_id(new_id) and not store.session_exists(new_id) and not store.is_trashed(new_id)):
        new_id = uuid.uuid4().hex

    track_names = [t for t in wire.TRACKS if any(t in m.tracks for m in mats)]
    track_meta: Dict[str, dict] = {}
    try:
        for track in track_names:
            ref = next(m.tracks[track] for m in mats if track in m.tracks)
            rate, channels, width = ref.rate, ref.channels, ref.width
            frame_bytes = channels * width
            dst = store.track_wav_path(new_id, track)
            dst.parent.mkdir(parents=True, exist_ok=True)
            gap_entries: List[dict] = []
            with wave.open(str(dst), "wb") as out:
                out.setnchannels(channels)
                out.setsampwidth(width)
                out.setframerate(rate)
                written = 0

                def silence(frames: int) -> None:
                    nonlocal written
                    while frames > 0:
                        n = min(frames, _CHUNK_FRAMES)
                        out.writeframes(b"\x00" * (n * frame_bytes))
                        written += n
                        frames -= n

                for step in plan:
                    mat = step["mat"]
                    if step["gap_before"] > 0:
                        before = written
                        silence(int(round(step["offset"] * rate)) - written)
                        gap_entries.append({
                            "event": "gap", "segment": 0, "frames_before": before, "frames_padded": written - before,
                            "seconds_lost": round((written - before) / rate, 3), "reason": "between-recordings",
                            "t": before / rate,
                        })
                    else:
                        silence(int(round(step["offset"] * rate)) - written)
                    part_start = written
                    audio = mat.tracks.get(track)
                    if audio is not None:
                        silence(int(round(audio.offset_sec * rate)))
                        with wave.open(str(audio.path), "rb") as fin:
                            while True:
                                data = fin.readframes(_CHUNK_FRAMES)
                                if not data:
                                    break
                                out.writeframes(data)
                                written += len(data) // frame_bytes
                        for a, b, reason in audio.gap_intervals():
                            fa = part_start + int(round(a * rate))
                            fb = part_start + int(round(b * rate))
                            if fb > fa:
                                gap_entries.append({
                                    "event": "gap", "segment": 0, "frames_before": fa, "frames_padded": fb - fa,
                                    "seconds_lost": round((fb - fa) / rate, 3), "reason": reason or "gap", "t": fa / rate,
                                })
                    silence(part_start + int(round(mat.duration * rate)) - written)
                silence(int(round(total * rate)) - written)
            device = ""
            for m in mats:
                if track in m.tracks:
                    info = (m.meta.get("tracks") or {}).get(track) or {}
                    device = str(info.get("device") or "")
                    if device:
                        break
            gap_entries.sort(key=lambda e: e["frames_before"])
            timing = [{
                "event": "open", "segment": 0, "frames": 0, "t": 0.0, "wall": first.started,
                "samplerate": rate, "channels": 1, "device": device,
            }] + gap_entries + [
                {"event": "progress", "segment": 0, "frames": written, "t": written / rate},
                {"event": "close", "segment": 0, "frames": written, "t": written / rate},
            ]
            _write_timing(store.track_timing_path(new_id, track), timing)
            ref_info = (first.meta.get("tracks") or {}).get(track)
            if not isinstance(ref_info, dict):
                ref_info = next(
                    ((m.meta.get("tracks") or {}).get(track) for m in mats if isinstance((m.meta.get("tracks") or {}).get(track), dict)),
                    {},
                )
            track_meta[track] = _clean_track_meta(ref_info, duration=written / rate, keep_late_attach=False)

        segments: List[dict] = []
        gaps_meta = []
        lane_track = track_names[0] if track_names else "system"
        for idx, step in enumerate(plan):
            mat, off = step["mat"], step["offset"]
            if step["gap_before"] > 0:
                end_gap = off
                start_gap = off - step["gap_before"]
                segments.append({
                    "start": round(start_gap, 3), "end": round(end_gap, 3), "track": lane_track,
                    "label": "", "text": "", "in_gap": True, "approximate": False,
                })
                gaps_meta.append({
                    "after": plan[idx - 1]["mat"].session_id, "inserted_sec": round(step["gap_before"], 3),
                    "real_gap_sec": round(step["real_gap"], 3), "capped": step["capped"],
                })
            for seg in mat.segments:
                shifted = dict(seg)
                shifted["start"] = round(float(seg["start"]) + off, 3)
                shifted["end"] = round(float(seg["end"]) + off, 3)
                segments.append(shifted)
        segments.sort(key=lambda s: (s["start"], str(s.get("track") or "")))

        devices = []
        for m in mats:
            d = str(m.meta.get("device") or "")
            if d and d not in devices:
                devices.append(d)
        meta = {k: v for k, v in first.meta.items() if k not in ("tracks", "name_updated_at", "duration_sec", "split", "upload")}
        if isinstance(first.meta.get("upload"), dict):
            meta["upload"] = dict(first.meta["upload"])
        meta.update({
            "name": new_name, "name_updated_at": time.time(),
            "started_wall": first.started, "created": _created_like(first.meta.get("created"), first.started),
            "duration_sec": round(total, 3), "tracks": track_meta,
            "combined_from": [m.session_id for m in mats],
            "combine": {
                "gap_cap_sec": COMBINE_GAP_CAP_SEC,
                "parts": [
                    {
                        "session_id": s["mat"].session_id, "name": s["mat"].name,
                        "started_wall": s["mat"].started, "offset_sec": round(s["offset"], 3),
                        "duration_sec": round(s["mat"].duration, 3),
                        "overlap": bool(s["overlap"]),
                    }
                    for s in plan
                ],
                "gaps": gaps_meta,
                "devices": devices if len(devices) > 1 else None,
            },
        })
        if meta["combine"]["devices"] is None:
            del meta["combine"]["devices"]
        _finish_session(store, new_id, meta, segments, job_note={"op": "combine", "from": [m.session_id for m in mats]})

        trashed: List[str] = []
        try:
            for mat in mats:
                if store.trash_session(mat.session_id, source="combine") is None:
                    raise SplitMergeError(f"{mat.name} disappeared while combining", 409)
                trashed.append(mat.session_id)
                _mark_trash(store, mat.session_id, derived_kind="combine", derived_ids=[new_id])
        except BaseException:
            for sid in reversed(trashed):
                try:
                    store.restore_session(sid)
                except Exception:  # noqa: BLE001
                    logger.exception("splitmerge: could not restore %s after a failed combine", sid)
            raise
    except BaseException:
        _cleanup_new(store, [new_id])
        raise

    store.announce_derived("combine", [m.session_id for m in mats], [new_id])
    notes: List[str] = []
    if regenerate_notes and ai_enabled:
        notes = _queue_notes(store, [new_id])
    logger.info("combined %s into %s", [m.session_id for m in mats], new_id)
    return {
        "session_id": new_id, "name": new_name, "duration_sec": round(total, 3),
        "sources": [m.session_id for m in mats], "gaps": gaps_meta, "notes_queued": notes,
        "undo": f"/v1/sessions/{new_id}/uncombine",
    }


def uncombine(store, session_id: str) -> dict:
    """Undo a combine: restore every original and remove the combined meeting."""
    if not store.session_exists(session_id):
        raise SplitMergeError("unknown meeting", 404)
    meta = store.read_session_meta(session_id)
    sources = [s for s in meta.get("combined_from") or [] if store_mod.is_safe_id(s)]
    if not sources:
        raise SplitMergeError("this meeting was not made by combining others", 409)
    for sid in sources:
        if not store.is_trashed(sid):
            raise SplitMergeError(f"{sid} is no longer in Recently deleted, so the combine cannot be undone", 409)
        if store.session_exists(sid):
            raise SplitMergeError(f"a meeting with id {sid} already exists", 409)
    restored = []
    try:
        for sid in sources:
            store.restore_session(sid)
            restored.append(sid)
    except store_mod.TrashConflict as exc:
        for sid in reversed(restored):
            store.trash_session(sid, source="combine")
        raise SplitMergeError(str(exc), 409) from exc
    store.trash_session(session_id, source="uncombine")
    store.purge_trashed(session_id)
    store.announce_derived("uncombine", [session_id], restored)
    return {"restored": restored, "removed": session_id}


# -- continuation hint -----------------------------------------------------------------------------------------


def continuations(store, session_id: str) -> dict:
    """The meeting (same device) that ended just before this one started, or started right after it ended."""
    row = store.session_index_row(session_id)
    if row is None:
        raise SplitMergeError("unknown meeting", 404)
    device = str(row.get("device") or "").strip()
    start = float(row.get("created") or 0.0)
    duration = _num(row.get("duration_sec"))
    out: Dict[str, Optional[dict]] = {"previous": None, "next": None}
    if not device or duration is None:
        return out
    end = start + duration
    best: Dict[str, Tuple[float, dict]] = {}
    for other in store.index.sessions_between(start - 6 * 3600, end + CONTINUATION_WINDOW_SEC + 60):
        oid = other["session_id"]
        if oid == session_id or str(other.get("device") or "").strip() != device:
            continue
        o_dur = _num(other.get("duration_sec"))
        if o_dur is None:
            continue
        o_start = float(other["created"])
        o_end = o_start + o_dur
        if -5.0 <= start - o_end <= CONTINUATION_WINDOW_SEC:          # other ended just before we began
            gap, kind = start - o_end, "previous"
        elif -5.0 <= o_start - end <= CONTINUATION_WINDOW_SEC:        # other began just after we ended
            gap, kind = o_start - end, "next"
        else:
            continue
        if kind not in best or abs(gap) < best[kind][0]:
            best[kind] = (abs(gap), {
                "session_id": oid, "name": other.get("name") or oid, "gap_sec": round(max(gap, 0.0), 1),
                "duration_sec": o_dur,
            })
    for kind, (_, info) in best.items():
        out[kind] = info
    return out
