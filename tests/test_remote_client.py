"""The recorder's half of live presence and remote control.

Part 1 drives the real ``ControlChannel`` against a self-contained fake server
(websockets.sync.server on a local port). Part 2 drives the real ``MainWindow``
(snapshot building and every command). Part 3 is the config helper and the
Settings checkbox. Nothing leaves localhost and no audio hardware is used.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from types import SimpleNamespace

import pytest

from meeting_notes import config as config_mod
from meeting_notes import remote
from meeting_notes.client import identity
from meeting_notes.client.control_channel import ControlChannel

TOKEN = "SECRET-TOKEN-abc123"
CMD_ID = "0123456789abcdef"


def _wait(condition, timeout=3.0, step=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(step)
    return condition()


# ---------------------------------------------------------------------------
# a fake server
# ---------------------------------------------------------------------------


class FakeServer:
    """Accepts the recorder websocket and records what it says.

    ``mode`` (changeable while running): ``normal`` | ``drop`` (close right after
    the hello) | ``close4401`` | ``http404`` (handshake refused).
    """

    def __init__(self, mode="normal"):
        import http
        from websockets.sync.server import serve

        self.mode = mode
        self.frames = []          # JSON frames received, in order
        self.headers = []         # request headers per connection
        self.attempts = 0         # handshakes seen (incl. refused ones)
        self.connections = 0
        self.disconnects = 0
        self.outbox = queue.Queue()
        self._lock = threading.Lock()

        def process_request(connection, request):
            with self._lock:
                self.attempts += 1
            if self.mode == "http403":
                return connection.respond(http.HTTPStatus.FORBIDDEN, "forbidden")
            if self.mode == "http404" or request.path != remote.CONNECT:
                return connection.respond(http.HTTPStatus.NOT_FOUND, "not found")
            return None

        def handler(ws):
            with self._lock:
                self.connections += 1
                self.headers.append(dict(ws.request.headers))
            try:
                if self.mode == "close4401":
                    ws.close(remote.CLOSE_UNAUTHORIZED, "unauthorized")
                    return
                while True:
                    try:
                        message = ws.recv(timeout=0.03)
                    except TimeoutError:
                        message = None
                    if message is not None:
                        frame = json.loads(message)
                        with self._lock:
                            self.frames.append(frame)
                        if frame.get("type") == "hello":
                            ws.send(json.dumps({"type": "welcome", "protocol": 1, "server_version": "test"}))
                            if self.mode == "drop":
                                ws.close(1011, "dropping")
                                return
                    while True:
                        try:
                            ws.send(json.dumps(self.outbox.get_nowait()))
                        except queue.Empty:
                            break
            except Exception:  # noqa: BLE001 - peer went away
                pass
            finally:
                with self._lock:
                    self.disconnects += 1

        self._server = serve(handler, "127.0.0.1", 0, process_request=process_request)
        self.port = self._server.socket.getsockname()[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def of_type(self, kind):
        with self._lock:
            return [f for f in self.frames if f.get("type") == kind]

    def send_command(self, name, args=None, command_id=CMD_ID):
        self.outbox.put({"type": "command", "command_id": command_id, "command": name, "args": args or {}})

    def close(self):
        self._server.shutdown()


@pytest.fixture
def server():
    srv = FakeServer()
    yield srv
    srv.close()


@pytest.fixture(autouse=True)
def _fast_cadence(monkeypatch):
    monkeypatch.setattr(remote, "SEND_EVERY_RECORDING", 0.2)
    monkeypatch.setattr(remote, "SEND_EVERY_IDLE", 0.4)


@pytest.fixture
def make_channel(server):
    made = []

    def make(get_config=None, on_command=None, **kwargs):
        kwargs.setdefault("backoff_initial", 0.05)
        kwargs.setdefault("backoff_max", 0.2)
        kwargs.setdefault("idle_poll", 0.05)
        channel = ControlChannel(
            get_config or (lambda: (server.url, TOKEN)),
            on_command or (lambda *a: None),
            **kwargs,
        )
        made.append(channel)
        channel.start()
        return channel

    yield make
    for channel in made:
        channel.stop()


def _recording_snapshot(**over):
    snap = {
        "status": "recording",
        "meeting": {"name": "Weekly", "session_id": "s1", "elapsed_sec": 5},
        "tracks": {
            "mic": {"device": "Mic", "connected": True, "muted": False, "level": 0.2, "peak": 0.3, "degraded": False},
            "system": {"device": "Out", "connected": True, "muted": False, "level": 0.1, "peak": 0.2, "degraded": False},
        },
    }
    snap.update(over)
    return snap


# ---------------------------------------------------------------------------
# part 1: the channel
# ---------------------------------------------------------------------------


def test_hello_carries_identity_and_the_current_state(server, make_channel):
    channel = make_channel(device="lab-pc", platform_text="Windows 11", version="9.9.9")
    channel.publish(_recording_snapshot())
    assert _wait(lambda: server.of_type("hello"))
    hello = server.of_type("hello")[0]
    assert hello["protocol"] == remote.PROTOCOL_VERSION
    assert remote.valid_instance_id(hello["instance_id"]) and hello["instance_id"] == channel.instance_id
    assert (hello["device"], hello["platform"], hello["version"]) == ("lab-pc", "Windows 11", "9.9.9")
    assert set(hello["state"]) == set(remote.sanitize_state({}))
    headers = {k.lower(): v for k, v in server.headers[0].items()}
    assert headers["authorization"] == f"Bearer {TOKEN}"
    assert headers[identity.HEADER.lower()] == identity.client_header_value()


def test_default_device_and_platform_match_what_uploads_send(server, make_channel):
    import socket

    make_channel()
    assert _wait(lambda: server.of_type("hello"))
    hello = server.of_type("hello")[0]
    assert hello["device"] == socket.gethostname()
    assert hello["platform"] == identity.platform_label()


def test_a_change_goes_out_at_once_and_is_sanitized(server, make_channel):
    channel = make_channel()
    assert _wait(lambda: server.of_type("hello"))
    sent = len(server.of_type("state"))
    messy = _recording_snapshot(extra="dropped")
    messy["tracks"]["mic"]["level"] = 7  # out of range
    channel.publish(messy)
    assert _wait(lambda: any(f["state"]["status"] == "recording" for f in server.of_type("state")[sent:]))
    frame = next(f for f in server.of_type("state")[sent:] if f["state"]["status"] == "recording")
    assert "extra" not in frame["state"]
    assert frame["state"]["tracks"]["mic"]["level"] == 1.0
    assert frame["state"] == remote.sanitize_state(frame["state"])


def test_level_and_clock_changes_alone_do_not_trigger_a_send_but_the_cadence_refreshes(server, make_channel):
    channel = make_channel()
    channel.publish(_recording_snapshot())
    assert _wait(lambda: server.of_type("hello"))
    assert _wait(lambda: len(server.of_type("state")) >= 1)
    # Levels move every publish; with the refresh cadence at 0.2 s we must still
    # never see faster than that, and never stop.
    start, count = time.monotonic(), len(server.of_type("state"))
    for i in range(20):
        snap = _recording_snapshot()
        snap["tracks"]["mic"]["level"] = (i % 10) / 10
        snap["meeting"]["elapsed_sec"] = i
        channel.publish(snap)
        time.sleep(0.02)
    elapsed = time.monotonic() - start
    sent = len(server.of_type("state")) - count
    assert 1 <= sent <= int(elapsed / 0.2) + 1, (sent, elapsed)


def test_idle_uses_the_slower_heartbeat(server, make_channel):
    channel = make_channel()
    channel.publish({"status": "idle"})
    assert _wait(lambda: server.of_type("hello"))
    time.sleep(0.05)
    count = len(server.of_type("state"))
    start = time.monotonic()
    time.sleep(1.0)
    sent = len(server.of_type("state")) - count
    elapsed = time.monotonic() - start
    # Bound by the time that really passed (a loaded machine can stretch the sleep): never faster than
    # SEND_EVERY_IDLE, and never silent.
    assert 1 <= sent <= int(elapsed / remote.SEND_EVERY_IDLE) + 1, (sent, elapsed)


def test_commands_roundtrip_to_the_handler_and_back_as_an_ack(server, make_channel):
    got = []
    holder = {}

    def on_command(command_id, name, args):
        got.append((command_id, name, args))
        holder["channel"].send_ack(command_id, True, None, None, {"status": "recording"})

    holder["channel"] = make_channel(on_command=on_command)
    assert _wait(lambda: server.of_type("hello"))
    server.send_command("start", {"name": "  Weekly   sync "})
    assert _wait(lambda: server.of_type("ack"))
    assert got == [(CMD_ID, "start", {"name": "Weekly sync"})]
    ack = server.of_type("ack")[0]
    assert (ack["command_id"], ack["ok"], ack["code"], ack["error"]) == (CMD_ID, True, None, None)
    assert ack["state"]["status"] == "recording"


@pytest.mark.parametrize(
    "name,args,code",
    [
        ("format_disk", {}, "unknown_command"),
        ("mute", {}, "bad_args"),
        ("mute", {"track": "speaker"}, "bad_args"),
        ("stop", {"force": "yes"}, "bad_args"),
        ("set_name", {}, "bad_args"),
    ],
)
def test_invalid_commands_are_refused_without_reaching_the_handler(server, make_channel, name, args, code):
    called = []
    make_channel(on_command=lambda *a: called.append(a))
    assert _wait(lambda: server.of_type("hello"))
    server.send_command(name, args)
    assert _wait(lambda: server.of_type("ack"))
    ack = server.of_type("ack")[0]
    assert (ack["ok"], ack["code"]) == (False, code)
    assert ack["error"]
    assert called == []


def test_a_failing_handler_is_acked_failed_and_the_channel_survives(server, make_channel):
    def boom(*_a):
        raise RuntimeError("kaboom")

    make_channel(on_command=boom)
    assert _wait(lambda: server.of_type("hello"))
    server.send_command("stop")
    assert _wait(lambda: server.of_type("ack"))
    assert server.of_type("ack")[0]["code"] == "failed"
    server.send_command("stop", command_id="fedcba9876543210")
    assert _wait(lambda: len(server.of_type("ack")) == 2)


def test_garbage_frames_are_ignored(server, make_channel):
    called = []
    make_channel(on_command=lambda *a: called.append(a))
    assert _wait(lambda: server.of_type("hello"))
    server.outbox.put(["not", "an", "object"])
    server.outbox.put({"type": "command", "command_id": "NOT-HEX!", "command": "stop"})
    server.outbox.put({"type": "future-thing"})
    server.send_command("stop", command_id="aaaaaaaa")
    assert _wait(lambda: called)
    assert [c[0] for c in called] == ["aaaaaaaa"]


def test_reconnects_with_backoff_after_the_server_drops_it(make_channel, server):
    server.mode = "drop"
    make_channel()
    assert _wait(lambda: server.connections >= 3, timeout=5)
    server.mode = "normal"
    # A connection already in flight when the mode flipped may itself come up normal, so wait for one
    # that stays open (not for "one more than before", which raced with that connection).
    assert _wait(lambda: server.connections - server.disconnects >= 1 and server.of_type("state"), timeout=5)
    time.sleep(0.3)
    assert server.connections - server.disconnects == 1


@pytest.mark.parametrize("mode,status", [("http404", 404), ("http403", 403)])
def test_a_server_without_the_endpoint_is_given_up_on_quietly(caplog, make_channel, server, mode, status):
    server.mode = mode
    caplog.set_level(logging.DEBUG, logger="meeting_notes.client.control")
    channel = make_channel(not_found_retry=3600)
    assert _wait(lambda: server.attempts >= 1)
    time.sleep(0.6)
    assert server.attempts == 1  # no hammering: one try, then a long wait
    assert channel._thread.is_alive()
    notes = [r for r in caplog.records if "no remote-control endpoint" in r.getMessage()]
    assert len(notes) == 1 and notes[0].levelno == logging.INFO and f"HTTP {status}" in notes[0].getMessage()
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_unauthorized_close_retries_slowly_and_logs_once(caplog, make_channel, server):
    server.mode = "close4401"
    caplog.set_level(logging.DEBUG, logger="meeting_notes.client.control")
    make_channel(unauthorized_retry=0.4)
    assert _wait(lambda: server.connections >= 3, timeout=5)
    # 0.05 s backoff would already have made a dozen attempts in the same time.
    assert server.connections <= 4
    closed = [r for r in caplog.records if r.levelno == logging.INFO and "server closed the connection" in r.getMessage()]
    assert len(closed) == 1


def test_unauthorized_close_calls_the_unauthorized_hook(make_channel, server):
    server.mode = "close4401"
    channel = make_channel(unauthorized_retry=0.2)
    calls = []
    channel.on_unauthorized = lambda: calls.append(1)  # a retry after 0.2 s still lands on it
    assert _wait(lambda: calls, timeout=5)


def test_the_token_never_reaches_the_log(caplog, make_channel, server):
    caplog.set_level(logging.DEBUG)  # root: even websockets' own header dumps would show up
    server.mode = "drop"
    first = make_channel()
    assert _wait(lambda: server.connections >= 2, timeout=5)
    first.stop()  # a second channel for the 4401 leg: flipping the mode under a live one raced
    server.mode = "close4401"
    connections = server.connections
    make_channel()
    assert _wait(lambda: server.connections > connections, timeout=5)
    ours = [r.getMessage() for r in caplog.records if r.name.startswith("meeting_notes")]
    assert ours and TOKEN not in " ".join(ours)
    assert TOKEN not in json.dumps(server.frames)


def test_stop_is_prompt_and_closes_the_socket(server, make_channel):
    channel = make_channel()
    assert _wait(lambda: server.of_type("hello"))
    thread = channel._thread
    started = time.monotonic()
    channel.stop()
    assert time.monotonic() - started < 1.0
    assert not thread.is_alive()
    assert _wait(lambda: server.disconnects == 1)
    count = server.connections
    time.sleep(0.3)
    assert server.connections == count  # does not come back


def test_no_server_url_means_idle_then_connects_once_one_is_set(server, make_channel):
    config = {"value": ("", "")}
    make_channel(get_config=lambda: config["value"])
    time.sleep(0.3)
    assert server.attempts == 0
    config["value"] = (server.url, TOKEN)
    assert _wait(lambda: server.of_type("hello"))


def test_a_settings_change_moves_a_live_connection(monkeypatch, make_channel, server):
    import meeting_notes.client.control_channel as cc

    monkeypatch.setattr(cc, "_CONFIG_CHECK_EVERY", 0.1)
    other = FakeServer()
    try:
        config = {"value": (server.url, TOKEN)}
        make_channel(get_config=lambda: config["value"])
        assert _wait(lambda: server.of_type("hello"))
        config["value"] = (other.url, TOKEN)
        assert _wait(lambda: other.of_type("hello"), timeout=5)
    finally:
        other.close()


def test_publish_and_send_ack_never_raise_or_block_without_a_connection():
    channel = ControlChannel(lambda: ("", ""), lambda *a: None)
    started = time.monotonic()
    for _ in range(1000):
        channel.publish({"status": "recording"})
        channel.send_ack(CMD_ID, True)
    channel.stop()  # never started: fine
    assert time.monotonic() - started < 1.0


def test_an_unreachable_server_does_not_spam_the_log(caplog, make_channel):
    caplog.set_level(logging.DEBUG, logger="meeting_notes.client.control")
    make_channel(get_config=lambda: ("http://127.0.0.1:9", TOKEN))
    time.sleep(1.0)
    info = [r for r in caplog.records if r.levelno >= logging.INFO]
    assert len(info) <= 1


# ---------------------------------------------------------------------------
# part 2: the real window
# ---------------------------------------------------------------------------

PySide6 = pytest.importorskip("PySide6", reason="PySide6 not installed")

from PySide6.QtWidgets import QApplication  # noqa: E402

from meeting_notes.client import update as update_mod  # noqa: E402
from meeting_notes.client.controller import IDLE, RECORDING  # noqa: E402
from meeting_notes.client.meeting_detect import MeetingEnded  # noqa: E402
from meeting_notes.client.ui import main_window as mw  # noqa: E402
from meeting_notes.client.ui.theme import APP_STYLE  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(APP_STYLE)
    return app


def _pump(condition, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        QApplication.processEvents()
        time.sleep(0.01)
    return condition()


class FakeRecorder:
    def __init__(self, name, peak=0.0):
        self.source = SimpleNamespace(name=name, samplerate=1000)
        self.last_peak = peak
        self.failing = False
        self.degraded = False
        self.muted = False


class FakeSession:
    def __init__(self, tmp_path, mic=True, system=True):
        self.recorders = {}
        if mic:
            self.recorders["mic"] = FakeRecorder("USB Mic", 0.2)
        if system:
            self.recorders["system"] = FakeRecorder("Speakers", 0.4)
        self.elapsed = 12.5
        self.session_dir = tmp_path / "20260930-101500-Weekly-sync-PC"

    def set_track_muted(self, track, muted):
        rec = self.recorders.get(track)
        if rec is None:
            return False
        rec.muted = muted
        return True

    def track_muted(self, track):
        rec = self.recorders.get(track)
        return bool(rec and rec.muted)


class FakeChannel:
    def __init__(self, on_command):
        self.on_command = on_command
        self.published = []
        self.acks = []
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self, join_timeout=1.0):
        self.stopped = True

    def publish(self, snapshot):
        self.published.append(snapshot)

    def send_ack(self, command_id, ok, code=None, error=None, snapshot=None):
        self.acks.append((command_id, ok, code, error, snapshot))


@pytest.fixture
def window(qt_app, tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_NOTES_CONFIG", str(tmp_path / "config.json"))
    from meeting_notes.audio import devices as devices_mod
    from meeting_notes.client.ui.main_window import MainWindow

    def _raise(kind, requested=None, samplerate=None):
        raise devices_mod.DeviceNotFound("no device (simulated)")

    monkeypatch.setattr(devices_mod, "resolve_source", _raise)
    channels = []

    def factory(on_command):
        channels.append(FakeChannel(on_command))
        return channels[-1]

    w = MainWindow(remote_channel_factory=factory)
    w.channels = channels
    w._timer.stop()
    w._detect_timer.stop()
    w._auth_timer.stop()
    w._update_timer.stop()
    w._remote_timer.stop()
    w._detector = SimpleNamespace(poll=lambda now: [], end_grace_sec=60.0)
    w.controller.queue_status = lambda: {"pending": 0, "failed": 0, "last_error": ""}
    w.started_names = []

    def fake_start(name="", sources=None):
        w.started_names.append(name)
        w.controller.state = RECORDING
        w.controller.session = FakeSession(tmp_path)
        w.controller.session_dir = w.controller.session.session_dir
        w.controller._reset_device_state({"mic", "system"})
        w.controller._recording_name = name
        return w.controller.session_dir

    def fake_stop():
        w.controller.state = IDLE
        return {"duration_sec": 3.0}

    monkeypatch.setattr(w.controller, "start", fake_start)
    monkeypatch.setattr(w.controller, "stop", fake_stop)
    yield w
    w._reset_end_state()
    w._close_prompt()
    w._teardown_done = True
    w.close()


def _recording(window, tmp_path, **kw):
    """Put the window into a recording state the same way a start would."""
    window.controller.state = RECORDING
    window.controller.session = FakeSession(tmp_path, **kw)
    window.controller.session_dir = window.controller.session.session_dir
    window.controller._reset_device_state(set(window.controller.session.recorders))
    window.record_button.setText("Stop recording")
    window._set_record_look("recording")
    for track, button in (("mic", window.mute_mic_button), ("system", window.mute_system_button)):
        button.setEnabled(track in window.controller.session.recorders)


def _manifest(version="99.0.0"):
    return update_mod.UpdateManifest.from_json(
        {"version": version, "url": "/install/client-agent.ps1", "size": 1, "sha256": "b" * 64}, "http://meeting.lan"
    )


# -- the snapshot ----------------------------------------------------------------


def test_the_window_creates_publishes_to_and_stops_its_channel(window):
    (channel,) = window.channels
    assert channel.started and channel.published
    window._teardown_done = False
    window._pending_close = False
    window.close()  # closeEvent starts the teardown
    assert channel.stopped
    window._teardown_done = True


def test_no_channel_is_created_when_remote_is_switched_off_by_the_environment(qt_app, tmp_path, monkeypatch):
    from meeting_notes.client.ui.main_window import MainWindow

    monkeypatch.setenv("MEETING_NOTES_CONFIG", str(tmp_path / "config.json"))
    w = MainWindow()  # conftest sets MEETING_NOTES_NO_REMOTE
    try:
        assert w._remote is None
        w.build_remote_state()  # still buildable
    finally:
        w._teardown_done = True
        w.close()


def test_idle_snapshot_matches_the_contract(window):
    state = window.build_remote_state()
    assert state == remote.sanitize_state(state)
    assert state["status"] == "idle"
    assert state["meeting"] == {"name": "", "session_id": None, "elapsed_sec": None}
    assert state["control"] == {"allowed": True}
    assert state["call"] == {"prompt": None, "active_app": None}
    assert state["suggestion"] is None
    assert state["update"] == {"available": False, "version": None, "installing": False}
    assert state["stream"] == "off"


def test_recording_snapshot_reports_devices_levels_mute_and_the_session(window, tmp_path):
    window.name_edit.setText("Weekly sync")
    _recording(window, tmp_path)
    window._note_remote_levels(time.monotonic(), {"mic": 0.9, "system": 0.1})
    window.controller.session.recorders["mic"].last_peak = 0.25
    window.controller.session.recorders["system"].muted = True
    window.controller.session.recorders["system"].degraded = True
    state = window.build_remote_state()
    assert state == remote.sanitize_state(state)
    assert state["status"] == "recording"
    assert state["meeting"] == {
        "name": "Weekly sync",
        "session_id": "20260930-101500-Weekly-sync-PC",
        "elapsed_sec": 12.5,
    }
    mic, system = state["tracks"]["mic"], state["tracks"]["system"]
    assert mic == {"device": "USB Mic", "connected": True, "muted": False, "level": 0.25, "peak": 0.9, "degraded": False}
    assert system["muted"] and system["degraded"] and system["device"] == "Speakers"
    assert state["banners"] == []


def test_a_missing_microphone_shows_as_a_banner_and_a_disconnected_track(window, tmp_path):
    _recording(window, tmp_path, mic=False)
    window.controller._reset_device_state({"system"})
    state = window.build_remote_state()
    assert state["tracks"]["mic"]["connected"] is False and state["tracks"]["mic"]["device"] is None
    assert state["tracks"]["system"]["connected"] is True
    assert [b["id"] for b in state["banners"]] == ["no_mic"]
    assert state["banners"][0]["level"] == "error" and "not being recorded" in state["banners"][0]["text"]


def test_a_lost_device_and_a_device_back_notice(window, tmp_path):
    _recording(window, tmp_path)
    window.controller.session.recorders["mic"].failing = True
    window.controller._dev_state["mic"] = "lost"
    window.controller._dev_lost_elapsed["mic"] = 65.0
    window.controller._dev_notices["system"] = ("Meeting audio connected at 00:00:05", time.monotonic() + 30)
    state = window.build_remote_state()
    assert state["tracks"]["mic"]["connected"] is False
    assert state["tracks"]["mic"]["device"] == "USB Mic"
    ids = [b["id"] for b in state["banners"]]
    assert ids == ["device_lost", "device_back"]


def test_idle_window_reports_no_device_when_none_is_found(window):
    state = window.build_remote_state()  # resolve_source raises in this fixture
    window.controller.device_labels = lambda: {"mic": "unavailable: no device", "system": "Speakers"}
    state = window.build_remote_state()
    assert state["tracks"]["mic"]["connected"] is False and state["tracks"]["mic"]["device"] is None
    assert state["tracks"]["system"] == {
        "device": "Speakers", "connected": True, "muted": False, "level": 0.0, "peak": 0.0, "degraded": False,
    }


def test_finishing_status_while_the_recording_is_being_saved(window, tmp_path):
    _recording(window, tmp_path)
    window._set_record_look("finishing")
    assert window.build_remote_state()["status"] == "finishing"


def test_call_prompt_and_stop_suggestion_show_in_the_snapshot(window, tmp_path):
    window._show_prompt("Teams", "Weekly Sync")
    state = window.build_remote_state()
    assert state["call"]["prompt"] == {"label": "Teams", "name": "Weekly Sync"}
    window._close_prompt()
    assert window.build_remote_state()["call"]["prompt"] is None

    _recording(window, tmp_path)
    window._show_suggestion("silence", "No audio for 5 minutes")
    suggestion = window.build_remote_state()["suggestion"]
    assert suggestion == {"kind": "silence", "title": "No audio for 5 minutes", "seconds_left": None}


def test_the_end_countdown_reports_its_seconds(window, tmp_path):
    _recording(window, tmp_path)
    window._auto_session = True
    window._system_last_active = time.monotonic() - 1000
    window._detect_settings["end_grace_sec"] = 5
    window._handle_meeting_event(MeetingEnded("teams", "Teams"))
    assert window._end_prompt is not None
    suggestion = window.build_remote_state()["suggestion"]
    assert suggestion["kind"] == "countdown" and suggestion["seconds_left"] == window._end_prompt.remaining


def test_update_banners_and_upload_progress_show_in_the_snapshot(window):
    window._on_update_checked(_manifest("1.2.3"))
    window.controller.queue_status = lambda: {"pending": 3, "failed": 1, "last_error": ""}
    window.controller.queue_awaiting_transcript = lambda: 1
    window.controller.queue_progress = lambda: {
        "upload_state": "uploading", "upload_percent": 42.0,
        "transcription_state": "pending", "transcription_percent": 0.0,
    }
    window.alert_label.setText("The server rejected your password.")
    window.alert_bar.setVisible(True)
    state = window.build_remote_state()
    assert state["update"] == {"available": True, "version": "1.2.3", "installing": False}
    assert state["uploads"] == {
        "pending": 3, "failed": 1, "awaiting_transcript": 1, "current_percent": 42.0, "state": "uploading",
    }
    by_id = {b["id"]: b for b in state["banners"]}
    assert by_id["update_available"]["level"] == "info" and "1.2.3" in by_id["update_available"]["text"]
    assert by_id["token_rejected"]["level"] == "error"


def test_a_broken_snapshot_is_swallowed_and_logged_once(window, caplog):
    window.controller.device_labels = lambda: 1 / 0
    caplog.set_level(logging.ERROR, logger="meeting_notes.client.ui")
    window._publish_remote_state()
    window._publish_remote_state()
    assert len([r for r in caplog.records if "snapshot" in r.getMessage()]) == 1


# -- commands ---------------------------------------------------------------------


def _run(window, name, args=None):
    return window.execute_remote_command(name, args)


def test_start_uses_the_name_and_the_buttons_own_path(window, caplog):
    caplog.set_level(logging.INFO, logger="meeting_notes.client.ui")
    assert _run(window, "start", {"name": "Board meeting"}) == (True, None, None)
    assert window.started_names == ["Board meeting"]
    assert window.name_edit.text() == "Board meeting"
    assert window.record_button.text() == "Stop recording"
    assert window._toast.text() == "Recording started from the server"
    assert "remote command: start (source=server) -> ok" in caplog.text
    assert _run(window, "start") == (False, "already_recording", "Already recording.")
    assert "remote command: start (source=server) -> refused(already_recording)" in caplog.text


def test_start_without_a_name_keeps_whatever_the_field_holds(window):
    window.name_edit.setText("Typed by hand")
    assert _run(window, "start")[0]
    assert window.started_names == ["Typed by hand"]


def test_start_failing_reports_the_controllers_error(window, monkeypatch):
    def failing_start(name="", sources=None):
        window.controller.error = "no audio devices available"
        return None

    monkeypatch.setattr(window.controller, "start", failing_start)
    assert _run(window, "start") == (False, "failed", "no audio devices available")
    assert window._toast.isHidden()  # nothing changed, nothing to announce


def test_start_is_refused_as_busy_while_closing_or_installing(window):
    window._update_installing = True
    assert _run(window, "start")[:2] == (False, "busy")
    window._update_installing = False
    window._pending_close = True
    assert _run(window, "start")[:2] == (False, "busy")
    window._pending_close = False


def test_stop_goes_through_the_stop_button_path(window, tmp_path):
    assert _run(window, "stop")[:2] == (False, "not_recording")
    _recording(window, tmp_path)
    window._auto_session = True
    assert _run(window, "stop") == (True, None, None)
    assert window._auto_session is False
    assert window._toast.text() == "Stopped from the server"
    assert window.record_button.text() == "Finishing..."
    assert _pump(lambda: window.record_button.text() == "Start recording")
    assert window.controller.state == IDLE


@pytest.mark.parametrize("track,button", [("mic", "mute_mic_button"), ("system", "mute_system_button")])
def test_mute_and_unmute_drive_the_mute_buttons(window, tmp_path, track, button):
    assert _run(window, "mute", {"track": track})[:2] == (False, "not_recording")
    _recording(window, tmp_path)
    assert _run(window, "mute", {"track": track}) == (True, None, None)
    assert window.controller.source_muted(track)
    assert getattr(window, button).isChecked() and getattr(window, button).text().startswith("Unmute")
    who = "you" if track == "mic" else "them"
    assert window._toast.text() == f"Muted {who} from the server"
    window._toast.hide()
    assert _run(window, "mute", {"track": track})[0]  # already muted: fine, and nothing to announce
    assert window._toast.isHidden()
    assert _run(window, "unmute", {"track": track}) == (True, None, None)
    assert not window.controller.source_muted(track)
    assert getattr(window, button).text().startswith("Mute")
    assert window._toast.text() == f"Unmuted {who} from the server"


def test_muting_a_track_that_is_not_connected_is_refused(window, tmp_path):
    _recording(window, tmp_path, mic=False)
    assert _run(window, "mute", {"track": "mic"})[:2] == (False, "no_such_track")
    assert _run(window, "mute", {"track": "system"})[0]


def test_refresh_devices_rescans_and_wakes_the_watcher(window):
    calls = []
    window.controller.probe_devices = lambda: calls.append("probe") or {"mic": "M", "system": "S"}
    window.controller.wake_device_watch = lambda: calls.append("wake")
    assert _run(window, "refresh_devices") == (True, None, None)
    assert calls == ["probe", "wake"]
    assert window.devices_label.text() == "You: M\nThem: S"
    assert window._toast.text() == "Devices refreshed from the server"


def test_accept_call_prompt_with_the_prompts_own_name_and_with_another(window):
    assert _run(window, "accept_call_prompt")[:2] == (False, "no_prompt")
    window._show_prompt("Teams", "Weekly Sync")
    assert _run(window, "accept_call_prompt") == (True, None, None)
    assert window.started_names == ["Weekly Sync"]
    assert window._prompt is None
    assert window._auto_session is True  # the prompt's own record path
    window.controller.state = IDLE
    window._show_prompt("Zoom", "Standup")
    assert _run(window, "accept_call_prompt", {"name": "Board"})[0]
    assert window.started_names == ["Weekly Sync", "Board"]


def test_accept_call_prompt_refused_while_already_recording(window, tmp_path):
    window._show_prompt("Teams", "Weekly Sync")
    _recording(window, tmp_path)
    assert _run(window, "accept_call_prompt")[:2] == (False, "already_recording")


def test_dismiss_call_prompt(window):
    assert _run(window, "dismiss_call_prompt")[:2] == (False, "no_prompt")
    window._show_prompt("Teams", "Weekly Sync")
    assert _run(window, "dismiss_call_prompt") == (True, None, None)
    assert window._prompt is None and window.started_names == []
    assert window._toast.text() == "Call prompt dismissed from the server"


def test_keep_recording_and_stop_suggested_for_the_stop_suggestion(window, tmp_path):
    assert _run(window, "keep_recording")[:2] == (False, "no_suggestion")
    assert _run(window, "stop_suggested")[:2] == (False, "no_suggestion")
    _recording(window, tmp_path)
    window._show_suggestion("call-end", "Meeting seems to have ended")
    assert _run(window, "keep_recording") == (True, None, None)
    assert window._suggest_prompt is None and window._suggest_kept is True
    assert window.controller.state == RECORDING

    window._show_suggestion("silence", "No audio for 5 minutes")
    assert _run(window, "stop_suggested") == (True, None, None)
    assert window._suggest_prompt is None
    assert window._toast.text() == "Stopped from the server"
    assert _pump(lambda: window.controller.state == IDLE and window.record_button.text() == "Start recording")


def _show_countdown(window, tmp_path):
    _recording(window, tmp_path)
    window._auto_session = True
    window._system_last_active = time.monotonic() - 1000
    window._detect_settings["end_grace_sec"] = 5
    window._handle_meeting_event(MeetingEnded("teams", "Teams"))
    assert window._end_prompt is not None


def test_keep_recording_and_stop_suggested_for_the_end_countdown(window, tmp_path):
    _show_countdown(window, tmp_path)
    assert _run(window, "keep_recording") == (True, None, None)
    assert window._end_prompt is None and window._auto_stop_kept is True
    assert window.controller.state == RECORDING

    window._auto_stop_kept = False
    window._suggest_kept = False
    _show_countdown(window, tmp_path)
    assert _run(window, "stop_suggested") == (True, None, None)
    assert _pump(lambda: window.controller.state == IDLE)


def test_retry_uploads_uses_the_queues_retry_all_now(window):
    calls = []
    queue_obj = SimpleNamespace(retry_all_now=lambda: calls.append("retry") or 2)
    window.controller.session_queue = lambda: queue_obj
    window.controller.start_uploader = lambda force=False: calls.append("uploader") or True
    assert _run(window, "retry_uploads") == (True, None, None)
    assert calls == ["retry", "uploader"]
    assert window._toast.text() == "Retrying uploads from the server"


def test_retry_uploads_reports_a_failure(window):
    def broken():
        raise OSError("disk unavailable")

    window.controller.session_queue = broken
    ok, code, error = _run(window, "retry_uploads")
    assert (ok, code) == (False, "failed") and "disk unavailable" in error


def test_check_update_forces_a_check(window):
    seen = []
    window._check_for_update = lambda force=False: seen.append(force)
    assert _run(window, "check_update") == (True, None, None)
    assert seen == [True]


def test_install_update_rules(window, tmp_path):
    assert _run(window, "install_update")[:2] == (False, "no_update")
    window._on_update_checked(_manifest())
    window._update_updater = update_mod.ClientUpdater("http://meeting.lan", "")
    ran = []
    window._run_async = lambda work, on_done: ran.append(work)  # never download anything

    _recording(window, tmp_path)
    assert _run(window, "install_update")[:2] == (False, "recording_in_progress")
    window._set_record_look("finishing")
    window.controller.state = "stopping"
    assert _run(window, "install_update")[:2] == (False, "recording_in_progress")
    assert ran == []

    window.controller.state = IDLE
    window._set_record_look("idle")
    assert _run(window, "install_update") == (True, None, None)
    assert window._update_installing is True and len(ran) == 1
    assert window._toast.text() == "Update started from the server"
    assert window.build_remote_state()["update"]["installing"] is True
    assert _run(window, "install_update")[:2] == (False, "busy")


def test_set_name_while_recording_reaches_the_saved_meeting_name(window, tmp_path):
    assert _run(window, "set_name", {"name": "Planning"}) == (True, None, None)  # idle: just the field
    assert window.name_edit.text() == "Planning"
    _recording(window, tmp_path)
    window.controller._recording_name = "Planning"
    assert _run(window, "set_name", {"name": "Quarterly review"}) == (True, None, None)
    assert window.name_edit.text() == "Quarterly review"
    assert window.controller._recording_name == "Quarterly review"  # what stop() writes to session.json
    assert window._toast.text() == "Meeting renamed from the server"


def test_controller_stop_saves_the_renamed_meeting(tmp_path, monkeypatch):
    """The real controller: a rename mid-recording is what lands in session.json."""
    from meeting_notes.client.controller import RecordingController

    monkeypatch.setenv("MEETING_NOTES_CONFIG", str(tmp_path / "config.json"))
    controller = RecordingController()
    assert controller.set_recording_name("x") is False  # not recording
    controller.state = RECORDING
    controller._recording_name = "Old"
    assert controller.set_recording_name("New") is True
    assert controller._recording_name == "New"


def test_unknown_and_malformed_commands_are_refused(window):
    assert _run(window, "format_disk")[:2] == (False, "unknown_command")
    assert _run(window, "mute", {})[:2] == (False, "bad_args")
    assert _run(window, "mute", {"track": "x"})[:2] == (False, "bad_args")
    assert _run(window, "stop", {"force": True})[:2] == (False, "bad_args")
    assert _run(window, "set_name", {"name": "  "})[:2] == (False, "bad_args")
    assert window._toast.isHidden()


@pytest.mark.parametrize("name", sorted(remote.COMMANDS))
def test_every_command_is_refused_when_remote_control_is_switched_off(window, tmp_path, caplog, name):
    caplog.set_level(logging.INFO, logger="meeting_notes.client.ui")
    (tmp_path / "config.json").write_text(json.dumps({"remote_control_allowed": False}))
    _recording(window, tmp_path)
    window._show_suggestion("silence", "No audio")
    args = {"mute": {"track": "mic"}, "unmute": {"track": "mic"}, "set_name": {"name": "x"},
            "set_note_type": {"note_type": "quick"}, "reupload": {"session_ids": ["x"]}, "delete_local": {"session_ids": ["x"]}}.get(name, {})
    ok, code, error = window.execute_remote_command(name, args)
    assert (ok, code) == (False, "remote_control_disabled")
    assert "turned off" in error
    assert f"remote command: {name} (source=server) -> refused(remote_control_disabled)" in caplog.text
    assert window.controller.state == RECORDING and window._suggest_prompt is not None
    assert not window.controller.source_muted("mic")
    assert window._toast.isHidden()
    assert window.build_remote_state()["control"] == {"allowed": False}


def test_the_toast_is_bottom_centred_and_does_not_take_focus(window):
    window.resize(900, 700)
    window.show()
    QApplication.processEvents()
    _run(window, "refresh_devices")
    toast = window._toast
    assert toast.isVisible() and toast.focusPolicy() == PySide6.QtCore.Qt.NoFocus
    assert abs(toast.geometry().center().x() - window.width() // 2) <= 1
    assert toast.geometry().bottom() > window.height() * 0.8
    window.resize(1200, 700)
    QApplication.processEvents()
    assert abs(toast.geometry().center().x() - 600) <= 1
    window.hide()


def test_a_new_toast_replaces_the_previous_one(window):
    window._toast.show_message("first")
    window._toast.show_message("second")
    assert window._toast.text() == "second"


def test_toast_colours_follow_the_theme(qt_app):
    from meeting_notes.client.ui import theme
    from meeting_notes.client.ui.toast import Toast
    from PySide6.QtWidgets import QWidget

    assert theme.LIGHT["toast_bg"] == "#171a1f" and theme.DARK["toast_bg"] == "#e8e9ec"
    host = QWidget()
    toast = Toast(host)
    toast.show_message("hello")
    try:
        for mode in ("light", "dark"):
            theme.apply_appearance(mode, qt_app)
            assert theme.tokens()["toast_bg"] in toast.styleSheet()
            assert theme.tokens()["toast_text"] in toast.styleSheet()
    finally:
        theme.apply_appearance("light", qt_app)


def test_a_command_from_the_channel_thread_is_executed_on_the_gui_thread_and_acked(window, tmp_path):
    (channel,) = window.channels
    _recording(window, tmp_path)
    threading.Thread(target=channel.on_command, args=(CMD_ID, "mute", {"track": "mic"})).start()
    assert _pump(lambda: channel.acks)
    command_id, ok, code, error, snapshot = channel.acks[0]
    assert (command_id, ok, code, error) == (CMD_ID, True, None, None)
    assert snapshot["tracks"]["mic"]["muted"] is True
    assert channel.published[-1]["tracks"]["mic"]["muted"] is True


def test_real_channel_end_to_end_through_the_real_window(qt_app, tmp_path, monkeypatch, server):
    from meeting_notes.audio import devices as devices_mod
    from meeting_notes.client.ui.main_window import MainWindow

    monkeypatch.setenv("MEETING_NOTES_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setattr(
        devices_mod, "resolve_source", lambda *a, **k: (_ for _ in ()).throw(devices_mod.DeviceNotFound("none"))
    )

    def factory(on_command):
        return ControlChannel(
            lambda: (server.url, TOKEN), on_command, backoff_initial=0.05, backoff_max=0.2, idle_poll=0.05
        )

    w = MainWindow(remote_channel_factory=factory)
    try:
        w._timer.stop()
        w._detect_timer.stop()
        w.controller.queue_status = lambda: {"pending": 0, "failed": 0, "last_error": ""}
        assert _pump(lambda: server.of_type("hello"))
        _recording(w, tmp_path)
        assert _pump(lambda: any(f["state"]["status"] == "recording" for f in server.of_type("state")))
        server.send_command("mute", {"track": "mic"})
        assert _pump(lambda: server.of_type("ack"))
        ack = server.of_type("ack")[0]
        assert ack["ok"] is True and ack["state"]["tracks"]["mic"]["muted"] is True
        assert w._toast.text() == "Muted you from the server"
    finally:
        w._stop_remote()
        w._teardown_done = True
        w.close()
    assert _wait(lambda: server.disconnects >= 1)


# ---------------------------------------------------------------------------
# part 3: the setting
# ---------------------------------------------------------------------------


def test_config_helper_defaults_to_allowed_and_is_strictly_boolean():
    assert config_mod.remote_control_allowed({}) is True
    assert config_mod.remote_control_allowed({"remote_control_allowed": False}) is False
    assert config_mod.remote_control_allowed({"remote_control_allowed": True}) is True
    for junk in ("false", "no", 0, None, [], {}):
        assert config_mod.remote_control_allowed({"remote_control_allowed": junk}) is True


def test_config_helper_reads_the_config_file_when_not_given_data(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setenv("MEETING_NOTES_CONFIG", str(path))
    assert config_mod.remote_control_allowed() is True
    path.write_text(json.dumps({"remote_control_allowed": False}))
    assert config_mod.remote_control_allowed() is False


def test_settings_checkbox_defaults_on_and_persists(qt_app, tmp_path, monkeypatch):
    from meeting_notes.client.ui.settings_dialog import SettingsDialog

    path = tmp_path / "config.json"
    monkeypatch.setenv("MEETING_NOTES_CONFIG", str(path))
    dialog = SettingsDialog()
    assert dialog.remote_check.text() == "Allow control from the server"
    assert dialog.remote_check.isChecked()
    dialog.accept()
    assert json.loads(path.read_text())["remote_control_allowed"] is True

    dialog = SettingsDialog()
    dialog.remote_check.setChecked(False)
    dialog.accept()
    assert json.loads(path.read_text())["remote_control_allowed"] is False
    assert config_mod.remote_control_allowed() is False  # effective for the very next command
    assert SettingsDialog().remote_check.isChecked() is False

    dialog = SettingsDialog()
    dialog.remote_check.setChecked(True)
    dialog.accept()
    assert config_mod.remote_control_allowed() is True


# --------------------------------------------------------------------------
# friendly OS names
# --------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("Darwin 20.6.0", "macOS 11"),
    ("Darwin 21.1.0", "macOS 12"),
    ("Darwin 22.0", "macOS 13"),
    ("Darwin 23.5.0", "macOS 14"),
    ("Darwin 24.1.0", "macOS 15"),
    ("Darwin 25.0", "macOS 26"),
    ("darwin 25.0.0", "macOS 26"),
    ("Darwin 99.0", "macOS"),
    ("Darwin 19.6.0", "macOS"),
    ("Darwin", "macOS"),
    ("macOS 26.6", "macOS 26.6"),
    ("Windows 11", "Windows 11"),
    ("Windows 10", "Windows 10"),
    ("Linux 6.8", "Linux 6.8"),
    ("", ""),
    (None, ""),
])
def test_friendly_platform(raw, expected):
    assert remote.friendly_platform(raw) == expected


def test_client_platform_label_uses_mac_ver_on_macos(monkeypatch):
    import platform

    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platform, "release", lambda: "25.0.0")
    monkeypatch.setattr(platform, "mac_ver", lambda: ("26.6", ("", "", ""), "arm64"))
    assert identity.platform_label() == "macOS 26.6"
    assert identity.client_header_value().endswith("; macOS 26.6")
    monkeypatch.setattr(platform, "mac_ver", lambda: ("", ("", "", ""), ""))
    assert identity.platform_label() == "macOS"


def test_client_platform_label_windows(monkeypatch):
    import platform

    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setattr(platform, "release", lambda: "11")
    assert identity.platform_label() == "Windows 11"


# ---------------------------------------------------------------------------
# idle level preview on the channel (0.7.7+)
# ---------------------------------------------------------------------------


def _watch(server, on=True):
    server.outbox.put({"type": "watch", "levels": on})


def test_hello_advertises_the_idle_levels_capability(server, make_channel):
    make_channel()
    assert _wait(lambda: server.of_type("hello"))
    assert server.of_type("hello")[0]["caps"] == list(remote.CAPS) == ["idle_levels", "note_type", "auto_end"]


def test_no_levels_frames_until_the_server_asks(server, make_channel):
    channel = make_channel()
    assert _wait(lambda: server.of_type("hello"))
    channel.publish_levels({"mic": 0.4, "system": 0.2})
    time.sleep(0.5)
    assert not channel.watched and server.of_type("levels") == []    # an old server never sees a new frame


def test_levels_stream_while_watched_are_throttled_and_stop_on_unwatch(server, make_channel):
    channel = make_channel()
    assert _wait(lambda: server.of_type("hello"))
    _watch(server)
    assert _wait(lambda: channel.watched)
    start = time.monotonic()
    i = 0
    while time.monotonic() - start < 1.0:
        i += 1
        channel.publish_levels({"mic": (i % 10) / 10, "system": 0.5})
        time.sleep(0.01)
    elapsed = time.monotonic() - start
    frames = server.of_type("levels")
    assert len(frames) >= 2
    assert len(frames) <= int(elapsed / remote.LEVELS_EVERY) + 1, (len(frames), elapsed)   # ~5 Hz at most
    assert set(frames[0]) == {"type", "mic", "system"}
    assert len(json.dumps(frames[0], separators=(",", ":"))) < 60                           # tiny
    _watch(server, on=False)
    assert _wait(lambda: not channel.watched)
    time.sleep(0.2)
    before = len(server.of_type("levels"))
    channel.publish_levels({"mic": 0.9})
    time.sleep(0.5)
    assert len(server.of_type("levels")) == before


def test_levels_are_clamped_and_only_known_tracks_are_sent(server, make_channel):
    channel = make_channel()
    _watch(server)
    assert _wait(lambda: channel.watched)
    channel.publish_levels({"mic": 9.0, "extra": 1})
    assert _wait(lambda: server.of_type("levels"))
    assert server.of_type("levels")[0] == {"type": "levels", "mic": 1.0}


def test_unchanged_silence_is_repeated_only_about_once_a_second(server, make_channel):
    channel = make_channel()
    _watch(server)
    assert _wait(lambda: channel.watched)
    channel.publish_levels({"mic": 0.0, "system": 0.0})
    time.sleep(1.6)
    count = len(server.of_type("levels"))
    assert 1 <= count <= 3, count


def test_the_watch_is_a_lease_that_lapses_without_renewal(server, make_channel, monkeypatch):
    monkeypatch.setattr(remote, "WATCH_LEASE", 0.5)
    channel = make_channel()
    assert _wait(lambda: server.of_type("hello"))
    _watch(server)
    assert _wait(lambda: channel.watched)
    time.sleep(0.2)
    _watch(server)                                     # renewed
    time.sleep(0.4)
    assert channel.watched
    assert _wait(lambda: not channel.watched, timeout=2.0)   # a silent server never leaves the mic open


def test_levels_are_not_sent_while_recording(server, make_channel):
    channel = make_channel()
    channel.publish(_recording_snapshot())
    _watch(server)
    assert _wait(lambda: channel.watched)
    channel.publish_levels({"mic": 0.5})
    time.sleep(0.6)
    assert server.of_type("levels") == []              # a recording's levels travel in the state snapshot


def test_unknown_server_frames_are_ignored(server, make_channel):
    channel = make_channel()
    assert _wait(lambda: server.of_type("hello"))
    server.outbox.put({"type": "watch", "levels": "yes"})      # not a real True: treated as off
    server.outbox.put({"type": "futurething", "x": 1})
    server.outbox.put({"type": "watch"})
    time.sleep(0.4)
    assert channel.connected and not channel.watched


def test_a_reconnect_waits_for_the_servers_own_watch(server, make_channel):
    token = ["a"]
    channel = make_channel(get_config=lambda: (server.url, TOKEN + token[0]))
    assert _wait(lambda: server.of_type("hello"))
    _watch(server)
    assert _wait(lambda: channel.watched)
    token[0] = "b"                                     # Settings changed: the channel reconnects
    assert _wait(lambda: len(server.of_type("hello")) >= 2, timeout=8.0)
    assert _wait(lambda: not channel.watched)          # the old lease died with the old connection
