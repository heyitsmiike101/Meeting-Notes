"""Whisper and pyannote on the local CUDA GPU, loaded from the helper pack's ``models/`` directory.

Models are cached across jobs. torch / ctranslate2 / faster-whisper / pyannote are imported lazily, and
``prepare_dll_paths`` must run before ctranslate2 is first imported on Windows.
"""

from __future__ import annotations

import gc
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

log = logging.getLogger("meeting_notes.compute_helper")

_dll_dirs_added = False


def prepare_dll_paths() -> List[str]:
    """Windows: make the pip-installed cuBLAS / cuDNN and torch DLLs findable by ctranslate2. Idempotent."""
    global _dll_dirs_added
    added: List[str] = []
    if _dll_dirs_added or not sys.platform.startswith("win") or not hasattr(os, "add_dll_directory"):
        return added
    _dll_dirs_added = True
    roots = {Path(p) for p in sys.path if p and p.lower().endswith("site-packages")}
    candidates: List[Path] = []
    for root in roots:
        nvidia = root / "nvidia"
        if nvidia.is_dir():
            candidates.extend(sorted(nvidia.glob("*/bin")))
        candidates.append(root / "torch" / "lib")
    for directory in candidates:
        if directory.is_dir():
            try:
                os.add_dll_directory(str(directory))
                os.environ["PATH"] = str(directory) + os.pathsep + os.environ.get("PATH", "")
                added.append(str(directory))
            except OSError:
                log.debug("could not add DLL directory %s", directory, exc_info=True)
    return added


class Engine:
    """What the worker needs from the GPU stack. Tests substitute a fake with the same methods."""

    def __init__(self, models_dir: "Path | str", *, gpu_name: Optional[str] = None):
        self.models_dir = Path(models_dir)
        self.gpu_name = gpu_name or ""
        self.compute_type: Optional[str] = None  # of the loaded Whisper model
        self.last_used = time.monotonic()
        self._whisper: Dict[str, object] = {}  # batch instances (idle unload applies to these only)
        self._diarizers: Dict[str, object] = {}
        # Live uses its own model instances, never a batch transcriber (even for the same model id), each
        # side guarded by its own lock; per-job batch options are only ever set under the batch lock.
        self._batch_lock = threading.RLock()
        self._live_lock = threading.Lock()
        self._live_whisper: Dict[str, object] = {}
        self.live_compute_type: Optional[str] = None
        prepare_dll_paths()

    @property
    def device_label(self) -> str:
        label = f"cuda:{self.gpu_name}" if self.gpu_name else "cuda"
        return f"{label} ({self.compute_type})" if self.compute_type else label

    @staticmethod
    def compute_type_candidates() -> List[str]:
        """What this GPU supports, best first. Pascal cards (GTX 10xx) have no efficient float16."""
        preferred = ["float16", "int8_float16", "int8_float32", "float32"]
        try:
            prepare_dll_paths()
            import ctranslate2

            supported = set(ctranslate2.get_supported_compute_types("cuda"))
        except Exception:  # noqa: BLE001 - fall through to trying everything
            return preferred
        ordered = [c for c in preferred if c in supported]
        return ordered or preferred

    # -- availability --------------------------------------------------------

    def whisper_path(self, name: str) -> Path:
        return self.models_dir / "whisper" / name

    def diarization_path(self) -> Path:
        return self.models_dir / "diarization"

    def can_diarize(self) -> bool:
        path = self.diarization_path()
        return path.is_dir() and any(path.iterdir())

    # -- whisper -------------------------------------------------------------

    def _transcriber(self, name: str):
        cached = self._whisper.get(name)
        if cached is not None:
            return cached
        from ..transcribe.faster_whisper_backend import FasterWhisperTranscriber
        from ..transcribe.protocol import BackendUnavailableError

        path = self.whisper_path(name)
        if not path.is_dir():
            raise RuntimeError(f"Whisper model {name!r} is not in the helper pack ({path})")
        last: Optional[Exception] = None
        for compute_type in self.compute_type_candidates():
            transcriber = FasterWhisperTranscriber(
                str(path), device="cuda", compute_type=compute_type, local_files_only=True,
                cpu_fallback=False)
            try:
                transcriber.load()
            except BackendUnavailableError as exc:
                last = exc
                log.warning("Whisper %s on cuda failed to load: %s", compute_type, exc)
                continue
            self._whisper[name] = transcriber
            self.compute_type = compute_type
            log.info("Whisper %s loaded on cuda (%s)", name, compute_type)
            return transcriber
        raise RuntimeError(f"could not load Whisper model {name!r} on the GPU: {last}")

    def transcribe(self, wav: Path, track: str, whisper: dict,
                   progress: Optional[Callable[[float], None]] = None) -> List[dict]:
        with self._batch_lock:
            self.last_used = time.monotonic()
            transcriber = self._transcriber(str(whisper.get("model") or "small.en"))
            transcriber.beam_size = int(whisper.get("beam_size") or 5)
            transcriber.hotwords = (whisper.get("hotwords") or "").strip() or None
            transcriber.language = whisper.get("language") or None
            try:
                segments = transcriber.transcribe(Path(wav), track, progress=progress)
            finally:
                self.last_used = time.monotonic()
        return [{"start": float(s.start), "end": float(s.end), "text": s.text} for s in segments]

    # -- live (separate instances, own lock) ---------------------------------

    def live_models(self) -> List[str]:
        """Whisper model folders in the pack: what this helper can serve live."""
        root = self.models_dir / "whisper"
        try:
            return sorted(p.name for p in root.iterdir() if p.is_dir())
        except OSError:
            return []

    @property
    def live_device_label(self) -> str:
        label = f"cuda:{self.gpu_name}" if self.gpu_name else "cuda"
        return f"{label} ({self.live_compute_type})" if self.live_compute_type else label

    def _live_model(self, name: str):
        cached = self._live_whisper.get(name)
        if cached is not None:
            return cached
        path = self.whisper_path(name)
        if not path.is_dir():
            raise RuntimeError(f"Whisper model {name!r} is not in the helper pack")
        prepare_dll_paths()
        from faster_whisper import WhisperModel

        last: Optional[Exception] = None
        for compute_type in self.compute_type_candidates():
            try:
                model = WhisperModel(str(path), device="cuda", compute_type=compute_type, local_files_only=True)
            except Exception as exc:  # noqa: BLE001 - try the next compute type; OOM must surface
                if "out of memory" in str(exc).lower():
                    raise
                last = exc
                log.warning("live Whisper %s on cuda failed to load: %s", compute_type, exc)
                continue
            self._live_whisper[name] = model
            self.live_compute_type = compute_type
            log.info("live Whisper %s loaded on cuda (%s)", name, compute_type)
            return model
        raise RuntimeError(f"could not load live Whisper model {name!r} on the GPU: {last}")

    def live_transcribe(self, name: str, audio, track: str = "") -> List[dict]:
        """Transcribe one cut utterance (float32 mono 16 kHz numpy array); times are relative to it."""
        with self._live_lock:
            model = self._live_model(name)
            segments, _info = model.transcribe(
                audio, language="en" if name.endswith(".en") else None, beam_size=1, best_of=1,
                temperature=0.0, vad_filter=False, word_timestamps=False, condition_on_previous_text=False)
            return [{"start": float(s.start), "end": float(s.end), "text": (s.text or "").strip()}
                    for s in segments]

    def live_unload(self) -> None:
        with self._live_lock:
            if not self._live_whisper:
                return
            self._live_whisper.clear()
            self.live_compute_type = None
        self._free_vram()

    # -- diarization ---------------------------------------------------------

    def load_diarizer(self):
        key = str(self.diarization_path())
        diarizer = self._diarizers.get(key)
        if diarizer is None:
            from ..transcribe.pyannote_backend import PyannoteDiarizer

            diarizer = PyannoteDiarizer(model=key, device="cuda")
            self._diarizers[key] = diarizer
        return diarizer

    def diarize(self, wav: Path, diarization: dict) -> List[dict]:
        self.last_used = time.monotonic()
        diarizer = self.load_diarizer()
        diarizer.min_speakers = diarization.get("min_speakers")
        diarizer.max_speakers = diarization.get("max_speakers")
        turns = diarizer.diarize(Path(wav))
        self.last_used = time.monotonic()
        return [{"start": float(t.start), "end": float(t.end), "speaker": t.speaker} for t in turns]

    # -- VRAM ----------------------------------------------------------------

    def unload_if_idle(self, max_idle_seconds: float = 600.0) -> bool:
        """Free the models (the GPU is shared, e.g. with Ollama) after ``max_idle_seconds`` unused."""
        if not (self._whisper or self._diarizers):
            return False
        if time.monotonic() - self.last_used < max_idle_seconds:
            return False
        with self._batch_lock:
            self._whisper.clear()
            self._diarizers.clear()
            self.compute_type = None
        self._free_vram()
        log.info("batch models unloaded after %.0f s idle", max_idle_seconds)
        return True

    @staticmethod
    def _free_vram() -> None:
        gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass
