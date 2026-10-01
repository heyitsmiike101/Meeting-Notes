"""Settings is a sidebar of pages; Logs is one of them. Headless, via Qt's offscreen platform."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6", reason="PySide6 not installed")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from meeting_notes import config as config_mod  # noqa: E402
from meeting_notes.client.ui import settings_dialog as sd  # noqa: E402
from meeting_notes.client.ui.theme import APP_STYLE  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(APP_STYLE)
    return app


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_NOTES_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("MEETING_NOTES_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("MEETING_NOTES_NO_DETECT", "1")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def _dialog(**kwargs):
    dialog = sd.SettingsDialog(**kwargs)
    # Never touch the disk scan or the network from a test.
    dialog.folder_stats = lambda folder: {"count": 0, "bytes": 0}
    return dialog


def test_sidebar_lists_the_pages_in_order_and_switches(qt_app, home):
    dialog = _dialog()
    keys = ["general", "audio", "recordings", "server", "remote", "logs", "about"]
    assert dialog.page_keys() == keys
    assert [dialog.nav.item(i).text() for i in range(dialog.nav.count())] == [
        "General", "Audio", "Recordings", "Server", "Remote control", "Logs", "About",
    ]
    assert dialog.current_page() == "general"
    seen = set()
    for key in keys:
        dialog.show_page(key)
        assert dialog.current_page() == key
        current = dialog.pages.currentWidget()
        assert current is dialog._page_widgets[key]
        seen.add(id(current))
    assert len(seen) == len(keys)  # every row shows its own page
    dialog.close()


def test_keyboard_navigation_moves_between_pages(qt_app, home):
    dialog = _dialog()
    dialog.show()
    assert dialog.nav.focusPolicy() != Qt.NoFocus  # reachable by Tab, driven by the arrow keys
    QTest.keyClick(dialog.nav, Qt.Key_Down)
    assert dialog.current_page() == "audio"
    QTest.keyClick(dialog.nav, Qt.Key_End)
    assert dialog.current_page() == "about"
    QTest.keyClick(dialog.nav, Qt.Key_Home)
    assert dialog.current_page() == "general"
    dialog.close()


def _page_of(dialog, widget) -> str:
    """The key of the page whose widget tree contains ``widget``."""
    for key, page in dialog._page_widgets.items():
        node = widget
        while node is not None:
            if node is page:
                return key
            node = node.parentWidget()
    raise AssertionError(f"{widget!r} is on no page")


def test_every_setting_still_exists_on_a_sensible_page(qt_app, home):
    dialog = _dialog()
    expected = {
        "appearance_combo": "general",
        "detect_check": "general",
        "auto_stop_check": "general",
        "suggest_stop_check": "general",
        "levels_check": "audio",
        "save_dir_edit": "recordings",
        "folder_error": "recordings",
        "retention_combo": "recordings",
        "cleanup_button": "recordings",
        "local_stats_label": "recordings",
        "cleanup_result": "recordings",
        "url_edit": "server",
        "token_edit": "server",
        "test_button": "server",
        "result_label": "server",
        "live_check": "server",
        "upload_check": "server",
        "update_check": "server",
        "remote_check": "remote",
        "logs_panel": "logs",
    }
    for name, page in expected.items():
        widget = getattr(dialog, name)
        assert isinstance(widget, QWidget), name
        assert _page_of(dialog, widget) == page, name
    assert dialog.about_labels["version"].text()
    dialog.close()


def test_changes_on_every_page_are_saved_together(qt_app, home):
    config_path = home / "config.json"
    dialog = _dialog()
    dialog.appearance_combo.setCurrentIndex(dialog.appearance_combo.findData("dark"))
    dialog.detect_check.setChecked(False)
    dialog.auto_stop_check.setChecked(False)
    dialog.suggest_stop_check.setChecked(False)
    dialog.levels_check.setChecked(False)
    dialog.save_dir_edit.setText(str(home / "Where"))
    dialog.retention_combo.setCurrentIndex(dialog.retention_combo.findData(30))
    dialog.url_edit.setText("http://10.0.0.5:8000/")
    dialog.token_edit.setText("tok")
    dialog.live_check.setChecked(False)
    dialog.upload_check.setChecked(False)
    dialog.update_check.setChecked(False)
    dialog.remote_check.setChecked(True)
    dialog.show_page("server")
    dialog.accept()

    saved = json.loads(config_path.read_text())
    assert saved["appearance"] == "dark"
    assert saved["meeting_detection"]["enabled"] is False
    assert saved["meeting_detection"]["auto_stop"] is False
    assert saved["meeting_detection"]["suggest_stop"] is False
    assert saved["show_audio_levels"] is False
    assert saved["save_dir"] == str(home / "Where")
    assert saved["local_retention_days"] == 30
    assert saved["server"] == {
        "url": "http://10.0.0.5:8000",
        "token": "tok",
        "live_preview": False,
        "auto_upload": False,
        "check_updates": False,
    }
    assert saved["remote_control_allowed"] is True
    assert saved["settings_page"] == "server"


def test_cancel_changes_nothing_but_remembers_the_page(qt_app, home):
    config_mod.save_config({"save_dir": str(home / "rec"), "server": {"url": "http://a", "token": "t"}})
    dialog = _dialog()
    dialog.url_edit.setText("http://changed")
    dialog.show_page("recordings")
    dialog.reject()
    saved = json.loads((home / "config.json").read_text())
    assert saved["server"]["url"] == "http://a"
    assert saved["settings_page"] == "recordings"
    again = _dialog()
    assert again.current_page() == "recordings"
    again.close()


def test_opens_on_the_requested_page_and_ignores_an_unknown_one(qt_app, home):
    logs = _dialog(page="logs")
    assert logs.current_page() == "logs"
    logs.close()
    odd = _dialog(page="nonsense")
    assert odd.current_page() == "general"
    odd.close()


def test_logs_page_has_the_log_viewer_and_actions(qt_app, home):
    dialog = _dialog(page="logs")
    panel = dialog.logs_panel
    assert panel.source_keys() == ["client", "startup", "audio", "queue", "config", "about"]
    for button in (panel.refresh_button, panel.copy_button, panel.folder_button, panel.zip_button, panel.send_button):
        assert button.text()
    assert not panel.send_button.isEnabled()  # no server configured
    # Saving a server URL elsewhere is picked up when the page is shown again.
    config_mod.save_config({"server": {"url": "http://meeting.lan", "token": ""}})
    dialog.show_page("about")
    dialog.show_page("logs")
    assert panel.send_button.isEnabled()
    dialog.close()


def test_a_bad_save_folder_brings_the_recordings_page_forward(qt_app, home, monkeypatch):
    from meeting_notes.client import paths

    app_dir = home / "Local" / "MeetingNotes"
    monkeypatch.setattr(paths, "app_folders", lambda: [app_dir])
    dialog = _dialog()
    dialog.show_page("server")
    dialog.save_dir_edit.setText(str(app_dir / "recordings"))
    dialog.accept()
    assert dialog.current_page() == "recordings"
    assert not dialog.folder_error.isHidden()
    dialog.close()


def test_test_connection_and_save_anyway_still_work_from_the_server_page(qt_app, home):
    from meeting_notes.client import authcheck

    dialog = _dialog(page="server")
    dialog.checker = lambda url, token: authcheck.CheckResult(authcheck.REJECTED, url, "HTTP 403", 403)
    dialog.url_edit.setText("http://meeting.lan")
    dialog.token_edit.setText("wrong")
    dialog._save_clicked()
    deadline_ok = False
    for _ in range(300):
        QApplication.processEvents()
        if dialog.save_button.text() == "Save anyway":
            deadline_ok = True
            break
        QTest.qWait(10)
    assert deadline_ok
    dialog._save_clicked()
    assert config_mod.server_settings()["token"] == "wrong"
    dialog.close()


def test_refitting_a_result_label_does_not_make_it_taller_each_time(qt_app, home):
    dialog = _dialog(page="recordings")
    dialog.show()
    text = "Moved 12 recordings to the Recycle Bin, freeing 1.4 GB. Kept: 3 still waiting to upload."
    dialog._show_cleanup(text, "ok")
    first = dialog.cleanup_result.height()
    for _ in range(3):
        dialog._show_cleanup(text, "ok")
    assert dialog.cleanup_result.height() == first
    dialog.close()
