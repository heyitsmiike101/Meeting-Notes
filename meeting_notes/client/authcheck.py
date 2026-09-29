"""A cheap authenticated probe of the server, to tell a wrong token from an outage.

``GET /v1/sessions?per_page=1`` is protected by the same bearer token the
uploader uses (``auth.require_token``), so a bad token answers 401/403 and a
good one 200, while ``/health`` is open and proves nothing about the token. If
the server runs with no token configured it answers 200 to anything, which is
correctly reported as connected.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import httpx

from meeting_notes.client.api import ServerClient, ServerUnavailable

log = logging.getLogger("meeting_notes.client.auth")

OK = "ok"
REJECTED = "rejected"
UNREACHABLE = "unreachable"
NO_SERVER = "no_server"
ERROR = "error"


@dataclass(frozen=True)
class CheckResult:
    status: str
    url: str = ""
    detail: str = ""
    http_status: Optional[int] = None

    @property
    def ok(self) -> bool:
        return self.status == OK

    @property
    def rejected(self) -> bool:
        return self.status == REJECTED

    @property
    def unreachable(self) -> bool:
        return self.status == UNREACHABLE

    def message(self) -> str:
        """The plain sentence shown next to the token field."""
        if self.status == OK:
            return f"Connected to {self.url}"
        if self.status == REJECTED:
            return "Token rejected by the server"
        if self.status == UNREACHABLE:
            return f"Can't reach the server at {self.url}"
        if self.status == NO_SERVER:
            return "Enter a server URL first"
        return f"The server answered unexpectedly ({self.detail})"


def check_connection(url: str, token: str, timeout: float = 6.0) -> CheckResult:
    """Blocking check; call it from a background thread. Never raises."""
    url = (url or "").strip().rstrip("/")
    if not url:
        return CheckResult(NO_SERVER)
    try:
        with ServerClient(url, token or None, timeout=timeout) as client:
            client.list_sessions(per_page=1)
    except ServerUnavailable as exc:
        result = CheckResult(UNREACHABLE, url, str(exc))
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code in (401, 403):
            result = CheckResult(REJECTED, url, f"HTTP {code}", code)
        else:
            result = CheckResult(ERROR, url, f"HTTP {code}", code)
    except Exception as exc:  # noqa: BLE001 - a check must never raise into the UI
        result = CheckResult(ERROR, url, f"{type(exc).__name__}: {exc}")
    else:
        result = CheckResult(OK, url, "HTTP 200", 200)
    log.info("auth check %s: %s %s", url, result.status, result.detail)
    return result
