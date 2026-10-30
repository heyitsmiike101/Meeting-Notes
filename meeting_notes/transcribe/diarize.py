"""Optional speaker diarization for the mixed system-audio track."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Protocol, runtime_checkable

from .protocol import Segment


@dataclass(frozen=True)
class SpeakerTurn:
    start: float
    end: float
    speaker: str


@dataclass
class Diarization:
    """One diarized track: its turns, plus (when the backend can) a voice embedding per raw speaker id."""

    turns: list = field(default_factory=list)
    embeddings: dict = field(default_factory=dict)  # raw speaker id -> L2-normalised list[float]


@runtime_checkable
class Diarizer(Protocol):
    def diarize(self, wav_path: Path) -> list[SpeakerTurn]: ...


def speaker_labels(turns: list[SpeakerTurn], prefix: str = "Them") -> dict[str, str]:
    """Raw speaker id -> ``"<prefix> N"``, numbered by first appearance in time."""
    ordered_ids: list[str] = []
    for turn in sorted(turns, key=lambda t: (t.start, t.end)):
        if turn.speaker not in ordered_ids:
            ordered_ids.append(turn.speaker)
    return {speaker: f"{prefix} {i + 1}" for i, speaker in enumerate(ordered_ids)}


def assign_speakers(
    segments: list[Segment],
    turns: list[SpeakerTurn],
    *,
    prefix: str = "Them",
) -> list[Segment]:
    """Attach stable human-readable labels using maximum time overlap.

    Whisper does not expose dependable word timestamps in this project, so a
    transcript segment cannot safely be split at a diarization boundary. The
    speaker occupying the largest part of the segment wins. Segments with no
    overlap retain the ordinary track label.
    """
    return assign_labels(segments, turns, speaker_labels(turns, prefix))


def assign_labels(segments: list[Segment], turns: list[SpeakerTurn], labels: dict[str, str]) -> list[Segment]:
    """Like ``assign_speakers`` with an explicit raw id -> label map. Turns whose id is not in the map are
    ignored, so segments only they cover keep the ordinary track label."""
    ordered_ids: list[str] = []
    for turn in sorted(turns, key=lambda t: (t.start, t.end)):
        if turn.speaker in labels and turn.speaker not in ordered_ids:
            ordered_ids.append(turn.speaker)

    result: list[Segment] = []
    for segment in segments:
        overlaps: dict[str, float] = {}
        for turn in turns:
            if turn.speaker not in labels:
                continue
            overlap = max(0.0, min(segment.end, turn.end) - max(segment.start, turn.start))
            if overlap:
                overlaps[turn.speaker] = overlaps.get(turn.speaker, 0.0) + overlap
        if overlaps:
            speaker = max(overlaps, key=lambda key: (overlaps[key], -ordered_ids.index(key)))
            result.append(replace(segment, speaker=labels[speaker]))
        else:
            result.append(segment)
    return result
