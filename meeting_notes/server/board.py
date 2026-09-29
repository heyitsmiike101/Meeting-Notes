"""Board numbers: the short ``M-0142`` label each meeting carries on the shelf.

A board number is derived from the session id (32-bit FNV-1a, modulo 10000), so
it is stable across restarts, reindexing and browsers without needing a stored
counter. It is a display label, not a key: two sessions can in principle share
one, and the session id stays the identity everywhere else.
"""

from __future__ import annotations

import re
from typing import Iterable, List, Optional

_FNV_OFFSET = 0x811C9DC5
_FNV_PRIME = 0x01000193
_QUERY = re.compile(r"^\s*m-?(\d{1,4})\s*$", re.IGNORECASE)


def board_digits(session_id: str) -> str:
    """The four digits of a session's board number, zero padded."""
    value = _FNV_OFFSET
    for byte in session_id.encode("utf-8"):
        value ^= byte
        value = (value * _FNV_PRIME) & 0xFFFFFFFF
    return f"{value % 10000:04d}"


def board_number(session_id: str) -> str:
    """The display label, e.g. ``M-0142``."""
    return "M-" + board_digits(session_id)


def parse_board_query(q: Optional[str]) -> Optional[str]:
    """Digits typed after ``M-`` (one to four), or None if ``q`` is not a board query."""
    match = _QUERY.match(q or "")
    return match.group(1) if match else None


def matching_ids(session_ids: Iterable[str], digits: str) -> List[str]:
    """Session ids whose board number equals (four digits) or starts with ``digits``."""
    return [sid for sid in session_ids if board_digits(sid).startswith(digits)]
