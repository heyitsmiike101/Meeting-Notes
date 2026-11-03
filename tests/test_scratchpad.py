"""The notes scratchpad: free text and links typed in the recorder during a meeting.

The recorder keeps it in ``scratchpad.md`` (a draft before Start), sends it as ``meta["scratchpad"]``; the server
stores it as typed, shows it rendered on the meeting page ("Your notes"), adds it to the AI notes prompt, the export zip
and the agent API.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
import time
import zipfile
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from meeting_notes import config as config_mod
from meeting_notes import scratchpad as sp
from meeting_notes import wire
from meeting_notes.client import scratchpad as store_mod_client
from meeting_notes.server.agent.service import AgentService
from meeting_notes.server.app import create_app
from tests.test_server import silence_pcm

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated:DeprecationWarning"
)

# One line of notes as typed into the editor (a paragraph), which the Markdown serializer hands back unchanged.
TYPED_TEXT = "typed while recording, see https://x.io/a"
NOTES_TEXT = "Agenda: budget\n  - see https://example.com/doc?id=1.\n(https://wiki.example.org/Foo_(bar)) and **bold**"


# -- the shared rule ---------------------------------------------------------------------------------------


LINK_CASES = [
    ("see https://example.com.", ["https://example.com"]),
    ("(https://example.com)", ["https://example.com"]),
    ("https://en.wikipedia.org/wiki/Foo_(bar)", ["https://en.wikipedia.org/wiki/Foo_(bar)"]),
    ("a https://x.io/a, b http://y.io/b!", ["https://x.io/a", "http://y.io/b"]),
    ("~~https://x.io/y~~", ["https://x.io/y"]),
    ("[label](https://x.io/z)", []),  # explicit link syntax is left alone, as in Life Dash
    ("ftp://nope.example javascript:alert(1) https://.", []),
    ("HTTPS://Upper.example/Path", ["HTTPS://Upper.example/Path"]),
    ("dup https://a.io and https://a.io", ["https://a.io"]),
]


@pytest.mark.parametrize("text, links", LINK_CASES)
def test_find_links_follows_the_life_dash_rule(text, links):
    assert sp.find_links(text) == links


def test_link_at_and_spans():
    text = "go to https://a.io/x. now"
    (start, end, url), = sp.link_spans(text)
    assert text[start:end] == url == "https://a.io/x"
    assert sp.link_at(text, start) == url and sp.link_at(text, end - 1) == url
    assert sp.link_at(text, end) is None and sp.link_at(text, 0) is None


def test_clean_keeps_text_as_typed():
    assert sp.clean("  indented\r\nline\rthree\n\n") == "  indented\nline\nthree"
    assert sp.clean("a\x00b") == "ab"
    for empty in (None, "", "  \n\t", 5, ["x"], {"a": 1}):
        assert sp.clean(empty) is None
    assert len(sp.clean("x" * (sp.MAX_CHARS + 50))) == sp.MAX_CHARS


def test_prompt_section_marks_the_notes_as_the_users_and_keeps_links():
    assert sp.prompt_section(None) == "" and sp.prompt_section("  ") == ""
    text = sp.prompt_section("Ask about pricing")
    assert "## Your notes (written by the user during the meeting)" in text
    assert "<user_notes>\nAsk about pricing\n</user_notes>" in text
    assert "Never attribute them to a speaker" in text and "Links" not in text
    assert 'under a "Links" heading' in sp.prompt_section("deck https://x.io/deck")


def test_join_orders_and_skips_empty():
    assert sp.join(["a", None, "  ", "b"]) == "a\n\nb"
    assert sp.join([None, ""]) is None


def test_renderer_autolinks_with_the_same_rule():
    """meeting_notes/markdown.py reuses scratchpad's tail rule: it links exactly what find_links finds."""
    from meeting_notes import markdown as md

    for text, links in LINK_CASES:
        if "](" in text:  # explicit [label](url) is a Markdown link, not an autolink
            continue
        rendered = re.findall(r'<a href="([^"]+)" target="_blank"', md.render(text))
        assert [u.replace("&amp;", "&") for u in dict.fromkeys(rendered)] == links, text


# -- the recorder's files ------------------------------------------------------------------------------------


@pytest.fixture
def cfg_path(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setenv("MEETING_NOTES_CONFIG", str(path))
    return path


def test_draft_and_session_files(tmp_path, cfg_path):
    assert store_mod_client.draft_path() == tmp_path / "scratchpad-draft.md"
    assert store_mod_client.save("before start")
    assert store_mod_client.load_draft() == "before start"
    session = tmp_path / "s1"
    session.mkdir()
    assert store_mod_client.attach_draft(session) == "before start"
    assert not store_mod_client.draft_path().exists()
    assert (session / "scratchpad.md").read_text(encoding="utf-8") == "before start"
    assert store_mod_client.save("during\r\nthe meeting", session)
    assert store_mod_client.read_session(session) == "during\nthe meeting"
    assert store_mod_client.save("", None) and not store_mod_client.draft_path().exists()  # emptied draft: gone
    assert store_mod_client.attach_draft(tmp_path / "s2") == ""
    assert store_mod_client.read_session(tmp_path / "s2") is None


def _record(tmp_path, *, draft=None, typed=None, server_url=""):
    from meeting_notes.client.controller import RecordingController
    from tests.fakes import FakeSource

    config_mod.save_config({
        "save_dir": str(tmp_path / "Meeting Notes"),
        "server": {"url": server_url, "token": "", "live_preview": False, "auto_upload": True},
    })
    controller = RecordingController()
    if draft is not None:
        assert controller.save_scratchpad(draft)  # idle: the draft
    sources = {"mic": FakeSource(name="Fake Mic", samplerate=48000), "system": FakeSource(name="Fake Loopback", samplerate=48000)}
    session_dir = controller.start("standup", sources=sources)
    assert session_dir is not None, controller.error
    if typed is not None:
        assert controller.save_scratchpad(typed)  # recording: the session folder
    time.sleep(0.4)
    meta = controller.stop()
    return controller, session_dir, meta


def test_controller_puts_the_scratchpad_in_the_meta(tmp_path, cfg_path):
    _, session_dir, meta = _record(tmp_path / "a", draft="typed before start")
    assert meta["scratchpad"] == "typed before start"
    assert not store_mod_client.draft_path().exists()
    _, session_dir, meta = _record(tmp_path / "b", draft="draft", typed=NOTES_TEXT)
    assert meta["scratchpad"] == NOTES_TEXT
    assert json.loads((session_dir / "session.json").read_text())["scratchpad"] == NOTES_TEXT
    assert (session_dir / "scratchpad.md").read_text(encoding="utf-8") == NOTES_TEXT  # the local copy stays
    _, session_dir, meta = _record(tmp_path / "c", typed="   ")
    assert "scratchpad" not in meta


def test_the_scratchpad_reaches_the_server_on_upload_and_from_the_file(tmp_path, cfg_path):
    from meeting_notes.server import store as store_mod
    from tests.test_integration import LiveServer
    from tests.test_note_type_client import _pump

    live = LiveServer(tmp_path / "server-data")
    url = live.start()
    try:
        controller, session_dir, _ = _record(tmp_path, typed=NOTES_TEXT, server_url=url)
        try:
            assert controller.start_uploader()
            assert _pump(lambda: controller.queue_status()["pending"] == 0, 30)
            store = store_mod.Store(str(tmp_path / "server-data"))
            (sid,) = [p.name for p in (tmp_path / "server-data" / "sessions").iterdir() if p.is_dir()]
            assert store.read_session_meta(sid)["scratchpad"] == NOTES_TEXT

            # A session.json without the key (an older recording): the queue sends scratchpad.md instead.
            meta_path = session_dir / "session.json"
            meta = json.loads(meta_path.read_text())
            meta.pop("scratchpad")
            meta_path.write_text(json.dumps(meta))
            (session_dir / "scratchpad.md").write_text("edited later", encoding="utf-8")
            assert len(controller.reupload_recordings([session_dir]).queued) == 1
            assert _pump(lambda: store.read_session_meta(sid).get("scratchpad") == "edited later", 30)
        finally:
            controller.stop_uploader()
    finally:
        live.stop()


# -- the server ---------------------------------------------------------------------------------------------


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    return create_app(data_root=str(tmp_path / "data"), enable_mcp=False)


def _finalize(client, sid, meta_extra):
    pcm = silence_pcm(2.0)
    resp = client.post(wire.track_upload_path(sid, "mic"), content=pcm,
                       headers={"Content-Type": "application/octet-stream", "X-Frames": str(len(pcm) // 2)})
    assert resp.status_code == 200, resp.text
    meta = {"created": "2026-10-23", "tracks": {"mic": {}}, **meta_extra}
    resp = client.post(wire.finalize_path(sid), json={"meta": meta, "timing": {}, "settings": {}})
    assert resp.status_code == 200, resp.text


def test_finalize_stores_cleans_and_keeps_the_scratchpad(app):
    client = TestClient(app)
    store = app.state.store
    _finalize(client, "f1", {"scratchpad": "line one\r\nhttps://x.io/a\n"})
    assert store.read_session_meta("f1")["scratchpad"] == "line one\nhttps://x.io/a"
    for bad in (5, ["x"], {"a": 1}, "   "):
        _finalize(client, "f2", {"scratchpad": bad})  # never a failed finalize
        assert "scratchpad" not in store.read_session_meta("f2")
    # a re-upload that carries no notes keeps the stored ones; one that carries new ones replaces them
    _finalize(client, "f1", {})
    assert store.read_session_meta("f1")["scratchpad"] == "line one\nhttps://x.io/a"
    _finalize(client, "f1", {"scratchpad": "newer"})
    assert store.read_session_meta("f1")["scratchpad"] == "newer"
    assert client.get("/v1/sessions/f1").json()["meta"]["scratchpad"] == "newer"


def test_review_prompt_carries_the_scratchpad(app):
    client = TestClient(app)
    store = app.state.store
    _meeting_with_transcript(store, "p1", {"name": "Sync", "scratchpad": "pricing deck https://x.io/deck"})
    _meeting_with_transcript(store, "p2", {"name": "Plain"})
    with_notes = store.create_review("p1", force=True)
    without = store.create_review("p2", force=True)
    text = client.get(f"/v1/bridge/review/{with_notes['review_id']}/workflow.md").text
    assert "## Your notes (written by the user during the meeting)" in text
    assert "pricing deck https://x.io/deck" in text and 'under a "Links" heading' in text
    assert "Your notes" not in client.get(f"/v1/bridge/review/{without['review_id']}/workflow.md").text


def _meeting_with_transcript(store, sid, meta):
    store.write_session_meta(sid, meta)
    job_id = store.create_job(sid)
    store.update_job(job_id, state=wire.JobState.DONE, progress=1.0)
    store.write_transcript(job_id, "# Meeting transcript\n\nHello there", json.dumps({"segments": []}))


def test_export_zip_and_agent_api_include_the_raw_notes(app):
    client = TestClient(app)
    store = app.state.store
    _meeting_with_transcript(store, "e1", {"name": "Planning", "created": "2026-10-23", "scratchpad": NOTES_TEXT})
    _meeting_with_transcript(store, "e2", {"name": "Other", "created": "2026-10-23"})
    archive = zipfile.ZipFile(io.BytesIO(client.get("/sessions/e1/export.zip").content))
    assert sorted(archive.namelist()) == ["Planning - transcript.md", "Planning - your notes.md"]
    assert archive.read("Planning - your notes.md").decode("utf-8") == NOTES_TEXT + "\n"
    archive = zipfile.ZipFile(io.BytesIO(client.get("/sessions/e2/export.zip").content))
    assert archive.namelist() == ["Other - transcript.md"]

    service = AgentService(store)
    assert service.get_meeting("e1")["scratchpad"] == NOTES_TEXT
    assert service.get_meeting("e2")["scratchpad"] is None


def test_meeting_page_has_the_your_notes_section(app):
    page = TestClient(app).get("/meetings").text
    assert re.search(r'<details class="extras your-notes" id="your-notes" open><summary>Your notes</summary>', page)
    assert 'id="your-notes-text"' in page and "showScratchpad(meta.scratchpad,id)" in page
    assert "Nothing typed in the recorder for this meeting." in page and 'id="your-notes-empty"' in page
    # the server renders the Markdown; the page fetches it and injects the HTML it returns
    body = page[page.index("function showScratchpad("):]
    body = body[: body.index("\n")]
    assert "/scratchpad.html" in body and "innerHTML" in body


def test_scratchpad_html_route_renders_the_markdown(app):
    client = TestClient(app)
    store = app.state.store
    _meeting_with_transcript(store, "h1", {"name": "Sync", "scratchpad": "# Plan\n- [x] done\nsee https://x.io/a.\n<script>alert(1)</script>"})
    _meeting_with_transcript(store, "h2", {"name": "Plain"})
    resp = client.get("/v1/sessions/h1/scratchpad.html")
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/html")
    assert "<h2>Plan</h2>" in resp.text and 'class="note-task"' in resp.text
    assert '<a href="https://x.io/a" target="_blank" rel="noopener noreferrer">https://x.io/a</a>.' in resp.text
    assert "<script" not in resp.text and "&lt;script&gt;" in resp.text
    empty = client.get("/v1/sessions/h2/scratchpad.html")
    assert empty.status_code == 200 and empty.text == ""
    assert client.get("/v1/sessions/nope/scratchpad.html").text == ""
    assert client.get("/v1/sessions/..%2Fx/scratchpad.html").status_code in (400, 404)


def test_combine_joins_every_parts_notes(app):
    from meeting_notes.server import splitmerge as sm
    from tests.split_helpers import make_meeting, ramp, seg

    store = app.state.store
    t0 = 1_760_000_000.0
    make_meeting(store, "a", "Kickoff", t0, 30, [seg(1, 5, "You", "a-one")], mic=ramp(30),
                 extra_meta={"scratchpad": "first part"})
    make_meeting(store, "b", "Kickoff again", t0 + 60, 30, [seg(1, 5, "You", "b-one")], mic=ramp(30))
    make_meeting(store, "c", "Wrap", t0 + 120, 30, [seg(1, 5, "You", "c-one")], mic=ramp(30),
                 extra_meta={"scratchpad": "third part"})
    new_id = sm.combine_sessions(store, ["c", "a", "b"])["session_id"]
    assert store.read_session_meta(new_id)["scratchpad"] == "first part\n\nthird part"


# -- the recorder's UI ----------------------------------------------------------------------------------------


@pytest.fixture
def window(tmp_path, cfg_path, monkeypatch):
    pytest.importorskip("PySide6", reason="PySide6 not installed")
    from PySide6.QtWidgets import QApplication

    from meeting_notes.audio import devices as devices_mod
    from meeting_notes.client.controller import RECORDING, RecordingController
    from meeting_notes.client.ui.main_window import MainWindow
    from meeting_notes.client.ui.theme import APP_STYLE

    qt = QApplication.instance() or QApplication([])
    qt.setStyleSheet(APP_STYLE)
    monkeypatch.setenv("MEETING_NOTES_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("MEETING_NOTES_NO_DETECT", "1")

    def _raise(kind, requested=None, samplerate=None):
        raise devices_mod.DeviceNotFound("no device (simulated)")

    monkeypatch.setattr(devices_mod, "resolve_source", _raise)
    store_mod_client.save("left from last time")
    w = MainWindow()
    for timer in ("_timer", "_auth_timer", "_detect_timer", "_update_timer"):
        getattr(w, timer).stop()
    session_dir = tmp_path / "20261023-100000-Sync-PC"
    session_dir.mkdir()
    controller = w.controller

    def fake_start(name="", sources=None):
        store_mod_client.attach_draft(session_dir)
        controller.state = RECORDING
        controller.session = SimpleNamespace(
            request_stop=lambda: None, finalize=lambda: {"duration_sec": 3.0, "tracks": {"mic": {}}}, recorders={},
            elapsed=12.5, track_muted=lambda track: False,
        )
        controller.session_dir = session_dir
        controller._recording_name = name
        return session_dir

    monkeypatch.setattr(controller, "start", fake_start)
    original_stop = RecordingController.stop
    monkeypatch.setattr(controller, "stop", lambda: original_stop(controller))
    w.session_dir = session_dir
    w.qt = qt
    yield w
    w._leave_compact(persist=False)
    w._teardown_done = True
    w.close()


def _pump(qt, condition, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qt.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return False


def test_the_window_shows_the_notes_with_the_draft(window):
    assert window.scratchpad.text() == "left from last time"
    window.show()
    assert window.scratchpad_pane.isVisible()
    assert window.scratchpad_pane.editor.accessibleName() == "Your notes"  # no title on the pane, the editor is named
    assert not window.scratchpad_pane.close_button.isVisible()
    assert not hasattr(window, "notes_button")  # the notes are always there; nothing opens or closes them


def test_typing_autosaves_and_the_notes_go_with_the_recording(window):
    qt = window.qt
    model = window.scratchpad
    model.document.setPlainText("draft edit")
    assert model.status == "Saving..."
    assert _pump(qt, lambda: model.status == "Saved", 3)
    assert store_mod_client.load_draft() == "draft edit"

    window._start()
    assert (window.session_dir / "scratchpad.md").read_text(encoding="utf-8") == "draft edit"
    model.document.setPlainText(TYPED_TEXT)  # typed while recording; Stop saves at once, no pause needed
    window._stop()
    assert model.locked and window.scratchpad_pane.editor.isReadOnly()
    from meeting_notes.client.controller import IDLE

    assert _pump(qt, lambda: window.controller.state == IDLE and window._record_state == "idle", 10)
    meta = json.loads((window.session_dir / "session.json").read_text(encoding="utf-8"))
    assert meta["scratchpad"] == TYPED_TEXT
    assert model.text() == "" and not model.locked  # the next meeting starts empty
    assert not store_mod_client.draft_path().exists()


def test_a_failed_start_keeps_the_notes_as_the_draft(window, monkeypatch):
    def failed_start(name="", sources=None):
        store_mod_client.attach_draft(window.session_dir)  # the draft moved, then the start failed
        window.controller.error = "no devices"
        return None

    monkeypatch.setattr(window.controller, "start", failed_start)
    window._start()
    assert store_mod_client.load_draft() == "left from last time"
    assert window.scratchpad.text() == "left from last time"


def test_failed_save_says_not_saved(window):
    model = window.scratchpad
    model._saver = lambda text: False
    model.document.setPlainText("x")
    assert model.flush() is False and model.status == "Not saved"


def test_links_are_found_under_the_mouse(window):
    window.show()
    editor = window.scratchpad_pane.editor
    window.scratchpad.set_text("open https://x.io/a now")
    window.qt.processEvents()
    cursor = editor.textCursor()
    cursor.setPosition(9)  # inside the link
    point = editor.cursorRect(cursor).center()
    assert editor.link_at(point) == "https://x.io/a"
    cursor.setPosition(2)
    assert editor.link_at(editor.cursorRect(cursor).center()) is None


def test_one_view_card_notes_and_folded_preview(window):
    window.resize(1000, 760)
    window.show()

    def top(widget):
        return widget.mapTo(window, widget.rect().topLeft()).y()

    # name, people, type, Start; under them the clock, the two lanes with their icons and device names
    assert top(window.record_button) < top(window.clock) < top(window.scratchpad_pane) < top(window.preview)
    assert window.waveform.thin and window.waveform.isVisible()
    assert window.mic_source_button.isVisible() and window.system_source_button.isVisible()
    assert window.mic_device_label.isVisible() and window.system_device_label.isVisible()
    assert not window.mute_mic_button.isVisible() and not window.mute_system_button.isVisible()
    assert window.transcript_toggle.isVisible()
    assert window.preview.maximumHeight() == window._folded_preview_height()  # folded unless opened out


def test_device_names_are_one_line_and_scroll_on_hover(window):
    from meeting_notes.client.ui import marquee

    window.resize(900, 760)
    window.show()
    window.controller.device_labels = lambda: {"mic": "Microphone (Jabra Evolve2 65 - Hands-Free AG Audio) " * 2,
                                               "system": "Speakers"}
    window.controller.device_notes = lambda: {"mic": "Automatic passed over the headset mic. More here."}
    window.controller.device_banners = lambda: []
    window._sync_devices()
    window.qt.processEvents()
    mic, system = window.mic_device_label, window.system_device_label
    assert system.text() == "Them: Speakers" and system.overflow() == 0
    assert mic.text().startswith("You: Microphone") and "\n" not in mic.text()
    assert not mic.wordWrap()  # one line, never wrapped
    assert "Automatic passed over" in mic.toolTip()  # the automatic-choice note is on hover
    assert mic.overflow() > 0 and mic.offset == 0
    mic._hovered = True
    mic._restart()
    for _ in range(marquee.PAUSE_TICKS + 30):
        mic._step()
    assert mic.offset == 30  # scrolls while the mouse is over it
    mic._hovered = False
    mic._restart()
    assert mic.offset == 0  # and goes back to the start when it leaves


def test_the_notes_views_preview_folds_to_its_last_two_lines(window):
    from meeting_notes.client.ui.main_window import TRANSCRIPT_FOLDED_LINES

    window.resize(1000, 760)
    window.show()
    preview = window.preview
    for i in range(8):
        preview.appendPlainText(f"line {i + 1}")
    window._apply_transcript_fold()
    window.qt.processEvents()
    assert window.transcript_toggle.text() == "Show all" and not window.transcript_toggle.isChecked()
    assert preview.height() == preview.maximumHeight() == window._folded_preview_height()
    bar = preview.verticalScrollBar()
    assert bar.maximum() == 8 - TRANSCRIPT_FOLDED_LINES and bar.value() == bar.maximum()  # exactly the last two lines
    assert preview.firstVisibleBlock().text() == "line 7"

    window.transcript_toggle.click()  # open it out
    window.qt.processEvents()
    assert window.transcript_toggle.text() == "Show less"
    assert preview.height() > window._folded_preview_height()
    assert config_mod.ui_state()["transcript_expanded"] is True  # remembered

    window.transcript_toggle.click()
    assert config_mod.ui_state()["transcript_expanded"] is False
    assert preview.maximumHeight() == window._folded_preview_height()


def test_new_lines_keep_the_folded_preview_at_the_end(window):
    window.resize(1000, 760)
    window.show()
    window._start()
    partials = [{"track": "mic" if i % 2 else "system", "text": f"said {i}"} for i in range(6)]
    window.controller.partials = lambda: partials
    window._drain_partials()
    window.qt.processEvents()
    preview = window.preview
    assert preview.firstVisibleBlock().text() == "Them: said 4"  # the last two lines
    bar = preview.verticalScrollBar()
    assert bar.value() == bar.maximum()
    assert preview.document().lastBlock().text() == "You: said 5"


def test_the_mic_and_speaker_icons_light_up_and_mute(window, monkeypatch):
    import time as _time

    from meeting_notes.client.ui.main_window import SOURCE_LIT_HOLD_SEC

    window.show()
    mic, speaker = window.mic_source_button, window.system_source_button
    now = _time.monotonic()
    window._sync_source_buttons(now)
    assert mic.property("lit") == "false" and speaker.property("lit") == "false"

    window._note_source_levels(now, {"mic": 0.4, "system": 0.001})  # you are heard, they are quiet
    window._sync_source_buttons(now)
    assert mic.property("lit") == "true" and speaker.property("lit") == "false"
    window._sync_source_buttons(now + SOURCE_LIT_HOLD_SEC + 0.1)  # goes dark once the sound stops
    assert mic.property("lit") == "false"

    # Idle: nothing to mute, so a click changes nothing.
    speaker.click()
    assert not window.mute_system_button.isChecked() and not speaker.isChecked()
    assert "while recording" in speaker.toolTip()

    # Recording: the icon mutes that side through the mute button (struck through, never lit while muted).
    window._start()
    muted = {}
    monkeypatch.setattr(window.controller, "set_source_muted", lambda track, on: muted.update({track: on}) or True)
    window.mute_system_button.setEnabled(True)
    speaker.click()
    assert muted == {"system": True} and window.mute_system_button.isChecked() and speaker.isChecked()
    assert window.mute_system_button.text() == "Unmute them"
    assert speaker.toolTip() == "Unmute the meeting audio"
    window._note_source_levels(now, {"system": 0.5})
    window._sync_source_buttons(now)
    assert speaker.property("lit") == "false"
    window.mute_system_button.click()  # unmuting from the recorder view's button shows on the icon too
    window._sync_source_buttons(now)
    assert not speaker.isChecked() and muted == {"system": False}


def test_notes_view_icons_exist():
    from meeting_notes.client.ui import icons

    for name in ("mic", "mic-off", "speaker", "speaker-off", "chevron-up", "chevron-down"):
        assert name in icons._DRAWERS
        assert not icons.make_icon(name, size=20).pixmap(20, 20).isNull()
    for name in ("recorder-collapse", "recorder-expand"):
        assert name not in icons._DRAWERS


def test_the_notes_popout_size_is_remembered(window):
    from meeting_notes.client.ui import scratchpad as ui

    window.enter_compact_mode(persist=False)
    window._compact_bar.notes_button.click()
    popout = window._scratchpad_popout
    assert (popout.width(), popout.height()) == ui.POPOUT_SIZE
    popout.resize(500, 600)
    assert _pump(window.qt, lambda: config_mod.ui_state().get("notes_popout_size") == [500, 600], 3)
    assert ui.remembered_popout_size() == (500, 600)
    fresh = ui.ScratchpadPopout(window.scratchpad)
    assert (fresh.width(), fresh.height()) == (500, 600)
    fresh.deleteLater()
    assert ui.remembered_popout_size({"notes_popout_size": [10, 10]}) == ui.POPOUT_MIN_SIZE
    assert ui.remembered_popout_size({"notes_popout_size": "big"}) == ui.POPOUT_SIZE
    window._leave_compact(persist=False)


def test_compact_bar_notes_button_opens_the_window_beside_the_bar(window):
    window.enter_compact_mode(persist=False)
    bar = window._compact_bar
    assert bar.notes_button.toolTip() == "Your notes"
    bar.notes_button.click()
    popout = window._scratchpad_popout
    assert popout is not None and popout.isVisible() and bar.notes_button.isChecked()
    assert popout.view.editor.document() is window.scratchpad.document  # the same text as the window's pane
    assert popout.frameGeometry().right() < bar.frameGeometry().left() + 1
    bar.notes_button.click()
    assert not popout.isVisible() and not bar.notes_button.isChecked()
    bar.notes_button.click()
    window._leave_compact(persist=False)  # expanding takes the notes window down with the bar
    assert not popout.isVisible()
