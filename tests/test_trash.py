"""Recently deleted: soft delete, restore, purge, re-upload, UI markup."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from meeting_notes import wire
from meeting_notes.server import retention, web
from meeting_notes.server import settings as settings_mod
from meeting_notes.server import store as store_mod
from meeting_notes.server.app import create_app
from meeting_notes.server.store import TRASH_RETENTION_DAYS, Store
from tests.agent_helpers import WEB_TOKEN, bearer, make_agent_app, seg, _add_meeting
from tests.test_web import StubTranscriber, _upload_finalize_and_wait, wait_for_job_state

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated:DeprecationWarning"
)

DAY = 86400.0
WEB = {"Authorization": f"Bearer {WEB_TOKEN}"}


def seed_meeting(store: Store, sid="m-1", name="Design sync", *, audio=True, notes=True, created="2026-09-01T15:00:00Z"):
    """A meeting with metadata, audio, a done job + transcript and (optionally) a finished review."""
    segments = [seg(0, 4, "You", "Let's talk about the roadmap."), seg(5, 9, "Them", "Budget is tight.", "system")]
    notes_payload = {"title": name, "summary": "Roadmap talk.", "meeting_notes": "Body.", "participants": [], "key_points": [],
                     "decisions": [], "action_items": [], "open_questions": [], "risks": [], "next_steps": []}
    _add_meeting(store, sid, name, created, 1200, segments, notes_payload if notes else None)
    if audio:
        wav = store.track_wav_path(sid, "mic")
        wav.parent.mkdir(parents=True, exist_ok=True)
        wav.write_bytes(b"RIFF" + b"\x00" * 2000)
        store.write_session_meta(sid, store.read_session_meta(sid))  # refresh has_audio in the index


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "data"), str(tmp_path / "media"))


# -- soft delete keeps everything ------------------------------------------------


def test_trash_keeps_every_file_and_hides_it_from_the_index(store):
    seed_meeting(store)
    job_id = store.jobs_for_session("m-1")[0]["job_id"]
    review_id = store.list_reviews(session_id="m-1")[0]["review_id"]

    record = store.trash_session("m-1", source="bulk")
    assert record["deleted_via"] == "bulk" and record["has_audio"] is True and record["size_bytes"] > 0

    # Live locations are empty ...
    assert not store.session_dir("m-1").exists()
    assert not store.media_session_dir("m-1").exists()
    assert store.read_job(job_id) is None and store.read_review(review_id) is None
    assert store.session_index_row("m-1") is None
    assert store.session_detail("m-1") is None
    assert store.list_sessions()["total"] == 0 and store.list_sessions()["audio_total"] == 0
    assert store.list_sessions(q="roadmap")["total"] == 0
    assert store.list_jobs() == [] and store.list_reviews() == []
    # ... and every piece is preserved in the trash folders.
    entry = store.trash_dir / "m-1"
    assert (entry / "session" / "session.json").is_file()
    assert (entry / "jobs" / f"{job_id}.json").is_file()
    assert (entry / "jobs" / f"{job_id}.transcript.json").is_file()
    assert (entry / "reviews" / f"{review_id}.json").is_file()
    assert (entry / "trash.json").is_file()
    assert (store.media_trash_dir / "m-1" / "mic.wav").is_file()
    assert store.is_trashed("m-1")


def test_trash_unknown_session_returns_none(store):
    assert store.trash_session("nope") is None


def test_trash_marks_pending_jobs_as_interrupted(store):
    seed_meeting(store, notes=False)
    queued = store.create_job("m-1")
    store.trash_session("m-1")
    store.restore_session("m-1")
    assert store.read_job(queued)["state"] == wire.JobState.ERROR
    assert "deleted" in store.read_job(queued)["error"]


def test_update_job_never_resurrects_a_moved_job(store):
    seed_meeting(store, notes=False)
    job_id = store.jobs_for_session("m-1")[0]["job_id"]
    store.trash_session("m-1")
    store.update_job(job_id, state=wire.JobState.RUNNING)
    store.write_transcript(job_id, "x", "{}")
    assert not store.job_path(job_id).exists()
    assert not store.job_transcript_path(job_id).exists()


# -- restore ---------------------------------------------------------------------


def test_restore_round_trip_puts_everything_back_and_reindexes(store):
    seed_meeting(store)
    before_meta = store.read_session_meta("m-1")
    before_job = store.jobs_for_session("m-1")[0]
    before_review = store.list_reviews(session_id="m-1")[0]
    before_row = store.session_index_row("m-1")
    assert before_row["has_audio"]

    store.trash_session("m-1")
    store.restore_session("m-1")

    assert store.read_session_meta("m-1") == before_meta
    assert store.jobs_for_session("m-1") == [before_job]
    assert store.list_reviews(session_id="m-1") == [before_review]
    assert store.track_wav_path("m-1", "mic").read_bytes().startswith(b"RIFF")
    row = store.session_index_row("m-1")
    assert row["has_audio"] and row["latest_state"] == wire.JobState.DONE
    assert row["review"]["status"] == "done"
    assert store.list_sessions(q="roadmap")["total"] == 1
    assert store.list_sessions()["audio_total"] == 1
    assert not store.is_trashed("m-1") and store.list_trash() == []
    assert not (store.trash_dir / "m-1").exists() and not (store.media_trash_dir / "m-1").exists()


def test_restore_unknown_and_conflict(store):
    assert store.restore_session("nope") is None
    seed_meeting(store)
    store.trash_session("m-1")
    store.write_session_meta("m-1", {"name": "fresh"})  # the id is live again
    with pytest.raises(store_mod.TrashConflict):
        store.restore_session("m-1")
    assert store.is_trashed("m-1")  # nothing was lost


def test_trash_survives_a_reindex_and_restart(tmp_path):
    root, media = str(tmp_path / "d"), str(tmp_path / "m")
    first = Store(root, media)
    seed_meeting(first)
    first.trash_session("m-1")
    second = Store(root, media)
    second.reindex()
    assert second.list_sessions()["total"] == 0
    assert [i["session_id"] for i in second.list_trash()] == ["m-1"]
    second.restore_session("m-1")
    assert second.list_sessions()["total"] == 1


# -- permanent delete, purge, empty ----------------------------------------------


def test_purge_trashed_removes_everything(store):
    seed_meeting(store)
    store.trash_session("m-1")
    assert store.purge_trashed("m-1") is True
    assert not (store.trash_dir / "m-1").exists() and not (store.media_trash_dir / "m-1").exists()
    assert store.list_trash() == []
    assert store.purge_trashed("m-1") is False
    assert store.restore_session("m-1") is None


def test_auto_purge_after_thirty_days_with_injected_clock(store, caplog):
    assert TRASH_RETENTION_DAYS == 30
    seed_meeting(store, "old", "Old one")
    seed_meeting(store, "new", "New one")
    t0 = 1_800_000_000.0
    store.trash_session("old", now=t0)
    store.trash_session("new", now=t0 + 10 * DAY)

    listing = {i["session_id"]: i for i in store.list_trash(now=t0 + 25 * DAY)}
    assert listing["old"]["days_left"] == 5 and listing["new"]["days_left"] == 15
    assert store.purge_expired_trash(now=t0 + 29.9 * DAY) == []

    with caplog.at_level("INFO"):
        purged = retention.purge_trash(store, now=t0 + 30 * DAY)
    assert purged == ["old"]
    assert any("Old one" in r.getMessage() or "purged 1" in r.getMessage() for r in caplog.records)
    assert [i["session_id"] for i in store.list_trash(now=t0 + 30 * DAY)] == ["new"]
    assert not (store.media_trash_dir / "old").exists()
    assert store.purge_expired_trash(now=t0 + 40 * DAY) == ["new"]


def test_retention_worker_sweep_purges_expired_trash(store):
    seed_meeting(store)
    store.trash_session("m-1", now=time.time() - 31 * DAY)
    retention.RetentionWorker(store, interval_minutes=60)._sweep_once()
    assert store.list_trash() == []


def test_empty_trash(store):
    seed_meeting(store, "a", "A")
    seed_meeting(store, "b", "B")
    store.trash_session("a")
    store.trash_session("b")
    assert store.empty_trash() == 2
    assert store.list_trash() == []
    assert store.empty_trash() == 0


# -- other consumers never see trashed meetings ----------------------------------


def test_retention_never_touches_trashed_audio(store):
    seed_meeting(store)
    store.trash_session("m-1")
    settings = settings_mod.Settings(model="base.en", audio_retention_days=0, delete_audio_only_after_success=False)
    assert retention.apply_retention(store, settings) == []
    assert retention.apply_retention(store, settings, session_ids=["m-1"]) == []
    assert (store.media_trash_dir / "m-1" / "mic.wav").is_file()


def test_review_claim_queue_skips_trashed_meetings(store):
    seed_meeting(store, notes=False)
    review = store.create_review("m-1")
    store.trash_session("m-1")
    assert store.claim_next_review() is None
    store.restore_session("m-1")
    claimed = store.claim_next_review()
    assert claimed and claimed["review_id"] == review["review_id"]


def test_agent_api_and_search_exclude_trashed(tmp_path, monkeypatch):
    app, _ = make_agent_app(tmp_path, monkeypatch, with_mcp=False)
    client = TestClient(app)
    key = bearer(app.state.agent_keys.create("reader")["key"])
    assert client.delete("/v1/sessions/m-plan", headers=WEB).status_code == 200

    ids = [m["id"] for m in client.get("/api/v1/meetings", headers=key).json()["items"]]
    assert "m-plan" not in ids and "m-budget" in ids
    assert client.get("/api/v1/meetings/m-plan", headers=key).status_code == 404
    assert client.get("/api/v1/meetings/m-plan/transcript", headers=key).status_code == 404
    hits = client.get("/api/v1/search?q=beta", headers=key).json()
    assert "m-plan" not in str(hits)
    assert "Ship the beta on Monday" not in str(client.get("/api/v1/decisions", headers=key).json())
    # Agent keys have no access to the trash at all.
    assert client.get("/v1/trash", headers=key).status_code in (401, 403)
    assert client.post("/v1/trash/m-plan/restore", headers=key).status_code in (401, 403)
    assert client.delete("/v1/trash/m-plan", headers=key).status_code in (401, 403)
    assert client.post("/v1/trash/empty", headers=key).status_code in (401, 403)
    assert [i["session_id"] for i in client.get("/v1/trash", headers=WEB).json()["items"]] == ["m-plan"]


# -- HTTP API --------------------------------------------------------------------


@pytest.fixture
def client_and_store(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = create_app(data_root=str(tmp_path / "data"), media_root=str(tmp_path / "media"), enable_mcp=False)
    seed_meeting(app.state.store, "m-1", "Design sync")
    seed_meeting(app.state.store, "m-2", "Budget review", created="2026-09-02T15:00:00Z")
    return TestClient(app), app.state.store


def test_delete_api_moves_to_trash_and_hides_meeting(client_and_store):
    client, store = client_and_store
    assert client.get("/v1/sessions").json()["total"] == 2
    resp = client.delete("/v1/sessions/m-1?via=bulk")
    assert resp.status_code == 200
    assert resp.json()["trashed"] is True and resp.json()["days_left"] == 30

    listing = client.get("/v1/sessions").json()
    assert listing["total"] == 1 and listing["audio_total"] == 1
    assert client.get("/v1/sessions?q=roadmap").json()["items"][0]["session_id"] == "m-2"
    detail = client.get("/v1/sessions/m-1")
    assert detail.status_code == 404 and "Recently deleted" in detail.json()["detail"]
    assert client.get("/v1/sessions/never-existed").json()["detail"] == "unknown session"
    assert client.delete("/v1/sessions/m-1").status_code == 404  # already in trash
    assert store.list_trash()[0]["deleted_via"] == "bulk"
    # Opening it in the web UI sends you to Recently deleted, not an endless spinner.
    page = client.get("/sessions/m-1", follow_redirects=False)
    assert page.status_code == 303 and page.headers["location"] == "/meetings/trash"


def test_web_delete_route_is_a_soft_delete(client_and_store):
    client, store = client_and_store
    assert client.post("/sessions/m-1/delete", follow_redirects=False).status_code == 303
    assert store.is_trashed("m-1") and store.list_trash()[0]["deleted_via"] == "web"
    assert (store.media_trash_dir / "m-1" / "mic.wav").is_file()


def test_trash_list_shape(client_and_store):
    client, _ = client_and_store
    client.delete("/v1/sessions/m-1")
    body = client.get("/v1/trash").json()
    assert body["total"] == 1 and body["retention_days"] == 30
    item = body["items"][0]
    for key in ("session_id", "name", "created", "duration_sec", "deleted_at", "days_left", "has_audio", "size_bytes"):
        assert key in item
    assert item["name"] == "Design sync" and item["duration_sec"] == 1200
    assert item["days_left"] == 30 and item["has_audio"] is True and item["size_bytes"] > 0


def test_restore_endpoint_round_trip_and_errors(client_and_store):
    client, _ = client_and_store
    client.delete("/v1/sessions/m-1")
    assert client.post("/v1/trash/m-1/restore").json() == {"session_id": "m-1", "restored": True}
    assert client.get("/v1/sessions/m-1").status_code == 200
    assert client.get("/v1/sessions").json()["total"] == 2
    assert client.get("/v1/trash").json()["total"] == 0
    assert client.post("/v1/trash/m-1/restore").status_code == 404
    assert client.post("/v1/trash/bad%20id/restore").status_code in (400, 404)


def test_restore_conflict_is_409(client_and_store):
    client, store = client_and_store
    client.delete("/v1/sessions/m-1")
    store.write_session_meta("m-1", {"name": "impostor"})
    assert client.post("/v1/trash/m-1/restore").status_code == 409


def test_permanent_delete_and_empty_endpoints(client_and_store):
    client, store = client_and_store
    client.delete("/v1/sessions/m-1")
    client.delete("/v1/sessions/m-2")
    assert client.delete("/v1/trash/m-1").json()["purged"] is True
    assert client.delete("/v1/trash/m-1").status_code == 404
    assert client.get("/v1/trash").json()["total"] == 1
    assert client.post("/v1/trash/empty").json() == {"purged": 1}
    assert client.get("/v1/trash").json()["total"] == 0
    assert not list(store.media_trash_dir.glob("*"))


def test_trash_endpoints_require_the_web_token(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_NOTES_TOKEN", "sekret")
    app = create_app(data_root=str(tmp_path / "data"), enable_mcp=False)
    seed_meeting(app.state.store)
    c = TestClient(app)
    app.state.store.trash_session("m-1")
    for method, path in (("get", "/v1/trash"), ("post", "/v1/trash/m-1/restore"),
                         ("delete", "/v1/trash/m-1"), ("post", "/v1/trash/empty"),
                         ("delete", "/v1/sessions/m-1")):
        assert getattr(c, method)(path).status_code == 401, path
    assert c.get("/meetings/trash", follow_redirects=False).status_code == 303  # to /login
    assert c.get("/v1/trash", headers={"Authorization": "Bearer sekret"}).status_code == 200
    assert app.state.store.is_trashed("m-1")


# -- re-upload of a trashed id ----------------------------------------------------


def _client_with_stub(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    segments = {"mic": [seg_obj("Hello there")]}
    app = create_app(transcriber_factory=lambda **_kw: StubTranscriber(segments), data_root=str(tmp_path / "data"))
    return TestClient(app), app.state.store


def seg_obj(text):
    from meeting_notes.transcribe.protocol import Segment
    return Segment(start=0.0, end=1.0, text=text, track="mic")


def test_reupload_into_trashed_id_restores_it_first(tmp_path, monkeypatch):
    client, store = _client_with_stub(tmp_path, monkeypatch)
    first_job = _upload_finalize_and_wait(client, "sess-a")
    client.post("/v1/sessions/sess-a/review")
    assert client.delete("/v1/sessions/sess-a").status_code == 200
    assert store.is_trashed("sess-a")

    # The client re-uploads the meeting folder (track upload, then finalize).
    second_job = _upload_finalize_and_wait(client, "sess-a")
    assert not store.is_trashed("sess-a") and store.list_trash() == []
    jobs = {j["job_id"] for j in store.jobs_for_session("sess-a")}
    assert jobs == {first_job, second_job}  # history came back with it
    assert client.get("/v1/sessions/sess-a").status_code == 200
    assert store.list_reviews(session_id="sess-a")  # notes came back too


def test_pipeline_put_and_track_upload_also_restore(tmp_path, monkeypatch):
    client, store = _client_with_stub(tmp_path, monkeypatch)
    _upload_finalize_and_wait(client, "sess-a")
    client.delete("/v1/sessions/sess-a")
    resp = client.put(
        "/v1/sessions/sess-a/pipeline", json={"state": "uploading", "percent": 10, "bytes_received": 1, "bytes_total": 10}
    )
    assert resp.status_code == 200 and not store.is_trashed("sess-a")

    client.delete("/v1/sessions/sess-a")
    pcm = b"\x00\x00" * 160
    assert client.post(wire.track_upload_path("sess-a", "mic"), content=pcm).status_code == 200
    assert not store.is_trashed("sess-a") and store.session_dir("sess-a").exists()
    assert store.list_jobs()  # the original job is back, not orphaned in trash


def test_reupload_of_permanently_deleted_id_creates_it_fresh(tmp_path, monkeypatch):
    client, store = _client_with_stub(tmp_path, monkeypatch)
    first_job = _upload_finalize_and_wait(client, "sess-a")
    client.delete("/v1/sessions/sess-a")
    assert client.delete("/v1/trash/sess-a").status_code == 200

    second_job = _upload_finalize_and_wait(client, "sess-a")
    assert second_job != first_job
    assert [j["job_id"] for j in store.jobs_for_session("sess-a")] == [second_job]
    assert store.list_trash() == []


# -- UI markup ---------------------------------------------------------------------


def _meetings_page(tmp_path, monkeypatch) -> str:
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    return TestClient(create_app(data_root=str(tmp_path / "data"))).get("/meetings").text


def test_meetings_page_uses_a_dialog_that_lists_meetings_and_an_undo_toast(tmp_path, monkeypatch):
    page = _meetings_page(tmp_path, monkeypatch)
    assert '<dialog class="dialog" id="confirm-dialog"' in page
    assert "Move to Recently deleted" in page
    assert "moved to Recently deleted" in page and "label:'Undo'" in page
    assert "/v1/trash/'+encodeURIComponent(id)+'/restore" in page
    assert "30 days" in page
    # The old bare confirm() prompts are gone; the dialog lists name, date and length per meeting.
    assert "Delete the selected entries" not in page
    assert "confirm(" not in page.replace("window.confirm(", "").replace("confirmDialog(", "")
    assert "di-name" in page and "meetingSummary" in page and "meetingItems(ids)" in page
    # Delete audio also names the meetings.
    assert "deleteAudioDialog(ids)" in page and "deleteAudioDialog([id])" in page
    # Entry point to the trash view, and the menu item wording.
    assert 'href="/meetings/trash"' in page and "Recently deleted</span>" in page
    assert "Delete entire entry" not in page and "Delete meeting" in page


def test_trash_page_renders_with_empty_state_and_actions(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    client = TestClient(create_app(data_root=str(tmp_path / "data")))
    resp = client.get("/meetings/trash")
    assert resp.status_code == 200
    page = resp.text
    assert "<title>Meeting Notes | Recently deleted</title>" in page and "<h1>Recently deleted</h1>" in page
    assert "/v1/trash" in page and "Empty trash" in page and "data-restore" in page and "data-purge" in page
    assert "Nothing in Recently deleted" in page and "stay here for ' + RETENTION_DAYS + ' days" in page
    assert "var RETENTION_DAYS = 30;" in page
    assert "Delete permanently" in page and "Restore" in page
    assert '<dialog class="dialog" id="confirm-dialog"' in page


def test_stylesheet_defines_dialog_and_toast_action_tokens_in_all_themes():
    css = web.stylesheet_text()
    assert ".dialog::backdrop" in css and ".toast-action" in css and ".trash-row" in css
    assert css.count("--toast-action:") == 3 and css.count("--scrim:") == 3
