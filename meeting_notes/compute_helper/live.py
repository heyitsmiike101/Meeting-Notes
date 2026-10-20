"""The helper's live-transcription client: one outbound WebSocket to ``<server>/v1/compute/live``.

Contract: docs/gpu-helper-design.md ("Live transcription on a helper", "Changes after design review").
The server sends one ``utterance`` per mature chunk of a recording; the helper answers ``result`` or ``error``
("expired", "busy", or a message). One utterance is decoded at a time with one more allowed to wait (a third gets
``busy`` at once). The thread owns its own asyncio loop; decoding runs in an executor so pings keep flowing.
A CUDA out-of-memory disables live until the runtime restarts (batch is unaffected).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Sequence

import numpy as np

log = logging.getLogger("meeting_notes.compute_helper")

LIVE_PATH = "/v1/compute/live"
BACKOFF_SECONDS = (2, 5, 15, 30)
PING_SECONDS = 20.0
MAX_FRAME_BYTES = 1 << 21  # utterances are ~290 KB of PCM, ~390 KB base64
TARGET_RATE = 16000
STABLE_AFTER_SECONDS = 10.0  # a connection this long resets the backoff


def live_url(server: str) -> str:
    base = server.strip().rstrip("/")
    if base.lower().startswith("https://"):
        base = "wss://" + base[8:]
    elif base.lower().startswith("http://"):
        base = "ws://" + base[7:]
    return base + LIVE_PATH


def is_cuda_oom(exc: BaseException) -> bool:
    seen = 0
    while exc is not None and seen < 5:
        text = f"{type(exc).__name__} {exc}".lower()
        if "out of memory" in text or "outofmemory" in text:
            return True
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return False


def decode_pcm(b64: str, sample_rate: int = TARGET_RATE) -> np.ndarray:
    """int16 mono little-endian base64 -> float32 in [-1, 1] at 16 kHz."""
    raw = base64.b64decode(b64, validate=True)
    if len(raw) % 2:
        raw = raw[:-1]
    audio = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if sample_rate != TARGET_RATE and audio.size and sample_rate > 0:
        n = max(1, int(round(audio.size * TARGET_RATE / sample_rate)))
        audio = np.interp(np.linspace(0, audio.size - 1, n), np.arange(audio.size), audio).astype(np.float32)
    return audio


class LiveClient:
    """Keeps the live connection up while enabled and not paused; serves utterances with ``engine``.

    ``engine`` needs ``live_models() -> list[str]``, ``live_transcribe(model, audio, track) -> list[dict]``,
    ``live_unload()`` and optionally ``live_device_label``. ``on_status`` gets
    ``{connected, served, avg_ms, error}`` on every change.
    """

    def __init__(self, server: str, key: str, engine: Any, *, pack_version: str = "", gpu: Optional[dict] = None,
                 on_status: Optional[Callable[[dict], None]] = None,
                 backoff: Sequence[float] = BACKOFF_SECONDS, ping_seconds: float = PING_SECONDS,
                 clock: Callable[[], float] = time.time):
        self.url = live_url(server)
        self._key = key
        self.engine = engine
        self.pack_version = pack_version
        self.gpu = gpu
        self._on_status = on_status
        self._backoff = tuple(backoff) or (2,)
        self._ping = ping_seconds
        self._clock = clock
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._connected = False
        self._served = 0
        self._total_ms = 0.0
        self._error: Optional[str] = None
        self._disabled = False

    # -- status --------------------------------------------------------------

    def status(self) -> dict:
        with self._lock:
            avg = int(round(self._total_ms / self._served)) if self._served else None
            return {"connected": self._connected, "served": self._served, "avg_ms": avg, "error": self._error}

    def _publish(self) -> None:
        if self._on_status is not None:
            try:
                self._on_status(self.status())
            except Exception:  # noqa: BLE001
                log.debug("live status callback failed", exc_info=True)

    def _set_connected(self, value: bool) -> None:
        with self._lock:
            if self._connected == value:
                return
            self._connected = value
        self._publish()

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._thread_main, name="compute-helper-live", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)

    def set_paused(self, paused: bool) -> None:
        if paused:
            self._paused.set()
        else:
            self._paused.clear()

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception:  # noqa: BLE001 - live must never take the runtime down
            log.exception("live client crashed")
        finally:
            self._set_connected(False)

    # -- connection loop -----------------------------------------------------

    async def _sleep(self, seconds: float) -> None:
        """Sleep, waking early when stopped."""
        end = time.monotonic() + seconds
        while not self._stop.is_set() and time.monotonic() < end:
            await asyncio.sleep(min(0.05, max(0.0, end - time.monotonic())))

    async def _main(self) -> None:
        from websockets.asyncio.client import connect

        failures = 0
        while not self._stop.is_set():
            if self._disabled or self._paused.is_set():
                await self._sleep(0.05)
                continue
            began = time.monotonic()
            try:
                async with connect(
                        self.url, additional_headers={"Authorization": f"Bearer {self._key}"},
                        max_size=MAX_FRAME_BYTES, ping_interval=self._ping, ping_timeout=self._ping,
                        open_timeout=10, proxy=None) as ws:
                    await self._session(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - connection refused, handshake rejected, dropped, ...
                log.info("live connection ended: %s", type(exc).__name__ + (f": {exc}" if str(exc) else ""))
            self._set_connected(False)
            if self._paused.is_set() or self._disabled or self._stop.is_set():
                await asyncio.get_running_loop().run_in_executor(None, self._unload)
                failures = 0
                continue
            failures = 0 if time.monotonic() - began >= STABLE_AFTER_SECONDS else failures + 1
            await self._sleep(self._backoff[min(max(failures, 1) - 1, len(self._backoff) - 1)])
        await asyncio.get_running_loop().run_in_executor(None, self._unload)

    def _unload(self) -> None:
        unload = getattr(self.engine, "live_unload", None)
        if unload is not None:
            try:
                unload()
            except Exception:  # noqa: BLE001
                log.debug("live unload failed", exc_info=True)

    # -- one connection ------------------------------------------------------

    async def _send(self, ws, payload: Dict[str, Any]) -> None:
        import json

        await ws.send(json.dumps(payload, separators=(",", ":")))

    async def _session(self, ws) -> None:
        import json

        try:
            models: List[str] = list(self.engine.live_models())
        except Exception:  # noqa: BLE001
            models = []
        await self._send(ws, {"type": "hello", "models": models, "gpu": self.gpu, "pack_version": self.pack_version})
        self._set_connected(True)
        queue: "asyncio.Queue[dict]" = asyncio.Queue(maxsize=1)
        loop = asyncio.get_running_loop()

        async def watcher() -> None:
            while not (self._stop.is_set() or self._paused.is_set() or self._disabled):
                await asyncio.sleep(0.05)
            await ws.close()

        async def serve() -> None:
            while True:
                request = await queue.get()
                await self._serve_one(ws, request, loop)

        tasks = [asyncio.ensure_future(watcher()), asyncio.ensure_future(serve())]
        try:
            async for raw in ws:
                try:
                    message = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if not isinstance(message, dict) or message.get("type") != "utterance":
                    continue
                rid = message.get("id")
                if not isinstance(rid, str) or not rid:
                    continue
                # The server sends its remaining budget; turn it into a deadline on this machine's clock, so a
                # clock difference between the two PCs never expires (or keeps) work by mistake.
                budget = message.get("budget_ms")
                if isinstance(budget, (int, float)) and not isinstance(budget, bool):
                    message["_local_deadline"] = self._clock() + max(0.0, float(budget)) / 1000.0
                if self._expired(message):
                    await self._send(ws, {"type": "error", "id": rid, "error": "expired"})
                    continue
                if self._disabled:
                    await self._send(ws, {"type": "error", "id": rid, "error": "live disabled on this helper"})
                    continue
                try:
                    queue.put_nowait(message)
                except asyncio.QueueFull:
                    await self._send(ws, {"type": "error", "id": rid, "error": "busy"})
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def _expired(self, message: dict) -> bool:
        deadline = message.get("_local_deadline", message.get("deadline"))
        return isinstance(deadline, (int, float)) and not isinstance(deadline, bool) and self._clock() >= deadline

    async def _serve_one(self, ws, request: dict, loop) -> None:
        rid = request["id"]
        if self._expired(request):
            await self._send(ws, {"type": "error", "id": rid, "error": "expired"})
            return
        began = time.monotonic()
        try:
            segments = await loop.run_in_executor(None, self._transcribe, request)
        except Exception as exc:  # noqa: BLE001
            if is_cuda_oom(exc):
                with self._lock:
                    self._disabled = True
                    self._error = "CUDA out of memory (live transcription is off until the helper restarts)"
                log.error("live: %s", exc)
                self._publish()
                await self._send(ws, {"type": "error", "id": rid, "error": "CUDA out of memory"})
                await ws.close()
                return
            log.warning("live utterance failed: %s", exc)
            await self._send(ws, {"type": "error", "id": rid, "error": f"{type(exc).__name__}: {exc}"[:200]})
            return
        elapsed_ms = (time.monotonic() - began) * 1000.0
        with self._lock:
            self._served += 1
            self._total_ms += elapsed_ms
        self._publish()
        device = getattr(self.engine, "live_device_label", None) or "cuda"
        await self._send(ws, {"type": "result", "id": rid, "segments": segments, "device": device})

    def _transcribe(self, request: dict) -> List[dict]:
        model = request.get("model")
        if not isinstance(model, str) or model not in self.engine.live_models():
            raise RuntimeError(f"model {model!r} is not in the helper pack")
        rate = request.get("sample_rate")
        audio = decode_pcm(str(request.get("pcm_b64") or ""),
                           int(rate) if isinstance(rate, (int, float)) and rate > 0 else TARGET_RATE)
        if audio.size == 0:
            return []
        return [{"start": float(s["start"]), "end": float(s["end"]), "text": str(s["text"])}
                for s in self.engine.live_transcribe(model, audio, str(request.get("track") or ""))]
