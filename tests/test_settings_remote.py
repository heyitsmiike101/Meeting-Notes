"""The app's Settings on the server's Recorders page (0.7.15): protocol, the recorder's side, the server's side and
the page itself.

Bottom to top: the protocol helpers (``remote.clean_command`` / ``sanitize_result``), the real ``ControlChannel``
carrying a settings result, the real ``MainWindow`` handling ``get_settings`` / ``set_settings``, the server route with
a fake recorder (and without the capability), the real client talking to the real server (the contract test with the
current client code) and the page (markup, CSS, and the panel's JavaScript run against a tiny fake DOM in node).
No audio hardware, no external service; everything stays on localhost.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List

import httpx
import pytest

from meeting_notes import config as config_mod
from meeting_notes import remote
from meeting_notes.client import settings_schema as S
from meeting_notes.server import web
from tests.test_recorders_server import Env, start, wait_until
from tests.test_recordings_remote import FakeRecorder, ResultChannel, _answered, _settle, ack
from tests.test_remote_client import (  # noqa: F401 - fixtures used by name
    CMD_ID,
    FakeChannel,
    _pump,
    _recording,
    _wait,
    make_channel,
    qt_app,
    server,
    window,
)

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

TOKEN = "SECRET-sign-in-token-9f3a"
NOTE_TYPES = [{"id": "standard", "name": "Standard"}, {"id": "quick", "name": "Quick notes"}]
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


@pytest.fixture(autouse=True)
def _isolated_client_config(tmp_path, monkeypatch):
    """Every test reads its own known client config, never the machine's: on a developer PC the payload
    otherwise came from the real ~/.meeting-notes/config.json (which has a server address), on the Mac build
    from an empty fake home, and a test that clears the address passed on one and failed on the other."""
    import json as _json

    cfg = tmp_path / "isolated-client-config.json"
    cfg.write_text(_json.dumps({"server": {"url": "http://meeting.example:8000", "token": "isolated-token"}}),
                   encoding="utf-8")
    monkeypatch.setenv("MEETING_NOTES_CONFIG", str(cfg))


def _payload(**over: Any) -> Dict[str, Any]:
    args = dict(note_types=NOTE_TYPES, server_default_note_type="standard",
                devices={"mic": ["USB Mic"], "system": ["Speakers"]}, stats={"count": 3, "bytes": 5000})
    args.update(over)
    return S.settings_payload(**args)


# =============================================================================
# 1. protocol
# =============================================================================


def test_the_settings_capability_is_known_and_fits_the_hello():
    assert remote.CAP_SETTINGS == "settings" and "settings" in remote.CAPS
    assert len(remote.CAPS) <= remote.MAX_CAPS
    assert remote.clean_caps(["idle_levels", "settings", "bogus", "settings"]) == ("idle_levels", "settings")


def test_get_settings_takes_no_arguments():
    assert remote.clean_command("get_settings", None) == ("get_settings", {})
    assert remote.clean_command("get_settings", {}) == ("get_settings", {})
    with pytest.raises(ValueError, match="unexpected argument: changes"):
        remote.clean_command("get_settings", {"changes": {"appearance": "dark"}})


def test_set_settings_changes_are_scalars_with_known_looking_keys():
    changes = {"appearance": "dark", "meeting_detection.auto_record": True, "local_retention_days": 30, "save_dir": "C:\\x"}
    assert remote.clean_command("set_settings", {"changes": changes}) == ("set_settings", {"changes": changes})


@pytest.mark.parametrize("args,message", [
    (None, "changes must be"),
    ({}, "changes must be"),
    ({"changes": {}}, "changes must be"),
    ({"changes": []}, "changes must be"),
    ({"changes": "appearance"}, "changes must be"),
    ({"changes": {"Appearance": "dark"}}, "invalid setting name"),
    ({"changes": {"a.b.c": "x"}}, "invalid setting name"),
    ({"changes": {"../x": "x"}}, "invalid setting name"),
    ({"changes": {"a" * 50: True}}, "invalid setting name"),
    ({"changes": {"appearance": None}}, "value must be"),
    ({"changes": {"appearance": 1.5}}, "value must be"),
    ({"changes": {"appearance": ["dark"]}}, "value must be"),
    ({"changes": {"appearance": {"a": 1}}}, "value must be"),
    ({"changes": {"save_dir": "x" * 1001}}, "value must be"),
    ({"changes": {"local_retention_days": 10**10}}, "value must be"),
    ({"changes": {f"k{i}": True for i in range(41)}}, "at most 40"),
    ({"changes": {"appearance": "dark"}, "extra": 1}, "unexpected argument: extra"),
])
def test_set_settings_rejects_malformed_changes(args, message):
    with pytest.raises(ValueError, match=message):
        remote.clean_command("set_settings", args)


def test_settings_results_are_sanitized_to_the_documented_shape():
    payload = _payload()
    assert remote.sanitize_result("get_settings", payload) == payload
    hostile = {
        "schema": {"pages": [{"key": "Bad Key", "title": "x", "items": []},
                             {"key": "general", "title": "G" * 500, "items": [
                                 {"type": "field", "key": "appearance", "kind": "evil", "label": "x"},
                                 {"type": "field", "key": "appearance", "kind": "bool", "label": "L" * 900, "help": "<b>x</b>",
                                  "choices": [[1, 2, 3], ["a", "b"] * 2, [{"x": 1}, "y"]] + [["v%d" % i, "l"] for i in range(40)],
                                  "show_if": [{"key": "bad key", "eq": 1}, {"key": "a", "eq": [1]}, {"key": "a", "eq": True}] * 5,
                                  "confirm": {"on": "never", "title": "t"}, "script": "alert(1)"},
                                 {"type": "section", "id": "../x", "title": "t"},
                                 {"type": "note", "id": "note.a", "text": "n" * 2000,
                                  "variants": [{"if": [], "text": "v"}] * 9},
                                 "junk",
                             ] + [{"type": "section", "id": f"section.s{i}", "title": "t"} for i in range(100)]}] * 30},
        "values": {"appearance": ["no"], "good": "yes", "Bad Key": 1, "n": 5, "f": 1.5, "t": "x" * 5000, "b": True},
        "devices": {"mic": ["a"] * 500 + [None, 5, "b\n c"], "system": "nope", "evil": ["x"]},
        "note_types": [{"id": "ok", "name": "N"}, {"id": "bad id", "name": "x"}, "junk", {"id": "x", "name": ""}] * 80,
        "server_default_note_type": "../../etc",
        "extra": "dropped",
    }
    clean = remote.sanitize_result("get_settings", hostile)
    assert set(clean) == {"schema", "values", "devices", "note_types", "server_default_note_type"}
    assert len(clean["schema"]["pages"]) == remote.MAX_SCHEMA_PAGES // 2  # only the first 12 raw pages are looked at; half of them are junk
    page = clean["schema"]["pages"][0]
    assert page["key"] == "general" and len(page["title"]) == 60 and len(page["items"]) <= remote.MAX_SCHEMA_ITEMS
    field = page["items"][0]
    assert field["kind"] == "bool" and field["label"] == "L" * 200 and "script" not in field and "confirm" not in field
    assert field["choices"] == [["v%d" % i, "l"] for i in range(13)]  # the first 16 raw entries, 3 of them junk
    assert field["show_if"] == [{"key": "a", "eq": True}] * 2 and "help" in field  # the first 6 raw conditions, 4 of them junk
    assert page["items"][1]["text"] == "n" * 800 and len(page["items"][1]["variants"]) == 4
    assert clean["values"] == {"good": "yes", "n": 5, "t": "x" * 1000, "b": True}
    assert clean["devices"]["mic"] == ["a"] * remote.MAX_DEVICES and clean["devices"]["system"] == []
    assert len(clean["note_types"]) == remote.MAX_NOTE_TYPES // 2 and clean["note_types"][1]["name"] == "x"
    assert clean["server_default_note_type"] == ""
    assert remote.sanitize_result("get_settings", None)["schema"] == {"pages": []}
    assert remote.sanitize_result("get_settings", "junk")["note_types"] == []


def test_a_set_settings_result_keeps_only_saved_errors_and_values():
    raw = {"saved": ["appearance", "Bad Key", 5], "errors": {"save_dir": "e" * 900, "Bad Key": "x"},
           "values": {"appearance": "dark", "server.token": True}, "extra": 1}
    assert remote.sanitize_result("set_settings", raw) == {
        "saved": ["appearance"], "errors": {"save_dir": "e" * 300}, "values": {"appearance": "dark", "server.token": True}}
    assert remote.sanitize_result("set_settings", None) == {"saved": [], "errors": {}, "values": {}}
    assert remote.sanitize_result("start", {"saved": []}) is None


def test_a_real_payload_fits_a_normal_ack_frame_with_room_to_spare():
    assert len(json.dumps(_payload(), separators=(",", ":"))) < remote.MAX_RESULT_BYTES / 10


# =============================================================================
# 2. the real ControlChannel
# =============================================================================


def test_the_hello_advertises_the_settings_capability(server, make_channel):
    make_channel()
    assert _wait(lambda: server.of_type("hello"))
    assert "settings" in server.of_type("hello")[0]["caps"]


def test_the_channel_delivers_a_settings_result_in_the_ack(server, make_channel):
    holder = {}
    payload = _payload()

    def on_command(command_id, name, args):
        holder["channel"].send_ack(command_id, True, result=payload if name == "get_settings" else
                                   {"saved": ["appearance"], "errors": {}, "values": {"appearance": "dark"}})

    holder["channel"] = make_channel(on_command=on_command)
    assert _wait(lambda: server.of_type("hello"))
    server.send_command("get_settings", {}, command_id="aaaaaaaa11111111")
    assert _wait(lambda: server.of_type("ack"))
    got = server.of_type("ack")[0]
    assert got["ok"] is True and got["result"] == payload
    server.send_command("set_settings", {"changes": {"appearance": "dark"}}, command_id="bbbbbbbb22222222")
    assert _wait(lambda: len(server.of_type("ack")) == 2)
    assert server.of_type("ack")[1]["result"] == {"saved": ["appearance"], "errors": {}, "values": {"appearance": "dark"}}


def test_a_malformed_set_settings_never_reaches_the_handler(server, make_channel):
    seen = []
    make_channel(on_command=lambda *a: seen.append(a))
    assert _wait(lambda: server.of_type("hello"))
    server.send_command("set_settings", {"changes": {"appearance": None}})
    assert _wait(lambda: server.of_type("ack"))
    ack_ = server.of_type("ack")[0]
    assert ack_["ok"] is False and ack_["code"] == "bad_args" and seen == []


# =============================================================================
# 3. the real MainWindow
# =============================================================================


@pytest.fixture
def swin(window, tmp_path, monkeypatch):
    """The window with a config of its own, a result-aware channel, note types, and nothing slow or external."""
    save = tmp_path / "Recordings"
    save.mkdir()
    (tmp_path / "config.json").write_text(json.dumps({
        "save_dir": str(save), "server": {"url": "", "token": TOKEN}, "appearance": "light"}))
    channel = ResultChannel(window._on_remote_command)
    window._remote = channel
    window.channel = channel
    window.async_calls = []
    window._run_async = lambda fn, done=None: window.async_calls.append(fn)  # restart_uploader etc. never run
    window._settings_device_lister = lambda: {"mic": ["USB Mic", "Webcam"], "system": ["Speakers"]}
    window._note_types = [dict(t) for t in NOTE_TYPES]
    window._server_default_note_type = "standard"
    from meeting_notes.client.ui import theme

    window.themes = []
    monkeypatch.setattr(theme, "apply_appearance", lambda value: window.themes.append(value))
    window.save = save
    return window


def _cfg(tmp_path) -> dict:
    return json.loads((tmp_path / "config.json").read_text())


def test_get_settings_answers_with_the_schema_values_devices_and_note_types(swin, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="meeting_notes.client.ui")
    seen = []
    swin._settings_device_lister = lambda: seen.append(threading.current_thread()) or {"mic": ["USB Mic"], "system": []}
    swin._on_remote_command(CMD_ID, "get_settings", {})
    (_cid, ok, code, error, result), = _answered(swin)
    assert (ok, code, error) == (True, None, None)
    assert result["schema"] == S.remote_schema(S.build_schema(note_types_known=True))
    assert result["values"]["appearance"] == "light" and result["values"]["server.token"] is True
    assert result["values"]["recordings.stats"] == "0 recordings, 0 B"
    assert result["devices"] == {"mic": ["USB Mic"], "system": []}
    assert result["note_types"] == NOTE_TYPES and result["server_default_note_type"] == "standard"
    assert seen and seen[0] is not threading.current_thread()  # the device scan is off the GUI thread
    assert "remote command: get_settings (source=server)" in caplog.text
    assert TOKEN not in json.dumps(result) and TOKEN not in caplog.text


def test_the_note_type_note_shows_when_the_app_has_no_note_types(swin):
    swin._note_types = []
    swin._on_remote_command(CMD_ID, "get_settings", {})
    result = _answered(swin)[0][4]
    ids = [i.get("id") for p in result["schema"]["pages"] for i in p["items"]]
    assert "note.note_type_offline" in ids


def test_get_settings_and_set_settings_are_refused_when_remote_control_is_off(swin, tmp_path):
    cfg = _cfg(tmp_path)
    cfg["remote_control_allowed"] = False
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    swin._on_remote_command(CMD_ID, "get_settings", {})
    swin._on_remote_command("0123456789abcde0", "set_settings", {"changes": {"appearance": "dark"}})
    results = _answered(swin, 2)
    assert [(r[1], r[2]) for r in results] == [(False, "remote_control_disabled")] * 2
    assert all(r[4] is None for r in results)
    assert _cfg(tmp_path)["appearance"] == "light"


def test_set_settings_saves_and_applies_like_the_dialogs_save(swin, tmp_path, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="meeting_notes.client")
    swin._on_remote_command(CMD_ID, "set_settings", {"changes": {
        "meeting_detection.enabled": False, "appearance": "dark", "server.url": "http://other.example:9000/",
        "show_audio_levels": False, "default_note_type": "quick", "audio_devices.mic": "Webcam"}})
    (_cid, ok, code, error, result), = _answered(swin)
    assert (ok, code, error) == (True, None, None)
    assert result["errors"] == {} and result["saved"] == sorted([
        "meeting_detection.enabled", "appearance", "server.url", "show_audio_levels", "default_note_type", "audio_devices.mic"])
    saved = _cfg(tmp_path)
    assert saved["appearance"] == "dark" and saved["server"]["url"] == "http://other.example:9000"
    assert saved["server"]["token"] == TOKEN and saved["meeting_detection"]["enabled"] is False
    assert saved["audio_devices"]["mic"] == "Webcam" and saved["default_note_type"] == "quick"
    # what the dialog's accept does afterwards
    assert swin.themes == ["dark"]
    assert swin._detect_settings["enabled"] is False
    assert swin._idle_levels_enabled is False
    assert swin.async_calls and swin.async_calls[0] == swin.controller.restart_uploader
    assert not swin.settings_button.isEnabled()           # until the uploader has restarted
    assert swin.channel.published                          # the server sees the new state at once
    assert result["values"]["appearance"] == "dark" and result["values"]["server.url"] == "http://other.example:9000"
    assert "remote command: set_settings (source=server) -> saved" in caplog.text
    assert TOKEN not in json.dumps(result) and TOKEN not in caplog.text


def test_the_server_can_turn_delete_after_upload_on_and_off(swin, tmp_path):
    swin._on_remote_command(CMD_ID, "get_settings", {})
    values = _answered(swin)[0][4]["values"]
    assert values["delete_after_upload"] is False
    fields = [i for p in _answered(swin)[0][4]["schema"]["pages"] for i in p["items"] if i.get("key") == "delete_after_upload"]
    assert fields and fields[0]["kind"] == "bool"
    swin._on_remote_command("0123456789abcde1", "set_settings", {"changes": {"delete_after_upload": True}})
    result = _answered(swin, 2)[1][4]
    assert result["saved"] == ["delete_after_upload"] and result["errors"] == {}
    assert _cfg(tmp_path)["delete_after_upload"] is True and config_mod.delete_after_upload() is True
    swin._on_remote_command("0123456789abcde2", "set_settings", {"changes": {"delete_after_upload": False}})
    assert _cfg(tmp_path)["delete_after_upload"] is False
    swin._on_remote_command("0123456789abcde3", "set_settings", {"changes": {"delete_after_upload": "yes"}})
    bad = _answered(swin, 4)[3][4]
    assert bad["saved"] == [] and set(bad["errors"]) == {"delete_after_upload"}
    assert _cfg(tmp_path)["delete_after_upload"] is False


def test_set_settings_that_changes_nothing_does_not_restart_anything(swin):
    swin._on_remote_command(CMD_ID, "set_settings", {"changes": {"appearance": "light"}})
    (_cid, ok, _c, _e, result), = _answered(swin)
    assert ok is True and result["saved"] == [] and swin.async_calls == [] and swin.themes == []


def test_set_settings_refuses_everything_when_one_value_is_bad(swin, tmp_path):
    swin._on_remote_command(CMD_ID, "set_settings", {"changes": {"appearance": "dark", "local_retention_days": 14,
                                                                  "server.token": "stolen"}})
    (_cid, ok, code, error, result), = _answered(swin)
    assert (ok, code) == (False, "invalid_settings") and error
    assert result["saved"] == [] and set(result["errors"]) == {"local_retention_days", "server.token"}
    assert result["errors"]["server.token"] == S.NOT_REMOTE
    assert _cfg(tmp_path)["appearance"] == "light" and _cfg(tmp_path)["server"]["token"] == TOKEN
    assert swin.async_calls == [] and swin.themes == []


def test_set_settings_validates_the_save_folder_like_the_dialog(swin, tmp_path):
    blocker = tmp_path / "afile"
    blocker.write_text("x")
    swin._on_remote_command(CMD_ID, "set_settings", {"changes": {"save_dir": str(blocker / "sub")}})
    result = _answered(swin)[0]
    assert result[1] is False and "save_dir" in result[4]["errors"]
    good = tmp_path / "New" / "Folder"
    swin._on_remote_command("0123456789abcde1", "set_settings", {"changes": {"save_dir": str(good)}})
    assert _answered(swin, 2)[1][4]["saved"] == ["save_dir"] and good.is_dir()


def test_a_note_type_the_app_does_not_know_is_refused(swin):
    swin._on_remote_command(CMD_ID, "set_settings", {"changes": {"default_note_type": "mystery"}})
    result = _answered(swin)[0]
    assert result[1] is False and "default_note_type" in result[4]["errors"]


def test_set_settings_is_refused_while_the_local_settings_dialog_is_open(swin, tmp_path):
    swin._settings_dialog = object()
    swin._on_remote_command(CMD_ID, "set_settings", {"changes": {"appearance": "dark"}})
    (_cid, ok, code, _e, _r), = _answered(swin)
    assert (ok, code) == (False, "busy") and _cfg(tmp_path)["appearance"] == "light"
    swin._settings_dialog = None
    swin._on_remote_command("0123456789abcde2", "get_settings", {})  # reading is fine while it is open
    assert _answered(swin, 2)[1][1] is True


def test_settings_can_be_changed_while_recording_just_like_the_dialog(swin, tmp_path):
    _recording(swin, tmp_path)
    swin._on_remote_command(CMD_ID, "set_settings", {"changes": {"appearance": "dark", "audio_devices.mic": "Webcam"}})
    (_cid, ok, _c, _e, result), = _answered(swin)
    assert ok is True and result["saved"] == ["appearance", "audio_devices.mic"]
    assert swin.controller.state == "recording"  # nothing was stopped; a recording keeps its devices


def test_turning_remote_control_off_is_answered_first_and_written_after(swin, tmp_path):
    swin._on_remote_command(CMD_ID, "set_settings", {"changes": {"remote_control_allowed": False, "appearance": "dark"}})
    (_cid, ok, _c, _e, result), = _answered(swin)
    assert ok is True and result["saved"] == ["appearance", "remote_control_allowed"]
    assert result["values"]["remote_control_allowed"] is False
    assert config_mod.remote_control_allowed(_cfg(tmp_path)) is True        # not yet: the answer goes out first
    assert _pump(lambda: config_mod.remote_control_allowed(_cfg(tmp_path)) is False)
    assert _pump(lambda: swin.channel.published and swin.channel.published[-1]["control"] == {"allowed": False})
    swin._on_remote_command("0123456789abcde3", "get_settings", {})
    assert _answered(swin, 2)[1][2] == "remote_control_disabled"


def test_turning_it_off_does_nothing_when_another_value_is_bad(swin, tmp_path):
    swin._on_remote_command(CMD_ID, "set_settings", {"changes": {"remote_control_allowed": False, "appearance": "nope"}})
    assert _answered(swin)[0][1] is False
    _settle()
    time.sleep(0.4)
    _settle()
    assert config_mod.remote_control_allowed(_cfg(tmp_path)) is True


def test_a_command_from_the_channel_thread_reaches_the_settings_handler(swin):
    threading.Thread(target=swin.channel.on_command, args=(CMD_ID, "get_settings", {})).start()
    assert _answered(swin)[0][1] is True


# =============================================================================
# 4. the server route with a fake recorder
# =============================================================================


@pytest.fixture()
def env(tmp_path, monkeypatch):
    server_, e = start(tmp_path, monkeypatch)
    e.hub.command_timeout = 0.5
    yield e
    server_.stop()


def _post(env: Env, iid: str, command: str, args: dict) -> httpx.Response:
    with env.web() as http:
        return http.post(f"/v1/recorders/{iid}/commands", json={"command": command, "args": args})


def test_a_recorder_with_the_capability_is_listed_with_it_and_answers_get_settings(env):
    payload = _payload()
    rec = FakeRecorder(env, lambda cmd, args, _cid: ack(payload) if cmd == "get_settings" else None, caps=["settings"])
    try:
        item = wait_until(lambda: next((i for i in env.hub.list_items() if i["instance_id"] == rec.instance_id), None))
        assert "settings" in item["caps"]
        res = _post(env, rec.instance_id, "get_settings", {})
        assert res.status_code == 200 and res.json()["ok"] is True
        assert res.json()["result"] == payload
        assert rec.seen == [("get_settings", {})]
    finally:
        rec.close()


def test_set_settings_is_forwarded_with_its_changes_and_the_result_comes_back(env):
    result = {"saved": ["appearance"], "errors": {}, "values": {"appearance": "dark"}}
    rec = FakeRecorder(env, lambda cmd, args, _cid: ack(result), caps=["settings"])
    try:
        wait_until(lambda: env.hub.get(rec.instance_id))
        res = _post(env, rec.instance_id, "set_settings", {"changes": {"appearance": "dark"}})
        assert res.status_code == 200 and res.json()["result"] == result
        assert rec.seen == [("set_settings", {"changes": {"appearance": "dark"}})]
    finally:
        rec.close()


def test_a_refused_save_carries_its_errors(env):
    refusal = {"saved": [], "errors": {"save_dir": "That is not a folder."}, "values": {"save_dir": "C:\\r"}}
    rec = FakeRecorder(env, lambda *_a: ack(refusal, ok=False, code="invalid_settings", error="Some settings could not be saved."),
                       caps=["settings"])
    try:
        wait_until(lambda: env.hub.get(rec.instance_id))
        body = _post(env, rec.instance_id, "set_settings", {"changes": {"save_dir": "x"}}).json()
        assert body["ok"] is False and body["code"] == "invalid_settings" and body["result"] == refusal
    finally:
        rec.close()


def test_bad_set_settings_requests_are_400_and_never_reach_the_recorder(env):
    rec = FakeRecorder(env, lambda *_a: ack({}), caps=["settings"])
    try:
        wait_until(lambda: env.hub.get(rec.instance_id))
        for args in ({}, {"changes": {}}, {"changes": {"appearance": None}}, {"changes": {"Bad Key": 1}}):
            assert _post(env, rec.instance_id, "set_settings", args).status_code == 400
        assert rec.seen == []
    finally:
        rec.close()


def test_a_recorder_without_the_capability_is_never_sent_a_settings_command(env):
    rec = FakeRecorder(env, lambda *_a: ack({}))        # an older app: no caps in its hello
    try:
        item = wait_until(lambda: next((i for i in env.hub.list_items() if i["instance_id"] == rec.instance_id), None))
        assert "settings" not in item["caps"]
        for command, args in (("get_settings", {}), ("set_settings", {"changes": {"appearance": "dark"}})):
            res = _post(env, rec.instance_id, command, args)
            assert res.status_code == 400 and "too old" in res.json()["detail"]
        assert rec.seen == []
        assert _post(env, rec.instance_id, "refresh_devices", {}).status_code == 200   # its other commands still work
    finally:
        rec.close()


def test_settings_commands_wait_longer_than_ordinary_ones(env):
    assert remote.TIMEOUT_FACTOR["get_settings"] > 1 and remote.TIMEOUT_FACTOR["set_settings"] > 1
    rec = FakeRecorder(env, lambda *_a: None, caps=["settings"])   # never answers
    try:
        wait_until(lambda: env.hub.get(rec.instance_id))
        started = time.monotonic()
        assert _post(env, rec.instance_id, "get_settings", {}).status_code == 504
        assert time.monotonic() - started >= env.hub.command_timeout * 2
    finally:
        rec.close()


def test_a_hostile_recorder_cannot_push_unbounded_settings_through_the_server(env):
    # as much as one ack frame may carry (remote.MAX_ACK_FRAME_BYTES), all of it over the limits
    huge = {"schema": {"pages": [{"key": "general", "title": "x", "items": [
        {"type": "note", "id": "note.n%d" % i, "text": "t" * 200} for i in range(100)]}] * 12},
        "values": {"k%d" % i: "v" * 2000 for i in range(60)}, "devices": {"mic": ["d"] * 5000}, "note_types": []}
    assert len(json.dumps(huge)) < remote.MAX_RESULT_BYTES
    rec = FakeRecorder(env, lambda *_a: ack(huge), caps=["settings"])
    try:
        wait_until(lambda: env.hub.get(rec.instance_id))
        result = _post(env, rec.instance_id, "get_settings", {}).json()["result"]
        assert len(result["schema"]["pages"]) <= remote.MAX_SCHEMA_PAGES
        assert all(len(p["items"]) <= remote.MAX_SCHEMA_ITEMS for p in result["schema"]["pages"])
        assert len(json.dumps(result)) < 400_000
    finally:
        rec.close()


# =============================================================================
# 5. the current client against the current server (contract test)
# =============================================================================


def test_the_current_client_and_server_round_trip_settings_through_the_real_channel(swin, tmp_path, monkeypatch):
    """The real ``ControlChannel`` carrying a real ``MainWindow`` to the real server: the capability shows in the
    Recorders list, ``get_settings`` returns the dialog's schema, ``set_settings`` changes the app's config, a refused
    save changes nothing, and the sign-in token is in none of it."""
    from meeting_notes.client.control_channel import ControlChannel

    (tmp_path / "srv").mkdir()
    server_, env_ = start(tmp_path / "srv", monkeypatch)
    env_.hub.command_timeout = 3.0
    iid = uuid.uuid4().hex
    channel = ControlChannel(lambda: (env_.base, ""), swin._remote_command_from_thread, instance_id=iid, device="Desk PC",
                             backoff_initial=0.05, backoff_max=0.2, idle_poll=0.05)
    swin._remote = channel
    channel.publish(swin.build_remote_state())
    channel.start()
    sent: list = []

    def call(command: str, args: dict) -> dict:
        out: Dict[str, Any] = {}

        def run() -> None:
            with env_.web() as http:
                res = http.post(f"/v1/recorders/{iid}/commands", json={"command": command, "args": args}, timeout=15)
                out["status"], out["body"] = res.status_code, res.json()
                sent.append(res.text)

        t = threading.Thread(target=run)
        t.start()
        assert _pump(lambda: not t.is_alive(), timeout=15), "the command never came back"
        return out

    try:
        assert _pump(lambda: env_.hub.get(iid) is not None, timeout=10), "the recorder never connected"
        assert "settings" in env_.hub.get(iid).caps
        got = call("get_settings", {})
        assert got["status"] == 200 and got["body"]["ok"] is True
        result = got["body"]["result"]
        assert result["schema"] == S.remote_schema(S.build_schema(note_types_known=True))
        assert result["devices"] == {"mic": ["USB Mic", "Webcam"], "system": ["Speakers"]}
        assert [t["id"] for t in result["note_types"]] == ["standard", "quick"]

        done = call("set_settings", {"changes": {"appearance": "dark", "meeting_detection.suggest_stop": False}})
        assert done["body"]["ok"] is True
        assert done["body"]["result"]["saved"] == ["appearance", "meeting_detection.suggest_stop"]
        saved = _cfg(tmp_path)
        assert saved["appearance"] == "dark" and saved["meeting_detection"]["suggest_stop"] is False

        bad = call("set_settings", {"changes": {"appearance": "light", "local_retention_days": 14}})
        assert bad["body"]["ok"] is False and bad["body"]["code"] == "invalid_settings"
        assert set(bad["body"]["result"]["errors"]) == {"local_retention_days"}
        assert _cfg(tmp_path)["appearance"] == "dark"

        stolen = call("set_settings", {"changes": {"server.token": "x"}})
        assert stolen["body"]["result"]["errors"]["server.token"] == S.NOT_REMOTE and _cfg(tmp_path)["server"]["token"] == TOKEN
        assert not any(TOKEN in text for text in sent)
    finally:
        channel.stop(join_timeout=2.0)
        swin._remote = None
        server_.stop()


# =============================================================================
# 6. the page
# =============================================================================


def _page() -> str:
    return web.render_recorders_page(token_configured=False)


def test_the_page_has_the_settings_panel_and_keeps_the_old_note_for_older_apps():
    page = _page()
    for needle in ('<dialog class="dialog rec-panel rs-panel" id="set-panel" aria-labelledby="rs-title">',
                   'id="rs-nav" role="tablist"', 'id="rs-page"', 'id="rs-save"', 'id="rs-cancel"', 'id="rs-retry"',
                   'aria-label="Close settings"', 'role="status" aria-live="polite"'):
        assert needle in page, needle
    # the links stay, in the panel's footer and in the popover an older app still gets
    assert page.count('href="/settings#settings-recorders-heading"') >= 2 and page.count('href="/settings#settings-logs-heading"') >= 3
    js = web._RECORDERS_JS
    assert "can only be changed on the computer it runs on" in js                       # the old note, for an app without the cap
    assert "rsSupported(own.item)" in js and "rsOpen(owner.dataset.id, toggle)" in js   # the button opens the panel only with the cap
    assert "recTogglePop(owner, toggle.dataset.pop)" in js
    assert "'settings') >= 0" in js or "indexOf('settings')" in js


def test_the_panel_markup_is_static_and_all_text_goes_in_as_text():
    js = web._RECORDERS_JS
    block = js[js.index("/* ---- Settings panel"): js.index("function recInit()")]
    assert not re.search(r"\.innerHTML\s*=", block), "the settings panel must not write markup"
    assert "insertAdjacentHTML" not in block and "document.write" not in block and "eval(" not in block
    assert "outerHTML" not in block
    for fn in ("rsBuild", "rsBuildItem", "rsApplyRules", "rsSave", "rsLoad", "rsOnFrame", "rsRequestClose"):
        assert f"function {fn}(" in block
    # the commands it sends
    assert "recPost(id, 'get_settings', {})" in block and "recPost(id, 'set_settings', {changes: changes})" in block
    # innerHTML is still only ever given a template or an icon anywhere in the page script
    for expr in re.findall(r"\.innerHTML\s*=\s*([^;]+);", js):
        assert re.fullmatch(r"REC_CARD_HTML|REC_ROW_HTML|icon\([^)]*\)", expr.strip()), expr


def test_the_panel_css_uses_tokens_works_at_phone_width_and_in_dark_mode():
    css = web.stylesheet_text()
    start = css.index("/* Settings panel")
    block = css[start: css.index("@media (max-width:1100px)", start)]
    assert ".rs-main {" in block and ".rs-nav {" in block and ".rs-field {" in block
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", block), "hard-coded colour"
    assert "box-shadow" not in block and "rgb(" not in block
    assert set(re.findall(r"var\((--[a-z0-9-]+)\)", block)) <= set(re.findall(r"(--[a-z0-9-]+):", css)), "unknown token"
    phone = css[css.index("@media (max-width:860px)"): css.index("@media (prefers-reduced-motion")]
    for rule in (".rs-main { grid-template-columns:minmax(0,1fr)", ".rs-nav { flex-direction:row", ".rs-field { grid-template-columns:minmax(0,1fr)",
                 ".rs-note { margin-left:0"):
        assert rule in phone, rule
    assert "min-height:44px" in phone[phone.index(".rs-tab"):][:140] and ".rs-buttons .btn { flex:1 1 0; min-height:44px" in phone
    assert "[hidden]" in block   # hidden rows really disappear (display:grid would otherwise override it)


def test_the_page_renders_for_the_route(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from meeting_notes.server.app import create_app

    monkeypatch.setenv("MEETING_NOTES_TOKEN", "t0k")
    with TestClient(create_app(data_root=str(tmp_path / "d"))) as c:
        page = c.get("/recorders", headers={"Authorization": "Bearer t0k"})
    assert page.status_code == 200 and 'id="set-panel"' in page.text and "function rsSave(" in page.text


# -- the panel's JavaScript, against a tiny fake DOM --------------------------------------------------------

FAKE_DOM = r"""
class FakeEl {
  constructor(tag) { this.tagName = String(tag).toUpperCase(); this.children = []; this.attrs = {}; this.dataset = {}; this.listeners = {};
    this._text = ''; this.hidden = false; this.className = ''; this.value = ''; this.checked = false; this.disabled = false; this.parent = null;
    var self = this;
    this.classList = {toggle: function (c, on) { var set = self.className.split(/\s+/).filter(Boolean).filter(function (x) { return x !== c; }); if (on) set.push(c); self.className = set.join(' '); }};
  }
  get textContent() { return this.children.length ? this.children.map(function (c) { return c.textContent; }).join('') : this._text; }
  set textContent(v) { this.children = []; this._text = String(v); }
  appendChild(c) { this.children.push(c); c.parent = this; return c; }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k === 'id') this.id = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  removeAttribute(k) { delete this.attrs[k]; }
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
  contains(n) { return n === this || this.children.some(function (c) { return c.contains(n); }); }
  fire(t) { var self = this; (this.listeners[t] || []).forEach(function (f) { f({target: self}); }); }
}
var document = {createElement: function (t) { return new FakeEl(t); }, getElementById: function () { return null; }, body: {contains: function () { return true; }}};
"""

HARNESS = r"""
const vm = require('vm'), fs = require('fs');
const ctx = {window: {MN_ICONS: {}}, console, Promise};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), ctx);
const out = vm.runInContext(fs.readFileSync(process.argv[3], 'utf8'), ctx);
process.stdout.write(JSON.stringify(out));
"""


def _run_node(tmp_path: Path, prelude: str, checks: str) -> Any:
    script = tmp_path / "page.js"
    script.write_text(web._JS_HELPERS + FAKE_DOM + web._RECORDERS_JS + prelude, encoding="utf-8")
    check = tmp_path / "checks.js"
    check.write_text(checks, encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    done = subprocess.run([NODE, str(harness), str(script), str(check)], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def _expected_rows(schema: Dict[str, Any], values: Dict[str, Any]) -> Dict[str, List[str]]:
    """What a page must show for ``values``: section titles, shown notes and shown fields' labels, in order."""
    out: Dict[str, List[str]] = {}
    for page in schema["pages"]:
        rows = []
        for item in page["items"]:
            if not S.is_shown(item, values):
                continue
            if item["type"] == "section":
                rows.append(item["title"])
            elif item["type"] == "note":
                if S.note_text(item, values):
                    rows.append(S.note_text(item, values))
            else:
                rows.append(item["label"])
        out[page["key"]] = rows
    return out


PANEL_SETUP = r"""
var schema = %(schema)s, values = %(values)s;
function mkUi() {
  var u = {};
  ['dlg','title','summary','close','alert','alertIc','alertText','main','nav','body','page','state','stateTitle','stateText','retry','status','cancel','save','saveText'].forEach(function (k) { u[k] = new FakeEl('div'); });
  u.alertIc.innerHTML = ''; return u;
}
rsUi = mkUi();
function mkPanel(over) {
  var P = Object.assign({id: 'x', device: 'Desk PC', schema: schema, values: Object.assign({}, values), draft: Object.assign({}, values),
    devices: {mic: ['USB Mic', '<img src=x onerror=alert(1)>'], system: ['Speakers']}, types: [{id: 'standard', name: 'Standard'}, {id: 'quick', name: 'Quick notes'}],
    dflt: 'quick', els: {}, pages: {}, page: 'general', errors: {}, loaded: false, loading: false, saving: false, offline: false, locked: false,
    error: '', status: '', statusKind: ''}, over || {});
  rs = P; rsBuild(P); rsSyncControls(P); P.loaded = true; rsRender();
  return P;
}
function rowText(row) {
  if (row.className.indexOf('rs-section') >= 0 || row.className.indexOf('rs-note') >= 0) return row.textContent;
  var first = row.children[0].textContent;
  return first || row.children[1].children[0].children[1].textContent;   // a checkbox row: the label is beside the box
}
function shown(P) {
  var out = {};
  Object.keys(P.pages).forEach(function (k) {
    out[k] = P.pages[k].sec.children.slice(1).filter(function (r) { return !r.hidden; }).map(rowText);
  });
  return out;
}
"""


def _panel_setup() -> str:
    payload = _payload()
    return PANEL_SETUP % {"schema": json.dumps(payload["schema"]), "values": json.dumps(payload["values"])}


@needs_node
def test_the_panel_draws_the_schema_page_by_page_in_order(tmp_path):
    payload = _payload()
    got = _run_node(tmp_path, _panel_setup(), "(function () { var P = mkPanel(); return {tabs: P.schema.pages.map(function (p) { return P.pages[p.key].tab.textContent; }), shown: shown(P)}; })()")
    assert got["tabs"] == ["General", "Audio", "Recordings", "Server", "GPU helper", "Remote control", "About"]
    assert got["shown"] == _expected_rows(payload["schema"], payload["values"])
    # spot checks of the client's own wording
    assert got["shown"]["general"][:4] == ["Appearance", "Theme", "Meeting detection", "Offer to record Teams, Zoom and Google Meet calls"]
    assert "Stop when the meeting closes" in got["shown"]["general"]
    assert "Also end" not in got["shown"]["general"]  # auto record is off


@needs_node
def test_the_panels_visibility_rules_match_the_python_ones_for_every_combination(tmp_path):
    payload = _payload()
    cases = []
    for enabled in (True, False):
        for auto in (True, False):
            for end in ("manual", "hour", "silence", "bye"):
                for live in (True, False):
                    cases.append({"meeting_detection.enabled": enabled, "meeting_detection.auto_record": auto,
                                  "meeting_detection.auto_end": end, "server.live_preview": live})
    checks = "(function () { var out = []; %s.forEach(function (c) { var P = mkPanel({draft: Object.assign({}, values, c)}); var rec = P.els['meeting_detection.auto_record'].input;" \
             " out.push({shown: shown(P), recordDisabled: rec.disabled}); }); return out; })()" % json.dumps(cases)
    got = _run_node(tmp_path, _panel_setup(), checks)
    assert len(got) == 32
    for case, result in zip(cases, got):
        values = {**payload["values"], **case}
        assert result["shown"] == _expected_rows(payload["schema"], values), case
        assert result["recordDisabled"] is (not case["meeting_detection.enabled"]), case


@needs_node
def test_the_panels_option_lists_match_the_dialogs(tmp_path):
    items = S.items_by_id(S.build_schema())
    cases = [
        ("device", "audio_devices.mic", [["USB Mic", "Webcam"], "Old Mic"]),
        ("device", "audio_devices.mic", [["USB Mic"], ""]),
        ("device", "audio_devices.system", [[], "$&Gone"]),
        ("note", "default_note_type", [NOTE_TYPES, "quick", ""]),
        ("note", "default_note_type", [NOTE_TYPES, "missing", "missing"]),
        ("note", "default_note_type", [[], "", "ghost"]),
    ]
    expected, calls = [], []
    for kind, key, args in cases:
        if kind == "device":
            expected.append([list(p) for p in S.device_options(items[key], args[0], args[1])])
            calls.append("rsDeviceOptions(items[%s], %s, %s)" % (json.dumps(key), json.dumps(args[0]), json.dumps(args[1])))
        else:
            expected.append([list(p) for p in S.note_type_options(items[key], args[0], args[1], args[2])])
            calls.append("rsNoteTypeOptions(items[%s], %s, %s, %s)" % (json.dumps(key), json.dumps(args[0]), json.dumps(args[1]), json.dumps(args[2])))
    prelude = "var items = {}; var sch = %s; sch.pages.forEach(function (p) { p.items.forEach(function (i) { if (i.key) items[i.key] = i; }); });" % json.dumps(S.remote_schema(S.build_schema()))
    checks = "[%s].map(function (opts) { return opts.map(function (o) { return [o.value, o.label]; }); })" % ", ".join(calls)
    assert _run_node(tmp_path, prelude, checks) == expected


@needs_node
def test_editing_the_panel_gives_the_changes_the_page_would_send(tmp_path):
    checks = r"""(function () {
      var P = mkPanel();
      var none = rsChanges(P);
      P.els['server.url'].input.value = ' http://other:9000/ '; P.els['server.url'].input.fire('input');
      P.els['meeting_detection.enabled'].input.checked = false; P.els['meeting_detection.enabled'].input.fire('change');
      var sel = P.els['appearance'].input; sel.value = '2'; sel.fire('change');                      // Dark
      var retention = P.els['local_retention_days'].input; retention.value = '3'; retention.fire('change');   // 90 days
      var mic = P.els['audio_devices.mic'].input; mic.value = '2'; mic.fire('change');                // the hostile device name
      var changes = rsChanges(P);
      var asks = rsConfirms(P, changes);
      P.els['remote_control_allowed'].input.checked = false; P.els['remote_control_allowed'].input.fire('change');
      var asks2 = rsConfirms(P, rsChanges(P));
      P.els['server.url'].input.value = ''; P.els['server.url'].input.fire('input');
      var asks3 = rsConfirms(P, rsChanges(P));
      return {none: none, changes: changes, asks: asks, asks2: asks2.map(function (a) { return a.title; }), emptyLead: asks3[0].lead,
              saveEnabled: !rsUi.save.disabled, secret: P.els['server.token'].stat.textContent, version: P.els['about.version'].stat.textContent,
              hostile: P.els['audio_devices.mic'].input.children.map(function (o) { return o.textContent; })};
    })()"""
    got = _run_node(tmp_path, _panel_setup(), checks)
    assert got["none"] == {}
    assert got["changes"] == {"server.url": " http://other:9000/ ", "meeting_detection.enabled": False, "appearance": "dark",
                              "local_retention_days": 90, "audio_devices.mic": "<img src=x onerror=alert(1)>"}
    assert [a["title"] for a in got["asks"]] == ["Change the server address?"]
    assert got["asks"][0]["lead"] == "This computer will reconnect to  http://other:9000/ ; if it is wrong the recorder will drop off this page."
    assert got["asks"][0]["danger"] is False and got["asks"][0]["confirmLabel"] == "Change address"
    assert got["asks2"] == ["Change the server address?", "Turn off remote control?"]
    assert got["emptyLead"] == "This computer will reconnect to no server; if it is wrong the recorder will drop off this page."
    assert got["saveEnabled"] is True
    assert got["secret"] == "Set" and got["version"]
    assert "<img src=x onerror=alert(1)>" in got["hostile"]  # shown as text, in an option


@needs_node
def test_the_panel_blocks_editing_when_the_recorder_is_offline_locked_or_saving(tmp_path):
    checks = r"""(function () {
      var out = {};
      ['offline', 'locked', 'saving'].forEach(function (why) {
        var P = mkPanel(); P[why] = true; rsRender();
        P.els['appearance'].input.value = '1'; P.draft.appearance = 'light'; P.draft.appearance = 'dark';
        rsRender();
        out[why] = {inputDisabled: P.els['appearance'].input.disabled, saveDisabled: rsUi.save.disabled, alert: rsUi.alert.hidden ? '' : rsUi.alertText.textContent};
      });
      var P = mkPanel({loaded: false}); P.loaded = false; P.locked = true; rsRender();
      out.lockedState = {title: rsUi.stateTitle.textContent, nav: rsUi.nav.hidden};
      var Q = mkPanel(); Q.errors = {'save_dir': 'That is not a folder.'}; rsRender();
      out.error = {text: Q.els['save_dir'].err.textContent, hidden: Q.els['save_dir'].err.hidden, invalid: Q.els['save_dir'].input.attrs['aria-invalid'],
                   tabMarked: Q.pages.recordings.tab.className.indexOf('has-error') >= 0};
      return out;
    })()"""
    got = _run_node(tmp_path, _panel_setup(), checks)
    for why in ("offline", "locked", "saving"):
        assert got[why]["inputDisabled"] is True and got[why]["saveDisabled"] is True, why
    assert "went offline" in got["offline"]["alert"] and "turned off" in got["locked"]["alert"]
    assert got["lockedState"]["title"] == "Remote control is turned off" and got["lockedState"]["nav"] is True
    assert got["error"] == {"text": "That is not a folder.", "hidden": False, "invalid": "true", "tabMarked": True}
