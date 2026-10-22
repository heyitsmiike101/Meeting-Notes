"""The suite must never see the real user profile (config, recordings, GPU runtime)."""

import os
import tempfile
from pathlib import Path


def test_home_and_app_data_are_a_throwaway_folder():
    from meeting_notes import config as config_mod
    from meeting_notes.client import gpu_runtime

    temp = Path(tempfile.gettempdir()).resolve()
    for path in (Path.home(), config_mod.DEFAULT_SAVE_DIR, config_mod.DEFAULT_CONFIG_PATH, gpu_runtime.runtime_root()):
        assert temp in Path(path).resolve().parents, path
    assert os.environ.get("MEETING_NOTES_NO_BACKGROUND_COMPRESS") == "1"
