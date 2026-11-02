"""Bridge side of the "name from introductions" job (logic in ``intro_names.py`` and ``people.py``).

Mirrors the split-suggestions bridge routes: workflow, transcript, complete and failure. The claim itself is in
``notes_api.claim_review_api`` (offered last, only to a bridge that asks for ``intro_names``). All routes need the
web/admin token. ``install_intro_names`` is one call from ``create_app``.
"""

from __future__ import annotations

import json
import logging
from typing import Callable, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool

from .. import review_contract
from ..ai_login import is_login_failure
from . import auth
from . import intro_names as intro_mod
from . import people as people_mod
from . import store as store_mod

logger = logging.getLogger("meeting_notes.server.intro_names")


def install_intro_names(
    app: FastAPI, *, store, queue: intro_mod.IntroNamesQueue, after_change: Optional[Callable[[], object]] = None,
) -> intro_mod.IntroNamesQueue:
    """``after_change``: the same hook People edits use (the voice recheck's ``request_run``); called after a
    result named or merged someone, so other meetings match their voice again."""
    app.state.intro_names = queue

    def job_or_404(job_id: str) -> dict:
        if not store_mod.is_safe_id(job_id):
            raise HTTPException(status_code=400, detail=f"invalid job id: {job_id!r}")
        job = queue.read(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown intro-names job")
        return job

    async def body_object(request: Request) -> dict:
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="invalid JSON body") from exc
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="request body must be an object")
        return body

    @app.get("/v1/bridge/intro-names/workflow.md")
    async def intro_workflow_api(_auth: None = Depends(auth.require_token)):
        return Response(review_contract.intro_workflow_text(), media_type="text/markdown; charset=utf-8")

    @app.get("/v1/bridge/intro-names/{job_id}/transcript")
    async def intro_transcript_api(job_id: str, _auth: None = Depends(auth.require_token)):
        job = job_or_404(job_id)
        text = await run_in_threadpool(
            intro_mod.transcript_text, store, job.get("session_id", ""), str(job.get("transcript_job_id") or ""))
        return Response(text, media_type="text/plain; charset=utf-8")

    def finish(job_id: str, job: dict, body: dict) -> dict:
        """Check the answer against the transcript, apply what holds, close the job. Runs off the event loop."""
        try:
            queue.active(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        session_id, transcript_job_id = str(job.get("session_id") or ""), str(job.get("transcript_job_id") or "")
        applied: list = []
        # Only while the job's transcript is still the meeting's newest one; an older answer is dropped, not applied.
        latest = store.latest_done_job(session_id) if store.session_exists(session_id) else None
        if latest is not None and latest.get("job_id") == transcript_job_id and people_mod.is_enabled(store.root):
            entries = intro_mod.validate_names(store, session_id, transcript_job_id, body)
            applied = people_mod.apply_intro_names(store.root, session_id, entries)
        done = queue.complete(job_id, applied)
        if any(a["outcome"] in ("named", "merged") for a in applied) and after_change is not None:
            try:
                after_change()
            except Exception:  # noqa: BLE001 - the names are applied; the nightly recheck is the backstop
                logger.warning("intro names: the voice recheck request failed", exc_info=True)
        return queue.public(done)

    @app.post("/v1/bridge/intro-names/{job_id}/complete")
    async def intro_complete_api(job_id: str, request: Request, _auth: None = Depends(auth.require_token)):
        job = job_or_404(job_id)
        body = await body_object(request)
        try:
            review_contract.validate_intro_names(body)
        except review_contract.ReviewValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return await run_in_threadpool(finish, job_id, job, body)

    @app.post("/v1/bridge/intro-names/{job_id}/failure")
    async def intro_failure_api(job_id: str, request: Request, _auth: None = Depends(auth.require_token)):
        job_or_404(job_id)
        body = await body_object(request)
        error = body.get("error")
        if not isinstance(error, str) or not error.strip():
            raise HTTPException(status_code=400, detail="error must be a non-empty string")
        login_failed = getattr(app.state, "ai_login_failed", None)
        if login_failed is not None and is_login_failure(error):
            await run_in_threadpool(login_failed, error)  # the AI login check (ai_health.py) holds the queue
        try:
            job = await run_in_threadpool(queue.fail, job_id, error.strip()[:500])
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return queue.public(job)

    return queue
