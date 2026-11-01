"""Recently deleted routes, installed by ``create_app``."""

from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException
from starlette.concurrency import run_in_threadpool

from . import auth, store as store_mod
from .route_helpers import _safe_session_id_or_400


def install_trash(app: FastAPI, *, store) -> None:
    """Recently deleted: list, empty, restore, purge."""

    # -- JSON API: Recently deleted ----------------------------------------
    # Web/admin token only (require_token), never agent keys: agents can't
    # see, restore or purge deleted meetings.

    @app.get("/v1/trash")
    async def list_trash_api(_auth: None = Depends(auth.require_token)):
        items = await run_in_threadpool(store.list_trash)
        return {"items": items, "total": len(items), "retention_days": store_mod.TRASH_RETENTION_DAYS}

    @app.post("/v1/trash/empty")
    async def empty_trash_api(_auth: None = Depends(auth.require_token)):
        count = await run_in_threadpool(store.empty_trash)
        return {"purged": count}

    @app.post("/v1/trash/{session_id}/restore")
    async def restore_trash_api(session_id: str, _auth: None = Depends(auth.require_token)):
        _safe_session_id_or_400(session_id)
        try:
            record = await run_in_threadpool(store.restore_session, session_id)
        except store_mod.TrashConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if record is None:
            raise HTTPException(status_code=404, detail="not in Recently deleted")
        return {"session_id": session_id, "restored": True}

    @app.delete("/v1/trash/{session_id}")
    async def purge_trash_api(session_id: str, _auth: None = Depends(auth.require_token)):
        _safe_session_id_or_400(session_id)
        if not await run_in_threadpool(store.purge_trashed, session_id):
            raise HTTPException(status_code=404, detail="not in Recently deleted")
        return {"session_id": session_id, "purged": True}
