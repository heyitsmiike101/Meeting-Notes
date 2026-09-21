"""Optional local pyannote Community-1 speaker diarization backend."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .diarize import SpeakerTurn
from .protocol import BackendUnavailableError


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
            self.pipeline.to(torch.device(device))
        self.min_speakers = min_speakers
        self.max_speakers = max_speakers

    def diarize(self, wav_path: Path) -> list[SpeakerTurn]:
        kwargs = {}
        if self.min_speakers is not None:
            kwargs["min_speakers"] = self.min_speakers
        if self.max_speakers is not None:
            kwargs["max_speakers"] = self.max_speakers
        output = self.pipeline(str(wav_path), **kwargs)
        annotation = getattr(output, "exclusive_speaker_diarization", None)
        if annotation is None:
            annotation = getattr(output, "speaker_diarization", output)
        return [
            SpeakerTurn(float(turn.start), float(turn.end), str(speaker))
            for turn, _, speaker in annotation.itertracks(yield_label=True)
        ]
