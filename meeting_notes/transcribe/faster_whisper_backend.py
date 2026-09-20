"""The real transcription backend, backed by ``faster-whisper``.

This is the only module in the package allowed to import ``faster_whisper``,
and even here the import happens inside ``__init__`` rather than at module
scope -- so simply importing *this file* (e.g. because ``protocol.py``
resolved the name "faster-whisper") doesn't blow up until someone actually
tries to construct the transcriber, and the failure then comes with an
actionable message instead of a bare ``ModuleNotFoundError``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .protocol import BackendUnavailableError, Segment, register


class FasterWhisperTranscriber:
    """Transcribes WAV tracks with a single shared ``faster_whisper`` model.

    The model is loaded once, lazily, and reused across both the mic and
    system tracks -- loading a whisper model is the expensive part (seconds to
    tens of seconds, plus real RAM/VRAM), so doing it twice per meeting would
    be pure waste.
    """

    def __init__(
        self,
        model_size: str = "base.en",
        device: str = "auto",
        compute_type: str = "auto",
        language: Optional[str] = None,
    ):
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.language = language
        self._model = None  # loaded lazily, on first transcribe()

    def _load_model(self):
        if self._model is not None:
            return self._model
        try:
            import faster_whisper
        except ImportError as exc:
            raise BackendUnavailableError(
                "The 'faster-whisper' backend requires the faster-whisper "
                "package, which is not installed. Install it with: "
                "pip install 'meeting-notes[whisper]'"
            ) from exc
        self._model = faster_whisper.WhisperModel(
            self.model_size, device=self.device, compute_type=self.compute_type
        )
        return self._model

    def transcribe(self, wav_path: Path, track: str) -> list[Segment]:
        model = self._load_model()
        raw_segments, _info = model.transcribe(str(wav_path), language=self.language)
        return [
            Segment(start=float(s.start), end=float(s.end), text=s.text.strip(), track=track)
            for s in raw_segments
        ]


register("faster-whisper", FasterWhisperTranscriber)
