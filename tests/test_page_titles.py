"""Every web page's tab title reads ``Meeting Notes | <Subpage>``."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from notion_helpers import add_meeting

from meeting_notes.server import web
from meeting_notes.server.app import create_app

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated:DeprecationWarning"
)


def _title(page: str) -> str:
    return re.search(r"<title>(.*?)</title>", page, re.S).group(1)


def test_every_rendered_page_uses_the_brand_prefix():
    assert _title(web.render_login_page()) == "Meeting Notes | Sign in"
    assert _title(web.render_home_page(token_configured=True)) == "Meeting Notes | Home"
    assert _title(web.render_transcriptions_page(token_configured=True)) == "Meeting Notes | Meetings"
    assert _title(web.render_trash_page(token_configured=True)) == "Meeting Notes | Recently deleted"
    assert _title(web.render_recorders_page(token_configured=True)) == "Meeting Notes | Recorders"
    assert _title(web.render_settings_page(web.settings_mod.Settings(), token_configured=True)) == "Meeting Notes | Settings"
    assert _title(web.render_install_page("http://x", token_configured=True)) == "Meeting Notes | Install"


def test_a_meeting_page_uses_the_escaped_name_and_updates_client_side():
    page = web.render_transcriptions_page(token_configured=True, initial_session_id="x", page_title='Q3 <b>"plan"</b> & co')
    assert _title(page) == "Meeting Notes | Q3 &lt;b&gt;&quot;plan&quot;&lt;/b&gt; &amp; co"
    # Loaded, renamed and closed on the client: the tab title follows.
    assert "document.title='Meeting Notes | '+(name||'Meeting')" in page
    assert "setDocTitle(meta.name||id)" in page and "setDocTitle(name)" in page
    assert "document.title='Meeting Notes | Meetings'" in page


def test_session_route_titles_the_page_with_the_meeting_name(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = create_app(data_root=str(tmp_path / "data"))
    add_meeting(app.state.store, "m1", "Roadmap <sync>", "2026-09-30 10:00", complete=False, queue_review=False)
    client = TestClient(app)
    page = client.get("/sessions/m1")
    assert page.status_code == 200
    assert _title(page.text) == "Meeting Notes | Roadmap &lt;sync&gt;"
    assert _title(client.get("/meetings").text) == "Meeting Notes | Meetings"
