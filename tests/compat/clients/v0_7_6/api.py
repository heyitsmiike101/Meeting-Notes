# COMPAT FIXTURE - do not edit. Verbatim copy of meeting_notes/client/api.py from the 0.7.6 client
# (release/0.7.6), with only the meeting_notes.* imports rewritten to be package-relative.
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
from typing import Any, Callable, Dict, Iterator, Optional, Union
from urllib.parse import quote

import httpx

from . import remote, wire
from . import identity, version_gate

# Read/write in chunks this big so uploading a multi-hour, multi-hundred-MB
# recording never has to hold more than one chunk in memory at a time.
_UPLOAD_CHUNK_BYTES = 1 << 20
# A completed recording can be hundreds of MB.  Keep connection/pool failure
# detection responsive while allowing a slow-but-live LAN to spend minutes
# writing the body or waiting for server-side decoding.
UPLOAD_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=120.0, pool=10.0)

# The server owns decoding and transcription for an imported recording.  Keep
# this list deliberately small and explicit: it is shared by the API and the
# file-picker so a typo cannot result in an upload which the server cannot
# decode.  The endpoint accepts the original bytes; the Windows client does
# not ship a codec or run transcription locally.
SUPPORTED_RECORDING_TYPES = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/opus",
    ".aac": "audio/aac",
    ".webm": "audio/webm",
}


def recording_content_type(path: Path) -> str:
    """Return the supported MIME type for an imported recording.

    The upload API intentionally rejects unknown extensions before opening a
    connection.  This gives the UI a useful error and keeps an arbitrary file
    from being sent to the server as if it were audio.
    """
    suffix = Path(path).suffix.lower()
    try:
        return SUPPORTED_RECORDING_TYPES[suffix]
    except KeyError as exc:
        supported = ", ".join(sorted(SUPPORTED_RECORDING_TYPES))
        raise ValueError(f"unsupported audio format {suffix or '(none)'}; expected {supported}") from exc


class ServerUnavailable(Exception):
    """The server could not be reached at all (down, unplugged, wrong LAN).

    Distinct from an HTTP error response, which means the server *is* there
    and rejected the request -- that's a real problem to report, not a queue
    to fall back to.
    """


def _iter_file(
    path: Path,
    chunk_size: int = _UPLOAD_CHUNK_BYTES,
    on_chunk: Optional[Callable[[int, int], None]] = None,
) -> Iterator[bytes]:
    """Yield a file in bounded chunks, optionally reporting byte progress."""
    total = Path(path).stat().st_size
    sent = 0
    with open(path, "rb") as fh:
        while True:
            data = fh.read(chunk_size)
            if not data:
                return
            sent += len(data)
            if on_chunk is not None:
                on_chunk(sent, total)
            yield data


class ServerClient:
    """Sync HTTP client for the endpoints ``meeting_notes.wire`` defines."""

    def __init__(
        self,
        base_url: str,
        token: Optional[str] = None,
        timeout: Union[float, "httpx.Timeout"] = 10.0,
    ):
        """``timeout`` is handed straight to ``httpx.Client``, which already
        accepts either a single float (same budget for every phase) or an
        ``httpx.Timeout`` with separate connect/read/write/pool budgets. A
        quick health probe wants a short single number; a caller uploading a
        multi-hundred-MB recording body wants a much longer read/write budget
        without also waiting that long just to notice a dead connection --
        see ``meeting_notes.client.queue``'s ``UploadWorker`` for that case.
        """
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._client = httpx.Client(
            base_url=self.base_url, timeout=timeout, headers=identity.client_headers()
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ServerClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _headers(self, **extra: str) -> Dict[str, str]:
        headers = dict(wire.auth_headers(self.token))
        headers.update(identity.client_headers())
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
        # A 426 means "this client is too old for the server" (banner in the UI);
        # any success means it is fine after all.
        version_gate.inspect_response(resp)
        resp.raise_for_status()
        return resp

    # -- calls ----------------------------------------------------------

    def health(self) -> Dict[str, Any]:
        return self._request("GET", wire.HEALTH, headers=self._headers()).json()

    def upload_track(
        self,
        session_id: str,
        track: str,
        pcm_path: Path,
        frames: int,
        *,
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> Dict[str, Any]:
        """Stream ``pcm_path`` (16 kHz mono int16 PCM) to the server.

        Reads the file in fixed-size chunks rather than loading it whole --
        a completed recording can be well over 100 MB, and this runs on the
        same laptop that just spent the meeting recording it. ``frames`` goes
        along as an ``X-Frames`` header purely so the server can cross-check
        what it actually received against what the client meant to send;
        the server's own count (from bytes on the wire) is authoritative.
        """
        path = wire.track_upload_path(session_id, track)
        headers = self._headers(
            **{"Content-Type": "application/octet-stream", "X-Frames": str(frames)}
        )
        resp = self._request(
            "POST",
            path,
            content=_iter_file(Path(pcm_path), on_chunk=progress_callback),
            headers=headers,
        )
        return resp.json() if resp.content else {}

    def upload_recording(self, recording_path: Path, *, name: str = "") -> Dict[str, Any]:
        """Upload an existing audio recording for server-side transcription.

        Contract assumption (kept in one method so the client is easy to
        update): ``POST /v1/uploads`` accepts a multipart field named ``file``
        and an optional text ``name`` field, then returns a JSON object such as
        ``{session_id, job_id, state}``.  The server is responsible for
        decoding common formats and selecting its configured transcription
        model; this client never transcribes or decodes the recording.
        """
        path = Path(recording_path).expanduser()
        if not path.is_file():
            raise FileNotFoundError(path)
        content_type = recording_content_type(path)
        data = {"name": name} if name else None
        # A file object lets httpx stream multipart content instead of making
        # a second whole-file bytes copy.  _request retains the same error
        # semantics as track uploads (unreachable vs HTTP rejection).
        with path.open("rb") as fh:
            resp = self._request(
                "POST",
                "/v1/uploads",
                files={"file": (path.name, fh, content_type)},
                data=data,
                headers=self._headers(),
            )
        return resp.json() if resp.content else {}

    # A descriptive alias for callers that use "audio" rather than
    # "recording" in their UI terminology.
    upload_audio = upload_recording

    def finalize(
        self,
        session_id: str,
        meta: Dict[str, Any],
        timing: Dict[str, Any],
    ) -> str:
        """Ask the server to run the authoritative transcription pass. Returns job_id."""
        path = wire.finalize_path(session_id)
        body = {"meta": meta, "timing": timing}
        resp = self._request("POST", path, json=body, headers=self._headers())
        data = resp.json()
        return data["job_id"]

    def report_upload_status(self, session_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Publish regular-recorder lifecycle state to the server.

        Contract assumption: ``PUT /v1/sessions/{session_id}/pipeline``
        accepts JSON containing ``state``, ``bytes_received``,
        ``bytes_total`` and ``percent``, plus optional ``name``/``device``
        metadata. Transcription progress is derived from the server job.
        This is best-effort from the queue worker; the local queue remains the
        source of truth when the server is offline.
        """
        safe_id = quote(session_id, safe="")
        resp = self._request(
            "PUT",
            wire.pipeline_path(safe_id),
            json=payload,
            headers=self._headers(),
        )
        return resp.json() if resp.content else {}

    def job(self, job_id: str) -> Dict[str, Any]:
        return self._request("GET", wire.job_path(job_id), headers=self._headers()).json()

    def transcript(self, job_id: str) -> Dict[str, Any]:
        return self._request(
            "GET", wire.job_transcript_path(job_id), headers=self._headers()
        ).json()

    # -- session history -------------------------------------------------

    def list_sessions(
        self,
        *,
        q: Optional[str] = None,
        state: Optional[str] = None,
        page: int = 1,
        per_page: int = 50,
    ) -> Dict[str, Any]:
        """Return the server's indexed, paginated session history."""
        params: Dict[str, Any] = {"page": page, "per_page": per_page}
        if q:
            params["q"] = q
        if state:
            params["state"] = state
        return self._request(
            "GET", "/v1/sessions", params=params, headers=self._headers()
        ).json()

    def session_detail(self, session_id: str) -> Dict[str, Any]:
        safe_id = quote(session_id, safe="")
        return self._request(
            "GET", f"/v1/sessions/{safe_id}", headers=self._headers()
        ).json()

    def recordings_status(self, session_ids) -> Dict[str, Dict[str, Any]]:
        """What the server knows about each of these session ids (0.7.6+ servers).

        Returns ``{session_id: {on_server, has_copy, in_trash, transcription, error}}``; see
        ``meeting_notes.recording_status``. Ids the server cannot take (not a valid session id) are
        left out. An older server answers 404, which surfaces as ``httpx.HTTPStatusError``.
        """
        ids = [i for i in dict.fromkeys(session_ids) if remote.valid_session_id(i)]
        found: Dict[str, Dict[str, Any]] = {}
        for start in range(0, len(ids), 500):
            resp = self._request(
                "POST",
                "/v1/recordings/status",
                json={"session_ids": ids[start:start + 500]},
                headers=self._headers(),
            )
            items = (resp.json() or {}).get("items")
            if isinstance(items, dict):
                found.update({k: v for k, v in items.items() if isinstance(v, dict)})
        return found

    def retranscribe_session(self, session_id: str) -> Dict[str, Any]:
        safe_id = quote(session_id, safe="")
        return self._request(
            "POST", f"/v1/sessions/{safe_id}/retranscribe", headers=self._headers()
        ).json()

    def delete_session_audio(self, session_id: str) -> Dict[str, Any]:
        safe_id = quote(session_id, safe="")
        return self._request(
            "POST", f"/v1/sessions/{safe_id}/delete-audio", headers=self._headers()
        ).json()

    def delete_session(self, session_id: str) -> Dict[str, Any]:
        safe_id = quote(session_id, safe="")
        return self._request(
            "DELETE", f"/v1/sessions/{safe_id}", headers=self._headers()
        ).json()
