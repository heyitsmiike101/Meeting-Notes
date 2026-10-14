"""The Processing panel: per-meeting upload / transcription progress in the main window."""

from __future__ import annotations

import json
import time

import pytest

pytest.importorskip("PySide6", reason="PySide6 not installed")

from PySide6.QtWidgets import QApplication  # noqa: E402

from meeting_notes.client.ui.processing_panel import describe_entry  # noqa: E402
from meeting_notes.client.ui.theme import APP_STYLE  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(APP_STYLE)
    return app


@pytest.fixture
def window(qt_app, tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_NOTES_CONFIG", str(tmp_path / "config.json"))
    from meeting_notes.audio import devices as devices_mod
    from meeting_notes.client.ui.main_window import MainWindow

    def _raise(kind, requested=None, samplerate=None):
        raise devices_mod.DeviceNotFound("no device (simulated)")

    monkeypatch.setattr(devices_mod, "resolve_source", _raise)
    w = MainWindow()
    w._timer.stop()
    w._detect_timer.stop()
    w.entries = []
    w.processing_panel.set_compact(False)
    monkeypatch.setattr(w.controller, "queue_entries", lambda: w.entries)
    yield w
    w._teardown_done = True
    w.close()


def make_session(tmp_path, folder, name=None):
    d = tmp_path / folder
    d.mkdir()
    if name is not None:
        (d / "session.json").write_text(json.dumps({"name": name}))
    return str(d)


def entry(session_dir, **kw):
    base = {
        "id": session_dir.replace("\\", "/").rsplit("/", 1)[-1],
        "session_dir": session_dir,
        "status": "pending",
        "attempts": 0,
        "last_error": None,
        "upload_state": "pending",
        "upload_percent": 0.0,
        "transcription_state": "pending",
        "transcription_percent": 0.0,
        "finalized": False,
        "enqueued_at": time.time(),
        "next_attempt_at": None,
    }
    base.update(kw)
    return base


def refresh(window):
    window._refresh_processing()
    return window.processing_panel


def test_hidden_when_the_queue_is_empty(window):
    panel = refresh(window)
    assert panel.isHidden() and not panel.active
    # a finished meeting is not "not done"
    window.entries = [entry("x", upload_state="complete", transcription_state="complete")]
    assert refresh(window).isHidden()


def test_uploading_entry_shows_name_badge_bar_and_checklist(window, tmp_path):
    d = make_session(tmp_path, "2026-10-13_1", "Weekly sync")
    window.entries = [entry(d, upload_state="uploading", upload_percent=42.4)]
    panel = refresh(window)
    assert not panel.isHidden() and panel.active
    assert panel.summary_label.text() == "1 meeting"
    assert panel.badges[0].text() == "Uploading 42%"
    assert panel.rows[0].accessibleName() == "Weekly sync: uploading 42 percent"
    assert [b.percent for b in panel.bars] == [42]
    steps = [(n.text(), t.text(), dot.tone) for dot, n, t in panel.checklist_rows]
    assert steps == [
        ("Upload audio", "Uploading 42%", "active"),
        ("Transcribe recording", "Waiting", "idle"),
        ("Transcript ready", "Waiting", "idle"),
    ]
    assert panel.retry_buttons == []
    # the status line no longer repeats the numbers
    assert window._queue_note() == ""


def test_transcribing_and_queued_entries(window, tmp_path):
    a = make_session(tmp_path, "a", "Standup")
    b = make_session(tmp_path, "b", "Retro")
    window.entries = [
        entry(a, upload_state="complete", transcription_state="transcribing", transcription_percent=63, finalized=True),
        entry(b, upload_state="complete", transcription_state="queued", finalized=True, enqueued_at=time.time() + 1),
    ]
    panel = refresh(window)
    assert [x.text() for x in panel.badges] == ["Transcribing 63%", "Queued"]
    assert [bar.percent for bar in panel.bars] == [63]  # none for "Queued"
    steps = [(t.text(), dot.tone) for dot, _n, t in panel.checklist_rows]
    assert steps == [("Complete", "ok"), ("Transcribing 63%", "active"), ("Waiting", "idle")]


def test_failed_entry_shows_error_and_retry(window, tmp_path, monkeypatch):
    d = make_session(tmp_path, "f", "Client call")
    window.entries = [entry(d, status="failed", upload_state="error", attempts=5, last_error="ServerUnavailable: refused")]
    panel = refresh(window)
    assert panel.summary_label.text() == "1 meeting · 1 failed"
    assert panel.badges[0].text() == "Upload failed"
    assert panel.checklist_rows[0][2].text() == "Failed: server unreachable"
    assert len(panel.retry_buttons) == 1
    calls = []

    class Q:
        def retry_all_now(self):
            calls.append("retry")

    monkeypatch.setattr(window.controller, "session_queue", lambda: Q())
    monkeypatch.setattr(window.controller, "start_uploader", lambda force=False: calls.append("start"))
    panel.retry_buttons[0].click()
    assert calls == ["retry", "start"]


def test_retry_time_shown_after_a_failed_attempt():
    soon = time.time() + 600
    info = describe_entry(
        {"upload_state": "pending", "attempts": 1, "last_error": "boom", "next_attempt_at": soon},
        time.time(),
        lambda e: "server unreachable",
    )
    assert info["badge"].startswith("Retrying at ") and info["tone"] == "warn"
    waiting = describe_entry({"upload_state": "pending"}, time.time(), str)
    assert waiting["badge"] == "Waiting to upload" and waiting["percent"] is None


def test_more_than_three_entries_collapse(window, tmp_path):
    window.entries = [
        entry(make_session(tmp_path, f"m{i}", f"Meeting {i}"), enqueued_at=1000 + i) for i in range(5)
    ]
    panel = refresh(window)
    assert len(panel.rows) == 3
    assert panel.more_label is not None and panel.more_label.text() == "and 2 more"
    assert panel.summary_label.text() == "5 meetings"
    assert panel.rows[0].accessibleName().startswith("Meeting 0:")


def test_no_rebuild_when_nothing_changed(window, tmp_path):
    d = make_session(tmp_path, "r", "Same")
    window.entries = [entry(d, upload_state="uploading", upload_percent=10)]
    panel = refresh(window)
    built = panel.rebuilds
    for _ in range(50):
        window._refresh_processing()
    assert panel.rebuilds == built
    window.entries = [entry(d, upload_state="uploading", upload_percent=11)]
    window._refresh_processing()
    assert panel.rebuilds == built + 1
    window.entries = []
    window._refresh_processing()
    assert panel.isHidden() and panel.rebuilds == built + 2


def test_name_comes_from_session_json_and_is_cached(window, tmp_path):
    d = make_session(tmp_path, "2026-10-13_09-00", "Budget review")
    e = entry(d)
    panel = window.processing_panel
    assert panel.meeting_name(e) == "Budget review"
    (tmp_path / "2026-10-13_09-00" / "session.json").write_text(json.dumps({"name": "Changed"}))
    assert panel.meeting_name(e) == "Budget review"  # cached, not re-read
    plain = make_session(tmp_path, "2026-10-13_10-00")
    assert panel.meeting_name(entry(plain)) == "2026-10-13_10-00"  # folder name fallback


def test_status_line_keeps_legacy_text_when_the_panel_is_empty(window, monkeypatch):
    monkeypatch.setattr(window.controller, "queue_status", lambda: {"pending": 2, "failed": 0, "last_error": ""})
    monkeypatch.setattr(window.controller, "queue_awaiting_transcript", lambda: 0)
    monkeypatch.setattr(window.controller, "queue_progress", lambda: {})
    assert "2 uploads pending" in window._queue_note()


def test_checklist_folds_in_a_short_window_and_the_toggle_wins(window, tmp_path):
    d = make_session(tmp_path, "t", "Folded")
    window.entries = [entry(d, upload_state="uploading", upload_percent=5)]
    panel = refresh(window)
    assert panel.checklist_rows and panel.details_button.text() == "Hide details"
    panel.set_compact(True)
    assert not panel.checklist_rows and panel.details_button.text() == "Show details"
    assert panel.badges[0].text() == "Uploading 5%"  # the row itself stays
    panel.details_button.click()
    assert len(panel.checklist_rows) == 3
    panel.set_compact(False)
    panel.set_compact(True)
    assert len(panel.checklist_rows) == 3  # the person's choice sticks


def test_cleared_error_shows_live_transcription_state(window, tmp_path, monkeypatch):
    d = make_session(tmp_path, "live", "Weekly sync")
    window.entries = [
        entry(d, upload_state="complete", transcription_state="transcribing", transcription_percent=41, finalized=True,
              last_error=None)
    ]
    panel = refresh(window)
    assert panel.badges[0].text() == "Transcribing 41%"
    assert window._queue_note() == ""  # the panel owns the detail; no stale error on the status line
    monkeypatch.setattr(window.controller, "queue_status", lambda: {"pending": 1, "failed": 0, "last_error": ""})
    monkeypatch.setattr(window.controller, "queue_awaiting_transcript", lambda: 1)
    monkeypatch.setattr(window.controller, "queue_progress", lambda: window.entries[0])
    window.processing_panel.setVisible(False)
    assert "unreachable" not in window._queue_note()


def test_failed_transcription_still_shows_its_error(window, tmp_path):
    d = make_session(tmp_path, "bad", "Broken")
    window.entries = [
        entry(d, status="failed", upload_state="complete", transcription_state="error", finalized=True,
              last_error="TranscriptionFailed: Transcription failed: CUDA out of memory")
    ]
    panel = refresh(window)
    assert panel.badges[0].text() == "Needs attention"
    assert panel.retry_buttons
    assert "Failed:" in panel.checklist_rows[1][2].text()
