"""The Settings schema, its parity with the real dialog, and the shared save (0.7.15).

The dialog and the server's Recorders page draw the same schema (``client/settings_schema.py``). These tests walk the
real ``SettingsDialog`` so a field added to one screen and not the other fails the suite, and exercise the validation
and the save that the dialog's Save and the server's remote Save share. Headless (Qt offscreen); no network.
"""

from __future__ import annotations

import itertools
import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6", reason="PySide6 not installed")

from PySide6.QtWidgets import QApplication, QCheckBox, QFormLayout, QLabel  # noqa: E402

from meeting_notes import config as config_mod  # noqa: E402
from meeting_notes import remote  # noqa: E402
from meeting_notes.client import settings_schema as S  # noqa: E402
from meeting_notes.client.ui import settings_dialog as sd  # noqa: E402
from meeting_notes.client.ui.theme import APP_STYLE  # noqa: E402

TOKEN = "SECRET-sign-in-token-9f3a"
NOTE_TYPES = [{"id": "standard", "name": "Standard"}, {"id": "quick", "name": "Quick notes"}]


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(APP_STYLE)
    return app


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_NOTES_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("MEETING_NOTES_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    # The theme is process-wide: a test must not restyle the whole run.
    from meeting_notes.client.ui import theme

    monkeypatch.setattr(theme, "apply_appearance", lambda *_a, **_k: None)
    return tmp_path


def _dialog(**kwargs):
    dialog = sd.SettingsDialog(**kwargs)
    dialog.folder_stats = lambda folder: {"count": 0, "bytes": 0}  # no disk scan
    return dialog


def _write(home: Path, data: dict) -> None:
    (home / "config.json").write_text(json.dumps(data), encoding="utf-8")


def _read(home: Path) -> dict:
    return json.loads((home / "config.json").read_text(encoding="utf-8"))


# -- the schema itself -----------------------------------------------------------------------------


def test_every_field_names_a_known_kind_and_has_what_its_kind_needs():
    schema = S.build_schema()
    seen = set()
    for item in S.page_items(schema):
        assert S.item_id(item) not in seen, "ids must be unique"
        seen.add(S.item_id(item))
        if item["type"] != "field":
            continue
        assert item["kind"] in S.KINDS
        assert remote.valid_setting_key(item["key"])
        assert item["label"]
        if item["kind"] == "choice":
            assert item["choices"] and all(len(c) == 2 for c in item["choices"])
        if item["kind"] == "device":
            assert item["device_kind"] in ("mic", "system") and item["first_label"] and "{name}" in item["missing_label"]
        if item["kind"] == "note_type":
            assert item["first_label"] and "{name}" in item["first_label_named"] and "{id}" in item["missing_label"]


def test_pages_are_in_the_dialogs_order_and_the_remote_schema_drops_the_dialog_only_ones():
    schema = S.build_schema()
    assert [p["key"] for p in schema["pages"]] == ["general", "audio", "recordings", "server", "remote", "logs", "about"]
    assert [p["title"] for p in schema["pages"]] == [
        "General", "Audio", "Recordings", "Server", "Remote control", "Logs", "About"]
    assert [p["key"] for p in S.remote_schema(schema)["pages"]] == ["general", "audio", "recordings", "server", "remote", "about"]
    assert [p["key"] for p in schema["pages"] if p.get("local_only")] == ["logs"]


def test_the_remote_schema_survives_the_servers_sanitizer_unchanged():
    """What the app builds is exactly what the server keeps: no field, text or rule is lost on the way."""
    for platform, known in (("win32", True), ("darwin", False)):
        remote_schema = S.remote_schema(S.build_schema(platform=platform, note_types_known=known))
        assert remote.sanitize_settings_schema(remote_schema) == remote_schema
    assert len(json.dumps(remote_schema)) < 30_000


def test_macos_gets_its_speaker_note_and_windows_does_not():
    mac = S.items_by_id(S.build_schema(platform="darwin"))
    win = S.items_by_id(S.build_schema(platform="win32"))
    assert "note.devices_mac" in mac and "note.devices_mac" not in win
    assert mac["audio_devices.system"]["disable_if_empty"] is True and "disable_if_empty" not in win["audio_devices.system"]


def test_the_note_type_note_appears_only_when_the_note_types_are_unknown():
    assert "note.note_type_offline" in S.items_by_id(S.build_schema(note_types_known=False))
    assert "note.note_type_offline" not in S.items_by_id(S.build_schema(note_types_known=True))


def test_conditions_and_note_variants():
    values = {"a": True, "b": "x"}
    assert S.conditions_hold(None, values) and S.conditions_hold([], values)
    assert S.conditions_hold([{"key": "a", "eq": True}, {"key": "b", "ne": "y"}], values)
    assert not S.conditions_hold([{"key": "a", "eq": False}], values)
    assert not S.conditions_hold([{"key": "b", "ne": "x"}], values)
    assert not S.conditions_hold([{"key": "missing", "eq": True}], values)
    assert not S.conditions_hold([{"key": "a", "eq": 1}], values)  # True is not the number 1
    bye = S.items_by_id(S.build_schema())["note.auto_end_bye"]
    assert "Goodbyes are heard" in S.note_text(bye, {"server.live_preview": True})
    assert "needs the live preview" in S.note_text(bye, {"server.live_preview": False})


# -- parity with the real dialog ---------------------------------------------------------------------


def _row_widgets(form: QFormLayout, row: int):
    """(label widget or None, field widget or layout) of one form row."""
    label = form.itemAt(row, QFormLayout.LabelRole)
    field = form.itemAt(row, QFormLayout.FieldRole) or form.itemAt(row, QFormLayout.SpanningRole)
    if field is None:
        return None, None
    return (label.widget() if label is not None else None), (field.widget() or field.layout())


def _shown_label_text(form: QFormLayout, widget) -> str:
    label = form.labelForField(widget)
    return label.text() if isinstance(label, QLabel) else ""


@pytest.mark.parametrize("known_types", [True, False])
def test_dialog_has_exactly_the_schemas_items_in_the_schemas_order(qt_app, home, known_types):
    dialog = _dialog(note_types=NOTE_TYPES if known_types else None, server_default_note_type="standard")
    schema_pages = {p["key"]: p for p in dialog._schema["pages"]}
    assert dialog.page_keys() == [p["key"] for p in dialog._schema["pages"]]
    assert [dialog.nav.item(i).text() for i in range(dialog.nav.count())] == [p["title"] for p in dialog._schema["pages"]]

    # every schema item is shown by exactly one row of its own page, in the schema's order
    by_page: dict = {}
    for page, item_id, form, widget in dialog.schema_rows:
        if item_id is not None:
            by_page.setdefault(page, []).append((item_id, form, widget))
    for key, page in schema_pages.items():
        expected = [S.item_id(i) for i in page["items"]]
        assert [i for i, _f, _w in by_page.get(key, [])] == expected, key

    # no widget row exists that the schema does not know (or that is not a declared dialog-only helper)
    registered = [(id(f), id(w)) for _p, _i, f, w in dialog.schema_rows]
    forms = {id(f): f for _p, _i, f, _w in dialog.schema_rows}
    for form in forms.values():
        for row in range(form.rowCount()):
            _label, field = _row_widgets(form, row)
            assert field is not None
            assert (id(form), id(field)) in registered, f"a row nobody declared: {field}"

    items = S.items_by_id(dialog._schema)
    for _page, item_id, form, widget in (r for r in dialog.schema_rows if r[1] is not None):
        item = items[item_id]
        if item["type"] == "section":
            assert widget.text() == item["title"]
        elif item["type"] == "note":
            texts = [item["text"]] + [v["text"] for v in item.get("variants", [])]
            assert widget.text() in texts, item_id
        elif item["kind"] == "bool":
            assert isinstance(widget, QCheckBox) and widget.text() == item["label"]
            assert widget.toolTip() == item.get("help", "")
        else:
            assert _shown_label_text(form, widget) == item["label"] or form.labelForField(widget) is not None
            label = form.labelForField(widget)
            assert label is not None and label.text() == item["label"], item_id


def test_dialog_choices_and_tooltips_are_the_schemas(qt_app, home):
    dialog = _dialog(note_types=NOTE_TYPES, server_default_note_type="quick")
    items = S.items_by_id(dialog._schema)

    def pairs(combo):
        return [(combo.itemData(i), combo.itemText(i)) for i in range(combo.count())]

    for key, combo in (
        ("appearance", dialog.appearance_combo),
        ("meeting_detection.auto_end", dialog.auto_end_combo),
        ("local_retention_days", dialog.retention_combo),
    ):
        assert pairs(combo) == [tuple(c) for c in items[key]["choices"]], key
        assert combo.accessibleName() == (items[key].get("aria") or items[key]["label"])
    # devices: Automatic first, a saved device that is gone stays marked
    config_mod.save_config({"audio_devices": {"mic": "Old Mic"}})
    dialog = _dialog()
    dialog.set_device_names({"mic": ["USB Mic"], "system": ["Speakers"]})
    assert pairs(dialog.mic_combo) == [(v, label) for v, label in S.device_options(items["audio_devices.mic"], ["USB Mic"], "Old Mic")]
    assert pairs(dialog.mic_combo)[0] == ("", "Automatic (system default)")
    assert pairs(dialog.mic_combo)[-1] == ("Old Mic", "Old Mic (not connected)")
    assert dialog.system_combo.toolTip() == items["audio_devices.system"]["help"]
    # note types: "Server default (name)", then the types
    dialog = _dialog(note_types=NOTE_TYPES, server_default_note_type="quick")
    assert pairs(dialog.default_note_type_combo) == S.note_type_options(
        items["default_note_type"], NOTE_TYPES, "quick", "")
    assert pairs(dialog.default_note_type_combo)[0] == ("", "Server default (Quick notes)")


@pytest.mark.parametrize("enabled,auto,end,live", list(itertools.product([True, False], [True, False],
                                                                          ["manual", "hour", "silence", "bye"], [True, False])))
def test_dialog_visibility_follows_the_schemas_rules(qt_app, home, enabled, auto, end, live):
    dialog = _dialog()
    dialog.show()
    dialog.show_page("general")
    dialog.detect_check.setChecked(enabled)
    dialog.auto_record_check.setChecked(auto)
    dialog.auto_end_combo.setCurrentIndex(dialog.auto_end_combo.findData(end))
    dialog.live_check.setChecked(live)
    values = dialog.collect_values()
    items = S.items_by_id(dialog._schema)
    assert dialog.auto_record_check.isEnabled() == S.is_enabled(items["meeting_detection.auto_record"], values)
    assert (not dialog.auto_end_combo.isHidden()) == S.is_shown(items["meeting_detection.auto_end"], values)
    assert (not dialog.auto_end_note.isHidden()) == S.is_shown(items["note.auto_end"], values)
    bye = items["note.auto_end_bye"]
    want = S.note_text(bye, values) if S.is_shown(bye, values) else ""
    assert (not dialog.auto_end_bye_note.isHidden()) == bool(want)
    if want:
        assert dialog.auto_end_bye_note.text() == want
    dialog.close()


def test_every_editable_field_is_in_the_dialogs_collected_values_and_vice_versa(qt_app, home):
    dialog = _dialog()
    fields = S.fields_by_key(dialog._schema)
    editable = {k for k, f in fields.items() if f["kind"] not in ("secret", "readonly")}
    assert set(dialog.collect_values()) == editable
    # get_values reports the same keys, plus the secret as a flag
    assert set(S.get_values({})) == editable | {"server.token"}
    assert {k for k, f in fields.items() if f["kind"] == "readonly"} == set(S.read_only_values({}, {"count": 1, "bytes": 2}))


def test_a_save_from_the_dialog_writes_what_merge_values_writes(qt_app, home):
    _write(home, {"server": {"url": "http://old", "token": TOKEN, "auto_update": True}, "extra": 1,
                  "meeting_detection": {"end_grace_sec": 90, "unknown": 1}})
    dialog = _dialog()
    dialog.show_page("general")
    dialog.detect_check.setChecked(False)
    dialog.url_edit.setText("http://new.example:8000/")
    dialog.accept()
    saved = _read(home)
    assert saved["server"] == {"url": "http://new.example:8000", "token": TOKEN, "live_preview": True,
                               "auto_upload": True, "check_updates": True}
    assert saved["meeting_detection"] == {"enabled": False, "auto_stop": True, "suggest_stop": True,
                                          "auto_record": False, "auto_end": "hour", "end_grace_sec": 90}
    assert saved["extra"] == 1 and saved["password_prompted"] is True and saved["settings_page"] == "general"


def test_the_dialog_saves_delete_after_upload_and_it_defaults_off(qt_app, home):
    _write(home, {})
    dialog = _dialog()
    assert dialog.delete_after_upload_check.isChecked() is False
    assert dialog.collect_values()["delete_after_upload"] is False
    dialog.show_page("recordings")
    dialog.delete_after_upload_check.setChecked(True)
    dialog.accept()
    assert _read(home)["delete_after_upload"] is True
    assert config_mod.delete_after_upload() is True


# -- the shared save -------------------------------------------------------------------------------


def test_get_values_reads_the_config_the_way_the_app_does(home):
    _write(home, {"appearance": "DARK", "local_retention_days": 30, "default_note_type": "quick",
                  "audio_devices": {"mic": "USB Mic", "system": 7}, "remote_control_allowed": False,
                  "server": {"url": "http://s", "token": TOKEN, "live_preview": False},
                  "meeting_detection": {"auto_end": "call"}})
    values = S.get_values()
    assert values["appearance"] == "dark" and values["local_retention_days"] == 30
    assert values["default_note_type"] == "quick" and values["audio_devices.mic"] == "USB Mic"
    assert values["audio_devices.system"] == "" and values["remote_control_allowed"] is False
    assert values["server.url"] == "http://s" and values["server.live_preview"] is False
    assert values["meeting_detection.auto_end"] == "manual" and values["meeting_detection.auto_stop"] is True
    assert values["server.token"] is True and TOKEN not in json.dumps(values)


def test_apply_changes_saves_through_the_dialogs_normalisers(home):
    _write(home, {"server": {"url": "http://s", "token": TOKEN}, "keep": "me"})
    result = S.apply_changes({
        "appearance": "dark", "meeting_detection.auto_record": True, "meeting_detection.auto_end": "bye",
        "audio_devices.mic": "  USB Mic ", "local_retention_days": 7, "show_audio_levels": False,
        "server.url": " http://other:9000/ ", "server.live_preview": False, "default_note_type": "quick",
    }, known_note_types=["quick"])
    assert result.errors == {} and result.saved == sorted([
        "appearance", "meeting_detection.auto_record", "meeting_detection.auto_end", "audio_devices.mic",
        "local_retention_days", "show_audio_levels", "server.url", "server.live_preview", "default_note_type"])
    saved = _read(home)
    assert saved["appearance"] == "dark" and saved["local_retention_days"] == 7 and saved["show_audio_levels"] is False
    assert saved["audio_devices"] == {"mic": "USB Mic", "system": ""} and saved["default_note_type"] == "quick"
    assert saved["server"]["url"] == "http://other:9000" and saved["server"]["token"] == TOKEN
    assert saved["server"]["live_preview"] is False and saved["keep"] == "me"
    assert saved["meeting_detection"]["auto_record"] is True and saved["meeting_detection"]["auto_end"] == "bye"
    assert saved["meeting_detection"]["end_grace_sec"] == 60
    assert result.values["appearance"] == "dark" and result.values["server.token"] is True
    assert TOKEN not in json.dumps(result.values)


def test_apply_changes_with_nothing_different_writes_nothing(home):
    _write(home, {"appearance": "dark"})
    before = (home / "config.json").read_text()
    result = S.apply_changes({"appearance": "dark", "meeting_detection.enabled": True})
    assert result.saved == [] and result.errors == {} and (home / "config.json").read_text() == before


def test_the_side_effects_of_a_save_run_after_it_and_not_for_a_refusal(home, monkeypatch):
    from meeting_notes.client.ui import theme

    applied, secrets = [], []
    monkeypatch.setattr(theme, "apply_appearance", lambda value: applied.append(value))
    from meeting_notes.client import logsetup

    monkeypatch.setattr(logsetup, "register_secret", lambda s: secrets.append(s))
    target = home / "Made" / "Here"
    _write(home, {"server": {"token": TOKEN}})
    result = S.apply_changes({"appearance": "light", "save_dir": str(target)})
    assert result.saved == ["appearance", "save_dir"] and applied == ["light"] and secrets == [TOKEN]
    assert target.is_dir()  # the folder is created now, like the dialog does
    applied.clear()
    assert S.apply_changes({"appearance": "bogus"}).saved == [] and applied == []


@pytest.mark.parametrize("key,value", [
    ("appearance", "purple"), ("appearance", True), ("meeting_detection.enabled", "yes"), ("meeting_detection.enabled", 1),
    ("meeting_detection.auto_end", "call"), ("local_retention_days", 14), ("local_retention_days", "7"),
    ("local_retention_days", True), ("audio_devices.mic", 5), ("audio_devices.mic", "a" * 400),
    ("audio_devices.system", "tab\there"), ("default_note_type", "../x"), ("default_note_type", "unknown-type"),
    ("server.url", 5), ("server.url", "http://a\nb"), ("server.url", "x" * 600), ("save_dir", 5), ("save_dir", "a\nb"),
])
def test_apply_changes_refuses_what_a_widget_would_not_allow(home, key, value):
    _write(home, {"appearance": "dark"})
    before = (home / "config.json").read_text()
    result = S.apply_changes({key: value}, known_note_types=["quick"])
    assert key in result.errors and result.saved == []
    assert (home / "config.json").read_text() == before


def test_the_token_and_the_readonly_fields_cannot_be_changed_remotely(home):
    _write(home, {"server": {"token": TOKEN}})
    result = S.apply_changes({"server.token": "new", "about.version": "9", "recordings.stats": "x", "nope": True})
    assert set(result.errors) == {"server.token", "about.version", "recordings.stats", "nope"}
    assert result.errors["server.token"] == S.NOT_REMOTE
    assert _read(home)["server"]["token"] == TOKEN


def test_one_bad_value_saves_nothing(home):
    _write(home, {"appearance": "dark"})
    result = S.apply_changes({"appearance": "light", "local_retention_days": 14})
    assert result.saved == [] and list(result.errors) == ["local_retention_days"]
    assert _read(home)["appearance"] == "dark"


def test_the_save_folder_is_checked_like_the_dialog_and_for_being_writable(home, monkeypatch):
    # inside the app folder: the dialog's rule
    app = home / "App"
    monkeypatch.setattr("meeting_notes.client.paths.app_folders", lambda: [app])
    result = S.apply_changes({"save_dir": str(app / "recordings")})
    assert "app folder" in result.errors["save_dir"]
    # a file where a folder is needed cannot hold recordings
    blocker = home / "afile"
    blocker.write_text("x")
    assert "save_dir" in S.apply_changes({"save_dir": str(blocker / "sub")}).errors
    assert "save_dir" in S.apply_changes({"save_dir": str(blocker)}).errors
    # an empty value means the default folder
    monkeypatch.setattr(config_mod, "DEFAULT_SAVE_DIR", home / "Default")
    _write(home, {"save_dir": str(home / "Elsewhere")})
    assert S.apply_changes({"save_dir": "  "}).saved == ["save_dir"]
    assert Path(_read(home)["save_dir"]) == home / "Default"


def test_a_note_type_must_be_known_unless_it_is_already_saved(home):
    _write(home, {"default_note_type": "gone"})
    assert S.apply_changes({"default_note_type": "gone"}, known_note_types=[]).errors == {}
    assert "default_note_type" in S.apply_changes({"default_note_type": "other"}, known_note_types=[]).errors
    assert S.apply_changes({"default_note_type": ""}, known_note_types=[]).saved == ["default_note_type"]
    assert "default_note_type" not in _read(home)  # "Server default" removes it, like the dialog


def test_the_sign_in_token_is_in_no_payload(home):
    _write(home, {"server": {"url": "http://s", "token": TOKEN}})
    payload = S.settings_payload(note_types=NOTE_TYPES, server_default_note_type="standard",
                                 devices={"mic": ["Mic"], "system": []}, stats={"count": 2, "bytes": 2048})
    text = json.dumps(payload)
    assert TOKEN not in text and "password_prompted" not in text
    assert payload["values"]["server.token"] is True
    assert payload["values"]["recordings.stats"] == "2 recordings, 2.0 KB"
    assert payload["values"]["about.version"]
    assert remote.sanitize_result("get_settings", payload) == payload
