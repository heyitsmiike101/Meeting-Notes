"""Live-transcript stream auth for the mobile recorder (``docs/mobile-recorder-design.md``, "Live transcript").

A phone page cannot send the shared token on a websocket (and must not have it in a URL), so it asks for a
short-lived, single-use, session-bound **stream ticket** over its cookie-authenticated, same-origin HTTP call and
passes it as a websocket subprotocol value ``mn-ticket.<ticket>``. ``/v1/stream`` (app.py) validates it here.

Also held here: the per-session connection registry (generation replacement + the live fence) and the
rate/offset limits for ticketed sockets. Desktop token/header streams never touch any of this.
"""

from __future__ import annotations

import asyncio
import collections
import hashlib
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import wire
from . import auth
from . import mobile_common
from . import store as store_mod

SUBPROTOCOL_PREFIX = "mn-ticket."
TICKET_TTL = 60.0
MAX_TICKETS = 100

# Close codes (4400/4401/4403/4409 already exist on /v1/stream).
CLOSE_BAD_REQUEST = 4400
CLOSE_UNAUTHORIZED = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_REPLACED = 4410  # a newer connection for the session took over
CLOSE_FINISHED = 4411  # the upload was finished; the stream may not write any more

MAX_OFFSET_FRAMES = 4 * 3600 * wire.STREAM_SAMPLE_RATE  # no phone meeting streams past 4 h of audio
RATE_FACTOR = 2.0  # frames may arrive at most this much faster than real time...
BURST_SECONDS = 20.0  # ...with this much slack (a reconnect resends its buffer)
FENCE_RECHECK_SECONDS = 2.0


def ticket_from_subprotocols(subprotocols) -> Optional[str]:
    """The ``mn-ticket.<ticket>`` subprotocol value offered by the client, whole (to echo on accept)."""
    for value in subprotocols or []:
        if isinstance(value, str) and value.startswith(SUBPROTOCOL_PREFIX) and len(value) > len(SUBPROTOCOL_PREFIX):
            return value
    return None


@dataclass
class Ticket:
    session_id: str
    generation: int
    expires: float


class TicketStore:
    """In-memory tickets, kept only as SHA-256 hashes; single use; bounded."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self._lock = threading.Lock()
        self._tickets: "collections.OrderedDict[str, Ticket]" = collections.OrderedDict()
        self._generations: Dict[str, int] = {}

    @staticmethod
    def _hash(ticket: str) -> str:
        return hashlib.sha256(ticket.encode("utf-8")).hexdigest()

    def issue(self, session_id: str) -> tuple:
        ticket = secrets.token_urlsafe(32)
        with self._lock:
            generation = self._generations.get(session_id, 0) + 1
            self._generations[session_id] = generation
            self._tickets[self._hash(ticket)] = Ticket(session_id, generation, self.clock() + TICKET_TTL)
            while len(self._tickets) > MAX_TICKETS:
                self._tickets.popitem(last=False)
        return ticket, generation

    def consume(self, ticket: str) -> Optional[Ticket]:
        """The ticket's record, removing it (single use); None when unknown, used or expired."""
        with self._lock:
            record = self._tickets.pop(self._hash(ticket), None)
        if record is None or record.expires < self.clock():
            return None
        return record


@dataclass(eq=False)
class Conn:
    """One live ticketed socket."""

    session_id: str
    generation: int
    websocket: object
    loop: asyncio.AbstractEventLoop
    closed: bool = False
    fenced: bool = False
    tokens: float = BURST_SECONDS * wire.STREAM_SAMPLE_RATE
    last_refill: float = field(default_factory=time.monotonic)
    last_fence_check: float = field(default_factory=time.monotonic)

    def spend(self, frames: int, now: float) -> bool:
        """Token bucket in frames: refills at ``RATE_FACTOR`` x real time up to ``BURST_SECONDS`` of audio."""
        capacity = BURST_SECONDS * wire.STREAM_SAMPLE_RATE
        self.tokens = min(capacity, self.tokens + (now - self.last_refill) * RATE_FACTOR * wire.STREAM_SAMPLE_RATE)
        self.last_refill = now
        if frames > self.tokens:
            return False
        self.tokens -= frames
        return True


class MobileStream:
    """Tickets plus the registry of live ticketed sockets (at most one per session)."""

    def __init__(self, store):
        self.store = store
        self.tickets = TicketStore()
        self._lock = threading.Lock()
        self._conns: Dict[str, Conn] = {}

    def origin_ok(self, websocket) -> bool:
        """The socket's ``Origin`` must be one of the server's own page origins (same rule as HTTP writes)."""
        origin = mobile_common._origin_of(websocket.headers.get("origin") or "")
        if not origin:
            return False
        allowed = mobile_common.allowed_origins(self.store.root)
        if not allowed:
            scheme = "https" if websocket.url.scheme == "wss" else "http"
            host = websocket.headers.get("host") or websocket.url.netloc
            allowed = [mobile_common._origin_of(f"{scheme}://{host}")]
        return origin in allowed

    def register(self, conn: Conn) -> Optional[Conn]:
        """Make ``conn`` the session's live socket; returns the older one it replaces (the caller closes it).
        Raises ValueError when a same-or-newer ticketed socket already holds the session."""
        with self._lock:
            old = self._conns.get(conn.session_id)
            if old is not None and old.generation >= conn.generation:
                raise ValueError("stale generation")
            self._conns[conn.session_id] = conn
        return old

    def is_held(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._conns

    def release(self, conn: Conn) -> None:
        with self._lock:
            if self._conns.get(conn.session_id) is conn:
                del self._conns[conn.session_id]

    def is_current(self, conn: Conn) -> bool:
        with self._lock:
            return self._conns.get(conn.session_id) is conn

    @staticmethod
    def close_conn(conn: Conn, code: int, reason: str) -> None:
        """Close a socket from any thread or task; best effort."""
        conn.closed = True

        async def _close():
            try:
                await conn.websocket.close(code=code, reason=wire.close_reason(reason))
            except Exception:  # noqa: BLE001 - already closing / gone
                pass

        coro = _close()
        try:
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is conn.loop:
                conn.loop.create_task(coro)
            else:
                asyncio.run_coroutine_threadsafe(coro, conn.loop)
        except Exception:  # noqa: BLE001 - the loop is gone, so is the socket
            coro.close()

    def fence(self, session_id: str) -> None:
        """Registered with ``mobile_common.register_live_closer``: the upload was finished."""
        with self._lock:
            conn = self._conns.get(session_id)
        if conn is not None:
            conn.fenced = True
            self.close_conn(conn, CLOSE_FINISHED, "upload finished")

    def writes_allowed(self, conn: Conn, now: float) -> bool:
        """Cheap per-write fence check: the closer's flag at once, the state file at most every 2 s."""
        if conn.fenced:
            return False
        if now - conn.last_fence_check >= FENCE_RECHECK_SECONDS:
            conn.last_fence_check = now
            if not mobile_common.live_writes_allowed(self.store, conn.session_id):
                conn.fenced = True
        return not conn.fenced


def install_mobile_stream(app, *, store) -> MobileStream:
    """Register the ticket route and the live fence; the ``/v1/stream`` handler (app.py) uses the returned object."""
    ms = MobileStream(store)
    app.state.mobile_stream = ms
    mobile_common.register_live_closer(ms.fence)

    router = APIRouter()

    @router.post("/v1/mobile/sessions/{session_id}/stream-ticket")
    async def stream_ticket(session_id: str, request: Request, _auth: None = Depends(auth.require_token)):
        mobile_common.require_same_origin(request, store.root)
        state = mobile_common.read_state(store, session_id) if store_mod.is_safe_id(session_id) else None
        if state is None or state.get("state") != mobile_common.OPEN:
            raise HTTPException(status_code=409, detail="this recording is not open for live transcript")
        ticket, generation = ms.tickets.issue(session_id)
        return {"ticket": ticket, "generation": generation, "expires_in": int(TICKET_TTL)}

    app.include_router(router)
    return ms
