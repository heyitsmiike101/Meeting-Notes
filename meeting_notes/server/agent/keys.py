"""Per-agent API keys, stored under the data root next to ``settings.json``.

Deliberately a separate credential from ``MEETING_NOTES_TOKEN``: the shared web
and recorder token never grants agent access, and an agent key never grants web
or recorder access (``auth.py`` compares only against the env token, and nothing
here ever looks at it).

A key is ``mnk_`` + ``secrets.token_urlsafe(24)``. The plaintext is returned
exactly once by :meth:`AgentKeyStore.create`; only its SHA-256 hash and a
12-character display prefix are persisted. Writes are atomic
(write-temp-then-rename, the same helper the store uses) under one lock.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
import uuid
from pathlib import Path
from typing import List, Optional

from ..store import _atomic_write_json
from .errors import AgentError

KEY_PREFIX = "mnk_"
SCOPES = ("read", "write")
DEFAULT_SCOPES = ("read",)
# last_used_at is best-effort: an agent polling every few seconds must not
# rewrite the key file on every request.
_LAST_USED_WRITE_INTERVAL = 60.0


def hash_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def normalize_scopes(scopes) -> List[str]:
    if scopes is None:
        return list(DEFAULT_SCOPES)
    if isinstance(scopes, str):
        scopes = [s for s in scopes.replace(",", " ").split() if s]
    scopes = list(scopes)
    if not scopes:
        raise ValueError("at least one scope is required")
    out: List[str] = []
    for scope in scopes:
        if scope not in SCOPES:
            raise ValueError(f"unknown scope {scope!r}; must be one of {', '.join(SCOPES)}")
        if scope not in out:
            out.append(scope)
    # write implies the ability to read what you are writing about.
    if "write" in out and "read" not in out:
        out.insert(0, "read")
    return out


def public_view(record: dict) -> dict:
    """A key record safe to show anywhere (never includes the hash)."""
    return {
        "id": record["id"],
        "name": record["name"],
        "prefix": record["prefix"],
        "scopes": list(record["scopes"]),
        "created_at": record["created_at"],
        "last_used_at": record.get("last_used_at"),
        "revoked_at": record.get("revoked_at"),
    }


class AgentKeyStore:
    def __init__(self, root):
        self.path = Path(root) / "agent_keys.json"
        self._lock = threading.RLock()
        self._last_flush: dict = {}

    # -- persistence -------------------------------------------------------

    def _load(self) -> List[dict]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        keys = payload.get("keys") if isinstance(payload, dict) else None
        return [k for k in keys if isinstance(k, dict)] if isinstance(keys, list) else []

    def _save(self, keys: List[dict]) -> None:
        _atomic_write_json(self.path, {"keys": keys})

    # -- management --------------------------------------------------------

    def create(self, name: str, scopes=None) -> dict:
        """Mint a key. Returns the public view plus ``key`` (plaintext, once)."""
        name = (name or "").strip()
        if not name:
            raise ValueError("name is required")
        if len(name) > 80:
            raise ValueError("name must be 80 characters or fewer")
        scope_list = normalize_scopes(scopes)
        plaintext = KEY_PREFIX + secrets.token_urlsafe(24)
        record = {
            "id": uuid.uuid4().hex[:12],
            "name": name,
            "prefix": plaintext[:12],
            "hash": hash_key(plaintext),
            "scopes": scope_list,
            "created_at": time.time(),
            "last_used_at": None,
            "revoked_at": None,
        }
        with self._lock:
            keys = self._load()
            keys.append(record)
            self._save(keys)
        return {**public_view(record), "key": plaintext}

    def list(self) -> List[dict]:
        with self._lock:
            return [public_view(k) for k in self._load()]

    def revoke(self, ident: str) -> Optional[dict]:
        """Revoke by id or display prefix. Returns the record, or None if unknown."""
        with self._lock:
            keys = self._load()
            for record in keys:
                if ident in (record.get("id"), record.get("prefix")):
                    if not record.get("revoked_at"):
                        record["revoked_at"] = time.time()
                        self._save(keys)
                    return public_view(record)
        return None

    # -- authentication ----------------------------------------------------

    def authenticate(self, plaintext: Optional[str]) -> Optional[dict]:
        """The stored record for a plaintext key (revoked ones included), or None."""
        if not plaintext or not plaintext.startswith(KEY_PREFIX):
            return None
        digest = hash_key(plaintext)
        with self._lock:
            keys = self._load()
            found = None
            for record in keys:
                # compare_digest on every record: no early exit that could leak
                # which stored hash a guess was closest to.
                if hmac.compare_digest(str(record.get("hash", "")), digest):
                    found = record
            if found is None:
                return None
            if not found.get("revoked_at"):
                now = time.time()
                found["last_used_at"] = now
                if now - self._last_flush.get(found["id"], 0.0) >= _LAST_USED_WRITE_INTERVAL:
                    self._last_flush[found["id"]] = now
                    self._save(keys)
            return dict(found)


def resolve_access(keys: AgentKeyStore, bearer: Optional[str], scope: str = "read") -> dict:
    """The single access decision, used by BOTH the REST dependency and every
    MCP tool. Returns the key record or raises :class:`AgentError`.

    401 = no/unknown/revoked credential, 403 = valid key lacking the scope.
    """
    if not bearer:
        raise AgentError(
            401, "api_key_required", "an agent API key is required (Authorization: Bearer mnk_...)"
        )
    record = keys.authenticate(bearer)
    if record is None:
        raise AgentError(401, "invalid_api_key", "unknown API key")
    if record.get("revoked_at"):
        raise AgentError(401, "api_key_revoked", "this API key has been revoked")
    if scope not in record.get("scopes", []):
        raise AgentError(403, "scope_missing", f"this key lacks the '{scope}' scope")
    return record
