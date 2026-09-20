"""The authoritative final-pass transcription queue.

The live preview (``live.py``) is explicitly disposable -- a fast model on
whatever audio has arrived so far. This is the other half of the two-pass
design described in ``wire``'s module docstring: once the client uploads the
complete recording, a job here runs the real (optionally slower, optionally
larger) model against each track's full WAV and produces the transcript that
actually gets kept.

A single background thread processes one job at a time. That's a deliberate
simplification, not an oversight: this server is CPU-only and a single
faster-whisper call already saturates it, so running jobs concurrently would
only make each one slower without doing the meeting-note-taker any favours.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Callable, Optional

from .. import wire
from ..timing import load_timing_log
from ..transcribe.merge import merge_tracks, render_json, render_markdown
from ..transcribe.protocol import Transcriber
from . import store as store_mod

logger = logging.getLogger("meeting_notes.server.jobs")

# session_id, settings -> Transcriber. Injectable so tests can hand in a stub
# that returns canned Segments -- no Whisper model can be loaded in most test
# environments (this one included: no network to fetch weights).
TranscriberFactory = Callable[..., Transcriber]


class JobQueue:
    """FIFO of finalize jobs, worked off by one background thread."""

    def __init__(self, store: store_mod.Store, transcriber_factory: Optional[TranscriberFactory]):
        self.store = store
        self.transcriber_factory = transcriber_factory
        self._queue: "queue.Queue[Optional[str]]" = queue.Queue()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="meeting-notes-jobs", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._queue.put(None)  # unblock a queue.get() that's waiting
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def enqueue(self, session_id: str, settings: Optional[dict] = None) -> str:
        job_id = self.store.create_job(session_id, settings or {})
        self._queue.put(job_id)
        return job_id

    # -- worker ---------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                job_id = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if job_id is None:
                continue
            # A single bad job (a corrupt WAV, a transcriber that raises, a
            # missing timing log) must not take the worker thread down --
            # every later job would then sit QUEUED forever with no one
            # servicing the queue. So the entire per-job body is guarded, and
            # the failure is recorded on that job rather than re-raised.
            try:
                self._process(job_id)
            except Exception as exc:  # noqa: BLE001 - see comment above
                logger.exception("job %s failed", job_id)
                try:
                    self.store.update_job(
                        job_id, state=wire.JobState.ERROR, error=str(exc), progress=1.0
                    )
                except Exception:
                    # Even recording the failure failed (e.g. disk gone) --
                    # nothing more we can safely do from here.
                    logger.exception("job %s: also failed to record the error", job_id)

    def _process(self, job_id: str) -> None:
        job = self.store.read_job(job_id)
        if job is None:
            return
        session_id = job["session_id"]
        settings = job.get("settings") or {}

        self.store.update_job(job_id, state=wire.JobState.RUNNING, progress=0.0, error=None)

        if self.transcriber_factory is None:
            raise RuntimeError("no transcriber is configured on this server")
        transcriber = self.transcriber_factory(**(settings.get("transcriber") or {}))

        meta = self.store.read_session_meta(session_id)
        tracks = sorted((meta.get("tracks") or {}).keys()) or list(wire.TRACKS)

        track_segments: dict = {}
        clocks: dict = {}
        total_steps = max(len(tracks), 1)
        for i, track in enumerate(tracks):
            wav_path = self.store.track_wav_path(session_id, track)
            track_segments[track] = transcriber.transcribe(wav_path, track) if wav_path.exists() else []

            timing_path = self.store.track_timing_path(session_id, track)
            if timing_path.exists():
                clocks[track] = load_timing_log(timing_path)

            self.store.update_job(job_id, progress=round((i + 1) / total_steps, 4))

        # A track can have real transcript segments but no usable clock -- no
        # timing log was uploaded, or it exists but never logged a point
        # (e.g. the client died before its first progress write). merge_tracks
        # still places that track's text (WAV-relative, flagged approximate)
        # rather than dropping it, but it's still worth a loud note here: this
        # is not something that should happen in normal operation.
        untimed = [
            t for t in tracks if track_segments.get(t) and (t not in clocks or not len(clocks[t].frames))
        ]
        if untimed:
            logger.warning(
                "job %s: no timing log for track(s) %s -- using approximate WAV-relative "
                "timestamps instead",
                job_id,
                untimed,
            )

        merged = merge_tracks(track_segments, clocks, labels=settings.get("labels"))
        markdown = render_markdown(merged, meta)
        json_text = render_json(merged, meta)
        self.store.write_transcript(job_id, markdown, json_text)

        self.store.update_job(job_id, state=wire.JobState.DONE, progress=1.0, error=None)
