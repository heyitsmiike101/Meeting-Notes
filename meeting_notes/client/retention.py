"""Optional clean-up of recordings kept on this computer.

The local save folder is the only backup a meeting has if the server ever
loses it, so this module is built around one rule: **never delete when in
doubt**. A recording folder is removed only when every one of these holds:

* it is not the recording in progress;
* it has a readable ``session.json`` (so it is a real recording);
* it is no longer on the upload queue (not pending, not failed);
* it is older than the chosen number of days (start time from its metadata,
  folder modification time as the fallback);
* the server, reachable with a valid token right now, returns the meeting
  (HTTP 200), does not mark it deleted/trashed, and shows both the upload and
  the transcription as complete with a finished transcript.

Any other outcome (404, other HTTP status, unreachable server, rejected
token, still transcribing, a response we cannot read) keeps the local copy and
logs why. Deletion goes to the Windows Recycle Bin (macOS Trash) where possible.
"""

from __future__ import annotations

import logging
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import httpx

from meeting_notes.client.api import ServerClient, ServerUnavailable
from meeting_notes.client.queue import SessionQueue
from meeting_notes.client.recordings import (
    dir_size,
    format_size,
    read_meta,
    session_folders,
    started_time,
)

log = logging.getLogger("meeting_notes.client.retention")

DELETE = "delete"
KEEP = "keep"

# Keys that mean "this meeting was deleted / is in the server's trash". Checked
# on the detail document and on its ``meta``. Any truthy value keeps the local
# copy, whichever spelling the server uses.
_TRASH_KEYS = (
    "deleted", "deleted_at", "is_deleted", "trashed", "trashed_at", "is_trashed",
    "in_trash", "trash", "recently_deleted",
)
_TRASH_STATES = {"deleted", "trashed", "trash"}


@dataclass
class Decision:
    folder: Path
    action: str  # DELETE or KEEP
    reason: str
    size_bytes: int = 0

    @property
    def delete(self) -> bool:
        return self.action == DELETE


@dataclass
class CleanupReport:
    deleted: List[Decision] = field(default_factory=list)
    kept: List[Decision] = field(default_factory=list)
    failed: List[Decision] = field(default_factory=list)

    @property
    def freed_bytes(self) -> int:
        return sum(d.size_bytes for d in self.deleted)

    def summary(self) -> str:
        n = len(self.deleted)
        if not n:
            return "Nothing to clean up"
        return (
            f"Moved {n} recording{'s' if n != 1 else ''} to the {trash_name()}, "
            f"freeing {format_size(self.freed_bytes)}"
        )


def _trash_reason(detail: Dict[str, Any]) -> Optional[str]:
    """Why ``detail`` looks deleted/trashed, or ``None`` if nothing says so."""
    meta = detail.get("meta") if isinstance(detail.get("meta"), dict) else {}
    for source in (detail, meta):
        for key in _TRASH_KEYS:
            if source.get(key):
                return f"server marks the meeting as deleted ({key})"
    pipeline = detail.get("pipeline") if isinstance(detail.get("pipeline"), dict) else {}
    for value in (detail.get("state"), detail.get("status"), pipeline.get("state")):
        if isinstance(value, str) and value.lower() in _TRASH_STATES:
            return f"server marks the meeting as deleted (state {value})"
    return None


def server_safe_reason(detail: Any, session_id: str) -> Optional[str]:
    """``None`` when the server's detail proves the meeting is safely stored,
    otherwise the reason it is not (which keeps the local copy)."""
    if not isinstance(detail, dict):
        return "server sent an unreadable response"
    trashed = _trash_reason(detail)
    if trashed:
        return trashed
    if detail.get("session_id") != session_id:
        return "server response is for a different meeting"
    pipeline = detail.get("pipeline")
    if not isinstance(pipeline, dict):
        return "server did not report the processing state"
    upload = (pipeline.get("upload") or {}).get("state")
    if upload != "complete":
        return f"upload on the server is {upload or 'unknown'}"
    transcription = (pipeline.get("transcription") or {}).get("state")
    if transcription != "complete":
        return f"transcription on the server is {transcription or 'unknown'}"
    if not detail.get("transcript_job_id"):
        return "server has no finished transcript"
    return None


class ServerUnusable(Exception):
    """The server cannot vouch for anything right now (down / token rejected)."""


def _check_server(fetch_detail: Callable[[str], Any], session_id: str) -> Optional[str]:
    """Ask the server; return a keep-reason or ``None`` if it is safe to delete."""
    try:
        detail = fetch_detail(session_id)
    except ServerUnavailable:
        raise ServerUnusable("server is unreachable") from None
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code in (401, 403):
            raise ServerUnusable(f"server rejected the token (HTTP {code})") from None
        if code == 404:
            return "server does not have this meeting (HTTP 404)"
        return f"server answered HTTP {code}"
    except Exception as exc:  # noqa: BLE001 - unreadable/odd response: never delete
        return f"could not verify with the server ({type(exc).__name__})"
    return server_safe_reason(detail, session_id)


def evaluate_recording(
    folder: Path,
    *,
    days: int,
    queue: SessionQueue,
    fetch_detail: Callable[[str], Any],
    active_dir: Optional[Path] = None,
    now: Optional[float] = None,
) -> Decision:
    """Decide the fate of one folder. Raises ``ServerUnusable`` when the
    server cannot be relied on (the caller then keeps everything)."""
    folder = Path(folder)
    now = time.time() if now is None else now
    size = dir_size(folder)

    def keep(reason: str) -> Decision:
        return Decision(folder, KEEP, reason, size)

    if days <= 0:
        return keep("keeping recordings forever")
    if active_dir is not None:
        try:
            if folder.resolve() == Path(active_dir).resolve():
                return keep("recording in progress")
        except OSError:
            return keep("could not tell whether it is the active recording")
    try:
        meta = read_meta(folder)
    except (OSError, ValueError):
        return keep("no readable session.json")
    if queue.is_queued(folder):
        return keep("still on the upload queue")
    age_days = (now - started_time(folder, meta)) / 86400.0
    if age_days < days:
        return keep(f"only {age_days:.1f} days old (keeping {days})")
    reason = _check_server(fetch_detail, folder.name)
    if reason:
        return keep(reason)
    return Decision(folder, DELETE, f"safe on the server and {age_days:.0f} days old", size)


def plan_cleanup(
    save_dir: Path,
    days: int,
    *,
    queue: SessionQueue,
    fetch_detail: Callable[[str], Any],
    active_dir: Optional[Path] = None,
    now: Optional[float] = None,
) -> List[Decision]:
    """Evaluate every recording folder without deleting anything."""
    decisions: List[Decision] = []
    unusable: Optional[str] = None
    for folder in session_folders(save_dir):
        if unusable is not None:
            # One "server cannot vouch" answer covers the rest: no more calls.
            decision = Decision(folder, KEEP, unusable, dir_size(folder))
        else:
            try:
                decision = evaluate_recording(
                    folder, days=days, queue=queue, fetch_detail=fetch_detail,
                    active_dir=active_dir, now=now,
                )
            except ServerUnusable as exc:
                unusable = str(exc)
                decision = Decision(folder, KEEP, unusable, dir_size(folder))
        decisions.append(decision)
        if not decision.delete:
            log.info("cleanup: keeping %s: %s", folder.name, decision.reason)
    return decisions


def summarize_kept(decisions: List[Decision]) -> str:
    """A short "3 too recent, 1 still uploading" line for the kept recordings."""
    labels = (
        ("only ", "too recent"),
        ("still on the upload queue", "still uploading"),
        ("server does not have", "not on the server"),
        ("upload on the server", "still processing"),
        ("transcription on the server", "still processing"),
        ("server has no finished", "still processing"),
        ("server is unreachable", "server unreachable"),
        ("server rejected", "server rejected the token"),
        ("recording in progress", "recording in progress"),
    )
    counts = {}
    for d in decisions:
        if d.delete:
            continue
        label = next((text for prefix, text in labels if d.reason.startswith(prefix)), "other")
        counts[label] = counts.get(label, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: -kv[1])
    return ", ".join(f"{n} {label}" for label, n in ordered)


# -- removal -----------------------------------------------------------------------

def trash_name() -> str:
    """What the platform calls the place removed recordings go."""
    return "Trash" if sys.platform == "darwin" else "Recycle Bin"


def move_to_recycle_bin(path: Path) -> str:
    """Send ``path`` to the Recycle Bin (Windows) or the Trash (macOS).

    Returns ``"recycle-bin"`` or ``"trash"``. Falls back to permanent deletion
    (returns ``"permanent"``) on other platforms or if the shell operation is
    unavailable.
    """
    path = Path(path)
    if sys.platform == "darwin":
        try:
            _mac_trash(path)
            return "trash"
        except Exception as exc:  # noqa: BLE001
            log.warning("Trash unavailable for %s (%s); deleting permanently", path, exc)
    if sys.platform == "win32":
        try:
            _shell_recycle(path)
            return "recycle-bin"
        except Exception as exc:  # noqa: BLE001
            log.warning("recycle bin unavailable for %s (%s); deleting permanently", path, exc)
    shutil.rmtree(path)
    return "permanent"


def _mac_trash(path: Path) -> None:
    """Move ``path`` to the user's Trash with NSFileManager (restorable in Finder)."""
    from Foundation import NSFileManager, NSURL

    url = NSURL.fileURLWithPath_(str(path.resolve()))
    ok, _result, error = NSFileManager.defaultManager().trashItemAtURL_resultingItemURL_error_(url, None, None)
    if not ok or path.exists():
        raise OSError(f"trashItemAtURL failed: {error}")


def _shell_recycle(path: Path) -> None:
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _pack_ = 1 if ctypes.sizeof(ctypes.c_void_p) == 4 else 8
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", ctypes.c_ushort),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    FO_DELETE = 3
    FOF_SILENT = 0x0004
    FOF_NOCONFIRMATION = 0x0010
    FOF_ALLOWUNDO = 0x0040
    FOF_NOERRORUI = 0x0400
    op = SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = FO_DELETE
    # pFrom is a list of paths, each NUL-terminated, ending with a second NUL.
    from_buffer = ctypes.create_unicode_buffer(str(path.resolve()) + "\0\0")
    op.pFrom = ctypes.cast(from_buffer, wintypes.LPCWSTR)
    op.pTo = None
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_NOERRORUI | FOF_SILENT
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if result != 0 or op.fAnyOperationsAborted or path.exists():
        raise OSError(f"SHFileOperationW failed (code {result})")


def execute_plan(
    plan: List[Decision],
    *,
    queue: SessionQueue,
    active_dir: Optional[Path] = None,
    remover: Callable[[Path], str] = move_to_recycle_bin,
) -> CleanupReport:
    """Delete the planned folders, re-checking the cheap local conditions first
    (the queue and the active recording may have changed since planning)."""
    report = CleanupReport()
    for decision in plan:
        if not decision.delete:
            report.kept.append(decision)
            continue
        folder = decision.folder
        recheck: Optional[str] = None
        if active_dir is not None and folder.resolve() == Path(active_dir).resolve():
            recheck = "recording in progress"
        elif queue.is_queued(folder):
            recheck = "still on the upload queue"
        elif not folder.exists():
            recheck = "already gone"
        if recheck:
            report.kept.append(Decision(folder, KEEP, recheck, decision.size_bytes))
            log.info("cleanup: keeping %s: %s", folder.name, recheck)
            continue
        try:
            method = remover(folder)
        except Exception as exc:  # noqa: BLE001 - one bad folder must not stop the rest
            report.failed.append(Decision(folder, KEEP, f"could not delete: {exc}", decision.size_bytes))
            log.warning("cleanup: could not delete %s: %s", folder, exc)
            continue
        report.deleted.append(decision)
        log.info(
            "cleanup: deleted %s (%s, %s, %s)",
            folder.name, decision.reason, format_size(decision.size_bytes), method,
        )
    if report.deleted or report.failed:
        log.info("cleanup: %s", report.summary())
    return report


# -- server-backed entry points ------------------------------------------------------

def _client(url: str, token: Optional[str]) -> ServerClient:
    return ServerClient(url, token or None, timeout=10.0)


def plan_with_server(
    save_dir: Path,
    days: int,
    url: str,
    token: Optional[str],
    *,
    active_dir: Optional[Path] = None,
    client_factory: Optional[Callable[[], ServerClient]] = None,
) -> List[Decision]:
    """Dry run against the configured server (nothing is deleted)."""
    queue = SessionQueue.for_save_dir(Path(save_dir))
    if not url:
        log.info("cleanup: no server configured; keeping every recording")
        return [Decision(f, KEEP, "no server configured", dir_size(f)) for f in session_folders(save_dir)]
    client = client_factory() if client_factory else _client(url, token)
    try:
        return plan_cleanup(
            save_dir, days, queue=queue, fetch_detail=client.session_detail, active_dir=active_dir
        )
    finally:
        client.close()


def run_with_server(
    save_dir: Path,
    days: int,
    url: str,
    token: Optional[str],
    *,
    active_dir: Optional[Path] = None,
    client_factory: Optional[Callable[[], ServerClient]] = None,
    remover: Callable[[Path], str] = move_to_recycle_bin,
) -> CleanupReport:
    """Plan and execute in one go (the background policy run)."""
    log.info("cleanup: policy run, keep %d days", days)
    queue = SessionQueue.for_save_dir(Path(save_dir))
    plan = plan_with_server(save_dir, days, url, token, active_dir=active_dir, client_factory=client_factory)
    return execute_plan(plan, queue=queue, active_dir=active_dir, remover=remover)


def folder_stats(save_dir: Path) -> Dict[str, int]:
    """Number of recording folders and their total size in bytes."""
    folders = session_folders(save_dir)
    return {"count": len(folders), "bytes": sum(dir_size(f) for f in folders)}
