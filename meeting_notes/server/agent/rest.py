"""REST surface for agents (``/api/v1/*``) plus the admin key routes.

Auth here is the agent key only, read from the ``Authorization`` header (never
from the web login cookie, and never the shared ``MEETING_NOTES_TOKEN``).
Errors are :class:`AgentError` -> ``{"detail", "code"}``.
"""

from __future__ import annotations

import json
from typing import Callable, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from .. import auth
from . import spec
from .errors import AgentError, bad_request
from .keys import AgentKeyStore, resolve_access
from .service import AgentService


def _bearer(request: Request) -> Optional[str]:
    return auth.extract_bearer(request.headers.get("authorization"))


def _text(value):
    """Markdown/text results are plain strings; JSON ones are dicts."""
    if isinstance(value, str):
        media = "text/markdown; charset=utf-8" if value.lstrip().startswith("#") else "text/plain; charset=utf-8"
        return Response(content=value, media_type=media)
    return value


def build_router(
    service: AgentService,
    keys: AgentKeyStore,
    *,
    base_url_getter: Callable[[Request], str],
    mcp_enabled: bool,
) -> APIRouter:
    router = APIRouter()

    def require(scope: str):
        def dependency(request: Request) -> dict:
            return resolve_access(keys, _bearer(request), scope)

        return Depends(dependency)

    read = require("read")
    write = require("write")

    # -- open discovery ---------------------------------------------------------

    @router.get("/api/v1/manifest")
    async def manifest(request: Request):
        return spec.build_manifest(base_url_getter(request), mcp_enabled=mcp_enabled)

    @router.get("/llms.txt", response_class=PlainTextResponse)
    async def llms_txt(request: Request):
        return PlainTextResponse(
            spec.build_llms_txt(base_url_getter(request), mcp_enabled=mcp_enabled),
            media_type="text/plain; charset=utf-8",
        )

    @router.get("/api-docs.md")
    async def api_docs(request: Request):
        return Response(
            spec.build_api_docs(base_url_getter(request), mcp_enabled=mcp_enabled),
            media_type="text/markdown; charset=utf-8",
        )

    # -- reads ------------------------------------------------------------------

    @router.get("/api/v1/meetings")
    async def list_meetings(
        q: Optional[str] = None,
        since: Optional[str] = None,
        date_from: Optional[str] = Query(None, alias="from"),
        date_to: Optional[str] = Query(None, alias="to"),
        has_notes: Optional[str] = None,
        limit: Optional[str] = None,
        cursor: Optional[str] = None,
        _key: dict = read,
    ):
        return await run_in_threadpool(
            service.list_meetings, q, since, date_from, date_to, has_notes, limit, cursor
        )

    @router.get("/api/v1/meetings/{meeting_id}")
    async def get_meeting(meeting_id: str, _key: dict = read):
        return await run_in_threadpool(service.get_meeting, meeting_id)

    @router.get("/api/v1/meetings/{meeting_id}/notes")
    async def get_notes(meeting_id: str, format: str = "json", _key: dict = read):
        return _text(await run_in_threadpool(service.get_notes, meeting_id, format))

    @router.get("/api/v1/meetings/{meeting_id}/transcript")
    async def get_transcript(
        meeting_id: str,
        format: str = "json",
        speaker: Optional[str] = None,
        start_sec: Optional[str] = None,
        end_sec: Optional[str] = None,
        _key: dict = read,
    ):
        return _text(
            await run_in_threadpool(
                service.get_transcript, meeting_id, format, speaker, start_sec, end_sec
            )
        )

    @router.get("/api/v1/search")
    async def search(q: Optional[str] = None, limit: Optional[str] = None, _key: dict = read):
        return await run_in_threadpool(service.search, q or "", limit)

    @router.get("/api/v1/action-items")
    async def action_items(
        since: Optional[str] = None,
        date_from: Optional[str] = Query(None, alias="from"),
        date_to: Optional[str] = Query(None, alias="to"),
        q: Optional[str] = None,
        owner: Optional[str] = None,
        limit: Optional[str] = None,
        cursor: Optional[str] = None,
        _key: dict = read,
    ):
        return await run_in_threadpool(
            service.list_action_items, since, date_from, date_to, q, owner, limit, cursor
        )

    @router.get("/api/v1/decisions")
    async def decisions(
        since: Optional[str] = None,
        date_from: Optional[str] = Query(None, alias="from"),
        date_to: Optional[str] = Query(None, alias="to"),
        q: Optional[str] = None,
        limit: Optional[str] = None,
        cursor: Optional[str] = None,
        _key: dict = read,
    ):
        return await run_in_threadpool(
            service.list_decisions, since, date_from, date_to, q, limit, cursor
        )

    @router.get("/api/v1/note-templates")
    async def note_templates(_key: dict = read):
        return await run_in_threadpool(service.list_note_templates)

    # -- writes (write scope; nothing here deletes) --------------------------------

    @router.post("/api/v1/meetings/{meeting_id}/notes/generate")
    async def generate_notes(
        meeting_id: str,
        force: Optional[str] = None,
        template: Optional[str] = None,
        _key: dict = write,
    ):
        return await run_in_threadpool(service.generate_notes, meeting_id, force, template)

    @router.post("/api/v1/meetings/{meeting_id}/notion")
    async def send_to_notion(meeting_id: str, _key: dict = write):
        return await run_in_threadpool(service.send_to_notion, meeting_id)

    @router.patch("/api/v1/meetings/{meeting_id}")
    async def rename_meeting(meeting_id: str, request: Request, _key: dict = write):
        try:
            body = await request.json()
        except (ValueError, json.JSONDecodeError):
            raise bad_request("invalid JSON body", "invalid_body")
        if not isinstance(body, dict):
            raise bad_request("body must be a JSON object", "invalid_body")
        return await run_in_threadpool(service.rename_meeting, meeting_id, body.get("name"))

    # -- key management: the EXISTING web/admin auth, not an agent key --------------

    admin = Depends(auth.require_token)

    @router.get("/v1/agent-keys")
    async def list_keys(_auth: None = admin):
        return {"items": await run_in_threadpool(keys.list)}

    @router.post("/v1/agent-keys", status_code=201)
    async def create_key(request: Request, _auth: None = admin):
        try:
            body = await request.json()
        except (ValueError, json.JSONDecodeError):
            raise HTTPException(status_code=400, detail="invalid JSON body")
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="body must be a JSON object")
        try:
            return await run_in_threadpool(keys.create, body.get("name"), body.get("scopes"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @router.delete("/v1/agent-keys/{key_id}")
    async def revoke_key(key_id: str, _auth: None = admin):
        record = await run_in_threadpool(keys.revoke, key_id)
        if record is None:
            raise HTTPException(status_code=404, detail="unknown key")
        return record

    return router


def agent_error_response(_request: Request, exc: AgentError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content=exc.body())
