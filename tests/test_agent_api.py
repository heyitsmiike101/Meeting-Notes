"""Agent REST API: keys, auth boundaries, every read/write endpoint, discovery."""

from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient

from meeting_notes.server.agent import spec
from meeting_notes.server.agent.keys import AgentKeyStore, hash_key, resolve_access
from meeting_notes.server.agent.errors import AgentError
from tests.agent_helpers import WEB_TOKEN, bearer, make_agent_app

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated:DeprecationWarning"
)

WEB = {"Authorization": f"Bearer {WEB_TOKEN}"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    app, _ = make_agent_app(tmp_path, monkeypatch, with_mcp=False)
    client = TestClient(app)
    keys = app.state.agent_keys
    read_key = keys.create("reader")["key"]
    write_key = keys.create("writer", ["read", "write"])["key"]
    return {"app": app, "client": client, "keys": keys, "read": bearer(read_key), "write": bearer(write_key),
            "store": app.state.store}


# -- key store --------------------------------------------------------------------


def test_key_create_hash_prefix_revoke_last_used(tmp_path):
    keys = AgentKeyStore(tmp_path)
    created = keys.create("Claude Code")
    plaintext = created["key"]
    assert plaintext.startswith("mnk_") and len(plaintext) > 30
    assert created["prefix"] == plaintext[:12]
    assert created["scopes"] == ["read"] and created["last_used_at"] is None

    on_disk = (tmp_path / "agent_keys.json").read_text(encoding="utf-8")
    assert plaintext not in on_disk
    assert hash_key(plaintext) in on_disk
    assert "key" not in keys.list()[0]

    record = resolve_access(keys, plaintext, "read")
    assert record["id"] == created["id"]
    assert keys.list()[0]["last_used_at"] is not None

    with pytest.raises(AgentError) as denied:
        resolve_access(keys, plaintext, "write")
    assert denied.value.status == 403 and denied.value.code == "scope_missing"

    revoked = keys.revoke(created["prefix"])
    assert revoked["revoked_at"] is not None
    with pytest.raises(AgentError) as gone:
        resolve_access(keys, plaintext, "read")
    assert gone.value.status == 401 and gone.value.code == "api_key_revoked"
    assert keys.revoke("nope") is None


def test_key_store_validation_and_persistence(tmp_path):
    keys = AgentKeyStore(tmp_path)
    with pytest.raises(ValueError):
        keys.create("  ")
    with pytest.raises(ValueError):
        keys.create("x", ["admin"])
    assert keys.create("w", ["write"])["scopes"] == ["read", "write"]
    assert [k["name"] for k in AgentKeyStore(tmp_path).list()] == ["w"]  # reloads from disk
    for bad in (None, "", "mnk_wrong", "not-a-key"):
        with pytest.raises(AgentError) as exc:
            resolve_access(keys, bad)
        assert exc.value.status == 401


# -- auth boundaries ---------------------------------------------------------------


def test_data_endpoints_need_an_agent_key(env):
    c = env["client"]
    for path in ("/api/v1/meetings", "/api/v1/meetings/m-plan", "/api/v1/meetings/m-plan/notes",
                 "/api/v1/meetings/m-plan/transcript", "/api/v1/search?q=beta",
                 "/api/v1/action-items", "/api/v1/decisions"):
        r = c.get(path)
        assert r.status_code == 401, path
        assert r.json()["code"] == "api_key_required"
        # The shared web/recorder token must NOT open the agent API.
        r = c.get(path, headers=WEB)
        assert r.status_code == 401 and r.json()["code"] == "invalid_api_key", path
    assert c.get("/api/v1/meetings", headers=bearer("mnk_bogus")).status_code == 401


def test_agent_key_does_not_open_web_or_recorder_api(env):
    c = env["client"]
    assert c.get("/v1/sessions", headers=env["read"]).status_code == 403
    assert c.get("/v1/sessions", headers=env["write"]).status_code == 403
    assert c.get("/v1/agent-keys", headers=env["write"]).status_code == 403
    assert c.get("/v1/sessions", headers=WEB).status_code == 200


def test_web_token_cookie_is_not_accepted_on_agent_routes(env):
    c = env["client"]
    c.cookies.set("meeting_notes_token", WEB_TOKEN)
    assert c.get("/api/v1/meetings").status_code == 401


def test_write_endpoints_need_write_scope(env):
    c = env["client"]
    r = c.post("/api/v1/meetings/m-standup/notes/generate", headers=env["read"])
    assert r.status_code == 403 and r.json()["code"] == "scope_missing"
    r = c.patch("/api/v1/meetings/m-standup", headers=env["read"], json={"name": "x"})
    assert r.status_code == 403
    assert c.post("/api/v1/meetings/m-standup/notes/generate").status_code == 401


def test_revoked_key_stops_working(env):
    c = env["client"]
    assert c.get("/api/v1/meetings", headers=env["read"]).status_code == 200
    prefix = env["read"]["Authorization"].split(" ")[1][:12]
    assert c.delete(f"/v1/agent-keys/{prefix}", headers=WEB).status_code == 200
    r = c.get("/api/v1/meetings", headers=env["read"])
    assert r.status_code == 401 and r.json()["code"] == "api_key_revoked"


# -- admin key management (existing web auth) --------------------------------------


def test_admin_key_routes_use_the_web_token(env):
    c = env["client"]
    assert c.get("/v1/agent-keys").status_code == 401
    assert c.post("/v1/agent-keys", json={"name": "n"}).status_code == 401
    created = c.post("/v1/agent-keys", headers=WEB, json={"name": "New agent", "scopes": ["read", "write"]})
    assert created.status_code == 201
    body = created.json()
    assert body["key"].startswith("mnk_") and body["scopes"] == ["read", "write"]
    listed = c.get("/v1/agent-keys", headers=WEB).json()["items"]
    assert any(k["id"] == body["id"] for k in listed)
    assert all("key" not in k and "hash" not in k for k in listed)
    assert c.post("/v1/agent-keys", headers=WEB, json={"name": ""}).status_code == 400
    assert c.post("/v1/agent-keys", headers=WEB, json={"name": "n", "scopes": ["root"]}).status_code == 400
    assert c.delete("/v1/agent-keys/nope", headers=WEB).status_code == 404
    revoked = c.delete(f"/v1/agent-keys/{body['id']}", headers=WEB).json()
    assert revoked["revoked_at"] is not None
    # The freshly minted key really works until revoked.
    assert c.get("/api/v1/meetings", headers=bearer(body["key"])).status_code == 401


# -- reads ---------------------------------------------------------------------------


def test_list_meetings_shape_and_order(env):
    r = env["client"].get("/api/v1/meetings", headers=env["read"])
    assert r.status_code == 200
    body = r.json()
    assert [m["id"] for m in body["items"]] == ["m-raw", "m-standup", "m-budget", "m-plan"]
    assert body["count"] == 4 and body["next_cursor"] is None
    plan = body["items"][3]
    assert plan["name"] == "Sprint planning"
    assert plan["created"] == "2026-09-01T15:00:00Z"
    assert plan["duration_sec"] == 1800 and plan["device"] == "Laptop"
    assert plan["transcription_state"] == "complete"
    assert plan["notes_state"] == "done" and plan["has_notes"] is True
    assert re.fullmatch(r"M-\d{4}", plan["board"])
    raw = body["items"][0]
    assert raw["transcription_state"] == "pending" and raw["notes_state"] == "none" and raw["has_notes"] is False


def test_list_meetings_filters(env):
    c, h = env["client"], env["read"]

    def ids(query):
        r = c.get("/api/v1/meetings" + query, headers=h)
        assert r.status_code == 200, r.text
        return [m["id"] for m in r.json()["items"]]

    assert ids("?q=fresh") == ["m-raw"]  # name only
    assert ids("?q=budget") == ["m-budget", "m-plan"]  # name + transcript text
    assert ids("?q=migration") == ["m-plan"]  # transcript text
    assert ids("?has_notes=true") == ["m-budget", "m-plan"]
    assert ids("?has_notes=false") == ["m-raw", "m-standup"]
    assert ids("?from=2026-09-10&to=2026-09-20") == ["m-standup", "m-budget"]  # 'to' date is inclusive
    assert ids("?from=2026-09-10T15:00:01Z") == ["m-raw", "m-standup"]
    assert c.get("/api/v1/meetings?since=garbage", headers=h).json()["code"] == "invalid_date"
    assert c.get("/api/v1/meetings?has_notes=maybe", headers=h).status_code == 400
    assert c.get("/api/v1/meetings?limit=abc", headers=h).json()["code"] == "invalid_limit"


def test_since_sees_notes_completed_after_it(env):
    c, h, store = env["client"], env["read"], env["store"]
    import time

    future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
    got = c.get(f"/api/v1/meetings?since={future}", headers=h).json()["items"]
    # Every seeded meeting was written "just now", so all are updated after that instant.
    assert len(got) == 4
    later = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
    assert c.get(f"/api/v1/meetings?since={later}", headers=h).json()["items"] == []


def test_limit_and_cursor_paging(env):
    c, h = env["client"], env["read"]
    first = c.get("/api/v1/meetings?limit=3", headers=h).json()
    assert [m["id"] for m in first["items"]] == ["m-raw", "m-standup", "m-budget"]
    assert first["next_cursor"]
    second = c.get(f"/api/v1/meetings?limit=3&cursor={first['next_cursor']}", headers=h).json()
    assert [m["id"] for m in second["items"]] == ["m-plan"]
    assert second["next_cursor"] is None
    assert c.get("/api/v1/meetings?cursor=@@@", headers=h).json()["code"] == "invalid_cursor"
    # limit is capped, never an error
    assert c.get("/api/v1/meetings?limit=99999", headers=h).status_code == 200


def test_get_meeting(env):
    c, h = env["client"], env["read"]
    body = c.get("/api/v1/meetings/m-plan", headers=h).json()
    assert body["id"] == "m-plan" and body["notes"]["title"] == "Sprint planning"
    assert body["transcript"] == {"available": True, "segment_count": 4, "speakers": ["Them", "You"]}
    assert body["notes_completed_at"]
    standup = c.get("/api/v1/meetings/m-standup", headers=h).json()
    assert standup["notes"] is None and standup["transcript"]["available"] is True
    raw = c.get("/api/v1/meetings/m-raw", headers=h).json()
    assert raw["transcript"]["available"] is False
    missing = c.get("/api/v1/meetings/nope", headers=h)
    assert missing.status_code == 404 and missing.json()["code"] == "unknown_meeting"
    assert c.get("/api/v1/meetings/bad..id%00", headers=h).status_code in (400, 404)


def test_get_notes_json_and_markdown(env):
    c, h = env["client"], env["read"]
    body = c.get("/api/v1/meetings/m-plan/notes", headers=h).json()
    assert body["meeting_id"] == "m-plan" and body["notes"]["decisions"] == ["Ship the beta on Monday"]
    assert body["review_id"] and body["completed_at"]
    md = c.get("/api/v1/meetings/m-plan/notes?format=markdown", headers=h)
    assert md.headers["content-type"].startswith("text/markdown")
    text = md.text
    for needle in ("# Sprint planning", "## Summary", "## Decisions", "- Ship the beta on Monday",
                   "## Action items", "- [ ] Finish the migration (owner: Sarah, due: 2026-09-05) - blocks the beta",
                   "- [ ] Send the launch email (owner: Mike)", "## Key points", "## Open questions",
                   "## Risks", "## Next steps", "## Notes"):
        assert needle in text, needle
    none = c.get("/api/v1/meetings/m-standup/notes", headers=h)
    assert none.status_code == 404 and none.json()["code"] == "notes_not_available"
    assert c.get("/api/v1/meetings/m-plan/notes?format=pdf", headers=h).json()["code"] == "invalid_format"


def test_notes_pending_is_409(env):
    c, h = env["client"], env["read"]
    env["store"].create_review("m-standup")
    r = c.get("/api/v1/meetings/m-standup/notes", headers=h)
    assert r.status_code == 409 and r.json()["code"] == "notes_pending"


def test_transcript_formats_and_filters(env):
    c, h = env["client"], env["read"]
    data = c.get("/api/v1/meetings/m-plan/transcript", headers=h).json()
    assert data["count"] == 4 and data["speakers"] == ["Them", "You"]
    assert data["segments"][0] == {"start": 0.0, "end": 8.0, "speaker": "You",
                                   "text": "Welcome everyone, let's plan the sprint."}
    assert all("hallucinated" not in s["text"] for s in data["segments"])  # in_gap dropped

    md = c.get("/api/v1/meetings/m-plan/transcript?format=markdown", headers=h)
    assert md.headers["content-type"].startswith("text/markdown")
    assert "**[00:00:00] You:** Welcome everyone" in md.text
    assert "**[00:00:08] Them:** I can take the migration" in md.text

    txt = c.get("/api/v1/meetings/m-plan/transcript?format=text", headers=h)
    assert txt.headers["content-type"].startswith("text/plain")
    assert txt.text.splitlines()[1] == "[00:00:08] Them: I can take the migration work by Friday."

    them = c.get("/api/v1/meetings/m-plan/transcript?speaker=them", headers=h).json()
    assert [s["speaker"] for s in them["segments"]] == ["Them", "Them"]
    assert c.get("/api/v1/meetings/m-plan/transcript?speaker=Nobody", headers=h).json()["count"] == 0

    window = c.get("/api/v1/meetings/m-plan/transcript?start_sec=20.5&end_sec=35", headers=h).json()
    assert [s["start"] for s in window["segments"]] == [21.0]  # overlap semantics, gap segment dropped
    early = c.get("/api/v1/meetings/m-plan/transcript?end_sec=9", headers=h).json()
    assert [s["start"] for s in early["segments"]] == [0.0, 8.5]
    both = c.get("/api/v1/meetings/m-plan/transcript?speaker=you&start_sec=25", headers=h).json()
    assert [s["start"] for s in both["segments"]] == [21.0]
    bad = c.get("/api/v1/meetings/m-plan/transcript?start_sec=9&end_sec=3", headers=h)
    assert bad.status_code == 400 and bad.json()["code"] == "invalid_window"
    assert c.get("/api/v1/meetings/m-plan/transcript?start_sec=x", headers=h).status_code == 400
    assert c.get("/api/v1/meetings/m-plan/transcript?format=xml", headers=h).json()["code"] == "invalid_format"
    none = c.get("/api/v1/meetings/m-raw/transcript", headers=h)
    assert none.status_code == 404 and none.json()["code"] == "transcript_not_available"


def test_search_transcripts_and_notes(env):
    c, h = env["client"], env["read"]
    body = c.get("/api/v1/search?q=migration", headers=h).json()
    assert [i["id"] for i in body["items"]] == ["m-plan"]
    matches = body["items"][0]["matches"]
    transcript = [m for m in matches if m["source"] == "transcript"]
    assert transcript[0]["start"] == 8.5 and transcript[0]["speaker"] == "Them"
    assert "migration" in transcript[0]["text"]
    assert any(m["source"] == "notes" and m["field"] == "action_items" for m in matches)

    # Only in notes (not spoken anywhere in the transcripts)
    only_notes = c.get("/api/v1/search?q=analytics", headers=h).json()
    assert [i["id"] for i in only_notes["items"]] == ["m-budget"]
    assert only_notes["items"][0]["matches"][0]["source"] == "notes"

    # Name match, newest first across several hits
    several = c.get("/api/v1/search?q=budget", headers=h).json()
    assert [i["id"] for i in several["items"]] == ["m-budget", "m-plan"]
    assert any(m["source"] == "name" for m in several["items"][0]["matches"])
    assert c.get("/api/v1/search?q=zzzznothing", headers=h).json()["items"] == []
    assert c.get("/api/v1/search", headers=h).json()["code"] == "missing_query"
    assert c.get("/api/v1/search?q=budget&limit=1", headers=h).json()["count"] == 1


def test_action_items_aggregate_across_meetings(env):
    c, h = env["client"], env["read"]
    body = c.get("/api/v1/action-items", headers=h).json()
    assert body["count"] == 3 and body["next_cursor"] is None
    first = body["items"][0]  # newest meeting first
    assert first == {
        "meeting_id": "m-budget", "meeting_name": "Budget review", "meeting_board": first["meeting_board"],
        "meeting_date": "2026-09-10T15:00:00Z", "action": "Draft the hiring freeze memo",
        "owner": "Mike", "due_date": "2026-09-15", "context": None,
    }
    assert [i["action"] for i in body["items"][1:]] == ["Finish the migration", "Send the launch email"]

    mine = c.get("/api/v1/action-items?owner=mike", headers=h).json()["items"]
    assert [i["action"] for i in mine] == ["Draft the hiring freeze memo", "Send the launch email"]
    assert [i["action"] for i in c.get("/api/v1/action-items?q=migration", headers=h).json()["items"]] == [
        "Finish the migration"]
    ranged = c.get("/api/v1/action-items?from=2026-09-05&to=2026-09-30", headers=h).json()["items"]
    assert [i["meeting_id"] for i in ranged] == ["m-budget"]
    assert c.get("/api/v1/action-items?since=2100-01-01", headers=h).json()["items"] == []

    page1 = c.get("/api/v1/action-items?limit=2", headers=h).json()
    assert page1["count"] == 2 and page1["next_cursor"]
    page2 = c.get(f"/api/v1/action-items?limit=2&cursor={page1['next_cursor']}", headers=h).json()
    assert [i["action"] for i in page2["items"]] == ["Send the launch email"] and page2["next_cursor"] is None


def test_decisions_aggregate(env):
    c, h = env["client"], env["read"]
    body = c.get("/api/v1/decisions", headers=h).json()
    assert [(d["meeting_id"], d["decision"]) for d in body["items"]] == [
        ("m-budget", "Freeze new hires until Q1"),
        ("m-budget", "Renew the analytics contract"),
        ("m-plan", "Ship the beta on Monday"),
    ]
    assert body["items"][0]["meeting_name"] == "Budget review" and body["items"][0]["meeting_date"]
    assert [d["decision"] for d in c.get("/api/v1/decisions?q=beta", headers=h).json()["items"]] == [
        "Ship the beta on Monday"]
    assert c.get("/api/v1/decisions?limit=1", headers=h).json()["next_cursor"]


def test_aggregates_use_newest_done_review_only(env):
    c, h, store = env["client"], env["read"], env["store"]
    review = store.create_review("m-plan", force=True)  # queued regeneration, no payload yet
    body = c.get("/api/v1/action-items?q=migration", headers=h).json()
    assert body["count"] == 1  # the older done review still answers
    assert review["status"] == "queued"


# -- writes --------------------------------------------------------------------------


def test_generate_notes_queues_a_review_like_the_web_route(env):
    c, store = env["client"], env["store"]
    r = c.post("/api/v1/meetings/m-standup/notes/generate", headers=env["write"])
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "queued" and body["meeting_id"] == "m-standup"
    assert store.read_review(body["review_id"])["status"] == "queued"
    again = c.post("/api/v1/meetings/m-standup/notes/generate", headers=env["write"]).json()
    assert again["review_id"] == body["review_id"]  # idempotent, same as POST /v1/sessions/{id}/review
    forced = c.post("/api/v1/meetings/m-standup/notes/generate?force=true", headers=env["write"]).json()
    assert forced["review_id"] != body["review_id"]
    no_transcript = c.post("/api/v1/meetings/m-raw/notes/generate", headers=env["write"])
    assert no_transcript.status_code == 409 and no_transcript.json()["code"] == "no_transcript"
    assert c.post("/api/v1/meetings/ghost/notes/generate", headers=env["write"]).status_code == 404


def test_rename_meeting(env):
    c, store = env["client"], env["store"]
    r = c.patch("/api/v1/meetings/m-standup", headers=env["write"], json={"name": "  Daily sync  "})
    assert r.status_code == 200 and r.json() == {"meeting_id": "m-standup", "name": "Daily sync"}
    assert store.read_session_meta("m-standup")["name"] == "Daily sync"
    got = c.get("/api/v1/meetings/m-standup", headers=env["read"]).json()
    assert got["name"] == "Daily sync"
    for body in ({"name": ""}, {"name": 5}, {}, {"name": "x" * 201}):
        assert c.patch("/api/v1/meetings/m-standup", headers=env["write"], json=body).json()["code"] == "invalid_name"
    assert c.patch("/api/v1/meetings/m-standup", headers=env["write"], content=b"nope").json()["code"] == "invalid_body"
    assert c.patch("/api/v1/meetings/ghost", headers=env["write"], json={"name": "x"}).status_code == 404


def test_agents_have_no_delete_route(env):
    c = env["client"]
    for path in ("/api/v1/meetings/m-plan", "/api/v1/meetings/m-plan/transcript"):
        assert c.delete(path, headers=env["write"]).status_code in (404, 405)
    assert c.get("/api/v1/meetings/m-plan", headers=env["read"]).status_code == 200


# -- discovery -----------------------------------------------------------------------


def test_manifest_is_open_and_describes_everything(env):
    r = env["client"].get("/api/v1/manifest")  # no key
    assert r.status_code == 200
    m = r.json()
    assert m["name"] == "meeting-notes" and m["auth"]["header"].startswith("Authorization: Bearer mnk_")
    assert set(m["auth"]["scopes"]) == {"read", "write"}
    assert m["base_url_hint"] == "http://testserver"
    assert {t["name"] for t in m["tools"]} == {t["name"] for t in spec.TOOLS}
    assert m["mcp"]["url"] == "http://testserver/mcp" and m["mcp"]["enabled"] is False
    assert "/api/v1/meetings" in {e["path"] for e in m["endpoints"]}
    assert {e["path"] for e in m["admin_endpoints"]} == {"/v1/agent-keys", "/v1/agent-keys/{id}"}
    assert m["docs"]["llms_txt"].endswith("/llms.txt")
    assert "detail" in m["conventions"]["errors"]


def test_llms_txt_and_api_docs_are_open_and_complete(env):
    c = env["client"]
    llms = c.get("/llms.txt")
    assert llms.status_code == 200 and llms.headers["content-type"].startswith("text/plain")
    docs = c.get("/api-docs.md")
    assert docs.status_code == 200 and docs.headers["content-type"].startswith("text/markdown")
    for route in spec.agent_routes():
        assert f"{route['method']} http://testserver{route['path']}" in llms.text, route
        assert f"`{route['method']} {route['path']}`" in docs.text, route
    for tool in spec.TOOLS:
        assert f"tool {tool['name']}" in llms.text
        assert f"`{tool['name']}`" in docs.text
    assert "meetingnotes://manifest" in llms.text and "meeting_brief" in llms.text
    assert "Authorization: Bearer mnk_" in llms.text


def test_base_url_getter_overrides_request_host(tmp_path, monkeypatch):
    import dataclasses

    from meeting_notes.server import settings as settings_mod
    from meeting_notes.server.app import create_app

    monkeypatch.setenv("MEETING_NOTES_TOKEN", WEB_TOKEN)
    app = create_app(data_root=str(tmp_path / "d"), enable_mcp=False)
    current = settings_mod.load_settings(app.state.store.root)
    settings_mod.save_settings(
        app.state.store.root, dataclasses.replace(current, server_address="http://meeting.lan/")
    )
    assert TestClient(app).get("/api/v1/manifest").json()["base_url_hint"] == "http://meeting.lan"


def _normalise(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{}", path)


def test_route_table_and_real_routes_do_not_drift(env):
    """Every documented route exists, and every /api/v1, llms and key route is documented."""
    documented = {(r["method"], _normalise(r["path"])) for r in spec.ROUTES}
    actual = set()
    # FastAPI wraps included routers lazily, so read the generated schema instead of app.routes.
    for path, methods in env["app"].openapi()["paths"].items():
        if path.startswith("/api/v1/") or path in ("/llms.txt", "/api-docs.md") or path.startswith("/v1/agent-keys"):
            for method in methods:
                actual.add((method.upper(), _normalise(path)))
    assert actual == documented


def test_every_tool_maps_to_a_documented_route_and_back(env):
    routes = {spec.route_key(r): r for r in spec.ROUTES}
    for tool in spec.TOOLS:
        route = routes[tool["route"]]
        assert route["tool"] == tool["name"]
        assert route["scope"] == tool["scope"]
    for r in spec.ROUTES:
        if r["tool"]:
            assert r["tool"] in {t["name"] for t in spec.TOOLS}


def test_existing_web_and_recorder_auth_is_unchanged(env):
    c = env["client"]
    assert c.get("/v1/sessions").status_code == 401
    assert c.get("/v1/sessions", headers=WEB).status_code == 200
    assert c.get("/health").status_code == 200


def test_existing_validation_error_shape_is_untouched(env):
    r = env["client"].get("/v1/sessions?page=abc", headers=WEB)
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], list)  # FastAPI default, not the agent {"detail","code"} shape
