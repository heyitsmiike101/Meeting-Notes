"""Job pipeline with GPU helper offload: choosing where a job runs, fallbacks, restart, settings, store."""

from __future__ import annotations

import pytest

from meeting_notes import wire
from meeting_notes.server import compute as compute_mod
from meeting_notes.server import settings as settings_mod
from meeting_notes.server.app import create_app
from meeting_notes.server.store import Store
from tests.compute_helpers import Rig

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated:DeprecationWarning"
)


# -- store.transition_job -----------------------------------------------------


def test_transition_job_is_compare_and_update_with_dotted_keys(tmp_path):
    store = Store(str(tmp_path / "data"))
    job_id = store.create_job("s1", {})
    store.update_job(job_id, compute={"status": "offered", "n": 1})

    assert store.transition_job(job_id, {"compute.status": "leased"}, progress=0.5) is None
    assert store.read_job(job_id)["progress"] == 0.0  # nothing written on a mismatch

    new = store.transition_job(job_id, {"compute.status": "offered", "state": "queued"}, **{"compute.status": "leased", "compute.attempt": "a"})
    assert new["compute"] == {"status": "leased", "n": 1, "attempt": "a"}
    assert store.read_job(job_id)["compute"]["status"] == "leased"
    # the old expectation no longer holds: a second claimer loses
    assert store.transition_job(job_id, {"compute.status": "offered"}, **{"compute.status": "leased"}) is None
    # an absent nested key never matches, and a missing job is None
    assert store.transition_job(job_id, {"compute.nope": None}, progress=1.0) is None
    assert store.transition_job("f" * 32, {}, progress=1.0) is None


# -- where a job runs ---------------------------------------------------------


def test_offload_off_by_default_runs_locally_with_no_compute_record(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, offload=False)
    assert rig.poll().status_code == 204  # a helper is online
    job_id = rig.new_job()
    job = rig.wait_state(job_id)
    assert job["state"] == wire.JobState.DONE
    assert "compute" not in job
    assert sorted(rig.transcriber.calls) == ["mic", "system"]


def test_offload_on_without_a_usable_helper_runs_locally(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    # no helper ever polled -> not online -> local
    job = rig.wait_state(rig.new_job("s0"))
    assert job["state"] == wire.JobState.DONE and "compute" not in job
    # a paused helper is not usable either
    assert rig.poll(paused=True).status_code == 204
    job = rig.wait_state(rig.new_job("s1"))
    assert job["state"] == wire.JobState.DONE and "compute" not in job
    assert len(rig.transcriber.calls) == 4


def test_a_busy_helper_still_gets_the_offer(tmp_path, monkeypatch):
    # A helper that is busy now will be free in a minute or two; that beats the server's CPU, so the job waits.
    rig = Rig(tmp_path, monkeypatch)
    assert rig.poll(state="busy").status_code == 204
    job_id = rig.new_job("s1")
    assert rig.wait_compute(job_id, "offered")["state"] == wire.JobState.RUNNING
    assert rig.transcriber.calls == []


def test_available_helper_gets_the_offer_and_the_worker_moves_on(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, compute_claim_timeout_seconds=40)
    job_id = rig.offered_job(seconds=10.0)
    job = rig.job(job_id)
    assert job["state"] == wire.JobState.RUNNING  # not blocked, not local
    c = job["compute"]
    assert c["status"] == "offered"
    assert c["offer_expires"] == c["offered_at"] + 40
    assert c["tracks"] == [{"name": "mic", "seconds": 10.0}, {"name": "system", "seconds": 10.0}]
    assert c["settings"]["whisper"]["beam_size"] >= 1
    assert rig.transcriber.calls == []


def test_several_meetings_at_once_queue_for_one_helper_in_order(tmp_path, monkeypatch):
    """Three meetings arrive together with one helper: each waits its turn on the GPU, none falls back to the
    CPU even though the claim timeout passes many times while the helper is busy."""
    rig = Rig(tmp_path, monkeypatch, compute_claim_timeout_seconds=30)
    assert rig.poll(state="busy").status_code == 204  # online, busy with something else
    jobs = [rig.new_job(f"s{i}") for i in range(3)]
    for job_id in jobs:
        rig.wait_compute(job_id, "offered")
    for job_id in jobs:
        # the helper stays busy for 4 claim timeouts, polling as it heartbeats
        for _ in range(4):
            rig.clock.advance(30)
            assert rig.poll(state="busy").status_code == 204
            rig.registry.sweep()
        lease = rig.claim()  # idle again: takes the oldest waiting offer
        assert lease["job_id"] == job_id
        done = rig.client.post(f"/v1/compute/jobs/{job_id}/complete", json=rig.good_result(lease), headers=rig.helper_headers)
        assert done.status_code == 200, done.text
        assert rig.wait_state(job_id)["compute"]["status"] == "done"
    assert rig.transcriber.calls == []  # nothing ran on the server


def test_an_offer_never_waits_past_the_maximum(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    assert rig.poll(state="busy").status_code == 204
    job_id = rig.new_job("s1")
    rig.wait_compute(job_id, "offered")
    waited = 0
    while waited <= compute_mod.OFFER_MAX_WAIT_SECONDS:
        rig.clock.advance(30)
        waited += 30
        assert rig.poll(state="busy").status_code == 204
        rig.registry.sweep()
    _assert_ran_here_once(rig, job_id, "waited too long for a helper")


# -- fallbacks ---------------------------------------------------------------


def _assert_ran_here_once(rig: Rig, job_id: str, reason: str):
    job = rig.wait_state(job_id)
    assert job["state"] == wire.JobState.DONE, job
    assert job["compute"]["status"] == "local"
    assert job["compute"]["fallback_reason"] == reason
    assert sorted(rig.transcriber.calls) == ["mic", "system"], "the local run must happen exactly once"
    assert rig.transcript(job_id)["segments"]


def test_unclaimed_offer_expires_to_the_local_queue_exactly_once(tmp_path, monkeypatch):
    # The helper polled once and then went silent (PC asleep): the offer falls back once the claim timeout has
    # passed with no usable helper online.
    rig = Rig(tmp_path, monkeypatch, compute_claim_timeout_seconds=30)
    job_id = rig.offered_job()
    rig.clock.advance(40)  # still counts as online (45 s): keeps waiting
    rig.registry.sweep()
    assert rig.job(job_id)["compute"]["status"] == "offered"
    rig.clock.advance(10)  # 50 s since its last poll: offline
    rig.registry.sweep()
    assert rig.job(job_id)["compute"]["status"] == "offered"  # only 10 s without a helper so far
    rig.clock.advance(25)  # 35 s without a usable helper
    rig.registry.sweep()
    rig.registry.sweep()
    _assert_ran_here_once(rig, job_id, "no claim")


def test_heartbeat_loss_revokes_the_attempt_and_runs_locally(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    job_id = rig.offered_job()
    lease = rig.claim()
    rig.clock.advance(59)
    rig.registry.sweep()
    assert rig.job(job_id)["compute"]["status"] == "leased"
    rig.clock.advance(2)
    rig.registry.sweep()
    _assert_ran_here_once(rig, job_id, "heartbeat lost")
    late = rig.client.post(f"/v1/compute/jobs/{job_id}/complete", json=rig.good_result(lease), headers=rig.helper_headers)
    assert late.status_code == 409


def test_lease_over_budget_runs_locally(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    job_id = rig.offered_job(seconds=40.0)  # 80 s of audio -> budget 120 + 20 = 140 s
    lease = rig.claim()
    assert lease["deadline_seconds"] == 140
    for _ in range(7):  # keep heartbeating so only the budget can expire the lease
        rig.clock.advance(20)
        assert rig.client.post(f"/v1/compute/jobs/{job_id}/heartbeat", json=rig.body(lease, progress=0.1), headers=rig.helper_headers).status_code == 200
    rig.clock.advance(20)  # 160 s since the claim, past the 140 s budget
    rig.registry.sweep()
    _assert_ran_here_once(rig, job_id, "over budget")


def test_registry_budget_formula():
    assert compute_mod.lease_budget_seconds(0) == 120
    assert compute_mod.lease_budget_seconds(1312.5) == 448.125


def test_restart_takes_offered_and_leased_jobs_back(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    offered = rig.offered_job("s1")
    rig.queue.stop()
    # a second server process over the same data root
    transcriber_calls = []

    class Local:
        def transcribe(self, wav_path, track):
            transcriber_calls.append(track)
            return []

    app2 = create_app(transcriber_factory=lambda **_kw: Local(), data_root=str(tmp_path / "data"))
    store2 = app2.state.store
    import time

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = store2.read_job(offered)
        if job["state"] == wire.JobState.DONE:
            break
        time.sleep(0.02)
    assert job["state"] == wire.JobState.DONE
    assert job["compute"]["status"] == "local" and job["compute"]["fallback_reason"] == "restart"
    assert job["compute"]["attempt"] is None and job["resumed_after_restart"] is True
    assert sorted(transcriber_calls) == ["mic", "system"]
    app2.state.job_queue.stop()


def test_ensure_job_does_not_duplicate_an_offered_job(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    job_id = rig.offered_job()
    assert rig.queue.ensure_job("s1") == job_id
    rig.claim()
    assert rig.queue.ensure_job("s1") == job_id


# -- diarization on the local path ---------------------------------------------


class _Diarizer:
    def __init__(self, fail=False):
        self.fail, self.calls = fail, 0

    def diarize(self, wav_path):
        self.calls += 1
        if self.fail:
            raise RuntimeError("boom")
        from meeting_notes.transcribe.diarize import SpeakerTurn

        return [SpeakerTurn(0.0, 1.0, "A")]


def test_diarize_failure_keeps_them_and_records_a_warning(tmp_path, monkeypatch):
    diar = _Diarizer(fail=True)
    rig = Rig(tmp_path, monkeypatch, offload=False, diarizer_factory=lambda: diar)
    job = rig.wait_state(rig.new_job())
    assert job["state"] == wire.JobState.DONE
    assert job["warnings"] == ["speaker labels failed: boom"]
    labels = [s["label"] for s in rig.transcript(job["job_id"])["segments"]]
    assert sorted(labels) == ["Them", "You"]


def test_diarizer_factory_failure_is_also_tolerated(tmp_path, monkeypatch):
    def broken():
        raise RuntimeError("no token")

    rig = Rig(tmp_path, monkeypatch, offload=False, diarizer_factory=broken)
    job = rig.wait_state(rig.new_job())
    assert job["state"] == wire.JobState.DONE
    assert job["warnings"] == ["speaker labels failed: no token"]


def test_diarization_without_helper_skip_never_diarizes_here(tmp_path, monkeypatch):
    diar = _Diarizer()
    rig = Rig(tmp_path, monkeypatch, offload=False, diarizer_factory=lambda: diar, diarization_without_helper="skip")
    job = rig.wait_state(rig.new_job())
    assert job["state"] == wire.JobState.DONE and diar.calls == 0 and "warnings" not in job
    rig.configure(diarization_without_helper="run_here")
    rig.wait_state(rig.new_job("s2"))
    assert diar.calls == 1


# -- deletion -----------------------------------------------------------------


def test_trashed_meeting_while_offered_fails_the_job_and_helper_gets_410(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    job_id = rig.offered_job()
    lease = rig.claim()
    rig.store.trash_session("s1")
    resp = rig.client.post(f"/v1/compute/jobs/{job_id}/heartbeat", json=rig.body(lease, progress=0.2), headers=rig.helper_headers)
    assert resp.status_code == 410
    assert rig.client.get(f"/v1/compute/jobs/{job_id}/audio/mic", headers=rig.lease_headers(lease)).status_code == 410


def test_session_directory_removed_while_leased_fails_the_job(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    job_id = rig.offered_job()
    lease = rig.claim()
    import shutil

    shutil.rmtree(rig.store.session_dir("s1"))
    resp = rig.client.post(f"/v1/compute/jobs/{job_id}/heartbeat", json=rig.body(lease, progress=0.2), headers=rig.helper_headers)
    assert resp.status_code == 410
    job = rig.job(job_id)
    assert job["state"] == wire.JobState.ERROR and job["error"] == "meeting deleted"


# -- settings -----------------------------------------------------------------


def test_new_settings_defaults_and_roundtrip(tmp_path):
    s = settings_mod.Settings()
    assert (s.compute_offload_enabled, s.compute_claim_timeout_seconds, s.diarization_without_helper) == (False, 30, "run_here")
    assert {"compute_offload_enabled", "compute_claim_timeout_seconds", "diarization_without_helper"} <= set(s.to_dict())
    settings_mod.save_settings(tmp_path, settings_mod.Settings(compute_offload_enabled=True, compute_claim_timeout_seconds=90, diarization_without_helper="skip"))
    loaded = settings_mod.load_settings(tmp_path)
    assert (loaded.compute_offload_enabled, loaded.compute_claim_timeout_seconds, loaded.diarization_without_helper) == (True, 90, "skip")


def test_load_settings_repairs_bad_values(tmp_path):
    (tmp_path / "settings.json").write_text('{"compute_claim_timeout_seconds": 9999, "diarization_without_helper": "x"}', encoding="utf-8")
    loaded = settings_mod.load_settings(tmp_path)
    assert loaded.compute_claim_timeout_seconds == 30 and loaded.diarization_without_helper == "run_here"


def _fields(**over):
    base = settings_mod.Settings(model="small.en").to_dict()
    base.update(over)
    return base


@pytest.mark.parametrize("value", [4, 301, "abc"])
def test_validate_rejects_bad_claim_timeout(value):
    with pytest.raises(settings_mod.ValidationError):
        settings_mod.validate(_fields(compute_claim_timeout_seconds=value))


def test_validate_rejects_bad_diarization_policy_and_accepts_good():
    with pytest.raises(settings_mod.ValidationError):
        settings_mod.validate(_fields(diarization_without_helper="maybe"))
    ok = settings_mod.validate(_fields(compute_offload_enabled="on", compute_claim_timeout_seconds="5", diarization_without_helper="skip"))
    assert ok.compute_offload_enabled is True and ok.compute_claim_timeout_seconds == 5 and ok.diarization_without_helper == "skip"


def test_put_settings_without_the_new_keys_keeps_stored_values(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, compute_claim_timeout_seconds=77, diarization_without_helper="skip")
    payload = settings_mod.load_settings(rig.store.root).to_dict()
    for key in ("compute_offload_enabled", "compute_claim_timeout_seconds", "diarization_without_helper"):
        payload.pop(key)
    resp = rig.client.put("/v1/settings", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["compute_offload_enabled"], body["compute_claim_timeout_seconds"], body["diarization_without_helper"]) == (True, 77, "skip")
    resp = rig.client.put("/v1/settings", json={**payload, "revision": body["revision"], "compute_offload_enabled": False, "compute_claim_timeout_seconds": 12})
    assert resp.json()["compute_offload_enabled"] is False and resp.json()["compute_claim_timeout_seconds"] == 12


def test_cuda_request_without_a_gpu_falls_back_to_cpu(monkeypatch, caplog):
    import sys
    import types

    from meeting_notes.transcribe import faster_whisper_backend as fwb

    fake = types.SimpleNamespace(get_cuda_device_count=lambda: 0, get_supported_compute_types=lambda d: {"float32"})
    monkeypatch.setitem(sys.modules, "ctranslate2", fake)
    with caplog.at_level("WARNING"):
        assert fwb._resolve_device("cuda") == "cpu"
    assert "no usable CUDA device" in caplog.text
    assert fwb._resolve_device("cpu") == "cpu"


def test_cuda_compute_type_follows_what_the_gpu_supports(monkeypatch):
    import sys
    import types

    from meeting_notes.transcribe import faster_whisper_backend as fwb

    pascal = types.SimpleNamespace(get_supported_compute_types=lambda d: {"float32", "int8", "int8_float32"})
    monkeypatch.setitem(sys.modules, "ctranslate2", pascal)
    assert fwb._resolve_compute_type("auto", "cuda") == "int8_float32"
    turing = types.SimpleNamespace(get_supported_compute_types=lambda d: {"float16", "int8_float16", "float32"})
    monkeypatch.setitem(sys.modules, "ctranslate2", turing)
    assert fwb._resolve_compute_type("auto", "cuda") == "float16"
    assert fwb._resolve_compute_type("auto", "cpu") == "int8"


def test_a_failed_finalize_runs_the_meeting_here_instead_of_losing_it(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    job_id = rig.offered_job()
    lease = rig.claim()
    real = rig.queue.finish_remote
    calls = []

    def flaky(*a, **k):
        calls.append(1)
        raise OSError("disk hiccup")

    monkeypatch.setattr(rig.queue, "finish_remote", flaky)
    resp = rig.client.post(f"/v1/compute/jobs/{job_id}/complete", json=rig.good_result(lease), headers=rig.helper_headers)
    assert resp.status_code == 200 and calls
    monkeypatch.setattr(rig.queue, "finish_remote", real)
    job = rig.wait_state(job_id)
    assert job["compute"]["status"] == "local" and "helper's result" in job["compute"]["fallback_reason"]
    assert sorted(rig.transcriber.calls) == ["mic", "system"]


def test_a_heartbeat_that_lands_during_the_sweep_keeps_the_lease(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    job_id = rig.offered_job()
    lease = rig.claim()
    stale = rig.job(job_id)  # the sweeper reads the job...
    rig.clock.advance(30)
    # ...a heartbeat lands before it acts on what it read
    assert rig.client.post(f"/v1/compute/jobs/{job_id}/heartbeat", json=rig.body(lease, progress=0.2),
                           headers=rig.helper_headers).status_code == 200
    took = rig.registry._to_local(
        job_id,
        {"compute.status": "leased", "compute.attempt": stale["compute"]["attempt"],
         "compute.last_heartbeat": stale["compute"]["last_heartbeat"]},
        "heartbeat lost")
    assert took is False and rig.job(job_id)["compute"]["status"] == "leased"


def test_offered_job_captures_gpu_model_else_the_server_model(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, gpu_model="base.en")  # model is small.en
    job_id = rig.offered_job("s1")
    assert rig.job(job_id)["compute"]["settings"]["whisper"]["model"] == "base.en"


def test_capture_falls_back_to_the_server_model_without_a_gpu_model():
    from types import SimpleNamespace
    from meeting_notes.server.jobs import JobQueue

    current = SimpleNamespace(model="small.en", beam_size=5, diarization_enabled=False, diarization_model="d",
                              diarization_min_speakers=None, diarization_max_speakers=None)
    prepared = SimpleNamespace(settings={"transcriber": {"hotwords": None}}, tracks={})
    queue = JobQueue.__new__(JobQueue)
    assert queue._capture_compute_settings(current, prepared)["whisper"]["model"] == "small.en"
    current.gpu_model = "base.en"
    assert queue._capture_compute_settings(current, prepared)["whisper"]["model"] == "base.en"
