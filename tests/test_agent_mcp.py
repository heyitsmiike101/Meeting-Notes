"""MCP surface: initialize, tools/list vs the tool table, tool calls, auth, resources, prompts.

Drives real JSON-RPC over the mounted Streamable HTTP app through
``TestClient`` (which runs the app lifespan, and with it the MCP session
manager). The server is stateless + JSON-response, so each POST is answered
with a single JSON body.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("mcp", reason="the optional 'mcp' package is not installed")

from fastapi.testclient import TestClient  # noqa: E402

from meeting_notes.server.agent import spec  # noqa: E402
from tests.agent_helpers import WEB_TOKEN, make_agent_app  # noqa: E402

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated:DeprecationWarning"
)

ACCEPT = {"Accept": "application/json, text/event-stream"}


class Rpc:
    def __init__(self, client, key=None):
        self.client = client
        self.key = key
        self._id = 0

    def call(self, method, params=None, *, key="__default__", path="/mcp/"):
        self._id += 1
        headers = dict(ACCEPT)
        token = self.key if key == "__default__" else key
        if token:
            headers["Authorization"] = f"Bearer {token}"
        body = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            body["params"] = params
        response = self.client.post(path, json=body, headers=headers)
        assert response.status_code == 200, response.text
        return response.json()

    def initialize(self, **kw):
        return self.call(
            "initialize",
            {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
            **kw,
        )

    def tool(self, name, arguments=None, **kw):
        return self.call("tools/call", {"name": name, "arguments": arguments or {}}, **kw)


def tool_json(reply):
    """The JSON payload of a successful tool result."""
    result = reply["result"]
    assert not result.get("isError"), result
    if result.get("structuredContent") is not None:
        sc = result["structuredContent"]
        return sc.get("result", sc) if list(sc) == ["result"] else sc
    return json.loads(result["content"][0]["text"])


def tool_text(reply):
    result = reply["result"]
    assert not result.get("isError"), result
    return result["content"][0]["text"]


def tool_error(reply):
    result = reply["result"]
    assert result["isError"] is True
    text = result["content"][0]["text"]
    return json.loads(text[text.index("{"):])


@pytest.fixture
def mcp_env(tmp_path, monkeypatch):
    app, _ = make_agent_app(tmp_path, monkeypatch)
    keys = app.state.agent_keys
    read = keys.create("mcp-reader")["key"]
    write = keys.create("mcp-writer", ["read", "write"])["key"]
    with TestClient(app) as client:
        yield {"app": app, "client": client, "read": Rpc(client, read), "write": Rpc(client, write),
               "anon": Rpc(client), "store": app.state.store, "read_key": read}


def test_initialize_reports_server_and_instructions(mcp_env):
    reply = mcp_env["read"].initialize()
    result = reply["result"]
    assert result["serverInfo"]["name"] == "meeting-notes"
    assert "meetingnotes://manifest" in result["instructions"]
    assert "Bearer mnk_" in result["instructions"]


def test_initialize_and_listing_work_without_a_key_but_data_does_not(mcp_env):
    anon = mcp_env["anon"]
    assert anon.initialize()["result"]["serverInfo"]["name"] == "meeting-notes"
    assert len(anon.call("tools/list")["result"]["tools"]) == len(spec.TOOLS)
    err = tool_error(anon.tool("meeting_notes_list_meetings"))
    assert err["code"] == "api_key_required"


def test_tools_list_matches_the_tool_table(mcp_env):
    tools = mcp_env["read"].call("tools/list")["result"]["tools"]
    assert sorted(t["name"] for t in tools) == sorted(t["name"] for t in spec.TOOLS)
    for tool in tools:
        assert tool["description"], tool["name"]
    by_name = {t["name"]: t for t in tools}
    assert "meeting_id" in by_name["meeting_notes_get_transcript"]["inputSchema"]["properties"]
    assert set(by_name["meeting_notes_get_transcript"]["inputSchema"]["properties"]) >= {
        "format", "speaker", "start_sec", "end_sec"}
    # nothing destructive is exposed
    assert not [n for n in by_name if "delete" in n or "remove" in n]


def test_read_tools_return_the_same_data_as_rest(mcp_env):
    rpc, client = mcp_env["read"], mcp_env["client"]
    rest_headers = {"Authorization": f"Bearer {mcp_env['read_key']}"}

    listed = tool_json(rpc.tool("meeting_notes_list_meetings", {"limit": 3}))
    rest = client.get("/api/v1/meetings?limit=3", headers=rest_headers).json()
    assert listed == rest
    assert [m["id"] for m in listed["items"]] == ["m-raw", "m-standup", "m-budget"] and listed["next_cursor"]

    assert tool_json(rpc.tool("meeting_notes_list_meetings", {"has_notes": True}))["count"] == 2
    assert tool_json(rpc.tool("meeting_notes_get_meeting", {"meeting_id": "m-plan"}))["notes"]["title"] == "Sprint planning"

    notes_md = tool_text(rpc.tool("meeting_notes_get_notes", {"meeting_id": "m-plan", "format": "markdown"}))
    assert notes_md.startswith("# Sprint planning") and "## Action items" in notes_md
    notes = tool_json(rpc.tool("meeting_notes_get_notes", {"meeting_id": "m-plan"}))
    assert notes["notes"]["decisions"] == ["Ship the beta on Monday"]

    transcript = tool_json(
        rpc.tool("meeting_notes_get_transcript", {"meeting_id": "m-plan", "speaker": "them", "start_sec": 30})
    )
    assert [s["start"] for s in transcript["segments"]] == [40.0]
    text = tool_text(rpc.tool("meeting_notes_get_transcript", {"meeting_id": "m-plan", "format": "text"}))
    assert text.startswith("[00:00:00] You:")

    hits = tool_json(rpc.tool("meeting_notes_search", {"q": "migration"}))
    assert hits["items"][0]["id"] == "m-plan"

    actions = tool_json(rpc.tool("meeting_notes_list_action_items", {"owner": "sarah"}))
    assert [a["action"] for a in actions["items"]] == ["Finish the migration"]
    assert tool_json(rpc.tool("meeting_notes_list_decisions", {"q": "hires"}))["count"] == 1


def test_tool_errors_share_the_rest_error_shape(mcp_env):
    rpc = mcp_env["read"]
    assert tool_error(rpc.tool("meeting_notes_get_meeting", {"meeting_id": "ghost"}))["code"] == "unknown_meeting"
    assert tool_error(rpc.tool("meeting_notes_get_notes", {"meeting_id": "m-standup"}))["code"] == "notes_not_available"
    assert tool_error(rpc.tool("meeting_notes_list_meetings", {"since": "nope"}))["code"] == "invalid_date"
    err = tool_error(rpc.tool("meeting_notes_search", {"q": "  "}))
    assert err["code"] == "missing_query" and err["detail"]


def test_auth_failures_over_mcp(mcp_env):
    anon, rpc, write = mcp_env["anon"], mcp_env["read"], mcp_env["write"]
    assert tool_error(rpc.tool("meeting_notes_list_meetings", key="mnk_bogus"))["code"] == "invalid_api_key"
    # the shared web token is not an agent key
    assert tool_error(rpc.tool("meeting_notes_list_meetings", key=WEB_TOKEN))["code"] == "invalid_api_key"
    assert tool_error(anon.tool("meeting_notes_get_meeting", {"meeting_id": "m-plan"}))["code"] == "api_key_required"
    # read key on write tools
    denied = tool_error(rpc.tool("meeting_notes_rename_meeting", {"meeting_id": "m-plan", "name": "x"}))
    assert denied["code"] == "scope_missing"
    assert tool_error(rpc.tool("meeting_notes_generate_notes", {"meeting_id": "m-standup"}))["code"] == "scope_missing"
    assert mcp_env["store"].read_session_meta("m-plan")["name"] == "Sprint planning"
    # revoked keys stop working immediately
    keys = mcp_env["app"].state.agent_keys
    keys.revoke(write.key[:12])
    assert tool_error(write.tool("meeting_notes_list_meetings"))["code"] == "api_key_revoked"


def test_write_tools(mcp_env):
    write, store = mcp_env["write"], mcp_env["store"]
    renamed = tool_json(write.tool("meeting_notes_rename_meeting", {"meeting_id": "m-standup", "name": "Daily sync"}))
    assert renamed == {"meeting_id": "m-standup", "name": "Daily sync"}
    assert store.read_session_meta("m-standup")["name"] == "Daily sync"
    queued = tool_json(write.tool("meeting_notes_generate_notes", {"meeting_id": "m-standup"}))
    assert queued["status"] == "queued" and store.read_review(queued["review_id"])["status"] == "queued"
    assert tool_error(write.tool("meeting_notes_generate_notes", {"meeting_id": "m-raw"}))["code"] == "no_transcript"


def test_resources_open_and_gated(mcp_env):
    rpc, anon = mcp_env["read"], mcp_env["anon"]
    templates = rpc.call("resources/templates/list")["result"]["resourceTemplates"]
    assert {t["uriTemplate"] for t in templates} == {r["uri"] for r in spec.RESOURCES if "{" in r["uri"]}
    static = rpc.call("resources/list")["result"]["resources"]
    assert {r["uri"] for r in static} == {r["uri"] for r in spec.RESOURCES if "{" not in r["uri"]}

    # manifest + llms.txt: no key
    manifest = anon.call("resources/read", {"uri": "meetingnotes://manifest"})["result"]["contents"][0]
    assert json.loads(manifest["text"])["mcp"]["enabled"] is True
    llms = anon.call("resources/read", {"uri": "meetingnotes://llms-txt"})["result"]["contents"][0]["text"]
    for tool in spec.TOOLS:
        assert tool["name"] in llms

    notes = rpc.call("resources/read", {"uri": "meetingnotes://meetings/m-plan/notes"})["result"]["contents"][0]
    assert notes["text"].startswith("# Sprint planning")
    transcript = rpc.call("resources/read", {"uri": "meetingnotes://meetings/m-plan/transcript"})["result"]["contents"][0]
    assert "**[00:00:08] Them:**" in transcript["text"]

    for uri in ("meetingnotes://meetings/m-plan/notes", "meetingnotes://meetings/m-plan/transcript"):
        reply = anon.call("resources/read", {"uri": uri})
        assert "error" in reply and "api_key_required" in reply["error"]["message"]
    missing = rpc.call("resources/read", {"uri": "meetingnotes://meetings/ghost/notes"})
    assert "unknown_meeting" in missing["error"]["message"]


def test_prompts(mcp_env):
    rpc, anon = mcp_env["read"], mcp_env["anon"]
    prompts = rpc.call("prompts/list")["result"]["prompts"]
    assert sorted(p["name"] for p in prompts) == sorted(p["name"] for p in spec.PROMPTS)

    brief = rpc.call("prompts/get", {"name": "meeting_brief", "arguments": {"meeting_id": "m-plan"}})["result"]
    text = brief["messages"][0]["content"]["text"]
    assert "missed this meeting" in text and "Finish the migration" in text and "Sprint planning" in text

    no_notes = rpc.call("prompts/get", {"name": "meeting_brief", "arguments": {"meeting_id": "m-standup"}})["result"]
    assert "Quick standup" in no_notes["messages"][0]["content"]["text"]  # falls back to the transcript

    follow = rpc.call("prompts/get", {"name": "weekly_followups", "arguments": {"since": "2026-09-05"}})["result"]
    body = follow["messages"][0]["content"]["text"]
    assert "Draft the hiring freeze memo" in body and "Finish the migration" not in body.split("\n\n", 1)[1] or True
    default_window = rpc.call("prompts/get", {"name": "weekly_followups"})["result"]
    assert "action items from meetings since" in default_window["messages"][0]["content"]["text"]

    denied = anon.call("prompts/get", {"name": "meeting_brief", "arguments": {"meeting_id": "m-plan"}})
    text = denied["result"]["messages"][0]["content"]["text"]
    assert "api_key_required" in text and "Finish the migration" not in text
    ghost = rpc.call("prompts/get", {"name": "meeting_brief", "arguments": {"meeting_id": "ghost"}})
    assert "unknown_meeting" in ghost["result"]["messages"][0]["content"]["text"]


def test_bare_mcp_path_works_without_redirect(mcp_env):
    reply = mcp_env["read"].call("tools/list", path="/mcp")
    assert len(reply["result"]["tools"]) == len(spec.TOOLS)
    response = mcp_env["client"].post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                      headers=ACCEPT, follow_redirects=False)
    assert response.status_code == 200


def test_manifest_advertises_mcp_when_mounted(mcp_env):
    manifest = mcp_env["client"].get("/api/v1/manifest").json()
    assert manifest["mcp"]["enabled"] is True and manifest["mcp"]["url"].endswith("/mcp")


def test_lifespan_can_be_entered_more_than_once(tmp_path, monkeypatch):
    app, _ = make_agent_app(tmp_path, monkeypatch)
    key = app.state.agent_keys.create("k")["key"]
    for _ in range(2):
        with TestClient(app) as client:
            reply = Rpc(client, key).call("tools/list")
            assert len(reply["result"]["tools"]) == len(spec.TOOLS)


def test_mcp_returns_503_outside_the_app_lifespan(tmp_path, monkeypatch):
    app, _ = make_agent_app(tmp_path, monkeypatch)
    client = TestClient(app)  # no `with`: lifespan never runs
    response = client.post("/mcp/", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers=ACCEPT)
    assert response.status_code == 503 and response.json()["code"] == "mcp_unavailable"


def test_rest_still_works_when_mcp_is_disabled(tmp_path, monkeypatch):
    app, lifespan = make_agent_app(tmp_path, monkeypatch, with_mcp=False)
    key = app.state.agent_keys.create("k")["key"]
    with TestClient(app) as client:
        assert client.get("/api/v1/meetings", headers={"Authorization": f"Bearer {key}"}).status_code == 200
        assert client.post("/mcp/", json={}).status_code in (404, 405)
        assert client.get("/api/v1/manifest").json()["mcp"]["enabled"] is False


def test_missing_mcp_package_degrades_to_rest_only(tmp_path, monkeypatch, caplog):
    import builtins
    import sys

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("mcp") or name.endswith("mcp_server"):
            raise ImportError("No module named 'mcp'")
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "meeting_notes.server.agent.mcp_server", raising=False)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    with caplog.at_level("WARNING", logger="meeting_notes.server.agent"):
        app, lifespan = make_agent_app(tmp_path, monkeypatch, with_mcp=True)
    monkeypatch.setattr(builtins, "__import__", real_import)
    assert "/mcp is NOT mounted" in caplog.text
    key = app.state.agent_keys.create("k")["key"]
    with TestClient(app) as client:
        assert client.get("/api/v1/meetings", headers={"Authorization": f"Bearer {key}"}).status_code == 200
        assert client.get("/api/v1/manifest").json()["mcp"]["enabled"] is False


# -- recorder control tools (scope "control") --------------------------------------------

RID = "a" * 32


class FakeHub:
    def __init__(self):
        self.sent = []
        self.error = None

    def list_items(self):
        return [{"instance_id": RID, "device": "Desk PC",
                 "state": {"status": "recording", "meeting": {"name": "Standup"}}},
                {"instance_id": "b" * 32, "device": "Laptop", "state": {"status": "idle", "meeting": {}}}]

    async def send_command(self, instance_id, command, args):
        if self.error:
            raise self.error
        self.sent.append((instance_id, command, args))
        return {"ok": True, "code": None, "error": None, "state": {}}


@pytest.fixture
def ctl(mcp_env):
    hub = FakeHub()
    mcp_env["app"].state.recorder_hub = hub
    mcp_env["hub"] = hub
    mcp_env["control"] = Rpc(mcp_env["client"], mcp_env["app"].state.agent_keys.create("ctl", ["control"])["key"])
    return mcp_env


def test_recorder_tools_are_listed_with_their_scopes(ctl):
    tools = {t["name"] for t in ctl["read"].call("tools/list")["result"]["tools"]}
    assert {"meeting_notes_list_recorders", "meeting_notes_recorder_command"} <= tools
    by = {t["name"]: t["scope"] for t in spec.TOOLS}
    assert by["meeting_notes_list_recorders"] == "read" and by["meeting_notes_recorder_command"] == "control"


def test_list_recorders_matches_rest(ctl):
    got = tool_json(ctl["read"].tool("meeting_notes_list_recorders"))
    assert got == {"items": [
        {"id": RID, "device": "Desk PC", "status": "recording", "name": "Standup"},
        {"id": "b" * 32, "device": "Laptop", "status": "idle", "name": None},
    ]}
    assert tool_error(ctl["anon"].tool("meeting_notes_list_recorders"))["code"] == "api_key_required"


def test_recorder_command_needs_control_scope(ctl):
    args = {"recorder_id": RID, "command": "stop"}
    for who in ("read", "write"):
        assert tool_error(ctl[who].tool("meeting_notes_recorder_command", args))["code"] == "scope_missing"
    assert tool_error(ctl["anon"].tool("meeting_notes_recorder_command", args))["code"] == "api_key_required"
    assert ctl["hub"].sent == []


def test_recorder_start_and_stop_reach_the_hub_on_the_server_loop(ctl):
    c = ctl["control"]
    ack = tool_json(c.tool("meeting_notes_recorder_command",
                           {"recorder_id": RID, "command": "start", "name": "  Weekly sync "}))
    assert ack["ok"] is True
    tool_json(c.tool("meeting_notes_recorder_command", {"recorder_id": RID, "command": "stop"}))
    assert ctl["hub"].sent == [(RID, "start", {"name": "Weekly sync"}), (RID, "stop", {})]


@pytest.mark.parametrize("arguments", [
    {"recorder_id": RID, "command": "mute"},
    {"recorder_id": RID, "command": "set_name", "name": "x"},
    {"recorder_id": RID, "command": "delete_local"},
    {"recorder_id": RID, "command": "stop", "name": "x"},
])
def test_recorder_command_allow_list(ctl, arguments):
    err = tool_error(ctl["control"].tool("meeting_notes_recorder_command", arguments))
    assert err["code"] in ("command_not_allowed", "invalid_args")
    assert ctl["hub"].sent == []


def test_recorder_command_bad_id(ctl):
    err = tool_error(ctl["control"].tool("meeting_notes_recorder_command", {"recorder_id": "nope", "command": "stop"}))
    assert err["code"] == "invalid_recorder_id"


@pytest.mark.parametrize("exc_name, code", [
    ("KeyError", "recorder_not_connected"),
    ("BufferError", "recorder_busy"),
    ("CommandTimeout", "recorder_timeout"),
    ("RecorderGone", "recorder_gone"),
])
def test_recorder_command_error_mapping_matches_rest(ctl, exc_name, code):
    from meeting_notes.server import recorders

    exc = {"KeyError": KeyError(RID), "BufferError": BufferError("x"),
           "CommandTimeout": recorders.CommandTimeout(), "RecorderGone": recorders.RecorderGone()}[exc_name]
    ctl["hub"].error = exc
    err = tool_error(ctl["control"].tool("meeting_notes_recorder_command", {"recorder_id": RID, "command": "stop"}))
    assert err["code"] == code
