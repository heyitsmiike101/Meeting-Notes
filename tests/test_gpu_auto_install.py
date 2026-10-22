"""GPU support installs and updates itself in the background while the helper is on (client/gpu_auto.py).

Everything is faked: the installer, the server's pack version, the clock. Headless; no network.
"""

from __future__ import annotations

import json
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6", reason="PySide6 not installed")

from meeting_notes import config as config_mod  # noqa: E402
from meeting_notes.client import gpu_auto, gpu_helper, gpu_runtime  # noqa: E402
from tests.test_gpu_helper_client import (  # noqa: E402,F401
    FakeInstaller, FakeProc, _enable_gpu, _install_fake, _pump, _window, home, qt_app,
)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Sup:
    def __init__(self):
        self.stops = 0
        self.texts = []

    def stop(self):
        self.stops += 1

    def set_installing(self, text):
        self.texts.append(text)


class Env:
    def __init__(self, monkeypatch, pack="pack-2"):
        _enable_gpu(monkeypatch)
        self.pack = pack
        self.packs_asked = 0
        self.registered = []
        self.changed = 0
        self.clock = Clock()
        self.sup = Sup()
        self.auto = gpu_auto.GpuAutoInstaller(
            supervisor=self.sup, on_changed=self._changed, installer_factory=self._make,
            pack_status=self._pack, register=lambda url, token: self.registered.append((url, token)),
            clock=self.clock,
        )

    def _changed(self):
        self.changed += 1

    def _make(self, url, token, parent=None):
        return FakeInstaller(url, token, parent)

    def _pack(self, url, token):
        self.packs_asked += 1
        return self.pack

    def settle(self):
        """Let a check's worker thread report back."""
        assert _pump(lambda: not self.auto._checking)

    @property
    def installers(self):
        return FakeInstaller.made


@pytest.fixture
def env(qt_app, home, monkeypatch):
    return Env(monkeypatch)


def test_enabled_and_not_installed_starts_an_install(env):
    assert env.auto.check()
    env.settle()
    assert len(env.installers) == 1 and env.installers[0].started
    assert (env.installers[0].url, env.installers[0].token) == ("http://server.test", "pw")
    assert env.auto.busy
    assert env.sup.texts[-1] == "Installing GPU support..."
    env.installers[0].progress.emit("Downloading", 1, 4)
    assert env.sup.texts[-1] == "Installing GPU support: 25%"
    env.installers[0].finished.emit(True, "GPU support installed (pack abc)")
    assert not env.auto.busy and env.auto.last_ok and env.changed == 1
    assert env.sup.texts[-1] is None


def test_disabled_does_nothing(env):
    config_mod.save_config({"server": {"url": "http://server.test", "token": "pw"}, "gpu_helper": {"enabled": False}})
    assert env.auto.check() is False and env.auto.check(force=True) is False
    assert env.installers == []


def test_no_server_address_does_nothing(env):
    config_mod.save_config({"server": {"url": "", "token": ""}, "gpu_helper": {"enabled": True}})
    assert env.auto.check() is False


def test_a_busy_installer_is_not_started_twice(env):
    env.auto.check()
    env.settle()
    assert env.auto.check() is False and env.auto.check(force=True) is False
    assert len(env.installers) == 1


def test_recording_defers_the_check_until_it_ends(env):
    env.auto.set_recording(True)
    assert env.auto.check() is False
    assert env.installers == []
    env.auto.set_recording(False)  # the skipped check runs now
    env.settle()
    assert len(env.installers) == 1


def test_recording_ending_without_a_skipped_check_starts_nothing(env):
    env.auto.set_recording(True)
    env.auto.set_recording(False)
    assert env.installers == [] and not env.auto._checking


def test_force_ignores_recording(env):
    env.auto.set_recording(True)
    assert env.auto.check(force=True)
    env.settle()
    assert len(env.installers) == 1


def test_installed_and_the_server_pack_differs_updates(env):
    _install_fake(env_root(), "pack-1")
    env.auto.check()
    env.settle()
    assert len(env.installers) == 1 and env.sup.texts[-1] == "Updating GPU support..."
    assert env.packs_asked == 1


def test_the_same_pack_does_nothing(env):
    _install_fake(env_root(), "pack-2")
    config_mod.save_config({"server": {"url": "http://server.test", "token": "pw"},
                            "gpu_helper": {"enabled": True, "key": "mnh_k", "helper_id": "h"}})
    env.auto.check()
    env.settle()
    assert env.installers == [] and env.registered == []


def test_no_pack_on_the_server_yet_does_nothing_and_asks_again_next_time(env):
    _install_fake(env_root(), "pack-1")
    config_mod.save_config({"server": {"url": "http://server.test", "token": "pw"},
                            "gpu_helper": {"enabled": True, "key": "mnh_k", "helper_id": "h"}})
    env.pack = None
    env.auto.check()
    env.settle()
    assert env.installers == []
    env.pack = "pack-2"
    env.auto.check()
    env.settle()
    assert len(env.installers) == 1


def test_failures_back_off_5_minutes_then_30_then_2_hours(env):
    expected = [5 * 60, 30 * 60, 2 * 60 * 60, 2 * 60 * 60]
    for i, delay in enumerate(expected):
        env.auto.check()
        env.settle()
        assert len(env.installers) == i + 1
        env.installers[i].finished.emit(False, "Could not reach the server.")
        assert not env.auto.busy and not env.auto.last_ok and env.auto.last_message == "Could not reach the server."
        assert env.auto.check() is False  # waiting
        env.clock.now += delay - 1
        assert env.auto.check() is False
        env.clock.now += 2
    # a good install resets it
    env.auto.check()
    env.settle()
    env.installers[-1].finished.emit(True, "ok")
    assert env.auto._failures == 0 and env.auto._blocked_until == 0.0


def test_install_now_ignores_the_backoff(env):
    env.auto.check()
    env.settle()
    env.installers[0].finished.emit(False, "boom")
    assert env.auto.check() is False
    assert env.auto.check(force=True)
    env.settle()
    assert len(env.installers) == 2


def test_saving_settings_resets_the_backoff_and_checks_at_once(env):
    env.auto.check()
    env.settle()
    env.installers[0].finished.emit(False, "boom")
    env.auto.settings_changed()
    env.settle()
    assert len(env.installers) == 2


def test_cancel_pauses_automatic_tries_until_settings_change(env):
    env.auto.check()
    env.settle()
    env.auto.cancel()
    assert env.installers[0].cancelled
    env.installers[0].finished.emit(False, "Cancelled. Anything already installed was left as it was.")
    assert env.auto._failures == 0  # a cancel is not a failure
    assert env.auto.check() is False
    env.auto.settings_changed()
    env.settle()
    assert len(env.installers) == 2


def test_cancel_is_overridden_by_install_now(env):
    env.auto.check()
    env.settle()
    env.auto.cancel()
    env.installers[0].finished.emit(False, "Cancelled.")
    assert env.auto.check(force=True)
    env.settle()
    assert len(env.installers) == 2


def test_a_failure_after_the_runtime_was_stopped_still_restarts_it(env):
    env.auto.check()
    env.settle()
    env.installers[0].before_swap()
    assert env.sup.stops == 1
    env.installers[0].finished.emit(False, "swap failed")
    assert env.changed == 1


def test_a_rejected_key_is_cleared_and_the_pc_registers_again(env):
    _install_fake(env_root(), "pack-2")
    config_mod.save_config({"server": {"url": "http://server.test", "token": "pw"},
                            "gpu_helper": {"enabled": True, "key": "mnh_old", "helper_id": "h1",
                                           "instance_id": "inst", "installed_pack_version": "pack-2"}})
    gpu_helper.write_key_file("mnh_old")
    assert env.auto.on_key_rejected()
    group = json.loads(open(os.environ["MEETING_NOTES_CONFIG"], encoding="utf-8").read())["gpu_helper"]
    assert group["key"] is None and group["helper_id"] is None and group["instance_id"] == "inst"
    assert not gpu_helper.key_file_path().exists()
    env.settle()
    assert env.registered == [("http://server.test", "pw")]  # re-registered without downloading the pack again
    assert env.installers == [] and env.changed == 1 and env.auto.last_ok


def test_a_key_the_server_keeps_refusing_is_not_reset_in_a_loop(env):
    _install_fake(env_root(), "pack-2")
    assert env.auto.on_key_rejected()
    env.settle()
    assert env.auto.on_key_rejected() is False
    assert "keeps refusing" in env.auto.last_message
    env.clock.now += 11 * 60
    assert env.auto.on_key_rejected()


def test_the_supervisor_reports_a_rejected_key(qt_app, home, monkeypatch):
    _install_fake(home / "rt")
    config_mod.save_config({"server": {"url": "http://s"}, "gpu_helper": {"enabled": True, "key": "mnh_k"}})
    FakeProc.made = []
    sup = gpu_helper.GpuHelperSupervisor(process_factory=FakeProc, root=home / "rt", server_url=lambda: "http://s")
    seen = []
    sup.key_rejected.connect(lambda: seen.append(1))
    assert sup.start()
    proc = FakeProc.made[-1]
    proc.say(state="error", error="the server rejected this helper's key (revoked or wrong server)")
    proc.exit(gpu_helper.FATAL_EXIT_CODE)
    assert seen == [1]
    # any other fatal exit (bad config) is not a key problem
    sup2 = gpu_helper.GpuHelperSupervisor(process_factory=FakeProc, root=home / "rt", server_url=lambda: "http://s")
    seen2 = []
    sup2.key_rejected.connect(lambda: seen2.append(1))
    sup2.start()
    FakeProc.made[-1].say(state="error", error="no Whisper model in x")
    FakeProc.made[-1].exit(gpu_helper.FATAL_EXIT_CODE)
    assert seen2 == []


def test_the_supervisor_shows_the_install_progress_in_its_status(qt_app, home):
    sup = gpu_helper.GpuHelperSupervisor(root=home / "rt")
    seen = []
    sup.status_changed.connect(lambda st: seen.append(st["text"]))
    sup.set_installing("Installing GPU support: 40%")
    assert sup.status["text"] == "Installing GPU support: 40%" and gpu_helper.current_status_text() == seen[-1]
    sup.set_installing(None)
    assert sup.status["text"] == gpu_helper.TEXT_OFF


def env_root():
    from pathlib import Path

    return Path(os.environ["MEETING_NOTES_GPU_RUNTIME"])


# -- the window ------------------------------------------------------------------------------------------


def test_the_window_checks_on_save_and_when_the_helper_needs_support(qt_app, home, monkeypatch):
    window = _window(qt_app, monkeypatch, home)
    calls = []
    monkeypatch.setattr(window.gpu_auto, "check", lambda force=False: calls.append(("check", force)) or False)
    monkeypatch.setattr(window.gpu_auto, "settings_changed", lambda: calls.append("saved"))
    try:
        window._apply_saved_settings()
        assert "saved" in calls
        calls.clear()
        window._on_gpu_status({"state": gpu_helper.NOT_INSTALLED, "text": "x"})
        window._on_gpu_status({"state": gpu_helper.UPDATE_NEEDED, "text": "x"})
        window._on_gpu_status({"state": gpu_helper.IDLE, "text": "x"})
        assert calls == [("check", False), ("check", False)]
        recorded = []
        monkeypatch.setattr(window.gpu_auto, "set_recording", lambda on: recorded.append(on))
        window._tick()
        assert recorded == [False]
        assert window._gpu_auto_timer.isActive()
    finally:
        window.controller.stop_uploader()


@pytest.mark.parametrize("kind", ["unreachable", "busy"])
def test_a_dropped_download_or_busy_lock_retries_in_5_minutes_without_stepping_up(env, kind):
    """A server restart cutting the download off (or another process installing) is not a real failure."""
    for i in range(3):
        env.auto.check()
        env.settle()
        installer = env.installers[i]
        installer.failure_kind = kind
        installer.finished.emit(False, "Could not reach the server.")
        assert env.auto._failures == 0
        env.clock.now += gpu_auto.TRANSIENT_RETRY_SECONDS - 1
        assert env.auto.check() is False
        env.clock.now += 2
    env.auto.check()
    env.settle()
    assert len(env.installers) == 4
