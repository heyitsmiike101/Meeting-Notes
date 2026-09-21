"""Best-effort live preview over a websocket -- never the source of truth.

``LiveStreamer`` mirrors captured audio to the LAN server so a meeting can be
watched transcribing in near-real-time. That is the entire point of it, and
it is the entire reason its failure modes look the way they do: the *local
recording* is what the meeting actually depends on (see
``meeting_notes.wire``'s module docstring), so nothing that happens on this
websocket -- a dropped connection, a slow server, a full buffer, a bug in a
caller's ``on_partial`` callback -- may ever be allowed to raise into the
capture thread or block it for any length of time. Every public method here
is written to that constraint first and "useful preview" second.
"""

from __future__ import annotations

import json
import threading
from collections import deque
from typing import Callable, Deque, Dict, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import connect as ws_connect

from meeting_notes import wire

# How much unacknowledged audio to keep per track, in seconds at the wire's
# fixed 16 kHz. A reconnect (a Wi-Fi blip, the server restarting) replays
# cleanly from this; an outage longer than it leaves a hole in the server's
# copy, which the final upload fills -- the recording on disk never stopped.
# Two minutes costs ~3.8 MB per track and covers a server restart or a
# router reboot; the old 8 s did not even cover the client's own 15 s
# reconnect backoff, so every real outage left a hole (seen on a real run).
_DEFAULT_BUFFER_SECONDS = 120.0
_INITIAL_BACKOFF = 0.5
_MAX_BACKOFF = 15.0
_RECV_POLL_TIMEOUT = 0.2  # how long each recv() waits before checking for new submissions

# Close codes meeting_notes.server.app's websocket route sends when the
# problem is this session, not a transient network blip -- reconnecting with
# the same token/protocol would just get the same close again. See app.py's
# `stream` route for where these are raised.
_PERMANENT_CLOSE_CODES = {
    4400: "protocol mismatch or invalid session",
    4401: "unauthorized (check the token in Settings)",
}


class _PermanentStreamError(Exception):
    """Internal signal that the server rejected this session for a reason no
    amount of retrying will fix (bad token, protocol mismatch). Distinct from
    every other exception `_connect_and_pump` can raise, all of which mean
    "try again after a backoff"."""


class LiveStreamer:
    """Owns one background thread and one websocket connection to the server.

    ``submit`` is the only method meant to be called from the capture path;
    it is non-blocking and cannot raise. ``start``/``stop`` bracket a
    recording session. ``state`` and ``last_error`` are read-only status for
    a UI to display -- polling them, not callbacks, so a UI that never reads
    them imposes no cost here.
    """

    def __init__(
        self,
        base_url: str,
        token: Optional[str] = None,
        on_partial: Optional[Callable[[wire.Partial], None]] = None,
        *,
        buffer_seconds: float = _DEFAULT_BUFFER_SECONDS,
    ):
        self._ws_url = _to_ws_url(base_url) + wire.STREAM
        self.token = token
        self.on_partial = on_partial
        self._buffer_cap_frames = max(1, int(buffer_seconds * wire.STREAM_SAMPLE_RATE))

        self._lock = threading.Lock()
        # track -> deque[(frame_offset, pcm_bytes)], oldest first. Everything
        # in here is audio the server has not yet acknowledged.
        self._buffers: Dict[str, Deque[Tuple[int, bytes]]] = {}
        self._next_offset: Dict[str, int] = {}

        self._session_id: Optional[str] = None
        self._name = ""
        self._started_wall = 0.0

        self._state = "disconnected"
        self.last_error: Optional[str] = None
        # Set only when the server has permanently rejected this session (see
        # _PERMANENT_CLOSE_CODES); `_run` stops retrying once this is set, so
        # unlike `last_error` it is never overwritten by a later transient
        # failure -- there won't be one, the thread has already exited.
        self.permanent_error: Optional[str] = None

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def state(self) -> str:
        return self._state

    # -- lifecycle ------------------------------------------------------

    def start(self, session_id: str, name: str, started_wall: float) -> None:
        """Begin (or restart) streaming for a session. Never raises."""
        try:
            self.stop()  # in case a previous session's thread is still around
            with self._lock:
                self._session_id = session_id
                self._name = name
                self._started_wall = started_wall
                self._buffers.clear()
                self._next_offset.clear()
            self.permanent_error = None  # a fresh session gets a fresh chance
            self._stop_event = threading.Event()
            self._state = "connecting"
            self._thread = threading.Thread(
                target=self._run, name="live-streamer", daemon=True
            )
            self._thread.start()
        except Exception as exc:  # noqa: BLE001 - starting the preview must not break recording
            self._state = "disconnected"
            self.last_error = f"{type(exc).__name__}: {exc}"

    def stop(self, join_timeout: float = 2.0) -> None:
        """Stop streaming. Never raises, never blocks longer than ``join_timeout``."""
        try:
            self._stop_event.set()
            thread, self._thread = self._thread, None
            if thread is not None and thread.is_alive():
                thread.join(timeout=join_timeout)
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
        finally:
            self._state = "disconnected"

    def submit(self, track: str, pcm_bytes: bytes) -> None:
        """Hand off one chunk of 16 kHz mono int16 PCM. Non-blocking, never raises.

        Drops the oldest buffered audio for this track when the cap is hit,
        rather than blocking the caller -- the live view missing a second of
        preview is a non-event; a capture thread stalled waiting on a network
        write is a lost meeting.
        """
        try:
            if not pcm_bytes:
                return
            n_frames = len(pcm_bytes) // wire.BYTES_PER_FRAME
            if n_frames <= 0:
                return
            with self._lock:
                offset = self._next_offset.get(track, 0)
                buf = self._buffers.setdefault(track, deque())
                buf.append((offset, bytes(pcm_bytes)))
                self._next_offset[track] = offset + n_frames
                self._trim_locked(buf)
        except Exception as exc:  # noqa: BLE001 - see module docstring
            self.last_error = f"{type(exc).__name__}: {exc}"

    def _trim_locked(self, buf: Deque[Tuple[int, bytes]]) -> None:
        total = sum(len(data) // wire.BYTES_PER_FRAME for _, data in buf)
        while total > self._buffer_cap_frames and len(buf) > 1:
            _, dropped = buf.popleft()
            total -= len(dropped) // wire.BYTES_PER_FRAME

    # -- background thread -----------------------------------------------

    def _run(self) -> None:
        backoff = _INITIAL_BACKOFF
        while not self._stop_event.is_set():
            try:
                self._connect_and_pump()
                backoff = _INITIAL_BACKOFF  # a clean pump means the connection was good
            except _PermanentStreamError as exc:
                # The server has told us -- clearly, on purpose -- that this
                # session will never work. Retrying with backoff would just
                # be a slow way of hammering it with the same rejected
                # request forever, so stop the loop entirely rather than
                # falling through to the backoff/retry code below.
                self._state = "rejected"
                self.permanent_error = str(exc)
                self.last_error = str(exc)
                return
            except Exception as exc:  # noqa: BLE001 - any transport failure just means retry
                self._state = "disconnected"
                self.last_error = f"{type(exc).__name__}: {exc}"
            if self._stop_event.is_set():
                return
            self._state = "disconnected"
            if self._stop_event.wait(backoff):
                return
            backoff = min(backoff * 2, _MAX_BACKOFF)

    def _connect_and_pump(self) -> None:
        self._state = "connecting"
        headers = wire.auth_headers(self.token)
        try:
            with ws_connect(
                self._ws_url,
                additional_headers=headers or None,
                open_timeout=5,
                close_timeout=1,
            ) as ws:
                self._state = "connected"
                hello = wire.Hello(
                    session_id=self._session_id or "",
                    name=self._name,
                    tracks=list(wire.TRACKS),
                    started_wall=self._started_wall,
                )
                ws.send(json.dumps(wire.to_json(hello)))

                # Per-track high-water mark of what's been sent *this connection*.
                # Reset on every (re)connect so a fresh connection resends
                # whatever the buffer still holds unacknowledged -- that's the
                # resume-from-last-ack behaviour, driven entirely by what
                # survived in the buffer rather than by anything the new
                # connection has to remember.
                sent_upto: Dict[str, int] = {}

                while not self._stop_event.is_set():
                    self._send_pending(ws, sent_upto)
                    try:
                        message = ws.recv(timeout=_RECV_POLL_TIMEOUT)
                    except TimeoutError:
                        continue
                    self._handle_message(message)
        except InvalidStatus as exc:
            # The HTTP upgrade itself was refused, before any close code from
            # our own protocol could apply -- e.g. a reverse proxy or the
            # server's auth dependency rejecting the handshake outright.
            permanent = self._permanent_error_for_status(exc.response.status_code)
            if permanent is not None:
                raise permanent from exc
            raise
        except ConnectionClosed as exc:
            permanent = self._permanent_error_for_close(exc)
            if permanent is not None:
                raise permanent from exc
            raise

    def _permanent_error_for_status(self, status_code: int) -> Optional[_PermanentStreamError]:
        if status_code not in (401, 403):
            return None
        return _PermanentStreamError(
            "live preview rejected by server: unauthorized (check the token in Settings)"
        )

    def _permanent_error_for_close(self, exc: ConnectionClosed) -> Optional[_PermanentStreamError]:
        close = exc.rcvd  # the close frame the server sent us, if any
        if close is None:
            return None
        detail = _PERMANENT_CLOSE_CODES.get(close.code)
        if detail is None:
            return None
        message = f"live preview rejected by server: {detail}"
        if close.reason and close.reason not in detail:
            message += f" ({close.reason})"
        return _PermanentStreamError(message)

    def _send_pending(self, ws, sent_upto: Dict[str, int]) -> None:
        with self._lock:
            to_send = []
            for track, buf in self._buffers.items():
                # sent_upto is the EXCLUSIVE end of what this connection has
                # sent, so the chunk that starts exactly there is the next one
                # to go. This used to be `offset <= base`, which skipped that
                # chunk -- and since an ack only ever pops chunks *before* it,
                # the skipped one sat in the buffer forever. Net effect on a
                # real session: every other block never reached the server,
                # the contiguous prefix stuck at one block, and the live
                # preview was never fed past the first half second.
                base = sent_upto.get(track, 0)
                for offset, data in buf:
                    if offset < base:
                        continue
                    to_send.append((track, offset, data))
                    sent_upto[track] = offset + len(data) // wire.BYTES_PER_FRAME
        for track, offset, data in to_send:
            ws.send(wire.encode_audio_frame(track, offset, data))

    def _handle_message(self, raw) -> None:
        if isinstance(raw, (bytes, bytearray)):
            return  # the server never sends binary; ignore rather than choke on it
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return
        kind = data.get("type")
        if kind == "ack":
            self._apply_ack(data.get("track"), data.get("frames"))
        elif kind == "partial" and self.on_partial is not None:
            partial = wire.Partial(
                track=data.get("track", ""),
                start=float(data.get("start", 0.0)),
                end=float(data.get("end", 0.0)),
                text=data.get("text", ""),
            )
            try:
                self.on_partial(partial)
            except Exception as exc:  # noqa: BLE001 - a caller's callback must not kill the thread
                self.last_error = f"on_partial callback failed: {exc}"
        elif kind == "error":
            self.last_error = str(data.get("detail", "server error"))

    def _apply_ack(self, track, frames) -> None:
        if track is None or frames is None:
            return
        try:
            frames = int(frames)
        except (TypeError, ValueError):
            return
        with self._lock:
            buf = self._buffers.get(track)
            if not buf:
                return
            while buf:
                offset, data = buf[0]
                end = offset + len(data) // wire.BYTES_PER_FRAME
                if end > frames:
                    break
                buf.popleft()  # server has this chunk; stop resending it on reconnect


def _to_ws_url(base_url: str) -> str:
    parts = urlsplit(base_url)
    scheme = "wss" if parts.scheme == "https" else "ws"
    return urlunsplit((scheme, parts.netloc, parts.path.rstrip("/"), "", ""))
