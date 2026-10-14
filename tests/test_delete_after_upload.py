"""Settings > Recordings > "Delete from this computer after the server confirms the upload" (0.7.16).

The upload worker removes a recording's folder right after the finalize succeeded AND the server's own record of the
meeting shows the upload complete with the audio present. Everything else keeps the recording.
"""

from __future__ import annotations

import json
import shutil

import pytest

from meeting_notes import config as config_mod
from meeting_notes.client import recordings, retention
from meeting_notes.client import settings_schema as S
from meeting_notes.client.api import ServerUnavailable
from meeting_notes.client.queue import SessionQueue, UploadWorker
from tests.test_client_retention import _status_error
from tests.test_client_transport import _make_session_dir
from tests.test_client_upload_wait import DONE, QUEUED, FakeServer


def _detail(session_id, **over):
    doc = {
        "session_id": session_id,
        "meta": {},
        "has_audio": True,
        "pipeline": {"upload": {"state": "complete"}, "transcription": {"state": "pending"}, "state": "pending"},
        "transcript_job_id": None,
    }
    doc.update(over)
    return doc


class Server(FakeServer):
    """FakeServer that also answers GET /v1/sessions/{id}."""

    def __init__(self, job_script, detail=None):
        super().__init__(job_script)
        self.detail = detail or (lambda sid: _detail(sid))
        self.detail_calls = []

    def session_detail(self, session_id):
        self.detail_calls.append(session_id)
        return self.detail(session_id)


class Remover:
    def __init__(self):
        self.removed = []

    def __call__(self, path):
        self.removed.append(path.name)
        shutil.rmtree(path)
        return "test"


def _setup(tmp_path, *, enabled=True, script=(DONE,), detail=None, active=None, fail_upload=False):
    session_dir = _make_session_dir(tmp_path, "standup")
    queue = SessionQueue(tmp_path / ".upload-queue")
    entry_id = queue.enqueue(session_dir)
    server = Server(list(script), detail)
    if fail_upload:
        def boom(*a, **k):
            raise ServerUnavailable("down")

        server.upload_track = boom
    remover = Remover()
    worker = UploadWorker(
        queue, "http://unused", client_factory=server, poll_interval=0.001, max_poll_delay=0.002,
        poll_budget=0.01, delete_after_upload=lambda: enabled,
        active_dir=(lambda: session_dir) if active else (lambda: None), remover=remover,
    )
    return session_dir, queue, entry_id, server, worker, remover


# -- setting + schema ----------------------------------------------------------------------


def test_the_setting_is_off_by_default_and_strictly_boolean():
    assert config_mod.delete_after_upload({}) is False
    assert config_mod.delete_after_upload({"delete_after_upload": True}) is True
    for junk in (False, None, "true", "yes", 1, [], {}):
        assert config_mod.delete_after_upload({"delete_after_upload": junk}) is False


def test_the_schema_declares_it_once_in_recordings_local():
    schema = S.build_schema()
    page = next(p for p in schema["pages"] if p["key"] == "recordings")
    keys = [i.get("key") or i.get("id") for i in page["items"]]
    assert keys.index("section.local") < keys.index("delete_after_upload") < keys.index("recordings.stats")
    field = S.fields_by_key(schema)["delete_after_upload"]
    assert field["kind"] == "bool"
    assert field["label"] == "Delete from this computer after the server confirms the upload"
    for phrase in ("whole recording", "transcript and notes stay on the server", "fails to upload is never deleted",
                   "overrides Keep recordings"):
        assert phrase in field["help"]
    assert S.get_values({})["delete_after_upload"] is False


# -- the upload worker ---------------------------------------------------------------------


def test_a_confirmed_upload_deletes_the_local_recording(tmp_path):
    session_dir, queue, entry_id, server, worker, remover = _setup(tmp_path)
    worker.run_once()
    assert server.finalized == ["standup"] and server.detail_calls == ["standup"]
    assert not session_dir.exists() and remover.removed == ["standup"]
    assert queue.read_state(entry_id) is None  # transcript arrived, entry done
    assert queue.delete_pending_ids() == []


def test_off_deletes_nothing(tmp_path):
    session_dir, queue, entry_id, server, worker, remover = _setup(tmp_path, enabled=False)
    worker.run_once()
    assert session_dir.exists() and remover.removed == [] and server.detail_calls == []
    assert (session_dir / "transcript.md").exists()


def test_a_failed_upload_keeps_everything(tmp_path):
    session_dir, queue, entry_id, server, worker, remover = _setup(tmp_path, fail_upload=True)
    worker.run_once()
    assert session_dir.exists() and remover.removed == [] and server.detail_calls == []
    assert queue.read_state(entry_id) is not None


@pytest.mark.parametrize("detail", [
    lambda sid: _detail(sid, pipeline={"upload": {"state": "uploading"}}),            # partial
    lambda sid: _detail(sid, pipeline={"upload": {"state": "pending"}}),
    lambda sid: _detail(sid, pipeline={"upload": {"state": "error"}}),
    lambda sid: _detail(sid, has_audio=False),                                         # server has no audio
    lambda sid: _detail(sid, deleted_at="2026-10-01"),                                 # in the server's trash
    lambda sid: _detail(sid, session_id="someone-else"),
    lambda sid: _detail(sid, pipeline=None),
    lambda sid: "not json",
])
def test_an_unconfirmed_upload_keeps_the_recording_and_marks_a_retry(tmp_path, detail):
    session_dir, queue, entry_id, server, worker, remover = _setup(tmp_path, detail=detail)
    worker.run_once()
    assert session_dir.exists() and remover.removed == []
    assert (session_dir / "mic.wav").exists()


@pytest.mark.parametrize("failure", [ServerUnavailable("down"), _status_error(401), _status_error(500), ValueError("x")])
def test_server_trouble_during_the_check_keeps_it_and_retries_later(tmp_path, failure):
    def detail(sid):
        raise failure

    session_dir, queue, entry_id, server, worker, remover = _setup(tmp_path, detail=detail)
    worker.run_once()
    assert session_dir.exists() and remover.removed == []
    assert queue.delete_pending_ids() == ["standup"]
    assert queue.read_state(entry_id) is None  # the upload itself still finished normally


def test_the_404_answer_is_final_so_no_retry_is_marked(tmp_path):
    def detail(sid):
        raise _status_error(404)  # also what the server says for a meeting in its trash

    session_dir, queue, entry_id, server, worker, remover = _setup(tmp_path, detail=detail)
    worker.run_once()
    assert session_dir.exists() and queue.delete_pending_ids() == []


def test_the_recording_in_progress_is_never_touched(tmp_path):
    session_dir, queue, entry_id, server, worker, remover = _setup(tmp_path, active=True)
    worker.run_once()
    assert session_dir.exists() and remover.removed == [] and server.detail_calls == []


def test_a_slow_transcript_after_deletion_still_completes(tmp_path):
    script = [QUEUED] * 3 + [DONE]
    session_dir, queue, entry_id, server, worker, remover = _setup(tmp_path, script=script)
    for _ in range(200):
        for e in queue.pending():
            queue.update_progress(e["id"], next_attempt_at=None)
        worker.run_once()
        if queue.read_state(entry_id) is None:
            break
    assert queue.read_state(entry_id) is None
    assert server.finalized == ["standup"] and len(server.uploaded) == 2  # never re-uploaded or re-finalized
    assert not session_dir.exists() and remover.removed == ["standup"]


def test_the_server_losing_the_job_after_deletion_fails_cleanly(tmp_path):
    session_dir, queue, entry_id, server, worker, remover = _setup(tmp_path, script=[_status_error(404)])
    worker.run_once()
    state = queue.read_state(entry_id)
    assert not session_dir.exists()
    assert state["status"] == "failed" and "Re-transcribe" in state["last_error"]
    assert server.finalized == ["standup"]


def test_the_processing_panel_keeps_the_name_after_the_folder_is_gone(tmp_path):
    session_dir, queue, entry_id, server, worker, remover = _setup(tmp_path, script=[QUEUED])
    meta_path = session_dir / "session.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["name"] = "Weekly standup"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    worker.run_once()
    state = queue.read_state(entry_id)
    assert not session_dir.exists() and state["local_deleted"] is True and state["name"] == "Weekly standup"


# -- the retry sweep -----------------------------------------------------------------------


def test_the_sweep_retries_a_marked_recording_and_only_that_one(tmp_path):
    save = tmp_path / "Recordings"
    save.mkdir()
    marked = _make_session_dir(save, "marked")
    other = _make_session_dir(save, "other")  # uploaded earlier, never marked: must not be touched
    queue = SessionQueue.for_save_dir(save)
    queue.mark_delete_pending("marked")
    server = Server([DONE])
    report = retention.sweep_delete_after_upload(
        save, "http://x", None, client_factory=lambda: server, remover=Remover()
    )
    assert [d.folder.name for d in report.deleted] == ["marked"]
    assert not marked.exists() and other.exists() and server.detail_calls == ["marked"]
    assert queue.delete_pending_ids() == []


def test_the_sweep_keeps_the_marker_while_the_server_is_unreachable(tmp_path):
    save = tmp_path / "Recordings"
    save.mkdir()
    folder = _make_session_dir(save, "marked")
    queue = SessionQueue.for_save_dir(save)
    queue.mark_delete_pending("marked")

    def down(sid):
        raise ServerUnavailable("down")

    server = Server([DONE], detail=down)
    report = retention.sweep_delete_after_upload(
        save, "http://x", None, client_factory=lambda: server, remover=Remover()
    )
    assert not report.deleted and folder.exists() and queue.delete_pending_ids() == ["marked"]


def test_the_sweep_clears_a_vanished_marker_and_skips_a_queued_recording(tmp_path):
    save = tmp_path / "Recordings"
    save.mkdir()
    queued = _make_session_dir(save, "queued")
    queue = SessionQueue.for_save_dir(save)
    queue.enqueue(queued)
    queue.mark_delete_pending("queued")
    queue.mark_delete_pending("vanished")
    server = Server([DONE])
    report = retention.sweep_delete_after_upload(
        save, "http://x", None, client_factory=lambda: server, remover=Remover()
    )
    assert not report.deleted and queued.exists()
    assert queue.delete_pending_ids() == ["queued"] and server.detail_calls == []


def test_a_deleted_recording_just_leaves_the_local_lists(tmp_path):
    save = tmp_path / "Recordings"
    save.mkdir()
    keep = _make_session_dir(save, "keep")
    gone = _make_session_dir(save, "gone")
    assert {f.name for f in recordings.session_folders(save)} == {"keep", "gone"}
    shutil.rmtree(gone)
    assert [f.name for f in recordings.session_folders(save)] == ["keep"] and keep.exists()
