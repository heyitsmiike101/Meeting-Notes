"""The recordings commands a server can send (``list_recordings``, ``reupload``, ``delete_local``).

Non-UI, so the window just hands over its save folder, queue and active recording. Everything here
works from the save folder and the upload queue on disk, reusing ``recordings`` (scanning,
validating, re-queueing) and ``retention`` (Recycle Bin / Trash removal).

Safety rules for deleting (the local copy may be the only copy of a meeting):

* an id is only ever matched against the folders that really are in the save folder, never joined
  into a path, so ``..`` or an absolute path can not reach anything else;
* the recording in progress is refused (``active_recording``);
* a recording an uploader holds right now is refused (``uploading``);
* the folder goes to the Recycle Bin / Trash, and its queue entry is dropped so the uploader does not
  keep retrying a folder that is gone.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from meeting_notes import remote
from meeting_notes.client import recordings, retention
from meeting_notes.client.queue import SessionQueue

log = logging.getLogger("meeting_notes.client.remote_recordings")

# A list reply never grows past this many bytes of JSON (leaves room for the ack's own fields).
_RESULT_BUDGET = remote.MAX_RESULT_BYTES - 8 * 1024


def _same(a: Path, b: Optional[Path]) -> bool:
    if b is None:
        return False
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return False


def queue_view(queue: SessionQueue, folder: Path) -> Dict[str, Any]:
    """This recording's place on the upload queue, in ``remote.sanitize_recording``'s ``queue`` shape."""
    entry_id = queue.entry_id(folder)
    state = queue.read_state(entry_id)
    if state is None:
        return {"state": "not_queued", "percent": None, "error": None, "attempts": 0}
    attempts = int(state.get("attempts") or 0)
    error = state.get("last_error") or None
    if state.get("status") == "failed":
        return {"state": "failed", "percent": None, "error": error, "attempts": attempts}
    claimed = queue._claim_path(entry_id).exists()  # noqa: SLF001 - the queue's own claim marker
    if state.get("finalized") and not claimed:
        return {"state": "awaiting_transcript", "percent": 100.0, "error": None, "attempts": attempts}
    if claimed or state.get("upload_state") == "uploading":
        percent = state.get("upload_percent")
        return {"state": "uploading", "percent": percent if isinstance(percent, (int, float)) else None,
                "error": None, "attempts": attempts}
    return {"state": "pending", "percent": None, "error": error, "attempts": attempts}


def describe(info: recordings.RecordingInfo, queue: SessionQueue, active_dir: Optional[Path] = None) -> Dict[str, Any]:
    """One ``list_recordings`` row (also what the re-upload window feeds ``recording_status.combine``)."""
    active = _same(info.path, active_dir)
    view = queue_view(queue, info.path)
    if active:
        view = {"state": "recording", "percent": None, "error": None, "attempts": 0}
    return {
        "session_id": info.path.name,
        "name": info.name,
        "started": info.started,
        "duration_sec": info.duration_sec,
        "size_bytes": info.size_bytes,
        "valid": bool(info.valid) and not active,
        "reason": "Recording in progress" if active else info.error,
        "active": active,
        "queue": view,
    }


def list_recordings(
    save_dir: Path,
    queue: SessionQueue,
    active_dir: Optional[Path] = None,
    offset: int = 0,
    *,
    budget: int = _RESULT_BUDGET,
    limit: int = remote.MAX_RECORDINGS,
) -> Dict[str, Any]:
    """Recordings in ``save_dir``, newest first, one page that fits ``budget`` bytes of JSON.

    ``next_offset`` is set when more remain (the server asks again from there). Nothing past
    ``limit`` recordings is ever returned; ``total`` still counts every folder.
    """
    infos = recordings.scan_save_folder(save_dir, queue)
    total = len(infos)
    rows: List[Dict[str, Any]] = []
    used = 0
    index = max(0, int(offset))
    while index < min(total, limit):
        row = describe(infos[index], queue, active_dir)
        size = len(json.dumps(row, separators=(",", ":")).encode("utf-8")) + 1
        if rows and used + size > budget:
            break
        rows.append(row)
        used += size
        index += 1
    more = index < min(total, limit)
    return {"recordings": rows, "total": total, "offset": max(0, int(offset)), "next_offset": index if more else None}


def _folders_by_id(save_dir: Path) -> Dict[str, Path]:
    """Session folders that really exist in ``save_dir``, keyed by folder name."""
    return {folder.name: folder for folder in recordings.session_folders(save_dir)}


def reupload(
    save_dir: Path,
    queue: SessionQueue,
    session_ids: List[str],
    submit: Callable[[List[Path]], recordings.ReuploadResult],
    active_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Re-queue the named recordings through ``submit`` (the window's ``controller.reupload_recordings``,
    the same path as its Re-upload dialog). Returns the ``reupload`` result."""
    folders = _folders_by_id(save_dir)
    results: List[Dict[str, Any]] = []
    chosen: List[Path] = []
    for sid in session_ids:
        folder = folders.get(sid)
        if folder is None:
            results.append({"session_id": sid, "ok": False, "code": "not_found",
                            "error": "No such recording in the save folder."})
        elif _same(folder, active_dir):
            results.append({"session_id": sid, "ok": False, "code": "active_recording",
                            "error": "This recording is still in progress."})
        else:
            chosen.append(folder)
    queued = already = 0
    if chosen:
        outcome = submit(chosen)
        rejected = {Path(p).name: why for p, why in outcome.rejected.items()}
        done = {Path(p).name for p in list(outcome.queued) + list(outcome.already_queued)}
        queued, already = len(outcome.queued), len(outcome.already_queued)
        for folder in chosen:
            if folder.name in rejected:
                results.append({"session_id": folder.name, "ok": False, "code": "invalid",
                                "error": rejected[folder.name]})
            else:
                results.append({"session_id": folder.name, "ok": folder.name in done, "code": None, "error": None})
    order = {sid: i for i, sid in enumerate(session_ids)}
    results.sort(key=lambda r: order.get(r["session_id"], 0))
    return {"results": results, "queued": queued, "already_queued": already}


def is_uploading(queue: SessionQueue, folder: Path) -> bool:
    return queue_view(queue, folder)["state"] == "uploading"


def delete_folders(
    paths: List[Path],
    queue: SessionQueue,
    active_dir: Optional[Path] = None,
    remover: Optional[Callable[[Path], str]] = None,
) -> Dict[str, Any]:
    """Move these recording folders to the Recycle Bin / Trash. Per-folder results (``session_id`` is the
    folder name); one failure never stops the rest. The safety rules in the module docstring live here:
    the recording in progress and one an uploader holds right now are refused."""
    remover = remover or retention.move_to_recycle_bin  # resolved at call time so tests can patch it
    results: List[Dict[str, Any]] = []
    deleted = freed = 0
    for folder in paths:
        folder = Path(folder)
        sid = folder.name
        if _same(folder, active_dir):
            results.append({"session_id": sid, "ok": False, "code": "active_recording",
                            "error": "This recording is still in progress.", "bytes": 0, "method": None})
            continue
        if is_uploading(queue, folder):
            results.append({"session_id": sid, "ok": False, "code": "uploading",
                            "error": "This recording is uploading right now. Try again when it finishes.",
                            "bytes": 0, "method": None})
            continue
        size = recordings.dir_size(folder)
        try:
            method = remover(folder)
        except Exception as exc:  # noqa: BLE001 - one bad folder must not stop the rest
            log.warning("could not delete %s: %s", folder, exc)
            results.append({"session_id": sid, "ok": False, "code": "failed",
                            "error": f"Could not delete: {exc}", "bytes": 0, "method": None})
            continue
        try:
            queue.mark_done(queue.entry_id(folder))  # nothing left to upload
        except OSError as exc:
            log.warning("could not drop the queue entry of %s: %s", folder.name, exc)
        deleted += 1
        freed += size
        log.info("moved %s to the %s (%s)", folder.name, retention.trash_name(), method)
        results.append({"session_id": sid, "ok": True, "code": None, "error": None, "bytes": size, "method": method})
    return {"results": results, "deleted": deleted, "freed_bytes": freed}


def delete_local(
    save_dir: Path,
    queue: SessionQueue,
    session_ids: List[str],
    active_dir: Optional[Path] = None,
    remover: Optional[Callable[[Path], str]] = None,
) -> Dict[str, Any]:
    """Move the named recordings to the Recycle Bin / Trash. Per-id results; one failure never stops the rest."""
    folders = _folders_by_id(save_dir)
    missing: Dict[str, Dict[str, Any]] = {}
    found: List[Path] = []
    for sid in session_ids:
        folder = folders.get(sid)
        if folder is None:
            missing[sid] = {"session_id": sid, "ok": False, "code": "not_found",
                            "error": "No such recording in the save folder.", "bytes": 0, "method": None}
        else:
            found.append(folder)
    outcome = delete_folders(found, queue, active_dir, remover)
    by_id = {r["session_id"]: r for r in outcome["results"]}
    results = [missing[sid] if sid in missing else by_id[sid] for sid in session_ids]
    return {"results": results, "deleted": outcome["deleted"], "freed_bytes": outcome["freed_bytes"]}
