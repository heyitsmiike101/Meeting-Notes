"""Server side of live recorder presence and remote control (server/recorders.py).

Runs the real app under uvicorn (tests/compat/compat_support.LiveServer) and
talks to it with the ``websockets`` sync client plus httpx, the same way a
recorder and the web page do. That avoids TestClient's single-loop limits: a
blocking command POST and a recorder answering it live in separate threads.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Any, Callable, Dict, Optional

import httpx
import pytest
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from meeting_notes import __version__, remote
from meeting_notes.server import auth, compat
from meeting_notes.server.app import create_app
from tests.compat.compat_support import LiveServer

TOKEN = "s3cret-token"

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


class Env:
    def __init__(self, app, base: str, token: Optional[str]):
        self.app = app
        self.hub = app.state.recorder_hub
        self.base = base
        self.ws_base = base.replace("http://", "ws://")
        self.token = token

    def bearer(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def web(self) -> httpx.Client:
        cookies = {auth.WEB_TOKEN_COOKIE: self.token} if self.token else {}
        return httpx.Client(base_url=self.base, cookies=cookies, timeout=10)

    def recorder(self, instance_id: Optional[str] = None, *, hello: bool = True, headers=None, token="default",
                 **fields: Any):
        """An open recorder socket (welcome already consumed unless hello=False)."""
        use = self.bearer() if token == "default" else ({"Authorization": f"Bearer {token}"} if token else {})
        ws = connect(self.ws_base + remote.CONNECT, additional_headers={**use, **(headers or {})},
                     open_timeout=5, close_timeout=2)
        ws.instance_id = instance_id or uuid.uuid4().hex
        if hello:
            ws.send(json.dumps(hello_frame(ws.instance_id, **fields)))
            welcome = json.loads(ws.recv(timeout=5))
            assert welcome == {"type": "welcome", "protocol": 1, "server_version": __version__}
        return ws

    def events(self, *, origin: Optional[str] = None, cookie: bool = True):
        headers = {}
        if self.token and cookie:
            headers["Cookie"] = f"{auth.WEB_TOKEN_COOKIE}={self.token}"
        return connect(self.ws_base + remote.EVENTS, additional_headers=headers, origin=origin,
                       open_timeout=5, close_timeout=2)


def hello_frame(instance_id: str, **over: Any) -> Dict[str, Any]:
    frame = {"type": "hello", "protocol": 1, "instance_id": instance_id, "device": "Desk PC",
             "platform": "Windows 11", "version": __version__, "state": {"status": "idle"}}
    frame.update(over)
    return frame


def wait_until(pred: Callable[[], Any], timeout: float = 5.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = pred()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


def close_code(ws) -> int:
    with pytest.raises(ConnectionClosed):
        ws.recv(timeout=5)
    return ws.protocol.close_code


def start(tmp_path, monkeypatch, token: Optional[str] = None):
    if token:
        monkeypatch.setenv("MEETING_NOTES_TOKEN", token)
    else:
        monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = create_app(data_root=str(tmp_path / "data"))
    server = LiveServer(app)
    base = server.start()
    return server, Env(app, base, token)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    server, e = start(tmp_path, monkeypatch)
    e.hub.command_timeout = 1.0
    yield e
    server.stop()


@pytest.fixture()
def secured(tmp_path, monkeypatch):
    server, e = start(tmp_path, monkeypatch, TOKEN)
    e.hub.command_timeout = 1.0
    yield e
    server.stop()


def ids(e: Env):
    return [i["instance_id"] for i in e.hub.list_items()]


# -- registry lifecycle -------------------------------------------------------


def test_connect_list_disconnect(env):
    with env.web() as http:
        assert http.get(remote.LIST).json()["items"] == []
        ws = env.recorder(device="Desk PC", version="0.7.7", platform="Windows 11")
        body = http.get(remote.LIST).json()
        assert body["server_version"] == __version__ and body["min_client_version"] == compat.min_client_version()
        (item,) = body["items"]
        assert item["instance_id"] == ws.instance_id and item["device"] == "Desk PC"
        assert item["platform"] == "windows" and item["platform_text"] == "Windows 11"
        assert item["version"] == "0.7.7" and item["behind"] is True and item["outdated"] is False
        assert item["state"]["status"] == "idle" and item["address"] and item["connected_at"] <= item["last_seen"]
        ws.close()
        wait_until(lambda: not ids(env))
        assert http.get(remote.LIST).json()["items"] == []


def test_list_is_sorted_by_device_then_connection_time(env):
    a = env.recorder(device="b-box")
    b = env.recorder(device="a-box")
    c = env.recorder(device="a-box")
    wait_until(lambda: len(ids(env)) == 3)
    order = [(i["device"], i["instance_id"]) for i in env.hub.list_items()]
    assert [d for d, _ in order] == ["a-box", "a-box", "b-box"]
    assert [i for _, i in order[:2]] == [b.instance_id, c.instance_id]
    for ws in (a, b, c):
        ws.close()


def test_old_version_is_flagged_outdated(env):
    ws = env.recorder(version="0.1.0")
    (item,) = env.hub.list_items()
    assert item["outdated"] is True and item["behind"] is True
    ws.close()


def test_version_and_platform_fall_back_to_the_client_header(env):
    ws = env.recorder(version="", platform="", headers={compat.CLIENT_HEADER: "0.7.3; macOS 26"})
    (item,) = env.hub.list_items()
    assert item["version"] == "0.7.3" and item["platform"] == "macos"
    ws.close()


def test_stale_sweep_uses_injected_clock(env):
    clock = {"now": 1000.0}
    env.hub.clock = lambda: clock["now"]
    ws = env.recorder()
    wait_until(lambda: ids(env))
    assert env.hub.sweep(1000.0 + remote.STALE_AFTER - 1) == []
    assert env.hub.sweep(1000.0 + remote.STALE_AFTER + 1) == [ws.instance_id]
    assert ids(env) == []
    assert close_code(ws) == remote.CLOSE_IDLE


def test_a_frame_keeps_a_recorder_fresh(env):
    clock = {"now": 1000.0}
    env.hub.clock = lambda: clock["now"]
    ws = env.recorder()
    clock["now"] = 1025.0
    ws.send(json.dumps({"type": "state", "state": {"status": "recording"}}))
    wait_until(lambda: env.hub.list_items()[0]["state"]["status"] == "recording")
    assert env.hub.sweep(1040.0) == []
    ws.close()


def test_same_instance_replaces_the_old_connection(env):
    iid = uuid.uuid4().hex
    first = env.recorder(iid, device="old")
    second = env.recorder(iid, device="new")
    assert close_code(first) == remote.CLOSE_REPLACED
    time.sleep(0.2)  # the old socket's cleanup must not drop the new entry
    (item,) = env.hub.list_items()
    assert item["device"] == "new"
    second.close()
    wait_until(lambda: not ids(env))


def test_recorder_limit(env):
    env.hub.max_recorders = 2
    a, b = env.recorder(), env.recorder()
    c = env.recorder(hello=False)
    c.send(json.dumps(hello_frame(uuid.uuid4().hex)))
    assert close_code(c) == 4429
    a.close()
    b.close()


# -- auth ---------------------------------------------------------------------


@pytest.mark.parametrize("token", [None, "wrong"])
def test_connect_rejects_missing_or_wrong_token(secured, token):
    ws = secured.recorder(hello=False, token=token)
    assert close_code(ws) == remote.CLOSE_UNAUTHORIZED
    assert ids(secured) == []


def test_connect_accepts_query_token(secured):
    ws = connect(f"{secured.ws_base}{remote.CONNECT}?token={TOKEN}", open_timeout=5)
    ws.send(json.dumps(hello_frame(uuid.uuid4().hex)))
    assert json.loads(ws.recv(timeout=5))["type"] == "welcome"
    ws.close()


def test_web_endpoints_need_the_web_token(secured):
    rec = secured.recorder()
    iid = rec.instance_id
    with httpx.Client(base_url=secured.base) as anon:
        assert anon.get(remote.LIST).status_code == 401
        assert anon.post(remote.command_path(iid), json={"command": "stop"}).status_code == 401
        # A bearer that is not the server token (e.g. an agent API key) is refused too.
        bad = {"Authorization": "Bearer mnk_not_the_server_token"}
        assert anon.get(remote.LIST, headers=bad).status_code == 403
        assert anon.post(remote.command_path(iid), json={"command": "stop"}, headers=bad).status_code == 403
        assert anon.get(remote.LIST, headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200
    rec.close()


def test_events_feed_needs_auth_and_same_origin(secured):
    assert close_code(secured.events(cookie=False)) == remote.CLOSE_UNAUTHORIZED
    assert close_code(secured.events(origin="http://evil.example")) == 4403
    ok = secured.events(origin=secured.base)
    assert json.loads(ok.recv(timeout=5))["type"] == "snapshot"
    ok.close()


def test_open_server_allows_everything(env):
    ws = env.recorder()
    with httpx.Client(base_url=env.base) as anon:
        assert len(anon.get(remote.LIST).json()["items"]) == 1
    ws.close()


# -- commands -----------------------------------------------------------------


def answer_once(ws, reply: Callable[[Dict[str, Any]], Dict[str, Any]]) -> threading.Thread:
    """Recorder thread: read one command frame, send ``reply(frame)`` as its ack."""
    seen: Dict[str, Any] = {}

    def run() -> None:
        frame = json.loads(ws.recv(timeout=5))
        seen.update(frame)
        ws.send(json.dumps({"type": "ack", "command_id": frame["command_id"], **reply(frame)}))

    t = threading.Thread(target=run, daemon=True)
    t.seen = seen  # type: ignore[attr-defined]
    t.start()
    return t


def test_command_is_forwarded_and_ack_returned(env):
    ws = env.recorder()
    t = answer_once(ws, lambda f: {"ok": True, "code": None, "error": None, "state": {"status": "recording",
                                   "meeting": {"name": "Standup"}}})
    with env.web() as http:
        r = http.post(remote.command_path(ws.instance_id), json={"command": "start", "args": {"name": "  Standup  "}})
    t.join(5)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["code"] is None and body["error"] is None
    assert body["state"]["status"] == "recording" and body["state"]["meeting"]["name"] == "Standup"
    assert t.seen["type"] == "command" and t.seen["command"] == "start" and t.seen["args"] == {"name": "Standup"}
    assert remote.valid_command_id(t.seen["command_id"])
    assert env.hub.list_items()[0]["state"]["status"] == "recording"  # ack state refreshed the registry
    ws.close()


def test_recorder_refusal_is_http_200_with_ok_false(env):
    ws = env.recorder()
    t = answer_once(ws, lambda f: {"ok": False, "code": "already_recording", "error": "Already recording",
                                   "state": {"status": "recording"}})
    with env.web() as http:
        r = http.post(remote.command_path(ws.instance_id), json={"command": "start"})
    t.join(5)
    assert r.status_code == 200
    assert r.json()["ok"] is False and r.json()["code"] == "already_recording"
    assert r.json()["error"] == "Already recording"
    ws.close()


def test_command_times_out_with_504_and_forgets_the_id(env):
    env.hub.command_timeout = 0.3
    ws = env.recorder()
    with env.web() as http:
        r = http.post(remote.command_path(ws.instance_id), json={"command": "stop"})
    assert r.status_code == 504 and "detail" in r.json()
    assert json.loads(ws.recv(timeout=2))["command"] == "stop"  # it was delivered, just never answered
    assert env.hub.get(ws.instance_id).pending == {}
    ws.close()


def test_late_or_unknown_ack_is_ignored(env):
    ws = env.recorder()
    ws.send(json.dumps({"type": "ack", "command_id": "deadbeef" * 4, "ok": True}))
    ws.send(json.dumps({"type": "state", "state": {"status": "recording"}}))
    wait_until(lambda: env.hub.list_items()[0]["state"]["status"] == "recording")
    ws.close()


def test_unknown_recorder_is_404_and_bad_id_400(env):
    with env.web() as http:
        assert http.post(remote.command_path(uuid.uuid4().hex), json={"command": "stop"}).status_code == 404
        assert http.post(remote.command_path("not-an-id"), json={"command": "stop"}).status_code == 400


@pytest.mark.parametrize("body", [
    {"command": "format_disk"},
    {"command": "stop", "args": {"x": "1"}},
    {"command": "mute", "args": {"track": "speaker"}},
    {"command": "mute"},
    {"command": "set_name", "args": {"name": "  "}},
    {"command": "start", "args": ["name"]},
    {"args": {}},
    ["stop"],
])
def test_bad_commands_are_rejected_and_never_reach_the_recorder(env, body):
    ws = env.recorder()
    with env.web() as http:
        r = http.post(remote.command_path(ws.instance_id), json=body)
    assert r.status_code == 400 and r.json()["detail"]
    with pytest.raises(TimeoutError):
        ws.recv(timeout=0.3)
    ws.close()


def test_non_json_body_is_400(env):
    ws = env.recorder()
    with env.web() as http:
        r = http.post(remote.command_path(ws.instance_id), content=b"nope", headers={"content-type": "application/json"})
    assert r.status_code == 400
    ws.close()


def test_recorder_disconnect_while_waiting_is_409(env):
    env.hub.command_timeout = 3.0
    ws = env.recorder()

    def hang_up() -> None:
        ws.recv(timeout=5)
        ws.close()

    threading.Thread(target=hang_up, daemon=True).start()
    with env.web() as http:
        r = http.post(remote.command_path(ws.instance_id), json={"command": "stop"})
    assert r.status_code == 409


def test_pending_commands_are_bounded(env):
    env.hub.command_timeout = 2.0
    ws = env.recorder()
    results = []

    def fire() -> None:
        with env.web() as http:
            results.append(http.post(remote.command_path(ws.instance_id), json={"command": "stop"}).status_code)

    threads = [threading.Thread(target=fire) for _ in range(6)]
    for t in threads:
        t.start()
    wait_until(lambda: results.count(429) >= 2)  # 6 requests, 4 may wait: the other 2 are refused at once
    ws.close()
    for t in threads:
        t.join(10)
    assert results.count(429) >= 2


# -- events feed ----------------------------------------------------------------


def test_events_snapshot_upsert_and_remove(env):
    existing = env.recorder(device="first")
    feed = env.events()
    snap = json.loads(feed.recv(timeout=5))
    assert snap["type"] == "snapshot" and [i["device"] for i in snap["items"]] == ["first"]
    new = env.recorder(device="second")
    up = json.loads(feed.recv(timeout=5))
    assert up["type"] == "upsert" and up["item"]["instance_id"] == new.instance_id
    new.send(json.dumps({"type": "state", "state": {"status": "recording"}}))
    up = json.loads(feed.recv(timeout=5))
    assert up["item"]["state"]["status"] == "recording"
    new.close()
    gone = json.loads(feed.recv(timeout=5))
    assert gone == {"type": "remove", "instance_id": new.instance_id}
    existing.close()
    feed.close()


def test_unchanged_state_frames_do_not_spam_the_feed(env):
    ws = env.recorder()
    feed = env.events()
    json.loads(feed.recv(timeout=5))
    for _ in range(3):
        ws.send(json.dumps({"type": "state", "state": {"status": "idle"}}))
    with pytest.raises(TimeoutError):
        feed.recv(timeout=0.4)
    ws.close()
    feed.close()


def test_slow_subscriber_is_dropped_without_blocking_the_hub(env):
    sub = env.hub._subs  # noqa: SLF001
    feed = env.events()
    json.loads(feed.recv(timeout=5))
    wait_until(lambda: len(sub) == 1)
    ws = env.recorder()
    # Flood the subscriber queue faster than a stalled reader drains it.
    target = sub[0]
    env.hub._loop.call_soon_threadsafe(lambda: [env.hub._deliver({"type": "ping"}) for _ in range(200)])  # noqa: SLF001
    wait_until(lambda: target not in sub)
    assert ids(env) == [ws.instance_id]  # the hub carried on
    ws.close()
    feed.close()


def test_events_ping_keepalive(env, monkeypatch):
    from meeting_notes.server import recorders

    monkeypatch.setattr(recorders, "EVENTS_PING_EVERY", 0.2)
    feed = env.events()
    assert json.loads(feed.recv(timeout=5))["type"] == "snapshot"
    assert json.loads(feed.recv(timeout=5)) == {"type": "ping"}
    feed.close()


# -- hello validation and payload hygiene ----------------------------------------


@pytest.mark.parametrize("frame", [
    hello_frame("nothex"),
    hello_frame(uuid.uuid4().hex, protocol=2),
    hello_frame(uuid.uuid4().hex, protocol="1"),
    {"type": "state", "state": {}},
    {"type": "hello"},
])
def test_bad_hello_closes_4400(env, frame):
    ws = env.recorder(hello=False)
    ws.send(json.dumps(frame))
    assert close_code(ws) == remote.CLOSE_BAD_HELLO
    assert ids(env) == []


def test_non_json_or_huge_hello_closes_4400(env):
    ws = env.recorder(hello=False)
    ws.send("{{{ not json")
    assert close_code(ws) == remote.CLOSE_BAD_HELLO
    ws = env.recorder(hello=False)
    ws.send(json.dumps(hello_frame(uuid.uuid4().hex, device="x" * (remote.MAX_MESSAGE_BYTES + 10))))
    assert close_code(ws) == remote.CLOSE_BAD_HELLO


def test_silent_recorder_gets_4400_after_the_hello_deadline(env, monkeypatch):
    from meeting_notes.server import recorders

    monkeypatch.setattr(recorders, "HELLO_TIMEOUT", 0.3)
    ws = env.recorder(hello=False)
    assert close_code(ws) == remote.CLOSE_BAD_HELLO


def test_hello_text_is_capped_and_state_sanitized(env):
    ws = env.recorder(device="D" * 3000, platform="P" * 500, version="9" * 100 + ".1",
                      state={"status": "exploding", "junk": "x" * 2000, "banners": [{"id": "nope"}] * 100})
    (item,) = env.hub.list_items()
    assert len(item["device"]) <= remote.MAX_TEXT and len(item["platform_text"]) <= 60
    assert len(item["version"]) <= 32
    assert item["state"]["status"] == "idle" and item["state"]["banners"] == [] and "junk" not in item["state"]
    ws.close()


def test_garbage_frames_never_crash_or_bloat_the_registry(env):
    ws = env.recorder()
    for payload in ("not json", "[1,2,3]", "null", '{"type": "mystery"}', '{"type": "state", "state": 7}',
                    json.dumps({"type": "state", "state": {"meeting": {"name": "n" * 100000}}}),
                    json.dumps({"type": "state", "state": {"tracks": {"mic": {"level": float("nan")}}}}),
                    "x" * (remote.MAX_MESSAGE_BYTES + 1)):
        ws.send(payload)
    ws.send(b"\x00\x01binary")
    ws.send(json.dumps({"type": "state", "state": {"status": "recording", "meeting": {"name": "ok"}}}))
    wait_until(lambda: env.hub.list_items()[0]["state"]["status"] == "recording")
    state = env.hub.list_items()[0]["state"]
    assert state["meeting"]["name"] == "ok"
    assert len(json.dumps(state)) < 4000
    ws.close()


def test_a_flood_of_junk_frames_gets_the_socket_closed(env):
    ws = env.recorder()
    try:
        for _ in range(80):
            ws.send("junk")
    except ConnectionClosed:  # the server may hang up before the last frame
        pass
    assert close_code(ws) == remote.CLOSE_BAD_HELLO
    wait_until(lambda: not ids(env))


def test_ack_fields_are_sanitized(env):
    ws = env.recorder()
    t = answer_once(ws, lambda f: {"ok": "yes", "code": "c" * 500, "error": {"x": 1}, "state": "garbage"})
    with env.web() as http:
        r = http.post(remote.command_path(ws.instance_id), json={"command": "stop"})
    t.join(5)
    body = r.json()
    assert body["ok"] is False and len(body["code"]) <= 40 and body["error"] is None
    assert body["state"]["status"] == "idle"
    ws.close()


def test_old_macos_recorder_shows_a_friendly_os_name(env):
    ws = env.recorder(platform="Darwin 25.0.0")
    (item,) = env.hub.list_items()
    assert item["platform"] == "macos" and item["platform_text"] == "macOS 26"
    ws.close()
    ws = env.recorder(instance_id="b" * 32, platform="", headers={compat.CLIENT_HEADER: "0.7.5; Darwin 22.6.0"})
    assert {i["platform_text"] for i in env.hub.list_items()} >= {"macOS 13"}
    ws.close()
