"""When a meeting ran and how the talking split between you and the other side.

Used by the notes view, the downloaded notes and the Notion copy. Everything here is derived from data the
server already has: the recorder's ``session.json`` (``started_wall`` and ``duration_sec``) and the merged
transcript, whose segments carry the track they came from (``mic`` = you, ``system`` = them).

Talk time is the total seconds of transcribed speech per side (segment ``end - start``), so pauses and
silence count for nobody, and two people talking over each other both get the time.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Iterable, Optional

ME_TRACKS = {"mic"}
ME_SPEAKERS = {"you", "me"}


def _float(value) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and out not in (float("inf"), float("-inf")) else None


def _started(meta: dict) -> Optional[float]:
    wall = _float(meta.get("started_wall"))
    if wall and wall > 0:
        return wall
    created = meta.get("created")
    if isinstance(created, str) and created:
        try:
            return datetime.fromisoformat(created).timestamp()
        except ValueError:
            return None
    return _float(created) if isinstance(created, (int, float)) else None


def _side(segment: dict) -> Optional[str]:
    track = str(segment.get("track") or "").strip().lower()
    if track:
        return "me" if track in ME_TRACKS else "them"
    speaker = str(segment.get("speaker") or "").strip().lower()
    if speaker in ME_SPEAKERS:
        return "me"
    if speaker == "them":
        return "them"
    return None  # an imported transcript with named speakers: we can't tell which one is you


def talk_time(segments: Iterable) -> Optional[dict]:
    """``{me_sec, them_sec, me_pct, them_pct}`` from transcript segments, or None when nobody can be placed."""
    totals = {"me": 0.0, "them": 0.0}
    placed = False
    for seg in segments or []:
        if not isinstance(seg, dict):
            continue
        side = _side(seg)
        start, end = _float(seg.get("start")), _float(seg.get("end"))
        if side is None or start is None or end is None or end <= start:
            continue
        totals[side] += end - start
        placed = True
    total = totals["me"] + totals["them"]
    if not placed or total <= 0:
        return None
    me_pct = round(100.0 * totals["me"] / total)
    return {
        "me_sec": round(totals["me"], 1),
        "them_sec": round(totals["them"], 1),
        "me_pct": me_pct,
        "them_pct": 100 - me_pct,
    }


def meeting_stats(meta: Optional[dict], segments: Optional[list]) -> dict:
    """``{started, ended, duration_sec, talk}``: epoch seconds (None when unknown) and :func:`talk_time`."""
    meta = meta if isinstance(meta, dict) else {}
    segments = segments if isinstance(segments, list) else []
    started = _started(meta)
    duration = _float(meta.get("duration_sec"))
    if duration is None or duration <= 0:
        ends = [_float(s.get("end")) for s in segments if isinstance(s, dict)]
        ends = [e for e in ends if e is not None and e > 0]
        duration = max(ends) if ends else None
    ended = started + duration if started is not None and duration is not None else None
    return {
        "started": started,
        "ended": ended,
        "duration_sec": round(duration, 1) if duration is not None else None,
        "talk": talk_time(segments),
    }


def transcript_segments(store, job_id: str) -> list:
    """The merged transcript's segments for a transcription job ([] when missing or unreadable)."""
    transcript = store.read_transcript(str(job_id or "")) if job_id else None
    if not transcript:
        return []
    try:
        value = json.loads(transcript.get("json") or "{}")
    except (json.JSONDecodeError, TypeError):
        return []
    segments = value.get("segments") if isinstance(value, dict) else None
    return segments if isinstance(segments, list) else []


def stats_for_review(store, review: dict) -> dict:
    session_id = str(review.get("session_id") or "")
    try:
        meta = store.read_session_meta(session_id) if session_id and store.session_exists(session_id) else {}
    except Exception:  # noqa: BLE001 - stats are a nicety; never fail the caller
        meta = {}
    return meeting_stats(meta, transcript_segments(store, review.get("transcript_job_id")))


def duration_text(seconds: Optional[float]) -> str:
    """``1 h 5 min`` / ``42 min`` / ``under a minute``."""
    if seconds is None:
        return ""
    minutes = int(round(seconds / 60.0))
    if minutes < 1:
        return "under a minute"
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f"{hours} h {mins} min"
    return f"{hours} h" if hours else f"{mins} min"


def clock_text(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


def detail_lines(stats: dict, tz=None) -> list:
    """Plain lines for the Notion copy and the downloaded notes: start, end and talk time."""
    out = []
    started, ended = stats.get("started"), stats.get("ended")
    if started is not None:
        s = datetime.fromtimestamp(started, tz) if tz else datetime.fromtimestamp(started)
        out.append(f"Start: {s.strftime('%a %b')} {s.day}, {clock_text(s)}")
        if ended is not None:
            e = datetime.fromtimestamp(ended, tz) if tz else datetime.fromtimestamp(ended)
            day = "" if e.date() == s.date() else f"{e.strftime('%a %b')} {e.day}, "
            dur = duration_text(stats.get("duration_sec"))
            out.append(f"End: {day}{clock_text(e)}" + (f" ({dur})" if dur else ""))
    talk = stats.get("talk")
    if talk:
        out.append(
            f"Talk time: you {talk['me_pct']}% ({duration_text(talk['me_sec'])}), "
            f"them {talk['them_pct']}% ({duration_text(talk['them_sec'])})"
        )
    return out
