"""Client compatibility: which recorder versions this server promises to serve.

Policy: the server keeps working with the current release and the previous
``SUPPORTED_CLIENT_WINDOW`` (5) client releases. That promise is enforced by
``tests/compat`` (see its README), which runs each supported client's real
network code against the current server.

* ``RELEASES`` is the ordered list of shipped client versions. A release adds
  its version here (and its fixtures under ``tests/compat/clients``) in the
  same change that bumps ``meeting_notes.__version__``.
* ``min_client_version()`` is the oldest version inside the window. It is
  published in ``/health`` and ``/install/client-manifest.json``.
* Clients report ``X-Meeting-Notes-Client: <version>; <platform>`` on every
  request. Every client released before this header existed sends nothing, and
  a missing or unparseable header is a *legacy* client: always allowed, never
  rejected. Only a client that positively reports a version older than the
  window is told (HTTP 426) to update, and only when it tries to START
  uploading a new recording. Finalizing or polling an already-uploaded
  session, reading history and sending logs are never refused.
* Which recorders are connected right now is not tracked here: see
  ``recorders.py`` (in-memory, fed by each recorder's websocket).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .. import __version__

CLIENT_HEADER = "X-Meeting-Notes-Client"
SUPPORTED_CLIENT_WINDOW = 5

# Every shipped client version, oldest first. The running server's own version
# is appended automatically when missing, but tests/compat fails until the
# release is listed here on purpose.
RELEASES: List[str] = ["0.6.1", "0.7.0", "0.7.1", "0.7.2", "0.7.3", "0.7.4", "0.7.5", "0.7.6", "0.7.7", "0.7.8", "0.7.9", "0.7.10", "0.7.11", "0.7.12", "0.7.13", "0.7.14"]

_VERSION_RE = re.compile(r"^v?(\d+(?:\.\d+){0,3})")


def version_key(value: str) -> Optional[Tuple[int, ...]]:
    """``"0.7.10"`` -> ``(0, 7, 10, 0)``; suffixes like ``rc1`` are ignored."""
    match = _VERSION_RE.match(str(value or "").strip())
    if not match:
        return None
    parts = [int(p) for p in match.group(1).split(".")]
    parts += [0] * (4 - len(parts))
    return tuple(parts)


def released_versions(current: str = __version__) -> List[str]:
    """``RELEASES`` plus the running version, oldest first, without duplicates."""
    seen: Dict[Tuple[int, ...], str] = {}
    for version in [*RELEASES, current]:
        key = version_key(version)
        if key is not None and key not in seen:
            seen[key] = version
    return [seen[key] for key in sorted(seen)]


def supported_versions(current: str = __version__) -> List[str]:
    """The newest release and the ``SUPPORTED_CLIENT_WINDOW`` before it."""
    return released_versions(current)[-(SUPPORTED_CLIENT_WINDOW + 1):]


def min_client_version(current: str = __version__) -> str:
    return supported_versions(current)[0]


def is_too_old(version: str, current: str = __version__) -> bool:
    key = version_key(version)
    floor = version_key(min_client_version(current))
    return key is not None and floor is not None and key < floor


@dataclass(frozen=True)
class ClientInfo:
    version: str
    platform: str = ""

    def is_too_old(self, current: str = __version__) -> bool:
        return is_too_old(self.version, current)


def parse_client_header(value: Optional[str]) -> Optional[ClientInfo]:
    """Leniently parse ``"0.7.4; windows"``. None means a legacy/unknown client."""
    if not value:
        return None
    version, _, platform = str(value).partition(";")
    version = version.strip()
    if version_key(version) is None:
        return None
    platform = re.sub(r"[^\w .+()/-]", "", platform.strip())[:60]
    return ClientInfo(version=version[:32], platform=platform)


class ClientTooOld(Exception):
    """Raised to answer 426; ``create_app`` installs the handler that renders it."""

    def __init__(self, info: ClientInfo):
        super().__init__(info.version)
        self.info = info

    def payload(self) -> dict:
        floor = min_client_version()
        return {
            "detail": (
                f"This recorder (version {self.info.version}) is too old for this server. "
                f"Please update the recorder to version {floor} or newer "
                "(Settings, Check for updates). Your recording is safe on this computer and "
                "will upload after the update."
            ),
            "min_client_version": floor,
        }


class ClientVersionMiddleware:
    """Pure ASGI (so websockets pass through): parse the client header once and
    stash it on ``scope["state"]["client_info"]``. Nothing is recorded here; the
    live recorders registry (``recorders.py``) is fed by the recorder's own
    websocket, and the version lands in session meta on upload."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
            info = parse_client_header(headers.get(CLIENT_HEADER.lower()))
            scope.setdefault("state", {})["client_info"] = info
        await self.app(scope, receive, send)
