"""Builders for the split / combine tests: real WAVs whose samples identify their position."""

from __future__ import annotations

import json
import wave
from datetime import datetime

import numpy as np

from meeting_notes import wire

RATE = 4000  # WAV sample rate used in tests (small, so minutes of audio stay cheap)
CLOCK_SR = 48000  # the recorder's clock runs at the capture rate, not the WAV rate


def ramp(seconds: float, *, silent=(), start_sec: float = 0.0, rate: int = RATE) -> np.ndarray:
    """int16 audio whose value is ``(absolute frame) % 30000 + 1`` (loud, position-coded); zeros in ``silent`` spans."""
    n = int(round(seconds * rate))
    first = int(round(start_sec * rate))
    data = ((np.arange(first, first + n) % 30000) + 1).astype("<i2")
    for a, b in silent:
        data[int(a * rate): int(b * rate)] = 0
    return data


def read_wav(path):
    with wave.open(str(path), "rb") as fh:
        params = (fh.getnchannels(), fh.getsampwidth(), fh.getframerate(), fh.getnframes())
        data = np.frombuffer(fh.readframes(fh.getnframes()), dtype="<i2").copy()
    return params, data


def write_wav(path, samples: np.ndarray, rate: int = RATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(rate)
        fh.writeframes(np.asarray(samples, dtype="<i2").tobytes())


def timing_entries(t0: float, seconds: float, *, gaps=(), device="Test device") -> list:
    """A recorder timing log: frames in CLOCK_SR units, monotonic ``t0`` at frame 0."""
    entries = [{"event": "open", "segment": 0, "frames": 0, "t": t0, "wall": 1_700_000_000.0 + t0,
                "samplerate": CLOCK_SR, "channels": 1, "device": device}]
    step = 10
    for s in range(step, int(seconds), step):
        entries.append({"event": "progress", "segment": 0, "frames": s * CLOCK_SR, "t": t0 + s})
    for a, b, reason in gaps:
        entries.append({"event": "gap", "segment": 0, "frames_before": int(a * CLOCK_SR),
                        "frames_padded": int((b - a) * CLOCK_SR), "seconds_lost": b - a, "reason": reason, "t": t0 + a})
    entries.append({"event": "close", "segment": 0, "frames": int(seconds * CLOCK_SR), "t": t0 + seconds})
    return entries


def seg(start, end, label, text, track="mic", in_gap=False):
    return {"start": start, "end": end, "track": track, "label": label, "text": text,
            "in_gap": in_gap, "approximate": False}


def make_meeting(
    store,
    sid,
    name,
    started,
    duration,
    segments,
    *,
    mic=None,
    system=None,
    mic_timing=None,
    system_timing=None,
    device="Laptop",
    job_state=wire.JobState.DONE,
    extra_meta=None,
):
    """A finished meeting: meta, WAVs (if given), timing logs (if given), and a transcript."""
    tracks = {}
    for track, samples, timing in (("mic", mic, mic_timing), ("system", system, system_timing)):
        if samples is None:
            continue
        write_wav(store.track_wav_path(sid, track), samples)
        tracks[track] = {"samplerate": CLOCK_SR, "channels": 1, "device": f"{track} device", "label": track,
                         "frames": int(len(samples) / RATE * CLOCK_SR), "gaps": [], "segments": [], "warnings": []}
        if timing is not None:
            path = store.track_timing_path(sid, track)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("".join(json.dumps(e) + "\n" for e in timing), encoding="utf-8")
    meta = {
        "name": name, "device": device, "platform": "Windows 11", "started_wall": started,
        "created": datetime.fromtimestamp(started).isoformat(timespec="seconds"),
        "duration_sec": duration, "tracks": tracks, "upload": {"state": "complete", "percent": 100.0},
    }
    meta.update(extra_meta or {})
    store.write_session_meta(sid, meta)
    job_id = store.create_job(sid)
    store.write_transcript(job_id, "# Meeting transcript\n", json.dumps({"session": {}, "segments": segments}))
    store.update_job(job_id, state=job_state, progress=1.0)
    return job_id
