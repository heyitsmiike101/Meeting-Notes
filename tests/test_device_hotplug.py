"""Devices that appear, disappear or change while the app is open or recording.

Everything runs against the fake AudioSource layer through an injected device
resolver, so no audio hardware is involved. The scenarios come from a real
meeting: the headset was switched on three minutes after Start, and the whole
recording ended up with no "You" track.
"""

from __future__ import annotations

import json
import threading
import time
import wave
from contextlib import contextmanager
from pathlib import Path

import pytest

from meeting_notes import config as config_mod
from meeting_notes.audio.devices import DeviceNotFound
from meeting_notes.client.controller import (
    IDLE,
    RECORDING,
    RecordingController,
    missing_device_text,
)
from meeting_notes.client.device_watch import DeviceSnapshot, DeviceWatcher
from meeting_notes.timing import load_timing_log
from meeting_notes.transcribe.merge import merge_tracks
from meeting_notes.transcribe.protocol import Segment as TSegment
from tests.fakes import FakeReader, FakeSource

RATE = 1000


class SwitchSource(FakeSource):
    """A device that can be switched off (open and read then fail) and on."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.broken = False

    @contextmanager
    def open(self):
        if self.broken:
            self.opens += 1
            raise OSError(f"{self.name}: device not present")
        self.opens += 1
        reader = _SwitchReader(self)
        self.readers.append(reader)
        yield reader


class _SwitchReader(FakeReader):
    def read(self, numframes):
        if self.source.broken:
            raise OSError(f"{self.source.name}: device disconnected")
        return super().read(numframes)


class World:
    """The set of devices the fake OS currently has."""

    def __init__(self):
        self.devices = {"mic": [], "system": []}
        self.default = {"mic": None, "system": None}
        self.raise_on_enumerate = False
        self.calls = 0

    def add(self, kind, source, *, default=True):
        self.devices[kind].append(source)
        if default or self.default[kind] is None:
            self.default[kind] = source

    def remove(self, kind, source):
        self.devices[kind].remove(source)
        if self.default[kind] is source:
            self.default[kind] = self.devices[kind][0] if self.devices[kind] else None

    def resolve(self, kind, requested=None, samplerate=None):
        self.calls += 1
        if self.raise_on_enumerate:
            raise RuntimeError("Error 0x800401f0")
        if not self.devices[kind]:
            raise DeviceNotFound(f"no {kind} device")
        if requested is None:
            return self.default[kind]
        for src in self.devices[kind]:
            if requested.lower() in src.name.lower():
                return src
        raise DeviceNotFound(f"no {kind} device matching {requested!r}")


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setenv("MEETING_NOTES_CONFIG", str(path))
    config_mod.save_config({"save_dir": str(tmp_path / "rec")}, path)
    return path


@pytest.fixture
def world():
    return World()


def wait_for(cond, timeout=6.0, step=0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(step)
    return cond()


def make_controller(world):
    return RecordingController(device_resolver=world.resolve)


def wav_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()


def texts(controller):
    return [b["text"] for b in controller.device_banners()]


# --------------------------------------------------------------------------
# idle auto-refresh
# --------------------------------------------------------------------------


def test_idle_refresh_picks_up_a_new_mic_and_follows_the_default(cfg, world):
    world.add("system", FakeSource(name="Speakers", samplerate=RATE))
    controller = make_controller(world)
    watcher = controller.start_device_watch(interval=0.1)
    try:
        assert wait_for(lambda: controller.device_labels().get("system") == "Speakers")
        assert controller.device_labels()["mic"].startswith("unavailable")

        headset = FakeSource(name="Logitech PRO X 2 LIGHTSPEED", samplerate=RATE)
        world.add("mic", headset)
        assert wait_for(lambda: controller.device_labels()["mic"] == headset.name)

        # Not pinned: the OS default moving is followed while idle.
        laptop = FakeSource(name="Laptop Mic", samplerate=RATE)
        world.add("mic", laptop, default=True)
        assert wait_for(lambda: controller.device_labels()["mic"] == "Laptop Mic")

        # Unplugged: the label says so rather than showing a stale name.
        world.remove("mic", laptop)
        world.remove("mic", headset)
        assert wait_for(lambda: controller.device_labels()["mic"].startswith("unavailable"))
    finally:
        controller.stop_device_watch()
    assert watcher.polls > 3


def test_pinned_device_is_not_replaced_by_a_new_default(cfg, world):
    pro = FakeSource(name="Logitech PRO X", samplerate=RATE)
    world.add("mic", pro)
    world.add("system", FakeSource(name="Speakers", samplerate=RATE))
    config_mod.save_config({"save_dir": str(cfg.parent / "rec"), "mic": "Logitech"}, cfg)
    controller = make_controller(world)
    controller.start_device_watch(interval=0.1)
    try:
        assert wait_for(lambda: controller.device_labels().get("mic") == pro.name)
        world.add("mic", FakeSource(name="Laptop Mic", samplerate=RATE), default=True)
        time.sleep(0.5)
        assert controller.device_labels()["mic"] == pro.name
    finally:
        controller.stop_device_watch()


def test_enumeration_errors_never_crash_the_watcher(cfg, world):
    world.add("mic", FakeSource(name="Mic", samplerate=RATE))
    world.add("system", FakeSource(name="Speakers", samplerate=RATE))
    world.raise_on_enumerate = True
    controller = make_controller(world)
    controller.start_device_watch(interval=0.05)
    try:
        assert wait_for(lambda: "0x800401f0" in controller.device_labels().get("mic", ""))
        world.raise_on_enumerate = False
        assert wait_for(lambda: controller.device_labels()["mic"] == "Mic")
    finally:
        controller.stop_device_watch()


def test_watcher_survives_a_scan_and_callbacks_that_raise():
    calls = {"n": 0}

    def scan():
        calls["n"] += 1
        raise RuntimeError("COM exploded")

    def bad_poll(_snap):
        raise RuntimeError("callback bug")

    watcher = DeviceWatcher(scan, on_poll=bad_poll, interval=0.02)
    assert watcher.poll_once() is None  # a failed scan is swallowed
    watcher._scan = lambda: DeviceSnapshot()
    assert watcher.poll_once() is not None  # a raising callback is swallowed too
    watcher._scan = scan
    watcher.start()
    time.sleep(0.2)
    watcher.stop()
    assert calls["n"] >= 3


def test_wake_polls_without_waiting_for_the_interval(cfg, world):
    controller = make_controller(world)
    watcher = controller.start_device_watch(interval=60.0)
    try:
        assert wait_for(lambda: watcher.polls >= 1)
        world.add("mic", FakeSource(name="Late Mic", samplerate=RATE))
        controller.wake_device_watch()
        assert wait_for(lambda: controller.device_labels()["mic"] == "Late Mic", timeout=4.0)
    finally:
        controller.stop_device_watch()


# --------------------------------------------------------------------------
# starting without a device, then attaching mid-recording
# --------------------------------------------------------------------------


def test_start_without_a_mic_records_system_audio_and_shows_the_red_banner(cfg, world):
    world.add("system", FakeSource(name="Speakers", samplerate=RATE))
    controller = make_controller(world)
    assert controller.start("no-mic") is not None
    try:
        assert controller.state == RECORDING
        assert sorted(controller.session.recorders) == ["system"]
        banners = controller.device_banners()
        assert [b["track"] for b in banners] == ["mic"]
        assert banners[0]["level"] == "error"
        assert banners[0]["text"] == (
            "No microphone found — you are not being recorded. "
            "Connect one and it will be added automatically."
        )
        assert controller.device_labels()["mic"] == "not connected"
    finally:
        controller.stop()


def test_start_without_system_audio_says_it_cannot_hear_the_meeting(cfg, world):
    world.add("mic", FakeSource(name="Mic", samplerate=RATE))
    controller = make_controller(world)
    assert controller.start("no-loopback") is not None
    try:
        (banner,) = controller.device_banners()
        assert banner["track"] == "system"
        assert banner["text"].startswith("Can't hear the meeting")
    finally:
        controller.stop()


def test_start_with_no_devices_at_all_still_refuses(cfg, world):
    controller = make_controller(world)
    assert controller.start("nothing") is None
    assert controller.state == IDLE
    assert controller.error


def test_mic_appearing_mid_recording_is_attached_and_aligned(cfg, world):
    world.add("system", FakeSource(name="Speakers", samplerate=RATE, freq=30.0))
    controller = make_controller(world)
    controller.start_device_watch(interval=0.1)
    session_dir = controller.start("late-mic")
    assert session_dir is not None
    try:
        time.sleep(3.0)
        assert "mic" not in controller.session.recorders
        world.add("mic", FakeSource(name="Logitech PRO X 2 LIGHTSPEED", samplerate=RATE, freq=80.0))
        assert wait_for(lambda: "mic" in controller.session.recorders, timeout=4.0)
        attached_at = controller.session.elapsed
        assert 2.9 <= attached_at <= 4.5

        (banner,) = [b for b in controller.device_banners() if b["track"] == "mic"]
        assert banner["level"] == "ok"
        assert banner["text"].startswith("Microphone connected at 00:00:0")
        assert banner["text"].endswith("recording you from now on")
        assert "remaining" in banner and banner["remaining"] > 1

        time.sleep(2.0)  # let it record some real audio after attaching
    finally:
        controller.stop_device_watch()
        meta = controller.stop()

    session_len = meta["duration_sec"]
    mic_wav = session_dir / "mic.wav"
    system_wav = session_dir / "system.wav"
    assert mic_wav.exists() and system_wav.exists()
    # Full-length: silence up front, so the file spans the same time as the
    # track that was there from the start.
    assert wav_seconds(mic_wav) == pytest.approx(wav_seconds(system_wav), abs=0.75)
    assert wav_seconds(mic_wav) == pytest.approx(session_len, abs=0.75)

    mic_meta = meta["tracks"]["mic"]
    assert mic_meta["attached_late"] is True
    assert mic_meta["attach_gap_seconds"] == pytest.approx(attached_at, abs=0.6)
    assert [g["reason"] for g in mic_meta["gaps"]] == ["late-attach"]
    assert mic_meta["gaps"][0]["frames_before"] == 0
    assert mic_meta["gaps"][0]["seconds_lost"] == pytest.approx(attached_at, abs=0.6)
    assert "gaps" in meta["tracks"]["system"] and meta["tracks"]["system"]["gaps"] == []
    assert any(e["kind"] == "attach" and e["track"] == "mic" for e in meta["events"])

    # The frames before the attach point are silence; after it there is signal.
    with wave.open(str(mic_wav), "rb") as w:
        import numpy as np

        data = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    assert np.abs(data[: int(RATE * (attached_at - 0.7))]).max() == 0
    assert np.abs(data[int(RATE * (attached_at + 0.8)) :]).max() > 1000

    # Timing metadata: gap covers session start to attach time.
    clock = load_timing_log(session_dir / "mic.timing.jsonl")
    (gap,) = clock.gaps
    assert gap.reason == "late-attach"
    assert clock.in_gap(1.0) and not clock.in_gap(attached_at + 1.0)

    # Server-side alignment: "You" speaks at session time 4.5 s (WAV second 4.5,
    # because the file is full-length); the merged transcript places it there,
    # relative to a "Them" line at 1.0 s. Text whisper hallucinated over the
    # silent stretch is flagged as lost audio instead of being trusted.
    clocks = {
        "mic": clock,
        "system": load_timing_log(session_dir / "system.timing.jsonl"),
    }
    merged = merge_tracks(
        {
            "mic": [
                TSegment(start=1.0, end=2.0, text="phantom over silence", track="mic"),
                TSegment(start=attached_at + 1.0, end=attached_at + 2.0, text="hello I am here", track="mic"),
            ],
            "system": [TSegment(start=1.0, end=2.0, text="welcome everyone", track="system")],
        },
        clocks,
    )
    by_text = {m["text"]: m for m in merged}
    you = by_text["hello I am here"]
    assert you["label"] == "You" and not you["in_gap"]
    assert you["start"] == pytest.approx(attached_at + 1.0, abs=0.25)
    them = by_text["welcome everyone"]
    assert them["start"] == pytest.approx(1.0, abs=0.25)
    assert by_text["phantom over silence"]["in_gap"] is True
    assert [m["label"] for m in merged if not m["in_gap"]] == ["Them", "You"]


def test_system_audio_appearing_mid_recording_is_attached_too(cfg, world):
    world.add("mic", FakeSource(name="Mic", samplerate=RATE))
    controller = make_controller(world)
    controller.start_device_watch(interval=0.1)
    session_dir = controller.start("late-loopback")
    try:
        assert wait_for(lambda: bool(controller.device_banners()))
        world.add("system", FakeSource(name="Speakers", samplerate=RATE))
        assert wait_for(lambda: "system" in controller.session.recorders, timeout=4.0)
        (banner,) = [b for b in controller.device_banners() if b["track"] == "system"]
        assert "recording them from now on" in banner["text"]
    finally:
        controller.stop_device_watch()
        meta = controller.stop()
    assert meta["tracks"]["system"]["attached_late"] is True
    assert (session_dir / "system.wav").exists()


def test_success_banner_expires(cfg, world, monkeypatch):
    import meeting_notes.client.controller as ctl

    monkeypatch.setattr(ctl, "DEVICE_NOTICE_SECONDS", 0.5)
    world.add("system", FakeSource(name="Speakers", samplerate=RATE))
    controller = make_controller(world)
    controller.start_device_watch(interval=0.1)
    controller.start("expire")
    try:
        world.add("mic", FakeSource(name="Mic", samplerate=RATE))
        assert wait_for(lambda: any(b["level"] == "ok" for b in controller.device_banners()))
        assert wait_for(lambda: controller.device_banners() == [], timeout=3.0)
    finally:
        controller.stop_device_watch()
        controller.stop()


# --------------------------------------------------------------------------
# losing a device mid-recording
# --------------------------------------------------------------------------


def test_mic_lost_mid_recording_leaves_a_gap_then_reattaches(cfg, world):
    headset = SwitchSource(name="Logitech PRO X", samplerate=RATE, freq=80.0)
    world.add("mic", headset)
    world.add("system", FakeSource(name="Speakers", samplerate=RATE))
    controller = make_controller(world)
    controller.start_device_watch(interval=0.1)
    session_dir = controller.start("lost-mic")
    assert session_dir is not None
    try:
        time.sleep(1.5)
        assert controller.device_banners() == []
        headset.broken = True
        world.remove("mic", headset)  # switched off: gone from the OS too
        assert wait_for(lambda: any(b["level"] == "error" for b in controller.device_banners()), timeout=4.0)
        (banner,) = [b for b in controller.device_banners() if b["track"] == "mic"]
        assert banner["text"].startswith("Microphone disconnected at 00:00:0")
        assert "not being recorded" in banner["text"]
        assert controller.state == RECORDING  # the session carries on
        assert controller.device_labels()["mic"].endswith("(lost)")

        time.sleep(1.5)
        # It comes back as the same device (a new object, as Windows would give).
        headset2 = SwitchSource(name="Logitech PRO X", samplerate=RATE, freq=80.0)
        world.add("mic", headset2)
        assert wait_for(lambda: controller.session.recorders["mic"].source is headset2, timeout=8.0)
        assert wait_for(
            lambda: any(b["level"] == "ok" and "reconnected" in b["text"] for b in controller.device_banners()),
            timeout=4.0,
        )
        time.sleep(1.0)
    finally:
        controller.stop_device_watch()
        meta = controller.stop()

    mic_meta = meta["tracks"]["mic"]
    assert mic_meta["degraded"] is True
    assert mic_meta["gaps"], "the outage must be recorded as a gap"
    assert sum(g["seconds_lost"] for g in mic_meta["gaps"]) >= 1.0
    assert wav_seconds(session_dir / "mic.wav") == pytest.approx(wav_seconds(session_dir / "system.wav"), abs=1.5)
    kinds = [e["kind"] for e in meta["events"] if e["track"] == "mic"]
    assert "device-lost" in kinds and "replace" in kinds
    clock = load_timing_log(session_dir / "mic.timing.jsonl")
    lost = mic_meta["gaps"][0]
    assert clock.in_gap((lost["frames_before"] + 10) / RATE)


def test_lost_mic_falls_back_to_the_new_default_when_the_same_one_is_gone(cfg, world):
    headset = SwitchSource(name="Headset", samplerate=RATE)
    laptop = FakeSource(name="Laptop Mic", samplerate=RATE)
    world.add("mic", headset)
    world.add("mic", laptop, default=False)
    world.add("system", FakeSource(name="Speakers", samplerate=RATE))
    controller = make_controller(world)
    controller.start_device_watch(interval=0.1)
    controller.start("fallback")
    try:
        time.sleep(1.0)
        headset.broken = True
        world.remove("mic", headset)  # default becomes the laptop mic
        assert wait_for(lambda: controller.session.recorders["mic"].source is laptop, timeout=8.0)
    finally:
        controller.stop_device_watch()
        controller.stop()


def test_healthy_devices_are_not_swapped_when_the_default_changes(cfg, world):
    original = FakeSource(name="Original Mic", samplerate=RATE)
    world.add("mic", original)
    world.add("system", FakeSource(name="Speakers", samplerate=RATE))
    controller = make_controller(world)
    controller.start_device_watch(interval=0.05)
    controller.start("no-hopping")
    try:
        time.sleep(0.4)
        world.add("mic", FakeSource(name="New Default", samplerate=RATE), default=True)
        assert wait_for(lambda: controller.device_watcher.snapshot.name("mic") == "New Default")
        time.sleep(1.0)
        rec = controller.session.recorders["mic"]
        assert rec.source is original
        assert rec.abandoned_threads == 0 and not rec.degraded
        assert controller.device_banners() == []
    finally:
        controller.stop_device_watch()
        meta = controller.stop()
    assert meta["tracks"]["mic"]["device"] == "Original Mic"


def test_device_that_will_not_open_is_reported_not_hidden(cfg, world):
    dead = SwitchSource(name="Dead Mic", samplerate=RATE)
    dead.broken = True
    world.add("mic", dead)
    world.add("system", FakeSource(name="Speakers", samplerate=RATE))
    controller = make_controller(world)
    controller.start("cannot-open")
    try:
        assert wait_for(lambda: any(b["track"] == "mic" for b in controller.device_banners()), timeout=5.0)
        (banner,) = [b for b in controller.device_banners() if b["track"] == "mic"]
        assert banner["level"] == "error"
        assert banner["text"].startswith("Can't open the microphone")
    finally:
        controller.stop()


def test_attach_is_refused_once_the_session_is_stopping(cfg, world):
    world.add("system", FakeSource(name="Speakers", samplerate=RATE))
    controller = make_controller(world)
    controller.start("stopping")
    session = controller.session
    controller.stop()
    assert session.attach_source("mic", FakeSource(name="Late", samplerate=RATE)) is False
    assert "mic" not in session.recorders


def test_missing_text_constants_match_the_spec():
    assert missing_device_text("mic").startswith("No microphone found — you are not being recorded.")
    assert missing_device_text("system").startswith("Can't hear the meeting")


# --------------------------------------------------------------------------
# the window
# --------------------------------------------------------------------------


def test_device_change_message_filter():
    from meeting_notes.client.ui.devicechange import (
        DBT_DEVICEARRIVAL,
        DBT_DEVNODES_CHANGED,
        WM_DEVICECHANGE,
        is_device_change,
    )

    assert is_device_change(WM_DEVICECHANGE, DBT_DEVNODES_CHANGED)
    assert is_device_change(WM_DEVICECHANGE, DBT_DEVICEARRIVAL)
    assert not is_device_change(WM_DEVICECHANGE, 0x0018)
    assert not is_device_change(0x0001, DBT_DEVNODES_CHANGED)


def test_window_shows_red_then_green_banner_and_enables_mute(cfg, world, qt_app_hotplug):
    from meeting_notes.client.ui.main_window import MainWindow

    world.add("system", FakeSource(name="Speakers", samplerate=RATE))
    controller = make_controller(world)
    window = MainWindow(controller)
    window._timer.stop()
    controller.start_device_watch(interval=0.1)
    try:
        window._start()
        window._tick()
        assert window.controller.state == RECORDING
        assert window.device_bar.isVisibleTo(window)
        assert window.device_label.text().startswith("No microphone found")
        assert not window.mute_mic_button.isEnabled() and window.mute_system_button.isEnabled()
        assert "You: not connected" in window.devices_label.text()
        assert not window.device_ok_bar.isVisibleTo(window)

        world.add("mic", FakeSource(name="Headset", samplerate=RATE))
        assert wait_for(lambda: "mic" in controller.session.recorders, timeout=4.0)
        window._tick()
        assert not window.device_bar.isVisibleTo(window)
        assert window.device_ok_bar.isVisibleTo(window)
        assert "recording you from now on" in window.device_ok_label.text()
        assert window.mute_mic_button.isEnabled()
        assert "You: Headset" in window.devices_label.text()
    finally:
        controller.stop_device_watch()
        controller.stop()
    window._apply_stopped_ui(None)
    window._tick()
    assert not window.device_bar.isVisibleTo(window)


def test_idle_window_label_follows_the_watcher(cfg, world, qt_app_hotplug):
    from meeting_notes.client.ui.main_window import MainWindow

    controller = make_controller(world)
    window = MainWindow(controller)
    window._timer.stop()
    controller.start_device_watch(interval=0.1)
    try:
        world.add("mic", FakeSource(name="USB Mic", samplerate=RATE))
        assert wait_for(lambda: "USB Mic" in (window._tick() or window.devices_label.text()))
    finally:
        controller.stop_device_watch()


@pytest.fixture(scope="module")
def qt_app_hotplug():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from meeting_notes.client.ui.theme import APP_STYLE

    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(APP_STYLE)
    return app
