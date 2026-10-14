"""The "Both sides" playback track: a session's mic and system WAVs mixed into one.

Both track WAVs start at the session's t=0 (a device that attached late is
padded with silence by the recorder/uploader), so frame ``n`` of one track is the
same moment as frame ``n`` of the other once they are at the same rate and the
mix is aligned by frame index -- no timing log is needed.

The output is mono 16-bit at ``min(TARGET_RATE, the tracks' rates)``. Tracks of
other rates are resampled with linear interpolation and stereo is averaged to
mono. Each side is scaled by ``MIX_GAIN`` (0.5) before summing, so two full-scale
tracks can never clip, and a stretch where only the longer track is present has
the same level as one where both are. Work is done in blocks of ``BLOCK_SECONDS``
so a two-hour meeting never sits in memory.

The result is cached next to the tracks as ``mixed.mix`` -- deliberately not
``*.wav`` so it is never counted as a track or as audio (has_audio, audio_bytes,
retention sizes); ``Store.delete_session_audio`` removes it with the audio.
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
import wave
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import numpy as np

logger = logging.getLogger("meeting_notes.server.mixdown")

TARGET_RATE = 16000
MIX_GAIN = 0.5
BLOCK_SECONDS = 10.0
CACHE_NAME = "mixed.mix"
CACHE_GLOB = CACHE_NAME + "*"  # also the temp files of an interrupted build

_locks: Dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


class MixError(RuntimeError):
    """A source WAV could not be read for mixing."""


def _lock_for(path: Path) -> threading.Lock:
    key = str(path)
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


class _Source:
    """One WAV, readable as mono float samples at an arbitrary output rate."""

    def __init__(self, path: Path):
        try:
            self.fh = wave.open(str(path), "rb")
        except (wave.Error, EOFError, OSError) as exc:
            raise MixError(f"could not read {Path(path).name}: {exc}") from exc
        self.channels = self.fh.getnchannels()
        self.width = self.fh.getsampwidth()
        self.rate = self.fh.getframerate()
        self.frames = self.fh.getnframes()
        if self.width not in (1, 2, 3, 4) or self.rate <= 0 or self.channels < 1:
            self.fh.close()
            raise MixError(f"{Path(path).name}: unsupported WAV format")

    def close(self) -> None:
        self.fh.close()

    def out_frames(self, out_rate: int) -> int:
        return int(self.frames * out_rate / self.rate)

    def _read_mono(self, start: int, count: int) -> np.ndarray:
        """``count`` frames from frame ``start`` as mono float32 in [-1, 1]; short reads are zero padded."""
        if count <= 0 or start >= self.frames:
            return np.zeros(max(count, 0), dtype=np.float32)
        self.fh.setpos(start)
        data = self.fh.readframes(min(count, self.frames - start))
        usable = len(data) - (len(data) % (self.width * self.channels))
        data = data[:usable]
        if self.width == 1:
            samples = (np.frombuffer(data, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
        elif self.width == 2:
            samples = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
        elif self.width == 3:
            raw = np.frombuffer(data, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
            value = raw[:, 0] | (raw[:, 1] << 8) | (raw[:, 2] << 16)
            value = np.where(value & 0x800000, value - 0x1000000, value)
            samples = value.astype(np.float32) / 8388608.0
        else:
            samples = np.frombuffer(data, dtype="<i4").astype(np.float32) / 2147483648.0
        if self.channels > 1:
            samples = samples.reshape(-1, self.channels).mean(axis=1)
        if len(samples) < count:
            samples = np.concatenate([samples, np.zeros(count - len(samples), dtype=np.float32)])
        return samples

    def block(self, out_start: int, out_count: int, out_rate: int) -> np.ndarray:
        """Output samples ``[out_start, out_start + out_count)`` at ``out_rate`` (zeros past the end)."""
        total = self.out_frames(out_rate)
        n = max(0, min(out_count, total - out_start))
        result = np.zeros(out_count, dtype=np.float32)
        if n <= 0:
            return result
        if self.rate == out_rate:
            result[:n] = self._read_mono(out_start, n)
            return result
        positions = np.arange(out_start, out_start + n, dtype=np.float64) * (self.rate / out_rate)
        first = int(np.floor(positions[0]))
        last = int(np.floor(positions[-1])) + 1
        chunk = self._read_mono(first, last - first + 1)
        index = positions - first
        lo = np.floor(index).astype(np.int64)
        frac = (index - lo).astype(np.float32)
        hi = np.minimum(lo + 1, len(chunk) - 1)
        result[:n] = chunk[lo] * (1.0 - frac) + chunk[hi] * frac
        return result


def output_rate(rates: Sequence[int]) -> int:
    return min([TARGET_RATE, *[r for r in rates if r > 0]])


def mix_wavs(paths: Sequence[Path], dest: Path, *, block_seconds: float = BLOCK_SECONDS) -> int:
    """Mix ``paths`` into one mono 16-bit WAV at ``dest``; returns the frames written.

    Output length is that of the longest input. Frames are aligned by index (all
    inputs start at the session's t=0).
    """
    sources: List[_Source] = []
    try:
        for p in paths:
            sources.append(_Source(Path(p)))
        if not sources:
            raise MixError("nothing to mix")
        rate = output_rate([s.rate for s in sources])
        total = max(s.out_frames(rate) for s in sources)
        block = max(1, int(block_seconds * rate))
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(dest), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(rate)
            position = 0
            while position < total:
                count = min(block, total - position)
                mixed = np.zeros(count, dtype=np.float32)
                for s in sources:
                    mixed += s.block(position, count, rate)
                mixed *= MIX_GAIN
                pcm = np.clip(np.rint(mixed * 32767.0), -32768, 32767).astype("<i2")
                out.writeframes(pcm.tobytes())
                position += count
        return total
    finally:
        for s in sources:
            s.close()


def cache_is_fresh(cache: Path, sources: Sequence[Path]) -> bool:
    try:
        if not cache.is_file() or cache.stat().st_size <= 44:
            return False
        cache_time = cache.stat().st_mtime
        return all(cache_time >= Path(p).stat().st_mtime for p in sources)
    except OSError:
        return False


def ensure_mixed(
    sources: Sequence[Path], cache: Path, *, block_seconds: float = BLOCK_SECONDS
) -> Optional[Path]:
    """The cached mix of ``sources``, (re)built when missing or older than a source.

    Blocking (a long meeting takes a few seconds): call from a worker thread.
    Concurrent calls for the same cache serialize so the mix is built once.
    """
    sources = [Path(p) for p in sources]
    cache = Path(cache)
    if cache_is_fresh(cache, sources):
        return cache
    with _lock_for(cache):
        if cache_is_fresh(cache, sources):
            return cache
        tmp = cache.with_name(f"{cache.name}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            mix_wavs(sources, tmp, block_seconds=block_seconds)
            os.replace(tmp, cache)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
    return cache
