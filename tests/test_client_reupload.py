"""Re-upload a saved recording: listing, validation, queue reset and the dialog."""

from __future__ import annotations

import json
import wave
from pathlib import Path

import pytest

from meeting_notes import wire
from meeting_notes.client import recordings
from meeting_notes.client.queue import SessionQueue, UploadWorker
from tests.test_client_r2 import _close, _configure, _pump, _window, home, qt_app  # noqa: F401
from tests.test_client_transport import _make_session_dir

from PySide6.QtWidgets import QFileDialog


def _at(session_dir: Path, created: str, name: str = "") -> Path:
    """Give a fixture session a start time (and optional meeting name)."""
    meta_path = session_dir / "session.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["created"] = created
    if name:
        meta["name"] = name
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    return session_dir


def _broken(tmp_path: Path, name: str) -> Path:
    """A folder with session.json but an empty (header-only) WAV."""
    d = _make_session_dir(tmp_path, name)
    with wave.open(str(d / "mic.wav"), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(48000)
    return d


class _FakeServer:
    """Records what the uploader sends, like the fakes in test_client_transport."""

    def __init__(self):
        self.uploaded = []
        self.finalized = []

    def __call__(self):
        return self

    def upload_track(self, session_id, track, pcm_path, frames, progress_callback=None):
        self.uploaded.append((session_id, track))
        return {"frames": frames}

    def report_upload_status(self, session_id, payload):
        return {}

    def finalize(self, session_id, meta, timing):
        self.finalized.append(session_id)
        return "job-1"

    def job(self, job_id):
        return {"state": wire.JobState.DONE}

    def transcript(self, job_id):
        return {"markdown": "# ok\n", "json": "{}"}

    def close(self):
        pass


@pytest.fixture
def save_dir(tmp_path):
    root = tmp_path / "Meeting Notes"
    root.mkdir()
    _at(_make_session_dir(root, "2026-01-01_09-00-00_standup"), "2026-01-01T09:00:00", "Standup")
    _at(_make_session_dir(root, "2026-02-01_09-00-00_review"), "2026-02-01T09:00:00")
    _at(_broken(root, "2026-03-01_09-00-00_broken"), "2026-03-01T09:00:00")
    return root


# -- listing and validation -------------------------------------------------------


def test_scan_lists_valid_newest_first_and_flags_the_invalid_one(save_dir):
    queue = SessionQueue.for_save_dir(save_dir)
    found = recordings.scan_save_folder(save_dir, queue)
    assert [r.path.name for r in found] == [
        "2026-03-01_09-00-00_broken",
        "2026-02-01_09-00-00_review",
        "2026-01-01_09-00-00_standup",
    ]
    by_name = {r.path.name: r for r in found}
    assert by_name["2026-03-01_09-00-00_broken"].error == "mic.wav is empty"
    good = by_name["2026-01-01_09-00-00_standup"]
    assert good.name == "Standup"  # from session meta
    assert by_name["2026-02-01_09-00-00_review"].name == "2026-02-01_09-00-00_review"  # folder name fallback
    assert good.valid and good.duration_sec == pytest.approx(0.2)
    assert good.size_bytes > 0 and good.queued is False
    # the hidden queue folder is not a recording
    assert ".upload-queue" not in by_name


def test_validate_reports_missing_pieces(tmp_path):
    assert recordings.validate_recording(tmp_path / "nope") == "Not a folder"
    d = tmp_path / "x"
    d.mkdir()
    assert recordings.validate_recording(d) == "session.json is missing"
    (d / "session.json").write_text("{not json", encoding="utf-8")
    assert "unreadable" in recordings.validate_recording(d)
    good = _make_session_dir(tmp_path, "good")
    assert recordings.validate_recording(good) is None
    (good / "system.timing.jsonl").unlink()
    assert recordings.validate_recording(good) == "system.timing.jsonl is missing"
    (good / "mic.wav").unlink()
    assert recordings.validate_recording(good) == "mic.wav is missing"


# -- queue behaviour --------------------------------------------------------------


def test_requeue_resets_track_acks_so_a_fresh_worker_sends_every_track_and_finalizes(tmp_path):
    session_dir = _make_session_dir(tmp_path, "lost-meeting")
    queue = SessionQueue(tmp_path / ".upload-queue")
    entry = queue.enqueue(session_dir)
    # Earlier life: both tracks acked, then the entry went terminal-failed.
    queue.mark_track_uploaded(entry, "mic")
    queue.mark_track_uploaded(entry, "system")
    queue.mark_attempt_failed(entry, "boom", next_attempt_at=9e12, terminal=True)

    assert queue.requeue(session_dir) == entry
    state = queue.read_state(entry)
    assert state["uploaded_tracks"] == [] and state["status"] == "pending"
    assert state["attempts"] == 0 and state["next_attempt_at"] is None
    assert len(queue.pending()) == 1

    fake = _FakeServer()
    UploadWorker(queue, "http://unused", poll_interval=0.01, client_factory=fake).run_once()
    assert sorted(t for _sid, t in fake.uploaded) == ["mic", "system"]
    assert {sid for sid, _t in fake.uploaded} == {"lost-meeting"}  # original session id
    assert fake.finalized == ["lost-meeting"]
    assert queue.pending() == []


def test_reupload_of_an_already_queued_entry_is_not_duplicated(tmp_path):
    session_dir = _make_session_dir(tmp_path, "queued-one")
    queue = SessionQueue(tmp_path / ".upload-queue")
    queue.enqueue(session_dir)
    woke = []
    result = recordings.reupload(queue, [session_dir], wake=lambda: woke.append(1))
    assert result.queued == [] and len(result.already_queued) == 1
    assert len(queue.pending()) == 1
    assert result.summary() == "1 recording queued for upload"
    assert woke == [1]


def test_requeue_leaves_an_entry_that_is_uploading_right_now_alone(tmp_path):
    session_dir = _make_session_dir(tmp_path, "busy")
    queue = SessionQueue(tmp_path / ".upload-queue")
    entry = queue.enqueue(session_dir)
    queue.mark_track_uploaded(entry, "mic")
    assert queue.claim(entry)
    queue.requeue(session_dir)
    assert queue.read_state(entry)["uploaded_tracks"] == ["mic"]


def test_reupload_rejects_invalid_folders(tmp_path):
    good = _make_session_dir(tmp_path, "good")
    bad = _broken(tmp_path, "bad")
    queue = SessionQueue(tmp_path / ".upload-queue")
    result = recordings.reupload(queue, [good, bad])
    assert len(result.queued) == 1 and list(result.rejected) == [bad]
    assert [e["id"] for e in queue.pending()] == ["good"]
    assert "1 skipped" in result.summary()


def test_controller_reupload_wakes_and_starts_the_uploader(home, monkeypatch):
    from meeting_notes.client.controller import RecordingController

    _configure(home, url="http://meeting.lan")
    (home / "rec").mkdir()
    d = _make_session_dir(home / "rec", "wake-me")
    controller = RecordingController()
    queue = controller.session_queue()
    entry = queue.enqueue(d)
    queue.mark_attempt_failed(entry, "x", next_attempt_at=9e12)
    started = []
    monkeypatch.setattr(controller, "start_uploader", lambda force=False: started.append(force) or True)
    result = controller.reupload_recordings([d])
    assert result.total == 1
    assert queue.read_state(entry)["next_attempt_at"] is None
    assert started == [True]


# -- dialog ------------------------------------------------------------------------


def _dialog(save_dir, **kwargs):
    from meeting_notes.client.ui.reupload_dialog import ReuploadDialog

    kwargs.setdefault("server_configured", True)
    return ReuploadDialog(save_dir=save_dir, queue=SessionQueue.for_save_dir(save_dir), **kwargs)


def test_dialog_lists_valid_recordings_and_shows_the_invalid_error(qt_app, home, save_dir):
    SessionQueue.for_save_dir(save_dir).enqueue(save_dir / "2026-02-01_09-00-00_review")
    dialog = _dialog(save_dir)
    rows = {r.info.path.name: r for r in dialog.rows()}
    assert len(rows) == 3
    bad = rows["2026-03-01_09-00-00_broken"]
    assert bad.error_label is not None and bad.error_label.text() == "mic.wav is empty"
    assert not bad.check.isEnabled()
    review = rows["2026-02-01_09-00-00_review"]
    assert review.badge is not None and review.badge.text() == "Queued"
    valid = [r for r in dialog.rows() if r.info.valid]
    assert len(valid) == 2 and all(r.check.isEnabled() for r in valid)
    assert not dialog.reupload_button.isEnabled()
    dialog.close()


def test_dialog_select_all_and_reupload_queues_the_chosen_and_confirms(qt_app, home, save_dir):
    dialog = _dialog(save_dir)
    dialog.select_all.setChecked(True)
    assert len(dialog.selected()) == 2  # the invalid row is never selected
    assert dialog.reupload_button.text() == "Re-upload 2"
    dialog.reupload_button.click()
    queue = SessionQueue.for_save_dir(save_dir)
    assert sorted(e["id"] for e in queue.pending()) == [
        "2026-01-01_09-00-00_standup",
        "2026-02-01_09-00-00_review",
    ]
    assert dialog.result_label.text() == "2 recordings queued for upload."
    assert not dialog.result_box.isHidden()
    # the list refreshes: both now show the Queued badge
    assert sum(1 for r in dialog.rows() if r.badge is not None) == 2
    dialog.close()


def test_dialog_requires_a_configured_server(qt_app, home, save_dir):
    dialog = _dialog(save_dir, server_configured=False)
    dialog.select_all.setChecked(True)
    dialog.reupload_button.click()
    assert "server URL" in dialog.result_label.text()
    assert SessionQueue.for_save_dir(save_dir).pending() == []
    dialog.close()


def test_browse_for_a_recording_folder_elsewhere(qt_app, home, save_dir, tmp_path, monkeypatch):
    (tmp_path / "usb-stick").mkdir()
    elsewhere = _make_session_dir(tmp_path / "usb-stick", "from-usb")
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: str(elsewhere)))
    submitted = []

    def submit(folders):
        submitted.extend(folders)
        return recordings.reupload(SessionQueue.for_save_dir(save_dir), folders)

    dialog = _dialog(save_dir, submit=submit)
    dialog.browse_button.click()
    names = [r.info.path.name for r in dialog.rows()]
    assert "from-usb" in names
    assert [i.path.name for i in dialog.selected()] == ["from-usb"]
    dialog.reupload_button.click()
    assert [p.name for p in submitted] == ["from-usb"]
    assert [e["id"] for e in SessionQueue.for_save_dir(save_dir).pending()] == ["from-usb"]
    dialog.close()


def test_browse_to_an_empty_folder_shows_an_inline_error(qt_app, home, save_dir, tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: str(empty)))
    dialog = _dialog(save_dir)
    dialog.browse_button.click()
    assert "No recordings found" in dialog.result_label.text()
    dialog.close()


def test_main_window_more_menu_has_the_reupload_action(qt_app, home, monkeypatch):
    _configure(home, url="")
    window = _window(qt_app, monkeypatch)
    try:
        assert window.reupload_action in window.more_menu.actions()
        assert window.reupload_action.text() == "Re-upload a saved recording..."
    finally:
        _close(window)
