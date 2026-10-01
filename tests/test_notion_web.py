"""Notion export through the HTTP surface: settings, connection, per-meeting status, bulk/backfill,
the agent API, and the rendered pages. A fake Notion stands behind the client; no real API."""

from __future__ import annotations

import json
import logging
from datetime import timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from fake_notion import TOKEN, FakeNotion
from notion_helpers import TZ, add_meeting, notes
from meeting_notes.server.app import create_app
from meeting_notes.server import settings as settings_mod

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated:DeprecationWarning"
)

WEB_TOKEN = "web-secret"
WEB = {"Authorization": f"Bearer {WEB_TOKEN}"}


class Ctx:
    pass


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_NOTES_TOKEN", WEB_TOKEN)
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    c = Ctx()
    c.fake = FakeNotion()
    c.root_page = c.fake.add_page("Notes root")
    c.app = create_app(
        data_root=str(tmp_path / "data"),
        notion_options={"transport": c.fake.transport(), "sleep": lambda s: None, "min_interval": 0.0, "tz": TZ},
    )
    c.client = TestClient(c.app)
    c.store = c.app.state.store
    c.notion = c.app.state.notion
    return c


def connect(c):
    r = c.client.put("/v1/notion/token", json={"token": TOKEN}, headers=WEB)
    assert r.status_code == 200, r.text


def current(c) -> dict:
    payload = c.client.get("/v1/settings", headers=WEB).json()
    payload["model"] = payload.get("model") or "base.en"  # no MEETING_NOTES_MODEL in tests
    return payload


def set_parent(c, parent=None, auto=True):
    page = parent or c.root_page
    payload = current(c)
    payload["notion_parents"] = {"standard": f"https://www.notion.so/Notes-root-{page.replace('-', '')}"}
    payload["notion_auto_copy"] = auto
    r = c.client.put("/v1/settings", json=payload, headers=WEB)
    assert r.status_code == 200, r.text
    return r.json()


def drain(c):
    c.notion.run_pending()


# -- settings ---------------------------------------------------------------------------------


def test_settings_accept_page_links_and_normalise_to_ids(ctx):
    saved = set_parent(ctx)
    assert saved["notion_parents"] == {"standard": ctx.root_page.replace("-", "")}
    assert saved["notion_auto_copy"] is True
    again = ctx.client.get("/v1/settings", headers=WEB).json()
    assert again["notion_parents"] == saved["notion_parents"]


def test_invalid_parent_page_is_a_400_naming_the_style(ctx):
    payload = current(ctx)
    payload["notion_parents"] = {"webinar": "https://example.com/not-notion"}
    r = ctx.client.put("/v1/settings", json=payload, headers=WEB)
    assert r.status_code == 400 and "Detailed webinar" in r.json()["detail"]
    payload["notion_parents"] = {"standard": "12345"}
    assert ctx.client.put("/v1/settings", json=payload, headers=WEB).status_code == 400


def test_older_clients_that_omit_notion_fields_keep_them(ctx):
    set_parent(ctx)
    payload = current(ctx)
    for key in ("notion_parents", "notion_auto_copy"):
        payload.pop(key)
    assert ctx.client.put("/v1/settings", json=payload, headers=WEB).status_code == 200
    after = ctx.client.get("/v1/settings", headers=WEB).json()
    assert after["notion_auto_copy"] is True and after["notion_parents"]["standard"]


def test_web_form_saves_parents_and_auto_copy_and_stale_form_keeps_them(ctx):
    page = ctx.root_page.replace("-", "")
    form = {"model": "base.en", "beam_size": "5", "audio_retention_days": "-1",
            "retention_check_interval_minutes": "60", "ai_provider": "disabled",
            "notion_auto_copy": "on", "notion_parents": json.dumps({"standard": page, "quick": ""})}
    r = ctx.client.post("/settings", data=form, headers=WEB)
    assert r.status_code == 200 and "Settings saved" in r.text
    saved = settings_mod.load_settings(ctx.store.root)
    assert saved.notion_parents == {"standard": page} and saved.notion_auto_copy is True
    stale = {k: v for k, v in form.items() if not k.startswith("notion")}
    ctx.client.post("/settings", data=stale, headers=WEB)
    kept = settings_mod.load_settings(ctx.store.root)
    assert kept.notion_parents == {"standard": page} and kept.notion_auto_copy is True
    bad = dict(form, notion_parents=json.dumps({"standard": "nope"}))
    r = ctx.client.post("/settings", data=bad, headers=WEB)
    assert "Notion page for &#x27;Standard&#x27;" in r.text or "Notion page for 'Standard'" in r.text


def test_deleting_a_style_drops_its_parent(ctx):
    payload = current(ctx)
    payload["note_templates"] = payload["note_templates"] + [{"id": "tcust", "name": "Custom", "prompt": "p"}]
    payload["notion_parents"] = {"tcust": ctx.root_page}
    assert ctx.client.put("/v1/settings", json=payload, headers=WEB).json()["notion_parents"]["tcust"]
    payload["note_templates"] = [t for t in payload["note_templates"] if t["id"] != "tcust"]
    assert ctx.client.put("/v1/settings", json=payload, headers=WEB).json()["notion_parents"] == {}


# -- connection --------------------------------------------------------------------------------


def test_connect_status_test_and_remove(ctx):
    assert ctx.client.get("/v1/notion", headers=WEB).json()["connected"] is False
    bad = ctx.client.put("/v1/notion/token", json={"token": "ntn_nope"}, headers=WEB)
    assert bad.status_code == 400 and "rejected" in bad.json()["detail"]
    assert "ntn_nope" not in bad.text
    connect(ctx)
    state = ctx.client.get("/v1/notion", headers=WEB).json()
    assert state["connected"] and state["bot_name"] == "Meeting Notes bot" and state["source"] == "settings"
    assert state["workspace_name"] == "Mike's workspace"
    test = ctx.client.post("/v1/notion/test", headers=WEB).json()
    assert test == {"ok": True, "name": "Meeting Notes bot", "workspace": "Mike's workspace"}
    assert ctx.client.delete("/v1/notion/token", headers=WEB).json()["connected"] is False
    assert ctx.client.post("/v1/notion/test", headers=WEB).json()["ok"] is False


def test_notion_endpoints_need_the_web_token(ctx):
    for method, path in (("get", "/v1/notion"), ("put", "/v1/notion/token"), ("delete", "/v1/notion/token"),
                         ("post", "/v1/notion/test"), ("post", "/v1/notion/backfill"),
                         ("get", "/v1/sessions/x/notion"), ("post", "/v1/sessions/x/notion")):
        assert getattr(ctx.client, method)(path).status_code in (401, 403), path


def test_token_is_never_returned_by_any_api_or_written_to_settings(ctx, caplog):
    caplog.set_level(logging.DEBUG)
    connect(ctx)
    set_parent(ctx)
    add_meeting(ctx.store, "m1", "Planning", "2026-09-30 10:00")
    drain(ctx)
    keys = ctx.app.state.agent_keys
    key = keys.create("agent", ["read", "write"])["key"]
    agent = {"Authorization": f"Bearer {key}"}
    bodies = [
        ctx.client.get("/v1/settings", headers=WEB).text,
        ctx.client.get("/v1/notion", headers=WEB).text,
        ctx.client.get("/v1/sessions/m1/notion", headers=WEB).text,
        ctx.client.get("/v1/sessions/m1", headers=WEB).text,
        ctx.client.get("/settings", headers=WEB).text,
        ctx.client.get("/api/v1/meetings", headers=agent).text,
        ctx.client.get("/api/v1/meetings/m1", headers=agent).text,
        ctx.client.get("/api/v1/meetings/m1/notes", headers=agent).text,
        ctx.client.get("/api/v1/manifest").text,
        ctx.client.get("/api-docs.md").text,
        (ctx.store.root / "settings.json").read_text(),
        caplog.text,
    ]
    for body in bodies:
        assert TOKEN not in body
    assert (ctx.store.root / "notion" / "token").read_text().strip() == TOKEN


def test_env_token_is_reported_as_env_and_cannot_be_replaced(ctx, monkeypatch):
    monkeypatch.setenv("NOTION_TOKEN", TOKEN)
    state = ctx.client.get("/v1/notion", headers=WEB).json()
    assert state["connected"] and state["source"] == "env"
    r = ctx.client.put("/v1/notion/token", json={"token": "ntn_other"}, headers=WEB)
    assert r.status_code == 400 and "NOTION_TOKEN" in r.json()["detail"]
    assert TOKEN not in json.dumps(state)


# -- per meeting ----------------------------------------------------------------------------------


def test_bridge_completion_copies_automatically_end_to_end(ctx):
    connect(ctx)
    set_parent(ctx)
    ctx.store.write_session_meta("s1", {"name": "Board sync", "started_wall": 1_790_000_000})
    import meeting_notes.wire as wire

    job = ctx.store.create_job("s1")
    ctx.store.update_job(job, state=wire.JobState.DONE, progress=1.0)
    ctx.store.write_transcript(job, "# t", json.dumps({"segments": []}))
    review = ctx.client.post("/v1/sessions/s1/review", headers=WEB).json()
    claim = ctx.client.get("/v1/bridge/review/claim", headers=WEB)
    assert claim.status_code == 200
    r = ctx.client.post(f"/v1/bridge/review/{review['review_id']}/complete", json={"notes": notes()}, headers=WEB)
    assert r.status_code == 200
    assert ctx.client.get("/v1/sessions/s1/notion", headers=WEB).json()["state"] in ("pending", "copied")
    drain(ctx)
    status = ctx.client.get("/v1/sessions/s1/notion", headers=WEB).json()
    assert status["state"] == "copied" and status["url"] and status["style"]["name"] == "Standard"
    assert status["can_send"] is True and status["connected"] is True
    month = [n for n in ctx.fake.nodes.values() if n["type"] == "page" and n["title"].endswith("Standard")]
    assert len(month) == 1


def test_status_and_manual_send_with_disabled_reasons(ctx):
    add_meeting(ctx.store, "m1", "Planning", "2026-09-30 10:00")
    s = ctx.client.get("/v1/sessions/m1/notion", headers=WEB).json()
    assert s["state"] == "none" and s["can_send"] is False and "not connected" in s["reason"]
    assert ctx.client.post("/v1/sessions/m1/notion", headers=WEB).status_code == 409
    connect(ctx)
    s = ctx.client.get("/v1/sessions/m1/notion", headers=WEB).json()
    assert s["can_send"] is False and "no Notion parent page" in s["reason"] and "Standard" in s["reason"]
    r = ctx.client.post("/v1/sessions/m1/notion", headers=WEB)
    assert r.status_code == 409 and "parent page" in r.json()["detail"]
    set_parent(ctx, auto=False)
    ok = ctx.client.post("/v1/sessions/m1/notion", headers=WEB)
    assert ok.status_code == 200 and ok.json()["state"] == "pending"
    drain(ctx)
    done = ctx.client.get("/v1/sessions/m1/notion", headers=WEB).json()
    assert done["state"] == "copied" and done["month_page"] == "September-2026 Standard"
    # the link back to the meeting uses the request's own address when no server_address is configured
    toggle = next(n for n in ctx.fake.nodes.values() if n["type"] == "heading_1")
    last = ctx.fake.nodes[toggle["children"][-1]]["paragraph"]["rich_text"][0]["text"]["link"]["url"]
    assert last == "http://testserver/sessions/m1"


def test_no_notes_and_unknown_session(ctx):
    connect(ctx)
    set_parent(ctx)
    add_meeting(ctx.store, "m1", "Planning", "2026-09-30 10:00", queue_review=False)
    s = ctx.client.get("/v1/sessions/m1/notion", headers=WEB).json()
    assert s["can_send"] is False and "no notes" in s["reason"]
    assert ctx.client.post("/v1/sessions/m1/notion", headers=WEB).status_code == 409
    assert ctx.client.get("/v1/sessions/nope/notion", headers=WEB).status_code == 404
    assert ctx.client.post("/v1/sessions/nope/notion", headers=WEB).status_code == 404


def test_failed_status_carries_a_readable_reason_and_retry_works(ctx):
    connect(ctx)
    set_parent(ctx)
    page = ctx.root_page
    saved = ctx.fake.nodes.pop(page)
    add_meeting(ctx.store, "m1", "Planning", "2026-09-30 10:00")
    drain(ctx)
    s = ctx.client.get("/v1/sessions/m1/notion", headers=WEB).json()
    assert s["state"] == "failed" and "share it with the integration" in s["error"]
    ctx.fake.nodes[page] = saved  # the user shared the page
    assert ctx.client.post("/v1/sessions/m1/notion", headers=WEB).json()["state"] == "pending"
    drain(ctx)
    assert ctx.client.get("/v1/sessions/m1/notion", headers=WEB).json()["state"] == "copied"


def test_rename_through_the_api_updates_notion(ctx):
    connect(ctx)
    set_parent(ctx)
    add_meeting(ctx.store, "m1", "Planning", "2026-09-30 10:00")
    drain(ctx)
    assert ctx.client.patch("/v1/sessions/m1", json={"name": "Roadmap"}, headers=WEB).status_code == 200
    drain(ctx)
    page = ctx.fake.find_page("September-2026 Standard")
    assert ctx.fake.toggles(page) == ["Sep 30 · Roadmap"]


# -- backfill ---------------------------------------------------------------------------------------


def test_backfill_preview_and_run(ctx):
    connect(ctx)
    set_parent(ctx, auto=False)
    for i in range(3):
        add_meeting(ctx.store, f"b{i}", f"Old {i}", f"2026-09-0{i + 1} 09:00")
    p = ctx.client.get("/v1/notion/backfill", params={"template": "standard"}, headers=WEB).json()
    assert p["count"] == 3 and p["template"]["name"] == "Standard"
    r = ctx.client.post("/v1/notion/backfill", json={"template": "standard"}, headers=WEB).json()
    assert r["queued"] == 3
    drain(ctx)
    assert ctx.client.get("/v1/notion/backfill", params={"template": "standard"}, headers=WEB).json()["count"] == 0
    page = ctx.fake.find_page("September-2026 Standard")
    assert ctx.fake.toggles(page) == ["Sep 3 · Old 2", "Sep 2 · Old 1", "Sep 1 · Old 0"]


def test_backfill_needs_a_parent_page_and_a_known_style(ctx):
    connect(ctx)
    assert ctx.client.get("/v1/notion/backfill", params={"template": "quick"}, headers=WEB).status_code == 409
    assert ctx.client.get("/v1/notion/backfill", params={"template": "zzz"}, headers=WEB).status_code == 400


# -- agent API ------------------------------------------------------------------------------------------


def agent_headers(ctx, scopes):
    return {"Authorization": f"Bearer {ctx.app.state.agent_keys.create('a', scopes)['key']}"}


def test_agent_sees_notion_state_and_url_and_can_trigger_an_export(ctx):
    connect(ctx)
    set_parent(ctx, auto=False)
    add_meeting(ctx.store, "m1", "Planning", "2026-09-30 10:00")
    reader = agent_headers(ctx, ["read"])
    writer = agent_headers(ctx, ["read", "write"])
    meeting = ctx.client.get("/api/v1/meetings/m1", headers=reader).json()
    assert meeting["notion"] == {"state": "none", "url": None, "error": None}
    assert ctx.client.post("/api/v1/meetings/m1/notion", headers=reader).status_code == 403
    r = ctx.client.post("/api/v1/meetings/m1/notion", headers=writer)
    assert r.status_code == 200 and r.json()["notion"]["state"] == "pending"
    drain(ctx)
    meeting = ctx.client.get("/api/v1/meetings/m1", headers=reader).json()
    assert meeting["notion"]["state"] == "copied" and meeting["notion"]["url"].startswith("https://www.notion.so/")
    notes_json = ctx.client.get("/api/v1/meetings/m1/notes", headers=reader).json()
    assert notes_json["notion"]["url"] == meeting["notion"]["url"]


def test_agent_trigger_explains_why_it_cannot(ctx):
    add_meeting(ctx.store, "m1", "Planning", "2026-09-30 10:00")
    r = ctx.client.post("/api/v1/meetings/m1/notion", headers=agent_headers(ctx, ["read", "write"]))
    assert r.status_code == 409 and r.json()["code"] == "notion_not_ready" and "not connected" in r.json()["detail"]


def test_mcp_exposes_the_notion_tool_and_the_spec_documents_it(ctx):
    from meeting_notes.server.agent import spec

    assert any(t["name"] == "meeting_notes_send_to_notion" and t["scope"] == "write" for t in spec.TOOLS)
    assert any(r["path"] == "/api/v1/meetings/{id}/notion" for r in spec.ROUTES)
    manifest = ctx.client.get("/api/v1/manifest").json()
    assert any(t["name"] == "meeting_notes_send_to_notion" for t in manifest["tools"])


# -- rendered pages -----------------------------------------------------------------------------------------


def test_settings_page_renders_notion_section_and_per_style_fields(ctx):
    connect(ctx)
    set_parent(ctx)
    html = ctx.client.get("/settings", headers=WEB).text
    for marker in ("settings-notion-heading", 'id="notion-token"', 'type="password"', "notion_auto_copy",
                   "notion-parents-json", "style-notion-input", "style-notion-backfill", "Connections"):
        assert marker in html, marker
    assert html.count('class="style-notion-input"') == 4  # Standard, Quick, Webinar + the blank template
    assert ctx.root_page.replace("-", "") in html  # saved parent shown for Standard
    assert TOKEN not in html and 'name="notion_token"' not in html
    # the token input is never prefilled
    token_input = html[html.index('id="notion-token"') - 200: html.index('id="notion-token"') + 300]
    assert "value=" not in token_input.split(">")[0]


def test_meeting_view_and_meetings_page_have_the_notion_ui(ctx):
    add_meeting(ctx.store, "m1", "Planning", "2026-09-30 10:00")
    page = ctx.client.get("/sessions/m1", headers=WEB).text
    for marker in ("notion-box", "notion-send", "In Notion", "notion-open", "Sending…",
                   "Failed", "Retry", "bulk-notion"):
        assert marker in page, marker
    listing = ctx.client.get("/meetings", headers=WEB).text
    assert "bulk-notion" in listing and "Send to Notion" in listing


def test_parent_pages_resolve_titles_for_the_note_type_ui(ctx):
    r = ctx.client.get("/v1/notion/parents", headers=WEB)
    assert r.status_code == 200 and r.json() == {"items": {}}
    set_parent(ctx)
    page = ctx.root_page
    # Not connected: the link is known, the title is not.
    item = ctx.client.get("/v1/notion/parents", headers=WEB).json()["items"]["standard"]
    assert item["id"] == page.replace("-", "") and item["url"].endswith(page.replace("-", "")) and item["title"] is None
    connect(ctx)
    item = ctx.client.get("/v1/notion/parents", headers=WEB).json()["items"]["standard"]
    assert item["title"] == "Notes root"
    assert ctx.client.get("/v1/notion/parents").status_code in (401, 403)


def _list_item(ctx, sid):
    items = ctx.client.get("/v1/sessions", headers=WEB).json()["items"]
    return next(i for i in items if i["session_id"] == sid)


def test_meetings_list_carries_each_rows_notion_state_without_calling_notion(ctx):
    connect(ctx)
    set_parent(ctx)
    add_meeting(ctx.store, "m1", "Planning", "2026-09-30 10:00")      # auto copy queued
    add_meeting(ctx.store, "m2", "Standup", "2026-09-30 11:00", complete=False, queue_review=False)
    calls = True
    pending = _list_item(ctx, "m1")["notion"]
    assert pending["state"] in ("pending", "copied")  # the app's worker may already have run it
    assert _list_item(ctx, "m2")["notion"] is None   # nothing to show: no chip
    drain(ctx)
    done = _list_item(ctx, "m1")["notion"]
    assert done["state"] == "copied" and done["url"].startswith("https://www.notion.so/") and done["error"] is None
    if calls is not None:
        before = len(ctx.fake.requests)
        ctx.client.get("/v1/sessions", headers=WEB)
        assert len(ctx.fake.requests) == before  # listing never touches the Notion API
    ctx.notion.state.set_meeting("m1", status="failed", error="Notion said no", block_id=None)
    failed = _list_item(ctx, "m1")["notion"]
    assert failed["state"] == "failed" and failed["error"] == "Notion said no" and failed["url"] is None


def test_meetings_page_renders_the_notion_chip_markup():
    from meeting_notes.server.web import render_transcriptions_page
    page = render_transcriptions_page(token_configured=True)
    assert "function notionChip(row)" in page and "notionChip(row)+'</div>" in page
    for label in ("In Notion", "Sending…", "Notion failed"):
        assert label in page
    assert 'target="_blank" rel="noopener"' in page   # In Notion opens the block in a new tab
    assert "loadRows(true)" in page                    # bulk send refreshes the rows


def test_session_notion_destination_shows_parent_and_month_path(ctx):
    set_parent(ctx, auto=False)
    add_meeting(ctx.store, "m1", "Planning", "2026-09-30 10:00")
    # Not connected: the path is known from settings; the parent title falls back to None (UI: "Parent page").
    dest = ctx.client.get("/v1/sessions/m1/notion", headers=WEB).json()["destination"]
    assert dest["style"]["name"] == "Standard" and dest["parent"]["title"] is None
    assert dest["month"] == {"title": "September-2026 Standard", "url": None}
    connect(ctx)
    n_before = len(ctx.fake.requests)
    dest = ctx.client.get("/v1/sessions/m1/notion", headers=WEB).json()["destination"]
    assert dest["parent"]["title"] == "Notes root"
    n_after_first = len(ctx.fake.requests)
    assert n_after_first > n_before  # looked the parent up once
    ctx.client.get("/v1/sessions/m1/notion", headers=WEB)
    assert len(ctx.fake.requests) == n_after_first  # cached in the notion state: no more API calls per view
    assert ctx.notion.state.read()["parents"]
    r = ctx.client.post("/v1/sessions/m1/notion", headers=WEB)
    assert r.status_code in (200, 202), r.text
    drain(ctx)
    done = ctx.client.get("/v1/sessions/m1/notion", headers=WEB).json()
    assert done["state"] == "copied"
    assert done["destination"]["month"]["title"] == "September-2026 Standard"
    assert done["copied_style"] == "standard"
    assert done["destination"]["month"]["url"].startswith("https://www.notion.so/")
    # Another note type without a page: nothing to save to.
    quick = ctx.client.get("/v1/note-templates", headers=WEB).json()["items"][1]["id"]
    other = ctx.client.get(f"/v1/sessions/m1/notion?template={quick}", headers=WEB).json()["destination"]
    assert other["parent"] is None and other["month"] is None and other["style"]["id"] == quick
