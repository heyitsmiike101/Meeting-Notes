"""Diagnostic log bundles uploaded by the Windows client's Logs window.

Layout: ``<data_root>/client-logs/<device>/<UTC timestamp>-<short id>.zip``.
The device folder name is sanitized and the file name is generated here, so a
request can never choose a path. Only the newest ``KEEP_PER_DEVICE`` bundles of
each device are kept.
"""

from __future__ import annotations

import os
import re
import shutil
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, List, Optional

MAX_BYTES = 25 * 1024 * 1024
KEEP_PER_DEVICE = 20

_ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06")
_DEVICE_OK = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,64}$")
_NAME_OK = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}\.zip$")
_WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10)),
}


class ClientLogError(ValueError):
    """The upload was rejected; ``status`` is the HTTP status to answer with."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def sanitize_device(value: Optional[str]) -> str:
    """A safe single folder name: letters, digits, dot, dash, underscore."""
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip()).strip(".-_")[:64].strip(".-_")
    if not text:
        return "unknown"
    if text.split(".")[0].lower() in _WINDOWS_RESERVED:
        text = "_" + text
    return text


def looks_like_zip(head: bytes) -> bool:
    return head.startswith(_ZIP_MAGIC)


def _entry(path: Path, device: str) -> dict:
    stat = path.stat()
    return {
        "id": path.stem,
        "device": device,
        "name": path.name,
        "size": stat.st_size,
        "received_at": stat.st_mtime,
        "url": f"/v1/client-logs/{device}/{path.name}",
    }


class ClientLogStore:
    def __init__(self, root):
        self.dir = Path(root) / "client-logs"

    def save(self, device: Optional[str], source: BinaryIO) -> dict:
        """Validate and store one zip read from ``source`` (a seekable binary file)."""
        device = sanitize_device(device)
        source.seek(0, os.SEEK_END)
        size = source.tell()
        source.seek(0)
        if size <= 0:
            raise ClientLogError(400, "the uploaded file is empty")
        if size > MAX_BYTES:
            raise ClientLogError(413, f"log bundle exceeds the {MAX_BYTES // (1024 * 1024)} MB limit")
        if not looks_like_zip(source.read(4)):
            raise ClientLogError(400, "the uploaded file is not a zip archive")
        source.seek(0)
        if not zipfile.is_zipfile(source):
            raise ClientLogError(400, "the uploaded file is not a valid zip archive")
        source.seek(0)

        now = datetime.now(timezone.utc)
        short = uuid.uuid4().hex[:8]
        name = f"{now.strftime('%Y%m%dT%H%M%SZ')}-{short}.zip"
        folder = self.dir / device
        folder.mkdir(parents=True, exist_ok=True)
        final = folder / name
        tmp = folder / f".{short}.part"
        try:
            with open(tmp, "wb") as out:
                shutil.copyfileobj(source, out, 1 << 20)
            os.replace(tmp, final)
        finally:
            tmp.unlink(missing_ok=True)
        self._prune(folder)
        return {
            "id": final.stem,
            "device": device,
            "size": size,
            "received_at": now.timestamp(),
            "name": name,
        }

    def _prune(self, folder: Path) -> None:
        files = sorted((p for p in folder.glob("*.zip") if _NAME_OK.match(p.name)), key=lambda p: p.name)
        for old in files[: max(0, len(files) - KEEP_PER_DEVICE)]:
            try:
                old.unlink()
            except OSError:
                pass

    def list(self) -> List[dict]:
        """Every stored bundle, newest first."""
        items: List[dict] = []
        if not self.dir.is_dir():
            return items
        for folder in self.dir.iterdir():
            if not folder.is_dir() or not _DEVICE_OK.match(folder.name):
                continue
            for path in folder.glob("*.zip"):
                if _NAME_OK.match(path.name):
                    try:
                        items.append(_entry(path, folder.name))
                    except OSError:
                        continue
        items.sort(key=lambda item: (item["name"][:16], item["received_at"]), reverse=True)
        return items

    def resolve(self, device: str, name: str) -> Optional[Path]:
        """The stored file for ``device``/``name``, or None (never escapes the store)."""
        if not _DEVICE_OK.match(device or "") or not _NAME_OK.match(name or ""):
            return None
        path = self.dir / device / name
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(self.dir.resolve())
        except (OSError, ValueError):
            return None
        return resolved if resolved.is_file() else None

    def delete(self, device: str, name: str) -> bool:
        """Remove one stored bundle. False when there is no such bundle."""
        path = self.resolve(device, name)
        if path is None:
            return False
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        try:
            path.parent.rmdir()  # only succeeds once the computer has no bundles left
        except OSError:
            pass
        return True

    def delete_all(self) -> int:
        """Remove every stored bundle; returns how many were removed."""
        count = 0
        for item in self.list():
            if self.delete(item["device"], item["name"]):
                count += 1
        return count
