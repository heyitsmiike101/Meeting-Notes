"""Persisted device preferences, so you do not re-pick devices every meeting."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_CONFIG_PATH = Path.home() / ".meeting-notes" / "config.json"

# Where recordings go by default. A visible folder in the home directory, not a
# hidden app-support path: these are the user's meetings, and they will want to
# find, play and delete them without us.
DEFAULT_SAVE_DIR = Path.home() / "Meeting Notes"


def config_path(path: Optional[Path] = None) -> Path:
    """Resolve the config location at call time, not at import time.

    A default argument of ``DEFAULT_CONFIG_PATH`` would be captured when the
    module is first imported, so nothing could redirect it afterwards -- not a
    test, and not the MEETING_NOTES_CONFIG override that lets one machine keep
    separate profiles.
    """
    if path is not None:
        return Path(path)
    override = os.environ.get("MEETING_NOTES_CONFIG")
    return Path(override) if override else DEFAULT_CONFIG_PATH


def load_config(path: Optional[Path] = None) -> Dict[str, Any]:
    path = config_path(path)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # A corrupt config should never stop a meeting from being recorded.
        return {}


def save_config(data: Dict[str, Any], path: Optional[Path] = None) -> Path:
    path = config_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def save_dir(data: Optional[Dict[str, Any]] = None) -> Path:
    """The folder recordings are written to, expanded and created on demand."""
    data = load_config() if data is None else data
    raw = data.get("save_dir") or str(DEFAULT_SAVE_DIR)
    return Path(raw).expanduser()


def server_settings(data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Connection details for the transcription server on the LAN."""
    data = load_config() if data is None else data
    server = dict(data.get("server") or {})
    server.setdefault("url", "")
    server.setdefault("token", "")
    # Live preview is a convenience; the authoritative transcript always comes
    # from uploading the complete local recording afterwards.
    server.setdefault("live_preview", True)
    server.setdefault("auto_upload", True)
    # Update checks are harmless read-only requests and are enabled by
    # default. Applying an update can restart the desktop process, so that is
    # an explicit opt-in and remains disabled for existing installations.
    server.setdefault("check_updates", True)
    server.setdefault("auto_update", False)
    return server


def meeting_detection_settings(data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Settings for spotting Teams/Zoom/Meet calls and offering to record them."""
    data = load_config() if data is None else data
    raw = data.get("meeting_detection")
    settings = dict(raw) if isinstance(raw, dict) else {}
    settings.setdefault("enabled", True)
    settings.setdefault("auto_stop", True)
    settings.setdefault("end_grace_sec", 20)
    try:
        grace = float(settings["end_grace_sec"])
    except (TypeError, ValueError):
        grace = 20.0
    grace = min(300.0, max(5.0, grace))
    settings["end_grace_sec"] = int(grace) if grace == int(grace) else grace
    settings["enabled"] = bool(settings["enabled"])
    settings["auto_stop"] = bool(settings["auto_stop"])
    return settings
