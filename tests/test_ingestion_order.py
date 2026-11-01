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


# -- a re-run replaces finished notes ---------------------------------------------------------------------------------

from tests.notion_helpers import complete_notes, notes  # noqa: E402


def finish_notes(rig, session_id):
    """Wait for the meeting's notes to be queued, then complete them (as the bridge would). Returns the review."""
    (review,) = wait_reviews(rig, session_id)
    complete_notes(rig.store, review["review_id"], notes(title="First notes"))
    return rig.store.read_review(review["review_id"])


def wait_status(rig, review_id, status, timeout=5.0):
    deadline = time.monotonic() + timeout
    review = rig.store.read_review(review_id)
    while time.monotonic() < deadline and review["status"] != status:
        time.sleep(0.02)
        review = rig.store.read_review(review_id)
    return review


def skip_rig(tmp_path, monkeypatch, session_id, **settings):
    rig = make_rig(tmp_path, monkeypatch, offload=False, diarization_without_helper="skip", **settings)
    rig.wait_state(rig.new_job(session_id))
    return rig, finish_notes(rig, session_id)


def test_retranscribe_rebuilds_finished_notes_in_the_same_note_type(tmp_path, monkeypatch):
    rig, first = skip_rig(tmp_path, monkeypatch, "r1")
    rig.wait_state(rig.queue.enqueue("r1"))
    review = wait_status(rig, first["review_id"], "queued")
    assert review["status"] == "queued" and review["payload"] is None  # the old notes are replaced, not kept beside
    assert review["transcript_job_id"] == rig.store.latest_done_job("r1")["job_id"] != first["transcript_job_id"]
    assert review["template_id"] == first["template_id"]
    assert len(reviews(rig, "r1")) == 1


def test_a_failed_retranscribe_leaves_the_notes_alone(tmp_path, monkeypatch):
    rig, first = skip_rig(tmp_path, monkeypatch, "r2")

    def boom(wav_path, track):
        raise RuntimeError("model crashed")

    monkeypatch.setattr(rig.transcriber, "transcribe", boom)
    assert rig.wait_state(rig.queue.enqueue("r2"))["state"] == wire.JobState.ERROR
    time.sleep(0.2)
    review = rig.store.read_review(first["review_id"])
    assert review["status"] == "done" and review["payload"] and review["transcript_job_id"] == first["transcript_job_id"]


def test_a_retranscribe_makes_no_notes_for_a_meeting_without_any(tmp_path, monkeypatch):
    rig = make_rig(tmp_path, monkeypatch, offload=False, diarization_without_helper="skip", ai_provider="disabled")
    rig.wait_state(rig.new_job("r3"))
    rig.configure(ai_provider="codex")
    rig.wait_state(rig.queue.enqueue("r3"))
    time.sleep(0.2)
    assert reviews(rig, "r3") == []


def test_the_automatic_notes_switch_does_not_stop_a_rebuild(tmp_path, monkeypatch):
    rig, first = skip_rig(tmp_path, monkeypatch, "r4")
    rig.configure(auto_notes_types=[])  # no type generates notes by itself, but these exist and a re-run replaces them
    rig.wait_state(rig.queue.enqueue("r4"))
    assert wait_status(rig, first["review_id"], "queued")["status"] == "queued"


def test_a_rebuild_is_skipped_while_the_ai_provider_is_off(tmp_path, monkeypatch):
    rig, first = skip_rig(tmp_path, monkeypatch, "r5")
    rig.configure(ai_provider="disabled")
    rig.wait_state(rig.queue.enqueue("r5"))
    time.sleep(0.2)
    assert rig.store.read_review(first["review_id"])["status"] == "done"


def test_a_retranscribe_with_a_speakers_step_rebuilds_once_after_it(tmp_path, monkeypatch):
    rig, speakers_id = queued_rig(tmp_path, monkeypatch, "r6")
    rig.queue._fail_speakers(speakers_id, "boom")  # releases the first notes
    first = finish_notes(rig, "r6")
    rig.wait_state(rig.queue.enqueue("r6"))
    (again,) = [j for j in speakers_jobs(rig, "r6") if j["job_id"] != speakers_id]
    assert again["notes_after_speakers"] is True and again["notes_rebuild"] is True  # persisted: survives a restart
    time.sleep(0.2)
    assert rig.store.read_review(first["review_id"])["status"] == "done"  # not before the labels are in
    rig.queue._fail_speakers(again["job_id"], "boom")  # the transcript did change: the notes follow it anyway
    review = rig.store.read_review(first["review_id"])
    assert review["status"] == "queued" and len(reviews(rig, "r6")) == 1
    assert rig.job(again["job_id"])["notes_after_done"] is True
    rig.queue._notes_after_speakers(again["job_id"])  # asking again changes nothing (and never doubles the rebuild)
    assert len(reviews(rig, "r6")) == 1


def test_manual_find_speakers_rebuilds_the_notes_when_it_finishes(tmp_path, monkeypatch):
    rig, speakers_id = queued_rig(tmp_path, monkeypatch, "r7")
    rig.queue._fail_speakers(speakers_id, "boom")
    first = finish_notes(rig, "r7")
    again = rig.queue.enqueue_speakers("r7", queued=True)
    assert rig.job(again)["notes_rebuild"] is True
    rig.wait_compute(again, "offered")
    rig.configure(compute_offload_enabled=True)
    assert complete_diarize_only(rig, rig.claim()).status_code == 200
    assert rig.wait_state(again)["state"] == wire.JobState.DONE
    review = wait_status(rig, first["review_id"], "queued")
    assert review["status"] == "queued" and review["transcript_job_id"] == rig.store.latest_done_job("r7")["job_id"]


def test_a_failed_manual_find_speakers_leaves_the_notes_alone(tmp_path, monkeypatch):
    rig, speakers_id = queued_rig(tmp_path, monkeypatch, "r8")
    rig.queue._fail_speakers(speakers_id, "boom")
    first = finish_notes(rig, "r8")
    again = rig.queue.enqueue_speakers("r8", queued=True)
    rig.queue._fail_speakers(again, "boom")
    assert rig.store.read_review(first["review_id"])["status"] == "done"


def test_the_notes_say_a_rebuild_is_coming_while_it_waits(tmp_path, monkeypatch):
    rig, speakers_id = queued_rig(tmp_path, monkeypatch, "r9")
    rig.queue._fail_speakers(speakers_id, "boom")
    first = finish_notes(rig, "r9")
    rig.wait_state(rig.queue.enqueue("r9"))
    note = rig.client.get(f"/v1/meeting-notes/{first['review_id']}").json()
    assert note["transcript_outdated"] is True and note["rebuild_pending"] is True
    (again,) = [j for j in speakers_jobs(rig, "r9") if j["job_id"] != speakers_id]
    rig.queue._fail_speakers(again["job_id"], "boom")
    note = rig.client.get(f"/v1/meeting-notes/{first['review_id']}").json()
    assert note["transcript_outdated"] is False and note["rebuild_pending"] is False
