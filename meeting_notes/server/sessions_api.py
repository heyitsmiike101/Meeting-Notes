"""Meeting (session) routes, installed by ``create_app``."""

from __future__ import annotations

import functools
import json
import math
import time
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from starlette.concurrency import run_in_threadpool

from .. import markdown as markdown_mod, scratchpad as scratchpad_mod, wire
from . import auth, people as people_mod, settings as settings_mod, store as store_mod
from .route_helpers import _UNSET, _client_meta, _name_and_people, _refuse_stale_new_upload, _safe_session_id_or_400, restore_trashed_for


def install_sessions(
    app: FastAPI,
    *,
    store,
    live_sessions,
    live_sessions_lock,
    live_name_overrides,
    live_people_overrides,
    notion,
    recorder_hub,
) -> None:
    """Meeting (session) JSON API: list, live sessions, rename, pipeline, detail, scratchpad, delete audio, delete."""

    _restore_trashed = restore_trashed_for(store)
    def _refuse_audio_delete_while_needed(session_id: str) -> None:
        """Deleting the audio under a queued or running transcription / speakers job would make it fail later."""
        for job in store.jobs_for_session(session_id):
            if job.get("state") in (wire.JobState.QUEUED, wire.JobState.RUNNING) and not store_mod.is_superseded_job(job):
                raise HTTPException(
                    status_code=409,
                    detail="this meeting's audio is still being worked on; delete it once that has finished")

    def _add_note_types(items: list, current=None) -> None:
        """Give each listed meeting ``note_type: {id, name, applied}`` from its index row.

        ``applied`` is True when notes exist (the type they were made with), False when it is the type the
        notes will use (the recorder's tag, else the default). Uses the same rule as the ``note_type`` filter.
        """
        current = current or settings_mod.load_settings(store.root)
        default = current.default_template()
        for item in items:
            tag = item.pop("note_type_tag", None) or ""
            tid = item.pop("review_template_id", None)
            tname = item.pop("review_template_name", None)
            if (item.get("review") or {}).get("review_id"):
                found = current.review_template({"template_id": tid, "template_name": tname}) if tid else None
                if found and not current.find_template(found.get("id")):
                    found = None  # a deleted type counts as the default, as in the filter
                found = found or default
                item["note_type"] = {"id": found.get("id"), "name": found.get("name"), "applied": True}
            else:
                found = current.find_template(tag) if tag else None
                found = found if found and found.get("id") == tag else default
                item["note_type"] = {"id": found.get("id"), "name": found.get("name"), "applied": False}

    @app.get("/v1/sessions")
    async def list_sessions_api(
        q: Optional[str] = None,
        state: Optional[str] = None,
        page: int = 1,
        per_page: int = 50,
        note_type: Optional[str] = None,
        _auth: None = Depends(auth.require_token),
    ):
        page = max(page, 1)
        per_page = max(1, min(per_page, 200))
        current = settings_mod.load_settings(store.root)
        type_ids = [t["id"] for t in current.all_templates()]
        note_type = (note_type or "").strip() or None
        # Off the event loop: SQLite's C driver blocks the thread it runs on,
        # and a search against a very large transcript_text table (or the
        # LIKE fallback) is not guaranteed to be instant.
        result = await run_in_threadpool(
            functools.partial(
                store.list_sessions, q=q, state=state, page=page, per_page=per_page, note_type=note_type,
                note_type_ids=type_ids, default_note_type=current.default_template()["id"],
            )
        )
        # Per-row Notion state from the local state file (no Notion API calls).
        items = result.get("items") or []
        _add_note_types(items, current)
        status = await run_in_threadpool(notion.list_status, [i["session_id"] for i in items if i.get("session_id")])
        for item in items:
            item["notion"] = status.get(item.get("session_id"))
        if people_mod.enabled(current):
            for item in items:  # voice profiles on: whether "Update voice profiles" has anything to match
                item["has_voiceprint"] = people_mod.has_voiceprint(store.root, item.get("session_id") or "")
        return result

    @app.get("/v1/live")
    async def live_sessions_api(_auth: None = Depends(auth.require_token)):
        with live_sessions_lock:
            items = [
                {**entry, "partials": list(entry.get("partials") or [])}
                for entry in live_sessions.values()
            ]
        for entry in items:
            # The seq of the live "Your notes" a connected recorder holds for this meeting (None: none), so the pages
            # re-fetch the rendered notes only when they changed.
            held = recorder_hub.notes_for_session(str(entry.get("session_id") or ""))
            entry["notes_seq"] = held["seq"] if held else None
        return {"items": items, "total": len(items)}

    @app.get("/v1/live/{session_id}/notes")
    async def live_session_notes(session_id: str, _auth: None = Depends(auth.require_token)):
        """The "Your notes" text being typed on the recorder that is recording this live meeting.

        ``{text, session_id, seq, updated_at, instance_id}``; 404 when no connected recorder holds notes for it
        (an older recorder, nothing typed, or the meeting is over: the final text is on the meeting itself)."""
        _safe_session_id_or_400(session_id)
        notes = recorder_hub.notes_for_session(session_id)
        if notes is None:
            raise HTTPException(status_code=404, detail="no live notes for this meeting")
        return notes

    @app.get("/v1/live/{session_id}/notes.html", response_class=HTMLResponse)
    async def live_session_notes_html(session_id: str, _auth: None = Depends(auth.require_token)):
        """The live "Your notes" of this meeting rendered as HTML (``markdown.render`` escapes first); 404 as the JSON route."""
        _safe_session_id_or_400(session_id)
        notes = recorder_hub.notes_for_session(session_id)
        if notes is None:
            raise HTTPException(status_code=404, detail="no live notes for this meeting")
        return HTMLResponse(markdown_mod.render(notes["text"] or ""))

    @app.patch("/v1/live/{session_id}")
    async def rename_live_session(
        session_id: str,
        request: Request,
        _auth: None = Depends(auth.require_token),
    ):
        """Rename an active live meeting and/or set its people count; ended meetings are immutable here."""
        _safe_session_id_or_400(session_id)
        name, people = await _name_and_people(request)
        with live_sessions_lock:
            live = live_sessions.get(session_id)
            if live is None:
                raise HTTPException(status_code=404, detail="live session is no longer active")
            if name is not None:
                live["name"] = name
                live_name_overrides[session_id] = name
            if people is not _UNSET:
                live["people_count"] = people
                live_people_overrides[session_id] = people
            name = live.get("name")
        # Do not write the ordinary session metadata until finalize.  A live
        # stream already has an on-disk session directory for its audio; a
        # premature metadata write would index that incomplete directory and
        # expose a phantom saved-transcription row while the meeting is live.
        # ``live_name_overrides`` is applied to the real client metadata in
        # the finalize endpoint above.
        out = {"session_id": session_id, "name": name}
        if people is not _UNSET:
            out["people_count"] = people
        return out

    @app.patch("/v1/sessions/{session_id}")
    async def rename_session_api(
        session_id: str,
        request: Request,
        _auth: None = Depends(auth.require_token),
    ):
        """Rename a saved meeting and/or set (or, with null, clear) its people count: how many people were in
        it, including the user, 1-50. A changed count applies at the next retranscription."""
        _safe_session_id_or_400(session_id)
        name, people = await _name_and_people(request)
        try:
            if name is not None:
                await run_in_threadpool(store.rename_session, session_id, name)
            if people is not _UNSET:
                await run_in_threadpool(store.set_people_count, session_id, people)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        meta = store.read_session_meta(session_id)
        out = {"session_id": session_id, "name": meta.get("name") or name}
        if people is not _UNSET:
            out["people_count"] = people
        return out

    @app.put("/v1/sessions/{session_id}/pipeline")
    async def update_session_pipeline(
        session_id: str,
        request: Request,
        _auth: None = Depends(auth.require_token),
    ):
        """Persist recorder-side upload progress without queuing STT.

        This is intentionally separate from ``finalize``: a client can make
        an ended recording visible while its media upload is in progress, and
        a later finalize request remains the only operation that queues a
        transcription job.
        """
        _safe_session_id_or_400(session_id)
        try:
            payload = await request.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="invalid JSON body") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="request body must be an object")
        allowed = {"name", "device", "state", "percent", "bytes_received", "bytes_total"}
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise HTTPException(status_code=400, detail=f"unsupported pipeline field: {unknown[0]}")
        if "state" not in payload:
            raise HTTPException(status_code=400, detail="state is required")
        state = str(payload.get("state") or "").strip().lower()
        if state not in {"pending", "uploading", "complete", "error"}:
            raise HTTPException(status_code=400, detail="state must be pending, uploading, complete, or error")

        def _nonnegative_number(field: str, default: float = 0.0) -> float:
            value = payload.get(field, default)
            if isinstance(value, bool):
                raise HTTPException(status_code=400, detail=f"{field} must be a number")
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=f"{field} must be a number") from exc
            if not math.isfinite(number) or number < 0:
                raise HTTPException(status_code=400, detail=f"{field} must not be negative")
            return number

        percent = _nonnegative_number("percent")
        if percent > 100:
            raise HTTPException(status_code=400, detail="percent must be between 0 and 100")
        received = _nonnegative_number("bytes_received")
        total = _nonnegative_number("bytes_total")
        if int(received) != received or int(total) != total:
            raise HTTPException(status_code=400, detail="byte counts must be whole numbers")
        if total and received > total:
            raise HTTPException(status_code=400, detail="bytes_received must not exceed bytes_total")
        if state == "complete" and percent < 100:
            raise HTTPException(status_code=400, detail="complete uploads must have percent=100")

        await _restore_trashed(session_id)
        _refuse_stale_new_upload(request, store.session_exists(session_id))
        if not store.session_exists(session_id) and state != "pending":
            raise HTTPException(status_code=404, detail="unknown session")
        for field in ("name", "device"):
            if field in payload:
                value = payload[field]
                if not isinstance(value, str) or len(value.strip()) > 200:
                    raise HTTPException(status_code=400, detail=f"{field} must be a string of 200 characters or fewer")

        def update_pipeline(meta: dict) -> dict:
            if not meta:
                meta = {"created": time.time(), "tracks": {}}
            for field in ("name", "device"):
                if field in payload:
                    if field == "name" and meta.get("name_updated_at"):
                        continue
                    meta[field] = payload[field].strip()
            meta.update(_client_meta(request))
            meta["upload"] = {
                "state": state,
                "percent": round(percent, 2),
                "bytes_received": int(received),
                "bytes_total": int(total),
            }
            return meta

        store.update_session_meta(session_id, update_pipeline)
        detail = store.session_detail(session_id)
        return {
            "session_id": session_id,
            "state": detail["pipeline"]["state"],
            "pipeline": detail["pipeline"],
        }

    @app.get("/v1/sessions/{session_id}")
    async def session_detail_api(session_id: str, _auth: None = Depends(auth.require_token)):
        _safe_session_id_or_400(session_id)
        detail = await run_in_threadpool(store.session_detail, session_id)
        if detail is None:
            raise HTTPException(status_code=404, detail=_unknown_session_detail(session_id))
        if people_mod.is_enabled(store.root):
            await run_in_threadpool(_with_people, session_id, detail)
        return detail

    def _with_people(session_id: str, detail: dict) -> None:
        """Voice profiles on: every segment gets ``person_id`` and ``display``; pending suggestions are listed."""
        linked = people_mod.overlay(store.root, session_id)
        for segment in detail.get("segments") or []:
            if isinstance(segment, dict):
                hit = linked.get(segment.get("label"))
                segment["person_id"] = hit["person_id"] if hit else None
                segment["display"] = hit["display"] if hit else segment.get("label")
        detail["people_suggestions"] = people_mod.suggestions(store.root, session_id)
        detail["has_voiceprint"] = people_mod.has_voiceprint(store.root, session_id)

    @app.get("/v1/sessions/{session_id}/scratchpad.html", response_class=HTMLResponse)
    async def session_scratchpad_html(session_id: str, _auth: None = Depends(auth.require_token)):
        """The notes scratchpad rendered as HTML for the meeting page ("Your notes"); empty when there is none.

        Safe to inject: ``markdown.render`` escapes first and only emits tags it builds itself."""
        _safe_session_id_or_400(session_id)
        meta = await run_in_threadpool(store.read_session_meta, session_id)
        return HTMLResponse(markdown_mod.render(scratchpad_mod.clean((meta or {}).get("scratchpad")) or ""))

    @app.post("/v1/sessions/{session_id}/delete-audio")
    async def delete_session_audio_api(
        session_id: str, _auth: None = Depends(auth.require_token)
    ):
        _safe_session_id_or_400(session_id)
        if store.session_index_row(session_id) is None:
            raise HTTPException(status_code=404, detail="unknown session")
        _refuse_audio_delete_while_needed(session_id)
        freed = await run_in_threadpool(store.delete_session_audio, session_id)
        return {"session_id": session_id, "bytes_freed": freed}

    def _unknown_session_detail(session_id: str) -> str:
        if store.is_trashed(session_id):
            return "meeting is in Recently deleted; restore it with POST /v1/trash/{id}/restore"
        return "unknown session"

    @app.delete("/v1/sessions/{session_id}")
    async def delete_session_api(
        session_id: str,
        via: Optional[str] = None,
        _auth: None = Depends(auth.require_token),
    ):
        """Move a meeting to Recently deleted. It stays restorable for
        ``TRASH_RETENTION_DAYS`` days; ``DELETE /v1/trash/{id}`` removes it
        for good."""
        _safe_session_id_or_400(session_id)
        if store.session_index_row(session_id) is None:
            raise HTTPException(status_code=404, detail=_unknown_session_detail(session_id))
        source = via if via in ("bulk", "web", "client") else "api"
        record = await run_in_threadpool(lambda: store.trash_session(session_id, source=source))
        if record is None:
            raise HTTPException(status_code=404, detail="unknown session")
        return {
            "session_id": session_id,
            "deleted": True,
            "trashed": True,
            "deleted_at": record["deleted_at"],
            "days_left": store_mod.TRASH_RETENTION_DAYS,
        }


def install_session_actions(app: FastAPI, *, store, job_queue) -> None:
    """Retranscribe, find speakers and reprofile for one meeting."""

    def _queued_transcription(session_id: str) -> Optional[str]:
        """A transcription of this meeting still waiting to start. A second Retranscribe (a double click, or bulk
        Retranscribe over a meeting already in the queue) returns it instead of stacking a duplicate behind it.
        A running one is not reused: Retranscribe stays the way to restart a stuck job."""
        for job in store.jobs_for_session(session_id):
            if (job.get("state") == wire.JobState.QUEUED and not store_mod.is_speakers_job(job)
                    and not store_mod.is_superseded_job(job)):
                return str(job["job_id"])
        return None

    async def _start_find_speakers(session_id: str) -> str:
        """Queue a speakers-only job (re-diarize the stored audio, keep the transcript text) and return its id."""
        _safe_session_id_or_400(session_id)
        problem = await run_in_threadpool(job_queue.check_find_speakers, session_id)
        if problem is not None:
            raise HTTPException(status_code=problem[0], detail=problem[1])
        return await run_in_threadpool(job_queue.enqueue_speakers, session_id)

    async def _run_reprofile(session_id: str) -> dict:
        """Voice profiles: match one meeting's stored voiceprint against the current people again (no audio)."""
        if not people_mod.is_enabled(store.root):
            raise HTTPException(status_code=404, detail="Not Found")
        _safe_session_id_or_400(session_id)
        try:
            return await run_in_threadpool(people_mod.reprofile, store, session_id)
        except people_mod.PersonNotFound as exc:
            raise HTTPException(status_code=404, detail="unknown session") from exc
        except people_mod.NoVoiceprint as exc:
            row = store.session_index_row(session_id) or {}
            raise HTTPException(
                status_code=409,
                detail=("this meeting has no voice data yet; use Find speakers again first" if row.get("has_audio")
                        else "nothing to update: this meeting has no voice data and no audio to find speakers in"),
            ) from exc

    @app.post("/v1/sessions/{session_id}/retranscribe")
    async def retranscribe_api(session_id: str, _auth: None = Depends(auth.require_token)):
        _safe_session_id_or_400(session_id)
        row = store.session_index_row(session_id)
        if row is None:
            raise HTTPException(status_code=404, detail="unknown session")
        if not row.get("has_audio"):
            if (store.read_session_meta(session_id) or {}).get("source") == "transcript":
                raise HTTPException(
                    status_code=400,
                    detail="this meeting was uploaded as a transcript; there is no audio to retranscribe",
                )
            raise HTTPException(status_code=400, detail="no audio available to retranscribe")
        waiting = _queued_transcription(session_id)
        if waiting is not None:
            return {"session_id": session_id, "job_id": waiting, "already_queued": True}
        job_id = job_queue.enqueue(session_id)
        return {"session_id": session_id, "job_id": job_id}

    @app.post("/v1/sessions/{session_id}/find-speakers")
    async def find_speakers_api(session_id: str, _auth: None = Depends(auth.require_token)):
        job_id = await _start_find_speakers(session_id)
        return {"session_id": session_id, "job_id": job_id}

    @app.post("/v1/sessions/{session_id}/reprofile")
    async def reprofile_api(session_id: str, _auth: None = Depends(auth.require_token)):
        return await _run_reprofile(session_id)
