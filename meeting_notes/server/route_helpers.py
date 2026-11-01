"""Small helpers shared by the route modules (``install_*`` in this package): id and redirect checks, request-body
parsers, client-version gating, and per-app closures that read the store."""

from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool
from starlette.formparsers import MultiPartException, MultiPartParser

from . import compat, settings as settings_mod, store as store_mod

# Same logger name as app.py, so log records and test captures are unchanged by the split.
logger = logging.getLogger("meeting_notes.server.app")


class _RecordingMultipartParser(MultiPartParser):
    """Starlette multipart parser with a real per-file byte limit.

    Starlette's ``max_part_size`` applies only to ordinary form fields. File
    parts otherwise spool without a size ceiling before the endpoint can read
    them, so enforce the recording limit in the parser callback itself.
    """

    def __init__(self, *args, max_file_size: int, label: str = "recording", **kwargs):
        super().__init__(*args, **kwargs)
        self.max_file_size = max_file_size
        self.label = label
        self._current_file_size = 0

    def on_part_begin(self) -> None:
        super().on_part_begin()
        self._current_file_size = 0

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self._current_part.file is not None:
            self._current_file_size += end - start
            if self._current_file_size > self.max_file_size:
                raise MultiPartException(
                    f"{self.label} exceeds {self.max_file_size} byte limit"
                )
        super().on_part_data(data, start, end)

def _safe_next(value: str) -> str:
    """A post-login redirect target, only when it is a path on this server ("/record/", "/sessions/x?y=1"); else "".
    Never a scheme, a host, "//evil" or a backslash trick, so the login form cannot be used as an open redirect."""
    value = (value or "").strip()
    if (not value.startswith("/") or value.startswith("//") or "\\" in value
            or any(c in value for c in "\r\n\t") or len(value) > 512 or value.startswith("/login")):
        return ""
    return value

def _safe_session_id_or_400(session_id: str) -> None:
    """Every route that takes a session id refuses an unsafe one (path tricks, too long) the same way."""
    if not store_mod.is_safe_id(session_id):
        raise HTTPException(status_code=400, detail=f"invalid session_id: {session_id!r}")


def _client_info(conn) -> Optional[compat.ClientInfo]:
    return getattr(conn.state, "client_info", None)


def _client_meta(conn) -> dict:
    info = _client_info(conn)
    return {"client": {"version": info.version, "platform": info.platform}} if info else {}


def _refuse_stale_new_upload(conn, session_known: bool = False) -> None:
    """426 for a recorder older than the compatibility window that is
    starting a NEW upload. Never for an existing session (finalize, resume,
    polling): a recording already on the server is always finished."""
    info = _client_info(conn)
    if info is not None and not session_known and info.is_too_old():
        raise compat.ClientTooOld(info)


# -- JSON API: sessions -------------------------------------------------

async def _required_name(request: Request, field: str) -> str:
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc
    if not isinstance(body, dict) or not isinstance(body.get(field), str):
        raise HTTPException(status_code=400, detail=f"{field} must be a string")
    value = body[field].strip()
    if not value:
        raise HTTPException(status_code=400, detail=f"{field} must not be empty")
    if len(value) > 200:
        raise HTTPException(status_code=400, detail=f"{field} must be 200 characters or fewer")
    return value


_UNSET = object()


def _people_count_body(body) -> object:
    """The ``people_count`` of a JSON body: ``_UNSET`` when absent, None to clear, else a whole number 1-50."""
    if not isinstance(body, dict) or "people_count" not in body:
        return _UNSET
    value = body["people_count"]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 50:
        raise HTTPException(status_code=400, detail="people_count must be a whole number from 1 to 50, or null")
    return value


async def _name_and_people(request: Request) -> tuple:
    """``(name or None, people_count or _UNSET)`` of a rename body; at least one must be present."""
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc
    people = _people_count_body(body)
    if people is not _UNSET and isinstance(body, dict) and "name" not in body:
        return None, people
    return await _required_name_from(body, "name"), people


async def _required_name_from(body, field: str) -> str:
    if not isinstance(body, dict) or not isinstance(body.get(field), str):
        raise HTTPException(status_code=400, detail=f"{field} must be a string")
    value = body[field].strip()
    if not value:
        raise HTTPException(status_code=400, detail=f"{field} must not be empty")
    if len(value) > 200:
        raise HTTPException(status_code=400, detail=f"{field} must be 200 characters or fewer")
    return value


def appearance_reader(store):

    def _appearance() -> str:
        return settings_mod.load_settings(store.root).appearance
    return _appearance


def ai_enabled_reader(store):
    def _ai_enabled() -> bool:
        return settings_mod.load_settings(store.root).ai_provider != "disabled"
    return _ai_enabled


def restore_trashed_for(store):
    async def _restore_trashed(session_id: str) -> None:
        """A recorder re-sending a meeting that is in Recently deleted (the
        owner's recovery path) brings it back first, so the upload fills the
        existing meeting. An id that was permanently deleted is simply created
        fresh, as before."""
        try:
            if await run_in_threadpool(store.restore_if_trashed, session_id):
                logger.info("restored %s from Recently deleted for a re-upload", session_id)
        except store_mod.TrashConflict:
            pass  # the id is live again; the upload continues into it
    return _restore_trashed
