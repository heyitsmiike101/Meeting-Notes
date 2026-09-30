"""One clear status for a recording: what the recorder knows plus what the server knows.

Shared by both sides (like ``remote.py``): the server uses it to answer the Recorders page, and the
recorder's "Re-upload a saved recording" window uses it with the states it gets from
``POST /v1/recordings/status``, so both always say the same thing.

``local`` is a recorder row as ``remote.sanitize_recording`` documents it (the part that matters
here is ``active``, ``valid``, ``reason`` and ``queue {state, percent, error}``). ``server`` is what
the server knows about that session id, or ``None`` when it could not be asked::

    {"on_server": bool,          # a live (not deleted) meeting with this id exists
     "has_copy": bool,           # ... and its upload finished (a transcription job exists)
     "in_trash": bool,           # it is in the server's Recently deleted
     "transcription": "complete" | "transcribing" | "queued" | "error" | None,
     "error": str | None}

``combine`` returns ``{status, label, tone, detail, server_has_copy}``. ``tone`` is one of
``ok`` (green), ``info`` (blue), ``warn`` (amber), ``error`` (red), ``muted`` (unknown).
"""

from __future__ import annotations

from typing import Any, Dict, Optional

# status key -> tone. The label is built in ``combine`` (some carry a percent or a reason).
TONES = {
    "recording": "info",
    "uploading": "info",
    "waiting": "info",
    "failed": "error",
    "uploaded_ready": "ok",
    "uploaded_transcribing": "info",
    "uploaded_queued": "info",
    "uploaded_error": "warn",
    "partial": "warn",
    "in_trash": "warn",
    "not_on_server": "warn",
    "invalid": "error",
    "unknown": "muted",
}
STATUSES = tuple(TONES)

_SERVER_LABELS = {
    "uploaded_ready": "Uploaded · transcript ready",
    "uploaded_transcribing": "Uploaded · transcribing",
    "uploaded_queued": "Uploaded · transcription queued",
    "uploaded_error": "Uploaded · transcription failed",
}
_TRANSCRIPTION_STATUS = {
    "complete": "uploaded_ready",
    "transcribing": "uploaded_transcribing",
    "queued": "uploaded_queued",
    "error": "uploaded_error",
}

DELETE_WARNING = (
    "The server has no copy. This permanently removes the only copy "
    "(it goes to this computer's {trash})."
)


def _short(text: Any, limit: int = 160) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def server_state_from_row(row: Optional[Dict[str, Any]], in_trash: bool = False) -> Dict[str, Any]:
    """The ``server`` dict for one session from its live index row (``Index.get_session``) and trash flag.

    A row with no job yet (no ``latest_state``) is a session the server has started receiving but
    has not finalized: it is on the server but has no copy worth relying on.
    """
    if row is None:
        return {"on_server": False, "has_copy": False, "in_trash": bool(in_trash), "transcription": None, "error": None}
    job = row.get("latest_state")
    transcription = {"done": "complete", "running": "transcribing", "queued": "queued", "error": "error"}.get(job)
    return {
        "on_server": True,
        "has_copy": transcription is not None,
        "in_trash": False,
        "transcription": transcription,
        "error": row.get("latest_error") if transcription == "error" else None,
    }


def combine(local: Dict[str, Any], server: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The single status for one recording (see the module docstring)."""
    queue = local.get("queue") if isinstance(local.get("queue"), dict) else {}
    qstate = queue.get("state") or "not_queued"
    has_copy = bool(server and server.get("has_copy"))
    out = {"status": "unknown", "label": "Server status unknown", "tone": "muted", "detail": None,
           "server_has_copy": has_copy}

    def put(status: str, label: str, detail: Optional[str] = None) -> Dict[str, Any]:
        out.update(status=status, label=label, tone=TONES[status], detail=detail)
        return out

    # What this computer is doing right now wins over what the server says.
    if local.get("active") or qstate == "recording":
        return put("recording", "Recording now")
    if qstate == "uploading":
        pct = queue.get("percent")
        return put("uploading", f"Uploading {int(round(pct))}%" if isinstance(pct, (int, float)) else "Uploading")
    if qstate == "failed":
        return put("failed", f"Upload failed: {_short(queue.get('error')) or 'unknown error'}")
    if qstate == "pending":
        err = _short(queue.get("error"))
        return put("waiting", "Waiting to upload", f"Last try failed: {err}" if err else None)
    if server is None:
        return out
    if server.get("on_server"):
        status = _TRANSCRIPTION_STATUS.get(server.get("transcription") or "")
        if status is not None:
            detail = _short(server.get("error")) if status == "uploaded_error" and server.get("error") else None
            return put(status, _SERVER_LABELS[status], detail)
        return put("partial", "Partly uploaded", "The server started receiving this but never finished.")
    if server.get("in_trash"):
        return put("in_trash", "In server trash", "Deleted on the server; it is removed for good after 30 days.")
    if local.get("valid") is False:
        return put("invalid", f"Can't upload: {_short(local.get('reason')) or 'not a valid recording'}")
    return put("not_on_server", "Not on server")


def delete_warning(trash_name: str = "Recycle Bin") -> str:
    return DELETE_WARNING.format(trash=trash_name)
