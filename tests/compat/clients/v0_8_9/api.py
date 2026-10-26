# COMPAT FIXTURE - do not edit. Verbatim copy of meeting_notes/client/api.py from the 0.8.9 client
# (release/0.8.9), with only the meeting_notes.* imports rewritten to be package-relative.
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
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple, Union
from urllib.parse import quote, urljoin, urlsplit

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


# A server address that redirects (http -> https, meeting.lan -> a public name) moves for the whole process:
# configured base -> the base its redirects lead to. Every later ServerClient, the updater and the websockets start
# from the moved address, so a streamed upload is never sent to an address that only answers with a redirect.
_MOVED: Dict[str, str] = {}


def server_base(base_url: str) -> str:
    """The address to use for the configured server ``base_url`` (its redirect target once one was seen)."""
    base = (base_url or "").rstrip("/")
    return _MOVED.get(base, base)


def _moved_base(current_base: str, response: httpx.Response) -> Optional[str]:
    """The new server base a redirect points at, or None when it is not the same API call somewhere else.

    Only a redirect that keeps the API path counts (``http://meeting.lan/health`` ->
    ``https://meeting.example.com/health``); one to a login page or elsewhere is left to fail as before. An
    https address is never moved back to plain http.
    """
    location = response.headers.get("location")
    if not response.is_redirect or not location:
        return None
    base = urlsplit(current_base)
    requested = urlsplit(str(response.request.url))
    target = urlsplit(urljoin(str(response.request.url), location))
    suffix = requested.path[len(base.path.rstrip("/")):]
    if not requested.path.startswith(base.path.rstrip("/")) or not suffix:
        return None
    if not target.path.endswith(suffix) or target.query != requested.query or target.fragment:
        return None
    if target.scheme not in ("http", "https") or (base.scheme == "https" and target.scheme == "http"):
        return None
    new_base = f"{target.scheme}://{target.netloc}{target.path[: len(target.path) - len(suffix)]}".rstrip("/")
    return new_base if new_base != current_base else None


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


def clean_note_types(data: Any) -> Tuple[List[Dict[str, str]], str]:
    """``(types, default_id)`` from a ``note_templates()`` reply: ``types`` is ``[{id, name}]`` in the server's
    order (rows without a usable id dropped), ``default_id`` the server's default if it is one of them, else ''."""
    src = data if isinstance(data, dict) else {}
    types: List[Dict[str, str]] = []
    for item in src.get("items") if isinstance(src.get("items"), list) else []:
        if not isinstance(item, dict) or not remote.valid_note_type(item.get("id")):
            continue
        tid = item["id"].strip()
        if any(t["id"] == tid for t in types):
            continue
        name = " ".join(str(item.get("name") or "").split())[:80] or tid
        types.append({"id": tid, "name": name})
    default = src.get("default_template_id")
    default = default.strip() if isinstance(default, str) else ""
    return types, default if any(t["id"] == default for t in types) else ""


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
        self._configured_base = base_url.rstrip("/")
        self.base_url = server_base(self._configured_base)
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
        resp = self._send(method, path, **kwargs)
        new_base = _moved_base(self.base_url, resp)
        if new_base is not None:
            # The configured address redirects: move this client (and later ones) there and repeat the call. A
            # streamed body was already spent on the redirect, so that call reports "unreachable" and the
            # upload queue's next try goes straight to the new address.
            _MOVED[self._configured_base] = new_base
            self.base_url = new_base
            self._client.base_url = new_base
            if kwargs.get("files") is not None or not isinstance(kwargs.get("content"), (bytes, str, type(None))):
                raise ServerUnavailable(f"{method} {path}: the server moved to {new_base}; retrying there")
            resp = self._send(method, path, **kwargs)
        # A 426 means "this client is too old for the server" (banner in the UI);
        # any success means it is fine after all.
        version_gate.inspect_response(resp)
        resp.raise_for_status()
        return resp

    def _send(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            resp = self._client.request(method, path, **kwargs)
        except httpx.TransportError as exc:
            # Covers connection refused, DNS failure, and every flavour of
            # timeout -- all of them mean "couldn't reach the server", which
            # is exactly the condition callers need to queue-and-continue on.
            if self.base_url != self._configured_base:
                _MOVED.pop(self._configured_base, None)  # the old address gets a fresh try next time
            raise ServerUnavailable(f"{method} {path} failed: {exc}") from exc
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

    def upload_track_file(
        self,
        session_id: str,
        track: str,
        path: Path,
        *,
        content_type: str = "audio/ogg",
        track_format: str = "opus",
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> Dict[str, Any]:
        """Stream a compressed track file (Ogg Opus) to the same route as :meth:`upload_track`.

        Sent with ``X-Track-Format`` so the server decodes it itself; there is no ``X-Frames`` (the server counts
        what it decoded). Only for servers whose ``/health`` lists the format in ``track_formats``."""
        headers = self._headers(**{"Content-Type": content_type, "X-Track-Format": track_format})
        resp = self._request(
            "POST",
            wire.track_upload_path(session_id, track),
            content=_iter_file(Path(path), on_chunk=progress_callback),
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

    def upload_transcript(
        self,
        text: str,
        *,
        name: str = "",
        started_at: Optional[float] = None,
        source: str = "pasted",
        filename: str = "",
    ) -> Dict[str, Any]:
        """Create a meeting from a transcript the person already has (no audio, nothing is transcribed).

        ``POST /v1/sessions/transcript`` with ``{text, name?, started_at?, source, filename?}``.
        ``started_at`` is a unix timestamp; ``source`` is ``"pasted"`` or ``"file"`` (then ``filename``
        is the original file name). The server parses ``.vtt`` / ``.srt`` / timestamped / plain text.
        Servers older than 0.7.8 answer 404 or 405, which surfaces as ``httpx.HTTPStatusError``.
        """
        payload: Dict[str, Any] = {"text": text, "source": source}
        if name:
            payload["name"] = name
        if started_at is not None:
            payload["started_at"] = int(started_at)
        if filename:
            payload["filename"] = filename
        resp = self._request(
            "POST", "/v1/sessions/transcript", json=payload, headers=self._headers()
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

    def note_templates(self) -> Dict[str, Any]:
        """The server's note types: ``{default_template_id, items: [{id, name, builtin, default}]}``.

        ``GET /v1/note-templates``. A note type picks the AI summary prompt and where the notes are saved
        (for example a Notion page); the client tags each meeting it records with one (``meta.note_type``).
        Servers older than 0.7.6 answer 404, which surfaces as ``httpx.HTTPStatusError``.
        """
        return self._request("GET", "/v1/note-templates", headers=self._headers()).json()

    # -- GPU helper (docs/gpu-helper-design.md) ------------------------------

    def register_compute_helper(self, name: str, instance_id: str) -> Dict[str, Any]:
        """``POST /v1/compute/helpers``: ``{helper_id, key}`` (the key is shown once; the same ``instance_id``
        again replaces the old key)."""
        return self._request(
            "POST", "/v1/compute/helpers", json={"name": name, "instance_id": instance_id}, headers=self._headers()
        ).json()

    def compute_pack_manifest(self) -> Dict[str, Any]:
        """``GET /v1/compute/pack/manifest`` (404 when the server has not prepared a pack)."""
        return self._request("GET", "/v1/compute/pack/manifest", headers=self._headers()).json()

    def compute_pack_file(self, path: str, start: int = 0, version: Optional[str] = None):
        """A streaming ``GET /v1/compute/pack/files/{path}`` as a context manager; ``start`` > 0 asks for the rest
        of the file with a ``Range`` header (a 200 answer means the server sent the whole file again). ``version``
        pins the pack the manifest listed (410 when the server no longer has it)."""
        headers = self._headers()
        if start > 0:
            headers["Range"] = f"bytes={int(start)}-"
        return self._client.stream(
            "GET", "/v1/compute/pack/files/" + quote(path, safe="/"), headers=headers,
            params={"version": version} if version else None,
            timeout=httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0),
        )

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
