"""Reindex and client log bundle routes, installed by ``create_app``."""

from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool
from starlette.formparsers import MultiPartException

from . import auth
from .client_logs import ClientLogError, ClientLogStore
from .route_helpers import _RecordingMultipartParser


def install_ops(app: FastAPI, *, store, _client_log_max) -> None:
    """Reindex and the client log bundles."""

    @app.post("/v1/reindex")
    async def reindex_api(_auth: None = Depends(auth.require_token)):
        count = await run_in_threadpool(store.reindex)
        return {"reindexed": count}

    # -- JSON API: client log bundles (sent from the Windows client's Logs window) --

    client_logs = ClientLogStore(store.root)

    @app.post("/v1/client-logs", status_code=201)
    async def upload_client_logs(request: Request, _auth: None = Depends(auth.require_token)):
        try:
            form = await _RecordingMultipartParser(
                request.headers,
                request.stream(),
                max_files=1,
                max_fields=4,
                max_part_size=16 * 1024,
                max_file_size=_client_log_max(),
                label="log bundle",
            ).parse()
        except MultiPartException as exc:
            raise HTTPException(status_code=413 if "exceeds" in str(exc) else 400, detail=str(exc)) from exc
        except (AssertionError, RuntimeError) as exc:
            raise HTTPException(status_code=415, detail="multipart upload support is not installed") from exc
        try:
            upload = form.get("file")
            if upload is None or not hasattr(upload, "read") or not getattr(upload, "filename", None):
                raise HTTPException(status_code=400, detail="multipart field 'file' is required")
            try:
                return await run_in_threadpool(client_logs.save, form.get("device"), upload.file)
            except ClientLogError as exc:
                raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        finally:
            await form.close()

    @app.get("/v1/client-logs")
    async def list_client_logs(_auth: None = Depends(auth.require_token)):
        return {"items": await run_in_threadpool(client_logs.list)}

    @app.get("/v1/client-logs/{device}/{name}")
    async def download_client_log(device: str, name: str, _auth: None = Depends(auth.require_token)):
        path = client_logs.resolve(device, name)
        if path is None:
            raise HTTPException(status_code=404, detail="unknown log bundle")
        return FileResponse(
            path, media_type="application/zip", filename=f"{device}-{name}",
            headers={"X-Content-Type-Options": "nosniff"},
        )

    @app.delete("/v1/client-logs/{device}/{name}")
    async def delete_client_log(device: str, name: str, _auth: None = Depends(auth.require_token)):
        if not await run_in_threadpool(client_logs.delete, device, name):
            raise HTTPException(status_code=404, detail="unknown log bundle")
        return {"deleted": 1}

    @app.delete("/v1/client-logs")
    async def delete_all_client_logs(_auth: None = Depends(auth.require_token)):
        return {"deleted": await run_in_threadpool(client_logs.delete_all)}
