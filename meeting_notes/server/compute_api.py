"""HTTP API for GPU helpers (``/v1/compute/...``), per ``docs/gpu-helper-design.md``.

Registration and management take the main server token (the recording client registers itself; the web UI lists
and revokes). The work endpoints take a helper key only. The ``/v1/compute/pack/*`` endpoints live in
``helper_pack_api.py``. All the logic is in :class:`compute.HelperRegistry`; this file only maps it to HTTP.
"""

from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from . import auth
from .compute import MAX_BODY_BYTES, ComputeError, HelperRegistry, require_helper_key

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
