"""The web side of live "Your notes": the rendered-HTML routes and the page hooks that show them."""

from __future__ import annotations

import json
import re

import httpx
import pytest

from meeting_notes import remote
from meeting_notes.server import web
from meeting_notes.server.web import render_recorders_page, stylesheet_text
from tests.test_recorders_server import env, secured, wait_until  # noqa: F401 - fixtures

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

SESSION = "sess-1"


def notes_frame(text, session_id=SESSION, seq=1):
    return json.dumps({"type": "notes", "text": text, "session_id": session_id, "seq": seq})


def _html_url(ws):
    return remote.NOTES.format(instance_id=ws.instance_id) + ".html"


def test_recorder_notes_html_renders_markdown_and_escapes(env):  # noqa: F811
    ws = env.recorder()
    ws.send(notes_frame("# Plan\n\n- [ ] ask <script>alert(1)</script>\n\n**bold**"))
    wait_until(lambda: env.hub.notes_of(ws.instance_id))
    with env.web() as http:
        r = http.get(_html_url(ws))
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
        assert "<strong>bold</strong>" in r.text and "<h2" in r.text
        assert "<script>" not in r.text and "&lt;script&gt;" in r.text
    ws.close()


def test_recorder_notes_html_404_and_400(env):  # noqa: F811
    ws = env.recorder()
    with env.web() as http:
        assert http.get(_html_url(ws)).status_code == 404  # connected, no notes
        assert http.get(remote.NOTES.format(instance_id="f" * 32) + ".html").status_code == 404  # not connected
        assert http.get("/v1/recorders/not-an-id/notes.html").status_code == 400
    ws.close()


def test_recorder_notes_html_whitespace_text_is_an_empty_200(env):  # noqa: F811
    ws = env.recorder()
    # A whitespace-only frame clears the notes (404); an empty text can only be held from outside the frame path.
    with env.web() as http:
        ws.send(notes_frame("   \n  "))
        assert http.get(_html_url(ws)).status_code == 404
    env.hub.get(ws.instance_id).notes = {"text": "", "session_id": SESSION, "seq": 1, "updated_at": 1.0}
    with env.web() as http:
        r = http.get(_html_url(ws))
        assert r.status_code == 200 and r.text.strip() == ""
    ws.close()


def test_notes_html_routes_need_auth_and_the_live_one_finds_the_meeting(secured):  # noqa: F811
    ws = secured.recorder()
    ws.send(notes_frame("live *text*", session_id=SESSION, seq=1))
    wait_until(lambda: secured.hub.notes_of(ws.instance_id))
    with httpx.Client(base_url=secured.base, timeout=10) as anon:
        assert anon.get(_html_url(ws)).status_code == 401
        assert anon.get(f"/v1/live/{SESSION}/notes.html").status_code == 401
    with secured.web() as http:
        assert "<em>text</em>" in http.get(_html_url(ws)).text
        live = http.get(f"/v1/live/{SESSION}/notes.html")
        assert live.status_code == 200 and live.headers["content-type"].startswith("text/html")
        assert "<em>text</em>" in live.text
        assert http.get("/v1/live/other/notes.html").status_code == 404
    ws.close()
    with secured.web() as http:
        wait_until(lambda: http.get(f"/v1/live/{SESSION}/notes.html").status_code == 404)


def test_live_items_carry_the_notes_seq(env):  # noqa: F811
    ws = env.recorder()
    live = env.app.state.live_sessions
    live[SESSION] = {"session_id": SESSION, "name": "M", "device": "PC", "partials": []}
    live["no-notes"] = {"session_id": "no-notes", "name": "N", "device": "PC", "partials": []}
    ws.send(notes_frame("typed", session_id=SESSION, seq=7))
    wait_until(lambda: env.hub.notes_of(ws.instance_id))
    with env.web() as http:
        items = {i["session_id"]: i for i in http.get("/v1/live").json()["items"]}
    assert items[SESSION]["notes_seq"] == 7 and items["no-notes"]["notes_seq"] is None
    ws.close()


# -- page hooks ----------------------------------------------------------------


def test_recorders_page_has_the_notes_block_and_fetch_hooks():
    page = render_recorders_page(token_configured=True)
    js = web._RECORDERS_JS
    assert 'data-r="notes" hidden' in js and 'data-r="notesBody"' in js and "Your notes" in js
    assert "scratchpad-rendered" in js
    assert "/notes.html" in js and "card._notesKey" in js and "n.seq" in js
    assert "(draft, not recording yet)" in js and "n.truncated" in js and "n.length" in js
    assert "setTimeout" in js and "}, 500);" in js and "recUpdateNotes(card, it)" in js
    assert "credentials: 'same-origin'" in js
    assert "rec-grid" in page
    css = stylesheet_text()
    assert ".cw-notes" in css and ".cw-notes .scratchpad-rendered" in css


def test_dashboard_live_view_shows_the_notes():
    text = open(web.__file__, encoding="utf-8").read()
    assert "'/v1/live/' + encodeURIComponent(id) + '/notes.html'" in text
    assert "item.notes_seq" in text and "liveNotesBlock(item)" in text and "live-overlay-notes" in text
    assert re.search(r"liveNotes\[id\]\.seq === seq", text)
