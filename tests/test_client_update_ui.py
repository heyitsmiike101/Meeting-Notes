"""No automatic client updates; an Update button; the client-version header; HTTP 426."""

from __future__ import annotations

import functools
import json

import httpx
import pytest

from meeting_notes import __version__, config as config_mod
from meeting_notes.client import authcheck, identity, update, version_gate
from meeting_notes.client.api import ServerClient

PySide6 = pytest.importorskip("PySide6", reason="PySide6 not installed")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from meeting_notes.client.controller import IDLE, RECORDING  # noqa: E402
from meeting_notes.client.ui.theme import APP_STYLE  # noqa: E402

HEADER = "X-Meeting-Notes-Client"


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(APP_STYLE)
    return app


def _manifest(version="99.0.0", **extra):
    data = {"version": version, "url": "/install/client-agent.ps1", "size": 1, "sha256": "b" * 64}
    data.update(extra)
    return update.UpdateManifest.from_json(data, "http://meeting.lan")


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
    w._auth_timer.stop()
    w._update_timer.stop()
    w._auth_checker = lambda u, t: authcheck.CheckResult(authcheck.NO_SERVER)
    w.controller.queue_status = lambda: {"pending": 0, "failed": 0, "last_error": ""}
    ran = []
    w._run_async = lambda work, on_done: ran.append(work)  # never actually download anything
    w.async_ran = ran
    yield w
    w._teardown_done = True
    w.close()


# --------------------------------------------------------------------------
# no update is ever applied without a click
# --------------------------------------------------------------------------


def test_auto_update_setting_is_gone_and_old_configs_are_ignored(qt_app, tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "save_dir": str(tmp_path / "R"),
        "server": {"url": "http://meeting.lan", "auto_update": True, "check_updates": True},
    }))
    monkeypatch.setenv("MEETING_NOTES_CONFIG", str(config_path))
    assert "auto_update" not in config_mod.server_settings()

    from meeting_notes.client.ui.settings_dialog import SettingsDialog

    dialog = SettingsDialog()
    assert not hasattr(dialog, "auto_update_check")
    assert dialog.update_check.text().lower().startswith("check the server for client updates")
    dialog.accept()
    assert "auto_update" not in json.loads(config_path.read_text())["server"]


def test_a_config_with_auto_update_true_never_applies_an_update(window, tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({
        "server": {"url": "http://meeting.lan", "auto_update": True},
    }))
    assert not hasattr(window, "_maybe_auto_update")
    window._update_updater = object()  # would blow up if anything tried to use it
    window._on_update_checked(_manifest())
    window.controller.state = IDLE
    window._apply_stopped_ui(None)  # the old auto-apply hook point
    assert window.async_ran == []  # nothing downloaded, nothing launched
    assert window.update_bar.isVisibleTo(window)
    assert window._update_installing is False


def test_update_bar_shows_for_newer_and_offers_the_button(window):
    window._on_update_checked(_manifest("99.0.0"))
    assert window.update_bar.isVisibleTo(window)
    assert window.update_note.text() == "Update available: 99.0.0"
    assert window.update_button.text() == "Update now" and window.update_button.isVisibleTo(window)
    assert not window.whats_new_link.isVisibleTo(window)  # manifest carries no notes


def test_whats_new_link_only_with_notes(window, monkeypatch):
    window._on_update_checked(_manifest(notes_url="/whats-new"))
    assert window.whats_new_link.isVisibleTo(window)
    opened = []
    from meeting_notes.client.ui import main_window as mw

    monkeypatch.setattr(mw.QDesktopServices, "openUrl", lambda url: opened.append(url.toString()))
    window._show_whats_new()
    assert opened == ["http://meeting.lan/whats-new"]

    window._on_update_checked(_manifest(notes="Fixes a crash."))
    assert window.whats_new_link.isVisibleTo(window)
    shown = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a[2]))
    window._show_whats_new()
    assert shown == ["Fixes a crash."]

    window._on_update_checked(_manifest())
    assert not window.whats_new_link.isVisibleTo(window)

    # A notes link pointing somewhere else is dropped, not followed.
    assert _manifest(notes_url="https://elsewhere.example/x").notes_url == ""


def _fake_get(version, **extra):
    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"version": version, "url": "/install/client-agent.ps1", "size": 1,
                    "sha256": "b" * 64, **extra}

    return lambda url, **kwargs: Response()


@pytest.mark.parametrize("remote,shown", [("99.0.0", True), (__version__, False), ("0.0.1", False)])
def test_button_only_for_a_newer_version(monkeypatch, window, remote, shown):
    monkeypatch.setattr(update.httpx, "get", _fake_get(remote))
    manifest = update.ClientUpdater("http://meeting.lan", "t").check()
    assert (manifest is not None) is shown
    window._on_update_checked(manifest)
    assert window.update_bar.isVisibleTo(window) is shown


def test_clicking_update_while_recording_is_blocked_until_it_stops(window, monkeypatch):
    window._update_manifest = _manifest()
    window._update_updater = object()
    window.controller.state = RECORDING
    told = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: told.append(a[1]))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: pytest.fail("no confirmation dialog"))
    window.update_button.click()
    assert told == ["Recording in progress"] and window.async_ran == []
    window.controller.state = IDLE


def test_clicking_update_when_idle_downloads_without_asking(window, monkeypatch):
    window._update_manifest = _manifest()

    class Updater:
        def download(self, manifest):
            return "x.ps1"

        def apply(self, path):
            return None

    window._update_updater = Updater()
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: pytest.fail("no confirmation dialog"))
    window.update_button.click()
    assert len(window.async_ran) == 1 and window._update_installing


# --------------------------------------------------------------------------
# the client-version header
# --------------------------------------------------------------------------


def test_header_value_is_version_and_platform():
    value = identity.client_header_value()
    ver, plat = value.split("; ", 1)
    assert ver == __version__ and plat and plat.isascii()


class Recorder:
    def __init__(self, status=200, body=None):
        self.requests = []
        self.status = status
        self.body = body if body is not None else {"status": "ok", "sessions": [], "total": 0}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json=self.body)


@pytest.fixture
def mock_server(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(
        httpx, "Client", functools.partial(httpx.Client, transport=httpx.MockTransport(rec))
    )
    return rec


def test_every_server_client_request_carries_the_header(mock_server, tmp_path):
    pcm = tmp_path / "t.pcm16"
    pcm.write_bytes(b"\0\0" * 100)
    with ServerClient("http://meeting.lan", "tok") as client:
        client.health()
        client.list_sessions(per_page=1)
        client.upload_track("s1", "mic", pcm, 100)
    assert len(mock_server.requests) == 3
    assert {r.headers.get(HEADER) for r in mock_server.requests} == {identity.client_header_value()}
    assert all(r.headers["authorization"] == "Bearer tok" for r in mock_server.requests)


def test_auth_check_carries_the_header(mock_server):
    assert authcheck.check_connection("http://meeting.lan", "tok").ok
    assert mock_server.requests[0].headers[HEADER] == identity.client_header_value()


def test_updater_carries_the_header(monkeypatch):
    seen = {}

    def fake_get(url, **kwargs):
        seen.update(kwargs)
        return _fake_get("0.0.1")(url)

    monkeypatch.setattr(update.httpx, "get", fake_get)
    update.ClientUpdater("http://meeting.lan", "tok").check()
    assert seen["headers"][HEADER] == identity.client_header_value()


def test_live_stream_handshake_carries_the_header(monkeypatch):
    from meeting_notes.client import streamer

    seen = {}

    def fake_connect(url, **kwargs):
        seen.update(kwargs)
        raise RuntimeError("stop here")

    monkeypatch.setattr(streamer, "ws_connect", fake_connect)
    s = streamer.LiveStreamer(base_url="http://meeting.lan", token="tok", on_partial=lambda p: None)
    with pytest.raises(RuntimeError):
        s._connect_and_pump()
    assert seen["additional_headers"][HEADER] == identity.client_header_value()
    assert seen["additional_headers"]["Authorization"] == "Bearer tok"


# --------------------------------------------------------------------------
# HTTP 426: this version is too old
# --------------------------------------------------------------------------


def test_426_is_remembered_and_cleared_by_a_success(monkeypatch):
    rec = Recorder(426, {"detail": "client too old", "min_client_version": "9.9.9"})
    monkeypatch.setattr(
        httpx, "Client", functools.partial(httpx.Client, transport=httpx.MockTransport(rec))
    )
    with ServerClient("http://meeting.lan", "tok") as client:
        with pytest.raises(httpx.HTTPStatusError):
            client.health()
        gate = version_gate.too_old()
        assert gate["min_version"] == "9.9.9" and gate["detail"] == "client too old"
        rec.status, rec.body = 200, {"status": "ok"}
        client.health()
    assert version_gate.too_old() is None


def test_manifest_min_client_version_above_ours_flags_the_client(monkeypatch):
    monkeypatch.setattr(update.httpx, "get", _fake_get("99.0.0", min_client_version="98.0.0"))
    manifest = update.ClientUpdater("http://meeting.lan", "t").check()
    assert manifest.min_client_version == "98.0.0"
    assert version_gate.too_old()["min_version"] == "98.0.0"
    monkeypatch.setattr(update.httpx, "get", _fake_get("99.0.0", min_client_version="0.0.1"))
    update.ClientUpdater("http://meeting.lan", "t").check()
    assert version_gate.too_old() is None


def test_live_stream_426_is_a_permanent_error(monkeypatch):
    from meeting_notes.client import streamer

    s = streamer.LiveStreamer(base_url="http://meeting.lan", token="t", on_partial=lambda p: None)
    err = s._permanent_error_for_status(426)
    assert err is not None and "no longer supported" in str(err)
    assert version_gate.too_old() is not None


def test_upload_queue_does_not_burn_attempts_on_426():
    from meeting_notes.client.queue import _is_client_too_old

    response = httpx.Response(426, request=httpx.Request("POST", "http://x"))
    assert _is_client_too_old(httpx.HTTPStatusError("426", request=response.request, response=response))
    assert not _is_client_too_old(RuntimeError("nope"))


def test_window_shows_the_red_banner_with_an_update_button(window):
    window._refresh_alerts()
    assert not window.unsupported_bar.isVisibleTo(window)
    version_gate.note_too_old(version_gate.HTTP, "9.9.9", "too old")
    window._update_manifest = _manifest()  # already known, so no lookup is needed
    window._refresh_alerts()
    assert window.unsupported_bar.isVisibleTo(window)
    assert window.unsupported_label.text() == (
        "This version is no longer supported by the server — update to keep uploading"
    )
    assert window.unsupported_button.text() == "Update now"
    window._update_updater = type("U", (), {"download": lambda s, m: "x", "apply": lambda s, p: None})()
    window.unsupported_button.click()
    assert len(window.async_ran) == 1  # the same Update path as the bar's button
    version_gate.clear()
    window._refresh_alerts()
    assert not window.unsupported_bar.isVisibleTo(window)


def test_red_banner_looks_up_the_update_when_none_is_known(window, monkeypatch):
    checks = []
    monkeypatch.setattr(window, "_check_for_update", lambda force=False: checks.append(force))
    version_gate.note_too_old(version_gate.HTTP, "", "")
    window._refresh_alerts()
    window._refresh_alerts()
    assert checks == [True]  # once, forced (works even with the routine check off)
