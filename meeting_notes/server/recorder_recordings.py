"""Recordings saved on a recorder, joined with what the server knows (web auth only).

* ``GET  /v1/recorders/{id}/recordings``            ask the recorder (``list_recordings``), join, one status per row
* ``POST /v1/recorders/{id}/recordings/reupload``   forward ``reupload``  ``{session_ids: [...]}``
* ``POST /v1/recorders/{id}/recordings/delete``     forward ``delete_local`` ``{session_ids: [...]}``
* ``POST /v1/recordings/status``                    a recorder asks the state of its own session ids (client token)

The status wording and the join rules live in ``meeting_notes/recording_status.py`` so the recorder's
own window says the same thing. Agent API keys are not the server token, so they are refused by
``auth.require_token`` like everywhere else; none of this is reachable by the agent API.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from .. import recording_status, remote
from . import auth
from . import store as store_mod
from .recorders import CommandTimeout, RecorderGone, RecorderHub

logger = logging.getLogger("meeting_notes.server.recordings")

MAX_STATUS_IDS = 1000
MAX_PAGES = 50        # a list is paged at most this many times (the recorder pages by size)


async def _ask(hub: RecorderHub, instance_id: str, command: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """``hub.send_command`` with failures mapped to the HTTP errors the commands route uses."""
    try:
        return await hub.send_command(instance_id, command, args)
    except KeyError:
        raise HTTPException(status_code=404, detail="recorder is not connected") from None
    except BufferError:
        raise HTTPException(status_code=429, detail="the recorder is still busy with earlier commands") from None
    except CommandTimeout:
        raise HTTPException(status_code=504, detail="the recorder did not answer in time") from None
    except RecorderGone:
        raise HTTPException(status_code=409, detail="the recorder disconnected") from None


def server_states(store: store_mod.Store, session_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """What the server knows about each id: the live index row first, else the trash."""
    safe = [i for i in dict.fromkeys(session_ids) if store_mod.is_safe_id(i)]
    rows = store.index.get_many(safe)
    out: Dict[str, Dict[str, Any]] = {}
    for sid in session_ids:
        row = rows.get(sid)
        in_trash = row is None and sid in safe and store.is_trashed(sid)
        out[sid] = recording_status.server_state_from_row(row, in_trash)
    return out


def _ids_body(body: Any, limit: int) -> List[str]:
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    ids = body.get("session_ids")
    if not isinstance(ids, list) or not ids:
        raise HTTPException(status_code=400, detail="session_ids must be a non-empty list")
    if len(ids) > limit:
        raise HTTPException(status_code=400, detail=f"at most {limit} session ids per request")
    clean: List[str] = []
    for item in ids:
        if not remote.valid_session_id(item):
            raise HTTPException(status_code=400, detail="invalid session id")
        if item not in clean:
            clean.append(item)
    return clean


async def _json(request: Request) -> Any:
    try:
        return await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="body must be JSON") from None


def _refused(ack: Dict[str, Any]) -> Dict[str, Any]:
    return {"ok": False, "code": ack.get("code"), "error": ack.get("error") or "The recorder could not do that."}


def install_recorder_recordings(app: FastAPI, hub: RecorderHub, store: store_mod.Store) -> None:
    @app.get("/v1/recorders/{instance_id}/recordings")
    async def recorder_recordings(instance_id: str, _auth: None = Depends(auth.require_token)):
        """Every recording in the recorder's save folder with a single status per row."""
        if not remote.valid_instance_id(instance_id):
            raise HTTPException(status_code=400, detail="invalid recorder id")
        rows: List[Dict[str, Any]] = []
        offset, total, more = 0, 0, False
        for _ in range(MAX_PAGES):
            ack = await _ask(hub, instance_id, "list_recordings", {"offset": offset} if offset else {})
            if not ack.get("ok"):
                return _refused(ack)
            page = ack.get("result") or {}
            rows.extend(page.get("recordings") or [])
            total = page.get("total") or len(rows)
            nxt = page.get("next_offset")
            more = nxt is not None
            if nxt is None or nxt <= offset or len(rows) >= remote.MAX_RECORDINGS:
                break
            offset = nxt
        rows = rows[: remote.MAX_RECORDINGS]
        states = await run_in_threadpool(server_states, store, [r["session_id"] for r in rows])
        rec = hub.get(instance_id)
        out, by_status = [], {}
        for row in rows:
            server = states.get(row["session_id"])
            status = recording_status.combine(row, server)
            by_status[status["status"]] = by_status.get(status["status"], 0) + 1
            item = {**row, **status, "server": server}
            item["meeting_url"] = "/sessions/" + quote(row["session_id"], safe="") if server and server["on_server"] else None
            out.append(item)
        out.sort(key=lambda r: r.get("started") or 0, reverse=True)
        uploaded = sum(n for k, n in by_status.items() if k.startswith("uploaded_"))
        return {
            "ok": True,
            "instance_id": instance_id,
            "device": rec.device if rec else "",
            "trash_name": "Trash" if rec and rec.platform == "macos" else "Recycle Bin",
            "total": max(total, len(out)),
            "truncated": more or total > len(out),
            "recordings": out,
            "summary": {"total": len(out), "uploaded": uploaded, "by_status": by_status},
        }

    async def _forward_ids(instance_id: str, command: str, request: Request) -> Dict[str, Any]:
        if not remote.valid_instance_id(instance_id):
            raise HTTPException(status_code=400, detail="invalid recorder id")
        ids = _ids_body(await _json(request), MAX_STATUS_IDS)
        merged: Optional[Dict[str, Any]] = None
        for start in range(0, len(ids), remote.MAX_IDS):
            ack = await _ask(hub, instance_id, command, {"session_ids": ids[start:start + remote.MAX_IDS]})
            if not ack.get("ok"):
                if merged is None:
                    return _refused(ack)
                merged["ok"] = False
                merged["error"] = ack.get("error")
                break
            part = ack.get("result") or {}
            if merged is None:
                merged = {"ok": True, **part}
                merged["results"] = list(part.get("results") or [])
            else:
                merged["results"].extend(part.get("results") or [])
                for key in ("queued", "already_queued", "deleted", "freed_bytes"):
                    if key in part:
                        merged[key] = merged.get(key, 0) + part[key]
        merged = merged or {"ok": True, "results": []}
        merged["state"] = (hub.get(instance_id).state if hub.get(instance_id) else None)
        failed = [r for r in merged.get("results", []) if not r.get("ok")]
        logger.info("%s on %s: %d ids, %d refused", command, instance_id[:8], len(ids), len(failed))
        return merged

    @app.post("/v1/recorders/{instance_id}/recordings/reupload")
    async def recorder_reupload(instance_id: str, request: Request, _auth: None = Depends(auth.require_token)):
        """Ask the recorder to send saved recordings to the server again."""
        return await _forward_ids(instance_id, "reupload", request)

    @app.post("/v1/recorders/{instance_id}/recordings/delete")
    async def recorder_delete(instance_id: str, request: Request, _auth: None = Depends(auth.require_token)):
        """Ask the recorder to move saved recordings to its Recycle Bin / Trash."""
        return await _forward_ids(instance_id, "delete_local", request)

    @app.post("/v1/recordings/status")
    async def recordings_status(request: Request, _auth: None = Depends(auth.require_token)):
        """A recorder asks what the server knows about its own session ids (client token)."""
        ids = _ids_body(await _json(request), MAX_STATUS_IDS)
        states = await run_in_threadpool(server_states, store, ids)
        return {"items": states}
