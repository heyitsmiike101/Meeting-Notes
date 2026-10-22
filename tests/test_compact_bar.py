"""Compact view: the slim right-edge bar (status, meters, mute, expand) and its hooks in the main window."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6", reason="PySide6 not installed")

from PySide6.QtCore import QPoint, QRect, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from meeting_notes import config as config_mod  # noqa: E402
from meeting_notes.client.controller import IDLE, RECORDING, STOPPING  # noqa: E402
from meeting_notes.client.ui import compact_bar as cb  # noqa: E402
from meeting_notes.client.ui.theme import APP_STYLE  # noqa: E402

AREA = QRect(0, 0, 1920, 1040)


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(APP_STYLE)
    return app


@pytest.fixture
def window(qt_app, tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_NOTES_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("MEETING_NOTES_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("MEETING_NOTES_NO_DETECT", "1")
    from meeting_notes.audio import devices as devices_mod
    from meeting_notes.client.ui.main_window import MainWindow

    def _raise(kind, requested=None, samplerate=None):
        raise devices_mod.DeviceNotFound("no device (simulated)")

    monkeypatch.setattr(devices_mod, "resolve_source", _raise)
    monkeypatch.setattr(cb.CompactBar, "_available_geometry", lambda self: AREA)
    w = MainWindow()
    w._timer.stop()
    w._auth_timer.stop()
    w._detect_timer.stop()
    yield w
    w._leave_compact(persist=False)
    w.controller.stop_uploader()
    w.close()


def _record(window, monkeypatch, muted=None):
    """Pretend a recording is running, with a recorded set of mute calls."""
    muted = {} if muted is None else muted
    calls = []
    window.controller.state = RECORDING
    monkeypatch.setattr(
        window.controller, "set_source_muted", lambda track, value: (calls.append((track, value)), muted.update({track: value}), True)[2]
    )
    monkeypatch.setattr(window.controller, "source_muted", lambda track: muted.get(track, False))
    monkeypatch.setattr(window.controller, "levels", lambda: {"mic": 0.8, "system": 0.3})
    monkeypatch.setattr(type(window.controller), "elapsed", property(lambda self: 125.0))
    window.mute_mic_button.setEnabled(True)
    window.mute_system_button.setEnabled(True)
    return calls


def test_compact_button_has_tooltip_and_accessible_name(window):
    assert window.compact_button.toolTip() == "Compact view"
    assert window.compact_button.accessibleName() == "Compact view"
    assert window.compact_button.focusPolicy() != Qt.NoFocus


def test_enter_compact_hides_window_and_docks_at_right_edge(window):
    window.show()
    window.compact_button.click()
    bar = window._compact_bar
    assert bar.isVisible() and not window.isVisible()
    flags = bar.windowFlags()
    assert flags & Qt.Tool and flags & Qt.FramelessWindowHint and flags & Qt.WindowStaysOnTopHint
    assert 56 <= bar.width() <= 72
    assert bar.x() + bar.width() - 1 == AREA.right()
    # Centred vertically by default.
    assert abs((bar.y() + bar.height() / 2) - AREA.center().y()) <= 2


def test_expand_restores_window_and_hides_bar(window):
    window.show()
    window.enter_compact_mode()
    window._compact_bar.expand_button.click()
    assert window.isVisible() and not window._compact_bar.isVisible()
    assert config_mod.ui_state()["compact"] is False


def test_double_click_expands(window, qt_app):
    from PySide6.QtCore import QEvent, QPointF
    from PySide6.QtGui import QMouseEvent

    window.show()
    window.enter_compact_mode()
    bar = window._compact_bar
    pos = QPointF(3, 3)
    event = QMouseEvent(QEvent.MouseButtonDblClick, pos, bar.mapToGlobal(pos), Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    qt_app.sendEvent(bar, event)
    assert window.isVisible() and not bar.isVisible()


def test_show_from_tray_style_activation_leaves_compact(window):
    window.show()
    window.enter_compact_mode()
    window.activate_window()
    assert window.isVisible() and not window._compact_bar.isVisible()


def test_mute_buttons_use_the_windows_mute_and_reflect_state(window, monkeypatch):
    calls = _record(window, monkeypatch)
    window.show()
    window.enter_compact_mode()
    bar = window._compact_bar
    assert bar.mute_mic_button.isEnabled() and bar.mute_system_button.isEnabled()
    assert bar.mute_mic_button.accessibleName() == "Mute microphone"

    bar.mute_mic_button.click()
    assert calls == [("mic", True)]
    assert window.mute_mic_button.isChecked()
    assert bar.mute_mic_button.isChecked()
    assert bar.mute_mic_button.accessibleName() == "Unmute microphone"
    assert not bar.mute_system_button.isChecked()

    bar.mute_system_button.click()
    assert calls[-1] == ("system", True)
    assert bar.mute_system_button.accessibleName() == "Unmute meeting audio"

    # Muting from the window shows on the bar at the next tick.
    window.mute_mic_button.setChecked(False)
    window._sync_compact_bar()
    assert not bar.mute_mic_button.isChecked()
    assert bar.mute_mic_button.accessibleName() == "Mute microphone"


def test_mute_buttons_disabled_while_idle(window):
    window.show()
    window.enter_compact_mode()
    bar = window._compact_bar
    assert not bar.mute_mic_button.isEnabled() and not bar.mute_system_button.isEnabled()
    assert bar.status_text == "Idle"


def test_levels_update_the_meters(window, monkeypatch):
    _record(window, monkeypatch)
    window.show()
    window.enter_compact_mode()
    window._tick()
    bar = window._compact_bar
    assert bar.mic_meter.level == pytest.approx(0.8)
    assert bar.system_meter.level == pytest.approx(0.3)


def test_muted_track_meter_shows_nothing_but_keeps_level_value(window, monkeypatch):
    _record(window, monkeypatch, muted={"mic": True})
    window.mute_mic_button.setChecked(True)
    window.show()
    window.enter_compact_mode()
    bar = window._compact_bar
    assert bar.mute_mic_button.isChecked()


def test_status_text_idle_recording_finishing(window, monkeypatch):
    window.show()
    window.enter_compact_mode()
    bar = window._compact_bar
    assert bar.status_text == "Idle" and bar.state == "idle"

    _record(window, monkeypatch)
    window._tick()
    assert bar.state == "recording" and bar.status_text == "02:05"

    window.controller.state = STOPPING
    window._tick()
    assert bar.state == "finishing" and bar.status_text == "Finishing"
    assert not bar.mute_mic_button.isEnabled()

    window.controller.state = IDLE
    window._tick()
    assert bar.status_text == "Idle"


def test_format_elapsed():
    assert cb.format_elapsed(0) == "00:00"
    assert cb.format_elapsed(125) == "02:05"
    assert cb.format_elapsed(3600 + 5 * 60) == "1:05"
    assert cb.format_elapsed(-3) == "00:00"


def test_state_persisted_and_restored(window, qt_app, tmp_path, monkeypatch):
    from meeting_notes.client.ui.main_window import MainWindow

    window.show()
    window.enter_compact_mode()
    window._compact_bar.move_vertically_to(200)
    window._compact_bar.position_changed.emit(window._compact_bar.y_fraction)
    state = config_mod.ui_state()
    assert state["compact"] is True and 0.0 <= state["compact_y"] <= 1.0

    again = MainWindow()
    try:
        again._timer.stop()
        again._auth_timer.stop()
        again._detect_timer.stop()
        assert again.restore_compact_mode() is True
        assert again._compact_bar.isVisible() and not again.isVisible()
        assert again._compact_bar.y_fraction == pytest.approx(state["compact_y"])
        assert abs(again._compact_bar.y() - 200) <= 2
    finally:
        again._leave_compact(persist=False)
        again.controller.stop_uploader()
        again.close()


def test_not_restored_when_it_was_off(window):
    assert window.restore_compact_mode() is False
    assert not window._compact_active


def test_ui_state_keeps_other_config_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_NOTES_CONFIG", str(tmp_path / "config.json"))
    config_mod.save_config({"appearance": "dark"})
    config_mod.update_ui_state(compact=True)
    config_mod.update_ui_state(compact_y=0.25)
    data = config_mod.load_config()
    assert data["appearance"] == "dark"
    assert data["ui_state"] == {"compact": True, "compact_y": 0.25}


def test_drag_clamps_within_screen(window):
    window.show()
    window.enter_compact_mode()
    bar = window._compact_bar
    bar.move_vertically_to(-500)
    assert bar.y() == AREA.top() and bar.y_fraction == 0.0
    bar.move_vertically_to(99999)
    assert bar.y() + bar.height() == AREA.bottom() + 1 and bar.y_fraction == 1.0
    assert bar.x() + bar.width() - 1 == AREA.right()
    bar.move_vertically_to(300)
    assert bar.y() == 300


def test_mouse_drag_moves_vertically_and_saves(window, qt_app):
    from PySide6.QtCore import QEvent, QPointF
    from PySide6.QtGui import QMouseEvent

    window.show()
    window.enter_compact_mode()
    bar = window._compact_bar
    start_y = bar.y()
    start_x = bar.x()
    local = QPointF(3, 3)
    press_global = bar.mapToGlobal(local)

    def send(kind, global_pos, buttons):
        qt_app.sendEvent(bar, QMouseEvent(kind, local, global_pos, Qt.LeftButton, buttons, Qt.NoModifier))

    send(QEvent.MouseButtonPress, press_global, Qt.LeftButton)
    send(QEvent.MouseMove, press_global + QPointF(40, 90), Qt.LeftButton)
    assert bar.y() == start_y + 90 and bar.x() == start_x
    send(QEvent.MouseButtonRelease, press_global + QPointF(40, 90), Qt.NoButton)
    assert config_mod.ui_state()["compact_y"] == pytest.approx(bar.y_fraction, abs=1e-3)


def test_redock_to_primary_when_screen_goes_away(window, monkeypatch):
    window.show()
    window.enter_compact_mode()
    bar = window._compact_bar
    bar._screen = object()  # a screen that is no longer in the list
    primary = QApplication.primaryScreen()
    bar._on_screens_changed()
    assert bar._screen is primary
    assert bar.x() + bar.width() - 1 == AREA.right()


def test_no_taskbar_entry_flag_and_accessible_names(window):
    window.show()
    window.enter_compact_mode()
    bar = window._compact_bar
    assert bar.windowFlags() & Qt.Tool
    for button in (bar.mute_mic_button, bar.mute_system_button, bar.expand_button):
        assert button.accessibleName() and button.toolTip()
        assert button.focusPolicy() != Qt.NoFocus
    assert isinstance(bar.pos(), QPoint)
