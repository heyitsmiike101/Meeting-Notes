"""Generate the small Meeting Notes Windows icon without third-party tools.

The output is a multi-size ICO suitable for Nuitka's Windows resource and
Windows Shell shortcut icons.  Keeping the source procedural makes release
builds reproducible on the offline Windows build host.
"""

from __future__ import annotations

import struct
from pathlib import Path


def _pixel(size: int, x: int, y: int) -> tuple[int, int, int, int]:
    # Navy app tile with a cyan microphone and a white stand/wave mark.
    cx = (size - 1) / 2
    cy = (size - 1) / 2
    r = size * 0.45
    if (x - cx) ** 2 + (y - cy) ** 2 > r * r:
        return 0, 0, 0, 0
    sx, sy = x / size, y / size
    # Microphone capsule.
    mic = 0.32 < sx < 0.68 and 0.19 < sy < 0.64
    mic = mic and ((sx - 0.50) ** 2 / 0.18**2 + (sy - 0.415) ** 2 / 0.28**2 <= 1)
    # U-shaped pickup support and short stem/base.
    support = 0.22 < sx < 0.78 and 0.43 < sy < 0.73 and (
        abs(((sx - 0.50) / 0.29) ** 2 + ((sy - 0.55) / 0.25) ** 2 - 1) < 0.16
    )
    stem = 0.47 < sx < 0.53 and 0.70 < sy < 0.84
    base = 0.36 < sx < 0.64 and 0.82 < sy < 0.87
    if mic or support or stem or base:
        return 245, 252, 255, 255
    return 18, 38, 67, 255


def make_icon(path: Path) -> None:
    sizes = (16, 32, 48, 256)
    images: list[bytes] = []
    for size in sizes:
        # ICO DIB stores rows bottom-to-top and doubles the height for the mask.
        pixels = bytearray()
        for y in range(size - 1, -1, -1):
            for x in range(size):
                r, g, b, a = _pixel(size, x, y)
                pixels += bytes((b, g, r, a))
        mask_row = (size + 31) // 32 * 4
        mask = bytes(mask_row * size)
        images.append(struct.pack("<IIIHHIIIIII", 40, size, size * 2, 1, 32, 0,
                                  len(pixels) + len(mask), 0, 0, 0, 0) + pixels + mask)
    offset = 6 + 16 * len(sizes)
    out = bytearray(struct.pack("<HHH", 0, 1, len(sizes)))
    for size, image in zip(sizes, images):
        dimension = size if size < 256 else 0
        out += struct.pack(
            "<BBBBHHII",
            dimension,
            dimension,
            0,
            0,
            1,
            32,
            len(image),
            offset,
        )
        offset += len(image)
    for image in images:
        out += image
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(out)


if __name__ == "__main__":
    make_icon(Path(__file__).resolve().parents[1] / "assets" / "meeting-notes.ico")
