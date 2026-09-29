"""The generated Windows installer/uninstaller must never delete recordings."""

from __future__ import annotations

import pytest

from meeting_notes.server import web


@pytest.fixture(params=["installer", "uninstaller"])
def script(request):
    if request.param == "installer":
        return request.param, web.render_client_installer("http://meeting.lan")
    return request.param, web.render_client_uninstaller()


def test_guard_is_present_and_reads_the_saved_recordings_folder(script):
    kind, text = script
    assert "__RECORDINGS_GUARD__" not in text and "__ACTION__" not in text
    assert "function Assert-RecordingsAreSafe" in text
    assert "function Test-PathInside" in text
    assert 'ReadAllText($configFile, [Text.Encoding]::UTF8)' in text
    assert "$cfg.save_dir" in text
    assert '".meeting-notes"' in text and '"Meeting Notes"' in text
    assert "OrdinalIgnoreCase" in text and "TrimEnd('\\', '/')" in text
    assert "Your recordings folder is inside the app folder" in text
    assert "use Move recordings, or change the folder in Settings" in text
    assert f"run the {kind} again. Nothing was changed." in text
    assert '-Filter "*.wav"' in text and '-Filter ".upload-queue"' in text


def test_guard_runs_before_any_process_is_stopped_or_file_touched(script):
    kind, text = script
    guard_call = text.index("$recordingsDir = Assert-RecordingsAreSafe")
    if kind == "installer":
        first_side_effect = min(
            text.index("Stop-Process"), text.index("Invoke-WebRequest"), text.index("Rename-Item"),
            text.index("Remove-Item -LiteralPath $staging"),
        )
        assert guard_call < text.index("\ntry {") < first_side_effect
    else:
        assert guard_call < text.index("Stop-Process") < text.index("Remove-Item -LiteralPath $installDir")


def test_uninstaller_never_deletes_a_recordings_folder_inside_settings():
    text = web.render_client_uninstaller()
    assert "Test-PathInside $recordingsDir $settingsDir" in text
    assert text.index("Test-PathInside $recordingsDir $settingsDir") < text.index(
        "Remove-Item -LiteralPath $settingsDir -Recurse -Force"
    )
    assert "Client settings were kept because your recordings folder is inside" in text


def test_installer_still_swaps_by_rename_and_uses_the_manifest():
    text = web.render_client_installer("http://meeting.lan")
    assert 'Rename-Item -LiteralPath $installDir -NewName' in text
    assert '"http://meeting.lan/install/client-manifest.json"' in text
