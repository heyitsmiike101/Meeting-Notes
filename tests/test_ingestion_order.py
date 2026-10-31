"""Pipeline order: audio -> transcribe -> identify speakers -> label speakers -> generate notes.

Notes of a new meeting never start before the speaker steps have finished (or failed / been withdrawn)."""

from __future__ import annotations

import time

import pytest

from meeting_notes import wire
from meeting_notes.server import store as store_mod
from tests.test_find_speakers import Diarizer, complete_diarize_only, make_rig, speakers_jobs

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated:DeprecationWarning"
)


def reviews(rig, session_id):
    return rig.store.list_reviews(session_id)


def wait_reviews(rig, session_id, count=1, timeout=5.0):
    """The notes are queued just after the job is marked done, so give them a moment."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and len(reviews(rig, session_id)) < count:
        time.sleep(0.02)
    return reviews(rig, session_id)


def queued_rig(tmp_path, monkeypatch, session_id, **settings):
    rig = make_rig(tmp_path, monkeypatch, offload=False, diarization_without_helper="queue", **settings)
    rig.queue.diarizer_factory = lambda: Diarizer()
    rig.wait_state(rig.new_job(session_id))
    (speakers,) = speakers_jobs(rig, session_id)
    rig.wait_compute(speakers["job_id"], "offered")  # routed: nothing else will touch the job record now
    return rig, speakers["job_id"]


def test_notes_wait_for_the_queued_speakers_job(tmp_path, monkeypatch):
    rig, speakers_id = queued_rig(tmp_path, monkeypatch, "o1")
    assert rig.job(speakers_id)["notes_after_speakers"] is True  # recorded on the job (survives a restart)
    assert reviews(rig, "o1") == []  # transcript done, labels not: no notes yet
    rig.configure(compute_offload_enabled=True)
    lease = rig.claim()
    assert complete_diarize_only(rig, lease).status_code == 200
    assert rig.wait_state(speakers_id)["state"] == wire.JobState.DONE
    assert len(wait_reviews(rig, "o1")) == 1  # labels recorded, then the notes
    assert rig.job(speakers_id)["notes_after_done"] is True


def test_notes_still_come_when_the_speakers_job_fails(tmp_path, monkeypatch):
    rig, speakers_id = queued_rig(tmp_path, monkeypatch, "o2")
    assert reviews(rig, "o2") == []
    rig.queue._fail_speakers(speakers_id, "boom")
    assert rig.job(speakers_id)["state"] == wire.JobState.ERROR
    assert len(reviews(rig, "o2")) == 1


def test_notes_still_come_when_the_registry_gives_up_on_the_speakers_job(tmp_path, monkeypatch):
    rig, speakers_id = queued_rig(tmp_path, monkeypatch, "o3")
    assert reviews(rig, "o3") == []
    assert rig.registry._fail_job(speakers_id, {}, "too many attempts")
    assert len(reviews(rig, "o3")) == 1


def test_notes_still_come_when_the_speakers_job_is_withdrawn(tmp_path, monkeypatch):
    rig, speakers_id = queued_rig(tmp_path, monkeypatch, "o4")
    assert reviews(rig, "o4") == []
    rig.queue._withdraw_speakers_jobs("o4")
    assert store_mod.is_superseded_job(rig.job(speakers_id))
    assert len(reviews(rig, "o4")) == 1


def test_a_restart_between_the_job_ending_and_the_notes_still_queues_them(tmp_path, monkeypatch):
    rig, speakers_id = queued_rig(tmp_path, monkeypatch, "o5")
    # the job ended (with its transcript) but the server stopped before the notes were queued
    base = rig.store.latest_done_job("o5")
    stored = rig.store.read_transcript(base["job_id"])
    rig.store.write_transcript(speakers_id, stored["markdown"], stored["json"])
    rig.store.update_job(speakers_id, state=wire.JobState.DONE)
    assert reviews(rig, "o5") == []
    rig.queue.resume_interrupted()
    assert len(reviews(rig, "o5")) == 1


def test_a_restart_keeps_the_deferral_on_a_waiting_job(tmp_path, monkeypatch):
    rig, speakers_id = queued_rig(tmp_path, monkeypatch, "o6")
    rig.queue.resume_interrupted()
    job = rig.job(speakers_id)
    assert job["notes_after_speakers"] is True and job["state"] != wire.JobState.DONE
    assert reviews(rig, "o6") == []


def test_provider_disabled_never_makes_notes(tmp_path, monkeypatch):
    rig, speakers_id = queued_rig(tmp_path, monkeypatch, "o7", ai_provider="disabled")
    rig.queue._fail_speakers(speakers_id, "boom")
    assert reviews(rig, "o7") == []


def test_find_speakers_again_on_an_old_meeting_makes_no_notes(tmp_path, monkeypatch):
    rig, speakers_id = queued_rig(tmp_path, monkeypatch, "o8", ai_provider="disabled")
    rig.queue._fail_speakers(speakers_id, "boom")
    rig.configure(ai_provider="codex")
    again = rig.queue.enqueue_speakers("o8", queued=True)
    assert "notes_after_speakers" not in rig.job(again)
    rig.queue._fail_speakers(again, "boom")
    assert reviews(rig, "o8") == []


def test_retranscribe_does_not_make_notes_by_itself(tmp_path, monkeypatch):
    rig = make_rig(tmp_path, monkeypatch, offload=False, diarization_without_helper="run_here", ai_provider="disabled")
    rig.wait_state(rig.new_job("o9"))
    rig.configure(ai_provider="codex")
    rig.wait_state(rig.queue.enqueue("o9"))
    assert reviews(rig, "o9") == []


def test_without_a_speakers_step_notes_start_right_after_the_transcript(tmp_path, monkeypatch):
    rig = make_rig(tmp_path, monkeypatch, offload=False, diarization_without_helper="skip")
    rig.wait_state(rig.new_job("o10"))
    assert len(wait_reviews(rig, "o10")) == 1
