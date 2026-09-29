"""Main-window meeting-detection flow, driven by a fake detector."""

from __future__ import annotations

import json
import time

import pytest

pytest.importorskip("PySide6", reason="PySide6 not installed")

from PySide6.QtWidgets import QApplication  # noqa: E402

from meeting_notes.client.controller import IDLE, RECORDING  # noqa: E402
from meeting_notes.client.meeting_detect import MeetingEnded, MeetingStarted  # noqa: E402
from meeting_notes.client.ui.meeting_prompt import MeetingPrompt  # noqa: E402
from meeting_notes.client.ui.theme import APP_STYLE  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(APP_STYLE)
    return app


class FakeDetector:
    def __init__(self):
        self.queue = []
        self.end_grace_sec = 20.0

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
    feed(window, ENDED)
    assert _pump(lambda: window.controller.state == IDLE and window.record_button.text() == "Start recording")
    assert window.status_label.text().startswith("Call ended — recording stopped and queued.")
    assert not window._auto_session


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
    (tmp_path / "config.json").write_text(json.dumps({"meeting_detection": {"end_grace_sec": 60}}))
    window._apply_meeting_settings()
    assert window._detector.end_grace_sec == 60.0


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
    assert saved["meeting_detection"] == {"enabled": False, "auto_stop": False, "end_grace_sec": 45}
