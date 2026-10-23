"""Removing the far end's echo from the mic track before it is transcribed.

When a meeting is played through speakers instead of a headset, the mic hears the other side too, and the
transcript gets their words twice: once from the system track ("Them") and again, a moment later, from the mic
("You"). The system track is the exact audio that went to the speakers, so it is the reference an acoustic echo
canceller needs. This runs WebRTC's AEC3 (through the ``livekit`` package's ``AudioProcessingModule``) over the
whole mic track offline, after the meeting.

Two things a live canceller never has to deal with, done here first:

* **Alignment.** AEC3 only finds an echo that arrives up to about half a second after its reference, and never
  one that arrives before it. Both track WAVs start at the session's t=0 (see ``server/mixdown.py``), but the
  echo's lag depends on the output device (Bluetooth adds hundreds of milliseconds) and the two devices' clocks
  drift apart over a long meeting. So the lag is measured from the recording itself (cross-correlation, block by
  block), and the reference is fed shifted so the echo always arrives ``TARGET_LAG`` after it.
* **Nothing to remove.** With a headset there is no echo, and no block finds a confident lag: the mic is then
  left exactly as it is. The same goes for a meeting whose far end is silent.

The cleaned mic is written next to the tracks as ``mic.aec`` (a WAV, deliberately not ``*.wav`` so it is never
counted as a track or as audio, like ``mixed.mix``), frame for frame the same length as ``mic.wav`` so its
timing log still applies. ``mic.aec.json`` records what was done; both are reused until a track changes.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
import wave
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger("meeting_notes.transcribe.echo")

RATE = 16000
FRAME = RATE // 100  # AEC3 works in 10 ms frames
CACHE_SUFFIX = ".aec"
CACHE_GLOB = "*" + CACHE_SUFFIX + "*"  # the cleaned WAV, its record, and temp files of an interrupted build

# Lag measurement: blocks of the reference are matched against the mic up to MAX_LAG either side.
LAG_BLOCK_SECONDS = 10.0
MAX_LAG_SECONDS = 2.0
# ...when the reference is loud enough there to have left an echo (dBFS of int16 samples, RMS).
ACTIVE_DB = 40.0
# One block's correlation is too weak to trust: a faint echo under your own voice looks like chance. Summed over
# WINDOW_BLOCKS active blocks the echo adds up at one lag while chance does not. A window counts when its peak
# stands Z_SCORE standard deviations above the rest of the search range...
WINDOW_BLOCKS = 12
Z_SCORE = 12.0
# ...and only when a second window agrees on the lag within AGREE_SECONDS (chance peaks land anywhere). A meeting
# too short for two windows needs one far clearer peak instead.
AGREE_SECONDS = 0.03
Z_SCORE_ALONE = 20.0
# Where the echo should land after the shifted reference: inside AEC3's comfortable range.
TARGET_LAG_SECONDS = 0.06
# AEC3's own output delay at 16 kHz, measured: frame n of its output is frame n - 128 of its input.
OUTPUT_DELAY = 128
STREAM_SECONDS = 10.0
# Bumped whenever detection or cancellation changes, so a stored result (even "no echo found") is redone.
VERSION = 1

_locks: Dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


class EchoUnavailable(RuntimeError):
    """The echo canceller cannot run here (the ``livekit`` package is not installed)."""


@dataclass
class EchoResult:
    """What ``remove_echo`` did, stored next to the cleaned file and on the job."""

    applied: bool
    reason: str = ""
    lag_ms: Optional[Tuple[float, float]] = None  # smallest and largest echo lag found
    blocks: int = 0  # lag measurements that agreed (windows of WINDOW_BLOCKS blocks)
    seconds: float = 0.0  # how long the work took
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["lag_ms"] = list(self.lag_ms) if self.lag_ms else None
        if not data["extra"]:
            data.pop("extra")
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "EchoResult":
        lag = data.get("lag_ms")
        return cls(
            applied=bool(data.get("applied")),
            reason=str(data.get("reason") or ""),
            lag_ms=(float(lag[0]), float(lag[1])) if isinstance(lag, list) and len(lag) == 2 else None,
            blocks=int(data.get("blocks") or 0),
            seconds=float(data.get("seconds") or 0.0),
        )


def cleaned_path(mic_wav: Path) -> Path:
    """Where the cleaned copy of ``mic_wav`` lives (``mic.wav`` -> ``mic.aec``)."""
    mic_wav = Path(mic_wav)
    return mic_wav.with_name(mic_wav.stem + CACHE_SUFFIX)


def record_path(mic_wav: Path) -> Path:
    return cleaned_path(mic_wav).with_name(cleaned_path(mic_wav).name + ".json")


# -- reading ------------------------------------------------------------------------------------------------


class _Track:
    """A WAV read as int16-scaled float64 mono samples at ``RATE``, by sample range (zeros outside the file)."""

    def __init__(self, path: Path):
        # mixdown's reader handles every WAV width, stereo and other rates; it returns [-1, 1] floats.
        from ..server.mixdown import _Source

        self._src = _Source(Path(path))
        self.frames = self._src.out_frames(RATE)

    def close(self) -> None:
        self._src.close()

    def read(self, start: int, count: int) -> np.ndarray:
        out = np.zeros(max(count, 0), dtype=np.float64)
        lo, hi = max(start, 0), min(start + count, self.frames)
        if hi > lo:
            out[lo - start: hi - start] = self._src.block(lo, hi - lo, RATE).astype(np.float64) * 32768.0
        return out


def _db(x: np.ndarray) -> float:
    return 10.0 * float(np.log10(np.mean(np.square(x, dtype=np.float64)) + 1e-9)) if len(x) else -90.0


# -- pass 1: where is the echo? -------------------------------------------------------------------------------


def _window_lag(curve: np.ndarray) -> Tuple[int, float]:
    """The peak of a summed correlation curve and how far it stands above the rest (in standard deviations)."""
    k = int(np.argmax(curve))
    guard = int(0.02 * RATE)  # the peak's own shoulders are not "the rest"
    rest = np.concatenate([curve[: max(0, k - guard)], curve[k + guard:]])
    if len(rest) < 2:
        return k, 0.0
    return k, float((curve[k] - np.median(rest)) / (np.std(rest) + 1e-12))


def measure_lags(mic: _Track, ref: _Track) -> Tuple[np.ndarray, np.ndarray]:
    """Echo lag (samples; positive = the mic hears it after the reference) where it is clear, by position.

    Each active block of the reference is cross-correlated against the mic over +-``MAX_LAG_SECONDS``, normalised,
    and the curves are summed over windows of ``WINDOW_BLOCKS`` active blocks (overlapping by half). A window's lag
    counts when its peak is clear (``Z_SCORE``) and another clear window agrees with it (``AGREE_SECONDS``); a
    meeting with a single window needs ``Z_SCORE_ALONE``. Returns ``(positions, lags)``, possibly empty."""
    block = int(LAG_BLOCK_SECONDS * RATE)
    max_lag = int(MAX_LAG_SECONDS * RATE)
    total = min(mic.frames, ref.frames)
    blocks: List[Tuple[int, float, np.ndarray]] = []  # (active block index, centre, normalised curve)
    windows: List[Tuple[float, int, float, int, int]] = []  # (centre, lag, z, first block, last block)
    step = max(1, WINDOW_BLOCKS // 2)
    index = 0

    def close_window(group) -> None:
        centre = float(np.mean([c for _, c, _ in group]))
        k, z = _window_lag(np.sum([curve for _, _, curve in group], axis=0))
        windows.append((centre, k - max_lag, z, group[0][0], group[-1][0]))

    for start in range(0, max(total - block, 0) + 1, block):
        x = ref.read(start, block)
        if _db(x) < ACTIVE_DB:
            continue
        y = mic.read(start - max_lag, block + 2 * max_lag)
        norm = float(np.linalg.norm(x) * np.linalg.norm(y))
        if norm <= 0:
            continue
        n = 1 << int(np.ceil(np.log2(len(y) + len(x))))
        cc = np.fft.irfft(np.fft.rfft(y, n) * np.conj(np.fft.rfft(x, n)), n)[: len(y) - len(x) + 1]
        blocks.append((index, start + block / 2.0, np.abs(cc) / norm))
        index += 1
        if len(blocks) == WINDOW_BLOCKS:
            close_window(blocks)
            blocks = blocks[step:]  # keep only what the next window shares; memory stays a few MB
    if blocks and (not windows or len(blocks) > WINDOW_BLOCKS - step):
        close_window(blocks)  # the tail (or the whole of a short meeting)
    if not windows:
        return np.zeros(0), np.zeros(0)

    def disjoint(a, b) -> bool:  # windows that share blocks are not independent evidence
        return a[4] < b[3] or b[4] < a[3]

    agree = AGREE_SECONDS * RATE
    if any(disjoint(a, b) for i, a in enumerate(windows) for b in windows[i + 1:]):
        clear = [w for w in windows if w[2] >= Z_SCORE]
        kept = [w for w in clear if any(disjoint(w, o) and abs(o[1] - w[1]) <= agree for o in clear)]
    else:  # too short for two independent windows: one far clearer peak instead
        kept = [w for w in windows if w[2] >= Z_SCORE_ALONE]
    if not kept:
        return np.zeros(0), np.zeros(0)
    lags = np.array([float(w[1]) for w in kept])
    # A stray agreeing pair at a wrong lag must not bend the curve: smooth over each window's neighbours.
    lags = np.array([np.median(lags[max(0, i - 1): i + 2]) for i in range(len(lags))])
    return np.array([w[0] for w in kept]), lags


# -- pass 2: cancel it ----------------------------------------------------------------------------------------


def _livekit_canceller():
    """A fresh AEC3 instance as ``(feed_reference, process_mic)`` callables on int16 10 ms frames."""
    try:
        from livekit import rtc
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise EchoUnavailable("the livekit package is not installed") from exc
    apm = rtc.AudioProcessingModule(
        echo_cancellation=True, noise_suppression=False, high_pass_filter=False, auto_gain_control=False)

    def feed(ref: np.ndarray) -> None:
        apm.process_reverse_stream(rtc.AudioFrame(ref.tobytes(), RATE, 1, FRAME))

    def process(mic: np.ndarray) -> np.ndarray:
        frame = rtc.AudioFrame(mic.tobytes(), RATE, 1, FRAME)
        apm.process_stream(frame)
        return np.frombuffer(frame.data, dtype=np.int16).copy()

    return feed, process


CancellerFactory = Callable[[], Tuple[Callable[[np.ndarray], None], Callable[[np.ndarray], np.ndarray]]]


def _cancel(mic: _Track, ref: _Track, centres: np.ndarray, lags: np.ndarray, dest: Path,
            factory: CancellerFactory, output_delay: int) -> None:
    """Stream the mic through the canceller with the lag-aligned reference; write ``dest`` (16 kHz mono)."""
    feed, process = factory()
    target = TARGET_LAG_SECONDS * RATE
    total = mic.frames
    chunk = int(STREAM_SECONDS * RATE) // FRAME * FRAME
    # The output runs ``output_delay`` behind: process that much past the end and drop it from the front.
    padded = total + output_delay
    to_skip = output_delay
    written = 0
    with wave.open(str(dest), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        for start in range(0, padded, chunk):
            count = min(chunk, padded - start)
            count = (count + FRAME - 1) // FRAME * FRAME
            m = np.clip(mic.read(start, count), -32768, 32767).astype(np.int16)
            # Reference sample feeding mic sample n: the echo heard at n left the speakers at n - lag(n), and
            # it should reach the canceller ``target`` before that.
            positions = np.arange(start, start + count, dtype=np.float64)
            shift = np.interp(positions, centres, lags) - target
            src = positions - shift
            lo = int(np.floor(src.min())) - 1
            hi = int(np.ceil(src.max())) + 2
            window = ref.read(lo, hi - lo)
            r = np.interp(src - lo, np.arange(hi - lo, dtype=np.float64), window)
            r = np.clip(np.rint(r), -32768, 32767).astype(np.int16)
            cleaned = np.empty(count, dtype=np.int16)
            for i in range(0, count, FRAME):
                feed(r[i: i + FRAME])
                cleaned[i: i + FRAME] = process(m[i: i + FRAME])
            if to_skip:
                drop = min(to_skip, len(cleaned))
                cleaned = cleaned[drop:]
                to_skip -= drop
            keep = min(len(cleaned), total - written)
            if keep > 0:
                out.writeframes(cleaned[:keep].astype("<i2").tobytes())
                written += keep


# -- the entry point ------------------------------------------------------------------------------------------


def _stamp(path: Path) -> List[int]:
    st = Path(path).stat()
    return [int(st.st_size), int(st.st_mtime_ns)]


def _lock_for(path: Path) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(str(path), threading.Lock())


def cached_result(mic_wav: Path, system_wav: Path) -> Optional[EchoResult]:
    """The stored result for exactly these tracks (size and time), else ``None``: it must be (re)built."""
    try:
        record = json.loads(record_path(mic_wav).read_text(encoding="utf-8"))
        sources = {"mic": _stamp(mic_wav), "system": _stamp(system_wav)}
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(record, dict) or record.get("version") != VERSION or record.get("sources") != sources:
        return None
    result = EchoResult.from_dict(record)
    if result.applied and not cleaned_path(mic_wav).is_file():
        return None
    return result


def available() -> bool:
    """Whether the canceller (the ``livekit`` package) is installed."""
    try:
        from livekit import rtc  # noqa: F401
    except ImportError:
        return False
    return True


def remove_echo(mic_wav: Path, system_wav: Path, *, factory: Optional[CancellerFactory] = None,
                output_delay: Optional[int] = None) -> EchoResult:
    """Write ``mic.aec`` with the far end's echo removed from ``mic_wav``, unless there is none to remove.

    Reuses a previous result while both tracks are unchanged; concurrent calls for one meeting build it once.
    Blocking (about a minute for two hours of audio): call it from a worker thread. Raises ``EchoUnavailable``
    when the canceller is not installed."""
    mic_wav, system_wav = Path(mic_wav), Path(system_wav)
    cached = cached_result(mic_wav, system_wav)
    if cached is not None:
        return cached
    if factory is None:
        if not available():
            raise EchoUnavailable("the livekit package is not installed")
        factory = _livekit_canceller
        if output_delay is None:
            output_delay = OUTPUT_DELAY
    dest = cleaned_path(mic_wav)
    with _lock_for(dest):
        cached = cached_result(mic_wav, system_wav)
        if cached is not None:
            return cached
        return _build(mic_wav, system_wav, dest, factory, output_delay or 0)


def _build(mic_wav: Path, system_wav: Path, dest: Path, factory: CancellerFactory, output_delay: int) -> EchoResult:
    began = time.monotonic()
    # Stamped before reading: a track rewritten while this runs no longer matches, so the result is redone.
    sources = {"mic": _stamp(mic_wav), "system": _stamp(system_wav)}
    tmp = dest.with_name(f"{dest.name}.{uuid.uuid4().hex[:8]}.tmp")
    mic = ref = None
    try:
        mic, ref = _Track(mic_wav), _Track(system_wav)
        centres, lags = measure_lags(mic, ref)
        if not len(lags):
            result = EchoResult(applied=False, reason="no echo found")
        else:
            _cancel(mic, ref, centres, lags, tmp, factory, output_delay)
            os.replace(tmp, dest)
            result = EchoResult(
                applied=True, blocks=len(lags),
                lag_ms=(round(float(lags.min()) * 1000 / RATE, 1), round(float(lags.max()) * 1000 / RATE, 1)))
    finally:
        for track in (mic, ref):
            if track is not None:
                track.close()
        try:
            tmp.unlink()
        except OSError:
            pass
    result.seconds = round(time.monotonic() - began, 2)
    if not result.applied:
        try:
            dest.unlink()
        except OSError:
            pass
    rec = record_path(mic_wav)
    rec_tmp = rec.with_name(f"{rec.name}.{uuid.uuid4().hex[:8]}.tmp")
    rec_tmp.write_text(json.dumps({**result.to_dict(), "version": VERSION, "sources": sources}), encoding="utf-8")
    os.replace(rec_tmp, rec)
    logger.info("echo removal for %s: %s", mic_wav.parent.name, result.to_dict())
    return result
