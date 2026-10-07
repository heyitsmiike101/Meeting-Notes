"""Auto record: start recording a detected call without asking, and how that recording ends.

Covers the settings (config + dialog), the "Recording ... call" card, the strip in the main window with
its Disable auto end button, and the three Auto end choices (on the hour, after 30 seconds of silence,
manual only). Time is faked: the wall clock via ``window._wall_now`` and the audio clock by passing
explicit ``now`` values.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("PySide6", reason="PySide6 not installed")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from meeting_notes import config as config_mod  # noqa: E402
from meeting_notes.client.controller import IDLE, RECORDING  # noqa: E402
from meeting_notes.client.meeting_detect import MeetingEnded, MeetingStarted  # noqa: E402
from meeting_notes.client.ui import main_window as mw  # noqa: E402
from meeting_notes.client.ui.meeting_prompt import (  # noqa: E402
    AutoRecordCard,
    CallEndingPrompt,
    MeetingPrompt,
    clock_text,
)
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


class Clock:
    """A settable wall clock (``window._wall_now``)."""

    def __init__(self, when):
        self.when = when

    def __call__(self):
        return self.when


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
    w._clock = Clock(datetime(2026, 10, 6, 14, 3, 0))  # 2:03 PM: auto end on the hour is 3:00 PM
    w._wall_now = w._clock

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
    w._clear_auto_end_state()
    w._close_prompt()
    w._teardown_done = True
    w.close()


def feed(window, *events):
    window._detector.queue.extend(events)
    window._poll_meeting()


def write_settings(tmp_path, window, **detection):
    (tmp_path / "config.json").write_text(json.dumps({"meeting_detection": detection}))
    window._apply_meeting_settings()


def auto_on(tmp_path, window, mode="hour", **extra):
    write_settings(tmp_path, window, auto_record=True, auto_end=mode, **extra)


def start_call(tmp_path, window, mode="hour", **extra):
    auto_on(tmp_path, window, mode, **extra)
    feed(window, STARTED)
    assert window.controller.state == RECORDING and window._auto_session
    return window


def _pump(condition, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        QApplication.processEvents()
        time.sleep(0.01)
    return condition()


def stopped(window):
    """Wait for the async stop to finish and the window to be back to idle."""
    return _pump(lambda: window.controller.state == IDLE) and _pump(lambda: window._record_state == "idle")


def at(window, hour, minute, second=0):
    window._clock.when = datetime(2026, 10, 6, hour, minute, second)


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------


def test_config_defaults_and_invalid_auto_end():
    defaults = config_mod.meeting_detection_settings({})
    assert defaults["auto_record"] is False
    assert defaults["auto_end"] == "hour"
    assert config_mod.AUTO_END_CHOICES == ("call", "hour", "silence", "manual")
    for good in ("call", "hour", "silence", "manual"):
        assert config_mod.meeting_detection_settings({"meeting_detection": {"auto_end": good}})["auto_end"] == good
    for bad in ("sometimes", "", None, 5, ["hour"]):
        assert config_mod.meeting_detection_settings({"meeting_detection": {"auto_end": bad}})["auto_end"] == "hour"
    assert config_mod.meeting_detection_settings({"meeting_detection": {"auto_record": True}})["auto_record"] is True
    assert config_mod.meeting_detection_settings({"meeting_detection": {"auto_record": "yes"}})["auto_record"] is False


# --------------------------------------------------------------------------
# next_hour_deadline
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "start, expected",
    [
        ((14, 3), (15, 0)),       # the normal case
        ((13, 57), (15, 0)),      # joined early for the 2:00 meeting: run it to 3:00
        ((13, 50), (14, 0)),      # exactly 10 minutes to the hour is enough
        ((13, 50, 1), (15, 0)),   # a second less is not
        ((14, 52), (16, 0)),
        ((14, 0, 0), (15, 0)),    # strictly after the start
        ((23, 58), (1, 0)),       # across midnight
    ],
)
def test_next_hour_deadline(start, expected):
    base = datetime(2026, 10, 6)
    got = mw.next_hour_deadline(base.replace(hour=start[0], minute=start[1], second=start[2] if len(start) > 2 else 0))
    want = base.replace(hour=expected[0], minute=expected[1])
    if expected[0] < start[0]:
        want += timedelta(days=1)
    assert got == want


def test_next_hour_deadline_keeps_tzinfo_and_min_gap():
    start = datetime(2026, 10, 6, 14, 3, tzinfo=timezone.utc)
    got = mw.next_hour_deadline(start)
    assert got == datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc) and got.tzinfo is timezone.utc
    assert mw.next_hour_deadline(datetime(2026, 10, 6, 14, 50), min_gap_sec=0) == datetime(2026, 10, 6, 15, 0)
    assert mw.next_hour_deadline(datetime(2026, 10, 6, 14, 59), min_gap_sec=120) == datetime(2026, 10, 6, 16, 0)


def test_clock_text_has_no_leading_zero():
    assert clock_text(datetime(2026, 1, 1, 15, 0)) == "3:00 PM"
    assert clock_text(datetime(2026, 1, 1, 9, 5)) == "9:05 AM"
    assert clock_text(datetime(2026, 1, 1, 0, 0)) == "12:00 AM"
    assert clock_text(datetime(2026, 1, 1, 12, 30)) == "12:30 PM"


# --------------------------------------------------------------------------
# the card
# --------------------------------------------------------------------------


def test_card_text_per_mode(qt_app):
    deadline = datetime(2026, 10, 6, 15, 0)
    hour = AutoRecordCard("Teams", "Weekly Sync", "hour", deadline)
    assert hour.title_label.text() == "Recording Teams call"
    assert hour.name_label.text() == "Weekly Sync"
    assert hour.detail_label.text() == "Stops at 3:00 PM"
    assert hour.disable_button is not None and hour.disable_button.text() == "Disable auto end"
    assert hour.ok_button.text() == "OK" and hour.ok_button.isDefault() and hour.ok_button.objectName() == "record"
    assert hour.testAttribute(Qt.WA_ShowWithoutActivating)
    hour.close_silently()

    silence = AutoRecordCard("Zoom", "Call", "silence")
    assert silence.detail_label.text() == "Stops after 30 seconds of silence"
    assert silence.disable_button is not None
    silence.close_silently()

    call = AutoRecordCard("Teams", "Sync", "call")
    assert call.detail_label.text() == "Stops when the call ends"
    assert call.disable_button is not None
    call.close_silently()

    manual = AutoRecordCard("Google Meet", "Standup", "manual")
    assert manual.detail_label.text() == "Stop it yourself when the meeting is over"
    assert manual.disable_button is None
    manual.close_silently()


def test_card_signals_fire_once(qt_app):
    card = AutoRecordCard("Teams", "x", "hour", datetime(2026, 10, 6, 15, 0))
    got = []
    card.disable_requested.connect(lambda: got.append("disable"))
    card.dismissed.connect(lambda: got.append("ok"))
    card.disable_button.click()
    card.ok_button.click()  # ignored: already finished
    assert got == ["disable"]

    other = AutoRecordCard("Teams", "x", "hour", datetime(2026, 10, 6, 15, 0))
    other.disable_requested.connect(lambda: got.append("disable2"))
    other.dismissed.connect(lambda: got.append("ok2"))
    other.ok_button.click()
    other.disable_button.click()
    assert got == ["disable", "ok2"]

    esc = AutoRecordCard("Teams", "x", "manual")
    esc.dismissed.connect(lambda: got.append("esc"))
    esc.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
    assert got[-1] == "esc"

    quiet = AutoRecordCard("Teams", "x", "hour", datetime(2026, 10, 6, 15, 0))
    quiet.disable_requested.connect(lambda: got.append("never"))
    quiet.dismissed.connect(lambda: got.append("never"))
    quiet.close_silently()
    assert "never" not in got


def test_card_dismisses_itself_after_the_timeout(qt_app):
    card = AutoRecordCard("Teams", "x", "hour", datetime(2026, 10, 6, 15, 0), timeout_ms=30)
    got = []
    card.dismissed.connect(lambda: got.append("ok"))
    assert _pump(lambda: got == ["ok"])


def test_call_ending_prompt_accepts_a_title(qt_app):
    assert CallEndingPrompt(5, autostart=False).title_label.text() == "Call seems to have ended"
    prompt = CallEndingPrompt(15, title="No audio for a while", autostart=False)
    assert prompt.title_label.text() == "No audio for a while"
    assert prompt.countdown_label.text() == "Stopping in 15 s unless you keep recording."


# --------------------------------------------------------------------------
# starting
# --------------------------------------------------------------------------


def test_auto_record_off_still_shows_the_prompt(window, tmp_path):
    write_settings(tmp_path, window, auto_record=False)
    feed(window, STARTED)
    assert isinstance(window._prompt, MeetingPrompt)
    assert window.controller.state == IDLE and not window._auto_session
    assert window._auto_record_card is None and window.auto_end_bar.isHidden()


def test_auto_record_starts_without_a_prompt(window, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="meeting_notes.client.ui")
    start_call(tmp_path, window, "hour")
    assert window._prompt is None
    assert window.name_edit.text() == "Weekly Sync"
    assert isinstance(window._auto_record_card, AutoRecordCard)
    assert window._auto_record_card.title_label.text() == "Recording Teams call"
    assert window._auto_end_mode == "hour"
    assert window._auto_end_deadline == datetime(2026, 10, 6, 15, 0)
    assert any(
        "auto-recording Teams call 'Weekly Sync' (auto end: hour)" in r.message for r in caplog.records
    )


def test_auto_record_needs_detection_on(window, tmp_path):
    write_settings(tmp_path, window, enabled=False, auto_record=True)
    feed(window, STARTED)
    assert window.controller.state == IDLE and window._prompt is None


def test_hour_mode_strip_and_card(window, tmp_path):
    start_call(tmp_path, window, "hour")
    assert not window.auto_end_bar.isHidden()
    assert window.auto_end_label.text() == "Auto end at 3:00 PM"
    assert window.disable_auto_end_button.text() == "Disable auto end"
    assert window.disable_auto_end_button.accessibleName() == "Disable auto end for this recording"
    assert window._auto_record_card.detail_label.text() == "Stops at 3:00 PM"
    assert window._auto_record_card.disable_button is not None


def test_silence_mode_strip_and_card(window, tmp_path):
    start_call(tmp_path, window, "silence")
    assert not window.auto_end_bar.isHidden()
    assert window.auto_end_label.text() == "Auto end after 30 seconds of silence"
    assert window._auto_record_card.detail_label.text() == "Stops after 30 seconds of silence"
    assert window._auto_end_deadline is None


def test_manual_mode_has_no_strip_and_no_disable_button(window, tmp_path):
    start_call(tmp_path, window, "manual")
    assert window.auto_end_bar.isHidden()
    assert window._auto_record_card.disable_button is None
    assert window._auto_record_card.detail_label.text() == "Stop it yourself when the meeting is over"
    # And nothing ever stops it: not the clock, not silence.
    at(window, 23, 30)
    window._audio_last_active = 0.0
    window._note_levels(1.0, {"mic": 0.5})
    window._check_auto_end(10_000.0)
    assert window._auto_end_prompt is None and window.controller.state == RECORDING


def test_strip_is_hidden_when_not_recording_and_at_construction(window, tmp_path):
    assert window.auto_end_bar.isHidden()
    start_call(tmp_path, window, "hour")
    window._toggle()  # the user stops by hand
    assert stopped(window)
    assert window.auto_end_bar.isHidden()
    assert window._auto_end_mode is None and window._auto_record_card is None and not window._auto_session


def test_failed_start_changes_nothing(window, tmp_path, monkeypatch):
    monkeypatch.setattr(window.controller, "start", lambda name="", sources=None: None)
    auto_on(tmp_path, window, "hour")
    feed(window, STARTED)
    assert window.controller.state == IDLE
    assert not window._auto_session and window._auto_end_mode is None
    assert window._auto_record_card is None and window.auto_end_bar.isHidden()


def test_mode_is_captured_at_the_start(window, tmp_path):
    start_call(tmp_path, window, "hour")
    write_settings(tmp_path, window, auto_record=True, auto_end="manual")
    assert window._auto_end_mode == "hour"
    write_settings(tmp_path, window, auto_record=False, enabled=False)  # turning detection off alters nothing
    assert window._auto_end_mode == "hour" and window.controller.state == RECORDING
    assert window._auto_record_card is not None


def test_a_manual_start_afterwards_is_not_auto_ended(window, tmp_path):
    start_call(tmp_path, window, "hour")
    window._toggle()
    assert stopped(window)
    window._toggle()  # manual start
    assert window.controller.state == RECORDING
    assert window._auto_end_mode is None and not window._auto_session
    at(window, 15, 30)
    window._check_auto_end(time.monotonic())
    assert window._auto_end_prompt is None


# --------------------------------------------------------------------------
# on the hour
# --------------------------------------------------------------------------


def test_hour_countdown_starts_one_minute_before(window, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="meeting_notes.client.ui")
    start_call(tmp_path, window, "hour")
    at(window, 14, 30)
    window._check_auto_end(time.monotonic())
    at(window, 14, 58, 59)  # 61 s left
    window._check_auto_end(time.monotonic())
    assert window._auto_end_prompt is None

    at(window, 14, 59, 0)  # 60 s left
    window._check_auto_end(time.monotonic())
    prompt = window._auto_end_prompt
    assert isinstance(prompt, CallEndingPrompt)
    assert prompt.title_label.text() == "Meeting time is up"
    assert prompt.remaining == 60
    window._check_auto_end(time.monotonic())
    assert window._auto_end_prompt is prompt  # one countdown, not one per tick
    assert window.controller.state == RECORDING


def test_hour_countdown_expiring_stops_and_queues(window, tmp_path):
    start_call(tmp_path, window, "hour")
    at(window, 14, 59, 30)
    window._check_auto_end(time.monotonic())
    prompt = window._auto_end_prompt
    assert prompt.remaining == 30
    prompt.remaining = 1
    prompt.tick()  # one more second: expired
    assert window._auto_end_prompt is None
    assert _pump(lambda: window.controller.state == IDLE)
    assert _pump(lambda: window._record_state == "idle")
    assert "Meeting hour is up" in window.status_label.text()
    assert window.auto_end_bar.isHidden() and window._auto_end_mode is None
    assert window._auto_record_card is None


def test_hour_countdown_stop_now_stops(window, tmp_path):
    start_call(tmp_path, window, "hour")
    at(window, 14, 59, 30)
    window._check_auto_end(time.monotonic())
    window._auto_end_prompt.stop_button.click()
    assert _pump(lambda: window.controller.state == IDLE)


def test_hour_keep_recording_does_not_come_back(window, tmp_path):
    start_call(tmp_path, window, "hour")
    at(window, 14, 59, 10)
    window._check_auto_end(time.monotonic())
    window._auto_end_prompt.keep_button.click()
    assert window._auto_end_prompt is None
    assert window._auto_end_mode == "manual"
    assert window.auto_end_bar.isHidden()
    at(window, 15, 30)
    window._check_auto_end(time.monotonic())
    assert window._auto_end_prompt is None and window.controller.state == RECORDING


def test_hour_countdown_is_at_least_ten_seconds(window, tmp_path):
    start_call(tmp_path, window, "hour")
    at(window, 15, 5)  # the window was frozen past the deadline
    window._check_auto_end(time.monotonic())
    assert window._auto_end_prompt.remaining == 10


def test_no_other_suggestion_while_the_countdown_shows(window, tmp_path):
    start_call(tmp_path, window, "hour")
    at(window, 14, 59, 30)
    window._check_auto_end(time.monotonic())
    assert window._auto_end_prompt is not None
    window._audio_last_active = 0.0
    window._check_silence(10_000.0)
    assert window._suggest_prompt is None
    window._system_last_active = time.monotonic() - 1000
    window._end_pending = True
    window._check_end_suggestion(time.monotonic())
    assert window._suggest_prompt is None


def test_countdown_replaces_a_suggestion_already_showing(window, tmp_path):
    start_call(tmp_path, window, "hour")
    window._audio_last_active = 0.0
    window._check_silence(10_000.0)
    assert window._suggest_prompt is not None
    at(window, 14, 59, 30)
    window._check_auto_end(time.monotonic())
    assert window._auto_end_prompt is not None and window._suggest_prompt is None


# --------------------------------------------------------------------------
# Disable auto end
# --------------------------------------------------------------------------


@pytest.mark.parametrize("via", ["strip", "card"])
def test_disable_auto_end(window, tmp_path, caplog, via):
    caplog.set_level(logging.INFO, logger="meeting_notes.client.ui")
    start_call(tmp_path, window, "hour")
    card = window._auto_record_card
    if via == "strip":
        window.disable_auto_end_button.click()
    else:
        card.disable_button.click()
    assert window._auto_end_mode == "manual"
    assert window.auto_end_bar.isHidden()
    assert window._auto_record_card is None
    assert card._finished
    assert window._toast.text() == "Auto end off for this recording"
    assert any("auto end disabled for this recording" in r.message for r in caplog.records)
    at(window, 15, 30)
    window._check_auto_end(time.monotonic())
    assert window._auto_end_prompt is None and window.controller.state == RECORDING


def test_disable_closes_a_countdown_that_is_showing(window, tmp_path):
    start_call(tmp_path, window, "hour")
    at(window, 14, 59, 30)
    window._check_auto_end(time.monotonic())
    prompt = window._auto_end_prompt
    assert prompt is not None
    window.disable_auto_end_button.click()
    assert window._auto_end_prompt is None and prompt._finished
    at(window, 15, 1)
    window._check_auto_end(time.monotonic())
    assert window._auto_end_prompt is None and window.controller.state == RECORDING


def test_disable_in_silence_mode(window, tmp_path):
    start_call(tmp_path, window, "silence")
    window._note_levels(1000.0, {"mic": 0.5})
    window._check_auto_end(1020.0)
    assert window._auto_end_prompt is not None
    window._auto_record_card.disable_button.click()
    assert window._auto_end_prompt is None and window._auto_end_mode == "manual"
    window._check_auto_end(5000.0)
    assert window._auto_end_prompt is None and window.controller.state == RECORDING


# --------------------------------------------------------------------------
# after 30 seconds of silence
# --------------------------------------------------------------------------


def test_silence_never_ends_a_call_nobody_has_spoken_in(window, tmp_path):
    start_call(tmp_path, window, "silence")
    window._audio_last_active = 0.0
    window._note_levels(5.0, {"mic": 0.0, "system": 0.0})
    for t in (20.0, 60.0, 600.0):
        window._check_auto_end(t)
    assert window._auto_end_prompt is None and window.controller.state == RECORDING


def test_muted_track_does_not_count_as_heard(window, tmp_path):
    start_call(tmp_path, window, "silence")
    window.controller.source_muted = lambda track: True
    window._note_levels(100.0, {"mic": 0.9, "system": 0.9})
    assert window._audio_last_active == 100.0  # proves nothing, but never counts as quiet...
    assert not window._auto_end_heard         # ...nor as sound
    window._check_auto_end(10_000.0)
    assert window._auto_end_prompt is None


def test_a_quiet_track_below_the_threshold_is_not_heard(window, tmp_path):
    start_call(tmp_path, window, "silence")
    window._note_levels(100.0, {"mic": mw.SYSTEM_SILENCE_PEAK / 2, "system": 0.0})
    assert not window._auto_end_heard
    window._note_levels(101.0, {"mic": 0.0, "system": mw.SYSTEM_SILENCE_PEAK})
    assert window._auto_end_heard


def test_silence_countdown_after_sound_then_fifteen_quiet_seconds(window, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="meeting_notes.client.ui")
    start_call(tmp_path, window, "silence")
    t0 = 1000.0
    window._note_levels(t0, {"mic": 0.3, "system": 0.0})
    window._check_auto_end(t0 + 14.9)
    assert window._auto_end_prompt is None
    window._check_auto_end(t0 + 15)
    prompt = window._auto_end_prompt
    assert isinstance(prompt, CallEndingPrompt)
    assert prompt.title_label.text() == "No audio for a while"
    assert prompt.remaining == mw.AUTO_END_SILENCE_WARN_SEC == 15
    window._check_auto_end(t0 + 16)
    assert window._auto_end_prompt is prompt

    # Audio comes back: the countdown goes away without stopping anything.
    window._note_levels(t0 + 17, {"system": 0.4})
    window._check_auto_end(t0 + 18)
    assert window._auto_end_prompt is None and prompt._finished
    assert window.controller.state == RECORDING
    assert any("audio resumed; cancelling auto end countdown" in r.message for r in caplog.records)


def test_silence_countdown_expiring_stops(window, tmp_path):
    start_call(tmp_path, window, "silence")
    window._note_levels(1000.0, {"mic": 0.3})
    window._check_auto_end(1015.0)
    prompt = window._auto_end_prompt
    prompt.remaining = 1
    prompt.tick()
    assert _pump(lambda: window.controller.state == IDLE)
    assert _pump(lambda: window._record_state == "idle")
    assert "No audio for 30 seconds" in window.status_label.text()


def test_silence_keep_then_resume_then_quiet_again_counts_down_again(window, tmp_path):
    start_call(tmp_path, window, "silence")
    t0 = 1000.0
    window._note_levels(t0, {"mic": 0.3})
    window._check_auto_end(t0 + 15)
    window._auto_end_prompt.keep_button.click()
    assert window._auto_end_prompt is None and not window._auto_end_armed
    window._check_auto_end(t0 + 40)  # still quiet: not again
    window._check_auto_end(t0 + 400)
    assert window._auto_end_prompt is None

    t1 = t0 + 500
    window._note_levels(t1, {"system": 0.3})
    window._check_auto_end(t1 + 1)
    assert window._auto_end_armed
    window._check_auto_end(t1 + 15)
    assert window._auto_end_prompt is not None
    assert window.controller.state == RECORDING


def test_silence_mode_ignores_the_clock(window, tmp_path):
    start_call(tmp_path, window, "silence")
    at(window, 15, 30)
    window._note_levels(time.monotonic(), {"mic": 0.3})
    window._check_auto_end(time.monotonic())
    assert window._auto_end_prompt is None


# --------------------------------------------------------------------------
# existing logic
# --------------------------------------------------------------------------


def test_call_end_auto_stop_does_not_apply_to_auto_recorded_calls(window, tmp_path):
    start_call(tmp_path, window, "hour", auto_stop=True)
    window._system_last_active = time.monotonic() - 1000
    feed(window, ENDED)
    assert window._end_prompt is None
    assert not window._auto_stop_eligible()
    window._check_end_pending(time.monotonic())
    assert window._end_prompt is None and window.controller.state == RECORDING
    # The non-forcing suggestion still works.
    assert window._suggest_prompt is not None


def test_prompted_recordings_keep_their_call_end_countdown(window, tmp_path):
    write_settings(tmp_path, window, auto_record=False, auto_stop=True)
    feed(window, STARTED)
    window._prompt.record_button.click()
    assert window._auto_session and window._auto_end_mode is None
    assert window.auto_end_bar.isHidden()
    window._system_last_active = time.monotonic() - 1000
    feed(window, ENDED)
    assert isinstance(window._end_prompt, CallEndingPrompt)


def test_manual_stop_of_an_auto_recorded_call_cleans_up(window, tmp_path):
    start_call(tmp_path, window, "silence")
    window._note_levels(1000.0, {"mic": 0.3})
    window._check_auto_end(1015.0)
    prompt, card = window._auto_end_prompt, window._auto_record_card
    assert prompt is not None and card is not None
    window._toggle()
    assert prompt._finished and card._finished
    assert _pump(lambda: window.controller.state == IDLE)
    assert _pump(lambda: window._record_state == "idle")
    assert window._auto_end_prompt is None and window._auto_record_card is None
    assert window._auto_end_mode is None and window.auto_end_bar.isHidden()


def test_a_second_call_after_the_first_ended_starts_clean(window, tmp_path):
    start_call(tmp_path, window, "hour")
    window.disable_auto_end_button.click()
    window._toggle()
    assert stopped(window)
    write_settings(tmp_path, window, auto_record=True, auto_end="silence")
    feed(window, STARTED)
    assert window.controller.state == RECORDING
    assert window._auto_end_mode == "silence" and not window._auto_end_heard and window._auto_end_armed
    assert not window.auto_end_bar.isHidden()


def test_check_auto_end_never_raises(window, tmp_path):
    start_call(tmp_path, window, "hour")

    def boom():
        raise RuntimeError("clock broke")

    window._wall_now = boom
    window._check_auto_end(time.monotonic())  # must not raise
    window._tick()  # nor the timer slot
    assert window.controller.state == RECORDING


def test_closing_the_window_closes_the_cards(window, tmp_path):
    start_call(tmp_path, window, "hour")
    at(window, 14, 59, 30)
    window._check_auto_end(time.monotonic())
    prompt, card = window._auto_end_prompt, window._auto_record_card
    window._clear_auto_end_state()
    assert prompt._finished and card._finished
    assert window._auto_end_prompt is None and window._auto_record_card is None


# --------------------------------------------------------------------------
# settings dialog
# --------------------------------------------------------------------------


def _dialog(tmp_path, monkeypatch, meeting_detection=None):
    config_path = tmp_path / "config.json"
    data = {"save_dir": str(tmp_path / "R")}
    if meeting_detection is not None:
        data["meeting_detection"] = meeting_detection
    config_path.write_text(json.dumps(data))
    monkeypatch.setenv("MEETING_NOTES_CONFIG", str(config_path))
    from meeting_notes.client.ui.settings_dialog import SettingsDialog

    return SettingsDialog(), config_path


def _auto_end_row_hidden(dialog):
    label = dialog._meeting_form.labelForField(dialog.auto_end_combo)
    assert dialog.auto_end_combo.isHidden() == dialog.auto_end_note.isHidden() == label.isHidden()
    return dialog.auto_end_combo.isHidden()


def test_settings_auto_record_defaults_and_layout(qt_app, tmp_path, monkeypatch):
    dialog, _ = _dialog(tmp_path, monkeypatch)
    assert dialog.auto_record_check.text() == "Start recording automatically when a call starts"
    assert not dialog.auto_record_check.isChecked()
    assert dialog.auto_record_check.isEnabled()
    assert dialog.auto_end_combo.accessibleName() == "Auto end"
    assert [dialog.auto_end_combo.itemText(i) for i in range(4)] == [
        "When the call ends", "On the hour", "After 30 seconds of silence", "Manual only",
    ]
    assert [dialog.auto_end_combo.itemData(i) for i in range(4)] == ["call", "hour", "silence", "manual"]
    assert dialog.auto_end_combo.currentData() == "hour"
    assert _auto_end_row_hidden(dialog)  # only while auto record is on
    assert not dialog.auto_stop_check.isHidden()
    # Order: detect, auto record, auto end, then the call-end checkbox, then the suggestion checkbox.
    form = dialog._meeting_form
    rows = [form.getWidgetPosition(w)[0] for w in (
        dialog.detect_check, dialog.auto_record_check, dialog.auto_end_combo, dialog.auto_stop_check,
        dialog.suggest_stop_check)]
    assert rows == sorted(rows) and len(set(rows)) == 5
    dialog.close()


def test_auto_end_row_shows_with_auto_record_and_auto_stop_hides(qt_app, tmp_path, monkeypatch):
    dialog, _ = _dialog(tmp_path, monkeypatch)
    dialog.auto_record_check.setChecked(True)
    assert not _auto_end_row_hidden(dialog)
    assert dialog.auto_stop_check.isHidden()
    dialog.auto_record_check.setChecked(False)
    assert _auto_end_row_hidden(dialog)
    assert not dialog.auto_stop_check.isHidden()
    dialog.close()


def test_auto_record_follows_the_detection_checkbox(qt_app, tmp_path, monkeypatch):
    dialog, _ = _dialog(tmp_path, monkeypatch, {"auto_record": True, "auto_end": "silence"})
    assert dialog.auto_record_check.isChecked() and dialog.auto_record_check.isEnabled()
    assert dialog.auto_end_combo.currentData() == "silence"
    assert not _auto_end_row_hidden(dialog)

    dialog.detect_check.setChecked(False)
    assert not dialog.auto_record_check.isEnabled()
    assert _auto_end_row_hidden(dialog)           # no auto record means no auto end choice
    assert not dialog.auto_stop_check.isHidden()  # ...and prompts are not showing either way
    dialog.detect_check.setChecked(True)
    assert dialog.auto_record_check.isEnabled()
    assert not _auto_end_row_hidden(dialog)
    assert dialog.auto_stop_check.isHidden()
    dialog.close()


def test_settings_save_auto_record_and_auto_end(qt_app, tmp_path, monkeypatch):
    dialog, config_path = _dialog(tmp_path, monkeypatch, {"end_grace_sec": 45, "auto_stop": False})
    dialog.auto_record_check.setChecked(True)
    dialog.auto_end_combo.setCurrentIndex(dialog.auto_end_combo.findData("manual"))
    dialog.accept()
    saved = json.loads(config_path.read_text())["meeting_detection"]
    assert saved["auto_record"] is True and saved["auto_end"] == "manual"
    assert saved["end_grace_sec"] == 45
    assert saved["auto_stop"] is False  # hidden while auto record is on, but still saved
    assert saved["enabled"] is True and saved["suggest_stop"] is True


def test_settings_save_defaults(qt_app, tmp_path, monkeypatch):
    dialog, config_path = _dialog(tmp_path, monkeypatch)
    dialog.accept()
    saved = json.loads(config_path.read_text())["meeting_detection"]
    assert saved["auto_record"] is False and saved["auto_end"] == "hour"


# --------------------------------------------------------------------------
# Auto end "When the call ends"
# --------------------------------------------------------------------------


def test_call_mode_strip_and_card(window, tmp_path):
    start_call(tmp_path, window, "call")
    assert not window.auto_end_bar.isHidden()
    assert window.auto_end_label.text() == "Auto end when the call ends"
    assert window._auto_record_card.detail_label.text() == "Stops when the call ends"
    assert window._auto_end_deadline is None
    assert window._auto_stop_eligible()  # regardless of the auto_stop checkbox (default off for auto record)


def test_call_mode_stops_when_the_call_ends_even_with_auto_stop_off(window, tmp_path):
    start_call(tmp_path, window, "call", auto_stop=False)
    window._system_last_active = time.monotonic() - 1000
    feed(window, ENDED)
    prompt = window._end_prompt
    assert isinstance(prompt, CallEndingPrompt)
    prompt.stop_button.click()
    assert stopped(window)
    assert window.auto_end_bar.isHidden() and window._auto_end_mode is None


def test_call_mode_waits_for_the_system_audio_to_go_quiet(window, tmp_path):
    start_call(tmp_path, window, "call")
    window._system_last_active = time.monotonic()  # still talking
    feed(window, ENDED)
    assert window._end_pending and window._end_prompt is None and window.controller.state == RECORDING


def test_call_mode_keep_recording_turns_the_auto_end_off(window, tmp_path):
    start_call(tmp_path, window, "call")
    window._system_last_active = time.monotonic() - 1000
    feed(window, ENDED)
    window._end_prompt.keep_button.click()
    assert window._end_prompt is None and not window._end_pending
    assert window._auto_end_mode == "manual" and window.auto_end_bar.isHidden()
    assert not window._auto_stop_eligible()
    window._check_end_pending(time.monotonic())
    assert window._end_prompt is None and window.controller.state == RECORDING


def test_disable_auto_end_closes_a_showing_call_end_countdown(window, tmp_path):
    start_call(tmp_path, window, "call")
    window._system_last_active = time.monotonic() - 1000
    feed(window, ENDED)
    prompt = window._end_prompt
    assert prompt is not None
    window.disable_auto_end_button.click()
    assert window._end_prompt is None and prompt._finished and not window._end_pending
    assert window._auto_end_mode == "manual" and not window._auto_stop_eligible()
    assert window.controller.state == RECORDING


def test_hour_and_prompted_behaviour_unchanged_by_call_mode(window, tmp_path):
    start_call(tmp_path, window, "hour", auto_stop=True)
    assert not window._auto_stop_eligible()
