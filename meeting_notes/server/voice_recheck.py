"""Nightly voice recheck: match the speakers nobody has named yet against the people again.

A voice that was "Person 9" in a meeting stays "Person 9" there until something matches it again, even after the
same voice is named in a later meeting. Once a night, at ``voice_recheck_hour`` (server local time), this worker
runs ``people.reprofile`` on every meeting from the last ``voice_recheck_days`` days whose voiceprint still has an
unprofiled speaker (see ``people.recheck_candidates``). Reprofiling is cheap (stored embeddings, no audio) and keeps
every link confirmed by hand, so the sweep only ever fills in or improves automatic links.

The last run is kept in ``voice_recheck.json`` at the data root. A server with no record yet arms itself and
waits for the next scheduled hour rather than running at startup; "Recheck now" (``run_now``) runs it on demand,
and ``run_now(session_ids)`` reprofiles exactly those meetings (the Meetings page's bulk "Update voice profiles").
A run missed while the server was down is caught up once it is back.

Naming, merging, combining or confirming a person asks for a run too (``request_run``, trigger ``"person"``): it
never blocks the request that asked, repeated requests inside ``REQUEST_DELAY_SECONDS`` become one run, and it
shares the run lock with the nightly run and "Recheck now", so two rechecks never overlap.

Same start/stop/wake shape as ``RetentionWorker`` (retention.py).
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Optional

from . import people as people_mod
from . import settings as settings_mod
from .store import _atomic_write_json, _read_json
from .worker import BackgroundWorker

logger = logging.getLogger("meeting_notes.server.voice_recheck")

SECONDS_PER_DAY = 86400.0
STATE_FILE = "voice_recheck.json"
POLL_SECONDS = 60.0
REQUEST_DELAY_SECONDS = 5.0  # a burst of edits (several combines in a row) becomes one run
MAX_SELECTED = 2000


def _state_path(root) -> Path:
    return Path(root) / STATE_FILE


def read_state(root) -> dict:
    try:
        state = _read_json(_state_path(root))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _write_state(root, state: dict) -> None:
    _atomic_write_json(_state_path(root), state)


def last_scheduled(hour: int, now: float) -> float:
    """The most recent moment at or before ``now`` that the schedule fired: today at ``hour``:00 local time, or
    yesterday's if that is still to come."""
    for back in (0, 1, 2):
        day = time.localtime(now - back * SECONDS_PER_DAY)
        at = time.mktime((day.tm_year, day.tm_mon, day.tm_mday, hour, 0, 0, 0, 0, -1))
        if at <= now:
            return at
    return now - SECONDS_PER_DAY


def next_scheduled(hour: int, now: float) -> float:
    """The next moment after ``now`` the schedule fires."""
    for ahead in (0, 1, 2):
        day = time.localtime(now + ahead * SECONDS_PER_DAY)
        at = time.mktime((day.tm_year, day.tm_mon, day.tm_mday, hour, 0, 0, 0, 0, -1))
        if at > now:
            return at
    return now + SECONDS_PER_DAY


def is_due(hour: int, since: float, now: float) -> bool:
    """Whether a scheduled run fell between ``since`` (the last run, or when the server armed itself) and now."""
    return since < last_scheduled(hour, now)


def run_recheck(store, settings, now: Optional[float] = None, *, trigger: str = "nightly",
                session_ids: Optional[list] = None) -> dict:
    """Recheck voices and record the run. Returns ``{"ran", "trigger", "days", "meetings", "changed_meetings",
    "changed_speakers", "failed", "skipped"}``.

    By default the unprofiled speakers of every meeting from the last ``voice_recheck_days`` days. With
    ``session_ids`` exactly those meetings, whether or not they have an unprofiled speaker (the Meetings page's
    bulk action); ``days`` is then None and one with nothing stored to match (or gone) counts as ``skipped``."""
    now = time.time() if now is None else now
    days = int(settings.voice_recheck_days)
    result = {"ran": now, "trigger": trigger, "days": None if session_ids is not None else days, "meetings": 0,
              "changed_meetings": 0, "changed_speakers": 0, "failed": 0, "skipped": 0}
    if session_ids is None:
        todo = people_mod.recheck_candidates(store, now - days * SECONDS_PER_DAY)
    else:
        todo = list(dict.fromkeys(session_ids))
    for session_id in todo:
        try:
            out = people_mod.reprofile(store, session_id)
        except (people_mod.PersonNotFound, people_mod.NoVoiceprint):
            result["skipped"] += 1  # deleted or relabelled since the list was made, or no voice data
            continue
        except Exception:  # noqa: BLE001 - one bad meeting must not stop the rest
            logger.exception("voice recheck failed for %s", session_id)
            result["failed"] += 1
            continue
        result["meetings"] += 1
        if out.get("changed"):
            result["changed_meetings"] += 1
            result["changed_speakers"] += int(out["changed"])
    _write_state(store.root, {**read_state(store.root), "last_run": now, "last_result": result})
    logger.info("voice recheck (%s, %s): %d meetings checked, %d changed (%d speakers)", trigger,
                "selected meetings" if session_ids is not None else f"last {days} days", result["meetings"],
                result["changed_meetings"], result["changed_speakers"])
    return result


def status(root, settings, now: Optional[float] = None) -> dict:
    """What the Settings page shows: on/off, the schedule, the next run and the last run's result."""
    now = time.time() if now is None else now
    on = bool(settings.voice_recheck_enabled) and people_mod.enabled(settings)
    state = read_state(root)
    return {"enabled": on, "hour": settings.voice_recheck_hour, "days": settings.voice_recheck_days,
            "next_run": next_scheduled(settings.voice_recheck_hour, now) if on else None,
            "last_run": state.get("last_run"), "last_result": state.get("last_result")}


class VoiceRecheckWorker(BackgroundWorker):
    """Daemon thread that runs ``run_recheck`` once a night, and soon after a person is named, merged or confirmed.
    ``wake()`` (a settings save) makes it see a changed hour straight away."""

    thread_name = "meeting-notes-voice-recheck"

    def __init__(self, store, *, poll_seconds: float = POLL_SECONDS, request_delay: float = REQUEST_DELAY_SECONDS):
        super().__init__()
        self.store = store
        self.poll_seconds = poll_seconds
        self._delay = request_delay
        self._req_lock = threading.Lock()
        self._requested: Optional[str] = None  # trigger of a run asked for and not started yet (coalesces)
        self._run_lock = threading.Lock()  # one recheck at a time (nightly and "Recheck now")

    def run_now(self, session_ids: Optional[list] = None) -> dict:
        """Run the recheck straight away ("Recheck now", whatever the schedule says), or on exactly
        ``session_ids`` (trigger ``"selected"``)."""
        settings = settings_mod.load_settings(self.store.root)
        with self._run_lock:
            if session_ids is not None:
                return run_recheck(self.store, settings, trigger="selected", session_ids=session_ids)
            return run_recheck(self.store, settings, trigger="manual")

    def request_run(self, trigger: str = "person") -> bool:
        """Ask for a recheck soon and return at once (the caller never waits for it). Only while voice profiles are
        effective. A request made while one is already waiting joins it. True when a run is now pending."""
        try:
            if not people_mod.is_enabled(self.store.root):
                return False
        except Exception:  # noqa: BLE001 - never fail the edit that asked
            return False
        with self._req_lock:
            self._requested = self._requested or trigger
        self._wake.set()
        return True

    def _run_requested(self) -> Optional[dict]:
        """Start the run ``request_run`` asked for, after a short delay so a burst of edits is one run. A request
        made while it runs is kept for the next pass (the people changed again)."""
        with self._req_lock:
            if self._requested is None:
                return None
        if self._delay and self._stop.wait(self._delay):
            return None
        with self._req_lock:
            trigger, self._requested = self._requested, None
        if trigger is None:
            return None
        try:
            settings = settings_mod.load_settings(self.store.root)
            if not people_mod.enabled(settings):
                return None
            with self._run_lock:
                return run_recheck(self.store, settings, trigger=trigger)
        except Exception:  # noqa: BLE001 - one bad run must not kill the worker
            logger.exception("voice recheck (%s) failed", trigger)
            return None

    def run_once(self) -> None:
        self._run_requested()
        self._tick()

    def _tick(self, now: Optional[float] = None) -> Optional[dict]:
        try:
            settings = settings_mod.load_settings(self.store.root)
            if not (settings.voice_recheck_enabled and people_mod.enabled(settings)):
                return None
            now = time.time() if now is None else now
            state = read_state(self.store.root)
            since = state.get("last_run") or state.get("armed")
            if since is None:
                # First start with the recheck on: wait for the next scheduled hour instead of running now.
                _write_state(self.store.root, {**state, "armed": now})
                return None
            if not is_due(settings.voice_recheck_hour, float(since), now):
                return None
            with self._run_lock:
                return run_recheck(self.store, settings, now)
        except Exception:  # noqa: BLE001 - one bad night must not kill the worker
            logger.exception("voice recheck failed")
            return None
