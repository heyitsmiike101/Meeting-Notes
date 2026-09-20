"""Persisted device preferences, so you do not re-pick devices every meeting."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

DEFAULT_CONFIG_PATH = Path.home() / ".meeting-notes" / "config.json"


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> Dict[str, Any]:
    path = Path(path)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # A corrupt config should never stop a meeting from being recorded.
        return {}


def save_config(data: Dict[str, Any], path: Path = DEFAULT_CONFIG_PATH) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path
