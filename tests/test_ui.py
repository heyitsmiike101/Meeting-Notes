"""UI tests, run headless via Qt's offscreen platform.

These cannot judge whether the window looks good, but they do cover the things
that silently break: a widget that throws while painting, a settings dialog that
fails to persist what was typed, and the "no audio devices" path -- which is
exactly the state this test environment is in, and the state a user hits when a
device is unplugged.
"""

from __future__ import annotations

import json

import pytest

PySide6 = pytest.importorskip("PySide6", reason="PySide6 not installed")

from PySide6.QtGui import QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from meeting_notes import config as config_mod  # noqa: E402
from meeting_notes.client.ui.theme import APP_STYLE  # noqa: E402
from meeting_notes.client.ui.waveform import WaveformWidget  # noqa: E402


@pytest.fixture(scope="session")
def qt_app():
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(APP_STYLE)
    return app


def render(widget, width=600, height=200) -> QPixmap:
    widget.resize(width, height)
    pixmap = QPixmap(widget.size())
    widget.render(pixmap)
    return pixmap


def test_waveform_paints_without_error(qt_app):
    w = WaveformWidget()
    for i in range(200):
        w.push({"mic": (i % 20) / 20, "system": 1.0 - (i % 20) / 20})
    pixmap = render(w)
    assert not pixmap.isNull()
    assert pixmap.width() == 600


def test_waveform_paints_when_empty_and_when_cleared(qt_app):
    """A fresh widget paints before any audio arrives, and after a reset."""
    w = WaveformWidget()
    assert not render(w).isNull()
    w.push({"mic": 0.5, "system": 0.5})
    w.clear()
    assert w.peak("mic") == 0.0
    assert not render(w).isNull()


def test_waveform_clamps_out_of_range_levels(qt_app):
    """A level above 1.0 must not draw outside its lane."""
    w = WaveformWidget()
    w.push({"mic": 4.2, "system": -3.0})
    assert w.peak("mic") == 1.0
    assert w.peak("system") == 0.0
    assert not render(w).isNull()


def test_waveform_handles_a_missing_track(qt_app):
    """A track that never reports (failed device) is silence, not a crash."""
    w = WaveformWidget()
    w.push({"mic": 0.8})
    assert w.peak("system") == 0.0
    assert not render(w).isNull()


def test_settings_dialog_round_trips_config(qt_app, tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("MEETING_NOTES_CONFIG", str(config_path))

    from meeting_notes.client.ui.settings_dialog import SettingsDialog

    dialog = SettingsDialog()
    dialog.save_dir_edit.setText(str(tmp_path / "Recordings"))
    dialog.url_edit.setText("http://192.168.1.50:8000/")
    dialog.token_edit.setText("s3cret")
    dialog.model_combo.setCurrentText("small.en")
    dialog.live_check.setChecked(False)
    dialog.accept()

    saved = json.loads(config_path.read_text())
    assert saved["save_dir"] == str(tmp_path / "Recordings")
    # Trailing slash stripped, or every built URL would contain a double slash.
    assert saved["server"]["url"] == "http://192.168.1.50:8000"
    assert saved["server"]["token"] == "s3cret"
    assert saved["server"]["live_preview"] is False
    assert saved["transcribe"]["model"] == "small.en"
    # The save folder is created up front, so a bad path fails in the dialog
    # rather than partway into a meeting.
    assert (tmp_path / "Recordings").is_dir()


def test_main_window_reports_missing_devices_instead_of_crashing(qt_app, tmp_path, monkeypatch):
    """soundcard is not importable in this environment, which is the point:
    the window must open and explain itself rather than throw."""
    monkeypatch.setenv("MEETING_NOTES_CONFIG", str(tmp_path / "config.json"))

    from meeting_notes.client.ui.main_window import MainWindow

    window = MainWindow()
    window._timer.stop()
    assert "unavailable" in window.devices_label.text().lower()

    window._start()
    # Starting must fail loudly in the status line, and leave us idle.
    assert window.controller.state == "idle"
    assert window.status_label.text().lower().startswith("could not start")
    assert not render(window, 760, 600).isNull()
