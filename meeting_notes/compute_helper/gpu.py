"""GPU facts via ``nvidia-smi`` (no torch needed)."""

from __future__ import annotations

import shutil
import subprocess
from typing import Optional

_QUERY = "--query-gpu=name,memory.total,driver_version"


def _find_smi() -> Optional[str]:
    found = shutil.which("nvidia-smi")
    if found:
        return found
    import os

    candidate = os.path.join(
        os.environ.get("SystemRoot", r"C:\Windows"), "System32", "nvidia-smi.exe"
    )
    return candidate if os.path.exists(candidate) else None


def parse_smi(text: str) -> Optional[dict]:
    """First GPU from ``--format=csv,noheader,nounits`` output, or None."""
    for line in (text or "").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3 or not parts[0]:
            continue
        try:
            vram = int(float(parts[1]))
        except ValueError:
            vram = 0
        return {"name": parts[0], "vram_mb": vram, "driver": parts[2]}
    return None


def query_gpu() -> Optional[dict]:
    """``{name, vram_mb, driver}`` for the first NVIDIA GPU, or None when there is none or the tool is missing."""
    smi = _find_smi()
    if not smi:
        return None
    try:
        completed = subprocess.run(
            [smi, _QUERY, "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return parse_smi(completed.stdout)
