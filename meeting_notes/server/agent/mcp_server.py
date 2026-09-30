"""MCP server for agents, mounted at ``/mcp`` (Streamable HTTP, ``mcp>=2.2``).

Only import this module inside ``try/except ImportError``: the ``mcp`` SDK is
an optional dependency (``requires-python >=3.10``) and the REST half of the
agent surface must keep working without it.

Shape, borrowed from Lifedash and re-verified against the installed SDK:

* an ``MCPServer`` per install (no module globals), tools/resources/prompts
  registered from the tables in ``spec.py``;
* a pure-ASGI middleware copies ``Authorization: Bearer ...`` into a ContextVar
  for the duration of each request; every tool/resource/prompt then calls
  :func:`keys.resolve_access`, the same function the REST dependency uses;
* the SDK's ``StreamableHTTPSessionManager.run()`` can be entered only once per
  instance, so :meth:`McpHost.lifespan` builds a FRESH streamable app (and
  manager) on every entry, and the permanently-mounted dispatcher forwards to
  whichever one the running lifespan built;
* the server runs ``stateless_http`` + ``json_response``: each tool call is a
  plain POST answered with one JSON body. No SSE stream to buffer behind a
  reverse proxy, no session ids to lose across a container restart.
"""

from typing import Any, Callable, Optional

import contextlib
import functools
import json
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone

import anyio.to_thread
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError, ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request as StarletteRequest
from starlette.responses import JSONResponse

from ... import __version__
from . import spec
from .errors import AgentError
from .keys import AgentKeyStore, resolve_access
from .service import AgentService

_bearer: ContextVar = ContextVar("meeting_notes_agent_bearer", default=None)

INSTRUCTIONS = (
    "Meeting Notes turns recorded meetings into speaker-labelled transcripts and AI meeting notes. "
    "It is private: every tool and data resource needs the owner's agent key, sent as "
    "`Authorization: Bearer mnk_...` on the MCP connection. Start by reading the "
    "meetingnotes://manifest resource (or GET /api/v1/manifest); it lists every tool, scope and "
    "convention. Use meeting_notes_list_action_items to answer 'what do I owe people', "
    "meeting_notes_search to find where something was said, and poll with `since` (ISO 8601) "
    "instead of re-reading everything. Tools with write scope only queue notes generation or "
    "rename a meeting; nothing here deletes."
)


class _BearerMiddleware:
    """Pure ASGI (not BaseHTTPMiddleware, which would buffer streamed bodies)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        header = StarletteRequest(scope).headers.get("authorization", "")
        bearer = header[7:].strip() if header.lower().startswith("bearer ") else None
        token = _bearer.set(bearer or None)
        try:
            await self.app(scope, receive, send)
        finally:
            _bearer.reset(token)


def _tool_error(exc: AgentError) -> ToolError:
    return ToolError(exc.json())


def _prompt_error(exc: AgentError) -> str:
    # The SDK collapses any exception raised while rendering a prompt into a
    # generic "Error rendering prompt" protocol error, hiding the code. Return
    # the same {"detail","code"} JSON as the prompt body so the agent can act.
    return "This prompt could not be built. Error: " + exc.json()


def build_mcp(
    service: AgentService,
    keys: AgentKeyStore,
    *,
    base_url_getter: Callable[[], str] = lambda: "",
) -> MCPServer:
    mcp = MCPServer("meeting-notes", version=__version__, instructions=INSTRUCTIONS)

    async def call(scope: str, fn, *args, **kwargs):
        try:
            resolve_access(keys, _bearer.get(), scope)
            return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))
        except AgentError as exc:
            raise _tool_error(exc)

    def described(name: str) -> dict:
        return next(t for t in spec.TOOLS if t["name"] == name)

    # -- tools (one per row of spec.TOOLS; the drift test enforces the match) --------

    @mcp.tool(description=described("meeting_notes_list_meetings")["summary"] + (
        " q searches names and transcript text (board numbers like M-0142 work too). "
        "since/date_from/date_to are ISO 8601 (date_to inclusive). Page with next_cursor."))
    async def meeting_notes_list_meetings(
        q: Optional[str] = None,
        since: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        has_notes: Optional[bool] = None,
        limit: int = 50,
        cursor: Optional[str] = None,
    ) -> Any:
        return await call("read", service.list_meetings, q, since, date_from, date_to, has_notes, limit, cursor)

    @mcp.tool(description=described("meeting_notes_get_meeting")["summary"])
    async def meeting_notes_get_meeting(meeting_id: str) -> Any:
        return await call("read", service.get_meeting, meeting_id)

    @mcp.tool(description=described("meeting_notes_get_notes")["summary"] + " format is 'json' or 'markdown'.")
    async def meeting_notes_get_notes(meeting_id: str, format: str = "json") -> Any:
        return await call("read", service.get_notes, meeting_id, format)

    @mcp.tool(description=described("meeting_notes_get_transcript")["summary"] + (
        " format is 'json', 'markdown' or 'text'; speaker is a label such as 'You' or 'Them'; "
        "start_sec/end_sec select a window in seconds from the meeting start."))
    async def meeting_notes_get_transcript(
        meeting_id: str,
        format: str = "json",
        speaker: Optional[str] = None,
        start_sec: Optional[float] = None,
        end_sec: Optional[float] = None,
    ) -> Any:
        return await call("read", service.get_transcript, meeting_id, format, speaker, start_sec, end_sec)

    @mcp.tool(description=described("meeting_notes_search")["summary"])
    async def meeting_notes_search(q: str, limit: int = 20) -> Any:
        return await call("read", service.search, q, limit)

    @mcp.tool(description=described("meeting_notes_list_action_items")["summary"] + (
        " owner filters by (partial) owner name. since/date_from/date_to are ISO 8601. Page with next_cursor."))
    async def meeting_notes_list_action_items(
        since: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        q: Optional[str] = None,
        owner: Optional[str] = None,
        limit: int = 50,
        cursor: Optional[str] = None,
    ) -> Any:
        return await call("read", service.list_action_items, since, date_from, date_to, q, owner, limit, cursor)

    @mcp.tool(description=described("meeting_notes_list_decisions")["summary"])
    async def meeting_notes_list_decisions(
        since: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        q: Optional[str] = None,
        limit: int = 50,
        cursor: Optional[str] = None,
    ) -> Any:
        return await call("read", service.list_decisions, since, date_from, date_to, q, limit, cursor)

    @mcp.tool(description=described("meeting_notes_generate_notes")["summary"] + (
        " template is an optional note style id or name (see meeting_notes_list_note_templates); "
        "force=true regenerates existing notes, e.g. in a different style. Needs the write scope."))
    async def meeting_notes_generate_notes(
        meeting_id: str, force: bool = False, template: Optional[str] = None
    ) -> Any:
        return await call("write", service.generate_notes, meeting_id, force, template)

    @mcp.tool(description=described("meeting_notes_list_note_templates")["summary"])
    async def meeting_notes_list_note_templates() -> Any:
        return await call("read", service.list_note_templates)

    @mcp.tool(description=described("meeting_notes_rename_meeting")["summary"] + " Needs the write scope.")
    async def meeting_notes_rename_meeting(meeting_id: str, name: str) -> Any:
        return await call("write", service.rename_meeting, meeting_id, name)

    # -- resources -----------------------------------------------------------------

    def manifest_doc() -> dict:
        return spec.build_manifest(base_url_getter(), mcp_enabled=True)

    @mcp.resource("meetingnotes://manifest", mime_type="application/json")
    def manifest_resource() -> dict:
        """Same content as GET /api/v1/manifest. Open, no key needed."""
        return manifest_doc()

    @mcp.resource("meetingnotes://llms-txt", mime_type="text/plain")
    def llms_txt_resource() -> str:
        """Same content as GET /llms.txt. Open, no key needed."""
        return spec.build_llms_txt(base_url_getter(), mcp_enabled=True)

    def guarded(fn, *args) -> str:
        try:
            resolve_access(keys, _bearer.get(), "read")
            return fn(*args)
        except AgentError as exc:
            raise ResourceError(exc.json())

    @mcp.resource("meetingnotes://meetings/{id}/notes", mime_type="text/markdown")
    def notes_resource(id: str) -> str:
        """Meeting notes as markdown (agent key required)."""
        return guarded(service.get_notes, id, "markdown")

    @mcp.resource("meetingnotes://meetings/{id}/transcript", mime_type="text/markdown")
    def transcript_resource(id: str) -> str:
        """Transcript as markdown (agent key required)."""
        return guarded(service.get_transcript, id, "markdown")

    # -- prompts -------------------------------------------------------------------

    @mcp.prompt(description=next(p for p in spec.PROMPTS if p["name"] == "meeting_brief")["summary"])
    def meeting_brief(meeting_id: str) -> str:
        try:
            resolve_access(keys, _bearer.get(), "read")
            meeting = service.get_meeting(meeting_id)
            try:
                transcript = service.get_transcript(meeting_id, "text")
            except AgentError:
                transcript = ""
        except AgentError as exc:
            return _prompt_error(exc)
        notes = meeting.get("notes")
        data = {"meeting": {k: meeting[k] for k in ("id", "name", "created", "duration_sec")}, "notes": notes}
        if not notes:
            data["transcript"] = transcript[:20000]
        return (
            "Write a brief for someone who missed this meeting: what it was about, what was decided, "
            "what actions were assigned (with owners and dates), and any open questions or risks. "
            "Keep it under 200 words, plain language, no filler. If notes are missing, work from the "
            "transcript and say so.\n\n" + json.dumps(data, ensure_ascii=False, indent=2)
        )

    @mcp.prompt(description=next(p for p in spec.PROMPTS if p["name"] == "weekly_followups")["summary"])
    def weekly_followups(since: str = "") -> str:
        if not since.strip():
            since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            resolve_access(keys, _bearer.get(), "read")
            items = service.list_action_items(since=since, limit=200)
        except AgentError as exc:
            return _prompt_error(exc)
        return (
            f"These are the action items from meetings since {since}. Group them by owner, put items "
            "with due dates first, and flag anything without an owner. Then list the follow-ups the "
            "user personally owes people, most urgent first, as a short checklist.\n\n"
            + json.dumps(items, ensure_ascii=False, indent=2)
        )

    registered = {t.name for t in _list_tools(mcp)}
    expected = {t["name"] for t in spec.TOOLS}
    if registered != expected:  # pragma: no cover - guarded by tests
        raise RuntimeError(f"MCP tools drifted from spec.TOOLS: {sorted(registered ^ expected)}")
    return mcp


def _list_tools(mcp: MCPServer):
    return mcp._tool_manager.list_tools()


class McpHost:
    """Owns the fresh-per-lifespan streamable app and the permanent ASGI dispatcher."""

    def __init__(self, mcp: MCPServer):
        self.mcp = mcp
        self._target = None

    def _build(self):
        starlette_app = self.mcp.streamable_http_app(
            streamable_http_path="/",
            stateless_http=True,
            json_response=True,
            # Reached as meeting.lan / an IP / behind a proxy: the SDK's
            # rebinding allow-list would 421 all of them, and this server has
            # no browser-origin boundary to protect (bearer key on every call).
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        )
        return _BearerMiddleware(starlette_app), self.mcp.session_manager

    async def asgi(self, scope, receive, send) -> None:
        target = self._target
        if target is None:
            response = JSONResponse(
                {"detail": "MCP server is not running", "code": "mcp_unavailable"}, status_code=503
            )
            await response(scope, receive, send)
            return
        await target(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(self):
        target, manager = self._build()
        previous = self._target
        self._target = target
        try:
            async with manager.run():
                yield
        finally:
            self._target = previous
