"""HTTP calls to the LAN transcription server, for uploads and the final pass.

Two distinct outcomes matter to callers, and this module is careful to keep
them distinguishable: the *server is unreachable* (no route, refused
connection, timed out) versus *the server is reachable and said no* (bad
request, 500, etc). The first means "queue this for later and keep recording
regardless" -- see ``meeting_notes.client.queue`` -- and the second is a real
error that retrying blindly won't fix. Only the first raises
``ServerUnavailable``; the second surfaces as an ordinary ``httpx`` exception
(most often ``httpx.HTTPStatusError`` after ``raise_for_status``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterator, Optional

import httpx

from meeting_notes import wire

# Read/write in chunks this big so uploading a multi-hour, multi-hundred-MB
# recording never has to hold more than one chunk in memory at a time.
_UPLOAD_CHUNK_BYTES = 1 << 20


class ServerUnavailable(Exception):
    """The server could not be reached at all (down, unplugged, wrong LAN).

    Distinct from an HTTP error response, which means the server *is* there
    and rejected the request -- that's a real problem to report, not a queue
    to fall back to.
    """


def _iter_file(path: Path, chunk_size: int = _UPLOAD_CHUNK_BYTES) -> Iterator[bytes]:
    with open(path, "rb") as fh:
        while True:
            data = fh.read(chunk_size)
            if not data:
                return
            yield data


class ServerClient:
    """Sync HTTP client for the endpoints ``meeting_notes.wire`` defines."""

    def __init__(self, base_url: str, token: Optional[str] = None, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ServerClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _headers(self, **extra: str) -> Dict[str, str]:
        headers = dict(wire.auth_headers(self.token))
        headers.update(extra)
        return headers

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            resp = self._client.request(method, path, **kwargs)
        except httpx.TransportError as exc:
            # Covers connection refused, DNS failure, and every flavour of
            # timeout -- all of them mean "couldn't reach the server", which
            # is exactly the condition callers need to queue-and-continue on.
            raise ServerUnavailable(f"{method} {path} failed: {exc}") from exc
        resp.raise_for_status()
        return resp

    # -- calls ----------------------------------------------------------

    def health(self) -> Dict[str, Any]:
        return self._request("GET", wire.HEALTH, headers=self._headers()).json()

    def upload_track(self, session_id: str, track: str, pcm_path: Path, frames: int) -> Dict[str, Any]:
        """Stream ``pcm_path`` (16 kHz mono int16 PCM) to the server.

        Reads the file in fixed-size chunks rather than loading it whole --
        a completed recording can be well over 100 MB, and this runs on the
        same laptop that just spent the meeting recording it.
        """
        path = wire.track_upload_path(session_id, track)
        headers = self._headers(**{"Content-Type": "application/octet-stream"})
        resp = self._request(
            "PUT",
            path,
            content=_iter_file(Path(pcm_path)),
            params={"frames": frames},
            headers=headers,
        )
        return resp.json() if resp.content else {}

    def finalize(
        self,
        session_id: str,
        meta: Dict[str, Any],
        timing: Dict[str, Any],
        settings: Dict[str, Any],
    ) -> str:
        """Ask the server to run the authoritative transcription pass. Returns job_id."""
        path = wire.finalize_path(session_id)
        body = {"meta": meta, "timing": timing, "settings": settings}
        resp = self._request("POST", path, json=body, headers=self._headers())
        data = resp.json()
        return data["job_id"]

    def job(self, job_id: str) -> Dict[str, Any]:
        return self._request("GET", wire.job_path(job_id), headers=self._headers()).json()

    def transcript(self, job_id: str) -> Dict[str, Any]:
        return self._request(
            "GET", wire.job_transcript_path(job_id), headers=self._headers()
        ).json()
