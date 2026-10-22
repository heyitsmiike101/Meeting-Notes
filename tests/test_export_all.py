"""Export all: one meeting or a selection as a zip of transcript + notes Markdown."""

from __future__ import annotations

import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from meeting_notes import wire
from meeting_notes.server.app import create_app
from meeting_notes.server.web import render_transcriptions_page

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated:DeprecationWarning"
)

NOTES = {
    "title": "Planning",
    "summary": "We planned the quarter.",
    "key_points": ["Ship export"],
    "action_items": [{"action": "Write tests", "owner": "Mike"}],
}


def _client(tmp_path, monkeypatch, token=None):
    if token:
        monkeypatch.setenv("MEETING_NOTES_TOKEN", token)
    else:
        monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = create_app(data_root=str(tmp_path / "data"), enable_mcp=False)
    return TestClient(app), app.state.store


def _meeting(store, sid, name, transcript=True, notes=False):
    store.write_session_meta(sid, {"name": name, "created": "2026-09-20"})
    if transcript:
        job_id = store.create_job(sid)
        store.update_job(job_id, state=wire.JobState.DONE, progress=1.0)
        store.write_transcript(job_id, "# Meeting transcript\n\nHello there", json.dumps({"segments": []}))
    if notes:
        review = store.create_review(sid)
        store.claim_next_review()
        store.complete_review(review["review_id"], NOTES)


def _zip(resp):
    return zipfile.ZipFile(io.BytesIO(resp.content))


def test_single_export_has_transcript_and_notes(tmp_path, monkeypatch):
    client, store = _client(tmp_path, monkeypatch)
    _meeting(store, "s1", "Plan: Q4/2026", notes=True)
    resp = client.get("/sessions/s1/export.zip")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert "Plan- Q4-2026.zip" in resp.headers["content-disposition"]
    z = _zip(resp)
    assert sorted(z.namelist()) == ["Plan- Q4-2026 - notes.md", "Plan- Q4-2026 - transcript.md"]
    assert z.read("Plan- Q4-2026 - transcript.md").decode() == client.get("/sessions/s1/transcript.md").text
    notes = z.read("Plan- Q4-2026 - notes.md").decode()
    assert notes.startswith("# Planning\n")
    assert "## Summary\nWe planned the quarter." in notes
    assert "- Write tests — Owner: Mike" in notes


def test_single_export_omits_missing_files_and_404s_when_empty(tmp_path, monkeypatch):
    client, store = _client(tmp_path, monkeypatch)
    _meeting(store, "s1", "Only transcript")
    _meeting(store, "s2", "Nothing", transcript=False)
    assert _zip(client.get("/sessions/s1/export.zip")).namelist() == ["Only transcript - transcript.md"]
    resp = client.get("/sessions/s2/export.zip")
    assert resp.status_code == 404
    assert "Nothing to export" in resp.json()["detail"]
    assert client.get("/sessions/missing/export.zip").status_code == 404


def test_bulk_export_folders_collisions_and_skips(tmp_path, monkeypatch):
    client, store = _client(tmp_path, monkeypatch)
    _meeting(store, "a", "Standup", notes=True)
    _meeting(store, "b", "Standup")
    _meeting(store, "c", "Empty", transcript=False)
    resp = client.post("/sessions/export.zip", json={"ids": ["a", "b", "c", "a"]})
    assert resp.status_code == 200
    assert "Meeting notes export (2 meetings).zip" in resp.headers["content-disposition"]
    assert sorted(_zip(resp).namelist()) == [
        "Standup (2)/Standup (2) - transcript.md",
        "Standup/Standup - notes.md",
        "Standup/Standup - transcript.md",
    ]


def test_bulk_export_all_empty_is_an_error(tmp_path, monkeypatch):
    client, store = _client(tmp_path, monkeypatch)
    _meeting(store, "c", "Empty", transcript=False)
    resp = client.post("/sessions/export.zip", json={"ids": ["c"]})
    assert resp.status_code == 404
    assert "Nothing to export" in resp.json()["detail"]
    assert client.post("/sessions/export.zip", json={"ids": []}).status_code == 400
    assert client.post("/sessions/export.zip", json={"ids": ["../x"]}).status_code == 400


def test_export_requires_the_web_token(tmp_path, monkeypatch):
    client, store = _client(tmp_path, monkeypatch, token="sekret")
    _meeting(store, "s1", "Planning")
    for resp in (
        client.get("/sessions/s1/export.zip", follow_redirects=False),
        client.post("/sessions/export.zip", json={"ids": ["s1"]}, follow_redirects=False),
    ):
        assert resp.status_code == 303 and resp.headers["location"].startswith("/login")
    ok = client.get("/sessions/s1/export.zip", headers={"Authorization": "Bearer sekret"})
    assert ok.status_code == 200


def test_meetings_page_has_export_controls(tmp_path, monkeypatch):
    client, _store = _client(tmp_path, monkeypatch)
    html = client.get("/meetings").text
    assert 'id="export-all"' in html and "Export all (.zip)" in html
    assert 'title="Download the transcript and notes as Markdown in a zip"' in html
    assert 'class="btn secondary" id="bulk-export"' in html
    assert html.index('id="bulk-export"') < html.index('id="bulk-retranscribe"')
    assert html.index('id="export-all"') < html.index('class="menu-sep"')
    assert html.count("'bulk-notion','bulk-export','bulk-retranscribe'") == 2  # updateSelection + runBulk lists
