"""Shared pieces of the mobile recorder (``docs/mobile-recorder-design.md``).

The mobile recorder is split over a few modules that must agree on these:

* the per-session state file ``<media>/<session>/mobile/state.json`` and its states;
* the exact-``Origin`` check every cookie-authenticated mobile write uses (cross-site request forgery guard);
* the live fence: once a mobile session's ``finish`` is accepted, no live stream may write to it again.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Callable, Dict, List, Optional
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from . import settings as settings_mod

# Session states (persisted). ``open`` takes chunks; ``preparing`` is decoding a frozen manifest; ``finalized``
# is a normal meeting; ``failed`` keeps its chunks and an ``error`` so the page can retry.
OPEN, PREPARING, FINALIZED, FAILED = "open", "preparing", "finalized", "failed"
STATES = (OPEN, PREPARING, FINALIZED, FAILED)

SOURCE = "mobile"  # meta["source"] of a meeting recorded on the phone page
TRACK = "system"  # the one track a phone recording becomes (the track that gets speaker labels)


def mobile_dir(store, session_id: str) -> Path:
    return Path(store.media_session_dir(session_id)) / "mobile"


def state_path(store, session_id: str) -> Path:
    return mobile_dir(store, session_id) / "state.json"


def read_state(store, session_id: str) -> Optional[dict]:
    """The session's mobile state record, or None when it is not a mobile session (or unreadable)."""
    try:
        data = json.loads(state_path(store, session_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("state") in STATES else None


def write_state(store, session_id: str, data: dict) -> None:
    """Atomic write of the state record (temp file + ``os.replace``). Callers hold the session's lock."""
    path = state_path(store, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


_locks: Dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def session_lock(session_id: str) -> threading.Lock:
    """One lock per mobile session, shared by every module that changes its state."""
    with _locks_guard:
        lock = _locks.get(session_id)
        if lock is None:
            lock = _locks[session_id] = threading.Lock()
        return lock


# -- same-origin check ---------------------------------------------------------------------------------------------


def _origin_of(url: str) -> str:
    parts = urlsplit(url.strip())
    if not parts.scheme or not parts.netloc:
        return ""
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


def allowed_origins(root) -> List[str]:
    """Origins a browser page of this server can have: the configured ``server_address`` plus
    ``MEETING_NOTES_ALLOWED_ORIGINS`` (comma separated, e.g. the https name next to an http LAN name)."""
    origins = []
    configured = _origin_of(settings_mod.load_settings(root).server_address or "")
    if configured:
        origins.append(configured)
    for extra in (os.environ.get("MEETING_NOTES_ALLOWED_ORIGINS") or "").split(","):
        o = _origin_of(extra)
        if o and o not in origins:
            origins.append(o)
    return origins


def same_origin(request: Request, root) -> bool:
    """True when the request is not a cookie-authenticated browser request from another site.

    Requests carrying a bearer header (desktop clients, agents) are not browser-ambient and pass. Browser requests
    must send an ``Origin`` equal to one of :func:`allowed_origins`, or, when none is configured, equal to the
    request's own scheme and host."""
    if (request.headers.get("authorization") or "").lower().startswith("bearer "):
        return True
    origin = _origin_of(request.headers.get("origin") or "")
    if not origin:
        return False
    allowed = allowed_origins(root)
    if not allowed:
        allowed = [_origin_of(f"{request.url.scheme}://{request.headers.get('host') or request.url.netloc}")]
    return origin in allowed


def require_same_origin(request: Request, root) -> None:
    if not same_origin(request, root):
        raise HTTPException(status_code=403, detail="this request must come from the Meeting Notes page")


# -- live fence ----------------------------------------------------------------------------------------------------

# Callables that close any live stream of a session (registered by the stream side). The upload side calls
# :func:`fence_live` when ``finish`` is accepted; the stream side also refuses writes when :func:`read_state` says
# the session is past ``open``.
_live_closers: List[Callable[[str], None]] = []


def register_live_closer(fn: Callable[[str], None]) -> None:
    if fn not in _live_closers:
        _live_closers.append(fn)


def fence_live(session_id: str) -> None:
    for fn in list(_live_closers):
        try:
            fn(session_id)
        except Exception:  # noqa: BLE001 - closing a live socket is best effort; the state check still refuses writes
            pass


def live_writes_allowed(store, session_id: str) -> bool:
    """A live stream may write audio to this session: it is not a mobile session, or its upload is still open."""
    state = read_state(store, session_id)
    return state is None or state.get("state") == OPEN
