"""Regressions for bugs found in review. Each fails against the code as written
before its fix, so these are guards, not decoration.
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from meeting_notes.audio.session import RecordingSession
from meeting_notes.timing import FrameClock, build_clock
from meeting_notes.transcribe.merge import merge_tracks
from meeting_notes.transcribe.protocol import Segment
from tests.fakes import RELEASE, UnopenableSource

RATE = 1000


@pytest.fixture(autouse=True)
def release_stalled_threads():
    RELEASE.clear()
    yield
    RELEASE.set()
    time.sleep(0.05)


def test_unopenable_device_does_not_storm_restarts(tmp_path):
    """A device that never opens must be retried on the stall interval.

    last_progress only advances on a successful capture, so counting the
    watchdog from it alone meant a permanently dead device was restarted on
    every supervisor tick (4x/sec) for the whole meeting, spawning thousands of
    threads and defeating the worker's own exponential backoff.
    """
    source = UnopenableSource(name="ghost", samplerate=RATE)
    session = RecordingSession(
        session_dir=tmp_path,
        sources={"mic": source},
        block_seconds=0.05,
        stall_timeout=1.0,
    )
    session.start()
    stopper = threading.Timer(3.0, session.request_stop)
    stopper.start()
    session.supervise()
    meta = session.finalize(join_timeout=0.5)
    stopper.cancel()

    stalls = [e for e in meta["events"] if e["kind"] == "stall"]
    # ~2 expected over 3s at a 1s stall timeout. The pre-fix behaviour was one
    # per 0.25s tick, i.e. 8+.
    assert len(stalls) <= 4, f"restart storm: {len(stalls)} restarts in 3s"


def test_merge_ignores_a_track_whose_timing_log_is_empty(tmp_path):
    """An empty clock reports start_monotonic 0.0; that sentinel must not win
    the rebase, or every timestamp becomes hours-large garbage."""
    base = 10_000.0
    real = build_clock(
        [
            {"event": "open", "segment": 0, "frames": 0, "t": base, "samplerate": RATE},
            {"event": "progress", "segment": 0, "frames": RATE * 10, "t": base + 10.0},
        ]
    )
    empty = FrameClock(samplerate=RATE, frames=np.asarray([]), times=np.asarray([]))
    assert empty.start_monotonic == 0.0

    merged = merge_tracks(
        {"mic": [Segment(1.0, 2.0, "hello", "mic")], "system": []},
        {"mic": real, "system": empty},
        {"mic": "You", "system": "Them"},
    )

    assert merged, "the track with a real clock should still be placed"
    # Session-relative, so ~1s in. Rebasing on the 0.0 sentinel gave ~10001s.
    assert merged[0]["start"] == pytest.approx(1.0, abs=0.2)


def test_merge_with_only_empty_clocks_returns_nothing(tmp_path):
    empty = FrameClock(samplerate=RATE, frames=np.asarray([]), times=np.asarray([]))
    assert merge_tracks({"mic": [Segment(1.0, 2.0, "x", "mic")]}, {"mic": empty}, {}) == []


def test_transcript_header_shows_the_session_date():
    """render_markdown read session_meta['date'], but sessions record
    'created', so the date line never appeared in any real transcript."""
    from meeting_notes.transcribe.merge import render_markdown

    out = render_markdown(
        [{"start": 0.0, "end": 1.0, "track": "mic", "label": "You", "text": "hi", "in_gap": False}],
        {"created": "2026-09-20T14:30:00", "duration_sec": 61},
    )
    assert "2026-09-20" in out
