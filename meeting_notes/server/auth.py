"""Bearer-token auth for a LAN tool that would rather be loud than silent.

There's no user database here, just one shared secret (``MEETING_NOTES_TOKEN``)
for the whole server -- this is meant to run on a machine you already trust on
your own network, guarding against "anyone who can reach port 8000" rather
than multi-tenant access control. If the operator hasn't set a token, we don't
silently lock the door open either: every request is allowed, but a warning is
logged once so running with no auth is a decision someone can notice, not an
accident that goes unremarked until it matters.
"""

from __future__ import annotations

import hmac
import logging
import os
from typing import Optional

from fastapi import Header, HTTPException

logger = logging.getLogger("meeting_notes.server.auth")

_warned_insecure = False


def _configured_token() -> Optional[str]:
    # Read the env var live rather than caching it at import time: tests build
    # a fresh app per token configuration, and a real deployment should be
    # free to inject it however its process manager likes.
    return os.environ.get("MEETING_NOTES_TOKEN") or None


def _warn_insecure_once() -> None:
    global _warned_insecure
    if _warned_insecure:
        return
    _warned_insecure = True
    logger.warning(
        "MEETING_NOTES_TOKEN is not set -- this server is accepting requests "
        "from ANYONE who can reach it on the network, with no authentication "
        "at all. This is fine for a quick local test; set MEETING_NOTES_TOKEN "
        "before running this anywhere else on the LAN."
    )


def extract_bearer(header_value: Optional[str]) -> Optional[str]:
    """Pull the token out of an ``Authorization: Bearer <token>`` header."""
    if not header_value:
        return None
    scheme, _, value = header_value.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


def token_is_valid(token: Optional[str]) -> bool:
    """True if ``token`` matches the configured secret.

    Also true -- with a loud warning -- when no secret is configured at all.
    Uses ``hmac.compare_digest`` so a wrong guess can't be narrowed down by
    timing how long the comparison took.
    """
    expected = _configured_token()
    if expected is None:
        _warn_insecure_once()
        return True
    if not token:
        return False
    return hmac.compare_digest(token, expected)


def require_token(authorization: Optional[str] = Header(default=None)) -> None:
    """FastAPI dependency for HTTP routes. Raises 401/403 on failure.

    401 (no credentials presented) vs 403 (credentials presented but wrong) is
    the conventional split, and it's cheap to give a reconnecting client that
    extra bit of information about which problem it has.
    """
    expected = _configured_token()
    if expected is None:
        _warn_insecure_once()
        return
    token = extract_bearer(authorization)
    if token is None:
        raise HTTPException(status_code=401, detail="missing bearer token")
    if not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=403, detail="invalid bearer token")


def token_from_websocket(query_token: Optional[str], authorization: Optional[str]) -> Optional[str]:
    """A websocket client may not be able to set custom headers easily (some
    browser/JS websocket clients can't), so we accept the token either as a
    ``?token=`` query parameter or as a normal Authorization header -- whichever
    the client finds easier."""
    if query_token:
        return query_token
    return extract_bearer(authorization)


def authorize_websocket(query_token: Optional[str], authorization: Optional[str]) -> bool:
    """Plain-function equivalent of ``require_token`` for the websocket path,
    which (unlike an HTTP route) can't just raise HTTPException -- the caller
    decides how to close the connection."""
    return token_is_valid(token_from_websocket(query_token, authorization))
