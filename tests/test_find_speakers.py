"""Speakers-only jobs: the "queue" choice for speaker labels without a helper, "Find speakers again" (locally and on a
GPU helper, diarize-only leases), "Update voice profiles" (re-profile) and the Meetings page buttons."""

from __future__ import annotations

import dataclasses

import pytest
from fastapi.testclient import TestClient

from meeting_notes import wire
from meeting_notes.server import compute as compute_mod
from meeting_notes.server import people, retention
from meeting_notes.server import settings as settings_mod
from meeting_notes.server import store as store_mod
from meeting_notes.server import web
from meeting_notes.server.app import create_app
from meeting_notes.transcribe.diarize import SpeakerTurn
from meeting_notes.transcribe.protocol import Segment
from tests.compute_helpers import Rig
from tests.test_compute_jobs import _fields
from tests.test_people import appearance, basis, make_session, mix, record, store  # noqa: F401 (fixture)

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated:DeprecationWarning"
)

TURNS = [{"start": 0.0, "end": 1.0, "speaker": "A"}, {"start": 1.0, "end": 2.0, "speaker": "B"}]


class TwoSegments:
    """Transcribes every track as "one" (0-1 s) and "two" (1-2 s)."""

    def __init__(self):
        self.calls = []

    def transcribe(self, wav_path, track):
        self.calls.append(track)
        return [Segment(0.0, 1.0, "one", track), Segment(1.0, 2.0, "two", track)]


class Diarizer:
    def __init__(self):
        self.calls, self.threads = 0, []

    def diarize(self, wav_path, progress=None):
        import threading

        self.calls += 1
        self.threads.append(threading.current_thread().name)
        return [SpeakerTurn(0.0, 1.0, "A"), SpeakerTurn(1.0, 2.0, "B")]


def make_rig(tmp_path, monkeypatch, *, diarizer=None, **settings):
    settings.setdefault("diarization_enabled", True)
    return Rig(tmp_path, monkeypatch, transcriber=TwoSegments(),
               diarizer_factory=(lambda: diarizer) if diarizer is not None else None, **settings)


def speakers_jobs(rig, session_id, *, wait=True):
    """The meeting's speakers-only jobs. The job is queued just after its transcript is marked done, so wait a moment."""
    import time

    deadline = time.monotonic() + (5 if wait else 0)
    while True:
        found = [j for j in rig.store.jobs_for_session(session_id) if store_mod.is_speakers_job(j)]
        if found or time.monotonic() >= deadline:
            return found
        time.sleep(0.02)


def labels(rig, job_id):
    return [(s["track"], s["label"], s["text"]) for s in rig.transcript(job_id)["segments"]]


def complete_diarize_only(rig, lease, **extra):
    return rig.client.post(f"/v1/compute/jobs/{lease['job_id']}/complete",
                           json=rig.body(lease, device="cuda", speaker_turns=TURNS, **extra),
                           headers=rig.helper_headers)


# -- the "queue" choice --------------------------------------------------------------------------------------------


def test_queue_transcribes_without_labels_then_a_helper_adds_them(tmp_path, monkeypatch):
    diar = Diarizer()
    rig = make_rig(tmp_path, monkeypatch, offload=False, diarization_without_helper="queue",
                   audio_retention_days=0)
    rig.queue.diarizer_factory = lambda: diar
    job_id = rig.wait_state(rig.new_job("q1"))["job_id"]
    assert sorted(row[1] for row in labels(rig, job_id)) == ["Them", "Them", "You", "You"]  # no labels, notes as usual
    assert diar.calls == 0
    (speakers,) = speakers_jobs(rig, "q1")
    job = rig.wait_compute(speakers["job_id"], "offered")
    assert job["compute"]["wait_forever"] and job["compute"]["diarize_only"]
    # the speakers job holds the audio back although retention says "delete it now"
    assert rig.store.session_index_row("q1")["has_audio"]
    assert retention.apply_retention(rig.store, settings_mod.load_settings(rig.store.root)) == []
    pipeline = rig.store.session_detail("q1")["pipeline"]
    assert pipeline["transcription"]["state"] == "complete" and pipeline["speaker_labels"]["state"] == "waiting"
    assert rig.store.session_index_row("q1")["latest_state"] == "done"
    # GPU offload is off: a helper polling gets nothing and the job keeps waiting
    assert rig.poll().status_code == 204
    rig.clock.advance(3 * compute_mod.OFFER_MAX_WAIT_SECONDS)
    rig.registry.sweep()
    assert rig.job(speakers["job_id"])["compute"]["status"] == "offered"
    # switched on: the helper gets a diarize-only lease for the system track and returns turns only
    rig.configure(compute_offload_enabled=True)
    lease = rig.claim()
    assert lease["transcribe"] is False and [t["name"] for t in lease["tracks"]] == ["system"]
    assert lease["diarization"]["enabled"] is True
    assert complete_diarize_only(rig, lease).status_code == 200
    done = rig.wait_state(speakers["job_id"])
    assert done["state"] == wire.JobState.DONE
    assert labels(rig, speakers["job_id"]) == [
        ("mic", "You", "one"), ("system", "Them 1", "one"), ("mic", "You", "two"), ("system", "Them 2", "two")] or \
        sorted(labels(rig, speakers["job_id"])) == sorted([
            ("mic", "You", "one"), ("mic", "You", "two"), ("system", "Them 1", "one"), ("system", "Them 2", "two")])
    assert rig.transcriber.calls == ["mic", "system"]  # never transcribed again
    assert not rig.store.session_index_row("q1")["has_audio"]  # retention ran once the job ended


def test_queue_with_no_helper_online_still_transcribes_here_and_queues_the_labels(tmp_path, monkeypatch):
    rig = make_rig(tmp_path, monkeypatch, offload=True, diarization_without_helper="queue")
    job_id = rig.new_job("q2")
    assert rig.wait_state(job_id, timeout=10)["state"] == wire.JobState.DONE  # nobody online: transcribed here
    assert rig.transcriber.calls
    rig.wait_compute(speakers_jobs(rig, "q2")[0]["job_id"], "offered")


def test_an_old_helper_that_returns_the_full_transcript_costs_nothing(tmp_path, monkeypatch):
    rig = make_rig(tmp_path, monkeypatch, offload=True, diarization_without_helper="queue")
    rig.wait_state(rig.new_job("q3"))
    (speakers,) = speakers_jobs(rig, "q3")
    rig.wait_compute(speakers["job_id"], "offered")
    lease = rig.claim()
    result = rig.good_result(lease)  # an older helper ignores "transcribe": false and sends its own text
    result["speaker_turns"] = TURNS
    resp = rig.client.post(f"/v1/compute/jobs/{lease['job_id']}/complete", json=result, headers=rig.helper_headers)
    assert resp.status_code == 200, resp.text
    rig.wait_state(speakers["job_id"])
    rows = labels(rig, speakers["job_id"])
    assert {text for _t, _l, text in rows} == {"one", "two"}  # the server kept its own text
    assert {label for t, label, _x in rows if t == "system"} == {"Them 1", "Them 2"}


def test_queued_jobs_wait_for_one_busy_helper_instead_of_falling_back(tmp_path, monkeypatch):
    """Seventeen-meeting incident: one helper takes one job at a time, the rest must wait for it, not use the CPU."""
    rig = make_rig(tmp_path, monkeypatch, offload=True, diarization_without_helper="queue",
                   compute_claim_timeout_seconds=30)
    assert rig.poll(state="busy").status_code == 204  # online, busy
    jobs = [rig.new_job(f"b{i}") for i in range(3)]
    for job_id in jobs:
        rig.wait_compute(job_id, "offered")
    for _ in range(compute_mod.OFFER_MAX_WAIT_SECONDS // 30 + 5):  # far beyond the usual cap
        rig.clock.advance(30)
        assert rig.poll(state="busy").status_code == 204
        rig.registry.sweep()
    for job_id in jobs:
        assert rig.job(job_id)["compute"]["status"] == "offered"
    for job_id in jobs:
        lease = rig.claim()
        assert lease["job_id"] == job_id
        done = rig.client.post(f"/v1/compute/jobs/{job_id}/complete", json=rig.good_result(lease),
                               headers=rig.helper_headers)
        assert done.status_code == 200, done.text
        assert rig.wait_state(job_id)["compute"]["status"] == "done"
    assert rig.transcriber.calls == []  # the server's CPU never transcribed anything


def test_run_here_and_skip_keep_their_old_claim_timeout_fallback(tmp_path, monkeypatch):
    rig = make_rig(tmp_path, monkeypatch, offload=True, diarization_without_helper="run_here")
    assert rig.poll(state="busy").status_code == 204
    job_id = rig.new_job("r1")
    rig.wait_compute(job_id, "offered")
    assert "wait_for_helper" not in rig.job(job_id)["compute"]
    rig.clock.advance(compute_mod.OFFER_MAX_WAIT_SECONDS + 30)
    rig.poll(state="busy")
    rig.registry.sweep()
    assert rig.wait_state(job_id)["compute"]["status"] == "local"


# -- Find speakers again -------------------------------------------------------------------------------------------


def test_find_speakers_runs_on_the_speakers_worker_and_keeps_the_text(tmp_path, monkeypatch):
    diar = Diarizer()
    # "skip" leaves the first transcript unlabelled; a manual request overrides it
    rig = make_rig(tmp_path, monkeypatch, offload=False, diarization_without_helper="skip")
    rig.queue.diarizer_factory = lambda: diar
    first = rig.wait_state(rig.new_job("f1"))["job_id"]
    assert diar.calls == 0 and not speakers_jobs(rig, "f1", wait=False)
    resp = rig.client.post("/v1/sessions/f1/find-speakers")
    assert resp.status_code == 200, resp.text
    job = rig.wait_state(resp.json()["job_id"])
    assert job["state"] == wire.JobState.DONE and job["kind"] == "speakers"
    assert diar.threads == ["meeting-notes-speakers"]
    assert [r[2] for r in labels(rig, job["job_id"])] == [r[2] for r in labels(rig, first)]
    assert {r[1] for r in labels(rig, job["job_id"]) if r[0] == "system"} == {"Them 1", "Them 2"}
    assert all(r["transcript_job_id"] == first for r in rig.store.list_reviews("f1"))  # no notes for a re-label
    assert rig.store.session_detail("f1")["transcript_job_id"] == job["job_id"]
    assert rig.transcriber.calls == ["mic", "system"]


def test_find_speakers_is_refused_with_a_clear_message(tmp_path, monkeypatch):
    rig = make_rig(tmp_path, monkeypatch, offload=False)
    rig.wait_state(rig.new_job("e1"))
    assert rig.client.post("/v1/sessions/nope/find-speakers").status_code == 404
    rig.configure(diarization_enabled=False)
    resp = rig.client.post("/v1/sessions/e1/find-speakers")
    assert resp.status_code == 400 and "turned off" in resp.json()["detail"]
    rig.configure(diarization_enabled=True)
    rig.store.delete_session_audio("e1")
    resp = rig.client.post("/v1/sessions/e1/find-speakers")
    assert resp.status_code == 400 and "no audio" in resp.json()["detail"]
    assert rig.client.post("/sessions/e1/find-speakers", follow_redirects=False).status_code == 400


def test_a_newer_retranscribe_supersedes_a_waiting_speakers_job(tmp_path, monkeypatch):
    rig = make_rig(tmp_path, monkeypatch, offload=False, diarization_without_helper="queue")
    rig.wait_state(rig.new_job("s1"))
    (waiting,) = speakers_jobs(rig, "s1")
    assert rig.client.post("/v1/sessions/s1/retranscribe").status_code == 200
    gone = rig.job(waiting["job_id"])
    assert gone["state"] == wire.JobState.ERROR and store_mod.is_superseded_job(gone)
    assert waiting["job_id"] not in rig.registry._offered
    # ... and the new transcription queues its own labels
    import time

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and len([j for j in speakers_jobs(rig, "s1") if not store_mod.is_superseded_job(j)]) < 1:
        time.sleep(0.02)
    assert len([j for j in speakers_jobs(rig, "s1") if not store_mod.is_superseded_job(j)]) == 1


def test_a_speakers_job_finishing_after_a_newer_transcript_is_superseded(tmp_path, monkeypatch):
    diar = Diarizer()
    rig = make_rig(tmp_path, monkeypatch, offload=False, diarization_without_helper="skip")
    rig.queue.diarizer_factory = lambda: diar
    rig.wait_state(rig.new_job("s2"))
    speakers_id = rig.store.create_job("s2", {}, kind=store_mod.JOB_KIND_SPEAKERS)
    rig.store.update_job(speakers_id, state=wire.JobState.RUNNING)
    newer = rig.queue.enqueue("s2")  # withdraws the pending one and transcribes again
    rig.wait_state(newer)
    assert store_mod.is_superseded_job(rig.job(speakers_id))


# -- settings ------------------------------------------------------------------------------------------------------


def test_queue_is_a_valid_choice_and_the_error_lists_it():
    assert settings_mod.DIARIZATION_WITHOUT_HELPER_CHOICES == ("run_here", "queue", "skip")
    assert settings_mod.validate(_fields(diarization_without_helper="queue")).diarization_without_helper == "queue"
    with pytest.raises(settings_mod.ValidationError, match="run_here, queue or skip"):
        settings_mod.validate(_fields(diarization_without_helper="maybe"))
    page = web.render_settings_page(settings_mod.Settings(diarization_without_helper="queue"), token_configured=True) \
        if False else None
    assert page is None


# -- Update voice profiles -----------------------------------------------------------------------------------------


def test_reprofile_keeps_confirmed_links_and_rematches_the_rest(store):
    record(store, "s1", {"A": ("Them 1", basis(0), 30)})
    record(store, "s2", {"X": ("Them 1", mix(0, 1, 0.6), 30)}, job="j2")     # between SUGGEST and AUTO: not linked
    record(store, "s3", {"Y": ("Them 1", mix(0, 1, 0.6), 30), "Z": ("Them 2", basis(5), 30)}, job="j3")
    person = appearance(store, "s1", "Them 1")["person_id"]
    assert appearance(store, "s2", "Them 1")["person_id"] != person
    people.appearance_action(store.root, "s3", "Them 1", "assign", person)    # decided by hand
    confirmed_z = appearance(store, "s3", "Them 2")
    clips_before = appearance(store, "s2", "Them 1")["clips"]
    result = people.reprofile(store, "s2")
    assert result["changed"] == 1
    again = appearance(store, "s2", "Them 1")
    assert again["person_id"] == person and again["link"] == "auto" and again["clips"] == clips_before
    # a meeting with a confirmed link keeps it
    people.reprofile(store, "s3")
    assert appearance(store, "s3", "Them 1")["person_id"] == person and appearance(store, "s3", "Them 1")["link"] == "confirmed"
    assert appearance(store, "s3", "Them 2")["person_id"] == confirmed_z["person_id"]
    ids = [p["id"] for p in people.list_people(store)]
    assert len(ids) == len(set(ids)) == 2  # the orphaned "Person" of s2 was cleaned up


def test_reprofile_without_a_voiceprint(store):
    make_session(store, "empty")
    with pytest.raises(people.NoVoiceprint):
        people.reprofile(store, "empty")
    with pytest.raises(people.PersonNotFound):
        people.reprofile(store, "missing")
    assert not people.has_voiceprint(store.root, "empty")


def test_reprofile_routes(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = create_app(transcriber_factory=lambda **kw: None, data_root=str(tmp_path / "data"))
    root = tmp_path / "data"
    with TestClient(app) as client:
        def switch(on):
            current = settings_mod.load_settings(root)
            settings_mod.save_settings(root, dataclasses.replace(current, voice_profiles_enabled=on, diarization_enabled=on))

        assert client.post("/v1/sessions/x/reprofile").status_code == 404  # feature off
        assert client.post("/sessions/x/reprofile", follow_redirects=False).status_code == 404
        switch(True)
        assert client.post("/v1/sessions/nope/reprofile").status_code == 404
        app.state.store.ensure_session_dir("m1")
        app.state.store.write_session_meta("m1", {"name": "m1", "created": "2026-10-01"})
        resp = client.post("/v1/sessions/m1/reprofile")
        assert resp.status_code == 409 and "no voice data" in resp.json()["detail"]
        from meeting_notes.transcribe.diarize import Diarization

        ts = people.TrackSpeakers(Diarization([SpeakerTurn(0, 30, "A")], {"A": basis(0)}), {"A": "Them 1"})
        people.record_meeting(app.state.store, "m1", "j1", {"system": ts})
        ok = client.post("/v1/sessions/m1/reprofile")
        assert ok.status_code == 200 and ok.json() == {"session_id": "m1", "speakers": 1, "changed": 0}
        assert client.get("/v1/sessions/m1").json()["has_voiceprint"] is True


# -- Meetings page -------------------------------------------------------------------------------------------------


def test_buttons_follow_the_two_feature_switches():
    bare = web.render_transcriptions_page(token_configured=True)
    assert 'id="find-speakers"' not in bare and 'id="reprofile"' not in bare
    assert 'id="bulk-find-speakers"' not in bare and 'id="bulk-reprofile"' not in bare
    speakers = web.render_transcriptions_page(token_configured=True, diarization_enabled=True)
    assert 'id="find-speakers"' in speakers and 'id="bulk-find-speakers"' in speakers
    assert 'id="reprofile"' not in speakers and 'id="bulk-reprofile"' not in speakers
    both = web.render_transcriptions_page(token_configured=True, diarization_enabled=True, voice_profiles_enabled=True)
    assert 'id="reprofile"' in both and 'id="bulk-reprofile"' in both


def test_meetings_page_reads_the_settings(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = create_app(transcriber_factory=lambda **kw: None, data_root=str(tmp_path / "data"))
    with TestClient(app) as client:
        root = tmp_path / "data"

        def page(**fields):
            current = settings_mod.load_settings(root)
            settings_mod.save_settings(root, dataclasses.replace(current, **fields))
            return client.get("/meetings").text

        assert 'id="find-speakers"' not in page(diarization_enabled=False, voice_profiles_enabled=True)
        text = page(diarization_enabled=True, voice_profiles_enabled=False)
        assert 'id="find-speakers"' in text and 'id="reprofile"' not in text
        assert 'id="reprofile"' in page(diarization_enabled=True, voice_profiles_enabled=True)
