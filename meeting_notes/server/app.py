"""The FastAPI application: the network-facing half of the server.

``create_app`` is a factory rather than a module-level ``app`` object because
tests need an isolated store (temp directory) and an injectable stub
transcriber (no real Whisper model can run in most test environments, this
one included -- no network to fetch weights). ``app.py:app`` at the bottom is
the module-level instance uvicorn actually serves in Docker, wired from
environment variables.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from typing import Optional

import httpx
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Form, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.websockets import WebSocketDisconnect

from .. import __version__, review_contract, wire
from ..wav_io import wrap_raw_as_wav
from . import auth
from . import live as live_mod
from . import retention as retention_mod
from . import settings as settings_mod
from . import store as store_mod
from . import web
from .jobs import DiarizerFactory, JobQueue, TranscriberFactory

logger = logging.getLogger("meeting_notes.server.app")

# The server never has to guess a track's rate: every byte on the wire is
# defined (wire.py's own docstring) to be 16 kHz mono int16, whether it
# arrived over the live websocket or as a whole-track HTTP upload.
_UPLOAD_CHUNK = 1 << 16  # 64 KiB -- streamed, so a multi-hour upload is never
# held in memory all at once.

# Upper bound on how long a client might go without an ack; we actually ack
# any time the contiguous frame count moves at all (see the websocket route),
# which in practice is far more often than this -- it exists here only as the
# number the module docstring promises, not as a timer we wait for.
ACK_MAX_INTERVAL_SECONDS = 2.0


def _settings_transcriber_factory(store: store_mod.Store) -> TranscriberFactory:
    """Build the production transcriber factory: reads model/beam_size from
    the persisted settings (settings.py) at CALL time, not at server-start
    time.

    ``settings.py``'s own default already falls back to ``MEETING_NOTES_MODEL``
    when no settings.json exists yet, so this single factory covers both "an
    operator set the env var and never touched the settings page" and "an
    operator changed the model through the settings page" -- see
    ``create_app`` for the one case this does NOT cover (no model configured
    anywhere), which gets no factory at all rather than one that would fail.

    Reading settings at call time (not once, up front) is what lets a model
    or beam-size change made through the settings page or
    ``PUT /v1/settings`` take effect on the very next job or live-preview
    utterance -- no restart needed. (``LivePreview`` still caches the
    *instance* it builds across calls, which is why
    ``live_preview.reset_transcriber()`` also has to be called after a save;
    ``JobQueue`` never caches an instance, so jobs pick up a change for free.)
    """
    device = os.environ.get("MEETING_NOTES_DEVICE", "auto")

    def factory(**kwargs):
        # Imported lazily, at call time: importing meeting_notes.server.app
        # must not require faster-whisper to be installed at all, the same
        # seam meeting_notes.transcribe.protocol uses.
        from ..transcribe.protocol import get_transcriber

        current = settings_mod.load_settings(store.root)
        opts = {"model_size": current.model, "device": device, "beam_size": current.beam_size}
        opts.update(kwargs)
        return get_transcriber("faster-whisper", **opts)

    return factory


def _settings_diarizer_factory(store: store_mod.Store) -> DiarizerFactory:
    """Lazy, cached production diarizer controlled by persisted settings."""
    cached = {"key": None, "instance": None}

    def factory():
        current = settings_mod.load_settings(store.root)
        if not current.diarization_enabled:
            return None
        device = os.environ.get("MEETING_NOTES_DIARIZATION_DEVICE", "cpu")
        token = os.environ.get("HUGGINGFACE_TOKEN") or os.environ.get("HF_TOKEN")
        key = (
            current.diarization_model,
            device,
            current.diarization_min_speakers,
            current.diarization_max_speakers,
        )
        if cached["key"] != key:
            from ..transcribe.pyannote_backend import PyannoteDiarizer

            cached["instance"] = PyannoteDiarizer(
                model=current.diarization_model,
                token=token,
                device=device,
                min_speakers=current.diarization_min_speakers,
                max_speakers=current.diarization_max_speakers,
            )
            cached["key"] = key
        return cached["instance"]

    return factory


def create_app(
    transcriber_factory: Optional[TranscriberFactory] = None,
    diarizer_factory: Optional[DiarizerFactory] = None,
    data_root: Optional[str] = None,
) -> FastAPI:
    store = store_mod.Store(data_root)
    explicit_factory = transcriber_factory is not None
    if not explicit_factory:
        # A model configured only via settings.json (no MEETING_NOTES_MODEL,
        # saved later through the settings page) is treated the same as one
        # configured via the env var: settings.py's default already reads the
        # env var, so "no model anywhere" and "settings.model is empty" are
        # the same condition. See settings.py's _default_model for why this
        # deliberately does NOT fall back to a curated default model on its
        # own -- that would silently turn transcription on for an operator
        # who never asked for it.
        bootstrap_settings = settings_mod.load_settings(store.root)
        transcriber_factory = (
            _settings_transcriber_factory(store) if bootstrap_settings.model else None
        )

    # uvicorn configures only its own loggers; ours propagate to a root
    # logger with no handler, so Python's last-resort handler drops anything
    # below WARNING. That hid every "retention: deleted audio for ..." line
    # from `docker compose logs` on a real run. Wire the package's logger to
    # stderr at INFO once, and only if nobody (a test, an embedding app) has
    # already configured it.
    package_logger = logging.getLogger("meeting_notes")
    if not package_logger.handlers and not logging.getLogger().handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s:     %(name)s: %(message)s"))
        package_logger.addHandler(handler)
        package_logger.setLevel(logging.INFO)

    live_preview = live_mod.LivePreview(transcriber_factory)
    live_sessions: dict = {}
    live_sessions_lock = threading.Lock()
    # A live websocket can close just before the recorder's queued finalize
    # request arrives. Keep a rename made from the web UI long enough for that
    # finalize request to use it as the authoritative meeting name.
    live_name_overrides: dict = {}
    if diarizer_factory is None:
        diarizer_factory = _settings_diarizer_factory(store)
    job_queue = JobQueue(store, transcriber_factory, diarizer_factory)
    job_queue.start()
    retention_worker = retention_mod.RetentionWorker(store)
    retention_worker.start()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            # Runs on a clean shutdown. Both worker threads are also daemon
            # threads, so an unclean process exit doesn't hang either way --
            # this just lets an in-flight job (or sweep) finish (up to the
            # timeout) instead of being cut off mid-write.
            job_queue.stop()
            retention_worker.stop()

    app = FastAPI(title="meeting-notes server", lifespan=lifespan)
    # Exposed for tests and for anything that wants to reach past the routes
    # (e.g. to inspect job_queue directly) without a second construction path.
    app.state.store = store
    app.state.live_preview = live_preview
    app.state.job_queue = job_queue
    app.state.retention_worker = retention_worker
    app.state.transcriber_factory = transcriber_factory
    app.state.live_sessions = live_sessions

    async def bridge_control_request(method: str, path: str, payload: Optional[dict] = None):
        """Proxy bridge login controls without exposing its port to the LAN."""
        base_url = os.environ.get(
            "MEETING_NOTES_BRIDGE_CONTROL_URL", "http://meeting-notes-bridge:8765"
        ).rstrip("/")
        token = os.environ.get("MEETING_NOTES_TOKEN") or ""
        if not token:
            raise HTTPException(status_code=503, detail="bridge control requires a server token")
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.request(
                    method,
                    base_url + path,
                    json=payload,
                    headers={"Authorization": f"Bearer {token}"},
                )
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=503, detail="AI bridge is unavailable") from exc
        try:
            body = response.json()
        except ValueError:
            body = {"detail": "AI bridge returned an invalid response"}
        if response.status_code >= 400:
            raise HTTPException(
                status_code=502 if response.status_code >= 500 else response.status_code,
                detail=str(body.get("detail") or "AI bridge request failed"),
            )
        return body

    @app.exception_handler(auth.WebAuthRequired)
    async def _web_auth_required(_request: Request, _exc: auth.WebAuthRequired):
        # An HTML route with no valid cookie/header lands on a sign-in form,
        # not the bare JSON 401 an API caller would get from require_token.
        return RedirectResponse(url="/login", status_code=303)

    # -- health: no auth, so a client can probe reachability first ----------

    @app.get(wire.HEALTH)
    async def health():
        current = settings_mod.load_settings(store.root)
        return {
            "status": "ok",
            "protocol": wire.PROTOCOL_VERSION,
            "model": current.model or "none",
            "device": os.environ.get("MEETING_NOTES_DEVICE") or "cpu",
            "live_enabled": live_preview.enabled,
            "diarization_enabled": current.diarization_enabled,
        }

    # -- live stream ----------------------------------------------------

    @app.websocket(wire.STREAM)
    async def stream(websocket: WebSocket):
        await websocket.accept()

        query_token = websocket.query_params.get("token")
        auth_header = websocket.headers.get("authorization")
        if not auth.authorize_websocket(query_token, auth_header):
            await websocket.close(code=4401, reason="unauthorized")
            return

        try:
            raw_hello = await websocket.receive_json()
        except (WebSocketDisconnect, json.JSONDecodeError, KeyError, RuntimeError):
            await websocket.close(code=4400, reason="expected a JSON hello as the first message")
            return

        if not isinstance(raw_hello, dict) or raw_hello.get("type") != "hello":
            await websocket.close(code=4400, reason="expected a hello message")
            return

        session_id = raw_hello.get("session_id")
        client_protocol = raw_hello.get("protocol", wire.PROTOCOL_VERSION)
        tracks = raw_hello.get("tracks") or list(wire.TRACKS)

        if client_protocol != wire.PROTOCOL_VERSION:
            reason = (
                f"protocol version mismatch: client={client_protocol} "
                f"server={wire.PROTOCOL_VERSION}"
            )
            await websocket.send_json(wire.to_json(wire.ServerError(detail=reason)))
            await websocket.close(code=4400, reason=reason)
            return

        if not session_id or not store_mod.is_safe_id(str(session_id)):
            reason = f"invalid session_id: {session_id!r}"
            await websocket.send_json(wire.to_json(wire.ServerError(detail=reason)))
            await websocket.close(code=4400, reason=reason)
            return
        session_id = str(session_id)

        unknown = [t for t in tracks if t not in wire.TRACKS]
        if unknown:
            reason = f"unknown track(s): {unknown}"
            await websocket.send_json(wire.to_json(wire.ServerError(detail=reason)))
            await websocket.close(code=4400, reason=reason)
            return

        with live_sessions_lock:
            duplicate_active_session = session_id in live_sessions
            if not duplicate_active_session:
                live_sessions[session_id] = {
                    "session_id": session_id,
                    "name": str(raw_hello.get("name") or session_id),
                    "device": str(
                        raw_hello.get("device")
                        or (websocket.client.host if websocket.client else "Unknown device")
                    ),
                    "started_wall": float(raw_hello.get("started_wall") or time.time()),
                    "tracks": list(tracks),
                    "partials": [],
                }

        if duplicate_active_session:
            reason = f"session_id is already streaming: {session_id!r}"
            await websocket.send_json(wire.to_json(wire.ServerError(detail=reason)))
            await websocket.close(code=4409, reason=reason)
            return

        store.ensure_session_dir(session_id)

        # Highest frame count we've already acked per track -- an ack is only
        # worth sending again once this number can actually move, so a
        # duplicate/no-op write (see store.append_pcm) never produces a
        # redundant ack. Sending on every real advance is, in practice, far
        # more frequent than the "~2s" upper bound the wire contract asks
        # for, which is exactly what lets a reconnecting client trust the
        # last ack it saw.
        acked_frames: dict = {}
        # The live previewer's position per track: the absolute frame index
        # up to which it has been fed. Frames are handed over as they arrive,
        # NOT as the contiguous prefix advances -- seen on a real run: a
        # reconnect after an outage longer than the client's resend buffer
        # left a hole in the stream, the contiguous prefix stopped at the
        # hole, and the preview was dead for the rest of the meeting even
        # though audio was flowing again. A hole makes the previewer skip
        # ahead instead (the final upload fills it). It starts at whatever
        # the store already holds contiguously, so a reconnect does not
        # re-preview audio the previous connection already showed.
        fed_frames: dict = {}

        try:
            while True:
                try:
                    message = await websocket.receive()
                except WebSocketDisconnect:
                    break

                if message.get("type") == "websocket.disconnect":
                    break

                # Everything below this point can send a reply, and every one
                # of those sends can race a client that drops the connection
                # right after the inbound frame that triggered it. Only
                # `receive()` above was ever guarded before; a send hitting a
                # closed socket raised WebSocketDisconnect (or Starlette's own
                # RuntimeError, 'Cannot call "send" once a close message has
                # been sent') straight out of the handler -- a stack trace on
                # every ordinary wifi drop. Wrapping the rest of the loop body
                # here means a drop just ends this connection's loop cleanly;
                # `forget_session` in the `finally` below still always runs.
                try:
                    payload = message.get("bytes")
                    if payload is None:
                        text = message.get("text")
                        if text is not None:
                            # A control/JSON message where a binary audio
                            # frame was expected. Not fatal -- report and keep
                            # going.
                            await websocket.send_json(
                                wire.to_json(wire.ServerError(detail="expected a binary audio frame"))
                            )
                        continue

                    try:
                        track, offset, pcm = wire.decode_audio_frame(payload)
                    except wire.ProtocolError as exc:
                        # A malformed frame must never kill the connection --
                        # the rest of the stream (and the other track) is
                        # still good.
                        await websocket.send_json(wire.to_json(wire.ServerError(detail=str(exc))))
                        continue

                    if track not in fed_frames:
                        fed_frames[track] = store.contiguous_frames(session_id, track)

                    try:
                        # Left inline, unlike live_preview.poll() below: this
                        # is a small, bounded append to a file already open
                        # for writing, not a multi-second model load/decode --
                        # not worth a threadpool hop.
                        contiguous = store.append_pcm(session_id, track, offset, pcm)
                    except Exception as exc:  # noqa: BLE001 - see comment above
                        await websocket.send_json(wire.to_json(wire.ServerError(detail=str(exc))))
                        continue

                    position = fed_frames[track]
                    end = offset + len(pcm) // wire.BYTES_PER_FRAME
                    if end > position:
                        if offset > position:
                            live_preview.skip_to(session_id, track, offset)
                            new_pcm = pcm
                        else:
                            # A resend overlapping what we already fed: only
                            # the genuinely new tail goes to the previewer.
                            new_pcm = pcm[(position - offset) * wire.BYTES_PER_FRAME :]
                        live_preview.feed(session_id, track, new_pcm)
                        fed_frames[track] = end

                    if contiguous > acked_frames.get(track, 0):
                        acked_frames[track] = contiguous
                        await websocket.send_json(wire.to_json(wire.Ack(track=track, frames=contiguous)))

                    # live_preview.poll() lazily loads the Whisper model (can
                    # take seconds) and, once loaded, runs Silero VAD plus a
                    # full faster-whisper decode -- all synchronous CPU work.
                    # Calling it inline on this coroutine would freeze the
                    # entire single-worker event loop for the duration: every
                    # other session's acks and uploads, /health, job polling,
                    # everything. run_in_threadpool moves that work off the
                    # loop onto a worker thread; LivePreview's own lock (see
                    # live.py) keeps two sessions' passes from driving the
                    # model at once. This session's own handling still waits
                    # for its result -- the simplest correct design -- but the
                    # loop stays free for everyone else meanwhile.
                    partials = await run_in_threadpool(live_preview.poll, session_id, track)
                    for partial in partials:
                        with live_sessions_lock:
                            live = live_sessions.get(session_id)
                            if live is not None:
                                live["partials"].append(wire.to_json(partial))
                                live["partials"] = live["partials"][-200:]
                        await websocket.send_json(wire.to_json(partial))
                except WebSocketDisconnect:
                    break
                except RuntimeError as exc:
                    if "close message has been sent" not in str(exc):
                        raise
                    break
        finally:
            live_preview.forget_session(session_id)
            with live_sessions_lock:
                live_sessions.pop(session_id, None)

    # -- HTTP: track upload -----------------------------------------------

    @app.post(wire.track_upload_path("{session_id}", "{track}"))
    async def upload_track(
        session_id: str,
        track: str,
        request: Request,
        _auth: None = Depends(auth.require_token),
    ):
        if not store_mod.is_safe_id(session_id):
            raise HTTPException(status_code=400, detail=f"invalid session_id: {session_id!r}")
        if track not in wire.TRACKS:
            raise HTTPException(status_code=400, detail=f"unknown track: {track!r}")

        raw_path = store.track_raw_path(session_id, track)
        raw_path.parent.mkdir(parents=True, exist_ok=True)

        bytes_written = 0
        # Stream straight to disk in chunks -- an hours-long recording can
        # easily be 100+ MB, and reading the whole body into memory first
        # would be wasteful at best and a crash at worst.
        with open(raw_path, "wb") as fh:
            async for chunk in request.stream():
                if not chunk:
                    continue
                fh.write(chunk)
                bytes_written += len(chunk)
        # Count frames ONCE from the total, not per chunk. Transport chunk
        # boundaries have no reason to land on a 2-byte sample boundary, so
        # dividing each chunk separately silently discards the odd trailing
        # byte of every chunk that splits mid-sample -- undercounting a real
        # multi-MB upload and failing the X-Frames cross-check below with a
        # 400, even though the bytes on disk were perfectly fine.
        frames_written = bytes_written // wire.BYTES_PER_FRAME

        expected = request.headers.get("x-frames")
        if expected is not None:
            try:
                expected_frames = int(expected)
            except ValueError:
                expected_frames = None
            if expected_frames is not None and expected_frames != frames_written:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"X-Frames said {expected_frames} but {frames_written} frames "
                        "were received"
                    ),
                )

        return {"track": track, "frames": frames_written}

    # -- HTTP: finalize -----------------------------------------------------

    @app.post(wire.finalize_path("{session_id}"))
    async def finalize(session_id: str, request: Request, _auth: None = Depends(auth.require_token)):
        if not store_mod.is_safe_id(session_id):
            raise HTTPException(status_code=400, detail=f"invalid session_id: {session_id!r}")

        # A malformed body here used to bubble up as an unguarded 500: bad
        # JSON raised straight out of request.json(), and a non-dict body or
        # a non-dict `timing`/`meta`/`settings` (e.g. a client bug sending a
        # list) raised AttributeError from the first `.items()`/`.get()`
        # below. Both are just a bad request, not a server error -- report
        # them as 400s with a reason instead.
        try:
            body = await request.json()
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail=f"invalid JSON body: {exc}") from exc

        if not isinstance(body, dict):
            raise HTTPException(
                status_code=400, detail=f"request body must be a JSON object, got {type(body).__name__}"
            )

        meta = body.get("meta") or {}
        timing = body.get("timing") or {}
        # Older clients sent an empty settings object. Keep that shape
        # compatible, but reject overrides: transcription configuration is
        # owned by the server Settings page, never by an individual client.
        client_settings = body.get("settings") or {}

        for field_name, value in (("meta", meta), ("timing", timing), ("settings", client_settings)):
            if not isinstance(value, dict):
                raise HTTPException(
                    status_code=400,
                    detail=f"{field_name!r} must be an object if present, got {type(value).__name__}",
                )
        if client_settings:
            raise HTTPException(
                status_code=400,
                detail="transcription settings are controlled by the server",
            )

        with live_sessions_lock:
            renamed_name = live_name_overrides.pop(session_id, None)
        if renamed_name:
            meta = dict(meta)
            meta["name"] = renamed_name

        store.write_session_meta(session_id, meta)

        for track, entries in timing.items():
            if track not in wire.TRACKS:
                continue
            timing_path = store.track_timing_path(session_id, track)
            timing_path.parent.mkdir(parents=True, exist_ok=True)
            with open(timing_path, "w", encoding="utf-8") as fh:
                for entry in entries:
                    fh.write(json.dumps(entry, separators=(",", ":")) + "\n")

        # Every uploaded track's raw PCM becomes a real WAV now, at the one
        # sample rate anything ever arrives on the wire at (wire.py's own
        # invariant) -- the job worker only ever reads WAVs, never raw PCM.
        session_dir = store.session_dir(session_id)
        for raw_path in sorted(session_dir.glob("*.raw")):
            track = raw_path.stem
            wav_path = store.track_wav_path(session_id, track)
            wrap_raw_as_wav(raw_path, wav_path, wire.STREAM_SAMPLE_RATE)

        job_id = job_queue.enqueue(session_id)
        return {"job_id": job_id}

    # -- HTTP: job status / transcript --------------------------------------

    @app.get(wire.job_path("{job_id}"))
    async def job_status(job_id: str, _auth: None = Depends(auth.require_token)):
        if not store_mod.is_safe_id(job_id):
            raise HTTPException(status_code=400, detail=f"invalid job_id: {job_id!r}")
        job = store.read_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown job")
        return {
            "state": job.get("state"),
            "progress": job.get("progress"),
            "error": job.get("error"),
            "session_id": job.get("session_id"),
        }

    @app.get(wire.job_transcript_path("{job_id}"))
    async def job_transcript(job_id: str, _auth: None = Depends(auth.require_token)):
        if not store_mod.is_safe_id(job_id):
            raise HTTPException(status_code=400, detail=f"invalid job_id: {job_id!r}")
        job = store.read_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown job")
        transcript = store.read_transcript(job_id)
        if transcript is None:
            raise HTTPException(status_code=404, detail=f"job {job_id} is not done yet")
        return transcript

    # -- web UI: auth ---------------------------------------------------

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        if not auth.token_is_configured():
            # Nothing to sign in for -- matches the API's own "no token
            # means open" behaviour.
            return RedirectResponse(url="/", status_code=303)
        return web.render_login_page(error=bool(request.query_params.get("error")))

    @app.post("/login")
    async def login_submit(token: str = Form(...)):
        if not auth.token_is_valid(token):
            return RedirectResponse(url="/login?error=1", status_code=303)
        response = RedirectResponse(url="/", status_code=303)
        # httponly: invisible to page JS (nothing it does needs to read the
        # token back). samesite=lax: not sent on a cross-site request, but
        # still attached to same-origin fetch() calls the web UI's own pages
        # make against /v1/... -- see auth.require_token's cookie fallback.
        response.set_cookie(auth.WEB_TOKEN_COOKIE, token, httponly=True, samesite="lax")
        return response

    @app.post("/logout")
    async def logout():
        response = RedirectResponse(
            url="/login" if auth.token_is_configured() else "/", status_code=303
        )
        response.delete_cookie(auth.WEB_TOKEN_COOKIE)
        return response

    # -- web UI: sessions -------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def home_page(_auth: None = Depends(auth.require_web_token)):
        return web.render_home_page(token_configured=auth.token_is_configured())

    @app.get("/transcriptions", response_class=HTMLResponse)
    async def transcriptions_page(_auth: None = Depends(auth.require_web_token)):
        return web.render_transcriptions_page(token_configured=auth.token_is_configured())

    @app.get("/meeting-notes", response_class=HTMLResponse)
    async def meeting_notes_page(_auth: None = Depends(auth.require_web_token)):
        return web.render_meeting_notes_page(token_configured=auth.token_is_configured())

    @app.get("/sessions/{session_id}", response_class=HTMLResponse)
    async def session_detail_page(session_id: str, _auth: None = Depends(auth.require_web_token)):
        if not store_mod.is_safe_id(session_id):
            raise HTTPException(status_code=400, detail=f"invalid session_id: {session_id!r}")
        return web.render_transcriptions_page(
            token_configured=auth.token_is_configured(), initial_session_id=session_id
        )

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

    @app.get("/install", response_class=HTMLResponse)
    async def client_install_guide(
        request: Request, _auth: None = Depends(auth.require_web_token)
    ):
        current = settings_mod.load_settings(store.root)
        address = current.server_address or str(request.base_url).rstrip("/")
        return web.render_install_page(address, token_configured=auth.token_is_configured())

    @app.get("/install/client-agent.ps1")
    async def client_installer(
        request: Request, _auth: None = Depends(auth.require_web_token)
    ):
        current = settings_mod.load_settings(store.root)
        address = current.server_address or str(request.base_url).rstrip("/")
        script = web.render_client_installer(address)
        return Response(
            script,
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="Install-MeetingNotes.ps1"'},
        )

    @app.get("/install/uninstall-client.ps1")
    async def client_uninstaller(_auth: None = Depends(auth.require_web_token)):
        return Response(
            web.render_client_uninstaller(),
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="Uninstall-MeetingNotes.ps1"'},
        )

    @app.get("/install/client-manifest.json")
    async def client_manifest(request: Request):
        """Public metadata used before a recorder has a server token."""
        package = store.root / "client" / "MeetingNotes-Windows.zip"
        if not package.is_file():
            raise HTTPException(status_code=404, detail="Windows client package is not available")
        digest = hashlib.sha256()
        with package.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        address = str(request.base_url).rstrip("/")
        current = settings_mod.load_settings(store.root)
        installer_address = current.server_address or address
        installer = web.render_client_installer(installer_address).encode("utf-8")
        return {
            "url": address + "/install/MeetingNotes-Windows.zip",
            "sha256": digest.hexdigest(),
            "size": package.stat().st_size,
            "version": __version__,
            "installer": {
                "url": address + "/install/client-agent.ps1",
                "sha256": hashlib.sha256(installer).hexdigest(),
                "size": len(installer),
            },
        }

    @app.get("/install/MeetingNotes-Windows.zip")
    async def client_package():
        package = store.root / "client" / "MeetingNotes-Windows.zip"
        if not package.is_file():
            raise HTTPException(status_code=404, detail="Windows client package is not available")
        return FileResponse(package, media_type="application/zip", filename=package.name)

    @app.get("/sessions/{session_id}/transcript.md")
    async def download_transcript_markdown(
        session_id: str, _auth: None = Depends(auth.require_web_token)
    ):
        if not store_mod.is_safe_id(session_id):
            raise HTTPException(status_code=400, detail=f"invalid session_id: {session_id!r}")
        job = store.latest_done_job(session_id)
        transcript = store.read_transcript(job["job_id"]) if job else None
        if transcript is None:
            raise HTTPException(status_code=404, detail="no completed transcript for this session")
        return Response(
            content=transcript["markdown"],
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{session_id}.md"'},
        )

    @app.get("/sessions/{session_id}/transcript.json")
    async def download_transcript_json(
        session_id: str, _auth: None = Depends(auth.require_web_token)
    ):
        if not store_mod.is_safe_id(session_id):
            raise HTTPException(status_code=400, detail=f"invalid session_id: {session_id!r}")
        job = store.latest_done_job(session_id)
        transcript = store.read_transcript(job["job_id"]) if job else None
        if transcript is None:
            raise HTTPException(status_code=404, detail="no completed transcript for this session")
        return Response(
            content=transcript["json"],
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{session_id}.json"'},
        )

    @app.post("/sessions/{session_id}/delete-audio")
    async def delete_session_audio_route(
        session_id: str, _auth: None = Depends(auth.require_web_token)
    ):
        if not store_mod.is_safe_id(session_id):
            raise HTTPException(status_code=400, detail=f"invalid session_id: {session_id!r}")
        store.delete_session_audio(session_id)
        return RedirectResponse(url=f"/sessions/{session_id}", status_code=303)

    @app.post("/sessions/{session_id}/delete")
    async def delete_session_route(session_id: str, _auth: None = Depends(auth.require_web_token)):
        if not store_mod.is_safe_id(session_id):
            raise HTTPException(status_code=400, detail=f"invalid session_id: {session_id!r}")
        store.delete_session(session_id)
        return RedirectResponse(url="/", status_code=303)

    @app.post("/sessions/{session_id}/retranscribe")
    async def retranscribe_route(session_id: str, _auth: None = Depends(auth.require_web_token)):
        if not store_mod.is_safe_id(session_id):
            raise HTTPException(status_code=400, detail=f"invalid session_id: {session_id!r}")
        row = store.session_index_row(session_id)
        if row is None or not row.get("has_audio"):
            raise HTTPException(status_code=400, detail="no audio available to retranscribe")
        # Retranscription deliberately uses the server's current settings.
        job_queue.enqueue(session_id)
        return RedirectResponse(url=f"/sessions/{session_id}", status_code=303)

    # -- web UI: settings -------------------------------------------------

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(_auth: None = Depends(auth.require_web_token)):
        current = settings_mod.load_settings(store.root)
        return web.render_settings_page(current, token_configured=auth.token_is_configured())

    @app.post("/settings", response_class=HTMLResponse)
    async def settings_submit(request: Request, _auth: None = Depends(auth.require_web_token)):
        form = await request.form()
        try:
            new_settings = settings_mod.validate(dict(form))
        except settings_mod.ValidationError as exc:
            current = settings_mod.load_settings(store.root)
            return web.render_settings_page(
                current, token_configured=auth.token_is_configured(), error=str(exc)
            )
        settings_mod.save_settings(store.root, new_settings)
        retention_worker.wake()
        # See LivePreview.reset_transcriber's docstring: without this, a
        # model/beam_size change here would silently not apply to live
        # preview until the process restarted, even though JobQueue picks it
        # up on the very next job for free.
        live_preview.reset_transcriber()
        return web.render_settings_page(
            new_settings, token_configured=auth.token_is_configured(), message="Settings saved."
        )

    # -- JSON API: sessions -------------------------------------------------

    @app.get("/v1/sessions")
    async def list_sessions_api(
        q: Optional[str] = None,
        state: Optional[str] = None,
        page: int = 1,
        per_page: int = 50,
        _auth: None = Depends(auth.require_token),
    ):
        page = max(page, 1)
        per_page = max(1, min(per_page, 200))
        # Off the event loop: SQLite's C driver blocks the thread it runs on,
        # and a search against a very large transcript_text table (or the
        # LIKE fallback) is not guaranteed to be instant.
        return await run_in_threadpool(
            store.list_sessions, q=q, state=state, page=page, per_page=per_page
        )

    @app.get("/v1/live")
    async def live_sessions_api(_auth: None = Depends(auth.require_token)):
        with live_sessions_lock:
            items = [
                {**entry, "partials": list(entry.get("partials") or [])}
                for entry in live_sessions.values()
            ]
        return {"items": items, "total": len(items)}

    @app.patch("/v1/live/{session_id}")
    async def rename_live_session(
        session_id: str,
        request: Request,
        _auth: None = Depends(auth.require_token),
    ):
        """Rename an active live meeting; ended meetings are immutable here."""
        if not store_mod.is_safe_id(session_id):
            raise HTTPException(status_code=400, detail=f"invalid session_id: {session_id!r}")
        try:
            body = await request.json()
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="invalid JSON body") from exc
        if not isinstance(body, dict) or not isinstance(body.get("name"), str):
            raise HTTPException(status_code=400, detail="name must be a string")
        name = body["name"].strip()
        if not name:
            raise HTTPException(status_code=400, detail="name must not be empty")
        if len(name) > 200:
            raise HTTPException(status_code=400, detail="name must be 200 characters or fewer")
        with live_sessions_lock:
            live = live_sessions.get(session_id)
            if live is None:
                raise HTTPException(status_code=404, detail="live session is no longer active")
            live["name"] = name
            live_name_overrides[session_id] = name
        # Do not write the ordinary session metadata until finalize.  A live
        # stream already has an on-disk session directory for its audio; a
        # premature metadata write would index that incomplete directory and
        # expose a phantom saved-transcription row while the meeting is live.
        # ``live_name_overrides`` is applied to the real client metadata in
        # the finalize endpoint above.
        return {"session_id": session_id, "name": name}

    @app.get("/v1/sessions/{session_id}")
    async def session_detail_api(session_id: str, _auth: None = Depends(auth.require_token)):
        if not store_mod.is_safe_id(session_id):
            raise HTTPException(status_code=400, detail=f"invalid session_id: {session_id!r}")
        detail = await run_in_threadpool(store.session_detail, session_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="unknown session")
        return detail

    @app.post("/v1/sessions/{session_id}/delete-audio")
    async def delete_session_audio_api(
        session_id: str, _auth: None = Depends(auth.require_token)
    ):
        if not store_mod.is_safe_id(session_id):
            raise HTTPException(status_code=400, detail=f"invalid session_id: {session_id!r}")
        if store.session_index_row(session_id) is None:
            raise HTTPException(status_code=404, detail="unknown session")
        freed = await run_in_threadpool(store.delete_session_audio, session_id)
        return {"session_id": session_id, "bytes_freed": freed}

    @app.delete("/v1/sessions/{session_id}")
    async def delete_session_api(session_id: str, _auth: None = Depends(auth.require_token)):
        if not store_mod.is_safe_id(session_id):
            raise HTTPException(status_code=400, detail=f"invalid session_id: {session_id!r}")
        if store.session_index_row(session_id) is None:
            raise HTTPException(status_code=404, detail="unknown session")
        await run_in_threadpool(store.delete_session, session_id)
        return {"session_id": session_id, "deleted": True}

    @app.post("/v1/sessions/{session_id}/retranscribe")
    async def retranscribe_api(session_id: str, _auth: None = Depends(auth.require_token)):
        if not store_mod.is_safe_id(session_id):
            raise HTTPException(status_code=400, detail=f"invalid session_id: {session_id!r}")
        row = store.session_index_row(session_id)
        if row is None:
            raise HTTPException(status_code=404, detail="unknown session")
        if not row.get("has_audio"):
            raise HTTPException(status_code=400, detail="no audio available to retranscribe")
        job_id = job_queue.enqueue(session_id)
        return {"session_id": session_id, "job_id": job_id}

    # -- JSON API: explicit AI review queue -------------------------------

    def _review_id_or_400(review_id: str) -> None:
        if not store_mod.is_safe_id(review_id):
            raise HTTPException(status_code=400, detail=f"invalid review_id: {review_id!r}")

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
            "title": payload.get("title") or row.get("name") or session_id,
            "participants": payload.get("participants") or [],
            "summary": payload.get("summary"),
        }

    @app.post("/v1/sessions/{session_id}/review")
    async def queue_review_api(
        session_id: str,
        force: bool = False,
        _auth: None = Depends(auth.require_token),
    ):
        if not store_mod.is_safe_id(session_id):
            raise HTTPException(status_code=400, detail=f"invalid session_id: {session_id!r}")
        if not store.session_exists(session_id):
            raise HTTPException(status_code=404, detail="unknown session")
        try:
            return await run_in_threadpool(store.create_review, session_id, force)
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
        note = {
            **payload,
            "status": review.get("status"),
            "error": review.get("error"),
            "meta": meta,
            "transcript": segments,
        }
        return {
            "review_id": review.get("review_id"),
            "session_id": session_id,
            "transcript_job_id": review.get("transcript_job_id"),
            "status": review.get("status"),
            "error": review.get("error"),
            "created": review.get("created"),
            "updated": review.get("updated"),
            "completed_at": review.get("completed_at"),
            "notes": payload,
            "note": note,
            "transcript": segments,
        }

    @app.post("/v1/meeting-notes/{review_id}/retry")
    async def retry_meeting_note_api(
        review_id: str, _auth: None = Depends(auth.require_token)
    ):
        _review_or_404(review_id)
        try:
            return await run_in_threadpool(store.retry_review, review_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    # -- Codex bridge API --------------------------------------------------

    @app.get("/v1/bridge/workflow.md")
    async def bridge_workflow(_auth: None = Depends(auth.require_token)):
        return Response(
            review_contract.workflow_text(),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="meeting-notes-workflow.md"'},
        )

    @app.get("/v1/bridge/review/claim")
    async def claim_review_api(
        worker_id: Optional[str] = None,
        _auth: None = Depends(auth.require_token),
    ):
        ai_settings = settings_mod.load_settings(store.root)
        # Disabled is a real queue policy: leave reviews queued until an
        # operator selects a provider, rather than claiming work that cannot
        # be processed.
        if ai_settings.ai_provider == "disabled":
            return Response(status_code=204)
        review = await run_in_threadpool(store.claim_next_review)
        if review is None:
            return Response(status_code=204)
        review_id = str(review["review_id"])
        return {
            "id": review_id,
            "session_id": review.get("session_id"),
            "transcript_job_id": review.get("transcript_job_id"),
            "worker_id": worker_id,
            "transcript_url": f"/v1/bridge/review/{review_id}/transcript",
            "workflow_url": "/v1/bridge/workflow.md",
            "provider": {
                "name": ai_settings.ai_provider,
                "ollama_base_url": ai_settings.ollama_base_url,
                "ollama_model": ai_settings.ollama_model,
            },
        }

    @app.get("/v1/bridge/review/{review_id}/transcript")
    async def bridge_review_transcript(
        review_id: str, _auth: None = Depends(auth.require_token)
    ):
        review = _review_or_404(review_id)
        transcript = store.read_transcript(str(review.get("transcript_job_id") or ""))
        if not transcript:
            raise HTTPException(status_code=404, detail="review transcript is unavailable")
        return Response(transcript.get("markdown") or "", media_type="text/plain; charset=utf-8")

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
            return await run_in_threadpool(store.complete_review, review_id, notes)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

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
        try:
            return await run_in_threadpool(store.fail_review, review_id, error.strip()[:2000])
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    # The bridge control listener exists only on the private Compose network.
    # These authenticated proxy routes are the sole browser-facing path to it.
    @app.get("/v1/bridge/control/status")
    async def bridge_control_status_api(_auth: None = Depends(auth.require_token)):
        provider = settings_mod.load_settings(store.root).ai_provider
        if provider == "disabled":
            return {"provider": provider, "state": "disabled", "authenticated": False}
        if provider == "ollama":
            return {"provider": provider, "state": "configured", "authenticated": True}
        return await bridge_control_request("GET", "/v1/bridge/control/status")

    @app.post("/v1/bridge/control/login")
    async def bridge_control_login_api(_auth: None = Depends(auth.require_token)):
        provider = settings_mod.load_settings(store.root).ai_provider
        if provider != "codex":
            raise HTTPException(status_code=409, detail="Select Codex / ChatGPT before connecting")
        return await bridge_control_request(
            "POST", "/v1/bridge/control/login", {"provider": "codex"}
        )

    @app.post("/v1/bridge/control/logout")
    async def bridge_control_logout_api(_auth: None = Depends(auth.require_token)):
        return await bridge_control_request(
            "POST", "/v1/bridge/control/logout", {"provider": "codex"}
        )

    @app.post("/v1/reindex")
    async def reindex_api(_auth: None = Depends(auth.require_token)):
        count = await run_in_threadpool(store.reindex)
        return {"reindexed": count}

    # -- JSON API: settings -------------------------------------------------

    @app.get("/v1/settings")
    async def get_settings_api(_auth: None = Depends(auth.require_token)):
        return settings_mod.load_settings(store.root).to_dict()

    @app.put("/v1/settings")
    async def put_settings_api(payload: dict, _auth: None = Depends(auth.require_token)):
        try:
            new_settings = settings_mod.validate(payload)
        except settings_mod.ValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        settings_mod.save_settings(store.root, new_settings)
        retention_worker.wake()
        live_preview.reset_transcriber()
        return new_settings.to_dict()

    return app


# The module-level instance `uvicorn meeting_notes.server.app:app` serves in
# Docker. Configuration comes entirely from the environment (MEETING_NOTES_*)
# since there's no CLI to pass a factory through at that point.
app = create_app()
