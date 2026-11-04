"""AI notes, names and bridge-review routes, installed by ``create_app``."""

from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from .. import review_contract, scratchpad as scratchpad_mod
from ..ai_login import is_login_failure
from . import ai_health, auth, meeting_stats as meeting_stats_mod, names as names_mod, people as people_mod, settings as settings_mod, store as store_mod
from .route_helpers import _required_name, _safe_session_id_or_400

# Same logger name as app.py, so log records and test captures are unchanged by the split.
logger = logging.getLogger("meeting_notes.server.app")


def install_notes(
    app: FastAPI, *, store, live_preview, _ai_login_failed, _ai_login_works, split_ai, intro_names=None,
) -> None:
    """AI notes: templates, review queue, meeting-notes list/detail/retry/rename/participants, the names vocabulary and the bridge worker's review API."""

    # -- JSON API: explicit AI review queue -------------------------------

    def _review_id_or_400(review_id: str) -> None:
        if not store_mod.is_safe_id(review_id):
            raise HTTPException(status_code=400, detail=f"invalid review_id: {review_id!r}")

    def _rebuild_pending(session_id: str) -> bool:
        """The meeting's notes will be rebuilt on their own: a speakers job that waits to do it is still pending."""
        try:
            job = store.pending_speakers_job(session_id) if store.session_exists(session_id) else None
        except Exception:  # noqa: BLE001 - only a hint
            return False
        return bool(job and job.get("notes_rebuild") and not job.get("notes_after_done"))

    def _review_or_404(review_id: str) -> dict:
        _review_id_or_400(review_id)
        review = store.read_review(review_id)
        if review is None:
            raise HTTPException(status_code=404, detail="unknown review")
        return review

    def _transcript_segments(review: dict) -> list:
        transcript = store.read_transcript(str(review.get("transcript_job_id") or ""))
        if not transcript:
            return []
        try:
            value = json.loads(transcript.get("json") or "{}")
        except (json.JSONDecodeError, TypeError):
            return []
        segments = value.get("segments") if isinstance(value, dict) else None
        return segments if isinstance(segments, list) else []

    def _template_ref_or_400(ref) -> dict:
        """Resolve a caller-supplied template (id or name) to ``{id, name}``.

        A blank ``ref`` means the default note type. A named template that
        does not exist is a 400 rather than a silent fall-back, so a typo in a
        script does not quietly produce notes in the wrong type.
        """
        current = settings_mod.load_settings(store.root)
        if ref is None or (isinstance(ref, str) and not ref.strip()):
            found = current.default_template()
        else:
            found = current.find_template(ref)
            if found is None:
                raise HTTPException(status_code=400, detail=f"unknown note template: {ref!r}")
        return {"id": found["id"], "name": found["name"]}

    def _review_template(review: dict) -> Optional[dict]:
        """``{id, name}`` of the note type a review used, or None for legacy records.

        The live name wins while the template exists (so a rename shows up);
        once it is deleted the name stored on the review is used.
        """
        return settings_mod.load_settings(store.root).review_template(review)

    def _review_list_item(review: dict) -> dict:
        session_id = str(review.get("session_id") or "")
        row = store.session_index_row(session_id) or {}
        payload = review.get("payload") if isinstance(review.get("payload"), dict) else {}
        return {
            "review_id": review.get("review_id"),
            "session_id": session_id,
            "transcript_job_id": review.get("transcript_job_id"),
            "status": review.get("status"),
            "error": review.get("error"),
            "created": row.get("created") or review.get("created"),
            "updated": review.get("updated"),
            "completed_at": review.get("completed_at"),
            "name": row.get("name") or session_id,
            "session_name": row.get("name") or session_id,
            "device": row.get("device"),
            "platform": row.get("platform"),
            "duration_sec": row.get("duration_sec"),
            "title": review.get("title_override") or payload.get("title") or row.get("name") or session_id,
            "participants": payload.get("participants") or [],
            "summary": payload.get("summary"),
            "template": _review_template(review),
        }

    @app.get("/v1/note-templates")
    async def list_note_templates_api(_auth: None = Depends(auth.require_token)):
        """The note types a meeting can be generated with (no prompt text)."""
        current = settings_mod.load_settings(store.root)
        default_id = current.default_template()["id"]
        return {
            "default_template_id": default_id,
            "items": [
                {"id": t["id"], "name": t["name"], "builtin": t["builtin"], "default": t["id"] == default_id,
                 "auto_notes": current.auto_notes_for(t["id"])}
                for t in current.all_templates()
            ],
        }

    @app.post("/v1/sessions/{session_id}/review")
    async def queue_review_api(
        session_id: str,
        force: bool = False,
        template: Optional[str] = None,
        _auth: None = Depends(auth.require_token),
    ):
        _safe_session_id_or_400(session_id)
        if not store.session_exists(session_id):
            raise HTTPException(status_code=404, detail="unknown session")
        chosen = _template_ref_or_400(template)
        try:
            return await run_in_threadpool(store.create_review, session_id, force, chosen)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/v1/meeting-notes")
    async def list_meeting_notes_api(
        page: int = 1,
        per_page: int = 50,
        _auth: None = Depends(auth.require_token),
    ):
        page = max(1, page)
        per_page = max(1, min(per_page, 200))
        reviews = await run_in_threadpool(store.list_reviews)
        start = (page - 1) * per_page
        items = [_review_list_item(review) for review in reviews[start : start + per_page]]
        return {"items": items, "total": len(reviews), "page": page, "per_page": per_page}

    @app.get("/v1/meeting-notes/{review_id}")
    async def meeting_note_detail_api(
        review_id: str, _auth: None = Depends(auth.require_token)
    ):
        review = _review_or_404(review_id)
        session_id = str(review.get("session_id") or "")
        meta = store.read_session_meta(session_id) if store.session_exists(session_id) else {}
        payload = review.get("payload") if isinstance(review.get("payload"), dict) else {}
        segments = _transcript_segments(review)
        stats = meeting_stats_mod.meeting_stats(meta, segments)
        note = {
            **payload,
            "status": review.get("status"),
            "error": review.get("error"),
            "meta": meta,
            "transcript": segments,
            "template": _review_template(review),
            "stats": stats,
        }
        latest = store.latest_done_job(session_id) if store.session_exists(session_id) else None
        written = await run_in_threadpool(people_mod.written_as, store.root, session_id, payload.get("participants"))
        return {
            "review_id": review.get("review_id"),
            "session_id": session_id,
            "transcript_job_id": review.get("transcript_job_id"),
            # a newer transcript (Retranscribe / Find speakers again) than the one these notes were built from
            "transcript_outdated": bool(latest and latest.get("job_id") != review.get("transcript_job_id")),
            # ...and the notes are rebuilt on their own when the speaker labels it is waiting for finish
            "rebuild_pending": _rebuild_pending(session_id),
            "status": review.get("status"),
            "error": review.get("error"),
            "created": review.get("created"),
            "updated": review.get("updated"),
            "completed_at": review.get("completed_at"),
            "template": _review_template(review),
            "notes": payload,
            "note": note,
            "transcript": segments,
            "stats": stats,
            # Who was recognised by voice (the Participants rail merges them with the names the AI wrote, written_as
            # saying which of those names mean this voice in this meeting); a voice with no name carries its
            # profile's main sample, so the rail can play it before linking it.
            "people": [
                {**row, "written_as": written.get(row["person_id"], []),
                 **({"sample_url": people_mod.main_sample_url(store, row["person_id"])}
                    if not row.get("named") and not row.get("is_self") else {})}
                for row in people_mod.heard(store.root, session_id, segments)
            ],
        }

    @app.post("/v1/meeting-notes/{review_id}/retry")
    async def retry_meeting_note_api(
        review_id: str, request: Request, _auth: None = Depends(auth.require_token)
    ):
        """Re-queue a review. Optional JSON body ``{"template": id-or-name}``
        regenerates it in a different note type (no body keeps its note type)."""
        _review_or_404(review_id)
        chosen = None
        raw = await request.body()
        if raw.strip():
            try:
                body = json.loads(raw)
            except (ValueError, json.JSONDecodeError) as exc:
                raise HTTPException(status_code=400, detail="invalid JSON body") from exc
            if not isinstance(body, dict):
                raise HTTPException(status_code=400, detail="body must be a JSON object")
            if body.get("template") not in (None, ""):
                chosen = _template_ref_or_400(body.get("template"))
        try:
            return await run_in_threadpool(store.retry_review, review_id, chosen)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.patch("/v1/meeting-notes/{review_id}")
    async def rename_meeting_note_api(
        review_id: str,
        request: Request,
        _auth: None = Depends(auth.require_token),
    ):
        """Rename a generated meeting summary without altering the meeting name."""
        _review_id_or_400(review_id)
        title = await _required_name(request, "title")
        try:
            review = await run_in_threadpool(store.rename_review, review_id, title)
        except ValueError as exc:
            status_code = 404 if "does not exist" in str(exc) else 409
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc
        return _review_list_item(review)

    def _participants_or_400(body) -> list:
        """``[{"name", "was"}]`` from the request: trimmed single-line names,
        deduped case-insensitively (first wins), at most 50, 120 chars each."""
        if not isinstance(body, dict) or not isinstance(body.get("participants"), list):
            raise HTTPException(status_code=400, detail="participants must be a list")
        people, seen = [], set()
        for raw in body["participants"]:
            was = None
            if isinstance(raw, dict):
                name, was = raw.get("name"), raw.get("was")
            else:
                name = raw
            if not isinstance(name, str) or (was is not None and not isinstance(was, str)):
                raise HTTPException(status_code=400, detail="each participant must be a name")
            name, was = names_mod.clean_name(name), names_mod.clean_name(was) or None
            if not name:
                continue  # a blank row is just dropped
            if len(name) > names_mod.MAX_NAME_CHARS or (was and len(was) > names_mod.MAX_NAME_CHARS):
                raise HTTPException(
                    status_code=400, detail=f"names must be {names_mod.MAX_NAME_CHARS} characters or fewer")
            if name.casefold() in seen:
                continue
            seen.add(name.casefold())
            people.append({"name": name, "was": was})
        if len(people) > 50:
            raise HTTPException(status_code=400, detail="at most 50 participants")
        return people

    @app.put("/v1/meeting-notes/{review_id}/participants")
    async def set_meeting_participants_api(
        review_id: str,
        request: Request,
        _auth: None = Depends(auth.require_token),
    ):
        """Save a corrected participant list on finished notes. Renamed people are
        also corrected in the notes text and action-item owners (not the
        transcript), the names join the server-wide glossary used for future
        notes and transcripts, and a meeting already in Notion is re-synced."""
        _review_id_or_400(review_id)
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="invalid JSON body") from exc
        people = _participants_or_400(body)
        try:
            review = await run_in_threadpool(store.set_review_participants, review_id, people)
        except ValueError as exc:
            status_code = 404 if "does not exist" in str(exc) else 409
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc
        await run_in_threadpool(
            names_mod.record, store.root, [p["name"] for p in people],
            [(p["was"], p["name"]) for p in people if p["was"]],
        )
        live_preview.reset_transcriber()
        return {**_review_list_item(review), "participants": review["participants_override"]}

    @app.get("/v1/names")
    async def get_names_api(_auth: None = Depends(auth.require_token)):
        """The names glossary: known_names and name_corrections (oldest first)."""
        return await run_in_threadpool(names_mod.load, store.root)

    @app.post("/v1/names/add")
    async def add_names_api(request: Request, _auth: None = Depends(auth.require_token)):
        """Add one person (``{"name"}``) or one correction (``{"wrong","right"}``) by hand."""
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="invalid JSON body") from exc
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="body must be a JSON object")
        values = [names_mod.clean_name(body.get(k)) for k in ("name", "wrong", "right")]
        if any(len(v) > names_mod.MAX_NAME_CHARS for v in values):
            raise HTTPException(status_code=400, detail=f"names must be {names_mod.MAX_NAME_CHARS} characters or fewer")
        name, wrong, right = values
        if "name" in body:
            if not name:
                raise HTTPException(status_code=400, detail="type a name")
            data = await run_in_threadpool(names_mod.add_name, store.root, name)
        elif "wrong" in body or "right" in body:
            if not wrong or not right:
                raise HTTPException(status_code=400, detail="type both the misheard spelling and the correct one")
            if wrong == right:
                raise HTTPException(status_code=400, detail="the two spellings are the same")
            try:
                data = await run_in_threadpool(names_mod.add_correction, store.root, wrong, right)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from None
        else:
            raise HTTPException(status_code=400, detail="send name, or wrong and right")
        live_preview.reset_transcriber()
        return data

    @app.post("/v1/names/edit")
    async def edit_names_api(request: Request, _auth: None = Depends(auth.require_token)):
        """Edit one person (``{"name", "new_name"}``) or one correction (``{"wrong", "right", "new_wrong",
        "new_right"}``) in place."""
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="invalid JSON body") from exc
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="body must be a JSON object")
        fields = {k: names_mod.clean_name(body.get(k)) for k in ("new_name", "new_wrong", "new_right")}
        if any(len(v) > names_mod.MAX_NAME_CHARS for v in fields.values()):
            raise HTTPException(status_code=400, detail=f"names must be {names_mod.MAX_NAME_CHARS} characters or fewer")
        try:
            if isinstance(body.get("name"), str):
                if not fields["new_name"]:
                    raise HTTPException(status_code=400, detail="type a name")
                data = await run_in_threadpool(names_mod.rename_name, store.root, body["name"], fields["new_name"])
            elif isinstance(body.get("wrong"), str) and isinstance(body.get("right"), str):
                if not fields["new_wrong"] or not fields["new_right"]:
                    raise HTTPException(status_code=400, detail="type both the misheard spelling and the correct one")
                if fields["new_wrong"] == fields["new_right"]:
                    raise HTTPException(status_code=400, detail="the two spellings are the same")
                data = await run_in_threadpool(
                    names_mod.edit_correction, store.root, body["wrong"], body["right"],
                    fields["new_wrong"], fields["new_right"])
            else:
                raise HTTPException(status_code=400, detail="send name, or wrong and right")
        except KeyError:
            raise HTTPException(status_code=404, detail="that entry is not in the list any more; reload") from None
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        live_preview.reset_transcriber()
        return data

    @app.post("/v1/names/remove")
    async def remove_names_api(request: Request, _auth: None = Depends(auth.require_token)):
        """Remove one known name (``{"name"}``), one correction (``{"wrong","right"}``) or all (``{"all": true}``)."""
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="invalid JSON body") from exc
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="body must be a JSON object")
        if body.get("all") is True:
            data = await run_in_threadpool(names_mod.clear, store.root)
        elif isinstance(body.get("name"), str):
            data = await run_in_threadpool(names_mod.remove_name, store.root, body["name"])
        elif isinstance(body.get("wrong"), str) and isinstance(body.get("right"), str):
            data = await run_in_threadpool(names_mod.remove_correction, store.root, body["wrong"], body["right"])
        else:
            raise HTTPException(status_code=400, detail="send name, wrong and right, or all")
        live_preview.reset_transcriber()
        return data

    # -- Codex bridge API --------------------------------------------------

    @app.get("/v1/bridge/workflow.md")
    async def bridge_workflow(template: Optional[str] = None, _auth: None = Depends(auth.require_token)):
        """The Standard workflow (``ai_workflow``), or another note type via ``?template=``."""
        ai_settings = settings_mod.load_settings(store.root)
        chosen = ai_settings.find_template(template) if template else None
        if template and chosen is None:
            raise HTTPException(status_code=404, detail="unknown note template")
        prompt = chosen["prompt"] if chosen else ai_settings.ai_workflow
        return Response(
            review_contract.workflow_text(prompt),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="meeting-notes-workflow.md"'},
        )

    @app.get("/v1/bridge/review/{review_id}/workflow.md")
    async def bridge_review_workflow(review_id: str, _auth: None = Depends(auth.require_token)):
        """The prompt for ONE review: its chosen note template, resolved when
        the bridge fetches it. A review with no template (queued before
        templates existed) or whose template was since deleted gets the
        current default template."""
        review = _review_or_404(review_id)
        ai_settings = settings_mod.load_settings(store.root)
        chosen = ai_settings.find_template(review.get("template_id")) or ai_settings.default_template()
        # The names glossary is appended here, at serve time, so it reaches every
        # note type without touching the stored prompt text. So are the speakers recognised by voice (which
        # transcript labels are names; '' while voice profiles are off), the in-person line and the user's own notes
        # (the scratchpad typed in the recorder during the meeting).
        session_id = str(review.get("session_id") or "")
        try:
            meta = store.read_session_meta(session_id)
        except Exception:  # noqa: BLE001 - context only; a missing meta just omits it
            meta = {}
        meta = meta if isinstance(meta, dict) else {}
        in_person = names_mod.in_person_section() if meta.get("in_person") else ""
        return Response(
            review_contract.workflow_text(chosen["prompt"]) + names_mod.prompt_section(store.root)
            + people_mod.prompt_section(store.root, session_id) + in_person
            + scratchpad_mod.prompt_section(meta.get("scratchpad")),
            media_type="text/markdown; charset=utf-8",
        )

    @app.get("/v1/bridge/review/claim")
    async def claim_review_api(
        worker_id: Optional[str] = None,
        kinds: Optional[str] = None,
        _auth: None = Depends(auth.require_token),
    ):
        ai_settings = settings_mod.load_settings(store.root)
        # Disabled is a real queue policy: leave reviews queued until an
        # operator selects a provider, rather than claiming work that cannot
        # be processed.
        if ai_settings.ai_provider == "disabled":
            return Response(status_code=204)
        # The same while the provider's login is known to be broken: every job would fail on it, so the
        # queue waits until the login works again (ai_health.py; the banner tells the owner to reconnect).
        if ai_health.is_expired(store.root, ai_settings.ai_provider):
            return Response(status_code=204)
        provider = {
            "name": ai_settings.ai_provider,
            "codex_model": ai_settings.codex_model,
            "claude_model": ai_settings.claude_model,
            "codex_effort": ai_settings.codex_effort,
            "claude_effort": ai_settings.claude_effort,
            "ollama_base_url": ai_settings.ollama_base_url,
            "ollama_model": ai_settings.ollama_model,
        }
        wanted = (kinds or "").split(",")
        # Claim order: (1) a bridge that asks for ``split_suggestions`` first gets the owner's interactive "find
        # topic shifts" jobs, because someone is waiting on them; (2) then, for a bridge that asks for
        # ``intro_names``, the "name voices from introductions" jobs, which run before their meeting's notes (the
        # notes wait on them, intro_names.py); (3) then the meeting-notes reviews. An older bridge never asks for
        # (1) or (2), so never receives one; a bridge that cannot run (2) releases every held note at once.
        if intro_names is not None:
            await run_in_threadpool(intro_names.release_notes, "intro_names" not in wanted)
        if "split_suggestions" in wanted:
            split_job = await run_in_threadpool(split_ai.claim_next)
            if split_job is not None:
                job_id = str(split_job["job_id"])
                return {
                    "id": job_id,
                    "kind": "split_suggestions",
                    "session_id": split_job.get("session_id"),
                    "worker_id": worker_id,
                    "transcript_url": f"/v1/bridge/split-suggestions/{job_id}/transcript",
                    "workflow_url": "/v1/bridge/split-suggestions/workflow.md",
                    "provider": provider,
                }
        if "intro_names" in wanted and intro_names is not None:
            intro_job = await run_in_threadpool(intro_names.claim_next)
            if intro_job is not None:
                job_id = str(intro_job["job_id"])
                return {
                    "id": job_id,
                    "kind": "intro_names",
                    "session_id": intro_job.get("session_id"),
                    "worker_id": worker_id,
                    "transcript_url": f"/v1/bridge/intro-names/{job_id}/transcript",
                    "workflow_url": "/v1/bridge/intro-names/workflow.md",
                    "provider": provider,
                }
        review = await run_in_threadpool(store.claim_next_review)
        if review is None:
            return Response(status_code=204)
        review_id = str(review["review_id"])
        if not review.get("template_id"):
            default = ai_settings.default_template()
            review = await run_in_threadpool(
                store.record_review_template_if_unset,
                review_id, {"id": default["id"], "name": default["name"]},
            ) or review
        return {
            "id": review_id,
            "session_id": review.get("session_id"),
            "transcript_job_id": review.get("transcript_job_id"),
            "worker_id": worker_id,
            "transcript_url": f"/v1/bridge/review/{review_id}/transcript",
            "workflow_url": f"/v1/bridge/review/{review_id}/workflow.md",
            "provider": provider,
        }

    @app.get("/v1/bridge/review/{review_id}/transcript")
    async def bridge_review_transcript(
        review_id: str, _auth: None = Depends(auth.require_token)
    ):
        review = _review_or_404(review_id)
        transcript = store.read_transcript(str(review.get("transcript_job_id") or ""))
        if not transcript:
            raise HTTPException(status_code=404, detail="review transcript is unavailable")
        # Named speakers get their names; the recorder stays "You", which the workflow builds on (the name is in
        # the prompt's "Identified speakers" section instead).
        markdown = people_mod.apply_names_markdown(
            store.root, str(review.get("session_id") or ""), transcript.get("markdown") or "", keep_self=True)
        return Response(markdown, media_type="text/plain; charset=utf-8")

    @app.post("/v1/bridge/review/{review_id}/complete")
    async def complete_review_api(
        review_id: str,
        body: dict,
        _auth: None = Depends(auth.require_token),
    ):
        _review_or_404(review_id)
        try:
            notes = review_contract.validate_notes(body.get("notes"))
        except review_contract.ReviewValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            done = await run_in_threadpool(store.complete_review, review_id, notes)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        await run_in_threadpool(_ai_login_works)
        return done

    @app.post("/v1/bridge/review/{review_id}/failure")
    async def fail_review_api(
        review_id: str,
        body: dict,
        _auth: None = Depends(auth.require_token),
    ):
        _review_or_404(review_id)
        error = body.get("error")
        if not isinstance(error, str) or not error.strip():
            raise HTTPException(status_code=400, detail="error must be a non-empty string")
        if is_login_failure(error) and await run_in_threadpool(_ai_login_failed, error):
            # Not this meeting's fault: it goes back in the queue and runs once the login works again.
            try:
                return await run_in_threadpool(store.requeue_review, review_id)
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        try:
            return await run_in_threadpool(store.fail_review, review_id, error.strip()[:2000])
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
