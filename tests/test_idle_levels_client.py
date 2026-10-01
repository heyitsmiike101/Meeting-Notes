"""Live input levels before recording starts, on the recorder (the window's "Preview").

Covers the meter itself (``client/idle_meter.py``), how the controller starts / retargets / hands over to a
recording, and the window's rules (visible, watched, setting off, recording). Everything runs on the fake
AudioSource layer from ``tests/fakes.py``: no audio hardware, no network.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from meeting_notes import config as config_mod
from meeting_notes.client import idle_meter as idle_meter_mod
from meeting_notes.client.controller import IDLE, RECORDING, idle_meter_kinds
from meeting_notes.client.idle_meter import IdleMeter, idle_meter_wanted
from meeting_notes.timing import load_timing_log
from tests.fakes import FakeReader, FakeSource
from tests.test_device_hotplug import World, make_controller, wait_for

RATE = 1000


class TrackedSource(FakeSource):
    """Records how many opens overlap, so a handover that lets two readers hold a device shows up."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.open_now = 0
        self.max_open = 0
        self.closes = 0
        self.events = []
        self._lock = threading.Lock()

    @contextmanager
    def open(self):
        with self._lock:
            self.open_now += 1
            self.max_open = max(self.max_open, self.open_now)
            self.opens += 1
            self.events.append(("open", time.monotonic()))
        try:
            yield FakeReader(self)
        finally:
            with self._lock:
                self.open_now -= 1
                self.closes += 1
                self.events.append(("close", time.monotonic()))


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    monkeypatch.setenv("MEETING_NOTES_CONFIG", str(path))
    config_mod.save_config({"save_dir": str(tmp_path / "rec")}, path)
    return path


@pytest.fixture
def world():
    return World()


# -- rules ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "enabled,idle,visible,watched,expected",
    [
        (True, True, True, False, True),     # window on screen
        (True, True, False, True, True),     # hidden, but a web viewer is watching
        (True, True, True, True, True),
        (True, True, False, False, False),   # minimized to nowhere, nobody looking: devices stay closed
        (False, True, True, True, False),    # setting off beats everything
        (True, False, True, True, False),    # recording / finishing: the recorder owns the devices
    ],
)
def test_idle_meter_wanted_rules(enabled, idle, visible, watched, expected):
    assert idle_meter_wanted(enabled=enabled, idle=idle, window_visible=visible, watched=watched) is expected


def test_mac_meters_the_microphone_only():
    assert idle_meter_kinds("darwin") == ("mic",)
    assert idle_meter_kinds("win32") == ("mic", "system")


def test_the_setting_defaults_on_and_is_strictly_boolean():
    assert config_mod.idle_levels_enabled({}) is True
    assert config_mod.idle_levels_enabled({"show_audio_levels": False}) is False
    assert config_mod.idle_levels_enabled({"show_audio_levels": True}) is True
    for junk in ("false", 0, None, "no"):
        assert config_mod.idle_levels_enabled({"show_audio_levels": junk}) is True


# -- the meter ----------------------------------------------------------------------------------


def test_meter_reports_the_peak_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    mic = TrackedSource(name="Mic", samplerate=RATE, amplitude=0.5)
    meter = IdleMeter(block_seconds=0.05)
    meter.set_sources({"mic": mic})
    try:
        assert wait_for(lambda: meter.levels().get("mic", 0) > 0.3, timeout=3)
        assert 0.3 < meter.levels()["mic"] <= 0.5 + 1e-6
        assert meter.tracks() == ["mic"] and meter.active
    finally:
        assert meter.stop(1.0) is True
    assert not meter.active and meter.levels() == {}
    assert list(tmp_path.iterdir()) == []   # no file of any kind
    assert mic.closes == 1 and mic.open_now == 0


def test_levels_use_the_same_peak_as_a_recording():
    import numpy as np

    from meeting_notes.audio.track_recorder import block_peak

    block = np.array([[0.1, -0.7], [0.2, 0.3]], dtype=np.float32)
    assert block_peak(block) == pytest.approx(0.7)
    assert block_peak(np.zeros((4, 1), dtype=np.float32)) == 0.0


def test_unchanged_device_is_not_reopened_but_a_different_one_retargets():
    first = TrackedSource(name="Headset", samplerate=RATE)
    meter = IdleMeter(block_seconds=0.05)
    meter.set_sources({"mic": first})
    try:
        assert wait_for(lambda: first.opens == 1)
        # A re-scan hands back a fresh object for the same device: nothing restarts.
        same = TrackedSource(name="Headset", samplerate=RATE)
        meter.set_sources({"mic": same})
        time.sleep(0.2)
        assert first.opens == 1 and same.opens == 0 and first.open_now == 1
        # A different device: the old one is released and the new one metered.
        other = TrackedSource(name="Laptop Mic", samplerate=RATE, amplitude=0.25)
        meter.set_sources({"mic": other})
        assert wait_for(lambda: other.opens == 1 and first.open_now == 0)
        assert wait_for(lambda: 0.1 < meter.levels().get("mic", 0) <= 0.25 + 1e-6)
        # The device vanished (no source): that lane stops.
        meter.set_sources({})
        assert wait_for(lambda: other.open_now == 0)
        assert meter.levels() == {}
    finally:
        meter.stop(1.0)


def test_a_failing_device_shows_zero_and_is_retried(monkeypatch):
    monkeypatch.setattr(idle_meter_mod, "RETRY_SECONDS", 0.05)

    class Flaky(TrackedSource):
        broken = True

        @contextmanager
        def open(self):
            if self.broken:
                self.opens += 1
                raise OSError("device not present")
            with super().open() as reader:
                yield reader

    mic = Flaky(name="USB Mic", samplerate=RATE, amplitude=0.4)
    meter = IdleMeter(block_seconds=0.05)
    meter.set_sources({"mic": mic})
    try:
        assert wait_for(lambda: "mic" in meter.errors())
        assert meter.levels() == {"mic": 0.0}
        mic.broken = False
        assert wait_for(lambda: meter.levels().get("mic", 0) > 0.2, timeout=3)
        assert meter.errors() == {}
    finally:
        meter.stop(1.0)


def test_a_wedged_device_cannot_hold_up_stop():
    gate = threading.Event()                  # own gate: the shared fakes.RELEASE may already be set

    class Wedged(FakeSource):
        @contextmanager
        def open(self):
            with super().open() as reader:
                class Stuck:
                    def read(self_inner, n):
                        gate.wait(timeout=5)  # a native read that does not return
                        return reader.read(n)
                yield Stuck()

    meter = IdleMeter(block_seconds=0.05)
    meter.set_sources({"mic": Wedged(name="Stuck", samplerate=RATE)})
    time.sleep(0.15)
    began = time.monotonic()
    assert meter.stop(0.1) is False          # abandoned, not waited for
    assert time.monotonic() - began < 0.5
    assert not meter.active and meter.levels() == {}
    gate.set()                                # let the parked thread go
    assert wait_for(lambda: meter.stop(0.2), timeout=3)


# -- controller: start/stop rules, retargeting, handover to a recording ------------------------------


def test_controller_meters_while_wanted_and_lets_go_otherwise(cfg, world):
    mic = TrackedSource(name="Mic", samplerate=RATE, amplitude=0.4)
    speakers = TrackedSource(name="Speakers", samplerate=RATE, amplitude=0.2)
    world.add("mic", mic)
    world.add("system", speakers)
    controller = make_controller(world)
    controller.start_device_watch(interval=0.1)
    try:
        assert wait_for(lambda: controller.device_watcher.has_snapshot)
        assert controller.idle_levels() == {} and not controller.idle_meter_active   # not asked yet
        controller.set_idle_wanted(True)
        assert wait_for(lambda: controller.idle_meter_active)
        assert wait_for(lambda: controller.idle_levels().get("mic", 0) > 0.2 and controller.idle_levels().get("system", 0) > 0.1)
        # Not wanted any more (window hidden, nobody watching): every device is released.
        controller.set_idle_wanted(False)
        assert wait_for(lambda: mic.open_now == 0 and speakers.open_now == 0)
        assert not controller.idle_meter_active and controller.idle_levels() == {}
    finally:
        controller.stop_device_watch()
        controller.stop_idle_meter(1.0)


def test_controller_waits_for_the_first_device_scan(cfg, world):
    world.add("mic", TrackedSource(name="Mic", samplerate=RATE))
    controller = make_controller(world)
    controller.set_idle_wanted(True)         # no watcher yet: nothing to meter
    assert not controller.idle_meter_active
    controller.stop_idle_meter(1.0)


def test_mac_style_controller_meters_only_the_microphone(cfg, world):
    mic = TrackedSource(name="Mic", samplerate=RATE)
    speakers = TrackedSource(name="Speakers", samplerate=RATE)
    world.add("mic", mic)
    world.add("system", speakers)
    controller = make_controller(world)
    controller.idle_kinds = idle_meter_kinds("darwin")
    controller.start_device_watch(interval=0.1)
    try:
        controller.set_idle_wanted(True)
        assert wait_for(lambda: controller.idle_meter_active)
        assert wait_for(lambda: "mic" in controller.idle_levels())
        assert "system" not in controller.idle_levels()
        time.sleep(0.2)
        assert speakers.opens == 0           # the system device is never opened while idle
    finally:
        controller.stop_device_watch()
        controller.stop_idle_meter(1.0)


def test_device_changes_retarget_the_idle_meter(cfg, world):
    headset = TrackedSource(name="Headset", samplerate=RATE, amplitude=0.3)
    world.add("mic", headset)
    controller = make_controller(world)
    controller.start_device_watch(interval=0.1)
    try:
        controller.set_idle_wanted(True)
        assert wait_for(lambda: headset.opens == 1 and controller.idle_levels().get("mic", 0) > 0.1)
        # The user picks (or the OS switches to) another default device.
        laptop = TrackedSource(name="Laptop Mic", samplerate=RATE, amplitude=0.1)
        world.add("mic", laptop, default=True)
        assert wait_for(lambda: laptop.opens == 1 and headset.open_now == 0)
        assert wait_for(lambda: 0.05 < controller.idle_levels().get("mic", 0) <= 0.1 + 1e-6)
        # Unplugged: the bar goes away instead of showing a stale level.
        world.remove("mic", laptop)
        world.remove("mic", headset)
        assert wait_for(lambda: "mic" not in controller.idle_levels())
    finally:
        controller.stop_device_watch()
        controller.stop_idle_meter(1.0)


def test_starting_a_recording_takes_the_devices_over_cleanly(cfg, world, tmp_path):
    mic = TrackedSource(name="Mic", samplerate=RATE, amplitude=0.4)
    speakers = TrackedSource(name="Speakers", samplerate=RATE, amplitude=0.2)
    world.add("mic", mic)
    world.add("system", speakers)
    controller = make_controller(world)
    controller.start_device_watch(interval=0.1)
    save_dir = tmp_path / "rec"
    try:
        controller.set_idle_wanted(True)
        assert wait_for(lambda: mic.open_now == 1 and speakers.open_now == 1)
        assert not save_dir.exists() or list(save_dir.iterdir()) == []   # metering saved nothing
        session_dir = controller.start("handover")
        assert session_dir is not None and controller.state == RECORDING
        # The meter let go before the recorder opened: never two readers on one device at once.
        assert mic.max_open == 1 and speakers.max_open == 1
        assert mic.events[1][0] == "close" and mic.events[2][0] == "open"
        assert not controller.idle_meter_active and controller.idle_levels() == {}
        # While recording, a "keep metering" request (a stray tick) cannot reopen the devices.
        controller.set_idle_wanted(True)
        time.sleep(0.3)
        assert mic.max_open == 1 and speakers.max_open == 1
        assert wait_for(lambda: controller.levels().get("mic", 0) > 0.2)
        time.sleep(1.2)
    finally:
        controller.stop_device_watch()
        controller.stop()
    # The recording is complete from its first frame: no gap or padding at the handover.
    timing = load_timing_log(session_dir / "mic.timing.jsonl")
    assert timing.gaps == []
    # Back to idle: the preview can resume.
    assert controller.state == IDLE
    controller.start_device_watch(interval=0.1)
    try:
        controller.set_idle_wanted(True)
        assert wait_for(lambda: controller.idle_meter_active)
    finally:
        controller.stop_device_watch()
        controller.stop_idle_meter(1.0)


def test_a_start_in_flight_blocks_the_meter_from_restarting(cfg, world):
    world.add("mic", TrackedSource(name="Mic", samplerate=RATE))
    controller = make_controller(world)
    controller.start_device_watch(interval=0.1)
    try:
        assert wait_for(lambda: controller.device_watcher.has_snapshot)
        controller._starting = True          # what start() holds while it opens the devices
        controller.set_idle_wanted(True)
        controller.device_watcher.poll_once()
        assert not controller.idle_meter_active
        controller._starting = False
        controller.set_idle_wanted(True)
        assert wait_for(lambda: controller.idle_meter_active)
    finally:
        controller.stop_device_watch()
        controller.stop_idle_meter(1.0)


# -- the window ------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def qt_app_levels():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from meeting_notes.client.ui.theme import APP_STYLE

    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(APP_STYLE)
    return app


class FakeChannel:
    def __init__(self, watched=False):
        self.watched = watched
        self.levels_published = []
        self.published = []

    def start(self):
        pass

    def stop(self, join_timeout=1.0):
        pass

    def publish(self, snapshot):
        self.published.append(snapshot)

    def publish_levels(self, levels):
        self.levels_published.append(levels)

    def send_ack(self, *a, **kw):
        pass


def _window(cfg, world, qt_app_levels, channel=None, monkeypatch=None):
    from meeting_notes.client.ui.main_window import MainWindow

    controller = make_controller(world)
    channel = channel or FakeChannel()
    window = MainWindow(controller, remote_channel_factory=lambda on_command: channel)
    for timer in ("_timer", "_detect_timer", "_auth_timer", "_update_timer", "_remote_timer"):
        getattr(window, timer).stop()
    window.channel = channel
    controller.start_device_watch(interval=0.1)
    assert wait_for(lambda: controller.device_watcher.has_snapshot)
    return window, controller


def _close(window, controller):
    controller.stop_device_watch()
    controller.stop_idle_meter(1.0)
    window._teardown_done = True
    window.close()


def test_visible_window_previews_and_a_hidden_one_lets_go(cfg, world, qt_app_levels):
    mic = TrackedSource(name="Mic", samplerate=RATE, amplitude=0.4)
    world.add("mic", mic)
    window, controller = _window(cfg, world, qt_app_levels)
    try:
        window._tick()                                    # not shown: nothing is opened
        assert not controller.idle_meter_active and mic.opens == 0
        assert not window.waveform.preview
        window.show()
        window._tick()
        assert wait_for(lambda: controller.idle_meter_active)
        assert wait_for(lambda: (window._tick() or True) and window.waveform.peak("mic") > 0.2)
        assert window.waveform.preview
        window.showMinimized()
        window._tick()
        assert wait_for(lambda: mic.open_now == 0)
        window._tick()
        assert not window.waveform.preview
    finally:
        _close(window, controller)


def test_hidden_window_still_previews_while_a_web_viewer_watches(cfg, world, qt_app_levels):
    mic = TrackedSource(name="Mic", samplerate=RATE, amplitude=0.4)
    world.add("mic", mic)
    channel = FakeChannel(watched=False)
    window, controller = _window(cfg, world, qt_app_levels, channel)
    try:
        window._tick()
        assert not controller.idle_meter_active           # hidden, nobody watching
        channel.watched = True
        window._tick()
        assert wait_for(lambda: controller.idle_meter_active)
        assert wait_for(lambda: (window._tick() or True) and any(l and l.get("mic", 0) > 0.2 for l in channel.levels_published))
        channel.watched = False                           # last viewer left
        window._tick()
        assert wait_for(lambda: mic.open_now == 0)
        window._tick()
        assert channel.levels_published[-1] is None       # nothing is published without a meter
    finally:
        _close(window, controller)


def test_setting_off_keeps_every_device_closed(cfg, world, qt_app_levels):
    config_mod.save_config({**config_mod.load_config(), "show_audio_levels": False}, cfg)
    mic = TrackedSource(name="Mic", samplerate=RATE)
    world.add("mic", mic)
    channel = FakeChannel(watched=True)
    window, controller = _window(cfg, world, qt_app_levels, channel)
    try:
        window.show()
        for _ in range(3):
            window._tick()
        time.sleep(0.3)
        assert mic.opens == 0 and not controller.idle_meter_active and not window.waveform.preview
        state = window.build_remote_state()
        assert state["preview"] == {"supported": False, "active": False, "tracks": ["mic", "system"]}
    finally:
        _close(window, controller)


def test_remote_state_reports_preview_levels_then_recording_levels(cfg, world, qt_app_levels):
    world.add("mic", TrackedSource(name="Mic", samplerate=RATE, amplitude=0.4))
    window, controller = _window(cfg, world, qt_app_levels)
    try:
        window.show()
        window._tick()
        assert wait_for(lambda: controller.idle_meter_active and controller.idle_levels().get("mic", 0) > 0.2)
        state = window.build_remote_state()
        assert state["status"] == "idle"
        assert state["preview"]["supported"] is True and state["preview"]["active"] is True
        assert state["tracks"]["mic"]["level"] > 0.2 and state["tracks"]["mic"]["peak"] == state["tracks"]["mic"]["level"]
        window._start()
        assert controller.state == RECORDING
        window._tick()
        state = window.build_remote_state()
        assert state["status"] == "recording" and state["preview"]["active"] is False
        assert not window.waveform.preview
    finally:
        _close(window, controller)
        controller.stop()


def test_waveform_paints_preview_and_unavailable_tracks(qt_app_levels):
    from PySide6.QtGui import QPixmap

    from meeting_notes.client.ui.waveform import WaveformWidget

    w = WaveformWidget()
    for i in range(100):
        w.push({"mic": (i % 10) / 10, "system": 0.5})
    w.set_preview(True)
    w.set_track_unavailable("system", "System audio is not previewed on macOS.")
    assert w.preview and "not previewed on macOS" in w.toolTip() and "Nothing is recorded" in w.toolTip()
    for width in (300, 600):
        w.resize(width, 200)
        pixmap = QPixmap(w.size())
        w.render(pixmap)
        assert not pixmap.isNull()
    w.set_preview(False)
    assert "Nothing is recorded" not in w.toolTip()
    assert w.track_unavailable("system")
    w.set_track_unavailable("system", "")
    assert w.toolTip() == ""
