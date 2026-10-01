"""The client's Upload dialog: audio recording, transcript file, pasted transcript; and the call to the server."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6", reason="PySide6 not installed")

from PySide6.QtCore import QDateTime  # noqa: E402
from PySide6.QtWidgets import QApplication, QFileDialog  # noqa: E402

from meeting_notes import config as config_mod  # noqa: E402
from meeting_notes.client import authcheck  # noqa: E402
from meeting_notes.client import api as api_mod  # noqa: E402
from meeting_notes.client.ui import upload_dialog as ud  # noqa: E402
from meeting_notes.client.ui.theme import APP_STYLE  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(APP_STYLE)
    return app


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_NOTES_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("MEETING_NOTES_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("MEETING_NOTES_NO_DETECT", "1")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    config_mod.save_config({"save_dir": str(tmp_path / "rec"), "server": {"url": "http://meeting.lan", "token": "t"}})
    return tmp_path


def _pump(condition, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        QApplication.processEvents()
        time.sleep(0.01)
    return condition()


# -- the dialog ------------------------------------------------------------------------------------------


def test_dialog_offers_three_clear_choices_and_starts_on_audio(qt_app, home):
    dialog = ud.UploadDialog()
    assert [b.text() for b in dialog.mode_group.buttons()] == ["Audio recording", "Transcript file", "Paste a transcript"]
    assert dialog.mode() == ud.AUDIO
    assert not dialog.upload_button.isEnabled()  # nothing chosen yet
    # Audio: a file, no date/time (the server records the upload time).
    assert dialog.when_edit.isHidden() and dialog.text_edit.isHidden()
    dialog.set_mode(ud.TRANSCRIPT_PASTE)
    assert dialog.mode() == ud.TRANSCRIPT_PASTE
    assert not dialog.upload_button.isEnabled()
    dialog.text_edit.setPlainText("Jane: hello")
    assert dialog.upload_button.isEnabled()
    dialog.close()


def test_audio_flow_builds_an_audio_request(qt_app, home):
    audio = home / "standup.m4a"
    audio.write_bytes(b"x")
    dialog = ud.UploadDialog()
    dialog.choose_file(audio)
    assert dialog.upload_button.isEnabled()
    dialog.name_edit.setText("  Standup  ")
    dialog.accept()
    request = dialog.request
    assert (request.kind, request.path, request.name) == ("audio", audio, "Standup")
    assert request.label == "standup.m4a"


def test_transcript_file_flow_reads_text_defaults_name_and_date_from_the_file(qt_app, home):
    transcript = home / "Q3 planning.vtt"
    transcript.write_text("WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n<v Ana>Hi</v>\n", encoding="utf-8")
    modified = 1_790_605_800  # 2026-09-28T14:30:00Z
    os.utime(transcript, (modified, modified))
    dialog = ud.UploadDialog()
    dialog.set_mode(ud.TRANSCRIPT_FILE)
    dialog.choose_file(transcript)
    assert dialog.when_edit.dateTime().toSecsSinceEpoch() == modified  # defaults to the file's date
    dialog.accept()
    request = dialog.request
    assert request.kind == "transcript" and request.source == "file"
    assert request.filename == "Q3 planning.vtt" and request.name == "Q3 planning"
    assert request.started_at == modified
    assert "<v Ana>Hi</v>" in request.text


def test_choosing_the_other_kind_of_file_switches_the_choice(qt_app, home):
    audio, transcript = home / "call.mp3", home / "call.txt"
    audio.write_bytes(b"x")
    transcript.write_text("Jane: hi", encoding="utf-8")
    dialog = ud.UploadDialog()
    dialog.choose_file(transcript)  # on the Audio choice
    assert dialog.mode() == ud.TRANSCRIPT_FILE and dialog.upload_button.isEnabled()
    dialog.choose_file(audio)
    assert dialog.mode() == ud.AUDIO
    dialog.close()


def test_a_file_chosen_for_one_kind_does_not_carry_over_to_the_other(qt_app, home):
    transcript = home / "call.txt"
    transcript.write_text("Jane: hi", encoding="utf-8")
    dialog = ud.UploadDialog()
    dialog.choose_file(transcript)
    dialog.set_mode(ud.AUDIO)
    assert dialog.file_edit.text() == "" and not dialog.upload_button.isEnabled()
    dialog.close()


def test_pasted_flow_uses_the_chosen_date_and_a_default_name(qt_app, home):
    dialog = ud.UploadDialog()
    dialog.set_mode(ud.TRANSCRIPT_PASTE)
    dialog.text_edit.setPlainText("[00:00:05] Jane: Hello\n[00:00:12] Bob: Hi")
    when = QDateTime.fromSecsSinceEpoch(1_790_605_800)
    dialog.when_edit.setDateTime(when)
    dialog.accept()
    request = dialog.request
    assert request.kind == "transcript" and request.source == "pasted" and request.filename == ""
    assert request.started_at == 1_790_605_800
    assert request.name.startswith("Pasted transcript ")
    assert request.text.startswith("[00:00:05] Jane")


def test_a_pasted_transcript_can_be_named(qt_app, home):
    dialog = ud.UploadDialog()
    dialog.set_mode(ud.TRANSCRIPT_PASTE)
    dialog.text_edit.setPlainText("Jane: hello")
    dialog.name_edit.setText("Board call")
    dialog.accept()
    assert dialog.request.name == "Board call"


def test_default_date_for_pasted_text_is_now(qt_app, home):
    before = time.time()
    dialog = ud.UploadDialog()
    dialog.set_mode(ud.TRANSCRIPT_PASTE)
    dialog.text_edit.setPlainText("Jane: hello")
    dialog.accept()
    assert before - 120 <= dialog.request.started_at <= time.time() + 120


def test_oversized_and_empty_transcript_files_show_an_inline_error_and_keep_the_dialog_open(qt_app, home):
    big = home / "big.txt"
    big.write_bytes(b"a" * (ud.MAX_TRANSCRIPT_BYTES + 1))
    empty = home / "empty.txt"
    empty.write_text("  \n", encoding="utf-8")
    dialog = ud.UploadDialog()
    dialog.set_mode(ud.TRANSCRIPT_FILE)
    dialog.choose_file(big)
    dialog.accept()
    assert dialog.request is None and not dialog.error_label.isHidden()
    assert "2 MB" in dialog.error_label.text()
    dialog.choose_file(empty)
    dialog.accept()
    assert dialog.request is None and "empty" in dialog.error_label.text()
    dialog.close()


def test_read_transcript_file_handles_utf8_bom_utf16_and_legacy_encodings(tmp_path):
    (tmp_path / "a.txt").write_bytes("﻿Jane: café".encode("utf-8"))
    assert ud.read_transcript_file(tmp_path / "a.txt") == "Jane: café"
    (tmp_path / "b.txt").write_bytes("Jane: café".encode("utf-16"))
    assert ud.read_transcript_file(tmp_path / "b.txt") == "Jane: café"
    (tmp_path / "c.txt").write_bytes("Jane: café".encode("cp1252"))
    assert ud.read_transcript_file(tmp_path / "c.txt") == "Jane: café"
    with pytest.raises(ValueError, match="Could not open"):
        ud.read_transcript_file(tmp_path / "missing.txt")


def test_browse_button_uses_the_filter_for_the_chosen_kind(qt_app, home, monkeypatch):
    seen = []
    picked = home / "t.srt"
    picked.write_text("1\n00:00:01,000 --> 00:00:02,000\nhi\n", encoding="utf-8")

    def fake_open(parent, title, start, filters):
        seen.append((title, filters))
        return str(picked), ""

    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(fake_open))
    dialog = ud.UploadDialog(start_dir=str(home))
    dialog.set_mode(ud.TRANSCRIPT_FILE)
    dialog.browse_button.click()
    assert "*.vtt" in seen[0][1] and "*.srt" in seen[0][1] and "*.wav" not in seen[0][1]
    assert dialog.file_edit.text() == str(picked)
    dialog.set_mode(ud.AUDIO)
    dialog.browse_button.click()
    assert "*.wav" in seen[1][1]


# -- the server call, against a fake server --------------------------------------------------------------


def _fake_server(monkeypatch, handler):
    class Fake(api_mod.ServerClient):
        def __init__(self, base_url, token=None, timeout=10.0):
            super().__init__(base_url, token, timeout)
            self._client = httpx.Client(base_url=self.base_url, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(api_mod, "ServerClient", Fake)


def test_upload_transcript_posts_json_to_the_new_endpoint(monkeypatch):
    seen = {}

    def handler(request):
        seen["path"], seen["method"] = request.url.path, request.method
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(201, json={"session_id": "abc", "state": "done"})

    _fake_server(monkeypatch, handler)
    with api_mod.ServerClient("http://meeting.lan", "tok") as client:
        result = client.upload_transcript("Jane: hi", name="Call", started_at=1790605800.7, source="file", filename="c.txt")
    assert result["session_id"] == "abc"
    assert (seen["method"], seen["path"]) == ("POST", "/v1/sessions/transcript")
    assert seen["body"] == {"text": "Jane: hi", "source": "file", "name": "Call", "started_at": 1790605800, "filename": "c.txt"}
    assert seen["auth"] == "Bearer tok"


# -- the main window flow ------------------------------------------------------------------------------


def _window(qt_app, monkeypatch):
    from meeting_notes.audio import devices as devices_mod
    from meeting_notes.client.ui.main_window import MainWindow

    def _raise(kind, requested=None, samplerate=None):
        raise devices_mod.DeviceNotFound("no device (simulated)")

    monkeypatch.setattr(devices_mod, "resolve_source", _raise)
    window = MainWindow()
    window._timer.stop()
    window._auth_timer.stop()
    window._auth_checker = lambda url, token: authcheck.CheckResult(authcheck.NO_SERVER)
    return window


def _close(window):
    window.controller.stop_uploader()
    window.close()


def _fake_dialog(monkeypatch, request):
    from meeting_notes.client.ui import main_window as mw

    class Fake:
        def __init__(self, parent=None, start_dir=""):
            self.request = request

        def exec(self):
            return 1

    monkeypatch.setattr(mw, "UploadDialog", Fake)


def test_pasted_transcript_goes_through_the_window_to_the_server(qt_app, home, monkeypatch):
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"session_id": "s1", "state": "done", "notes": "queued"})

    _fake_server(monkeypatch, handler)
    request = ud.UploadRequest(kind="transcript", name="Board call", text="Jane: hi", started_at=1790605800, source="pasted")
    _fake_dialog(monkeypatch, request)
    window = _window(qt_app, monkeypatch)
    try:
        window.upload_button.click()
        assert _pump(lambda: window.upload_button.isEnabled() and "Added the transcript" in window.status_label.text())
        assert seen["path"] == "/v1/sessions/transcript"
        assert seen["body"]["name"] == "Board call" and seen["body"]["source"] == "pasted"
        assert "Board call" in window.status_label.text() and "Notes are being generated" in window.status_label.text()
    finally:
        _close(window)


def test_transcript_file_goes_through_the_window_with_its_filename(qt_app, home, monkeypatch):
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"session_id": "s2", "state": "done", "notes": None})

    _fake_server(monkeypatch, handler)
    request = ud.UploadRequest(
        kind="transcript", name="Q3", text="WEBVTT\n", started_at=1790605800, source="file", filename="Q3.vtt"
    )
    _fake_dialog(monkeypatch, request)
    window = _window(qt_app, monkeypatch)
    try:
        window._open_recording_upload()
        assert _pump(lambda: "Added the transcript" in window.status_label.text())
        assert seen["body"]["source"] == "file" and seen["body"]["filename"] == "Q3.vtt"
    finally:
        _close(window)


def test_audio_still_uploads_as_a_recording_with_its_name(qt_app, home, monkeypatch):
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["body"] = request.content
        return httpx.Response(200, json={"session_id": "s3", "job_id": "j9"})

    _fake_server(monkeypatch, handler)
    audio = home / "call.wav"
    audio.write_bytes(b"RIFFxxxx")
    _fake_dialog(monkeypatch, ud.UploadRequest(kind="audio", name="Call", path=audio))
    window = _window(qt_app, monkeypatch)
    try:
        window._open_recording_upload()
        assert _pump(lambda: "Uploaded call.wav" in window.status_label.text())
        assert seen["path"] == "/v1/uploads" and b'name="name"' in seen["body"] and b"Call" in seen["body"]
        assert "job j9" in window.status_label.text()
    finally:
        _close(window)


def test_an_old_server_gets_a_clear_message(qt_app, home, monkeypatch):
    _fake_server(monkeypatch, lambda request: httpx.Response(404, json={"detail": "Not Found"}))
    _fake_dialog(monkeypatch, ud.UploadRequest(kind="transcript", name="X", text="hi", source="pasted"))
    window = _window(qt_app, monkeypatch)
    try:
        window._open_recording_upload()
        assert _pump(lambda: "Could not upload" in window.status_label.text())
        assert "update the server" in window.status_label.text()
    finally:
        _close(window)


def test_a_server_refusal_shows_the_servers_reason(qt_app, home, monkeypatch):
    _fake_server(monkeypatch, lambda request: httpx.Response(400, json={"detail": "no transcript text was found"}))
    _fake_dialog(monkeypatch, ud.UploadRequest(kind="transcript", name="X", text="hi", source="pasted"))
    window = _window(qt_app, monkeypatch)
    try:
        window._open_recording_upload()
        assert _pump(lambda: "no transcript text was found" in window.status_label.text())
    finally:
        _close(window)


def test_cancelling_the_dialog_uploads_nothing(qt_app, home, monkeypatch):
    calls = []
    _fake_server(monkeypatch, lambda request: calls.append(request) or httpx.Response(500))
    from meeting_notes.client.ui import main_window as mw

    class Cancelled:
        def __init__(self, parent=None, start_dir=""):
            self.request = None

        def exec(self):
            return 0

    monkeypatch.setattr(mw, "UploadDialog", Cancelled)
    window = _window(qt_app, monkeypatch)
    try:
        window._open_recording_upload()
        QApplication.processEvents()
        assert calls == [] and window.upload_button.isEnabled()
    finally:
        _close(window)


def test_upload_without_a_server_says_so_and_never_opens_the_dialog(qt_app, home, monkeypatch):
    config_mod.save_config({"save_dir": str(home / "rec"), "server": {"url": "", "token": ""}})
    from meeting_notes.client.ui import main_window as mw

    def boom(*a, **k):
        raise AssertionError("the dialog must not open")

    monkeypatch.setattr(mw, "UploadDialog", boom)
    window = _window(qt_app, monkeypatch)
    try:
        window._open_recording_upload()
        assert "configure a server" in window.status_label.text()
    finally:
        _close(window)


def test_switching_choices_back_and_forth_keeps_the_hint_the_same_height(qt_app, home):
    dialog = ud.UploadDialog()
    dialog.show()
    first = dialog.hint.height()
    for mode in (ud.TRANSCRIPT_FILE, ud.TRANSCRIPT_PASTE, ud.AUDIO, ud.TRANSCRIPT_FILE, ud.AUDIO):
        dialog.set_mode(mode)
    assert dialog.hint.height() == first
    dialog.close()
