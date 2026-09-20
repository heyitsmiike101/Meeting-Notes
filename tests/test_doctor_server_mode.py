"""``doctor.py``'s "transcription backend" check, in server mode.

A separate file from test_doctor_and_upload.py (which already owns
TestCheckServer/TestCheckUploadQueue) purely to avoid two agents editing the
same file concurrently -- see the task notes for this change. Hardware-free
and network-free: this only ever exercises _check_transcription(), which
never opens a device or a socket.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from meeting_notes import config as config_mod
from meeting_notes import doctor
from meeting_notes.transcribe import faster_whisper_backend


@pytest.fixture()
def isolated_config(tmp_path, monkeypatch):
    """Point MEETING_NOTES_CONFIG at a private temp file, mirroring the
    fixture of the same name in test_doctor_and_upload.py, so this can never
    touch a real ~/.meeting-notes/config.json."""
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("MEETING_NOTES_CONFIG", str(config_path))
    save_dir = tmp_path / "recordings"
    save_dir.mkdir()
    return config_path, save_dir


def _write_config(config_path: Path, save_dir: Path, server=None) -> None:
    data = {"save_dir": str(save_dir)}
    if server is not None:
        data["server"] = server
    config_mod.save_config(data, path=config_path)


@pytest.fixture()
def no_model_cached(monkeypatch):
    """faster-whisper is installed (it really is, in this venv) but nothing
    is cached -- the state that used to always WARN regardless of server
    configuration."""
    monkeypatch.setattr(faster_whisper_backend, "model_is_downloaded", lambda *a, **kw: False)


def test_no_server_and_no_cache_still_warns(isolated_config, no_model_cached):
    """The pre-existing behaviour must survive: with no server configured,
    a missing local model is genuinely something to act on before the next
    offline meeting."""
    config_path, save_dir = isolated_config
    _write_config(config_path, save_dir, server=None)

    check = doctor._check_transcription()
    assert check.ok is False
    assert check.fix  # WARN, not FAIL/OK
    assert "no model is cached" in check.detail


def test_server_configured_and_no_cache_is_ok_not_a_warning(isolated_config, no_model_cached):
    """The fix: when a server does the final pass, a bare local machine with
    no cached model is expected, not a problem -- doctor must say so instead
    of warning about something that will never actually be needed."""
    config_path, save_dir = isolated_config
    server_url = "http://192.168.1.50:8000"
    _write_config(config_path, save_dir, server={"url": server_url, "token": ""})

    check = doctor._check_transcription()
    assert check.ok is True
    assert not check.fix
    assert server_url in check.detail
    assert "meeting-notes transcribe" in check.detail


def test_server_configured_but_model_already_cached_reports_cached(
    isolated_config, monkeypatch
):
    """A server being configured must not hide that a model IS cached --
    the ordinary "ready" report still wins when it's true."""
    config_path, save_dir = isolated_config
    _write_config(config_path, save_dir, server={"url": "http://example.invalid", "token": ""})
    monkeypatch.setattr(
        faster_whisper_backend, "model_is_downloaded", lambda m, *a, **kw: m == "base.en"
    )

    check = doctor._check_transcription()
    assert check.ok is True
    assert "base.en" in check.detail


def test_corrupt_config_falls_back_to_the_no_server_warning(isolated_config, no_model_cached):
    """A corrupt config must not crash this check -- _check_server already
    reports that problem on its own; this one just shouldn't blow up trying
    to read the same file."""
    config_path, save_dir = isolated_config
    config_path.write_text("{not valid json", encoding="utf-8")

    check = doctor._check_transcription()  # must not raise
    assert isinstance(check, doctor.Check)
    assert check.ok is False
