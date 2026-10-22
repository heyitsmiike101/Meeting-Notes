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
    auto_end_detail,
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
        self.name = None  # what the meeting's window calls it right now (current_name)

    def current_name(self):
        return self.name

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
    # The older tests exercise the single Also-end rule; the close rule is switched on explicitly (auto_stop=True).
    extra.setdefault("auto_stop", False)
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
    assert config_mod.AUTO_END_CHOICES == ("call", "bye", "hour", "silence", "manual")
    for good in ("bye", "hour", "silence", "manual"):
        assert config_mod.meeting_detection_settings({"meeting_detection": {"auto_end": good}})["auto_end"] == good
    # The old "When the call ends" loads as Nothing else with the close rule on (whatever auto_stop was).
    for stop in (True, False):
        migrated = config_mod.meeting_detection_settings({"meeting_detection": {"auto_end": "call", "auto_stop": stop}})
        assert migrated["auto_end"] == "manual" and migrated["auto_stop"] is True
    assert config_mod.meeting_detection_settings({"meeting_detection": {"auto_end": "call"}})["auto_stop"] is True
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

    close = AutoRecordCard("Teams", "Sync", "manual", end_on_close=True)
    assert close.detail_label.text() == "Stops when the meeting closes"
    assert close.disable_button is not None
    close.close_silently()

    both = AutoRecordCard("Teams", "Sync", "hour", deadline, end_on_close=True)
    assert both.detail_label.text() == "Stops when the meeting closes or on the hour (3:00 PM)"
    both.close_silently()
    assert auto_end_detail("silence", end_on_close=True) == "Stops when the meeting closes or after 30 seconds of silence"
    assert auto_end_detail("bye", end_on_close=True) == (
        "Stops when the meeting closes or after goodbyes and 20 seconds of silence"
    )
    assert auto_end_detail("call") == "Stops when the meeting closes"

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
        "auto-recording Teams call 'Weekly Sync' (also end: hour, stop on close: False)" in r.message for r in caplog.records
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


def test_close_rule_off_only_suggests_for_auto_recorded_calls(window, tmp_path):
    start_call(tmp_path, window, "hour", auto_stop=False)
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
    assert dialog.auto_end_combo.accessibleName() == "Also end"
    assert [dialog.auto_end_combo.itemText(i) for i in range(4)] == [
        "Nothing else", "On the hour", "After 30 seconds of silence", "When people say goodbye",
    ]
    assert [dialog.auto_end_combo.itemData(i) for i in range(4)] == ["manual", "hour", "silence", "bye"]
    assert dialog.auto_end_combo.count() == 4 and dialog.auto_end_combo.findData("call") == -1
    assert dialog.auto_stop_check.text() == "Stop when the meeting closes" and dialog.auto_stop_check.isChecked()
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


def test_auto_end_row_shows_with_auto_record_and_stop_on_close_stays(qt_app, tmp_path, monkeypatch):
    dialog, _ = _dialog(tmp_path, monkeypatch)
    dialog.auto_record_check.setChecked(True)
    assert not _auto_end_row_hidden(dialog)
    assert not dialog.auto_stop_check.isHidden()  # the close rule applies to prompted and auto-recorded calls
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
    assert not dialog.auto_stop_check.isHidden()
    dialog.close()


def test_settings_save_auto_record_and_auto_end(qt_app, tmp_path, monkeypatch):
    dialog, config_path = _dialog(tmp_path, monkeypatch, {"end_grace_sec": 45, "auto_stop": False})
    dialog.auto_record_check.setChecked(True)
    dialog.auto_end_combo.setCurrentIndex(dialog.auto_end_combo.findData("manual"))
    dialog.accept()
    saved = json.loads(config_path.read_text())["meeting_detection"]
    assert saved["auto_record"] is True and saved["auto_end"] == "manual"
    assert saved["end_grace_sec"] == 45
    assert saved["auto_stop"] is False
    assert saved["enabled"] is True and saved["suggest_stop"] is True


def test_settings_saved_call_choice_migrates_and_saves_migrated(qt_app, tmp_path, monkeypatch):
    dialog, config_path = _dialog(tmp_path, monkeypatch, {"auto_record": True, "auto_end": "call", "auto_stop": False})
    assert dialog.auto_end_combo.currentData() == "manual" and dialog.auto_stop_check.isChecked()
    dialog.accept()
    saved = json.loads(config_path.read_text())["meeting_detection"]
    assert saved["auto_end"] == "manual" and saved["auto_stop"] is True


def test_settings_save_defaults(qt_app, tmp_path, monkeypatch):
    dialog, config_path = _dialog(tmp_path, monkeypatch)
    dialog.accept()
    saved = json.loads(config_path.read_text())["meeting_detection"]
    assert saved["auto_record"] is False and saved["auto_end"] == "hour"


# --------------------------------------------------------------------------
# Stop when the meeting closes (auto_stop) on an auto-recorded call
# --------------------------------------------------------------------------


def close_the_call(window):
    window._system_last_active = time.monotonic() - 1000
    feed(window, ENDED)


@pytest.mark.parametrize("mode", ["manual", "hour", "silence", "bye"])
def test_auto_recorded_call_stops_on_close_in_every_also_end_mode(window, tmp_path, mode):
    start_call(tmp_path, window, mode, auto_stop=True)
    assert window._end_on_close and window._auto_stop_eligible()
    close_the_call(window)
    prompt = window._end_prompt
    assert isinstance(prompt, CallEndingPrompt)
    assert window._suggest_prompt is None
    prompt.remaining = 1
    prompt.tick()  # the countdown ran out: an automatic stop
    assert stopped(window)
    assert window.auto_end_bar.isHidden() and window._auto_end_mode is None
    assert window._ended_notice is not None  # the "stopped automatically" notice shows
    window._ended_notice.close_silently()


@pytest.mark.parametrize("mode", ["manual", "hour", "silence", "bye"])
def test_close_rule_off_only_suggests_in_every_also_end_mode(window, tmp_path, mode):
    start_call(tmp_path, window, mode, auto_stop=False)
    assert not window._end_on_close and not window._auto_stop_eligible()
    close_the_call(window)
    assert window._end_prompt is None and window.controller.state == RECORDING
    assert window._suggest_prompt is not None


def test_old_call_mode_that_bypassed_the_normaliser_still_means_stop_on_close(window, tmp_path):
    start_call(tmp_path, window, "call", auto_stop=False)  # normalised on load to manual + auto_stop
    assert window._auto_end_mode == "manual" and window._end_on_close
    window._reset_end_state()
    window._clear_auto_end_state()
    window._auto_session = False
    window.controller.state = IDLE
    window._detect_settings["auto_end"] = "call"  # a raw "call" that bypassed the normaliser
    window._detect_settings["auto_stop"] = False
    window._auto_record("Teams", "Sync")
    assert window._auto_end_mode == "manual" and window._end_on_close


def test_close_rule_strip_and_card_texts(window, tmp_path):
    start_call(tmp_path, window, "manual", auto_stop=True)
    assert not window.auto_end_bar.isHidden()
    assert window.auto_end_label.text() == "Auto end when the meeting closes"
    assert window._auto_record_card.detail_label.text() == "Stops when the meeting closes"
    assert window._auto_record_card.disable_button is not None
    assert window._auto_end_deadline is None


@pytest.mark.parametrize("mode, strip, card", [
    ("hour", "Auto end when the meeting closes or on the hour (3:00 PM)",
     "Stops when the meeting closes or on the hour (3:00 PM)"),
    ("silence", "Auto end when the meeting closes or after 30 s of silence",
     "Stops when the meeting closes or after 30 seconds of silence"),
    ("bye", "Auto end when the meeting closes or after goodbyes and 20 s of silence",
     "Stops when the meeting closes or after goodbyes and 20 seconds of silence"),
])
def test_combined_strip_and_card_texts(window, tmp_path, mode, strip, card):
    start_call(tmp_path, window, mode, auto_stop=True)
    assert window.auto_end_label.text() == strip
    assert window._auto_record_card.detail_label.text() == card


def test_manual_without_the_close_rule_has_no_strip_or_button(window, tmp_path):
    start_call(tmp_path, window, "manual", auto_stop=False)
    assert window.auto_end_bar.isHidden()
    assert window._auto_record_card.disable_button is None


def test_remote_state_reports_end_on_close(window, tmp_path):
    from meeting_notes import remote

    start_call(tmp_path, window, "manual", auto_stop=True)
    state = window.build_remote_state()
    assert state["auto_end"]["mode"] == "call" and state["auto_end"]["end_on_close"] is True
    assert state["auto_end"]["label"] == "Auto end when the meeting closes"
    assert remote.sanitize_state(state)["auto_end"]["end_on_close"] is True
    window._clear_auto_end_state()
    start_call_again = window._auto_record
    window._auto_session = False
    window.controller.state = IDLE
    start_call_again("Teams", "Sync")
    window._detect_settings["auto_end"] = "hour"
    window._end_on_close = True
    window._auto_end_mode = "hour"
    window._auto_end_deadline = datetime(2026, 10, 6, 15, 0)
    state = window.build_remote_state()
    assert state["auto_end"]["mode"] == "hour" and state["auto_end"]["end_on_close"] is True
    # An older recorder's state has no end_on_close: additive.
    old = {"auto_end": {"mode": "hour", "label": "Auto end at 3:00 PM"}}
    assert remote.sanitize_state(old)["auto_end"]["end_on_close"] is False


def test_close_waits_for_the_system_audio_to_go_quiet(window, tmp_path):
    start_call(tmp_path, window, "hour", auto_stop=True)
    window._system_last_active = time.monotonic()  # still talking
    feed(window, ENDED)
    assert window._end_pending and window._end_prompt is None and window.controller.state == RECORDING


def test_disable_auto_end_blocks_both_rules(window, tmp_path):
    start_call(tmp_path, window, "hour", auto_stop=True)
    close_the_call(window)
    prompt = window._end_prompt
    assert prompt is not None
    window.disable_auto_end_button.click()
    assert window._end_prompt is None and prompt._finished and not window._end_pending
    assert window._auto_end_mode == "manual" and not window._end_on_close
    assert not window._auto_stop_eligible() and window.auto_end_bar.isHidden()
    feed(window, ENDED)  # a later close does not stop it either
    assert window._end_prompt is None
    at(window, 14, 59, 30)  # ...nor does the hour
    window._check_auto_end(time.monotonic())
    assert window._auto_end_prompt is None and window.controller.state == RECORDING


def test_keep_recording_on_the_close_countdown_keeps_the_hour_rule(window, tmp_path):
    start_call(tmp_path, window, "hour", auto_stop=True)
    close_the_call(window)
    window._end_prompt.keep_button.click()
    assert window._end_prompt is None and not window._end_pending
    assert not window._end_on_close and not window._auto_stop_eligible()
    assert window._auto_end_mode == "hour"
    assert window.auto_end_label.text() == "Auto end at 3:00 PM" and not window.auto_end_bar.isHidden()
    window._check_end_pending(time.monotonic())
    assert window._end_prompt is None and window.controller.state == RECORDING
    at(window, 14, 59, 30)
    window._check_auto_end(time.monotonic())
    assert isinstance(window._auto_end_prompt, CallEndingPrompt)


def test_keep_recording_on_close_with_nothing_else_hides_the_strip(window, tmp_path):
    start_call(tmp_path, window, "manual", auto_stop=True)
    close_the_call(window)
    window._end_prompt.keep_button.click()
    assert window.auto_end_bar.isHidden() and window.controller.state == RECORDING


def test_two_countdowns_never_show_together(window, tmp_path):
    start_call(tmp_path, window, "hour", auto_stop=True)
    at(window, 14, 59, 30)
    window._check_auto_end(time.monotonic())
    hour_prompt = window._auto_end_prompt
    assert hour_prompt is not None
    close_the_call(window)
    assert window._end_prompt is None  # the hour countdown is already asking
    window._check_end_pending(time.monotonic())
    assert window._end_prompt is None and window._auto_end_prompt is hour_prompt
    hour_prompt.keep_button.click()  # keeping the hour: the close rule picks up
    window._check_end_pending(time.monotonic())
    assert isinstance(window._end_prompt, CallEndingPrompt) and window._auto_end_prompt is None


def test_a_close_countdown_holds_the_also_end_rule_back(window, tmp_path):
    start_call(tmp_path, window, "silence", auto_stop=True)
    close_the_call(window)
    assert window._end_prompt is not None
    window._note_levels(1000.0, {"mic": 0.3})
    window._check_auto_end(1015.0)
    assert window._auto_end_prompt is None


def test_prompted_recordings_still_follow_auto_stop(window, tmp_path):
    write_settings(tmp_path, window, auto_record=False, auto_stop=False)
    feed(window, STARTED)
    window._prompt.record_button.click()
    assert window._auto_end_mode is None and not window._auto_stop_eligible()


# --------------------------------------------------------------------------
# when people say goodbye
# --------------------------------------------------------------------------


def bye(window, text="ok bye everyone", track="system", now=1000.0):
    window._note_partial({"track": track, "text": text, "type": "partial"}, now)


def test_bye_mode_strip_card_and_remote_state(window, tmp_path):
    from meeting_notes import remote

    start_call(tmp_path, window, "bye")
    assert not window.auto_end_bar.isHidden()
    assert window.auto_end_label.text() == "Auto end after goodbyes and 20 s of silence"
    assert window._auto_record_card.detail_label.text() == "Stops after goodbyes and 20 seconds of silence"
    assert window._auto_record_card.disable_button is not None
    assert window._auto_end_deadline is None and window._bye_heard_at is None
    assert "bye" in remote.AUTO_END_MODES


def test_bye_constants():
    assert mw.AUTO_END_BYE_SILENCE_SEC == 20 and mw.AUTO_END_BYE_WARN_SEC == 10
    assert mw.AUTO_END_BYE_EXPIRY_SEC == 300


def test_bye_without_a_goodbye_never_ends(window, tmp_path):
    start_call(tmp_path, window, "bye")
    window._note_levels(1000.0, {"mic": 0.3})
    for t in (1010.0, 1020.0, 1600.0, 90_000.0):
        window._check_auto_end(t)
    assert window._auto_end_prompt is None and window.controller.state == RECORDING
    bye(window, "that is all for the budget review")  # not a farewell
    assert window._bye_heard_at is None


def test_bye_then_ten_quiet_seconds_shows_the_countdown(window, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="meeting_notes.client.ui")
    start_call(tmp_path, window, "bye")
    window._note_levels(1000.0, {"mic": 0.3})
    bye(window, "alright, see you next week! This is private chatter.", now=1000.5)
    window._check_auto_end(1009.9)
    assert window._auto_end_prompt is None
    window._check_auto_end(1010.0)
    prompt = window._auto_end_prompt
    assert isinstance(prompt, CallEndingPrompt)
    assert prompt.title_label.text() == "Meeting seems to be over"
    assert prompt.remaining == mw.AUTO_END_BYE_WARN_SEC == 10
    window._check_auto_end(1012.0)
    assert window._auto_end_prompt is prompt
    # The log names the matched phrase, never the transcript.
    heard = [r.message for r in caplog.records if "goodbye heard (" in r.message]
    assert heard and "see you next week" in heard[0] and "private chatter" not in heard[0]


def test_bye_countdown_expiring_stops_with_the_note(window, tmp_path):
    start_call(tmp_path, window, "bye")
    window._note_levels(1000.0, {"system": 0.3})
    bye(window, "bye")
    window._check_auto_end(1010.0)
    prompt = window._auto_end_prompt
    prompt.remaining = 1
    prompt.tick()
    assert _pump(lambda: window.controller.state == IDLE)
    assert _pump(lambda: window._record_state == "idle")
    assert "Goodbyes said and 20 seconds of silence" in window.status_label.text()
    assert "recording stopped and queued" in window.status_label.text()


def test_bye_audio_resuming_closes_the_countdown_and_later_silence_ends_it(window, tmp_path):
    start_call(tmp_path, window, "bye")
    window._note_levels(1000.0, {"mic": 0.3})
    bye(window, "goodbye", track="mic")
    window._check_auto_end(1010.0)
    first = window._auto_end_prompt
    assert first is not None
    window._note_levels(1012.0, {"mic": 0.4})   # somebody keeps chatting
    window._check_auto_end(1013.0)
    assert window._auto_end_prompt is None and first._finished
    assert window._bye_heard_at is not None      # the goodbye stays armed
    assert window.controller.state == RECORDING
    window._check_auto_end(1022.0)               # 10 s since the chatter
    assert window._auto_end_prompt is not None and window._auto_end_prompt is not first


def test_bye_expires_after_five_minutes_without_the_silence_and_a_new_one_rearms(window, tmp_path):
    start_call(tmp_path, window, "bye")
    bye(window, "bye", now=1000.0)
    window._note_levels(1299.0, {"mic": 0.3})
    window._check_auto_end(1299.5)               # still chatting just inside the window
    assert window._bye_heard_at == 1000.0
    window._note_levels(1301.0, {"mic": 0.3})
    window._check_auto_end(1301.5)               # chatter past the 5 minutes: forgotten
    assert window._bye_heard_at is None
    window._check_auto_end(1400.0)               # long quiet now does nothing
    assert window._auto_end_prompt is None
    bye(window, "see you later", now=1450.0)     # a new goodbye arms it again
    window._check_auto_end(1460.0)
    assert window._auto_end_prompt is not None


def test_bye_keep_recording_clears_the_goodbye(window, tmp_path):
    start_call(tmp_path, window, "bye")
    window._note_levels(1000.0, {"mic": 0.3})
    bye(window, "bye now")
    window._check_auto_end(1010.0)
    window._auto_end_prompt.keep_button.click()
    assert window._auto_end_prompt is None and window._bye_heard_at is None
    assert window._auto_end_mode == "bye"        # still on: a new goodbye can end it
    window._check_auto_end(1100.0)
    assert window._auto_end_prompt is None and window.controller.state == RECORDING
    bye(window, "take care", now=1100.0)
    window._check_auto_end(1110.0)
    assert window._auto_end_prompt is not None


@pytest.mark.parametrize("track", ["mic", "system"])
def test_bye_from_either_track_counts(window, tmp_path, track):
    start_call(tmp_path, window, "bye")
    bye(window, "have a good one", track=track)
    assert window._bye_heard_at is not None


def test_bye_is_ignored_in_other_modes_and_outside_auto_sessions(window, tmp_path):
    start_call(tmp_path, window, "silence")
    bye(window, "goodbye")
    assert window._bye_heard_at is None
    window.disable_auto_end_button.click()
    window._toggle()
    assert stopped(window)
    start_call(tmp_path, window, "bye")
    window._auto_session = False
    bye(window, "goodbye")
    assert window._bye_heard_at is None


def test_disable_auto_end_in_bye_mode(window, tmp_path):
    start_call(tmp_path, window, "bye")
    window._note_levels(1000.0, {"mic": 0.3})
    bye(window, "bye")
    window._check_auto_end(1010.0)
    assert window._auto_end_prompt is not None
    window.disable_auto_end_button.click()
    assert window._auto_end_mode == "manual" and window._auto_end_prompt is None and window._bye_heard_at is None
    assert window.auto_end_bar.isHidden()
    window._check_auto_end(5000.0)
    assert window._auto_end_prompt is None and window.controller.state == RECORDING


def test_bye_state_is_forgotten_with_the_recording(window, tmp_path):
    start_call(tmp_path, window, "bye")
    bye(window, "bye")
    window._clear_auto_end_state()
    assert window._bye_heard_at is None


def test_bye_partials_flow_in_from_the_live_preview(window, tmp_path):
    start_call(tmp_path, window, "bye")
    items = [{"track": "system", "text": "so that wraps it up", "type": "partial"}]
    window.controller.partials = lambda: list(items)
    window._drain_partials()
    assert window._bye_heard_at is None
    items.append({"track": "mic", "text": "Thanks everyone, bye!", "type": "partial"})
    window._drain_partials()
    assert window._bye_heard_at is not None


def test_check_auto_end_bye_never_raises(window, tmp_path):
    start_call(tmp_path, window, "bye")
    window._bye_heard_at = "oops"  # a broken value must not escape into the Qt loop
    window._check_auto_end(time.monotonic())
    assert window.controller.state == RECORDING


def test_settings_bye_choice_and_live_preview_note(qt_app, tmp_path, monkeypatch):
    dialog, config_path = _dialog(tmp_path, monkeypatch, {"auto_record": True, "auto_end": "bye"})
    assert dialog.auto_end_combo.currentData() == "bye"
    assert dialog.auto_end_combo.currentText() == "When people say goodbye"
    assert not dialog.auto_end_bye_note.isHidden()
    assert "live preview" in dialog.auto_end_bye_note.text()
    assert "needs the live preview" not in dialog.auto_end_bye_note.text()
    dialog.live_check.setChecked(False)
    assert "needs the live preview" in dialog.auto_end_bye_note.text()
    dialog.auto_end_combo.setCurrentIndex(dialog.auto_end_combo.findData("hour"))
    assert dialog.auto_end_bye_note.isHidden()
    dialog.auto_end_combo.setCurrentIndex(dialog.auto_end_combo.findData("bye"))
    dialog.accept()
    assert json.loads(config_path.read_text())["meeting_detection"]["auto_end"] == "bye"


# --------------------------------------------------------------------------
# being told: flashing taskbar and a notice that stays
# --------------------------------------------------------------------------


def _alerts(monkeypatch):
    calls = []
    monkeypatch.setattr(QApplication, "alert", staticmethod(lambda widget, msec=0: calls.append(msec)))
    return calls


def test_the_countdown_flashes_the_taskbar_until_the_window_is_used(window, tmp_path, monkeypatch):
    calls = _alerts(monkeypatch)
    start_call(tmp_path, window, "hour")
    calls.clear()  # the auto-record card flashes too; only the countdown matters here
    at(window, 14, 59, 30)
    window._check_auto_end(time.monotonic())
    assert window._auto_end_prompt is not None
    assert calls == [0]  # 0 = keep flashing until the window is activated


def test_an_auto_end_leaves_a_notice_and_flashes(window, tmp_path, monkeypatch):
    from meeting_notes.client.ui.meeting_prompt import AutoEndedNotice

    calls = _alerts(monkeypatch)
    start_call(tmp_path, window, "hour")
    at(window, 14, 59, 30)
    window._check_auto_end(time.monotonic())
    calls.clear()
    window._auto_end_prompt.remaining = 1
    window._auto_end_prompt.tick()  # expired: the recording stops
    assert stopped(window)
    notice = window._ended_notice
    try:
        assert isinstance(notice, AutoEndedNotice) and notice.isVisible()
        assert notice.title_label.text() == "Recording stopped automatically"
        assert "Meeting hour is up" in notice.detail_label.text()
        assert notice.when_label.text().startswith("Stopped at ")
        assert calls == [0]
        notice.dismiss_button.click()
        assert window._ended_notice is None and not notice.isVisible()
    finally:
        if window._ended_notice is not None:
            window._ended_notice.close_silently()


def test_a_second_auto_end_replaces_the_first_notice(window, monkeypatch):
    _alerts(monkeypatch)
    window._show_ended_notice("first")
    first = window._ended_notice
    window._show_ended_notice("second")
    try:
        assert window._ended_notice is not first and not first.isVisible()
        assert window._ended_notice.detail_label.text() == "second"
    finally:
        window._ended_notice.close_silently()


def test_open_on_the_notice_brings_the_window_forward(window, tmp_path, monkeypatch):
    _alerts(monkeypatch)
    raised = []
    monkeypatch.setattr(window, "activateWindow", lambda: raised.append(1))
    window._show_ended_notice("No audio for 30 seconds — recording stopped and queued.")
    notice = window._ended_notice
    notice.open_button.click()
    assert raised == [1] and window._ended_notice is None


def test_a_manual_stop_shows_no_notice(window, tmp_path, monkeypatch):
    _alerts(monkeypatch)
    start_call(tmp_path, window, "hour")
    window._stop()
    assert stopped(window)
    assert window._ended_notice is None


def test_prompts_open_on_the_screen_under_the_mouse(qt_app, monkeypatch):
    from meeting_notes.client.ui import meeting_prompt

    class Screen:
        def __init__(self, x):
            from PySide6.QtCore import QRect

            self._r = QRect(x, 0, 1000, 800)

        def availableGeometry(self):
            return self._r

    second = Screen(3000)
    monkeypatch.setattr(meeting_prompt.QGuiApplication, "screenAt", staticmethod(lambda pos: second))
    notice = meeting_prompt.AutoEndedNotice("test")
    try:
        meeting_prompt.place_bottom_right(notice)
        assert notice.x() >= 3000  # on the screen with the mouse, not the primary one
    finally:
        notice.close_silently()
    monkeypatch.setattr(meeting_prompt.QGuiApplication, "screenAt", staticmethod(lambda pos: None))
    assert meeting_prompt.prompt_screen() is QApplication.primaryScreen()


def test_a_call_that_moves_windows_cancels_the_stop_countdown(window, tmp_path):
    """From a real log: Teams' "Meeting join" window closed 90 s in (detected as the call ending, nobody had
    spoken yet), and the same meeting carried on in another window a few seconds later. The countdown that
    started must not stop the recording."""
    start_call(tmp_path, window, "manual", auto_stop=True)
    close_the_call(window)
    prompt = window._end_prompt
    assert isinstance(prompt, CallEndingPrompt)
    feed(window, MeetingStarted("browser", "Browser", "Browser call 10:01 AM"))
    assert window._end_prompt is None and prompt._finished and not prompt.isVisible()
    assert window.controller.state == RECORDING and not window._end_pending
    # ...and the real end of the meeting later still stops it.
    close_the_call(window)
    assert isinstance(window._end_prompt, CallEndingPrompt)
    window._end_prompt.stop_button.click()
    assert stopped(window)
    if window._ended_notice is not None:
        window._ended_notice.close_silently()


def test_stop_now_on_the_close_countdown_is_not_an_automatic_stop(window, tmp_path, monkeypatch):
    """From a real log: the person pressed Stop now and got "Recording stopped automatically"."""
    alerts = []
    monkeypatch.setattr(QApplication, "alert", staticmethod(lambda w, msec=0: alerts.append(msec)))
    start_call(tmp_path, window, "manual", auto_stop=True)
    close_the_call(window)
    alerts.clear()
    window._end_prompt.stop_button.click()
    assert stopped(window)
    assert window._ended_notice is None and alerts == []
    assert "Recording stopped and queued." in window.status_label.text()


def test_stop_now_on_an_hour_countdown_is_not_an_automatic_stop(window, tmp_path, monkeypatch):
    monkeypatch.setattr(QApplication, "alert", staticmethod(lambda w, msec=0: None))
    start_call(tmp_path, window, "hour", auto_stop=False)
    at(window, 14, 59, 30)
    window._check_auto_end(time.monotonic())
    window._auto_end_prompt.stop_button.click()
    assert stopped(window)
    assert window._ended_notice is None


# -- the meeting's real name arriving after the start --------------------------------------------------------------


def test_the_meetings_own_name_replaces_the_automatic_one(window, tmp_path, caplog):
    import time as _time

    caplog.set_level(logging.INFO, logger="meeting_notes.client.ui")
    start_call(tmp_path, window, "manual")
    assert window.name_edit.text() == "Weekly Sync"
    renamed = []
    window.controller.set_recording_name = renamed.append
    window._detector.name = None
    window._refresh_meeting_name(_time.monotonic())  # nothing better yet
    assert window.name_edit.text() == "Weekly Sync"
    window._detector.name = "Mike/Tara 1:1"
    window._refresh_meeting_name(_time.monotonic())
    assert window.name_edit.text() == "Mike/Tara 1:1" and renamed[-1] == "Mike/Tara 1:1"
    assert any("meeting's own name appeared" in r.message for r in caplog.records)


def test_a_name_typed_by_hand_is_never_replaced(window, tmp_path):
    import time as _time

    start_call(tmp_path, window, "manual")
    window.name_edit.setText("Budget review")  # the user named it
    window._detector.name = "Mike/Tara 1:1"
    window._refresh_meeting_name(_time.monotonic())
    assert window.name_edit.text() == "Budget review"
    assert window._name_refresh_until == 0.0  # and it stops looking


def test_the_name_is_only_watched_for_a_while(window, tmp_path):
    import time as _time
    from meeting_notes.client.ui import main_window as mw

    start_call(tmp_path, window, "manual")
    window._detector.name = "Mike/Tara 1:1"
    window._refresh_meeting_name(_time.monotonic() + mw.NAME_REFRESH_SEC + 1)
    assert window.name_edit.text() == "Weekly Sync" and window._name_refresh_until == 0.0
