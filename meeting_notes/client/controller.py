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

import logging
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from meeting_notes import config as config_mod
from meeting_notes import wire
from meeting_notes.audio.session import RecordingSession, create_session_dir

log = logging.getLogger("meeting_notes.client.controller")

IDLE = "idle"
RECORDING = "recording"
STOPPING = "stopping"

# How long a cached queue_status() answer stays valid. queue_status() is
# polled by the UI's 33ms timer, and pending() re-globs the queue directory
# and re-parses every entry's JSON on every call -- fine once, ruinous 30x/sec
# forever. A short cache means the status line still updates well within
# human-perceptible time, without the disk churn.
_QUEUE_STATUS_CACHE_SECONDS = 1.0


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
        self._uploader = None
        self._queue = None
        self._queue_status_cache: Optional[Dict[str, int]] = None
        self._queue_status_cached_at: float = 0.0
        self.device_diagnostic_path: Optional[Path] = None
        self._last_stream_state: Optional[str] = None
        self._last_stream_error: Optional[str] = None

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
                log.warning("device probe: %s unavailable: %s", kind, exc)
        log.info("device probe: mic=%r system=%r", found.get("mic"), found.get("system"))
        try:
            report = devices_mod.audio_diagnostic_report()
            path = Path.home() / ".meeting-notes" / "audio-device-diagnostic.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(report, encoding="utf-8")
            self.device_diagnostic_path = path
        except Exception as exc:  # noqa: BLE001 - diagnostics must never block startup
            self.device_diagnostic_path = None
            log.warning("could not write the audio diagnostic: %s", exc)
        return found

    # -- lifecycle -----------------------------------------------------------

    def start(self, name: str = "", sources: Optional[Dict[str, object]] = None) -> Optional[Path]:
        """Begin recording. ``sources`` overrides device discovery.

        The override exists so the whole path -- capture, live stream, queue,
        upload -- can be driven in a test with fake sources. Without it the
        only code reachable off a machine with audio hardware is the failure
        branch, which is exactly how three integration bugs got shipped past a
        green test suite.
        """
        if self.state != IDLE:
            return self.session_dir
        self.error = None
        self._partials.clear()
        self._recording_name = name

        cfg = config_mod.load_config()
        problems: list = []
        if sources is None:
            from meeting_notes.audio import devices as devices_mod

            sources = {}
            for kind in ("mic", "system"):
                try:
                    sources[kind] = devices_mod.resolve_source(kind, cfg.get(kind))
                except Exception as exc:  # noqa: BLE001
                    problems.append(f"{kind}: {exc}")
        if not sources:
            self.error = "; ".join(problems) or "no audio devices available"
            log.error("recording could not start: %s", self.error)
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
        log.info(
            "recording started: dir=%s tracks=%s name=%r problems=%s",
            self.session_dir, sorted(sources), name, "; ".join(problems) or "none",
        )
        self._thread = threading.Thread(target=self._supervise, daemon=True, name="supervisor")
        self._thread.start()
        return self.session_dir

    def _supervise(self) -> None:
        try:
            self.session.supervise()
        except Exception as exc:  # noqa: BLE001
            self.error = f"{type(exc).__name__}: {exc}"
            log.exception("recording supervisor failed")

    def stop(self) -> Optional[dict]:
        if self.state != RECORDING or self.session is None:
            return None
        self.state = STOPPING
        self.session.request_stop()
        if self._thread:
            self._thread.join(timeout=3.0)
        meta = self.session.finalize()
        # Preserve the user-facing meeting name and recorder identity in the
        # authoritative metadata uploaded to the server. The directory slug
        # is an implementation detail and is not a useful history-table name.
        import platform
        import socket
        import json

        meta["name"] = getattr(self, "_recording_name", "")
        meta["device"] = socket.gethostname()
        meta["platform"] = meta.get("platform") or f"{platform.system()} {platform.release()}"
        (Path(self.session_dir) / "session.json").write_text(
            json.dumps(meta, indent=2), encoding="utf-8"
        )
        self.last_meta = meta
        self._stop_streamer()
        self._queue_for_upload()
        self.state = IDLE
        log.info(
            "recording stopped: dir=%s duration=%.1fs", self.session_dir, float(meta.get("duration_sec") or 0)
        )
        return meta

    # -- per-source controls -------------------------------------------------

    def set_source_muted(self, track: str, muted: bool) -> bool:
        """Mute/unmute one source without interrupting any other source.

        A muted recorder continues consuming its device and writing timeline
        aligned silence.  This is intentionally not implemented by stopping
        or replacing the source: either operation can wedge native audio
        backends and would also interrupt live preview for the whole meeting.
        """
        if self.state != RECORDING or self.session is None:
            return False
        return self.session.set_track_muted(track, muted)

    def source_muted(self, track: str) -> bool:
        if self.session is None:
            return False
        return self.session.track_muted(track)

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

    def _record_partial(self, partial) -> None:
        """Normalise to a plain dict before anything downstream sees it.

        The streamer hands over a ``wire.Partial`` dataclass. The UI reads
        ``partials()`` as mappings, and a dataclass has no ``.get()``, so
        without this the first live partial raises AttributeError inside the Qt
        timer tick. Converting here keeps one canonical shape rather than
        making every consumer know which side produced it.
        """
        if not isinstance(partial, dict):
            partial = wire.to_json(partial)
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
            self._session_queue(cfg).enqueue(self.session_dir)
        except Exception as exc:  # noqa: BLE001
            self.error = f"could not queue for upload: {exc}"
            log.error("could not queue %s for upload: %s", self.session_dir, exc)
        else:
            log.info("queued %s for upload", self.session_dir)

    # -- upload queue --------------------------------------------------------

    def _session_queue(self, cfg: Optional[dict] = None):
        """The queue lives in its own directory beside the recordings.

        Via for_save_dir, not SessionQueue(save_dir): the constructor takes the
        QUEUE directory, so passing the save directory scattered JSON state
        files among the user's meeting folders.
        """
        from meeting_notes.client.queue import SessionQueue

        if self._queue is None:
            cfg = config_mod.load_config() if cfg is None else cfg
            self._queue = SessionQueue.for_save_dir(config_mod.save_dir(cfg))
        return self._queue

    def session_queue(self):
        """The upload queue for the configured save folder (public accessor)."""
        return self._session_queue()

    def reupload_recordings(self, folders):
        """Put saved recordings back on the queue and wake the uploader.

        Each folder keeps its original session id (its folder name); its
        track acknowledgements are cleared so every track is sent again and
        the session is finalised again -- see ``SessionQueue.requeue``. Works
        even when "upload finished recordings" is off, because the person
        asked for these uploads explicitly.
        """
        from meeting_notes.client import recordings

        queue = self._session_queue()
        result = recordings.reupload(queue, folders, wake=queue.retry_all_now)
        self._queue_status_cache = None  # the status line should show them at once
        if result.total:
            self.start_uploader(force=True)
        return result

    def start_uploader(self, force: bool = False) -> bool:
        """Start draining the upload queue, and keep draining it.

        Runs for as long as the app is open rather than only after a recording.
        A meeting captured while the server was down has to upload the next
        time the app runs; tying the uploader to "just finished recording"
        would mean the backlog only clears if you happen to record again.
        """
        if self._uploader is not None:
            return True
        cfg = config_mod.load_config()
        server = config_mod.server_settings(cfg)
        if not server.get("url") or not (server.get("auto_upload") or force):
            return False
        try:
            from meeting_notes.client.queue import UploadWorker

            self._uploader = UploadWorker(
                self._session_queue(cfg),
                server["url"],
                server.get("token") or None,
            )
            self._uploader.start()
            log.info("uploader started for %s", server["url"])
            return True
        except Exception as exc:  # noqa: BLE001 - uploading must never block recording
            self.error = f"uploader did not start: {exc}"
            self._uploader = None
            log.error("uploader did not start: %s", exc)
            return False

    def stop_uploader(self) -> None:
        if self._uploader is None:
            return
        try:
            self._uploader.stop()
        except Exception:  # noqa: BLE001
            pass
        self._uploader = None

    def restart_uploader(self) -> None:
        """Re-read settings: the server URL, token or save folder may have changed."""
        self.stop_uploader()
        self._queue = None
        # The save folder (and therefore which queue directory is "current")
        # may have just changed, so a stale cached count from the old one
        # must not linger until its TTL expires.
        self._queue_status_cache = None
        try:
            # Settings just changed -- the token or URL may now be right, so
            # everything waiting (given up, or parked at the auth backoff)
            # gets another go right away.
            woken = self._session_queue().retry_all_now()
            log.info("uploader restart: %s queued entries made due immediately", woken)
        except Exception as exc:  # noqa: BLE001 - never let queue housekeeping block a restart
            log.warning("uploader restart: retry_all_now failed: %s", exc)
        self.start_uploader()

    def queue_status(self) -> Dict[str, int]:
        """Counts for the status line. Never raises -- it runs on every UI tick.

        Cached for _QUEUE_STATUS_CACHE_SECONDS: see that constant's comment.
        """
        now = time.monotonic()
        if (
            self._queue_status_cache is not None
            and (now - self._queue_status_cached_at) < _QUEUE_STATUS_CACHE_SECONDS
        ):
            return self._queue_status_cache
        try:
            entries = self._session_queue().pending()
        except Exception:  # noqa: BLE001
            result = {"pending": 0, "failed": 0, "last_error": ""}
        else:
            # The most recent error across the queue, so the status line can
            # say WHY an upload is stuck (403 from a wrong token, server
            # refusing connections) instead of just that it is.
            errored = [e for e in entries if e.get("last_error")]
            errored.sort(key=lambda e: e.get("last_attempt_at") or 0)
            result = {
                "pending": sum(1 for e in entries if e.get("status") != "failed"),
                "failed": sum(1 for e in entries if e.get("status") == "failed"),
                "last_error": str(errored[-1]["last_error"]) if errored else "",
            }
        self._queue_status_cache = result
        self._queue_status_cached_at = now
        return result

    def queue_progress(self) -> Dict[str, object]:
        """Return the most useful persisted lifecycle state for the UI.

        Kept separate from ``queue_status`` so existing callers that compare
        its compact count mapping remain compatible.  The worker writes these
        fields as it streams each bounded chunk and while polling the server
        job, so this method never performs network work.
        """
        try:
            entries = self._session_queue().pending()
        except Exception:
            return {}
        active = [
            entry
            for entry in entries
            if entry.get("status") != "failed"
            and (
                entry.get("upload_state") in {"uploading", "pending"}
                or entry.get("transcription_state") in {"transcribing", "pending"}
            )
        ]
        if not active:
            return {}
        # FIFO order mirrors queue order, while percentages describe the
        # oldest active meeting (the one a user is most likely waiting for).
        entry = active[0]
        return {
            "upload_state": entry.get("upload_state", "pending"),
            "upload_percent": float(entry.get("upload_percent") or 0.0),
            "transcription_state": entry.get("transcription_state", "pending"),
            "transcription_percent": float(entry.get("transcription_percent") or 0.0),
        }

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
        state = getattr(self._streamer, "state", "unknown")
        if state != self._last_stream_state:
            log.info("live stream state: %s -> %s", self._last_stream_state, state)
            self._last_stream_state = state
        return state

    def stream_error(self) -> Optional[str]:
        """A human-readable reason the live preview gave up for good, or
        ``None`` while it's still connecting/retrying (a transient failure
        isn't worth alarming the user over -- see streamer.py's docstring)."""
        if self._streamer is None:
            return None
        error = getattr(self._streamer, "permanent_error", None)
        if error != self._last_stream_error:
            if error:
                log.error("live stream gave up: %s", error)
            self._last_stream_error = error
        return error
