"""Optional local pyannote Community-1 speaker diarization backend."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable, Optional

from .diarize import SpeakerTurn
from .protocol import BackendUnavailableError

logger = logging.getLogger(__name__)


class PyannoteDiarizer:
    def __init__(
        self,
        model: str = "pyannote/speaker-diarization-community-1",
        token: Optional[str] = None,
        device: str = "cpu",
        min_speakers: Optional[int] = None,
        max_speakers: Optional[int] = None,
    ):
        try:
            import torch
            from pyannote.audio import Pipeline
        except ImportError as exc:
            raise BackendUnavailableError(
                "Speaker diarization is enabled but pyannote.audio is not installed. "
                "Install the 'diarization' extra or build Docker with "
                "INSTALL_DIARIZATION=true."
            ) from exc
        try:
            self.pipeline = Pipeline.from_pretrained(model, token=token or None)
        except Exception as exc:  # gated model/token errors need a useful boundary
            raise BackendUnavailableError(
                f"Could not load diarization model {model!r}. Accept its Hugging Face "
                "terms and set HUGGINGFACE_TOKEN, or disable diarization."
            ) from exc
        if device and device != "cpu":
            if device.startswith("cuda") and not torch.cuda.is_available():
                # Same fallback as transcription: a GPU that isn't usable must not fail every job.
                logger.warning("diarization device %s requested but CUDA is not available; using the CPU", device)
                device = "cpu"
            else:
                self.pipeline.to(torch.device(device))
        self.device = device
        # One pipeline is shared by the job thread and the in-person speakers worker: never run it twice at once.
        self._lock = threading.Lock()
        self.min_speakers = min_speakers
        self.max_speakers = max_speakers

    def diarize(self, wav_path: Path, progress: Optional[Callable[[float], None]] = None) -> list[SpeakerTurn]:
        """``progress``, when given, is called with the fraction (0..1) of the diarization done."""
        kwargs = {}
        if self.min_speakers is not None:
            kwargs["min_speakers"] = self.min_speakers
        if self.max_speakers is not None:
            kwargs["max_speakers"] = self.max_speakers
        if progress is not None:
            kwargs["hook"] = _progress_hook(progress)
        with self._lock:
            output = self.pipeline(_load_waveform(wav_path), **kwargs)
        annotation = getattr(output, "exclusive_speaker_diarization", None)
        if annotation is None:
            annotation = getattr(output, "speaker_diarization", output)
        return [
            SpeakerTurn(float(turn.start), float(turn.end), str(speaker))
            for turn, _, speaker in annotation.itertracks(yield_label=True)
        ]


# Share of the diarization time each pyannote step takes (measured on a GTX 1060 and on CPU: the speaker
# embeddings dominate). Steps not listed are quick and only move the bar to their start.
_STEP_SHARES = (("segmentation", 0.25), ("speaker_counting", 0.0), ("embeddings", 0.72), ("discrete_diarization", 0.03))


def _progress_hook(progress: Callable[[float], None]):
    """A pyannote pipeline ``hook`` that turns its per-step batch counts into one 0..1 fraction."""
    starts, total = {}, 0.0
    for name, share in _STEP_SHARES:
        starts[name] = (total, share)
        total += share

    def hook(step_name, step_artifact=None, file=None, total=None, completed=None):  # noqa: ARG001 - pyannote's API
        start, share = starts.get(step_name, (None, 0.0))
        if start is None:
            return
        fraction = start + (share * min(completed / total, 1.0) if total and completed is not None else 0.0)
        if step_artifact is not None and completed is None:
            fraction = start + share  # the step's final call carries its result
        try:
            progress(min(max(fraction, 0.0), 1.0))
        except Exception:  # noqa: BLE001 - progress is cosmetic; it must never fail the diarization
            logger.debug("diarization progress callback failed", exc_info=True)

    return hook


def _load_waveform(wav_path: Path):
    """``{"waveform", "sample_rate"}`` for pyannote, read with the standard library.

    Passing a path makes pyannote decode through torchcodec, which needs FFmpeg's shared libraries: present in
    the Docker image, missing on a Windows GPU helper ("torchcodec is not available"). Meeting tracks are always
    16-bit PCM WAV, so reading them directly avoids that dependency everywhere. Anything else falls back to the
    path.
    """
    import wave

    import numpy as np
    import torch

    try:
        with wave.open(str(wav_path), "rb") as wf:
            if wf.getsampwidth() != 2:
                return str(wav_path)
            channels, rate = wf.getnchannels(), wf.getframerate()
            pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype="<i2")
    except (wave.Error, EOFError):
        return str(wav_path)
    audio = pcm.astype(np.float32) / 32768.0
    audio = audio.reshape(-1, channels).T if channels > 1 else audio.reshape(1, -1)
    return {"waveform": torch.from_numpy(np.ascontiguousarray(audio)), "sample_rate": rate}

