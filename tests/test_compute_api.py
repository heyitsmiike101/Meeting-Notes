"""The /v1/compute helper API: keys, polling, leases, results, and every refusal."""

from __future__ import annotations

import json

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from meeting_notes import wire
from meeting_notes.server import compute as compute_mod
from meeting_notes.server import compute_api
from tests.compute_helpers import Rig

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated:DeprecationWarning"
)


def _complete(rig: Rig, job_id: str, payload, headers=None):
    return rig.client.post(f"/v1/compute/jobs/{job_id}/complete", json=payload, headers=headers or rig.helper_headers)


# -- keys ---------------------------------------------------------------------


def test_register_list_and_revoke_helpers(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    resp = rig.client.post("/v1/compute/helpers", json={"name": "Den PC", "instance_id": "inst-2"})
    assert resp.status_code == 201
    created = resp.json()
    assert created["key"].startswith("mnh_") and created["helper_id"]
    rows = rig.client.get("/v1/compute/helpers").json()["helpers"]
    assert {r["name"] for r in rows} == {"Test PC", "Den PC"}
    assert all("key_hash" not in r and "key" not in r for r in rows)
    assert rig.client.delete(f"/v1/compute/helpers/{created['helper_id']}").status_code == 200
    assert rig.client.delete("/v1/compute/helpers/nope").status_code == 404
    revoked = {r["name"]: r["revoked"] for r in rig.client.get("/v1/compute/helpers").json()["helpers"]}
    assert revoked == {"Test PC": False, "Den PC": True}
    assert rig.poll(headers={"Authorization": f"Bearer {created['key']}"}).status_code == 401


def test_registration_validates_and_needs_the_main_token(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    assert rig.client.post("/v1/compute/helpers", json={"name": "", "instance_id": "x"}).status_code == 400
    assert rig.client.post("/v1/compute/helpers", json={"name": "x", "instance_id": ""}).status_code == 400
    monkeypatch.setenv("MEETING_NOTES_TOKEN", "main-secret")
    assert rig.client.post("/v1/compute/helpers", json={"name": "x", "instance_id": "y"}).status_code == 401
    assert rig.client.get("/v1/compute/helpers", headers=rig.helper_headers).status_code == 403  # a helper key is not the main token
    ok = rig.client.post("/v1/compute/helpers", json={"name": "x", "instance_id": "y"}, headers={"Authorization": "Bearer main-secret"})
    assert ok.status_code == 201


def test_same_instance_id_replaces_the_old_key(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    old = rig.helper_headers
    new = rig.register("Test PC again", "inst-1")
    assert rig.poll(headers=old).status_code == 401
    assert rig.poll(headers=new).status_code == 204


def test_keys_are_stored_hashed_only(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    key = rig.helper_headers["Authorization"].split()[1]
    text = (tmp_path / "data" / "compute" / "helpers.json").read_text(encoding="utf-8")
    assert key not in text
    record = json.loads(text)["helpers"][0]
    assert record["key_hash"] == compute_mod.hash_secret(key) and record["key_prefix"] == key[:12]
    # durable: a fresh registry over the same root still authenticates it
    assert compute_mod.HelperRegistry(tmp_path / "data").authenticate(key)["helper_id"] == record["helper_id"]


def test_helper_endpoints_need_a_helper_key(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    assert rig.client.post("/v1/compute/poll", json={}).status_code == 401
    assert rig.client.post("/v1/compute/poll", json={}, headers={"Authorization": "Bearer mnh_unknown"}).status_code == 403
    monkeypatch.setenv("MEETING_NOTES_TOKEN", "main-secret")
    assert rig.client.post("/v1/compute/poll", json={}, headers={"Authorization": "Bearer main-secret"}).status_code == 403


def test_require_helper_key_or_token_accepts_either(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    probe = FastAPI()
    probe.state.compute_registry = rig.registry

    @probe.get("/probe")
    def probe_route(who=Depends(compute_mod.require_helper_key_or_token)):
        return {"helper": who["name"] if who else None}

    client = TestClient(probe)
    monkeypatch.setenv("MEETING_NOTES_TOKEN", "main-secret")
    assert client.get("/probe", headers=rig.helper_headers).json() == {"helper": "Test PC"}
    assert client.get("/probe", headers={"Authorization": "Bearer main-secret"}).json() == {"helper": None}
    assert client.get("/probe").status_code == 401
    assert client.get("/probe", headers={"Authorization": "Bearer wrong"}).status_code == 403
    assert client.get("/probe", headers={"Authorization": "Bearer mnh_wrong"}).status_code == 403
    key = rig.helper_headers["Authorization"].split()[1]
    rig.registry.revoke_helper(rig.registry.authenticate(key)["helper_id"])
    assert client.get("/probe", headers=rig.helper_headers).status_code == 401


# -- polling ------------------------------------------------------------------


def test_poll_returns_204_without_work_and_pack_outdated_header(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    assert rig.poll().status_code == 204
    rig.registry.pack_version_provider = lambda: "p2"
    resp = rig.poll()  # still reports pack p1
    assert resp.status_code == 204 and resp.headers["X-Pack-Outdated"] == "1"
    assert rig.registry.any_available() is False


def test_outdated_or_busy_or_paused_helpers_get_no_lease(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    job_id = rig.offered_job()
    assert rig.poll(state="busy").status_code == 204
    assert rig.poll(paused=True).status_code == 204
    rig.registry.pack_version_provider = lambda: "p2"
    out = rig.poll()
    assert out.status_code == 204 and out.headers.get("X-Pack-Outdated") == "1"
    rig.registry.pack_version_provider = lambda: "p1"
    assert rig.poll().status_code == 200
    assert rig.job(job_id)["compute"]["status"] == "leased"


def test_lease_contents(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, diarization_enabled=True, diarization_min_speakers=2, diarization_max_speakers=5)
    job_id = rig.offered_job("sess-1", seconds=20.0)
    lease = rig.claim()
    assert lease["job_id"] == job_id and lease["session_id"] == "sess-1"
    assert lease["board"].startswith("M-") and lease["heartbeat_seconds"] == 15
    assert lease["deadline_seconds"] == 120 + 10  # 40 s of audio
    assert lease["tracks"] == [
        {"name": "mic", "seconds": 20.0, "url": f"/v1/compute/jobs/{job_id}/audio/mic"},
        {"name": "system", "seconds": 20.0, "url": f"/v1/compute/jobs/{job_id}/audio/system"},
    ]
    assert lease["whisper"]["model"] and lease["whisper"]["language"] is None
    assert lease["diarization"] == {"enabled": True, "model": "pyannote/speaker-diarization-community-1", "min_speakers": 2, "max_speakers": 5}
    # only the hash of the lease is stored
    stored = rig.job(job_id)["compute"]
    assert stored["lease_hash"] == compute_mod.hash_secret(lease["lease"])
    assert lease["lease"] not in json.dumps(rig.job(job_id))
    assert stored["helper_name"] == "Test PC" and stored["attempt"] == lease["attempt"]


def test_only_one_of_two_helpers_gets_the_job(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    other = rig.register("Other PC", "inst-2")
    rig.poll(headers=other)
    rig.offered_job()
    first, second = rig.poll(), rig.poll(headers=other)
    assert sorted([first.status_code, second.status_code]) == [200, 204]


# -- audio, heartbeat, release -------------------------------------------------


def test_audio_download_needs_the_live_attempt(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    job_id = rig.offered_job()
    lease = rig.claim()
    ok = rig.client.get(lease["tracks"][0]["url"], headers=rig.lease_headers(lease))
    assert ok.status_code == 200 and ok.content[:4] == b"RIFF" and ok.headers["content-type"] == "audio/wav"
    assert rig.client.get(f"/v1/compute/jobs/{job_id}/audio/nope", headers=rig.lease_headers(lease)).status_code == 404
    bad = {**rig.lease_headers(lease), "X-Lease": "wrong"}
    assert rig.client.get(lease["tracks"][0]["url"], headers=bad).status_code == 409
    assert rig.client.get(lease["tracks"][0]["url"], headers=rig.helper_headers).status_code == 409
    other = rig.register("Other PC", "inst-2")
    assert rig.client.get(lease["tracks"][0]["url"], headers={**other, "X-Attempt": lease["attempt"], "X-Lease": lease["lease"]}).status_code == 409


def test_heartbeat_updates_progress_capped_below_one_and_keeps_the_lease_alive(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    job_id = rig.offered_job()
    lease = rig.claim()
    url = f"/v1/compute/jobs/{job_id}/heartbeat"
    rig.clock.advance(50)
    assert rig.client.post(url, json=rig.body(lease, progress=0.42, step="mic"), headers=rig.helper_headers).status_code == 200
    job = rig.job(job_id)
    assert job["progress"] == 0.42 and job["compute"]["last_heartbeat"] == rig.clock.now
    rig.clock.advance(50)  # 100 s since the claim, 50 s since the heartbeat: still alive
    rig.registry.sweep()
    assert rig.job(job_id)["compute"]["status"] == "leased"
    assert rig.client.post(url, json=rig.body(lease, progress=7), headers=rig.helper_headers).status_code == 200
    assert rig.job(job_id)["progress"] == 0.99
    assert rig.client.post(url, json=rig.body(lease, progress=0.1), headers=rig.helper_headers).status_code == 200
    assert rig.job(job_id)["progress"] == 0.99  # never goes backwards
    assert rig.client.post(url, json={**rig.body(lease, progress=0.5), "lease": "x"}, headers=rig.helper_headers).status_code == 409
    helpers = rig.client.get("/v1/compute/helpers").json()["helpers"]
    assert helpers[0]["current_job"] == job_id and helpers[0]["progress"] == 0.99


def test_release_sends_the_job_local_once(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    job_id = rig.offered_job()
    lease = rig.claim()
    url = f"/v1/compute/jobs/{job_id}/release"
    assert rig.client.post(url, json={**rig.body(lease), "lease": "x"}, headers=rig.helper_headers).status_code == 409
    assert rig.client.post(url, json=rig.body(lease, error="CUDA out of memory"), headers=rig.helper_headers).status_code == 200
    job = rig.wait_state(job_id)
    assert job["state"] == wire.JobState.DONE
    assert job["compute"]["fallback_reason"] == "helper failed: CUDA out of memory"
    assert sorted(rig.transcriber.calls) == ["mic", "system"]
    assert rig.client.post(url, json=rig.body(lease), headers=rig.helper_headers).status_code == 409


def test_revoking_a_helper_takes_its_job_back(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    job_id = rig.offered_job()
    rig.claim()
    helper_id = rig.client.get("/v1/compute/helpers").json()["helpers"][0]["helper_id"]
    assert rig.client.delete(f"/v1/compute/helpers/{helper_id}").status_code == 200
    job = rig.wait_state(job_id)
    assert job["compute"]["fallback_reason"] == "helper revoked" and job["state"] == wire.JobState.DONE


# -- completion ---------------------------------------------------------------


def test_complete_finalizes_the_job_like_a_local_run(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    job_id = rig.offered_job()
    lease = rig.claim()
    resp = _complete(rig, job_id, rig.good_result(lease))
    assert resp.status_code == 200, resp.text
    job = rig.job(job_id)
    assert job["state"] == wire.JobState.DONE and job["progress"] == 1.0
    c = job["compute"]
    assert (c["status"], c["helper_name"], c["device"]) == ("done", "Test PC", "cuda")
    assert rig.transcriber.calls == []  # nothing ran on the server
    segs = rig.transcript(job_id)["segments"]
    assert sorted(s["text"] for s in segs) == ["gpu mic", "gpu system"]
    assert {s["label"] for s in segs} == {"You", "Them"}


def test_duplicate_completion_is_idempotent_and_changes_nothing(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    job_id = rig.offered_job()
    lease = rig.claim()
    assert _complete(rig, job_id, rig.good_result(lease)).status_code == 200
    before = rig.client.get(wire.job_transcript_path(job_id)).json()
    again = _complete(rig, job_id, rig.good_result(lease))
    assert again.status_code == 200 and again.json()["duplicate"] is True
    assert rig.client.get(wire.job_transcript_path(job_id)).json() == before
    # a different attempt/lease on a finished job is refused
    assert _complete(rig, job_id, {**rig.good_result(lease), "attempt": "other"}).status_code == 409


def test_late_result_from_a_revoked_attempt_is_refused(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    job_id = rig.offered_job()
    lease = rig.claim()
    rig.clock.advance(61)
    rig.registry.sweep()
    job = rig.wait_state(job_id)
    assert _complete(rig, job_id, rig.good_result(lease)).status_code == 409
    assert rig.job(job_id)["compute"]["status"] == "local"
    assert "gpu mic" not in json.dumps(rig.transcript(job_id))
    assert job["state"] == wire.JobState.DONE


def test_diarization_turns_label_the_system_track(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, diarization_enabled=True)
    job_id = rig.offered_job()
    lease = rig.claim()
    payload = rig.good_result(lease)
    payload["tracks"]["system"] = [
        {"start": 0.0, "end": 2.0, "text": "first voice"},
        {"start": 3.0, "end": 5.0, "text": "second voice"},
    ]
    payload["speaker_turns"] = [
        {"start": 0.0, "end": 2.5, "speaker": "SPEAKER_01"},
        {"start": 2.5, "end": 6.0, "speaker": "SPEAKER_00"},
    ]
    assert _complete(rig, job_id, payload).status_code == 200
    labels = {s["text"]: s["label"] for s in rig.transcript(job_id)["segments"]}
    assert labels["first voice"] == "Them 1" and labels["second voice"] == "Them 2"
    assert labels["gpu mic"] == "You"


@pytest.mark.parametrize(
    "mutate, why",
    [
        (lambda p: p["tracks"].pop("system"), "missing track"),
        (lambda p: p["tracks"].update(extra=[]), "unknown track"),
        (lambda p: p["tracks"].update(mic=[{"start": 0, "end": 12.5, "text": "x"}]), "past track length + 2 s"),
        (lambda p: p["tracks"].update(mic=[{"start": 2, "end": 1, "text": "x"}]), "end before start"),
        (lambda p: p["tracks"].update(mic=[{"start": -1, "end": 1, "text": "x"}]), "negative"),
        (lambda p: p["tracks"].update(mic=[{"start": 3, "end": 4, "text": "a"}, {"start": 1, "end": 2, "text": "b"}]), "unsorted"),
        (lambda p: p["tracks"].update(mic=[{"start": 0, "end": 1, "text": "x" * 4001}]), "text too long"),
        (lambda p: p["tracks"].update(mic=[{"start": 0, "end": 1, "text": 5}]), "text not a string"),
        (lambda p: p["tracks"].update(mic=[{"start": "0", "end": 1, "text": "x"}]), "time not a number"),
        (lambda p: p["tracks"].update(mic="nope"), "not a list"),
        (lambda p: p.update(speaker_turns=[{"start": 0, "end": 1, "speaker": "A"}]), "turns not requested"),
    ],
)
def test_invalid_results_are_422_and_leave_the_lease_active(tmp_path, monkeypatch, mutate, why):
    rig = Rig(tmp_path, monkeypatch)
    job_id = rig.offered_job()
    lease = rig.claim()
    payload = rig.good_result(lease)
    mutate(payload)
    assert _complete(rig, job_id, payload).status_code == 422, why
    assert rig.job(job_id)["compute"]["status"] == "leased"
    assert _complete(rig, job_id, rig.good_result(lease)).status_code == 200  # a corrected result is still accepted


def test_non_finite_numbers_and_segment_cap_are_rejected(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    job_id = rig.offered_job()
    lease = rig.claim()
    raw = json.dumps(rig.good_result(lease)).replace('"start": 0.0, "end": 1.0, "text": "gpu mic"', '"start": NaN, "end": 1.0, "text": "gpu mic"')
    resp = rig.client.post(f"/v1/compute/jobs/{job_id}/complete", content=raw, headers={**rig.helper_headers, "Content-Type": "application/json"})
    assert resp.status_code == 422
    payload = rig.good_result(lease)
    payload["tracks"]["mic"] = [{"start": 0.0, "end": 0.0, "text": "x"}] * (compute_mod.MAX_SEGMENTS_PER_TRACK + 1)
    assert _complete(rig, job_id, payload).status_code == 422


def test_bad_speaker_turns_are_rejected(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, diarization_enabled=True)
    job_id = rig.offered_job()
    lease = rig.claim()
    for turns in ([{"start": 0, "end": 99, "speaker": "A"}], [{"start": 0, "end": 1, "speaker": ""}], "x"):
        payload = rig.good_result(lease)
        payload["speaker_turns"] = turns
        assert _complete(rig, job_id, payload).status_code == 422
    payload = rig.good_result(lease)
    payload["speaker_turns"] = [{"start": 0, "end": 1, "speaker": "A"}]
    assert _complete(rig, job_id, payload).status_code == 200


def test_oversized_and_malformed_bodies(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    job_id = rig.offered_job()
    lease = rig.claim()
    monkeypatch.setattr(compute_api, "MAX_BODY_BYTES", 200)
    big = _complete(rig, job_id, rig.good_result(lease, padding="x" * 500))
    assert big.status_code == 413
    monkeypatch.undo()
    notjson = rig.client.post(f"/v1/compute/jobs/{job_id}/complete", content=b"{oops", headers={**rig.helper_headers, "Content-Type": "application/json"})
    assert notjson.status_code == 400
    assert rig.job(job_id)["compute"]["status"] == "leased"


# -- health -------------------------------------------------------------------


def test_health_reports_compute_without_losing_existing_fields(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    body = rig.client.get(wire.HEALTH).json()
    assert body["status"] == "ok" and "protocol" in body and "diarization_enabled" in body
    assert body["compute"] == {"offload_enabled": True, "helpers_online": 0}
    rig.poll()
    assert rig.client.get(wire.HEALTH).json()["compute"]["helpers_online"] == 1
    rig.clock.advance(46)
    assert rig.client.get(wire.HEALTH).json()["compute"]["helpers_online"] == 0


def test_session_detail_never_exposes_lease_secrets(tmp_path):
    from meeting_notes.server import store as store_mod

    job = {"job_id": "j1", "state": "running", "compute": {"status": "leased", "attempt": "a1", "lease_hash": "h",
                                                          "helper_id": "x", "settings": {}, "helper_name": "PC"}}
    public = store_mod._public_job(job)
    assert public["compute"] == {"status": "leased", "helper_name": "PC"}
    assert job["compute"]["attempt"] == "a1"  # the stored record is untouched
