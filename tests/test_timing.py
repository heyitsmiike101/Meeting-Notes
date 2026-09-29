"""Hardware-free tests for meeting_notes.timing and meeting_notes.transcribe.merge.

The whole point of the FrameClock design is drift correction -- a track's
frame count is not a reliable clock on its own -- so these tests specifically
fabricate a device that runs faster than its nominal rate and assert the
clock tracks the *real* timing rather than naive frames/samplerate arithmetic.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from meeting_notes.timing import FrameClock, Gap, Segment, TimingLogWriter, build_clock, load_timing_log
from meeting_notes.transcribe.merge import merge_tracks
from meeting_notes.transcribe.protocol import Segment as TSegment


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _drift_entries(actual_rate: float, duration_s: float, interval_s: float, *, nominal_rate: int = 48000, start_t: float = 1000.0):
    """Fabricate a timing log as if a device produced frames at ``actual_rate``
    (not ``nominal_rate``) for ``duration_s`` seconds, logging progress every
    ``interval_s`` seconds -- exactly the shape a real TimingLogWriter produces.
    """
    entries = [
        {
            "event": "open",
            "segment": 0,
            "frames": 0,
            "t": start_t,
            "wall": 0.0,
            "samplerate": nominal_rate,
            "channels": 1,
            "device": "fake",
        }
    ]
    t = interval_s
    while t < duration_s:
        frames = int(round(actual_rate * t))
        entries.append({"event": "progress", "segment": 0, "frames": frames, "t": start_t + t})
        t += interval_s
    entries.append(
        {"event": "close", "segment": 0, "frames": int(round(actual_rate * duration_s)), "t": start_t + duration_s}
    )
    return entries


# --------------------------------------------------------------------------
# clean log: exact points + linear interpolation
# --------------------------------------------------------------------------


def test_clock_exact_points_and_linear_interpolation():
    entries = [
        {"event": "open", "segment": 0, "frames": 0, "t": 100.0, "wall": 0.0, "samplerate": 48000, "channels": 1, "device": "x"},
        {"event": "progress", "segment": 0, "frames": 48000, "t": 101.0},
        {"event": "progress", "segment": 0, "frames": 96000, "t": 102.0},
        {"event": "close", "segment": 0, "frames": 96000, "t": 102.0},
    ]
    clock = build_clock(entries)

    # Exactly at logged points.
    assert clock.monotonic_at(0) == pytest.approx(100.0)
    assert clock.monotonic_at(48000) == pytest.approx(101.0)
    assert clock.monotonic_at(96000) == pytest.approx(102.0)

    # Linearly between points.
    assert clock.monotonic_at(24000) == pytest.approx(100.5)
    assert clock.monotonic_at(72000) == pytest.approx(101.5)

    assert clock.samplerate == 48000
    assert clock.total_frames == 96000
    assert clock.start_monotonic == pytest.approx(100.0)


# --------------------------------------------------------------------------
# drift
# --------------------------------------------------------------------------


def test_segment_drift_ppm_recovers_real_rate():
    # Device nominally 48000 Hz but actually produces 48050 Hz -- e.g. a
    # cheap crystal running fast. Over 5 minutes that's easily measurable.
    entries = _drift_entries(actual_rate=48050.0, duration_s=300.0, interval_s=30.0)
    clock = build_clock(entries)

    assert len(clock.segments) == 1
    drift = clock.segments[0].drift_ppm
    assert drift is not None
    # (48050/48000 - 1) * 1e6 ~= 1041.7 ppm
    assert drift == pytest.approx(1041.7, abs=15.0)


def test_clock_tracks_real_timing_not_naive_nominal_rate():
    entries = _drift_entries(actual_rate=48050.0, duration_s=300.0, interval_s=30.0, start_t=0.0)
    clock = build_clock(entries)

    end_frame = 48050.0 * 300.0
    real_time = clock.monotonic_at(end_frame)
    naive_time = end_frame / 48000.0  # what you'd get ignoring drift entirely

    # The clock should reproduce the fabricated real schedule (t=300 at that
    # frame) almost exactly...
    assert real_time == pytest.approx(300.0, abs=0.05)
    # ...while the naive nominal-rate calculation has drifted meaningfully
    # away from it by the end of a 5-minute recording.
    assert abs(naive_time - real_time) > 0.2

    # And the gap should *grow* over time, not be some fixed/negligible
    # amount picked up front -- check an earlier point drifted less.
    mid_frame = 48050.0 * 150.0
    mid_real = clock.monotonic_at(mid_frame)
    mid_naive = mid_frame / 48000.0
    assert abs(mid_naive - mid_real) < abs(naive_time - real_time)


def test_drift_ppm_none_for_too_short_segment():
    seg = Segment(
        index=0,
        samplerate=48000,
        start_frame=0,
        end_frame=48000,
        start_monotonic=0.0,
        end_monotonic=1.0,  # well under the 5s minimum
    )
    assert seg.drift_ppm is None


def test_drift_ppm_none_for_zero_frames():
    seg = Segment(
        index=0,
        samplerate=48000,
        start_frame=1000,
        end_frame=1000,  # no frames elapsed
        start_monotonic=0.0,
        end_monotonic=10.0,
    )
    assert seg.drift_ppm is None


# --------------------------------------------------------------------------
# extrapolation
# --------------------------------------------------------------------------


def test_extrapolation_before_first_and_after_last_uses_nominal_rate():
    clock = FrameClock(
        samplerate=48000,
        frames=np.asarray([48000.0, 96000.0]),
        times=np.asarray([10.0, 11.0]),
    )
    # Before the first logged point: nominal-rate extrapolation backwards.
    before = clock.monotonic_at(0)
    assert before == pytest.approx(10.0 - 48000 / 48000)
    # After the last logged point: nominal-rate extrapolation forwards.
    after = clock.monotonic_at(96000 + 4800)
    assert after == pytest.approx(11.0 + 4800 / 48000)


def test_extrapolation_does_not_crash_with_single_point():
    clock = FrameClock(samplerate=48000, frames=np.asarray([1000.0]), times=np.asarray([5.0]))
    assert clock.monotonic_at(0) == pytest.approx(5.0 - 1000 / 48000)
    assert clock.monotonic_at(2000) == pytest.approx(5.0 + 1000 / 48000)


def test_extrapolation_empty_clock_does_not_crash():
    clock = FrameClock(samplerate=48000, frames=np.asarray([]), times=np.asarray([]))
    assert clock.monotonic_at(1234) == 0.0


# --------------------------------------------------------------------------
# in_gap
# --------------------------------------------------------------------------


def test_in_gap_detects_padded_stretch():
    clock = FrameClock(
        samplerate=48000,
        frames=np.asarray([0.0, 48000.0 * 10]),
        times=np.asarray([0.0, 10.0]),
        gaps=[Gap(frames_before=48000, frames_padded=24000, seconds_lost=0.5, reason="test")],
    )
    # frames_before=48000 (1.0s), frames_after=72000 (1.5s)
    assert clock.in_gap(0.99) is False
    assert clock.in_gap(1.0) is True  # inclusive lower bound
    assert clock.in_gap(1.25) is True
    assert clock.in_gap(1.5) is False  # exclusive upper bound
    assert clock.in_gap(2.0) is False


# --------------------------------------------------------------------------
# torn/truncated final log line
# --------------------------------------------------------------------------


def test_load_timing_log_skips_torn_final_line(tmp_path):
    path = tmp_path / "mic.timing.jsonl"
    writer = TimingLogWriter(path)
    writer.open_segment(frames=0, samplerate=48000, channels=1, device="fake")
    writer.progress(frames=48000, force=True)
    writer.close_segment(frames=48000)
    writer.close()

    good_line_count = len(path.read_text(encoding="utf-8").splitlines())
    assert good_line_count == 3  # open, progress, close

    # Simulate a kill mid-write: an incomplete JSON fragment with no
    # trailing newline appended after the last good line.
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"event":"progress","segment":0,"fra')

    clock = load_timing_log(path)
    # The good points are still usable -- the clock builds and is self
    # consistent at its known points, despite the torn tail.
    assert clock.total_frames == 48000
    assert clock.monotonic_at(clock.frames[-1]) == pytest.approx(float(clock.times[-1]))
    assert clock.monotonic_at(0) == pytest.approx(float(clock.times[0]))


# --------------------------------------------------------------------------
# merge_tracks: chronological interleaving across two clocks
# --------------------------------------------------------------------------


def test_merge_tracks_interleaves_chronologically_with_gap_and_rebase():
    # mic opens first (t=1000.0), system opens 2s later (t=1002.0) -- session
    # time 0.0 must rebase onto the EARLIEST of the two, i.e. mic's start.
    mic_clock = FrameClock(
        samplerate=48000,
        frames=np.asarray([0.0, 48000.0 * 60]),
        times=np.asarray([1000.0, 1060.0]),
        gaps=[Gap(frames_before=48000 * 20, frames_padded=48000 * 5, seconds_lost=5.0, reason="dropout")],
    )
    system_clock = FrameClock(
        samplerate=48000,
        frames=np.asarray([0.0, 48000.0 * 60]),
        times=np.asarray([1002.0, 1062.0]),
    )

    mic_segments = [
        TSegment(start=0.0, end=5.0, text="Hello from mic", track="mic"),
        TSegment(start=22.0, end=24.0, text="mic during gap", track="mic"),  # inside the 20-25s gap
        TSegment(start=40.0, end=45.0, text="mic after gap", track="mic"),
    ]
    system_segments = [
        TSegment(start=3.0, end=6.0, text="Hello from system", track="system"),
        TSegment(start=50.0, end=52.0, text="system later", track="system"),
    ]

    merged = merge_tracks(
        {"mic": mic_segments, "system": system_segments},
        {"mic": mic_clock, "system": system_clock},
    )

    assert len(merged) == 5
    # Chronological order, interleaved by track, not grouped track-by-track.
    tracks_in_order = [m["track"] for m in merged]
    assert tracks_in_order == ["mic", "system", "mic", "mic", "system"]

    # Rebased onto the earliest clock start (mic's t=1000.0).
    assert merged[0]["start"] == pytest.approx(0.0)
    assert merged[0]["label"] == "You"
    assert merged[1]["start"] == pytest.approx(5.0)
    assert merged[1]["label"] == "Them"
    assert merged[3]["start"] == pytest.approx(40.0)
    assert merged[4]["start"] == pytest.approx(52.0)

    # The mic segment sitting inside the logged gap is flagged; nothing else is.
    assert merged[2]["text"] == "mic during gap"
    assert merged[2]["in_gap"] is True
    assert all(not m["in_gap"] for m in merged if m["text"] != "mic during gap")


def test_merge_tracks_breaks_ties_by_track_name():
    # Two tracks whose segments land at the exact same session-relative
    # instant must still produce a deterministic (not input-order-dependent)
    # ordering: alphabetically by track name.
    clock = FrameClock(samplerate=1, frames=np.asarray([0.0, 1000.0]), times=np.asarray([0.0, 1000.0]))
    segments = {
        "b": [TSegment(start=10.0, end=11.0, text="B", track="b")],
        "a": [TSegment(start=10.0, end=12.0, text="A", track="a")],
    }
    merged = merge_tracks(segments, {"a": clock, "b": clock})
    assert [m["track"] for m in merged] == ["a", "b"]


def test_merge_tracks_uses_drift_corrected_clock_not_naive_rate():
    # Reuse the drifting-device log from the FrameClock drift tests: the
    # merged, session-relative time for a segment near the end of a 5-minute
    # drifting recording must reflect the *real* schedule, not what you'd get
    # assuming the nominal sample rate throughout.
    entries = _drift_entries(actual_rate=48050.0, duration_s=300.0, interval_s=30.0, start_t=0.0)
    drifting_clock = build_clock(entries)
    steady_clock = FrameClock(samplerate=48000, frames=np.asarray([0.0, 48000.0 * 300]), times=np.asarray([0.0, 300.0]))

    # A segment starting at the very end of the drifting track, expressed in
    # that track's own WAV-relative seconds (frames / nominal samplerate --
    # this is where a transcriber's segment timestamps come from).
    end_wav_seconds = (48050.0 * 300.0) / 48000.0
    mic_segments = [TSegment(start=end_wav_seconds, end=end_wav_seconds + 1.0, text="near the end", track="mic")]

    merged = merge_tracks({"mic": mic_segments}, {"mic": drifting_clock, "system": steady_clock})

    naive_rebased = end_wav_seconds  # what you'd get with zero drift correction
    assert abs(merged[0]["start"] - naive_rebased) > 0.2


def test_merge_tracks_empty_clocks_returns_empty():
    assert merge_tracks({}, {}) == []


def test_merge_tracks_falls_back_to_wav_relative_time_for_a_track_with_no_clock():
    """A track with no timing log used to be dropped outright -- silent data
    loss for a track we actually transcribed successfully. It must still be
    placed (WAV-relative, flagged approximate), alongside the clocked track
    which keeps its previous, precisely-rebased behaviour. See
    meeting_notes.transcribe.merge.merge_tracks and tests/test_regressions.py
    for the full story (Fix A)."""
    clock = FrameClock(samplerate=1, frames=np.asarray([0.0, 10.0]), times=np.asarray([0.0, 10.0]))
    segments = {
        "mic": [TSegment(start=1.0, end=2.0, text="ok", track="mic")],
        "ghost": [TSegment(start=1.0, end=2.0, text="no clock for this one", track="ghost")],
    }
    merged = merge_tracks(segments, {"mic": clock})
    assert len(merged) == 2
    by_track = {seg["track"]: seg for seg in merged}
    assert by_track["mic"]["approximate"] is False
    assert by_track["ghost"]["approximate"] is True
    assert by_track["ghost"]["start"] == pytest.approx(1.0)
    assert by_track["ghost"]["end"] == pytest.approx(2.0)
