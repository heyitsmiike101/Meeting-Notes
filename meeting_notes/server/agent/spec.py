"""The ONE route table and the ONE tool table for the agent surface.

``manifest``, ``/llms.txt`` and ``/api-docs.md`` are all rendered from
``ROUTES``/``TOOLS``/``RESOURCES``/``PROMPTS`` below; the REST router and the
MCP server must register exactly what is listed here (tests assert both
directions, so a new route or tool that is not documented fails the suite).
"""

from __future__ import annotations

from typing import List, Optional

from ... import __version__

APP_NAME = "meeting-notes"
KEY_HEADER = "Authorization: Bearer mnk_<key>"
API_PREFIX = "/api/v1"

# audience: "open" (no key), "agent" (agent key, scope shown), "admin" (the
# existing web/admin token -- for humans managing keys, not for agents).
ROUTES: List[dict] = [
    {"method": "GET", "path": "/api/v1/manifest", "audience": "open", "scope": None,
     "summary": "Machine-readable description of this API (auth, scopes, endpoints, tools).", "params": [], "tool": None},
    {"method": "GET", "path": "/llms.txt", "audience": "open", "scope": None,
     "summary": "Plain-text index: one line per endpoint and per MCP tool.", "params": [], "tool": None},
    {"method": "GET", "path": "/api-docs.md", "audience": "open", "scope": None,
     "summary": "This API described in Markdown.", "params": [], "tool": None},
    {"method": "GET", "path": "/api/v1/meetings", "audience": "agent", "scope": "read",
     "summary": "List meetings, newest first, with notes/transcription state.",
     "params": ["q: search names + transcript text (also M-0142 board numbers)",
                "since: ISO 8601; meetings created, updated or with notes completed after it",
                "from, to: ISO 8601 date/datetime range on the meeting date (to is inclusive)",
                "has_notes: true|false", "limit: 1-200 (default 50)", "cursor: next_cursor from the previous page"],
     "tool": "meeting_notes_list_meetings"},
    {"method": "GET", "path": "/api/v1/meetings/{id}", "audience": "agent", "scope": "read",
     "summary": "One meeting: metadata, notes (if done) and transcript availability.", "params": [],
     "tool": "meeting_notes_get_meeting"},
    {"method": "GET", "path": "/api/v1/meetings/{id}/notes", "audience": "agent", "scope": "read",
     "summary": "AI meeting notes (summary, decisions, action items, key points).",
     "params": ["format: json (default) | markdown"], "tool": "meeting_notes_get_notes"},
    {"method": "GET", "path": "/api/v1/meetings/{id}/transcript", "audience": "agent", "scope": "read",
     "summary": "Speaker-labelled transcript with timestamps.",
     "params": ["format: json (default) | markdown | text", "speaker: only this speaker label (e.g. You, Them)",
                "start_sec, end_sec: only segments overlapping this window"],
     "tool": "meeting_notes_get_transcript"},
    {"method": "GET", "path": "/api/v1/note-templates", "audience": "agent", "scope": "read",
     "summary": "Note types (prompts) that notes generation can use: id, name, builtin, default.", "params": [],
     "tool": "meeting_notes_list_note_templates"},
    {"method": "GET", "path": "/api/v1/search", "audience": "agent", "scope": "read",
     "summary": "Full-text search across transcripts and notes; returns matching snippets.",
     "params": ["q: required", "limit: 1-50 (default 20)"], "tool": "meeting_notes_search"},
    {"method": "GET", "path": "/api/v1/action-items", "audience": "agent", "scope": "read",
     "summary": "Action items across all meetings with notes (what do I owe people).",
     "params": ["since, from, to: as for meetings", "q: substring filter", "owner: substring filter on owner",
                "limit: 1-200 (default 50)", "cursor"], "tool": "meeting_notes_list_action_items"},
    {"method": "GET", "path": "/api/v1/decisions", "audience": "agent", "scope": "read",
     "summary": "Decisions across all meetings with notes.",
     "params": ["since, from, to: as for meetings", "q: substring filter", "limit: 1-200 (default 50)", "cursor"],
     "tool": "meeting_notes_list_decisions"},
    {"method": "POST", "path": "/api/v1/meetings/{id}/notes/generate", "audience": "agent", "scope": "write",
     "summary": "Queue AI notes generation for a meeting (idempotent unless force).",
     "params": ["force: true|false (query) - regenerate even if notes exist",
                "template: note type id or name (query, optional; see /api/v1/note-templates). "
                "Default note type when omitted. Applies only when a generation is queued, so combine with force "
                "to re-run existing notes in another note type"],
     "tool": "meeting_notes_generate_notes"},
    {"method": "POST", "path": "/api/v1/meetings/{id}/notion", "audience": "agent", "scope": "write",
     "summary": "Queue a copy of the meeting's notes to Notion (its note type's parent page; needs a connected "
                "integration). Status appears as notion {state, url} on the meeting and its notes.",
     "params": [], "tool": "meeting_notes_send_to_notion"},
    {"method": "PATCH", "path": "/api/v1/meetings/{id}", "audience": "agent", "scope": "write",
     "summary": "Rename a meeting. JSON body: {\"name\": \"...\"}.", "params": ["name: 1-200 characters (body)"],
     "tool": "meeting_notes_rename_meeting"},
    {"method": "GET", "path": "/v1/agent-keys", "audience": "admin", "scope": None,
     "summary": "List agent keys (never shows secrets). Web/admin token.", "params": [], "tool": None},
    {"method": "POST", "path": "/v1/agent-keys", "audience": "admin", "scope": None,
     "summary": "Create an agent key; plaintext returned once. Body {name, scopes}. Web/admin token.",
     "params": [], "tool": None},
    {"method": "DELETE", "path": "/v1/agent-keys/{id}", "audience": "admin", "scope": None,
     "summary": "Revoke an agent key (by id or prefix). Web/admin token.", "params": [], "tool": None},
]

TOOLS: List[dict] = [
    {"name": "meeting_notes_list_meetings", "scope": "read", "route": "GET /api/v1/meetings",
     "summary": "List meetings (q, since, from, to, has_notes, limit, cursor)."},
    {"name": "meeting_notes_get_meeting", "scope": "read", "route": "GET /api/v1/meetings/{id}",
     "summary": "Get one meeting with its notes and transcript availability."},
    {"name": "meeting_notes_get_notes", "scope": "read", "route": "GET /api/v1/meetings/{id}/notes",
     "summary": "Get AI notes for a meeting as JSON or markdown."},
    {"name": "meeting_notes_get_transcript", "scope": "read", "route": "GET /api/v1/meetings/{id}/transcript",
     "summary": "Get a transcript as json/markdown/text, filter by speaker or time window."},
    {"name": "meeting_notes_search", "scope": "read", "route": "GET /api/v1/search",
     "summary": "Full-text search across transcripts and notes with snippets."},
    {"name": "meeting_notes_list_action_items", "scope": "read", "route": "GET /api/v1/action-items",
     "summary": "Action items across meetings (since, from, to, q, owner)."},
    {"name": "meeting_notes_list_decisions", "scope": "read", "route": "GET /api/v1/decisions",
     "summary": "Decisions across meetings (since, from, to, q)."},
    {"name": "meeting_notes_list_note_templates", "scope": "read", "route": "GET /api/v1/note-templates",
     "summary": "List the note types (templates) available for notes generation."},
    {"name": "meeting_notes_generate_notes", "scope": "write", "route": "POST /api/v1/meetings/{id}/notes/generate",
     "summary": "Queue AI notes generation for a meeting (optional note-style template)."},
    {"name": "meeting_notes_send_to_notion", "scope": "write", "route": "POST /api/v1/meetings/{id}/notion",
     "summary": "Copy a meeting's notes to Notion (state/url are on the meeting as `notion`)."},
    {"name": "meeting_notes_rename_meeting", "scope": "write", "route": "PATCH /api/v1/meetings/{id}",
     "summary": "Rename a meeting."},
]

RESOURCES: List[dict] = [
    {"uri": "meetingnotes://manifest", "summary": "Same content as GET /api/v1/manifest (open)."},
    {"uri": "meetingnotes://llms-txt", "summary": "Same content as GET /llms.txt (open)."},
    {"uri": "meetingnotes://meetings/{id}/notes", "summary": "Meeting notes as markdown (key required)."},
    {"uri": "meetingnotes://meetings/{id}/transcript", "summary": "Transcript as markdown (key required)."},
]

PROMPTS: List[dict] = [
    {"name": "meeting_brief", "summary": "Brief someone who missed a meeting (arg: meeting_id)."},
    {"name": "weekly_followups", "summary": "What action items are owed since a date (arg: since)."},
]

CONVENTIONS = {
    "errors": 'Every error is JSON {"detail": "...", "code": "..."} with the matching HTTP status.',
    "pagination": "List endpoints take limit (capped) and cursor; pass the returned next_cursor back until it is null.",
    "incremental": "Poll with ?since=<ISO 8601> to get only what changed after that instant.",
    "timestamps": "ISO 8601 UTC ('Z'). Transcript start/end are seconds from the start of the meeting.",
    "privacy": "Meeting content is private: every data endpoint and tool requires an agent key.",
}


def agent_routes() -> List[dict]:
    return [r for r in ROUTES if r["audience"] in ("open", "agent")]


def route_key(route: dict) -> str:
    return f"{route['method']} {route['path']}"


def _base(base_url: Optional[str]) -> str:
    return (base_url or "").rstrip("/")


def build_manifest(base_url: Optional[str], *, mcp_enabled: bool) -> dict:
    base = _base(base_url)
    return {
        "name": APP_NAME,
        "version": __version__,
        "description": "Meeting recordings turned into speaker-labelled transcripts and AI meeting notes.",
        "base_url_hint": base,
        "auth": {
            "type": "bearer",
            "header": KEY_HEADER,
            "key_prefix": "mnk_",
            "scopes": {
                "read": "Read meetings, notes, transcripts, action items, decisions, search.",
                "write": "Also queue notes generation and rename meetings. Never deletes anything.",
            },
            "note": "Every data endpoint and tool needs a key. Ask the owner to create one in Settings.",
        },
        "endpoints": [
            {k: r[k] for k in ("method", "path", "scope", "summary", "params", "tool")}
            for r in ROUTES if r["audience"] in ("open", "agent")
        ],
        "admin_endpoints": [
            {k: r[k] for k in ("method", "path", "summary")} for r in ROUTES if r["audience"] == "admin"
        ],
        "mcp": {
            "enabled": mcp_enabled,
            "url": f"{base}/mcp",
            "transport": "streamable-http",
            "auth_header": KEY_HEADER,
        },
        "tools": [dict(t) for t in TOOLS],
        "resources": [dict(r) for r in RESOURCES],
        "prompts": [dict(p) for p in PROMPTS],
        "docs": {"llms_txt": f"{base}/llms.txt", "api_docs": f"{base}/api-docs.md", "manifest": f"{base}{API_PREFIX}/manifest"},
        "conventions": dict(CONVENTIONS),
    }


def build_llms_txt(base_url: Optional[str], *, mcp_enabled: bool) -> str:
    base = _base(base_url)
    lines = [
        f"# {APP_NAME}",
        "",
        "> Meeting recordings as speaker-labelled transcripts and AI meeting notes. Private: a key is required for all data.",
        "",
        f"Auth: send `{KEY_HEADER}` on every data request. Scopes: read (default), write (queue notes, rename).",
        f"Start with GET {base}{API_PREFIX}/manifest. Errors are JSON {{detail, code}}. "
        "Poll with ?since=<ISO 8601>; lists take limit and cursor.",
        "",
        "## REST endpoints",
    ]
    for r in agent_routes():
        need = "no key" if r["audience"] == "open" else f"{r['scope']} key"
        lines.append(f"- {r['method']} {base}{r['path']} ({need}): {r['summary']}")
    lines += ["", f"## MCP ({'enabled' if mcp_enabled else 'not installed on this server'})",
              f"Streamable HTTP at {base}/mcp with the same Authorization header."]
    for t in TOOLS:
        lines.append(f"- tool {t['name']} ({t['scope']}): {t['summary']}")
    for r in RESOURCES:
        lines.append(f"- resource {r['uri']}: {r['summary']}")
    for p in PROMPTS:
        lines.append(f"- prompt {p['name']}: {p['summary']}")
    lines += ["", "## Docs", f"- {base}/api-docs.md", f"- {base}{API_PREFIX}/manifest", ""]
    return "\n".join(lines)


def build_api_docs(base_url: Optional[str], *, mcp_enabled: bool) -> str:
    base = _base(base_url)
    lines = [
        f"# {APP_NAME} agent API",
        "",
        "Read (and lightly write) meeting transcripts and AI notes from an agent.",
        "",
        "## Authentication",
        "",
        f"Send `{KEY_HEADER}`. Keys are created by the owner (Settings, or `POST /v1/agent-keys` with the admin token)",
        "and shown once. Scopes: `read` (default) and `write` (queue notes generation, rename a meeting).",
        "Agents can never delete anything. The web/recorder token does not work here, and agent keys do not work",
        "on the web UI or recorder API.",
        "",
        "## Conventions",
        "",
    ]
    lines += [f"- **{k}**: {v}" for k, v in CONVENTIONS.items()]
    lines += ["", "## Endpoints", ""]
    for r in agent_routes():
        need = "no key" if r["audience"] == "open" else f"{r['scope']} key"
        lines.append(f"### `{r['method']} {r['path']}`")
        lines.append("")
        lines.append(f"{r['summary']} ({need})")
        if r["params"]:
            lines.append("")
            lines += [f"- `{p.split(':', 1)[0]}`:{p.split(':', 1)[1]}" if ":" in p else f"- {p}" for p in r["params"]]
        if r["tool"]:
            lines += ["", f"MCP tool: `{r['tool']}`"]
        lines.append("")
    lines += ["## Key management (admin token, for humans)", ""]
    for r in ROUTES:
        if r["audience"] == "admin":
            lines.append(f"- `{r['method']} {r['path']}`: {r['summary']}")
    lines += ["", f"## MCP ({'enabled' if mcp_enabled else 'not installed on this server'})", "",
              f"Streamable HTTP at `{base}/mcp`, same bearer key. Read the `meetingnotes://manifest` resource first.", ""]
    for t in TOOLS:
        lines.append(f"- `{t['name']}` ({t['scope']}) - {t['summary']}  (REST: `{t['route']}`)")
    lines.append("")
    for r in RESOURCES:
        lines.append(f"- resource `{r['uri']}` - {r['summary']}")
    for p in PROMPTS:
        lines.append(f"- prompt `{p['name']}` - {p['summary']}")
    lines.append("")
    return "\n".join(lines)
