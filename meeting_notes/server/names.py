"""Server-wide names glossary, persisted at ``<data_root>/names.json``.

Every participant list a user saves teaches the server how people's names are
spelled: ``known_names`` (correct spellings) and ``name_corrections`` (wrong ->
right, from real renames). Both are stored oldest first and capped, so the
oldest entries fall off. The glossary is fed to future notes (appended to the
prompt at serve time, for every note type) and, names only, to transcription
as a bias. It is deliberately separate from settings.json: it changes on every
participants save, not through the settings form.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Iterable, List

MAX_NAMES = 500
MAX_CORRECTIONS = 500
PROMPT_MAX_NAMES = 200
PROMPT_MAX_CORRECTIONS = 200
HOTWORDS_MAX_NAMES = 50
HOTWORDS_MAX_CHARS = 200
MAX_NAME_CHARS = 120

_lock = threading.Lock()


def clean_name(value) -> str:
    """One trimmed line: whitespace (including newlines) collapsed."""
    return re.sub(r"\s+", " ", value).strip() if isinstance(value, str) else ""


def _path(root) -> Path:
    return Path(root) / "names.json"


def load(root) -> dict:
    try:
        raw = json.loads(_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    names: List[str] = []
    for v in raw.get("known_names") or []:
        v = clean_name(v)[:MAX_NAME_CHARS]
        if v and v.casefold() not in {n.casefold() for n in names}:
            names.append(v)
    corrections: List[dict] = []
    for c in raw.get("name_corrections") or []:
        if isinstance(c, dict):
            w, r = clean_name(c.get("wrong"))[:MAX_NAME_CHARS], clean_name(c.get("right"))[:MAX_NAME_CHARS]
            if w and r and w != r:
                corrections.append({"wrong": w, "right": r})
    return {"known_names": names[-MAX_NAMES:], "name_corrections": corrections[-MAX_CORRECTIONS:]}


def _save(root, data: dict) -> None:
    path = _path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".names-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def record(root, saved_names: Iterable[str], renames: Iterable[tuple]) -> dict:
    """Learn from one participants save.

    Every saved name becomes a known name. A rename (old, new) becomes a
    correction unless old equals new, differs only by case, or old is already a
    known name (then it is probably a different real person, not a mishearing).
    Removing a person records nothing.
    """
    with _lock:
        data = load(root)
        known_before = {n.casefold() for n in data["known_names"]}
        corrections = data["name_corrections"]
        for old, new in renames:
            old, new = clean_name(old), clean_name(new)
            if not old or not new or old == new or old.casefold() == new.casefold():
                continue
            if old.casefold() in known_before:
                continue
            corrections = [c for c in corrections if c["wrong"] != old]
            corrections.append({"wrong": old, "right": new})
        names = data["known_names"]
        for n in saved_names:
            n = clean_name(n)
            if n:
                names = [k for k in names if k.casefold() != n.casefold()] + [n]
        data = {"known_names": names[-MAX_NAMES:], "name_corrections": corrections[-MAX_CORRECTIONS:]}
        _save(root, data)
        return data


def add_name(root, name: str) -> dict:
    """Add one person by hand (Settings > People and names). An existing name with other casing is replaced."""
    name = clean_name(name)
    with _lock:
        data = load(root)
        if name:
            names = [n for n in data["known_names"] if n.casefold() != name.casefold()] + [name]
            data["known_names"] = names[-MAX_NAMES:]
            _save(root, data)
        return data


def add_correction(root, wrong: str, right: str) -> dict:
    """Add one correction by hand: transcripts that say ``wrong`` mean ``right``.

    Unlike a learned rename, a hand-made correction is kept even when ``wrong`` is a known name (the user says it is
    wrong, so it stops being a known name) or differs only by case. The correct spelling becomes a known name, and an
    older correction for the same wrong spelling is replaced."""
    wrong, right = clean_name(wrong), clean_name(right)
    with _lock:
        data = load(root)
        if wrong and right and wrong != right:
            fixes = [c for c in data["name_corrections"] if c["wrong"] != wrong] + [{"wrong": wrong, "right": right}]
            names = [n for n in data["known_names"] if n != wrong and n.casefold() != right.casefold()] + [right]
            data = {"known_names": names[-MAX_NAMES:], "name_corrections": fixes[-MAX_CORRECTIONS:]}
            _save(root, data)
        return data


def rename_name(root, old: str, new: str) -> dict:
    """Edit one person's spelling in place (keeps its position). Corrections whose correct spelling was ``old``
    now point to ``new``. Raises KeyError when ``old`` is not a known name, ValueError when ``new`` is another
    existing person (other casing of ``old`` itself is fine)."""
    new = clean_name(new)
    with _lock:
        data = load(root)
        names = data["known_names"]
        if old not in names:
            raise KeyError(old)
        if any(n.casefold() == new.casefold() and n != old for n in names):
            raise ValueError("that person is already in the list")
        data["known_names"] = [new if n == old else n for n in names]
        data["name_corrections"] = [
            {"wrong": c["wrong"], "right": new if c["right"] == old else c["right"]}
            for c in data["name_corrections"]
            if c["wrong"] != new  # a correction from the new spelling to anything would now be wrong
        ]
        _save(root, data)
        return data


def edit_correction(root, wrong: str, right: str, new_wrong: str, new_right: str) -> dict:
    """Edit one correction in place. Its new correct spelling becomes a known name (like adding one by hand), and
    another correction for the same new wrong spelling is replaced. Raises KeyError when it does not exist."""
    new_wrong, new_right = clean_name(new_wrong), clean_name(new_right)
    with _lock:
        data = load(root)
        fixes = data["name_corrections"]
        at = next((i for i, c in enumerate(fixes) if c["wrong"] == wrong and c["right"] == right), None)
        if at is None:
            raise KeyError(wrong)
        edited = {"wrong": new_wrong, "right": new_right}
        fixes = [edited if i == at else c for i, c in enumerate(fixes) if i == at or c["wrong"] != new_wrong]
        names = [n for n in data["known_names"] if n != new_wrong]
        if not any(n.casefold() == new_right.casefold() for n in names):
            names.append(new_right)
        data = {"known_names": names[-MAX_NAMES:], "name_corrections": fixes[-MAX_CORRECTIONS:]}
        _save(root, data)
        return data


def remove_name(root, name: str) -> dict:
    with _lock:
        data = load(root)
        data["known_names"] = [n for n in data["known_names"] if n != name]
        _save(root, data)
        return data


def remove_correction(root, wrong: str, right: str) -> dict:
    with _lock:
        data = load(root)
        data["name_corrections"] = [
            c for c in data["name_corrections"] if not (c["wrong"] == wrong and c["right"] == right)
        ]
        _save(root, data)
        return data


def clear(root) -> dict:
    with _lock:
        data = {"known_names": [], "name_corrections": []}
        _save(root, data)
        return data


IN_PERSON_PROMPT_LINE = (
    "This was an in-person meeting recorded on one phone; 'Speaker N' labels are voices told apart by software, "
    "not names, and may split or merge people."
)


def in_person_section() -> str:
    """Context line appended to the review prompt of an in-person (phone) meeting."""
    return "\n\n## In-person meeting\n\n" + IN_PERSON_PROMPT_LINE + "\n"


def prompt_section(root) -> str:
    """Text appended to a review's prompt, or '' when the glossary is empty."""
    data = load(root)
    names = list(reversed(data["known_names"]))[:PROMPT_MAX_NAMES]
    corrections = list(reversed(data["name_corrections"]))[:PROMPT_MAX_CORRECTIONS]
    if not names and not corrections:
        return ""
    out = ["", "", "## Known names", ""]
    if names:
        out.append(
            "Use these exact spellings when the transcript clearly refers to these people; "
            "never add people who weren't in the meeting: " + "; ".join(names)
        )
    if corrections:
        out.append("")
        out.append(
            "Common mishearings (the transcript may spell a name the wrong way; use the right spelling): "
            + "; ".join(f"'{c['wrong']}' -> '{c['right']}'" for c in corrections)
        )
    return "\n".join(out) + "\n"


def hotwords(root) -> str:
    """Short, bounded list of correct names to bias transcription, or ''.

    Never includes a correction's wrong spelling.
    """
    picked: List[str] = []
    for n in reversed(load(root)["known_names"][-HOTWORDS_MAX_NAMES:]):
        if len(", ".join(picked + [n])) > HOTWORDS_MAX_CHARS:
            break
        picked.append(n)
    return ", ".join(picked)
