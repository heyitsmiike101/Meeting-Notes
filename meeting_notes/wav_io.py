"""Sample conversion and crash-safe audio file handling.

Recording appends raw little-endian mono int16 PCM to a ``.raw`` file. Nothing
in that file can be corrupted by an unclean exit, because there is no header to
keep in sync. A valid ``.wav`` is produced only at finalize time -- either on a
clean stop, or later via ``meeting-notes repair``.

This is deliberately not ``wave.Wave_write``: that class only patches its RIFF
and data chunk sizes in ``close()``, so a process killed mid-meeting leaves a
file that claims zero length and will not open.
"""

from __future__ import annotations

import json
import struct
import wave
from pathlib import Path
from typing import Iterator

import numpy as np

SAMPLE_WIDTH = 2  # int16
_COPY_CHUNK_FRAMES = 1 << 16


def downmix_mono(block: np.ndarray) -> np.ndarray:
    """Collapse a ``(frames, channels)`` block to ``(frames,)`` by averaging.

    Averaging, not summing: summing two correlated channels near full scale
    clips. Note this is also why we never ask the backend for ``channels=1`` --
    ``soundcard`` interprets that as a channel *map* and would hand back the
    left channel alone, silently discarding everything panned right.
    """
    arr = np.asarray(block, dtype=np.float32)
    if arr.ndim == 1:
        return arr
    if arr.shape[1] == 1:
        return arr[:, 0]
    return arr.mean(axis=1, dtype=np.float32)


def float_to_int16(block: np.ndarray) -> np.ndarray:
    """Convert float32 audio in [-1.0, 1.0] to int16, clamping and rounding.

    The naive ``(x * 32768).astype(np.int16)`` is wrong twice over: it truncates
    toward zero instead of rounding, and at exactly +1.0 it produces 32768,
    which wraps to -32768 and becomes an audible click at the loudest moment of
    the recording.
    """
    arr = np.asarray(block, dtype=np.float32)
    # NaN would survive the clip and turn into arbitrary garbage in the cast.
    arr = np.nan_to_num(arr, nan=0.0, posinf=1.0, neginf=-1.0)
    scaled = np.round(np.clip(arr, -1.0, 1.0) * 32767.0)
    return scaled.astype(np.int16)


class RawTrackWriter:
    """Append-only writer for one track's mono int16 PCM."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "ab")
        self.frames = self.path.stat().st_size // SAMPLE_WIDTH

    def write_float(self, block: np.ndarray) -> int:
        """Downmix, convert and append one capture block. Returns frames written."""
        return self.write_int16(float_to_int16(downmix_mono(block)))

    def write_int16(self, mono: np.ndarray) -> int:
        mono = np.ascontiguousarray(mono, dtype="<i2")
        self._fh.write(mono.tobytes())
        self.frames += mono.size
        return int(mono.size)

    def write_silence(self, numframes: int) -> int:
        """Pad the track with silence, used to hold wall-clock alignment over a gap."""
        if numframes <= 0:
            return 0
        return self.write_int16(np.zeros(int(numframes), dtype=np.int16))

    def flush(self) -> None:
        self._fh.flush()

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.flush()
            self._fh.close()

    def __enter__(self) -> "RawTrackWriter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def raw_frame_count(raw_path: Path) -> int:
    return Path(raw_path).stat().st_size // SAMPLE_WIDTH


def wrap_raw_as_wav(
    raw_path: Path,
    wav_path: Path,
    samplerate: int,
    *,
    remove_raw: bool = False,
) -> int:
    """Write ``raw_path``'s PCM into a valid mono WAV. Returns frames written.

    Streams in chunks so a multi-hour recording never has to fit in memory.
    Any trailing partial sample (possible if the process died mid-write) is
    dropped rather than shifting every subsequent sample by one byte.
    """
    raw_path, wav_path = Path(raw_path), Path(wav_path)
    frames = raw_frame_count(raw_path)
    wav_path.parent.mkdir(parents=True, exist_ok=True)

    with open(raw_path, "rb") as src, wave.open(str(wav_path), "wb") as dst:
        dst.setnchannels(1)
        dst.setsampwidth(SAMPLE_WIDTH)
        dst.setframerate(int(samplerate))
        remaining = frames
        while remaining > 0:
            take = min(remaining, _COPY_CHUNK_FRAMES)
            data = src.read(take * SAMPLE_WIDTH)
            if not data:
                break
            usable = len(data) - (len(data) % SAMPLE_WIDTH)
            dst.writeframes(data[:usable])
            remaining -= usable // SAMPLE_WIDTH

    if remove_raw:
        raw_path.unlink()
    return frames


def finalize_session(session_dir: Path, *, remove_raw: bool = False) -> dict:
    """Wrap every leftover ``.raw`` in a session directory into a ``.wav``.

    Used both on clean shutdown and by ``meeting-notes repair`` after a crash.
    Sample rates come from ``session.json``; a track missing from it falls back
    to 48000 with the guess recorded in the result.
    """
    session_dir = Path(session_dir)
    meta_path = session_dir / "session.json"
    meta = {}
    if meta_path.exists():
        meta = json.loads(meta_path.read_text())

    rates = {t: int(info.get("samplerate", 0)) for t, info in (meta.get("tracks") or {}).items()}
    results: dict = {}
    for raw_path in sorted(session_dir.glob("*.raw")):
        track = raw_path.stem
        rate = rates.get(track) or 48000
        wav_path = raw_path.with_suffix(".wav")
        frames = wrap_raw_as_wav(raw_path, wav_path, rate, remove_raw=remove_raw)
        results[track] = {
            "wav": str(wav_path),
            "frames": frames,
            "samplerate": rate,
            "duration_sec": round(frames / rate, 3) if rate else None,
            "rate_guessed": track not in rates,
        }
    return results


def read_wav_mono(wav_path: Path) -> tuple[np.ndarray, int]:
    """Read a mono 16-bit WAV back as float32 in [-1, 1]. Test/diagnostic helper."""
    with wave.open(str(wav_path), "rb") as fh:
        rate = fh.getframerate()
        frames = fh.readframes(fh.getnframes())
        channels = fh.getnchannels()
    data = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32767.0
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return data, rate
