"""Bridges the recorder, the live preview stream and the upload queue.

Keeps every long-running or failure-prone thing off the Qt main thread. The UI
only ever polls cheap properties from here, which is why the window cannot be
blocked by a wedged audio device or an unreachable server.

The ordering rule that shapes this file: the local recording is the artifact.
Streaming to the server is a disposable convenience, so every streaming failure
is swallowed here rather than surfaced as an error that could stop a meeting
being recorded.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from meeting_notes import config as config_mod
from meeting_notes.audio.session import RecordingSession, create_session_dir

IDLE = "idle"
RECORDING = "recording"
STOPPING = "stopping"


class RecordingController:
    def __init__(self, on_partial: Optional[Callable] = None):
        self.state = IDLE
        self.session: Optional[RecordingSession] = None
        self.session_dir: Optional[Path] = None
        self.error: Optional[str] = None
        self.on_partial = on_partial
        self._thread: Optional[threading.Thread] = None
        self._streamer = None
        self._downsamplers: Dict[str, object] = {}
        self._partials: List[dict] = []
        self._partial_lock = threading.Lock()
        self.last_meta: Optional[dict] = None

    # -- device discovery ----------------------------------------------------

    def probe_devices(self) -> Dict[str, str]:
        """Resolve devices without starting, so the UI can show what it found."""
        from meeting_notes.audio import devices as devices_mod

        found = {}
        for kind in ("mic", "system"):
            try:
                found[kind] = devices_mod.resolve_source(kind).name
            except Exception as exc:  # noqa: BLE001
                found[kind] = f"unavailable: {exc}"
        return found

    # -- lifecycle -----------------------------------------------------------

    def start(self, name: str = "") -> Optional[Path]:
        if self.state != IDLE:
            return self.session_dir
        self.error = None
        self._partials.clear()

        from meeting_notes.audio import devices as devices_mod

        cfg = config_mod.load_config()
        sources = {}
        problems = []
        for kind, key in (("mic", "mic"), ("system", "system")):
            try:
                sources[kind] = devices_mod.resolve_source(kind, cfg.get(key))
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{kind}: {exc}")
        if not sources:
            self.error = "; ".join(problems) or "no audio devices available"
            return None
        if problems:
            # One track is better than none, but say so rather than silently
            # recording half a conversation.
            self.error = "; ".join(problems)

        self.session_dir = create_session_dir(config_mod.save_dir(cfg), name)
        self._start_streamer(cfg, name)
        self.session = RecordingSession(
            session_dir=self.session_dir,
            sources=sources,
            on_block=self._mirror_block if self._streamer else None,
        )
        self.session.start()
        self.state = RECORDING
        self._thread = threading.Thread(target=self._supervise, daemon=True, name="supervisor")
        self._thread.start()
        return self.session_dir

    def _supervise(self) -> None:
        try:
            self.session.supervise()
        except Exception as exc:  # noqa: BLE001
            self.error = f"{type(exc).__name__}: {exc}"

    def stop(self) -> Optional[dict]:
        if self.state != RECORDING or self.session is None:
            return None
        self.state = STOPPING
        self.session.request_stop()
        if self._thread:
            self._thread.join(timeout=3.0)
        meta = self.session.finalize()
        self.last_meta = meta
        self._stop_streamer()
        self._queue_for_upload()
        self.state = IDLE
        return meta

    # -- live preview stream -------------------------------------------------

    def _start_streamer(self, cfg: dict, name: str) -> None:
        server = config_mod.server_settings(cfg)
        if not server.get("url") or not server.get("live_preview"):
            return
        try:
            from meeting_notes.client.streamer import LiveStreamer
        except Exception:  # noqa: BLE001 - transport is optional
            return
        try:
            self._streamer = LiveStreamer(
                base_url=server["url"], token=server.get("token"), on_partial=self._record_partial
            )
            self._streamer.start(
                session_id=Path(self.session_dir).name, name=name, started_wall=time.time()
            )
        except Exception:  # noqa: BLE001
            self._streamer = None

    def _mirror_block(self, track: str, block) -> None:
        """Downsample a capture block to 16 kHz and hand it to the stream."""
        if self._streamer is None:
            return
        try:
            from meeting_notes.client.resample import Downsampler
            from meeting_notes.wav_io import downmix_mono, float_to_int16

            source_rate = self.session.sources[track].samplerate
            ds = self._downsamplers.get(track)
            if ds is None:
                ds = self._downsamplers[track] = Downsampler(source_rate)
            mono16 = ds.process(downmix_mono(block))
            self._streamer.submit(track, float_to_int16(mono16).tobytes())
        except Exception:  # noqa: BLE001 - never let the preview hurt the recording
            pass

    def _record_partial(self, partial: dict) -> None:
        with self._partial_lock:
            self._partials.append(partial)
        if self.on_partial:
            try:
                self.on_partial(partial)
            except Exception:  # noqa: BLE001
                pass

    def partials(self) -> List[dict]:
        with self._partial_lock:
            return list(self._partials)

    def _stop_streamer(self) -> None:
        if self._streamer is None:
            return
        try:
            self._streamer.stop()
        except Exception:  # noqa: BLE001
            pass
        self._streamer = None
        self._downsamplers.clear()

    def _queue_for_upload(self) -> None:
        """Hand the finished recording to the retrying uploader.

        Queued unconditionally, even when the server is reachable right now:
        the queue is what makes an unreachable server a non-event rather than a
        lost transcript.
        """
        cfg = config_mod.load_config()
        server = config_mod.server_settings(cfg)
        if not server.get("url") or not server.get("auto_upload"):
            return
        try:
            from meeting_notes.client.queue import SessionQueue

            SessionQueue(config_mod.save_dir(cfg)).enqueue(self.session_dir)
        except Exception as exc:  # noqa: BLE001
            self.error = f"could not queue for upload: {exc}"

    # -- polled by the UI ----------------------------------------------------

    @property
    def elapsed(self) -> float:
        return self.session.elapsed if self.session and self.state != IDLE else 0.0

    def levels(self) -> Dict[str, float]:
        if not self.session or self.state == IDLE:
            return {}
        return {t: r.last_peak for t, r in self.session.recorders.items()}

    def degraded(self) -> Dict[str, bool]:
        if not self.session:
            return {}
        return {t: r.degraded for t, r in self.session.recorders.items()}

    def stream_state(self) -> str:
        if self._streamer is None:
            return "off"
        return getattr(self._streamer, "state", "unknown")
