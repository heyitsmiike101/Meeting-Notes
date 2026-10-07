"""Spotting a goodbye in live-preview text (Auto end "When people say goodbye").

Pure text matching, no Qt and no state, so it is tested on its own. The window feeds it each live
partial and, after a farewell, waits for both tracks to go quiet before it ends the recording.

Rules, kept deliberately simple:

* Case-insensitive, whole-word matching on the words of the text (punctuation ignored). Apostrophes stay
  inside a word, so "see you're right" is not "see you".
* A long partial (more than ``LONG_PARTIAL_WORDS`` words) counts only if the phrase is in its last
  ``LONG_PARTIAL_TAIL`` words: people say goodbye at the end of what they say.
* "see you" counts only at the end or before a goodbye-ish word ("see you later", "see you next week",
  "see you then"), never in "I see you have a point".
* "take care" does not count before "of" ("take care of the deploy"); "goodbye" does not count before "to".
* The thanks-the-room phrases ("thanks everyone", "thank you all", ...) are also how meetings open, so they
  count only in a short partial (``THANKS_MAX_WORDS`` words or fewer) and only as its closing words.
"""

from __future__ import annotations

import re
from typing import List, Optional

LONG_PARTIAL_WORDS = 15
LONG_PARTIAL_TAIL = 6
THANKS_MAX_WORDS = 8

_WORD = re.compile(r"[a-z0-9]+(?:'[a-z]+)*")

_DAYPARTS = r"(?:day|night|weekend|evening|afternoon|morning|one|rest of (?:your|the) day|rest of (?:your|the) week)"
_SEE_YOU_NEXT = (
    r"(?:later|soon|then|there|around|tomorrow|next (?:time|week|month|call|meeting)|all|guys|everyone|folks"
    r"|(?:on )?(?:monday|tuesday|wednesday|thursday|friday)|in (?:a bit|the morning|a while))"
)

# Each entry: (name, regex over the space-joined words)
_PATTERNS = (
    ("goodbye", r"good ?bye(?! to\b)"),
    ("bye", r"bye(?: bye| now| everyone| all| guys| folks)?"),
    ("see you", rf"see (?:you|ya)(?:$| {_SEE_YOU_NEXT}(?= |$))"),
    ("talk to you later", r"talk(?:ing)? to you (?:later|soon|tomorrow|next (?:week|time))"),
    ("talk soon", r"talk soon"),
    ("take care", r"take care(?! of\b)"),
    ("have a good one", rf"have a (?:good|great|nice|wonderful|lovely) {_DAYPARTS}"),
    ("catch you later", r"catch you (?:later|soon|next time)"),
    ("later everyone", r"later (?:everyone|all|guys|folks|y'all)"),
    ("cheers", r"cheers"),
)
_THANKS = ("thanks everyone", r"(?:thanks|thank you) (?:everyone|everybody|all|guys|folks|team)(?: again| so much| very much| today)?")

_COMPILED = [(name, re.compile(rf"(?:^| )({pattern})(?= |$)")) for name, pattern in _PATTERNS]
_THANKS_RE = re.compile(rf"(?:^| )({_THANKS[1]})$")


def words(text: str) -> List[str]:
    """The lower-cased words of ``text`` (punctuation dropped, apostrophes kept inside a word)."""
    return _WORD.findall((text or "").lower().replace("\u2019", "'"))


def find_farewell(text: str) -> Optional[str]:
    """The farewell phrase heard in ``text`` (as matched, lower case), or ``None``."""
    tokens = words(text)
    if not tokens:
        return None
    total = len(tokens)
    scan = tokens[-LONG_PARTIAL_TAIL:] if total > LONG_PARTIAL_WORDS else tokens
    joined = " ".join(scan)
    for name, regex in _COMPILED:
        found = regex.search(joined)
        if found:
            return found.group(1)
    if total <= THANKS_MAX_WORDS:
        found = _THANKS_RE.search(joined)
        if found:
            return found.group(1)
    return None


def is_farewell(text: str) -> bool:
    """True when ``text`` (a live partial) sounds like someone saying goodbye."""
    return find_farewell(text) is not None
