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
