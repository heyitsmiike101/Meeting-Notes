"""Shared plumbing for the client-compatibility contract tests.

Each ``clients/vX_Y_Z/`` folder is a frozen copy of one released recorder's
network layer (see README.md). ``load_client`` imports one of them under a
private package name so its relative imports resolve, and the ``LiveServer``
runs the CURRENT server on a real socket so the historical code talks to it
over genuine HTTP and websockets, exactly as an installed recorder would.
"""

from __future__ import annotations

import importlib.util
import json
import socket
import sys
import threading
import time
import wave
from pathlib import Path
from types import SimpleNamespace
from typing import List

import numpy as np
import uvicorn

CLIENTS_DIR = Path(__file__).parent / "clients"
_SUBMODULES = ("wire", "api", "resample", "streamer", "queue", "update", "logs_send", "remote", "control_channel")


def fixture_dirs() -> List[Path]:
    """Every frozen client folder, oldest version first."""
    return sorted(
        (p for p in CLIENTS_DIR.iterdir() if p.is_dir() and p.name.startswith("v") and (p / "__init__.py").exists()),
        key=lambda p: tuple(int(x) for x in p.name[1:].split("_")),
    )


def folder_version(path: Path) -> str:
    return path.name[1:].replace("_", ".")


def load_client(path: Path) -> SimpleNamespace:
    """Import a frozen client package; returns its modules as attributes."""
    name = f"compat_client_{path.name}"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            name, path / "__init__.py", submodule_search_locations=[str(path)]
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    package = sys.modules[name]
    ns = SimpleNamespace(version=package.__version__, commit=getattr(package, "COMMIT", ""), package=package)
    for sub in _SUBMODULES:
        if (path / f"{sub}.py").exists():
            setattr(ns, sub, importlib.import_module(f"{name}.{sub}"))
    return ns


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LiveServer:
    def __init__(self, app):
        self.port = free_port()
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> str:
        self.thread.start()
        deadline = time.monotonic() + 15
        while not self.server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        if not self.server.started:
            raise RuntimeError("server did not start in time")
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


def make_session_dir(parent: Path, name: str = "session") -> Path:
    """A two-track recording folder shaped like the recorder produces
    (48 kHz mono WAVs, timing sidecars, session.json)."""
    session_dir = parent / name
    session_dir.mkdir(parents=True)
    rate = 48000
    tracks = {}
    for track, freq in (("mic", 440.0), ("system", 220.0)):
        wav_path = session_dir / f"{track}.wav"
        t = np.arange(int(rate * 0.2)) / rate
        samples = (0.3 * np.sin(2 * np.pi * freq * t) * 32767).astype("<i2")
        with wave.open(str(wav_path), "wb") as fh:
            fh.setnchannels(1)
            fh.setsampwidth(2)
            fh.setframerate(rate)
            fh.writeframes(samples.tobytes())
        duration = len(samples) / rate
        (session_dir / f"{track}.timing.jsonl").write_text(
            json.dumps({"event": "open", "segment": 0, "frames": 0, "t": 0.0, "wall": 0.0,
                        "samplerate": rate, "channels": 1, "device": "test"}) + "\n"
            + json.dumps({"event": "close", "segment": 0, "frames": len(samples), "t": duration}) + "\n",
            encoding="utf-8",
        )
        tracks[track] = {"wav": wav_path.name, "samplerate": rate, "channels": 1,
                         "label": "You" if track == "mic" else "Them",
                         "frames": len(samples), "duration_sec": round(duration, 3)}
    meta = {"version": 1, "created": "2026-01-01T00:00:00", "duration_sec": 0.2, "tracks": tracks}
    (session_dir / "session.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return session_dir


def pcm_tone(seconds: float, freq: float = 300.0) -> bytes:
    """16 kHz mono int16 little-endian PCM, as the wire format defines."""
    n = int(16000 * seconds)
    return (np.sin(2 * np.pi * freq * np.arange(n) / 16000) * 8000).astype("<i2").tobytes()


def write_wav_16k(path: Path, seconds: float = 0.3) -> Path:
    """A canonical 16 kHz mono int16 WAV (the server accepts it without ffmpeg)."""
    with wave.open(str(path), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(16000)
        fh.writeframes(pcm_tone(seconds))
    return path
