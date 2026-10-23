"""Bluetooth headsets: the other side is recorded whichever profile the call puts the earbuds in, a track that hears
nothing at all while the other one does is flagged, and overlapping GPU installs never delete each other."""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager

import numpy as np
import pytest

from meeting_notes.audio import devices
from meeting_notes.audio.devices import DeviceInfo
from meeting_notes.audio.mixed_source import MixedSource, call_profile_siblings

# -- which outputs belong together --------------------------------------------------------------------------


GRANT = [
    "Headphones (Realtek USB2.0 Audio)", "Speaker (Realtek(R) Audio)", "Speakers (Steam Streaming Speakers)",
    "Line (Realtek USB2.0 Audio)", "Headphones (WF-C710N)", "Headset (WF-C710N)",
]


def test_a_bluetooth_headsets_two_outputs_go_together():
    assert call_profile_siblings("Headphones (WF-C710N)", GRANT) == ["Headset (WF-C710N)"]
    assert call_profile_siblings("Headset (WF-C710N)", GRANT) == ["Headphones (WF-C710N)"]
    assert call_profile_siblings("Headphones (Pixel Buds)", ["Headphones (Pixel Buds)", "Headset Earphone (Pixel Buds)"]) == [
        "Headset Earphone (Pixel Buds)"]


def test_other_outputs_of_one_card_stay_apart():
    assert call_profile_siblings("Headphones (Realtek USB2.0 Audio)", GRANT) == []  # Line is not a call profile
    assert call_profile_siblings("Speaker (Realtek(R) Audio)", GRANT) == []
    assert call_profile_siblings("Speakers", ["Speakers", "Headset"]) == []


# -- recording them together ----------------------------------------------------------------------------------


class FakeReader:
    def __init__(self, blocks, fail_after=None, short=False):
        self.blocks = list(blocks)
        self.reads = 0
        self.fail_after = fail_after
        self.short = short

    def read(self, numframes):
        self.reads += 1
        if self.fail_after is not None and self.reads > self.fail_after:
            raise OSError("device gone")
        block = self.blocks.pop(0) if self.blocks else np.zeros((numframes, 1), np.float32)
        return block[: numframes // 2] if self.short else block[:numframes]

    def drain_warnings(self):
        return ["data discontinuity in recording"] if self.reads == 1 else []


class FakeSource:
    def __init__(self, name, reader=None, channels=1, samplerate=48000, fail_open=False):
        self.name, self.channels, self.samplerate = name, channels, samplerate
        self.reader = reader or FakeReader([])
        self.fail_open = fail_open
        self.closed = False

    @contextmanager
    def open(self):
        if self.fail_open:
            raise OSError("cannot open")
        try:
            yield self.reader
        finally:
            self.closed = True


def test_the_two_outputs_are_added_up_into_one_mono_track():
    stereo = np.array([[0.2, 0.4], [0.2, 0.4], [0.0, 0.0], [0.0, 0.0]], np.float32)  # music-quality profile
    call = np.array([[0.0], [0.0], [0.5], [0.5]], np.float32)  # the call profile, after the switch
    source = MixedSource(FakeSource("Headphones (X)", FakeReader([stereo]), channels=2), [FakeSource("Headset (X)", FakeReader([call]))])
    assert (source.name, source.channels, source.samplerate) == ("Headphones (X)", 1, 48000)
    with source.open() as reader:
        out = reader.read(4)
        assert reader.drain_warnings() == ["data discontinuity in recording"] * 2
    assert out.shape == (4, 1)
    assert np.allclose(out[:, 0], [0.3, 0.3, 0.5, 0.5])


def test_loud_sums_are_clipped():
    a = np.full((3, 1), 0.8, np.float32)
    source = MixedSource(FakeSource("A (X)", FakeReader([a])), [FakeSource("B (X)", FakeReader([a.copy()]))])
    with source.open() as reader:
        assert np.all(reader.read(3) == 1.0)


def test_an_extra_that_falls_short_is_padded_never_waited_for():
    a = np.full((4, 1), 0.1, np.float32)
    b = np.full((4, 1), 0.2, np.float32)
    source = MixedSource(FakeSource("A (X)", FakeReader([a, a])), [FakeSource("B (X)", FakeReader([b, b], short=True))])
    with source.open() as reader:
        assert np.allclose(reader.read(4)[:, 0], [0.3, 0.3, 0.1, 0.1])  # the chosen device sets the pace
        assert len(reader.read(4)) == 4


def test_an_extra_that_goes_away_is_dropped_and_the_track_carries_on(caplog):
    a = np.full((2, 1), 0.1, np.float32)
    extra = FakeSource("Headset (X)", FakeReader([a, a], fail_after=1))
    source = MixedSource(FakeSource("Headphones (X)", FakeReader([a, a, a])), [extra])
    with caplog.at_level(logging.WARNING), source.open() as reader:
        assert np.allclose(reader.read(2)[:, 0], 0.2)
        assert np.allclose(reader.read(2)[:, 0], 0.1)  # the extra failed: just the chosen device now
        assert reader.extra_names == []
        assert np.allclose(reader.read(2)[:, 0], 0.1)
    assert "stopped also recording Headset (X)" in caplog.text


def test_an_extra_that_will_not_open_is_skipped(caplog):
    a = np.full((2, 1), 0.1, np.float32)
    source = MixedSource(FakeSource("Headphones (X)", FakeReader([a])), [FakeSource("Headset (X)", fail_open=True)])
    with caplog.at_level(logging.WARNING), source.open() as reader:
        assert np.allclose(reader.read(2)[:, 0], 0.1)
    assert "could not also record Headset (X)" in caplog.text


def test_losing_the_chosen_device_ends_the_stream_like_any_device_loss():
    source = MixedSource(FakeSource("Headphones (X)", FakeReader([], fail_after=0)), [FakeSource("Headset (X)")])
    with source.open() as reader, pytest.raises(OSError):
        reader.read(2)


def _pairs(names):
    return [(object(), DeviceInfo(id=n, name=n, kind="system", channels=2, samplerate=48000, is_default=False)) for n in names]


def test_resolving_a_bluetooth_headset_records_both_of_its_outputs(monkeypatch):
    monkeypatch.setattr(devices, "_raw_system_sources", lambda raise_errors=False: _pairs(GRANT))
    monkeypatch.setattr(devices.sys, "platform", "win32")
    source = devices.resolve_source("system", "Headphones (WF-C710N)", samplerate=48000)
    assert isinstance(source, MixedSource)
    assert source.name == "Headphones (WF-C710N)" and source.extra_names == ["Headset (WF-C710N)"]
    assert source.extras[0].samplerate == 48000
    plain = devices.resolve_source("system", "Speaker (Realtek(R) Audio)")
    assert not isinstance(plain, MixedSource) and plain.name == "Speaker (Realtek(R) Audio)"


def test_only_windows_mixes(monkeypatch):
    monkeypatch.setattr(devices, "_raw_system_sources", lambda raise_errors=False: _pairs(GRANT))
    monkeypatch.setattr(devices.sys, "platform", "darwin")
    assert not isinstance(devices.resolve_source("system", "Headphones (WF-C710N)"), MixedSource)


# -- a track that hears nothing ------------------------------------------------------------------------------

pytest.importorskip("PySide6", reason="PySide6 not installed")

from tests.test_compact_bar import _record, qt_app, window  # noqa: E402,F401
from meeting_notes.client.ui import main_window as mw  # noqa: E402


def _levels(window, now, mic, system):
    window._last_levels = {"mic": mic, "system": system}
    window._note_levels(now, window._last_levels)


def test_the_other_side_silent_while_you_talk_is_flagged_after_three_minutes(window, monkeypatch, caplog):
    _record(window, monkeypatch)
    monkeypatch.setattr(window.controller, "device_labels", lambda: {"mic": "Headset (WF-C710N)", "system": "Headphones (WF-C710N)"})
    window._reset_dead_tracks()
    start = time.monotonic()
    _levels(window, start, 0.3, 0.0)
    assert window._dead_track_text(start + 60) is None
    _levels(window, start + 179, 0.3, 0.0)
    with caplog.at_level(logging.WARNING):
        text = window._dead_track_text(start + 181)
        again = window._dead_track_text(start + 182)
    assert text.startswith("Nothing from the other side for 3 minutes on Headphones (WF-C710N).")
    assert again == text and caplog.text.count("system track silent") == 1  # logged once per episode
    _levels(window, start + 183, 0.3, 0.01)  # sound again
    assert window._dead_track_text(start + 184) is None


def test_a_dead_mic_while_they_talk_is_flagged_after_a_minute(window, monkeypatch):
    _record(window, monkeypatch)
    monkeypatch.setattr(window.controller, "device_labels", lambda: {"mic": "Headset (WF-C710N)", "system": "Speakers"})
    window._reset_dead_tracks()
    start = time.monotonic()
    _levels(window, start + 59, 0.0, 0.2)
    assert window._dead_track_text(start + 61).startswith("Nothing from your microphone for 1 minute on Headset (WF-C710N).")


def test_a_quiet_room_or_a_pause_on_both_sides_is_not_a_dead_device(window, monkeypatch):
    _record(window, monkeypatch)
    window._reset_dead_tracks()
    start = time.monotonic()
    _levels(window, start + 100, 0.002, 0.2)  # a mic that only hears the room still delivers
    assert window._dead_track_text(start + 101) is None
    window._reset_dead_tracks()
    _levels(window, start, 0.0, 0.0)
    assert window._dead_track_text(start + 400) is None  # nobody talking: nothing to compare against


def test_a_muted_track_is_never_flagged(window, monkeypatch):
    _record(window, monkeypatch, muted={"system": True})
    window._reset_dead_tracks()
    start = time.monotonic()
    _levels(window, start + 300, 0.3, 0.0)
    assert window._dead_track_text(start + 301) is None


def test_the_warning_shows_in_the_device_strip(window, monkeypatch):
    _record(window, monkeypatch)
    monkeypatch.setattr(window.controller, "device_banners", lambda: [])
    monkeypatch.setattr(window.controller, "device_labels", lambda: {"mic": "M", "system": "S"})
    monkeypatch.setattr(window, "_dead_track_text", lambda now: "Nothing from the other side for 3 minutes on S.")
    window._sync_devices()
    assert window.device_label.text() == "Nothing from the other side for 3 minutes on S."
    assert not window.device_bar.isHidden()


# -- overlapping GPU installs --------------------------------------------------------------------------------

from tests.test_gpu_auto_install import Env  # noqa: E402
from tests.test_gpu_helper_client import home  # noqa: E402,F401


def test_an_older_install_ending_never_deletes_the_newer_one(qt_app, home, monkeypatch):
    env = Env(monkeypatch)
    env.auto.check()
    env.settle()
    older = env.installers[0]
    env.auto._installer = None  # as if a second install had been started on top of it (0.8.6 did this)
    env.auto._set_busy(False)
    env.auto._start_install("http://server.test", "pw", False)
    newer = env.installers[1]
    deleted = []
    monkeypatch.setattr(older, "deleteLater", lambda: deleted.append("older"))
    monkeypatch.setattr(newer, "deleteLater", lambda: deleted.append("newer"))
    older.finished.emit(False, "The server has no GPU helper pack yet.")
    assert deleted == ["older"] and env.auto._installer is newer and env.auto.busy
    newer.finished.emit(False, "The server has no GPU helper pack yet.")
    assert deleted == ["older", "newer"] and env.auto._installer is None and not env.auto.busy


def test_a_running_install_is_never_doubled(qt_app, home, monkeypatch):
    env = Env(monkeypatch)
    env.auto.check()
    env.settle()
    first = env.installers[0]
    first.running = lambda: True
    env.auto._start_install("http://server.test", "pw", False)
    assert env.installers == [first]


# -- Automatic never opens a headset mic the meeting apps do not use ------------------------------------------

from meeting_notes.audio import soundcard_source  # noqa: E402
from meeting_notes.audio.mixed_source import is_call_profile_name  # noqa: E402
from meeting_notes.client import settings_schema as S  # noqa: E402

MICS = ["Microphone Array (Intel Smart Sound Technology (Intel SST))", "Microphone (PHILIPS SPL6208 Mic)",
        "Headset (WF-C710N)"]


def _mics(names, default):
    return [(object(), DeviceInfo(id=f"id:{n}", name=n, kind="mic", channels=1, samplerate=48000, is_default=(n == default)))
            for n in names]


@pytest.fixture
def windows_mics(monkeypatch):
    monkeypatch.setattr(devices.sys, "platform", "win32")

    def use(names, default, comms):
        monkeypatch.setattr(devices, "_raw_microphones", lambda raise_errors=False: _mics(names, default))
        monkeypatch.setattr(soundcard_source, "default_communications_device_id",
                            lambda kind: None if comms is None else f"id:{comms}")

    return use


def test_headset_names():
    assert is_call_profile_name("Headset (WF-C710N)") and is_call_profile_name("Hands-Free AG Audio (Pixel Buds)")
    assert not is_call_profile_name("Headphones (WF-C710N)") and not is_call_profile_name("Microphone (PHILIPS SPL6208 Mic)")


def test_a_headset_mic_the_meeting_apps_use_is_recorded_from(windows_mics):
    windows_mics(MICS, default="Headset (WF-C710N)", comms="Headset (WF-C710N)")
    source = devices.resolve_source("mic")
    assert source.name == "Headset (WF-C710N)"
    assert source.pick_note.startswith("Recording from Headset (WF-C710N): it is the microphone your meeting apps use")


def test_a_headset_mic_the_meeting_apps_do_not_use_is_passed_over(windows_mics):
    windows_mics(MICS, default="Headset (WF-C710N)", comms="Microphone (PHILIPS SPL6208 Mic)")
    source = devices.resolve_source("mic")
    assert source.name == "Microphone (PHILIPS SPL6208 Mic)"  # the one the meeting apps use
    assert source.pick_note.startswith("Not recording from Headset (WF-C710N): your meeting apps do not use it")
    assert "Using Microphone (PHILIPS SPL6208 Mic) instead" in source.pick_note
    windows_mics(MICS, default="Headset (WF-C710N)", comms=None)  # Windows will not say: any other mic
    assert devices.resolve_source("mic").name == MICS[0]


def test_the_only_mic_being_a_headset_is_used_with_a_warning(windows_mics):
    windows_mics(["Headset (WF-C710N)"], default="Headset (WF-C710N)", comms="Microphone (X)")
    source = devices.resolve_source("mic")
    assert source.name == "Headset (WF-C710N)" and "the only one connected" in source.pick_note


def test_an_ordinary_default_mic_has_nothing_to_say(windows_mics):
    windows_mics(MICS, default="Microphone (PHILIPS SPL6208 Mic)", comms="Headset (WF-C710N)")
    source = devices.resolve_source("mic")
    assert source.name == "Microphone (PHILIPS SPL6208 Mic)" and source.pick_note == ""


def test_a_headset_mic_chosen_by_hand_is_always_used(windows_mics):
    windows_mics(MICS, default="Microphone (PHILIPS SPL6208 Mic)", comms="Microphone (PHILIPS SPL6208 Mic)")
    source = devices.resolve_source("mic", "Headset (WF-C710N)")
    assert source.name == "Headset (WF-C710N)" and source.pick_note == ""


def test_the_rule_is_windows_only(monkeypatch):
    monkeypatch.setattr(devices.sys, "platform", "darwin")
    monkeypatch.setattr(devices, "_raw_microphones", lambda raise_errors=False: _mics(MICS, "Headset (WF-C710N)"))
    assert devices.resolve_source("mic").name == "Headset (WF-C710N)"


def test_the_note_reaches_the_window_the_log_and_the_recording(monkeypatch, caplog, tmp_path):
    from meeting_notes.client.controller import RecordingController

    class Src:
        def __init__(self, name, note=""):
            self.name, self.channels, self.samplerate, self.pick_note = name, 1, 16000, note

    note = "Not recording from Headset (X): your meeting apps do not use it."
    ctl = RecordingController(device_resolver=lambda kind, requested=None, samplerate=None: Src("Mic", note) if kind == "mic" else Src("Speakers"))
    with caplog.at_level(logging.INFO):
        ctl.probe_devices()
        ctl.probe_devices()
    assert caplog.text.count("automatic mic device: Not recording from Headset (X)") == 1
    ctl.start_device_watch(interval=3600)
    try:
        assert ctl.device_notes() == {"mic": note}
    finally:
        ctl.stop_device_watch()


def test_settings_warn_about_a_headset_mic_on_windows_only():
    win = S.items_by_id(S.build_schema(platform="win32"))
    note = win["note.devices_bluetooth_mic"]
    assert "note.devices_bluetooth_mic" not in S.items_by_id(S.build_schema(platform="darwin"))
    assert S.is_shown(note, {"audio_devices.mic": "Headset (WF-C710N)"})
    assert S.is_shown(note, {"audio_devices.mic": "Hands-Free AG Audio (Buds)"})
    assert not S.is_shown(note, {"audio_devices.mic": ""})
    assert not S.is_shown(note, {"audio_devices.mic": "Headphones (WF-C710N)"})
    assert "call mode" in note["text"]


def test_the_web_settings_page_applies_the_same_condition():
    from pathlib import Path as _P

    from meeting_notes.server import web

    js = _P(web.__file__).read_text(encoding="utf-8")
    assert "if ('has' in c)" in js and "toLowerCase()" in js
