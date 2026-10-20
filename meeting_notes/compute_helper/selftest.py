"""``self-test``: prove torch CUDA and CTranslate2 CUDA work by transcribing a short clip on the GPU."""

from __future__ import annotations

import json
import math
import struct
import tempfile
import time
import wave
from pathlib import Path
from typing import Optional

from . import gpu as gpu_mod
from .engine import Engine, prepare_dll_paths


def write_test_wav(path: Path, seconds: float = 3.0, rate: int = 16000) -> None:
    """A quiet 220 Hz tone, 16 kHz mono int16 (the format the server sends)."""
    frames = b"".join(
        struct.pack("<h", int(3000 * math.sin(2 * math.pi * 220 * i / rate))) for i in range(int(seconds * rate)))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(frames)


def run(models_dir: Path, whisper_model: Optional[str] = None) -> dict:
    result = {"ok": False, "torch_cuda": False, "ctranslate2_cuda": False, "gpu": None,
              "whisper_seconds": None, "diarization_loaded": False, "error": None}
    try:
        prepare_dll_paths()
        info = gpu_mod.query_gpu()
        result["gpu"] = info["name"] if info else None
        import torch

        result["torch_cuda"] = bool(torch.cuda.is_available())
        if not result["torch_cuda"]:
            raise RuntimeError("torch cannot see a CUDA GPU")
        import ctranslate2

        result["ctranslate2_cuda"] = ctranslate2.get_cuda_device_count() > 0
        if not result["ctranslate2_cuda"]:
            raise RuntimeError("CTranslate2 cannot see a CUDA GPU")
        engine = Engine(models_dir, gpu_name=result["gpu"])
        name = whisper_model
        if not name:
            found = sorted(p.name for p in (Path(models_dir) / "whisper").glob("*") if p.is_dir())
            if not found:
                raise RuntimeError(f"no Whisper model in {Path(models_dir) / 'whisper'}")
            name = found[0]
        with tempfile.TemporaryDirectory(prefix="mn-selftest-") as raw:
            wav = Path(raw) / "clip.wav"
            write_test_wav(wav)
            started = time.monotonic()
            engine.transcribe(wav, "selftest", {"model": name, "beam_size": 1})
            result["whisper_seconds"] = round(time.monotonic() - started, 2)
        if engine.can_diarize():
            engine.load_diarizer()
            result["diarization_loaded"] = True
        result["ok"] = True
    except Exception as exc:  # noqa: BLE001 - the result is the report
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def main(models_dir: Path, whisper_model: Optional[str] = None) -> int:
    result = run(models_dir, whisper_model)
    print(json.dumps(result))
    return 0 if result["ok"] else 1
