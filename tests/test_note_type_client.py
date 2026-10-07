"""Note types in the client: the default (Settings), the per-meeting picker next to the meeting name, the
``note_type`` in the saved/uploaded session metadata, and the remote commands that drive them.

The server half is ``test_note_type_tagging.py``; the protocol and Recorders page ``test_remote_note_type.py``.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest

pytest.importorskip("PySide6", reason="PySide6 not installed")

from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

from meeting_notes import config as config_mod  # noqa: E402
from meeting_notes import remote  # noqa: E402
from meeting_notes.client import api as api_mod  # noqa: E402
from meeting_notes.client import authcheck  # noqa: E402
from meeting_notes.client.controller import IDLE, RECORDING, RecordingController  # noqa: E402
from meeting_notes.client.meeting_detect import MeetingStarted  # noqa: E402
from meeting_notes.client.ui.settings_dialog import SettingsDialog  # noqa: E402
from meeting_notes.client.ui.theme import APP_STYLE  # noqa: E402
from tests.fakes import FakeSource  # noqa: E402

TYPES = [
    {"id": "standard", "name": "Standard"},
    {"id": "quick", "name": "Quick notes"},
    {"id": "webinar", "name": "Detailed webinar"},
]
STARTED = MeetingStarted("teams", "Teams", "Weekly Sync")


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(APP_STYLE)
    return app


def _pump(condition, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        QApplication.processEvents()
        time.sleep(0.01)
    return condition()


class FakeDetector:
    def __init__(self):
        self.queue = []
        self.end_grace_sec = 60.0

    def poll(self, now):
        events, self.queue = self.queue, []
        return events


@pytest.fixture
def cfg_path(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setenv("MEETING_NOTES_CONFIG", str(path))
    return path


@pytest.fixture
def window(qt_app, tmp_path, cfg_path, monkeypatch):
    """A real window and a real controller whose recording is faked: start() just flips the state and
    stop() writes session.json through the real RecordingController.stop (so ``note_type`` lands there)."""
    from meeting_notes.audio import devices as devices_mod
    from meeting_notes.client.ui.main_window import MainWindow

    def _raise(kind, requested=None, samplerate=None):
        raise devices_mod.DeviceNotFound("no device (simulated)")

    monkeypatch.setattr(devices_mod, "resolve_source", _raise)
    w = MainWindow()
    for _ in range(3):
        QApplication.processEvents()  # the startup single-shots (no server configured yet: nothing happens)
    w._timer.stop()
    w._detect_timer.stop()
    w._auth_timer.stop()
    w._update_timer.stop()
    w._detector = FakeDetector()
    w._wall_now = lambda: datetime(2026, 10, 6, 14, 3, 0)
    session_dir = tmp_path / "20261006-140300-Weekly-Sync-PC"
    session_dir.mkdir()
    controller = w.controller

    def fake_start(name="", sources=None):
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
    yield w
    w._reset_end_state()
    w._clear_auto_end_state()
    w._close_prompt()
    w._teardown_done = True
    w.close()


def saved_meta(window):
    return json.loads((window.session_dir / "session.json").read_text(encoding="utf-8"))


def ids(combo):
    return [combo.itemData(i) for i in range(combo.count())]


def stopped(window):
    return _pump(lambda: window.controller.state == IDLE) and _pump(lambda: window._record_state == "idle")


# -- config ------------------------------------------------------------------------------


def test_default_note_type_setting_is_strict():
    assert config_mod.default_note_type_setting({}) == ""
    assert config_mod.default_note_type_setting({"default_note_type": "quick"}) == "quick"
    assert config_mod.default_note_type_setting({"default_note_type": "  quick "}) == "quick"
    assert config_mod.default_note_type_setting({"default_note_type": "t0123-ab_C"}) == "t0123-ab_C"
    for junk in (None, "", "  ", 3, True, ["quick"], {"id": "quick"}, "has space", "../x", "a" * 65, "q\nx"):
        assert config_mod.default_note_type_setting({"default_note_type": junk}) == ""
    assert config_mod.default_note_type_setting(None) == ""  # no config file


def test_default_note_type_setting_reads_the_config_file(cfg_path):
    assert config_mod.default_note_type_setting() == ""
    cfg_path.write_text(json.dumps({"default_note_type": "webinar"}))
    assert config_mod.default_note_type_setting() == "webinar"


# -- api -----------------------------------------------------------------------------------


def test_clean_note_types_keeps_usable_rows_and_a_valid_default():
    data = {
        "default_template_id": "quick",
        "items": [
            {"id": "standard", "name": "Standard", "builtin": True, "default": False},
            {"id": "quick", "name": "  Quick   notes ", "builtin": True, "default": True},
            {"id": "quick", "name": "dup"},
            {"id": "bad id", "name": "x"},
            {"name": "no id"},
            "junk",
            {"id": "t1", "name": ""},
        ],
    }
    types, default = api_mod.clean_note_types(data)
    assert types == [
        {"id": "standard", "name": "Standard"}, {"id": "quick", "name": "Quick notes"}, {"id": "t1", "name": "t1"},
    ]
    assert default == "quick"
    assert api_mod.clean_note_types({"items": data["items"], "default_template_id": "gone"})[1] == ""
    assert api_mod.clean_note_types(None) == ([], "")
    assert api_mod.clean_note_types({"items": "x"}) == ([], "")


def test_server_client_note_templates_gets_the_endpoint_with_the_token():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, request.headers.get("authorization")))
        return httpx.Response(200, json={"default_template_id": "standard", "items": [{"id": "standard", "name": "Standard"}]})

    client = api_mod.ServerClient("http://server.test", "tok")
    client._client = httpx.Client(base_url="http://server.test", transport=httpx.MockTransport(handler))
    assert client.note_templates()["default_template_id"] == "standard"
    assert seen == [("GET", "/v1/note-templates", "Bearer tok")]


# -- the picker ----------------------------------------------------------------------------


def test_picker_is_hidden_until_two_types_are_known(window):
    combo = window.note_type_combo
    assert combo.isHidden() and combo.count() == 0
    assert combo.accessibleName() == "Note type" and combo.minimumHeight() == 40
    window._apply_note_types(TYPES[:1], "standard")
    assert combo.isHidden() and combo.count() == 1  # one type: nothing to choose
    window._apply_note_types(TYPES, "standard")
    assert not combo.isHidden() and ids(combo) == ["standard", "quick", "webinar"]
    assert [combo.itemText(i) for i in range(3)] == ["Standard", "Quick notes", "Detailed webinar"]


def test_picker_sits_between_the_name_and_the_record_button(window):
    layout = window.name_edit.parentWidget().layout()
    controls = next(layout.itemAt(i).layout() for i in range(layout.count()) if layout.itemAt(i).layout() is not None
                    and layout.itemAt(i).layout().indexOf(window.name_edit) >= 0)
    order = [controls.indexOf(w) for w in (window.name_edit, window.note_type_combo, window.record_button)]
    assert order == sorted(order) and -1 not in order


@pytest.mark.parametrize(
    "saved, server_default, expected",
    [
        ("webinar", "quick", "webinar"),     # the saved default wins when the server has it
        ("gone", "quick", "quick"),          # saved but no longer on the server: the server's default
        ("", "quick", "quick"),              # nothing saved: the server's default
        ("", "", "standard"),                # the server named none we know: the first type
        ("gone", "", "standard"),
    ],
)
def test_picker_preselects_the_saved_default_else_the_server_default(window, cfg_path, saved, server_default, expected):
    cfg_path.write_text(json.dumps({"default_note_type": saved} if saved else {}))
    window._apply_note_types(TYPES, server_default)
    assert window.note_type_combo.currentData() == expected
    assert window.controller.note_type == expected  # what stop() would save


def test_the_selection_survives_a_refresh_while_the_type_still_exists(window):
    window._apply_note_types(TYPES, "standard")
    window.note_type_combo.setCurrentIndex(window.note_type_combo.findData("webinar"))
    window._apply_note_types(TYPES + [{"id": "t9", "name": "Extra"}], "quick")
    assert window.note_type_combo.currentData() == "webinar" and window.controller.note_type == "webinar"
    assert window._server_default_note_type == "quick"
    window._apply_note_types(TYPES[:2], "quick")  # webinar was deleted on the server
    assert window.note_type_combo.currentData() == "quick"


def test_the_choice_is_saved_at_stop_and_the_picker_resets_afterwards(window):
    window._apply_note_types(TYPES, "standard")
    window._start()
    assert window.controller.state == RECORDING
    window.note_type_combo.setCurrentIndex(window.note_type_combo.findData("quick"))  # editable while recording
    assert window.note_type_combo.isEnabled()
    window._stop()
    assert stopped(window)
    assert saved_meta(window)["note_type"] == "quick"
    assert window.note_type_combo.currentData() == "standard"  # the next meeting starts from the default
    assert window.controller.note_type == "standard"


def test_the_picker_resets_to_the_saved_default_not_just_the_first(window, cfg_path):
    cfg_path.write_text(json.dumps({"default_note_type": "webinar"}))
    window._apply_note_types(TYPES, "standard")
    window.note_type_combo.setCurrentIndex(0)
    window._start()
    window._stop()
    assert stopped(window)
    assert saved_meta(window)["note_type"] == "standard"
    assert window.note_type_combo.currentData() == "webinar"


def test_an_unfetched_list_falls_back_to_the_saved_default_else_omits_the_key(window, cfg_path):
    window._start()
    window._stop()
    assert stopped(window)
    assert "note_type" not in saved_meta(window)  # nothing known: the server uses its own default
    cfg_path.write_text(json.dumps({"default_note_type": "quick"}))
    window.session_dir.joinpath("session.json").unlink()
    window._start()
    window._stop()
    assert stopped(window)
    assert saved_meta(window)["note_type"] == "quick"


def test_an_auto_recorded_call_is_tagged_with_the_default_note_type(window, cfg_path):
    cfg_path.write_text(json.dumps({
        "default_note_type": "webinar",
        "meeting_detection": {"enabled": True, "auto_record": True, "auto_end": "manual"},
    }))
    window._apply_meeting_settings()
    window._apply_note_types(TYPES, "standard")
    window._detector.queue.append(STARTED)
    window._poll_meeting()
    assert window.controller.state == RECORDING and window._auto_session
    assert window.note_type_combo.currentData() == "webinar"
    window._stop()
    assert stopped(window)
    assert saved_meta(window)["note_type"] == "webinar"


def test_an_auto_recorded_call_keeps_a_note_type_picked_beforehand(window, cfg_path):
    # The combo returns to the default after every recording, so a different pick while idle is deliberate.
    cfg_path.write_text(json.dumps({
        "default_note_type": "webinar",
        "meeting_detection": {"enabled": True, "auto_record": True, "auto_end": "manual"},
    }))
    window._apply_meeting_settings()
    window._apply_note_types(TYPES, "standard")
    window.note_type_combo.setCurrentIndex(window.note_type_combo.findData("quick"))
    window._detector.queue.append(STARTED)
    window._poll_meeting()
    assert window.controller.state == RECORDING
    window._stop()
    assert stopped(window)
    assert saved_meta(window)["note_type"] == "quick"
    assert window.note_type_combo.currentData() == "webinar"  # back to the default for the next meeting


def test_an_auto_recorded_call_without_a_saved_default_gets_the_servers(window, cfg_path):
    cfg_path.write_text(json.dumps({"meeting_detection": {"enabled": True, "auto_record": True, "auto_end": "manual"}}))
    window._apply_meeting_settings()
    window._apply_note_types(TYPES, "quick")
    window._detector.queue.append(STARTED)
    window._poll_meeting()
    window._stop()
    assert stopped(window)
    assert saved_meta(window)["note_type"] == "quick"


# -- fetching the list ---------------------------------------------------------------------


def _server_config(cfg_path, **extra):
    cfg_path.write_text(json.dumps({"server": {"url": "http://meeting.test", "token": "tok"}, **extra}))


def test_the_list_is_fetched_off_thread_and_applied(window, cfg_path):
    _server_config(cfg_path)
    calls = []

    def fetcher(url, token):
        calls.append((url, token))
        return TYPES, "quick"

    window._note_types_fetcher = fetcher
    window._refresh_note_types("test")
    assert window._note_types_running
    assert _pump(lambda: window.note_type_combo.count() == 3)
    assert calls == [("http://meeting.test", "tok")]
    assert window.note_type_combo.currentData() == "quick" and not window.note_type_combo.isHidden()
    assert not window._note_types_running


def test_a_failed_fetch_is_silent_and_keeps_the_last_list(window, cfg_path):
    _server_config(cfg_path)
    window._apply_note_types(TYPES, "quick")

    def boom(url, token):
        raise ConnectionError("down")

    window._note_types_fetcher = boom
    window._refresh_note_types("test")
    assert _pump(lambda: not window._note_types_running)
    assert ids(window.note_type_combo) == ["standard", "quick", "webinar"]
    window._note_types_fetcher = lambda url, token: ([], "")  # an empty answer does not wipe it either
    window._refresh_note_types("test")
    assert _pump(lambda: not window._note_types_running)
    assert window.note_type_combo.count() == 3


def test_nothing_is_fetched_without_a_server(window, cfg_path):
    window._note_types_fetcher = lambda url, token: pytest.fail("fetched without a server")
    window._refresh_note_types("test")
    assert not window._note_types_running


def test_the_list_is_fetched_when_the_connection_check_succeeds(window, cfg_path):
    _server_config(cfg_path)
    window._note_types_fetcher = lambda url, token: (TYPES, "standard")
    window._on_auth_checked(authcheck.CheckResult(authcheck.OK, "http://meeting.test"))
    assert _pump(lambda: window.note_type_combo.count() == 3)
    window._note_types_fetcher = lambda url, token: pytest.fail("fetched after a rejected password")
    window._on_auth_checked(authcheck.CheckResult(authcheck.REJECTED, "http://meeting.test"))
    assert not window._note_types_running


def test_the_list_is_fetched_at_startup_and_after_settings_are_saved(window, cfg_path, monkeypatch):
    _server_config(cfg_path)
    reasons = []
    monkeypatch.setattr(window, "_refresh_note_types", lambda reason="": reasons.append(reason))
    monkeypatch.setattr(window, "_start_auth_check", lambda reason="": None)
    monkeypatch.setattr(window, "_check_for_update", lambda force=False: None)
    window._on_uploader_restarted(None)
    assert reasons == ["settings saved"]


# -- the real controller and the queue ------------------------------------------------------


def _record(tmp_path, cfg_path, note_type=None, server_url="", seconds=0.6):
    config_mod.save_config({
        "save_dir": str(tmp_path / "Meeting Notes"),
        "server": {"url": server_url, "token": "", "live_preview": False, "auto_upload": True},
    })
    controller = RecordingController()
    if note_type is not None:
        controller.note_type = note_type
    sources = {"mic": FakeSource(name="Fake Mic", samplerate=48000), "system": FakeSource(name="Fake Loopback", samplerate=48000)}
    session_dir = controller.start("standup", sources=sources)
    assert session_dir is not None, controller.error
    time.sleep(seconds)
    meta = controller.stop()
    return controller, session_dir, meta


def test_controller_stop_writes_the_note_type_and_omits_it_when_unknown(tmp_path, cfg_path):
    _, session_dir, meta = _record(tmp_path / "a", cfg_path, "quick")
    assert meta["note_type"] == "quick"
    assert json.loads((session_dir / "session.json").read_text())["note_type"] == "quick"
    _, session_dir, meta = _record(tmp_path / "b", cfg_path)
    assert "note_type" not in meta and "note_type" not in json.loads((session_dir / "session.json").read_text())
    for junk in ("", "  ", "has space", "../x"):
        _, session_dir, meta = _record(tmp_path / f"c{abs(hash(junk))}", cfg_path, junk)
        assert "note_type" not in meta


def test_the_tag_reaches_the_server_on_upload_and_on_a_reupload(tmp_path, cfg_path):
    from meeting_notes.server import store as store_mod
    from tests.test_integration import LiveServer

    live = LiveServer(tmp_path / "server-data")
    url = live.start()
    try:
        controller, session_dir, _ = _record(tmp_path, cfg_path, "webinar", server_url=url)
        try:
            assert controller.start_uploader()
            assert _pump(lambda: controller.queue_status()["pending"] == 0, 30)
            store = store_mod.Store(str(tmp_path / "server-data"))
            (sid,) = [p.name for p in (tmp_path / "server-data" / "sessions").iterdir() if p.is_dir()]
            assert store.read_session_meta(sid)["note_type"] == "webinar"
            assert _pump(lambda: store.list_reviews(session_id=sid), 30)  # tagged: notes without the auto setting
            assert store.list_reviews(session_id=sid)[0]["template_id"] == "webinar"

            # re-upload of the saved recording: session.json is read again, the tag goes along again
            store.update_session_meta(sid, lambda m: m.pop("note_type", None) and None)
            assert "note_type" not in store.read_session_meta(sid)
            result = controller.reupload_recordings([session_dir])
            assert len(result.queued) == 1
            assert _pump(lambda: store.read_session_meta(sid).get("note_type") == "webinar", 30)
        finally:
            controller.stop_uploader()
    finally:
        live.stop()


# -- Settings --------------------------------------------------------------------------------


def _dialog(cfg_path, saved=None, types=None, server_default=""):
    data = {"default_note_type": saved} if saved else {}
    cfg_path.write_text(json.dumps(data))
    return SettingsDialog(note_types=types, server_default_note_type=server_default)


def test_settings_has_a_notes_section_after_meeting_detection(qt_app, cfg_path):
    dialog = _dialog(cfg_path, types=TYPES, server_default="quick")
    combo = dialog.default_note_type_combo
    assert combo.accessibleName() == "Default note type"
    form = dialog._meeting_form
    rows = [form.getWidgetPosition(w)[0] for w in (dialog.suggest_stop_check, combo)]
    assert rows == sorted(rows) and len(set(rows)) == 2
    texts = [w.text() for w in dialog.findChildren(QLabel)]
    assert "Notes" in texts
    assert any("Meetings recorded here get notes of this type automatically, saved where that note type sends them" in t for t in texts)
    assert not any("Connect to the server to choose a note type." in t for t in texts)
    assert dialog._page_widgets["general"].isAncestorOf(combo)
    dialog.close()


def test_settings_lists_server_default_then_each_type(qt_app, cfg_path):
    dialog = _dialog(cfg_path, types=TYPES, server_default="quick")
    combo = dialog.default_note_type_combo
    assert ids(combo) == ["", "standard", "quick", "webinar"]
    assert combo.itemText(0) == "Server default (Quick notes)"
    assert combo.currentData() == ""
    dialog.close()
    dialog = _dialog(cfg_path, types=TYPES)  # no server default known
    assert dialog.default_note_type_combo.itemText(0) == "Server default"
    dialog.close()


def test_settings_preselects_the_saved_default(qt_app, cfg_path):
    dialog = _dialog(cfg_path, saved="webinar", types=TYPES, server_default="quick")
    assert dialog.default_note_type_combo.currentData() == "webinar"
    dialog.close()


def test_settings_without_a_list_shows_server_default_the_saved_id_and_a_hint(qt_app, cfg_path):
    dialog = _dialog(cfg_path, saved="webinar")
    combo = dialog.default_note_type_combo
    assert ids(combo) == ["", "webinar"] and combo.itemText(1) == "webinar" and combo.currentData() == "webinar"
    assert any("Connect to the server to choose a note type." in w.text() for w in dialog.findChildren(QLabel))
    dialog.close()
    dialog = _dialog(cfg_path)
    assert ids(dialog.default_note_type_combo) == [""]
    dialog.close()


def test_settings_keeps_a_saved_id_the_server_no_longer_has(qt_app, cfg_path):
    dialog = _dialog(cfg_path, saved="old", types=TYPES, server_default="standard")
    combo = dialog.default_note_type_combo
    assert ids(combo)[-1] == "old" and "not found on the server" in combo.itemText(combo.count() - 1)
    assert combo.currentData() == "old"
    dialog.close()


def test_settings_saves_the_default_note_type(qt_app, cfg_path):
    dialog = _dialog(cfg_path, types=TYPES, server_default="quick")
    dialog.default_note_type_combo.setCurrentIndex(dialog.default_note_type_combo.findData("webinar"))
    dialog.accept()
    assert json.loads(cfg_path.read_text())["default_note_type"] == "webinar"
    assert config_mod.default_note_type_setting() == "webinar"

    dialog = SettingsDialog(note_types=TYPES, server_default_note_type="quick")
    assert dialog.default_note_type_combo.currentData() == "webinar"
    dialog.default_note_type_combo.setCurrentIndex(0)  # back to "Server default"
    dialog.accept()
    assert "default_note_type" not in json.loads(cfg_path.read_text())


def test_saving_settings_without_a_list_does_not_lose_the_saved_id(qt_app, cfg_path):
    dialog = _dialog(cfg_path, saved="webinar")
    dialog.accept()
    assert json.loads(cfg_path.read_text())["default_note_type"] == "webinar"


def test_the_window_hands_its_list_to_the_dialog_and_resets_the_picker(window, cfg_path, monkeypatch):
    from meeting_notes.client.ui import main_window as mw

    seen = {}

    class Dlg:
        def __init__(self, parent=None, page=None, **kw):
            seen.update(kw)

        def exec(self):
            cfg_path.write_text(json.dumps({"default_note_type": "webinar"}))
            return True

    monkeypatch.setattr(mw, "SettingsDialog", Dlg)
    monkeypatch.setattr(window, "_refresh_devices", lambda: None)
    window._open_settings()
    assert seen == {}  # no list known: the dialog gets nothing extra and says "Connect to the server"
    window._apply_note_types(TYPES, "quick")
    window._open_settings()
    assert seen["note_types"] == TYPES and seen["server_default_note_type"] == "quick"
    assert window.note_type_combo.currentData() == "webinar"  # idle: the new default shows at once
    # a recording in progress keeps whatever was chosen
    window._start()
    window.note_type_combo.setCurrentIndex(window.note_type_combo.findData("standard"))
    cfg_path.write_text(json.dumps({"default_note_type": "quick"}))
    window._open_settings()
    assert window.note_type_combo.currentData() == "standard"


# -- remote ------------------------------------------------------------------------------------


def test_the_snapshot_reports_the_picker_and_the_auto_end(window, cfg_path):
    state = window.build_remote_state()
    assert state["note_type"] is None and state["auto_end"] == {"mode": None, "label": None}
    assert state == remote.sanitize_state(state)
    window._apply_note_types(TYPES, "quick")
    assert window.build_remote_state()["note_type"] == "quick"
    window.note_type_combo.setCurrentIndex(window.note_type_combo.findData("webinar"))
    state = window.build_remote_state()
    assert state["note_type"] == "webinar" and state == remote.sanitize_state(state)


def _auto_record(window, cfg_path, mode="hour"):
    cfg_path.write_text(json.dumps({"meeting_detection": {"enabled": True, "auto_record": True, "auto_end": mode}}))
    window._apply_meeting_settings()
    window._detector.queue.append(STARTED)
    window._poll_meeting()
    assert window.controller.state == RECORDING


def test_the_snapshot_reports_the_auto_end_strip(window, cfg_path):
    _auto_record(window, cfg_path, "hour")
    state = window.build_remote_state()
    assert state["auto_end"] == {"mode": "hour", "label": "Auto end at 3:00 PM"}
    assert state == remote.sanitize_state(state)
    window._disable_auto_end()
    assert window.build_remote_state()["auto_end"] == {"mode": None, "label": None}


def test_the_snapshot_reports_a_silence_auto_end(window, cfg_path):
    _auto_record(window, cfg_path, "silence")
    assert window.build_remote_state()["auto_end"] == {"mode": "silence", "label": "Auto end after 30 seconds of silence"}


def test_the_snapshot_has_no_auto_end_for_manual_mode_or_a_manual_recording(window, cfg_path):
    _auto_record(window, cfg_path, "manual")
    assert window.build_remote_state()["auto_end"] == {"mode": None, "label": None}


def test_set_note_type_command_selects_the_type_idle_or_recording(window):
    window._apply_note_types(TYPES, "standard")
    assert window.execute_remote_command("set_note_type", {"note_type": "quick"}) == (True, None, None)
    assert window.note_type_combo.currentData() == "quick" and window.controller.note_type == "quick"
    assert window._toast.text() == "Note type changed from the server"
    window._start()
    assert window.execute_remote_command("set_note_type", {"note_type": "webinar"}) == (True, None, None)
    assert window.note_type_combo.currentData() == "webinar"
    window._stop()
    assert stopped(window)
    assert saved_meta(window)["note_type"] == "webinar"  # what the Recorders page picked is what was saved


def test_set_note_type_command_is_quiet_when_nothing_changes_and_refuses_unknown_ids(window):
    window._apply_note_types(TYPES, "standard")
    window._toast.hide()
    assert window.execute_remote_command("set_note_type", {"note_type": "standard"}) == (True, None, None)
    assert window._toast.isHidden()
    ok, code, error = window.execute_remote_command("set_note_type", {"note_type": "nope"})
    assert (ok, code) == (False, "bad_args") and "not known" in error
    assert window.note_type_combo.currentData() == "standard"
    assert window.execute_remote_command("set_note_type", {"note_type": "bad id"})[:2] == (False, "bad_args")
    assert window.execute_remote_command("set_note_type", {})[:2] == (False, "bad_args")


def test_set_note_type_command_without_a_known_list_is_refused(window):
    assert window.execute_remote_command("set_note_type", {"note_type": "quick"})[:2] == (False, "bad_args")


def test_set_note_type_command_is_refused_while_finishing(window):
    window._apply_note_types(TYPES, "standard")
    window._set_record_look("finishing")
    assert window.execute_remote_command("set_note_type", {"note_type": "quick"})[:2] == (False, "busy")
    window._set_record_look("idle")


def test_set_note_type_command_is_refused_when_control_is_off(window, cfg_path):
    cfg_path.write_text(json.dumps({"remote_control_allowed": False}))
    window._apply_note_types(TYPES, "standard")
    assert window.execute_remote_command("set_note_type", {"note_type": "quick"})[:2] == (False, "remote_control_disabled")
    assert window.note_type_combo.currentData() == "standard"


def test_disable_auto_end_command(window, cfg_path):
    assert window.execute_remote_command("disable_auto_end")[:2] == (False, "no_auto_end")  # idle
    _auto_record(window, cfg_path, "hour")
    assert not window.auto_end_bar.isHidden()
    assert window.execute_remote_command("disable_auto_end") == (True, None, None)
    assert window._auto_end_mode == "manual" and window.auto_end_bar.isHidden()
    assert window._toast.text() == "Auto end turned off from the server"
    assert window.execute_remote_command("disable_auto_end")[:2] == (False, "no_auto_end")  # nothing left to turn off


def test_disable_auto_end_is_refused_for_a_manual_recording_and_when_control_is_off(window, cfg_path):
    window._start()
    assert window.execute_remote_command("disable_auto_end")[:2] == (False, "no_auto_end")
    window._stop()
    assert stopped(window)
    _auto_record(window, cfg_path, "silence")
    cfg_path.write_text(json.dumps({"remote_control_allowed": False}))
    assert window.execute_remote_command("disable_auto_end")[:2] == (False, "remote_control_disabled")
    assert window._auto_end_mode == "silence"
