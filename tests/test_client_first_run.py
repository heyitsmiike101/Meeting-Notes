"""First-run problems on a Mac: no server password, missing macOS permissions, and the retry schedule.

The macOS probes are faked (monkeypatched module functions and ``sys.platform`` where a pure function
checks it); nothing here asks macOS for anything or opens a real System Settings pane.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6", reason="PySide6 not installed")

from PySide6.QtWidgets import QApplication  # noqa: E402

from meeting_notes import config as config_mod  # noqa: E402
from meeting_notes.client import authcheck, permissions  # noqa: E402
from meeting_notes.client.ui import main_window as mw  # noqa: E402
from meeting_notes.client.ui.theme import APP_STYLE  # noqa: E402

URL = "http://meeting.lan"
ERRNO_65 = "GET /v1/sessions failed: [Errno 65] No route to host"


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


def _configure(tmp_path, url=URL, token="", **extra):
    data = {"save_dir": str(tmp_path / "rec"), "server": {"url": url, "token": token}}
    data.update(extra)
    config_mod.save_config(data)


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
    monkeypatch.setattr(window.controller, "queue_status", lambda: {"pending": 0, "failed": 0, "last_error": ""})
    return window


def _close(window):
    window.controller.stop_uploader()
    window.close()


def _perm(key, status, **kw):
    titles = {
        permissions.MICROPHONE: "Microphone",
        permissions.SCREEN: "Screen & System Audio Recording",
        permissions.LOCAL_NETWORK: "Local Network",
    }
    return permissions.Permission(
        key, titles[key], status, steps=["Open it.", "Switch Meeting Notes on."],
        note="why", settings_url=permissions.SETTINGS_URLS[key], **kw,
    )


def _probe(mic=permissions.GRANTED, screen=permissions.GRANTED, net=permissions.UNKNOWN, **flags):
    """A fake permissions.snapshot returning the given statuses; ``state`` can be changed between calls."""
    state = {"mic": mic, "screen": screen, "net": net, "calls": 0}

    def probe(url="", error=""):
        state["calls"] += 1
        return [
            _perm(permissions.MICROPHONE, state["mic"], can_request=flags.get("can_request", False)),
            _perm(permissions.SCREEN, state["screen"], needs_restart=True),
            _perm(permissions.LOCAL_NETWORK, state["net"]),
        ]

    probe.state = state
    return probe


def _mac_window(qt_app, monkeypatch, probe, bundled=False):
    window = _window(qt_app, monkeypatch)
    window._perm_enabled = True
    window._perm_probe = probe
    window._is_bundled = lambda: bundled
    window._refresh_permissions()
    return window


# -- the permissions module -------------------------------------------------------------


def test_snapshot_is_empty_off_macos(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert permissions.snapshot(URL, ERRNO_65) == []


def _fake_mac(monkeypatch, mic, screen):
    monkeypatch.setattr(permissions, "_darwin", lambda: True)
    monkeypatch.setattr(permissions, "microphone_state", lambda: mic)
    monkeypatch.setattr(permissions, "screen_granted", lambda: screen)


@pytest.mark.parametrize(
    "mic_state, expected, can_request",
    [(3, permissions.GRANTED, False), (0, permissions.NOT_GRANTED, True), (2, permissions.NOT_GRANTED, False),
     (1, permissions.NOT_GRANTED, False), (None, permissions.UNKNOWN, False)],
)
def test_microphone_status_mapping(monkeypatch, mic_state, expected, can_request):
    _fake_mac(monkeypatch, mic_state, True)
    mic = permissions.snapshot(URL, "")[0]
    assert (mic.key, mic.status, mic.can_request) == (permissions.MICROPHONE, expected, can_request)


def test_deep_links_and_steps(monkeypatch):
    _fake_mac(monkeypatch, 2, False)
    items = {p.key: p for p in permissions.snapshot(URL, "")}
    assert items[permissions.MICROPHONE].settings_url == (
        "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"
    )
    assert items[permissions.SCREEN].settings_url.endswith("?Privacy_ScreenCapture")
    assert items[permissions.LOCAL_NETWORK].settings_url.endswith("?Privacy_LocalNetwork")
    assert items[permissions.SCREEN].status == permissions.NOT_GRANTED
    assert any("Quit and reopen" in step for step in items[permissions.SCREEN].steps)
    assert any("Switch Meeting Notes on" in step for step in items[permissions.LOCAL_NETWORK].steps)


def test_local_network_needs_errno_65_and_a_lan_host(monkeypatch):
    _fake_mac(monkeypatch, 3, True)
    def net(url, error):
        return permissions.snapshot(url, error)[2].status
    assert net(URL, ERRNO_65) == permissions.NOT_GRANTED
    assert net("http://192.168.1.50:8000", "[Errno 65] No route to host") == permissions.NOT_GRANTED
    assert net("https://notes.example.com", ERRNO_65) == permissions.UNKNOWN  # not a LAN host
    assert net(URL, "connection refused") == permissions.UNKNOWN
    assert net(URL, "") == permissions.UNKNOWN


@pytest.mark.parametrize(
    "url, lan",
    [("http://meeting.lan", True), ("meeting.local", True), ("http://10.1.2.3:8000", True), ("http://192.168.0.2", True),
     ("http://nas", True), ("http://169.254.1.1", True), ("https://example.com", False), ("http://8.8.8.8", False), ("", False)],
)
def test_is_lan_host(url, lan):
    assert permissions.is_lan_host(url) is lan


def test_request_microphone_never_prompts_when_disabled(monkeypatch):
    monkeypatch.setattr(permissions, "_darwin", lambda: True)
    monkeypatch.setenv("MEETING_NOTES_NO_PERMISSION_PROMPT", "1")
    assert permissions.request_microphone() is False


# -- the overlay -------------------------------------------------------------------------


def test_overlay_shows_missing_permissions_with_status_and_steps(qt_app, home, monkeypatch):
    _configure(home, token="x")
    window = _mac_window(qt_app, monkeypatch, _probe(mic=permissions.GRANTED, screen=permissions.NOT_GRANTED))
    try:
        overlay = window.perm_overlay
        assert not overlay.isHidden()
        assert window.perm_bar.isHidden()
        assert overlay.title_label.text() == "Meeting Notes needs a few permissions"
        rows = overlay.rows
        assert rows[permissions.MICROPHONE]["badge"].text() == "Granted"
        assert rows[permissions.MICROPHONE]["steps"] is None  # nothing to do for a granted one
        assert rows[permissions.SCREEN]["badge"].text() == "Not granted"
        assert rows[permissions.SCREEN]["steps"].text() == "1. Open it.\n2. Switch Meeting Notes on."
        assert rows[permissions.SCREEN]["open"].text() == "Open System Settings"
        assert rows[permissions.LOCAL_NETWORK]["badge"].text() == "Unknown"
        assert "microphone" in overlay.intro_label.text()  # mic-only recording is explained
        assert "Screen & System Audio Recording" in window.perm_label.text()
    finally:
        _close(window)


def test_overlay_hidden_when_everything_is_granted_or_not_macos(qt_app, home, monkeypatch):
    _configure(home, token="x")
    window = _mac_window(qt_app, monkeypatch, _probe())
    try:
        assert window.perm_overlay.isHidden() and window.perm_bar.isHidden()
        window._perm_enabled = False
        window._refresh_permissions()
        assert window.perm_overlay.isHidden()
    finally:
        _close(window)
    # A real window on this (non-mac) platform never enables it.
    window = _window(qt_app, monkeypatch)
    try:
        assert window._perm_enabled == (sys.platform == "darwin")
    finally:
        _close(window)


def test_open_system_settings_buttons_use_the_deep_links(qt_app, home, monkeypatch):
    _configure(home, token="x")
    opened = []
    monkeypatch.setattr(mw.QDesktopServices, "openUrl", staticmethod(lambda url: opened.append(url.toString())))
    window = _mac_window(
        qt_app, monkeypatch, _probe(mic=permissions.NOT_GRANTED, screen=permissions.NOT_GRANTED, net=permissions.NOT_GRANTED)
    )
    try:
        for key in (permissions.MICROPHONE, permissions.SCREEN, permissions.LOCAL_NETWORK):
            window.perm_overlay.rows[key]["open"].click()
        assert opened == [permissions.SETTINGS_URLS[k] for k in (
            permissions.MICROPHONE, permissions.SCREEN, permissions.LOCAL_NETWORK)]
    finally:
        _close(window)


def test_allow_microphone_only_when_not_asked_yet(qt_app, home, monkeypatch):
    _configure(home, token="x")
    window = _mac_window(qt_app, monkeypatch, _probe(mic=permissions.NOT_GRANTED, can_request=True))
    asked = []
    monkeypatch.setattr(permissions, "request_microphone", lambda cb=None: asked.append(cb) or True)
    try:
        window.perm_overlay.rows[permissions.MICROPHONE]["allow"].click()
        assert len(asked) == 1
    finally:
        _close(window)
    window = _mac_window(qt_app, monkeypatch, _probe(mic=permissions.NOT_GRANTED, can_request=False))
    try:
        assert window.perm_overlay.rows[permissions.MICROPHONE]["allow"] is None
    finally:
        _close(window)


def test_quit_and_reopen_only_for_the_bundled_app(qt_app, home, monkeypatch):
    _configure(home, token="x")
    window = _mac_window(qt_app, monkeypatch, _probe(screen=permissions.NOT_GRANTED), bundled=False)
    try:
        assert window.perm_overlay.rows[permissions.SCREEN]["restart"] is None
    finally:
        _close(window)
    window = _mac_window(qt_app, monkeypatch, _probe(screen=permissions.NOT_GRANTED), bundled=True)
    launched = []
    monkeypatch.setattr(permissions, "bundle_path", lambda: Path("/Applications/Meeting Notes.app"))
    monkeypatch.setattr(mw.QProcess, "startDetached", staticmethod(lambda prog, args: launched.append((prog, args)) or True))
    closed = []
    real_close = window.close
    monkeypatch.setattr(window, "close", lambda: closed.append(1))
    try:
        window.perm_overlay.rows[permissions.SCREEN]["restart"].click()
        assert launched and launched[0][0] == "/bin/sh"
        assert Path(launched[0][1][-1]) == Path("/Applications/Meeting Notes.app")
        assert "open -n" in launched[0][1][1]
        assert closed == [1]
    finally:
        monkeypatch.setattr(window, "close", real_close)
        _close(window)


def test_not_now_leaves_a_strip_and_fix_brings_the_panel_back(qt_app, home, monkeypatch):
    _configure(home, token="x")
    window = _mac_window(qt_app, monkeypatch, _probe(screen=permissions.NOT_GRANTED))
    try:
        window.perm_overlay.not_now_button.click()
        assert window.perm_overlay.isHidden() and not window.perm_bar.isHidden()
        assert window.perm_button.text() == "Fix"
        window._refresh_permissions()  # still dismissed: a re-read does not nag
        assert window.perm_overlay.isHidden()
        window.perm_button.click()
        assert not window.perm_overlay.isHidden() and window.perm_bar.isHidden()
    finally:
        _close(window)


def test_a_new_missing_permission_reopens_a_dismissed_panel(qt_app, home, monkeypatch):
    _configure(home, token="x")
    probe = _probe(screen=permissions.NOT_GRANTED)
    window = _mac_window(qt_app, monkeypatch, probe)
    try:
        window.perm_overlay.not_now_button.click()
        probe.state["net"] = permissions.NOT_GRANTED
        window._refresh_permissions()
        assert not window.perm_overlay.isHidden()
    finally:
        _close(window)


def test_check_again_closes_the_panel_once_granted_and_rechecks_the_server(qt_app, home, monkeypatch):
    _configure(home, token="x")
    probe = _probe(screen=permissions.NOT_GRANTED)
    window = _mac_window(qt_app, monkeypatch, probe)
    checks = []
    window._auth_checker = lambda url, token: checks.append(url) or authcheck.CheckResult(authcheck.OK, url)
    try:
        window.perm_overlay.check_button.click()
        assert not window.perm_overlay.isHidden()  # still missing
        probe.state["screen"] = permissions.GRANTED
        window.perm_overlay.check_button.click()
        assert window.perm_overlay.isHidden() and window.perm_bar.isHidden()
        deadline = time.monotonic() + 3
        while not checks and time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(0.01)
        assert checks == [URL]
    finally:
        _close(window)


def test_regaining_focus_rereads_the_permissions(qt_app, home, monkeypatch):
    _configure(home, token="x")
    probe = _probe(screen=permissions.NOT_GRANTED)
    window = _mac_window(qt_app, monkeypatch, probe)
    try:
        assert not window.perm_overlay.isHidden()
        probe.state["screen"] = permissions.GRANTED  # switched on in System Settings
        before = probe.state["calls"]
        window._on_window_activated()
        assert probe.state["calls"] == before + 1
        assert window.perm_overlay.isHidden()
    finally:
        _close(window)


def test_a_failed_start_brings_the_panel_back(qt_app, home, monkeypatch):
    _configure(home, token="x")
    window = _mac_window(qt_app, monkeypatch, _probe(mic=permissions.NOT_GRANTED))
    try:
        window.perm_overlay.not_now_button.click()
        assert window.perm_overlay.isHidden()
        monkeypatch.setattr(window.controller, "start", lambda name="", sources=None: None)
        window.controller.error = "mic: not allowed to record"
        window._start()
        assert not window.perm_overlay.isHidden()
    finally:
        _close(window)


def test_errno_65_on_a_lan_server_shows_local_network(qt_app, home, monkeypatch):
    _configure(home, token="x")
    _fake_mac(monkeypatch, 3, True)
    window = _window(qt_app, monkeypatch)
    window._perm_enabled = True
    window._perm_probe = permissions.snapshot
    window._is_bundled = lambda: False
    try:
        window._refresh_permissions()
        assert window.perm_overlay.isHidden()
        window._on_auth_checked(authcheck.CheckResult(authcheck.UNREACHABLE, URL, ERRNO_65))
        assert not window.perm_overlay.isHidden()
        row = window.perm_overlay.rows[permissions.LOCAL_NETWORK]
        assert row["badge"].text() == "Not granted"
        assert "Local Network" in window.perm_label.text()
        # Reaching the server clears it.
        window._on_auth_checked(authcheck.CheckResult(authcheck.OK, URL))
        assert window.perm_overlay.isHidden()
    finally:
        _close(window)


def test_overlay_covers_the_recorder_card_but_not_the_strips(qt_app, home, monkeypatch):
    _configure(home, token="")
    window = _mac_window(qt_app, monkeypatch, _probe(screen=permissions.NOT_GRANTED))
    try:
        window.resize(900, 700)
        window.show()
        QApplication.processEvents()
        window._refresh_alerts()
        QApplication.processEvents()
        overlay = window.perm_overlay
        card_top = window.record_button.mapTo(overlay.parentWidget(), window.record_button.rect().topLeft()).y()
        assert overlay.y() <= card_top
        assert not window.password_bar.isHidden()
        strip_bottom = window.password_bar.mapTo(overlay.parentWidget(), window.password_bar.rect().bottomLeft()).y()
        assert overlay.y() >= strip_bottom - 1
        window.close()
    finally:
        _close(window)


# -- server password -----------------------------------------------------------------------


def test_empty_token_shows_the_password_strip_without_any_network(qt_app, home, monkeypatch):
    _configure(home, token="")
    window = _window(qt_app, monkeypatch)
    try:
        window._refresh_alerts()
        assert not window.password_bar.isHidden()
        assert window.password_label.text() == (
            "Enter the server password to connect. Meetings record but won't upload until it's set."
        )
        assert window.password_button.text() == "Enter password"
        assert window.alert_bar.isHidden()
        # A server that answers without one (no password configured) clears it.
        window._auth_state = "ok"
        window._refresh_alerts()
        assert window.password_bar.isHidden()
    finally:
        _close(window)


def test_no_strip_without_a_server_or_with_a_password(qt_app, home, monkeypatch):
    _configure(home, url="", token="")
    window = _window(qt_app, monkeypatch)
    try:
        window._refresh_alerts()
        assert window.password_bar.isHidden()
    finally:
        _close(window)
    _configure(home, token="secret-pw")
    window = _window(qt_app, monkeypatch)
    try:
        window._refresh_alerts()
        assert window.password_bar.isHidden()
    finally:
        _close(window)


def test_rejection_with_no_password_means_enter_it_not_rejected(qt_app, home, monkeypatch):
    _configure(home, token="")
    window = _window(qt_app, monkeypatch)
    try:
        window._on_auth_checked(authcheck.CheckResult(authcheck.REJECTED, URL, "HTTP 401", 401))
        assert not window.password_bar.isHidden()
        assert window.alert_bar.isHidden()
    finally:
        _close(window)
    _configure(home, token="wrong")
    window = _window(qt_app, monkeypatch)
    try:
        window._on_auth_checked(authcheck.CheckResult(authcheck.REJECTED, URL, "HTTP 401", 401))
        assert window.password_bar.isHidden()
        assert not window.alert_bar.isHidden()
        assert window.alert_label.text().startswith("The server rejected your password.")
    finally:
        _close(window)


def test_control_channel_4401_shows_the_strip_at_once(qt_app, home, monkeypatch):
    _configure(home, token="")
    window = _window(qt_app, monkeypatch)
    try:
        window._remote_bridge.unauthorized.emit()
        QApplication.processEvents()
        assert window._auth_state == "rejected"
        assert not window.password_bar.isHidden()
    finally:
        _close(window)
    _configure(home, token="old")
    window = _window(qt_app, monkeypatch)
    monkeypatch.setattr(QApplication, "alert", staticmethod(lambda *a: None))
    try:
        window._remote_bridge.unauthorized.emit()
        QApplication.processEvents()
        assert not window.alert_bar.isHidden()
    finally:
        _close(window)


def test_the_window_hooks_the_channel_for_4401(qt_app, home, monkeypatch):
    _configure(home, token="x")
    from meeting_notes.client.ui.main_window import MainWindow
    from meeting_notes.audio import devices as devices_mod

    monkeypatch.setattr(devices_mod, "resolve_source", lambda *a, **k: (_ for _ in ()).throw(devices_mod.DeviceNotFound("x")))

    class Channel:
        on_unauthorized = None

        def start(self): ...
        def stop(self, join_timeout=0.3): ...
        def publish(self, snapshot): ...
        def publish_levels(self, levels): ...
        watched = False

    channel = Channel()
    window = MainWindow(remote_channel_factory=lambda on_command: channel)
    window._timer.stop()
    window._auth_timer.stop()
    try:
        assert callable(channel.on_unauthorized)
        channel.on_unauthorized()
        QApplication.processEvents()
        assert window._auth_state == "rejected"
    finally:
        _close(window)


def test_first_run_opens_settings_on_the_password_once(qt_app, home, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_NO_FIRST_RUN_PROMPT", raising=False)
    _configure(home, token="")
    calls = []
    window = _window(qt_app, monkeypatch)
    monkeypatch.setattr(
        window, "_open_settings", lambda page=None, **kw: calls.append((page, kw))
    )
    try:
        window._maybe_first_run_password()
        assert calls == [("server", {"focus_password": True, "first_run": True})]
        assert config_mod.load_config()["password_prompted"] is True
        assert config_mod.load_config()["server"]["url"] == URL  # the rest of the config survives
        window._maybe_first_run_password()  # a later start, or a second call
        assert len(calls) == 1
    finally:
        _close(window)
    window = _window(qt_app, monkeypatch)  # the next launch, token still empty
    monkeypatch.setattr(window, "_open_settings", lambda page=None, **kw: calls.append((page, kw)))
    try:
        window._maybe_first_run_password()
        assert len(calls) == 1
    finally:
        _close(window)


def test_first_run_prompt_skipped_with_a_token_or_no_server(qt_app, home, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_NO_FIRST_RUN_PROMPT", raising=False)
    calls = []
    for url, token in ((URL, "pw"), ("", "")):
        _configure(home, url=url, token=token)
        window = _window(qt_app, monkeypatch)
        monkeypatch.setattr(window, "_open_settings", lambda page=None, **kw: calls.append(page))
        try:
            window._maybe_first_run_password()
        finally:
            _close(window)
    assert calls == []


def test_enter_password_button_opens_settings_on_the_field(qt_app, home, monkeypatch):
    _configure(home, token="")
    window = _window(qt_app, monkeypatch)
    calls = []
    monkeypatch.setattr(window, "_open_settings", lambda page=None, **kw: calls.append((page, kw)))
    try:
        window._refresh_alerts()
        window.password_button.click()
        assert calls == [("server", {"focus_password": True})]
    finally:
        _close(window)


def test_settings_dialog_focuses_the_password_and_explains_it(qt_app, home):
    from meeting_notes.client.ui.settings_dialog import SettingsDialog

    _configure(home, token="")
    dialog = SettingsDialog(page=None, focus_password=True, first_run=True)
    try:
        assert dialog.current_page() == "server"
        dialog.show()
        QApplication.processEvents()
        assert dialog.token_edit.hasFocus() or dialog.focusWidget() is dialog.token_edit
        assert dialog.password_hint.text() == f"Paste the same password you use to sign in at {URL}."
        assert dialog.token_edit.accessibleName() == "Server password"
        assert "token" not in dialog.token_edit.placeholderText().lower()
    finally:
        dialog.close()


def test_saving_a_password_marks_the_prompt_as_done(qt_app, home):
    from meeting_notes.client.ui.settings_dialog import SettingsDialog

    _configure(home, token="")
    dialog = SettingsDialog()
    dialog.token_edit.setText("hunter2")
    dialog.accept()
    cfg = config_mod.load_config()
    assert cfg["server"]["token"] == "hunter2" and cfg["password_prompted"] is True


# -- faster retry after "unreachable" -------------------------------------------------------


def test_unreachable_check_retries_after_10s_then_30s_then_stops(qt_app, home, monkeypatch):
    _configure(home, token="x")
    window = _window(qt_app, monkeypatch)
    unreachable = authcheck.CheckResult(authcheck.UNREACHABLE, URL, ERRNO_65)
    try:
        assert mw.AUTH_RETRY_MS == (10_000, 30_000)
        window._on_auth_checked(unreachable)
        assert window._auth_retry_timer.isActive() and window._auth_retry_timer.interval() == 10_000
        window._auth_retry_timer.stop()
        window._on_auth_checked(unreachable)
        assert window._auth_retry_timer.interval() == 30_000
        window._auth_retry_timer.stop()
        window._on_auth_checked(unreachable)
        assert not window._auth_retry_timer.isActive()  # back to the 5 minute cadence
        # Reaching the server (even with a wrong password) resets the schedule.
        window._on_auth_checked(authcheck.CheckResult(authcheck.OK, URL))
        window._on_auth_checked(unreachable)
        assert window._auth_retry_timer.interval() == 10_000
    finally:
        _close(window)


def test_the_retry_timer_runs_another_check(qt_app, home, monkeypatch):
    _configure(home, token="x")
    window = _window(qt_app, monkeypatch)
    seen = []
    window._auth_checker = lambda url, token: seen.append(url) or authcheck.CheckResult(authcheck.OK, url)
    try:
        window._auth_retry_timer.timeout.emit()
        deadline = time.monotonic() + 3
        while not seen and time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(0.01)
        assert seen == [URL]
    finally:
        _close(window)


def test_a_mac_sh_config_counts_as_a_first_run(qt_app, home, monkeypatch):
    """The exact file install/mac.sh leaves behind: a server URL, nothing else (no token, no flag)."""
    monkeypatch.delenv("MEETING_NOTES_NO_FIRST_RUN_PROMPT", raising=False)
    config_mod.save_config({"server": {"url": URL}})
    calls = []
    window = _window(qt_app, monkeypatch)
    monkeypatch.setattr(window, "_open_settings", lambda page=None, **kw: calls.append((page, kw)))
    try:
        window._refresh_alerts()
        assert not window.password_bar.isHidden()  # the strip is there from the first moment
        window._maybe_first_run_password()
        assert calls == [("server", {"focus_password": True, "first_run": True})]
        assert config_mod.load_config() == {"server": {"url": URL}, "password_prompted": True}
    finally:
        _close(window)


# -- first-run permission prompts (macOS) ----------------------------------------------------


class _FakeMac:
    """Fakes the AVFoundation / CoreGraphics calls and records the order they were made in."""

    def __init__(self, monkeypatch, mic_state=0, screen=False):
        from meeting_notes.audio import devices as devices_mod

        self.calls = []
        self.mic_cb = None
        self.mic_state = mic_state
        self.screen = screen
        self.devices = devices_mod
        monkeypatch.delenv("MEETING_NOTES_NO_PERMISSION_PROMPT", raising=False)
        monkeypatch.delenv("MEETING_NOTES_NO_FIRST_RUN_PROMPT", raising=False)
        monkeypatch.setattr(devices_mod, "_permission_requested", False)
        monkeypatch.setattr(permissions, "microphone_state", lambda: self.mic_state)
        monkeypatch.setattr(permissions, "screen_granted", lambda: self.screen)
        monkeypatch.setattr(permissions, "request_microphone", self._request_mic)
        monkeypatch.setattr(permissions, "request_screen", self._request_screen)

    def _request_mic(self, on_done=None):
        self.calls.append("mic")
        self.mic_cb = on_done
        return True

    def _request_screen(self):
        self.calls.append("screen")
        return True


def _first_run_window(qt_app, monkeypatch, probe, settings_calls):
    window = _window(qt_app, monkeypatch)
    window._perm_enabled = True
    window._perm_probe = probe
    window._is_bundled = lambda: False
    window._first_run_pending = window._perm_first_run_due()
    window._perm_first_run_hold = window._first_run_pending
    window._first_run_gate = False
    monkeypatch.setattr(window, "_open_settings", lambda page=None, **kw: settings_calls.append((page, kw)))
    return window


def test_first_run_asks_mic_then_screen_then_shows_the_panel_then_the_password(qt_app, home, monkeypatch):
    _configure(home, token="")
    fake = _FakeMac(monkeypatch, mic_state=0, screen=False)
    probe = _probe(mic=permissions.NOT_GRANTED, screen=permissions.NOT_GRANTED)
    settings = []
    window = _first_run_window(qt_app, monkeypatch, probe, settings)
    try:
        assert window._first_run_pending and window.perm_overlay.isHidden()  # the panel waits for the prompts
        window._first_run_begin()
        assert fake.calls == ["mic"]  # screen recording waits for the mic answer
        assert config_mod.load_config()["permissions_prompted"] is True  # written before prompting
        assert window.perm_overlay.isHidden() and settings == []
        window._first_run_bridge.done.emit(True)  # the mic answer arrives (marshalled to the GUI thread)
        QApplication.processEvents()
        assert fake.calls == ["mic", "screen"]
        assert fake.devices.permission_requested() is True
        assert not window.perm_overlay.isHidden()  # the panel, with fresh statuses
        assert settings == []  # the password dialog waits for the panel to close
        window._permissions_not_now()
        QApplication.processEvents()
        assert settings == [("server", {"focus_password": True, "first_run": True})]
        assert config_mod.load_config()["password_prompted"] is True
        window._first_run_begin()  # nothing repeats
        window._refresh_permissions()
        QApplication.processEvents()
        assert fake.calls == ["mic", "screen"] and len(settings) == 1
    finally:
        _close(window)


def test_first_run_password_opens_when_the_panel_closes_with_everything_granted(qt_app, home, monkeypatch):
    _configure(home, token="")
    fake = _FakeMac(monkeypatch, mic_state=0, screen=False)
    probe = _probe(mic=permissions.NOT_GRANTED, screen=permissions.NOT_GRANTED)
    settings = []
    window = _first_run_window(qt_app, monkeypatch, probe, settings)
    try:
        window._first_run_begin()
        window._first_run_bridge.done.emit(True)
        QApplication.processEvents()
        assert settings == []
        probe.state["mic"] = permissions.GRANTED
        probe.state["screen"] = permissions.GRANTED
        window._permissions_check_again()
        QApplication.processEvents()
        assert window.perm_overlay.isHidden()
        assert len(settings) == 1
    finally:
        _close(window)


def test_first_run_asks_only_for_what_is_undetermined(qt_app, home, monkeypatch):
    _configure(home, token="")
    fake = _FakeMac(monkeypatch, mic_state=3, screen=True)  # both already granted
    settings = []
    window = _first_run_window(qt_app, monkeypatch, _probe(), settings)
    try:
        window._first_run_begin()
        QApplication.processEvents()
        assert fake.calls == []
        assert len(settings) == 1  # nothing missing: the password dialog follows straight away
    finally:
        _close(window)
    fake = _FakeMac(monkeypatch, mic_state=2, screen=False)  # mic denied (not "not determined"), screen undetermined
    config_mod.save_config({"server": {"url": URL, "token": ""}})
    window = _first_run_window(qt_app, monkeypatch, _probe(mic=permissions.NOT_GRANTED), [])
    try:
        window._first_run_begin()
        assert fake.calls == ["screen"]
    finally:
        _close(window)


def test_first_run_never_prompts_with_the_env_switch(qt_app, home, monkeypatch):
    _configure(home, token="")
    fake = _FakeMac(monkeypatch)
    monkeypatch.setenv("MEETING_NOTES_NO_PERMISSION_PROMPT", "1")
    settings = []
    window = _first_run_window(qt_app, monkeypatch, _probe(mic=permissions.NOT_GRANTED), settings)
    try:
        assert not window._first_run_pending
        window._first_run_begin()
        QApplication.processEvents()
        assert fake.calls == []
        assert "permissions_prompted" not in config_mod.load_config()
        window._refresh_permissions()
        window._permissions_not_now()  # the panel (statuses only, no prompts) closes
        QApplication.processEvents()
        assert len(settings) == 1  # the password step is independent of the permission prompts
    finally:
        _close(window)


def test_recording_start_does_not_prompt_again_after_the_first_run(qt_app, home, monkeypatch):
    from meeting_notes.audio import devices as devices_mod

    _configure(home, token="")
    fake = _FakeMac(monkeypatch, mic_state=3, screen=False)
    window = _first_run_window(qt_app, monkeypatch, _probe(screen=permissions.NOT_GRANTED), [])
    try:
        window._first_run_begin()
        QApplication.processEvents()
        assert fake.calls == ["screen"]
    finally:
        _close(window)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(devices_mod.screencapture_source, "available", lambda: (True, ""))
    monkeypatch.setattr(devices_mod.screencapture_source, "permission_granted", lambda: False)
    asked = []
    monkeypatch.setattr(devices_mod.screencapture_source, "request_permission", lambda: asked.append(1) or True)
    assert devices_mod.prompt_system_permission_once() is False
    assert asked == []


def test_recording_start_still_prompts_once_when_first_run_never_asked(monkeypatch):
    from meeting_notes.audio import devices as devices_mod

    monkeypatch.setattr(devices_mod, "_permission_requested", False)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(devices_mod.screencapture_source, "available", lambda: (True, ""))
    monkeypatch.setattr(devices_mod.screencapture_source, "permission_granted", lambda: False)
    asked = []
    monkeypatch.setattr(devices_mod.screencapture_source, "request_permission", lambda: asked.append(1) or True)
    assert devices_mod.prompt_system_permission_once() is True
    assert devices_mod.prompt_system_permission_once() is False
    assert asked == [1]


def test_upgraded_install_gets_permission_prompts_but_not_the_password_dialog(qt_app, home, monkeypatch):
    # Saved password, older version: no permissions_prompted, password_prompted maybe absent.
    _configure(home, token="pw")
    fake = _FakeMac(monkeypatch, mic_state=0, screen=False)
    settings = []
    window = _first_run_window(
        qt_app, monkeypatch, _probe(mic=permissions.NOT_GRANTED, screen=permissions.NOT_GRANTED), settings
    )
    try:
        window._first_run_begin()
        window._first_run_bridge.done.emit(True)
        QApplication.processEvents()
        assert fake.calls == ["mic", "screen"]
        window._permissions_not_now()
        QApplication.processEvents()
        assert settings == []  # a token is saved
        assert config_mod.load_config()["permissions_prompted"] is True
    finally:
        _close(window)
    # And the next launch does not ask again.
    fake = _FakeMac(monkeypatch, mic_state=0, screen=False)
    window = _first_run_window(qt_app, monkeypatch, _probe(), [])
    try:
        assert not window._first_run_pending
        window._first_run_begin()
        assert fake.calls == []
    finally:
        _close(window)


def test_first_run_continues_if_the_microphone_request_is_never_answered():
    """A completion handler that never fires must not leave first run (panel, password) stuck."""
    from meeting_notes.client.ui import main_window as mw

    assert mw.FIRST_RUN_MIC_TIMEOUT_MS > 0
    source = __import__("inspect").getsource(mw.MainWindow._first_run_begin)
    assert "FIRST_RUN_MIC_TIMEOUT_MS" in source and "_first_run_after_mic" in source
