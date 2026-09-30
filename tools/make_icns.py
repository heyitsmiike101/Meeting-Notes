"""Build ``MeetingNotes.icns`` for the macOS app bundle (run on a Mac).

The Windows icon is procedural (``tools/make_icon.py``), so instead of scaling
the 256 px ``.ico`` up we render the same artwork at every size an ``.iconset``
wants and let ``iconutil`` (ships with macOS) assemble the ``.icns``.

    python tools/make_icns.py build/MeetingNotes.icns
"""

from __future__ import annotations

import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_icon import _pixel  # noqa: E402

# (file name in the iconset, pixel size)
ICONSET = (
    ("icon_16x16.png", 16),
    ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32),
    ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128),
    ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256),
    ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512),
    ("icon_512x512@2x.png", 1024),
)


def _png(size: int) -> bytes:
    raw = bytearray()
    for y in range(size):
        raw.append(0)  # filter: none
        for x in range(size):
            raw += bytes(_pixel(size, x, y))

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def make_icns(target: Path) -> None:
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "MeetingNotes.iconset"
        iconset.mkdir()
        cache: dict = {}
        for name, size in ICONSET:
            if size not in cache:
                cache[size] = _png(size)
            (iconset / name).write_bytes(cache[size])
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(target)], check=True)


if __name__ == "__main__":
    make_icns(Path(sys.argv[1] if len(sys.argv) > 1 else "build/MeetingNotes.icns"))
