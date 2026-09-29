"""Focused coverage for complete-recording uploads and split server storage."""

from __future__ import annotations

import io
import json
import time
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from meeting_notes import wire
from meeting_notes.server import app as app_module
from meeting_notes.server.app import create_app
from meeting_notes.server.store import Store


class EmptyTranscriber:
    def transcribe(self, wav_path: Path, track: str):
        assert wav_path.exists()
        return []


def _wav_bytes(*, seconds: float = 0.05, sample_rate: int = wire.STREAM_SAMPLE_RATE, channels: int = 1) -> bytes:
    frames = int(seconds * sample_rate)
    stream = io.BytesIO()
    with wave.open(stream, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(b"\0\0" * frames * channels)
    return stream.getvalue()


def _wait_job(client: TestClient, job_id: str, expected: str = wire.JobState.DONE) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        body = client.get(wire.job_path(job_id)).json()
        if body["state"] in (expected, wire.JobState.ERROR):
            return body
        time.sleep(0.02)
    raise AssertionError(f"job did not reach {expected}: {body}")


def _upload(client: TestClient, payload: bytes, filename: str = "meeting.wav", **data):
    return client.post(
        wire.upload_path(),
        files={"file": (filename, payload, "audio/wav")},
        data=data,
    )


def test_complete_upload_is_streamed_to_media_and_exposes_pipeline(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = create_app(
        transcriber_factory=lambda **_: EmptyTranscriber(),
        data_root=str(tmp_path / "app-data"),
        media_root=str(tmp_path / "media"),
    )
    with TestClient(app) as client:
        response = _upload(client, _wav_bytes(), name="Imported meeting", device="Phone")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["session_id"] and body["job_id"]
        assert body["upload_percent"] == 100.0
        assert body["pipeline"]["upload"]["state"] == "complete"
        assert body["pipeline"]["transcription"]["state"] in ("pending", "transcribing", "complete")

        done = _wait_job(client, body["job_id"])
        assert done["state"] == wire.JobState.DONE
        assert done["upload_percent"] == 100.0
        assert done["transcription_percent"] == 100.0
        detail = client.get(f"/v1/sessions/{body['session_id']}").json()
        assert detail["meta"]["name"] == "Imported meeting"
        assert detail["meta"]["device"] == "Phone"
        assert detail["pipeline"]["state"] == "complete"
        assert (tmp_path / "media" / "sessions" / body["session_id"] / "system.wav").exists()
        assert (tmp_path / "app-data" / "sessions" / body["session_id"] / "session.json").exists()
        assert (tmp_path / "app-data" / "jobs").is_dir()
        assert (tmp_path / "app-data" / "index.sqlite").exists()


def test_upload_rejects_invalid_format_and_empty_recording(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = create_app(transcriber_factory=lambda **_: EmptyTranscriber(), data_root=str(tmp_path / "data"))
    with TestClient(app) as client:
        invalid = _upload(client, b"not audio", filename="notes.txt")
        assert invalid.status_code == 415
        empty = _upload(client, b"", filename="empty.wav")
        assert empty.status_code == 422
        sessions = client.get("/v1/sessions").json()["items"]
        assert sessions and sessions[0]["pipeline"]["upload"]["state"] == "error"


def test_upload_limit_is_enforced_during_multipart_parsing(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    monkeypatch.setattr(app_module, "_MAX_RECORDING_BYTES", 32)
    app = create_app(transcriber_factory=lambda **_: EmptyTranscriber(), data_root=str(tmp_path / "data"))
    with TestClient(app) as client:
        response = _upload(client, _wav_bytes())
        assert response.status_code == 413
        assert "exceeds" in response.json()["detail"]
        # Rejection happens before Starlette can spool the complete file or
        # create a session that would linger as a failed phantom upload.
        sessions = client.get("/v1/sessions").json()["items"]
        assert sessions == []


def test_noncanonical_wav_does_not_get_renamed_without_ffmpeg(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    monkeypatch.setattr(app_module.shutil, "which", lambda _: None)
    app = create_app(transcriber_factory=lambda **_: EmptyTranscriber(), data_root=str(tmp_path / "data"))
    with TestClient(app) as client:
        # Valid WAV container, but 8 kHz stereo: it requires normalization.
        response = _upload(client, _wav_bytes(sample_rate=8000, channels=2))
        assert response.status_code == 422
        sessions = client.get("/v1/sessions").json()["items"]
        assert sessions[0]["pipeline"]["upload"]["state"] == "error"


def test_pipeline_states_and_split_storage(tmp_path):
    app_root = tmp_path / "app"
    media_root = tmp_path / "media"
    store = Store(str(app_root), str(media_root))
    store.write_session_meta("s1", {"name": "one", "upload": {"state": "pending", "percent": 20}})
    assert store.session_detail("s1")["pipeline"]["state"] == "pending"
    job_id = store.create_job("s1")
    store.update_job(job_id, state=wire.JobState.RUNNING, progress=0.4)
    assert store.session_detail("s1")["pipeline"]["transcription"]["state"] == "transcribing"
    store.update_job(job_id, state=wire.JobState.DONE, progress=1.0)
    assert store.session_detail("s1")["pipeline"]["transcription"]["state"] == "complete"
    assert (app_root / "jobs" / f"{job_id}.json").exists()
    assert (app_root / "sessions").exists()
    assert (app_root / "sessions" / "s1" / "session.json").exists()


def test_pipeline_route_persists_progress_without_creating_a_job(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = create_app(transcriber_factory=lambda **_: EmptyTranscriber(), data_root=str(tmp_path / "data"))
    with TestClient(app) as client:
        response = client.put(
            wire.pipeline_path("client-session"),
            json={"name": "From recorder", "device": "Laptop", "state": "pending", "percent": 0, "bytes_received": 0, "bytes_total": 100},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["pipeline"]["upload"]["state"] == "pending"
        assert client.get("/v1/sessions").json()["items"][0]["session_id"] == "client-session"
        assert not app.state.store.list_jobs()

        uploading = client.put(
            wire.pipeline_path("client-session"),
            json={"state": "uploading", "percent": 50, "bytes_received": 50, "bytes_total": 100},
        )
        assert uploading.status_code == 200
        assert uploading.json()["pipeline"]["upload"]["percent"] == 50.0
        assert client.put(wire.pipeline_path("client-session"), json={"state": "complete", "percent": 99}).status_code == 400
        assert client.put(wire.pipeline_path("client-session"), json={"state": "uploading", "percent": 101}).status_code == 400
        assert client.put(wire.pipeline_path("client-session"), json={"state": "uploading", "percent": 10, "bytes_received": 20, "bytes_total": 10}).status_code == 400


def test_finalize_preserves_pipeline_identity_and_completes_upload_state(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = create_app(transcriber_factory=lambda **_: EmptyTranscriber(), data_root=str(tmp_path / "data"))
    with TestClient(app) as client:
        session_id = "recorder-finalize"
        pending = client.put(
            wire.pipeline_path(session_id),
            json={"name": "Recorder name", "device": "Laptop", "state": "pending", "percent": 0, "bytes_received": 0, "bytes_total": 10},
        )
        assert pending.status_code == 200
        assert client.put(
            wire.pipeline_path(session_id),
            json={"state": "uploading", "percent": 80, "bytes_received": 8, "bytes_total": 10},
        ).status_code == 200
        assert client.post(
            wire.track_upload_path(session_id, "mic"),
            content=b"\0\0" * 10,
            headers={"Content-Type": "application/octet-stream"},
        ).status_code == 200
        finalized = client.post(
            wire.finalize_path(session_id),
            json={"meta": {"tracks": {"mic": {"sample_rate": wire.STREAM_SAMPLE_RATE}}}, "timing": {}},
        )
        assert finalized.status_code == 200, finalized.text
        detail = client.get(f"/v1/sessions/{session_id}").json()
        assert detail["meta"]["name"] == "Recorder name"
        assert detail["meta"]["device"] == "Laptop"
        assert detail["pipeline"]["upload"]["state"] == "complete"
        assert detail["pipeline"]["upload"]["percent"] == 100.0


def test_legacy_sessions_are_migrated_to_explicit_media_root(tmp_path):
    app_root = tmp_path / "app"
    old_session = app_root / "sessions" / "legacy"
    old_session.mkdir(parents=True)
    (old_session / "session.json").write_text(json.dumps({"name": "legacy"}), encoding="utf-8")
    (old_session / "system.wav").write_bytes(b"old audio")
    media_root = tmp_path / "media"
    store = Store(str(app_root), str(media_root))
    assert (app_root / "sessions" / "legacy" / "session.json").exists()
    assert (media_root / "sessions" / "legacy" / "system.wav").exists()
    assert store.session_exists("legacy")


def test_unavailable_explicit_media_root_fails_without_moving_legacy_audio(tmp_path):
    app_root = tmp_path / "app"
    old_session = app_root / "sessions" / "legacy"
    old_session.mkdir(parents=True)
    audio = old_session / "system.wav"
    audio.write_bytes(b"durable audio")
    unavailable_media = tmp_path / "not-a-directory"
    unavailable_media.write_text("occupied", encoding="utf-8")

    with pytest.raises(OSError):
        Store(str(app_root), str(unavailable_media))

    assert audio.read_bytes() == b"durable audio"


def test_legacy_migration_replaces_partial_destination_atomically(tmp_path):
    app_root = tmp_path / "app"
    old_session = app_root / "sessions" / "legacy"
    old_session.mkdir(parents=True)
    source = old_session / "system.wav"
    source.write_bytes(b"complete durable audio")
    media_session = tmp_path / "media" / "sessions" / "legacy"
    media_session.mkdir(parents=True)
    destination = media_session / "system.wav"
    destination.write_bytes(b"partial")

    Store(str(app_root), str(tmp_path / "media"))

    assert destination.read_bytes() == b"complete durable audio"
    assert not source.exists()
    assert not list(media_session.glob(".migrate-*.tmp"))


def test_default_store_keeps_legacy_single_root_layout(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_MEDIA", raising=False)
    monkeypatch.delenv("MEETING_NOTES_AUDIO", raising=False)
    monkeypatch.delenv("MEETING_NOTES_RECORDINGS", raising=False)
    store = Store(str(tmp_path / "data"))
    assert store.media_root == store.root
    store.write_session_meta("same", {})
    assert (tmp_path / "data" / "sessions" / "same" / "session.json").exists()


def test_equivalent_relative_and_absolute_roots_do_not_migrate_over_themselves(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    session = tmp_path / "shared" / "sessions" / "same"
    session.mkdir(parents=True)
    audio = session / "system.wav"
    audio.write_bytes(b"keep me")

    store = Store("shared", str(tmp_path / "shared"))

    assert store.root == store.media_root
    assert audio.read_bytes() == b"keep me"


def test_delete_audio_preserves_metadata_and_transcript_but_delete_session_cleans_both(tmp_path):
    store = Store(str(tmp_path / "app"), str(tmp_path / "media"))
    store.write_session_meta("keep", {"name": "Keep"})
    store.ensure_media_session_dir("keep").joinpath("system.wav").write_bytes(b"audio")
    job_id = store.create_job("keep")
    store.write_transcript(job_id, "# transcript", '{"segments": []}')
    store.update_job(job_id, state=wire.JobState.DONE, progress=1.0)
    store.delete_session_audio("keep")
    assert (tmp_path / "app" / "sessions" / "keep" / "session.json").exists()
    assert store.read_transcript(job_id) is not None
    assert not (tmp_path / "media" / "sessions" / "keep" / "system.wav").exists()
    store.delete_session("keep")
    assert not (tmp_path / "app" / "sessions" / "keep").exists()
    assert not (tmp_path / "media" / "sessions" / "keep").exists()
    assert store.read_transcript(job_id) is None
