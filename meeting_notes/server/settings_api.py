"""Settings routes (the /settings form and the JSON settings API), installed by ``create_app``."""

from __future__ import annotations

import dataclasses
import json
import logging

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from starlette.concurrency import run_in_threadpool

from . import auth, board as board_mod, people as people_mod, queue_view, settings as settings_mod, store as store_mod, voice_recheck as voice_recheck_mod, web

# Same logger name as app.py, so log records and test captures are unchanged by the split.
logger = logging.getLogger("meeting_notes.server.app")


def queue_type_renamer(notion):

    def _queue_type_renames(old: settings_mod.Settings, new: settings_mod.Settings) -> None:
        """Note type names show up in Notion month page titles: retitle them in the background."""
        try:
            old_names = {t["id"]: t["name"] for t in old.all_templates()}
            for t in new.all_templates():
                if t["id"] in old_names and old_names[t["id"]] != t["name"]:
                    notion.queue_month_retitle(t["id"], old_names[t["id"]], t["name"])
        except Exception:  # noqa: BLE001
            logger.exception("could not queue Notion month page renames")
    return _queue_type_renames


def install_settings_pages(
    app: FastAPI,
    *,
    store,
    live_preview,
    compute_registry,
    helper_pack,
    retention_worker,
    voice_recheck,
    notion,
) -> None:
    """The /settings form, the GPU-helper summary and /v1/queue."""

    _queue_type_renames = queue_type_renamer(notion)
    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(_auth: None = Depends(auth.require_web_token)):
        current = settings_mod.load_settings(store.root)
        return web.render_settings_page(current, token_configured=auth.token_is_configured())

    @app.get("/settings/gpu-helpers")
    async def settings_gpu_helpers(_auth: None = Depends(auth.require_web_token)):
        """Data for the GPU helpers block and the Recorders page: helpers with the meeting each is working on,
        and the helper pack (status plus when it was built and the note about speaker labels)."""
        return await run_in_threadpool(_gpu_helpers_view)

    def _gpu_helpers_view() -> dict:
        compute_registry.sweep()
        helpers = []
        for row in compute_registry.list_helpers():
            if row.get("revoked"):
                continue
            board = None
            job_id = row.get("current_job")
            if job_id and store_mod.is_safe_id(str(job_id)):
                job = store.read_job(str(job_id)) or {}
                if job.get("session_id"):
                    board = board_mod.board_number(str(job["session_id"]))
            helpers.append({
                "helper_id": row["helper_id"], "name": row.get("name", ""), "gpu": row.get("gpu"),
                "online": bool(row.get("online")), "state": row.get("state"), "paused": bool(row.get("paused")),
                "last_seen": row.get("last_seen"), "progress": row.get("progress"),
                "current_board": board, "last_error": row.get("last_error"), "live": row.get("live"),
            })
        pack = helper_pack.status()
        manifest = helper_pack.manifest() or {}
        pack["created"] = manifest.get("created")
        if not pack.get("note"):
            pack["note"] = manifest.get("note")
        return {"helpers": helpers, "pack": pack}

    @app.get("/v1/queue")
    async def queue_api(_auth: None = Depends(auth.require_token)):
        """The Queue page's data: running work, what waits and why, the GPU helpers, and recent failures."""
        return await run_in_threadpool(_queue_view)

    def _queue_view() -> dict:
        helpers = _gpu_helpers_view()["helpers"]
        return queue_view.build_queue(store, settings=settings_mod.load_settings(store.root), helpers=helpers)

    @app.post("/settings", response_class=HTMLResponse)
    async def settings_submit(request: Request, _auth: None = Depends(auth.require_web_token)):
        form = await request.form()
        fields = dict(form)
        stored = settings_mod.load_settings(store.root)
        # The theme control saves itself instantly (PUT /v1/appearance, which does not bump the revision). The form
        # only writes the theme when it was changed on this very page (it differs from what the page was rendered
        # with); otherwise a page opened before a theme change elsewhere would put the old theme back.
        posted, rendered = fields.get("appearance"), fields.pop("appearance_rendered", None)
        if not posted or (rendered is not None and posted == rendered):
            fields["appearance"] = stored.appearance
        if settings_mod.stale_revision(fields.get("revision"), stored.revision):
            return web.render_settings_page(
                stored, token_configured=auth.token_is_configured(),
                error="Settings were changed somewhere else; nothing was saved. Reload to see them.",
            )
        # Anything the page did not post keeps its stored value (a stale page or a script must not wipe it): see
        # settings_mod.KEEP_IF_MISSING. Diarization itself and the other checkboxes post an explicit 0/on, so a
        # missing one is "not on this form", never "off"; the "Remote speaker labels" section is no longer rendered.
        fields = settings_mod.keep_stored(fields, stored, form=True)
        try:
            new_settings = settings_mod.validate(fields)
        except settings_mod.ValidationError as exc:
            current = settings_mod.load_settings(store.root)
            return web.render_settings_page(
                current, token_configured=auth.token_is_configured(), error=str(exc)
            )
        new_settings = settings_mod.save_settings(store.root, new_settings)
        _queue_type_renames(stored, new_settings)
        retention_worker.wake()
        voice_recheck.wake()
        # See LivePreview.reset_transcriber's docstring: without this, a
        # model/beam_size change here would silently not apply to live
        # preview until the process restarted, even though JobQueue picks it
        # up on the very next job for free.
        live_preview.reset_transcriber()
        return web.render_settings_page(
            new_settings, token_configured=auth.token_is_configured(), message="Settings saved."
        )


def install_settings_json(
    app: FastAPI,
    *,
    update_checker,
    store,
    live_preview,
    retention_worker,
    voice_recheck,
    notion,
) -> None:
    """Settings JSON API, voice recheck, update status and appearance."""

    _queue_type_renames = queue_type_renamer(notion)
    # -- JSON API: settings -------------------------------------------------

    @app.get("/v1/settings")
    async def get_settings_api(_auth: None = Depends(auth.require_token)):
        return settings_mod.load_settings(store.root).to_dict()

    @app.put("/v1/settings")
    async def put_settings_api(payload: dict, _auth: None = Depends(auth.require_token)):
        try:
            stored = settings_mod.load_settings(store.root)
            if settings_mod.stale_revision(payload.get("revision"), stored.revision):
                raise HTTPException(
                    status_code=409,
                    detail="Settings were changed somewhere else; nothing was saved. Reload to see them.",
                )
            # Older callers must not reset what they do not send (diarization, GPU helper, note types ...).
            payload = settings_mod.keep_stored(payload, stored, form=False)
            new_settings = settings_mod.validate(payload)
        except settings_mod.ValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        new_settings = settings_mod.save_settings(store.root, new_settings)
        _queue_type_renames(stored, new_settings)
        retention_worker.wake()
        voice_recheck.wake()
        live_preview.reset_transcriber()
        return new_settings.to_dict()

    @app.get("/v1/voice-recheck")
    async def voice_recheck_status_api(_auth: None = Depends(auth.require_token)):
        """The nightly voice recheck: schedule, next run and the last run's result."""
        return voice_recheck_mod.status(store.root, settings_mod.load_settings(store.root))

    @app.post("/v1/voice-recheck")
    async def voice_recheck_run_api(request: Request, _auth: None = Depends(auth.require_token)):
        """Run the voice recheck now. Without a body: the same window as the nightly one (unprofiled voices of the
        last ``voice_recheck_days`` days). With ``{"session_ids": [...]}``: exactly those meetings, all of their
        voices (the bulk "Update voice profiles"). Answers with the run's result (see ``voice_recheck.run_recheck``)."""
        if not people_mod.is_enabled(store.root):
            raise HTTPException(status_code=409, detail="voice profiles are off; turn them on to recheck voices")
        ids = None
        raw = await request.body()
        if raw.strip():
            try:
                body = json.loads(raw)
            except (ValueError, json.JSONDecodeError) as exc:
                raise HTTPException(status_code=400, detail="invalid JSON body") from exc
            if not isinstance(body, dict):
                raise HTTPException(status_code=400, detail="body must be a JSON object")
            ids = body.get("session_ids")
            if ids is not None:
                if (not isinstance(ids, list) or len(ids) > voice_recheck_mod.MAX_SELECTED
                        or not all(isinstance(i, str) and store_mod.is_safe_id(i) for i in ids)):
                    raise HTTPException(status_code=400, detail="session_ids must be a list of meeting ids")
        return await run_in_threadpool(voice_recheck.run_now, ids)

    @app.get("/v1/update-status")
    async def update_status_api(_auth: None = Depends(auth.require_token)):
        """Cached result of the GitHub version check; never fetches on request."""
        return update_checker.status()

    @app.put("/v1/appearance")
    async def put_appearance_api(payload: dict, _auth: None = Depends(auth.require_token)):
        """The web UI's theme switch: changes only ``appearance``, nothing else."""
        value = str(payload.get("appearance") or "").strip().lower()
        if value not in settings_mod.APPEARANCE_CHOICES:
            raise HTTPException(status_code=400, detail="appearance must be system, light, or dark")
        settings_mod.save_settings(
            store.root, dataclasses.replace(settings_mod.load_settings(store.root), appearance=value), bump=False
        )
        return {"appearance": value}
