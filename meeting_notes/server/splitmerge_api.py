"""HTTP surface for split / combine (logic lives in ``splitmerge.py``).

All routes need the web/admin token (``auth.require_token``); agent keys never
reach them. ``install_split_merge`` is one call from ``create_app``.
"""

from __future__ import annotations

import json
from typing import Callable

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool

from .. import review_contract
from . import auth
from . import splitmerge as sm
from . import store as store_mod


def install_split_merge(
    app: FastAPI,
    *,
    store,
    live_sessions: dict,
    live_sessions_lock,
    ai_enabled: Callable[[], bool],
) -> sm.SplitAiQueue:
    split_ai = sm.SplitAiQueue(store)
    app.state.split_ai = split_ai

    def is_live(session_id: str) -> bool:
        with live_sessions_lock:
            return session_id in live_sessions

    def check_id(session_id: str) -> None:
        if not store_mod.is_safe_id(session_id):
            raise HTTPException(status_code=400, detail=f"invalid session_id: {session_id!r}")

    async def run(fn, *args, **kwargs):
        """Run blocking work off the event loop, relaying ``SplitMergeError`` as an HTTP error."""
        try:
            return await run_in_threadpool(lambda: fn(*args, **kwargs))
        except sm.SplitMergeError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        except store_mod.InvalidId as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    async def body_object(request: Request) -> dict:
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="invalid JSON body") from exc
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="request body must be an object")
        return body

    def flag(body: dict, key: str) -> bool:
        value = body.get(key, False)
        if not isinstance(value, bool):
            raise HTTPException(status_code=400, detail=f"{key} must be true or false")
        return value

    # -- split ------------------------------------------------------------------

    def suggestions(session_id: str) -> dict:
        mat = sm.load_material(store, session_id)
        ai = split_ai.public(split_ai.latest(session_id, mat.transcript_job_id))
        result = sm.suggest_points(mat, ai["suggestions"] if ai["status"] == "done" else None)
        result["ai"] = ai
        result["ai_available"] = ai_enabled()
        return result

    @app.get("/v1/sessions/{session_id}/split-suggestions")
    async def split_suggestions_api(session_id: str, _auth: None = Depends(auth.require_token)):
        check_id(session_id)
        return await run(suggestions, session_id)

    def queue_ai(session_id: str, force: bool) -> dict:
        if not ai_enabled():
            raise sm.SplitMergeError("Turn on an AI provider in Settings to get topic suggestions", 409)
        mat = sm.load_material(store, session_id)
        job = split_ai.enqueue(session_id, mat.transcript_job_id, force=force)
        return split_ai.public(job)

    @app.post("/v1/sessions/{session_id}/split-suggestions/ai")
    async def split_suggestions_ai_api(
        session_id: str, force: bool = False, _auth: None = Depends(auth.require_token)
    ):
        check_id(session_id)
        return await run(queue_ai, session_id, force)

    @app.post("/v1/sessions/{session_id}/split")
    async def split_api(session_id: str, request: Request, _auth: None = Depends(auth.require_token)):
        check_id(session_id)
        body = await body_object(request)
        unknown = sorted(set(body) - {"points", "names", "regenerate_notes"})
        if unknown:
            raise HTTPException(status_code=400, detail=f"unsupported field: {unknown[0]}")
        return await run(
            sm.split_session, store, session_id, body.get("points"),
            names=body.get("names"), regenerate_notes=flag(body, "regenerate_notes"),
            ai_enabled=ai_enabled(), is_live=is_live,
        )

    @app.post("/v1/sessions/{session_id}/unsplit")
    async def unsplit_api(session_id: str, _auth: None = Depends(auth.require_token)):
        check_id(session_id)
        return await run(sm.unsplit, store, session_id)

    # -- combine ------------------------------------------------------------------

    @app.post("/v1/sessions/combine")
    async def combine_api(request: Request, _auth: None = Depends(auth.require_token)):
        body = await body_object(request)
        unknown = sorted(set(body) - {"ids", "name", "regenerate_notes"})
        if unknown:
            raise HTTPException(status_code=400, detail=f"unsupported field: {unknown[0]}")
        return await run(
            sm.combine_sessions, store, body.get("ids"), name=body.get("name"),
            regenerate_notes=flag(body, "regenerate_notes"), ai_enabled=ai_enabled(), is_live=is_live,
        )

    @app.post("/v1/sessions/{session_id}/uncombine")
    async def uncombine_api(session_id: str, _auth: None = Depends(auth.require_token)):
        check_id(session_id)
        return await run(sm.uncombine, store, session_id)

    @app.get("/v1/sessions/{session_id}/continuations")
    async def continuations_api(session_id: str, _auth: None = Depends(auth.require_token)):
        check_id(session_id)
        return await run(sm.continuations, store, session_id)

    # -- bridge side of the optional AI topic-shift job --------------------------------

    def job_or_404(job_id: str) -> dict:
        if not store_mod.is_safe_id(job_id):
            raise HTTPException(status_code=400, detail=f"invalid job id: {job_id!r}")
        job = split_ai.read(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown split-suggestions job")
        return job

    @app.get("/v1/bridge/split-suggestions/workflow.md")
    async def split_workflow_api(_auth: None = Depends(auth.require_token)):
        return Response(review_contract.split_workflow_text(), media_type="text/markdown; charset=utf-8")

    @app.get("/v1/bridge/split-suggestions/{job_id}/transcript")
    async def split_transcript_api(job_id: str, _auth: None = Depends(auth.require_token)):
        job = job_or_404(job_id)
        text = await run_in_threadpool(
            sm.ai_transcript_text, store, job.get("session_id", ""), str(job.get("transcript_job_id") or "")
        )
        return Response(text, media_type="text/plain; charset=utf-8")

    @app.post("/v1/bridge/split-suggestions/{job_id}/complete")
    async def split_complete_api(job_id: str, request: Request, _auth: None = Depends(auth.require_token)):
        job_or_404(job_id)
        body = await body_object(request)
        try:
            review_contract.validate_split_suggestions(body)
        except review_contract.ReviewValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        job = await run(split_ai.complete, job_id, body)
        return split_ai.public(job)

    @app.post("/v1/bridge/split-suggestions/{job_id}/failure")
    async def split_failure_api(job_id: str, request: Request, _auth: None = Depends(auth.require_token)):
        job_or_404(job_id)
        body = await body_object(request)
        error = body.get("error")
        if not isinstance(error, str) or not error.strip():
            raise HTTPException(status_code=400, detail="error must be a non-empty string")
        job = await run(split_ai.fail, job_id, error.strip()[:500])
        return split_ai.public(job)

    return split_ai
