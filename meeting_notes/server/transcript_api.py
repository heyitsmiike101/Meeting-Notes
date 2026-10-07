"""``POST /v1/sessions/transcript``: create a meeting from a transcript that already exists.

Used by the recorder's Upload dialog (a ``.txt`` / ``.vtt`` / ``.srt`` file, or pasted text) and the web
Home page. Nothing is transcribed: the session gets no audio and one finished transcript job, exactly
like a meeting whose audio was later removed, so the web UI, search, notes (made automatically, with the default
note type), Notion export, split and combine all work on it unchanged. ``install_transcript_upload`` is one
call from ``create_app``.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

from fastapi import Depends, FastAPI, HTTPException, Request

from .. import wire
from ..transcribe.merge import render_json, render_markdown
from . import auth
from . import transcript_import as ti

logger = logging.getLogger("meeting_notes.server.transcript_api")

SOURCES = ("pasted", "file")
# JSON escapes non-ASCII text (up to six bytes a character), so the body cap is looser than the text cap;
# the text itself is measured exactly in ``parse_transcript``.
_BODY_LIMIT = 4 * ti.MAX_TRANSCRIPT_BYTES + 64 * 1024
_MAX_EPOCH = 4102444800  # 2100-01-01


def _started_epoch(value) -> float:
    """``started_at`` as epoch seconds: a number, or an ISO 8601 string (``Z`` or an offset)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return time.time()
    epoch: Optional[float] = None
    if isinstance(value, bool):
        epoch = None
    elif isinstance(value, (int, float)):
        epoch = float(value)
        if epoch > 1e11:  # milliseconds
            epoch /= 1000.0
    elif isinstance(value, str):
        text = value.strip()
        try:
            epoch = float(text)
            if epoch > 1e11:
                epoch /= 1000.0
        except ValueError:
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                epoch = parsed.timestamp()
            except ValueError:
                epoch = None
    if epoch is None or not 0 < epoch < _MAX_EPOCH:
        raise HTTPException(
            status_code=400,
            detail="started_at must be an ISO 8601 date/time (2026-09-28T14:30:00Z) or a unix timestamp",
        )
    return epoch


def _default_name(filename: str, source: str, epoch: float) -> str:
    stem = os.path.splitext(os.path.basename(filename or ""))[0].strip()
    if stem:
        return stem[:200]
    when = datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    return f"{'Uploaded' if source == 'file' else 'Pasted'} transcript {when}"


def install_transcript_upload(
    app: FastAPI,
    *,
    store,
    job_queue,
    client_meta: Callable[[Request], dict],
    refuse_stale: Callable[[Request], None],
) -> None:
    @app.post("/v1/sessions/transcript", status_code=201)
    async def upload_transcript(request: Request, _auth: None = Depends(auth.require_token)):
        """Create a finished meeting from ``{name?, started_at?, text, source?, filename?}``."""
        refuse_stale(request)
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > _BODY_LIMIT:
            raise HTTPException(status_code=413, detail=_too_big())
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > _BODY_LIMIT:
                raise HTTPException(status_code=413, detail=_too_big())
        try:
            payload = json.loads(bytes(body).decode("utf-8") or "null")
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=400, detail="request body must be JSON") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="request body must be a JSON object")

        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            raise HTTPException(status_code=400, detail="text is required: the transcript to upload")
        source = payload.get("source", "pasted")
        if source not in SOURCES:
            raise HTTPException(status_code=400, detail=f"source must be one of: {', '.join(SOURCES)}")
        filename = payload.get("filename")
        if filename is not None and not isinstance(filename, str):
            raise HTTPException(status_code=400, detail="filename must be a string")
        filename = os.path.basename((filename or "").replace("\\", "/"))[:255]
        name = payload.get("name")
        if name is not None and not isinstance(name, str):
            raise HTTPException(status_code=400, detail="name must be a string")
        started = _started_epoch(payload.get("started_at"))
        name = (name or "").strip()[:200] or _default_name(filename, source, started)

        try:
            parsed = ti.parse_transcript(text, filename)
        except ti.TranscriptError as exc:
            status = 413 if "exceeds" in str(exc) else 400
            raise HTTPException(status_code=status, detail=str(exc)) from exc

        iso = datetime.fromtimestamp(started, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        duration = round(parsed.duration, 3)
        meta = {
            "name": name,
            "device": "Uploaded transcript",
            "source": "transcript",
            "created": iso,
            "started_wall": started,
            "duration_sec": duration,
            "tracks": {},
            "transcript_upload": {
                "source": source,
                "filename": filename or None,
                "format": parsed.format,
                "approximate": parsed.approximate,
                "segments": len(parsed.segments),
                "speakers": parsed.speakers,
                "characters": len(text),
            },
            **client_meta(request),
        }
        session_id = uuid.uuid4().hex
        try:
            store.ensure_session_dir(session_id)
            store.write_session_meta(session_id, meta)
            markdown = render_markdown(parsed.segments, meta)
            if parsed.approximate:
                markdown = markdown.replace(
                    f"_Timestamps for the {ti.TRACK} track are approximate (no timing log)._",
                    "_Timestamps are estimated: the uploaded text had no times._",
                )
            json_text = render_json(parsed.segments, meta)
            job_id = store.create_job(
                session_id, {"transcript_upload": True, "source": source, "filename": filename or None}
            )
            store.write_transcript(job_id, markdown, json_text)
            store.update_job(job_id, state=wire.JobState.DONE, progress=1.0, error=None)
        except Exception as exc:  # noqa: BLE001 - never leave a half-made meeting behind
            logger.exception("transcript upload failed for %s", session_id)
            try:
                store.delete_session(session_id)
            except Exception:  # noqa: BLE001
                logger.exception("could not clean up %s", session_id)
            raise HTTPException(status_code=500, detail=f"could not store the transcript: {exc}") from exc

        notes = job_queue.auto_queue_notes(session_id)
        return {
            "session_id": session_id,
            "job_id": job_id,
            "state": wire.JobState.DONE,
            "name": name,
            "started_at": iso,
            "duration_sec": duration,
            "segments": len(parsed.segments),
            "speakers": parsed.speakers,
            "format": parsed.format,
            "approximate": parsed.approximate,
            "notes": "queued" if notes else None,
            "pipeline": store.session_detail(session_id)["pipeline"],
        }


def _too_big() -> str:
    return f"transcript exceeds the {ti.MAX_TRANSCRIPT_BYTES // (1024 * 1024)} MB limit"
