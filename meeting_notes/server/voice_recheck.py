"""Nightly voice recheck: match the speakers nobody has named yet against the people again.

A voice that was "Person 9" in a meeting stays "Person 9" there until something matches it again, even after the
same voice is named in a later meeting. Once a night, at ``voice_recheck_hour`` (server local time), this worker
runs ``people.reprofile`` on every meeting from the last ``voice_recheck_days`` days whose voiceprint still has an
unprofiled speaker (see ``people.recheck_candidates``). Reprofiling is cheap (stored embeddings, no audio) and keeps
every link confirmed by hand, so the sweep only ever fills in or improves automatic links.

The last run is kept in ``voice_recheck.json`` at the data root. A server with no record yet arms itself and
waits for the next scheduled hour rather than running at startup; "Recheck now" (``run_now``) runs it on demand.
A run missed while the server was down is caught up once it is back.

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

logger = logging.getLogger("meeting_notes.server.voice_recheck")

SECONDS_PER_DAY = 86400.0
STATE_FILE = "voice_recheck.json"
POLL_SECONDS = 60.0


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


def run_recheck(store, settings, now: Optional[float] = None, *, trigger: str = "nightly") -> dict:
    """Recheck the unprofiled speakers of every meeting from the last ``voice_recheck_days`` days and record the
    run. Returns ``{"ran", "trigger", "days", "meetings", "changed_meetings", "changed_speakers", "failed"}``."""
    now = time.time() if now is None else now
    days = int(settings.voice_recheck_days)
    result = {"ran": now, "trigger": trigger, "days": days, "meetings": 0, "changed_meetings": 0,
              "changed_speakers": 0, "failed": 0}
    for session_id in people_mod.recheck_candidates(store, now - days * SECONDS_PER_DAY):
        try:
            out = people_mod.reprofile(store, session_id)
        except (people_mod.PersonNotFound, people_mod.NoVoiceprint):
            continue  # deleted or relabelled since the list was made
        except Exception:  # noqa: BLE001 - one bad meeting must not stop the rest
            logger.exception("voice recheck failed for %s", session_id)
            result["failed"] += 1
            continue
        result["meetings"] += 1
        if out.get("changed"):
            result["changed_meetings"] += 1
            result["changed_speakers"] += int(out["changed"])
    _write_state(store.root, {**read_state(store.root), "last_run": now, "last_result": result})
    logger.info("voice recheck (%s, last %d days): %d meetings checked, %d changed (%d speakers)", trigger, days,
                result["meetings"], result["changed_meetings"], result["changed_speakers"])
    return result


def status(root, settings, now: Optional[float] = None) -> dict:
    """What the Settings page shows: on/off, the schedule, the next run and the last run's result."""
    now = time.time() if now is None else now
    on = bool(settings.voice_recheck_enabled) and people_mod.enabled(settings)
    state = read_state(root)
    return {"enabled": on, "hour": settings.voice_recheck_hour, "days": settings.voice_recheck_days,
            "next_run": next_scheduled(settings.voice_recheck_hour, now) if on else None,
            "last_run": state.get("last_run"), "last_result": state.get("last_result")}


class VoiceRecheckWorker:
    """Daemon thread that runs ``run_recheck`` once a night."""

    def __init__(self, store, *, poll_seconds: float = POLL_SECONDS):
        self.store = store
        self._poll = poll_seconds
        self._stop = threading.Event()
        self._wake = threading.Event()  # set by a settings save so a changed hour is seen straight away
        self._run_lock = threading.Lock()  # one recheck at a time (nightly and "Recheck now")
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="meeting-notes-voice-recheck", daemon=True)
        self._thread.start()

    def wake(self) -> None:
        self._wake.set()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def run_now(self) -> dict:
        """Run the recheck straight away (the Settings page's "Recheck now"), whatever the schedule says."""
        settings = settings_mod.load_settings(self.store.root)
        with self._run_lock:
            return run_recheck(self.store, settings, trigger="manual")

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            self._tick()
            self._wake.wait(timeout=self._poll)

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
