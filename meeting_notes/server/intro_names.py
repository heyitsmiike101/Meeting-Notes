"""Name unnamed voices from the introductions in a meeting ("Hi, I'm Dana from Acme").

A third kind of AI job on the bridge, next to meeting notes and split suggestions. When the voice-profile step
leaves a voice unnamed, a small separate job reads the start of the transcript and says which unnamed speakers gave
their name or were introduced. It runs **before** the meeting's notes, so the notes are written with those names: the
notes the meeting is due (first notes, or a rebuild after a re-run) are held on the job (``notes_after``) and queued
when it ends (``release_notes``). The claim route (``notes_api.claim_review_api``) offers these jobs ahead of the notes,
only to a bridge that asks for ``kinds=...intro_names``; a bridge that does not ask releases every hold at once, and a
hold never outlasts ``HOLD_SECONDS``, so notes are late at worst, never lost.

* ``IntroNamesQueue`` -- one small JSON file per job under ``<data>/intro_names/``, keyed by meeting +
  ``transcript_job_id``. A newer job for the same meeting supersedes the old one (a re-run replaces the old result).
* ``maybe_enqueue`` -- called when a transcript with speaker labels has been recorded; best-effort, all the gates.
* ``excerpt_lines`` / ``transcript_text`` -- what the AI reads: the first ~15 minutes plus later lines that look
  like introductions, capped, under a header naming the candidate labels.
* ``validate_names`` -- the server-side checks on the answer before anything is applied (``people.apply_intro_names``).

Routes are in ``intro_names_api.py``; the voice-profile side (naming, merging, undo) is in ``people.py``.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from pathlib import Path
from typing import List, Optional

from . import names as names_mod
from . import people as people_mod
from . import settings as settings_mod
from . import store as store_mod

logger = logging.getLogger("meeting_notes.server.intro_names")

EARLY_SECONDS = 15 * 60.0   # the part of the meeting sent whole
MAX_CHARS = 25_000          # the whole excerpt, header excluded
LATE_CHARS = 6_000          # most of it that later "introduction-looking" lines may take
MAX_LINE_CHARS = 1_000
MAX_NAME_CHARS = 60
MAX_COMPANY_CHARS = 80
MAX_NAMES = 30
KEEP_FINISHED_SECONDS = 14 * 86400.0
HOLD_SECONDS = 20 * 60.0    # the longest a meeting's notes wait for its introduction job

# Words that make a later line look like an introduction (its neighbours are sent too).
_INTRO_WORDS = re.compile(
    r"\b(i'?m|i am|my name|name'?s|this is|it'?s|meet|introduc\w*|joining|welcome|call me|here from|"
    r"works? (?:at|for|with)|runs?|lead|manager|director)\b", re.IGNORECASE)
_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)


def normalise(text) -> str:
    """Lower-case words separated by single spaces: case, punctuation and whitespace do not matter for a quote."""
    return _NON_WORD.sub(" ", str(text or "").casefold()).strip()


# -- the queue ----------------------------------------------------------------------------------------------------


class IntroNamesQueue:
    """Async, optional "name voices from introductions" jobs for the bridge worker.

    Same shape as ``splitmerge.SplitAiQueue``: an older bridge never sees these jobs, and one small JSON file per job
    lives under ``<data>/intro_names/``. Differences: ``enqueue`` supersedes the meeting's earlier jobs, and
    ``complete`` stores what was applied rather than the raw answer."""

    STALE_SECONDS = 15 * 60

    def __init__(self, store):
        self.store = store
        self.dir = Path(store.root) / "intro_names"
        self._lock = threading.RLock()
        # Queues a meeting's held notes: ``notes_release(session_id, mode)``, mode "auto" (first notes) or "rebuild".
        # Set by ``JobQueue``; without it a hold is only marked released.
        self.notes_release = None

    def _path(self, job_id: str) -> Path:
        store_mod._check_id(job_id, "intro names job")
        return self.dir / f"{job_id}.json"

    def _read(self, job_id: str) -> Optional[dict]:
        try:
            value = store_mod._read_json(self._path(job_id))
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def _write(self, job: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        store_mod._atomic_write_json(self._path(job["job_id"]), job)

    def read(self, job_id: str) -> Optional[dict]:
        with self._lock:
            return self._read(job_id)

    def _all(self) -> List[dict]:
        out = []
        if self.dir.exists():
            for p in self.dir.glob("*.json"):
                if store_mod.is_safe_id(p.stem):
                    job = self._read(p.stem)
                    if job:
                        out.append(job)
        return sorted(out, key=lambda j: j.get("created", 0), reverse=True)

    def latest(self, session_id: str, transcript_job_id: Optional[str] = None) -> Optional[dict]:
        with self._lock:
            for job in self._all():
                if job.get("session_id") != session_id or job.get("status") == "superseded":
                    continue
                if transcript_job_id and job.get("transcript_job_id") != transcript_job_id:
                    continue
                return job
        return None

    def enqueue(self, session_id: str, transcript_job_id: str, notes_after: Optional[str] = None) -> dict:
        """Queue a job for this transcript. The same transcript already queued, running or done keeps its job; any
        other job of the meeting (an older transcript's) is superseded, so only the newest result counts.

        ``notes_after`` ("auto" or "rebuild"): the meeting's notes wait for this job (see ``release_notes``). A hold
        still pending on a superseded job moves to the new one ("auto", first notes, wins over "rebuild")."""
        with self._lock:
            existing = self.latest(session_id, transcript_job_id)
            if existing and existing.get("status") in ("queued", "running", "done"):
                if notes_after and existing.get("status") != "done" and not existing.get("notes_after"):
                    existing.update(notes_after=notes_after, notes_released=False)
                    self._write(existing)
                return existing
            now = time.time()
            carried = []
            for job in self._all():
                if job.get("session_id") == session_id and job.get("status") in ("queued", "running", "done", "error"):
                    if job.get("notes_after") and not job.get("notes_released"):
                        carried.append(job["notes_after"])
                        job["notes_released"] = True  # moved to the new job, which queues the notes once
                    job.update(status="superseded", updated=now)
                    self._write(job)
                elif job.get("status") in ("superseded", "done", "error") and now - float(job.get("updated") or 0) > KEEP_FINISHED_SECONDS:
                    try:
                        self._path(job["job_id"]).unlink()
                    except OSError:
                        pass
            job = {
                "job_id": uuid.uuid4().hex, "session_id": session_id,
                "transcript_job_id": transcript_job_id, "status": "queued",
                "created": now, "updated": now, "claimed_at": None, "error": None, "applied": None,
            }
            holds = carried + ([notes_after] if notes_after else [])
            if holds:
                job.update(notes_after="auto" if "auto" in holds else holds[0], notes_released=False)
            self._write(job)
            return job

    def release_notes(self, force: bool = False) -> list:
        """Queue the notes held on finished jobs (done or error), on jobs held longer than ``HOLD_SECONDS``, and, with
        ``force`` (a bridge that cannot run these jobs is asking for work), on every job. Each hold is released once.
        Returns ``[(session_id, mode)]`` released."""
        now, out = time.time(), []
        with self._lock:
            for job in self._all():
                if not job.get("notes_after") or job.get("notes_released"):
                    continue
                if force or job.get("status") in ("done", "error") or now - float(job.get("created") or 0) >= HOLD_SECONDS:
                    job.update(notes_released=True, updated=now)
                    self._write(job)
                    out.append((job.get("session_id"), job["notes_after"]))
        for session_id, mode in out:
            logger.info("session %s: introductions done or given up, queueing its notes (%s)", session_id, mode)
            if self.notes_release is not None:
                try:
                    self.notes_release(session_id, mode)
                except Exception:  # noqa: BLE001 - one meeting's notes must not stop the others
                    logger.exception("session %s: queueing the notes held for introductions failed", session_id)
        return out

    def claim_next(self) -> Optional[dict]:
        now = time.time()
        with self._lock:
            for job in self._all():
                if job.get("status") == "running" and now - float(job.get("claimed_at") or 0) >= self.STALE_SECONDS:
                    job.update(status="queued", claimed_at=None, updated=now)
                    self._write(job)
            for job in reversed(self._all()):  # oldest first
                if job.get("status") != "queued":
                    continue
                if not self.store.session_exists(job.get("session_id", "")) or self.store.is_trashed(job["session_id"]):
                    job.update(status="error", error="meeting no longer exists", updated=now)
                    self._write(job)
                    continue
                job.update(status="running", claimed_at=now, updated=now, error=None)
                self._write(job)
                return job
        return None

    def _active(self, job_id: str) -> dict:
        job = self._read(job_id)
        if job is None:
            raise KeyError("unknown intro-names job")
        if job.get("status") not in ("queued", "running"):
            raise ValueError(f"job is not active: {job.get('status')}")
        return job

    def active(self, job_id: str) -> dict:
        """The job when it can still take a result. KeyError for an unknown job, ValueError for a finished or
        superseded one (the route answers 404 / 409)."""
        with self._lock:
            return self._active(job_id)

    def complete(self, job_id: str, applied: list) -> dict:
        with self._lock:
            job = self._active(job_id)
            job.update(status="done", applied=applied, updated=time.time(), error=None)
            self._write(job)
        self.release_notes()  # the names are in: the meeting's notes may start
        return job

    def fail(self, job_id: str, error: str) -> dict:
        with self._lock:
            job = self._active(job_id)
            job.update(status="error", error=str(error)[:500], updated=time.time())
            self._write(job)
        self.release_notes()  # no names this time: the notes go ahead with the labels they have
        return job

    @staticmethod
    def public(job: Optional[dict]) -> dict:
        if not job:
            return {"status": "none", "applied": []}
        return {"status": job.get("status"), "job_id": job.get("job_id"), "session_id": job.get("session_id"),
                "error": job.get("error"), "applied": job.get("applied") or []}


# -- when to queue one ----------------------------------------------------------------------------------------------


def maybe_enqueue(queue: Optional[IntroNamesQueue], store, session_id: str, transcript_job_id: str,
                  notes_after: Optional[str] = None) -> Optional[dict]:
    """Queue the introduction job for a meeting whose transcript (``transcript_job_id``) has its speaker labels
    recorded. Only when ``intro_naming`` is on, voice profiles are effective, the AI provider is not disabled and
    the meeting has an unnamed voice that is not the recorder. Returns the job, or None when nothing was queued.
    ``notes_after``: hold the meeting's notes on the job (``IntroNamesQueue.enqueue``); the caller queues them itself
    when no job holds them.

    Best-effort, like ``JobQueue._maybe_auto_queue_review``: a failure here must never fail the transcript job, so
    it is logged and swallowed."""
    try:
        if queue is None:
            return None
        current = settings_mod.load_settings(store.root)
        if not (current.intro_naming and people_mod.enabled(current) and current.ai_provider != "disabled"):
            return None
        if not people_mod.intro_candidates(store.root, session_id, current)["unnamed"]:
            return None
        job = queue.enqueue(session_id, transcript_job_id, notes_after=notes_after)
        logger.info("session %s: queued the introduction names job", session_id)
        return job
    except Exception:  # noqa: BLE001 - see docstring
        logger.exception("session %s: queueing the introduction names job failed", session_id)
        return None


# -- what the AI reads ------------------------------------------------------------------------------------------------


def transcript_segments(store, transcript_job_id: str) -> list:
    """The speech segments of a stored transcript (dicts), in time order; no gap markers, no empty text."""
    transcript = store.read_transcript(transcript_job_id) or {}
    try:
        raw = json.loads(transcript.get("json") or "{}").get("segments") or []
    except (json.JSONDecodeError, AttributeError):
        raw = []
    segs = [s for s in raw if isinstance(s, dict) and not s.get("in_gap") and str(s.get("text") or "").strip()]
    return sorted(segs, key=lambda s: float(s.get("start") or 0.0) if isinstance(s.get("start"), (int, float)) else 0.0)


def _line(seg: dict) -> str:
    start = seg.get("start") if isinstance(seg.get("start"), (int, float)) else 0.0
    label = str(seg.get("label") or seg.get("track") or "Speaker")
    return f"[{start:.1f}s] {label}: {str(seg['text']).strip()[:MAX_LINE_CHARS]}"


def excerpt_segments(segments: list) -> list:
    """The segments the AI sees: everything in the first ``EARLY_SECONDS``, plus later lines that look like an
    introduction (with the line before and after, which usually hold the answer), capped at ``MAX_CHARS`` in all.
    Later lines get at most ``LATE_CHARS`` of that; the early part fills the rest. Time order is kept."""
    def start(seg):
        return float(seg["start"]) if isinstance(seg.get("start"), (int, float)) else 0.0

    early = [i for i, seg in enumerate(segments) if start(seg) < EARLY_SECONDS]
    late = set()
    for i, seg in enumerate(segments):
        if start(seg) >= EARLY_SECONDS and _INTRO_WORDS.search(str(seg.get("text") or "")):
            late.update(j for j in (i - 1, i, i + 1) if 0 <= j < len(segments) and start(segments[j]) >= EARLY_SECONDS)
    chosen, used = [], 0
    for i in sorted(late):
        size = len(_line(segments[i])) + 1
        if used + size > LATE_CHARS:
            break
        chosen.append(i)
        used += size
    late_used = used
    kept_early, used = [], late_used
    for i in early:
        size = len(_line(segments[i])) + 1
        if used + size > MAX_CHARS:
            break
        kept_early.append(i)
        used += size
    return [segments[i] for i in sorted(kept_early + chosen)]


def header_text(candidates: dict) -> str:
    """The short header above the excerpt: which labels may be named, which are named already, who "You" is."""
    named = "; ".join(f"{label} = {name}" for label, name in sorted(candidates["named"].items())) or "none"
    return (
        "# Who is who\n\n"
        f"Unnamed voices (the only labels you may name): {', '.join(candidates['unnamed']) or 'none'}\n"
        f"Already named (leave alone): {named}\n"
        '"You" is the person who recorded the meeting; never name "You".\n\n'
        "# Transcript excerpt (the start of the meeting plus later lines that look like introductions)\n\n")


def transcript_text(store, session_id: str, transcript_job_id: str) -> str:
    """The text the AI is given: the header, then one ``[123.4s] Label: text`` line per excerpt segment."""
    candidates = people_mod.intro_candidates(store.root, session_id)
    lines = [_line(s) for s in excerpt_segments(transcript_segments(store, transcript_job_id))]
    return header_text(candidates) + "\n".join(lines) + "\n"


# -- checking the answer --------------------------------------------------------------------------------------------


def validate_names(store, session_id: str, transcript_job_id: str, result) -> list:
    """The entries of a bridge answer that survive the server-side checks, cleaned: ``[{label, name, company,
    quote, confidence}]``. Dropped: a label that is not an unnamed candidate (named, the recorder, unknown), a
    second entry for a label, a quote that is not in the excerpt the AI saw (case, punctuation and spacing are
    ignored), a name whose first word is not in its quote, an empty or over-long name."""
    candidates = set(people_mod.intro_candidates(store.root, session_id)["unnamed"])
    haystack = " " + " ".join(normalise(s["text"][:MAX_LINE_CHARS])
                              for s in excerpt_segments(transcript_segments(store, transcript_job_id))) + " "
    out, seen = [], set()
    for item in (result.get("names") if isinstance(result, dict) else None) or []:
        if not isinstance(item, dict) or len(out) >= MAX_NAMES:
            continue
        label = names_mod.clean_name(item.get("label"))
        name = names_mod.clean_name(item.get("name"))
        quote = names_mod.clean_name(item.get("quote"))
        company = names_mod.clean_name(item.get("company"))[:MAX_COMPANY_CHARS]
        if (label not in candidates or label in seen or item.get("confidence") not in ("high", "low")
                or not name or len(name) > MAX_NAME_CHARS):
            continue
        quote_norm, first = normalise(quote), (normalise(name).split() or [""])[0]
        if not quote_norm or f" {quote_norm} " not in haystack or first not in quote_norm.split():
            continue
        seen.add(label)
        out.append({"label": label, "name": name, "company": company, "quote": quote,
                    "confidence": item["confidence"]})
    return out
