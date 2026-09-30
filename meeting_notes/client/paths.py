"""Where recordings may live, and moving them out of the app folder.

Installing a new client replaces ``%LOCALAPPDATA%/MeetingNotes`` on Windows or
``~/Applications/Meeting Notes.app`` on macOS (and the running exe's own directory), so a recordings folder inside either would be
wiped by an update. These helpers normalise paths case-insensitively and do the
copy-verify-delete move.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Callable, List, Optional

log = logging.getLogger("meeting_notes.client.paths")


def _norm(path) -> str:
    text = os.path.normcase(os.path.abspath(os.path.expanduser(os.path.expandvars(str(path)))))
    if sys.platform == "darwin":
        text = text.lower()  # APFS/HFS+ are case-insensitive by default
    return text.rstrip("\\/")


def app_folders() -> List[Path]:
    """Folders an installer may replace: the install dir and the exe's directory."""
    folders: List[Path] = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        folders.append(Path(local) / "MeetingNotes")
    # A packaged (Nuitka) build runs from its own directory; a source run's
    # interpreter directory is not "the app", so only frozen/compiled builds count.
    if sys.platform == "darwin":
        folders.append(Path.home() / "Applications" / "Meeting Notes.app")
    if getattr(sys, "frozen", False) or "__compiled__" in globals() or _is_compiled():
        exe_dir = Path(sys.executable).resolve().parent
        folders.append(exe_dir)
        if sys.platform == "darwin" and exe_dir.name == "MacOS" and exe_dir.parent.name == "Contents":
            folders.append(exe_dir.parent.parent)  # the .app bundle itself
    return folders


def _is_compiled() -> bool:
    main = sys.modules.get("__main__")
    return bool(main is not None and hasattr(main, "__compiled__"))


def inside_app_folder(path, folders: Optional[List[Path]] = None) -> Optional[Path]:
    """The app folder that contains ``path`` (or equals it), else None."""
    target = _norm(path)
    for folder in folders if folders is not None else app_folders():
        base = _norm(folder)
        if target == base or target.startswith(base + os.sep):
            return Path(folder)
    return None


def validate_save_dir(path, folders: Optional[List[Path]] = None) -> Optional[str]:
    """An error sentence if ``path`` may not hold recordings, else None."""
    folder = inside_app_folder(path, folders)
    if folder is None:
        return None
    return (
        f"Recordings can't be saved inside the app folder ({folder}): installing an "
        "update replaces it. Choose a folder such as Documents/Meeting Notes."
    )


def move_recordings(
    source: Path,
    destination: Path,
    progress: Optional[Callable[[str], None]] = None,
) -> int:
    """Copy every entry of ``source`` into ``destination``, verify sizes, then delete.

    Returns the number of files moved. Nothing is deleted from ``source`` unless
    every copy verified; on any mismatch the copies are left and the original
    stays the source of truth. Queue state (``.upload-queue``) is rewritten to
    point at the new location.
    """
    source, destination = Path(source), Path(destination)
    if not source.is_dir():
        return 0
    destination.mkdir(parents=True, exist_ok=True)
    files = [p for p in source.rglob("*") if p.is_file() and p.suffix != ".claim"]
    copied: List[tuple] = []
    for src in files:
        dst = destination / src.relative_to(source)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists() and dst.stat().st_size == src.stat().st_size:
            copied.append((src, dst))
            continue
        shutil.copy2(src, dst)
        if dst.stat().st_size != src.stat().st_size:
            log.error("move verify failed for %s", src)
            raise OSError(f"copy of {src.name} did not verify; originals left in place")
        copied.append((src, dst))
        if progress:
            progress(src.name)
    _repoint_queue(destination / ".upload-queue", source, destination)
    for src, _dst in copied:
        try:
            src.unlink()
        except OSError:
            log.warning("could not delete moved file %s", src)
    for directory in sorted((p for p in source.rglob("*") if p.is_dir()), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass
    log.info("moved %d recording files from %s to %s", len(copied), source, destination)
    return len(copied)


def _repoint_queue(queue_dir: Path, old_root: Path, new_root: Path) -> None:
    import json

    if not queue_dir.is_dir():
        return
    for path in queue_dir.glob("*.json"):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            session_dir = state.get("session_dir") or ""
            if session_dir and _norm(session_dir).startswith(_norm(old_root)):
                relative = Path(session_dir).resolve().relative_to(Path(old_root).resolve())
                state["session_dir"] = str(Path(new_root).resolve() / relative)
                path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        except (OSError, ValueError):
            continue
