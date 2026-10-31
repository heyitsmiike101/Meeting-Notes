"""The Queue page's data: what is running, what is waiting (and for what), and what failed lately.

One pure function, ``build_queue``, assembles it from the store (``GET /v1/queue`` and the tests call it). It is polled
every few seconds, so it never scans job files: active jobs come from the SQLite index (``jobs`` by state, the newest
review per meeting by status) and only those few records are read in full.

Where each kind of work waits:

* a GPU helper: an *offered* job (``job["compute"]["status"] == "offered"``). A helper claims transcriptions before
  speakers-only jobs, each oldest offer first (``ComputeRegistry.poll``), so that is the order shown;
* this server: a queued transcription (one worker thread, first in first out) or a speakers-only job on its speakers
  worker (first in first out as well);
* the notes worker: queued reviews, oldest first.

These three lanes run side by side, so the order is exact inside a lane and only a creation-time order across lanes.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from .. import wire
from . import board as board_mod
from . import store as store_mod

logger = logging.getLogger("meeting_notes.server.queue_view")

FAILURE_WINDOW_SECONDS = 24 * 3600
MAX_FAILURES = 20
ERROR_TEXT_LIMIT = 240

LANE_HELPER, LANE_SERVER, LANE_NOTES = "helper", "server", "notes"
_LANE_ORDER = {LANE_HELPER: 0, LANE_SERVER: 1, LANE_NOTES: 2}

KIND_LABELS = {
    "transcribe": "Transcribe",
    "find_speakers": "Find speakers",
    "speaker_labels_in_person": "Speaker labels (in person)",
    "notes": "Meeting notes",
}
# The ingestion chart's wording (Settings > Ingestion) for what each kind of work is.
PIPELINE_STEPS = {
    "transcribe": "Transcribe",
    "find_speakers": "Identify speakers",
    "speaker_labels_in_person": "Identify speakers",
    "notes": "Generate notes",
}
STATUS_LABELS = {"running": "Running", "waiting": "Waiting", "failed": "Failed"}
ORDER_HINT = (
    "Work waiting for a GPU helper, work waiting for this server and queued notes are separate lines that run "
    "side by side. Inside each line the order is exact; between lines this list is only ordered by what is "
    "likely to start first."
)


def _num(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _pct(fraction) -> Optional[float]:
    number = _num(fraction)
    return None if number is None else round(min(max(number, 0.0), 1.0) * 100.0, 1)


class _Names:
    """Meeting title, number and in-person flag for a session, read once per call."""

    def __init__(self, store):
        self.store = store
        self._cache: dict = {}

    def get(self, session_id: str) -> dict:
        if session_id not in self._cache:
            meta: dict = {}
            try:
                if store_mod.is_safe_id(session_id):
                    meta = self.store.read_session_meta(session_id) or {}
            except Exception:  # noqa: BLE001 - a half-written meeting still gets a row
                logger.debug("queue: could not read the metadata of %s", session_id, exc_info=True)
            board = board_mod.board_number(session_id) if session_id else ""
            self._cache[session_id] = {
                "title": str(meta.get("name") or board or session_id),
                "board": board,
                "in_person": bool(meta.get("in_person")),
            }
        return self._cache[session_id]


def _job_kind(job: dict, names: _Names) -> str:
    if store_mod.is_speakers_job(job):
        return "speaker_labels_in_person" if names.get(job["session_id"])["in_person"] else "find_speakers"
    return "transcribe"


def _base(job: dict, names: _Names, now: float) -> dict:
    session_id = str(job.get("session_id") or "")
    info = names.get(session_id)
    kind = _job_kind(job, names)
    created = _num(job.get("created")) or now
    return {
        "type": "job",
        "id": job.get("job_id"),
        "session_id": session_id,
        "title": info["title"],
        "board": info["board"],
        "kind": kind,
        "kind_label": KIND_LABELS[kind],
        "pipeline_step": PIPELINE_STEPS[kind],
        "created": created,
        "waited_seconds": max(0, int(now - created)),
        # Notes of a new meeting start only after this speakers job ends (jobs.py, _notes_after_speakers).
        "notes_follow": bool(job.get("notes_after_speakers") and not job.get("notes_after_done")),
    }


def _running_job(job: dict, names: _Names, now: float) -> dict:
    row = _base(job, names, now)
    compute = job.get("compute") if isinstance(job.get("compute"), dict) else {}
    stages = store_mod.job_stages(job)
    stage = stages["stage"]
    if compute.get("status") == "leased":
        helper = str(compute.get("helper_name") or "").strip()
        row["where"] = f"GPU helper {helper}" if helper else "GPU helper"
        row["helper_name"] = helper
        started = _num(compute.get("claimed_at"))
    elif compute.get("status") == "done":
        row["where"] = "This server (finishing)"
        started = _num(compute.get("claimed_at"))
    else:
        row["where"] = "This server"
        started = _num(job.get("started_at"))
    if row["kind"] != "transcribe":
        percent = stages["speakers_percent"]
        step = "Labelling speakers"
    elif stage == "speakers":
        percent, step = stages["speakers_percent"], "Labelling speakers"
    elif stage == "transcribing":
        percent, step = stages["transcribe_percent"], "Transcribing"
    else:
        percent, step = None, "Starting"
    if percent is None:
        percent = _pct(job.get("progress"))
        if not percent and not stage:
            percent = None
    row.update(stage=stage or ("speakers" if row["kind"] != "transcribe" else None), step=step, percent=percent,
               started_at=started, elapsed_seconds=None if started is None else max(0, int(now - started)))
    return row


def _waiting_job(job: dict, names: _Names, now: float, settings) -> dict:
    row = _base(job, names, now)
    compute = job.get("compute") if isinstance(job.get("compute"), dict) else {}
    state_running = job.get("state") == wire.JobState.RUNNING
    if compute.get("status") == "offered":
        row.update(lane=LANE_HELPER, sort=(1 if compute.get("diarize_only") else 0,
                                           _num(compute.get("offered_at")) or row["created"]))
        row["where"] = "GPU helper"
        row["wait_forever"] = bool(compute.get("wait_forever"))
        row["wait_for_helper"] = bool(compute.get("wait_for_helper"))
        if compute.get("wait_forever") or getattr(settings, "compute_never_local", False):
            row["reason"] = "Waiting for a GPU helper (no time limit)"
            if not getattr(settings, "compute_offload_enabled", False):
                row["reason"] += ": GPU helpers are switched off in Settings"
        elif compute.get("in_person"):
            minutes = int(round((_num(compute.get("max_wait")) or 0.0) / 60.0))
            row["reason"] = f"Waiting for a GPU helper (up to {minutes} min, then this server)"
        elif compute.get("wait_for_helper"):
            row["reason"] = "Waiting for a GPU helper (this server only steps in if none is online)"
        else:
            seconds = int(_num(compute.get("claim_timeout")) or 0)
            row["reason"] = (f"Offered to GPU helpers (this server takes it if none claims it in {seconds} s)"
                             if seconds else "Offered to GPU helpers")
    elif store_mod.is_speakers_job(job) and state_running:
        # Handed to this server's speakers worker (one job at a time) and not started yet.
        row.update(lane=LANE_SERVER, sort=(0, row["created"]), where="This server",
                   reason="Waiting for this server's speaker-label worker")
    elif store_mod.is_speakers_job(job):
        row.update(lane=LANE_HELPER if getattr(settings, "compute_offload_enabled", False) else LANE_SERVER,
                   sort=(1, row["created"]), where="GPU helper",
                   reason="Waiting to be sent to a GPU helper")
    else:
        row.update(lane=LANE_SERVER, sort=(0, row["created"]), where="This server")
        if getattr(settings, "compute_never_local", False):
            row.update(lane=LANE_HELPER, where="GPU helper", reason="Waiting for a GPU helper (no time limit)")
        elif getattr(settings, "compute_offload_enabled", False):
            row["reason"] = "Queued (a GPU helper takes it if one is free, otherwise this server does)"
        else:
            row["reason"] = "Queued for this server's worker"
    return row


def _note_row(review: dict, names: _Names, now: float, settings, *, running: bool) -> dict:
    session_id = str(review.get("session_id") or "")
    info = names.get(session_id)
    created = _num(review.get("created")) or now
    row = {
        "type": "notes", "id": review.get("review_id"), "session_id": session_id, "title": info["title"],
        "board": info["board"], "kind": "notes", "kind_label": KIND_LABELS["notes"],
        "pipeline_step": PIPELINE_STEPS["notes"], "notes_follow": False, "created": created,
        "waited_seconds": max(0, int(now - created)), "lane": LANE_NOTES, "sort": (0, created),
        "template_name": review.get("template_name") or None,
    }
    if running:
        started = _num(review.get("claimed_at"))
        provider = str(getattr(settings, "ai_provider", "") or "")
        row.update(where=f"AI notes ({provider})" if provider and provider != "disabled" else "AI notes",
                   step="Writing the notes", stage="notes", percent=None, started_at=started,
                   elapsed_seconds=None if started is None else max(0, int(now - started)))
    else:
        provider = str(getattr(settings, "ai_provider", "") or "")
        row["where"] = f"AI notes ({provider})" if provider and provider != "disabled" else "AI notes"
        if provider == "disabled":
            row["reason"] = "Waiting: meeting notes are turned off in Settings"
        else:
            row["reason"] = "Waiting for the notes worker"
    return row


def _failures(store, names: _Names, now: float) -> list:
    out = []
    rows = store.jobs_in_states(
        (wire.JobState.ERROR,), updated_since=now - FAILURE_WINDOW_SECONDS, limit=MAX_FAILURES * 3)
    for row in rows:
        if row.get("error") == store_mod.JOB_SUPERSEDED_ERROR:
            continue
        job = store.read_job(row["job_id"])
        if job is None or store_mod.is_superseded_job(job):
            continue
        item = _base(job, names, now)
        item.pop("waited_seconds", None)
        error = str(job.get("error") or row.get("error") or "").strip()
        item.update(error=error[:ERROR_TEXT_LIMIT], failed_at=_num(row.get("updated")),
                    status="failed", status_label=STATUS_LABELS["failed"], since=_num(row.get("updated")))
        out.append(item)
        if len(out) >= MAX_FAILURES:
            break
    return out


def summarize_settings(settings) -> dict:
    """The queue-related settings the page shows read-only (it edits the others through ``/v1/settings``)."""
    types = list(getattr(settings, "auto_notes_types", None) or [])
    try:
        total = len(settings.all_templates())
    except Exception:  # noqa: BLE001
        total = len(types)
    return {
        "compute_offload_enabled": bool(settings.compute_offload_enabled),
        "compute_never_local": bool(settings.compute_never_local),
        "diarization_enabled": bool(settings.diarization_enabled),
        "voice_profiles_enabled": bool(settings.voice_profiles_enabled),
        "diarization_without_helper": settings.diarization_without_helper,
        "ai_provider": settings.ai_provider,
        "auto_notes_types": len(types),
        "note_types": total,
    }


def build_queue(store, *, settings, helpers: Optional[list] = None, now: Optional[float] = None) -> dict:
    """Everything the Queue page shows. ``helpers`` is the list ``/settings/gpu-helpers`` already builds."""
    now = time.time() if now is None else float(now)
    names = _Names(store)
    running: list = []
    waiting: list = []

    def skip(job: dict) -> bool:
        session_id = str(job.get("session_id") or "")
        try:
            return not session_id or store.is_trashed(session_id)
        except Exception:  # noqa: BLE001
            return False

    for row in store.jobs_in_states((wire.JobState.RUNNING, wire.JobState.QUEUED)):
        job = store.read_job(row["job_id"])
        if job is None or store_mod.is_superseded_job(job) or skip(job):
            continue
        compute = job.get("compute") if isinstance(job.get("compute"), dict) else {}
        if job.get("state") == wire.JobState.RUNNING and compute.get("status") != "offered":
            if store_mod.is_speakers_job(job) and compute.get("status") not in ("leased", "done") \
                    and _num(job.get("started_at")) is None:
                waiting.append(_waiting_job(job, names, now, settings))  # on the speakers worker, not started
            else:
                running.append(_running_job(job, names, now))
        else:
            waiting.append(_waiting_job(job, names, now, settings))

    for review in store.active_reviews():
        if skip(review):
            continue
        is_running = review.get("status") == "running"
        item = _note_row(review, names, now, settings, running=is_running)
        (running if is_running else waiting).append(item)

    for r in running:
        r.update(status="running", status_label=STATUS_LABELS["running"], since=r.get("started_at") or r["created"])
    for r in waiting:
        r.update(status="waiting", status_label=STATUS_LABELS["waiting"], since=r["created"])
    waiting.sort(key=lambda r: (_LANE_ORDER[r["lane"]], r["sort"], r["created"]))
    positions: dict = {}
    for r in waiting:
        positions[r["lane"]] = positions.get(r["lane"], 0) + 1
        r["position"] = positions[r["lane"]]
        r.pop("sort", None)
    running.sort(key=lambda r: r.get("started_at") or r["created"])

    return {
        "now": now,
        "running": running,
        "waiting": waiting,
        "order_hint": ORDER_HINT,
        "failures": _failures(store, names, now),
        "helpers": list(helpers or []),
        "settings": summarize_settings(settings),
    }
