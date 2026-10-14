"""The meeting search query language.

A query is a list of terms separated by spaces.  A term is a word, a
``"quoted phrase"`` or a field filter ``field:word`` / ``field:"quoted phrase"``.

Fields (case-insensitive; every filter ANDs with the others)::

    title:       the meeting's name, or the title of its newest notes
    attendee:    people on the newest notes (aliases: attendees:, person:)
    notes:       the text of the newest finished notes
    transcript:  the transcript text

A bare word or phrase must match somewhere (name, computer, board number such
as ``M-0142``, transcript, notes or attendees); every bare term has to match.
An unknown ``foo:bar`` is plain text, and a filter with nothing after it
(``title:`` or ``title:""``) is ignored.

This module only parses and holds the notes-text helpers shared by the index
and the agent API; the SQL lives in ``index.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional

FIELDS = ("title", "attendee", "notes", "transcript")
ALIASES = {"attendees": "attendee", "person": "attendee"}
LABELS = {"title": "title", "attendee": "attendee", "notes": "notes", "transcript": "transcript"}


@dataclass(frozen=True)
class Term:
    """One search term: ``field`` is None for a bare word/phrase."""

    text: str
    field: Optional[str] = None


@dataclass(frozen=True)
class Query:
    terms: List[Term] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.terms

    @property
    def has_filters(self) -> bool:
        return any(t.field for t in self.terms)

    def by_field(self, name: Optional[str]) -> List[Term]:
        return [t for t in self.terms if t.field == name]

    @property
    def find_text(self) -> str:
        """What to highlight when a meeting is opened from this search: the first bare,
        ``notes:`` or ``transcript:`` term (title and attendee filters are not text in the view)."""
        for t in self.terms:
            if t.field in (None, "notes", "transcript"):
                return t.text
        return ""

    @property
    def snippet_terms(self) -> List[str]:
        """Terms that can be quoted from a transcript or the notes body."""
        return [t.text for t in self.terms if t.field in (None, "notes", "transcript")]


def canonical_field(name: str) -> Optional[str]:
    low = (name or "").strip().lower()
    low = ALIASES.get(low, low)
    return low if low in FIELDS else None


def _read_quoted(s: str, i: int) -> tuple:
    """``s[i]`` is an opening quote; return (text, index after the closing quote).
    An unterminated quote runs to the end."""
    j = s.find('"', i + 1)
    if j == -1:
        return s[i + 1:], len(s)
    return s[i + 1:j], j + 1


def parse(q: Optional[str]) -> Query:
    s = q or ""
    n = len(s)
    i = 0
    terms: List[Term] = []
    while i < n:
        if s[i].isspace():
            i += 1
            continue
        if s[i] == '"':
            text, i = _read_quoted(s, i)
            text = text.strip()
            if text:
                terms.append(Term(text))
            continue
        # A bare token up to whitespace; it may be "field:" followed by a word or a quoted phrase.
        j = i
        while j < n and not s[j].isspace() and s[j] != '"':
            j += 1
        token = s[i:j]
        colon = token.find(":")
        fld = canonical_field(token[:colon]) if colon > 0 else None
        if fld is not None and colon == len(token) - 1 and j < n and s[j] == '"':
            # field:"quoted phrase"
            text, i = _read_quoted(s, j)
            text = text.strip()
            if text:
                terms.append(Term(text, fld))
            continue
        if fld is not None:
            value = token[colon + 1:]
            i = j
            if value.strip():
                terms.append(Term(value, fld))
            continue
        # Plain text, possibly glued to a quote (abc"def ghi" -> abc, "def ghi").
        i = j
        if token:
            terms.append(Term(token))
    return Query(terms)


# -- notes text (what the index stores) --------------------------------------


def _strs(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        out: List[str] = []
        for entry in value:
            if isinstance(entry, str):
                if entry.strip():
                    out.append(entry)
            elif isinstance(entry, dict):
                for key in ("name", "text", "title", "action", "item"):
                    v = entry.get(key)
                    if isinstance(v, str) and v.strip():
                        out.append(v)
                        break
        return out
    return []


def attendee_names(payload: Any) -> List[str]:
    """Participants of a notes payload (strings or ``{name}`` objects)."""
    if not isinstance(payload, dict):
        return []
    out: List[str] = []
    for entry in payload.get("participants") or []:
        if isinstance(entry, str):
            name = entry.strip()
        elif isinstance(entry, dict):
            name = str(entry.get("name") or "").strip()
        else:
            name = ""
        if name:
            out.append(name)
    return out


def notes_body(payload: Any) -> str:
    """All the searchable text of finished notes (not the title or attendees)."""
    if not isinstance(payload, dict):
        return ""
    parts: List[str] = []
    for key in ("summary", "meeting_notes"):
        parts.extend(_strs(payload.get(key)))
    for key in ("key_points", "decisions", "open_questions", "risks", "next_steps"):
        parts.extend(_strs(payload.get(key)))
    items = payload.get("action_items")
    if isinstance(items, list):
        for item in items:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                for key in ("action", "owner", "context"):
                    v = item.get(key)
                    if isinstance(v, str) and v.strip():
                        parts.append(v)
    return "\n".join(parts)


def notes_title(payload: Any) -> str:
    return str(payload.get("title") or "").strip() if isinstance(payload, dict) else ""
