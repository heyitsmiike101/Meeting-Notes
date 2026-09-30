"""Main-window meeting-detection flow, driven by a fake detector."""

from __future__ import annotations

import json
import time

import pytest

pytest.importorskip("PySide6", reason="PySide6 not installed")

from PySide6.QtWidgets import QApplication  # noqa: E402

from meeting_notes.client.controller import IDLE, RECORDING  # noqa: E402
from meeting_notes.client.meeting_detect import MeetingEnded, MeetingStarted  # noqa: E402
from meeting_notes.client.ui.meeting_prompt import CallEndingPrompt, MeetingPrompt  # noqa: E402
from meeting_notes.client.ui.theme import APP_STYLE  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(APP_STYLE)
    return app


class FakeDetector:
    def __init__(self):
        self.queue = []
        self.end_grace_sec = 60.0

    def poll(self, now):
        events, self.queue = self.queue, []
        return events


def _pump(condition, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        QApplication.processEvents()
        time.sleep(0.01)
    return condition()


STARTED = MeetingStarted("teams", "Teams", "Weekly Sync")
ENDED = MeetingEnded("teams", "Teams")


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
    w._detector = FakeDetector()
    started_names = []

    def fake_start(name=""):
        started_names.append(name)
        w.controller.state = RECORDING
        return tmp_path

    def fake_stop():
        w.controller.state = IDLE
        return {"duration_sec": 3.0}

    monkeypatch.setattr(w.controller, "start", fake_start)
    monkeypatch.setattr(w.controller, "stop", fake_stop)
    w.started_names = started_names
    yield w
    w._close_prompt()
    w._teardown_done = True
    w.close()


def call_over(window, silent_for=1000.0):
    """The system-audio track has been quiet for ``silent_for`` seconds."""
    window._system_last_active = time.monotonic() - silent_for


def run_countdown(window):
    prompt = window._end_prompt
    assert prompt is not None
    for _ in range(prompt.remaining):
        prompt.tick()


def feed(window, *events):
    window._detector.queue.extend(events)
    window._poll_meeting()


def test_prompt_widget_signals_and_contents(qt_app):
    prompt = MeetingPrompt("Zoom", "Zoom call 2:30 PM")
    got = []
    prompt.record_requested.connect(lambda n: got.append(("record", n)))
    prompt.dismissed.connect(lambda: got.append(("dismissed", None)))
    assert prompt.title_label.text() == "Zoom call detected"
    assert prompt.name_edit.text() == "Zoom call 2:30 PM"
    assert prompt.record_button.text() == "Record" and prompt.record_button.isDefault()
    assert prompt.later_button.text() == "Not now"
    prompt.name_edit.setText("  Custom  ")
    prompt.record_button.click()
    prompt.later_button.click()  # ignored: already finished
    assert got == [("record", "Custom")]

    other = MeetingPrompt("Zoom", "x", timeout_ms=10)
    got.clear()
    other.dismissed.connect(lambda: got.append("dismissed"))
    other.show_prompt()
    assert _pump(lambda: got == ["dismissed"])


def test_record_starts_with_name_and_call_end_auto_stops(window):
    feed(window, STARTED)
    assert window._prompt is not None
    window._prompt.name_edit.setText("Board sync")
    window._prompt.record_button.click()
    assert window.started_names == ["Board sync"]
    assert window.name_edit.text() == "Board sync"
    assert window.controller.state == RECORDING and window._auto_session
    call_over(window)
    feed(window, ENDED)
    # Banner with a countdown first; recording still running.
    assert isinstance(window._end_prompt, CallEndingPrompt)
    assert window.controller.state == RECORDING
    run_countdown(window)
    assert _pump(lambda: window.controller.state == IDLE and window.record_button.text() == "Start recording")
    assert window.status_label.text().startswith("Call ended — recording stopped and queued.")
    assert not window._auto_session and window._end_prompt is None


def _start_prompted(window):
    feed(window, STARTED)
    window._prompt.record_button.click()
    assert window._auto_session and window.controller.state == RECORDING


def test_system_audio_still_playing_blocks_auto_stop(window):
    _start_prompted(window)
    # Detector says the call ended, but system audio played 5 s ago.
    window._system_last_active = time.monotonic() - 5
    feed(window, ENDED)
    assert window._end_prompt is None and window._end_pending
    assert window.controller.state == RECORDING
    window._poll_meeting()
    assert window._end_prompt is None
    # Once the audio has been quiet for the grace period the banner appears.
    call_over(window, 61)
    window._poll_meeting()
    assert window._end_prompt is not None
    assert window.controller.state == RECORDING  # still not stopped


def test_levels_feed_the_silence_clock(window):
    _start_prompted(window)
    call_over(window)
    window._note_system_level(time.monotonic(), 0.001)  # below threshold: still silent
    assert time.monotonic() - window._system_last_active > 900
    window._note_system_level(time.monotonic(), 0.2)  # audible
    assert time.monotonic() - window._system_last_active < 1


def test_muted_system_track_never_counts_as_silence(window, monkeypatch):
    _start_prompted(window)
    call_over(window)
    monkeypatch.setattr(window.controller, "source_muted", lambda track: True)
    window._note_system_level(time.monotonic(), 0.0)
    assert time.monotonic() - window._system_last_active < 1


def test_keep_recording_cancels_auto_stop_for_this_recording(window):
    _start_prompted(window)
    call_over(window)
    feed(window, ENDED)
    window._end_prompt.keep_button.click()
    assert window._end_prompt is None and window._auto_stop_kept and not window._end_pending
    feed(window, ENDED)
    window._poll_meeting()
    assert window._end_prompt is None
    assert window.controller.state == RECORDING
    # A later recording is a fresh decision.
    window._toggle()
    assert _pump(lambda: window.controller.state == IDLE)
    window._toggle()
    assert not window._auto_stop_kept


def test_stop_now_stops_immediately(window):
    _start_prompted(window)
    call_over(window)
    feed(window, ENDED)
    window._end_prompt.stop_button.click()
    assert _pump(lambda: window.controller.state == IDLE)


def test_countdown_only_stops_when_it_finishes(window):
    _start_prompted(window)
    call_over(window)
    feed(window, ENDED)
    prompt = window._end_prompt
    assert prompt.remaining == 60
    for _ in range(59):
        prompt.tick()
    QApplication.processEvents()
    assert window.controller.state == RECORDING
    prompt.tick()
    assert _pump(lambda: window.controller.state == IDLE)


def test_system_audio_resuming_cancels_the_countdown(window):
    _start_prompted(window)
    call_over(window)
    feed(window, ENDED)
    assert window._end_prompt is not None
    window._note_system_level(time.monotonic(), 0.3)
    window._poll_meeting()
    assert window._end_prompt is None and window._end_pending
    assert window.controller.state == RECORDING


def test_manual_stop_clears_pending_end_state(window):
    _start_prompted(window)
    call_over(window)
    feed(window, ENDED)
    window._toggle()
    assert _pump(lambda: window.controller.state == IDLE)
    assert window._end_prompt is None and not window._end_pending


def test_manual_recording_is_not_auto_stopped(window):
    window._toggle()  # manual start
    assert window.controller.state == RECORDING and not window._auto_session
    feed(window, STARTED)
    assert window._prompt is None  # no prompt while recording
    feed(window, ENDED)
    QApplication.processEvents()
    assert window.controller.state == RECORDING


def test_manual_stop_of_prompted_recording_clears_auto_flag(window):
    feed(window, STARTED)
    window._prompt.record_button.click()
    assert window._auto_session
    window._toggle()  # user stops it by hand mid-call
    assert _pump(lambda: window.controller.state == IDLE)
    assert not window._auto_session
    window.started_names.clear()
    window._toggle()  # user restarts manually
    assert window.controller.state == RECORDING and not window._auto_session
    feed(window, ENDED)
    assert window.controller.state == RECORDING


def test_not_now_dismisses_and_call_end_closes_prompt(window):
    feed(window, STARTED)
    window._prompt.later_button.click()
    assert window._prompt is None
    assert window.started_names == []
    # A new call after the previous one ended prompts again.
    feed(window, ENDED)
    feed(window, STARTED)
    assert window._prompt is not None
    feed(window, ENDED)
    assert window._prompt is None


def test_one_prompt_at_a_time(window):
    feed(window, STARTED)
    first = window._prompt
    feed(window, MeetingStarted("zoom", "Zoom", "Zoom call"))
    assert window._prompt is first


def test_disabled_setting_means_no_prompt(window, tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"meeting_detection": {"enabled": False}}))
    window._apply_meeting_settings()
    feed(window, STARTED)
    assert window._prompt is None


def test_auto_stop_disabled_keeps_recording(window, tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"meeting_detection": {"auto_stop": False}}))
    window._apply_meeting_settings()
    feed(window, STARTED)
    window._prompt.record_button.click()
    feed(window, ENDED)
    QApplication.processEvents()
    assert window.controller.state == RECORDING


def test_settings_change_updates_detector_grace(window, tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"meeting_detection": {"end_grace_sec": 90}}))
    window._apply_meeting_settings()
    assert window._detector.end_grace_sec == 90.0


def test_detector_exceptions_never_escape_the_timer(window):
    class Boom:
        def poll(self, now):
            raise RuntimeError("probe blew up")

    window._detector = Boom()
    window._poll_meeting()  # must not raise


def test_settings_dialog_saves_detection_without_clobbering(qt_app, tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "save_dir": str(tmp_path / "R"), "other": 1,
        "meeting_detection": {"enabled": True, "auto_stop": True, "end_grace_sec": 45},
    }))
    monkeypatch.setenv("MEETING_NOTES_CONFIG", str(config_path))
    from meeting_notes.client.ui.settings_dialog import SettingsDialog

    dialog = SettingsDialog()
    assert dialog.detect_check.isChecked() and dialog.auto_stop_check.isChecked()
    dialog.detect_check.setChecked(False)
    dialog.auto_stop_check.setChecked(False)
    dialog.url_edit.setText("http://h:1")
    dialog.accept()
    saved = json.loads(config_path.read_text())
    assert saved["other"] == 1
    assert saved["server"]["url"] == "http://h:1"
    assert saved["meeting_detection"] == {"enabled": False, "auto_stop": False, "suggest_stop": True, "end_grace_sec": 45}


def test_status_note_shows_uploaded_transcribing_queued(window, monkeypatch):
    monkeypatch.setattr(window.controller, "queue_status", lambda: {"pending": 1, "failed": 0, "last_error": ""})
    monkeypatch.setattr(window.controller, "queue_awaiting_transcript", lambda: 1)
    monkeypatch.setattr(window.controller, "queue_progress", lambda: {
        "upload_state": "complete", "upload_percent": 100.0,
        "transcription_state": "queued", "transcription_percent": 0.0})
    note = window._queue_note()
    assert "Uploaded · transcribing (queued)" in note
    assert "upload pending" not in note and "failed" not in note
