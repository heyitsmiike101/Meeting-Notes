"""Live transcription on a GPU helper: the server end of the helper's live WebSocket (``docs/gpu-helper-design.md``).

A helper keeps one outbound WebSocket (``/v1/compute/live``, see ``compute_api``) open. :class:`LiveRemote` is the
registry of those connections and the bridge between the two worlds: ``LivePreview.poll`` runs on a worker thread
and calls the blocking :meth:`LiveRemote.transcribe`; the connection lives on the app's event loop. A request is
sent with ``asyncio.run_coroutine_threadsafe`` and answered through a ``concurrent.futures.Future``.

Rules that keep a slow or broken helper harmless (every failure is just "return None", and the caller then
transcribes the chunk locally exactly as before):

* the wait is bounded; a request that times out is tombstoned so its late result is ignored;
* a result is accepted only from the connection that was sent the request, only for an id still pending;
* when a connection closes every request pending on it fails at once;
* at most ``MAX_PENDING`` requests are outstanding per connection;
* a helper with ``COOLDOWN_AFTER`` consecutive timeouts/errors is skipped for ``COOLDOWN_SECONDS``;
* results are validated like a batch result (finite, sorted times within the utterance + 1 s, bounded text).
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import json
import logging
import threading
import time
import uuid
from collections import OrderedDict
from typing import Any, Callable, Dict, List, Optional

from ..transcribe.protocol import Segment
from .compute import MAX_SEGMENT_TEXT_CHARS, _number

logger = logging.getLogger("meeting_notes.server.live_remote")

DEFAULT_TIMEOUT = 2.5  # seconds a chunk may wait for the helper before it is transcribed locally
DEFAULT_LIVE_MODEL = "small.en"
MAX_PENDING = 8  # outstanding requests per connection
COOLDOWN_AFTER = 3  # consecutive timeouts/errors before a helper is skipped
COOLDOWN_SECONDS = 60.0
MAX_FRAME_BYTES = 1024 * 1024
MAX_RESULT_SEGMENTS = 200
RESULT_SLACK_SECONDS = 1.0
PING_SECONDS = 20.0
HELLO_TIMEOUT = 10.0
MAX_TOMBSTONES = 256
EMA_WEIGHT = 0.3
# Helper errors that are not the helper's fault: it is busy with an earlier utterance, or ours came too late.
_BENIGN_ERRORS = ("busy", "expired")


class _Outcome:
    """What a request ended with: segments, or the reason it produced none."""

    __slots__ = ("segments", "error")

    def __init__(self, segments: Optional[List[Segment]] = None, error: Optional[str] = None):
        self.segments = segments
        self.error = error


class LiveConnection:
    """One open live socket. ``pending`` and the counters are guarded by ``LiveRemote._lock``."""

    def __init__(self, helper_id: str, name: str, websocket, loop, hello: dict):
        self.helper_id = helper_id
        self.name = name
        self.websocket = websocket
        self.loop = loop
        self.models: List[str] = hello.get("models") or []
        self.gpu: Optional[dict] = hello.get("gpu")
        self.pack_version: Optional[str] = hello.get("pack_version")
        self.pending: Dict[str, concurrent.futures.Future] = {}
        self.tombstones: "OrderedDict[str, None]" = OrderedDict()
        self.failures = 0
        self.cooldown_until = 0.0
        self.closed = False
        self.send_lock: Optional[asyncio.Lock] = None  # created on the loop by LiveRemote.attach


class LiveRemote:
    """Registry of live helper connections plus the thread-safe ``transcribe`` entry point."""

    def __init__(
        self,
        settings_provider: Optional[Callable[[], Any]] = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ):
        self.settings_provider = settings_provider
        self._clock = clock
        self._wall = wall_clock
        self._lock = threading.RLock()
        self._conns: List[LiveConnection] = []
        self._stats: Dict[str, dict] = {}  # helper_id -> {served, avg_ms, last_error}; outlives a connection
        self._rr = 0
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # -- settings ---------------------------------------------------------

    def _settings(self):
        if self.settings_provider is None:
            return None
        try:
            return self.settings_provider()
        except Exception:  # noqa: BLE001 - a settings read must never break live preview
            logger.exception("could not read settings for live remote")
            return None

    def model(self) -> str:
        """The model to request: ``gpu_live_model``, else ``small.en``."""
        value = str(getattr(self._settings(), "gpu_live_model", "") or "").strip()
        return value if value and value.lower() not in ("same", "default") else DEFAULT_LIVE_MODEL

    def enabled(self) -> bool:
        """Offload and live-on-helper are on (missing settings count as on) and a live connection is open."""
        s = self._settings()
        if not bool(getattr(s, "compute_offload_enabled", True)):
            return False
        if not bool(getattr(s, "live_on_helper", True)):
            return False
        with self._lock:
            return any(not c.closed for c in self._conns)

    # -- connections (asyncio side) --------------------------------------

    def attach(self, helper_id: str, name: str, websocket, hello: dict) -> LiveConnection:
        """Register a connection. Call from the app's event loop; replaces an older one for the same helper."""
        loop = asyncio.get_running_loop()
        conn = LiveConnection(helper_id, name, websocket, loop, hello)
        conn.send_lock = asyncio.Lock()
        with self._lock:
            self._loop = loop
            older = [c for c in self._conns if c.helper_id == helper_id]
            self._conns.append(conn)
            self._stats.setdefault(helper_id, {"served": 0, "avg_ms": None, "last_error": None})
        for old in older:
            self._close(old, 4000, "replaced by a newer live connection")
        return conn

    def detach(self, conn: LiveConnection) -> None:
        """Forget a connection and fail everything pending on it. Idempotent, any thread."""
        with self._lock:
            conn.closed = True
            if conn in self._conns:
                self._conns.remove(conn)
            pending, conn.pending = list(conn.pending.values()), {}
        for fut in pending:
            if not fut.done():
                fut.set_result(_Outcome(error="connection closed"))

    def close_helper(self, helper_id: str, reason: str = "helper key revoked") -> None:
        """Close every live socket of a helper (key revoked or replaced). Thread-safe."""
        with self._lock:
            conns = [c for c in self._conns if c.helper_id == helper_id]
        for conn in conns:
            self._close(conn, 4401, reason)

    def close_all(self) -> None:
        with self._lock:
            conns = list(self._conns)
        for conn in conns:
            self._close(conn, 1001, "server shutting down")

    def _close(self, conn: LiveConnection, code: int, reason: str) -> None:
        self.detach(conn)
        try:
            asyncio.run_coroutine_threadsafe(conn.websocket.close(code=code, reason=reason), conn.loop)
        except RuntimeError:  # the loop is gone; nothing left to close
            pass

    async def _send(self, conn: LiveConnection, text: str) -> None:
        async with conn.send_lock:
            await conn.websocket.send_text(text)

    async def ping(self, conn: LiveConnection) -> None:
        await self._send(conn, json.dumps({"type": "ping"}))

    def handle_frame(self, conn: LiveConnection, frame: Any) -> None:
        """A helper -> server ``result`` / ``error`` frame (asyncio side). Anything else is ignored."""
        if not isinstance(frame, dict):
            return
        kind = frame.get("type")
        if kind not in ("result", "error"):
            return
        request_id = frame.get("id")
        if not isinstance(request_id, str):
            return
        with self._lock:
            fut = conn.pending.pop(request_id, None)
        if fut is None or fut.done():
            return  # unknown, tombstoned (timed out) or already failed: ignore
        if kind == "error":
            fut.set_result(_Outcome(error=str(frame.get("error") or "error")[:200]))
            return
        length = getattr(fut, "utterance_seconds", 0.0)
        track = getattr(fut, "track", "")
        try:
            fut.set_result(_Outcome(segments=self._validate(frame.get("segments"), length, track)))
        except ValueError as exc:
            fut.set_result(_Outcome(error=f"invalid result: {exc}"))

    @staticmethod
    def _validate(items: Any, utterance_seconds: float, track: str) -> List[Segment]:
        if not isinstance(items, list) or len(items) > MAX_RESULT_SEGMENTS:
            raise ValueError("segments must be a list of at most %d" % MAX_RESULT_SEGMENTS)
        limit = utterance_seconds + RESULT_SLACK_SECONDS
        out: List[Segment] = []
        previous = 0.0
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                raise ValueError(f"segment {i} must be an object")
            start, end, text = _number(item.get("start")), _number(item.get("end")), item.get("text")
            if start is None or end is None or not 0.0 <= start <= end:
                raise ValueError(f"segment {i}: times must satisfy 0 <= start <= end")
            if start >= utterance_seconds:
                # Whisper predicting timestamps past the audio it was given (larger models do this near the end of
                # an utterance): the same hallucination signature the local path drops in live.py. Drop the segment,
                # keep the rest of the result.
                continue
            end = min(end, limit)
            if start < previous:
                raise ValueError(f"segment {i}: segments must be sorted by start")
            if not isinstance(text, str) or len(text) > MAX_SEGMENT_TEXT_CHARS:
                raise ValueError(f"segment {i}: text must be a string of at most {MAX_SEGMENT_TEXT_CHARS} characters")
            previous = start
            out.append(Segment(start=start, end=end, text=text, track=track))
        return out

    # -- worker-thread side ----------------------------------------------

    def _pick(self, model: str) -> Optional[LiveConnection]:
        """Next ready connection, round-robin. Caller holds ``_lock``."""
        now = self._clock()
        ready = [
            c
            for c in self._conns
            if not c.closed
            and now >= c.cooldown_until
            and len(c.pending) < MAX_PENDING
            and (not c.models or model in c.models)
        ]
        if not ready:
            return None
        self._rr += 1
        return ready[self._rr % len(ready)]

    def transcribe(
        self, pcm: bytes, sample_rate: int, track: str, model: str, timeout: float = DEFAULT_TIMEOUT
    ) -> Optional[List[Segment]]:
        """Transcribe one utterance on a helper. Blocks up to ``timeout``; None means "do it locally"."""
        utterance_seconds = (len(pcm) // 2) / float(sample_rate or 1)
        request_id = uuid.uuid4().hex
        fut: concurrent.futures.Future = concurrent.futures.Future()
        fut.utterance_seconds = utterance_seconds  # type: ignore[attr-defined]
        fut.track = track  # type: ignore[attr-defined]
        with self._lock:
            conn = self._pick(model)
            if conn is None:
                return None
            conn.pending[request_id] = fut
        message = json.dumps(
            {
                "type": "utterance",
                "id": request_id,
                "model": model,
                "track": track,
                "sample_rate": sample_rate,
                "deadline": self._wall() + timeout,  # server epoch seconds; for logs only (clocks may differ)
                "budget_ms": int(timeout * 1000),  # the helper derives its own local deadline from this
                "pcm_b64": base64.b64encode(pcm).decode("ascii"),
            }
        )
        started = self._clock()
        try:
            sent = asyncio.run_coroutine_threadsafe(self._send(conn, message), conn.loop)
        except RuntimeError as exc:  # the loop is closed
            self._abandon(conn, request_id)
            self._record_failure(conn, f"send failed: {exc}")
            return None
        try:
            outcome = fut.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            sent.cancel()
            self._abandon(conn, request_id, tombstone=True)
            self._record_failure(conn, "timed out")
            return None
        if outcome.segments is None:
            error = outcome.error or "error"
            if error in _BENIGN_ERRORS:
                self._note(conn, last_error=error)
            elif error != "connection closed":
                self._record_failure(conn, error)
            return None
        self._record_success(conn, (self._clock() - started) * 1000.0)
        return outcome.segments

    def _abandon(self, conn: LiveConnection, request_id: str, *, tombstone: bool = False) -> None:
        with self._lock:
            conn.pending.pop(request_id, None)
            if tombstone:
                conn.tombstones[request_id] = None
                while len(conn.tombstones) > MAX_TOMBSTONES:
                    conn.tombstones.popitem(last=False)

    def _note(self, conn: LiveConnection, **fields) -> None:
        with self._lock:
            self._stats.setdefault(conn.helper_id, {"served": 0, "avg_ms": None, "last_error": None}).update(fields)

    def _record_success(self, conn: LiveConnection, elapsed_ms: float) -> None:
        with self._lock:
            conn.failures = 0
            st = self._stats.setdefault(conn.helper_id, {"served": 0, "avg_ms": None, "last_error": None})
            st["served"] += 1
            st["avg_ms"] = elapsed_ms if st["avg_ms"] is None else st["avg_ms"] + EMA_WEIGHT * (elapsed_ms - st["avg_ms"])
            st["last_error"] = None

    def _record_failure(self, conn: LiveConnection, error: str) -> None:
        with self._lock:
            conn.failures += 1
            if conn.failures >= COOLDOWN_AFTER:
                conn.cooldown_until = self._clock() + COOLDOWN_SECONDS
                conn.failures = 0
                logger.warning("live helper %s skipped for %.0f s after repeated failures (%s)", conn.name, COOLDOWN_SECONDS, error)
            self._stats.setdefault(conn.helper_id, {"served": 0, "avg_ms": None, "last_error": None})["last_error"] = error[:200]

    # -- presence ---------------------------------------------------------

    def stats(self, helper_id: str) -> dict:
        """``live`` presence for one helper: connected, served, avg_ms, last_error, ready."""
        with self._lock:
            st = dict(self._stats.get(helper_id) or {"served": 0, "avg_ms": None, "last_error": None})
            conn = next((c for c in self._conns if c.helper_id == helper_id and not c.closed), None)
            connected = conn is not None
            ready = connected and self._clock() >= conn.cooldown_until
        return {
            "connected": connected,
            "served": st["served"],
            "avg_ms": None if st["avg_ms"] is None else round(st["avg_ms"]),
            "last_error": st["last_error"],
            "ready": ready,
        }

