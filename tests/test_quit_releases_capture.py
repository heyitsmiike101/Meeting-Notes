"""Quitting always releases macOS system-audio capture (the purple screen-recording indicator).

The ScreenCaptureKit layer is faked; nothing here needs a Mac.
"""

from __future__ import annotations

import sys
import threading
import types

import pytest

from meeting_notes.audio import screencapture_source as sck
from meeting_notes.client import app as app_mod
from meeting_notes.client.controller import IDLE, RECORDING, RecordingController


@pytest.fixture(autouse=True)
def _clean_registry():
    """The stream registry is module state: start and end every test with it empty."""
    def clear():
        with sck._OPEN_LOCK:
            sck._OPEN_STREAMS.clear()
            sck._UNCONFIRMED.clear()
    clear()
    yield
    clear()


class _Handle(sck.StreamHandle):
    def __init__(self):
        self.stops = 0

    def stop(self):
        self.stops += 1


def _factory(handles):
    def make(on_audio, on_error):
        h = _Handle()
        handles.append(h)
        return h

    return make


def test_stop_all_streams_stops_a_stream_whose_owner_thread_is_wedged():
    handles = []
    source = sck.ScreenCaptureKitSource(stream_factory=_factory(handles))
    entered, release = threading.Event(), threading.Event()

    def owner():  # a recorder thread that never reaches its ``finally`` in time
        with source.open():
            entered.set()
            release.wait(10)

    t = threading.Thread(target=owner, daemon=True)
    t.start()
    assert entered.wait(2)
    assert sck.open_stream_count() == 1
    assert sck.stop_all_streams() == 1
    assert handles[0].stops == 1 and sck.open_stream_count() == 0
    release.set()
    t.join(2)
    assert sck.open_stream_count() == 0
    assert sck.stop_all_streams() == 0  # idempotent


def test_normal_close_unregisters_the_stream():
    handles = []
    with sck.ScreenCaptureKitSource(stream_factory=_factory(handles)).open():
        assert sck.open_stream_count() == 1
    assert sck.open_stream_count() == 0 and handles[0].stops == 1


def test_one_failing_stop_does_not_block_the_others():
    class Bad(_Handle):
        def stop(self):
            raise RuntimeError("boom")

    good = _Handle()
    with sck._OPEN_LOCK:
        sck._OPEN_STREAMS[1] = (Bad(), sck.BlockQueue())
        sck._OPEN_STREAMS[2] = (good, sck.BlockQueue())
    assert sck.stop_all_streams() == 2
    assert good.stops == 1


class _FakeStream:
    def __init__(self, answers_start):
        self.answers_start = answers_start
        self.stop_calls = 0

    def addStreamOutput_type_sampleHandlerQueue_error_(self, *a):
        return True, None

    def startCaptureWithCompletionHandler_(self, cb):
        if self.answers_start:
            cb(None)

    def stopCaptureWithCompletionHandler_(self, cb):
        self.stop_calls += 1
        cb(None)


def _fake_sck(monkeypatch, stream):
    noop = lambda *a: None  # noqa: E731
    config = types.SimpleNamespace(
        **{
            n: noop
            for n in (
                "setCapturesAudio_",
                "setExcludesCurrentProcessAudio_",
                "setSampleRate_",
                "setChannelCount_",
                "setWidth_",
                "setHeight_",
                "setMinimumFrameInterval_",
                "setShowsCursor_",
            )
        }
    )
    content = types.SimpleNamespace(displays=lambda: [object()])
    scs = types.SimpleNamespace(
        SCShareableContent=types.SimpleNamespace(
            getShareableContentWithCompletionHandler_=lambda cb: cb(content, None)
        ),
        SCContentFilter=types.SimpleNamespace(
            alloc=lambda: types.SimpleNamespace(initWithDisplay_excludingWindows_=lambda d, w: object())
        ),
        SCStreamConfiguration=types.SimpleNamespace(alloc=lambda: types.SimpleNamespace(init=lambda: config)),
        SCStream=types.SimpleNamespace(
            alloc=lambda: types.SimpleNamespace(initWithFilter_configuration_delegate_=lambda f, c, h: stream)
        ),
        SCStreamOutputTypeAudio=1,
        SCStreamOutputTypeScreen=0,
    )
    monkeypatch.setitem(sys.modules, "ScreenCaptureKit", scs)
    monkeypatch.setitem(sys.modules, "CoreMedia", types.SimpleNamespace(CMTimeMake=lambda a, b: None))
    monkeypatch.setitem(sys.modules, "objc", types.SimpleNamespace())
    monkeypatch.setitem(sys.modules, "Foundation", types.SimpleNamespace(NSObject=object))
    monkeypatch.setattr(sck, "available", lambda: (True, ""))
    monkeypatch.setattr(sck, "permission_granted", lambda: True)

    class H:
        @classmethod
        def alloc(cls):
            return types.SimpleNamespace(init=lambda: types.SimpleNamespace())

    monkeypatch.setattr(sck, "_handler_class", lambda: H)


def test_a_stream_that_times_out_while_starting_is_stopped_not_leaked(monkeypatch):
    stream = _FakeStream(answers_start=False)
    _fake_sck(monkeypatch, stream)
    with pytest.raises(sck.SystemAudioUnavailable):
        sck.ObjcStream.start(lambda b: None, lambda e: None, timeout=0.05)
    assert stream.stop_calls == 1


def test_objc_stream_stop_is_idempotent(monkeypatch):
    stream = _FakeStream(answers_start=True)
    _fake_sck(monkeypatch, stream)
    handle = sck.ObjcStream.start(lambda b: None, lambda e: None, timeout=1)
    handle.stop()
    handle.stop()
    assert stream.stop_calls == 1


def test_controller_shutdown_saves_recording_releases_meter_and_streams(monkeypatch):
    calls = []
    c = RecordingController(device_resolver=lambda *a, **k: None)
    c.state = RECORDING
    monkeypatch.setattr(c, "stop", lambda: calls.append("stop"))
    monkeypatch.setattr(c, "stop_idle_meter", lambda *a: calls.append("meter"))
    monkeypatch.setattr(c, "stop_device_watch", lambda: calls.append("watch"))
    monkeypatch.setattr(sck, "stop_all_streams", lambda: calls.append("streams"))
    c.shutdown()
    assert calls == ["stop", "meter", "watch", "streams"]
    c.state = IDLE
    c.shutdown()  # idempotent and never raises


def test_controller_shutdown_survives_a_failing_stop(monkeypatch):
    calls = []
    c = RecordingController(device_resolver=lambda *a, **k: None)
    c.state = RECORDING

    def boom():
        raise RuntimeError("x")

    monkeypatch.setattr(c, "stop", boom)
    monkeypatch.setattr(sck, "stop_all_streams", lambda: calls.append("streams"))
    c.shutdown()
    assert calls == ["streams"]


def test_app_quit_cleanup_runs_controller_shutdown_once(monkeypatch):
    hooks = []
    app = types.SimpleNamespace(aboutToQuit=types.SimpleNamespace(connect=hooks.append))
    shutdowns = []
    window = types.SimpleNamespace(controller=types.SimpleNamespace(shutdown=lambda: shutdowns.append(1)))
    registered = []
    monkeypatch.setattr("atexit.register", registered.append)
    monkeypatch.setattr(app_mod.sys, "platform", "win32")  # no force-exit watchdog off macOS
    app_mod._install_quit_cleanup(app, window)
    hooks[0]()
    registered[0]()
    assert shutdowns == [1]


def test_macos_quit_arms_a_force_exit_watchdog(monkeypatch):
    hooks, timers = [], []
    app = types.SimpleNamespace(aboutToQuit=types.SimpleNamespace(connect=hooks.append))
    window = types.SimpleNamespace(controller=types.SimpleNamespace(shutdown=lambda: None))
    monkeypatch.setattr("atexit.register", lambda f: None)
    monkeypatch.setattr(app_mod.sys, "platform", "darwin")

    class FakeTimer:
        def __init__(self, delay, fn):
            self.delay, self.fn, self.daemon, self.started = delay, fn, False, False
            timers.append(self)

        def start(self):
            self.started = True

    monkeypatch.setattr("threading.Timer", FakeTimer)
    app_mod._install_quit_cleanup(app, window)
    hooks[0]()
    assert timers and timers[0].started and timers[0].daemon


# -- lid closed mid-recording: sleep and wake ------------------------------------------------------


class _Sleepy(sck.StreamHandle):
    """A stream whose stop macOS does not confirm until ``answers`` is set (as around sleep)."""

    def __init__(self):
        self.stops = 0
        self.answers = False

    def stop(self):
        self.stops += 1
        return self.answers


def test_an_unconfirmed_stop_is_remembered_and_retried():
    handle = _Sleepy()
    with sck.ScreenCaptureKitSource(stream_factory=lambda a, e: handle).open():
        pass
    assert handle.stops == 1 and sck.open_stream_count() == 1  # not forgotten
    assert sck.retry_unconfirmed_stops() == 1  # still unanswered
    handle.answers = True
    assert sck.retry_unconfirmed_stops() == 0
    assert handle.stops == 3 and sck.open_stream_count() == 0


def test_quitting_retries_an_unconfirmed_stop():
    handle = _Sleepy()
    with sck.ScreenCaptureKitSource(stream_factory=lambda a, e: handle).open():
        pass
    handle.answers = True
    assert sck.stop_all_streams() == 1
    assert handle.stops == 2 and sck.open_stream_count() == 0


def test_the_next_open_retries_an_unconfirmed_stop_in_the_background():
    old = _Sleepy()
    with sck.ScreenCaptureKitSource(stream_factory=lambda a, e: old).open():
        pass
    old.answers = True
    new = _Handle()
    with sck.ScreenCaptureKitSource(stream_factory=lambda a, e: new).open():
        for _ in range(100):
            if old.stops >= 2:
                break
            threading.Event().wait(0.02)
    assert old.stops == 2 and sck.open_stream_count() == 0


def test_objc_stream_keeps_a_stream_whose_stop_is_not_confirmed(monkeypatch):
    stream = _FakeStream(answers_start=True)
    _fake_sck(monkeypatch, stream)
    handle = sck.ObjcStream.start(lambda b: None, lambda e: None, timeout=1)
    stream.stopCaptureWithCompletionHandler_ = lambda cb: setattr(stream, "stop_calls", stream.stop_calls + 1)
    monkeypatch.setattr(threading.Event, "wait", lambda self, timeout=None: self.is_set())
    assert handle.stop() is False and handle._stream is stream  # kept for another try
    stream.stopCaptureWithCompletionHandler_ = lambda cb: cb(None)
    assert handle.stop() is True and handle._stream is None


def test_session_reopen_track_restarts_the_recorder(tmp_path):
    from meeting_notes.audio.session import RecordingSession

    restarts = []
    session = RecordingSession.__new__(RecordingSession)
    session._attach_lock = threading.Lock()
    session._closing = False
    session.stop_event = threading.Event()
    session.recorders = {"system": types.SimpleNamespace(restart=restarts.append)}
    session.events = []
    session.started_monotonic = 0.0
    assert session.reopen_track("system", "system-wake") is True
    assert restarts == ["system-wake"] and session.events[-1].kind == "reopen"
    assert session.reopen_track("mic", "system-wake") is False
    session.stop_event.set()
    assert session.reopen_track("system", "system-wake") is False and restarts == ["system-wake"]


def test_wake_reopens_system_audio_only_while_recording(monkeypatch):
    reopened, retried = [], []
    c = RecordingController(device_resolver=lambda *a, **k: None)
    c.session = types.SimpleNamespace(recorders={"system": object(), "mic": object()},
                                      reopen_track=lambda t, r: reopened.append((t, r)))
    monkeypatch.setattr(sck, "retry_unconfirmed_stops_async", lambda: retried.append(1))
    c.state = IDLE
    c.on_system_wake()
    assert reopened == [] and retried == [1]
    c.state = RECORDING
    c.on_system_sleep()
    c.on_system_wake()
    assert reopened == [("system", "system-wake")] and retried == [1, 1]


def test_wake_never_raises(monkeypatch):
    c = RecordingController(device_resolver=lambda *a, **k: None)
    c.state = RECORDING

    def boom(*a):
        raise RuntimeError("x")

    c.session = types.SimpleNamespace(recorders={"system": object()}, reopen_track=boom)
    monkeypatch.setattr(sck, "retry_unconfirmed_stops_async", boom)
    c.on_system_wake()


def test_power_hooks_are_a_no_op_off_macos_and_handlers_never_raise(monkeypatch):
    from meeting_notes.client import power_mac

    monkeypatch.setattr(power_mac.sys, "platform", "win32")
    calls = []

    def boom():
        raise RuntimeError("x")

    assert power_mac.install(lambda: calls.append("sleep"), boom) is False
    power_mac._call("sleep")
    power_mac._call("wake")  # raising handler is swallowed
    assert calls == ["sleep"]


def test_app_wires_sleep_and_wake_to_the_controller(monkeypatch):
    from meeting_notes.client import power_mac

    got = []
    monkeypatch.setattr(power_mac, "install", lambda s, w: got.append((s, w)) or True)
    controller = types.SimpleNamespace(on_system_sleep=lambda: None, on_system_wake=lambda: None)
    app_mod._install_power_hooks(types.SimpleNamespace(controller=controller))
    assert got == [(controller.on_system_sleep, controller.on_system_wake)]
