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


def test_failed_smoke_test_exits_without_a_dialog(monkeypatch):
    """Nobody can close a dialog in the smoke test: showing one hung the 0.8.5 build for hours."""
    seen = {}

    def boom(argv=None):
        raise RuntimeError("the Opus encoder is not available")

    monkeypatch.setattr(app, "main", boom)
    monkeypatch.setattr(app, "_report_startup_error", lambda exc, show_dialog=True: seen.setdefault("dialog", show_dialog))

    assert app.run(["meeting-notes-ui", "--smoke-test"]) == 1
    assert seen["dialog"] is False


def test_opus_self_test_names_the_import_error(monkeypatch):
    import builtins

    from meeting_notes.client import compress

    real_import = builtins.__import__

    def no_av(name, *args, **kwargs):
        if name == "av":
            raise ImportError("No module named 'av.sidedata.encparams'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_av)
    import pytest

    with pytest.raises(RuntimeError, match="encparams"):
        compress.self_test()
