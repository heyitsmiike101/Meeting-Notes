"""Round-2 client features: token alerts, connection test, logging, Logs window,
and keeping recordings out of the app folder."""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import zipfile
from pathlib import Path

import httpx
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6", reason="PySide6 not installed")

from PySide6.QtWidgets import QApplication  # noqa: E402

from meeting_notes import config as config_mod  # noqa: E402
from meeting_notes.client import authcheck, logs as logs_mod, logsetup, paths  # noqa: E402
from meeting_notes.client.api import ServerClient  # noqa: E402
from meeting_notes.client.ui.theme import APP_STYLE  # noqa: E402

TOKEN = "s3cr3t-token-VALUE-123"


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(APP_STYLE)
    return app


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An isolated config, home and log directory."""
    monkeypatch.setenv("MEETING_NOTES_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("MEETING_NOTES_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("MEETING_NOTES_NO_DETECT", "1")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def _configure(tmp_path, url="http://meeting.lan", token=TOKEN, **extra):
    data = {"save_dir": str(tmp_path / "rec"), "server": {"url": url, "token": token}}
    data.update(extra)
    config_mod.save_config(data)


def _pump(condition, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        QApplication.processEvents()
        time.sleep(0.01)
    return condition()


def _mock_client(monkeypatch, module, handler):
    """Make ``module.ServerClient`` talk to an in-memory handler."""

    class Fake(ServerClient):
        def __init__(self, base_url, token=None, timeout=10.0):
            super().__init__(base_url, token, timeout)
            self._client = httpx.Client(base_url=self.base_url, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(module, "ServerClient", Fake)


def _window(qt_app, monkeypatch):
    from meeting_notes.audio import devices as devices_mod
    from meeting_notes.client.ui.main_window import MainWindow

    def _raise(kind, requested=None, samplerate=None):
        raise devices_mod.DeviceNotFound("no device (simulated)")

    monkeypatch.setattr(devices_mod, "resolve_source", _raise)
    window = MainWindow()
    window._timer.stop()
    window._auth_timer.stop()
    window._auth_checker = lambda url, token: authcheck.CheckResult(authcheck.NO_SERVER)
    return window


def _close(window):
    window.controller.stop_uploader()
    window.close()


# -- A. alert strips -------------------------------------------------------------


def test_wrong_token_shows_a_red_strip_and_clears_when_a_check_succeeds(qt_app, home, monkeypatch):
    _configure(home, url="")
    window = _window(qt_app, monkeypatch)
    alerts = []
    monkeypatch.setattr(QApplication, "alert", staticmethod(lambda widget, *a: alerts.append(widget)))
    try:
        forbidden = "HTTPStatusError: Client error '403 Forbidden' for url 'http://meeting.lan/v1/x'"
        monkeypatch.setattr(
            window.controller, "queue_status", lambda: {"pending": 3, "failed": 0, "last_error": forbidden}
        )
        window._refresh_alerts()
        assert not window.alert_bar.isHidden()
        assert window.alert_label.text() == (
            "The server rejected your password. 3 meetings are waiting to upload."
        )
        assert window.alert_button.text() == "Fix in Settings"
        assert len(alerts) == 1  # the taskbar is flashed once, when it first appears
        window._refresh_alerts()
        assert len(alerts) == 1

        # A proactive check that succeeds clears it, even though the queue still
        # remembers the old error.
        window._auth_state = "ok"
        window._refresh_alerts()
        assert window.alert_bar.isHidden()
    finally:
        _close(window)


def test_stream_rejection_and_proactive_rejection_raise_the_strip(qt_app, home, monkeypatch):
    _configure(home, url="")
    window = _window(qt_app, monkeypatch)
    monkeypatch.setattr(QApplication, "alert", staticmethod(lambda *a: None))
    try:
        monkeypatch.setattr(window.controller, "queue_status", lambda: {"pending": 0, "failed": 0, "last_error": ""})
        window.controller.state = "recording"
        monkeypatch.setattr(
            window.controller,
            "stream_error",
            lambda: "live preview rejected by server: unauthorized (check the password in Settings)",
        )
        window._refresh_alerts()
        assert not window.alert_bar.isHidden()
        assert "rejected your password" in window.alert_label.text()
        window.controller.state = "idle"
        monkeypatch.setattr(window.controller, "stream_error", lambda: None)
        window._refresh_alerts()
        assert window.alert_bar.isHidden()

        window._auth_state = "rejected"
        window._refresh_alerts()
        assert not window.alert_bar.isHidden()
        assert "1 meeting is" not in window.alert_label.text()
    finally:
        _close(window)


def test_proactive_check_runs_in_background_and_sets_state(qt_app, home, monkeypatch):
    _configure(home)
    window = _window(qt_app, monkeypatch)
    monkeypatch.setattr(QApplication, "alert", staticmethod(lambda *a: None))
    seen = {}

    def fake_checker(url, token):
        seen["thread"] = threading.current_thread().name
        seen["args"] = (url, token)
        return authcheck.CheckResult(authcheck.REJECTED, url, "HTTP 403", 403)

    try:
        window._auth_checker = fake_checker
        window._start_auth_check("test")
        assert _pump(lambda: window._auth_state == "rejected")
        assert seen["args"] == ("http://meeting.lan", TOKEN)
        assert seen["thread"] != threading.main_thread().name
        assert not window.alert_bar.isHidden()

        window._auth_checker = lambda url, token: authcheck.CheckResult(authcheck.OK, url, "HTTP 200", 200)
        window._start_auth_check("test")
        assert _pump(lambda: window._auth_state == "ok")
        assert window.alert_bar.isHidden()
    finally:
        _close(window)


def test_unreachable_server_is_a_quiet_strip_only_when_uploads_wait(qt_app, home, monkeypatch):
    _configure(home, url="")
    window = _window(qt_app, monkeypatch)
    try:
        refused = "ServerUnavailable: POST /v1/x failed: [WinError 10061] refused"
        monkeypatch.setattr(
            window.controller, "queue_status", lambda: {"pending": 2, "failed": 0, "last_error": refused}
        )
        window._refresh_alerts()
        assert window.alert_bar.isHidden()
        assert not window.warn_bar.isHidden()
        assert "2 meetings are waiting" in window.warn_label.text()

        monkeypatch.setattr(window.controller, "queue_status", lambda: {"pending": 0, "failed": 0, "last_error": ""})
        window._auth_state = "unreachable"
        window._refresh_alerts()
        assert window.warn_bar.isHidden()  # nothing is waiting: nothing to say
    finally:
        _close(window)


# -- B. connection test -----------------------------------------------------------


@pytest.mark.parametrize(
    "handler, status, message",
    [
        (lambda r: httpx.Response(200, json={"items": []}), authcheck.OK, "Connected to http://meeting.lan"),
        (lambda r: httpx.Response(403), authcheck.REJECTED, "Password rejected by the server"),
        (lambda r: httpx.Response(401), authcheck.REJECTED, "Password rejected by the server"),
        (lambda r: httpx.Response(500), authcheck.ERROR, "The server answered unexpectedly (HTTP 500)"),
    ],
)
def test_check_connection_maps_status_codes(monkeypatch, handler, status, message):
    _mock_client(monkeypatch, authcheck, handler)
    result = authcheck.check_connection("http://meeting.lan/", TOKEN)
    assert result.status == status
    assert result.message() == message


def test_check_connection_sends_the_token_to_a_protected_path(monkeypatch):
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={})

    _mock_client(monkeypatch, authcheck, handler)
    authcheck.check_connection("http://meeting.lan", TOKEN)
    assert seen == {"path": "/v1/sessions", "auth": f"Bearer {TOKEN}"}


def test_check_connection_reports_unreachable_and_missing_url(monkeypatch):
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    _mock_client(monkeypatch, authcheck, refuse)
    result = authcheck.check_connection("http://meeting.lan", TOKEN)
    assert result.status == authcheck.UNREACHABLE
    assert result.message() == "Can't reach the server at http://meeting.lan"
    assert authcheck.check_connection("", TOKEN).status == authcheck.NO_SERVER


def test_settings_test_connection_button_shows_the_result_inline(qt_app, home):
    from meeting_notes.client.ui.settings_dialog import SettingsDialog

    _configure(home)
    dialog = SettingsDialog()
    dialog.checker = lambda url, token: authcheck.CheckResult(authcheck.OK, url, "HTTP 200", 200)
    dialog.test_button.click()
    assert _pump(lambda: dialog.result_label.text() == "Connected to http://meeting.lan")
    assert not dialog.result_box.isHidden()

    dialog.checker = lambda url, token: authcheck.CheckResult(authcheck.REJECTED, url, "HTTP 403", 403)
    dialog.test_button.click()
    assert _pump(lambda: dialog.result_label.text() == "Password rejected by the server")
    dialog.checker = lambda url, token: authcheck.CheckResult(authcheck.UNREACHABLE, url, "x")
    dialog.test_button.click()
    assert _pump(lambda: dialog.result_label.text() == "Can't reach the server at http://meeting.lan")
    dialog.close()


def test_saving_a_rejected_token_asks_inline_then_saves_anyway(qt_app, home):
    from meeting_notes.client.ui.settings_dialog import SettingsDialog

    _configure(home, token="old")
    dialog = SettingsDialog()
    dialog.checker = lambda url, token: authcheck.CheckResult(authcheck.REJECTED, url, "HTTP 403", 403)
    dialog.token_edit.setText("wrong-token")
    dialog._save_clicked()
    assert _pump(lambda: dialog.save_button.text() == "Save anyway")
    assert "Password rejected by the server" in dialog.result_label.text()
    assert dialog.result() != 1
    assert config_mod.server_settings()["token"] == "old"

    dialog._save_clicked()  # the second press keeps it
    assert config_mod.server_settings()["token"] == "wrong-token"
    dialog.close()


def test_saving_a_good_token_saves_after_the_check(qt_app, home):
    from meeting_notes.client.ui.settings_dialog import SettingsDialog

    _configure(home, token="old")
    dialog = SettingsDialog()
    dialog.checker = lambda url, token: authcheck.CheckResult(authcheck.OK, url, "HTTP 200", 200)
    dialog.token_edit.setText("fresh")
    dialog._save_clicked()
    assert _pump(lambda: config_mod.server_settings()["token"] == "fresh")
    dialog.close()


# -- C. logging ---------------------------------------------------------------------


@pytest.fixture
def clean_logging(monkeypatch):
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    monkeypatch.setattr(logsetup, "_hooks_installed", False)
    yield
    logsetup.teardown_logging()


def test_log_file_is_created_and_the_token_is_masked(home, clean_logging):
    _configure(home)
    path = logsetup.setup_logging()
    assert path == home / "logs" / "client.log"
    log = logging.getLogger("meeting_notes.client.test")
    log.info("connecting with token %s and header %s", TOKEN, f"Authorization: Bearer {TOKEN}")
    log.info("url http://x/?token=abc123&y=1 and {'token': 'zzz'}")
    for handler in logging.getLogger("meeting_notes").handlers:
        handler.flush()
    text = path.read_text(encoding="utf-8")
    assert "connecting with token" in text
    assert TOKEN not in text
    assert "abc123" not in text and "zzz" not in text
    assert "Bearer ***" in text


def test_uncaught_exceptions_are_written_to_the_log(home, clean_logging):
    _configure(home)
    path = logsetup.setup_logging()
    previous = sys.excepthook

    try:
        raise RuntimeError(f"boom {TOKEN}")
    except RuntimeError:
        exc_info = sys.exc_info()
    # The hook chains to the previous hook; silence it for the test.
    logsetup._hooks_installed = False
    sys.excepthook = lambda *a: None
    logsetup.install_excepthooks()
    sys.excepthook(*exc_info)

    def in_thread():
        raise ValueError("thread failure")

    threading.excepthook = lambda args: None
    logsetup._hooks_installed = False
    logsetup.install_excepthooks()
    worker = threading.Thread(target=in_thread, name="probe-thread")
    worker.start()
    worker.join()
    for handler in logging.getLogger("meeting_notes").handlers:
        handler.flush()
    text = path.read_text(encoding="utf-8")
    assert "Uncaught exception" in text and "RuntimeError" in text
    assert TOKEN not in text
    assert "Uncaught exception in thread probe-thread" in text and "thread failure" in text
    del previous


def test_redact_text_masks_the_known_secret_and_bearer_values():
    out = logsetup.redact_text(f"a {TOKEN} b Bearer abc.def-ghi c", [TOKEN])
    assert TOKEN not in out and "abc.def-ghi" not in out
    assert out.count("***") == 2


# -- D. logs window and bundle ---------------------------------------------------------


def _seed_logs(home):
    _configure(home)
    (home / ".meeting-notes").mkdir(exist_ok=True)
    (home / ".meeting-notes" / "audio-device-diagnostic.log").write_text("mic list\n", encoding="utf-8")
    (home / ".meeting-notes" / "client-startup-error.log").write_text("no startup error\n", encoding="utf-8")
    log_dir = home / "logs"
    log_dir.mkdir(exist_ok=True)
    (log_dir / "client.log").write_text(f"new line with {TOKEN}\n", encoding="utf-8")
    (log_dir / "client.log.1").write_text("older line\n", encoding="utf-8")
    from meeting_notes.client.queue import SessionQueue

    queue = SessionQueue.for_save_dir(config_mod.save_dir())
    session = home / "rec" / "2026-09-28-standup"
    session.mkdir(parents=True)
    entry = queue.enqueue(session)
    queue.mark_attempt_failed(entry, "HTTPStatusError: 403 Forbidden", next_attempt_at=time.time() + 300)


def test_logs_panel_lists_every_source_and_redacts(qt_app, home):
    from meeting_notes.client.ui.logs_dialog import LogsPanel

    _seed_logs(home)
    dialog = LogsPanel()
    assert dialog.source_keys() == ["client", "startup", "audio", "queue", "config", "about"]
    text = dialog.viewer.toPlainText()
    assert "new line with ***" in text and "older line" in text
    assert text.index("new line") < text.index("older line")  # newest file first
    assert TOKEN not in text
    dialog.select("queue")
    queue_text = dialog.viewer.toPlainText()
    assert "2026-09-28-standup" in queue_text and "403 Forbidden" in queue_text and "Attempts    1" in queue_text
    dialog.select("config")
    config_view = dialog.viewer.toPlainText()
    assert TOKEN not in config_view and "***" in config_view
    dialog.select("about")
    assert "Meeting Notes client" in dialog.viewer.toPlainText()
    assert dialog.send_button.isEnabled()
    dialog.close()


def test_logs_panel_send_is_disabled_without_a_server(qt_app, home):
    from meeting_notes.client.ui.logs_dialog import LogsPanel

    _configure(home, url="")
    dialog = LogsPanel()
    assert not dialog.send_button.isEnabled()
    assert "server URL" in dialog.status_label.text()
    dialog.close()


def test_zip_bundle_contains_every_source_with_the_token_redacted(home, tmp_path):
    _seed_logs(home)
    out = tmp_path / "bundle.zip"
    logs_mod.build_zip(out)
    with zipfile.ZipFile(out) as archive:
        names = set(archive.namelist())
        assert names == {
            "client.log",
            "client-startup-error.log",
            "audio-device-diagnostic.log",
            "upload-queue.txt",
            "config.json",
            "about.txt",
        }
        everything = "".join(archive.read(n).decode("utf-8") for n in names)
    assert TOKEN not in everything
    assert "older line" in everything and "mic list" in everything
    assert logs_mod.default_zip_name().startswith("MeetingNotes-logs-")


def test_send_zip_handles_success_404_and_rejection(home, monkeypatch):
    seen = {}

    def ok(request):
        seen["path"] = request.url.path
        seen["ctype"] = request.headers["content-type"]
        seen["body"] = request.content
        return httpx.Response(200, json={"id": "log-42"})

    _mock_client(monkeypatch, logs_mod, ok)
    result = logs_mod.send_zip("http://meeting.lan", TOKEN, b"PKzip", "bundle.zip")
    assert result.ok and result.remote_id == "log-42" and "log-42" in result.message
    assert seen["path"] == "/v1/client-logs" and seen["ctype"].startswith("multipart/form-data")
    assert b'name="file"' in seen["body"] and b'name="device"' in seen["body"]

    _mock_client(monkeypatch, logs_mod, lambda r: httpx.Response(404))
    missing = logs_mod.send_zip("http://meeting.lan", TOKEN, b"x", "b.zip")
    assert not missing.ok and "doesn't accept logs yet" in missing.message and "Save all as .zip" in missing.message
    _mock_client(monkeypatch, logs_mod, lambda r: httpx.Response(405))
    assert "doesn't accept logs yet" in logs_mod.send_zip("http://m", TOKEN, b"x", "b.zip").message
    _mock_client(monkeypatch, logs_mod, lambda r: httpx.Response(403))
    assert "rejected your password" in logs_mod.send_zip("http://m", TOKEN, b"x", "b.zip").message
    assert not logs_mod.send_zip("", TOKEN, b"x", "b.zip").ok


def test_logs_panel_send_reports_a_404_inline(qt_app, home, monkeypatch):
    from meeting_notes.client.ui.logs_dialog import LogsPanel

    _seed_logs(home)
    _mock_client(monkeypatch, logs_mod, lambda r: httpx.Response(404))
    dialog = LogsPanel()
    dialog.send_button.click()
    assert _pump(lambda: "doesn't accept logs yet" in dialog.status_label.text())
    assert dialog.send_button.isEnabled()
    dialog.close()


def test_more_menu_action_opens_logs(qt_app, home, monkeypatch):
    _configure(home, url="")
    window = _window(qt_app, monkeypatch)
    opened = []
    try:
        from meeting_notes.client.ui import main_window as mw

        class FakeDialog:
            def __init__(self, parent=None, page=None):
                opened.append((parent, page))

            def exec(self):
                return 0

        monkeypatch.setattr(mw, "SettingsDialog", FakeDialog)
        assert window.audio_log_button.text() == "Logs..."
        assert window.audio_log_button.isEnabled()
        window.audio_log_button.trigger()
        assert opened == [(window, "logs")]  # Logs is a page of Settings
    finally:
        _close(window)


# -- E. recordings never inside the app folder ---------------------------------------


def test_validate_save_dir_rejects_the_app_folder_case_insensitively(tmp_path):
    app_dir = tmp_path / "Local" / "MeetingNotes"
    folders = [app_dir, tmp_path / "exe dir"]
    assert paths.validate_save_dir(app_dir, folders)
    assert paths.validate_save_dir(str(app_dir).upper() + os.sep + "recordings", folders)
    assert paths.validate_save_dir(tmp_path / "exe dir" / "Meeting Notes", folders)
    assert paths.validate_save_dir(tmp_path / "Local" / "MeetingNotesExtra", folders) is None
    assert paths.validate_save_dir(tmp_path / "Documents" / "Meeting Notes", folders) is None


def test_settings_rejects_a_folder_inside_the_app_folder(qt_app, home, monkeypatch):
    from meeting_notes.client.ui.settings_dialog import SettingsDialog

    app_dir = home / "Local" / "MeetingNotes"
    monkeypatch.setattr(paths, "app_folders", lambda: [app_dir])
    _configure(home)
    dialog = SettingsDialog()
    dialog.save_dir_edit.setText(str(app_dir / "recordings"))
    assert not dialog.folder_error.isHidden()
    assert "installing an update replaces it" in dialog.folder_error.text().replace("\n", " ")
    dialog.accept()
    assert Path(config_mod.load_config()["save_dir"]) == home / "rec"  # nothing was saved
    dialog._save_clicked()
    assert dialog.result() != 1

    dialog.save_dir_edit.setText(str(home / "Documents" / "Meeting Notes"))
    assert dialog.folder_error.isHidden()
    dialog.close()


def test_startup_warns_when_recordings_live_in_the_app_folder_and_moves_on_click(qt_app, home, monkeypatch):
    app_dir = home / "Local" / "MeetingNotes"
    session = app_dir / "recordings" / "2026-09-28-standup"
    session.mkdir(parents=True)
    (session / "mic.wav").write_bytes(b"RIFF" + b"0" * 64)
    monkeypatch.setattr(paths, "app_folders", lambda: [app_dir])
    monkeypatch.setattr(config_mod, "DEFAULT_SAVE_DIR", home / "Meeting Notes")
    config_mod.save_config({"save_dir": str(app_dir / "recordings"), "server": {"url": "", "token": ""}})

    window = _window(qt_app, monkeypatch)
    try:
        assert not window.folder_bar.isHidden()
        assert "inside the app folder" in window.folder_label.text()
        assert window.move_button.text() == "Move recordings"
        assert (session / "mic.wav").exists()  # nothing moves until the click

        window.move_button.click()
        assert _pump(lambda: window.folder_bar.isHidden() and not window._moving_recordings)
        assert (home / "Meeting Notes" / "2026-09-28-standup" / "mic.wav").exists()
        assert not (session / "mic.wav").exists()
        assert Path(config_mod.load_config()["save_dir"]) == home / "Meeting Notes"
    finally:
        _close(window)


def test_move_recordings_keeps_the_originals_when_a_copy_does_not_verify(tmp_path, monkeypatch):
    import shutil

    source = tmp_path / "src"
    (source / "a").mkdir(parents=True)
    (source / "a" / "x.wav").write_bytes(b"12345678")
    real_copy = shutil.copy2

    def bad_copy(src, dst, *a, **k):
        real_copy(src, dst, *a, **k)
        Path(dst).write_bytes(b"12")  # truncated

    monkeypatch.setattr(shutil, "copy2", bad_copy)
    with pytest.raises(OSError):
        paths.move_recordings(source, tmp_path / "dst")
    assert (source / "a" / "x.wav").read_bytes() == b"12345678"


def test_move_recordings_repoints_the_upload_queue(tmp_path):
    source = tmp_path / "src"
    session = source / "2026-09-28-standup"
    session.mkdir(parents=True)
    (session / "mic.wav").write_bytes(b"abcd")
    queue_dir = source / ".upload-queue"
    queue_dir.mkdir()
    (queue_dir / "2026-09-28-standup.json").write_text(
        json.dumps({"session_dir": str(session.resolve()), "attempts": 0}), encoding="utf-8"
    )
    moved = paths.move_recordings(source, tmp_path / "dst")
    assert moved == 2
    state = json.loads((tmp_path / "dst" / ".upload-queue" / "2026-09-28-standup.json").read_text("utf-8"))
    assert Path(state["session_dir"]) == (tmp_path / "dst" / "2026-09-28-standup").resolve()
    assert not source.exists() or not any(source.rglob("*.wav"))


def test_dark_titlebar_helper_is_safe_everywhere(qt_app):
    from PySide6.QtWidgets import QWidget

    from meeting_notes.client.ui.theme import apply_dark_titlebar, install_dark_titlebar

    widget = QWidget()
    install_dark_titlebar(widget)
    widget.show()
    assert apply_dark_titlebar(widget) in (True, False)  # never raises
    widget.close()
