"""Which client version is actually published for each platform.

Normally the Windows and macOS packages are built for the server's own ``__version__`` and published with it.
When a release ships without a new macOS build (the Mac that builds it isn't around), the old macOS package
stays published; announcing the server's version for it would make every Mac recorder install the same old
package again and again. So the macOS manifest, and the Recorders page's "behind" badge for Mac recorders,
use the version written in the published app's ``Info.plist`` instead.
"""

from __future__ import annotations

import plistlib
import threading
import zipfile
from pathlib import Path
from typing import Dict, Optional, Tuple

from .. import __version__

MAC_PACKAGE = "MeetingNotes-macOS.zip"
_PLIST_SUFFIX = ".app/Contents/Info.plist"

_lock = threading.Lock()
_cache: Dict[str, Tuple[Tuple[float, int], Optional[str]]] = {}
_root: Optional[Path] = None


def configure(root) -> None:
    """Where the published packages live (``<data>/client``'s parent); set by ``create_app``."""
    global _root
    _root = Path(root) if root else None


def mac_package_version(package: Path) -> Optional[str]:
    """``CFBundleShortVersionString`` of the app inside the macOS zip, or None (cached per file)."""
    try:
        stat = package.stat()
    except OSError:
        return None
    key = (stat.st_mtime, stat.st_size)
    with _lock:
        hit = _cache.get(str(package))
        if hit and hit[0] == key:
            return hit[1]
    version = None
    try:
        with zipfile.ZipFile(package) as zf:
            names = [n for n in zf.namelist()
                     if n.endswith(_PLIST_SUFFIX) and "/._" not in n and n.count("/") == 2]
            if names:
                data = plistlib.loads(zf.read(names[0]))
                value = data.get("CFBundleShortVersionString") or data.get("CFBundleVersion")
                version = str(value).strip() or None if value else None
    except (OSError, zipfile.BadZipFile, plistlib.InvalidFileException, ValueError, KeyError):
        version = None
    with _lock:
        _cache[str(package)] = (key, version)
    return version


def published_version(platform: str, root=None) -> str:
    """The client version a recorder on ``platform`` ("windows", "macos", ...) can update to."""
    base = Path(root) if root else _root
    if platform == "macos" and base is not None:
        found = mac_package_version(base / "client" / MAC_PACKAGE)
        if found:
            return found
    return __version__
