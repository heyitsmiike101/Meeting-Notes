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
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .. import __version__

CLIENT_HEADER = "X-Meeting-Notes-Client"
SUPPORTED_CLIENT_WINDOW = 5

# Every shipped client version, oldest first. The running server's own version
# is appended automatically when missing, but tests/compat fails until the
# release is listed here on purpose.
RELEASES: List[str] = ["0.6.1", "0.7.0", "0.7.1", "0.7.2", "0.7.3", "0.7.4"]

_VERSION_RE = re.compile(r"^v?(\d+(?:\.\d+){0,3})")
_MAX_CLIENTS = 200
_TOUCH_INTERVAL = 60.0


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


class ClientRegistry:
    """Last-seen version and platform per recorder, in ``<data_root>/clients.json``.

    Recorders are keyed by the device name they send with uploads (hello,
    pipeline, log bundles). Requests that carry no device (job polling, history)
    update the device last seen from the same address, or an address-keyed row.
    Writes are throttled so polling never turns into disk churn.
    """

    def __init__(self, root):
        self.path = Path(root) / "clients.json"
        self._lock = threading.Lock()
        self._rows: Dict[str, dict] = {}
        self._by_address: Dict[str, str] = {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            rows = data.get("clients") if isinstance(data, dict) else None
            if isinstance(rows, dict):
                self._rows = {str(k): v for k, v in rows.items() if isinstance(v, dict)}
        except (OSError, ValueError):
            pass

    def touch(
        self,
        info: Optional[ClientInfo],
        address: str = "",
        device: str = "",
        now: Optional[float] = None,
    ) -> None:
        """Note a request from a recorder.

        ``info`` None (no header) is only recorded for a named device, as a
        legacy recorder, and never overwrites a version already known.
        """
        now = time.time() if now is None else now
        device = (device or "").strip()[:200]
        address = (address or "")[:64]
        with self._lock:
            if info is None and not device:
                return
            key = device or self._by_address.get(address) or (f"address {address}" if address else "")
            if not key:
                return
            stale = None
            if device and address:
                self._by_address[address] = device
                stale = self._rows.pop(f"address {address}", None)
            row = self._rows.get(key)
            changed = stale is not None
            if row is None:
                row = stale or {"device": key, "version": "", "platform": "", "first_seen": now, "last_seen": 0.0}
                row["device"] = key
                self._rows[key] = row
                changed = True
            if info is not None and (row.get("version"), row.get("platform")) != (info.version, info.platform):
                row["version"], row["platform"] = info.version, info.platform
                changed = True
            if address and row.get("address") != address:
                row["address"] = address
                changed = True
            if changed or now - float(row.get("last_seen") or 0) >= _TOUCH_INTERVAL:
                row["last_seen"] = now
                self._save_locked()

    def _save_locked(self) -> None:
        if len(self._rows) > _MAX_CLIENTS:
            keep = sorted(self._rows.items(), key=lambda kv: kv[1].get("last_seen", 0), reverse=True)
            self._rows = dict(keep[:_MAX_CLIENTS])
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".clients-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"clients": self._rows}, fh, separators=(",", ":"))
            os.replace(tmp, self.path)
        except OSError:
            pass

    def list(self, current: str = __version__) -> List[dict]:
        with self._lock:
            rows = [dict(r) for r in self._rows.values()]
        for row in rows:
            row["outdated"] = bool(row.get("version")) and is_too_old(row["version"], current)
        rows.sort(key=lambda r: r.get("last_seen", 0), reverse=True)
        return rows


class ClientVersionMiddleware:
    """Pure ASGI (so websockets pass through): parse the client header once,
    stash it on ``scope["state"]["client_info"]`` and note the caller."""

    def __init__(self, app, registry: ClientRegistry):
        self.app = app
        self.registry = registry

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
            info = parse_client_header(headers.get(CLIENT_HEADER.lower()))
            scope.setdefault("state", {})["client_info"] = info
            if info is not None:
                client = scope.get("client")
                try:
                    self.registry.touch(info, address=client[0] if client else "")
                except Exception:  # noqa: BLE001 - bookkeeping must never fail a request
                    pass
        await self.app(scope, receive, send)
