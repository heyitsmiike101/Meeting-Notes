"""Notion export routes, installed by ``create_app``."""

from __future__ import annotations

from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from . import auth, settings as settings_mod
from .route_helpers import _safe_session_id_or_400


def install_notion(app: FastAPI, *, store, notion) -> None:
    """Notion export: status, token, page picker, backfill and per-meeting send/remove."""

    # -- JSON API: Notion export (notion.py) ---------------------------------

    def _notion_status_payload() -> dict:
        current = settings_mod.load_settings(store.root)
        return {
            **notion.connection(),
            "auto_copy": current.notion_auto_copy,
            "auto_types": list(current.notion_auto_types),
            "parents": dict(current.notion_parents),
            "pending": notion.pending_count(),
        }

    @app.get("/v1/notion")
    async def notion_status_api(_auth: None = Depends(auth.require_token)):
        """Connection and destination state. The token itself is never returned."""
        return await run_in_threadpool(_notion_status_payload)

    @app.get("/v1/notion/parents")
    async def notion_parents_api(_auth: None = Depends(auth.require_token)):
        """Each note type's Notion parent page as ``{id, url, title}`` (title is best effort, may be null)."""
        parents = dict(settings_mod.load_settings(store.root).notion_parents)
        return {"items": await run_in_threadpool(notion.parent_pages, parents)}

    @app.get("/v1/notion/pages")
    async def notion_pages_api(refresh: bool = False, _auth: None = Depends(auth.require_token)):
        """Pages the Notion integration can see, as ``{connected, items: [{id, title, url, parent, icon}], error}``
        (``parent`` is the parent page's id or null), for the settings page picker. Cached for a minute."""
        return await run_in_threadpool(notion.list_pages, refresh)

    @app.put("/v1/notion/token")
    async def notion_connect_api(payload: dict, _auth: None = Depends(auth.require_token)):
        """Validate (GET /v1/users/me) and save an integration token. Write-only."""
        token = payload.get("token") if isinstance(payload, dict) else None
        if not isinstance(token, str):
            raise HTTPException(status_code=400, detail="token must be a string")
        result = await run_in_threadpool(notion.connect, token)
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("error") or "Could not connect to Notion")
        return await run_in_threadpool(_notion_status_payload)

    @app.delete("/v1/notion/token")
    async def notion_disconnect_api(_auth: None = Depends(auth.require_token)):
        await run_in_threadpool(notion.disconnect)
        return await run_in_threadpool(_notion_status_payload)

    @app.post("/v1/notion/test")
    async def notion_test_api(_auth: None = Depends(auth.require_token)):
        """Call Notion's ``GET /v1/users/me`` and report the bot / workspace, or why it failed."""
        return await run_in_threadpool(notion.test)

    def _backfill_template_or_400(ref) -> dict:
        current = settings_mod.load_settings(store.root)
        found = current.find_template(ref) if isinstance(ref, str) else None
        if found is None:
            raise HTTPException(status_code=400, detail=f"unknown note template: {ref!r}")
        if not current.notion_parents.get(found["id"]):
            raise HTTPException(
                status_code=409, detail=f"The \"{found['name']}\" note type has no Notion parent page."
            )
        return found

    @app.get("/v1/notion/backfill")
    async def notion_backfill_preview_api(
        template: str, quick: bool = False, _auth: None = Depends(auth.require_token)
    ):
        """What a backfill of one note type would do: ``new`` (never copied), ``moved`` (copied under an old Notion
        page) and ``gone`` (deleted in Notion; checked against Notion unless ``quick``). ``count`` is the total."""
        found = _backfill_template_or_400(template)
        plan = await run_in_threadpool(lambda: notion.backfill_plan(found["id"], check_notion=not quick))
        counts = {k: len(v) for k, v in plan.items()}
        return {"template": {"id": found["id"], "name": found["name"]}, "count": sum(counts.values()), **counts}

    @app.post("/v1/notion/backfill")
    async def notion_backfill_api(payload: dict, _auth: None = Depends(auth.require_token)):
        found = _backfill_template_or_400(payload.get("template") if isinstance(payload, dict) else None)
        if not notion.connected():
            raise HTTPException(status_code=409, detail="Notion is not connected.")
        count = await run_in_threadpool(notion.backfill, found["id"])
        return {"template": {"id": found["id"], "name": found["name"]}, "queued": count}

    @app.get("/v1/sessions/{session_id}/notion")
    async def session_notion_status_api(
        session_id: str, template: Optional[str] = None, _auth: None = Depends(auth.require_token)
    ):
        _safe_session_id_or_400(session_id)
        if not store.session_exists(session_id):
            raise HTTPException(status_code=404, detail="unknown session")
        return await run_in_threadpool(notion.session_status, session_id, template)

    @app.post("/v1/sessions/{session_id}/notion")
    async def session_notion_send_api(
        session_id: str, request: Request, _auth: None = Depends(auth.require_token)
    ):
        """Queue a copy of this meeting's notes to Notion (its note type's parent page)."""
        _safe_session_id_or_400(session_id)
        if not store.session_exists(session_id):
            raise HTTPException(status_code=404, detail="unknown session")
        ok, reason = await run_in_threadpool(notion.can_send, session_id)
        if not ok:
            raise HTTPException(status_code=409, detail=reason)
        base = "" if settings_mod.load_settings(store.root).server_address else str(request.base_url)
        await run_in_threadpool(notion.enqueue_export, session_id, source="manual", base_url=base)
        return await run_in_threadpool(notion.session_status, session_id)

    @app.delete("/v1/sessions/{session_id}/notion")
    async def session_notion_remove_api(session_id: str, _auth: None = Depends(auth.require_token)):
        """"Remove this note from Notion": the meeting's toggle goes to Notion's trash (a block that is already
        gone counts as done), its Notion state is cleared, and it is not sent again automatically until someone
        sends it by hand. Runs in the background; the returned status says ``removing``."""
        _safe_session_id_or_400(session_id)
        if not store.session_exists(session_id):
            raise HTTPException(status_code=404, detail="unknown session")
        if not notion.connected():
            raise HTTPException(status_code=409, detail="Notion is not connected. Add the integration token in Settings.")
        await run_in_threadpool(notion.request_remove, session_id)
        return await run_in_threadpool(notion.session_status, session_id)
