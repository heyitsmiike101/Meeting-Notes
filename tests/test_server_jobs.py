"""Idempotent finalize and resuming jobs interrupted by a restart."""

from __future__ import annotations

import shutil
import time

import pytest
from fastapi.testclient import TestClient

from meeting_notes import wire
from meeting_notes.server.jobs import JobQueue
from meeting_notes.server.store import Store
from meeting_notes.transcribe.protocol import Segment
from tests.test_server import (
    GatedTranscriber,
    StubTranscriber,
    _upload_and_finalize,
    make_app,
    silence_pcm,
    wait_for_job_state,
)

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated:DeprecationWarning"
)

FINALIZE_BODY = {"meta": {"created": "2026-09-20", "tracks": {"mic": {}}}, "timing": {}, "settings": {}}


def _client(tmp_path, monkeypatch, factory):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    return TestClient(make_app(tmp_path, transcriber_factory=factory))


def _job_count(tmp_path):
    jobs = (tmp_path / "data" / "jobs").glob("*.json")
    return len([p for p in jobs if not p.name.endswith(".transcript.json")])


# -- Bug 1: finalize is idempotent -------------------------------------------------


def test_repeated_finalize_returns_the_same_active_job(tmp_path, monkeypatch):
    gate = GatedTranscriber({"mic": [Segment(start=0.0, end=1.0, text="hi", track="mic")]})
    client = _client(tmp_path, monkeypatch, lambda **_kw: gate)
    job_id = _upload_and_finalize(client, "s1", {"mic": silence_pcm(1.0)})
    assert gate.entered.wait(5)  # running now
    for _ in range(3):
        resp = client.post(wire.finalize_path("s1"), json=FINALIZE_BODY)
        assert resp.status_code == 200
        assert resp.json() == {"job_id": job_id}
    gate.release.set()
    wait_for_job_state(client, job_id, wire.JobState.DONE)
    # Audio unchanged and the job finished: still the same job.
    assert client.post(wire.finalize_path("s1"), json=FINALIZE_BODY).json() == {"job_id": job_id}
    assert _job_count(tmp_path) == 1


def test_finalize_after_new_track_upload_enqueues_a_new_job(tmp_path, monkeypatch):
    gate = GatedTranscriber({})
    client = _client(tmp_path, monkeypatch, lambda **_kw: gate)
    first = _upload_and_finalize(client, "s2", {"mic": silence_pcm(1.0)})
    assert gate.entered.wait(5)
    time.sleep(0.05)
    second = _upload_and_finalize(client, "s2", {"system": silence_pcm(1.0)})
    assert second != first
    gate.release.set()
    wait_for_job_state(client, second, wire.JobState.DONE)
    assert _job_count(tmp_path) == 2


def test_finalize_after_errored_job_enqueues_a_new_job(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, None)
    first = _upload_and_finalize(client, "s3", {"mic": silence_pcm(0.5)})
    wait_for_job_state(client, first, wire.JobState.ERROR)
    second = client.post(wire.finalize_path("s3"), json=FINALIZE_BODY).json()["job_id"]
    assert second != first


def test_retranscribe_still_forces_a_new_job(tmp_path, monkeypatch):
    gate = GatedTranscriber({})
    client = _client(tmp_path, monkeypatch, lambda **_kw: gate)
    first = _upload_and_finalize(client, "s4", {"mic": silence_pcm(1.0)})
    assert gate.entered.wait(5)
    resp = client.post("/v1/sessions/s4/retranscribe")
    assert resp.status_code == 200
    assert resp.json()["job_id"] != first
    gate.release.set()
    assert _job_count(tmp_path) == 2


# -- Bug 2: resume jobs interrupted by a restart ------------------------------------


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "data"), str(tmp_path / "media"))


def _session(store, sid):
    store.write_session_meta(sid, {"name": sid, "tracks": {"mic": {}}})
    wav = store.track_wav_path(sid, "mic")
    wav.parent.mkdir(parents=True, exist_ok=True)
    wav.write_bytes(b"RIFF" + b"\x00" * 100)


def _job(store, sid, state, created, progress=0.0):
    job_id = store.create_job(sid)
    store.update_job(job_id, state=state, created=created, progress=progress)
    return job_id


def _queued_ids(q):
    return list(q._queue.queue)


def test_resume_requeues_stale_jobs_oldest_first(store):
    _session(store, "a")
    _session(store, "b")
    jb = _job(store, "b", wire.JobState.QUEUED, 200.0)
    ja = _job(store, "a", wire.JobState.RUNNING, 100.0, progress=0.5)
    done = _job(store, "a", wire.JobState.DONE, 50.0, progress=1.0)
    q = JobQueue(store, None)
    assert q.resume_interrupted() == [ja, jb]
    assert _queued_ids(q) == [ja, jb]
    running = store.read_job(ja)
    assert running["state"] == wire.JobState.QUEUED
    assert running["progress"] == 0.0
    assert running["resumed_after_restart"] is True
    assert store.read_job(done)["state"] == wire.JobState.DONE


def test_resume_only_newest_stale_job_per_session(store):
    _session(store, "a")
    old1 = _job(store, "a", wire.JobState.QUEUED, 10.0)
    old2 = _job(store, "a", wire.JobState.RUNNING, 20.0, progress=0.3)
    new = _job(store, "a", wire.JobState.QUEUED, 30.0)
    q = JobQueue(store, None)
    assert q.resume_interrupted() == [new]
    for old in (old1, old2):
        job = store.read_job(old)
        assert job["state"] == wire.JobState.ERROR and job["error"] == "superseded"
    # The session's latest job is the resumed one, not a superseded one.
    assert store.session_index_row("a")["latest_job_id"] == new
    assert store.session_detail("a")["pipeline"]["transcription"]["job_id"] == new


def test_stale_job_older_than_a_finished_one_is_superseded_not_resumed(store):
    _session(store, "a")
    stale = _job(store, "a", wire.JobState.RUNNING, 10.0)
    done = _job(store, "a", wire.JobState.DONE, 20.0, progress=1.0)
    q = JobQueue(store, None)
    assert q.resume_interrupted() == []
    assert store.read_job(stale)["error"] == "superseded"
    assert store.session_index_row("a")["latest_job_id"] == done


def test_latest_job_skips_a_superseded_job_even_if_newest(store):
    _session(store, "a")
    done = _job(store, "a", wire.JobState.DONE, 10.0, progress=1.0)
    sup = _job(store, "a", wire.JobState.ERROR, 20.0, progress=1.0)
    store.update_job(sup, error="superseded", superseded=True)
    assert store.session_index_row("a")["latest_job_id"] == done
    pipeline = store.session_detail("a")["pipeline"]
    assert pipeline["transcription"]["job_id"] == done
    assert pipeline["transcription"]["state"] == "complete"


def test_resume_marks_missing_and_trashed_sessions_meeting_deleted(store):
    _session(store, "gone")
    _session(store, "trashed")
    j_missing = _job(store, "gone", wire.JobState.QUEUED, 1.0)
    shutil.rmtree(store.session_dir("gone"))  # session dir vanished behind the job
    j_orphan = store.create_job("never-existed")
    store.update_job(j_orphan, state=wire.JobState.RUNNING)
    j_trashed = _job(store, "trashed", wire.JobState.QUEUED, 2.0)
    store.trash_session("trashed")
    q = JobQueue(store, None)
    assert q.resume_interrupted() == []
    assert _queued_ids(q) == []
    for jid in (j_missing, j_orphan):
        job = store.read_job(jid)
        assert job["state"] == wire.JobState.ERROR and job["error"] == "meeting deleted"
    assert store.read_job(j_trashed) is None  # lives in the trash, untouched


def test_resumed_job_actually_runs_after_start(store):
    _session(store, "a")
    seg = [Segment(start=0.0, end=1.0, text="hello", track="mic")]
    job = _job(store, "a", wire.JobState.RUNNING, 5.0, progress=0.4)
    q = JobQueue(store, lambda **_kw: StubTranscriber({"mic": seg}))
    q.resume_interrupted()
    q.start()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and store.read_job(job)["state"] != wire.JobState.DONE:
            time.sleep(0.02)
        assert store.read_job(job)["state"] == wire.JobState.DONE
    finally:
        q.stop()
