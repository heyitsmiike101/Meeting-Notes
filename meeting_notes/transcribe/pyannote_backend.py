"""Optional local pyannote Community-1 speaker diarization backend."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

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
        self.min_speakers = min_speakers
        self.max_speakers = max_speakers

    def diarize(self, wav_path: Path) -> list[SpeakerTurn]:
        kwargs = {}
        if self.min_speakers is not None:
            kwargs["min_speakers"] = self.min_speakers
        if self.max_speakers is not None:
            kwargs["max_speakers"] = self.max_speakers
        output = self.pipeline(_load_waveform(wav_path), **kwargs)
        annotation = getattr(output, "exclusive_speaker_diarization", None)
        if annotation is None:
            annotation = getattr(output, "speaker_diarization", output)
        return [
            SpeakerTurn(float(turn.start), float(turn.end), str(speaker))
            for turn, _, speaker in annotation.itertracks(yield_label=True)
        ]


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

