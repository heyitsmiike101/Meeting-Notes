"""Upload worker: finalize happens once; waiting for transcription is not a failure."""

from __future__ import annotations

import httpx
import pytest

from meeting_notes import wire
from meeting_notes.client.api import ServerUnavailable
from meeting_notes.client.queue import SessionQueue, UploadWorker
from tests.test_client_transport import _make_session_dir


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "http://x/v1/jobs/j")
    return httpx.HTTPStatusError("boom", request=request, response=httpx.Response(code, request=request))


class FakeServer:
    """Callable client factory; ``job_script`` yields job() results or raises."""

    def __init__(self, job_script):
        self.job_script = list(job_script)
        self.finalized = []
        self.uploaded = []
        self.job_calls = 0
        self.job_ids = 0

    def __call__(self):
        return self

    def upload_track(self, session_id, track, pcm_path, frames, progress_callback=None):
        self.uploaded.append((session_id, track))
        return {"frames": frames}

    def report_upload_status(self, session_id, payload):
        return {}

    def finalize(self, session_id, meta, timing):
        self.finalized.append(session_id)
        self.job_ids += 1
        return f"job-{self.job_ids}"

    def job(self, job_id):
        self.job_calls += 1
        item = self.job_script.pop(0) if len(self.job_script) > 1 else self.job_script[0]
        if isinstance(item, Exception):
            raise item
        return item

    def transcript(self, job_id):
        return {"markdown": "# ok\n", "json": "{}"}

    def close(self):
        pass


QUEUED = {"state": wire.JobState.QUEUED}
RUNNING = {"state": wire.JobState.RUNNING, "progress": 0.4}
DONE = {"state": wire.JobState.DONE}


def _setup(tmp_path, script, **kw):
    session_dir = _make_session_dir(tmp_path, "webinar")
    queue = SessionQueue(tmp_path / ".upload-queue")
    entry_id = queue.enqueue(session_dir)
    server = FakeServer(script)
    kw.setdefault("poll_interval", 0.001)
    kw.setdefault("max_poll_delay", 0.002)
    worker = UploadWorker(queue, "http://unused", client_factory=server, **kw)
    return session_dir, queue, entry_id, server, worker


def test_slow_transcription_finalizes_once_and_is_not_a_failure(tmp_path):
    # Busy server: queued for many polls, transient outages, then done.
    script = [QUEUED] * 5 + [ServerUnavailable("down"), _status_error(503)] + [RUNNING] * 3 + [DONE]
    session_dir, queue, entry_id, server, worker = _setup(tmp_path, script, poll_budget=0.01)
    # Each pass has a tiny wait budget; keep passing until the entry is gone.
    for _ in range(500):
        for entry in queue.pending():  # make backoff windows due immediately
            queue.update_progress(entry["id"], next_attempt_at=None)
        worker.run_once()
        state = queue.read_state(entry_id)
        if state is None:
            break
        assert state["finalized"] and state["job_id"] == "job-1"
        assert state["upload_state"] == "complete"
        assert state["status"] != "failed" and state["attempts"] == 0
    assert queue.read_state(entry_id) is None
    assert server.finalized == ["webinar"]
    assert (session_dir / "transcript.md").exists()
    assert len(server.uploaded) == 2  # each track sent once


def test_queued_job_shows_uploaded_transcribing_queued(tmp_path):
    session_dir, queue, entry_id, server, worker = _setup(tmp_path, [QUEUED], poll_budget=0.01)
    worker.run_once()
    state = queue.read_state(entry_id)
    assert state["upload_state"] == "complete"
    assert state["transcription_state"] == "queued"
    assert state["finalized"] is True and state["last_error"] is None
    assert not (queue._claim_path(entry_id)).exists()


def test_restart_resumes_polling_without_finalizing_or_uploading(tmp_path):
    session_dir, queue, entry_id, server, worker = _setup(tmp_path, [QUEUED], poll_budget=0.01)
    worker.run_once()
    assert server.finalized == ["webinar"]
    # "Restart": a brand new queue object and worker over the same directory.
    queue2 = SessionQueue(tmp_path / ".upload-queue")
    queue2.update_progress(entry_id, next_attempt_at=None)
    server2 = FakeServer([RUNNING, DONE])
    UploadWorker(queue2, "http://unused", client_factory=server2, poll_interval=0.001).run_once()
    assert server2.finalized == [] and server2.uploaded == []
    assert queue2.read_state(entry_id) is None
    assert (session_dir / "transcript.md").exists()


def test_job_error_is_surfaced_and_never_refinalized(tmp_path):
    script = [{"state": wire.JobState.ERROR, "error": "CUDA out of memory"}]
    session_dir, queue, entry_id, server, worker = _setup(tmp_path, script)
    worker.run_once()
    state = queue.read_state(entry_id)
    assert state["status"] == "failed"
    assert "Transcription failed" in state["last_error"] and "CUDA" in state["last_error"]
    assert state["transcription_state"] == "error" and state["upload_state"] == "complete"
    # More passes, even after "retry all" (settings change), never re-finalize.
    for _ in range(3):
        queue.retry_all_now()
        worker.run_once()
    assert server.finalized == ["webinar"]
    assert len(server.uploaded) == 2


def test_job_404_refinalizes_exactly_once(tmp_path):
    script = [_status_error(404), DONE]
    session_dir, queue, entry_id, server, worker = _setup(tmp_path, script)
    worker.run_once()
    assert server.finalized == ["webinar", "webinar"]
    assert queue.read_state(entry_id) is None
    assert (session_dir / "transcript.md").exists()


def test_job_404_after_refinalize_is_an_ordinary_failure_not_a_loop(tmp_path):
    session_dir, queue, entry_id, server, worker = _setup(tmp_path, [_status_error(404)])
    worker.run_once()
    assert server.finalized == ["webinar", "webinar"]
    state = queue.read_state(entry_id)
    assert state is not None and state["attempts"] == 1


def test_worker_stop_during_wait_is_not_a_failed_attempt(tmp_path):
    session_dir, queue, entry_id, server, worker = _setup(tmp_path, [QUEUED], poll_budget=60)
    worker._stop_event.set()
    worker.run_once()  # returns before touching the entry
    worker._stop_event.clear()
    # Stop arrives mid-wait:
    orig = server.job

    def job_then_stop(job_id):
        worker._stop_event.set()
        return orig(job_id)

    server.job = job_then_stop
    worker.run_once()
    state = queue.read_state(entry_id)
    assert state["attempts"] == 0 and state["finalized"] is True and state["status"] == "pending"
    assert not queue._claim_path(entry_id).exists()
