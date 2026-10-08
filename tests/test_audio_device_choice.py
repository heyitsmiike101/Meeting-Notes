"""Settings -> Audio device pickers: config, the dialog rows, and recording with the chosen device.

No hardware: the controller gets a fake device resolver and the dialog a fake device lister.
"""

from __future__ import annotations

import json
import logging
import time

import pytest

from meeting_notes import config as config_mod
from meeting_notes.audio import devices as devices_mod
from meeting_notes.audio.devices import DeviceInfo, DeviceNotFound
from meeting_notes.client.controller import RecordingController
from tests.fakes import FakeSource

# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------


def test_audio_device_settings_defaults_to_automatic():
    assert config_mod.audio_device_settings({}) == {"mic": "", "system": ""}
    assert config_mod.audio_device_settings({"audio_devices": None}) == {"mic": "", "system": ""}


def test_audio_device_settings_keeps_clean_names():
    got = config_mod.audio_device_settings({"audio_devices": {"mic": "  Headset Mic (USB)  ", "system": "Speakers (Realtek)"}})
    assert got == {"mic": "Headset Mic (USB)", "system": "Speakers (Realtek)"}


@pytest.mark.parametrize("bad", [5, True, None, ["Mic"], {"a": 1}, "", "   ", "two\nlines", "x" * 500, "bell\x07"])
def test_audio_device_settings_strict_cleaning(bad):
    got = config_mod.audio_device_settings({"audio_devices": {"mic": bad, "system": "Speakers"}})
    assert got == {"mic": "", "system": "Speakers"}


def test_audio_device_settings_ignores_unknown_kinds_and_a_non_dict():
    assert config_mod.audio_device_settings({"audio_devices": {"camera": "Cam"}}) == {"mic": "", "system": ""}
    for junk in ("Mic", ["Mic"], 7):
        assert config_mod.audio_device_settings({"audio_devices": junk}) == {"mic": "", "system": ""}


def test_requested_audio_device_prefers_settings_then_the_older_keys():
    assert config_mod.requested_audio_device("mic", {}) is None
    assert config_mod.requested_audio_device("mic", {"mic": "Logitech"}) == "Logitech"
    both = {"mic": "Logitech", "audio_devices": {"mic": "Blue Yeti"}}
    assert config_mod.requested_audio_device("mic", both) == "Blue Yeti"
    assert config_mod.requested_audio_device("system", both) is None
    assert config_mod.requested_audio_device("mic", {"mic": 3}) is None


# --------------------------------------------------------------------------
# enumeration helper
# --------------------------------------------------------------------------


def _info(name, kind, id_=None):
    return DeviceInfo(id=id_ or name, name=name, kind=kind, channels=2, samplerate=48000, is_default=False)


def test_selectable_device_names_dedupes_and_skips_screencapturekit(monkeypatch):
    monkeypatch.setattr(devices_mod, "list_microphones", lambda: [_info("USB Mic", "mic"), _info("USB Mic", "mic"), _info("Built-in", "mic")])
    assert devices_mod.selectable_device_names("mic") == ["USB Mic", "Built-in"]
    monkeypatch.setattr(
        devices_mod,
        "list_system_sources",
        lambda: [_info("System audio (ScreenCaptureKit)", "system", devices_mod.SCK_DEVICE_ID), _info("BlackHole 2ch", "system")],
    )
    assert devices_mod.selectable_device_names("system") == ["BlackHole 2ch"]


def test_selectable_device_names_never_raises(monkeypatch):
    def boom():
        raise RuntimeError("COM exploded")

    monkeypatch.setattr(devices_mod, "list_microphones", boom)
    assert devices_mod.selectable_device_names("mic") == []
    with pytest.raises(ValueError):
        devices_mod.selectable_device_names("camera")


# --------------------------------------------------------------------------
# the controller
# --------------------------------------------------------------------------


class Resolver:
    """Fake ``resolve_source``: a set of connected device names per kind; records every request."""

    def __init__(self, **connected):
        self.connected = {"mic": ["Built-in Mic"], "system": ["Speakers"]}
        self.connected.update(connected)
        self.calls = []

    def __call__(self, kind, requested=None, samplerate=None):
        self.calls.append((kind, requested))
        names = self.connected[kind]
        if requested is None:
            if not names:
                raise DeviceNotFound(f"no {kind} device")
            return FakeSource(name=names[0], samplerate=1000)
        for name in names:
            if requested.lower() in name.lower():
                return FakeSource(name=name, samplerate=1000)
        raise DeviceNotFound(f"no {kind} device matching {requested!r}")


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setenv("MEETING_NOTES_CONFIG", str(path))

    def write(**extra):
        config_mod.save_config({"save_dir": str(tmp_path / "rec"), **extra}, path)

    write()
    return write


def test_automatic_asks_for_the_os_default(cfg):
    resolver = Resolver()
    snap = RecordingController(device_resolver=resolver)._scan_devices()
    assert resolver.calls == [("mic", None), ("system", None)]
    assert snap.name("mic") == "Built-in Mic" and snap.fallbacks == {}


def test_the_chosen_device_is_passed_to_resolve_source(cfg):
    cfg(audio_devices={"mic": "USB Headset", "system": "Monitor Speakers"})
    resolver = Resolver(mic=["Built-in Mic", "USB Headset"], system=["Speakers", "Monitor Speakers"])
    snap = RecordingController(device_resolver=resolver)._scan_devices()
    assert resolver.calls == [("mic", "USB Headset"), ("system", "Monitor Speakers")]
    assert snap.name("mic") == "USB Headset" and snap.name("system") == "Monitor Speakers"
    assert snap.fallbacks == {}


def test_a_missing_chosen_device_falls_back_to_automatic_and_says_so(cfg, caplog):
    caplog.set_level(logging.WARNING, logger="meeting_notes.client.controller")
    cfg(audio_devices={"mic": "USB Headset"})
    resolver = Resolver()
    controller = RecordingController(device_resolver=resolver)
    snap = controller._scan_devices()
    assert resolver.calls[:2] == [("mic", "USB Headset"), ("mic", None)]
    assert snap.name("mic") == "Built-in Mic" and not snap.errors
    assert snap.fallbacks == {"mic": "USB Headset"}
    # Logged once for that device, not on every 3-second scan.
    controller._scan_devices()
    controller._scan_devices()
    warnings = [r for r in caplog.records if "USB Headset" in r.getMessage()]
    assert len(warnings) == 1 and "automatic" in warnings[0].getMessage()


def test_the_main_window_label_names_the_fallback(cfg):
    cfg(audio_devices={"mic": "USB Headset"})
    controller = RecordingController(device_resolver=Resolver())
    controller.start_device_watch(interval=60)
    try:
        deadline = time.monotonic() + 5
        while not controller.device_watcher.has_snapshot and time.monotonic() < deadline:
            time.sleep(0.02)
        label = controller.device_labels()["mic"]
        assert label.startswith("Built-in Mic") and "USB Headset is not connected" in label
        assert controller.device_fallbacks() == {"mic": "USB Headset"}
    finally:
        controller.stop_device_watch()


def test_recording_starts_with_the_automatic_device_when_the_chosen_one_is_gone(cfg, tmp_path):
    cfg(audio_devices={"mic": "USB Headset", "system": "Gone Speakers"})
    controller = RecordingController(device_resolver=Resolver())
    assert controller.start("fallback") is not None
    try:
        names = {k: r.source.name for k, r in controller.session.recorders.items()}
        assert names == {"mic": "Built-in Mic", "system": "Speakers"}
    finally:
        controller.stop()


def test_recording_uses_the_chosen_device_when_it_is_connected(cfg):
    cfg(audio_devices={"mic": "USB Headset"})
    controller = RecordingController(device_resolver=Resolver(mic=["Built-in Mic", "USB Headset"]))
    assert controller.start("chosen") is not None
    try:
        assert controller.session.recorders["mic"].source.name == "USB Headset"
    finally:
        controller.stop()


def test_the_idle_meter_follows_the_chosen_device(cfg):
    cfg(audio_devices={"mic": "USB Headset"})
    controller = RecordingController(device_resolver=Resolver(mic=["Built-in Mic", "USB Headset"]))
    controller.start_device_watch(interval=60)
    try:
        deadline = time.monotonic() + 5
        while not controller.device_watcher.has_snapshot and time.monotonic() < deadline:
            time.sleep(0.02)
        assert controller.device_watcher.snapshot.name("mic") == "USB Headset"
        cfg(audio_devices={"mic": ""})  # back to Automatic: the next scan follows
        controller.device_watcher.poll_once()
        assert controller.device_watcher.snapshot.name("mic") == "Built-in Mic"
    finally:
        controller.stop_device_watch()
        controller.stop_idle_meter(1.0)


# --------------------------------------------------------------------------
# the Settings page
# --------------------------------------------------------------------------

pytest.importorskip("PySide6", reason="PySide6 not installed")

from PySide6.QtWidgets import QApplication  # noqa: E402

from meeting_notes.client.ui.theme import APP_STYLE  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(APP_STYLE)
    return app


def _dialog(tmp_path, monkeypatch, audio_devices=None, lister=None):
    path = tmp_path / "config.json"
    data = {"save_dir": str(tmp_path / "R")}
    if audio_devices is not None:
        data["audio_devices"] = audio_devices
    path.write_text(json.dumps(data))
    monkeypatch.setenv("MEETING_NOTES_CONFIG", str(path))
    from meeting_notes.client.ui.settings_dialog import SettingsDialog

    return SettingsDialog(device_lister=lister), path


def _items(combo):
    return [(combo.itemText(i), combo.itemData(i)) for i in range(combo.count())]


def _pump(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        QApplication.processEvents()
        time.sleep(0.01)
    return cond()


def test_audio_page_rows_and_default_are_automatic(qt_app, tmp_path, monkeypatch):
    dialog, _ = _dialog(tmp_path, monkeypatch)
    assert dialog.mic_combo.accessibleName() == "Microphone"
    assert dialog.system_combo.accessibleName() == "Speakers (what you hear)"
    from PySide6.QtWidgets import QLabel

    labels = [label.text() for label in dialog.mic_combo.parentWidget().findChildren(QLabel)]
    assert "Microphone" in labels and "Speakers" in labels
    for combo in (dialog.mic_combo, dialog.system_combo):
        assert combo.itemText(0) == "Automatic (system default)" and combo.itemData(0) == ""
        assert combo.currentData() == ""
    dialog.close()


def test_devices_fill_in_off_the_gui_thread(qt_app, tmp_path, monkeypatch):
    lister = lambda: {"mic": ["Built-in Mic", "USB Headset"], "system": ["Speakers"]}  # noqa: E731
    dialog, _ = _dialog(tmp_path, monkeypatch, lister=lister)
    assert _pump(lambda: dialog.mic_combo.count() == 3)
    assert _items(dialog.mic_combo) == [
        ("Automatic (system default)", ""), ("Built-in Mic", "Built-in Mic"), ("USB Headset", "USB Headset"),
    ]
    assert _items(dialog.system_combo)[1:] == [("Speakers", "Speakers")]
    dialog.close()


def test_a_saved_device_that_is_not_connected_stays_in_the_list(qt_app, tmp_path, monkeypatch):
    dialog, _ = _dialog(tmp_path, monkeypatch, {"mic": "USB Headset", "system": ""}, lister=lambda: {"mic": ["Built-in Mic"], "system": []})
    assert _pump(lambda: dialog.mic_combo.count() == 3)
    assert ("USB Headset (not connected)", "USB Headset") in _items(dialog.mic_combo)
    assert dialog.mic_combo.currentData() == "USB Headset"
    assert dialog.system_combo.currentData() == ""
    dialog.close()


def test_the_not_connected_entry_shows_before_the_listing_arrives(qt_app, tmp_path, monkeypatch):
    dialog, _ = _dialog(tmp_path, monkeypatch, {"mic": "USB Headset"}, lister=lambda: (time.sleep(0.3), {"mic": [], "system": []})[1])
    assert dialog.mic_combo.currentText() == "USB Headset (not connected)"
    dialog.close()


def test_a_connected_saved_device_is_not_marked(qt_app, tmp_path, monkeypatch):
    dialog, _ = _dialog(tmp_path, monkeypatch, {"mic": "USB Headset"}, lister=lambda: {"mic": ["USB Headset"], "system": []})
    assert _pump(lambda: dialog.mic_combo.currentText() == "USB Headset")
    assert dialog.mic_combo.count() == 2
    dialog.close()


def test_a_failing_listing_leaves_automatic_working(qt_app, tmp_path, monkeypatch):
    def boom():
        raise RuntimeError("no audio backend")

    dialog, path = _dialog(tmp_path, monkeypatch, lister=boom)
    _pump(lambda: not dialog._bridges)
    assert dialog.mic_combo.count() == 1
    dialog.accept()
    assert json.loads(path.read_text())["audio_devices"] == {"mic": "", "system": ""}


def test_saving_stores_the_chosen_names(qt_app, tmp_path, monkeypatch):
    dialog, path = _dialog(tmp_path, monkeypatch, lister=lambda: {"mic": ["Built-in Mic", "USB Headset"], "system": ["Speakers", "Monitor"]})
    assert _pump(lambda: dialog.mic_combo.count() == 3 and dialog.system_combo.count() == 3)
    dialog.mic_combo.setCurrentIndex(dialog.mic_combo.findData("USB Headset"))
    dialog.system_combo.setCurrentIndex(dialog.system_combo.findData("Monitor"))
    dialog.accept()
    assert json.loads(path.read_text())["audio_devices"] == {"mic": "USB Headset", "system": "Monitor"}


def test_saving_automatic_stores_empty_strings(qt_app, tmp_path, monkeypatch):
    dialog, path = _dialog(tmp_path, monkeypatch, {"mic": "USB Headset", "system": "Monitor"}, lister=lambda: {"mic": [], "system": []})
    _pump(lambda: not dialog._bridges)
    dialog.mic_combo.setCurrentIndex(0)
    dialog.system_combo.setCurrentIndex(0)
    dialog.accept()
    assert json.loads(path.read_text())["audio_devices"] == {"mic": "", "system": ""}


def test_saving_keeps_a_not_connected_choice(qt_app, tmp_path, monkeypatch):
    dialog, path = _dialog(tmp_path, monkeypatch, {"mic": "USB Headset"}, lister=lambda: {"mic": [], "system": []})
    _pump(lambda: not dialog._bridges)
    dialog.accept()
    assert json.loads(path.read_text())["audio_devices"]["mic"] == "USB Headset"


def test_the_page_says_a_change_applies_to_the_next_recording(qt_app, tmp_path, monkeypatch):
    dialog, _ = _dialog(tmp_path, monkeypatch)
    page = dialog.mic_combo.parentWidget()
    text = " ".join(label.text() for label in page.findChildren(type(dialog.auto_end_note)))
    assert "next recording" in text and "not connected" in text
    dialog.close()


def test_macos_system_picker_is_explained_and_only_enabled_with_a_driver(qt_app, tmp_path, monkeypatch):
    import meeting_notes.client.ui.settings_dialog as sd

    monkeypatch.setattr(sd.sys, "platform", "darwin")
    dialog, _ = _dialog(tmp_path, monkeypatch, lister=lambda: {"mic": ["Built-in Mic"], "system": []})
    assert dialog._mac_system_note is not None and "ScreenCaptureKit" not in dialog._mac_system_note.text()
    assert _pump(lambda: dialog.mic_combo.count() == 2)
    assert not dialog.system_combo.isEnabled() and dialog.system_combo.count() == 1
    dialog.set_device_names({"mic": [], "system": ["BlackHole 2ch"]})
    assert dialog.system_combo.isEnabled()
    dialog.close()
