# COMPAT FIXTURE - do not edit. Verbatim copy of meeting_notes/client/control_channel.py from the 0.7.6 client
# (release/0.7.6), with only the meeting_notes.* imports rewritten to be package-relative.
"""The recorder's side of live presence and remote control (see ``meeting_notes.remote``).

One daemon thread keeps an authenticated websocket open to the server. It pushes
the window's state snapshot (a change at once, otherwise a refresh that doubles
as the heartbeat) and receives commands from the web UI's Recorders page.

Like the live preview (``streamer.py``) this is a convenience and never the
source of truth: nothing that happens on this socket -- a server without the
endpoint, a dropped connection, a bad frame, a failing handler -- may raise into
or block the recording/UI thread. ``publish`` is a dict assignment under a lock;
the command handler runs on this thread and is expected to hand the work to the
UI thread (the window does, through a Qt signal) and answer later with
``send_ack``.
"""

from __future__ import annotations

import json
import logging
import queue
import random
import socket
import threading
import time
import uuid
from typing import Any, Callable, Dict, Optional, Tuple

from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import connect as ws_connect

from . import __version__, remote, wire
from . import identity
from .streamer import _to_ws_url

log = logging.getLogger("meeting_notes.client.control")

_INITIAL_BACKOFF = 0.5
_MAX_BACKOFF = 30.0
_IDLE_POLL = 3.0                # no server configured: look again this often
_NOT_FOUND_RETRY = 30 * 60.0    # server without the endpoint (HTTP 404 / 403)
_UNAUTHORIZED_RETRY = 60.0      # token rejected (4401 / HTTP 401)
_RECV_POLL = 0.1                # how often the loop looks for acks to send / a stop request
_CONFIG_CHECK_EVERY = 3.0       # how often a live connection re-reads url/token
_STABLE_AFTER = 5.0             # a connection this old was "good": backoff restarts

# Close codes after which retrying fast would just get the same answer.
_SLOW_CLOSE_CODES = (remote.CLOSE_UNAUTHORIZED, remote.CLOSE_BAD_HELLO)

# websockets logs every handshake header (the bearer token) at DEBUG; this logger never does.
_WS_LOG = logging.getLogger("meeting_notes.client.control.ws")
_WS_LOG.setLevel(logging.INFO)

Config = Tuple[str, str]  # (server url, token)


def refusal_code(error: ValueError) -> str:
    """Ack code for a ``remote.clean_command`` failure."""
    return "unknown_command" if str(error) == "unknown command" else "bad_args"


def _stable_key(state: Dict[str, Any]) -> str:
    """The snapshot minus what changes every few milliseconds (levels, clock)."""
    stable = json.loads(json.dumps(state))
    stable["meeting"].pop("elapsed_sec", None)
    for track in stable["tracks"].values():
        track.pop("level", None)
        track.pop("peak", None)
    return json.dumps(stable, sort_keys=True)


class ControlChannel:
    def __init__(
        self,
        get_config: Callable[[], Config],
        on_command: Callable[[str, str, Dict[str, str]], None],
        *,
        instance_id: Optional[str] = None,
        device: Optional[str] = None,
        platform_text: Optional[str] = None,
        version: str = __version__,
        connect: Optional[Callable[..., Any]] = None,
        backoff_initial: float = _INITIAL_BACKOFF,
        backoff_max: float = _MAX_BACKOFF,
        idle_poll: float = _IDLE_POLL,
        not_found_retry: float = _NOT_FOUND_RETRY,
        unauthorized_retry: float = _UNAUTHORIZED_RETRY,
        min_send_gap: float = remote.MIN_SEND_GAP,
    ):
        self._get_config = get_config
        self._on_command = on_command
        # One id per app launch: the server uses it to replace a stale connection.
        self.instance_id = instance_id or uuid.uuid4().hex
        self._device = device or socket.gethostname()
        self._platform = platform_text or identity.platform_label()
        self._version = version
        self._connect = connect or ws_connect
        self._backoff_initial = backoff_initial
        self._backoff_max = backoff_max
        self._idle_poll = idle_poll
        self._not_found_retry = not_found_retry
        self._unauthorized_retry = unauthorized_retry
        self._min_gap = min_send_gap

        self._lock = threading.Lock()
        self._latest: Any = None
        self._version_counter = 0
        self._acks: "queue.Queue[dict]" = queue.Queue(maxsize=64)
        self._command_names: Dict[str, str] = {}   # command_id -> command name, for sanitizing its result
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._noted: set = set()      # states already announced at INFO since the last stable connection
        self._connected_at: Optional[float] = None
        self.connected = False  # read-only status for tests and diagnostics

    # -- public (any thread) ---------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="control-channel", daemon=True)
        self._thread.start()

    def stop(self, join_timeout: float = 1.0) -> None:
        """Ask the thread to close the socket and exit; never raises, waits briefly."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=join_timeout)

    def publish(self, snapshot: Any) -> None:
        """Hand over the latest window state. Cheap and non-blocking."""
        with self._lock:
            self._latest = snapshot
            self._version_counter += 1

    def send_ack(
        self,
        command_id: str,
        ok: bool,
        code: Optional[str] = None,
        error: Optional[str] = None,
        snapshot: Any = None,
        result: Any = None,
    ) -> None:
        """Answer a command. Thread-safe; sent by the channel thread, dropped if disconnected.

        ``result`` (recordings commands only) rides along in the ack, normalized by
        ``remote.sanitize_result`` and dropped if it would not fit ``remote.MAX_RESULT_BYTES``.
        """
        if snapshot is None:
            with self._lock:
                snapshot = self._latest
        frame = {
            "type": "ack",
            "command_id": command_id,
            "ok": bool(ok),
            "code": code,
            "error": error,
            "state": remote.sanitize_state(snapshot),
        }
        if result is not None:
            clean = remote.sanitize_result(self._command_names.pop(command_id, ""), result)
            if clean is not None and len(json.dumps(clean, separators=(",", ":"))) <= remote.MAX_RESULT_BYTES:
                frame["result"] = clean
        self._command_names.pop(command_id, None)
        try:
            self._acks.put_nowait(frame)
        except queue.Full:
            log.debug("ack queue full; dropping ack for %s", command_id)

    # -- thread ------------------------------------------------------------------

    def _note(self, key: str, message: str, *args: Any) -> None:
        """INFO the first time a state is seen, DEBUG while it repeats.

        A connection that held for a while resets this, so the next outage is announced again.
        """
        if self._connected_at is not None:
            if time.monotonic() - self._connected_at >= _STABLE_AFTER:
                self._noted.clear()
            self._connected_at = None
        level = logging.DEBUG if key in self._noted else logging.INFO
        self._noted.add(key)
        log.log(level, message, *args)

    def _config(self) -> Config:
        try:
            url, token = self._get_config()
        except Exception:  # noqa: BLE001 - settings unreadable: behave as "not configured"
            return "", ""
        return (url or "").strip(), token or ""

    def _sleep(self, seconds: float, config: Config) -> None:
        """Wait, but wake early on stop or when the url/token in Settings changed."""
        deadline = time.monotonic() + seconds
        while not self._stop.is_set():
            left = deadline - time.monotonic()
            if left <= 0:
                return
            if self._stop.wait(min(left, self._idle_poll)):
                return
            if self._config() != config:
                return

    def _run(self) -> None:
        backoff = self._backoff_initial
        while not self._stop.is_set():
            config = self._config()
            url, token = config
            if not url:
                self._stop.wait(self._idle_poll)
                continue
            started = time.monotonic()
            delay = None  # None: ordinary backoff
            try:
                self._session(url, token, config)
            except InvalidStatus as exc:
                status = exc.response.status_code
                if status in (403, 404):
                    # An old server has no such route: 404, or 403 when it closes the
                    # websocket before accepting it. A bad token is 4401 after accept.
                    self._note(
                        "no-endpoint",
                        "server has no remote-control endpoint (HTTP %d); checking again in %d min",
                        status,
                        int(self._not_found_retry // 60),
                    )
                    delay = self._not_found_retry
                elif status == 401:
                    self._note("unauthorized", "remote control: server rejected the token (HTTP %d)", status)
                    delay = self._unauthorized_retry
                else:
                    self._note(f"http-{status}", "remote control: server answered HTTP %d", status)
            except ConnectionClosed as exc:
                code = exc.rcvd.code if exc.rcvd is not None else None
                if code in _SLOW_CLOSE_CODES:
                    self._note(f"closed-{code}", "remote control: server closed the connection (%s)", code)
                    delay = self._unauthorized_retry
                elif code == remote.CLOSE_REPLACED:
                    self._note("replaced", "remote control: replaced by a newer connection from this app")
                else:
                    self._note("disconnected", "remote control: disconnected (%s)", code or "no close code")
            except Exception as exc:  # noqa: BLE001 - any transport failure means retry
                self._note(f"error-{type(exc).__name__}", "remote control: cannot reach the server (%s)", type(exc).__name__)
            finally:
                self.connected = False
            if self._stop.is_set():
                return
            if time.monotonic() - started >= _STABLE_AFTER:
                backoff = self._backoff_initial
            if delay is None:
                self._sleep(backoff * random.uniform(0.8, 1.2), config)
                backoff = min(backoff * 2, self._backoff_max)
            else:
                self._sleep(delay, config)

    def _session(self, url: str, token: str, config: Config) -> None:
        headers = {**wire.auth_headers(token or None), **identity.client_headers()}
        with self._connect(
            _to_ws_url(url) + remote.CONNECT,
            additional_headers=headers,
            open_timeout=5,
            close_timeout=1,
            max_size=remote.MAX_COMMAND_BYTES,
            logger=_WS_LOG,
        ) as ws:
            while not self._acks.empty():  # acks for commands of an older connection
                self._acks.get_nowait()
            last_key, state = self._current_state()
            self._send(ws, {
                "type": "hello",
                "protocol": remote.PROTOCOL_VERSION,
                "instance_id": self.instance_id,
                "device": self._device,
                "platform": self._platform,
                "version": self._version,
                "state": state,
            })
            self.connected = True
            self._note("connected", "remote control: connected to %s", url)
            self._connected_at = time.monotonic()
            last_send = time.monotonic()
            seen_version = self._version_counter
            last_config_check = last_send
            while not self._stop.is_set():
                while True:
                    try:
                        frame = self._acks.get_nowait()
                    except queue.Empty:
                        break
                    self._send(ws, frame)
                    last_key, last_send = _stable_key(frame["state"]), time.monotonic()
                now = time.monotonic()
                if self._version_counter != seen_version:  # a new snapshot arrived
                    seen_version = self._version_counter
                    key, state = self._current_state()
                else:
                    key = None
                every = remote.SEND_EVERY_IDLE if state["status"] == "idle" else remote.SEND_EVERY_RECORDING
                since = now - last_send
                if since >= every or (key is not None and key != last_key and since >= self._min_gap):
                    # A change goes out at once (not faster than the min gap); otherwise
                    # the periodic refresh, which doubles as the heartbeat.
                    self._send(ws, {"type": "state", "state": state})
                    last_key, last_send = (key if key is not None else last_key), now
                elif key is not None and key != last_key:
                    seen_version -= 1  # changed but too soon after the last send: retry next turn
                if now - last_config_check >= _CONFIG_CHECK_EVERY:
                    last_config_check = now
                    if self._config() != config:
                        log.info("remote control: server settings changed; reconnecting")
                        return
                try:
                    message = ws.recv(timeout=_RECV_POLL)
                except TimeoutError:
                    continue
                self._handle(ws, message)

    def _current_state(self) -> Tuple[str, Dict[str, Any]]:
        with self._lock:
            raw = self._latest
        state = remote.sanitize_state(raw)
        return _stable_key(state), state

    @staticmethod
    def _send(ws: Any, frame: dict) -> None:
        ws.send(json.dumps(frame, separators=(",", ":")))

    def _handle(self, ws: Any, message: Any) -> None:
        if isinstance(message, (bytes, bytearray)):
            return
        try:
            data = json.loads(message)
        except (TypeError, ValueError):
            return
        if not isinstance(data, dict) or data.get("type") != "command":
            return  # "welcome" and anything newer we do not understand
        command_id = data.get("command_id")
        if not remote.valid_command_id(command_id):
            return
        try:
            name, args = remote.clean_command(data.get("command"), data.get("args"))
        except ValueError as exc:
            self.send_ack(command_id, False, refusal_code(exc), str(exc))
            return
        if name in remote.RECORDING_COMMANDS:
            if len(self._command_names) > 64:
                self._command_names.clear()
            self._command_names[command_id] = name
        try:
            self._on_command(command_id, name, args)
        except Exception as exc:  # noqa: BLE001 - a handler bug must not kill the channel
            log.exception("remote command handler failed")
            self.send_ack(command_id, False, "failed", f"{type(exc).__name__}: {exc}")
