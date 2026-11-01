"""The web UI's HTML pages, installed by ``create_app``."""

from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from .. import wire
from . import auth, mixdown as mixdown_mod, people as people_mod, settings as settings_mod, store as store_mod, web
from .route_helpers import _safe_session_id_or_400, ai_enabled_reader, appearance_reader


def install_pages(app: FastAPI, *, store) -> None:
    """The web UI's HTML pages (meetings, trash, recorders, queue, people, meeting detail) and the meeting audio files."""

    _appearance = appearance_reader(store)
    _ai_enabled = ai_enabled_reader(store)
    def _speaker_page_flags() -> dict:
        """What the Meetings page shows for speaker labels: "Find speakers again" with speaker labels on, "Update
        voice profiles" with voice profiles on too."""
        current = settings_mod.load_settings(store.root)
        return {"diarization_enabled": bool(current.diarization_enabled),
                "voice_profiles_enabled": people_mod.enabled(current)}

    # ``/meetings`` is the canonical URL; ``/transcriptions`` is kept as an
    # alias so existing bookmarks keep working.
    @app.get("/meetings", response_class=HTMLResponse)
    @app.get("/transcriptions", response_class=HTMLResponse)
    async def transcriptions_page(_auth: None = Depends(auth.require_web_token)):
        return web.render_transcriptions_page(
            token_configured=auth.token_is_configured(),
            ai_enabled=_ai_enabled(),
            appearance=_appearance(),
            **_speaker_page_flags(),
        )

    @app.get("/meetings/trash", response_class=HTMLResponse)
    async def trash_page(_auth: None = Depends(auth.require_web_token)):
        return web.render_trash_page(
            token_configured=auth.token_is_configured(), appearance=_appearance()
        )

    @app.get("/recorders", response_class=HTMLResponse)
    async def recorders_page(_auth: None = Depends(auth.require_web_token)):
        return web.render_recorders_page(
            token_configured=auth.token_is_configured(), appearance=_appearance()
        )

    @app.get("/queue", response_class=HTMLResponse)
    async def queue_page(_auth: None = Depends(auth.require_web_token)):
        return web.render_queue_page(
            settings_mod.load_settings(store.root), token_configured=auth.token_is_configured())

    @app.get("/people", response_class=HTMLResponse)
    async def people_page(_auth: None = Depends(auth.require_web_token)):
        if not people_mod.is_enabled(store.root):
            raise HTTPException(status_code=404, detail="Not Found")
        return web.render_people_page(token_configured=auth.token_is_configured(), appearance=_appearance())

    @app.get("/people/{person_id}", response_class=HTMLResponse)
    async def person_page(person_id: str, _auth: None = Depends(auth.require_web_token)):
        if not people_mod.is_enabled(store.root) or not store_mod.is_safe_id(person_id):
            raise HTTPException(status_code=404, detail="Not Found")
        return web.render_person_page(
            person_id, token_configured=auth.token_is_configured(), appearance=_appearance()
        )

    @app.get("/meeting-notes")
    async def meeting_notes_page(request: Request, _auth: None = Depends(auth.require_web_token)):
        """The old Meeting notes page lives on as the Meetings page; bookmarks keep working (query string kept)."""
        query = request.url.query
        return RedirectResponse(url="/meetings" + (f"?{query}" if query else ""), status_code=303)

    @app.get("/sessions/{session_id}", response_class=HTMLResponse)
    async def session_detail_page(session_id: str, _auth: None = Depends(auth.require_web_token)):
        _safe_session_id_or_400(session_id)
        if store.is_trashed(session_id):
            return RedirectResponse(url="/meetings/trash", status_code=303)
        # Meetings with finished notes open on the notes; anything else opens
        # on the transcript, as before.
        row = store.session_index_row(session_id) or {}
        initial_view = "notes" if (row.get("review") or {}).get("status") == "done" else None
        return web.render_transcriptions_page(
            token_configured=auth.token_is_configured(),
            initial_session_id=session_id,
            initial_view=initial_view,
            ai_enabled=_ai_enabled(),
            appearance=_appearance(),
            page_title=str(row.get("name") or "") or "Meeting",
            **_speaker_page_flags(),
        )

    @app.get("/sessions/{session_id}/audio/mixed")
    async def session_audio_mixed(session_id: str, _auth: None = Depends(auth.require_web_token)):
        """Both sides in one WAV (mic + system summed; see ``mixdown``).

        Registered before ``/audio/{track}`` so "mixed" is never read as a track
        name. With a single track on hand that track is served as is; with none
        (audio deleted, transcript-only meeting) it is a 404, which the page
        never asks for because it only shows the player when a track exists.
        """
        if not store_mod.is_safe_id(session_id):
            raise HTTPException(status_code=400, detail="invalid session")
        present = [
            path for path in (store.track_wav_path(session_id, t) for t in wire.TRACKS) if path.exists()
        ]
        cache = store.mixed_cache_path(session_id)
        if not present:
            raise HTTPException(status_code=404, detail="audio is not available")
        if len(present) == 1:
            try:
                cache.unlink()
            except OSError:
                pass
            return FileResponse(present[0], media_type="audio/wav", filename=present[0].name)
        try:
            path = await run_in_threadpool(mixdown_mod.ensure_mixed, present, cache)
        except mixdown_mod.MixError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return FileResponse(path, media_type="audio/wav", filename="both-sides.wav")

    @app.get("/sessions/{session_id}/audio/{track}")
    async def session_audio(
        session_id: str, track: str, _auth: None = Depends(auth.require_web_token)
    ):
        if not store_mod.is_safe_id(session_id) or track not in wire.TRACKS:
            raise HTTPException(status_code=400, detail="invalid session or track")
        path = store.track_wav_path(session_id, track)
        if not path.exists():
            raise HTTPException(status_code=404, detail="audio is not available")
        return FileResponse(path, media_type="audio/wav", filename=path.name)
