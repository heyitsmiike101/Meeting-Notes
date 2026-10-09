# COMPAT FIXTURE - do not edit. Verbatim copy of meeting_notes/client/version_gate.py from the 0.7.13 client
# (release/0.7.13), with only the meeting_notes.* imports rewritten to be package-relative.
"""Remembers that the server says this client is too old.

Two ways to find out: any request answered ``426 Upgrade Required`` (JSON body
``{"detail": ..., "min_client_version": "x.y.z"}``), or the update manifest
carrying a ``min_client_version`` newer than this client. Either way the window
shows a red "no longer supported" banner with an Update button. Written to from
worker threads (uploader, streamer, updater), read from the UI thread.
"""

from __future__ import annotations

import logging
import threading
from typing import Dict, Optional

log = logging.getLogger("meeting_notes.client.version_gate")

HTTP = "http"          # a 426 response; cleared by the next successful request
MANIFEST = "manifest"  # the update manifest's min_client_version

_lock = threading.Lock()
_notes: Dict[str, Dict[str, str]] = {}


def note_too_old(source: str, min_version: str = "", detail: str = "") -> None:
    with _lock:
        fresh = source not in _notes
        _notes[source] = {"min_version": min_version or "", "detail": detail or ""}
    if fresh:
        log.warning(
            "server says this client is too old (via %s): min=%s %s", source, min_version or "?", detail or ""
        )


def clear(source: Optional[str] = None) -> None:
    with _lock:
        if source is None:
            _notes.clear()
        else:
            _notes.pop(source, None)


def too_old() -> Optional[Dict[str, str]]:
    """The reason the server refuses this client, or ``None``."""
    with _lock:
        for source in (HTTP, MANIFEST):
            if source in _notes:
                return dict(_notes[source], source=source)
    return None


def inspect_response(response) -> None:
    """Look at any server response: note a 426, and forget one after a success."""
    status = getattr(response, "status_code", None)
    if status == 426:
        min_version, detail = "", ""
        try:
            body = response.json()
            if isinstance(body, dict):
                min_version = str(body.get("min_client_version") or "")
                detail = str(body.get("detail") or "")
        except Exception:  # noqa: BLE001 - the body is optional
            pass
        note_too_old(HTTP, min_version, detail)
    elif isinstance(status, int) and status < 400:
        clear(HTTP)


def is_too_old_error(exc: BaseException) -> bool:
    return getattr(getattr(exc, "response", None), "status_code", None) == 426
