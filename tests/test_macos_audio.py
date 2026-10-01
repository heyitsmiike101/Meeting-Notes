"""macOS system audio: ScreenCaptureKit source logic and device selection.

Everything here runs on any OS: CoreMedia is replaced by a tiny fake, the
capture stream by a factory that feeds numpy blocks, and TCC (the permission
database) by monkeypatched probes. The real PyObjC bridge is exercised by
docs/manual-testing.md on a Mac.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from meeting_notes.audio import devices, screencapture_source as sck
from meeting_notes.audio.session import RecordingSession


# -- a fake CoreMedia --------------------------------------------------------------


class FakeCoreMedia:
    """Just the five CoreMedia functions ``extract_pcm`` calls."""

    def __init__(self, *, rate=48000.0, channels=2, float_=True, bits=32, planar=False, payload=b"", frames=0):
        flags = (1 if float_ else 4) | 8 | (32 if planar else 0)
        self.asbd = SimpleNamespace(
            mSampleRate=rate, mFormatFlags=flags, mChannelsPerFrame=channels, mBitsPerChannel=bits
        )
        self.payload = payload
        self.frames = frames
        self.copy_status = 0

    def CMSampleBufferGetFormatDescription(self, sbuf):
        return "desc"

    def CMAudioFormatDescriptionGetStreamBasicDescription(self, desc):
        return self.asbd

    def CMSampleBufferGetNumSamples(self, sbuf):
        return self.frames

    def CMSampleBufferGetDataBuffer(self, sbuf):
        return "block"

    def CMBlockBufferGetDataLength(self, block):
        return len(self.payload)

    def CMBlockBufferCopyDataBytes(self, block, offset, length, dest):
        return self.copy_status, self.payload[offset : offset + length]


def _ramp(frames):
    left = np.linspace(0.0, 1.0, frames, dtype=np.float32)
    return left, -left


def test_interleaved_float_sample_buffer_becomes_frames_by_channels():
    left, right = _ramp(480)
    data = np.stack([left, right], axis=1).astype("<f4").tobytes()
    cm = FakeCoreMedia(payload=data, frames=480)
    out = sck.sample_buffer_to_array(object(), cm)
    assert out.shape == (480, 2) and out.dtype == np.float32
    assert np.allclose(out[:, 0], left) and np.allclose(out[:, 1], right)


def test_planar_float_sample_buffer_is_channel_major():
    """ScreenCaptureKit delivers non-interleaved float32: all of L, then all of R."""
    left, right = _ramp(480)
    data = np.concatenate([left, right]).astype("<f4").tobytes()
    cm = FakeCoreMedia(payload=data, frames=480, planar=True)
    out = sck.sample_buffer_to_array(object(), cm)
    assert out.shape == (480, 2)
    assert np.allclose(out[:, 0], left) and np.allclose(out[:, 1], right)


def test_int16_is_scaled_to_unit_range():
    samples = np.array([[0, 0], [16384, -16384], [32767, -32768]], dtype="<i2")
    cm = FakeCoreMedia(payload=samples.tobytes(), frames=3, float_=False, bits=16)
    out = sck.sample_buffer_to_array(object(), cm)
    assert np.allclose(out[1], [0.5, -0.5])
    assert out[2, 1] == -1.0


def test_mono_and_wrong_rate_are_conformed_to_48k_stereo():
    mono = np.ones(441, dtype="<f4")
    cm = FakeCoreMedia(payload=mono.tobytes(), frames=441, channels=1, rate=44100.0)
    out = sck.sample_buffer_to_array(object(), cm)
    assert out.shape[1] == 2
    assert abs(len(out) - 480) <= 1
    assert np.allclose(out, 1.0)


def test_frame_count_trims_padding_and_short_data_is_tolerated():
    left, right = _ramp(100)
    data = np.stack([left, right], axis=1).astype("<f4").tobytes() + b"\x00" * 16
    out = sck.sample_buffer_to_array(object(), FakeCoreMedia(payload=data, frames=100))
    assert out.shape == (100, 2)
    out = sck.sample_buffer_to_array(object(), FakeCoreMedia(payload=data[: 8 * 50 + 3], frames=100))
    assert out.shape == (50, 2)


def test_copy_failure_and_missing_buffers_raise():
    cm = FakeCoreMedia(payload=b"\x00" * 8, frames=1)
    cm.copy_status = -12345
    with pytest.raises(ValueError, match="CMBlockBufferCopyDataBytes"):
        sck.extract_pcm(object(), cm)
    cm.CMSampleBufferGetDataBuffer = lambda sbuf: None
    with pytest.raises(ValueError, match="no data buffer"):
        sck.extract_pcm(object(), cm)


def test_unsupported_format_is_rejected():
    fmt = sck.PcmFormat(48000.0, 2, is_float=False, bits=24, non_interleaved=False)
    with pytest.raises(ValueError):
        sck.pcm_bytes_to_array(b"\x00" * 12, fmt)


# -- BlockQueue: the recorder's blocking read, kept on wall-clock time ---------------------


def _queue(rate=1000, slack=0.02):
    return sck.BlockQueue(rate=rate, channels=2, slack=slack)


def test_read_returns_exactly_the_requested_frames_in_order():
    q = _queue()
    q.feed(np.arange(150, dtype=np.float32).reshape(75, 2))
    q.feed(np.arange(150, 300, dtype=np.float32).reshape(75, 2))
    first = q.read(100)
    assert first.shape == (100, 2)
    assert first[0, 0] == 0 and first[99, 1] == 199
    second = q.read(50)
    assert second[0, 0] == 200 and second[49, 1] == 299


def test_silent_stream_is_padded_so_the_timeline_keeps_wall_clock_pace():
    q = _queue(rate=1000, slack=0.02)
    started = time.monotonic()
    blocks = [q.read(100) for _ in range(5)]  # 0.5 s of audio, none ever arrives
    elapsed = time.monotonic() - started
    assert all(b.shape == (100, 2) and not b.any() for b in blocks)
    # Deadlines are absolute, so slack does not accumulate: ~0.5 s, not 5 x (0.1 + slack) drift.
    assert 0.45 <= elapsed <= 0.75
    assert q.padded_frames == 500


def test_late_frames_after_padding_are_dropped_so_the_track_does_not_run_ahead():
    q = _queue(rate=1000, slack=0.01)
    q.read(100)  # nothing arrived: 100 frames of silence, debt = 100
    assert q.padded_frames == 100
    q.feed(np.ones((60, 2), dtype=np.float32))  # the late audio: swallowed by the debt
    q.feed(np.full((100, 2), 0.5, dtype=np.float32))  # 40 of debt left, 60 usable frames
    assert q.dropped_frames == 100
    out = q.read(60)
    assert np.allclose(out, 0.5)


def test_stream_error_is_raised_to_the_recorder_after_buffered_audio_drains():
    q = _queue()
    q.feed(np.ones((40, 2), dtype=np.float32))
    q.fail(RuntimeError("stream stopped"))
    assert q.read(40).shape == (40, 2)
    with pytest.raises(RuntimeError, match="stream stopped"):
        q.read(40)


def test_a_stream_that_delivers_nothing_at_all_is_reported_dead():
    q = sck.BlockQueue(rate=1000, slack=0.01, stall_after=0.2)
    q.read(50)  # young stream: padded silence, no complaint yet
    time.sleep(0.25)
    with pytest.raises(RuntimeError, match="delivered no audio"):
        q.read(50)
    q.feed(np.ones((50, 2), dtype=np.float32))  # a live stream (even quiet) is never called dead
    assert q.read(50).shape == (50, 2)


def test_close_unblocks_a_waiting_reader():
    q = _queue(rate=1, slack=30)  # would wait ~31 s
    result = {}

    def reader():
        result["block"] = q.read(1)

    t = threading.Thread(target=reader)
    t.start()
    time.sleep(0.05)
    q.close()
    t.join(2)
    assert not t.is_alive()
    assert result["block"].shape == (1, 2)


def test_only_real_stalls_are_reported_as_warnings():
    q = _queue(rate=1000, slack=0.01)
    q.read(100)  # 0.1 s of padding: normal while nothing plays
    assert q.drain_warnings() == []


# -- the AudioSource itself -----------------------------------------------------------------


class _FakeHandle(sck.StreamHandle):
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


def test_source_open_starts_the_stream_feeds_blocks_and_stops_it():
    handle = _FakeHandle()
    seen = {}

    def factory(on_audio, on_error):
        seen["on_audio"], seen["on_error"] = on_audio, on_error
        on_audio(np.full((480, 2), 0.25, dtype=np.float32))
        return handle

    source = sck.ScreenCaptureKitSource(stream_factory=factory)
    assert (source.name, source.channels, source.samplerate) == (sck.SOURCE_NAME, 2, 48000)
    with source.open() as reader:
        block = reader.read(480)
        assert block.shape == (480, 2) and np.allclose(block, 0.25)
        assert reader.drain_warnings() == []
    assert handle.stopped


def test_source_open_surfaces_permission_errors_from_the_factory():
    def factory(on_audio, on_error):
        raise sck.SystemAudioPermissionError()

    with pytest.raises(sck.SystemAudioPermissionError, match="Screen & System Audio Recording"):
        with sck.ScreenCaptureKitSource(stream_factory=factory).open():
            pass


def test_recording_session_writes_a_wall_clock_aligned_system_track(tmp_path):
    """The source plugs into the real recorder with no mac-specific code."""
    stop = threading.Event()

    def factory(on_audio, on_error):
        def pump():
            # ~real time delivery of 10 ms chunks, deliberately a bit bursty
            while not stop.is_set():
                on_audio(np.full((480, 2), 0.1, dtype=np.float32))
                time.sleep(0.01)

        threading.Thread(target=pump, daemon=True).start()
        return _FakeHandle()

    session = RecordingSession(
        session_dir=tmp_path / "s",
        sources={"system": sck.ScreenCaptureKitSource(stream_factory=factory)},
        block_seconds=0.25,
    )
    session.start()
    time.sleep(1.3)
    session.request_stop()
    stop.set()
    meta = session.finalize()
    track = meta["tracks"]["system"]
    assert track["samplerate"] == 48000 and track["channels"] == 2
    # ~1.3 s of wall clock: never more audio than time elapsed, never wildly less.
    assert 0.75 * 48000 <= track["frames"] <= 1.5 * 48000


# -- availability + the ObjC bridge refusing politely -------------------------------------------


def test_availability_is_false_off_macos_and_bridge_refuses(monkeypatch):
    monkeypatch.setattr(sck.sys, "platform", "win32")
    ok, why = sck.available()
    assert not ok and "macOS" in why
    assert sck.permission_granted() is None
    assert sck.request_permission() is False
    with pytest.raises(sck.SystemAudioUnavailable):
        sck.ObjcStream.start(lambda a: None, lambda e: None)


def test_old_macos_reports_the_version_requirement(monkeypatch):
    monkeypatch.setattr(sck.sys, "platform", "darwin")
    monkeypatch.setattr(sck, "macos_version", lambda: (12, 6))
    ok, why = sck.available()
    assert not ok and "macOS 13" in why


def test_permission_prompt_can_be_disabled_for_unattended_runs(monkeypatch):
    monkeypatch.setattr(sck.sys, "platform", "darwin")
    monkeypatch.setenv("MEETING_NOTES_NO_PERMISSION_PROMPT", "1")
    assert sck.request_permission() is False


# -- device selection on macOS -------------------------------------------------------------------


class _MacSoundcard:
    def __init__(self, names=("MacBook Pro Microphone",)):
        self._mics = [SimpleNamespace(name=n, id=f"id-{i}", channels=1) for i, n in enumerate(names)]

    def all_microphones(self, include_loopback=False):
        assert not include_loopback, "never ask soundcard for loopback on macOS"
        return list(self._mics)

    def default_microphone(self):
        return self._mics[0]

    def default_speaker(self):
        return SimpleNamespace(name="MacBook Pro Speakers")


@pytest.fixture
def mac(monkeypatch):
    """A macOS 13+ Mac with PyObjC; permission state set per test."""
    state = SimpleNamespace(granted=True, requests=0, soundcard=_MacSoundcard())
    monkeypatch.setattr(devices.sys, "platform", "darwin")
    monkeypatch.setattr(devices.soundcard_source, "import_soundcard", lambda: state.soundcard)
    monkeypatch.setattr(devices.screencapture_source, "available", lambda: (True, ""))
    monkeypatch.setattr(devices.screencapture_source, "permission_granted", lambda: state.granted)

    def request():
        state.requests += 1
        return False

    monkeypatch.setattr(devices.screencapture_source, "request_permission", request)
    monkeypatch.setattr(devices, "_permission_requested", False)
    return state


def test_screencapturekit_is_the_default_system_source(mac):
    systems = devices.list_system_sources()
    assert [s.id for s in systems] == [devices.SCK_DEVICE_ID]
    assert systems[0].is_default and systems[0].channels == 2 and systems[0].samplerate == 48000
    source = devices.resolve_source("system")
    assert isinstance(source, sck.ScreenCaptureKitSource)


def test_blackhole_stays_listed_and_can_be_chosen_by_name(mac):
    mac.soundcard = _MacSoundcard(("MacBook Pro Microphone", "BlackHole 2ch"))
    ids = [s.id for s in devices.list_system_sources()]
    assert ids[0] == devices.SCK_DEVICE_ID and len(ids) == 2
    chosen = devices.resolve_source("system", "blackhole")
    assert chosen.name == "BlackHole 2ch"


def test_permission_denied_without_a_driver_gives_the_settings_message(mac):
    mac.granted = False
    with pytest.raises(devices.DeviceNotFound) as raised:
        devices.resolve_source("system")
    assert "Screen & System Audio Recording" in str(raised.value)
    assert mac.requests == 0, "plain probing must never show the permission prompt"


def test_recording_start_requests_permission_once(mac):
    mac.granted = False
    for _ in range(2):
        with pytest.raises(devices.DeviceNotFound):
            devices.resolve_source("system", interactive=True)
    assert mac.requests == 1


def test_permission_denied_falls_back_to_an_installed_driver(mac):
    mac.granted = False
    mac.soundcard = _MacSoundcard(("MacBook Pro Microphone", "BlackHole 2ch"))
    systems = devices.list_system_sources()
    assert systems[0].name == "BlackHole 2ch" and systems[0].is_default
    assert systems[-1].id == devices.SCK_DEVICE_ID and not systems[-1].is_default
    source = devices.resolve_source("system", interactive=True)
    assert source.name == "BlackHole 2ch"
    assert mac.requests == 1, "the prompt is still shown once so the next recording needs no driver"


def test_old_macos_without_bindings_uses_only_the_driver_route(mac, monkeypatch):
    monkeypatch.setattr(devices.screencapture_source, "available", lambda: (False, "needs macOS 13 or newer"))
    assert devices.list_system_sources() == []
    with pytest.raises(devices.DeviceNotFound) as raised:
        devices.resolve_source("system")
    assert "BlackHole" in str(raised.value) and "macOS 13" in str(raised.value)


def test_windows_and_linux_never_list_screencapturekit(monkeypatch):
    monkeypatch.setattr(devices.sys, "platform", "linux")
    monkeypatch.setattr(devices.soundcard_source, "import_soundcard", lambda: _MacSoundcard())
    assert devices.list_system_sources() == []


# -- hot-plug with the ScreenCaptureKit system source --------------------------------------------
#
# A ScreenCaptureKit stream is not a soundcard device, so the device watcher treats
# "system" as "is ScreenCaptureKit usable right now" (available + permission) and the
# mic as a CoreAudio enumeration through soundcard. Attaching mid-recording then goes
# through the same RecordingSession.attach_source / late-attach-gap path as on Windows.


def test_prompt_system_permission_once_only_asks_when_denied_and_only_once(mac):
    mac.granted = True
    assert devices.prompt_system_permission_once() is False
    assert mac.requests == 0
    mac.granted = False
    devices.prompt_system_permission_once()
    devices.prompt_system_permission_once()
    assert mac.requests == 1


def test_the_device_scan_never_prompts_for_permission(mac):
    from meeting_notes.client.controller import RecordingController

    mac.granted = False
    controller = RecordingController()
    snap = controller._scan_devices()
    assert snap.sources["system"] is None
    assert "Screen & System Audio Recording" in snap.errors["system"]
    assert snap.name("mic") == "MacBook Pro Microphone"
    assert mac.requests == 0


def test_screencapturekit_attaches_mid_recording_when_it_becomes_usable(mac, monkeypatch, tmp_path):
    from meeting_notes import config as config_mod
    from meeting_notes.client.controller import RECORDING, RecordingController
    from tests.fakes import FakeSource
    from tests.test_device_hotplug import wait_for

    monkeypatch.setenv("MEETING_NOTES_CONFIG", str(tmp_path / "config.json"))
    config_mod.save_config({"save_dir": str(tmp_path / "rec")}, tmp_path / "config.json")
    monkeypatch.setattr(
        devices.screencapture_source,
        "ScreenCaptureKitSource",
        lambda samplerate, channels: FakeSource(name=sck.SOURCE_NAME, samplerate=1000),
    )
    mac.granted = False
    controller = RecordingController()
    controller.start_device_watch(interval=0.1)
    session_dir = controller.start("late-sck", sources={"mic": FakeSource(name="Mic", samplerate=1000)})
    try:
        assert controller.state == RECORDING
        assert wait_for(lambda: bool(controller.device_banners()))
        assert "system" not in controller.session.recorders
        mac.granted = True  # the user allowed it; the next scan offers ScreenCaptureKit
        assert wait_for(lambda: "system" in controller.session.recorders, timeout=4.0)
    finally:
        controller.stop_device_watch()
        meta = controller.stop()
    assert meta["tracks"]["system"]["attached_late"] is True
    assert (session_dir / "system.wav").exists()
