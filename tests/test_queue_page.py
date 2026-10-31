"""The Queue page: its data function (running, waiting, ordering, failures), ``GET /v1/queue`` and its auth, and the
page itself (nav item, the jobs / helpers / failures tables; no settings form)."""

from __future__ import annotations

import dataclasses
import re
import shutil
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from meeting_notes import wire
from meeting_notes.server import queue_view
from meeting_notes.server import settings as settings_mod
from meeting_notes.server import store as store_mod
from meeting_notes.server import web
from meeting_notes.server.app import create_app

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated:DeprecationWarning"
)

NOW = 100_000.0


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    return create_app(transcriber_factory=lambda **_kw: None, data_root=str(tmp_path / "data"))


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def store(app):
    return app.state.store


def session(store, sid, name=None, **meta):
    store.ensure_session_dir(sid)
    store.write_session_meta(sid, {"name": name or f"Meeting {sid}", "created": "2026-10-01", **meta})
    return sid


def job(store, sid, *, state=wire.JobState.QUEUED, kind=None, created=NOW - 60, **fields):
    job_id = store.create_job(sid, {}, kind=kind)
    store.update_job(job_id, state=state, created=created, **fields)
    return job_id


def offered(offered_at, **extra):
    return {"status": "offered", "offered_at": offered_at, "offer_expires": offered_at + 30, "claim_timeout": 30.0,
            "tracks": [{"name": "system", "seconds": 5.0}], **extra}


def build(store, **kwargs):
    return queue_view.build_queue(store, settings=settings_mod.load_settings(store.root), now=NOW, **kwargs)


def ids(rows):
    return [r["id"] for r in rows]


# -- running ---------------------------------------------------------------------------------------------------


def test_empty_queue(store):
    data = build(store)
    assert data["running"] == [] and data["waiting"] == [] and data["failures"] == []
    assert data["settings"]["compute_offload_enabled"] is False


def test_running_local_transcription(store):
    session(store, "a", "Weekly sync")
    j = job(store, "a", state=wire.JobState.RUNNING, progress=0.5, stage="transcribing", transcribe_progress=0.5,
            started_at=NOW - 125)
    (row,) = build(store)["running"]
    assert row["id"] == j and row["title"] == "Weekly sync" and row["session_id"] == "a"
    assert row["kind_label"] == "Transcribe" and row["where"] == "This server"
    assert row["step"] == "Transcribing" and row["percent"] == 50.0 and row["elapsed_seconds"] == 125


def test_running_job_on_a_helper_uses_the_lease(store):
    session(store, "a", "Call")
    job(store, "a", state=wire.JobState.RUNNING, kind=store_mod.JOB_KIND_SPEAKERS, progress=0.3, stage="speakers",
        speakers_progress=0.3, compute={"status": "leased", "helper_name": "Gaming PC", "claimed_at": NOW - 40})
    (row,) = build(store)["running"]
    assert row["kind_label"] == "Find speakers"
    assert row["where"] == "GPU helper Gaming PC" and row["step"] == "Labelling speakers"
    assert row["percent"] == 30.0 and row["elapsed_seconds"] == 40


def test_in_person_speakers_job_has_its_own_label(store):
    session(store, "p", "Hallway", in_person=True)
    job(store, "p", state=wire.JobState.RUNNING, kind=store_mod.JOB_KIND_SPEAKERS, started_at=NOW - 5)
    (row,) = build(store)["running"]
    assert row["kind_label"] == "Speaker labels (in person)"


def test_local_speakers_job_not_started_waits_on_the_speakers_worker(store):
    session(store, "a")
    started = job(store, "a", state=wire.JobState.RUNNING, kind=store_mod.JOB_KIND_SPEAKERS, started_at=NOW - 9)
    session(store, "b")
    behind = job(store, "b", state=wire.JobState.RUNNING, kind=store_mod.JOB_KIND_SPEAKERS)
    data = build(store)
    assert ids(data["running"]) == [started]
    assert ids(data["waiting"]) == [behind]
    assert "speaker-label worker" in data["waiting"][0]["reason"]


# -- waiting ---------------------------------------------------------------------------------------------------


def test_waiting_for_a_helper_orders_transcriptions_first_then_by_offer_time(store):
    for sid in "abcd":
        session(store, sid)
    late_text = job(store, "a", state=wire.JobState.RUNNING, created=NOW - 10, compute=offered(NOW - 10))
    speakers = job(store, "b", state=wire.JobState.RUNNING, kind=store_mod.JOB_KIND_SPEAKERS, created=NOW - 500,
                   compute=offered(NOW - 500, diarize_only=True, wait_forever=True))
    early_text = job(store, "c", state=wire.JobState.RUNNING, created=NOW - 90, compute=offered(NOW - 90))
    in_person = job(store, "d", state=wire.JobState.RUNNING, created=NOW - 50,
                    compute=offered(NOW - 50, in_person=True, max_wait=600.0))
    waiting = build(store)["waiting"]
    assert ids(waiting) == [early_text, in_person, late_text, speakers]
    assert [r["position"] for r in waiting] == [1, 2, 3, 4]
    assert all(r["lane"] == "helper" for r in waiting)
    by_id = {r["id"]: r for r in waiting}
    assert by_id[speakers]["wait_forever"] is True and "no time limit" in by_id[speakers]["reason"]
    assert "10 min" in by_id[in_person]["reason"]
    assert by_id[early_text]["waited_seconds"] == 90 and "Offered to GPU helpers" in by_id[early_text]["reason"]


def test_wait_for_helper_offers_explain_themselves(store):
    session(store, "a")
    job(store, "a", state=wire.JobState.RUNNING, compute=offered(NOW - 5, wait_for_helper=True))
    (row,) = build(store)["waiting"]
    assert row["wait_for_helper"] is True and "Waiting for a GPU helper" in row["reason"]


def test_queued_jobs_wait_for_this_server_in_creation_order(store):
    for sid in "abc":
        session(store, sid)
    second = job(store, "b", created=NOW - 20)
    first = job(store, "a", created=NOW - 80)
    third = job(store, "c", created=NOW - 5)
    waiting = build(store)["waiting"]
    assert ids(waiting) == [first, second, third]
    assert {r["lane"] for r in waiting} == {"server"} and [r["position"] for r in waiting] == [1, 2, 3]
    assert waiting[0]["reason"] == "Queued for this server's worker"


def test_queued_job_with_offload_on_says_a_helper_may_take_it(store):
    session(store, "a")
    job(store, "a")
    settings_mod.save_settings(
        store.root, dataclasses.replace(settings_mod.load_settings(store.root), compute_offload_enabled=True))
    (row,) = build(store)["waiting"]
    assert "GPU helper takes it" in row["reason"]


def make_done_transcript(store, sid):
    done = store.create_job(sid, {})
    store.update_job(done, state=wire.JobState.DONE, progress=1.0)
    store.write_transcript(done, "# t", '{"segments": []}')
    return done


def test_notes_queued_and_running(store):
    session(store, "a", "Planning")
    session(store, "b", "Retro")
    make_done_transcript(store, "a")
    make_done_transcript(store, "b")
    first = store.create_review("a", template={"id": "general", "name": "General"})
    second = store.create_review("b")
    claimed = store.claim_next_review()
    assert claimed["review_id"] == first["review_id"]
    data = build(store)
    (running,) = data["running"]
    assert running["id"] == first["review_id"] and running["kind_label"] == "Meeting notes"
    assert running["title"] == "Planning" and running["step"] == "Writing the notes"
    (waiting,) = data["waiting"]
    assert waiting["id"] == second["review_id"] and waiting["lane"] == "notes"
    assert waiting["reason"] == "Waiting for the notes worker"


def test_lanes_are_listed_helper_then_server_then_notes(store):
    for sid in "abc":
        session(store, sid)
    make_done_transcript(store, "c")
    store.create_review("c")
    queued = job(store, "a", created=NOW - 500)
    offered_id = job(store, "b", state=wire.JobState.RUNNING, created=NOW - 10, compute=offered(NOW - 10))
    waiting = build(store)["waiting"]
    assert [r["lane"] for r in waiting] == ["helper", "server", "notes"]
    assert waiting[0]["id"] == offered_id and waiting[1]["id"] == queued


def test_notes_wait_says_when_notes_are_off(store):
    session(store, "a")
    make_done_transcript(store, "a")
    store.create_review("a")
    settings_mod.save_settings(store.root, dataclasses.replace(settings_mod.load_settings(store.root), ai_provider="disabled"))
    (row,) = build(store)["waiting"]
    assert "turned off" in row["reason"]


def test_superseded_and_trashed_jobs_are_not_listed(store):
    session(store, "a")
    session(store, "b")
    job(store, "a", superseded=True)
    job(store, "b")
    store.trash_session("b")
    assert build(store)["waiting"] == []


# -- failures --------------------------------------------------------------------------------------------------


def test_recent_failures_exclude_superseded_and_old_ones(store):
    for sid in "abcd":
        session(store, sid)
    now = time.time()
    fresh = job(store, "a", state=wire.JobState.ERROR, error="GPU ran out of memory")
    job(store, "b", state=wire.JobState.ERROR, error=store_mod.JOB_SUPERSEDED_ERROR)
    flagged = job(store, "c", state=wire.JobState.ERROR, error="boom", superseded=True)
    old = job(store, "d", state=wire.JobState.ERROR, error="ancient")
    store.index.upsert_job(job_id=old, session_id="d", state="error", progress=1.0, error="ancient",
                           created=now - 90_000, updated=now - 90_000)
    data = queue_view.build_queue(store, settings=settings_mod.load_settings(store.root), now=now + 5)
    assert ids(data["failures"]) == [fresh]
    assert data["failures"][0]["error"] == "GPU ran out of memory" and flagged not in ids(data["failures"])
    assert data["running"] == [] and data["waiting"] == []


# -- API and page ----------------------------------------------------------------------------------------------


def test_api_returns_the_view_with_helpers(client, store):
    session(store, "a", "Standup")
    job(store, "a", state=wire.JobState.RUNNING, started_at=time.time() - 3)
    resp = client.post("/v1/compute/helpers", json={"name": "Desk PC", "instance_id": "inst-1"})
    assert resp.status_code == 201
    data = client.get("/v1/queue").json()
    assert [r["title"] for r in data["running"]] == ["Standup"]
    assert [h["name"] for h in data["helpers"]] == ["Desk PC"]
    assert {"waiting", "failures", "settings", "order_hint"} <= set(data)


def test_api_and_page_need_the_token(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_NOTES_TOKEN", "queue-token")
    client = TestClient(create_app(transcriber_factory=lambda **_kw: None, data_root=str(tmp_path / "data")))
    assert client.get("/v1/queue").status_code == 401
    assert client.get("/v1/queue", headers={"Authorization": "Bearer wrong"}).status_code == 403
    assert client.get("/v1/queue", headers={"Authorization": "Bearer queue-token"}).status_code == 200
    page = client.get("/queue", follow_redirects=False)
    assert page.status_code in (302, 303, 307) and "/login" in page.headers["location"]
    assert client.get("/queue", headers={"Authorization": "Bearer queue-token"}).status_code == 200


def test_page_has_nav_item_and_is_a_table_without_settings(client):
    html_text = client.get("/queue").text
    for tag in ("<title>Meeting Notes | Queue", 'href="/queue"', "<span>Queue</span>", 'aria-current="page"'):
        assert tag in html_text
    sidebar = html_text[html_text.index('<nav class="primary"'): html_text.index("</nav>", html_text.index('<nav class="primary"'))]
    assert sidebar.index('href="/meetings"') < sidebar.index('href="/queue"') < sidebar.index('href="/recorders"')
    # the settings form is gone: one line pointing to Settings > GPU helpers
    for control in ("q-offload", "q-claim", "q-without-helper", "q-inperson", "q-inperson-wait", "q-never-local", "<select"):
        assert control not in html_text, control
    assert "Queue settings are in" in html_text and 'href="/settings#gpu-helpers-heading"' in html_text
    # jobs, helpers and failures are tables (the existing .table-wrap pattern keeps phones from scrolling the page)
    assert html_text.count('class="table-wrap qtable"') == 3
    for column in ("Meeting", "Step", "Status", "Where", "Progress", "Time", "Name", "GPU", "State", "Current job", "Reason"):
        assert f'<th scope="col">{column}</th>' in html_text, column
    assert 'id="q-jobs"' in html_text and 'id="q-helpers"' in html_text and 'id="q-fail"' in html_text
    assert "Nothing is running or waiting." in html_text


def test_rows_carry_the_chart_wording_status_and_time(store):
    for sid in "abcd":
        session(store, sid)
    make_done_transcript(store, "d")
    running = job(store, "a", state=wire.JobState.RUNNING, started_at=NOW - 30, stage="transcribing")
    speakers = job(store, "b", state=wire.JobState.RUNNING, kind=store_mod.JOB_KIND_SPEAKERS, created=NOW - 50,
                   compute=offered(NOW - 50, diarize_only=True, wait_forever=True), notes_after_speakers=True)
    store.create_review("d")
    failed = job(store, "c", state=wire.JobState.ERROR, error="boom")
    data = build(store)
    (run_row,) = data["running"]
    assert (run_row["pipeline_step"], run_row["status"], run_row["status_label"]) == ("Transcribe", "running", "Running")
    assert run_row["since"] == NOW - 30 and run_row["id"] == running
    waiting = {r["id"]: r for r in data["waiting"]}
    wait_row = waiting[speakers]
    assert (wait_row["pipeline_step"], wait_row["status"], wait_row["where"]) == ("Identify speakers", "waiting", "GPU helper")
    assert wait_row["notes_follow"] is True and wait_row["since"] == NOW - 50
    notes_row = [r for r in data["waiting"] if r["kind"] == "notes"][0]
    assert notes_row["pipeline_step"] == "Generate notes" and notes_row["where"].startswith("AI notes")
    (fail_row,) = [r for r in data["failures"] if r["id"] == failed]
    assert fail_row["status"] == "failed" and fail_row["status_label"] == "Failed" and fail_row["pipeline_step"] == "Transcribe"
    # old fields stay (GET /v1/queue is backward compatible)
    assert {"kind", "kind_label", "where", "step", "percent", "elapsed_seconds"} <= set(run_row)


def test_settings_save_the_way_the_page_does(client, store):
    """The settings the Queue page used to edit still save through PUT /v1/settings (now from Settings > GPU helpers)."""
    cur = {**client.get("/v1/settings").json(), "model": "small.en"}  # a fresh data folder has no model yet
    body = {**cur, "compute_offload_enabled": True, "compute_claim_timeout_seconds": 45,
            "diarization_without_helper": "skip", "in_person_diarization": "off", "in_person_helper_wait_minutes": 3}
    resp = client.put("/v1/settings", json=body)
    assert resp.status_code == 200, resp.text
    saved = settings_mod.load_settings(store.root)
    assert (saved.compute_offload_enabled, saved.compute_claim_timeout_seconds, saved.diarization_without_helper,
            saved.in_person_diarization, saved.in_person_helper_wait_minutes) == (True, 45, "skip", "off", 3)
    now = resp.json()
    assert client.put("/v1/settings", json={**now, "compute_claim_timeout_seconds": 4}).status_code == 400
    assert client.put("/v1/settings", json={**now, "in_person_helper_wait_minutes": 61}).status_code == 400


def test_icon_exists_in_both_icon_tables():
    assert "queue" in web._ICON_PATHS
    assert '"queue"' in (web._STATIC_DIR / "icons.js").read_text(encoding="utf-8")


def test_page_script_parses_in_node(client, tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    scripts = re.findall(r"<script>(.*?)</script>", client.get("/queue").text, re.S)
    assert scripts
    for i, script in enumerate(scripts):
        path = tmp_path / f"queue-{i}.js"
        path.write_text(script, encoding="utf-8")
        result = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr[:400]
