"""Live notes ("Your notes") over the recorder channel: server side (server/recorders.py, app.py, remote.py)."""

from __future__ import annotations

import json
import time

import httpx
import pytest

from meeting_notes import remote
from tests.test_recorders_server import env, ids, secured, wait_until  # noqa: F401 - fixtures

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

SESSION = "20261023-100000-Sync-PC"


def notes_frame(text, session_id=SESSION, seq=1):
    return json.dumps({"type": "notes", "text": text, "session_id": session_id, "seq": seq})


def _first(http):
    return http.get(remote.LIST).json()["items"][0]


def test_a_notes_frame_is_stored_listed_and_truncated_in_the_list(env):  # noqa: F811
    from meeting_notes.server.recorders import NOTES_PREVIEW_CHARS

    ws = env.recorder()
    with env.web() as http:
        assert _first(http)["notes"] is None
        ws.send(notes_frame("short note https://x.example", seq=1))
        item = wait_until(lambda: _first(http) if _first(http)["notes"] else None)
        assert item["notes"] == {"text": "short note https://x.example", "truncated": False, "length": 28,
                                 "session_id": SESSION, "seq": 1, "updated_at": item["notes"]["updated_at"]}
        big = "a" * (NOTES_PREVIEW_CHARS + 500)
        ws.send(notes_frame(big, seq=2))
        item = wait_until(lambda: _first(http) if _first(http)["notes"]["seq"] == 2 else None)
        assert item["notes"]["truncated"] is True and item["notes"]["text"] == "a" * NOTES_PREVIEW_CHARS
        assert item["notes"]["length"] == len(big)
        full = http.get(remote.NOTES.format(instance_id=ws.instance_id))
        assert full.status_code == 200
        body = full.json()
        assert body["text"] == big and body["seq"] == 2 and body["session_id"] == SESSION
        assert isinstance(body["updated_at"], float)
    ws.close()


def test_events_carry_the_notes_and_an_empty_text_clears_them(env):  # noqa: F811
    ws = env.recorder()
    feed = env.events()
    json.loads(feed.recv(timeout=5))
    ws.send(notes_frame("typed", seq=1))
    up = json.loads(feed.recv(timeout=5))
    assert up["type"] == "upsert" and up["item"]["notes"]["text"] == "typed"
    ws.send(notes_frame("typed", seq=2))  # unchanged text: nothing for the pages
    with pytest.raises(TimeoutError):
        feed.recv(timeout=0.4)
    ws.send(notes_frame("", session_id=None, seq=3))
    up = json.loads(feed.recv(timeout=5))
    assert up["item"]["notes"] is None
    assert env.hub.notes_of(ws.instance_id) is None
    with env.web() as http:
        assert http.get(remote.NOTES.format(instance_id=ws.instance_id)).status_code == 404
    ws.close()
    feed.close()


def test_bad_notes_frames_are_ignored_and_an_old_seq_loses(env):  # noqa: F811
    ws = env.recorder()
    ws.send(notes_frame("good", seq=5))
    wait_until(lambda: env.hub.notes_of(ws.instance_id))
    for bad in (
        {"type": "notes", "text": 5, "session_id": None, "seq": 6},
        {"type": "notes", "text": "x", "session_id": "../evil", "seq": 6},
        {"type": "notes", "text": "x", "session_id": None, "seq": "6"},
        {"type": "notes", "text": "x", "session_id": None, "seq": True},
        {"type": "notes", "text": "x", "session_id": None, "seq": -1},
        {"type": "notes", "text": "stale", "session_id": None, "seq": 4},
        {"type": "notes"},
    ):
        ws.send(json.dumps(bad))
    ws.send(json.dumps({"type": "state", "state": {"status": "idle"}}))
    time.sleep(0.3)
    assert env.hub.notes_of(ws.instance_id)["text"] == "good"
    assert ids(env) == [ws.instance_id]  # still connected: a few bad frames never hang up
    ws.close()


def test_a_large_notes_frame_is_accepted_up_to_the_cap(env):  # noqa: F811
    from meeting_notes import scratchpad

    ws = env.recorder()
    ws.send(notes_frame("é" * (scratchpad.MAX_CHARS + 10), seq=1))
    notes = wait_until(lambda: env.hub.notes_of(ws.instance_id))
    assert len(notes["text"]) == scratchpad.MAX_CHARS == 200_000
    ws.close()


def test_notes_need_auth_and_the_live_route_finds_the_recorders_meeting(secured):  # noqa: F811
    ws = secured.recorder()
    ws.send(notes_frame("live text", session_id="sess-1", seq=1))
    wait_until(lambda: secured.hub.notes_of(ws.instance_id))
    url = remote.NOTES.format(instance_id=ws.instance_id)
    with httpx.Client(base_url=secured.base, timeout=10) as anon:
        assert anon.get(url).status_code == 401
        assert anon.get("/v1/live/sess-1/notes").status_code == 401
    with secured.web() as http:
        assert http.get(url).json()["text"] == "live text"
        live = http.get("/v1/live/sess-1/notes")
        assert live.status_code == 200
        assert live.json()["text"] == "live text" and live.json()["instance_id"] == ws.instance_id
        assert http.get("/v1/live/other/notes").status_code == 404
        assert http.get("/v1/recorders/not-an-id/notes").status_code == 400
        assert http.get(remote.NOTES.format(instance_id="f" * 32)).status_code == 404
    ws.close()
    with secured.web() as http:  # gone with the recorder: memory only
        wait_until(lambda: http.get("/v1/live/sess-1/notes").status_code == 404)


def test_sanitize_notes_normalises_and_caps():
    assert remote.sanitize_notes({"text": "a\r\nb\x00", "session_id": None, "seq": 0}) == \
        {"text": "a\nb", "session_id": None, "seq": 0}
    assert remote.sanitize_notes({"text": "  \n", "session_id": "s", "seq": 1})["text"] == ""
    assert remote.sanitize_notes("nope") is None
