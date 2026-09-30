"""The "meeting seems over -- stop recording?" suggestion.

For every recording (manual ones included), never automatic: the only thing that
stops a recording here is the person pressing Stop recording.
"""

from __future__ import annotations

import json
import logging
import time

import pytest

pytest.importorskip("PySide6", reason="PySide6 not installed")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from meeting_notes.client.controller import IDLE, RECORDING  # noqa: E402
from meeting_notes.client.meeting_detect import MeetingEnded, MeetingStarted  # noqa: E402
from meeting_notes.client.ui import main_window as mw  # noqa: E402
from meeting_notes.client.ui.meeting_prompt import CallEndingPrompt, StopSuggestionPrompt  # noqa: E402
from meeting_notes.client.ui.theme import APP_STYLE  # noqa: E402

STARTED = MeetingStarted("teams", "Teams", "Weekly Sync")
ENDED = MeetingEnded("teams", "Teams")


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

    def fake_start(name="", sources=None):
        w.controller.state = RECORDING
        return tmp_path

    def fake_stop():
        w.controller.state = IDLE
        return {"duration_sec": 3.0}

    monkeypatch.setattr(w.controller, "start", fake_start)
    monkeypatch.setattr(w.controller, "stop", fake_stop)
    yield w
    w._reset_end_state()
    w._close_prompt()
    w._teardown_done = True
    w.close()


def feed(window, *events):
    window._detector.queue.extend(events)
    window._poll_meeting()


def call_over(window, silent_for=1000.0):
    window._system_last_active = time.monotonic() - silent_for


def write_settings(tmp_path, window, **detection):
    (tmp_path / "config.json").write_text(json.dumps({"meeting_detection": detection}))
    window._apply_meeting_settings()


def manual_recording(window):
    window._toggle()
    assert window.controller.state == RECORDING and not window._auto_session


# --------------------------------------------------------------------------
# the card itself
# --------------------------------------------------------------------------


def test_card_contents_and_outcomes(qt_app):
    prompt = StopSuggestionPrompt("Meeting seems to have ended", "Stop recording?")
    got = []
    prompt.stop_requested.connect(lambda: got.append("stop"))
    prompt.keep_requested.connect(lambda: got.append("keep"))
    assert prompt.title_label.text() == "Meeting seems to have ended"
    assert prompt.stop_button.text() == "Stop recording" and prompt.stop_button.isDefault()
    assert prompt.keep_button.text() == "Keep recording"
    prompt.stop_button.click()
    prompt.keep_button.click()  # ignored: already finished
    assert got == ["stop"]

    other = StopSuggestionPrompt()
    other.keep_requested.connect(lambda: got.append("keep"))
    other.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
    assert got == ["stop", "keep"]

    silent = StopSuggestionPrompt()
    silent.stop_requested.connect(lambda: got.append("stop2"))
    silent.keep_requested.connect(lambda: got.append("keep2"))
    silent.close_silently()
    assert got == ["stop", "keep"]  # no outcome signalled


# --------------------------------------------------------------------------
# call-ended evidence, manual recordings
# --------------------------------------------------------------------------


def test_manual_recording_gets_a_suggestion_and_is_never_stopped_by_it(window, caplog):
    caplog.set_level(logging.INFO, logger="meeting_notes.client.ui")
    manual_recording(window)
    call_over(window)
    feed(window, ENDED)
    assert isinstance(window._suggest_prompt, StopSuggestionPrompt)
    assert window._suggest_prompt.title_label.text() == "Meeting seems to have ended"
    assert window._end_prompt is None  # not the countdown
    # However long we wait, nothing stops it.
    for extra in (60, 600, 3600, 86400):
        window._check_end_pending(time.monotonic() + extra)
        window._check_silence(time.monotonic() + extra)
    assert window.controller.state == RECORDING
    assert window._suggest_prompt is not None
    assert any("stop suggestion shown (call-end)" in r.message for r in caplog.records)


def test_no_suggestion_while_system_audio_is_still_playing(window):
    manual_recording(window)
    window._system_last_active = time.monotonic()
    feed(window, ENDED)
    assert window._suggest_prompt is None and window._end_pending
    call_over(window)
    window._poll_meeting()
    assert window._suggest_prompt is not None


def test_stop_recording_button_stops(window, caplog):
    caplog.set_level(logging.INFO, logger="meeting_notes.client.ui")
    manual_recording(window)
    call_over(window)
    feed(window, ENDED)
    window._suggest_prompt.stop_button.click()
    assert window._suggest_prompt is None
    assert _pump(lambda: window.controller.state == IDLE)
    assert any("user chose Stop recording" in r.message for r in caplog.records)


def test_keep_recording_suppresses_this_call_but_a_new_call_re_arms(window, caplog):
    caplog.set_level(logging.INFO, logger="meeting_notes.client.ui")
    manual_recording(window)
    call_over(window)
    feed(window, ENDED)
    window._suggest_prompt.keep_button.click()
    assert window._suggest_prompt is None and window._suggest_kept
    assert any("user chose Keep recording" in r.message for r in caplog.records)
    feed(window, ENDED)  # same call, more end evidence
    assert window._suggest_prompt is None and not window._end_pending
    assert window.controller.state == RECORDING

    feed(window, STARTED)  # a new call in the same recording
    assert not window._suggest_kept
    call_over(window)
    feed(window, ENDED)
    assert window._suggest_prompt is not None


def test_audio_resuming_dismisses_a_call_end_suggestion(window, caplog):
    caplog.set_level(logging.INFO, logger="meeting_notes.client.ui")
    manual_recording(window)
    call_over(window)
    feed(window, ENDED)
    assert window._suggest_prompt is not None
    window._note_levels(time.monotonic(), {"system": 0.3})
    window._poll_meeting()
    assert window._suggest_prompt is None
    assert window.controller.state == RECORDING
    assert any("dismissed automatically" in r.message for r in caplog.records)


def test_a_call_coming_back_dismisses_the_suggestion(window):
    manual_recording(window)
    call_over(window)
    feed(window, ENDED)
    assert window._suggest_prompt is not None
    feed(window, STARTED)
    assert window._suggest_prompt is None
    assert window.controller.state == RECORDING


def test_setting_off_means_no_suggestion(window, tmp_path):
    write_settings(tmp_path, window, suggest_stop=False)
    manual_recording(window)
    call_over(window)
    feed(window, ENDED)
    assert window._suggest_prompt is None and not window._end_pending
    window._audio_last_active = time.monotonic() - 10_000
    window._check_silence(time.monotonic())
    assert window._suggest_prompt is None


def test_prompt_started_recording_keeps_its_countdown(window):
    feed(window, STARTED)
    window._prompt.record_button.click()
    assert window._auto_session
    call_over(window)
    feed(window, ENDED)
    assert isinstance(window._end_prompt, CallEndingPrompt)
    assert window._suggest_prompt is None


def test_prompt_started_with_auto_stop_off_still_gets_a_suggestion(window, tmp_path):
    write_settings(tmp_path, window, auto_stop=False)
    feed(window, STARTED)
    window._prompt.record_button.click()
    call_over(window)
    feed(window, ENDED)
    assert window._end_prompt is None
    assert window._suggest_prompt is not None
    assert window.controller.state == RECORDING


def test_starting_and_stopping_clear_the_suggestion(window):
    manual_recording(window)
    call_over(window)
    feed(window, ENDED)
    assert window._suggest_prompt is not None
    window._toggle()  # user stops by hand
    assert _pump(lambda: window.controller.state == IDLE)
    assert window._suggest_prompt is None


# --------------------------------------------------------------------------
# silence fallback (fake clock)
# --------------------------------------------------------------------------


def test_silence_for_five_minutes_suggests_and_rearms_after_audio(window, caplog):
    caplog.set_level(logging.INFO, logger="meeting_notes.client.ui")
    manual_recording(window)
    t0 = 10_000.0
    window._audio_last_active = t0
    window._check_silence(t0 + mw.SILENCE_SUGGEST_SEC - 1)
    assert window._suggest_prompt is None

    window._check_silence(t0 + mw.SILENCE_SUGGEST_SEC + 1)
    prompt = window._suggest_prompt
    assert prompt is not None and prompt.title_label.text() == "No audio for 5 minutes"
    assert window.controller.state == RECORDING

    # Once per silence: not again while it lasts, even after Keep.
    prompt.keep_button.click()
    assert window._suggest_prompt is None
    window._check_silence(t0 + 2 * mw.SILENCE_SUGGEST_SEC)
    window._check_silence(t0 + 10 * mw.SILENCE_SUGGEST_SEC)
    assert window._suggest_prompt is None

    # Audio resumes, then goes quiet again: it can come back.
    t1 = t0 + 11 * mw.SILENCE_SUGGEST_SEC
    window._note_levels(t1, {"mic": 0.2, "system": 0.0})
    window._check_silence(t1 + 1)
    window._check_silence(t1 + mw.SILENCE_SUGGEST_SEC + 2)
    assert window._suggest_prompt is not None
    assert sum("stop suggestion shown (silence)" in r.message for r in caplog.records) == 2


def test_silence_suggestion_is_dismissed_when_audio_resumes(window):
    manual_recording(window)
    t0 = 5_000.0
    window._audio_last_active = t0
    window._check_silence(t0 + mw.SILENCE_SUGGEST_SEC + 1)
    assert window._suggest_prompt is not None
    window._note_levels(t0 + mw.SILENCE_SUGGEST_SEC + 5, {"system": 0.4})
    window._check_silence(t0 + mw.SILENCE_SUGGEST_SEC + 6)
    assert window._suggest_prompt is None
    assert window.controller.state == RECORDING


def test_quiet_mic_and_system_below_threshold_count_as_silence_but_muted_does_not(window):
    manual_recording(window)
    t0 = 1_000.0
    window._audio_last_active = t0
    window._note_levels(t0 + 400, {"mic": 0.001, "system": 0.002})  # under the threshold
    assert window._audio_last_active == t0
    window.controller.source_muted = lambda track: track == "mic"
    window._note_levels(t0 + 500, {"mic": 0.0, "system": 0.0})  # muted proves nothing
    assert window._audio_last_active == t0 + 500


def test_silence_never_stops_by_itself(window):
    manual_recording(window)
    window._audio_last_active = 0.0
    for t in (400, 4000, 40_000):
        window._check_silence(float(t))
    assert window.controller.state == RECORDING


def test_no_second_card_while_the_countdown_is_showing(window):
    feed(window, STARTED)
    window._prompt.record_button.click()
    call_over(window)
    feed(window, ENDED)
    assert window._end_prompt is not None
    window._audio_last_active = 0.0
    window._check_silence(10_000.0)
    assert window._suggest_prompt is None


def test_settings_dialog_has_the_suggest_stop_checkbox(qt_app, tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"save_dir": str(tmp_path / "R")}))
    monkeypatch.setenv("MEETING_NOTES_CONFIG", str(config_path))
    from meeting_notes.client.ui.settings_dialog import SettingsDialog

    dialog = SettingsDialog()
    assert dialog.suggest_stop_check.text() == "Suggest stopping when a meeting seems over"
    assert dialog.suggest_stop_check.isChecked()  # default on
    dialog.suggest_stop_check.setChecked(False)
    dialog.accept()
    assert json.loads(config_path.read_text())["meeting_detection"]["suggest_stop"] is False


def _pump(condition, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        QApplication.processEvents()
        time.sleep(0.01)
    return condition()
