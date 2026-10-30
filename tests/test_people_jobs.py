"""Voice profiles through the job pipeline (local call, in-person, GPU helper) and the /v1/people REST surface."""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from meeting_notes import wire
from meeting_notes.server import people
from meeting_notes.server.jobs import diarize_call_full
from meeting_notes.transcribe.diarize import Diarization, SpeakerTurn
from meeting_notes.transcribe.protocol import Segment
from tests.compute_helpers import Rig
from tests.test_people import DIM, basis, mix
from tests.test_server import silence_pcm

SECONDS = 60.0


def finalize(client, sid, *, seconds=SECONDS, tracks=("mic", "system"), meta_extra=None):
    for track in tracks:
        pcm = silence_pcm(seconds)
        resp = client.post(
            wire.track_upload_path(sid, track), content=pcm,
            headers={"Content-Type": "application/octet-stream", "X-Frames": str(len(pcm) // 2)})
        assert resp.status_code == 200, resp.text
    meta = {"created": "2026-10-22", "tracks": {t: {} for t in tracks}, **(meta_extra or {})}
    resp = client.post(wire.finalize_path(sid), json={"meta": meta, "timing": {}, "settings": {}})
    assert resp.status_code == 200, resp.text
    return resp.json()["job_id"]


class Speech:
    """Transcriber stub with several segments per track, so speaker labels have something to attach to."""

    SEGMENTS = {
        "system": [(1, 5, "a talks"), (16, 20, "b talks")],
        "mic": [(1, 3, "hello"), (6, 10, "room talk"), (22, 26, "echo talk"), (35, 37, "short"), (46, 50, "mike again")],
        "": [(1, 5, "speaker one"), (16, 20, "speaker two")],
    }

    def __init__(self):
        self.calls = []

    def transcribe(self, wav_path, track):
        self.calls.append(track)
        return [Segment(start=a, end=b, text=t, track=track) for a, b, t in self.SEGMENTS[track]]


class FullDiarizer:
    """Has ``diarize_full`` like PyannoteDiarizer; the answer depends on the track (the WAV's name)."""

    def __init__(self, system_vectors=None, fail=False):
        self.calls = []
        self.plain_calls = 0
        self.fail = fail
        self.system_vectors = system_vectors or {"A": basis(0), "B": basis(1)}

    def diarize_full(self, wav_path, progress=None, min_speakers=None, max_speakers=None):
        self.calls.append((wav_path.stem, min_speakers, max_speakers))
        if self.fail:
            raise RuntimeError("boom")
        if wav_path.stem == "mic":
            turns = [SpeakerTurn(0, 4, "M0"), SpeakerTurn(4, 20, "M1"), SpeakerTurn(20, 34, "M2"),
                     SpeakerTurn(34, 38, "M3"), SpeakerTurn(44, 60, "M0")]
            vectors = {"M0": basis(5), "M1": basis(2), "M2": mix(0, 4, 0.9), "M3": basis(3)}
        else:
            turns = [SpeakerTurn(0, 14, "A"), SpeakerTurn(14, 30, "B")]
            vectors = self.system_vectors
        return Diarization(turns, vectors)

    def diarize(self, wav_path, progress=None):
        self.plain_calls += 1
        return self.diarize_full(wav_path, progress).turns


def make_rig(tmp_path, monkeypatch, *, on=True, diarizer=None, offload=False, **settings):
    diar = diarizer or FullDiarizer()
    settings = {"diarization_enabled": True, "voice_profiles_enabled": on, **settings}
    rig = Rig(tmp_path, monkeypatch, offload=offload, transcriber=Speech(), diarizer_factory=lambda: diar, **settings)
    rig.diar = diar
    return rig


def labels(rig, job_id):
    return [(s["label"], s["text"]) for s in rig.transcript(job_id)["segments"]]


def voiceprint(rig, sid):
    return json.loads((rig.store.root / "people" / "voiceprints" / f"{sid}.json").read_text(encoding="utf-8"))


def by_label(rig, sid):
    return {a["label"]: a for a in voiceprint(rig, sid)["speakers"]}


# -- a call, run on this server -----------------------------------------------------------------------------------


def test_a_call_labels_the_system_and_mic_tracks_and_records_voiceprints(tmp_path, monkeypatch):
    rig = make_rig(tmp_path, monkeypatch)
    job = rig.wait_state(finalize(rig.client, "c1"))
    assert job["state"] == wire.JobState.DONE
    got = dict((text, label) for label, text in labels(rig, job["job_id"]))
    assert got == {"a talks": "Them 1", "b talks": "Them 2", "hello": "You", "room talk": "In room 1",
                   "echo talk": "You", "short": "You", "mike again": "You"}  # echo + fragment fold into You
    assert sorted(rig.diar.calls) == [("mic", 1, 4), ("system", None, None)]
    spoken = by_label(rig, "c1")
    assert sorted(spoken) == ["In room 1", "Them 1", "Them 2", "You"]
    assert spoken["You"]["link"] == "confirmed" and spoken["You"]["track"] == "mic"
    assert all(a["clips"] for a in spoken.values()) and "embedding" in spoken["Them 1"]
    shown = people.overlay(rig.store.root, "c1")
    assert shown["You"]["display"] == "You" and {shown[k]["display"] for k in ("Them 1", "Them 2", "In room 1")} == {
        "Person 1", "Person 2", "Person 3"}
    assert job["speakers_progress"] == 1.0


def test_the_next_meeting_recognises_the_same_voices(tmp_path, monkeypatch):
    rig = make_rig(tmp_path, monkeypatch)
    rig.wait_state(finalize(rig.client, "c1"))
    rig.wait_state(finalize(rig.client, "c2"))
    first, second = by_label(rig, "c1"), by_label(rig, "c2")
    for label in ("Them 1", "Them 2", "In room 1", "You"):
        assert first[label]["person_id"] == second[label]["person_id"], label
    assert second["Them 1"]["link"] == "auto"
    assert len(people.list_people(rig.store)) == 4
    assert all(p["meetings"] == 2 for p in people.list_people(rig.store))


def test_a_people_count_only_caps_the_far_end_when_the_mic_is_diarized(tmp_path, monkeypatch):
    rig = make_rig(tmp_path, monkeypatch)
    rig.wait_state(finalize(rig.client, "c1", meta_extra={"people_count": 3}))
    assert sorted(rig.diar.calls, key=str) == [("mic", 1, 4), ("system", None, 2)]


def test_a_people_count_of_two_skips_diarization_and_records_nothing(tmp_path, monkeypatch):
    rig = make_rig(tmp_path, monkeypatch)
    job = rig.wait_state(finalize(rig.client, "c1", meta_extra={"people_count": 2}))
    assert job["state"] == wire.JobState.DONE and rig.diar.calls == []
    assert not (rig.store.root / "people").exists()


def test_feature_off_is_the_old_behaviour(tmp_path, monkeypatch):
    rig = make_rig(tmp_path, monkeypatch, on=False)
    job = rig.wait_state(finalize(rig.client, "c1"))
    assert {t: lab for lab, t in labels(rig, job["job_id"])} == {
        "a talks": "Them 1", "b talks": "Them 2", "hello": "You", "room talk": "You", "echo talk": "You",
        "short": "You", "mike again": "You"}
    assert rig.diar.calls == [("system", None, None)] and rig.diar.plain_calls == 1  # plain diarize(), system only
    assert not (rig.store.root / "people").exists()


def test_diarization_off_means_voice_profiles_are_off_too(tmp_path, monkeypatch):
    rig = make_rig(tmp_path, monkeypatch, diarization_enabled=False)
    job = rig.wait_state(finalize(rig.client, "c1"))
    assert job["state"] == wire.JobState.DONE and not (rig.store.root / "people").exists()


def test_a_diarizer_without_diarize_full_still_labels_but_records_nothing(tmp_path, monkeypatch):
    class Plain:
        def diarize(self, wav_path):
            return [SpeakerTurn(0, 14, "A"), SpeakerTurn(14, 30, "B")]

    rig = make_rig(tmp_path, monkeypatch, diarizer=Plain())
    job = rig.wait_state(finalize(rig.client, "c1"))
    assert [lab for lab, _ in labels(rig, job["job_id"]) if lab.startswith("Them")] == ["Them 1", "Them 2"]
    assert not (rig.store.root / "people" / "voiceprints").exists()


def test_a_failed_retranscribe_diarization_drops_the_old_voiceprint_and_keeps_the_job(tmp_path, monkeypatch):
    rig = make_rig(tmp_path, monkeypatch)
    rig.wait_state(finalize(rig.client, "c1"))
    assert (rig.store.root / "people" / "voiceprints" / "c1.json").exists()
    rig.diar.fail = True
    resp = rig.client.post("/v1/sessions/c1/retranscribe")
    assert resp.status_code in (200, 202), resp.text
    job = rig.wait_state(resp.json()["job_id"])
    assert job["state"] == wire.JobState.DONE and job["warnings"]
    assert not (rig.store.root / "people" / "voiceprints" / "c1.json").exists()


def test_a_voiceprint_failure_never_fails_the_job(tmp_path, monkeypatch):
    rig = make_rig(tmp_path, monkeypatch)
    monkeypatch.setattr(people, "record_meeting", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    job = rig.wait_state(finalize(rig.client, "c1"))
    assert job["state"] == wire.JobState.DONE and labels(rig, job["job_id"])


def test_diarize_call_full_passes_only_what_the_diarizer_accepts():
    seen = {}

    class Narrow:
        def diarize(self, wav):
            seen["narrow"] = True
            return [SpeakerTurn(0, 1, "A")]

    class Wide:
        def diarize_full(self, wav, progress=None, min_speakers=None, max_speakers=None):
            seen["wide"] = (progress, min_speakers, max_speakers)
            return Diarization([SpeakerTurn(0, 1, "A")], {"A": [1.0, 0.0]})

    assert diarize_call_full(Narrow(), "x.wav", progress=print, pin=3, full=True).turns[0].speaker == "A"
    assert diarize_call_full(Wide(), "x.wav", pin=3, bound=True, full=True).embeddings == {"A": [1.0, 0.0]}
    assert seen["wide"] == (None, None, 3)
    diarize_call_full(Wide(), "x.wav", pin=3, full=True)
    assert seen["wide"] == (None, 3, 3)
    diarize_call_full(Wide(), "x.wav", full=True, min_speakers=1, max_speakers=4)
    assert seen["wide"] == (None, 1, 4)


# -- in person ----------------------------------------------------------------------------------------------------


def test_an_in_person_meeting_records_speaker_n_voiceprints(tmp_path, monkeypatch):
    rig = make_rig(tmp_path, monkeypatch)
    job = rig.wait_state(finalize(rig.client, "ip1", tracks=("system",), meta_extra={"in_person": True, "source": "mobile"}))
    assert job["state"] == wire.JobState.DONE
    assert sorted(by_label(rig, "ip1")) == ["Speaker 1", "Speaker 2"]
    assert [lab for lab, _ in labels(rig, job["job_id"])] == ["Speaker 1", "Speaker 2"]
    assert rig.diar.calls == [("system", None, None)]  # no mic track, nothing extra


# -- a GPU helper ---------------------------------------------------------------------------------------------------


def vec(i):
    v = [0.0] * DIM
    v[i] = 1.0
    return v


def helper_result(rig, lease, **extra):
    return {**rig.good_result(lease), **extra}


def complete(rig, job_id, body):
    return rig.client.post(f"/v1/compute/jobs/{job_id}/complete", json=body, headers=rig.helper_headers)


def test_lease_fields(tmp_path, monkeypatch):
    on = make_rig(tmp_path / "on", monkeypatch, offload=True)
    on.offered_job(seconds=SECONDS)
    d = on.claim()["diarization"]
    assert d["enabled"] and d["embeddings"] is True and d["mic"] is True
    off = make_rig(tmp_path / "off", monkeypatch, on=False, offload=True)
    off.offered_job(seconds=SECONDS)
    d = off.claim()["diarization"]
    assert d["enabled"] and "embeddings" not in d and "mic" not in d


def test_a_helper_result_with_embeddings_is_matched_and_the_mic_split_server_side(tmp_path, monkeypatch):
    rig = make_rig(tmp_path, monkeypatch, offload=True)
    job_id = rig.offered_job(seconds=SECONDS)
    lease = rig.claim()
    body = helper_result(
        rig, lease,
        speaker_turns=[{"start": 0, "end": 14, "speaker": "S0"}, {"start": 14, "end": 30, "speaker": "S1"}],
        speaker_turns_mic=[{"start": 0, "end": 30, "speaker": "M0"}, {"start": 30, "end": 50, "speaker": "M1"},
                           {"start": 50, "end": 54, "speaker": "M2"}],
        speaker_embeddings={"system": {"S0": vec(0), "S1": vec(1)},
                            "mic": {"M0": vec(5), "M1": vec(2), "M2": vec(3)}})
    body["tracks"]["mic"] = [{"start": 1, "end": 3, "text": "mike"}, {"start": 35, "end": 40, "text": "in the room"}]
    body["tracks"]["system"] = [{"start": 1, "end": 5, "text": "far end"}]
    assert complete(rig, job_id, body).status_code == 200
    rig.wait_state(job_id)
    assert {t: lab for lab, t in labels(rig, job_id)} == {"mike": "You", "in the room": "In room 1", "far end": "Them 1"}
    spoken = by_label(rig, "s1")
    assert sorted(spoken) == ["In room 1", "Them 1", "Them 2", "You"]  # M2 is short: folded into You


def test_bad_helper_vectors_are_dropped_not_rejected(tmp_path, monkeypatch):
    rig = make_rig(tmp_path, monkeypatch, offload=True)
    job_id = rig.offered_job(seconds=SECONDS)
    lease = rig.claim()
    body = helper_result(
        rig, lease,
        speaker_turns=[{"start": 0, "end": 14, "speaker": "S0"}, {"start": 14, "end": 30, "speaker": "S1"},
                       {"start": 30, "end": 40, "speaker": "S2"}, {"start": 40, "end": 50, "speaker": "S3"}],
        speaker_embeddings={"system": {"S0": vec(0), "S1": [1.0, 2.0], "S2": ["x"] * DIM, "S3": vec(3), "GHOST": vec(4)},
                            "weird": {"S0": vec(0)}, "mic": "nope"})
    assert complete(rig, job_id, body).status_code == 200
    rig.wait_state(job_id)
    assert sorted(by_label(rig, "s1")) == ["Them 1", "Them 4"]  # only the two good vectors survived


def test_embeddings_in_a_garbage_shape_are_ignored(tmp_path, monkeypatch):
    rig = make_rig(tmp_path, monkeypatch, offload=True)
    job_id = rig.offered_job(seconds=SECONDS)
    lease = rig.claim()
    body = helper_result(rig, lease, speaker_turns=[{"start": 0, "end": 14, "speaker": "S0"}],
                         speaker_embeddings=[1, 2, 3])
    assert complete(rig, job_id, body).status_code == 200
    assert rig.wait_state(job_id)["state"] == wire.JobState.DONE
    assert not (rig.store.root / "people" / "voiceprints" / "s1.json").exists()


def test_mic_turns_are_422_when_not_requested_and_checked_against_the_mic_length(tmp_path, monkeypatch):
    off = make_rig(tmp_path / "off", monkeypatch, on=False, offload=True)
    job_id = off.offered_job(seconds=SECONDS)
    lease = off.claim()
    body = helper_result(off, lease, speaker_turns_mic=[{"start": 0, "end": 5, "speaker": "M0"}])
    assert complete(off, job_id, body).status_code == 422
    on = make_rig(tmp_path / "on", monkeypatch, offload=True)
    job_id = on.offered_job(seconds=SECONDS)
    lease = on.claim()
    too_long = helper_result(on, lease, speaker_turns_mic=[{"start": 0, "end": SECONDS + 30, "speaker": "M0"}])
    assert complete(on, job_id, too_long).status_code == 422


def test_a_helper_result_is_unchanged_when_the_feature_is_off(tmp_path, monkeypatch):
    rig = make_rig(tmp_path, monkeypatch, on=False, offload=True)
    job_id = rig.offered_job(seconds=SECONDS)
    lease = rig.claim()
    body = helper_result(rig, lease, speaker_turns=[{"start": 0, "end": 14, "speaker": "S0"}],
                         speaker_embeddings={"system": {"S0": vec(0)}})
    assert complete(rig, job_id, body).status_code == 200
    rig.wait_state(job_id)
    assert not (rig.store.root / "people").exists()
    assert {lab for lab, _ in labels(rig, job_id)} == {"You", "Them 1"}


# -- the helper runtime ---------------------------------------------------------------------------------------------


def test_engine_diarize_passes_bounds_per_call_and_returns_embeddings(tmp_path):
    from meeting_notes.compute_helper.engine import Engine

    class Shared:
        min_speakers = max_speakers = None

        def __init__(self):
            self.seen = []

        def diarize_full(self, wav, progress=None, min_speakers=None, max_speakers=None):
            self.seen.append((min_speakers, max_speakers))
            return Diarization([SpeakerTurn(0, 2, "A")], {"A": [1.0, 0.0]})

    engine = Engine(tmp_path)
    shared = Shared()
    engine.load_diarizer = lambda: shared
    turns, vectors = engine.diarize_full(tmp_path / "x.wav", {"min_speakers": 1, "max_speakers": 4})
    assert turns == [{"start": 0.0, "end": 2.0, "speaker": "A"}] and vectors == {"A": [1.0, 0.0]}
    assert engine.diarize(tmp_path / "x.wav", {"min_speakers": None, "max_speakers": 2}) == turns
    assert shared.seen == [(1, 4), (None, 2)]
    assert (shared.min_speakers, shared.max_speakers) == (None, None)  # the shared instance is never changed


# -- REST and the read-time overlay ------------------------------------------------------------------------------------


@pytest.fixture
def rig(tmp_path, monkeypatch):
    r = make_rig(tmp_path, monkeypatch)
    r.wait_state(finalize(r.client, "c1"))
    r.wait_state(finalize(r.client, "c2"))
    return r


def person_of(rig, sid, label):
    return by_label(rig, sid)[label]["person_id"]


def test_everything_is_404_while_off(tmp_path, monkeypatch):
    rig = make_rig(tmp_path, monkeypatch, on=False)
    c = rig.client
    for method, path in (("get", "/v1/people"), ("get", "/v1/people/p_x"), ("patch", "/v1/people/p_x"),
                         ("post", "/v1/people/p_x/merge"), ("post", "/v1/people/appearances"),
                         ("delete", "/v1/people/p_x"), ("delete", "/v1/people"),
                         ("get", "/v1/people/p_x/clips/s1/a-1.wav")):
        assert getattr(c, method)(path).status_code == 404, (method, path)


def test_the_token_is_required(tmp_path, monkeypatch):
    rig = make_rig(tmp_path, monkeypatch)
    monkeypatch.setenv("MEETING_NOTES_TOKEN", "secret")
    assert rig.client.get("/v1/people").status_code in (401, 403)
    assert rig.client.get("/v1/people", headers={"Authorization": "Bearer secret"}).status_code == 200


def test_list_and_profile(rig):
    listing = rig.client.get("/v1/people?sort=alpha").json()["people"]
    assert len(listing) == 4 and {p["display"] for p in listing} == {"You", "Person 1", "Person 2", "Person 3"}
    assert rig.client.get("/v1/people?sort=sideways").status_code == 400
    pid = person_of(rig, "c1", "Them 1")
    profile = rig.client.get(f"/v1/people/{pid}").json()
    assert profile["meetings"] == 2 and len(profile["appearances"]) == 2 and profile["samples"]
    assert {q["text"] for q in profile["quotes"]} == {"a talks"}
    assert "embedding" not in json.dumps(profile)
    assert rig.client.get("/v1/people/p_nope").status_code == 404
    assert rig.client.get("/v1/people/..%2Fx").status_code in (400, 404)


def test_patch_merge_delete(rig):
    c = rig.client
    a, b = person_of(rig, "c1", "Them 1"), person_of(rig, "c1", "Them 2")
    done = c.patch(f"/v1/people/{a}", json={"name": "Ann Lee", "email": "ann@example.com", "company": "Acme"})
    assert done.status_code == 200 and done.json()["display"] == "Ann Lee"
    assert c.patch(f"/v1/people/{a}", json={"email": "not an email"}).status_code == 400
    assert c.patch(f"/v1/people/{a}", json={"name": 5}).status_code == 400
    assert c.patch("/v1/people/p_nope", json={"name": "x"}).status_code == 404
    merged = c.post(f"/v1/people/{b}/merge", json={"into": a})
    assert merged.status_code == 200 and merged.json()["meetings"] == 2
    assert c.post(f"/v1/people/{a}/merge", json={"into": a}).status_code == 400
    assert c.post(f"/v1/people/{a}/merge", json={}).status_code == 400
    assert c.delete(f"/v1/people/{a}").json() == {"ok": True}
    assert by_label(rig, "c1")["Them 1"]["person_id"] is None
    assert c.delete(f"/v1/people/{a}").status_code == 404


def test_appearance_actions_over_rest(rig):
    c = rig.client
    a, room = person_of(rig, "c1", "Them 1"), person_of(rig, "c1", "In room 1")
    out = c.post("/v1/people/appearances", json={"session_id": "c1", "label": "Them 1", "action": "unlink"})
    assert out.status_code == 200 and out.json()["person_id"] not in (a, room) and out.json()["link"] == "confirmed"
    out = c.post("/v1/people/appearances", json={"session_id": "c1", "label": "Them 1", "action": "assign", "person_id": room})
    assert out.json()["person_id"] == room
    assert c.post("/v1/people/appearances", json={"session_id": "c1", "label": "Them 1", "action": "confirm"}).status_code == 400
    assert c.post("/v1/people/appearances", json={"session_id": "c1", "label": "Nobody", "action": "unlink"}).status_code == 404
    assert c.post("/v1/people/appearances", json={"label": "x"}).status_code == 400


def test_clip_route_serves_stored_samples_only(rig):
    pid = person_of(rig, "c1", "Them 1")
    sample = rig.client.get(f"/v1/people/{pid}").json()["samples"][0]
    resp = rig.client.get(sample["url"])
    assert resp.status_code == 200 and resp.headers["content-type"] == "audio/wav" and resp.content[:4] == b"RIFF"
    other = person_of(rig, "c1", "Them 2")
    assert rig.client.get(sample["url"].replace(pid, other)).status_code == 404
    assert rig.client.get(f"/v1/people/{pid}/clips/c1/..%2F..%2Fpeople.json").status_code in (400, 404)
    assert rig.client.get(f"/v1/people/{pid}/clips/c1/nothing-9.wav").status_code == 404


def test_delete_all_removes_the_folder(rig):
    assert rig.client.delete("/v1/people").json() == {"ok": True}
    assert not (rig.store.root / "people").exists()
    assert rig.client.get("/v1/people").json() == {"people": []}


def test_session_json_gets_person_ids_display_names_and_suggestions(rig):
    pid = person_of(rig, "c1", "Them 1")
    rig.client.patch(f"/v1/people/{pid}", json={"name": "Ann Lee"})
    detail = rig.client.get("/v1/sessions/c2").json()
    shown = {s["label"]: (s["person_id"], s["display"]) for s in detail["segments"]}
    assert shown["Them 1"] == (pid, "Ann Lee") and shown["You"][1] == "You" and shown["In room 1"][1].startswith("Person ")
    assert detail["people_suggestions"] == []
    # a pending suggestion is listed
    pending = by_label(rig, "c2")["Them 2"]
    other = person_of(rig, "c2", "Them 1")
    vp_path = rig.store.root / "people" / "voiceprints" / "c2.json"
    data = json.loads(vp_path.read_text(encoding="utf-8"))
    for a in data["speakers"]:
        if a["label"] == "Them 2":
            a.update(link="suggested", suggested_person_id=other)
    vp_path.write_text(json.dumps(data), encoding="utf-8")
    people._voiceprints.clear()
    assert pending
    suggestions = rig.client.get("/v1/sessions/c2").json()["people_suggestions"]
    assert [(s["label"], s["suggested_display"]) for s in suggestions] == [("Them 2", "Ann Lee")]


def test_session_json_is_untouched_while_off(tmp_path, monkeypatch):
    rig = make_rig(tmp_path, monkeypatch, on=False)
    rig.wait_state(finalize(rig.client, "c1"))
    detail = rig.client.get("/v1/sessions/c1").json()
    assert "people_suggestions" not in detail and all("person_id" not in s for s in detail["segments"])


def test_notes_transcript_and_export_zip_use_names_of_named_people_only(rig):
    pid = person_of(rig, "c1", "Them 1")
    rig.client.patch(f"/v1/people/{pid}", json={"name": "Ann Lee"})
    review = rig.store.create_review("c1")
    text = rig.client.get(f"/v1/bridge/review/{review['review_id']}/transcript").text
    assert "Ann Lee:** a talks" in text and "Them 2:** b talks" in text and "You:** hello" in text
    zipped = rig.client.get("/sessions/c1/export.zip")
    assert zipped.status_code == 200
    with zipfile.ZipFile(io.BytesIO(zipped.content)) as archive:
        transcript = next(archive.read(n).decode("utf-8") for n in archive.namelist() if "transcript" in n)
    assert "Ann Lee:** a talks" in transcript and "Them 2:** b talks" in transcript
    stored = rig.store.read_transcript(rig.store.latest_done_job("c1")["job_id"])["markdown"]
    assert "Them 1:** a talks" in stored  # the stored transcript is never rewritten


def test_deleting_a_meeting_for_good_forgets_its_voices(rig):
    rig.client.delete("/v1/sessions/c1")  # soft delete: the voiceprint stays but is hidden
    assert (rig.store.root / "people" / "voiceprints" / "c1.json").exists()
    assert all(p["meetings"] == 1 for p in people.list_people(rig.store))
    assert rig.store.purge_trashed("c1")
    assert not (rig.store.root / "people" / "voiceprints" / "c1.json").exists()
    assert not (rig.store.root / "people" / "clips" / "c1").exists()


# -- the helper's poll loop ---------------------------------------------------------------------------------------------


class VoiceEngine:
    """The fake engine of test_compute_helper plus ``diarize_full`` (turns and embeddings)."""

    def __init__(self):
        from tests.test_compute_helper import FakeEngine

        self.base = FakeEngine()
        self.calls = []

    def __getattr__(self, name):
        return getattr(self.base, name)

    def diarize_full(self, wav, diarization, progress=None):
        self.calls.append((wav.stem, diarization.get("min_speakers"), diarization.get("max_speakers")))
        if progress:
            progress(1.0)
        return [{"start": 0.0, "end": 1.0, "speaker": f"{wav.stem}_0"}], {f"{wav.stem}_0": [0.5, 0.5]}


def run_worker(engine, diarization):
    from tests.test_compute_helper import FakeServer, Harness, _lease

    server = FakeServer([_lease(diarization={"enabled": True, "model": "x", "min_speakers": None,
                                             "max_speakers": 2, **diarization})])
    h = Harness(server, engine).start()
    assert h.wait_for(lambda: server.posted("/complete"))
    h.stop()
    return server.posted("/complete")[0]


def test_the_worker_diarizes_the_mic_and_returns_embeddings_when_the_lease_asks():
    engine = VoiceEngine()
    body = run_worker(engine, {"mic": True, "embeddings": True})
    assert sorted(engine.calls) == [("mic", 1, 4), ("system", None, 2)]
    assert body["speaker_turns"] == [{"start": 0.0, "end": 1.0, "speaker": "system_0"}]
    assert body["speaker_turns_mic"] == [{"start": 0.0, "end": 1.0, "speaker": "mic_0"}]
    assert body["speaker_embeddings"] == {"system": {"system_0": [0.5, 0.5]}, "mic": {"mic_0": [0.5, 0.5]}}


def test_the_worker_result_is_unchanged_without_the_new_lease_fields():
    engine = VoiceEngine()
    body = run_worker(engine, {})
    assert engine.calls == []  # plain diarize() was used
    assert body["speaker_turns"] == [{"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00"}]
    assert "speaker_turns_mic" not in body and "speaker_embeddings" not in body


def test_an_engine_without_diarize_full_still_works_with_embeddings_requested():
    from tests.test_compute_helper import FakeEngine

    body = run_worker(FakeEngine(), {"embeddings": True})
    assert body["speaker_turns"] and body["speaker_embeddings"] == {}


# -- the setting ---------------------------------------------------------------------------------------------------------


def test_voice_profiles_setting_defaults_off_and_round_trips(tmp_path, monkeypatch):
    from meeting_notes.server import settings as settings_mod

    rig = make_rig(tmp_path / "x", monkeypatch, on=False)
    base = {"model": "small.en", "beam_size": 5, "audio_retention_days": -1, "retention_check_interval_minutes": 60,
            "diarization_model": "pyannote/speaker-diarization-community-1"}
    got = rig.client.get("/v1/settings").json()
    assert got["voice_profiles_enabled"] is False
    put = rig.client.put("/v1/settings", json={**base, "diarization_enabled": True, "voice_profiles_enabled": True})
    assert put.status_code == 200 and put.json()["voice_profiles_enabled"] is True, put.text
    assert settings_mod.load_settings(rig.store.root).voice_profiles_enabled is True
    # an older caller that does not send it must not switch it off
    rig.client.put("/v1/settings", json={**base, "diarization_enabled": True})
    assert settings_mod.load_settings(rig.store.root).voice_profiles_enabled is True
    # the web form keeps it too when the checkbox is not on the form, and obeys an explicit 0
    form = {"model": "small.en", "beam_size": "5", "audio_retention_days": "-1", "retention_check_interval_minutes": "60",
            "ai_provider": "disabled", "appearance": "system", "diarization_enabled": "on"}
    rig.client.post("/settings", data=form)
    assert settings_mod.load_settings(rig.store.root).voice_profiles_enabled is True
    rig.client.post("/settings", data={**form, "voice_profiles_enabled": "0"})
    assert settings_mod.load_settings(rig.store.root).voice_profiles_enabled is False
