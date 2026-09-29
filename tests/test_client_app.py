from meeting_notes.client import app


def test_startup_error_is_written_to_standard_user_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(app.Path, "home", classmethod(lambda cls: tmp_path))

    path = app._report_startup_error(RuntimeError("missing bundled dependency"), show_dialog=False)

    assert path == tmp_path / ".meeting-notes" / "client-startup-error.log"
    text = path.read_text(encoding="utf-8")
    assert "Meeting Notes could not start" in text
    assert "RuntimeError: missing bundled dependency" in text


def test_smoke_test_initializes_qt_without_starting_client(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")

    assert app.main(["meeting-notes-ui", "--smoke-test"]) == 0
