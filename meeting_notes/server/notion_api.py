"""A small, careful client for the Notion REST API.

Only the handful of endpoints the Notion export needs, with the behaviour the
API documentation asks of an integration:

* a pinned ``Notion-Version`` header (:data:`NOTION_VERSION`);
* roughly 3 requests per second (the documented average for most plans), by
  spacing requests at least :data:`MIN_INTERVAL` apart;
* ``429`` handling that honours ``Retry-After``;
* a bounded retry of transient (5xx / network) failures;
* every failure surfaced as a :class:`NotionError` with a human-readable
  ``reason`` and a ``retryable`` flag the job queue uses to decide whether to
  try again later.

The integration token is a secret. It is only ever placed in the
``Authorization`` header, and every message that leaves this module (errors,
log lines) is scrubbed of it first.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Callable, Iterator, Optional

import httpx

logger = logging.getLogger("meeting_notes.server.notion")

API_BASE = "https://api.notion.com"
# 2026-03-11 is the current version at the time of writing. It is the one that
# (a) replaced the flat ``after`` parameter of "append block children" with the
# ``position`` object ({"type": "start" | "end" | "after_block"}) that the
# newest-first ordering relies on, and (b) renamed ``archived`` to ``in_trash``.
NOTION_VERSION = "2026-03-11"
MIN_INTERVAL = 0.35  # seconds between requests: ~2.9 req/s
MAX_429_RETRIES = 5
MAX_TRANSIENT_RETRIES = 2
MAX_RETRY_AFTER = 60.0
REQUEST_TIMEOUT = 30.0

_ID_RE = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{32}(?![0-9a-fA-F])")
_UUID_RE = re.compile(
    r"(?<![0-9a-fA-F])[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}(?![0-9a-fA-F])"
)


def _find_id(text: str):
    """The page id in ``text``: a dashed UUID, else a bare 32-hex run. (A title slug such as
    ``Page-Title-<id>`` must not lend its hex-looking letters to the id.)"""
    uuids = _UUID_RE.findall(text)
    if uuids:
        return uuids[-1].replace("-", "").lower()
    bare = _ID_RE.findall(text)
    return bare[-1].lower() if bare else None


class NotionError(Exception):
    """A failed Notion call. ``str(exc)`` is safe to show to the user."""

    def __init__(self, reason: str, *, status: Optional[int] = None, code: Optional[str] = None,
                 retryable: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.status = status
        self.code = code
        self.retryable = retryable

    @property
    def not_found(self) -> bool:
        return self.status == 404 or self.code == "object_not_found"


class NotConnected(NotionError):
    def __init__(self):
        super().__init__("Notion is not connected. Add the integration token in Settings.", retryable=False)


def normalize_page_id(value: str) -> str:
    """Accept a Notion page URL or a raw id (with or without dashes) and return
    the 32-hex id in lowercase. Raises ``ValueError`` when none can be found."""
    text = str(value or "").strip()
    if not text:
        raise ValueError("Enter a Notion page link or id.")
    candidate = text
    if "://" in text or text.lower().startswith("notion.so") or "/" in text:
        from urllib.parse import urlparse

        parsed = urlparse(text if "://" in text else "https://" + text)
        host = (parsed.hostname or "").lower()
        if not (host == "notion.so" or host.endswith(".notion.so") or host.endswith("notion.site")
                or host == "app.notion.com" or host.endswith(".notion.com")):
            raise ValueError("That does not look like a Notion page link.")
        candidate = parsed.path
        # ``?p=<id>`` (database-style links) wins over the path when present.
        for part in parsed.query.split("&"):
            if part.startswith("p=") and _find_id(part[2:]):
                candidate = part[2:]
    found = _find_id(candidate)
    if not found:
        raise ValueError("Could not find a Notion page id in that value.")
    return found


def dashed(page_id: str) -> str:
    h = page_id.replace("-", "").lower()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def _friendly(status: int, code: str, message: str) -> str:
    if status == 401 or code == "unauthorized":
        return "Notion rejected the integration token (it may have been revoked). Reconnect it in Settings."
    if code == "restricted_resource" or status == 403:
        return ("The integration is not allowed to do that. Share the parent page with it "
                "(page menu, Connections) and make sure it can insert content.")
    if code == "object_not_found" or status == 404:
        return ("Notion could not find that page or block. If the page exists, share it with the "
                "integration (page menu, Connections).")
    if code == "rate_limited" or status == 429:
        return "Notion is rate limiting requests. It will be retried."
    if code == "validation_error":
        return f"Notion rejected the request: {message}"
    if status >= 500:
        return f"Notion had a temporary problem (HTTP {status}). It will be retried."
    return f"Notion returned an error ({code or status}): {message}"


class NotionClient:
    """Thread-safe (calls are serialized so the rate limit is shared)."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str = API_BASE,
        transport: Optional[httpx.BaseTransport] = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        min_interval: float = MIN_INTERVAL,
    ):
        self._token = token
        self._sleep = sleep
        self._clock = clock
        self._min_interval = min_interval
        self._last = None
        self._lock = threading.Lock()
        self._http = httpx.Client(base_url=base_url, transport=transport, timeout=REQUEST_TIMEOUT)

    def close(self) -> None:
        self._http.close()

    # -- plumbing ------------------------------------------------------------

    def _scrub(self, text: str) -> str:
        text = str(text)
        if self._token:
            text = text.replace(self._token, "[token]")
        return re.sub(r"(secret_|ntn_)[A-Za-z0-9]{8,}", "[token]", text)

    def _pace(self) -> None:
        if self._last is not None:
            wait = self._min_interval - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
        self._last = self._clock()

    def request(self, method: str, path: str, json: Optional[dict] = None, params: Optional[dict] = None) -> dict:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }
        rate_retries = transient = 0
        with self._lock:
            while True:
                self._pace()
                try:
                    response = self._http.request(method, path, json=json, params=params, headers=headers)
                except httpx.HTTPError as exc:
                    if transient < MAX_TRANSIENT_RETRIES:
                        transient += 1
                        self._sleep(float(2 ** transient))
                        continue
                    raise NotionError(
                        f"Could not reach Notion ({self._scrub(type(exc).__name__)}). It will be retried.",
                        retryable=True,
                    ) from None
                if response.status_code == 429:
                    if rate_retries < MAX_429_RETRIES:
                        rate_retries += 1
                        try:
                            delay = float(response.headers.get("Retry-After", "1"))
                        except ValueError:
                            delay = 1.0
                        self._sleep(min(max(delay, 0.0), MAX_RETRY_AFTER))
                        self._last = self._clock()
                        continue
                    raise NotionError(_friendly(429, "rate_limited", ""), status=429, code="rate_limited",
                                      retryable=True)
                if response.status_code >= 500:
                    if transient < MAX_TRANSIENT_RETRIES:
                        transient += 1
                        self._sleep(float(2 ** transient))
                        continue
                    raise NotionError(_friendly(response.status_code, "", ""), status=response.status_code,
                                      retryable=True)
                if response.status_code >= 400:
                    try:
                        body = response.json()
                    except ValueError:
                        body = {}
                    code = str(body.get("code") or "")
                    message = self._scrub(body.get("message") or "")
                    raise NotionError(
                        _friendly(response.status_code, code, message),
                        status=response.status_code,
                        code=code,
                        # A write conflict is transient; everything else 4xx is not.
                        retryable=code == "conflict_error",
                    )
                try:
                    return response.json()
                except ValueError:
                    return {}

    # -- endpoints -----------------------------------------------------------

    def me(self) -> dict:
        """``GET /v1/users/me``: the bot user (and its workspace name)."""
        return self.request("GET", "/v1/users/me")

    def get_page(self, page_id: str) -> dict:
        return self.request("GET", f"/v1/pages/{page_id}")

    def create_page(self, parent_page_id: str, title: str) -> dict:
        return self.request("POST", "/v1/pages", json={
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "properties": {"title": {"title": [{"type": "text", "text": {"content": title}}]}},
        })

    def get_block(self, block_id: str) -> dict:
        return self.request("GET", f"/v1/blocks/{block_id}")

    def iter_children(self, block_id: str) -> Iterator[dict]:
        cursor = None
        while True:
            params = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            data = self.request("GET", f"/v1/blocks/{block_id}/children", params=params)
            for item in data.get("results") or []:
                yield item
            if not data.get("has_more") or not data.get("next_cursor"):
                return
            cursor = data["next_cursor"]

    def append_children(self, block_id: str, children: list, position: Optional[dict] = None) -> dict:
        body: dict = {"children": children}
        if position:
            body["position"] = position
        return self.request("PATCH", f"/v1/blocks/{block_id}/children", json=body)

    def update_block(self, block_id: str, body: dict) -> dict:
        return self.request("PATCH", f"/v1/blocks/{block_id}", json=body)

    def delete_block(self, block_id: str) -> dict:
        return self.request("DELETE", f"/v1/blocks/{block_id}")


def is_trashed(obj: dict) -> bool:
    """A page/block that was deleted in Notion (``in_trash``, or ``archived`` on older versions)."""
    return bool(obj.get("in_trash") or obj.get("is_trashed") or obj.get("archived"))
