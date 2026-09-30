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
from ..transcribe.diarize import Diarizer, assign_speakers
from ..transcribe.protocol import Transcriber
from ..wav_io import wrap_raw_as_wav
from . import retention as retention_mod
from . import settings as settings_mod
from . import store as store_mod

logger = logging.getLogger("meeting_notes.server.jobs")

# session_id, settings -> Transcriber. Injectable so tests can hand in a stub
# that returns canned Segments -- no Whisper model can be loaded in most test
# environments (this one included: no network to fetch weights).
TranscriberFactory = Callable[..., Transcriber]
DiarizerFactory = Callable[[], Optional[Diarizer]]


class JobQueue:
    """FIFO of finalize jobs, worked off by one background thread."""

    def __init__(
        self,
        store: store_mod.Store,
        transcriber_factory: Optional[TranscriberFactory],
        diarizer_factory: Optional[DiarizerFactory] = None,
    ):
        self.store = store
        self.transcriber_factory = transcriber_factory
        self.diarizer_factory = diarizer_factory
        self._queue: "queue.Queue[Optional[str]]" = queue.Queue()
        self._stop = threading.Event()
        # Serializes "is there an active job? else create one" so concurrent
        # finalize retries can't both enqueue.
        self._ensure_lock = threading.Lock()
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

    def ensure_job(self, session_id: str, settings: Optional[dict] = None) -> str:
        """Idempotent enqueue, used by finalize.

        Rule: look at the session's newest non-superseded job.
          * queued / running / done, and no audio file of the session was
            modified after that job was created -> return that job's id
            (a retried finalize must not pile up duplicate work);
          * otherwise (no job, latest job errored, or audio changed since the
            job was created, e.g. a track uploaded afterwards) -> enqueue a
            new job.
        Explicit retranscribe calls ``enqueue`` directly and always forces one.
        """
        with self._ensure_lock:
            latest = next(
                (j for j in self.store.jobs_for_session(session_id) if not store_mod.is_superseded_job(j)),
                None,
            )
            if latest is not None and latest.get("state") in (
                wire.JobState.QUEUED,
                wire.JobState.RUNNING,
                wire.JobState.DONE,
            ):
                created = float(latest.get("created") or 0.0)
                if self.store.audio_mtime(session_id) <= created:
                    return latest["job_id"]
            return self.enqueue(session_id, settings)

    def resume_interrupted(self) -> list:
        """Re-enqueue jobs left queued/running by a previous process.

        Call once at startup, before ``start()``. Per session, only the
        newest non-superseded job may resume, and only if it is itself stale
        (queued/running): older stale jobs are marked error "superseded", and
        so is a stale job that a newer finished job already replaced. Jobs of
        sessions that no longer exist or are in the trash become error
        "meeting deleted". A resumed running job restarts from scratch
        (progress reset). Resumed jobs carry ``resumed_after_restart: true``
        and are queued oldest first. Returns the resumed job ids.
        """
        active = (wire.JobState.QUEUED, wire.JobState.RUNNING)
        by_session: dict = {}
        for row in self.store.job_records_on_disk():
            by_session.setdefault(row.get("session_id") or "", []).append(row)
        resume: list = []
        for session_id, rows in by_session.items():
            stale = [r for r in rows if r.get("state") in active]
            if not stale:
                continue
            gone = (
                not session_id
                or not store_mod.is_safe_id(session_id)
                or not self.store.session_exists(session_id)
                or self.store.is_trashed(session_id)
            )
            if gone:
                for r in stale:
                    self._fail_stale(r["job_id"], "meeting deleted")
                continue
            newest = max(
                (r for r in rows if r.get("error") != store_mod.JOB_SUPERSEDED_ERROR),
                key=lambda r: r.get("created") or 0.0,
            )
            for r in stale:
                if r["job_id"] == newest["job_id"]:
                    resume.append(r)
                else:
                    self._fail_stale(r["job_id"], store_mod.JOB_SUPERSEDED_ERROR)
        resume.sort(key=lambda r: r.get("created") or 0.0)
        ids = []
        for r in resume:
            self.store.update_job(
                r["job_id"],
                state=wire.JobState.QUEUED,
                progress=0.0,
                error=None,
                resumed_after_restart=True,
            )
            self._queue.put(r["job_id"])
            ids.append(r["job_id"])
            logger.info("job %s (session %s): resumed after restart", r["job_id"], r["session_id"])
        return ids

    def _fail_stale(self, job_id: str, reason: str) -> None:
        fields = {"state": wire.JobState.ERROR, "error": reason, "progress": 1.0}
        if reason == store_mod.JOB_SUPERSEDED_ERROR:
            fields["superseded"] = True
        self.store.update_job(job_id, **fields)

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

        # Only a meeting's *first* successful transcript counts as "new" for
        # auto-generated notes; a retranscribe of an existing meeting never
        # queues notes on its own.
        is_new_meeting = self.store.latest_done_job(session_id) is None

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
            raw_path = self.store.track_raw_path(session_id, track)
            if not wav_path.exists() and raw_path.exists():
                # Only finalize wrapped raw PCM into a WAV; a session that
                # was streamed but never finalized (the client died, or the
                # meeting was re-run from the web UI) has only the raw file.
                # Seen on a real run: that produced an empty "done"
                # transcript -- and immediate retention then deleted the
                # audio. Wrap it here so the job reads what actually exists.
                wrap_raw_as_wav(raw_path, wav_path, wire.STREAM_SAMPLE_RATE)
            track_segments[track] = transcriber.transcribe(wav_path, track) if wav_path.exists() else []
            if (
                track == "system"
                and wav_path.exists()
                and track_segments[track]
                and self.diarizer_factory is not None
            ):
                diarizer = self.diarizer_factory()
                if diarizer is not None:
                    track_segments[track] = assign_speakers(
                        track_segments[track], diarizer.diarize(wav_path)
                    )

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
        if is_new_meeting:
            self._maybe_auto_queue_review(session_id)
        self._maybe_delete_audio_immediately(session_id)

    def _maybe_auto_queue_review(self, session_id: str) -> None:
        """Queue meeting notes for a newly transcribed meeting when the
        "auto-generate notes" setting is on and an AI provider is selected.

        Uses the same ``Store.create_review`` as ``POST /v1/sessions/{id}/review``
        (idempotent: an existing queued/running/done review is returned, not
        duplicated). Best-effort: a failure here must never fail the
        transcription job, so it is logged and swallowed.
        """
        try:
            settings = settings_mod.load_settings(self.store.root)
            if not settings.auto_generate_notes or settings.ai_provider == "disabled":
                return
            self.store.create_review(session_id)
        except Exception:  # noqa: BLE001 - see docstring
            logger.exception("session %s: auto-queueing meeting notes failed", session_id)

    def _maybe_delete_audio_immediately(self, session_id: str) -> None:
        """0-day retention means "delete audio as soon as the transcript is
        done," not "within the next hour" -- so a job that just succeeded
        checks the policy for its own session right away, instead of waiting
        for RetentionWorker's next periodic sweep (retention.py's module
        docstring has the full rationale). Any other retention setting is
        left entirely to that sweep; re-implementing the rule table here
        would just be ``apply_retention`` with extra steps.

        Best-effort: a failure here must not turn a successful transcription
        job into a failed one. Retention will catch it on the next sweep
        regardless.
        """
        try:
            settings = settings_mod.load_settings(self.store.root)
            if settings.audio_retention_days == 0:
                retention_mod.apply_retention(self.store, settings, session_ids=[session_id])
        except Exception:  # noqa: BLE001 - see docstring
            logger.exception("session %s: immediate retention check failed", session_id)
