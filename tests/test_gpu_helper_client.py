"""The Windows client's side of the GPU helper: supervisor, Start at login, single instance, Settings page, window.

Everything is faked: the runtime process, the registry, the tray. Headless (Qt offscreen); no network.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6", reason="PySide6 not installed")

from PySide6.QtCore import QObject, QProcess, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from meeting_notes import config as config_mod  # noqa: E402
from meeting_notes import remote  # noqa: E402
from meeting_notes.client import authcheck, autostart, gpu_helper, gpu_runtime  # noqa: E402
from meeting_notes.client import settings_schema as S  # noqa: E402
from meeting_notes.client.single_instance import SingleInstance  # noqa: E402
from meeting_notes.client.ui import settings_dialog as sd  # noqa: E402
from meeting_notes.client.ui.theme import APP_STYLE  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(APP_STYLE)
    return app


@pytest.fixture
def home(tmp_path, monkeypatch, qt_app):
    monkeypatch.setenv("MEETING_NOTES_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("MEETING_NOTES_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("MEETING_NOTES_GPU_RUNTIME", str(tmp_path / "rt"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(gpu_runtime, "supported", lambda platform=None: (platform or "win32") == "win32")
    from meeting_notes.client.ui import theme

    monkeypatch.setattr(theme, "apply_appearance", lambda *_a, **_k: None)
    return tmp_path


def _pump(condition, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        QApplication.processEvents()
        time.sleep(0.01)
    return condition()


def _install_fake(root: Path, version="pack-1") -> None:
    cur = root / "current"
    gpu_runtime.venv_python(cur).parent.mkdir(parents=True)
    gpu_runtime.venv_python(cur).write_text("py")
    (cur / "helper-code.zip").write_bytes(b"zip")
    (cur / "models").mkdir()
    (cur / "pack.json").write_text(json.dumps({"pack_version": version}))


# -- the supervisor -------------------------------------------------------------------------------------


class FakeProc(QObject):
    readyReadStandardOutput = Signal()
    readyReadStandardError = Signal()
    finished = Signal(int, QProcess.ExitStatus)
    errorOccurred = Signal(QProcess.ProcessError)

    made = []

    def __init__(self):
        super().__init__()
        self.running = False
        self.program = None
        self.args = []
        self.env = None
        self.written = []
        self.out = b""
        self.killed = False
        FakeProc.made.append(self)

    def setProcessEnvironment(self, env):
        self.env = env

    def setProgram(self, program):
        self.program = program

    def setArguments(self, args):
        self.args = list(args)

    def start(self):
        self.running = True

    def state(self):
        return QProcess.Running if self.running else QProcess.NotRunning

    def write(self, data):
        self.written.append(json.loads(bytes(data).decode()))

    def closeWriteChannel(self):
        pass

    def waitForFinished(self, _ms):
        self.running = False
        return True

    def kill(self):
        self.killed = True
        self.running = False

    def readAllStandardOutput(self):
        out, self.out = self.out, b""
        return out

    def readAllStandardError(self):
        return b""

    # test helpers
    def say(self, **event):
        self.out += (json.dumps({"event": "status", **event}) + "\n").encode()
        self.readyReadStandardOutput.emit()

    def exit(self, code=1):
        self.running = False
        self.finished.emit(code, QProcess.NormalExit)

    def commands(self):
        return [w["cmd"] for w in self.written]


@pytest.fixture
def sup(home, monkeypatch):
    # Commands reach the runtime through the control file (compute_helper/ipc.py); record each change as the
    # command it means, on the newest fake process, so tests read like the protocol.
    state = {"paused": False, "recording": False}

    def record_control(path, *, paused, stop=False, recording=False):
        proc = FakeProc.made[-1] if FakeProc.made else None
        if proc is None:
            return
        if stop:
            proc.written.append({"cmd": "stop"})
            return
        if paused != state["paused"]:
            proc.written.append({"cmd": "pause" if paused else "resume"})
        if recording != state["recording"]:
            proc.written.append({"cmd": "record" if recording else "unrecord"})
        state["paused"], state["recording"] = paused, recording

    monkeypatch.setattr(gpu_helper, "write_control", record_control)
    _install_fake(home / "rt")
    config_mod.save_config({
        "server": {"url": "http://server.test", "token": "main-token"},
        "gpu_helper": {"enabled": True, "key": "mnh_key", "helper_id": "h1", "installed_pack_version": "pack-1"},
    })
    FakeProc.made = []
    s = gpu_helper.GpuHelperSupervisor(process_factory=FakeProc, root=home / "rt")
    s.events = []
    s.status_changed.connect(lambda st: s.events.append(st["text"]))
    return s


def test_start_runs_the_runtime_with_the_helper_key_and_code_on_the_path(sup, home):
    assert sup.start() is True
    proc = FakeProc.made[0]
    cur = home / "rt" / "current"
    assert proc.program == str(gpu_runtime.venv_python(cur))
    a = proc.args
    assert a[:4] == ["-m", "meeting_notes.compute_helper", "run", "--server"] and a[4] == "http://server.test"
    assert a[a.index("--models") + 1] == str(cur / "models")
    assert a[a.index("--pack-version") + 1] == "pack-1"
    key_file = Path(a[a.index("--key-file") + 1])
    assert key_file.parent == home and key_file.read_text() == "mnh_key"
    assert proc.env.value("PYTHONPATH") == str(cur / "helper-code.zip")
    assert "main-token" not in " ".join(a) and "main-token" not in (proc.env.toStringList().__repr__())
    assert sup.events == ["Starting"] and sup.running


def test_not_installed_says_so_and_starts_nothing(sup, home):
    import shutil

    shutil.rmtree(home / "rt")
    assert sup.start() is False
    assert FakeProc.made == [] and sup.status["text"] == "GPU support is not installed"


def test_status_lines_become_the_texts_the_server_uses(sup):
    sup.start()
    proc = FakeProc.made[0]
    proc.say(state="idle")
    proc.say(state="busy", job="j1", board="M-0142", progress=0.42)
    proc.say(state="offline")
    proc.say(state="update needed")
    proc.say(state="error", error="CUDA out of memory")
    assert sup.events == ["Starting", "Idle", "Working on M-0142 · 42%", "Offline", "Update GPU support",
                          "Error: CUDA out of memory"]
    assert gpu_helper.current_status_text() == "Error: CUDA out of memory"


def test_a_partial_line_waits_for_its_newline(sup):
    sup.start()
    proc = FakeProc.made[0]
    line = json.dumps({"event": "status", "state": "idle"}).encode()
    proc.out = line[:10]
    proc.readyReadStandardOutput.emit()
    assert sup.events == ["Starting"]
    proc.out = line[10:] + b"\nnot json\n"
    proc.readyReadStandardOutput.emit()
    assert sup.events[-1] == "Idle"


def test_recording_pauses_the_helper_and_the_end_resumes_it(sup):
    sup.start()
    proc = FakeProc.made[0]
    proc.say(state="idle")
    sup.set_recording(True)
    sup.set_recording(True)  # a repeat sends nothing
    assert proc.commands() == ["record"] and sup.status["text"] == "Paused while recording"
    proc.say(state="paused")
    assert sup.status["text"] == "Paused while recording"
    sup.set_recording(False)
    assert proc.commands() == ["record", "unrecord"] and sup.status["text"] == "Idle"


def test_a_runtime_that_starts_while_recording_is_paused_at_once(sup):
    sup.set_recording(True)
    sup.start()
    proc = FakeProc.made[0]
    proc.say(state="idle")
    assert proc.commands() == ["record"] and sup.status["text"] == "Paused while recording"


def test_the_tray_pause_and_a_recording_do_not_undo_each_other(sup):
    sup.start()
    proc = FakeProc.made[0]
    proc.say(state="idle")
    sup.set_user_paused(True)
    assert sup.status["text"] == "Paused"
    sup.set_recording(True)
    sup.set_recording(False)
    assert proc.commands() == ["pause", "record", "unrecord"] and sup.paused  # still paused by the person
    sup.set_user_paused(False)
    assert proc.commands() == ["pause", "record", "unrecord", "resume"] and sup.status["text"] == "Idle"


def test_the_control_file_carries_recording_apart_from_the_tray_pause(home):
    from meeting_notes.compute_helper.ipc import read_control, write_control

    path = home / "control.json"
    write_control(path, paused=False, recording=True)
    assert read_control(path) == {"paused": False, "recording": True, "stop": False}
    write_control(path, paused=True)
    assert read_control(path) == {"paused": True, "recording": False, "stop": False}


def test_recording_writes_recording_and_the_tray_pause_writes_paused(sup, home, monkeypatch):
    from meeting_notes.compute_helper import ipc

    monkeypatch.setattr(gpu_helper, "write_control", ipc.write_control)
    sup.start()
    path = sup._control_path()
    assert ipc.read_control(path) == {"paused": False, "recording": False, "stop": False}
    sup.set_recording(True)
    assert ipc.read_control(path)["recording"] is True and ipc.read_control(path)["paused"] is False
    sup.set_user_paused(True)
    assert ipc.read_control(path)["recording"] is True and ipc.read_control(path)["paused"] is True
    sup.set_recording(False)
    assert ipc.read_control(path)["recording"] is False and ipc.read_control(path)["paused"] is True


def test_the_status_text_gains_the_live_part(sup):
    sup.start()
    proc = FakeProc.made[0]
    on = {"connected": True, "served": 3, "avg_ms": 400, "error": None}
    off = {**on, "connected": False}
    proc.say(state="idle", live=on)
    assert sup.status["text"] == "Idle · Live: on"
    proc.say(state="busy", job="j1", board="M-0142", progress=0.42, live=on)
    assert sup.status["text"] == "Working on M-0142 · 42% · Live: on"
    proc.say(state="idle", live=off)
    assert sup.status["text"] == "Idle · Live: off"
    sup.set_recording(True)
    proc.say(state="paused", live=on)
    assert sup.status["text"] == "Paused while recording · Live: on"
    assert gpu_helper.current_status_text() == "Paused while recording · Live: on"
    proc.say(state="offline", live=on)
    assert sup.status["text"] == "Offline"
    sup.set_recording(False)
    proc.say(state="idle")  # an older runtime reports no live part
    assert sup.status["text"] == "Idle"


def test_a_crash_restarts_after_5_then_30_then_120_seconds_and_a_long_run_resets(sup):
    sup.start()
    delays = []
    for _ in range(4):
        FakeProc.made[-1].exit(1)
        delays.append(sup.restart_delay_ms)
        assert sup.status["text"] == "Offline"
        sup._on_restart_timer()
    assert delays == [5000, 30000, 120000, 120000] and len(FakeProc.made) == 5
    sup._started_at = time.monotonic() - 3600  # the last run was healthy
    FakeProc.made[-1].exit(1)
    assert sup.restart_delay_ms == 5000


def test_a_fatal_exit_code_does_not_restart_loop(sup):
    sup.start()
    proc = FakeProc.made[0]
    proc.say(state="error", error="The server refused this helper key")
    proc.exit(2)
    assert sup.restart_delay_ms is None and not sup._timer.isActive()
    assert sup.status["state"] == "error" and "refused" in sup.status["text"]
    assert len(FakeProc.made) == 1 and not sup.wanted


def test_stop_asks_nicely_does_not_restart_and_reports_off(sup):
    sup.start()
    proc = FakeProc.made[0]
    sup.stop()
    assert proc.commands() == ["stop"] and sup.status["text"] == "Off" and not sup.running
    proc.exit(0)  # a late exit signal changes nothing
    assert sup.restart_delay_ms is None and len(FakeProc.made) == 1


# -- Start at login ---------------------------------------------------------------------------------------


class FakeKey:
    def __init__(self, store):
        self.store = store

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeReg:
    HKEY_CURRENT_USER = "HKCU"
    KEY_READ, KEY_SET_VALUE, REG_SZ = 1, 2, 1

    def __init__(self):
        self.values = {}
        self.keys = []

    def OpenKey(self, root, path, reserved=0, access=0):
        assert root == "HKCU" and path == autostart.RUN_KEY
        return FakeKey(self.values)

    CreateKeyEx = OpenKey

    def SetValueEx(self, key, name, reserved, kind, value):
        key.store[name] = value

    def QueryValueEx(self, key, name):
        if name not in key.store:
            raise FileNotFoundError(name)
        return key.store[name], 1

    def DeleteValue(self, key, name):
        if name not in key.store:
            raise FileNotFoundError(name)
        del key.store[name]


def test_autostart_writes_the_run_value_for_the_packaged_exe():
    reg = FakeReg()
    exe = str(Path(r"C:/Users/me/AppData/Local/MeetingNotes/MeetingNotes.exe").resolve())
    assert autostart.set_enabled(reg, packaged=True, exe=exe) is True
    assert reg.values == {"MeetingNotes": f'"{exe}" --tray'}
    assert autostart.is_set(reg) is True
    assert autostart.remove(reg) is True and reg.values == {} and autostart.is_set(reg) is False
    assert autostart.remove(reg) is True  # already gone is fine


def test_autostart_does_nothing_for_a_source_run_or_without_a_registry():
    reg = FakeReg()
    assert autostart.set_enabled(reg, packaged=False) is False and reg.values == {}
    # (never call it with registry=None here: on Windows that is the real Run key)
    assert autostart.is_set(None) is False or sys.platform == "win32"


def test_autostart_follows_the_settings():
    reg = FakeReg()
    autostart.apply(True, True, reg, packaged=True, exe=r"C:/Apps/MeetingNotes.exe")
    assert "MeetingNotes" in reg.values
    autostart.apply(True, False, reg)
    assert reg.values == {}
    autostart.apply(True, True, reg, packaged=True, exe=r"C:/Apps/MeetingNotes.exe")
    autostart.apply(False, True, reg)
    assert reg.values == {}


def test_a_source_run_never_touches_the_real_run_key(monkeypatch):
    monkeypatch.setattr(autostart, "is_packaged", lambda: False)
    monkeypatch.setattr(autostart, "_registry", lambda: pytest.fail("touched the registry"))
    autostart.apply(True, True)
    autostart.apply(False, False)


# -- single instance --------------------------------------------------------------------------------------


def _second_launch(name: str, message: str) -> "subprocess.Popen":
    """Another process doing what ``app.main`` does on a second launch; prints whether it became the first."""
    code = "; ".join([
        "from PySide6.QtCore import QCoreApplication",
        "from meeting_notes.client import single_instance as si",
        "app = QCoreApplication([])",
        f"first = si.SingleInstance({name!r}).acquire(si.{message})",
        "print('first' if first else 'second')",
    ])
    root = str(Path(__file__).resolve().parents[1])
    env = {**os.environ, "PYTHONPATH": root}
    return subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True, env=env, cwd=root)


def _finish(proc, timeout=15.0) -> str:
    assert _pump(lambda: proc.poll() is not None, timeout), "the second launch did not finish"
    return proc.stdout.read().strip()


def test_a_second_launch_activates_the_first_and_exits(qt_app):
    name = "MeetingNotesTest-" + uuid.uuid4().hex[:8]
    first = SingleInstance(name)
    try:
        assert first.acquire() is True
        woke = []
        first.activate_requested.connect(lambda: woke.append(1))
        assert _finish(_second_launch(name, "ACTIVATE")) == "second"  # told, and exits
        assert _pump(lambda: woke == [1])
    finally:
        first.release()


def test_a_second_launch_with_tray_does_not_raise_the_window(qt_app):
    name = "MeetingNotesTest-" + uuid.uuid4().hex[:8]
    first = SingleInstance(name)
    try:
        assert first.acquire() is True
        woke = []
        first.activate_requested.connect(lambda: woke.append(1))
        assert _finish(_second_launch(name, "PING")) == "second"
        _pump(lambda: False, 0.3)
        assert woke == []
    finally:
        first.release()


def test_the_first_launch_after_a_crash_takes_over_a_stale_socket(qt_app):
    name = "MeetingNotesTest-" + uuid.uuid4().hex[:8]
    one = SingleInstance(name)
    assert one.acquire() is True
    one.release()
    two = SingleInstance(name)
    try:
        assert two.acquire() is True
    finally:
        two.release()


class _FakeServer:
    """Stands in for QLocalServer: ``listens`` is a list of listen() results, consumed in order."""

    listens: list = []
    removed: list = []

    def __init__(self, parent=None):
        self.newConnection = SimpleNamespace(connect=lambda *_a: None)

    @staticmethod
    def removeServer(name):
        _FakeServer.removed.append(name)

    def setSocketOptions(self, *_a):
        pass

    def listen(self, name):
        return _FakeServer.listens.pop(0)

    def errorString(self):
        return "boom"

    def setParent(self, _p):
        pass

    def deleteLater(self):
        pass

    UserAccessOption = 1


def _fake_listen(monkeypatch, listens, probes):
    from meeting_notes.client import single_instance as si

    _FakeServer.listens, _FakeServer.removed = list(listens), []
    monkeypatch.setattr(si, "QLocalServer", _FakeServer)
    answers = list(probes)
    sent = []
    monkeypatch.setattr(SingleInstance, "notify_running", lambda self, message=b"": (sent.append(message), answers.pop(0))[1])
    return sent


def test_a_failed_listen_that_finds_the_winner_behaves_as_a_second_instance(qt_app, monkeypatch):
    sent = _fake_listen(monkeypatch, [False], [False, True])  # no answer first, then the racing first instance answers
    one = SingleInstance("MeetingNotesTest-race")
    assert one.acquire() is False
    assert len(sent) == 2 and _FakeServer.removed == []  # never removed a live server's socket


def test_a_failed_listen_with_nobody_there_removes_the_stale_socket_and_retries_once(qt_app, monkeypatch):
    _fake_listen(monkeypatch, [False, True], [False, False])
    one = SingleInstance("MeetingNotesTest-stale")
    assert one.acquire() is True
    assert _FakeServer.removed == ["MeetingNotesTest-stale"] and one._server is not None
    one._server = None


def test_the_app_still_starts_when_listening_keeps_failing(qt_app, monkeypatch):
    # A recorder that won't open is worse than a rare second copy: start unguarded.
    _fake_listen(monkeypatch, [False, False], [False, False])
    one = SingleInstance("MeetingNotesTest-broken")
    assert one.acquire() is True and one._server is None
    assert _FakeServer.removed == ["MeetingNotesTest-broken"] and _FakeServer.listens == []


# -- the Settings page ------------------------------------------------------------------------------------


def test_the_gpu_helper_page_sits_after_server_with_the_planned_items():
    schema = S.build_schema(platform="win32")
    keys = [p["key"] for p in schema["pages"]]
    assert keys.index("gpu_helper") == keys.index("server") + 1
    page = next(p for p in schema["pages"] if p["key"] == "gpu_helper")
    assert page["title"] == "GPU helper" and not page.get("local_only")
    fields = {i["key"]: i for i in page["items"] if i["type"] == "field"}
    assert list(fields) == ["gpu_helper.enabled", "gpu_helper.gpu", "gpu_helper.runtime",
                            "gpu_helper.start_at_login", "gpu_helper.status"]
    assert fields["gpu_helper.enabled"]["label"] == "Let the server use this computer's graphics card"
    assert fields["gpu_helper.start_at_login"]["label"] == "Start when I sign in to Windows"
    assert fields["gpu_helper.start_at_login"]["enabled_if"] == [{"key": "gpu_helper.enabled", "eq": True}]
    assert [fields[k]["kind"] for k in fields] == ["bool", "readonly", "readonly", "bool", "readonly"]
    assert all(remote.valid_setting_key(k) for k in fields)


def test_other_platforms_get_only_the_not_supported_message():
    page = next(p for p in S.build_schema(platform="darwin")["pages"] if p["key"] == "gpu_helper")
    assert [i["type"] for i in page["items"]] == ["note"]
    assert page["items"][0]["text"] == "GPU helper is available on Windows PCs with an NVIDIA graphics card."


def test_the_remote_copy_of_the_page_survives_the_sanitizer():
    schema = S.remote_schema(S.build_schema(platform="win32"))
    assert remote.sanitize_settings_schema(schema) == schema
    assert "gpu_helper" in [p["key"] for p in schema["pages"]]


def test_the_helper_settings_default_off_and_are_strictly_typed():
    group = config_mod.gpu_helper_settings({})
    assert group["enabled"] is False and group["start_at_login"] is True
    assert group["helper_id"] is None and group["key"] is None and group["installed_pack_version"] is None
    assert config_mod.gpu_helper_settings({"gpu_helper": {"enabled": "yes", "start_at_login": 0}})["enabled"] is False
    assert config_mod.gpu_helper_settings({"gpu_helper": {"start_at_login": False}})["start_at_login"] is False
    assert config_mod.gpu_helper_settings({"gpu_helper": {"key": 7}})["key"] is None


def test_saving_the_switch_keeps_the_key_and_pack_version(home):
    config_mod.save_config({"gpu_helper": {"key": "mnh_k", "helper_id": "h", "installed_pack_version": "p"}})
    result = S.apply_changes({"gpu_helper.enabled": True, "gpu_helper.start_at_login": False})
    assert result.saved == ["gpu_helper.enabled", "gpu_helper.start_at_login"] and result.errors == {}
    group = json.loads((home / "config.json").read_text())["gpu_helper"]
    assert group == {"enabled": True, "start_at_login": False, "helper_id": "h", "key": "mnh_k",
                     "installed_pack_version": "p"}
    assert S.get_values()["gpu_helper.enabled"] is True
    assert "mnh_k" not in json.dumps(S.settings_payload(note_types=[], server_default_note_type="", devices={}))


def test_a_remote_change_must_be_a_real_boolean(home):
    assert "gpu_helper.enabled" in S.apply_changes({"gpu_helper.enabled": "yes"}).errors
    assert "gpu_helper.status" in S.apply_changes({"gpu_helper.status": "x"}).errors  # read-only


def test_the_payload_carries_the_card_runtime_and_status_lines(home, monkeypatch):
    monkeypatch.setattr(gpu_runtime, "cached_gpu", lambda *a, **k: ({"name": "RTX", "vram_mb": 8192, "driver": "1"}, True))
    monkeypatch.setattr(gpu_runtime, "_gpu_cache", {"at": 0.0, "info": {"name": "RTX", "vram_mb": 8192, "driver": "1"}, "done": True})
    payload = S.settings_payload(note_types=[], server_default_note_type="", devices={})
    values = payload["values"]
    assert values["gpu_helper.gpu"] == "RTX · 8 GB · driver 1"
    assert values["gpu_helper.runtime"] == "Not installed"
    assert values["gpu_helper.status"] == gpu_helper.current_status_text()
    assert remote.sanitize_result("get_settings", payload) == payload


def _dialog(**kw):
    dialog = sd.SettingsDialog(**kw)
    dialog.folder_stats = lambda folder: {"count": 0, "bytes": 0}
    return dialog


def test_the_dialog_page_has_the_switch_status_lines_and_the_local_buttons(qt_app, home):
    config_mod.save_config({"gpu_helper": {"enabled": True, "start_at_login": False}})
    dialog = _dialog()
    assert dialog.page_keys().index("gpu_helper") == dialog.page_keys().index("server") + 1
    assert dialog.gpu_enabled_check.isChecked() and not dialog.gpu_login_check.isChecked()
    assert dialog.gpu_login_check.isEnabled() and dialog.gpu_auto_note.isHidden()
    dialog.gpu_enabled_check.setChecked(False)
    assert not dialog.gpu_login_check.isEnabled()  # the schema's enabled_if
    assert dialog.gpu_install_button.text() == "Install now"
    assert not dialog.gpu_auto_note.isHidden()  # "GPU support installs automatically when this is on."
    assert not dialog.gpu_remove_button.isEnabled()
    assert dialog.collect_values()["gpu_helper.enabled"] is False
    assert dialog.gpu_runtime_label.text() == "Not installed"
    assert dialog.gpu_status_label.text() == gpu_helper.current_status_text()


class _Owner(QWidget):
    """A main-window stand-in: the supervisor, the real auto installer and the hook the dialog looks for on its parent."""

    def __init__(self, installer_factory=None):
        super().__init__()
        from meeting_notes.client.gpu_auto import GpuAutoInstaller

        self.calls = []
        self.gpu_supervisor = SimpleNamespace(
            stop=lambda: self.calls.append("stop"), status_changed=SimpleNamespace(connect=lambda *_a: None)
        )
        self.gpu_auto = GpuAutoInstaller(
            self, supervisor=self.gpu_supervisor, on_changed=self.on_gpu_runtime_changed,
            installer_factory=installer_factory,
        )

    def on_gpu_runtime_changed(self):
        self.calls.append("changed")


class FakeInstaller(QObject):
    progress = Signal(str, object, object)
    finished = Signal(bool, str)
    made = []

    def __init__(self, url, token, parent=None):
        super().__init__(parent)
        self.url, self.token = url, token
        self.before_swap = None
        self.started = False
        self.cancelled = False
        FakeInstaller.made.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def join(self, timeout=None):
        pass


def _enable_gpu(monkeypatch, url="http://server.test", token="pw"):
    """The helper on, a server set, and the window-less auto installer allowed to run."""
    monkeypatch.delenv("MEETING_NOTES_NO_GPU_HELPER", raising=False)
    config_mod.save_config({"server": {"url": url, "token": token}, "gpu_helper": {"enabled": True}})
    FakeInstaller.made = []


def _owner_with_dialog(monkeypatch):
    _enable_gpu(monkeypatch)
    owner = _Owner(installer_factory=lambda url, token, parent=None: FakeInstaller(url, token, parent))
    dialog = _dialog(parent=owner)
    return owner, dialog


def test_installed_runtime_offers_update_and_remove(qt_app, home, monkeypatch):
    _install_fake(home / "rt", "pack-1")
    monkeypatch.setattr(gpu_runtime, "pack_status", lambda url, token, **k: "pack-2")
    owner, dialog = _owner_with_dialog(monkeypatch)
    assert _pump(lambda: dialog._gpu_pack_known)
    assert dialog.gpu_install_button.text() == "Update now" and dialog.gpu_install_button.isEnabled()
    assert dialog.gpu_remove_button.isEnabled()
    monkeypatch.setattr(gpu_runtime, "pack_status", lambda url, token, **k: "pack-1")
    dialog._refresh_gpu_runtime()
    assert _pump(lambda: not dialog.gpu_install_button.isEnabled())  # up to date


def test_install_now_needs_a_server_address(qt_app, home, monkeypatch):
    owner, dialog = _owner_with_dialog(monkeypatch)
    dialog.url_edit.setText("")
    dialog._gpu_install_clicked()
    assert dialog.gpu_progress_label.text() == "Set the server address on the Server page first."
    assert FakeInstaller.made == []


def test_install_progress_and_result_show_on_the_page(qt_app, home, monkeypatch):
    monkeypatch.setattr(gpu_runtime, "installed_info", lambda root=None: None)
    owner, dialog = _owner_with_dialog(monkeypatch)
    dialog.url_edit.setText("http://server.test/")
    dialog.gpu_install_button.click()  # Install now
    assert _pump(lambda: bool(FakeInstaller.made))
    installer = FakeInstaller.made[0]
    assert (installer.url, installer.token) == ("http://server.test", "pw") and installer.started
    assert not dialog.gpu_cancel_button.isHidden() and not dialog.gpu_install_button.isEnabled()
    installer.progress.emit("Downloading", 1024, 4096)
    assert "Downloading" in dialog.gpu_progress_label.text() and dialog.gpu_progress.value() == 25
    installer.finished.emit(False, "Could not reach the server.")
    assert dialog.gpu_progress_label.text() == "Could not reach the server."
    assert dialog.gpu_progress_label.property("state") == "error"
    assert dialog.gpu_install_button.isEnabled()


def test_the_dialog_without_a_window_has_no_install_button_to_press(qt_app, home):
    dialog = _dialog()
    assert dialog._gpu_auto is None and not dialog.gpu_install_button.isEnabled()
    dialog._gpu_install_clicked()  # nothing to call, no crash
    dialog.reject()


def test_an_update_stops_the_runtime_before_the_swap_and_restarts_it_afterwards(qt_app, home, monkeypatch):
    monkeypatch.setattr(gpu_runtime, "installed_info", lambda root=None: None)
    owner, dialog = _owner_with_dialog(monkeypatch)
    dialog._gpu_install_clicked()
    assert _pump(lambda: bool(FakeInstaller.made))
    installer = FakeInstaller.made[0]
    assert owner.calls == []  # downloading does not interrupt a running helper
    installer.before_swap()  # the installer is about to replace `current`
    assert owner.calls == ["stop"]
    installer.finished.emit(True, "GPU support installed")
    assert owner.calls == ["stop", "changed"]


def test_a_failed_update_after_the_stop_still_brings_the_helper_back(qt_app, home, monkeypatch):
    monkeypatch.setattr(gpu_runtime, "installed_info", lambda root=None: None)
    owner, dialog = _owner_with_dialog(monkeypatch)
    dialog._gpu_install_clicked()
    assert _pump(lambda: bool(FakeInstaller.made))
    installer = FakeInstaller.made[0]
    installer.before_swap()
    installer.finished.emit(False, "The new runtime does not start")
    assert owner.calls == ["stop", "changed"]
    owner.gpu_auto._reset_failures()
    dialog._gpu_install_clicked()  # a failure before the stop leaves the running helper alone
    assert _pump(lambda: len(FakeInstaller.made) == 2)
    FakeInstaller.made[1].finished.emit(False, "Could not reach the server.")
    assert owner.calls == ["stop", "changed"]


def test_the_stop_from_the_install_thread_runs_on_the_gui_thread_and_waits(qt_app, home, monkeypatch):
    import threading

    monkeypatch.setattr(gpu_runtime, "installed_info", lambda root=None: None)
    owner, dialog = _owner_with_dialog(monkeypatch)
    threads = []
    owner.gpu_supervisor.stop = lambda: threads.append(threading.current_thread())
    dialog._gpu_install_clicked()
    assert _pump(lambda: bool(FakeInstaller.made))
    done = []
    worker = threading.Thread(target=lambda: (FakeInstaller.made[0].before_swap(), done.append(1)))
    worker.start()
    assert _pump(lambda: bool(done), 5.0)
    worker.join(5)
    assert threads == [threading.main_thread()]


def test_closing_the_dialog_does_not_cancel_the_install(qt_app, home, monkeypatch):
    monkeypatch.setattr(gpu_runtime, "installed_info", lambda root=None: None)
    owner, dialog = _owner_with_dialog(monkeypatch)
    dialog._gpu_install_clicked()
    assert _pump(lambda: bool(FakeInstaller.made))
    dialog.reject()
    assert FakeInstaller.made[0].cancelled is False and owner.gpu_auto.busy
    dialog2 = _dialog(parent=owner)  # reopening Settings shows the running install
    assert not dialog2.gpu_cancel_button.isHidden() and not dialog2.gpu_install_button.isEnabled()
    dialog2.gpu_cancel_button.click()  # only the Cancel button cancels
    assert FakeInstaller.made[0].cancelled is True


def test_remove_stops_the_runtime_first_and_restarts_state_even_when_it_fails(qt_app, home, monkeypatch):
    owner = _Owner()
    config_mod.save_config({"gpu_helper": {"enabled": True}})
    dialog = _dialog(parent=owner)
    monkeypatch.setattr(gpu_runtime, "remove_runtime", lambda: owner.calls.append("remove"))
    dialog._gpu_remove_clicked()
    assert owner.calls == ["stop", "remove", "changed"]
    assert not dialog.gpu_enabled_check.isChecked()
    assert config_mod.gpu_helper_settings()["enabled"] is False  # removed means off, so it does not reinstall itself
    assert dialog.gpu_progress_label.text() == "GPU support was removed and the helper turned off."
    owner.calls.clear()

    def boom():
        raise PermissionError(13, "in use")

    monkeypatch.setattr(gpu_runtime, "remove_runtime", boom)
    dialog._gpu_remove_clicked()
    assert owner.calls == ["stop", "changed"]


def test_saving_the_dialog_keeps_a_key_the_installer_saved_meanwhile(qt_app, home):
    dialog = _dialog()
    config_mod.save_config({**config_mod.load_config(), "gpu_helper": {"key": "mnh_new", "helper_id": "h9"}})
    dialog.gpu_enabled_check.setChecked(True)
    dialog.accept()
    group = json.loads((home / "config.json").read_text())["gpu_helper"]
    assert group["enabled"] is True and group["key"] == "mnh_new" and group["helper_id"] == "h9"


def test_on_another_os_the_dialog_shows_only_the_message(qt_app, home, monkeypatch):
    real = sys.platform
    monkeypatch.setattr(sys, "platform", "darwin")
    try:
        dialog = _dialog()
    finally:
        monkeypatch.setattr(sys, "platform", real)
    assert dialog.gpu_enabled_check is None and "gpu_helper.enabled" not in dialog.collect_values()
    texts = [w.text() for _p, i, _f, w in dialog.schema_rows if i == "note.gpu_helper_unsupported"]
    assert texts == [S.GPU_UNSUPPORTED]


# -- the window --------------------------------------------------------------------------------------------


class FakeTray(QObject):
    def __init__(self):
        super().__init__()
        self.visible = True
        self.statuses = []

    def set_status(self, text):
        self.statuses.append(text)

    def show(self):
        self.visible = True

    def hide(self):
        self.visible = False


def _window(qt_app, monkeypatch, tmp_path):
    from meeting_notes.audio import devices as devices_mod
    from meeting_notes.client.ui.main_window import MainWindow

    def _raise(kind, requested=None, samplerate=None):
        raise devices_mod.DeviceNotFound("no device (simulated)")

    monkeypatch.setattr(devices_mod, "resolve_source", _raise)
    config_mod.save_config({"save_dir": str(tmp_path / "rec"), "server": {"url": "", "token": ""}})
    window = MainWindow()
    window._timer.stop()
    window._auth_timer.stop()
    window._auth_checker = lambda url, token: authcheck.CheckResult(authcheck.NO_SERVER)
    return window


def test_closing_hides_to_the_tray_while_the_helper_is_on_and_quit_really_quits(qt_app, home, monkeypatch):
    window = _window(qt_app, monkeypatch, home)
    stops = []
    monkeypatch.setattr(window.gpu_supervisor, "stop", lambda: stops.append(1))
    try:
        window.gpu_tray = FakeTray()
        window.show()
        window.close()
        assert not window.isVisible() and not window._teardown_done  # still running, in the tray
        assert window.gpu_helper_active()
        window.quit_app()
        assert stops and _pump(lambda: window._teardown_done)
        assert window.gpu_tray.visible is False
    finally:
        window.controller.stop_uploader()


def test_closing_quits_as_before_when_the_helper_is_off(qt_app, home, monkeypatch):
    window = _window(qt_app, monkeypatch, home)
    try:
        assert not window.gpu_helper_active()
        window.show()
        window.close()
        assert _pump(lambda: window._teardown_done)
    finally:
        window.controller.stop_uploader()


def test_saved_settings_start_and_stop_the_helper_tray_and_autostart(qt_app, home, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_NO_GPU_HELPER")
    from meeting_notes.client.ui import main_window as mw

    calls = []
    monkeypatch.setattr(mw.autostart, "apply", lambda enabled, login, *a, **k: calls.append(("autostart", enabled, login)))
    monkeypatch.setattr(mw.GpuTray, "available", staticmethod(lambda: False))
    window = _window(qt_app, monkeypatch, home)
    monkeypatch.setattr(window.gpu_supervisor, "start", lambda: calls.append("start") or True)
    monkeypatch.setattr(window.gpu_supervisor, "stop", lambda: calls.append("stop"))
    try:
        calls.clear()
        config_mod.save_config({**config_mod.load_config(), "gpu_helper": {"enabled": True, "start_at_login": True}})
        window._apply_gpu_helper()
        assert ("autostart", True, True) in calls and "start" in calls
        calls.clear()
        config_mod.save_config({**config_mod.load_config(), "gpu_helper": {"enabled": False}})
        window._apply_gpu_helper()
        assert "stop" in calls and ("autostart", False, False) in calls and "start" not in calls
    finally:
        window.controller.stop_uploader()


def test_recording_pauses_the_helper_through_the_window(qt_app, home, monkeypatch):
    window = _window(qt_app, monkeypatch, home)
    seen = []
    monkeypatch.setattr(window.gpu_supervisor, "set_recording", lambda on: seen.append(on))
    try:
        window._tick()
        assert seen == [False]
        monkeypatch.setattr(window.controller, "state", "recording", raising=False)
        window._tick()
        assert seen[-1] is True
    finally:
        monkeypatch.setattr(window.controller, "state", "idle", raising=False)
        window.controller.stop_uploader()


def test_autostart_never_registers_a_python_interpreter():
    reg = FakeReg()
    assert autostart.set_enabled(reg, packaged=True, exe=r"C:/dev/.venv/Scripts/python.exe") is False
    assert autostart.set_enabled(reg, packaged=True, exe=r"C:/Python314/pythonw.exe") is False
    assert reg.values == {}


def test_client_main_has_no_function_local_module_names():
    # 0.8.0's first build crashed at startup: an `import os` inside main()'s smoke-test branch made `os` local
    # to the whole function, so the normal start path hit UnboundLocalError (the smoke test never got there).
    from meeting_notes.client import app as app_mod

    shadowed = {"os", "sys"} & set(app_mod.main.__code__.co_varnames)
    assert not shadowed, f"main() makes {shadowed} local; import them at module level only"
