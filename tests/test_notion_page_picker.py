"""The Notion page picker: ``NotionClient.search_pages``, ``NotionSync.list_pages``, ``GET /v1/notion/pages``
and the settings page markup. A fake Notion stands behind the client; no real API."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from fake_notion import TOKEN, FakeNotion
from notion_helpers import TZ
from meeting_notes.server.app import create_app
from meeting_notes.server.notion_api import NotionClient

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
    c.clock = [1000.0]
    c.app = create_app(
        data_root=str(tmp_path / "data"),
        notion_options={"transport": c.fake.transport(), "sleep": lambda s: None, "min_interval": 0.0, "tz": TZ,
                        "now": lambda: c.clock[0]},
    )
    c.client = TestClient(c.app)
    c.notion = c.app.state.notion
    return c


def connect(c):
    r = c.client.put("/v1/notion/token", json={"token": TOKEN}, headers=WEB)
    assert r.status_code == 200, r.text


def searches(c):
    return c.fake.calls("POST", "/v1/search")


def test_client_search_pages_paginates(ctx):
    for i in range(150):
        ctx.fake.add_page(f"Page {i}")
    client = NotionClient(TOKEN, transport=ctx.fake.transport(), sleep=lambda s: None, min_interval=0.0)
    got = list(client.search_pages())
    assert len(got) == 150 and len({p["id"] for p in got}) == 150
    calls = searches(ctx)
    assert len(calls) == 2
    assert calls[0]["body"] == {"filter": {"property": "object", "value": "page"}, "page_size": 100}
    assert calls[1]["body"]["start_cursor"]


def test_list_pages_builds_the_hierarchy(ctx):
    connect(ctx)
    fake = ctx.fake
    root = fake.add_page("Zeta", icon="\U0001F4D2")
    child = fake.add_page("child b", root)
    child_a = fake.add_page("Child A", root)
    grand = fake.add_page("Grand", child)
    other = fake.add_page("alpha")
    fake.add_page("Row", parent_type="database_id")
    gone = fake.add_page("Trashed", root)
    fake.trash(gone)
    # A page whose parent is not shared with the integration becomes a root.
    orphan = fake.add_page("Orphan", "11111111-1111-1111-1111-111111111111")
    result = ctx.notion.list_pages()
    assert result["connected"] is True and result["error"] is None
    items = {i["id"]: i for i in result["items"]}
    assert set(items) == {root, child, child_a, grand, other, orphan}
    assert items[root]["parent"] is None and items[root]["icon"] == "\U0001F4D2"
    assert items[child]["parent"] == root and items[child_a]["parent"] == root
    assert items[grand]["parent"] == child
    assert items[other]["parent"] is None and items[other]["icon"] is None
    assert items[orphan]["parent"] is None
    assert all(i["url"].startswith("https://www.notion.so/") for i in items.values())
    titles = [i["title"] for i in result["items"]]
    assert titles == sorted(titles, key=str.lower)
    assert "Row" not in titles and "Trashed" not in titles


def test_untitled_pages_get_a_name(ctx):
    connect(ctx)
    ctx.fake.add_page("")
    assert ctx.notion.list_pages()["items"][0]["title"] == "Untitled"


def test_list_pages_is_cached_for_a_minute(ctx):
    connect(ctx)
    ctx.fake.add_page("One")
    ctx.notion.list_pages()
    ctx.notion.list_pages()
    assert len(searches(ctx)) == 1
    ctx.notion.list_pages(refresh=True)
    assert len(searches(ctx)) == 2
    ctx.clock[0] += 61
    ctx.notion.list_pages()
    assert len(searches(ctx)) == 3


def test_not_connected(ctx):
    assert ctx.notion.list_pages() == {"connected": False, "items": [], "error": None}
    assert searches(ctx) == []


def test_error_is_reported_not_raised_and_not_cached(ctx):
    connect(ctx)
    ctx.fake.add_page("One")
    ctx.fake.fail(401, when=lambda m, p: p == "/v1/search")
    result = ctx.notion.list_pages()
    assert result["connected"] is True and result["items"] == [] and result["error"]
    assert TOKEN not in result["error"]
    assert len(ctx.notion.list_pages()["items"]) == 1  # the failure was not cached


def test_endpoint(ctx):
    assert ctx.client.get("/v1/notion/pages").status_code in (401, 403)
    body = ctx.client.get("/v1/notion/pages", headers=WEB).json()
    assert body == {"connected": False, "items": [], "error": None}
    connect(ctx)
    root = ctx.fake.add_page("Root")
    ctx.fake.add_page("Kid", root)
    body = ctx.client.get("/v1/notion/pages", headers=WEB).json()
    assert [i["title"] for i in body["items"]] == ["Kid", "Root"]
    ctx.fake.add_page("New")
    assert len(ctx.client.get("/v1/notion/pages", headers=WEB).json()["items"]) == 2
    assert len(ctx.client.get("/v1/notion/pages?refresh=1", headers=WEB).json()["items"]) == 3


def test_endpoint_is_not_in_the_agent_api(ctx):
    key = ctx.app.state.agent_keys.create("agent", ["read", "write"])["key"]
    agent = {"Authorization": f"Bearer {key}"}
    assert ctx.client.get("/v1/notion/pages", headers=agent).status_code in (401, 403)
    assert ctx.client.get("/api/v1/notion/pages", headers=agent).status_code == 404


def test_listing_remembers_configured_parent_titles(ctx):
    connect(ctx)
    page = ctx.fake.add_page("Notes root")
    payload = ctx.client.get("/v1/settings", headers=WEB).json()
    payload["model"] = payload.get("model") or "base.en"
    payload["notion_parents"] = {"standard": page}
    assert ctx.client.put("/v1/settings", json=payload, headers=WEB).status_code == 200
    ctx.notion.list_pages()
    assert ctx.notion.state.read()["parents"][page.replace("-", "")]["title"] == "Notes root"
    assert ctx.fake.calls("GET", "/v1/pages") == []
    ctx.notion.parent_pages({"standard": page.replace("-", "")})
    assert ctx.fake.calls("GET", "/v1/pages") == []


def test_settings_page_has_the_picker(ctx):
    html = ctx.client.get("/settings", headers=WEB).text
    assert "style-notion-pick" in html and "style-pick-panel" in html
    assert 'type="hidden" class="style-notion-input"' in html
    assert 'id="notion-parents-json"' in html
    assert "/v1/notion/pages" in html
    assert 'placeholder="Paste a Notion page link or id"' not in html
