"""HTTP API for GPU helpers (``/v1/compute/...``), per ``docs/gpu-helper-design.md``.

Registration and management take the main server token (the recording client registers itself; the web UI lists
and revokes). The work endpoints take a helper key only. The ``/v1/compute/pack/*`` endpoints live in
``helper_pack_api.py``. All the logic is in :class:`compute.HelperRegistry`; this file only maps it to HTTP.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, WebSocket
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool
from starlette.websockets import WebSocketDisconnect

from . import auth
from .compute import KEY_PREFIX, MAX_BODY_BYTES, ComputeError, HelperRegistry, require_helper_key
from .live_remote import HELLO_TIMEOUT, MAX_FRAME_BYTES, PING_SECONDS

logger = logging.getLogger("meeting_notes.server.compute_api")

router = APIRouter(prefix="/v1/compute", tags=["compute"])


def _registry(request: Request) -> HelperRegistry:
    return request.app.state.compute_registry


def _refuse(exc: ComputeError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.detail)


# -- registration and management (main token) -------------------------------


@router.post("/helpers", status_code=201)
def register_helper(request: Request, payload: dict, _auth: None = Depends(auth.require_token)):
    """Mint a helper key. Returned once; the same ``instance_id`` again replaces the old key."""
    try:
        return _registry(request).create_helper(str(payload.get("name") or ""), str(payload.get("instance_id") or ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/helpers")
def list_helpers(request: Request, _auth: None = Depends(auth.require_token)):
    registry = _registry(request)
    registry.sweep()
    return {"helpers": registry.list_helpers()}


@router.delete("/helpers/{helper_id}")
def revoke_helper(helper_id: str, request: Request, _auth: None = Depends(auth.require_token)):
    view = _registry(request).revoke_helper(helper_id)
    if view is None:
        raise HTTPException(status_code=404, detail="unknown helper")
    return view


# -- work (helper key) --------------------------------------------------------


@router.post("/poll")
def poll(request: Request, payload: dict, helper: dict = Depends(require_helper_key)):
    """Presence update; answers 204 (no work) or 200 with a lease."""
    lease, pack_outdated = _registry(request).poll(helper, payload)
    if lease is None:
        return Response(status_code=204, headers={"X-Pack-Outdated": "1"} if pack_outdated else None)
    return lease


@router.get("/jobs/{job_id}/audio/{track}")
def job_audio(
    job_id: str,
    track: str,
    request: Request,
    x_attempt: Optional[str] = Header(default=None),
    x_lease: Optional[str] = Header(default=None),
    helper: dict = Depends(require_helper_key),
):
    """One track as a 16 kHz mono int16 WAV, for the helper that holds the live attempt."""
    try:
        path = _registry(request).audio_path(helper, job_id, track, x_attempt, x_lease)
    except ComputeError as exc:
        raise _refuse(exc) from exc
    return FileResponse(path, media_type="audio/wav", filename=f"{track}.wav")


@router.post("/jobs/{job_id}/heartbeat")
def heartbeat(job_id: str, request: Request, payload: dict, helper: dict = Depends(require_helper_key)):
    try:
        return _registry(request).heartbeat(helper, job_id, payload)
    except ComputeError as exc:
        raise _refuse(exc) from exc


@router.post("/jobs/{job_id}/release")
def release(job_id: str, request: Request, payload: dict, helper: dict = Depends(require_helper_key)):
    try:
        return _registry(request).release(helper, job_id, payload)
    except ComputeError as exc:
        raise _refuse(exc) from exc


@router.post("/jobs/{job_id}/complete")
async def complete(job_id: str, request: Request, helper: dict = Depends(require_helper_key)):
    """The helper's transcript. The body is read here (not parsed by FastAPI) so the 20 MB limit is enforced."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="result too large")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="result too large")
    try:
        payload = json.loads(bytes(body))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="body must be JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    try:
        return await run_in_threadpool(_registry(request).complete, helper, job_id, payload)
    except ComputeError as exc:
        raise _refuse(exc) from exc


# -- live transcription (helper key, WebSocket) -------------------------------


def _clean_hello(frame) -> Optional[dict]:
    """Validate the helper's hello frame; None when it is not one."""
    if not isinstance(frame, dict) or frame.get("type") != "hello":
        return None
    models = frame.get("models")
    if not isinstance(models, list) or len(models) > 32 or not all(isinstance(m, str) and 0 < len(m) <= 128 for m in models):
        return None
    gpu = frame.get("gpu")
    pack = frame.get("pack_version")
    return {
        "models": models,
        "gpu": gpu if isinstance(gpu, dict) else None,
        "pack_version": pack[:64] if isinstance(pack, str) else None,
    }


async def _ping_loop(live_remote, conn) -> None:
    try:
        while True:
            await asyncio.sleep(PING_SECONDS)
            await live_remote.ping(conn)
    except Exception:  # noqa: BLE001 - a dead socket ends the read loop on its own
        return


async def _safe_close(websocket: WebSocket, code: int) -> None:
    try:
        await websocket.close(code=code)
    except Exception:  # noqa: BLE001 - already closed
        pass


@router.websocket("/live")
async def live(websocket: WebSocket):
    """A helper's live-transcription socket. The key goes in the Authorization header only (never the URL) and is
    checked before the socket is accepted."""
    app = websocket.app
    registry: HelperRegistry = app.state.compute_registry
    live_remote = getattr(app.state, "live_remote", None)
    token = auth.extract_bearer(websocket.headers.get("authorization"))
    if not token or not token.startswith(KEY_PREFIX):
        await websocket.close(code=4401)
        return
    record = registry.authenticate(token)
    if record is None:
        await websocket.close(code=4403)
        return
    if record.get("revoked"):
        await websocket.close(code=4401)
        return
    if live_remote is None:
        await websocket.close(code=1013)
        return
    # Wire the registry <-> live registry (idempotent; app.py may also do this at startup).
    if registry.on_helper_dropped is None:
        registry.on_helper_dropped = live_remote.close_helper
    if registry.live_stats_provider is None:
        registry.live_stats_provider = live_remote.stats

    await websocket.accept()
    try:
        message = await asyncio.wait_for(websocket.receive(), timeout=HELLO_TIMEOUT)
    except (asyncio.TimeoutError, WebSocketDisconnect, RuntimeError):
        await _safe_close(websocket, 4400)
        return
    hello = None
    text = message.get("text")
    if text is not None and len(text) <= MAX_FRAME_BYTES:
        try:
            hello = _clean_hello(json.loads(text))
        except ValueError:
            hello = None
    if hello is None:
        await _safe_close(websocket, 4400)
        return

    conn = live_remote.attach(record["helper_id"], record.get("name", ""), websocket, hello)
    ping_task = asyncio.create_task(_ping_loop(live_remote, conn))
    try:
        while not conn.closed:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            text = message.get("text")
            if text is None:
                if message.get("bytes") is not None:  # binary frames are not part of the protocol
                    await _safe_close(websocket, 1003)
                    break
                continue
            if len(text) > MAX_FRAME_BYTES:
                await _safe_close(websocket, 1009)
                break
            try:
                frame = json.loads(text)
            except ValueError:
                continue
            live_remote.handle_frame(conn, frame)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        ping_task.cancel()
        live_remote.detach(conn)
