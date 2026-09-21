"""Background deletion of session AUDIO -- never transcripts -- once the
operator's configured retention policy says it's no longer needed.

Deliberately separate from ``JobQueue``: retention is a policy sweep over
*existing* sessions, and it has to run periodically even when nothing new is
happening -- a session recorded last week has to eventually age out on its
own, with no new upload or job to trigger the check. The one exception is
0-day retention ("delete audio as soon as the transcript is done"), which
``JobQueue`` triggers immediately for just the session that finished (see
``JobQueue._process``'s call into ``apply_retention``) rather than waiting for
this worker's next sweep -- 0 days means "now," not "within the next hour."

Only ``should_delete_audio`` needs to know the actual rules; everything else
here is just "find sessions, ask the rule, act on yes." That split keeps the
rule table trivial to unit test with plain dicts, with no Store or filesystem
involved at all.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import List, Optional

from .. import wire
from . import settings as settings_mod
from . import store as store_mod

logger = logging.getLogger("meeting_notes.server.retention")

SECONDS_PER_DAY = 86400.0

# How soon after the worker thread starts to run the first sweep. Short, but
# not zero -- gives the rest of app startup (job queue, etc.) a moment to
# settle first. A session that aged out while the server was down shouldn't
# have to wait up to a full interval to be noticed, which is why this is
# "startup delay", not "first interval".
DEFAULT_STARTUP_DELAY_SECONDS = 1.0


def _job_is_active(state: Optional[str]) -> bool:
    return state in (wire.JobState.QUEUED, wire.JobState.RUNNING)


def should_delete_audio(session: dict, settings: settings_mod.Settings, now: float) -> bool:
    """Pure decision function: given one session's index row (``has_audio``,
    ``created``, ``latest_state``) and the current settings, should its audio
    be deleted right now?

    Rules, in order:
    * No audio at all -- nothing to do.
    * No job on record yet, or its latest job is queued/running -- never
      delete audio a job might still need (a retranscribe just enqueued must
      not race a sweep that deletes the very audio it's about to read).
    * ``delete_audio_only_after_success`` -- skip unless the latest job
      actually succeeded (DONE). This exists specifically so a failed job can
      be retried against the original audio instead of losing it.
    * ``audio_retention_days == -1`` -- keep forever.
    * ``== 0`` -- delete now (everything above already confirmed the job
      finished, and succeeded if that flag requires it).
    * ``== N`` -- delete once the session is at least N days old.
    """
    if not session.get("has_audio"):
        return False

    state = session.get("latest_state")
    if state is None or _job_is_active(state):
        return False

    if settings.delete_audio_only_after_success and state != wire.JobState.DONE:
        return False

    days = settings.audio_retention_days
    if days < 0:
        return False
    if days == 0:
        return True

    created = session.get("created") or 0.0
    age_days = (now - created) / SECONDS_PER_DAY
    return age_days >= days


def _transcript_is_empty(store: store_mod.Store, session: dict) -> bool:
    job_id = session.get("latest_job_id")
    if not job_id:
        return True
    transcript = store.read_transcript(job_id)
    if not transcript:
        return True
    try:
        return not json.loads(transcript.get("json") or "{}").get("segments")
    except (ValueError, TypeError, AttributeError):
        return True


def _sweep(store: store_mod.Store, settings: settings_mod.Settings, now: float, sessions: List[dict]) -> List[str]:
    deleted: List[str] = []
    for session in sessions:
        if not should_delete_audio(session, settings, now):
            continue
        session_id = session["session_id"]
        if settings.delete_audio_only_after_success and _transcript_is_empty(store, session):
            # "Succeeded" has to mean something came out. A job that ran on
            # a missing WAV (seen on a real run) or on audio Whisper decided
            # was all silence is DONE with zero segments; deleting the only
            # copy of the audio on the strength of that would be the one
            # irreversible mistake this whole flag exists to prevent.
            logger.info("retention: keeping audio for %s -- its transcript is empty", session_id)
            continue
        freed = store.delete_session_audio(session_id)
        deleted.append(session_id)
        logger.info("retention: deleted audio for session %s (%d bytes freed)", session_id, freed)
    return deleted


def apply_retention(
    store: store_mod.Store,
    settings: settings_mod.Settings,
    now: Optional[float] = None,
    *,
    session_ids: Optional[List[str]] = None,
) -> List[str]:
    """Apply the retention policy and delete whatever audio has aged out.

    With ``session_ids`` given, only those sessions are considered (used by
    ``JobQueue`` to act on the one session that just finished, without
    sweeping the whole index). Otherwise walks every session via
    ``Store.list_sessions``, paginated, so this never holds the whole
    (potentially very large) session list in memory at once.

    Returns the list of session ids whose audio was deleted, so callers
    (tests, the worker's log line) can report on what happened without
    re-querying.
    """
    now = time.time() if now is None else now

    if session_ids is not None:
        rows = [r for r in (store.session_index_row(sid) for sid in session_ids) if r is not None]
        return _sweep(store, settings, now, rows)

    deleted: List[str] = []
    page = 1
    per_page = 200
    while True:
        result = store.list_sessions(page=page, per_page=per_page)
        items = result["items"]
        if not items:
            break
        deleted.extend(_sweep(store, settings, now, items))
        if page * per_page >= result["total"]:
            break
        page += 1
    return deleted


class RetentionWorker:
    """Daemon thread that runs ``apply_retention`` on a timer.

    Same start/stop shape as ``JobQueue`` (jobs.py), so ``create_app`` wires
    both identically and shutdown is symmetric.
    """

    def __init__(
        self,
        store: store_mod.Store,
        *,
        interval_minutes: Optional[float] = None,
        startup_delay: float = DEFAULT_STARTUP_DELAY_SECONDS,
    ):
        self.store = store
        # Tests pass an explicit interval to avoid waiting on a real
        # settings-driven hour; production leaves this None and re-reads
        # settings.retention_check_interval_minutes every cycle, so a changed
        # interval takes effect on the next sweep without a restart.
        self._interval_override = interval_minutes
        self._startup_delay = startup_delay
        self._stop = threading.Event()
        # Set by wake(): the thread is normally asleep for a whole interval,
        # computed from the settings as they were when it dozed off. Seen on
        # a real run: retention changed from "never" to "immediately" with a
        # one-minute interval, and nothing happened for the rest of the OLD
        # hour-long nap. A settings save pokes this so the new policy is
        # applied right away.
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="meeting-notes-retention", daemon=True)
        self._thread.start()

    def wake(self) -> None:
        """Run a sweep now (well, as soon as the thread notices) instead of
        waiting out the current interval."""
        self._wake.set()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        if self._stop.wait(timeout=self._startup_delay):
            return
        while not self._stop.is_set():
            self._wake.clear()
            self._sweep_once()
            interval_seconds = max(self._current_interval_minutes() * 60.0, 1.0)
            deadline = time.monotonic() + interval_seconds
            while not self._stop.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self._wake.wait(timeout=min(remaining, 1.0)):
                    break

    def _sweep_once(self) -> None:
        try:
            settings = settings_mod.load_settings(self.store.root)
            apply_retention(self.store, settings)
        except Exception:  # noqa: BLE001 -- one bad sweep must not kill the worker
            logger.exception("retention sweep failed")

    def _current_interval_minutes(self) -> float:
        if self._interval_override is not None:
            return self._interval_override
        try:
            return settings_mod.load_settings(self.store.root).retention_check_interval_minutes
        except Exception:
            return float(settings_mod.DEFAULT_RETENTION_CHECK_INTERVAL_MINUTES)
