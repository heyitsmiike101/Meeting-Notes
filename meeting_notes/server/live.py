"""The disposable live preview: fast, approximate, superseded by the final pass.

Design constraint from ``wire``'s module docstring: a CPU-only server can't
run a large model against two live streams and keep up with realtime. So this
does the cheapest thing that still looks live -- accumulate each track's PCM,
and every few seconds of new audio run Silero VAD (bundled inside
``faster-whisper``, no model download or network involved -- see
``meeting_notes.transcribe.faster_whisper_backend``'s module docstring for the
sibling fact about VAD and timestamps) to find utterances that have clearly
finished, and only transcribe those.

The "clearly finished" check (an utterance's end must be at least
``LIVE_MATURITY`` seconds in the past) matters because VAD run on a buffer
that's still growing will report the *in-progress* utterance as if it ended
right at the buffer's edge -- there's no way to distinguish "they just
stopped talking" from "they're mid-sentence and we haven't received the rest
yet" except by waiting to see whether more speech follows. Committing that
too early would mean a Partial for half a sentence, immediately followed a
few seconds later by another Partial for the other half -- worse than just
waiting one more interval.
"""

from __future__ import annotations

import logging
import os
import tempfile
import wave
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

from .. import wire
from ..transcribe.protocol import Transcriber

logger = logging.getLogger("meeting_notes.server.live")

LIVE_INTERVAL = 8.0  # seconds of NEW audio between VAD passes, per track
LIVE_MATURITY = 1.0  # only transcribe an utterance that ended at least this long ago
SAMPLE_RATE = wire.STREAM_SAMPLE_RATE


def _int16_to_float32(pcm: bytes) -> np.ndarray:
    if not pcm:
        return np.zeros(0, dtype=np.float32)
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0


def _write_temp_wav(pcm: bytes, sample_rate: int) -> Path:
    """Materialize one utterance as a short-lived WAV file.

    The ``Transcriber`` protocol takes a file path, not raw samples (that's
    the same seam the final pass uses), so live preview has to write one of
    these per mature utterance. They're small -- a few seconds of audio -- and
    removed right after transcribing.
    """
    fd, name = tempfile.mkstemp(prefix="meeting-notes-live-", suffix=".wav")
    os.close(fd)
    path = Path(name)
    with wave.open(str(path), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(sample_rate)
        fh.writeframes(pcm)
    return path


class _TrackBuffer:
    """Uncommitted PCM for one track, plus the bookkeeping to find mature
    utterances in it without ever re-transcribing already-committed audio."""

    def __init__(self, sample_rate: int):
        self.sample_rate = sample_rate
        self._buffer = bytearray()
        # Absolute frame index (since track start) that buffer[0] represents.
        # This IS the live-preview commit pointer: everything before it has
        # already become a Partial and is gone from the buffer for good, so
        # nothing downstream of it is ever scanned or transcribed twice.
        self._committed_frame = 0
        # Total frames ever fed. Compared against the frame count as of the
        # last VAD pass purely to throttle passes to once per
        # LIVE_INTERVAL seconds of genuinely new audio.
        self._total_frames = 0
        self._last_run_frames = 0

    def feed(self, pcm: bytes) -> None:
        self._buffer.extend(pcm)
        self._total_frames += len(pcm) // wire.BYTES_PER_FRAME

    def ready(self, interval: float) -> bool:
        return (self._total_frames - self._last_run_frames) >= interval * self.sample_rate

    def take_mature_chunks(self, maturity: float) -> List[Tuple[int, int, bytes]]:
        """Run VAD over the uncommitted buffer and return
        ``(start_frame_abs, end_frame_abs, pcm)`` for each utterance that
        ended at least ``maturity`` seconds ago, in ORIGINAL track-time frame
        indices, and advance the commit pointer past them.
        """
        self._last_run_frames = self._total_frames
        if not self._buffer:
            return []

        # Imported here, not at module scope: this whole module (indeed the
        # rest of the server) has to stay importable in an environment with no
        # faster-whisper installed -- live preview is simply unavailable then,
        # via LivePreview.enabled, rather than an ImportError at startup. Same
        # lazy-import seam as meeting_notes.transcribe.protocol.
        from faster_whisper.vad import VadOptions, get_speech_timestamps

        audio = _int16_to_float32(bytes(self._buffer))
        chunks = get_speech_timestamps(audio, VadOptions(), sampling_rate=self.sample_rate)

        mature_before = self._total_frames - int(maturity * self.sample_rate)
        results: List[Tuple[int, int, bytes]] = []
        committed_local = 0  # end of the last mature chunk, buffer-relative
        for chunk in chunks:
            start_local, end_local = int(chunk["start"]), int(chunk["end"])
            end_abs = self._committed_frame + end_local
            if end_abs > mature_before:
                # Chunks come back in time order, so once one isn't mature
                # yet, neither is anything after it -- stop here and leave
                # the rest of the buffer for the next pass.
                break
            start_abs = self._committed_frame + start_local
            pcm = bytes(
                self._buffer[start_local * wire.BYTES_PER_FRAME : end_local * wire.BYTES_PER_FRAME]
            )
            results.append((start_abs, end_abs, pcm))
            committed_local = end_local

        if committed_local:
            # Drop the now-committed prefix. The buffer, and every future VAD
            # pass, only ever covers audio not yet turned into a Partial.
            del self._buffer[: committed_local * wire.BYTES_PER_FRAME]
            self._committed_frame += committed_local

        return results


class LivePreview:
    """Live-preview state for every (session, track) currently streaming.

    With no transcriber configured this degrades to doing nothing at all --
    ``feed``/``poll`` become no-ops -- rather than erroring, because a server
    with no model must still be able to accept and store the stream; the
    upload-then-finalize path is what actually matters.
    """

    def __init__(
        self,
        transcriber_factory: Optional[Callable[[], Transcriber]] = None,
        *,
        interval: float = LIVE_INTERVAL,
        maturity: float = LIVE_MATURITY,
        sample_rate: int = SAMPLE_RATE,
    ):
        self.transcriber_factory = transcriber_factory
        self.interval = interval
        self.maturity = maturity
        self.sample_rate = sample_rate
        self._transcriber: Optional[Transcriber] = None
        self._buffers: Dict[Tuple[str, str], _TrackBuffer] = {}

    @property
    def enabled(self) -> bool:
        return self.transcriber_factory is not None

    def _transcriber_instance(self) -> Optional[Transcriber]:
        if not self.enabled:
            return None
        if self._transcriber is None:
            # Built once, lazily, and reused for every utterance on every
            # track streamed to this server -- constructing it per utterance
            # would make "live" preview slower than just waiting for the
            # final pass.
            self._transcriber = self.transcriber_factory()
        return self._transcriber

    def feed(self, session_id: str, track: str, pcm: bytes) -> None:
        """Add newly-committed (contiguous, in-order) PCM for one track.

        Callers should only ever feed the contiguous prefix the store has
        acknowledged (see app.py's websocket handler) -- this module has no
        way to detect or fill a gap, it just trusts what it's given is really
        contiguous original-time audio.
        """
        if not self.enabled:
            return
        key = (session_id, track)
        buf = self._buffers.get(key)
        if buf is None:
            buf = _TrackBuffer(self.sample_rate)
            self._buffers[key] = buf
        buf.feed(pcm)

    def forget_session(self, session_id: str) -> None:
        """Drop a session's buffers once its stream ends -- nothing more will
        ever be committed for it, so holding the audio in memory is waste."""
        for key in [k for k in self._buffers if k[0] == session_id]:
            del self._buffers[key]

    def poll(self, session_id: str, track: str) -> List[wire.Partial]:
        """Run a VAD pass if enough new audio has arrived, and transcribe
        whatever matured. Returns zero or more Partials in ORIGINAL track
        time (absolute seconds since the track started), never buffer- or
        utterance-WAV-relative time.
        """
        if not self.enabled:
            return []
        buf = self._buffers.get((session_id, track))
        if buf is None or not buf.ready(self.interval):
            return []

        chunks = buf.take_mature_chunks(self.maturity)
        if not chunks:
            return []

        transcriber = self._transcriber_instance()
        partials: List[wire.Partial] = []
        for start_abs, _end_abs, pcm in chunks:
            wav_path = _write_temp_wav(pcm, self.sample_rate)
            try:
                segments = transcriber.transcribe(wav_path, track)
            except Exception:
                # A live-preview failure is never allowed to be fatal to the
                # session -- only the final pass has to actually succeed.
                logger.exception("live preview transcription failed for %s/%s", session_id, track)
                segments = []
            finally:
                try:
                    wav_path.unlink()
                except OSError:
                    pass

            # seg.start/end are relative to the utterance WAV we just wrote
            # (0.0 at its first sample). Shift by the utterance's absolute
            # start so what we emit is original track time.
            base = start_abs / self.sample_rate
            for seg in segments:
                text = (seg.text or "").strip()
                if not text:
                    continue
                partials.append(
                    wire.Partial(track=track, start=base + seg.start, end=base + seg.end, text=text)
                )
        return partials
