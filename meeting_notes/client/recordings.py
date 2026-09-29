"""Finding, validating and re-queueing recordings saved on this computer.

The save folder keeps every finished recording (``session.json`` plus one WAV
and one ``*.timing.jsonl`` per track). That local copy is the artifact: if the
server ever loses a meeting, pushing the folder back through the normal upload
queue restores it. This module is the non-UI half of that: it lists what is in
a folder, says whether each folder is really a recording the uploader can
send, and puts chosen ones back on the queue.
"""

from __future__ import annotations

import json
import logging
import wave
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

from meeting_notes.client.queue import SessionQueue

log = logging.getLogger("meeting_notes.client.recordings")

# A WAV with only a header (44 bytes) holds no audio.
_WAV_HEADER_BYTES = 44


@dataclass
class RecordingInfo:
    path: Path
    name: str
    started: float  # epoch seconds
    duration_sec: Optional[float]
    size_bytes: int
    queued: bool = False
    error: Optional[str] = None  # None means a valid, uploadable recording

    @property
    def valid(self) -> bool:
        return self.error is None


def dir_size(path: Path) -> int:
    """Total bytes of the files directly inside ``path`` (sessions are flat)."""
    total = 0
    try:
        for child in Path(path).iterdir():
            try:
                if child.is_file():
                    total += child.stat().st_size
            except OSError:
                continue
    except OSError:
        pass
    return total


def read_meta(path: Path) -> Dict:
    meta = json.loads((Path(path) / "session.json").read_text(encoding="utf-8"))
    if not isinstance(meta, dict):
        raise ValueError("session.json is not a JSON object")
    return meta


def started_time(path: Path, meta: Optional[Dict]) -> float:
    """Start of the recording (epoch seconds), falling back to the folder mtime."""
    if meta:
        try:
            return float(meta["started_wall"])
        except (KeyError, TypeError, ValueError):
            pass
        created = meta.get("created")
        if isinstance(created, str):
            try:
                return datetime.fromisoformat(created).timestamp()
            except ValueError:
                pass
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return 0.0


def validate_recording(path: Path) -> Optional[str]:
    """Return why ``path`` cannot be uploaded, or ``None`` if it can."""
    path = Path(path)
    if not path.is_dir():
        return "Not a folder"
    if not (path / "session.json").is_file():
        return "session.json is missing"
    try:
        meta = read_meta(path)
    except (OSError, ValueError) as exc:  # JSONDecodeError is a ValueError
        return f"session.json is unreadable ({exc})"
    tracks = meta.get("tracks")
    if not isinstance(tracks, dict) or not tracks:
        return "No audio tracks are listed in session.json"
    for track, info in sorted(tracks.items()):
        wav_name = info.get("wav") if isinstance(info, dict) else None
        if not wav_name:
            return f"Track '{track}' has no audio file listed"
        wav_path = path / wav_name
        if not wav_path.is_file():
            return f"{wav_name} is missing"
        if wav_path.stat().st_size <= _WAV_HEADER_BYTES:
            return f"{wav_name} is empty"
        try:
            with wave.open(str(wav_path), "rb") as fh:
                if fh.getnframes() <= 0:
                    return f"{wav_name} is empty"
        except (wave.Error, EOFError, OSError):
            return f"{wav_name} is not a readable WAV file"
        if not (path / f"{track}.timing.jsonl").is_file():
            return f"{track}.timing.jsonl is missing"
    return None


def _duration(path: Path, meta: Optional[Dict]) -> Optional[float]:
    if meta:
        try:
            value = float(meta.get("duration_sec"))
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
        best = 0.0
        for info in (meta.get("tracks") or {}).values():
            if not isinstance(info, dict):
                continue
            try:
                best = max(best, float(info.get("duration_sec") or 0))
            except (TypeError, ValueError):
                continue
        if best > 0:
            return best
    # Fall back to the WAV headers (cheap: no audio is read).
    best = 0.0
    for wav_path in path.glob("*.wav"):
        try:
            with wave.open(str(wav_path), "rb") as fh:
                rate = fh.getframerate()
                if rate > 0:
                    best = max(best, fh.getnframes() / rate)
        except (wave.Error, EOFError, OSError):
            continue
    return best or None


def inspect_recording(path: Path, queue: Optional[SessionQueue] = None) -> RecordingInfo:
    """Describe one folder: name, start, length, size, queued, and any problem."""
    path = Path(path)
    meta: Optional[Dict] = None
    try:
        meta = read_meta(path)
    except (OSError, ValueError):
        meta = None
    name = str((meta or {}).get("name") or "").strip() or path.name
    queued = False
    if queue is not None:
        try:
            queued = queue.is_queued(path)
        except OSError:
            queued = False
    is_dir = path.is_dir()
    return RecordingInfo(
        path=path,
        name=name,
        started=started_time(path, meta),
        duration_sec=_duration(path, meta) if is_dir else None,
        size_bytes=dir_size(path) if is_dir else 0,
        queued=queued,
        error=validate_recording(path),
    )


def looks_like_session(path: Path) -> bool:
    try:
        return (
            (path / "session.json").exists()
            or any(path.glob("*.wav"))
            or any(path.glob("*.timing.jsonl"))
        )
    except OSError:
        return False


def session_folders(save_dir: Path) -> List[Path]:
    """Session-looking folders directly under ``save_dir`` (hidden ones skipped;
    the upload queue lives in ``.upload-queue``)."""
    try:
        children = sorted(Path(save_dir).iterdir())
    except OSError:
        return []
    return [
        c for c in children
        if not c.name.startswith(".") and c.is_dir() and looks_like_session(c)
    ]


def scan_save_folder(save_dir: Path, queue: Optional[SessionQueue] = None) -> List[RecordingInfo]:
    """Every session-looking folder under ``save_dir``, newest first.

    Invalid ones are included (with ``error`` set) so the person can see why
    a folder they expected is not offered.
    """
    found = [inspect_recording(folder, queue) for folder in session_folders(save_dir)]
    found.sort(key=lambda r: r.started, reverse=True)
    return found


@dataclass
class ReuploadResult:
    queued: List[Path]
    already_queued: List[Path]
    rejected: Dict[Path, str]

    @property
    def total(self) -> int:
        return len(self.queued) + len(self.already_queued)

    def summary(self) -> str:
        count = self.total
        text = f"{count} recording{'s' if count != 1 else ''} queued for upload"
        if self.rejected:
            n = len(self.rejected)
            text += f"; {n} skipped because {'it is' if n == 1 else 'they are'} not valid"
        return text


def reupload(
    queue: SessionQueue,
    folders: Iterable[Path],
    *,
    wake: Optional[Callable[[], object]] = None,
) -> ReuploadResult:
    """Validate each folder and put it back on ``queue`` with fresh track state.

    ``wake`` (normally ``queue.retry_all_now``) is called once after queueing
    so a backed-off uploader picks the entries up right away.
    """
    result = ReuploadResult([], [], {})
    for folder in folders:
        folder = Path(folder)
        problem = validate_recording(folder)
        if problem:
            result.rejected[folder] = problem
            log.warning("re-upload skipped %s: %s", folder, problem)
            continue
        was_queued = queue.is_queued(folder)
        queue.requeue(folder)
        (result.already_queued if was_queued else result.queued).append(folder)
        log.info(
            "re-upload: %s %s (all tracks will be sent again)",
            folder, "was already on the queue, reset" if was_queued else "queued",
        )
    if result.total and wake is not None:
        try:
            wake()
        except Exception as exc:  # noqa: BLE001 - waking is best-effort; the poll picks it up anyway
            log.warning("re-upload: could not wake the uploader: %s", exc)
    log.info("re-upload: %s", result.summary())
    return result


def format_size(num_bytes: int) -> str:
    size = float(max(0, num_bytes))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"  # pragma: no cover


def format_duration(seconds: Optional[float]) -> str:
    if not seconds or seconds <= 0:
        return "unknown length"
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"
