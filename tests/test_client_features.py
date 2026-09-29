"""Focused tests for imported recordings and per-source mute controls."""

from __future__ import annotations

import json
import httpx
import numpy as np
import pytest

from meeting_notes.client.api import ServerClient, recording_content_type
from meeting_notes.client.queue import SessionQueue, UploadWorker
from tests.test_client_transport import _make_session_dir
from tests.test_track_recorder import make_recorder
from tests.fakes import FakeSource


def test_upload_recording_uses_multipart_and_keeps_audio_out_of_local_client(tmp_path):
    recording = tmp_path / "call.wav"
    recording.write_bytes(b"RIFF" + b"audio bytes")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = request.read()
        return httpx.Response(
            200,
            json={"session_id": "s1", "job_id": "j1", "state": "queued"},
            request=request,
        )

    client = ServerClient("http://server.invalid")
    client._client.close()
    client._client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://server.invalid"
    )
    try:
        result = client.upload_recording(recording, name="Imported call")
    finally:
        client.close()

    assert result["job_id"] == "j1"
    assert seen["path"] == "/v1/uploads"
    assert b'name="file"' in seen["body"]
    assert b"Imported call" in seen["body"]
    assert b"RIFFaudio bytes" in seen["body"]


def test_report_upload_status_uses_session_status_contract():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["method"] = request.method
        seen["json"] = request.read()
        return httpx.Response(200, json={"ok": True}, request=request)

    client = ServerClient("http://server.invalid")
    client._client.close()
    client._client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://server.invalid"
    )
    try:
        result = client.report_upload_status(
            "session-one",
            {"state": "uploading", "percent": 42.0, "bytes_received": 10, "bytes_total": 20},
        )
    finally:
        client.close()

    assert result == {"ok": True}
    assert seen["method"] == "PUT"
    assert seen["path"] == "/v1/sessions/session-one/pipeline"
    assert json.loads(seen["json"]) == {
        "state": "uploading",
        "percent": 42.0,
        "bytes_received": 10,
        "bytes_total": 20,
    }


def test_imported_recording_rejects_unknown_format_before_network(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("not audio", encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported audio format"):
        recording_content_type(path)


def test_recorder_upload_persists_byte_and_transcription_progress(tmp_path):
    session_dir = _make_session_dir(tmp_path, "progress-session")
    queue = SessionQueue(tmp_path / ".upload-queue")
    entry_id = queue.enqueue(session_dir)
    seen = []
    reports = []

    class FakeClient:
        def upload_track(self, session_id, track, pcm_path, frames, *, progress_callback=None):
            payload_size = pcm_path.stat().st_size
            if progress_callback:
                progress_callback(payload_size, payload_size)
            return {"track": track}

        def finalize(self, session_id, meta, timing):
            return "job-progress"

        def job(self, job_id):
            return {
                "state": "done",
                "progress": 1.0,
                "transcription_percent": 100.0,
            }

        def transcript(self, job_id):
            return {"markdown": "# done", "json": '{"segments": []}'}

        def report_upload_status(self, session_id, payload):
            reports.append((session_id, dict(payload)))
            return {"ok": True}

        def close(self):
            pass

    worker = UploadWorker(
        queue,
        "http://unused",
        client_factory=FakeClient,
        on_progress=lambda state: seen.append(state),
    )
    worker._upload_session(entry_id, session_dir, queue.pending()[0])

    assert seen
    assert any(state.get("upload_state") == "uploading" for state in seen)
    assert any(state.get("upload_percent") == 100.0 for state in seen)
    final = queue.read_state(entry_id)
    assert final["upload_state"] == "complete"
    assert final["transcription_state"] == "complete"
    assert final["transcription_percent"] == 100.0
    assert reports and reports[0][1]["state"] == "pending"
    assert all(
        set(payload) <= {"name", "device", "state", "percent", "bytes_received", "bytes_total"}
        for _, payload in reports
    )


def test_muting_one_track_writes_aligned_silence_and_leaves_recorder_running(tmp_path):
    mic, stop, _ = make_recorder(tmp_path, FakeSource(samplerate=1000, channels=1), track="mic")
    (tmp_path / "other").mkdir()
    other, other_stop, _ = make_recorder(
        tmp_path / "other", FakeSource(samplerate=1000, channels=1), track="system"
    )
    mic.start()
    other.start()
    try:
        import time

        time.sleep(0.15)
        before = mic.frames
        mic.set_muted(True)
        time.sleep(0.20)
        assert mic.muted is True
        assert mic.alive and other.alive
        mic.writer.flush()
        muted = np.fromfile(tmp_path / "mic.raw", dtype="<i2")
        assert mic.frames > before
        assert np.all(muted[-100:] == 0)
        mic.set_muted(False)
        time.sleep(0.15)
        assert mic.muted is False
        mic.writer.flush()
        unmuted = np.fromfile(tmp_path / "mic.raw", dtype="<i2")
        assert np.any(unmuted[-100:] != 0)
    finally:
        stop.set()
        other_stop.set()
        mic.close()
        other.close()
