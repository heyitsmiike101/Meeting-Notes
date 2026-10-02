"""Report (never apply) whether this server is behind ``main`` on GitHub.

A background thread fetches ``meeting_notes/__init__.py`` from the public repo
at startup and then every six hours, parses ``__version__`` and keeps the
result in memory. Requests only ever read that cached result, so a slow or
unreachable GitHub can never delay a page. Any failure leaves the status
"unknown" (``latest`` is ``None``, ``update_available`` false) and is logged at
debug level. ``MEETING_NOTES_UPDATE_CHECK=0`` turns the whole thing off.

This is separate from the client update manifest (``/install/client-manifest.json``).
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
import urllib.request
from typing import Callable, Optional, Tuple

from meeting_notes import __version__

logger = logging.getLogger(__name__)

REPO_URL = "https://github.com/heyitsmiike101/Meeting-Notes"
CHANGELOG_URL = f"{REPO_URL}/blob/main/CHANGELOG.md"
VERSION_URL = "https://raw.githubusercontent.com/heyitsmiike101/Meeting-Notes/main/meeting_notes/__init__.py"
CHECK_INTERVAL_SECONDS = 6 * 60 * 60
FETCH_TIMEOUT_SECONDS = 5.0

_VERSION_RE = re.compile(r"""^__version__\s*=\s*['"]([^'"]+)['"]""", re.MULTILINE)


def parse_version(text: str) -> Optional[Tuple[int, ...]]:
    """``"0.7.10"`` -> ``(0, 7, 10)``. Pre-release suffixes are ignored; junk -> ``None``."""
    parts = []
    for piece in str(text or "").strip().lstrip("vV").split("."):
        match = re.match(r"\d+", piece)
        if not match:
            return None
        parts.append(int(match.group()))
    return tuple(parts) if parts else None


def is_newer(latest: str, current: str) -> bool:
    a, b = parse_version(latest), parse_version(current)
    if a is None or b is None:
        return False
    width = max(len(a), len(b))
    pad = lambda t: t + (0,) * (width - len(t))  # noqa: E731
    return pad(a) > pad(b)


def extract_version(source: str) -> Optional[str]:
    match = _VERSION_RE.search(source or "")
    return match.group(1) if match else None


def fetch_main_init() -> str:
    """Download ``meeting_notes/__init__.py`` from ``main`` (no auth, short timeout)."""
    request = urllib.request.Request(VERSION_URL, headers={"User-Agent": f"meeting-notes/{__version__}"})
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:  # noqa: S310
        return response.read(64 * 1024).decode("utf-8", errors="replace")


def update_check_enabled() -> bool:
    return os.environ.get("MEETING_NOTES_UPDATE_CHECK", "1").strip().lower() not in ("0", "false", "no", "off")


class UpdateChecker:
    def __init__(
        self,
        fetch: Callable[[], str] = fetch_main_init,
        *,
        current: str = __version__,
        interval: float = CHECK_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.time,
        enabled: Optional[bool] = None,
    ) -> None:
        self._fetch = fetch
        self.current = current
        self.interval = interval
        self._clock = clock
        self.enabled = update_check_enabled() if enabled is None else enabled
        self._lock = threading.Lock()
        self._latest: Optional[str] = None
        self._checked_at: Optional[float] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def check_now(self) -> None:
        """One fetch; a failure keeps the previous result (or unknown) and never raises."""
        if not self.enabled:
            return
        try:
            latest = extract_version(self._fetch())
            if latest is None or parse_version(latest) is None:
                raise ValueError("no __version__ found")
        except Exception as exc:  # noqa: BLE001 -- unknown is the only failure mode
            logger.debug("update check failed: %s", exc)
            return
        with self._lock:
            self._latest = latest
            self._checked_at = self._clock()

    def status(self) -> dict:
        with self._lock:
            latest, checked_at = self._latest, self._checked_at
        return {
            "current": self.current,
            "latest": latest,
            "update_available": bool(latest) and is_newer(latest, self.current),
            "checked_at": checked_at,
            "repo_url": REPO_URL,
        }

    def _run(self) -> None:
        while not self._stop.is_set():
            self.check_now()
            if self._stop.wait(self.interval):
                break

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="update-check", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 1.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)


# The checker the web pages read from (set by create_app). A getter keeps the
# update notice out of every render_* signature.
_active: Optional[UpdateChecker] = None


def set_active(checker: Optional[UpdateChecker]) -> None:
    global _active
    _active = checker


def current_status() -> Optional[dict]:
    """Cached status when an update is available, else ``None``."""
    checker = _active
    if checker is None:
        return None
    status = checker.status()
    return status if status["update_available"] else None
