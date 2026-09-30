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
    kwargs.setdefault("status_provider", lambda ids: {})
    kwargs.setdefault("async_status", False)
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
    assert review.badge.text() == "Waiting to upload"  # queued locally; the pill is the status
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
    # the list refreshes: both now wait on the queue
    assert sum(1 for r in dialog.rows() if r.status["status"] == "waiting") == 2
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


# -- status pills, cache, delete ----------------------------------------------------

import httpx  # noqa: E402

READY = {"on_server": True, "has_copy": True, "in_trash": False, "transcription": "complete", "error": None}
GONE = {"on_server": False, "has_copy": False, "in_trash": False, "transcription": None, "error": None}


def _state(**kw):
    out = dict(READY)
    out.update(kw)
    return out


class _Provider:
    """A fake ``status_provider`` that records each call's ids."""

    def __init__(self, states=None, default=None, error=None):
        self.states = states or {}
        self.default = default
        self.error = error
        self.calls = []

    def __call__(self, ids):
        self.calls.append(list(ids))
        if self.error is not None:
            raise self.error
        return {i: self.states.get(i, self.default) for i in ids if self.states.get(i, self.default) is not None}


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _rows(dialog):
    return {r.info.path.name: r for r in dialog.rows()}


def _pill(row):
    return row.badge.text(), row.badge.property("tone")


@pytest.fixture
def many(tmp_path):
    """One recording per status."""
    root = tmp_path / "Meeting Notes"
    root.mkdir()
    names = ["rec", "upl", "wait", "fail", "ready", "trans", "queued", "txerr", "part", "trash", "gone", "unk"]
    for n, name in enumerate(names):
        _at(_make_session_dir(root, name), f"2026-04-{n + 1:02d}T09:00:00")
    _at(_broken(root, "invalid"), "2026-05-01T09:00:00")
    queue = SessionQueue.for_save_dir(root)
    upl = queue.enqueue(root / "upl")
    queue.claim(upl)
    queue.update_progress(upl, upload_percent=42.0)
    queue.enqueue(root / "wait")
    failed = queue.enqueue(root / "fail")
    queue.mark_attempt_failed(failed, "disk full", next_attempt_at=9e12, terminal=True)
    return root


def test_every_status_pill_text_and_tone(qt_app, home, many):
    provider = _Provider(
        states={
            "ready": READY,
            "trans": _state(transcription="transcribing"),
            "queued": _state(transcription="queued"),
            "txerr": _state(transcription="error", error="model crashed"),
            "part": _state(has_copy=False, transcription=None),
            "trash": _state(on_server=False, has_copy=False, in_trash=True, transcription=None),
            "gone": GONE,
            "invalid": GONE,
            "upl": GONE, "wait": GONE, "fail": GONE,
        }
    )
    dialog = _dialog(many, status_provider=provider, active_dir=many / "rec")
    rows = _rows(dialog)
    assert _pill(rows["rec"]) == ("Recording now", "info")
    assert _pill(rows["upl"]) == ("Uploading 42%", "info")
    assert _pill(rows["wait"]) == ("Waiting to upload", "info")
    assert _pill(rows["fail"]) == ("Upload failed: disk full", "error")
    assert _pill(rows["ready"]) == ("Uploaded · transcript ready", "ok")
    assert _pill(rows["trans"]) == ("Uploaded · transcribing", "info")
    assert _pill(rows["queued"]) == ("Uploaded · transcription queued", "info")
    assert _pill(rows["txerr"]) == ("Uploaded · transcription failed", "warn")
    assert rows["txerr"].badge.toolTip() == "model crashed"
    assert _pill(rows["part"]) == ("Partly uploaded", "warn")
    assert _pill(rows["trash"]) == ("In server trash", "warn")
    assert _pill(rows["gone"]) == ("Not on server", "warn")
    assert _pill(rows["invalid"]) == ("Can't upload: mic.wav is empty", "error")
    assert rows["invalid"].error_label.text() == "mic.wav is empty"  # the error line stays
    assert _pill(rows["unk"]) == ("Server status unknown", "muted")  # the server returned nothing for it
    # the recording in progress can not be picked, and nothing is asked about it
    assert not rows["rec"].check.isEnabled()
    assert "rec" not in provider.calls[0]
    dialog.close()


def test_long_failure_reason_is_cut_in_the_pill_but_kept_in_the_tooltip(qt_app, home, tmp_path):
    root = tmp_path / "Meeting Notes"
    root.mkdir()
    d = _at(_make_session_dir(root, "long"), "2026-04-01T09:00:00")
    queue = SessionQueue.for_save_dir(root)
    entry = queue.enqueue(d)
    queue.mark_attempt_failed(entry, "x" * 120, next_attempt_at=9e12, terminal=True)
    dialog = _dialog(root)
    row = _rows(dialog)["long"]
    assert len(row.badge.text()) <= 40 and row.badge.text().endswith("…")
    assert row.status["label"] in row.badge.toolTip()
    dialog.close()


def test_pills_say_checking_until_the_async_answer_arrives(qt_app, home, save_dir):
    import threading

    gate = threading.Event()

    def provider(ids):
        gate.wait(3)
        return {i: READY for i in ids}

    dialog = _dialog(save_dir, status_provider=provider, async_status=True)
    name = "2026-01-01_09-00-00_standup"
    assert _pill(_rows(dialog)[name]) == ("Checking server...", "muted")
    gate.set()
    _pump(lambda: _rows(dialog)[name].badge.text() != "Checking server...", timeout=3.0)
    assert _pill(_rows(dialog)[name]) == ("Uploaded · transcript ready", "ok")
    dialog.close()


def test_status_cache_ttl_and_refresh_button(qt_app, home, save_dir):
    clock = _Clock()
    provider = _Provider(default=READY)
    dialog = _dialog(save_dir, status_provider=provider, clock=clock)
    assert len(provider.calls) == 1 and len(provider.calls[0]) == 3
    dialog.refresh()
    dialog.refresh(keep_checked=True)
    assert len(provider.calls) == 1  # fresh answers are reused
    clock.now += 29
    dialog.refresh()
    assert len(provider.calls) == 1
    clock.now += 2  # 31 s: expired
    dialog.refresh()
    assert len(provider.calls) == 2 and len(provider.calls[1]) == 3
    assert dialog.refresh_status_button.objectName() == "refreshStatus"
    dialog.refresh_status_button.click()
    assert len(provider.calls) == 3 and len(provider.calls[2]) == 3
    dialog.close()


def test_only_missing_ids_are_requested_after_browse(qt_app, home, save_dir, tmp_path, monkeypatch):
    (tmp_path / "usb").mkdir()
    elsewhere = _make_session_dir(tmp_path / "usb", "from-usb")
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: str(elsewhere)))
    provider = _Provider(default=READY)
    dialog = _dialog(save_dir, status_provider=provider)
    dialog.browse_button.click()
    assert provider.calls[1] == ["from-usb"]
    dialog.close()


def test_reupload_invalidates_the_status_of_those_ids_and_refetches(qt_app, home, save_dir):
    provider = _Provider(default=READY)
    dialog = _dialog(save_dir, status_provider=provider)
    _rows(dialog)["2026-01-01_09-00-00_standup"].set_checked(True)
    dialog.reupload_button.click()
    assert provider.calls[-1] == ["2026-01-01_09-00-00_standup"]
    dialog.close()


def _http_404():
    return httpx.HTTPStatusError(
        "404", request=httpx.Request("POST", "http://x/v1/recordings/status"), response=httpx.Response(404)
    )


@pytest.mark.parametrize(
    "make_error, note",
    [
        (lambda: OSError("connection refused"), "Could not reach the server; showing what this computer knows."),
        (_http_404, "This server is too old to report status."),
    ],
)
def test_provider_failure_shows_local_status_and_a_note(qt_app, home, save_dir, make_error, note):
    SessionQueue.for_save_dir(save_dir).enqueue(save_dir / "2026-02-01_09-00-00_review")
    provider = _Provider(error=make_error())
    dialog = _dialog(save_dir, status_provider=provider)
    rows = _rows(dialog)
    assert _pill(rows["2026-02-01_09-00-00_review"]) == ("Waiting to upload", "info")
    assert _pill(rows["2026-01-01_09-00-00_standup"]) == ("Server status unknown", "muted")
    assert dialog.note_label.text() == note and not dialog.note_label.isHidden()
    dialog.refresh()
    assert len(provider.calls) == 1  # a failure is not hammered on every re-render
    dialog.refresh_status_button.click()
    assert len(provider.calls) == 2
    dialog.close()


def test_no_server_configured_means_no_provider_call_and_a_note(qt_app, home, save_dir):
    dialog = _dialog(save_dir, status_provider=None, server_configured=False)
    assert "No server is set up" in dialog.note_label.text()
    assert _pill(_rows(dialog)["2026-01-01_09-00-00_standup"]) == ("Server status unknown", "muted")
    dialog.close()


class _Confirm:
    def __init__(self, answer=True):
        self.answer = answer
        self.calls = []

    def __call__(self, names, warning):
        self.calls.append((list(names), warning))
        return self.answer


class _Remover:
    def __init__(self):
        self.moved = []

    def __call__(self, path):
        self.moved.append(Path(path).name)
        return "recycle-bin"


def _delete_dialog(root, provider, **kw):
    kw.setdefault("confirm", _Confirm())
    kw.setdefault("remover", _Remover())
    return _dialog(root, status_provider=provider, **kw)


def test_delete_button_follows_the_selection(qt_app, home, save_dir):
    dialog = _delete_dialog(save_dir, _Provider(default=READY))
    assert dialog.delete_button.objectName() == "deleteButton"
    assert dialog.delete_button.text() == "Delete from this computer..."
    assert not dialog.delete_button.isEnabled()
    _rows(dialog)["2026-01-01_09-00-00_standup"].set_checked(True)
    assert dialog.delete_button.isEnabled()
    _rows(dialog)["2026-01-01_09-00-00_standup"].set_checked(False)
    assert not dialog.delete_button.isEnabled()
    dialog.close()


def test_delete_confirm_warns_only_when_a_selected_row_has_no_server_copy(qt_app, home, save_dir):
    from meeting_notes.client import retention

    provider = _Provider(states={"2026-01-01_09-00-00_standup": READY, "2026-02-01_09-00-00_review": GONE})
    confirm = _Confirm(answer=False)
    dialog = _delete_dialog(save_dir, provider, confirm=confirm)
    rows = _rows(dialog)
    rows["2026-01-01_09-00-00_standup"].set_checked(True)
    dialog.delete_button.click()
    assert confirm.calls == [(["Standup"], None)]  # calm: the server has it
    rows["2026-02-01_09-00-00_review"].set_checked(True)
    dialog.delete_button.click()
    names, warning = confirm.calls[1]
    assert names == ["2026-02-01_09-00-00_review", "Standup"]  # newest first
    assert warning == (
        "The server has no copy. This permanently removes the only copy "
        f"(it goes to this computer's {retention.trash_name()})."
    )
    assert dialog._remover.moved == []  # declined both times
    dialog.close()


def test_delete_warns_when_the_server_could_not_be_asked(qt_app, home, save_dir):
    confirm = _Confirm(answer=False)
    dialog = _delete_dialog(save_dir, _Provider(error=OSError("down")), confirm=confirm)
    _rows(dialog)["2026-01-01_09-00-00_standup"].set_checked(True)
    dialog.delete_button.click()
    assert confirm.calls[0][1] and "no copy" in confirm.calls[0][1]
    dialog.close()


def test_delete_moves_folders_drops_queue_entries_and_reports(qt_app, home, save_dir):
    from meeting_notes.client import retention

    queue = SessionQueue.for_save_dir(save_dir)
    entry = queue.enqueue(save_dir / "2026-01-01_09-00-00_standup")
    remover = _Remover()
    dialog = _delete_dialog(save_dir, _Provider(default=READY), remover=remover)
    rows = _rows(dialog)
    rows["2026-01-01_09-00-00_standup"].set_checked(True)
    rows["2026-02-01_09-00-00_review"].set_checked(True)
    dialog.delete_button.click()
    assert sorted(remover.moved) == ["2026-01-01_09-00-00_standup", "2026-02-01_09-00-00_review"]
    assert queue.read_state(entry) is None and queue.pending() == []
    assert dialog.result_label.text() == f"Moved 2 recordings to the {retention.trash_name()}."
    assert not dialog.result_box.isHidden()
    dialog.close()


def test_delete_refuses_active_and_uploading_and_says_why(qt_app, home, many):
    remover = _Remover()
    queue = SessionQueue.for_save_dir(many)
    dialog = _delete_dialog(many, _Provider(default=READY), remover=remover, active_dir=many / "rec")
    rows = _rows(dialog)
    assert not rows["rec"].check.isEnabled()
    rows["rec"].set_checked(True)
    assert not rows["rec"].checked  # can not even be selected
    rows["upl"].set_checked(True)
    rows["ready"].set_checked(True)
    dialog.delete_button.click()
    assert remover.moved == ["ready"]
    text = dialog.result_label.text()
    assert text.startswith("Moved 1 recording to the")
    assert "upl: This recording is uploading right now" in text
    assert queue.read_state(queue.entry_id(many / "upl")) is not None  # the uploading entry is untouched
    dialog.close()


def test_delete_with_nothing_deleted_shows_an_error_line(qt_app, home, many):
    dialog = _delete_dialog(many, _Provider(default=READY))
    _rows(dialog)["upl"].set_checked(True)
    dialog.delete_button.click()
    assert dialog.result_label.text().startswith("Nothing was deleted.")
    assert dialog.result_label.property("state") == "error"
    dialog.close()


def test_delete_a_browsed_folder_outside_the_save_folder(qt_app, home, save_dir, tmp_path, monkeypatch):
    (tmp_path / "usb").mkdir()
    elsewhere = _make_session_dir(tmp_path / "usb", "from-usb")
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: str(elsewhere)))
    remover = _Remover()
    confirm = _Confirm()
    dialog = _delete_dialog(save_dir, _Provider(default=GONE), remover=remover, confirm=confirm)
    dialog.browse_button.click()
    dialog.delete_button.click()
    assert remover.moved == ["from-usb"]
    assert confirm.calls[0][1] is not None  # no server copy: warned
    assert dialog._extra == []
    dialog.close()


def test_confirm_dialog_shows_names_and_the_right_text(qt_app, home):
    from meeting_notes import recording_status
    from meeting_notes.client.ui.reupload_dialog import DeleteConfirmDialog

    names = [f"Meeting {i}" for i in range(8)]
    calm = DeleteConfirmDialog(names, None)
    assert calm.names_label.text().count("Meeting") == 5 and calm.more_label.text() == "and 3 more"
    assert calm.warning_box is None and "stay on the server" in calm.calm_label.text()
    assert calm.delete_button.text() == "Delete" and calm.cancel_button.isDefault()
    risky = DeleteConfirmDialog(names[:1], recording_status.delete_warning("Recycle Bin"))
    assert risky.warning_box is not None and "no copy" in risky.warning_label.text()
    assert risky.calm_label is None
    calm.close()
    risky.close()


# -- remote_recordings.delete_folders ----------------------------------------------


def test_delete_folders_applies_the_safety_rules_and_results_keep_order(tmp_path):
    from meeting_notes.client import remote_recordings

    root = tmp_path / "rec"
    root.mkdir()
    a, b, c, d = (_make_session_dir(root, n) for n in "abcd")
    queue = SessionQueue.for_save_dir(root)
    queue.enqueue(a)
    busy = queue.enqueue(b)
    assert queue.claim(busy)
    moved = []

    def remover(path):
        if path.name == "d":
            raise OSError("locked")
        moved.append(path.name)
        return "trash"

    out = remote_recordings.delete_folders([a, b, c, d], queue, active_dir=c, remover=remover)
    assert [(r["session_id"], r["code"]) for r in out["results"]] == [
        ("a", None), ("b", "uploading"), ("c", "active_recording"), ("d", "failed"),
    ]
    assert moved == ["a"] and out["deleted"] == 1 and out["freed_bytes"] > 0
    assert queue.read_state(queue.entry_id(a)) is None
    assert queue.read_state(busy) is not None
    assert "locked" in out["results"][3]["error"]


def test_delete_local_keeps_its_shape_and_reports_not_found_in_order(tmp_path):
    from meeting_notes.client import remote_recordings

    root = tmp_path / "rec"
    root.mkdir()
    _make_session_dir(root, "a")
    queue = SessionQueue.for_save_dir(root)
    out = remote_recordings.delete_local(root, queue, ["../x", "a"], None, lambda p: "trash")
    assert [(r["session_id"], r["code"]) for r in out["results"]] == [("../x", "not_found"), ("a", None)]
    assert out["deleted"] == 1
