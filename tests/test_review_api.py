"""HTTP contract tests for the review queue and Codex bridge.

These tests intentionally exercise the public API rather than the Store.  The
bridge contract is:

* ``POST /v1/sessions/{session_id}/review`` queues an explicit review;
* ``GET /v1/bridge/review/claim`` claims work and returns an ``id`` plus
  authenticated ``transcript_url`` and ``workflow_url``;
* the bridge downloads those URLs, then posts ``{"notes": ...}`` to
  ``POST /v1/bridge/review/{id}/complete`` or ``{"error": ...}`` to
  ``.../failure``;
* ``GET /v1/meeting-notes`` and ``GET /v1/meeting-notes/{id}`` expose done
  notes, while ``POST .../{id}/retry`` requeues an error.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from meeting_notes import wire
from meeting_notes.bridge import BridgeConfig, BridgeWorker
from meeting_notes.server.app import create_app
from meeting_notes.server import settings as settings_mod

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated:DeprecationWarning"
)

TOKEN = "review-secret"


def _headers():
    return {"Authorization": f"Bearer {TOKEN}"}


def _notes():
    return {
        "title": "Planning",
        "summary": "The team planned the release.",
        "meeting_notes": "The team reviewed and approved the release plan.",
        "participants": [],
        "key_points": ["The release is ready."],
        "decisions": ["Ship on Friday."],
        "action_items": [{"action": "Publish the release", "owner": None, "due_date": None}],
        "open_questions": [],
        "risks": [],
        "next_steps": ["Review the checklist"],
    }


def _app(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_NOTES_TOKEN", TOKEN)
    app = create_app(data_root=str(tmp_path / "data"))
    store = app.state.store
    store.write_session_meta("session-1", {"name": "Planning"})
    job_id = store.create_job("session-1")
    store.update_job(job_id, state=wire.JobState.DONE, progress=1.0)
    store.write_transcript(job_id, "# Planning", json.dumps({"segments": [{"text": "Ship Friday"}]}))
    return app


def test_review_api_requires_auth_and_does_not_auto_queue(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    client = TestClient(app)
    assert client.get("/v1/meeting-notes").status_code == 401
    assert client.post("/v1/sessions/session-1/review").status_code == 401
    assert client.get("/v1/bridge/review/claim", headers={"Authorization": "Bearer wrong"}).status_code == 403
    assert client.get("/v1/meeting-notes", headers=_headers()).json()["items"] == []


def test_queue_claim_download_complete_list_and_detail(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    client = TestClient(app)
    queued = client.post("/v1/sessions/session-1/review", headers=_headers())
    assert queued.status_code == 200
    review = queued.json()
    assert review["status"] == "queued"

    claim = client.get("/v1/bridge/review/claim?worker_id=test-worker", headers=_headers())
    assert claim.status_code == 200
    job = claim.json()
    assert job["id"] == review["review_id"]
    assert "transcript_url" in job and "workflow_url" in job
    assert client.get(job["transcript_url"], headers=_headers()).status_code == 200
    workflow = client.get(job["workflow_url"], headers=_headers())
    assert workflow.status_code == 200
    assert "workflow" in workflow.text.lower()

    completed = client.post(
        f"/v1/bridge/review/{job['id']}/complete",
        headers=_headers(),
        json={"notes": _notes()},
    )
    assert completed.status_code == 200
    assert completed.json()["status"] == "done"
    listed = client.get("/v1/meeting-notes", headers=_headers())
    assert listed.status_code == 200
    assert listed.json()["items"][0]["review_id"] == job["id"]
    detail = client.get(f"/v1/meeting-notes/{job['id']}", headers=_headers())
    assert detail.status_code == 200
    assert detail.json()["notes"]["summary"] == _notes()["summary"]
    assert detail.json()["transcript"]


def test_bridge_downloads_the_operator_configured_workflow(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    custom_workflow = "# Custom notes rules\n\nTranscript wins."
    settings_mod.save_settings(
        app.state.store.root,
        settings_mod.Settings(model="base.en", ai_workflow=custom_workflow),
    )

    response = TestClient(app).get("/v1/bridge/workflow.md", headers=_headers())

    assert response.status_code == 200
    assert response.text == custom_workflow


def test_bridge_rejects_malformed_result_and_supports_failure_retry(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    client = TestClient(app)
    review = client.post("/v1/sessions/session-1/review", headers=_headers()).json()
    claim = client.get("/v1/bridge/review/claim", headers=_headers()).json()

    malformed = client.post(
        f"/v1/bridge/review/{review['review_id']}/complete",
        headers=_headers(),
        json={"notes": {"summary": "missing required fields"}},
    )
    assert malformed.status_code in (400, 422)
    assert app.state.store.read_review(review["review_id"])["status"] == "running"

    failed = client.post(
        f"/v1/bridge/review/{claim['id']}/failure",
        headers=_headers(),
        json={"error": "codex unavailable"},
    )
    assert failed.status_code == 200
    assert failed.json()["status"] == "error"
    retried = client.post(
        f"/v1/meeting-notes/{claim['id']}/retry", headers=_headers()
    )
    assert retried.status_code == 200
    assert retried.json()["status"] == "queued"


def test_bridge_worker_processes_real_server_contract_end_to_end(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    client = TestClient(app)
    queued = client.post("/v1/sessions/session-1/review", headers=_headers()).json()
    worker = BridgeWorker(BridgeConfig("http://testserver", token=TOKEN, worker_id="e2e"))
    worker.client = client

    def fake_codex(command, *, cwd, env, check):
        transcript = Path(cwd, "transcript.txt").read_text(encoding="utf-8")
        workflow = Path(cwd, "workflow.md").read_text(encoding="utf-8")
        assert "# Planning" in transcript
        assert "source of truth" in workflow
        Path(command[command.index("-o") + 1]).write_text(
            json.dumps(_notes()), encoding="utf-8"
        )
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr("meeting_notes.bridge.subprocess.run", fake_codex)
    worker.run(once=True)

    detail = TestClient(app).get(
        f"/v1/meeting-notes/{queued['review_id']}", headers=_headers()
    )
    assert detail.status_code == 200
    assert detail.json()["status"] == "done"
    assert detail.json()["notes"]["title"] == "Planning"


def test_user_can_rename_meeting_and_summary_independently(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    client = TestClient(app)
    renamed = client.patch(
        "/v1/sessions/session-1", headers=_headers(), json={"name": "Canonical meeting"}
    )
    assert renamed.status_code == 200
    assert renamed.json() == {"session_id": "session-1", "name": "Canonical meeting"}

    review = client.post("/v1/sessions/session-1/review", headers=_headers()).json()
    claim = client.get("/v1/bridge/review/claim?worker_id=test-worker", headers=_headers()).json()
    generated = _notes() | {"title": "AI-generated summary"}
    assert client.post(
        f"/v1/bridge/review/{claim['id']}/complete",
        headers=_headers(), json={"notes": generated},
    ).status_code == 200

    summary = client.patch(
        f"/v1/meeting-notes/{review['review_id']}",
        headers=_headers(), json={"title": "Edited summary"},
    )
    assert summary.status_code == 200
    assert summary.json()["title"] == "Edited summary"
    assert summary.json()["name"] == "Canonical meeting"
    assert app.state.store.read_session_meta("session-1")["name"] == "Canonical meeting"
    detail = client.get(f"/v1/meeting-notes/{review['review_id']}", headers=_headers())
    assert detail.json()["notes"]["title"] == "Edited summary"

    assert client.post(
        f"/v1/meeting-notes/{review['review_id']}/retry", headers=_headers()
    ).status_code == 200
    regenerated = client.get(
        "/v1/bridge/review/claim?worker_id=test-worker", headers=_headers()
    ).json()
    assert regenerated["id"] == review["review_id"]
    assert client.post(
        f"/v1/bridge/review/{regenerated['id']}/complete",
        headers=_headers(), json={"notes": _notes() | {"title": "New AI title"}},
    ).status_code == 200
    detail = client.get(f"/v1/meeting-notes/{review['review_id']}", headers=_headers())
    assert detail.json()["notes"]["title"] == "Edited summary"
    assert detail.json()["note"]["meta"]["name"] == "Canonical meeting"


def test_rename_endpoints_validate_names_and_review_state(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    client = TestClient(app)
    assert client.patch(
        "/v1/sessions/session-1", headers=_headers(), json={"name": "   "}
    ).status_code == 400
    review = client.post("/v1/sessions/session-1/review", headers=_headers()).json()
    response = client.patch(
        f"/v1/meeting-notes/{review['review_id']}", headers=_headers(), json={"title": "Later"}
    )
    assert response.status_code == 409


def test_pipeline_update_cannot_overwrite_a_saved_meeting_rename(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    client = TestClient(app)
    assert client.patch(
        "/v1/sessions/session-1", headers=_headers(), json={"name": "User rename"}
    ).status_code == 200
    response = client.put(
        "/v1/sessions/session-1/pipeline",
        headers=_headers(),
        json={
            "name": "Stale client name",
            "state": "uploading",
            "percent": 50,
            "bytes_received": 50,
            "bytes_total": 100,
        },
    )
    assert response.status_code == 200
    meta = app.state.store.read_session_meta("session-1")
    assert meta["name"] == "User rename"
    assert meta["upload"]["percent"] == 50


def test_finalize_cannot_overwrite_a_renamed_legacy_session(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    client = TestClient(app)
    assert client.patch(
        "/v1/sessions/session-1", headers=_headers(), json={"name": "User rename"}
    ).status_code == 200
    response = client.post(
        wire.finalize_path("session-1"),
        headers=_headers(),
        json={"meta": {"name": "Stale client name"}, "timing": {}, "settings": {}},
    )
    assert response.status_code == 200
    assert app.state.store.read_session_meta("session-1")["name"] == "User rename"
