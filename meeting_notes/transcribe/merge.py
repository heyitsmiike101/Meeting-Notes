"""Combine two tracks' transcripts onto one shared, readable timeline.

This is the payoff of the whole dual-track design: a mic track and a system
track are each timed against ``time.monotonic()`` by the *same* process (see
``meeting_notes.timing``), so once each track's segment times are run through
its own ``FrameClock``, both land in one comparable timeline and can be
interleaved correctly -- not just concatenated track-by-track.
"""

from __future__ import annotations

import json
from typing import Optional

from ..timing import FrameClock
from .protocol import Segment

# Track name -> speaker label, for the common two-track case (your mic vs.
# whatever system/loopback audio captured everyone else). Callers can widen
# or override this via the ``labels`` argument.
DEFAULT_LABELS: dict[str, str] = {"mic": "You", "system": "Them"}


def merge_tracks(
    track_segments: dict[str, list[Segment]],
    clocks: dict[str, FrameClock],
    labels: Optional[dict[str, str]] = None,
) -> list[dict]:
    """Merge per-track segments into one chronological, session-relative list.

    Each segment's WAV-relative ``start``/``end`` (seconds into that track's
    own file) is mapped through that track's ``FrameClock`` onto the shared
    monotonic timeline, then rebased so 0.0 is the earliest point any track
    started -- i.e. session-relative seconds, matching what a listener
    actually experienced.
    """
    if not clocks:
        return []
    labels = {**DEFAULT_LABELS, **(labels or {})}

    # Rebase onto the earliest track start, not e.g. track "mic" specifically --
    # either track could have opened first (device init order isn't fixed).
    earliest = min(clock.start_monotonic for clock in clocks.values())

    merged: list[dict] = []
    for track, segments in track_segments.items():
        clock = clocks.get(track)
        if clock is None:
            # No timing log for this track -> no way to place it on the
            # shared timeline honestly, so drop it rather than guess.
            continue
        label = labels.get(track, track)
        for seg in segments:
            start_mono = clock.monotonic_at_seconds(seg.start)
            end_mono = clock.monotonic_at_seconds(seg.end)
            # A segment counts as "in a gap" if either edge falls inside the
            # padded-silence stretch -- most often that means the whole
            # segment does, since whisper segments don't usually straddle a
            # gap boundary cleanly, but checking both edges is cheap and safe.
            in_gap = clock.in_gap(seg.start) or clock.in_gap(seg.end)
            merged.append(
                {
                    "start": start_mono - earliest,
                    "end": end_mono - earliest,
                    "track": track,
                    "label": label,
                    "text": seg.text,
                    "in_gap": bool(in_gap),
                }
            )

    # Break ties deterministically by track name so output is stable across
    # runs even when two segments land at (numerically) the same instant.
    merged.sort(key=lambda d: (d["start"], d["track"]))
    return merged


def format_timestamp(seconds: float) -> str:
    """Render session-relative seconds as ``HH:MM:SS``."""
    total = int(round(seconds))
    if total < 0:
        total = 0
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def render_markdown(merged: list[dict], session_meta: dict) -> str:
    """Render a merged transcript as readable Markdown.

    Consecutive segments from the same speaker are coalesced into one
    paragraph so the label isn't repeated every few seconds. A run of
    ``in_gap`` segments -- typically whisper hallucinating text over padded
    silence during a device dropout, since there was no real audio there --
    is collapsed into one explicit lost-audio marker instead of printing
    text nobody actually said.
    """
    lines: list[str] = ["# Meeting transcript", ""]

    date = session_meta.get("date")
    duration = session_meta.get("duration_sec")
    meta_bits = []
    if date:
        meta_bits.append(f"Date: {date}")
    if duration is not None:
        meta_bits.append(f"Duration: {format_timestamp(duration)}")
    if meta_bits:
        lines.append(" · ".join(meta_bits))
        lines.append("")

    i, n = 0, len(merged)
    while i < n:
        seg = merged[i]
        if seg["in_gap"]:
            # Collapse the whole contiguous run of gap segments into one
            # marker spanning from the first segment's start to the last's
            # end, rather than one marker per (possibly hallucinated) segment.
            j = i
            while j < n and merged[j]["in_gap"]:
                j += 1
            lost = merged[j - 1]["end"] - seg["start"]
            lines.append(f"_[audio lost for {max(round(lost), 0)} seconds]_")
            lines.append("")
            i = j
            continue

        label = seg["label"]
        j = i
        texts = []
        while j < n and not merged[j]["in_gap"] and merged[j]["label"] == label:
            texts.append(merged[j]["text"])
            j += 1
        ts = format_timestamp(seg["start"])
        body = " ".join(t.strip() for t in texts if t.strip())
        lines.append(f"**[{ts}] {label}:** {body}")
        lines.append("")
        i = j

    return "\n".join(lines).rstrip() + "\n"


def render_json(merged: list[dict], session_meta: dict) -> str:
    """Render a merged transcript as JSON (machine-readable counterpart)."""
    payload = {"session": session_meta, "segments": merged}
    return json.dumps(payload, indent=2, ensure_ascii=False)
