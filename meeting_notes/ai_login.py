"""Recognise an AI provider's "your login has expired" failure.

Shared by the bridge (which runs the provider CLIs) and the server (which keeps the AI login health and holds the
notes queue while the login is broken; ``server/ai_health.py``). ``claude auth status`` only says whether saved
credentials exist, so an expired or revoked OAuth session still reads as logged in there: the real signal is a run
that fails with one of these messages, or the bridge's small test request failing the same way.
"""

from __future__ import annotations

import re

# Messages seen from the Claude Code CLI and the Codex CLI when the account login no longer works, e.g.
# "Failed to authenticate: OAuth session expired and could not be refreshed" and
# "Failed to authenticate. API Error: 401 OAuth access token has expired. Re-authenticate to continue."
_LOGIN_FAILURE_RE = re.compile(
    r"failed to authenticate"
    r"|oauth (session|access token|token)"
    r"|access token (has )?expired"
    r"|token (has )?expired"
    r"|re-?authenticate"
    r"|not logged in"
    r"|please (run )?/?login"
    r"|login (is )?required"
    r"|authentication_error"
    r"|invalid (api key|bearer token)"
    r"|refresh token"
    r"|\b401\b.*unauthori[sz]ed|unauthori[sz]ed.*\b401\b",
    re.IGNORECASE,
)


def is_login_failure(message: object) -> bool:
    """True when ``message`` (a job error or CLI output) says the provider login no longer works."""
    return isinstance(message, str) and bool(_LOGIN_FAILURE_RE.search(message))
