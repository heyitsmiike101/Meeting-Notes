"""In-person (phone) meetings: "Speaker N" labels, helper wait policy, the separate speakers worker, prompt line."""

from __future__ import annotations

import threading
import time

import pytest

from meeting_notes import wire
from meeting_notes.server import names as names_mod
from meeting_notes.server import settings as settings_mod
from meeting_notes.transcribe.diarize import SpeakerTurn
from tests.compute_helpers import Rig
from tests.test_server import silence_pcm


def upload_and_finalize(client, session_id, *, in_person=True, seconds=10.0, tracks=("system",)):
    """Upload and finalize a session whose meta says it was (or was not) recorded in person on a phone."""
    for track in tracks:
        pcm = silence_pcm(seconds)
        resp = client.post(
            wire.track_upload_path(session_id, track), content=pcm,
            headers={"Content-Type": "application/octet-stream", "X-Frames": str(len(pcm) // 2)})
        assert resp.status_code == 200, resp.text
    meta = {"created": "2026-10-20", "tracks": {t: {} for t in tracks}}
    if in_person:
        meta.update(in_person=True, source="mobile")
    resp = client.post(wire.finalize_path(session_id), json={"meta": meta, "timing": {}, "settings": {}})
    assert resp.status_code == 200, resp.text
    return resp.json()["job_id"]


class Diarizer:
    def __init__(self, fail=False, gate=None):
        self.calls, self.fail, self.gate = 0, fail, gate
        self.thread_names = []

    def diarize(self, wav_path, progress=None):
        self.calls += 1
        self.thread_names.append(threading.current_thread().name)
        if progress:
            progress(0.5)
        if self.gate is not None:
            assert self.gate.wait(10)
        if self.fail:
            raise RuntimeError("boom")
        return [SpeakerTurn(0.0, 1.0, "A")]


def labels(rig, job_id):
    return [s["label"] for s in rig.transcript(job_id)["segments"]]


# -- labels --------------------------------------------------------------------


def test_local_in_person_labels_are_speaker_n(tmp_path, monkeypatch):
    diar = Diarizer()
    rig = Rig(tmp_path, monkeypatch, offload=False, diarizer_factory=lambda: diar)
    job = rig.wait_state(upload_and_finalize(rig.client, "ip1"))
    assert job["state"] == wire.JobState.DONE
    assert labels(rig, job["job_id"]) == ["Speaker 1"]
    assert diar.thread_names == ["meeting-notes-speakers"]  # not on the transcription thread


def test_in_person_without_turns_uses_speaker_base_label(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, offload=False)  # no diarizer at all
    job = rig.wait_state(upload_and_finalize(rig.client, "ip2"))
    assert labels(rig, job["job_id"]) == ["Speaker"]


def test_non_in_person_labels_unchanged(tmp_path, monkeypatch):
    diar = Diarizer()
    rig = Rig(tmp_path, monkeypatch, offload=False, diarizer_factory=lambda: diar)
    job = rig.wait_state(upload_and_finalize(rig.client, "n1", in_person=False, tracks=("mic", "system")))
    assert sorted(labels(rig, job["job_id"])) == ["Them 1", "You"]
    assert diar.thread_names and "speakers" not in diar.thread_names[0]
    assert "Speaker" not in " ".join(labels(rig, job["job_id"]))


def test_helper_path_labels_are_speaker_n(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, diarization_enabled=True)
    assert rig.poll().status_code == 204
    job_id = upload_and_finalize(rig.client, "ip3")
    rig.wait_compute(job_id, "offered")
    lease = rig.claim()
    result = rig.good_result(lease)
    result["speaker_turns"] = [{"start": 0.0, "end": 1.0, "speaker": "X"}, {"start": 1.0, "end": 2.0, "speaker": "Y"}]
    result["tracks"]["system"] = [
        {"start": 0.0, "end": 1.0, "text": "one"}, {"start": 1.0, "end": 2.0, "text": "two"}]
    resp = rig.client.post(f"/v1/compute/jobs/{job_id}/complete", json=result, headers=rig.helper_headers)
    assert resp.status_code == 200, resp.text
    rig.wait_state(job_id)
    assert labels(rig, job_id) == ["Speaker 1", "Speaker 2"]


def test_helper_gets_no_diarization_when_in_person_off(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, diarization_enabled=True, in_person_diarization="off")
    rig.poll()
    job_id = upload_and_finalize(rig.client, "ip4")
    job = rig.wait_compute(job_id, "offered")
    assert job["compute"]["settings"]["diarization"]["enabled"] is False
    other = rig.new_job("plain", tracks=("mic", "system"))
    assert rig.wait_compute(other, "offered")["compute"]["settings"]["diarization"]["enabled"] is True


# -- the wait policy -----------------------------------------------------------


def test_in_person_waits_for_a_helper_that_is_not_online_then_runs_here(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, compute_claim_timeout_seconds=30, in_person_helper_wait_minutes=5)
    job_id = upload_and_finalize(rig.client, "w1")  # no helper has polled: none online
    job = rig.wait_compute(job_id, "offered")
    assert job["compute"]["claim_timeout"] == 300.0 and job["compute"]["max_wait"] == 300.0
    rig.clock.advance(200)  # far past the 30 s claim timeout, inside five minutes
    rig.registry.sweep()
    assert rig.job(job_id)["compute"]["status"] == "offered"
    rig.clock.advance(101)
    rig.registry.sweep()
    job = rig.wait_state(job_id)
    assert job["state"] == wire.JobState.DONE
    assert job["compute"]["status"] == "local" and rig.transcriber.calls == ["system"]


def test_in_person_wait_is_capped_even_while_a_busy_helper_is_online(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, in_person_helper_wait_minutes=2)
    rig.poll()
    job_id = upload_and_finalize(rig.client, "w2")
    rig.wait_compute(job_id, "offered")
    for _ in range(3):  # the helper keeps polling busy but never claims
        rig.clock.advance(40)
        rig.poll(state="busy")
        rig.registry.sweep()
    assert rig.job(job_id)["compute"]["status"] == "offered"
    rig.clock.advance(10)  # 130 s > 2 minutes
    rig.registry.sweep()
    assert rig.wait_state(job_id)["compute"]["status"] == "local"


def test_wait_of_zero_minutes_runs_here_at_once_and_other_meetings_keep_the_short_rule(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, in_person_helper_wait_minutes=0)
    job = rig.wait_state(upload_and_finalize(rig.client, "w3"))
    assert job["state"] == wire.JobState.DONE and job.get("compute") is None
    # an ordinary meeting with no helper online also runs here at once (unchanged behaviour)
    job = rig.wait_state(rig.new_job("w4"))
    assert job.get("compute") is None


def test_offload_off_in_person_runs_here(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, offload=False)
    assert rig.wait_state(upload_and_finalize(rig.client, "w5")).get("compute") is None


# -- the separate speakers worker ----------------------------------------------


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition never became true")


def test_job_stays_running_in_speakers_stage_and_notes_wait_for_labels(tmp_path, monkeypatch):
    gate = threading.Event()
    diar = Diarizer(gate=gate)
    rig = Rig(tmp_path, monkeypatch, offload=False, diarizer_factory=lambda: diar)
    job_id = upload_and_finalize(rig.client, "sp1")
    _wait(lambda: diar.calls == 1)
    job = rig.job(job_id)
    assert job["state"] == wire.JobState.RUNNING and job["stage"] == "speakers"
    assert job["speakers_progress"] is not None and job["transcribe_progress"] == 1.0
    info = rig.client.get(f"/v1/jobs/{job_id}").json()
    assert info["stage"] == "speakers" and info["transcribe_percent"] == 100.0
    assert rig.store.latest_review("sp1") is None  # notes do not start on an unlabelled transcript
    # the transcription thread is free: another meeting finishes while this one is still labelling
    other = rig.new_job("sp-other", tracks=("mic",))
    assert rig.wait_state(other)["state"] == wire.JobState.DONE
    gate.set()
    done = rig.wait_state(job_id)
    assert done["state"] == wire.JobState.DONE and labels(rig, job_id) == ["Speaker 1"]
    _wait(lambda: rig.store.latest_review("sp1") is not None)


def test_diarization_runs_for_in_person_even_when_without_helper_is_skip(tmp_path, monkeypatch):
    diar = Diarizer()
    rig = Rig(tmp_path, monkeypatch, offload=False, diarizer_factory=lambda: diar, diarization_without_helper="skip")
    job = rig.wait_state(upload_and_finalize(rig.client, "sp2"))
    assert diar.calls == 1 and labels(rig, job["job_id"]) == ["Speaker 1"]


def test_in_person_off_skips_diarization_with_a_warning_and_still_gets_notes(tmp_path, monkeypatch):
    diar = Diarizer()
    rig = Rig(tmp_path, monkeypatch, offload=False, diarizer_factory=lambda: diar, in_person_diarization="off")
    job = rig.wait_state(upload_and_finalize(rig.client, "sp3"))
    assert diar.calls == 0 and labels(rig, job["job_id"]) == ["Speaker"]
    assert "speaker labels are off for in-person meetings" in job["warnings"]
    _wait(lambda: rig.store.latest_review("sp3") is not None)


def test_failed_labelling_still_finishes_with_a_warning_and_queues_notes(tmp_path, monkeypatch):
    diar = Diarizer(fail=True)
    rig = Rig(tmp_path, monkeypatch, offload=False, diarizer_factory=lambda: diar)
    job = rig.wait_state(upload_and_finalize(rig.client, "sp4"))
    assert job["state"] == wire.JobState.DONE
    assert job["warnings"] == ["speaker labels failed: boom"]
    assert labels(rig, job["job_id"]) == ["Speaker"]
    _wait(lambda: rig.store.latest_review("sp4") is not None)


def test_labelling_is_one_at_a_time(tmp_path, monkeypatch):
    gate = threading.Event()
    diar = Diarizer(gate=gate)
    rig = Rig(tmp_path, monkeypatch, offload=False, diarizer_factory=lambda: diar)
    a = upload_and_finalize(rig.client, "q1")
    b = upload_and_finalize(rig.client, "q2")
    _wait(lambda: diar.calls == 1)
    time.sleep(0.3)
    assert diar.calls == 1 and rig.job(b)["state"] == wire.JobState.RUNNING
    gate.set()
    rig.wait_state(a)
    rig.wait_state(b)
    assert diar.calls == 2


# -- settings ------------------------------------------------------------------


def _fields(**over):
    base = settings_mod.Settings(model="small.en").to_dict()
    base.update(over)
    return base


def test_settings_defaults_and_round_trip(tmp_path):
    s = settings_mod.load_settings(tmp_path)
    assert (s.in_person_diarization, s.in_person_helper_wait_minutes) == ("wait_then_cpu", 5)
    assert {"in_person_diarization", "in_person_helper_wait_minutes"} <= set(s.to_dict())
    settings_mod.save_settings(
        tmp_path, settings_mod.Settings(in_person_diarization="off", in_person_helper_wait_minutes=12))
    loaded = settings_mod.load_settings(tmp_path)
    assert (loaded.in_person_diarization, loaded.in_person_helper_wait_minutes) == ("off", 12)


def test_bad_stored_values_fall_back(tmp_path):
    (tmp_path / "settings.json").write_text(
        '{"in_person_diarization": "x", "in_person_helper_wait_minutes": 999}', encoding="utf-8")
    s = settings_mod.load_settings(tmp_path)
    assert (s.in_person_diarization, s.in_person_helper_wait_minutes) == ("wait_then_cpu", 5)


def test_validate_in_person_settings():
    ok = settings_mod.validate(_fields(in_person_diarization="off", in_person_helper_wait_minutes="0"))
    assert (ok.in_person_diarization, ok.in_person_helper_wait_minutes) == ("off", 0)
    assert settings_mod.validate(_fields(in_person_helper_wait_minutes="60")).in_person_helper_wait_minutes == 60
    absent = _fields()
    absent.pop("in_person_helper_wait_minutes")
    assert settings_mod.validate(absent).in_person_helper_wait_minutes == 5
    for bad in ({"in_person_diarization": "maybe"}, {"in_person_helper_wait_minutes": "61"},
                {"in_person_helper_wait_minutes": "-1"}, {"in_person_helper_wait_minutes": "soon"}):
        with pytest.raises(settings_mod.ValidationError):
            settings_mod.validate(_fields(**bad))


def test_put_settings_round_trip_and_older_callers_keep_values(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, offload=False, in_person_diarization="off", in_person_helper_wait_minutes=9)
    payload = settings_mod.load_settings(rig.store.root).to_dict()
    for key in ("in_person_diarization", "in_person_helper_wait_minutes"):
        payload.pop(key)
    payload.pop("revision", None)
    assert rig.client.put("/v1/settings", json=payload).status_code == 200
    kept = settings_mod.load_settings(rig.store.root)
    assert (kept.in_person_diarization, kept.in_person_helper_wait_minutes) == ("off", 9)
    payload.update(in_person_diarization="wait_then_cpu", in_person_helper_wait_minutes=20)
    resp = rig.client.put("/v1/settings", json=payload)
    assert resp.status_code == 200, resp.text
    body = rig.client.get("/v1/settings").json()
    assert (body["in_person_diarization"], body["in_person_helper_wait_minutes"]) == ("wait_then_cpu", 20)
    assert rig.client.put("/v1/settings", json={**payload, "in_person_helper_wait_minutes": 70}).status_code == 400


def test_settings_page_form_round_trip(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, offload=False)
    page = rig.client.get("/settings").text
    assert "In-person meetings (phone recordings)" in page and "Wait for a GPU helper (minutes)" in page
    assert 'name="in_person_helper_wait_minutes"' in page and '<option value="wait_then_cpu" selected>' in page
    current = settings_mod.load_settings(rig.store.root)
    form = {k: ("on" if v is True else str(v)) for k, v in current.to_dict().items()
            if isinstance(v, (str, int, bool)) and v is not False and v is not None}
    form.update(in_person_diarization="off", in_person_helper_wait_minutes="15")
    resp = rig.client.post("/settings", data=form)
    assert resp.status_code == 200, resp.text
    saved = settings_mod.load_settings(rig.store.root)
    assert (saved.in_person_diarization, saved.in_person_helper_wait_minutes) == ("off", 15)
    assert '<option value="off" selected>' in rig.client.get("/settings").text


# -- review prompt -------------------------------------------------------------


def _workflow(rig, session_id):
    review = rig.store.create_review(session_id)
    resp = rig.client.get(f"/v1/bridge/review/{review['review_id']}/workflow.md")
    assert resp.status_code == 200, resp.text
    return resp.text


def test_prompt_line_only_for_in_person_meetings(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, offload=False)
    rig.wait_state(upload_and_finalize(rig.client, "pr1"))
    rig.wait_state(upload_and_finalize(rig.client, "pr2", in_person=False))
    assert names_mod.IN_PERSON_PROMPT_LINE in _workflow(rig, "pr1")
    assert names_mod.IN_PERSON_PROMPT_LINE not in _workflow(rig, "pr2")
