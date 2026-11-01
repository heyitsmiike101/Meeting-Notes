"""The recorder-facing wire protocol routes, installed by ``create_app``."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import time
import uuid
import wave
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
from starlette.concurrency import run_in_threadpool
from starlette.formparsers import MultiPartException
from starlette.websockets import WebSocketDisconnect

from .. import __version__, scratchpad as scratchpad_mod, wire
from ..wav_io import wrap_raw_as_wav
from . import ai_health, auth, compat, mobile_common, mobile_stream as mobile_stream_mod, settings as settings_mod, store as store_mod
from .transcript_api import install_transcript_upload
from .route_helpers import _RecordingMultipartParser, _UNSET, _client_info, _client_meta, _refuse_stale_new_upload, _safe_session_id_or_400, restore_trashed_for

# Same logger name as app.py, so log records and test captures are unchanged by the split.
logger = logging.getLogger("meeting_notes.server.app")


# The server never has to guess a track's rate: every byte on the wire is
# defined (wire.py's own docstring) to be 16 kHz mono int16, whether it
# arrived over the live websocket or as a whole-track HTTP upload.
_UPLOAD_CHUNK = 1 << 16  # 64 KiB -- streamed, so a multi-hour upload is never
# held in memory all at once.

_RECORDING_EXTENSIONS = frozenset({
    ".wav", ".mp3", ".m4a", ".mp4", ".aac", ".flac", ".ogg", ".oga", ".webm", ".opus",
})

# Upper bound on how long a client might go without an ack; we actually ack
# any time the contiguous frame count moves at all (see the websocket route),
# which in practice is far more often than this -- it exists here only as the
# number the module docstring promises, not as a timer we wait for.
ACK_MAX_INTERVAL_SECONDS = 2.0

def _is_canonical_wav(path) -> bool:
    """Check that an uploaded WAV is a safe input for the STT pipeline.

    We only bypass ffmpeg for the exact PCM shape used by the recorder. This
    prevents a file merely named ``.wav`` (or a compressed/WAVE variant) from
    being blindly renamed into the server's canonical track path.
    """
    try:
        with wave.open(str(path), "rb") as reader:
            return (
                reader.getnchannels() == 1
                and reader.getsampwidth() == 2
                and reader.getframerate() == wire.STREAM_SAMPLE_RATE
                and reader.getnframes() > 0
            )
    except (OSError, EOFError, wave.Error):
        return False


def install_recording(
    app: FastAPI,
    *,
    store,
    live_preview,
    live_sessions,
    live_sessions_lock,
    live_name_overrides,
    live_people_overrides,
    compute_registry,
    job_queue,
    mobile_stream,
    _max_recording_bytes,
) -> None:
    """The recorder's wire protocol: /health, the live websocket stream, whole-recording and track uploads, finalize, job status."""

    _restore_trashed = restore_trashed_for(store)
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
            "compute": {
                "offload_enabled": current.compute_offload_enabled,
                "helpers_online": compute_registry.online_count(),
            },
            "version": __version__,
            "min_client_version": compat.min_client_version(),
            # How a recorder may send a finished track: raw 16 kHz PCM (always) or Ogg Opus (decoded here).
            "track_formats": list(wire.TRACK_FORMATS),
            # Whether the AI provider's login still works (ai_health.py): notes wait while it is expired.
            "ai_login": {"provider": current.ai_provider,
                         "expired": ai_health.is_expired(store.root, current.ai_provider)},
        }

    # -- live stream ----------------------------------------------------

    @app.websocket(wire.STREAM)
    async def stream(websocket: WebSocket):
        # A phone page authenticates with a one-shot stream ticket sent as the subprotocol ``mn-ticket.<ticket>``
        # (and its exact Origin); every other client keeps the token/header path below, unchanged.
        ticket_protocol = mobile_stream_mod.ticket_from_subprotocols(websocket.scope.get("subprotocols"))
        ticket_record = None
        if ticket_protocol is not None:
            await websocket.accept(subprotocol=ticket_protocol)
            if mobile_stream.origin_ok(websocket):
                ticket_record = mobile_stream.tickets.consume(ticket_protocol[len(mobile_stream_mod.SUBPROTOCOL_PREFIX):])
            if ticket_record is None:
                await websocket.close(code=4401, reason=wire.close_reason("unauthorized"))
                return
        else:
            await websocket.accept()
            query_token = websocket.query_params.get("token")
            auth_header = websocket.headers.get("authorization")
            if not auth.authorize_websocket(query_token, auth_header):
                await websocket.close(code=4401, reason=wire.close_reason("unauthorized"))
                return

        try:
            raw_hello = await websocket.receive_json()
        except (WebSocketDisconnect, json.JSONDecodeError, KeyError, RuntimeError):
            await websocket.close(code=4400, reason=wire.close_reason("expected a JSON hello as the first message"))
            return

        if not isinstance(raw_hello, dict) or raw_hello.get("type") != "hello":
            await websocket.close(code=4400, reason=wire.close_reason("expected a hello message"))
            return

        session_id = raw_hello.get("session_id")
        client_protocol = raw_hello.get("protocol", wire.PROTOCOL_VERSION)
        tracks = raw_hello.get("tracks") or (["system"] if ticket_record else list(wire.TRACKS))

        if client_protocol != wire.PROTOCOL_VERSION:
            reason = (
                f"protocol version mismatch: client={client_protocol} "
                f"server={wire.PROTOCOL_VERSION}"
            )
            await websocket.send_json(wire.to_json(wire.ServerError(detail=reason)))
            await websocket.close(code=4400, reason=wire.close_reason(reason))
            return

        if not session_id or not store_mod.is_safe_id(str(session_id)):
            reason = f"invalid session_id: {session_id!r}"
            await websocket.send_json(wire.to_json(wire.ServerError(detail=reason)))
            await websocket.close(code=4400, reason=wire.close_reason(reason))
            return
        session_id = str(session_id)

        unknown = [t for t in tracks if t not in wire.TRACKS]
        if unknown:
            reason = f"unknown track(s): {unknown}"
            await websocket.send_json(wire.to_json(wire.ServerError(detail=reason)))
            await websocket.close(code=4400, reason=wire.close_reason(reason))
            return

        conn = None
        if ticket_record is not None:
            # The ticket binds this socket to one open mobile session and to the system track only.
            if session_id != ticket_record.session_id or any(t != "system" for t in tracks):
                await websocket.close(code=mobile_stream_mod.CLOSE_FORBIDDEN, reason=wire.close_reason("forbidden"))
                return
            if mobile_common.read_state(store, session_id) is None or not mobile_common.live_writes_allowed(store, session_id):
                await websocket.close(code=mobile_stream_mod.CLOSE_FINISHED, reason=wire.close_reason("upload finished"))
                return
            conn = mobile_stream_mod.Conn(
                session_id=session_id,
                generation=ticket_record.generation,
                websocket=websocket,
                loop=asyncio.get_running_loop(),
            )

        stale_info = _client_info(websocket)
        if stale_info is not None and stale_info.is_too_old():
            await _restore_trashed(session_id)
            if not store.session_exists(session_id):
                # Code 4400 is "permanent" for every released streamer, so an
                # old recorder stops retrying. The live preview is disposable
                # and its local recording is untouched.
                reason = compat.ClientTooOld(stale_info).payload()["detail"][:120]
                await websocket.send_json(wire.to_json(wire.ServerError(detail=reason)))
                await websocket.close(code=4400, reason=wire.close_reason(reason))
                return

        replaced_conn = None
        with live_sessions_lock:
            duplicate_active_session = session_id in live_sessions
            previous_partials: list = []
            if conn is not None and duplicate_active_session:
                # A newer ticket generation takes over a ticketed socket; a desktop stream is never displaced.
                try:
                    replaced_conn = mobile_stream.register(conn) if mobile_stream.is_held(session_id) else None
                except ValueError:
                    replaced_conn = None
                if replaced_conn is not None:
                    duplicate_active_session = False
                    previous_partials = list(live_sessions[session_id].get("partials") or [])
            elif conn is not None:
                try:
                    mobile_stream.register(conn)
                except ValueError:
                    duplicate_active_session = True
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
                    "partials": previous_partials,
                }

        if replaced_conn is not None:
            mobile_stream.close_conn(replaced_conn, mobile_stream_mod.CLOSE_REPLACED, "replaced by a newer connection")

        if duplicate_active_session:
            reason = f"session_id is already streaming: {session_id!r}"
            await websocket.send_json(wire.to_json(wire.ServerError(detail=reason)))
            await websocket.close(code=4409, reason=wire.close_reason(reason))
            return

        await _restore_trashed(session_id)
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
                if conn is not None and (conn.closed or not mobile_stream.is_current(conn)):
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

                    if conn is not None:
                        now = time.monotonic()
                        frame_count = len(pcm) // wire.BYTES_PER_FRAME
                        if track != "system":
                            await websocket.close(code=mobile_stream_mod.CLOSE_FORBIDDEN, reason=wire.close_reason("forbidden"))
                            break
                        if not mobile_stream.writes_allowed(conn, now):
                            conn.closed = True
                            await websocket.close(code=mobile_stream_mod.CLOSE_FINISHED, reason=wire.close_reason("upload finished"))
                            break
                        if offset < 0 or offset + frame_count > mobile_stream_mod.MAX_OFFSET_FRAMES or not conn.spend(frame_count, now):
                            conn.closed = True
                            await websocket.close(code=mobile_stream_mod.CLOSE_BAD_REQUEST, reason=wire.close_reason("stream limits exceeded"))
                            break

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
                                live["partials"] = live["partials"][-1000:]
                        await websocket.send_json(wire.to_json(partial))
                except WebSocketDisconnect:
                    break
                except RuntimeError as exc:
                    if "close message has been sent" not in str(exc):
                        raise
                    break
        finally:
            # A replaced ticketed socket must not tear down the session state its successor now owns.
            owns_session = conn is None or mobile_stream.is_current(conn)
            if conn is not None:
                mobile_stream.release(conn)
            if owns_session:
                live_preview.forget_session(session_id)
                with live_sessions_lock:
                    live_sessions.pop(session_id, None)

    # -- HTTP: complete-recording upload ---------------------------------

    @app.post(wire.upload_path())
    async def upload_recording(request: Request, _auth: None = Depends(auth.require_token)):
        """Accept one common meeting recording and queue server-side STT.

        The multipart parser spools large parts to disk; this handler then
        copies the file in bounded chunks to the media volume. A session
        metadata record is written before and during the copy, allowing Home
        to poll ``/v1/sessions`` and see pending/uploading percentages even
        while the request is still in flight.
        """
        _refuse_stale_new_upload(request)
        try:
            # Starlette's parser uses a 1 MiB SpooledTemporaryFile threshold,
            # then rolls the upload to disk; the handler itself reads that
            # UploadFile in 64 KiB chunks. Limit non-file multipart fields and
            # the number of parts to avoid an unbounded form attack.
            form = await _RecordingMultipartParser(
                request.headers,
                request.stream(),
                max_files=1,
                max_fields=2,
                max_part_size=64 * 1024,
                max_file_size=_max_recording_bytes(),
            ).parse()
        except MultiPartException as exc:
            status = 413 if "recording exceeds" in str(exc) else 400
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        except (AssertionError, RuntimeError) as exc:
            # ``python-multipart`` is part of the server extra. Keep a clear
            # operational error when a minimal development install omits it.
            raise HTTPException(status_code=415, detail="multipart upload support is not installed") from exc
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read") or not getattr(upload, "filename", None):
            raise HTTPException(status_code=400, detail="multipart field 'file' is required")

        filename = str(upload.filename)
        suffix = os.path.splitext(filename)[1].lower()
        if suffix not in _RECORDING_EXTENSIONS:
            allowed = ", ".join(sorted(_RECORDING_EXTENSIONS))
            raise HTTPException(status_code=415, detail=f"unsupported recording format; use: {allowed}")
        name_value = form.get("name")
        device_value = form.get("device")
        name = str(name_value or "").strip()[:200]
        device = str(device_value or "").strip()[:200]
        session_id = uuid.uuid4().hex
        session_dir = store.ensure_session_dir(session_id)
        media_dir = store.ensure_media_session_dir(session_id)
        total = int(getattr(upload, "size", 0) or 0)
        meta = {
            "name": name or os.path.splitext(os.path.basename(filename))[0] or session_id,
            "device": device,
            "source_format": suffix.lstrip("."),
            "created": time.time(),
            **_client_meta(request),
            "tracks": {"system": {"sample_rate": wire.STREAM_SAMPLE_RATE, "channels": 1, "source": "upload"}},
            "upload": {
                "state": "pending",
                "filename": os.path.basename(filename),
                "bytes_received": 0,
                "bytes_total": total,
                "percent": 0.0,
            },
        }
        store.write_session_meta(session_id, meta)

        def update_upload(**fields) -> dict:
            def mutate(current: dict) -> dict:
                upload_meta = current.get("upload")
                if not isinstance(upload_meta, dict):
                    upload_meta = {}
                upload_meta = dict(upload_meta)
                upload_meta.update(fields)
                current["upload"] = upload_meta
                return current
            return store.update_session_meta(session_id, mutate)

        tmp_path = media_dir / f".upload-{uuid.uuid4().hex}{suffix}"
        received = 0
        try:
            meta["upload"]["state"] = "uploading"
            update_upload(state="uploading")
            with open(tmp_path, "wb") as output:
                while True:
                    chunk = await upload.read(_UPLOAD_CHUNK)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > _max_recording_bytes():
                        raise ValueError(f"recording exceeds {_max_recording_bytes()} byte limit")
                    output.write(chunk)
                    # Persist at most once per MiB to keep SQLite/index churn
                    # low while still making progress visible to pollers.
                    if received == len(chunk) or received - int(meta["upload"]["bytes_received"]) >= (1 << 20):
                        meta["upload"].update(
                            bytes_received=received,
                            percent=round(received * 100.0 / total, 2) if total else 0.0,
                        )
                        update_upload(
                            bytes_received=meta["upload"]["bytes_received"],
                            percent=meta["upload"]["percent"],
                        )
            if not received:
                raise ValueError("recording file is empty")
            meta["upload"].update(bytes_received=received, bytes_total=received if not total else total, percent=100.0)
            input_path = media_dir / f"source{suffix}"
            os.replace(tmp_path, input_path)

            wav_path = store.track_wav_path(session_id, "system")
            if suffix == ".wav" and _is_canonical_wav(input_path):
                os.replace(input_path, wav_path)
            else:
                ffmpeg = shutil.which("ffmpeg")
                if not ffmpeg:
                    raise RuntimeError("ffmpeg is required for this recording format or WAV variant")
                proc = await run_in_threadpool(
                    subprocess.run,
                    [ffmpeg, "-nostdin", "-v", "error", "-i", str(input_path), "-ac", "1", "-ar", str(wire.STREAM_SAMPLE_RATE), "-c:a", "pcm_s16le", str(wav_path)],
                    capture_output=True,
                    timeout=600,
                    check=False,
                )
                try:
                    input_path.unlink()
                except OSError:
                    pass
                if proc.returncode != 0 or not wav_path.exists() or not _is_canonical_wav(wav_path):
                    error = (proc.stderr or b"").decode("utf-8", "replace")[-1000:]
                    raise ValueError(f"could not decode recording{(': ' + error) if error else ''}")

            meta["upload"].update(state="complete", bytes_received=received, percent=100.0)
            update_upload(
                state="complete",
                bytes_received=received,
                bytes_total=meta["upload"]["bytes_total"],
                percent=100.0,
            )
            job_id = job_queue.enqueue(session_id)
            job = store.read_job(job_id) or {}
            pipeline = store.session_detail(session_id)["pipeline"]
            return {
                "session_id": session_id,
                "job_id": job_id,
                "state": job.get("state", wire.JobState.QUEUED),
                "progress": job.get("progress", 0.0),
                "upload_percent": 100.0,
                "transcription_percent": round(float(job.get("progress", 0.0)) * 100.0, 2),
                "pipeline": pipeline,
            }
        except HTTPException:
            raise
        except Exception as exc:
            try:
                meta["upload"].update(state="error", bytes_received=received, percent=round(received * 100.0 / total, 2) if total else 0.0, error=str(exc))
                update_upload(
                    state="error",
                    bytes_received=received,
                    percent=meta["upload"]["percent"],
                    error=str(exc),
                )
            except Exception:
                logger.exception("failed to persist upload failure for %s", session_id)
            try:
                tmp_path.unlink()
            except OSError:
                pass
            status = 413 if "exceeds" in str(exc) else 422
            raise HTTPException(status_code=status, detail=str(exc)) from exc

    # -- HTTP: a transcript the person already has (no audio, nothing to transcribe) -----
    install_transcript_upload(
        app, store=store, job_queue=job_queue, client_meta=_client_meta, refuse_stale=_refuse_stale_new_upload
    )

    # -- HTTP: track upload -----------------------------------------------

    async def _receive_opus_track(session_id: str, track: str, request: Request, raw_path: Path) -> dict:
        """Take an Ogg Opus track body, decode it with ffmpeg to 16 kHz mono s16le, and store it as the track's
        ``.raw`` (temp file + atomic replace), after which it is indistinguishable from a PCM upload."""
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise HTTPException(status_code=415, detail="this server cannot decode Opus tracks (ffmpeg is missing)")
        token = uuid.uuid4().hex
        src_path = raw_path.with_name(f".{track}.{token}.opus.part")
        out_path = raw_path.with_name(f".{track}.{token}.raw.part")
        received = 0
        try:
            with open(src_path, "wb") as fh:
                async for chunk in request.stream():
                    if not chunk:
                        continue
                    received += len(chunk)
                    if received > _max_recording_bytes():
                        raise HTTPException(status_code=413, detail=f"track exceeds {_max_recording_bytes()} byte limit")
                    fh.write(chunk)
            if not received:
                raise HTTPException(status_code=422, detail="the Opus track body is empty")
            # Opus is ~100x smaller than real time; decoding runs far faster than that, so this is a generous cap.
            timeout = 120 + 60 * (received / (1 << 20))
            cmd = [
                ffmpeg, "-nostdin", "-v", "error", "-f", "ogg", "-i", str(src_path), "-vn", "-ac", "1",
                "-ar", str(wire.STREAM_SAMPLE_RATE), "-c:a", "pcm_s16le", "-f", "s16le", "-y", str(out_path),
            ]  # fmt: skip
            try:
                proc = await run_in_threadpool(
                    subprocess.run, cmd, capture_output=True, timeout=timeout, check=False
                )
            except subprocess.TimeoutExpired as exc:
                raise HTTPException(status_code=422, detail="decoding the Opus track timed out") from exc
            decoded = out_path.stat().st_size if out_path.exists() else 0
            if proc.returncode != 0 or decoded < wire.BYTES_PER_FRAME:
                tail = (proc.stderr or b"").decode("utf-8", "replace").strip()[-300:]
                raise HTTPException(
                    status_code=422, detail=f"could not decode the Opus track{(': ' + tail) if tail else ''}"
                )
            if decoded % wire.BYTES_PER_FRAME:
                with open(out_path, "rb+") as fh:
                    fh.truncate(decoded - decoded % wire.BYTES_PER_FRAME)
                decoded -= decoded % wire.BYTES_PER_FRAME
            os.replace(out_path, raw_path)
        finally:
            for leftover in (src_path, out_path):
                try:
                    leftover.unlink()
                except OSError:
                    pass
        return {"track": track, "frames": decoded // wire.BYTES_PER_FRAME, "format": "opus"}

    @app.post(wire.track_upload_path("{session_id}", "{track}"))
    async def upload_track(
        session_id: str,
        track: str,
        request: Request,
        _auth: None = Depends(auth.require_token),
    ):
        _safe_session_id_or_400(session_id)
        if track not in wire.TRACKS:
            raise HTTPException(status_code=400, detail=f"unknown track: {track!r}")

        await _restore_trashed(session_id)
        _refuse_stale_new_upload(request, store.session_exists(session_id))
        raw_path = store.track_raw_path(session_id, track)
        raw_path.parent.mkdir(parents=True, exist_ok=True)

        content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
        track_format = (request.headers.get("x-track-format") or "").strip().lower()
        if track_format not in ("", "pcm16", "opus"):
            raise HTTPException(status_code=415, detail=f"unsupported track format: {track_format!r}")
        if track_format == "opus" or content_type in ("audio/ogg", "audio/opus"):
            return await _receive_opus_track(session_id, track, request, raw_path)

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
        _safe_session_id_or_400(session_id)

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

        await _restore_trashed(session_id)
        with live_sessions_lock:
            renamed_name = live_name_overrides.pop(session_id, None)
            people_override = live_people_overrides.pop(session_id, _UNSET)
        if renamed_name:
            meta = dict(meta)
            meta["name"] = renamed_name
        if people_override is not _UNSET:
            meta = dict(meta)
            meta["people_count"] = people_override
        # The notes scratchpad is kept exactly as typed; anything that is not text is dropped, never a failed finalize.
        if "scratchpad" in meta:
            meta = dict(meta)
            scratchpad = scratchpad_mod.clean(meta.get("scratchpad"))
            if scratchpad is None:
                meta.pop("scratchpad", None)
            else:
                meta["scratchpad"] = scratchpad
        # An unusable people count (older/newer client, bad value) means automatic, never a failed finalize.
        if "people_count" in meta:
            meta = dict(meta)
            valid = store_mod.valid_people_count(meta.get("people_count"))
            if valid is None:
                meta.pop("people_count", None)
            else:
                meta["people_count"] = valid

        # Preserve recorder lifecycle metadata written through PUT /pipeline.
        # Finalize is authoritative that the recording reached the server, so
        # advance an in-progress upload to complete while retaining its byte
        # accounting and the name/device fields supplied earlier.
        def finalize_meta(previous_meta: dict) -> dict:
            final_meta = dict(meta)
            if previous_meta.get("name_updated_at") and previous_meta.get("name"):
                # A saved-meeting rename is a newer user choice than the
                # recorder's finalize payload, even for legacy sessions that
                # never recorded upload lifecycle metadata.
                final_meta["name"] = previous_meta["name"]
            if previous_meta.get("people_count_updated_at"):
                # Likewise a count edited on the saved meeting beats the recorder's.
                final_meta.pop("people_count", None)
                if previous_meta.get("people_count"):
                    final_meta["people_count"] = previous_meta["people_count"]
                final_meta["people_count_updated_at"] = previous_meta["people_count_updated_at"]
            if "scratchpad" not in final_meta and previous_meta.get("scratchpad"):
                # A re-upload from a recorder that sends no notes never wipes the ones already stored.
                final_meta["scratchpad"] = previous_meta["scratchpad"]
            previous_upload = (
                previous_meta.get("upload")
                if isinstance(previous_meta.get("upload"), dict)
                else None
            )
            if previous_upload is not None:
                # An explicit saved-meeting rename wins over an old client
                # finalize payload.  Device and upload fields still retain
                # recorder metadata supplied through PUT /pipeline.
                for field in ("name", "device"):
                    if not final_meta.get(field) and previous_meta.get(field):
                        final_meta[field] = previous_meta[field]
                upload = dict(previous_upload)
                incoming_upload = final_meta.get("upload")
                if isinstance(incoming_upload, dict):
                    upload.update(incoming_upload)
                upload["state"] = "complete"
                upload["percent"] = 100.0
                if upload.get("bytes_total"):
                    upload["bytes_received"] = max(
                        int(upload.get("bytes_received") or 0), int(upload.get("bytes_total") or 0)
                    )
                final_meta["upload"] = upload
            client_meta = _client_meta(request) or (
                {"client": previous_meta["client"]} if isinstance(previous_meta.get("client"), dict) else {}
            )
            final_meta.update(client_meta)
            return final_meta

        store.update_session_meta(session_id, finalize_meta)

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
        media_dir = store.media_session_dir(session_id)
        for raw_path in sorted(media_dir.glob("*.raw")):
            track = raw_path.stem
            wav_path = store.track_wav_path(session_id, track)
            try:
                # A retried finalize must not rewrite an up-to-date WAV: that
                # would bump its mtime and look like new audio to ensure_job.
                if wav_path.exists() and wav_path.stat().st_mtime >= raw_path.stat().st_mtime:
                    continue
            except OSError:
                pass
            wrap_raw_as_wav(raw_path, wav_path, wire.STREAM_SAMPLE_RATE)

        # Idempotent: a retried finalize returns the session's active (or
        # already finished, audio unchanged) job instead of queueing another.
        job_id = job_queue.ensure_job(session_id)
        return {"job_id": job_id}

    # -- HTTP: job status / transcript --------------------------------------

    @app.get(wire.job_path("{job_id}"))
    async def job_status(job_id: str, _auth: None = Depends(auth.require_token)):
        if not store_mod.is_safe_id(job_id):
            raise HTTPException(status_code=400, detail=f"invalid job_id: {job_id!r}")
        job = store.read_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown job")
        detail = store.session_detail(str(job.get("session_id") or ""))
        pipeline = detail.get("pipeline") if detail else None
        return {
            "state": job.get("state"),
            "progress": job.get("progress"),
            "error": job.get("error"),
            "session_id": job.get("session_id"),
            "upload_percent": pipeline["upload"]["percent"] if pipeline else 0.0,
            "transcription_percent": round(float(job.get("progress") or 0.0) * 100.0, 2),
            **store_mod.job_stages(job),
            "pipeline": pipeline,
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
