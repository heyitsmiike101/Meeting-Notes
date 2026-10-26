# COMPAT FIXTURE - do not edit. Verbatim copy of meeting_notes/client/compress.py from the 0.8.8 client
# (release/0.8.8), with only the meeting_notes.* imports rewritten to be package-relative.
"""Keep finished recordings small: each track becomes Ogg Opus at 24 kbps.

A recording is captured exactly as before (``mic.raw`` / ``system.raw``, wrapped into ``mic.wav`` /
``system.wav`` when it stops). Once it has stopped, :func:`compress_track` turns a WAV into ``{track}.opus``
(16 kHz mono, 24 kbps, ``voip`` tuning: roughly 2 MB per 20 minutes instead of 120 MB), checks the result by
decoding it again, and only then deletes the WAV. The Opus file is what the uploader sends to servers that accept
it (``/health`` ``track_formats``); for an older server it is decoded back to 16 kHz PCM (:func:`decode_to_pcm16`).

PyAV (``av``) does the codec work. If it is missing (an old build) or lacks ``libopus``, :func:`available` is
false and nothing here changes anything: recordings stay WAV and upload as PCM.

Nothing here raises out of :func:`compress_session`; a failure leaves the WAV untouched.
"""

from __future__ import annotations

import logging
import os
import threading
import wave
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np

from . import wire
from .resample import Downsampler

log = logging.getLogger("meeting_notes.client.compress")

OPUS_BITRATE = 24000
OPUS_SUFFIX = ".opus"
_FRAME_SAMPLES = wire.STREAM_SAMPLE_RATE // 50  # 20 ms
_READ_FRAMES = 1 << 17
# Compressed length must match the WAV's within this much (seconds, or this fraction, whichever is larger).
_TOLERANCE_SEC = 0.5
_TOLERANCE_FRACTION = 0.005

_locks_guard = threading.Lock()
_locks: Dict[str, threading.Lock] = {}


_av_import_error: Optional[str] = None


def _import_av():
    global _av_import_error
    try:
        import av  # noqa: WPS433

        return av
    except Exception as exc:  # noqa: BLE001 - ImportError, or a broken native library
        _av_import_error = f"{type(exc).__name__}: {exc}"
        return None


def available() -> bool:
    """PyAV imports and has the libopus encoder."""
    av = _import_av()
    if av is None:
        return False
    try:
        av.codec.Codec("libopus", "w")
        return True
    except Exception:  # noqa: BLE001
        return False


def session_lock(session_dir: Path) -> threading.Lock:
    """One lock per session folder, so two threads never compress the same session."""
    key = str(Path(session_dir).resolve())
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def opus_path_for(wav_path: Path) -> Path:
    return Path(wav_path).with_suffix(OPUS_SUFFIX)


def track_audio_path(session_dir: Path, track: str, info: Optional[Dict[str, Any]] = None) -> Optional[Path]:
    """The track's audio file on disk: the ``.opus`` copy if there is one, else the WAV, else ``None``."""
    session_dir = Path(session_dir)
    wav_name = info.get("wav") if isinstance(info, dict) else None
    wav_path = session_dir / (wav_name or f"{track}.wav")
    opus = opus_path_for(wav_path)
    if opus.is_file():
        return opus
    if wav_path.is_file():
        return wav_path
    return None


def audio_duration(path: Path) -> Optional[float]:
    """Seconds of audio in a WAV or Opus file, from headers only (no decoding). ``None`` if unreadable."""
    path = Path(path)
    if path.suffix.lower() == OPUS_SUFFIX:
        av = _import_av()
        if av is None:
            return None
        try:
            with av.open(str(path), format="ogg") as container:
                if container.duration:
                    return container.duration / 1_000_000.0
                stream = container.streams.audio[0]
                if stream.duration and stream.time_base:
                    return float(stream.duration * stream.time_base)
        except Exception:  # noqa: BLE001
            return None
        return None
    try:
        with wave.open(str(path), "rb") as fh:
            rate = fh.getframerate()
            return fh.getnframes() / rate if rate > 0 else None
    except (wave.Error, EOFError, OSError):
        return None


def has_audio(path: Path) -> bool:
    """A non-empty, readable WAV or Opus file."""
    duration = audio_duration(path)
    return duration is not None and duration > 0


def decode_to_pcm16(opus_path: Path, out_path: Path) -> int:
    """Decode an Opus (or any PyAV-readable) file to 16 kHz mono int16 PCM. Returns the frame count."""
    av = _import_av()
    if av is None:
        raise RuntimeError("PyAV is not available; cannot decode " + str(opus_path))
    total = 0
    resampler = av.AudioResampler(format="s16", layout="mono", rate=wire.STREAM_SAMPLE_RATE)
    with av.open(str(opus_path)) as container, open(out_path, "wb") as dst:
        stream = container.streams.audio[0]

        def write(frames) -> int:
            n = 0
            for rf in frames:
                arr = rf.to_ndarray().reshape(-1)[: rf.samples]
                dst.write(np.ascontiguousarray(arr, dtype="<i2").tobytes())
                n += rf.samples
            return n

        for packet in container.demux(stream):
            for frame in packet.decode():
                total += write(resampler.resample(frame))
        total += write(resampler.resample(None))
    return total


def _encode(wav_path: Path, tmp_path: Path) -> int:
    """WAV -> Ogg Opus at ``tmp_path``. Returns the number of 16 kHz samples encoded."""
    av = _import_av()
    if av is None:
        raise RuntimeError("PyAV is not available")
    rate_out = wire.STREAM_SAMPLE_RATE
    samples = 0
    with wave.open(str(wav_path), "rb") as src:
        if src.getnchannels() != 1 or src.getsampwidth() != 2:
            raise ValueError(f"{wav_path.name}: expected a mono 16-bit WAV")
        rate = src.getframerate()
        if src.getnframes() <= 0:
            raise ValueError(f"{wav_path.name}: no audio")
        downsampler = Downsampler(rate, rate_out)
        with av.open(str(tmp_path), mode="w", format="ogg") as out:
            stream = out.add_stream("libopus", rate=rate_out)
            stream.bit_rate = OPUS_BITRATE
            stream.layout = "mono"
            stream.codec_context.options = {"application": "voip", "frame_duration": "20"}
            pending = np.zeros(0, dtype="<i2")
            pts = 0

            def emit(block: np.ndarray) -> None:
                nonlocal pts
                frame = av.AudioFrame.from_ndarray(block.reshape(1, -1), format="s16", layout="mono")
                frame.sample_rate = rate_out
                frame.pts = pts
                pts += block.shape[0]
                for packet in stream.encode(frame):
                    out.mux(packet)

            while True:
                raw = src.readframes(_READ_FRAMES)
                if not raw:
                    break
                block = np.frombuffer(raw, dtype="<i2")
                pending = np.concatenate([pending, np.ascontiguousarray(downsampler.process(block), dtype="<i2")])
                usable = (pending.shape[0] // _FRAME_SAMPLES) * _FRAME_SAMPLES
                for start in range(0, usable, _FRAME_SAMPLES):
                    emit(pending[start : start + _FRAME_SAMPLES])
                samples += usable
                pending = pending[usable:]
            if pending.shape[0]:
                samples += pending.shape[0]
                emit(np.concatenate([pending, np.zeros(_FRAME_SAMPLES - pending.shape[0], dtype="<i2")]))
            for packet in stream.encode(None):
                out.mux(packet)
    return samples


def _fsync(path: Path) -> None:
    try:
        with open(path, "rb+") as fh:
            os.fsync(fh.fileno())
    except OSError:
        pass


def self_test() -> None:
    """Encode one second of a 48 kHz tone to Opus in a temp folder and check it (the packaged app's smoke test:
    a build without a working encoder must fail, not ship). Raises on any problem."""
    import tempfile

    if not available():
        raise RuntimeError("the Opus encoder (PyAV with libopus) is not available"
                           + (f" ({_av_import_error})" if _av_import_error else ""))
    with tempfile.TemporaryDirectory(prefix="mn-opus-check-") as tmp:
        wav = Path(tmp) / "mic.wav"
        rate = 48000
        tone = (np.sin(2 * np.pi * 440 * np.arange(rate) / rate) * 8000).astype("<i2")
        with wave.open(str(wav), "wb") as fh:
            fh.setnchannels(1)
            fh.setsampwidth(2)
            fh.setframerate(rate)
            fh.writeframes(tone.tobytes())
        out = compress_track(wav)
        if not out.exists() or out.stat().st_size < 100 or wav.exists():
            raise RuntimeError("the Opus self-test produced no usable file")


def compress_track(wav: Path) -> Path:
    """Compress ``wav`` to ``{track}.opus`` beside it, verify it, then delete the WAV. Raises on any failure.

    On failure the WAV is untouched and no temp file is left behind.
    """
    wav = Path(wav)
    final = opus_path_for(wav)
    tmp = final.with_name(final.name + ".tmp")
    sink = tmp.with_name(tmp.name + ".pcm")
    try:
        wav_seconds = audio_duration(wav)
        if not wav_seconds:
            raise ValueError(f"{wav.name}: cannot read its length")
        tmp.unlink(missing_ok=True)
        _encode(wav, tmp)
        _fsync(tmp)
        # Verify by decoding the result in full, not just reading the header.
        frames = decode_to_pcm16(tmp, sink)
        decoded_seconds = frames / float(wire.STREAM_SAMPLE_RATE)
        allowed = max(_TOLERANCE_SEC, wav_seconds * _TOLERANCE_FRACTION)
        if abs(decoded_seconds - wav_seconds) > allowed:
            raise ValueError(
                f"{wav.name}: compressed copy is {decoded_seconds:.2f}s but the recording is {wav_seconds:.2f}s"
            )
        os.replace(tmp, final)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    finally:
        sink.unlink(missing_ok=True)
    wav.unlink()
    return final


def compress_folder(save_dir: Path, active_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Compress every finished, not-yet-compressed recording under ``save_dir`` (offline recordings shrink too).

    Skips ``active_dir`` (the recording in progress) and sessions waiting in the upload queue (the uploader
    compresses those itself, right before sending). Never raises. Returns ``{folder name: compress_session result}``
    for the folders where something happened."""
    out: Dict[str, Any] = {}
    try:
        if not available():
            return out
        from meeting_notes.client.queue import SessionQueue
        from meeting_notes.client.recordings import session_folders

        queue = SessionQueue.for_save_dir(Path(save_dir))
        active = Path(active_dir).resolve() if active_dir else None
        for folder in session_folders(Path(save_dir)):
            try:
                if active is not None and folder.resolve() == active:
                    continue
                if not any(folder.glob("*.wav")) or queue.is_queued(folder):
                    continue
                result = compress_session(folder)
                if result["compressed"] or result["errors"]:
                    out[folder.name] = result
            except Exception as exc:  # noqa: BLE001
                log.warning("could not look at %s for compression: %s", folder, exc)
    except Exception as exc:  # noqa: BLE001
        log.warning("compress_folder(%s) failed: %s", save_dir, exc)
    return out


def compress_session(session_dir: Path, *, wait: bool = False) -> Dict[str, Any]:
    """Compress every finished WAV track of a session. Never raises.

    Returns ``{"compressed": [track, ...], "skipped": reason-or-None, "errors": {track: message}}``. A session that
    is still recording (any ``.raw`` file, or no ``session.json`` yet) is skipped, and the work is idempotent.
    If another thread is already compressing the session, ``wait=True`` (the uploader) waits for it; otherwise it
    is skipped.
    """
    session_dir = Path(session_dir)
    result: Dict[str, Any] = {"compressed": [], "skipped": None, "errors": {}}
    try:
        if not available():
            result["skipped"] = "opus encoder not available"
            return result
        if not session_dir.is_dir():
            result["skipped"] = "not a folder"
            return result
        lock = session_lock(session_dir)
        if not lock.acquire(blocking=wait):
            result["skipped"] = "already being compressed"
            return result
        try:
            if not (session_dir / "session.json").is_file() or any(session_dir.glob("*.raw")):
                result["skipped"] = "still recording or not finished"
                return result
            for track in wire.TRACKS:
                wav = session_dir / f"{track}.wav"
                if not wav.is_file():
                    continue
                try:
                    compress_track(wav)
                    result["compressed"].append(track)
                    log.info("compressed %s/%s to Opus", session_dir.name, track)
                except Exception as exc:  # noqa: BLE001
                    result["errors"][track] = f"{type(exc).__name__}: {exc}"
                    log.warning("could not compress %s/%s: %s", session_dir.name, track, exc)
        finally:
            lock.release()
    except Exception as exc:  # noqa: BLE001
        result["errors"]["session"] = f"{type(exc).__name__}: {exc}"
        log.warning("compress_session(%s) failed: %s", session_dir, exc)
    return result
