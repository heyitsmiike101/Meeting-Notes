"""A real client log: rotating file, uncaught-exception hooks, secret masking.

The packaged client has no console, so without this a failure leaves no trace.
Everything is written to ``~/.meeting-notes/logs/client.log`` (1 MB x 5). The
server token must never reach a log line: a ``logging.Filter`` masks the
configured token and any ``Bearer`` header text, defensively, on every record.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import re
import sys
import threading
from pathlib import Path
from typing import Iterable, List, Optional

LOG_DIR_NAME = "logs"
LOG_FILE_NAME = "client.log"
MAX_BYTES = 1_000_000
BACKUP_COUNT = 5
ROOT_LOGGER = "meeting_notes"

_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+")
_TOKEN_PARAM = re.compile(r"(?i)([?&]token=)[^&\s'\"]+")
_TOKEN_JSON = re.compile(r"""(?i)(["']token["']\s*:\s*["'])[^"']*(["'])""")
MASK = "***"


def app_data_dir() -> Path:
    return Path.home() / ".meeting-notes"


def log_dir() -> Path:
    override = os.environ.get("MEETING_NOTES_LOG_DIR")
    return Path(override) if override else app_data_dir() / LOG_DIR_NAME


def client_log_path() -> Path:
    return log_dir() / LOG_FILE_NAME


def redact_text(text: str, secrets: Iterable[str] = ()) -> str:
    """Mask the given secrets, Bearer credentials, ?token= and JSON "token" values."""
    if not text:
        return text
    for secret in secrets:
        if secret and len(secret) >= 3:
            text = text.replace(secret, MASK)
    text = _BEARER.sub(lambda m: m.group(1) + MASK, text)
    text = _TOKEN_PARAM.sub(lambda m: m.group(1) + MASK, text)
    text = _TOKEN_JSON.sub(lambda m: m.group(1) + MASK + m.group(2), text)
    return text


class SecretFilter(logging.Filter):
    """Masks secrets in the fully formatted message of every record."""

    def __init__(self, secrets: Optional[List[str]] = None):
        super().__init__()
        self.secrets: List[str] = [s for s in (secrets or []) if s]

    def add_secret(self, secret: str) -> None:
        if secret and secret not in self.secrets:
            self.secrets.append(secret)

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001
            message = str(record.msg)
        record.msg = redact_text(message, self.secrets)
        record.args = ()
        if record.exc_info:
            # Format now so the traceback text can be masked too.
            formatter = logging.Formatter()
            record.exc_text = redact_text(formatter.formatException(record.exc_info), self.secrets)
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redact_text(record.exc_text, self.secrets)
        return True


_secret_filter: Optional[SecretFilter] = None
_configured_path: Optional[Path] = None


def _configured_secrets() -> List[str]:
    try:
        from meeting_notes import config as config_mod

        token = (config_mod.server_settings().get("token") or "").strip()
        return [token] if token else []
    except Exception:  # noqa: BLE001
        return []


def register_secret(secret: str) -> None:
    """Teach the filter a token that appeared after startup (Settings saved)."""
    if _secret_filter is not None:
        _secret_filter.add_secret(secret)


def setup_logging(path: Optional[Path] = None, level: int = logging.INFO) -> Optional[Path]:
    """Attach the rotating file handler and the exception hooks. Idempotent.

    Returns the log path, or None if the log could not be opened (logging must
    never stop the app from starting).
    """
    global _secret_filter, _configured_path
    target = Path(path) if path is not None else client_log_path()
    logger = logging.getLogger(ROOT_LOGGER)
    if _configured_path == target and any(
        getattr(h, "_meeting_notes_client", False) for h in logger.handlers
    ):
        return target
    teardown_logging()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            target, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
        )
    except OSError:
        return None
    handler._meeting_notes_client = True  # type: ignore[attr-defined]
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s [%(threadName)s] %(message)s")
    )
    _secret_filter = SecretFilter(_configured_secrets())
    handler.addFilter(_secret_filter)
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    _configured_path = target
    install_excepthooks()
    return target


def teardown_logging() -> None:
    """Detach and close our handler (used by tests and on reconfiguration)."""
    global _secret_filter, _configured_path
    logger = logging.getLogger(ROOT_LOGGER)
    for handler in list(logger.handlers):
        if getattr(handler, "_meeting_notes_client", False):
            logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:  # noqa: BLE001
                pass
    _secret_filter = None
    _configured_path = None


_hooks_installed = False


def install_excepthooks() -> None:
    """Send uncaught exceptions (main thread and other threads) to the log."""
    global _hooks_installed
    if _hooks_installed:
        return
    _hooks_installed = True
    previous_sys = sys.excepthook
    previous_thread = threading.excepthook
    crash_log = logging.getLogger("meeting_notes.crash")

    def sys_hook(exc_type, exc, tb):
        try:
            crash_log.critical("Uncaught exception", exc_info=(exc_type, exc, tb))
        except Exception:  # noqa: BLE001
            pass
        previous_sys(exc_type, exc, tb)

    def thread_hook(args):
        try:
            name = args.thread.name if args.thread else "?"
            crash_log.critical(
                "Uncaught exception in thread %s",
                name,
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )
        except Exception:  # noqa: BLE001
            pass
        previous_thread(args)

    sys.excepthook = sys_hook
    threading.excepthook = thread_hook
