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
