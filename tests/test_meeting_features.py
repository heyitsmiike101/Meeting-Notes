"""Meetings page: notes-by-default, Generate button, bulk delete-audio, and
auto-generated notes for new meetings."""

from __future__ import annotations

import json
import wave

import pytest
from fastapi.testclient import TestClient

from meeting_notes import wire
from meeting_notes.server import settings as settings_mod
from meeting_notes.server import store as store_mod
from meeting_notes.server.app import create_app
from meeting_notes.server.jobs import JobQueue
from meeting_notes.server.web import render_transcriptions_page
from meeting_notes.transcribe.protocol import Segment

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated:DeprecationWarning"
)


def _app(tmp_path, monkeypatch, **settings):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = create_app(data_root=str(tmp_path / "data"))
    if settings:
        settings_mod.save_settings(
            app.state.store.root, settings_mod.Settings(model="base.en", **settings)
        )
    return app


def _finished_session(store: store_mod.Store, session_id: str = "s1") -> None:
    store.write_session_meta(session_id, {"name": "Planning", "created": "2026-09-20"})
    job_id = store.create_job(session_id)
    store.update_job(job_id, state=wire.JobState.DONE, progress=1.0)
    store.write_transcript(job_id, "# Planning", json.dumps({"segments": []}))


# -- 2: rename -------------------------------------------------------------


def test_meetings_route_and_legacy_alias_serve_the_same_page(tmp_path, monkeypatch):
    client = TestClient(_app(tmp_path, monkeypatch))
    new, old = client.get("/meetings"), client.get("/transcriptions")
    assert new.status_code == old.status_code == 200
    assert new.text == old.text
    assert "<title>Meeting Notes | Meetings" in new.text
    assert "<h1>Meetings</h1>" in new.text
    assert 'href="/meetings"' in new.text
    assert "Saved transcriptions" not in new.text
    assert "saved transcription" not in new.text.lower()
    assert 'aria-label="Select all visible meetings"' in new.text
    assert "No meetings found." in new.text


def test_session_deep_link_still_opens_the_meetings_page(tmp_path, monkeypatch):
    client = TestClient(_app(tmp_path, monkeypatch))
    _finished_session(client.app.state.store)
    resp = client.get("/sessions/s1")
    assert resp.status_code == 200
    assert 'openSession("s1", null)' in resp.text  # no notes yet -> transcript first


# -- 1: notes are the default view -----------------------------------------


def test_meeting_with_done_notes_opens_notes_first(tmp_path, monkeypatch):
    client = TestClient(_app(tmp_path, monkeypatch))
    store = client.app.state.store
    _finished_session(store)
    review = store.create_review("s1")
    store.claim_next_review()
    store.complete_review(review["review_id"], _notes())
    assert store.session_index_row("s1")["review"]["status"] == "done"
    resp = client.get("/sessions/s1")
    assert 'openSession("s1", "notes")' in resp.text


def _notes():
    return {
        "title": "Planning",
        "summary": "Planned.",
        "meeting_notes": "Planned the release.",
        "participants": [],
        "key_points": [],
        "decisions": [],
        "action_items": [],
        "open_questions": [],
        "risks": [],
        "next_steps": [],
    }


def test_notes_default_logic_is_in_the_page_script():
    page = render_transcriptions_page(token_configured=True)
    # List rows with finished notes pass a "notes" hint so the overlay never
    # flashes the transcript first...
    assert "row.classList.contains('notes-ready')?'notes':''" in page
    assert "setDetailView(hintView==='notes'?'notes':'transcript')" in page
    # ...and the detail response is the authority if the hint was stale.
    assert "pendingNotesDefault" in page
    # Transcript stays one click away.
    assert 'id="show-transcript"' in page
    page = render_transcriptions_page(
        token_configured=True, initial_session_id="abc", initial_view="notes"
    )
    assert 'openSession("abc", "notes")' in page


# -- 4: Generate button ----------------------------------------------------


def test_generate_button_only_when_ai_provider_enabled(tmp_path, monkeypatch):
    enabled = TestClient(_app(tmp_path / "a", monkeypatch, ai_provider="claude")).get("/meetings")
    assert "var aiEnabled = true;" in enabled.text
    assert "notes-generate" in enabled.text
    assert "/review'" in enabled.text
    disabled = TestClient(_app(tmp_path / "b", monkeypatch, ai_provider="disabled")).get("/meetings")
    assert "var aiEnabled = false;" in disabled.text


def test_generate_button_markup_is_a_real_accessible_button():
    page = render_transcriptions_page(token_configured=True, ai_enabled=True)
    assert '<button type="button" class="btn secondary sm notes-generate"' in page
    assert "aria-label=\"Generate meeting notes for" in page
    assert "e.stopPropagation();generateNotes(gen)" in page
    assert "btn.disabled = true" in page
    from meeting_notes.server.web import stylesheet_text

    css = stylesheet_text()
    assert ".mrow:focus-within .notes-generate" in css
    assert "@media (hover: none) { .notes-generate { opacity:1; } }" in css


# -- 5: bulk delete audio --------------------------------------------------


def test_bulk_delete_audio_button_and_handler_present():
    page = render_transcriptions_page(token_configured=True)
    assert 'id="bulk-delete-audio"' in page
    # Delete audio stays permanent, but is confirmed in a dialog that lists the meetings.
    assert "deleteAudioDialog(ids).then(function(ok){if(ok)runBulk('/delete-audio','POST');})" in page
    assert "Transcripts and notes are kept. This cannot be undone." in page
    assert page.count("'bulk-delete-audio'") >= 2  # enabled/disabled with the selection


def test_delete_audio_endpoint_is_idempotent_without_audio(tmp_path, monkeypatch):
    client = TestClient(_app(tmp_path, monkeypatch))
    _finished_session(client.app.state.store)
    for _ in range(2):
        resp = client.post("/v1/sessions/s1/delete-audio")
        assert resp.status_code == 200
        assert resp.json()["bytes_freed"] == 0
    assert client.post("/v1/sessions/missing/delete-audio").status_code == 404


# -- 3: every meeting gets notes (the old auto-generate setting is gone) ---------


def test_the_auto_generate_notes_setting_is_gone_but_a_stale_key_is_ignored(tmp_path, monkeypatch):
    assert not hasattr(settings_mod.Settings(), "auto_generate_notes")
    fields = {
        "model": "base.en",
        "beam_size": 5,
        "audio_retention_days": -1,
        "retention_check_interval_minutes": 60,
    }
    stale = settings_mod.validate({**fields, "auto_generate_notes": "on"})  # an old API caller: accepted, ignored
    assert "auto_generate_notes" not in stale.to_dict()
    settings_mod.save_settings(tmp_path, stale)
    assert "auto_generate_notes" not in (tmp_path / "settings.json").read_text(encoding="utf-8")


def test_an_old_settings_json_with_the_key_still_loads(tmp_path):
    (tmp_path / "settings.json").write_text(
        json.dumps({"model": "base.en", "ai_provider": "claude", "auto_generate_notes": False}), encoding="utf-8"
    )
    loaded = settings_mod.load_settings(tmp_path)
    assert loaded.ai_provider == "claude" and not hasattr(loaded, "auto_generate_notes")


def test_settings_form_has_no_auto_generate_checkbox_and_a_stale_post_succeeds(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    client = TestClient(app)
    page = client.get("/settings").text
    assert 'name="auto_generate_notes"' not in page
    assert "Automatically build meeting notes for new meetings" not in page
    assert "notes are built automatically for every" in page
    form = {
        "model": "base.en",
        "beam_size": "5",
        "audio_retention_days": "-1",
        "retention_check_interval_minutes": "60",
        "ai_provider": "claude",
        "auto_generate_notes": "on",  # a stale caller still sends it
    }
    assert "Settings saved" in client.post("/settings", data=form).text
    assert settings_mod.load_settings(app.state.store.root).ai_provider == "claude"


def _write_wav(store, session_id):
    store.write_session_meta(session_id, {"created": "2026-09-20", "tracks": {"mic": {}}})
    wav_path = store.track_wav_path(session_id, "mic")
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(wav_path), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(wire.STREAM_SAMPLE_RATE)
        fh.writeframes(b"\x00\x00" * wire.STREAM_SAMPLE_RATE)


class _Stub:
    def transcribe(self, wav_path, track):
        return [Segment(start=0.0, end=1.0, text="hi", track=track)]


def _run_job(store, session_id="s1"):
    queue = JobQueue(store, lambda **_kw: _Stub())
    job_id = store.create_job(session_id)
    queue._process(job_id)
    return job_id


def _setup(tmp_path, **settings):
    store = store_mod.Store(str(tmp_path / "data"))
    settings_mod.save_settings(store.root, settings_mod.Settings(model="base.en", **settings))
    _write_wav(store, "s1")
    return store


def test_new_meeting_transcription_auto_queues_notes_once(tmp_path):
    store = _setup(tmp_path, ai_provider="claude")
    job_id = _run_job(store)
    assert store.read_job(job_id)["state"] == wire.JobState.DONE
    reviews = store.list_reviews(session_id="s1")
    assert len(reviews) == 1 and reviews[0]["status"] == "queued"
    assert reviews[0]["transcript_job_id"] == job_id


def test_an_untagged_meeting_gets_default_type_notes_with_no_setting(tmp_path):
    store = _setup(tmp_path, ai_provider="claude", default_template_id="webinar")
    _run_job(store)
    (review,) = store.list_reviews(session_id="s1")
    assert review["template_id"] == "webinar"


def test_no_auto_queue_when_provider_disabled(tmp_path):
    store = _setup(tmp_path, ai_provider="disabled")
    _run_job(store)
    assert store.list_reviews(session_id="s1") == []


def test_old_settings_json_with_auto_generate_false_still_queues_notes(tmp_path):
    store = _setup(tmp_path, ai_provider="claude")
    path = store.root / "settings.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["auto_generate_notes"] = False
    path.write_text(json.dumps(data), encoding="utf-8")
    _run_job(store)
    assert len(store.list_reviews(session_id="s1")) == 1


def test_retranscribe_never_auto_queues(tmp_path):
    store = _setup(tmp_path, ai_provider="disabled")
    _run_job(store)  # the meeting's first transcript, notes off
    settings_mod.save_settings(store.root, settings_mod.Settings(model="base.en", ai_provider="claude"))
    _run_job(store)  # retranscribe of an existing meeting
    assert store.list_reviews(session_id="s1") == []


def test_auto_queue_does_not_duplicate_an_existing_review(tmp_path):
    store = _setup(tmp_path, ai_provider="claude")
    job_id = store.create_job("s1")
    JobQueue(store, lambda **_kw: _Stub())._process(job_id)
    assert len(store.list_reviews(session_id="s1")) == 1
    # Same session again as if it were still "new": create_review is idempotent
    # for the same transcript job, so calling the hook twice adds nothing.
    JobQueue(store, None)._maybe_auto_queue_review("s1")
    assert len(store.list_reviews(session_id="s1")) == 1


def test_auto_queue_failure_does_not_fail_the_job(tmp_path, monkeypatch):
    store = _setup(tmp_path, ai_provider="claude")

    def boom(*_a, **_k):
        raise RuntimeError("review store exploded")

    monkeypatch.setattr(store, "create_review", boom)
    job_id = _run_job(store)
    job = store.read_job(job_id)
    assert job["state"] == wire.JobState.DONE
    assert job["error"] is None
    assert store.read_transcript(job_id) is not None
