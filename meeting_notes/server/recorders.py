"""Live registry of connected recorders, plus remote control (protocol in ``remote.py``).

Everything here is in memory on purpose: a recorder is "connected" exactly as
long as its websocket is open (plus a stale sweep for half-dead sockets), so
nothing is persisted and a restarted server simply learns about recorders as
they reconnect.

* ``WS  /v1/recorders/connect``      recorder -> server (token auth)
* ``GET /v1/recorders``              list (web auth)
* ``WS  /v1/recorders/events``       live feed for the Recorders page (web auth)
* ``POST /v1/recorders/{id}/commands``  forward a whitelisted command, await the ack

The hub is touched from the event loop; ``sweep`` may also be called from a
test thread, so registry mutation is lock-guarded and anything that has to run
on the loop (publishing to subscribers, closing sockets) is handed over with
``call_soon_threadsafe``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket

from .. import __version__, remote
from . import auth, compat

logger = logging.getLogger("meeting_notes.server.recorders")

CLOSE_FORBIDDEN_ORIGIN = 4403
CLOSE_TOO_MANY = 4429

MAX_RECORDERS = 50
MAX_PENDING_COMMANDS = 4
MAX_SUBSCRIBERS = 20
SUBSCRIBER_QUEUE = 64
HELLO_TIMEOUT = 10.0
EVENTS_PING_EVERY = 20.0
SWEEP_EVERY = 5.0
MAX_JUNK_FRAMES = 50          # consecutive unusable frames before we hang up


class RecorderGone(Exception):
    """The recorder's socket closed while a command was waiting for its ack."""


class CommandTimeout(Exception):
    """No ack within the deadline."""


class _Recorder:
    """One connected recorder; ``ws`` is its live socket."""

    def __init__(self, ws: WebSocket, *, instance_id: str, device: str, platform_text: str,
                 version: str, address: str, state: Dict[str, Any], now: float):
        self.ws = ws
        self.instance_id = instance_id
        self.device = device
        self.platform_text = platform_text
        self.platform = remote.platform_family(platform_text)
        self.version = version
        self.address = address
        self.state = state
        self.connected_at = now
        self.last_seen = now
        self.dead = False
        self.pending: Dict[str, "asyncio.Future[Dict[str, Any]]"] = {}
        self.send_lock = asyncio.Lock()

    def item(self) -> Dict[str, Any]:
        key = compat.version_key(self.version)
        current = compat.version_key(__version__)
        return {
            "instance_id": self.instance_id,
            "device": self.device,
            "platform_text": self.platform_text,
            "platform": self.platform,
            "version": self.version,
            "connected_at": self.connected_at,
            "last_seen": self.last_seen,
            "address": self.address,
            "state": self.state,
            "behind": key is not None and current is not None and key < current,
            "outdated": bool(self.version) and compat.is_too_old(self.version),
        }


class _Subscriber:
    def __init__(self) -> None:
        self.queue: "asyncio.Queue[Optional[Dict[str, Any]]]" = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE)


class RecorderHub:
    """The in-memory registry, its event fan-out and the pending-command table."""

    def __init__(self, *, clock: Callable[[], float] = time.time, stale_after: float = remote.STALE_AFTER,
                 command_timeout: float = remote.COMMAND_TIMEOUT, max_recorders: int = MAX_RECORDERS,
                 sweep_every: float = SWEEP_EVERY):
        self.clock = clock
        self.stale_after = stale_after
        self.command_timeout = command_timeout
        self.max_recorders = max_recorders
        self.sweep_every = sweep_every
        self._items: Dict[str, _Recorder] = {}
        self._lock = threading.Lock()
        self._subs: List[_Subscriber] = []
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._sweeper: Optional["asyncio.Task[None]"] = None

    # -- reading ------------------------------------------------------------

    def list_items(self) -> List[Dict[str, Any]]:
        with self._lock:
            items = [r.item() for r in self._items.values()]
        items.sort(key=lambda i: (i["device"].lower(), i["connected_at"]))
        return items

    def get(self, instance_id: str) -> Optional[_Recorder]:
        with self._lock:
            return self._items.get(instance_id)

    def is_full(self, instance_id: str) -> bool:
        with self._lock:
            return instance_id not in self._items and len(self._items) >= self.max_recorders

    # -- registry mutation --------------------------------------------------

    def register(self, rec: _Recorder) -> None:
        """Add ``rec``; an existing connection with the same id is replaced (4409)."""
        self._loop = asyncio.get_running_loop()
        with self._lock:
            old = self._items.get(rec.instance_id)
            self._items[rec.instance_id] = rec
        if old is not None:
            self._retire(old, remote.CLOSE_REPLACED, "replaced by a newer connection")
        self._publish({"type": "upsert", "item": rec.item()})
        if self._sweeper is None or self._sweeper.done():
            self._sweeper = self._loop.create_task(self._sweep_loop())

    def unregister(self, rec: _Recorder) -> None:
        """Called when ``rec``'s socket ends; never removes a newer entry."""
        self._fail_pending(rec)
        with self._lock:
            if self._items.get(rec.instance_id) is not rec:
                return
            del self._items[rec.instance_id]
            empty = not self._items
        self._publish({"type": "remove", "instance_id": rec.instance_id})
        if empty and self._sweeper is not None and not self._sweeper.done():
            self._sweeper.cancel()
            self._sweeper = None

    def touch(self, rec: _Recorder) -> None:
        rec.last_seen = self.clock()

    def update_state(self, rec: _Recorder, state: Dict[str, Any]) -> None:
        """Take a new snapshot; only publishes when something visible changed."""
        if rec.dead or self.get(rec.instance_id) is not rec:
            return
        changed = state != rec.state
        rec.state = state
        if changed:
            self._publish({"type": "upsert", "item": rec.item()})

    def sweep(self, now: Optional[float] = None) -> List[str]:
        """Drop recorders silent for ``stale_after`` seconds (close 4408)."""
        now = self.clock() if now is None else now
        with self._lock:
            stale = [r for r in self._items.values() if now - r.last_seen > self.stale_after]
            for rec in stale:
                del self._items[rec.instance_id]
        for rec in stale:
            logger.info("recorder %s (%s) went silent; dropping", rec.instance_id[:8], rec.device)
            self._retire(rec, remote.CLOSE_IDLE, "idle too long")
            self._publish({"type": "remove", "instance_id": rec.instance_id})
        return [r.instance_id for r in stale]

    async def _sweep_loop(self) -> None:
        # Sleeps between passes and ends once nobody is connected.
        while self.get_count():
            await asyncio.sleep(self.sweep_every)
            self.sweep()

    def get_count(self) -> int:
        with self._lock:
            return len(self._items)

    def _retire(self, rec: _Recorder, code: int, reason: str) -> None:
        """Mark ``rec`` dead, fail its waiters and close its socket (loop-safe)."""
        rec.dead = True
        self._fail_pending(rec)

        async def _close() -> None:
            try:
                await rec.ws.close(code=code, reason=reason)
            except Exception:  # noqa: BLE001 - already gone
                pass

        self._on_loop(lambda: asyncio.ensure_future(_close()))

    @staticmethod
    def _fail_pending(rec: _Recorder) -> None:
        pending, rec.pending = rec.pending, {}
        for fut in pending.values():
            if not fut.done():
                fut.get_loop().call_soon_threadsafe(_set_gone, fut)

    def _on_loop(self, fn: Callable[[], Any]) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(fn)
        except RuntimeError:
            pass

    # -- events feed --------------------------------------------------------

    def subscribe(self) -> Optional[_Subscriber]:
        """New feed subscriber primed with a snapshot; None when at the cap."""
        self._loop = asyncio.get_running_loop()
        if len(self._subs) >= MAX_SUBSCRIBERS:
            return None
        sub = _Subscriber()
        sub.queue.put_nowait({"type": "snapshot", "items": self.list_items()})
        self._subs.append(sub)
        return sub

    def unsubscribe(self, sub: _Subscriber) -> None:
        if sub in self._subs:
            self._subs.remove(sub)

    def _publish(self, message: Dict[str, Any]) -> None:
        self._on_loop(lambda: self._deliver(message))

    def _deliver(self, message: Dict[str, Any]) -> None:
        for sub in list(self._subs):
            try:
                sub.queue.put_nowait(message)
            except asyncio.QueueFull:
                # A subscriber that can't keep up is dropped, never waited on.
                self.unsubscribe(sub)
                while not sub.queue.empty():
                    sub.queue.get_nowait()
                sub.queue.put_nowait(None)

    # -- commands -----------------------------------------------------------

    async def send_command(self, instance_id: str, command: str, args: Dict[str, str]) -> Dict[str, Any]:
        """Forward a command and wait for its ack.

        Raises ``KeyError`` (not connected), ``RecorderGone``, ``CommandTimeout``
        or ``BufferError`` (too many commands already in flight).
        """
        rec = self.get(instance_id)
        if rec is None or rec.dead:
            raise KeyError(instance_id)
        if len(rec.pending) >= MAX_PENDING_COMMANDS:
            raise BufferError("too many commands in flight")
        command_id = uuid.uuid4().hex
        fut: "asyncio.Future[Dict[str, Any]]" = asyncio.get_running_loop().create_future()
        rec.pending[command_id] = fut
        frame = json.dumps({"type": "command", "command_id": command_id, "command": command, "args": args})
        try:
            try:
                async with rec.send_lock:
                    await asyncio.wait_for(rec.ws.send_text(frame), self.command_timeout)
            except Exception as exc:  # noqa: BLE001 - dead socket or stalled send
                raise RecorderGone() from exc
            try:
                ack = await asyncio.wait_for(fut, self.command_timeout)
            except asyncio.TimeoutError:
                raise CommandTimeout() from None
        finally:
            rec.pending.pop(command_id, None)
        logger.info("command %s on %s (%s): ok=%s", command, instance_id[:8], rec.device, ack.get("ok"))
        return ack

    def resolve_ack(self, rec: _Recorder, message: Dict[str, Any]) -> None:
        """Match an ack frame to its waiting command and refresh state."""
        command_id = message.get("command_id")
        fut = rec.pending.get(command_id) if isinstance(command_id, str) else None
        if "state" in message:
            self.update_state(rec, remote.sanitize_state(message.get("state")))
        if fut is None or fut.done():
            return
        code = message.get("code")
        error = message.get("error")
        fut.set_result({
            "ok": message.get("ok") is True,
            "code": remote._opt_text(code, 40) if isinstance(code, str) else None,
            "error": remote._opt_text(error) if isinstance(error, str) else None,
            "state": rec.state,
        })


def _set_gone(fut: "asyncio.Future[Any]") -> None:
    if not fut.done():
        fut.set_exception(RecorderGone())


# -- websocket helpers -------------------------------------------------------


async def _read_json(ws: WebSocket, timeout: Optional[float]) -> Optional[Dict[str, Any]]:
    """Next frame as a dict; ``{}`` for a junk frame; None when the socket ended."""
    try:
        message = await asyncio.wait_for(ws.receive(), timeout)
    except (asyncio.TimeoutError, RuntimeError):
        return None
    if message.get("type") != "websocket.receive":
        return None
    text = message.get("text")
    if text is None or len(text) > remote.MAX_MESSAGE_BYTES:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


async def _close(ws: WebSocket, code: int, reason: str = "") -> None:
    try:
        await ws.close(code=code, reason=reason)
    except Exception:  # noqa: BLE001 - peer already gone
        pass


def _hello_fields(hello: Dict[str, Any], client_info: Optional[compat.ClientInfo], host: str) -> Optional[Dict[str, Any]]:
    """Validated identity from a hello frame, or None when it is unusable."""
    if hello.get("type") != "hello" or hello.get("protocol") != remote.PROTOCOL_VERSION:
        return None
    instance_id = hello.get("instance_id")
    if not remote.valid_instance_id(instance_id):
        return None
    version = remote._text(hello.get("version"), 32)
    if compat.version_key(version) is None:
        version = client_info.version if client_info else ""
    platform_text = remote._text(hello.get("platform"), 60) or (client_info.platform if client_info else "")
    device = remote._text(hello.get("device")) or (f"recorder at {host}" if host else "recorder")
    return {
        "instance_id": instance_id,
        "device": device,
        "platform_text": platform_text,
        "version": version,
        "state": remote.sanitize_state(hello.get("state")),
    }


def _web_authorized(ws: WebSocket) -> bool:
    token = auth.token_from_websocket(ws.query_params.get("token"), ws.headers.get("authorization"))
    return auth.token_is_valid(token or ws.cookies.get(auth.WEB_TOKEN_COOKIE))


def _same_origin(ws: WebSocket) -> bool:
    """Reject a browser page on another site (cross-site websocket hijacking)."""
    origin = ws.headers.get("origin")
    if not origin:
        return True
    host = ws.headers.get("host", "")
    return urlsplit(origin).netloc.lower() == host.lower()


# -- installation ------------------------------------------------------------


def install_recorders(app: FastAPI, *, hub: Optional[RecorderHub] = None) -> RecorderHub:
    """Register the recorder routes on ``app``; the hub is on ``app.state.recorder_hub``."""
    hub = hub or RecorderHub()
    app.state.recorder_hub = hub

    @app.websocket(remote.CONNECT)
    async def recorder_connect(websocket: WebSocket) -> None:
        await websocket.accept()
        if not auth.authorize_websocket(websocket.query_params.get("token"), websocket.headers.get("authorization")):
            await _close(websocket, remote.CLOSE_UNAUTHORIZED, "unauthorized")
            return
        host = websocket.client.host if websocket.client else ""
        client_info = getattr(websocket.state, "client_info", None)
        hello = await _read_json(websocket, HELLO_TIMEOUT)
        fields = _hello_fields(hello, client_info, host) if hello else None
        if fields is None:
            await _close(websocket, remote.CLOSE_BAD_HELLO, "bad hello")
            return
        if hub.is_full(fields["instance_id"]):
            await _close(websocket, CLOSE_TOO_MANY, "too many recorders")
            return
        rec = _Recorder(websocket, address=host, now=hub.clock(), **fields)
        try:
            # Register and welcome under the send lock so a command that is
            # forwarded the instant the recorder is listed still arrives after
            # the welcome frame.
            async with rec.send_lock:
                hub.register(rec)
                await websocket.send_json({"type": "welcome", "protocol": remote.PROTOCOL_VERSION,
                                           "server_version": __version__})
        except Exception:  # noqa: BLE001 - dropped during the handshake
            hub.unregister(rec)
            return
        logger.info("recorder %s (%s, %s) connected", rec.instance_id[:8], rec.device, rec.version or "?")
        try:
            junk = 0
            while not rec.dead:
                frame = await _read_json(websocket, hub.stale_after + 5)
                if frame is None:
                    break
                hub.touch(rec)
                kind = frame.get("type")
                if kind == "state":
                    junk = 0
                    hub.update_state(rec, remote.sanitize_state(frame.get("state")))
                elif kind == "ack":
                    junk = 0
                    hub.resolve_ack(rec, frame)
                else:
                    junk += 1
                    if junk > MAX_JUNK_FRAMES:
                        await _close(websocket, remote.CLOSE_BAD_HELLO, "too many bad frames")
                        break
        finally:
            hub.unregister(rec)
            logger.info("recorder %s (%s) disconnected", rec.instance_id[:8], rec.device)

    @app.get(remote.LIST)
    async def list_recorders(_auth: None = Depends(auth.require_token)):
        """Recorders connected right now, with version and live state."""
        return {
            "items": hub.list_items(),
            "server_version": __version__,
            "min_client_version": compat.min_client_version(),
        }

    @app.websocket(remote.EVENTS)
    async def recorder_events(websocket: WebSocket) -> None:
        await websocket.accept()
        if not _same_origin(websocket):
            await _close(websocket, CLOSE_FORBIDDEN_ORIGIN, "cross-origin")
            return
        if not _web_authorized(websocket):
            await _close(websocket, remote.CLOSE_UNAUTHORIZED, "unauthorized")
            return
        sub = hub.subscribe()
        if sub is None:
            await _close(websocket, CLOSE_TOO_MANY, "too many viewers")
            return

        async def _watch_disconnect() -> None:
            # The page never sends anything; this just notices the close.
            while await _read_json(websocket, None) is not None:
                pass

        watcher = asyncio.ensure_future(_watch_disconnect())
        try:
            while not watcher.done():
                getter = asyncio.ensure_future(sub.queue.get())
                done, _ = await asyncio.wait({getter, watcher}, timeout=EVENTS_PING_EVERY,
                                             return_when=asyncio.FIRST_COMPLETED)
                if getter not in done:
                    getter.cancel()
                    if watcher in done:
                        break
                    message: Optional[Dict[str, Any]] = {"type": "ping"}
                else:
                    message = getter.result()
                if message is None:
                    await _close(websocket, 1013, "too slow")
                    break
                await websocket.send_json(message)
        except Exception:  # noqa: BLE001 - closed mid-send
            pass
        finally:
            hub.unsubscribe(sub)
            watcher.cancel()

    @app.post(remote.COMMAND)
    async def recorder_command(instance_id: str, request: Request, _auth: None = Depends(auth.require_token)):
        """Forward a whitelisted command to one recorder and return its ack."""
        if not remote.valid_instance_id(instance_id):
            raise HTTPException(status_code=400, detail="invalid recorder id")
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(status_code=400, detail="body must be JSON") from None
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="body must be an object")
        try:
            command, args = remote.clean_command(body.get("command"), body.get("args"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            return await hub.send_command(instance_id, command, args)
        except KeyError:
            raise HTTPException(status_code=404, detail="recorder is not connected") from None
        except BufferError:
            raise HTTPException(status_code=429, detail="the recorder is still busy with earlier commands") from None
        except CommandTimeout:
            raise HTTPException(status_code=504, detail="the recorder did not answer in time") from None
        except RecorderGone:
            raise HTTPException(status_code=409, detail="the recorder disconnected") from None

    return hub
