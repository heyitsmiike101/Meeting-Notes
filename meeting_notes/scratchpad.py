"""The notes scratchpad: free text and links the user types during a meeting.

Both sides import this module. The recorder keeps the text in ``scratchpad.md`` in the session folder (saved as you
type) and sends it as ``meta["scratchpad"]`` with the finished recording; the server stores it in the session meta
exactly as typed, shows it rendered on the meeting page ("Your notes"), puts it in the export zip and the agent APIs, and
appends it to the AI notes prompt as context written by the user.

The text is stored as typed. The meeting page renders it as Markdown on the server (``meeting_notes/markdown.py``, a
port of Life Dash's ``app/services/markdown.py``), whose bare-link rule is the one here (``_BARE_URL`` /
``_split_url_tail``, GFM's autolink rule): http(s) only, ending at whitespace, with sentence punctuation and an
unpaired ``)`` handed back to the text. ``tests/test_scratchpad.py`` pins it.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

# Name of the file the recorder keeps in each session folder (and in the export zip, "<name> - your notes.md").
FILE_NAME = "scratchpad.md"
# Generous for typed notes and pasted links; anything longer is cut (a pasted document is not a scratchpad).
MAX_CHARS = 200_000

_BARE_URL = re.compile(r"(?<!\]\()\bhttps?://[^\s<>\"']+", re.IGNORECASE)
_URL_TAIL_PUNCT = ".,:;!?~*_"
_URL_HAS_HOST = re.compile(r"^https?://[^\s/]", re.IGNORECASE)


def clean(value) -> Optional[str]:
    """The scratchpad text to keep, or None when there is none (not a string, or only whitespace).

    Line endings become ``\\n`` and NULs are dropped; everything else stays exactly as typed (no trimming inside,
    no Markdown processing). Over :data:`MAX_CHARS` is cut to that length."""
    if not isinstance(value, str):
        return None
    text = value.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    if not text.strip():
        return None
    text = text[:MAX_CHARS]
    return text.rstrip("\n") if text.strip() else None


def _split_url_tail(url: str) -> Tuple[str, str]:
    """Split a candidate bare URL into (url, tail): the address, and the sentence punctuation riding on its end."""
    tail = ""
    while url:
        if url[-1] in _URL_TAIL_PUNCT:
            tail = url[-1] + tail
            url = url[:-1]
            continue
        if url.endswith(")") and url.count("(") < url.count(")"):
            tail = ")" + tail
            url = url[:-1]
            continue
        break
    return url, tail


def link_spans(text: Optional[str]) -> List[Tuple[int, int, str]]:
    """``(start, end, url)`` of every http(s) link in ``text``, in order."""
    out: List[Tuple[int, int, str]] = []
    for match in _BARE_URL.finditer(text or ""):
        url, _tail = _split_url_tail(match.group(0))
        if _URL_HAS_HOST.match(url):
            out.append((match.start(), match.start() + len(url), url))
    return out


def link_at(text: Optional[str], index: int) -> Optional[str]:
    """The link that covers character ``index`` of ``text``, or None."""
    for start, end, url in link_spans(text):
        if start <= index < end:
            return url
    return None


def find_links(text: Optional[str]) -> List[str]:
    """Every http(s) link in ``text``, in order, duplicates removed."""
    out: List[str] = []
    for _start, _end, url in link_spans(text):
        if url not in out:
            out.append(url)
    return out


def prompt_section(value) -> str:
    """Text appended to a review's prompt, or '' when the meeting has no scratchpad."""
    text = clean(value)
    if text is None:
        return ""
    lines = [
        "",
        "",
        "## Your notes (written by the user during the meeting)",
        "",
        "The user typed these notes and links while the meeting was running. Treat them as reliable context "
        "from the user, not as something anyone said: use them to get names, terms, numbers and intent right, "
        "and to fill in what the transcript only hints at. Never attribute them to a speaker.",
    ]
    links = find_links(text)
    if links:
        lines.append(
            "Keep every link from these notes, exactly as written: put them in meeting_notes under a "
            "\"Links\" heading, each with a few words on what it is, and use them where they belong elsewhere."
        )
    lines += ["", "<user_notes>", text, "</user_notes>"]
    return "\n".join(lines) + "\n"


def join(texts) -> Optional[str]:
    """Several meetings' scratchpads as one (combine): non-empty ones in order, a blank line between them."""
    parts = [t for t in (clean(t) for t in texts) if t]
    return clean("\n\n".join(parts)) if parts else None
