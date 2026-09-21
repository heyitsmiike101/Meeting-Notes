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


def test_merge_with_only_empty_clocks_falls_back_to_wav_relative_time(tmp_path):
    """This used to return [] -- the exact silent-data-loss bug Fix A closes:
    a track with real transcript segments but no usable clock must still be
    placed on the timeline (WAV-relative, flagged approximate), never dropped."""
    empty = FrameClock(samplerate=RATE, frames=np.asarray([]), times=np.asarray([]))
    merged = merge_tracks({"mic": [Segment(1.0, 2.0, "x", "mic")]}, {"mic": empty}, {})
    assert len(merged) == 1
    assert merged[0]["start"] == pytest.approx(1.0)
    assert merged[0]["end"] == pytest.approx(2.0)
    assert merged[0]["approximate"] is True


def test_transcript_header_shows_the_session_date():
    """render_markdown read session_meta['date'], but sessions record
    'created', so the date line never appeared in any real transcript."""
    from meeting_notes.transcribe.merge import render_markdown

    out = render_markdown(
        [{"start": 0.0, "end": 1.0, "track": "mic", "label": "You", "text": "hi", "in_gap": False}],
        {"created": "2026-09-20T14:30:00", "duration_sec": 61},
    )
    assert "2026-09-20" in out


# -- client/server split regressions ------------------------------------------


def test_fractional_resampler_does_not_accumulate_drift():
    """44.1kHz -> 16kHz must not run long.

    The interpolation cursor could step PAST the end of its buffer, and slicing
    with an out-of-range index clamps to empty, so the overshoot was dropped and
    each block skipped nearly a sample too few. That ran ~0.13% fast: about 4.5
    seconds of drift per hour, which would slide the live preview out of step
    with the recording it is supposed to be previewing.
    """
    from meeting_notes.client.resample import Downsampler

    for seconds in (1, 10, 60):
        rate = 44100
        t = np.arange(rate * seconds) / rate
        tone = (0.5 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32)
        ds = Downsampler(rate, 16000)
        out = np.concatenate([ds.process(tone[i : i + 512]) for i in range(0, len(tone), 512)])
        assert abs(len(out) - 16000 * seconds) <= 2, (
            f"{seconds}s of 44.1kHz produced {len(out)} samples, expected ~{16000 * seconds}"
        )


def test_live_preview_buffer_is_bounded_on_a_silent_track():
    """A track with no speech must not grow the server's memory without limit.

    VAD finds no mature utterance in silence, so nothing was ever committed and
    nothing was ever trimmed -- the normal state of your microphone while the
    other side talks. Each VAD pass also rescans the whole buffer, so the cost
    was quadratic in meeting length.
    """
    from meeting_notes.server.live import MAX_BUFFER_SECONDS, _TrackBuffer

    rate = 16000
    buf = _TrackBuffer(rate)
    seconds = int(MAX_BUFFER_SECONDS * 3)
    for _ in range(seconds):
        buf.feed(b"\x00\x00" * rate)

    held_frames = len(buf._buffer) // 2
    assert held_frames <= MAX_BUFFER_SECONDS * rate
    # Dropping old audio must advance the commit pointer, or every later
    # Partial would be placed at the wrong point on the track timeline.
    assert buf._committed_frame == seconds * rate - held_frames


def test_ids_reject_overlong_and_windows_reserved_names():
    """These ids become filenames, and they arrive from the network."""
    from meeting_notes.server.store import is_safe_id

    assert is_safe_id("2026-09-20_14-30-00_standup")
    assert is_safe_id("a" * 128)
    # Past a single path component's limit on most filesystems, and these gain
    # suffixes like ".timing.jsonl" -- an OSError instead of a clean rejection.
    assert not is_safe_id("a" * 129)
    # "con.raw" is not a file on Windows.
    for reserved in ("con", "CON", "nul", "com1", "LPT3"):
        assert not is_safe_id(reserved), f"{reserved!r} should be rejected"


def test_upload_counts_frames_from_total_bytes_not_per_chunk(tmp_path, monkeypatch):
    """A request body split mid-sample must not lose frames.

    The handler divided each transport chunk by the sample size separately.
    Chunk boundaries have no reason to fall on a 2-byte sample boundary, so
    every chunk that split mid-sample dropped its odd trailing byte from the
    count -- undercounting a real multi-MB upload and rejecting it with a 400
    against the client's X-Frames header, even though the bytes on disk were
    perfectly correct.

    This drives the ASGI app directly rather than going through TestClient,
    because TestClient coalesces the body into one even-sized chunk and so
    never reproduces the split that causes the bug.
    """
    import asyncio
    import json as _json

    from meeting_notes import wire
    from meeting_notes.server.app import create_app

    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = create_app(data_root=str(tmp_path / "data"))

    frames = 50_000
    payload = np.arange(frames, dtype="<i2").tobytes()
    step = 1023  # odd, so most boundaries land mid-sample
    chunks = [payload[i : i + step] for i in range(0, len(payload), step)]
    assert any(len(c) % 2 for c in chunks), "test must actually split mid-sample"

    path = wire.track_upload_path("sess-chunked", "mic")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.1"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", b"application/octet-stream"),
            (b"x-frames", str(frames).encode()),
        ],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
    }

    pending = list(chunks)
    messages = []

    async def receive():
        if pending:
            return {"type": "http.request", "body": pending.pop(0), "more_body": True}
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    asyncio.run(app(scope, receive, send))

    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    assert status == 200, f"{status}: {body!r}"
    assert _json.loads(body)["frames"] == frames

    stored = (tmp_path / "data" / "sessions" / "sess-chunked" / "mic.raw").read_bytes()
    assert stored == payload


# -- Fix A: merge_tracks must never silently drop a track's transcript ------


def test_merge_places_both_tracks_wav_relative_when_no_clocks_at_all():
    """finalize with timing={} (no timing log for either track) used to make
    merge_tracks return [] even though both tracks transcribed real segments.
    Both must still show up, using their own WAV-relative seconds directly,
    and merged in sorted order across tracks."""
    track_segments = {
        "mic": [Segment(2.0, 3.0, "second", "mic")],
        "system": [Segment(0.0, 1.0, "first", "system")],
    }
    merged = merge_tracks(track_segments, {}, {"mic": "You", "system": "Them"})

    assert len(merged) == 2
    assert [seg["text"] for seg in merged] == ["first", "second"]  # sorted by start
    assert all(seg["approximate"] for seg in merged)
    first, second = merged
    assert first["track"] == "system" and first["start"] == pytest.approx(0.0)
    assert first["end"] == pytest.approx(1.0)
    assert second["track"] == "mic" and second["start"] == pytest.approx(2.0)
    assert second["end"] == pytest.approx(3.0)


def test_merge_flags_only_the_track_without_a_clock_as_approximate():
    """One track has a real timing log, the other has none at all (not even
    an entry in `clocks`). The clocked one must be rebased exactly as before
    (approximate=False); the other must fall back to WAV-relative time,
    flagged approximate=True -- neither track's text may be dropped."""
    base = 5_000.0
    real = build_clock(
        [
            {"event": "open", "segment": 0, "frames": 0, "t": base, "samplerate": RATE},
            {"event": "progress", "segment": 0, "frames": RATE * 10, "t": base + 10.0},
        ]
    )
    track_segments = {
        "mic": [Segment(1.0, 2.0, "clocked", "mic")],
        "system": [Segment(4.0, 5.0, "unclocked", "system")],
    }
    merged = merge_tracks(track_segments, {"mic": real}, {"mic": "You", "system": "Them"})

    assert len(merged) == 2
    by_track = {seg["track"]: seg for seg in merged}

    clocked = by_track["mic"]
    assert clocked["approximate"] is False
    assert clocked["start"] == pytest.approx(1.0, abs=0.2)  # rebased onto its own clock's start

    unclocked = by_track["system"]
    assert unclocked["approximate"] is True
    # No clock to rebase against -> its own WAV-relative seconds, unchanged.
    assert unclocked["start"] == pytest.approx(4.0)
    assert unclocked["end"] == pytest.approx(5.0)


def test_merge_with_clocks_on_both_tracks_is_unaffected_by_the_approximate_flag():
    """Existing (pre-Fix-A) behaviour for the normal case -- both tracks have
    real timing logs -- must be unchanged: both rebased onto the shared
    timeline, neither flagged approximate."""
    base = 1_000.0
    mic_clock = build_clock(
        [
            {"event": "open", "segment": 0, "frames": 0, "t": base, "samplerate": RATE},
            {"event": "progress", "segment": 0, "frames": RATE * 5, "t": base + 5.0},
        ]
    )
    system_clock = build_clock(
        [
            {"event": "open", "segment": 0, "frames": 0, "t": base + 0.5, "samplerate": RATE},
            {"event": "progress", "segment": 0, "frames": RATE * 5, "t": base + 5.5},
        ]
    )
    track_segments = {
        "mic": [Segment(0.0, 1.0, "hello", "mic")],
        "system": [Segment(0.5, 1.5, "hi", "system")],
    }
    merged = merge_tracks(
        track_segments,
        {"mic": mic_clock, "system": system_clock},
        {"mic": "You", "system": "Them"},
    )

    assert len(merged) == 2
    assert all(seg["approximate"] is False for seg in merged)
    # mic opened first (base), so it rebases to 0.0; system opened 0.5s later.
    by_track = {seg["track"]: seg for seg in merged}
    assert by_track["mic"]["start"] == pytest.approx(0.0, abs=0.05)
    assert by_track["system"]["start"] == pytest.approx(1.0, abs=0.05)


def test_render_markdown_notes_which_track_has_approximate_timestamps():
    from meeting_notes.transcribe.merge import render_markdown

    merged = [
        {
            "start": 0.0,
            "end": 1.0,
            "track": "system",
            "label": "Them",
            "text": "hi",
            "in_gap": False,
            "approximate": True,
        },
        {
            "start": 2.0,
            "end": 3.0,
            "track": "mic",
            "label": "You",
            "text": "hello",
            "in_gap": False,
            "approximate": False,
        },
    ]
    out = render_markdown(merged, {"created": "2026-09-20"})
    assert "approximate" in out
    assert "system" in out.split("approximate")[0].splitlines()[-1] or "system track" in out
    # The clocked track must not also be called out as approximate.
    assert "mic track are approximate" not in out


def test_live_preview_commits_continuous_speech_instead_of_waiting_for_a_pause():
    """Seen on a real run: with an audiobook playing under the meeting, the
    system track was continuous speech, VAD never reported an utterance
    *ending*, nothing ever matured, and the live preview stayed empty for
    the whole recording. Speech longer than LIVE_MAX_UTTERANCE must be split
    so the first piece becomes a mature chunk once enough audio follows it."""
    import wave
    from pathlib import Path

    pytest.importorskip("faster_whisper")
    from meeting_notes.server.live import LIVE_MAX_UTTERANCE, _TrackBuffer

    fixture = Path(__file__).parent / "fixtures" / "jfk.wav"
    with wave.open(str(fixture)) as fh:
        assert fh.getframerate() == 16000
        clip = fh.readframes(fh.getnframes())
    # 66 s of back-to-back speech with no pause anywhere.
    buf = _TrackBuffer(16000)
    buf.feed(clip * 6)

    chunks = buf.take_mature_chunks(maturity=1.0)

    assert chunks, "continuous speech never produced a mature chunk"
    for start, end, _pcm in chunks:
        assert (end - start) / 16000 <= LIVE_MAX_UTTERANCE + 1.0
