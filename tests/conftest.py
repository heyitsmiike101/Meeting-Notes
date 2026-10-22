"""Shared test setup.

Qt must be told to use the offscreen platform before any QApplication exists,
otherwise importing the UI on a headless machine (CI, a container, a server)
fails on a missing display rather than skipping.
"""

from __future__ import annotations

import os
import tempfile

# Every test runs with a throwaway home and app-data folders, set before any meeting_notes import: the client
# works out ~/.meeting-notes (its config) and ~/Meeting Notes (the recordings) when it loads, and a test window
# that skipped the per-test fixtures once compressed the developer's real recordings and could as easily have
# run retention on them or written the real config. %LOCALAPPDATA% holds the GPU runtime.
def _keep_ffmpeg_on_path() -> None:
    """Tools found through the real profile (ffmpeg from WinGet lives under %LOCALAPPDATA%) stay reachable: put
    their folder on PATH before the profile is swapped out, or the ffmpeg-backed tests would quietly skip."""
    import glob
    import shutil

    if shutil.which("ffmpeg"):
        return
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return
    found = sorted(glob.glob(os.path.join(local, "Microsoft", "WinGet", "Packages", "*", "**", "ffmpeg.exe"),
                             recursive=True))
    if found:
        os.environ["PATH"] = os.path.dirname(found[0]) + os.pathsep + os.environ.get("PATH", "")


_keep_ffmpeg_on_path()
_SANDBOX = tempfile.mkdtemp(prefix="meeting-notes-test-home-")
for _var, _sub in (("USERPROFILE", ""), ("HOME", ""), ("APPDATA", "AppData/Roaming"), ("LOCALAPPDATA", "AppData/Local")):
    _path = os.path.join(_SANDBOX, _sub) if _sub else _SANDBOX
    os.makedirs(_path, exist_ok=True)
    os.environ[_var] = _path
# The background compression pass never runs in a test window (the compress tests call it directly).
os.environ.setdefault("MEETING_NOTES_NO_BACKGROUND_COMPRESS", "1")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# Never let a test window poll the real registry/windows for a live call.
os.environ.setdefault("MEETING_NOTES_NO_DETECT", "1")
# Windows must not poll the machine's real audio devices in the background.
os.environ.setdefault("MEETING_NOTES_NO_DEVICE_WATCH", "1")
# Windows must not open the remote-control websocket to any server.
os.environ.setdefault("MEETING_NOTES_NO_REMOTE", "1")
# Windows must not open Settings on their own for a missing server password (the tests that cover it unset this).
os.environ.setdefault("MEETING_NOTES_NO_FIRST_RUN_PROMPT", "1")
# No test may ever make macOS show a real Microphone / Screen Recording prompt (the tests that cover the
# first-run prompts unset this and fake the calls).
os.environ.setdefault("MEETING_NOTES_NO_PERMISSION_PROMPT", "1")

# The GPU helper never starts a runtime, shows a tray icon or touches the Run key under test.
os.environ.setdefault("MEETING_NOTES_NO_GPU_HELPER", "1")
# Each test run gets its own single-instance socket.
os.environ.setdefault("MEETING_NOTES_INSTANCE_SUFFIX", "tests")


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_version_gate():
    """The "server says this client is too old" state is process-wide."""
    from meeting_notes.client import version_gate

    version_gate.clear()
    yield
    version_gate.clear()
