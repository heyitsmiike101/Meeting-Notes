"""Opus track upload: client choice (Opus vs PCM fallback), server decode, readers."""

from __future__ import annotations

import os
import shutil
import wave
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from meeting_notes import wire
from meeting_notes.client import compress, recordings
from meeting_notes.client.queue import SessionQueue, UploadWorker
from meeting_notes.server.app import create_app
from tests.test_client_transport import _make_session_dir
from tests.test_client_upload_wait import DONE, FakeServer

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated:DeprecationWarning"
)
needs_opus = pytest.mark.skipif(not compress.available(), reason="PyAV with libopus is required")


def _find_ffmpeg():
    found = shutil.which("ffmpeg")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA")
    if local:
        for p in Path(local, "Microsoft", "WinGet", "Packages").glob("**/ffmpeg.exe"):
            os.environ["PATH"] = str(p.parent) + os.pathsep + os.environ.get("PATH", "")
            return str(p)
    return None


FFMPEG = _find_ffmpeg()
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is required")


class OpusServer(FakeServer):
    def __init__(self, formats):
        super().__init__([DONE])
        self.formats = formats
        self.opus_uploads = []
        self.pcm_uploads = []

    def health(self):
        out = {"status": "ok"}
        if self.formats is not None:
            out["track_formats"] = self.formats
        return out

    def upload_track(self, session_id, track, pcm_path, frames, progress_callback=None):
        self.pcm_uploads.append((track, frames, Path(pcm_path).stat().st_size))
        return {"frames": frames}

    def upload_track_file(self, session_id, track, path, *, content_type, track_format, progress_callback=None):
        assert (content_type, track_format) == ("audio/ogg", "opus")
        self.opus_uploads.append((track, Path(path).suffix, Path(path).stat().st_size))
        return {}


def _worker(tmp_path, server, compress_on=True):
    session_dir = _make_session_dir(tmp_path, "mtg")
    queue = SessionQueue(tmp_path / ".upload-queue")
    queue.enqueue(session_dir)
    worker = UploadWorker(
        queue, "http://unused", client_factory=server, poll_interval=0.001,
        compress_recordings=(lambda: True) if compress_on else None,
    )
    return session_dir, worker


@needs_opus
def test_opus_sent_when_server_advertises_it(tmp_path):
    server = OpusServer(["pcm16", "opus"])
    session_dir, worker = _worker(tmp_path, server)
    worker.run_once()
    assert sorted(t for t, *_ in server.opus_uploads) == ["mic", "system"]
    assert server.pcm_uploads == []
    assert not list(session_dir.glob("*.wav"))
    assert len(list(session_dir.glob("*.opus"))) == 2  # the local copy is kept
    assert not list(session_dir.glob("*.pcm16"))


@needs_opus
def test_old_server_gets_pcm_decoded_from_the_opus_copy(tmp_path):
    server = OpusServer(None)
    session_dir, worker = _worker(tmp_path, server)
    worker.run_once()
    assert server.opus_uploads == []
    assert sorted(t for t, *_ in server.pcm_uploads) == ["mic", "system"]
    for _track, frames, size in server.pcm_uploads:
        assert size == frames * 2
        assert abs(frames - 0.2 * wire.STREAM_SAMPLE_RATE) < 800
    assert not list(session_dir.glob("*.pcm16"))


def test_wav_path_unchanged_without_compression(tmp_path):
    server = OpusServer(["pcm16", "opus"])
    session_dir, worker = _worker(tmp_path, server, compress_on=False)
    worker.run_once()
    assert server.opus_uploads == []
    assert len(server.pcm_uploads) == 2
    assert len(list(session_dir.glob("*.wav"))) == 2


@needs_opus
def test_a_415_falls_back_to_pcm(tmp_path):
    import httpx

    class Refusing(OpusServer):
        def upload_track_file(self, *a, **k):
            req = httpx.Request("POST", "http://x")
            raise httpx.HTTPStatusError("no", request=req, response=httpx.Response(415, request=req))

    server = Refusing(["pcm16", "opus"])
    _session_dir, worker = _worker(tmp_path, server)
    worker.run_once()
    assert len(server.pcm_uploads) == 2


# -- readers -------------------------------------------------------------------------


@needs_opus
def test_recordings_accept_opus_tracks(tmp_path):
    session_dir = _make_session_dir(tmp_path, "mtg")
    assert recordings.validate_recording(session_dir) is None
    compress.compress_session(session_dir)
    assert not list(session_dir.glob("*.wav"))
    assert recordings.validate_recording(session_dir) is None
    assert recordings.looks_like_session(session_dir)
    assert abs(recordings._duration(session_dir, None) - 0.2) < 0.1
    (session_dir / "mic.opus").write_bytes(b"not opus")
    assert recordings.validate_recording(session_dir) is not None


# -- server --------------------------------------------------------------------------


def _opus_bytes(tmp_path, seconds=2.0):
    wav = tmp_path / "t.wav"
    rate = 48000
    t = np.arange(int(rate * seconds)) / rate
    pcm = (0.3 * np.sin(2 * np.pi * 300 * t) * 32767).astype("<i2")
    with wave.open(str(wav), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(rate)
        fh.writeframes(pcm.tobytes())
    return compress.compress_track(wav).read_bytes()


def _app(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    return TestClient(create_app(transcriber_factory=None, data_root=str(tmp_path / "data")))


def test_health_lists_track_formats(tmp_path, monkeypatch):
    client = _app(tmp_path, monkeypatch)
    assert client.get(wire.HEALTH).json()["track_formats"] == ["pcm16", "opus"]


@needs_opus
@needs_ffmpeg
def test_server_decodes_an_opus_track(tmp_path, monkeypatch):
    client = _app(tmp_path, monkeypatch)
    body = _opus_bytes(tmp_path, 2.0)
    resp = client.post(
        wire.track_upload_path("opus1", "mic"), content=body,
        headers={"Content-Type": "audio/ogg", "X-Track-Format": "opus"},
    )
    assert resp.status_code == 200, resp.text
    frames = resp.json()["frames"]
    assert abs(frames - 2.0 * wire.STREAM_SAMPLE_RATE) < 1600
    raw = next((tmp_path / "data").rglob("mic.raw"))
    assert raw.stat().st_size == frames * 2
    assert not list(raw.parent.glob(".*.part"))
    # the PCM route is untouched
    pcm = b"\x01\x00" * 1000
    resp = client.post(
        wire.track_upload_path("pcm1", "mic"), content=pcm,
        headers={"Content-Type": "application/octet-stream", "X-Frames": "1000"},
    )
    assert resp.status_code == 200 and resp.json()["frames"] == 1000


@needs_ffmpeg
def test_garbage_opus_is_a_client_error(tmp_path, monkeypatch):
    client = _app(tmp_path, monkeypatch)
    for body in (b"definitely not ogg" * 100, b""):
        resp = client.post(
            wire.track_upload_path("bad1", "mic"), content=body,
            headers={"Content-Type": "audio/ogg", "X-Track-Format": "opus"},
        )
        assert resp.status_code in (415, 422), resp.text
    assert not list((tmp_path / "data").rglob("mic.raw"))
    assert not list((tmp_path / "data").rglob(".*.part"))


def test_unknown_track_format_is_415(tmp_path, monkeypatch):
    client = _app(tmp_path, monkeypatch)
    resp = client.post(wire.track_upload_path("x1", "mic"), content=b"x", headers={"X-Track-Format": "mp3"})
    assert resp.status_code == 415
