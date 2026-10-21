"""Tests for the mobile recorder's live-transcript stream auth (stream tickets, generations, live fence)."""

from __future__ import annotations

import dataclasses
import threading
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from meeting_notes import wire
from meeting_notes.server import auth, mobile_common, mobile_stream
from meeting_notes.server import settings as settings_mod
from meeting_notes.server.app import create_app

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated:DeprecationWarning"
)

ORIGIN = "http://testserver"
SID = "mob-1"


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_NOTES_TOKEN", "s3cret")
    monkeypatch.delenv("MEETING_NOTES_ALLOWED_ORIGINS", raising=False)
    return create_app(data_root=str(tmp_path / "data"))


@pytest.fixture
def client(app):
    return TestClient(app)


def open_session(app, sid=SID, state="open"):
    mobile_common.write_state(app.state.store, sid, {"state": state})


def ticket_for(client, sid=SID, **kw):
    headers = {"origin": ORIGIN}
    headers.update(kw)
    resp = client.post(f"/v1/mobile/sessions/{sid}/stream-ticket", headers=headers, cookies={auth.WEB_TOKEN_COOKIE: "s3cret"})
    assert resp.status_code == 200, resp.text
    return resp.json()


def connect(client, ticket, origin=ORIGIN):
    headers = {"origin": origin} if origin else {}
    return client.websocket_connect(wire.STREAM, subprotocols=["mn-ticket." + ticket], headers=headers)


def hello(sid=SID, **kw):
    return wire.to_json(wire.Hello(session_id=sid, name="Phone", device="iPhone", tracks=["system"], **kw))


def frame(seconds=0.1, offset=0, track="system"):
    return wire.encode_audio_frame(track, offset, b"\x00\x00" * int(seconds * wire.STREAM_SAMPLE_RATE))


def expect_close(ws, code):
    with pytest.raises(WebSocketDisconnect) as info:
        for _ in range(5):
            ws.receive_json()  # a ServerError message may come before the close
    assert info.value.code == code


# -- ticket issuance ---------------------------------------------------------------------------------------------


def test_ticket_issuance(app, client):
    open_session(app)
    body = ticket_for(client)
    assert body["expires_in"] == 60 and body["generation"] == 1 and len(body["ticket"]) >= 32
    assert ticket_for(client)["generation"] == 2


def test_ticket_requires_auth(app, client):
    open_session(app)
    resp = client.post(f"/v1/mobile/sessions/{SID}/stream-ticket", headers={"origin": ORIGIN})
    assert resp.status_code == 401


def test_ticket_cookie_needs_origin_but_bearer_does_not(app, client):
    open_session(app)
    cookies = {auth.WEB_TOKEN_COOKIE: "s3cret"}
    assert client.post(f"/v1/mobile/sessions/{SID}/stream-ticket", cookies=cookies).status_code == 403
    assert client.post(f"/v1/mobile/sessions/{SID}/stream-ticket", cookies=cookies, headers={"origin": "http://evil.example"}).status_code == 403
    resp = client.post(f"/v1/mobile/sessions/{SID}/stream-ticket", headers={"authorization": "Bearer s3cret"})
    assert resp.status_code == 200


def test_ticket_conflicts_when_not_open_or_not_mobile(app, client):
    assert client.post("/v1/mobile/sessions/nope/stream-ticket", headers={"authorization": "Bearer s3cret"}).status_code == 409
    open_session(app, state="preparing")
    assert client.post(f"/v1/mobile/sessions/{SID}/stream-ticket", headers={"authorization": "Bearer s3cret"}).status_code == 409


# -- websocket with a ticket ---------------------------------------------------------------------------------------


def test_ticketed_stream_works_and_shows_in_live(app, client):
    open_session(app)
    t = ticket_for(client)["ticket"]
    with connect(client, t) as ws:
        assert ws.accepted_subprotocol == "mn-ticket." + t
        ws.send_json(hello())
        ws.send_bytes(frame(0.1))
        assert ws.receive_json() == {"type": "ack", "track": "system", "frames": 1600}
        items = client.get("/v1/live", headers={"authorization": "Bearer s3cret"}).json()["items"]
        assert items[0]["name"] == "Phone" and items[0]["device"] == "iPhone"
    assert client.get("/v1/live", headers={"authorization": "Bearer s3cret"}).json()["total"] == 0


def test_wrong_or_missing_origin_rejected(app, client):
    open_session(app)
    for origin in ("http://evil.example", None):
        t = ticket_for(client)["ticket"]
        with connect(client, t, origin=origin) as ws:
            expect_close(ws, 4401)


def test_ticket_is_single_use(app, client):
    open_session(app)
    t = ticket_for(client)["ticket"]
    with connect(client, t) as ws:
        ws.send_json(hello())
        ws.send_bytes(frame())
        ws.receive_json()
    with connect(client, t) as ws:
        expect_close(ws, 4401)


def test_unknown_ticket_rejected(client):
    with connect(client, "bogus") as ws:
        expect_close(ws, 4401)


def test_expired_ticket_rejected(app, client):
    open_session(app)
    t = ticket_for(client)["ticket"]
    store = app.state.mobile_stream.tickets
    real = store.clock
    store.clock = lambda: real() + 61
    with connect(client, t) as ws:
        expect_close(ws, 4401)


def test_tickets_stored_hashed_and_capped(app, client):
    open_session(app)
    t = ticket_for(client)["ticket"]
    tickets = app.state.mobile_stream.tickets
    assert t not in tickets._tickets
    for _ in range(mobile_stream.MAX_TICKETS + 5):
        tickets.issue(SID)
    assert len(tickets._tickets) == mobile_stream.MAX_TICKETS


def test_hello_session_mismatch_forbidden(app, client):
    open_session(app)
    open_session(app, "mob-2")
    t = ticket_for(client)["ticket"]
    with connect(client, t) as ws:
        ws.send_json(hello("mob-2"))
        expect_close(ws, 4403)


def test_non_system_track_rejected(app, client):
    open_session(app)
    t = ticket_for(client)["ticket"]
    with connect(client, t) as ws:
        ws.send_json(wire.to_json(wire.Hello(session_id=SID, tracks=["mic"])))
        expect_close(ws, 4403)
    t = ticket_for(client)["ticket"]
    with connect(client, t) as ws:
        ws.send_json(hello())
        ws.send_bytes(frame(track="mic"))
        expect_close(ws, 4403)


def test_generation_replaces_older_socket(app, client):
    open_session(app)
    t1 = ticket_for(client)["ticket"]
    t2 = ticket_for(client)["ticket"]
    with connect(client, t1) as old:
        old.send_json(hello())
        old.send_bytes(frame(0.1))
        old.receive_json()
        with connect(client, t2) as new:
            new.send_json(hello())
            new.send_bytes(frame(0.1, offset=1600))
            assert new.receive_json()["frames"] == 3200
            expect_close(old, 4410)
            # the old socket's teardown must not remove the new one from the live list
            assert client.get("/v1/live", headers={"authorization": "Bearer s3cret"}).json()["total"] == 1


def test_older_generation_does_not_replace_newer(app, client):
    open_session(app)
    t1 = ticket_for(client)["ticket"]
    t2 = ticket_for(client)["ticket"]
    with connect(client, t2) as new:
        new.send_json(hello())
        new.send_bytes(frame())
        new.receive_json()
        with connect(client, t1) as stale:
            stale.send_json(hello())
            expect_close(stale, 4409)


def test_desktop_stream_is_not_displaced_by_ticket(app, client):
    open_session(app)
    t = ticket_for(client)["ticket"]
    with client.websocket_connect(wire.STREAM + "?token=s3cret") as desktop:
        desktop.send_json(wire.to_json(wire.Hello(session_id=SID, tracks=["system"])))
        desktop.send_bytes(frame())
        desktop.receive_json()
        with connect(client, t) as ws:
            ws.send_json(hello())
            expect_close(ws, 4409)


def test_fence_after_state_leaves_open_refuses_writes(app, client, monkeypatch):
    monkeypatch.setattr(mobile_stream, "FENCE_RECHECK_SECONDS", 0)
    open_session(app)
    t = ticket_for(client)["ticket"]
    with connect(client, t) as ws:
        ws.send_json(hello())
        ws.send_bytes(frame())
        ws.receive_json()
        open_session(app, state="preparing")
        ws.send_bytes(frame(offset=1600))
        expect_close(ws, 4411)
    assert not app.state.store.track_raw_path(SID, "system").read_bytes()[3200:]


def test_fence_live_closes_socket(app, client):
    open_session(app)
    t = ticket_for(client)["ticket"]
    with connect(client, t) as ws:
        ws.send_json(hello())
        ws.send_bytes(frame())
        ws.receive_json()
        mobile_common.fence_live(SID)
        expect_close(ws, 4411)


def test_ticket_for_session_finished_before_connect_rejected(app, client):
    open_session(app)
    t = ticket_for(client)["ticket"]
    open_session(app, state="finalized")
    with connect(client, t) as ws:
        ws.send_json(hello())
        expect_close(ws, 4411)


def test_rate_limit_closes_4400(app, client):
    open_session(app)
    t = ticket_for(client)["ticket"]
    with connect(client, t) as ws:
        ws.send_json(hello())
        ws.send_bytes(frame(mobile_stream.BURST_SECONDS + 5))
        expect_close(ws, 4400)


def test_offset_bound_closes_4400(app, client):
    open_session(app)
    t = ticket_for(client)["ticket"]
    with connect(client, t) as ws:
        ws.send_json(hello())
        ws.send_bytes(frame(0.1, offset=mobile_stream.MAX_OFFSET_FRAMES))
        expect_close(ws, 4400)


def test_token_bucket_refills():
    conn = mobile_stream.Conn(SID, 1, None, None)
    now = conn.last_refill
    assert conn.spend(int(mobile_stream.BURST_SECONDS * 16000), now)
    assert not conn.spend(16000, now)
    assert conn.spend(16000, now + 1)  # 1 s of wall time buys 2 s of audio


# -- unchanged desktop path ----------------------------------------------------------------------------------------


def test_desktop_token_stream_unchanged(app, client):
    with client.websocket_connect(wire.STREAM + "?token=s3cret") as ws:
        ws.send_json(wire.to_json(wire.Hello(session_id="desk-1", tracks=["mic"])))
        ws.send_bytes(frame(0.1, track="mic"))
        assert ws.receive_json()["frames"] == 1600
    with client.websocket_connect(wire.STREAM) as ws:
        expect_close(ws, 4401)


# -- login cookie Secure -------------------------------------------------------------------------------------------


def _login_cookie(app, address, host="testserver"):
    root = app.state.store.root
    s = settings_mod.load_settings(root)
    settings_mod.save_settings(root, dataclasses.replace(s, server_address=address))
    resp = TestClient(app).post("/login", data={"token": "s3cret"}, headers={"Host": host}, follow_redirects=False)
    return resp.headers["set-cookie"].lower()


def test_login_cookie_secure_only_on_the_https_address(app):
    assert "secure" not in _login_cookie(app, "http://meeting.lan", "meeting.lan")
    assert "secure" in _login_cookie(app, "https://meeting.example.com", "meeting.example.com")
    # signing in on the plain-http LAN name next to an https server_address: a Secure cookie would be dropped
    assert "secure" not in _login_cookie(app, "https://meeting.example.com", "meeting.lan")
