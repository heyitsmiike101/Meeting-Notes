"""The real transcription backend, backed by ``faster-whisper``.

This is the only module allowed to import ``faster_whisper``, and even here the
import happens on first use rather than at module scope -- so resolving the name
"faster-whisper" in the registry doesn't blow up until someone actually
transcribes, and the failure then carries install instructions instead of a bare
ModuleNotFoundError.

Two facts about faster-whisper shape most of what follows, both confirmed
against its source rather than assumed:

* ``model.transcribe()`` returns a LAZY generator. Iterating it is what performs
  the decoding, which is why progress reporting lives in the loop below and not
  around the call.
* Voice activity detection does NOT corrupt timestamps. VAD concatenates the
  speech chunks and decodes compressed audio, but ``restore_speech_timestamps``
  maps every segment back by adding the silence elided before it. Segment times
  are therefore in ORIGINAL audio time, which is what makes them safe to feed
  into FrameClock and merge onto the shared two-track timeline. That matters a
  lot here: each track is mostly silence (your mic while they talk, their audio
  while you talk), so VAD is both a large speed win and the main defence against
  Whisper inventing text over dead air.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, List, Optional

from .protocol import BackendUnavailableError, Segment, is_real_text, register

# Curated for CPU-only use, which is what this project targets. Any other name
# faster-whisper knows still works; these are just the ones worth defaulting to.
MODEL_CHOICES = ("base.en", "small.en", "large-v3-turbo")

MODEL_NOTES = {
    "tiny.en": ("~75 MB", "fastest, makes real errors on names"),
    "base.en": ("~145 MB", "good default for clear meeting audio"),
    "small.en": ("~480 MB", "better on accents and poor mics, ~2-3x slower"),
    "large-v3-turbo": ("~1.6 GB", "near large-v3 accuracy, far faster"),
    "large-v3": ("~3.1 GB", "best accuracy, impractical without a GPU"),
}

ProgressFn = Callable[[str, float, float], None]


def _resolve_compute_type(requested: str, device: str) -> str:
    """Pick a compute type that is actually fast on the resolved device.

    faster-whisper's own default is "default", which on CPU means float32 --
    correct but needlessly slow. int8 is the documented CPU recommendation and
    is what makes small.en and turbo usable on a laptop. We only pick float16
    when we know we are on CUDA, since float16 on CPU is slower than int8 and
    not universally supported.
    """
    if requested and requested != "auto":
        return requested
    return "float16" if device == "cuda" else "int8"


def _resolve_device(requested: str) -> str:
    """Resolve "auto" to a concrete device so compute_type can match it.

    Note there is no Apple Silicon GPU option to detect: CTranslate2 has no
    Metal backend, so a Mac is always CPU here regardless of the chip.
    """
    if requested and requested != "auto":
        return requested
    try:
        import ctranslate2

        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda"
    except Exception:
        pass
    return "cpu"


class FasterWhisperTranscriber:
    """Transcribes WAV tracks with a single shared ``faster_whisper`` model.

    The model is loaded once, lazily, and reused across both the mic and system
    tracks -- loading is the expensive part (seconds to tens of seconds, plus
    real RAM), so doing it per track would be pure waste.
    """

    def __init__(
        self,
        model_size: str = "base.en",
        device: str = "auto",
        compute_type: str = "auto",
        language: Optional[str] = None,
        *,
        beam_size: int = 5,
        vad_filter: bool = True,
        condition_on_previous_text: bool = False,
        initial_prompt: Optional[str] = None,
        threads: int = 0,
        download_root: Optional[str] = None,
        local_files_only: bool = True,
        on_progress: Optional[ProgressFn] = None,
    ):
        self.model_size = model_size
        self.device = _resolve_device(device)
        self.compute_type = _resolve_compute_type(compute_type, self.device)
        self.language = language
        self.beam_size = beam_size
        self.vad_filter = vad_filter
        # Off by default. Whisper's context carry-over is the mechanism behind
        # runaway repetition loops, and in a meeting transcript a paragraph of
        # duplicated text is far more damaging than the modest context it buys.
        # faster-whisper's own distil-model example disables it for the same
        # reason. Turn it back on if you find the output under-punctuated.
        self.condition_on_previous_text = condition_on_previous_text
        self.initial_prompt = initial_prompt
        self.threads = threads
        self.download_root = download_root
        # Offline-first. WhisperModel always routes through
        # huggingface_hub.snapshot_download, which contacts the Hub to check for
        # updates even when the model is fully cached -- so without this every
        # transcription needs working network, and one done on a plane or hotel
        # wifi hangs or fails for no reason. We try the cache, then fall back to
        # a real download only on a miss.
        self.local_files_only = local_files_only
        self.on_progress = on_progress
        self._model = None

    # -- model ---------------------------------------------------------------

    def load(self):
        """Load (downloading on first use) and cache the model."""
        if self._model is not None:
            return self._model
        try:
            import faster_whisper
        except ImportError as exc:
            raise BackendUnavailableError(
                "The 'faster-whisper' backend requires the faster-whisper "
                "package, which is not installed. Install it with:\n"
                "    pip install 'meeting-notes[whisper]'"
            ) from exc

        def build(local_only: bool):
            return faster_whisper.WhisperModel(
                self.model_size,
                device=self.device,
                compute_type=self.compute_type,
                cpu_threads=self.threads,
                download_root=self.download_root,
                local_files_only=local_only,
            )

        try:
            try:
                self._model = build(self.local_files_only)
            except Exception:
                if not self.local_files_only:
                    raise
                # Cache miss: fall back to downloading.
                self._model = build(False)
        except Exception as exc:
            raise BackendUnavailableError(
                f"Could not load Whisper model {self.model_size!r} on "
                f"{self.device} ({self.compute_type}): {exc}\n"
                f"If this is the first run, the model has to be downloaded "
                f"({MODEL_NOTES.get(self.model_size, ('unknown size', ''))[0]}); "
                f"pre-fetch it with: meeting-notes models --download "
                f"{self.model_size}"
            ) from exc
        return self._model

    # -- transcription -------------------------------------------------------

    def warn_if_language_unsupported(self) -> Optional[str]:
        """English-only models do not refuse other languages, they mangle them.

        faster-whisper only logs a warning through the logging module and then
        decodes as English anyway, which from a CLI is invisible -- you get
        confident, fluent, wrong text. Surface it so the caller can print it.
        """
        if self.language and self.language != "en" and self.model_size.endswith(".en"):
            return (
                f"model {self.model_size!r} is English-only, so --language "
                f"{self.language!r} will be ignored and the audio decoded as "
                f"English anyway. Use large-v3-turbo for other languages."
            )
        return None

    def transcribe(self, wav_path: Path, track: str) -> List[Segment]:
        model = self.load()
        raw_segments, info = model.transcribe(
            str(wav_path),
            language=self.language,
            beam_size=self.beam_size,
            # Passed explicitly rather than inherited: this defaulted to False
            # in faster-whisper 1.0.x and True from 1.2, and silently getting
            # the 1.0 behaviour would mean decoding hours of silence and
            # inviting hallucinated text over it.
            vad_filter=self.vad_filter,
            condition_on_previous_text=self.condition_on_previous_text,
            initial_prompt=self.initial_prompt,
        )

        total = float(getattr(info, "duration", 0.0) or 0.0)
        speech = float(getattr(info, "duration_after_vad", 0.0) or 0.0)
        if self.on_progress:
            # Reported before any decoding so the user learns up front how much
            # audio actually has to be processed -- on a meeting track that is
            # usually a small fraction of the file, and knowing that is the
            # difference between "this is working" and "this is hung".
            self.on_progress(track, 0.0, speech or total)

        segments: List[Segment] = []
        for raw in raw_segments:  # iterating is what runs the model
            text = (raw.text or "").strip()
            # Punctuation-only segments are Whisper's tell for "there was
            # nothing here" (see protocol.is_real_text); keep them out of the
            # transcript rather than rendering a paragraph of dots.
            if is_real_text(text):
                segments.append(
                    Segment(
                        start=float(raw.start),
                        end=float(raw.end),
                        text=text,
                        track=track,
                    )
                )
            if self.on_progress and total > 0:
                self.on_progress(track, min(float(raw.end) / total, 1.0), speech or total)

        if self.on_progress:
            self.on_progress(track, 1.0, speech or total)
        return segments


def model_is_downloaded(model_size: str, download_root: Optional[str] = None) -> bool:
    """Best-effort check for whether a model is already in the local cache.

    Used by `models` and `doctor` so the first transcription after a meeting is
    not where someone discovers a 1.6 GB download is needed.
    """
    try:
        from faster_whisper.utils import _MODELS
    except Exception:
        return False

    repo = _MODELS.get(model_size)
    if repo is None:
        return Path(model_size).is_dir()

    root = download_root or os.environ.get("HF_HOME") or os.path.join(
        Path.home(), ".cache", "huggingface"
    )
    root_path = Path(root)
    candidates = [
        root_path / "hub" / f"models--{repo.replace('/', '--')}",
        root_path / f"models--{repo.replace('/', '--')}",
    ]
    return any(c.is_dir() and any(c.rglob("model.bin")) for c in candidates)


def download_model(model_size: str, download_root: Optional[str] = None) -> str:
    """Fetch a model into the local cache and return where it landed."""
    try:
        from faster_whisper.utils import download_model as _download
    except ImportError as exc:
        raise BackendUnavailableError(
            "Downloading models requires faster-whisper. Install it with:\n"
            "    pip install 'meeting-notes[whisper]'"
        ) from exc
    return _download(model_size, output_dir=download_root)


register("faster-whisper", FasterWhisperTranscriber)
