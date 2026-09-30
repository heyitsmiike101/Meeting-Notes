# COMPAT FIXTURE - do not edit. Verbatim copy of meeting_notes/client/logs.py from the 0.7.0 client
# (git cd67d09), with only the meeting_notes.* imports rewritten to be package-relative. Only SendResult/send_zip (the network part).
from __future__ import annotations

import logging
import socket

import httpx

from .api import ServerClient, ServerUnavailable  # compat fixture: import adjusted

log = logging.getLogger("compat.client.logs")


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
            return SendResult(False, "The server rejected your token.")
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
