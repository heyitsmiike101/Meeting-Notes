"""Shared test setup.

Qt must be told to use the offscreen platform before any QApplication exists,
otherwise importing the UI on a headless machine (CI, a container, a server)
fails on a missing display rather than skipping.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
