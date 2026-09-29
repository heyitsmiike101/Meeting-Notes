"""UI tests, run headless via Qt's offscreen platform.

These cannot judge whether the window looks good, but they do cover the things
that silently break: a widget that throws while painting, a settings dialog that
fails to persist what was typed, and the "no audio devices" path -- which is
exactly the state this test environment is in, and the state a user hits when a
device is unplugged.
"""

from __future__ import annotations

import json
import threading
import time

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
    dialog.live_check.setChecked(False)
    dialog.accept()

    saved = json.loads(config_path.read_text())
    assert saved["save_dir"] == str(tmp_path / "Recordings")
    # Trailing slash stripped, or every built URL would contain a double slash.
    assert saved["server"]["url"] == "http://192.168.1.50:8000"
    assert saved["server"]["token"] == "s3cret"
    assert saved["server"]["live_preview"] is False
    # Model selection is deliberately not a client concern.  Even if an old
    # config contained one, opening/saving the Windows settings must not keep
    # exposing or forwarding that transcription control.
    assert "model" not in saved["server"]
    assert not hasattr(dialog, "model_edit")
    # The save folder is created up front, so a bad path fails in the dialog
    # rather than partway into a meeting.
    assert (tmp_path / "Recordings").is_dir()


def _deny_all_devices(monkeypatch) -> None:
    """Make every device lookup fail, regardless of what hardware this
    machine actually has.

    Without this, the test that follows only passed by accident: it relied
    on soundcard finding nothing, which is true in CI but false on any
    machine with a real mic and loopback device -- exactly the situation
    that shipped a false-negative test.
    """
    from meeting_notes.audio import devices as devices_mod

    def _raise(kind, requested=None, samplerate=None):
        raise devices_mod.DeviceNotFound(f"no {kind} device (simulated for this test)")

    monkeypatch.setattr(devices_mod, "resolve_source", _raise)


def test_main_window_reports_missing_devices_instead_of_crashing(qt_app, tmp_path, monkeypatch):
    """Devices are simulated as unavailable (see _deny_all_devices) so this
    is hermetic on a machine with a real mic and loopback device too: the
    window must open and explain itself rather than throw."""
    monkeypatch.setenv("MEETING_NOTES_CONFIG", str(tmp_path / "config.json"))
    _deny_all_devices(monkeypatch)

    from meeting_notes.client.ui.main_window import MainWindow

    window = MainWindow()
    window._timer.stop()
    assert "unavailable" in window.devices_label.text().lower()

    window._start()
    # Starting must fail loudly in the status line, and leave us idle.
    assert window.controller.state == "idle"
    assert window.status_label.text().lower().startswith("could not start")
    assert not render(window, 760, 600).isNull()


def test_main_window_refreshes_devices_and_enables_audio_log(qt_app, tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_NOTES_CONFIG", str(tmp_path / "config.json"))
    from meeting_notes.client.ui.main_window import MainWindow

    window = MainWindow()
    window._timer.stop()
    diagnostic = tmp_path / "audio-device-diagnostic.log"
    diagnostic.write_text("test diagnostic\n", encoding="utf-8")

    def probe():
        window.controller.device_diagnostic_path = diagnostic
        return {"mic": "USB Microphone", "system": "USB Speakers"}

    monkeypatch.setattr(window.controller, "probe_devices", probe)
    window._refresh_devices()

    assert "USB Microphone" in window.devices_label.text()
    assert "USB Speakers" in window.devices_label.text()
    assert window.audio_log_button.isEnabled()


def test_main_window_exposes_release_upload_and_independent_mute_controls(
    qt_app, tmp_path, monkeypatch
):
    monkeypatch.setenv("MEETING_NOTES_CONFIG", str(tmp_path / "config.json"))
    _deny_all_devices(monkeypatch)

    from meeting_notes import __version__
    from meeting_notes.client.ui.main_window import MainWindow

    window = MainWindow()
    window._timer.stop()
    try:
        assert window.version_label.text() == f"v{__version__}"
        assert window.upload_button.text() == "Upload recording"
        assert window.mute_mic_button.text() == "Mute you"
        assert window.mute_system_button.text() == "Mute them"
        assert not window.mute_mic_button.isEnabled()
        assert not window.mute_system_button.isEnabled()
        assert window.mute_mic_button.accessibleName() == "Mute your microphone"
        assert window.mute_system_button.accessibleName() == "Mute system audio"

        window.resize(1000, 720)
        window.show()
        qt_app.processEvents()
        waveform = window.waveform.geometry()
        mic_centre = window.mute_mic_button.geometry().center().y()
        system_centre = window.mute_system_button.geometry().center().y()
        # Controls remain beside their matching half of the two-lane meter.
        assert waveform.top() < mic_centre < waveform.center().y()
        assert waveform.center().y() < system_centre < waveform.bottom()

        calls = []
        monkeypatch.setattr(
            window.controller,
            "set_source_muted",
            lambda track, muted: calls.append((track, muted)) or True,
        )
        window.mute_mic_button.setEnabled(True)
        window.mute_mic_button.click()
        assert calls == [("mic", True)]
        assert window.mute_mic_button.text() == "Unmute you"
        assert window.mute_mic_button.accessibleName() == "Unmute your microphone"
        assert window.mute_system_button.text() == "Mute them"
    finally:
        window.controller.stop_uploader()
        window.close()


def _pump(condition, timeout: float = 2.0) -> bool:
    """Process Qt events until ``condition()`` is true or ``timeout`` elapses.

    Standing in for QTest.qWait: the point of these tests is that the GUI
    event loop keeps running (i.e. stays paintable/responsive) while a
    background join is in progress, so driving it by actually pumping events
    -- rather than a bare time.sleep -- is what proves that, not just what
    waits for it.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        QApplication.processEvents()
        time.sleep(0.01)
    return condition()


def test_history_dialog_loads_search_results_and_transcript(qt_app):
    class FakeHistoryClient:
        base_url = "http://server:8000"

        def list_sessions(self, **kwargs):
            assert kwargs["q"] == "roadmap"
            return {
                "items": [{
                    "session_id": "session-1",
                    "name": "Roadmap sync",
                    "created": 1_700_000_000,
                    "latest_state": "done",
                }],
                "total": 1,
            }

        def session_detail(self, session_id):
            assert session_id == "session-1"
            return {
                "session_id": session_id,
                "meta": {"name": "Roadmap sync"},
                "jobs": [{"state": "done"}],
                "has_audio": True,
                "markdown": "# Transcript\n\nShip it.",
            }

    from meeting_notes.client.ui.history_dialog import HistoryDialog

    dialog = HistoryDialog(client=FakeHistoryClient())
    dialog.search_edit.setText("roadmap")
    dialog.refresh()
    assert _pump(lambda: dialog.sessions.count() == 1)
    assert _pump(lambda: "Ship it" in dialog.transcript.toPlainText())
    assert dialog.retranscribe_button.isEnabled()
    assert not render(dialog, 900, 600).isNull()
    dialog.close()


def test_stop_recording_does_not_block_the_gui_thread(qt_app, tmp_path, monkeypatch):
    """controller.stop() can join several background threads for real
    seconds. The window must stay responsive while that happens (proved here
    by pumping events during the join) and the record button must still end
    up back in its start state once the join actually finishes."""
    monkeypatch.setenv("MEETING_NOTES_CONFIG", str(tmp_path / "config.json"))
    _deny_all_devices(monkeypatch)

    from meeting_notes.client.controller import RECORDING
    from meeting_notes.client.ui.main_window import MainWindow

    window = MainWindow()
    window._timer.stop()

    stop_called = threading.Event()
    release = threading.Event()

    def slow_stop():
        stop_called.set()
        release.wait(timeout=2.0)
        return {"duration_sec": 1.5}

    window.controller.state = RECORDING
    monkeypatch.setattr(window.controller, "stop", slow_stop)

    window._stop()

    # _stop() must return immediately -- the fake join is still parked on
    # `release` -- with the UI already showing "in progress".
    assert stop_called.wait(timeout=1.0)
    assert not window.record_button.isEnabled()
    assert window.record_button.text() == "Finishing..."

    # The event loop is free to run right now (release hasn't fired yet):
    # pumping it must not block on the background join.
    t0 = time.monotonic()
    QApplication.processEvents()
    assert time.monotonic() - t0 < 0.5
    assert not window.record_button.isEnabled()  # still finishing

    release.set()
    assert _pump(lambda: window.record_button.text() == "Start recording")
    assert window.record_button.isEnabled()
    assert "Saved" in window.status_label.text()


def test_close_while_recording_defers_until_stop_finishes(qt_app, tmp_path, monkeypatch):
    """closeEvent must not join controller.stop()'s background threads on the
    GUI thread either: it should ignore the close, let the stop (and the
    uploader shutdown) finish off-thread, then actually close."""
    monkeypatch.setenv("MEETING_NOTES_CONFIG", str(tmp_path / "config.json"))
    _deny_all_devices(monkeypatch)

    from meeting_notes.client.controller import RECORDING
    from meeting_notes.client.ui.main_window import MainWindow

    window = MainWindow()
    window._timer.stop()

    release = threading.Event()

    def slow_stop():
        release.wait(timeout=2.0)
        return {"duration_sec": 0.5}

    stop_uploader_called = []
    window.controller.state = RECORDING
    monkeypatch.setattr(window.controller, "stop", slow_stop)
    monkeypatch.setattr(
        window.controller, "stop_uploader", lambda: stop_uploader_called.append(True)
    )

    window.close()
    # The close is deferred: neither half of teardown has run yet, and a
    # second close attempt while it's in flight must not start another one.
    assert window._pending_close is True
    assert not stop_uploader_called
    window.close()
    assert not stop_uploader_called

    release.set()
    assert _pump(lambda: bool(stop_uploader_called))
    assert _pump(lambda: window._pending_close is False)
    assert window._teardown_done is True


def test_settings_restart_uploader_does_not_block_the_gui_thread(qt_app, tmp_path, monkeypatch):
    """_open_settings's restart_uploader() call can block for as long as
    UploadWorker.stop()'s join_timeout (5s) if an upload is in flight -- same
    freeze risk as controller.stop(), so it needs the same async treatment."""
    monkeypatch.setenv("MEETING_NOTES_CONFIG", str(tmp_path / "config.json"))
    _deny_all_devices(monkeypatch)

    from meeting_notes.client.ui import main_window as main_window_mod

    class _FakeSettingsDialog:
        def __init__(self, parent=None):
            pass

        def exec(self):
            return 1  # QDialog.Accepted, without opening a real modal dialog

    monkeypatch.setattr(main_window_mod, "SettingsDialog", _FakeSettingsDialog)

    window = main_window_mod.MainWindow()
    window._timer.stop()

    release = threading.Event()

    def slow_restart():
        release.wait(timeout=2.0)

    monkeypatch.setattr(window.controller, "restart_uploader", slow_restart)

    window._open_settings()

    # Returned immediately -- the fake restart is still parked on `release`
    # -- with the settings button disabled to block a second overlapping call.
    assert not window.settings_button.isEnabled()

    release.set()
    assert _pump(lambda: window.settings_button.isEnabled())


def test_short_upload_error_names_the_cause():
    from meeting_notes.client.ui.main_window import _short_upload_error

    assert "token" in _short_upload_error("HTTPStatusError: Client error '403 Forbidden' for url 'http://x'")
    assert _short_upload_error("ServerUnavailable: POST /x failed: [WinError 10061] refused") == "server unreachable"
    assert _short_upload_error("RuntimeError: " + "y" * 100).endswith("...")
