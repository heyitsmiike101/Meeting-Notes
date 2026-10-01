"""Turn a transcript someone already has (Teams, Zoom, Meet, a text file, pasted text) into segments.

The result uses the same segment shape the transcription job produces
(``start``, ``end``, ``track``, ``label``, ``text``, ``in_gap``, ``approximate``), so the web UI,
notes, search, Notion export, split and combine treat an uploaded transcript like any other meeting.

Recognised layouts, tried in this order:

* **WebVTT / SRT** cues (``00:00:01.000 --> 00:00:04.000``), with ``<v Speaker>text`` voice tags or a
  leading ``Speaker:``.
* **Timestamped lines**: ``[00:12:34] Speaker: text``, ``00:12:34 Speaker: text``, ``(12:34) text``, and
  the Teams copy-paste shape (a ``Speaker  0:12`` line followed by what they said).
* **Plain text**: ``Speaker: text`` lines or free paragraphs. There are no real times, so the segments get
  an estimate (about 150 words a minute) and are marked ``approximate``.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

MAX_TRANSCRIPT_BYTES = 2 * 1024 * 1024
WORDS_PER_SECOND = 2.5  # ~150 wpm: used only when the text carries no times
MIN_SEGMENT_SEC = 1.0
LAST_SEGMENT_MIN_SEC = 2.0
MAX_CHUNK_CHARS = 1500  # a long paragraph is split so one "segment" is not a wall of text
TRACK = "system"  # uploaded text has no microphone track
DEFAULT_LABEL = "Transcript"


class TranscriptError(ValueError):
    """The text cannot be turned into a transcript (the message is shown to the user)."""


@dataclass
class ParsedTranscript:
    segments: List[dict] = field(default_factory=list)
    duration: float = 0.0
    format: str = "plain"  # vtt | srt | timestamped | plain
    timed: bool = False  # True when the times come from the text itself
    speakers: List[str] = field(default_factory=list)

    @property
    def approximate(self) -> bool:
        return not self.timed


_TS = r"(?:\d{1,2}:)?\d{1,2}:\d{2}(?:[.,]\d{1,3})?"
_CUE_RE = re.compile(rf"^\s*({_TS})\s*-->\s*({_TS})")
_LINE_TS_RE = re.compile(rf"^\s*[\[(]?\s*({_TS})\s*[\])]?\s*[-\u2013\u2014:]?\s*(.*)$")
# Teams copy/paste: "Jane Doe   0:12" (or "Jane Doe  1:02:33") alone on a line, text underneath.
_TEAMS_HEAD_RE = re.compile(rf"^\s*([^\d\[\]()<>:][^\[\]()<>:]{{0,60}}?)\s{{2,}}[\[(]?({_TS})[\])]?\s*$")
_TEAMS_HEAD_ALT_RE = re.compile(rf"^\s*([^\d\[\]()<>:][^\[\]()<>:]{{0,60}}?)\s*[\[(]({_TS})[\])]\s*$")
_VOICE_RE = re.compile(r"<v(?:\.[^\s>]+)*\s+([^>]+)>", re.IGNORECASE)
_TAG_RE = re.compile(r"</?[^>\n]{1,80}>")
# "Speaker: text": a short name-like label, not a time and not a sentence.
_SPEAKER_RE = re.compile(r"^\s*([^\s:\d\[\](){}<>][^:\[\](){}<>]{0,40}?)\s*:\s+(\S.*)$")
_SPEAKER_WORDS_MAX = 5
_NOT_SPEAKERS = {"note", "notes", "http", "https", "agenda", "action", "todo", "ps", "re", "fwd", "date", "time", "subject"}


def _seconds(value: str) -> float:
    parts = value.replace(",", ".").split(":")
    total = 0.0
    for part in parts:
        total = total * 60.0 + float(part)
    return total


def _clean_label(label: str) -> str:
    label = re.sub(r"\s+", " ", label).strip(" \t-\u2013\u2014*_")
    return label[:80]


def _split_speaker(text: str) -> Tuple[str, str]:
    """``"Jane: hi"`` -> (``"Jane"``, ``"hi"``); anything else -> (``""``, text)."""
    m = _SPEAKER_RE.match(text)
    if not m:
        return "", text.strip()
    label = _clean_label(m.group(1))
    words = label.split()
    if not label or len(words) > _SPEAKER_WORDS_MAX or label.lower() in _NOT_SPEAKERS or label.endswith((".", "?", "!")):
        return "", text.strip()
    return label, m.group(2).strip()


def _normalize(text: str) -> str:
    text = text.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    return text.strip()


def _words(text: str) -> int:
    return max(1, len(text.split()))


def _chunks(text: str) -> List[str]:
    """Split overlong text at sentence boundaries (then at a word) so segments stay readable."""
    text = text.strip()
    if len(text) <= MAX_CHUNK_CHARS:
        return [text] if text else []
    out: List[str] = []
    current = ""
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        while len(sentence) > MAX_CHUNK_CHARS:
            cut = sentence.rfind(" ", 0, MAX_CHUNK_CHARS)
            cut = cut if cut > 0 else MAX_CHUNK_CHARS
            if current:
                out.append(current)
                current = ""
            out.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if current and len(current) + 1 + len(sentence) > MAX_CHUNK_CHARS:
            out.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        out.append(current)
    return [c for c in out if c]


def _segment(start: float, end: float, label: str, text: str, approximate: bool) -> dict:
    return {
        "start": round(start, 3),
        "end": round(end, 3),
        "track": TRACK,
        "label": label or DEFAULT_LABEL,
        "text": text,
        "in_gap": False,
        "approximate": approximate,
    }


def _finish_timed(entries: List[Tuple[float, Optional[float], str, str]], fmt: str) -> ParsedTranscript:
    """``entries``: (start, end-or-None, label, text) in file order -> ordered, non-overlapping segments."""
    entries = [e for e in entries if e[3].strip()]
    if not entries:
        raise TranscriptError("no transcript text was found")
    # Times that go backwards (a pasted file with two clocks) fall back to the previous start.
    ordered: List[Tuple[float, Optional[float], str, str]] = []
    last = 0.0
    for start, end, label, text in entries:
        start = max(start, last)
        ordered.append((start, end, label, text))
        last = start
    segments: List[dict] = []
    for i, (start, end, label, text) in enumerate(ordered):
        following = ordered[i + 1][0] if i + 1 < len(ordered) else None
        if end is None or end <= start:
            estimate = max(MIN_SEGMENT_SEC, _words(text) / WORDS_PER_SECOND)
            end = start + (max(estimate, LAST_SEGMENT_MIN_SEC) if following is None else estimate)
        if following is not None:
            end = min(end, max(following, start))  # a segment never runs into the next one
        segments.append(_segment(start, end, label, text, False))
    parsed = ParsedTranscript(segments=segments, format=fmt, timed=True)
    parsed.duration = max(s["end"] for s in segments)
    parsed.speakers = _speakers(segments)
    return parsed


def _speakers(segments: List[dict]) -> List[str]:
    seen: List[str] = []
    for seg in segments:
        label = seg["label"]
        if label != DEFAULT_LABEL and label not in seen:
            seen.append(label)
    return seen


# -- WebVTT / SRT ----------------------------------------------------------------------------------------


def _parse_cues(text: str) -> Optional[ParsedTranscript]:
    lines = text.split("\n")
    if not any(_CUE_RE.match(line) for line in lines):
        return None
    fmt = "vtt" if lines[0].lstrip().upper().startswith("WEBVTT") else "srt"
    entries: List[Tuple[float, Optional[float], str, str]] = []
    i = 0
    while i < len(lines):
        m = _CUE_RE.match(lines[i])
        if not m:
            i += 1
            continue
        start, end = _seconds(m.group(1)), _seconds(m.group(2))
        i += 1
        body: List[str] = []
        while i < len(lines) and lines[i].strip():
            body.append(lines[i])
            i += 1
        label = ""
        cleaned: List[str] = []
        for line in body:
            voice = _VOICE_RE.search(line)
            if voice and not label:
                label = _clean_label(html.unescape(voice.group(1)))
            cleaned.append(html.unescape(_TAG_RE.sub("", line)).strip())
        joined = " ".join(c for c in cleaned if c)
        if not label:
            label, joined = _split_speaker(joined)
        entries.append((start, end, label, joined))
    # Merge cues that are the same speaker continuing (rolling captions repeat speakers every line).
    merged: List[Tuple[float, Optional[float], str, str]] = []
    for start, end, label, joined in entries:
        if merged and merged[-1][2] == label and merged[-1][1] is not None and start - merged[-1][1] <= 2.0 and len(merged[-1][3]) < MAX_CHUNK_CHARS:
            ps, _pe, pl, pt = merged[-1]
            merged[-1] = (ps, end, pl, f"{pt} {joined}".strip())
        else:
            merged.append((start, end, label, joined))
    parsed = _finish_timed(merged, fmt)
    return parsed


# -- timestamped lines ---------------------------------------------------------------------------------


def _parse_timestamped(text: str) -> Optional[ParsedTranscript]:
    lines = [ln for ln in text.split("\n")]
    nonblank = [ln for ln in lines if ln.strip()]
    if not nonblank:
        return None
    teams = sum(1 for ln in nonblank if _TEAMS_HEAD_RE.match(ln) or _TEAMS_HEAD_ALT_RE.match(ln))
    leading = sum(1 for ln in nonblank if _LINE_TS_RE.match(ln) and _LINE_TS_RE.match(ln).group(2).strip())
    if teams >= 2 and teams >= leading:
        return _parse_teams(lines)
    # A timestamp at the start of at least a third of the lines (and two lines) is a timestamped transcript.
    if leading >= 2 and leading * 3 >= len(nonblank):
        return _parse_leading_ts(lines)
    return None


def _parse_leading_ts(lines: List[str]) -> ParsedTranscript:
    entries: List[Tuple[float, Optional[float], str, str]] = []
    pending: Optional[List] = None  # [start, label, [text parts]]

    def flush() -> None:
        nonlocal pending
        if pending is not None:
            entries.append((pending[0], None, pending[1], " ".join(pending[2]).strip()))
            pending = None

    for line in lines:
        if not line.strip():
            continue
        m = _LINE_TS_RE.match(line)
        if m and m.group(2).strip():
            flush()
            label, body = _split_speaker(m.group(2))
            pending = [_seconds(m.group(1)), label, [body]]
        elif pending is not None:
            pending[2].append(line.strip())  # a wrapped continuation line
        else:
            label, body = _split_speaker(line.strip())
            pending = [0.0, label, [body]]
    flush()
    return _finish_timed(entries, "timestamped")


def _parse_teams(lines: List[str]) -> ParsedTranscript:
    entries: List[Tuple[float, Optional[float], str, str]] = []
    label, start, parts = "", 0.0, []

    def flush() -> None:
        nonlocal parts
        if parts:
            entries.append((start, None, label, " ".join(parts).strip()))
        parts = []

    for line in lines:
        if not line.strip():
            continue
        m = _TEAMS_HEAD_RE.match(line) or _TEAMS_HEAD_ALT_RE.match(line)
        if m:
            flush()
            label, start = _clean_label(m.group(1)), _seconds(m.group(2))
        else:
            parts.append(line.strip())
    flush()
    return _finish_timed(entries, "timestamped")


# -- plain text ------------------------------------------------------------------------------------------


def _parse_plain(text: str) -> ParsedTranscript:
    """No times: paragraphs (or ``Speaker:`` lines) with an estimated, approximate timeline."""
    blocks = [b for b in re.split(r"\n\s*\n", text) if b.strip()]
    pieces: List[Tuple[str, str]] = []
    lines_with_speaker = sum(1 for ln in text.split("\n") if ln.strip() and _split_speaker(ln)[0])
    nonblank_lines = [ln for ln in text.split("\n") if ln.strip()]
    if lines_with_speaker and lines_with_speaker * 2 >= len(nonblank_lines):
        # One utterance per "Speaker:" line; lines without a label continue the previous speaker.
        label, buf = "", []

        def flush() -> None:
            if buf:
                for chunk in _chunks(" ".join(buf)):
                    pieces.append((label, chunk))

        for line in nonblank_lines:
            who, body = _split_speaker(line)
            if who:
                flush()
                label, buf = who, [body]
            else:
                buf.append(line.strip())
        flush()
    else:
        if len(blocks) == 1 and len(nonblank_lines) > 2:
            units = nonblank_lines  # no blank lines at all: one utterance per line
        else:
            units = [" ".join(ln.strip() for ln in b.splitlines() if ln.strip()) for b in blocks]
        for unit in units:
            for chunk in _chunks(unit):
                pieces.append(("", chunk))
    pieces = [(label, text_) for label, text_ in pieces if text_.strip()]
    if not pieces:
        raise TranscriptError("no transcript text was found")
    segments: List[dict] = []
    clock = 0.0
    for label, chunk in pieces:
        duration = max(MIN_SEGMENT_SEC, _words(chunk) / WORDS_PER_SECOND)
        segments.append(_segment(clock, clock + duration, label, chunk, True))
        clock += duration
    parsed = ParsedTranscript(segments=segments, format="plain", timed=False, duration=clock)
    parsed.speakers = _speakers(segments)
    return parsed


def parse_transcript(text: str, filename: Optional[str] = None) -> ParsedTranscript:
    """Parse ``text`` (any of the layouts in the module docstring). Raises :class:`TranscriptError`."""
    if not isinstance(text, str):
        raise TranscriptError("transcript text must be a string")
    if len(text.encode("utf-8", "ignore")) > MAX_TRANSCRIPT_BYTES:
        raise TranscriptError(f"transcript exceeds the {MAX_TRANSCRIPT_BYTES // (1024 * 1024)} MB limit")
    text = _normalize(text)
    if not text:
        raise TranscriptError("transcript text is empty")
    parsed = _parse_cues(text)
    if parsed is None:
        parsed = _parse_timestamped(text)
    if parsed is None:
        parsed = _parse_plain(text)
    return parsed
