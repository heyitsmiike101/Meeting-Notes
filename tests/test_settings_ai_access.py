"""Settings page: the AI access (agent keys) and Client logs sections, and their wiring."""

from __future__ import annotations

import asyncio
import re

import pytest
from fastapi.testclient import TestClient

from meeting_notes.server.app import create_app
from meeting_notes.server.settings import Settings
from meeting_notes.server.web import render_install_page, render_settings_page

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated:DeprecationWarning"
)


def _page(address="http://meeting.lan/"):
    return render_settings_page(Settings(server_address=address), token_configured=True)


def test_settings_lists_both_sections_in_the_index():
    page = _page()
    assert 'href="#settings-agents-heading"' in page and 'href="#settings-logs-heading"' in page
    assert 'id="settings-agents-heading">AI access<' in page
    assert 'id="settings-logs-heading">Client logs<' in page


def test_new_sections_sit_outside_the_save_form():
    page = _page()
    form_end = page.index("</form>")
    assert page.index('id="settings-agents-heading"') > form_end
    assert page.index('id="settings-logs-heading"') > form_end
    assert page.count("<form") == page.count("</form>")
    assert "does not wait for Save settings" in page


def test_ai_access_copy_and_controls():
    page = _page()
    assert "cannot delete" in page and "never opens this website" in page
    assert "Allow writes (build notes, rename meetings)" in page
    assert "/v1/agent-keys" in page and "confirm(" in page
    assert "claude mcp add --transport http meeting-notes " in page and '/mcp --header "Authorization: Bearer ' in page
    for link in ("/api/v1/manifest", "/llms.txt", "/api-docs.md"):
        assert f'href="{link}"' in page
    assert "No keys yet" in page and "Nothing sent yet" in page


def test_saved_server_address_is_embedded_with_an_origin_fallback():
    assert '"http://meeting.lan"' in _page("http://meeting.lan/")
    assert "location.origin" in _page("")
    assert 'var SAVED_ADDRESS = "";' in _page("")


def test_no_placeholder_leaks_and_scripts_have_no_stray_python_braces():
    page = _page()
    assert not re.search(r"__[A-Z_]+__", page)
    assert "{immediate" not in page and "{_JS_HELPERS}" not in page


def test_install_page_points_to_client_logs():
    page = render_install_page("http://meeting.lan", token_configured=True)
    assert "Send to server" in page and "Client logs" in page


def test_create_app_serves_agent_routes_and_mcp_without_extra_wiring(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_NOTES_TOKEN", "web-secret")
    app = create_app(data_root=str(tmp_path / "data"))
    admin = {"Authorization": "Bearer web-secret"}
    with TestClient(app) as client:
        created = client.post("/v1/agent-keys", json={"name": "t", "scopes": ["read"]}, headers=admin)
        assert created.status_code == 201 and created.json()["key"].startswith("mnk_")
        key = created.json()["key"]
        assert client.get("/api/v1/manifest").json()["mcp"]["enabled"] is True
        ok = client.post(
            "/mcp/",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json, text/event-stream"},
        )
        assert ok.status_code == 200
        listed = client.get("/v1/agent-keys", headers=admin).json()["items"]
        assert len(listed) == 1 and "hash" not in listed[0]
        assert client.get("/settings", headers=admin).status_code == 200


def test_server_shutdown_still_stops_workers_when_the_agent_context_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_NOTES_TOKEN", "web-secret")
    app = create_app(data_root=str(tmp_path / "data"))
    stopped = []
    monkeypatch.setattr(app.state.job_queue, "stop", lambda *a, **k: stopped.append("jobs"))
    monkeypatch.setattr(app.state.retention_worker, "stop", lambda *a, **k: stopped.append("retention"))

    class Boom:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            raise RuntimeError("mcp shutdown failed")

    async def run():
        async with app.router.lifespan_context(app):
            pass

    app.state.agent_lifespan = Boom()
    with pytest.raises(RuntimeError, match="mcp shutdown failed"):
        asyncio.run(run())
    assert stopped == ["jobs", "retention"]
