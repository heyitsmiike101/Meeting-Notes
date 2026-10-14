"""Continuous, duration-weighted transcription progress (server jobs)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from meeting_notes import wire
from meeting_notes.server import jobs as jobs_mod
from meeting_notes.server.store import Store
from meeting_notes.transcribe.protocol import Segment
from tests.test_server import _upload_and_finalize, make_app, silence_pcm, wait_for_job_state

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated:DeprecationWarning"
)


class ProgressTranscriber:
    """Reports ``steps`` evenly spaced fractions per track."""

    def __init__(self, steps: int = 10):
        self.steps = steps

    def transcribe(self, wav_path, track, progress=None):
        for k in range(1, self.steps + 1):
            if progress:
                progress(k / self.steps)
        return [Segment(start=0.0, end=1.0, text=f"hello {track}", track=track)]


class OldStyleTranscriber:
    def __init__(self):
        self.calls = []

    def transcribe(self, wav_path, track):
        self.calls.append(track)
        return [Segment(start=0.0, end=1.0, text=f"hi {track}", track=track)]


@pytest.fixture
def recorded(monkeypatch):
    """Every progress value written to the store while a job is RUNNING."""
    values: list = []
    real = Store.update_job

    def spy(self, job_id, **fields):
        if "progress" in fields and fields.get("state") is None:
            values.append(fields["progress"])
        return real(self, job_id, **fields)

    monkeypatch.setattr(Store, "update_job", spy)
    return values


def _run(tmp_path, monkeypatch, transcriber, tracks):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    client = TestClient(make_app(tmp_path, transcriber_factory=lambda **_kw: transcriber))
    job_id = _upload_and_finalize(client, "p1", tracks)
    return client, wait_for_job_state(client, job_id, wire.JobState.DONE)


def test_progress_is_fine_grained_monotonic_and_duration_weighted(tmp_path, monkeypatch, recorded):
    monkeypatch.setattr(jobs_mod, "PROGRESS_MIN_STEP", 0.0)  # no throttling here
    tracks = {"mic": silence_pcm(10.0), "system": silence_pcm(30.0)}
    client, job = _run(tmp_path, monkeypatch, ProgressTranscriber(10), tracks)
    assert job["state"] == wire.JobState.DONE
    assert job["progress"] == 1.0
    # tracks are processed sorted: mic (25% of the audio) then system (75%)
    running = [v for v in recorded if v < 1.0]
    assert running == sorted(running), "progress went backwards"
    assert len(set(running)) >= 15
    assert max(running) <= jobs_mod.PROGRESS_RUNNING_CAP
    assert any(abs(v - 0.025) < 1e-6 for v in running)  # mic at 10%  -> 0.25 * 0.1
    assert any(abs(v - 0.25) < 1e-6 for v in running)  # mic done
    assert any(abs(v - 0.625) < 1e-6 for v in running)  # system at 50% -> 0.25 + 0.75 * 0.5
    assert running[-1] == jobs_mod.PROGRESS_RUNNING_CAP  # system done, capped below 1.0


def test_old_style_transcriber_without_progress_still_works(tmp_path, monkeypatch, recorded):
    stub = OldStyleTranscriber()
    client, job = _run(tmp_path, monkeypatch, stub, {"mic": silence_pcm(2.0), "system": silence_pcm(2.0)})
    assert job["state"] == wire.JobState.DONE
    assert sorted(stub.calls) == ["mic", "system"]
    running = [v for v in recorded if v < 1.0]
    assert running == [0.5, 0.99]  # per-track steps (second capped below 1.0)


def test_throttle_limits_store_writes(tmp_path, monkeypatch, recorded):
    now = [0.0]
    monkeypatch.setattr(jobs_mod, "_clock", lambda: now[0])
    tracks = {"mic": silence_pcm(10.0), "system": silence_pcm(10.0)}

    class Chatty:
        def transcribe(self, wav_path, track, progress=None):
            for k in range(1, 1001):  # 0.1% steps
                progress(k / 1000)
            return []

    _run(tmp_path, monkeypatch, Chatty(), tracks)
    running = [v for v in recorded if v < 1.0]
    # 0.5-point steps over the whole job -> ~200 writes at most, far below 2000 reports
    assert 50 < len(running) <= 205
    steps = [b - a for a, b in zip(running, running[1:])]
    assert all(s > 0 for s in steps)


def test_time_based_write_when_progress_moves_slowly(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(jobs_mod, "_clock", lambda: now[0])
    writes: list = []

    class FakeStore:
        def update_job(self, job_id, **fields):
            writes.append(fields["progress"])

    p = jobs_mod._JobProgress(FakeStore(), "j", [100.0])
    cb = p.step_callback(0)
    cb(0.001)  # 0.1 point, no time passed: skipped
    assert writes == []
    now[0] = 2.5
    cb(0.002)  # still tiny, but 2 s passed
    assert writes == [0.002]
    cb(0.001)  # going backwards: ignored
    cb(1.0)
    assert writes[-1] == jobs_mod.PROGRESS_RUNNING_CAP
    assert writes == sorted(writes)


def test_progress_never_reaches_one_before_done(monkeypatch):
    writes: list = []

    class FakeStore:
        def update_job(self, job_id, **fields):
            writes.append(fields["progress"])

    p = jobs_mod._JobProgress(FakeStore(), "j", [5.0, 5.0])
    p.step_callback(0)(1.0)
    p.finish_step(0)
    p.step_callback(1)(1.0)
    p.finish_step(1)
    assert writes and max(writes) < 1.0


def test_zero_length_tracks_fall_back_to_equal_shares():
    p = jobs_mod._JobProgress(object(), "j", [0.0, 0.0])
    assert p.weights == [0.5, 0.5]


def test_diarization_adds_a_trailing_share():
    p = jobs_mod._JobProgress(object(), "j", [10.0, 10.0], [0.0, 10.0])
    assert p.weights == pytest.approx([1 / 3, 2 / 3])
    assert p.transcribe_share == pytest.approx([1 / 3, 1 / 3])
