"""The FastAPI application: the network-facing half of the server.

``create_app`` is a factory rather than a module-level ``app`` object because
tests need an isolated store (temp directory) and an injectable stub
transcriber (no real Whisper model can run in most test environments, this
one included -- no network to fetch weights). ``app.py:app`` at the bottom is
the module-level instance uvicorn actually serves in Docker, wired from
environment variables.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
from starlette.websockets import WebSocketDisconnect

from .. import wire
from ..wav_io import wrap_raw_as_wav
from . import auth
from . import live as live_mod
from . import store as store_mod
from .jobs import JobQueue, TranscriberFactory

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


def _env_transcriber_factory() -> Optional[TranscriberFactory]:
    """Build the production transcriber factory from the environment.

    Returns ``None`` (rather than a factory that would fail) when no model is
    configured, so an operator who hasn't set one up yet still gets a working
    recorder/upload/store server -- it just can't transcribe anything until
    MEETING_NOTES_MODEL is set. See live.py and jobs.py for how each half
    handles a ``None`` factory.
    """
    model = os.environ.get("MEETING_NOTES_MODEL")
    if not model:
        return None
    device = os.environ.get("MEETING_NOTES_DEVICE", "auto")

    def factory(**kwargs):
        # Imported lazily, at call time: importing meeting_notes.server.app
        # must not require faster-whisper to be installed at all, the same
        # seam meeting_notes.transcribe.protocol uses.
        from ..transcribe.protocol import get_transcriber

        opts = {"model_size": model, "device": device}
        opts.update(kwargs)
        return get_transcriber("faster-whisper", **opts)

    return factory


def create_app(
    transcriber_factory: Optional[TranscriberFactory] = None,
    data_root: Optional[str] = None,
) -> FastAPI:
    store = store_mod.Store(data_root)
    if transcriber_factory is None:
        transcriber_factory = _env_transcriber_factory()

    live_preview = live_mod.LivePreview(transcriber_factory)
    job_queue = JobQueue(store, transcriber_factory)
    job_queue.start()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            # Runs on a clean shutdown. The worker thread is also a daemon
            # thread, so an unclean process exit doesn't hang either way --
            # this just lets an in-flight job finish (up to the timeout)
            # instead of being cut off mid-write.
            job_queue.stop()

    app = FastAPI(title="meeting-notes server", lifespan=lifespan)
    # Exposed for tests and for anything that wants to reach past the routes
    # (e.g. to inspect job_queue directly) without a second construction path.
    app.state.store = store
    app.state.live_preview = live_preview
    app.state.job_queue = job_queue
    app.state.transcriber_factory = transcriber_factory

    # -- health: no auth, so a client can probe reachability first ----------

    @app.get(wire.HEALTH)
    async def health():
        return {
            "status": "ok",
            "protocol": wire.PROTOCOL_VERSION,
            "model": os.environ.get("MEETING_NOTES_MODEL") or "none",
            "device": os.environ.get("MEETING_NOTES_DEVICE") or "cpu",
            "live_enabled": live_preview.enabled,
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

        store.ensure_session_dir(session_id)

        # Highest frame count we've already acked per track -- an ack is only
        # worth sending again once this number can actually move, so a
        # duplicate/no-op write (see store.append_pcm) never produces a
        # redundant ack. Sending on every real advance is, in practice, far
        # more frequent than the "~2s" upper bound the wire contract asks
        # for, which is exactly what lets a reconnecting client trust the
        # last ack it saw.
        acked_frames: dict = {}
        # Highest contiguous frame count we've already handed to the live
        # previewer per track, so a re-ack of unchanged data (or the next
        # frame in an already-fed region) never re-feeds -- and never
        # re-transcribes -- audio the previewer has already buffered.
        fed_frames: dict = {}

        try:
            while True:
                try:
                    message = await websocket.receive()
                except WebSocketDisconnect:
                    break

                if message.get("type") == "websocket.disconnect":
                    break

                payload = message.get("bytes")
                if payload is None:
                    text = message.get("text")
                    if text is not None:
                        # A control/JSON message where a binary audio frame
                        # was expected. Not fatal -- report and keep going.
                        await websocket.send_json(
                            wire.to_json(wire.ServerError(detail="expected a binary audio frame"))
                        )
                    continue

                try:
                    track, offset, pcm = wire.decode_audio_frame(payload)
                except wire.ProtocolError as exc:
                    # A malformed frame must never kill the connection -- the
                    # rest of the stream (and the other track) is still good.
                    await websocket.send_json(wire.to_json(wire.ServerError(detail=str(exc))))
                    continue

                try:
                    contiguous = store.append_pcm(session_id, track, offset, pcm)
                except Exception as exc:  # noqa: BLE001 - see comment above
                    await websocket.send_json(wire.to_json(wire.ServerError(detail=str(exc))))
                    continue

                already_fed = fed_frames.get(track, 0)
                if contiguous > already_fed:
                    new_pcm = store.read_pcm_range(session_id, track, already_fed, contiguous)
                    live_preview.feed(session_id, track, new_pcm)
                    fed_frames[track] = contiguous

                if contiguous > acked_frames.get(track, 0):
                    acked_frames[track] = contiguous
                    await websocket.send_json(wire.to_json(wire.Ack(track=track, frames=contiguous)))

                for partial in live_preview.poll(session_id, track):
                    await websocket.send_json(wire.to_json(partial))
        finally:
            live_preview.forget_session(session_id)

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

        body = await request.json()
        meta = body.get("meta") or {}
        timing = body.get("timing") or {}
        settings = body.get("settings") or {}

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

        job_id = job_queue.enqueue(session_id, settings)
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

    return app


# The module-level instance `uvicorn meeting_notes.server.app:app` serves in
# Docker. Configuration comes entirely from the environment (MEETING_NOTES_*)
# since there's no CLI to pass a factory through at that point.
app = create_app()
