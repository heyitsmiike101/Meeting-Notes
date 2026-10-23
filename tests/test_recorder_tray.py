"""The Mac menu-bar icon: up beside the window, mirrors the recording and drives the window's own controls."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6", reason="PySide6 not installed")

from PySide6.QtWidgets import QApplication, QSystemTrayIcon  # noqa: E402

from meeting_notes.client.controller import IDLE, STOPPING  # noqa: E402
from meeting_notes.client.ui import main_window as mw  # noqa: E402
from meeting_notes.client.ui import tray as tray_mod  # noqa: E402
from meeting_notes.client.ui.theme import APP_STYLE  # noqa: E402
from tests.test_compact_bar import _record  # noqa: E402


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
    monkeypatch.delenv("MEETING_NOTES_NO_TRAY", raising=False)
    monkeypatch.setattr(mw, "recorder_tray_wanted", lambda: True)  # as on a Mac
    monkeypatch.setattr(QSystemTrayIcon, "show", lambda self: None)  # never a real status item
    from meeting_notes.audio import devices as devices_mod

    def _raise(kind, requested=None, samplerate=None):
        raise devices_mod.DeviceNotFound("no device (simulated)")

    monkeypatch.setattr(devices_mod, "resolve_source", _raise)
    w = mw.MainWindow()
    w._timer.stop()
    w._auth_timer.stop()
    w._detect_timer.stop()
    yield w
    w.controller.stop_uploader()
    w.close()


def test_only_a_mac_gets_the_recorders_menu_bar_icon(monkeypatch):
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True))
    assert tray_mod.recorder_tray_wanted("darwin") is True
    assert tray_mod.recorder_tray_wanted("win32") is False
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: False))
    assert tray_mod.recorder_tray_wanted("darwin") is False


def test_off_under_test_and_where_not_wanted(qt_app, tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_NOTES_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("MEETING_NOTES_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(mw, "recorder_tray_wanted", lambda: False)
    monkeypatch.delenv("MEETING_NOTES_NO_TRAY", raising=False)
    w = mw.MainWindow()
    try:
        assert w.recorder_tray is None
    finally:
        w.controller.stop_uploader()
        w.close()


def test_the_icon_is_up_beside_the_window(window):
    window.show()
    tray = window.recorder_tray
    assert tray is not None
    assert window.isVisible()  # both: the icon never replaces the window
    menu = [a.text() for a in tray.menu.actions() if not a.isSeparator()]
    assert menu == ["Idle", "Start recording", "Mute you", "Mute them", "Show Meeting Notes", "Quit Meeting Notes"]
    assert not tray.status_action.isEnabled()
    assert not tray.mute_actions["mic"].isEnabled() and not tray.mute_actions["system"].isEnabled()


def test_it_follows_the_recording(window, monkeypatch):
    tray = window.recorder_tray
    _record(window, monkeypatch)
    window._sync_recorder_tray()  # (a full tick re-checks devices, and the fake recording has none)
    assert tray.status_action.text() == "Recording 02:05" and tray.record_action.text() == "Stop recording"
    assert tray.mute_actions["mic"].isEnabled() and tray.mute_actions["system"].isEnabled()
    assert tray.icon.toolTip() == "Meeting Notes · Recording 02:05"

    window.controller.state = STOPPING
    window._tick()
    assert tray.status_action.text() == "Finishing" and not tray.record_action.isEnabled()
    assert not tray.mute_actions["mic"].isEnabled()

    window.controller.state = IDLE
    window._tick()
    assert tray.status_action.text() == "Idle" and tray.record_action.text() == "Start recording"
    assert tray.record_action.isEnabled()


def test_mute_from_the_menu_uses_the_windows_mute(window, monkeypatch):
    calls = _record(window, monkeypatch)
    window._sync_recorder_tray()
    tray = window.recorder_tray
    tray.mute_actions["mic"].trigger()
    assert calls == [("mic", True)] and window.mute_mic_button.isChecked()
    # and a mute made in the window shows in the menu, without echoing a second call back
    window.mute_system_button.setChecked(True)
    calls.clear()
    window._sync_recorder_tray()
    assert tray.mute_actions["system"].isChecked() and calls == []


def test_start_and_stop_go_through_the_record_button(window, monkeypatch):
    clicks = []
    monkeypatch.setattr(window.record_button, "click", lambda: clicks.append(1))
    window.recorder_tray.record_action.trigger()
    assert clicks == [1]
    window.record_button.setEnabled(False)  # finishing: nothing to press
    window.recorder_tray.record_action.trigger()
    assert clicks == [1]


def test_show_and_quit(window, monkeypatch):
    window.hide()
    window.recorder_tray.open_action.trigger()
    assert window.isVisible()
    quits = []
    monkeypatch.setattr(window, "quit_app", lambda: quits.append(1))
    # the signal was connected to the bound method at setup; reconnect the stub the same way
    window.recorder_tray.quit_requested.disconnect()
    window.recorder_tray.quit_requested.connect(window.quit_app)
    window.recorder_tray.quit_action.trigger()
    assert quits == [1]


def test_the_icon_turns_red_while_recording(window, monkeypatch):
    tray = window.recorder_tray
    assert tray.icon.icon().isMask()  # a template image: macOS tints it for the menu bar
    _record(window, monkeypatch)
    window._tick()
    assert not tray.icon.icon().isMask()
