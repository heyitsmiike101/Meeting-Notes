"""Capture-layer tests: the failure modes that actually lose a meeting.

Everything here runs against FakeSource, so it is fully deterministic and needs
no audio hardware -- which is the point, since CI runners have none.
"""

from __future__ import annotations

import json
import queue
import threading
import time
import wave
from contextlib import contextmanager

import numpy as np
import pytest

from meeting_notes.audio.session import RecordingSession
from meeting_notes.audio.track_recorder import TrackRecorder, describe_error
from meeting_notes.timing import load_timing_log
from tests.fakes import (
    RELEASE,
    FakeSource,
    HealsOnReopenSource,
    RightChannelOnlySource,
)

RATE = 1000
BLOCK = 0.05


@pytest.fixture(autouse=True)
def release_stalled_threads():
    RELEASE.clear()
    yield
    # Let any thread parked in a simulated stall fall out, so wedged fakes
    # never leak across tests.
    RELEASE.set()
    time.sleep(0.05)


def make_recorder(tmp_path, source, track="mic", **kw):
    stop = threading.Event()
    errors: "queue.Queue" = queue.Queue()
    rec = TrackRecorder(
        track, source, tmp_path, stop, errors, block_seconds=BLOCK, progress_interval=0.01, **kw
    )
    return rec, stop, errors


def test_records_audio_and_finalizes(tmp_path):
    source = FakeSource(samplerate=RATE, channels=2)
    rec, stop, _ = make_recorder(tmp_path, source)
    rec.start()
    time.sleep(0.5)
    stop.set()
    rec.close()

    assert rec.frames > 0
    raw = np.fromfile(tmp_path / "mic.raw", dtype="<i2")
    assert raw.size == rec.frames
    # A 0.5 amplitude sine should land well inside int16 range, never clipped.
    assert 0 < np.abs(raw).max() < 32767


def test_stereo_is_downmixed_not_left_channel_only(tmp_path):
    """Guards the soundcard channels=1 trap: right-channel audio must survive.

    If anything in the chain kept only channel 0, this source -- silent left,
    signal right -- would record as pure digital silence and nothing would
    raise. That is exactly how the bug would reach a real meeting.
    """
    source = RightChannelOnlySource(samplerate=RATE, channels=2)
    rec, stop, _ = make_recorder(tmp_path, source)
    rec.start()
    time.sleep(0.3)
    stop.set()
    rec.close()

    raw = np.fromfile(tmp_path / "mic.raw", dtype="<i2")
    assert raw.size > 0
    assert np.abs(raw).max() > 1000, "right-channel audio was discarded"


def test_device_disconnect_is_reported_and_file_is_intact(tmp_path):
    source = FakeSource(samplerate=RATE, raise_after=3)
    rec, stop, errors = make_recorder(tmp_path, source)
    rec.start()
    time.sleep(0.6)
    stop.set()
    rec.close()

    err = errors.get_nowait()
    assert err.track == "mic"
    assert "disconnected" in err.message
    assert rec.degraded is True
    # Whatever was captured before the device vanished must still be there.
    assert rec.frames > 0


class _BareAssertionSource:
    """A source whose ``open()`` fails with an ``AssertionError`` carrying no
    message -- the exact shape of soundcard's bare ``assert`` on the WASAPI
    mix format tag (see soundcard_source.py's WASAPI patch). Used to prove
    the recorder never reports such a failure as the useless bare
    "AssertionError: "."""

    name = "bare-assert-device"
    channels = 1
    samplerate = RATE

    @contextmanager
    def open(self):
        # Deliberately `raise AssertionError()` rather than a bare `assert`
        # statement: pytest rewrites asserts in test files to carry a
        # generated message, which would defeat the point of this fixture --
        # soundcard's own bare assert (in site-packages, never rewritten by
        # pytest) has a genuinely empty str().
        raise AssertionError()
        yield  # pragma: no cover - unreachable, keeps this a generator


def test_error_with_empty_message_still_names_something_useful(tmp_path):
    """describe_error's fallback: an exception with an empty str() must not
    collapse the queued TrackError into 'AssertionError: ' with nothing
    after the colon -- session.json would be useless for debugging that."""
    source = _BareAssertionSource()
    rec, stop, errors = make_recorder(tmp_path, source)
    rec.start()
    time.sleep(0.1)
    stop.set()
    rec.close()

    err = errors.get_nowait()
    assert err.track == "mic"
    assert err.message.startswith("AssertionError")
    assert err.message != "AssertionError: "
    assert not err.message.endswith(": ")
    # Falls back to the traceback's innermost frame, e.g.
    # "AssertionError (tests/test_track_recorder.py:123)".
    assert ":" in err.message


def test_describe_error_falls_back_to_traceback_location():
    """Unit-level check of describe_error itself, independent of the
    recorder: a message-less exception resolves to file:line, a normal one
    keeps its own message untouched."""
    try:
        raise AssertionError()  # deliberately message-less, see note above
    except AssertionError as exc:
        described = describe_error(exc)
    assert described.startswith("AssertionError (")
    assert described.endswith(")")
    assert ":" in described

    normal = ValueError("bad samplerate")
    try:
        raise normal
    except ValueError as exc:
        assert describe_error(exc) == "ValueError: bad samplerate"

    # No traceback at all (never raised) -- must still not blow up.
    assert describe_error(AssertionError()) == "AssertionError"


def test_recovers_when_device_comes_back(tmp_path):
    source = HealsOnReopenSource(samplerate=RATE)
    rec, stop, errors = make_recorder(tmp_path, source)
    rec.start()
    # First open raises; the recorder backs off 1s and tries again.
    time.sleep(1.6)
    stop.set()
    rec.close()

    assert source.opens >= 2
    assert rec.frames > 0, "should have captured audio after the device returned"


def test_stall_trips_watchdog_and_sibling_keeps_recording(tmp_path):
    """The macOS nightmare: one device wedges forever inside a blocking read."""
    stalling = FakeSource(name="wedged", samplerate=RATE, stall_after=2)
    healthy = FakeSource(name="healthy", samplerate=RATE)

    session = RecordingSession(
        session_dir=tmp_path,
        sources={"mic": stalling, "system": healthy},
        block_seconds=BLOCK,
        stall_timeout=0.4,
        progress_interval=0.01,
    )
    session.start()
    stopper = threading.Timer(1.6, session.request_stop)
    stopper.start()
    session.supervise()
    meta = session.finalize(join_timeout=0.5)
    stopper.cancel()

    stalls = [e for e in meta["events"] if e["kind"] == "stall"]
    assert stalls, "watchdog should have fired on the wedged device"
    assert stalls[0]["track"] == "mic"

    # The healthy track must be entirely unaffected by its sibling wedging.
    assert meta["tracks"]["system"]["frames"] > 0
    assert meta["tracks"]["system"]["degraded"] is False
    assert meta["tracks"]["mic"]["degraded"] is True

    # And the wedged track must still produce a playable WAV.
    with wave.open(str(tmp_path / "mic.wav")) as fh:
        assert fh.getnchannels() == 1
        assert fh.getframerate() == RATE


def test_gap_is_padded_with_silence_to_hold_alignment(tmp_path):
    """After a stall, frame position must still mean the same wall-clock offset."""
    stalling = FakeSource(name="wedged", samplerate=RATE, stall_after=2)
    session = RecordingSession(
        session_dir=tmp_path,
        sources={"mic": stalling},
        block_seconds=BLOCK,
        stall_timeout=0.4,
        progress_interval=0.01,
    )
    session.start()
    stopper = threading.Timer(1.6, session.request_stop)
    stopper.start()
    session.supervise()
    meta = session.finalize(join_timeout=0.5)
    stopper.cancel()

    gaps = meta["tracks"]["mic"]["gaps"]
    assert gaps, "a stall must be recorded as a gap"
    assert gaps[0]["seconds_lost"] > 0

    clock = load_timing_log(tmp_path / "mic.timing.jsonl")
    assert clock.gaps
    # A position inside the padded stretch is flagged, one at the start is not.
    inside = (clock.gaps[0].frames_before + 1) / RATE
    assert clock.in_gap(inside) is True
    assert clock.in_gap(0.0) is False

    # Total duration should be close to real elapsed time despite the stall,
    # because the lost stretch was padded rather than spliced out.
    assert meta["tracks"]["mic"]["duration_sec"] == pytest.approx(1.6, abs=0.6)


def test_stop_is_prompt(tmp_path):
    source = FakeSource(samplerate=RATE)
    session = RecordingSession(
        session_dir=tmp_path, sources={"mic": source}, block_seconds=BLOCK, stall_timeout=5.0
    )
    session.start()
    time.sleep(0.3)
    began = time.monotonic()
    session.request_stop()
    session.finalize(join_timeout=1.0)
    # Shutdown must take about one block, not hang on the reader.
    assert time.monotonic() - began < 1.0


def test_session_json_is_written_and_complete(tmp_path):
    session = RecordingSession(
        session_dir=tmp_path,
        sources={"mic": FakeSource(samplerate=RATE), "system": FakeSource(samplerate=RATE)},
        block_seconds=BLOCK,
    )
    session.start()
    time.sleep(0.3)
    session.request_stop()
    session.finalize()

    meta = json.loads((tmp_path / "session.json").read_text())
    assert set(meta["tracks"]) == {"mic", "system"}
    assert meta["tracks"]["mic"]["label"] == "You"
    assert meta["tracks"]["system"]["label"] == "Them"
    for track in ("mic", "system"):
        assert meta["tracks"][track]["duration_sec"] > 0
        assert (tmp_path / f"{track}.wav").exists()
        # raw files are removed once safely wrapped
        assert not (tmp_path / f"{track}.raw").exists()
