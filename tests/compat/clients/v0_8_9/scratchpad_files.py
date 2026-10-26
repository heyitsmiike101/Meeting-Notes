# COMPAT FIXTURE - do not edit. Verbatim copy of meeting_notes/client/scratchpad.py from the 0.8.9 client
# (release/0.8.9), with only the meeting_notes.* imports rewritten to be package-relative. `meeting_notes.config` (the draft's folder, not network code) stays the live module.
"""Where the recorder keeps the notes scratchpad on disk (no Qt here).

While a recording runs the text lives in ``<session folder>/scratchpad.md``; before one starts it is a draft next to
the config (``~/.meeting-notes/scratchpad-draft.md``) that the next recording takes over. Every save replaces the file
atomically, so a crash loses at most the last pause in typing. ``RecordingController.stop()`` reads the session file
into ``meta["scratchpad"]``; the upload queue falls back to the file when an older ``session.json`` lacks the key.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from meeting_notes import config as config_mod
from . import scratchpad as scratchpad_mod

log = logging.getLogger("meeting_notes.client.scratchpad")

DRAFT_NAME = "scratchpad-draft.md"


def draft_path() -> Path:
    return config_mod.config_path().parent / DRAFT_NAME


def session_path(session_dir) -> Path:
    return Path(session_dir) / scratchpad_mod.FILE_NAME


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def save(text: str, session_dir=None) -> bool:
    """Save the scratchpad: into the session folder while recording, else as the draft. Never raises."""
    path = session_path(session_dir) if session_dir else draft_path()
    try:
        if not text and not session_dir:
            path.unlink(missing_ok=True)  # an emptied draft is no draft
        else:
            _write(path, text or "")
        return True
    except OSError:
        log.warning("could not save the notes scratchpad to %s", path, exc_info=True)
        return False


def load_draft() -> str:
    return _read(draft_path())


def attach_draft(session_dir) -> str:
    """At recording start: move the draft into the new session folder. Returns the text ('' when there was none)."""
    text = load_draft()
    if text:
        if save(text, session_dir):
            try:
                draft_path().unlink(missing_ok=True)
            except OSError:
                log.debug("could not remove the scratchpad draft", exc_info=True)
    return text


def read_session(session_dir) -> Optional[str]:
    """The session's scratchpad as it goes into the meta, or None when it has none."""
    if not session_dir:
        return None
    return scratchpad_mod.clean(_read(session_path(session_dir)))
