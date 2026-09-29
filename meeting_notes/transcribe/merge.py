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

    A track can lack a usable clock -- no timing log was uploaded at all
    (``clocks`` doesn't mention it), or the log exists but never logged a
    single point (e.g. the client crashed before its first progress write).
    Silently dropping that track's segments would mean the user loses the
    whole transcript for a track whose audio and text we actually have --
    a silent, total data-loss bug. Instead we fall back to that track's own
    WAV-relative seconds, AS IF the track had started exactly at the earliest
    point any other track's clock logged (or at 0.0 if nothing did) -- every
    segment marked ``"approximate": True`` so callers can flag it rather than
    presenting it as precisely aligned. That's a real guess about cross-track
    alignment, but it preserves order within the track and, crucially, the
    text itself.
    """
    labels = {**DEFAULT_LABELS, **(labels or {})}

    # Rebase onto the earliest track start, not e.g. track "mic" specifically --
    # either track could have opened first (device init order isn't fixed).
    # Only clocks that actually logged something. An empty timing log reports
    # start_monotonic 0.0, and letting that sentinel win the min() would rebase
    # every timestamp onto the monotonic epoch, i.e. hours of garbage.
    started = [c.start_monotonic for c in clocks.values() if len(c.frames)]
    # If nothing logged anything at all there's no shared timeline to rebase
    # onto -- every segment (if there's a clock-driven one at all) falls back
    # to WAV-relative time below, which doesn't actually use `earliest`, but a
    # real number has to go here regardless of that.
    earliest = min(started) if started else 0.0

    merged: list[dict] = []
    for track, segments in track_segments.items():
        if not segments:
            continue
        clock = clocks.get(track)
        has_clock = clock is not None and len(clock.frames)
        label = labels.get(track, track)
        for seg in segments:
            segment_label = seg.speaker or label
            if has_clock:
                start_mono = clock.monotonic_at_seconds(seg.start)
                end_mono = clock.monotonic_at_seconds(seg.end)
                # A segment counts as "in a gap" if either edge falls inside
                # the padded-silence stretch -- most often that means the
                # whole segment does, since whisper segments don't usually
                # straddle a gap boundary cleanly, but checking both edges is
                # cheap and safe.
                in_gap = clock.in_gap(seg.start) or clock.in_gap(seg.end)
                merged.append(
                    {
                        "start": start_mono - earliest,
                        "end": end_mono - earliest,
                        "track": track,
                        "label": segment_label,
                        "text": seg.text,
                        "in_gap": bool(in_gap),
                        "approximate": False,
                    }
                )
            else:
                # No timing log for this track -- see the docstring. Use its
                # own WAV-relative seconds unchanged: that is exactly what
                # they'd be if this track had opened at `earliest` (rebasing
                # onto `earliest` is a no-op for a track that starts there).
                # There's no gap information without a clock, so "in_gap" is
                # never true here -- we have no basis to claim it is.
                merged.append(
                    {
                        "start": seg.start,
                        "end": seg.end,
                        "track": track,
                        "label": segment_label,
                        "text": seg.text,
                        "in_gap": False,
                        "approximate": True,
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

    date = session_meta.get("created") or session_meta.get("date")
    duration = session_meta.get("duration_sec")
    meta_bits = []
    if date:
        meta_bits.append(f"Date: {date}")
    if duration is not None:
        meta_bits.append(f"Duration: {format_timestamp(duration)}")
    if meta_bits:
        lines.append(" · ".join(meta_bits))
        lines.append("")

    # Flag any track whose segments came from the WAV-relative fallback in
    # merge_tracks (no timing log) so the reader knows those timestamps are a
    # guess, not a fault of the transcript itself. Sorted for stable output.
    approximate_tracks = sorted({seg["track"] for seg in merged if seg.get("approximate")})
    if approximate_tracks:
        for track in approximate_tracks:
            lines.append(f"_Timestamps for the {track} track are approximate (no timing log)._")
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
