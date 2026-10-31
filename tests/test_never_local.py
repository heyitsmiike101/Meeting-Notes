"""No maximum on "Wait for a helper", the sweeper cap that follows it, and "Never transcribe on this server"
(``compute_never_local``): every job waits for a GPU helper with no time limit and nothing falls back to the CPU."""

from __future__ import annotations

import re
import time

import pytest

from meeting_notes import wire
from meeting_notes.server import compute as compute_mod
from meeting_notes.server import queue_view, retention, web
from meeting_notes.server import settings as settings_mod
from meeting_notes.server import store as store_mod
from meeting_notes.server.app import create_app
from tests.compute_helpers import Rig
from tests.test_compute_jobs import _fields
from tests.test_find_speakers import Diarizer, TwoSegments, TURNS, complete_diarize_only, speakers_jobs
from tests.test_in_person_speakers import upload_and_finalize

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated:DeprecationWarning"
)

DAY = 24 * 3600.0


def never_local_rig(tmp_path, monkeypatch, **settings):
    settings.setdefault("compute_never_local", True)
    settings.setdefault("diarization_enabled", True)
    return Rig(tmp_path, monkeypatch, transcriber=TwoSegments(), **settings)


def idle_forever(rig, seconds, step=300.0):
    """Let time pass with a busy-but-online helper polling, sweeping as the server does."""
    waited = 0.0
    while waited < seconds:
        rig.clock.advance(step)
        waited += step
        rig.poll(state="busy")
        rig.registry.sweep()


# -- no maximum on the wait ------------------------------------------------------------------------------------------


def test_claim_timeout_has_a_minimum_but_no_maximum(tmp_path):
    assert settings_mod.validate(_fields(compute_claim_timeout_seconds=100000)).compute_claim_timeout_seconds == 100000
    with pytest.raises(settings_mod.ValidationError):
        settings_mod.validate(_fields(compute_claim_timeout_seconds=4))
    settings_mod.save_settings(tmp_path, settings_mod.Settings(compute_claim_timeout_seconds=86400))
    assert settings_mod.load_settings(tmp_path).compute_claim_timeout_seconds == 86400
    assert not hasattr(settings_mod, "COMPUTE_CLAIM_TIMEOUT_RANGE")


def test_pages_have_no_maximum_on_the_wait():
    page = web.render_settings_page(settings_mod.Settings(), token_configured=True)
    assert re.search(r'id="gpu-claim-timeout" min="5" value="30"', page) and "300 seconds" not in page
    queue = web.render_queue_page(settings_mod.Settings(), token_configured=True)
    assert 'id="q-claim" min="5" value="30"' in queue and "no upper limit" in queue


def test_the_fifteen_minute_cap_gives_way_to_a_longer_claim_timeout(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, compute_claim_timeout_seconds=3600)
    assert rig.poll(state="busy").status_code == 204
    job_id = rig.new_job("long")
    rig.wait_compute(job_id, "offered")
    idle_forever(rig, compute_mod.OFFER_MAX_WAIT_SECONDS + 600)  # past the old cap, inside the claim timeout
    assert rig.job(job_id)["compute"]["status"] == "offered"
    idle_forever(rig, 3600)
    assert rig.wait_state(job_id)["compute"]["fallback_reason"] == "waited too long for a helper"


# -- never run on this server ----------------------------------------------------------------------------------------


def test_with_no_helper_online_the_job_waits_and_keeps_its_audio(tmp_path, monkeypatch):
    rig = never_local_rig(tmp_path, monkeypatch, audio_retention_days=0)
    job_id = rig.new_job("n1")  # no helper has ever polled
    job = rig.wait_compute(job_id, "offered")
    assert job["state"] == wire.JobState.RUNNING and job["compute"]["wait_forever"] is True
    rig.clock.advance(10 * DAY)
    rig.registry.sweep()
    assert rig.job(job_id)["compute"]["status"] == "offered" and rig.transcriber.calls == []
    # retention must not delete the audio of a transcription that has not run yet
    assert retention.apply_retention(rig.store, settings_mod.load_settings(rig.store.root)) == []
    assert rig.store.session_index_row("n1")["has_audio"]
    # a helper that shows up much later gets it
    lease = rig.claim()
    assert lease["job_id"] == job_id and "transcribe" not in lease
    done = rig.client.post(f"/v1/compute/jobs/{job_id}/complete", json=rig.good_result(lease), headers=rig.helper_headers)
    assert done.status_code == 200, done.text
    assert rig.wait_state(job_id)["compute"]["status"] == "done" and rig.transcriber.calls == []


def test_with_gpu_helpers_off_jobs_wait_and_the_queue_says_so(tmp_path, monkeypatch):
    rig = never_local_rig(tmp_path, monkeypatch, offload=False)
    job_id = rig.new_job("n2")
    rig.wait_compute(job_id, "offered")
    assert rig.poll().status_code == 204  # a helper polling is not given the job while offload is off
    assert rig.transcriber.calls == []
    queue = queue_view.build_queue(rig.store, settings=settings_mod.load_settings(rig.store.root))
    (row,) = [r for r in queue["waiting"] if r["id"] == job_id]
    assert row["reason"].startswith("Waiting for a GPU helper (no time limit)") and "switched off" in row["reason"]
    assert queue["settings"]["compute_never_local"] is True
    rig.configure(compute_offload_enabled=True)
    assert rig.claim()["job_id"] == job_id


def test_a_failing_helper_gets_the_job_offered_again_never_the_cpu(tmp_path, monkeypatch):
    rig = never_local_rig(tmp_path, monkeypatch)
    rig.poll()
    job_id = rig.new_job("n3")
    rig.wait_compute(job_id, "offered")
    for attempt in range(1, compute_mod.MAX_WAITING_FAILURES):
        lease = rig.claim()
        resp = rig.client.post(f"/v1/compute/jobs/{job_id}/release", json=rig.body(lease, error="CUDA out of memory"),
                               headers=rig.helper_headers)
        assert resp.status_code == 200, resp.text
        job = rig.job(job_id)
        assert job["compute"]["status"] == "offered" and job["compute"]["failures"] == attempt
        assert job["state"] == wire.JobState.RUNNING
    # heartbeat loss counts the same way; the last allowed failure gives the job up in error, still not run here
    lease = rig.claim()
    rig.clock.advance(compute_mod.HEARTBEAT_TIMEOUT_SECONDS + 5)
    rig.registry.sweep()
    job = rig.job(job_id)
    assert job["state"] == wire.JobState.ERROR and "attempts" in job["error"]
    assert rig.transcriber.calls == []


def test_a_restart_offers_the_job_again_never_runs_it_here(tmp_path, monkeypatch):
    rig = never_local_rig(tmp_path, monkeypatch)
    rig.poll()
    job_id = rig.new_job("n4")
    rig.wait_compute(job_id, "offered")
    rig.claim()  # leased to the helper when the server goes down
    rig.queue.stop()
    calls = []

    class Local:
        def transcribe(self, wav_path, track):
            calls.append(track)
            return []

    app2 = create_app(transcriber_factory=lambda **_kw: Local(), data_root=str(tmp_path / "data"))
    store2 = app2.state.store
    deadline = time.monotonic() + 5
    job = {}
    while time.monotonic() < deadline:
        job = store2.read_job(job_id)
        if (job.get("compute") or {}).get("status") == "offered":
            break
        time.sleep(0.02)
    assert job["compute"]["status"] == "offered" and job["compute"]["wait_forever"] and job["resumed_after_restart"]
    time.sleep(0.3)
    assert calls == [] and store2.read_job(job_id)["state"] == wire.JobState.RUNNING
    app2.state.job_queue.stop()


def test_in_person_meetings_wait_for_a_helper_too(tmp_path, monkeypatch):
    rig = never_local_rig(tmp_path, monkeypatch, in_person_helper_wait_minutes=5)
    job_id = upload_and_finalize(rig.client, "ip", seconds=10.0)
    job = rig.wait_compute(job_id, "offered")
    assert job["compute"]["wait_forever"] and job["compute"]["in_person"] and "max_wait" not in job["compute"]
    rig.clock.advance(3 * 3600)  # far past the five minutes
    rig.registry.sweep()
    assert rig.job(job_id)["compute"]["status"] == "offered" and rig.transcriber.calls == []
    assert rig.queue.speakers.pending() == 0


def test_find_speakers_waits_for_a_helper_and_is_never_run_here(tmp_path, monkeypatch):
    diar = Diarizer()
    rig = never_local_rig(tmp_path, monkeypatch)
    rig.queue.diarizer_factory = lambda: diar
    rig.poll()
    job_id = rig.new_job("n5")
    rig.wait_compute(job_id, "offered")
    lease = rig.claim()
    done = rig.client.post(f"/v1/compute/jobs/{job_id}/complete", json=rig.good_result(lease), headers=rig.helper_headers)
    assert done.status_code == 200, done.text
    rig.wait_state(job_id)
    resp = rig.client.post("/v1/sessions/n5/find-speakers")
    assert resp.status_code == 200, resp.text
    speakers_id = resp.json()["job_id"]
    job = rig.wait_compute(speakers_id, "offered")
    assert job["compute"]["wait_forever"] and job["compute"]["diarize_only"]
    rig.clock.advance(5 * DAY)
    rig.registry.sweep()
    assert rig.job(speakers_id)["compute"]["status"] == "offered" and diar.calls == 0
    assert rig.queue.speakers.pending() == 0
    lease = rig.claim()
    assert lease["transcribe"] is False
    assert complete_diarize_only(rig, lease).status_code == 200
    assert rig.wait_state(speakers_id)["state"] == wire.JobState.DONE and diar.calls == 0
    assert len(speakers_jobs(rig, "n5")) == 1 and TURNS


def test_the_setting_is_off_by_default_and_changes_nothing(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, offload=False)
    assert settings_mod.Settings().compute_never_local is False
    job = rig.wait_state(rig.new_job("off"))
    assert "compute" not in job and rig.transcriber.calls  # ran on this server as before


# -- settings and pages ------------------------------------------------------------------------------------------------


def test_setting_round_trips_through_load_validate_and_the_api(tmp_path, monkeypatch):
    settings_mod.save_settings(tmp_path, settings_mod.Settings(compute_never_local=True))
    assert settings_mod.load_settings(tmp_path).compute_never_local is True
    assert settings_mod.validate(_fields(compute_never_local="on")).compute_never_local is True
    assert settings_mod.validate(_fields(compute_never_local="0")).compute_never_local is False
    rig = Rig(tmp_path / "rig", monkeypatch, compute_never_local=True)
    payload = settings_mod.load_settings(rig.store.root).to_dict()
    payload.pop("compute_never_local")  # a caller that predates the setting must not reset it
    body = rig.client.put("/v1/settings", json=payload).json()
    assert body["compute_never_local"] is True
    off = rig.client.put("/v1/settings", json={**payload, "revision": body["revision"], "compute_never_local": False})
    assert off.json()["compute_never_local"] is False


def _tag(page, element_id):
    return re.search(r'<[^>]*id="%s"[^>]*>' % element_id, page).group(0)


def test_pages_warn_when_never_local_is_on_with_gpu_helpers_off():
    cases = [(True, False, True), (True, True, False), (False, False, False)]
    for never, offload, shown in cases:
        s = settings_mod.Settings(compute_never_local=never, compute_offload_enabled=offload)
        for page, warn in ((web.render_settings_page(s, token_configured=True), "gpu-nl-warning"),
                           (web.render_queue_page(s, token_configured=True), "q-nl-warning")):
            assert ("hidden" not in _tag(page, warn)) is shown, (never, offload, warn)
            assert "Never transcribe on this server" in page
            note = re.search(r'<p class="help nl-note"[^>]*>', page).group(0)
            assert ("hidden" not in note) is never
    page = web.render_settings_page(settings_mod.Settings(compute_never_local=True), token_configured=True)
    assert 'name="compute_never_local"' in page and re.search(r'id="gpu-never-local"[^>]*checked', page)
    queue = web.render_queue_page(settings_mod.Settings(compute_never_local=True), token_configured=True)
    assert re.search(r'id="q-never-local" checked', queue)
