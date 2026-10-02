"""Collect, redact, bundle and send the client's diagnostics.

Sources (what the Logs window lists): the rotating client log, the startup
error log, the audio-device diagnostic, the upload queue, the configuration
(token masked) and an About page. ``build_zip`` bundles all of them, redacted;
``send_zip`` uploads that bundle to the server's ``/v1/client-logs``.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import logging
import platform
import socket
import sys
import zipfile
from pathlib import Path
from typing import List, Optional, Tuple

import httpx

from meeting_notes import __version__
from meeting_notes import config as config_mod
from meeting_notes.client import logsetup
from meeting_notes.client.api import ServerClient, ServerUnavailable

log = logging.getLogger("meeting_notes.client.logs")

CLIENT_LOG = "client"
STARTUP = "startup"
AUDIO = "audio"
QUEUE = "queue"
CONFIG = "config"
ABOUT = "about"

# (key, title, file name used inside the zip)
SOURCES: List[Tuple[str, str, str]] = [
    (CLIENT_LOG, "Client log", "client.log"),
    (STARTUP, "Startup errors", "client-startup-error.log"),
    (AUDIO, "Audio devices", "audio-device-diagnostic.log"),
    (QUEUE, "Upload queue", "upload-queue.txt"),
    (CONFIG, "Configuration", "config.json"),
    (ABOUT, "About", "about.txt"),
]

NOTHING = "(nothing recorded yet)"


def _secrets() -> List[str]:
    token = (config_mod.server_settings().get("token") or "").strip()
    return [token] if token else []


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def client_log_files(base: Optional[Path] = None) -> List[Path]:
    """client.log then its rotated siblings (.1 is the newest of the old ones)."""
    base = base or logsetup.client_log_path()
    files = [base] + [base.with_name(f"{base.name}.{i}") for i in range(1, logsetup.BACKUP_COUNT + 1)]
    return [p for p in files if p.exists()]


def client_log_text() -> str:
    parts = []
    for path in client_log_files():
        parts.append(f"===== {path.name} =====\n{_read(path)}")
    return "\n".join(parts)


def _when(value) -> str:
    if not value:
        return "-"
    try:
        return dt.datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return "-"


def upload_queue_summary() -> str:
    from meeting_notes.client.queue import SessionQueue

    try:
        queue = SessionQueue.for_save_dir(config_mod.save_dir())
        entries = queue.pending()
    except Exception as exc:  # noqa: BLE001
        return f"Could not read the upload queue: {exc}"
    if not entries:
        return "No recordings are waiting to upload."
    lines = []
    for entry in entries:
        lines.append(f"Meeting     {Path(str(entry.get('session_dir') or entry['id'])).name}")
        lines.append(
            f"  Status      {entry.get('status', 'pending')} "
            f"(upload {entry.get('upload_state', '-')}, "
            f"transcription {entry.get('transcription_state', '-')})"
        )
        lines.append(f"  Attempts    {entry.get('attempts', 0)}")
        lines.append(f"  Last error  {entry.get('last_error') or '-'}")
        lines.append(f"  Next try    {_when(entry.get('next_attempt_at'))}")
        lines.append(f"  Queued      {_when(entry.get('enqueued_at'))}")
        lines.append("")
    return "\n".join(lines).rstrip()


def config_text() -> str:
    raw = _read(config_mod.config_path())
    if not raw:
        return "(no config file yet)"
    try:
        data = json.loads(raw)
    except ValueError:
        return logsetup.redact_text(raw, _secrets())
    server = data.get("server")
    if isinstance(server, dict) and server.get("token"):
        server["token"] = logsetup.MASK
    return logsetup.redact_text(json.dumps(data, indent=2), _secrets())


def about_text() -> str:
    return "\n".join(
        [
            f"Meeting Notes client  v{__version__}",
            f"Executable            {sys.executable}",
            f"Python                {platform.python_version()}",
            f"OS                    {platform.system()} {platform.release()} ({platform.version()})",
            f"Device                {socket.gethostname()}",
            f"Save folder           {config_mod.save_dir()}",
            f"Config                {config_mod.config_path()}",
            f"Logs folder           {logsetup.log_dir()}",
        ]
    )


def source_text(key: str) -> str:
    """The redacted text for one source (never empty)."""
    home = logsetup.app_data_dir()
    if key == CLIENT_LOG:
        text = client_log_text()
    elif key == STARTUP:
        text = _read(home / "client-startup-error.log")
    elif key == AUDIO:
        text = _read(home / "audio-device-diagnostic.log")
    elif key == QUEUE:
        text = upload_queue_summary()
    elif key == CONFIG:
        text = config_text()
    elif key == ABOUT:
        text = about_text()
    else:
        text = ""
    return logsetup.redact_text(text, _secrets()) or NOTHING


def default_zip_name(now: Optional[dt.datetime] = None) -> str:
    now = now or dt.datetime.now()
    device = "".join(c if c.isalnum() or c in "-_" else "-" for c in socket.gethostname()) or "device"
    return f"MeetingNotes-logs-{device}-{now:%Y%m%d-%H%M%S}.zip"


def build_zip(path: Optional[Path] = None) -> bytes:
    """Bundle every source, redacted. Also writes ``path`` when given."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for key, _title, name in SOURCES:
            archive.writestr(name, source_text(key))
    data = buffer.getvalue()
    if path is not None:
        Path(path).write_bytes(data)
    return data


class SendResult:
    def __init__(self, ok: bool, message: str, remote_id: str = ""):
        self.ok = ok
        self.message = message
        self.remote_id = remote_id


def send_zip(url: str, token: str, data: bytes, name: str, timeout: float = 30.0) -> SendResult:
    """POST the zip to /v1/client-logs. Blocking: call from a background thread."""
    if not (url or "").strip():
        return SendResult(False, "No server is configured (open Settings).")
    try:
        with ServerClient(url, token or None, timeout=timeout) as client:
            resp = client._request(  # noqa: SLF001 - the one place that knows this endpoint
                "POST",
                "/v1/client-logs",
                files={"file": (name, data, "application/zip")},
                data={"device": socket.gethostname()},
                headers=client._headers(),  # noqa: SLF001
            )
    except ServerUnavailable:
        return SendResult(False, f"Can't reach the server at {url}.")
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code in (404, 405):
            return SendResult(False, "This server doesn't accept logs yet. Use Save all as .zip.")
        if code in (401, 403):
            return SendResult(False, "The server rejected your password.")
        return SendResult(False, f"The server refused the logs (HTTP {code}).")
    except Exception as exc:  # noqa: BLE001
        return SendResult(False, f"Could not send: {exc}")
    remote_id = ""
    try:
        body = resp.json()
        remote_id = str(body.get("id") or body.get("log_id") or body.get("name") or "")
    except Exception:  # noqa: BLE001
        pass
    log.info("sent client logs to server (id=%s)", remote_id or "?")
    suffix = f" (id {remote_id})" if remote_id else ""
    return SendResult(True, f"Sent to the server{suffix}.", remote_id)
