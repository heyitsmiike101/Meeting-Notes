"""Is the AI provider's login still working? (The "AI login" check.)

``claude auth status`` (and ``codex login status``) only report whether saved credentials exist, so an expired
OAuth session went unnoticed: every notes job then failed one by one. This module keeps one answer for the server:

* A notes or split job that fails the way an expired login does (``ai_login.is_login_failure``) marks the login
  **expired**. The notes job is put back in the queue rather than failed, and the claim endpoint hands out no work
  while the login is expired, so queued meetings wait instead of failing.
* A background thread asks the bridge for a tiny real request (``/v1/bridge/control/verify``) every few hours, and
  every few minutes while expired. ``ok`` clears it, ``login_failed`` sets it; anything else (no network, an older
  bridge without the endpoint) changes nothing.
* A notes job that completes, or a finished re-login in Settings, clears it too.

Every page shows a banner while it is expired (``web.py``), ``/health`` carries ``ai_login``, and Settings → AI shows
the last check and has a "Check now" button. The state is saved in ``ai_health.json`` under the data root, so a
restart does not forget it. Only codex and claude have a login; ollama and disabled are never "expired".
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from .worker import BackgroundWorker

logger = logging.getLogger(__name__)

FILE_NAME = "ai_health.json"
CHECK_INTERVAL_SECONDS = 6 * 60 * 60  # a real request every six hours while the login works
EXPIRED_CHECK_INTERVAL_SECONDS = 5 * 60  # and every five minutes while it does not
LOGIN_PROVIDERS = ("claude", "codex")
PROVIDER_NAMES = {"claude": "Claude", "codex": "ChatGPT (Codex)"}

_lock = threading.Lock()


def _path(root: Path) -> Path:
    return Path(root) / FILE_NAME


def _read(root: Path) -> dict:
    try:
        data = json.loads(_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(root: Path, data: dict) -> None:
    path = _path(root)
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        logger.warning("ai login: could not save %s", path, exc_info=True)


def state(root: Path, provider: str) -> dict:
    """The saved answer for ``provider``: ``expired`` (bool), ``since``, ``error``, ``checked_at``, ``result``."""
    with _lock:
        data = _read(root)
    if provider not in LOGIN_PROVIDERS or data.get("provider") != provider:
        return {"provider": provider, "expired": False, "since": None, "error": None,
                "checked_at": data.get("checked_at") if data.get("provider") == provider else None, "result": None}
    return {
        "provider": provider,
        "expired": bool(data.get("expired")),
        "since": data.get("since"),
        "error": data.get("error"),
        "checked_at": data.get("checked_at"),
        "result": data.get("result"),
    }


def is_expired(root: Path, provider: str) -> bool:
    return state(root, provider)["expired"]


def mark_expired(root: Path, provider: str, error: str = "") -> None:
    if provider not in LOGIN_PROVIDERS:
        return
    with _lock:
        data = _read(root)
        if not (data.get("provider") == provider and data.get("expired")):
            logger.warning("ai login: %s login no longer works; notes are on hold until it is reconnected", provider)
            data = {"provider": provider, "expired": True, "since": time.time(),
                    "checked_at": data.get("checked_at") if data.get("provider") == provider else None}
        data["error"] = str(error or "")[:300] or data.get("error")
        _write(root, data)


def mark_ok(root: Path, provider: str, *, checked: bool = False) -> None:
    """The login works again (a job completed, a re-login finished, or a test request went through)."""
    if provider not in LOGIN_PROVIDERS:
        return
    with _lock:
        data = _read(root)
        same = data.get("provider") == provider
        if same and data.get("expired"):
            logger.info("ai login: %s login works again; held notes continue", provider)
        new = {"provider": provider, "expired": False, "since": None, "error": None,
               "checked_at": data.get("checked_at") if same else None, "result": data.get("result") if same else None}
        if checked:
            new.update(checked_at=time.time(), result="ok")
        if new != data:
            _write(root, new)


def record_check(root: Path, provider: str, result: str, detail: Optional[str] = None) -> None:
    """Apply one test request's answer: ``ok``, ``login_failed`` or ``error`` (which changes nothing but the time)."""
    if provider not in LOGIN_PROVIDERS:
        return
    if result == "ok":
        mark_ok(root, provider, checked=True)
        return
    if result == "login_failed":
        mark_expired(root, provider, detail or "the test request was refused: the login has expired")
    with _lock:
        data = _read(root)
        if data.get("provider") != provider:
            data = {"provider": provider, "expired": False}
        data.update(checked_at=time.time(), result=result)
        _write(root, data)


class LoginChecker(BackgroundWorker):
    """Background thread: a real test request through the bridge, often while the login is expired, rarely otherwise.

    ``verify(provider)`` returns the bridge's answer dict (``result``, ``detail``) or ``None`` when the bridge could
    not be asked; ``provider()`` returns the configured AI provider.
    """

    thread_name = "ai-login-check"
    stop_timeout = 1.0

    def __init__(
        self,
        root: Path,
        provider: Callable[[], str],
        verify: Callable[[str], Optional[dict]],
        *,
        enabled: bool = True,
        interval: float = CHECK_INTERVAL_SECONDS,
        expired_interval: float = EXPIRED_CHECK_INTERVAL_SECONDS,
    ) -> None:
        super().__init__()
        self.root = Path(root)
        self._provider = provider
        self._verify = verify
        self.enabled = enabled
        self.interval = interval
        self.expired_interval = expired_interval

    def check_now(self) -> Optional[dict]:
        """One test request; the answer is recorded and returned (``None`` when there was nothing to ask)."""
        try:
            provider = self._provider()
        except Exception:  # noqa: BLE001 - settings unreadable: try again next time
            return None
        if provider not in LOGIN_PROVIDERS:
            return None
        try:
            answer = self._verify(provider)
        except Exception as exc:  # noqa: BLE001 - the check must never take the server down
            logger.debug("ai login check failed: %s", exc)
            answer = None
        if not isinstance(answer, dict) or answer.get("result") not in ("ok", "login_failed", "error"):
            return None
        record_check(self.root, provider, str(answer["result"]), answer.get("detail"))
        return answer

    def _next_wait(self) -> float:
        try:
            provider = self._provider()
        except Exception:  # noqa: BLE001
            return self.interval
        return self.expired_interval if is_expired(self.root, provider) else self.interval

    def _run(self) -> None:
        # The first check waits a minute: the bridge container starts alongside the server.
        if self._stop.wait(60):
            return
        while not self._stop.is_set():
            self.check_now()
            self._wake.clear()
            deadline = time.monotonic() + self._next_wait()
            while not self._stop.is_set() and time.monotonic() < deadline:
                if self._wake.wait(min(30.0, max(0.0, deadline - time.monotonic()))):
                    self._wake.clear()
                    deadline = min(deadline, time.monotonic() + self.expired_interval)

    def start(self) -> None:
        if self.enabled:  # a disabled checker (tests, no provider login) never runs
            super().start()


_active_root: Optional[Path] = None


def set_active(root: Optional[Path]) -> None:
    """The data root the page banner reads (one server per process; ``create_app`` sets it)."""
    global _active_root
    _active_root = Path(root) if root is not None else None


def active_banner() -> Optional[dict]:
    """``banner_state`` for the active server's configured provider, or ``None`` (never raises)."""
    root = _active_root
    if root is None:
        return None
    try:
        from meeting_notes.server import settings as settings_mod

        return banner_state(root, settings_mod.load_settings(root).ai_provider)
    except Exception:  # noqa: BLE001 - a banner must never break a page
        return None


def banner_state(root: Path, provider: str) -> Optional[dict]:
    """What the page banner needs while the login is expired, else ``None``."""
    current = state(root, provider)
    if not current["expired"]:
        return None
    return {"provider": provider, "name": PROVIDER_NAMES.get(provider, provider), "since": current["since"]}


def as_public(root: Path, provider: str) -> dict[str, Any]:
    """For ``/health`` and the Settings page: no error text beyond the redacted excerpt."""
    current = state(root, provider)
    applies = provider in LOGIN_PROVIDERS
    return {
        "provider": provider,
        "applies": applies,
        "expired": current["expired"],
        "since": current["since"],
        "checked_at": current["checked_at"],
        "last_result": current["result"],
        "error": current["error"] if current["expired"] else None,
    }
