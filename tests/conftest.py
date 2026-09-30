"""Shared test setup.

Qt must be told to use the offscreen platform before any QApplication exists,
otherwise importing the UI on a headless machine (CI, a container, a server)
fails on a missing display rather than skipping.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# Never let a test window poll the real registry/windows for a live call.
os.environ.setdefault("MEETING_NOTES_NO_DETECT", "1")
# Windows must not poll the machine's real audio devices in the background.
os.environ.setdefault("MEETING_NOTES_NO_DEVICE_WATCH", "1")


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_version_gate():
    """The "server says this client is too old" state is process-wide."""
    from meeting_notes.client import version_gate

    version_gate.clear()
    yield
    version_gate.clear()
