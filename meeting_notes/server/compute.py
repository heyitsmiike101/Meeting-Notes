"""GPU helpers: the registry of helper keys and presence, and the offer / lease logic for offloaded transcription.

A *helper* is a recording client on a PC with an NVIDIA GPU that transcribes jobs for this server. The contract
is ``docs/gpu-helper-design.md``; the short version:

* ``JobQueue`` (jobs.py) *offers* a job (``job["compute"]["status"] == "offered"``) and moves on.
* A helper polls, *claims* the oldest offer (``leased``, a new attempt id and a secret lease) and heartbeats.
* The helper posts the result (``done``) or releases the job; the server finalizes exactly as for a local run.
* Anything else -- no claim in time, silence for 60 s, running over budget, a release, a restart, a revoked key --
  hands the job back to the local queue (``local``, with ``fallback_reason``), exactly once. A job is never
  offered twice.

Every transition is an atomic compare-and-update on the job record (``Store.transition_job``), so two racing
requests (or a request and the sweeper) can never both win. The lease secret is stored only as SHA-256, like
agent API keys (agent/keys.py). Helper keys (``mnh_...``) authorize only the ``/v1/compute/*`` helper endpoints;
the main server token never goes to a helper runtime.

Time comes from an injectable ``clock`` (wall seconds) so tests do not have to sleep.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import secrets
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

from fastapi import Header, HTTPException, Request

from ..transcribe.diarize import SpeakerTurn
from ..transcribe.protocol import Segment
from . import auth
from . import board as board_mod
from . import store as store_mod
from .store import _atomic_write_json

logger = logging.getLogger("meeting_notes.server.compute")

KEY_PREFIX = "mnh_"

# A helper counts as online (and may be offered work) when it polled this recently.
ONLINE_SECONDS = 45.0
# An offer never waits longer than this for a busy helper; then the server does the job itself.
OFFER_MAX_WAIT_SECONDS = 15 * 60
# A leased job whose helper has been silent this long goes back to the local queue.
HEARTBEAT_TIMEOUT_SECONDS = 60.0
HEARTBEAT_INTERVAL_SECONDS = 15
# Lease budget: base + per-second-of-audio. A job that runs longer than this is taken back.
BUDGET_BASE_SECONDS = 120.0
BUDGET_PER_AUDIO_SECOND = 0.25
SWEEP_INTERVAL_SECONDS = 2.0
HELPER_STATES = ("idle", "busy", "paused", "installing", "error")

# Result limits (the contract's "Result validation").
MAX_SEGMENTS_PER_TRACK = 20_000
MAX_SEGMENT_TEXT_CHARS = 4_000
MAX_BODY_BYTES = 20 * 1024 * 1024
TRACK_SLACK_SECONDS = 2.0
MAX_SPEAKER_TURNS = 200_000


def hash_secret(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def lease_budget_seconds(total_audio_seconds: float) -> float:
    """How long a helper may hold a job: 120 s + 0.25 x the audio length."""
    return BUDGET_BASE_SECONDS + BUDGET_PER_AUDIO_SECOND * max(0.0, float(total_audio_seconds))


class ComputeError(Exception):
    """A helper request that must be refused with ``status`` (409 revoked attempt, 410 meeting gone, 422 invalid...)."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _number(value) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _clean_text(value, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _stage_fields(body: dict, step: Optional[str], job: dict) -> dict:
    """The per-stage progress a heartbeat reports (see ``store.STAGE_RESET``). Helpers before 0.8.1 send only
    ``progress`` and ``step``: their stage comes from the step text and the stage percents stay unknown."""
    stage = body.get("stage")
    if stage not in ("transcribing", "speakers"):
        stage = "speakers" if (step or "").startswith("diariz") else "transcribing"
    fields: dict = {"stage": stage}
    if "transcribe_progress" in body:
        value = _number(body.get("transcribe_progress"))
        if value is not None:
            fields["transcribe_progress"] = max(min(max(value, 0.0), 1.0), float(job.get("transcribe_progress") or 0.0))
        if body.get("speakers_progress") is None:
            fields["speakers_progress"] = None
        else:
            value = _number(body.get("speakers_progress"))
            if value is not None:
                fields["speakers_progress"] = max(
                    min(max(value, 0.0), 1.0), float(job.get("speakers_progress") or 0.0))
    return fields


class HelperRegistry:
    """Durable helper keys (``<data>/compute/helpers.json``), in-memory presence, and the lease logic."""

    def __init__(self, root, *, clock: Callable[[], float] = time.time):
        self.path = Path(root) / "compute" / "helpers.json"
        self.clock = clock
        # Returns the pack version helpers must be on (helper_pack.py), or None when there is no pack to compare.
        self.pack_version_provider: Optional[Callable[[], Optional[str]]] = None
        # Live transcription (live_remote.LiveRemote): ``live_stats_provider(helper_id)`` gives a helper's ``live``
        # presence; ``on_helper_dropped(helper_id, reason)`` closes its live socket when the key is revoked/replaced.
        self.live_stats_provider: Optional[Callable[[str], Optional[dict]]] = None
        self.on_helper_dropped: Optional[Callable[[str, str], None]] = None
        self.store: Optional[store_mod.Store] = None
        self.queue = None  # the JobQueue; set by attach()
        self._lock = threading.RLock()
        self._helpers: list = self._load()
        self._presence: dict = {}
        # Jobs this registry is watching (offered or leased), and the offered ones in offer order.
        self._tracked: dict = {}
        self._offered: dict = {}
        self._offer_seen: dict = {}  # job id -> last time a usable helper was online while it waited

    def attach(self, store: store_mod.Store, queue) -> None:
        self.store = store
        self.queue = queue

    # -- durable helpers ---------------------------------------------------

    def _load(self) -> list:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        rows = payload.get("helpers") if isinstance(payload, dict) else None
        return [r for r in rows if isinstance(r, dict) and r.get("helper_id")] if isinstance(rows, list) else []

    def _save(self) -> None:
        _atomic_write_json(self.path, {"helpers": self._helpers})

    @staticmethod
    def public_view(record: dict) -> dict:
        """A helper record safe to show anywhere (never the key hash)."""
        return {
            "helper_id": record["helper_id"],
            "name": record.get("name", ""),
            "created": record.get("created"),
            "key_prefix": record.get("key_prefix", ""),
            "revoked": bool(record.get("revoked")),
        }

    def create_helper(self, name: str, instance_id: str) -> dict:
        """Mint a key for a helper. The same ``instance_id`` again replaces (revokes) its old key."""
        name = (name or "").strip()
        instance_id = (instance_id or "").strip()
        if not name:
            raise ValueError("name is required")
        if len(name) > 80:
            raise ValueError("name must be 80 characters or fewer")
        if not instance_id or len(instance_id) > 128:
            raise ValueError("instance_id is required (128 characters or fewer)")
        plaintext = KEY_PREFIX + secrets.token_urlsafe(24)
        record = {
            "helper_id": uuid.uuid4().hex[:12],
            "name": name,
            "instance_id": instance_id,
            "created": self.clock(),
            "key_hash": hash_secret(plaintext),
            "key_prefix": plaintext[:12],
            "revoked": False,
        }
        replaced: list = []
        with self._lock:
            for old in self._helpers:
                if old.get("instance_id") == instance_id and not old.get("revoked"):
                    old["revoked"] = True
                    replaced.append(old["helper_id"])
            self._helpers.append(record)
            self._save()
        for helper_id in replaced:
            self._drop_helper(helper_id, "helper key replaced")
        return {"helper_id": record["helper_id"], "key": plaintext}

    def list_helpers(self) -> list:
        """Every helper with its presence (revoked ones included, flagged)."""
        now = self.clock()
        with self._lock:
            rows = [dict(r) for r in self._helpers]
            presence = {k: dict(v) for k, v in self._presence.items()}
        out = []
        for r in rows:
            view = self.public_view(r)
            p = presence.get(r["helper_id"])
            seen = p.get("last_seen") if p else None
            online = bool(seen is not None and now - seen <= ONLINE_SECONDS and not r.get("revoked"))
            view.update(
                {
                    "online": online,
                    "last_seen": seen,
                    "state": (p.get("state") if p else None) if online else "offline",
                    "version": p.get("version") if p else None,
                    "runtime_version": p.get("runtime_version") if p else None,
                    "pack_version": p.get("pack_version") if p else None,
                    "gpu": p.get("gpu") if p else None,
                    "paused": bool(p.get("paused")) if p else False,
                    "current_job": p.get("current_job") if p and online else None,
                    "progress": p.get("progress") if p and online else None,
                    "last_error": p.get("last_error") if p else None,
                    "live": self._live_view(r["helper_id"]),
                }
            )
            out.append(view)
        return out

    def _live_view(self, helper_id: str) -> dict:
        """The helper's live-transcription presence (``connected``, ``served``, ``avg_ms``, ``last_error``, ``ready``)."""
        provider = self.live_stats_provider
        try:
            live = provider(helper_id) if provider else None
        except Exception:  # noqa: BLE001 - presence must never fail a listing
            logger.exception("live stats failed")
            live = None
        return live or {"connected": False, "served": 0, "avg_ms": None, "last_error": None, "ready": False}

    def revoke_helper(self, helper_id: str) -> Optional[dict]:
        """Revoke a helper's key (idempotent). Its active job, if any, goes to the local queue."""
        with self._lock:
            for record in self._helpers:
                if record.get("helper_id") == helper_id:
                    if not record.get("revoked"):
                        record["revoked"] = True
                        self._save()
                    view = self.public_view(record)
                    break
            else:
                return None
        self._drop_helper(helper_id, "helper revoked")
        return view

    def _drop_helper(self, helper_id: str, reason: str) -> None:
        callback = self.on_helper_dropped
        if callback is not None:
            try:
                callback(helper_id, reason)
            except Exception:  # noqa: BLE001 - closing a live socket must not block a revoke
                logger.exception("closing live socket for %s failed", helper_id)
        with self._lock:
            self._presence.pop(helper_id, None)
            leased = [j for j in self._tracked if j not in self._offered]
        for job_id in leased if self.store is not None else []:
            job = self.store.read_job(job_id)
            compute = (job or {}).get("compute") or {}
            if compute.get("status") == "leased" and compute.get("helper_id") == helper_id:
                self._to_local(job_id, {"compute.status": "leased", "compute.helper_id": helper_id}, reason)

    def authenticate(self, plaintext: Optional[str]) -> Optional[dict]:
        """The helper record (revoked ones included) for a plaintext key, or None."""
        if not plaintext or not plaintext.startswith(KEY_PREFIX):
            return None
        digest = hash_secret(plaintext)
        found = None
        with self._lock:
            for record in self._helpers:
                # compare_digest on every record: no early exit that could leak how close a guess was.
                if hmac.compare_digest(str(record.get("key_hash", "")), digest):
                    found = record
            return dict(found) if found else None

    # -- presence ----------------------------------------------------------

    def touch(self, helper_id: str, info: Optional[dict] = None) -> None:
        """Record that a helper was just heard from, with whatever it reported (a poll's body)."""
        info = info or {}
        with self._lock:
            p = self._presence.setdefault(helper_id, {})
            p["last_seen"] = self.clock()
            for key in ("version", "runtime_version", "pack_version"):
                if key in info:
                    p[key] = _clean_text(info.get(key), 64) or None
            if isinstance(info.get("gpu"), dict):
                gpu = info["gpu"]
                vram = gpu.get("vram_mb")
                p["gpu"] = {
                    "name": _clean_text(gpu.get("name"), 120),
                    "vram_mb": int(vram) if _number(vram) is not None else None,
                    "driver": _clean_text(gpu.get("driver"), 64),
                }
            if "state" in info:
                state = info.get("state")
                p["state"] = state if state in HELPER_STATES else "error"
            if "paused" in info:
                p["paused"] = bool(info.get("paused"))
            if "last_error" in info:
                p["last_error"] = _clean_text(info.get("last_error"), 300) or None

    def online_count(self) -> int:
        now = self.clock()
        with self._lock:
            live = {r["helper_id"] for r in self._helpers if not r.get("revoked")}
            return sum(1 for hid, p in self._presence.items() if hid in live and now - p.get("last_seen", 0) <= ONLINE_SECONDS)

    def _eligible(self, helper_id: str, presence: dict) -> bool:
        """Idle, not paused, recently seen and on the server's pack: could take a job right now."""
        if self.clock() - presence.get("last_seen", 0) > ONLINE_SECONDS:
            return False
        if presence.get("state") != "idle" or presence.get("paused") or presence.get("current_job"):
            return False
        return not self._pack_outdated(presence)

    def _pack_outdated(self, presence: dict) -> bool:
        wanted = self.pack_version_provider() if self.pack_version_provider else None
        return wanted is not None and presence.get("pack_version") != wanted

    def any_online(self) -> bool:
        """A helper that is online, not paused, healthy and on the server's pack -- idle or busy. Offers wait for
        one of these (queueing behind its current job) instead of falling back after the claim timeout."""
        now = self.clock()
        with self._lock:
            live = {r["helper_id"] for r in self._helpers if not r.get("revoked")}
            for hid, p in self._presence.items():
                if hid not in live or now - p.get("last_seen", 0) > ONLINE_SECONDS:
                    continue
                if p.get("paused") or p.get("state") not in ("idle", "busy"):
                    continue
                if not self._pack_outdated(p):
                    return True
            return False

    def any_available(self) -> bool:
        """A helper that could take one more job than the offers already waiting for one."""
        with self._lock:
            live = {r["helper_id"] for r in self._helpers if not r.get("revoked")}
            idle = sum(1 for hid, p in self._presence.items() if hid in live and self._eligible(hid, p))
            return idle > len(self._offered)

    # -- offers ------------------------------------------------------------

    def track(self, job_id: str) -> None:
        """Watch a freshly offered job (JobQueue calls this right after writing its offer)."""
        with self._lock:
            self._tracked[job_id] = True
            self._offered[job_id] = True

    def _untrack(self, job_id: str) -> None:
        with self._lock:
            self._tracked.pop(job_id, None)
            self._offered.pop(job_id, None)
            self._offer_seen.pop(job_id, None)

    def _to_local(self, job_id: str, expect: dict, reason: str) -> bool:
        """Atomically take a job back for the local queue. True when this call did it (so it queues it once)."""
        record = self.store.transition_job(
            job_id,
            expect,
            **{
                "compute.status": "local",
                "compute.fallback_reason": reason,
                "compute.attempt": None,
                "compute.lease_hash": None,
                "progress": 0.0,
                **store_mod.STAGE_RESET,
            },
        )
        if record is None:
            return False
        self._untrack(job_id)
        with self._lock:
            for p in self._presence.values():
                if p.get("current_job") == job_id:
                    p["current_job"] = None
                    p["progress"] = None
        logger.info("job %s: back to the local queue (%s)", job_id, reason)
        self.queue.requeue_local(job_id)
        return True

    def _fail_deleted(self, job_id: str) -> None:
        """The meeting was trashed or deleted while a helper held the job."""
        self.store.transition_job(
            job_id,
            {},
            **{
                "state": "error",
                "error": "meeting deleted",
                "progress": 1.0,
                "compute.status": "local",
                "compute.fallback_reason": "meeting deleted",
                "compute.attempt": None,
                "compute.lease_hash": None,
            },
        )
        self._untrack(job_id)

    def _gone(self, job: dict) -> bool:
        sid = str(job.get("session_id") or "")
        return not sid or not self.store.session_exists(sid) or self.store.is_trashed(sid)

    def sweep(self) -> None:
        """Expire offers nobody claimed, and leases that went silent or ran over budget."""
        now = self.clock()
        online = self.any_online()
        with self._lock:
            job_ids = list(self._tracked)
        for job_id in job_ids:
            job = self.store.read_job(job_id)
            if job is None:
                self._untrack(job_id)
                continue
            compute = job.get("compute") or {}
            status = compute.get("status")
            if status not in ("offered", "leased"):
                self._untrack(job_id)
            elif self._gone(job):
                self._fail_deleted(job_id)
            elif status == "offered":
                # An offer waits while some helper is online (it may be busy with an earlier job). It falls back
                # after the claim timeout with no usable helper, and after OFFER_MAX_WAIT_SECONDS in any case.
                offered_at = float(compute.get("offered_at") or 0.0)
                timeout = float(compute.get("claim_timeout") or 0.0) or max(
                    0.0, float(compute.get("offer_expires") or 0.0) - offered_at)
                with self._lock:
                    if online:
                        self._offer_seen[job_id] = now
                    seen = self._offer_seen.get(job_id, offered_at)
                if now - offered_at > OFFER_MAX_WAIT_SECONDS:
                    self._to_local(job_id, {"compute.status": "offered"}, "waited too long for a helper")
                elif now - max(seen, offered_at) >= timeout:
                    self._to_local(job_id, {"compute.status": "offered"}, "no claim")
            else:
                attempt = {"compute.status": "leased", "compute.attempt": compute.get("attempt")}
                if now - float(compute.get("last_heartbeat") or 0.0) > HEARTBEAT_TIMEOUT_SECONDS:
                    # Only if no heartbeat landed since this read: the transition checks the value we judged.
                    self._to_local(
                        job_id, {**attempt, "compute.last_heartbeat": compute.get("last_heartbeat")}, "heartbeat lost")
                elif now > float(compute.get("lease_deadline") or 0.0):
                    self._to_local(job_id, attempt, "over budget")

    # -- helper requests ---------------------------------------------------

    def poll(self, helper: dict, info: dict) -> "tuple[Optional[dict], bool]":
        """A helper's poll: update presence, and claim the oldest offer if it can take work.

        Returns ``(lease or None, pack_outdated)``.
        """
        self.sweep()
        helper_id = helper["helper_id"]
        self.touch(helper_id, info)
        with self._lock:
            presence = dict(self._presence.get(helper_id) or {})
        if presence.get("state") != "idle" or presence.get("paused"):
            return None, False
        if self._pack_outdated(presence):
            return None, True
        with self._lock:
            candidates = list(self._offered)
        for job_id in candidates:
            lease = self._claim(job_id, helper)
            if lease is not None:
                return lease, False
        return None, False

    def _claim(self, job_id: str, helper: dict) -> Optional[dict]:
        job = self.store.read_job(job_id)
        if job is None:
            self._untrack(job_id)
            return None
        if self._gone(job):
            self._fail_deleted(job_id)
            return None
        compute = job.get("compute") or {}
        total = sum(float(t.get("seconds") or 0.0) for t in compute.get("tracks") or [])
        budget = lease_budget_seconds(total)
        now = self.clock()
        attempt = secrets.token_hex(8)
        lease = secrets.token_urlsafe(24)
        record = self.store.transition_job(
            job_id,
            {"compute.status": "offered", "state": "running"},
            **{
                "compute.status": "leased",
                "compute.attempt": attempt,
                "compute.lease_hash": hash_secret(lease),
                "compute.helper_id": helper["helper_id"],
                "compute.helper_name": helper.get("name", ""),
                "compute.claimed_at": now,
                "compute.last_heartbeat": now,
                "compute.lease_deadline": now + budget,
                "compute.fallback_reason": None,
                **store_mod.STAGE_RESET,
            },
        )
        if record is None:
            with self._lock:
                self._offered.pop(job_id, None)
            return None
        with self._lock:
            self._offered.pop(job_id, None)
            p = self._presence.setdefault(helper["helper_id"], {})
            p["state"] = "busy"
            p["current_job"] = job_id
            p["progress"] = 0.0
        c = record["compute"]
        logger.info("job %s: leased to helper %s (attempt %s)", job_id, helper.get("name"), attempt)
        return {
            "job_id": job_id,
            "attempt": attempt,
            "lease": lease,
            "session_id": record["session_id"],
            "board": board_mod.board_number(record["session_id"]),
            "tracks": [
                {
                    "name": t["name"],
                    "seconds": t["seconds"],
                    "url": f"/v1/compute/jobs/{job_id}/audio/{t['name']}",
                }
                for t in c.get("tracks") or []
            ],
            "whisper": c["settings"]["whisper"],
            "diarization": c["settings"]["diarization"],
            "heartbeat_seconds": HEARTBEAT_INTERVAL_SECONDS,
            "deadline_seconds": int(budget),
        }

    def _active_job(self, helper: dict, job_id: str, attempt, lease, *, allow_done: bool = False) -> dict:
        """The job record when ``attempt``/``lease`` is the helper's live attempt, else a ``ComputeError``."""
        self.sweep()
        if not store_mod.is_safe_id(job_id):
            raise ComputeError(404, "unknown job")
        job = self.store.read_job(job_id)
        if job is None:
            self._untrack(job_id)
            raise ComputeError(410, "meeting deleted")
        compute = job.get("compute") or {}
        if self._gone(job):
            if compute.get("status") in ("offered", "leased"):
                self._fail_deleted(job_id)
            raise ComputeError(410, "meeting deleted")
        statuses = ("leased", "done") if allow_done else ("leased",)
        if (
            compute.get("status") not in statuses
            or not isinstance(attempt, str)
            or not isinstance(lease, str)
            or not compute.get("attempt")
            or not hmac.compare_digest(str(compute.get("attempt")), attempt)
            or not hmac.compare_digest(str(compute.get("lease_hash") or ""), hash_secret(lease))
            or compute.get("helper_id") != helper["helper_id"]
        ):
            raise ComputeError(409, "this attempt is no longer active")
        return job

    def audio_path(self, helper: dict, job_id: str, track: str, attempt, lease) -> Path:
        job = self._active_job(helper, job_id, attempt, lease)
        self.touch(helper["helper_id"])
        offered = {t["name"] for t in (job.get("compute") or {}).get("tracks") or []}
        if track not in offered:
            raise ComputeError(404, "unknown track")
        path = self.store.track_wav_path(job["session_id"], track)
        if not path.is_file():
            raise ComputeError(410, "audio is gone")
        return path

    def heartbeat(self, helper: dict, job_id: str, body: dict) -> dict:
        job = self._active_job(helper, job_id, body.get("attempt"), body.get("lease"))
        raw = _number(body.get("progress"))
        progress = min(max(raw if raw is not None else 0.0, 0.0), 0.99)
        progress = max(progress, float(job.get("progress") or 0.0))
        step = _clean_text(body.get("step"), 80) or None
        updated = self.store.transition_job(
            job_id,
            {"compute.status": "leased", "compute.attempt": job["compute"]["attempt"]},
            **{"progress": progress, "compute.last_heartbeat": self.clock(), "compute.step": step,
               **_stage_fields(body, step, job)},
        )
        if updated is None:
            raise ComputeError(409, "this attempt is no longer active")
        with self._lock:
            p = self._presence.setdefault(helper["helper_id"], {})
            p.update(last_seen=self.clock(), state="busy", current_job=job_id, progress=progress)
        return {"ok": True}

    def release(self, helper: dict, job_id: str, body: dict) -> dict:
        job = self._active_job(helper, job_id, body.get("attempt"), body.get("lease"))
        error = _clean_text(body.get("error"), 300)
        reason = f"helper failed: {error}" if error else "helper released"
        if not self._to_local(
            job_id, {"compute.status": "leased", "compute.attempt": job["compute"]["attempt"]}, reason
        ):
            raise ComputeError(409, "this attempt is no longer active")
        self.touch(helper["helper_id"], {"last_error": error} if error else None)
        return {"ok": True}

    def complete(self, helper: dict, job_id: str, body: dict) -> dict:
        """Accept a helper's result: validate, mark the attempt done, then finalize like a local run."""
        job = self._active_job(helper, job_id, body.get("attempt"), body.get("lease"), allow_done=True)
        compute = job["compute"]
        if compute.get("status") == "done":
            return {"ok": True, "duplicate": True}  # the accepted attempt reporting again
        track_segments, turns = self._validate_result(body, compute)
        device = _clean_text(body.get("device"), 64) or None
        timings = body.get("timings") if isinstance(body.get("timings"), dict) else None
        if timings is not None and len(str(timings)) > 2000:
            timings = None
        done = self.store.transition_job(
            job_id,
            {"compute.status": "leased", "compute.attempt": compute["attempt"]},
            **{
                "compute.status": "done",
                "compute.device": device,
                "compute.timings": timings,
                "compute.completed_at": self.clock(),
            },
        )
        if done is None:
            # Lost a race: another completion of this very attempt is fine, anything else was revoked.
            again = (self.store.read_job(job_id) or {}).get("compute") or {}
            if again.get("status") == "done" and again.get("attempt") == compute["attempt"]:
                return {"ok": True, "duplicate": True}
            raise ComputeError(409, "this attempt is no longer active")
        self._untrack(job_id)
        with self._lock:
            p = self._presence.setdefault(helper["helper_id"], {})
            p.update(last_seen=self.clock(), state="idle", current_job=None, progress=None)
        try:
            self.queue.finish_remote(job_id, track_segments, turns)
        except Exception as exc:  # noqa: BLE001 - never lose the meeting: run it here instead
            logger.exception("job %s: finishing the helper's result failed; transcribing it here", job_id)
            self._to_local(
                job_id, {"compute.status": "done", "compute.attempt": compute["attempt"]},
                f"could not use the helper's result: {exc}"[:300])
        return {"ok": True}

    def _validate_result(self, body: dict, compute: dict) -> "tuple[dict, list]":
        """Contract validation. Returns ``({track: [Segment]}, [SpeakerTurn])``; raises ComputeError(422)."""
        lengths = {t["name"]: float(t.get("seconds") or 0.0) for t in compute.get("tracks") or []}
        tracks = body.get("tracks")
        if not isinstance(tracks, dict) or set(tracks) != set(lengths):
            raise ComputeError(422, f"tracks must be exactly {sorted(lengths)}")
        result: dict = {}
        for name, items in tracks.items():
            if not isinstance(items, list):
                raise ComputeError(422, f"track {name!r} must be a list of segments")
            if len(items) > MAX_SEGMENTS_PER_TRACK:
                raise ComputeError(422, f"track {name!r} has more than {MAX_SEGMENTS_PER_TRACK} segments")
            limit = lengths[name] + TRACK_SLACK_SECONDS
            segments: list = []
            previous = 0.0
            for i, item in enumerate(items):
                if not isinstance(item, dict):
                    raise ComputeError(422, f"track {name!r} segment {i} must be an object")
                start, end, text = _number(item.get("start")), _number(item.get("end")), item.get("text")
                if start is None or end is None or not 0.0 <= start <= end <= limit:
                    raise ComputeError(422, f"track {name!r} segment {i}: times must satisfy 0 <= start <= end <= {limit:g}")
                if start < previous:
                    raise ComputeError(422, f"track {name!r} segment {i}: segments must be sorted by start")
                if not isinstance(text, str) or len(text) > MAX_SEGMENT_TEXT_CHARS:
                    raise ComputeError(422, f"track {name!r} segment {i}: text must be a string of at most {MAX_SEGMENT_TEXT_CHARS} characters")
                previous = start
                segments.append(Segment(start=start, end=end, text=text, track=name))
            result[name] = segments
        raw_turns = body.get("speaker_turns")
        turns: list = []
        if raw_turns is not None:
            if not (compute.get("settings") or {}).get("diarization", {}).get("enabled"):
                raise ComputeError(422, "speaker_turns were not requested")
            if not isinstance(raw_turns, list) or len(raw_turns) > MAX_SPEAKER_TURNS:
                raise ComputeError(422, "speaker_turns must be a list")
            limit = lengths.get("system", max(lengths.values(), default=0.0)) + TRACK_SLACK_SECONDS
            for i, item in enumerate(raw_turns):
                start = _number(item.get("start")) if isinstance(item, dict) else None
                end = _number(item.get("end")) if isinstance(item, dict) else None
                speaker = item.get("speaker") if isinstance(item, dict) else None
                if start is None or end is None or not 0.0 <= start <= end <= limit:
                    raise ComputeError(422, f"speaker_turns[{i}]: times must satisfy 0 <= start <= end <= {limit:g}")
                if not isinstance(speaker, str) or not speaker or len(speaker) > 64:
                    raise ComputeError(422, f"speaker_turns[{i}]: speaker must be a short string")
                turns.append(SpeakerTurn(start=start, end=end, speaker=speaker))
        return result, turns


class ComputeSweeper:
    """Background thread that runs ``HelperRegistry.sweep`` every couple of seconds."""

    def __init__(self, registry: HelperRegistry, interval: float = SWEEP_INTERVAL_SECONDS):
        self.registry = registry
        self.interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="meeting-notes-compute-sweeper", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.registry.sweep()
            except Exception:  # noqa: BLE001 - a bad sweep must not end the thread
                logger.exception("compute sweep failed")


# -- FastAPI dependencies --------------------------------------------------


def _registry(request: Request) -> HelperRegistry:
    return request.app.state.compute_registry


def require_helper_key(request: Request, authorization: Optional[str] = Header(default=None)) -> dict:
    """Dependency: a valid, non-revoked helper key (``Authorization: Bearer mnh_...``). Returns its record.

    401 = no credential or a revoked key, 403 = a key this server does not know. The main server token is
    deliberately not accepted here.
    """
    token = auth.extract_bearer(authorization)
    if not token:
        raise HTTPException(status_code=401, detail="missing helper key")
    record = _registry(request).authenticate(token)
    if record is None:
        raise HTTPException(status_code=403, detail="invalid helper key")
    if record.get("revoked"):
        raise HTTPException(status_code=401, detail="helper key revoked")
    return record


def require_helper_key_or_token(request: Request, authorization: Optional[str] = Header(default=None)) -> Optional[dict]:
    """Dependency: a valid helper key *or* the main server token. Returns the helper record, or None for the token."""
    token = auth.extract_bearer(authorization)
    if token and token.startswith(KEY_PREFIX):
        return require_helper_key(request, authorization)
    auth.require_token(request, authorization)
    return None
