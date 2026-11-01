"""Speaker labels on this server's processor, off the transcription thread.

An in-person (phone) meeting that no GPU helper took is transcribed by the normal job thread *without* speaker
labels. Labelling the one voice track can take longer than the meeting itself on a CPU, so it must not hold the
job thread (and every other meeting) hostage: the job thread hands the transcript here and moves on. This single
low-priority worker diarizes the system WAV, applies the "Speaker N" labels, and only then finishes the job
(writes the transcript, marks it DONE, queues notes). Until then the job stays RUNNING in its "speakers" stage.
Notes therefore never start on an unlabelled transcript.

The same worker also runs the speakers-only jobs ("Find speakers again") that no GPU helper took: they re-label an
existing transcript without transcribing again (``JobQueue.run_speakers_here``).
"""

from __future__ import annotations

import logging
import os
import queue
import sys
import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .. import wire
from ..transcribe.diarize import assign_speakers, speaker_labels
from . import people as people_mod
from .worker import BackgroundWorker

logger = logging.getLogger("meeting_notes.server.speakers_worker")

IN_PERSON_PREFIX = "Speaker"  # "Speaker 1", "Speaker 2", ...


@dataclass
class SpeakerTask:
    job_id: str
    prepared: Any  # jobs.Prepared
    track_segments: dict
    progress: Any  # jobs._JobProgress, shared with the transcription stage
    index: int  # the system track's step in ``progress``
    diarizer: Any


@dataclass
class SpeakersJobTask:
    """A speakers-only job (``kind == "speakers"``) to run here; everything it needs is read from the job."""

    job_id: str


def _lower_priority() -> None:
    """Best effort: make this thread yield the processor to transcription and the web server."""
    try:
        if sys.platform == "win32":
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.SetThreadPriority(kernel32.GetCurrentThread(), -1)  # THREAD_PRIORITY_BELOW_NORMAL
        elif hasattr(os, "setpriority") and hasattr(threading, "get_native_id"):
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)
    except Exception:  # noqa: BLE001 - priority is a courtesy
        logger.debug("could not lower the speaker worker's priority", exc_info=True)


class SpeakersWorker(BackgroundWorker):
    """One background thread, one task at a time."""

    thread_name = "meeting-notes-speakers"

    def __init__(self, jobs):
        super().__init__()
        self.jobs = jobs  # the JobQueue: finalizes jobs, owns the store and the diarizer factory
        self._queue: "queue.Queue[Optional[object]]" = queue.Queue()

    def _on_stop(self) -> None:
        self._queue.put(None)  # unblock a get() that is waiting

    def submit(self, task: SpeakerTask) -> None:
        self._queue.put(task)

    def submit_job(self, job_id: str) -> None:
        self._queue.put(SpeakersJobTask(job_id))

    def pending(self) -> int:
        return self._queue.qsize()

    def _run(self) -> None:
        _lower_priority()
        while not self._stop.is_set():
            try:
                task = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if task is None:
                continue
            try:
                if isinstance(task, SpeakersJobTask):
                    self.jobs.run_speakers_here(task.job_id)
                else:
                    self.process(task)
            except Exception as exc:  # noqa: BLE001 - one bad task must not stop the worker
                logger.exception("job %s: speaker labelling failed", task.job_id)
                if isinstance(task, SpeakersJobTask):
                    self.jobs._fail_speakers(task.job_id, str(exc))
                    continue
                try:
                    self.jobs.store.update_job(
                        task.job_id, state=wire.JobState.ERROR, error=str(exc), progress=1.0)
                except Exception:  # noqa: BLE001
                    logger.exception("job %s: also failed to record the error", task.job_id)

    def process(self, task: SpeakerTask) -> None:
        """Label the speakers, then finish the job. Public so tests can drive one task synchronously."""
        store = self.jobs.store
        job = store.read_job(task.job_id)
        if job is None or job.get("state") != wire.JobState.RUNNING:
            return  # deleted or superseded while waiting
        prepared, segments = task.prepared, task.track_segments
        warnings = list(job.get("warnings") or [])
        speakers: dict = {}
        system = segments.get("system") or []
        wav = prepared.wav_paths.get("system")
        if system and wav is not None and wav.exists() and task.diarizer is not None:
            try:
                from .jobs import diarize_call, diarize_call_full, speaker_pin

                callback = task.progress.speakers_callback(task.index)
                pin = speaker_pin(prepared.meta, people_mod.is_enabled(store.root))[1]
                if people_mod.is_enabled(store.root):
                    diarization = diarize_call_full(task.diarizer, wav, progress=callback, pin=pin, full=True)
                    turns = diarization.turns
                else:
                    diarization = None
                    turns = diarize_call(task.diarizer, wav, progress=callback, pin=pin)
                segments["system"] = assign_speakers(system, turns, prefix=IN_PERSON_PREFIX)
                if diarization is not None:
                    speakers["system"] = people_mod.TrackSpeakers(diarization, speaker_labels(turns, IN_PERSON_PREFIX))
            except Exception as exc:  # noqa: BLE001 - keep the unlabelled track; notes still go ahead
                logger.warning("job %s: speaker labels failed: %s", task.job_id, exc, exc_info=True)
                warnings.append(f"speaker labels failed: {exc}")
        task.progress.finish_step(task.index)
        if warnings != (job.get("warnings") or []):
            store.update_job(task.job_id, warnings=warnings)
        self.jobs._finalize(task.job_id, prepared, segments, speakers)
