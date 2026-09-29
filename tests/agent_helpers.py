"""Shared setup for the agent API / MCP tests.

Builds a real ``create_app`` server, wires ``install_agent_access`` onto it (the
same call ``create_app`` makes in production) and seeds four meetings through
the real ``Store`` APIs.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from meeting_notes import wire
from meeting_notes.server.app import create_app

WEB_TOKEN = "web-secret"


def epoch(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def seg(start, end, label, text, track="mic", in_gap=False):
    return {"start": start, "end": end, "label": label, "text": text, "track": track, "in_gap": in_gap}


PLANNING_SEGMENTS = [
    seg(0.0, 8.0, "You", "Welcome everyone, let's plan the sprint."),
    seg(8.5, 20.0, "Them", "I can take the migration work by Friday.", "system"),
    seg(21.0, 30.0, "You", "Great. We decided to ship the beta on Monday."),
    seg(31.0, 33.0, "Them", "hallucinated silence text", "system", in_gap=True),
    seg(40.0, 55.0, "Them", "Budget is the main risk for the beta.", "system"),
]

PLANNING_NOTES = {
    "title": "Sprint planning",
    "summary": "The team planned the sprint and agreed to ship the beta on Monday.",
    "meeting_notes": "Long form notes about the sprint plan and the beta launch.",
    "participants": ["Mike", "Sarah"],
    "key_points": ["Migration is the critical path"],
    "decisions": ["Ship the beta on Monday"],
    "action_items": [
        {"action": "Finish the migration", "owner": "Sarah", "due_date": "2026-09-05", "context": "blocks the beta"},
        {"action": "Send the launch email", "owner": "Mike", "due_date": None, "context": None},
    ],
    "open_questions": ["Who approves the budget?"],
    "risks": ["Budget overrun"],
    "next_steps": ["Review on Friday"],
}

BUDGET_NOTES = {
    "title": "Budget review",
    "summary": "Q4 budget was trimmed.",
    "meeting_notes": "The Q4 budget was reviewed line by line.",
    "participants": ["Mike"],
    "key_points": ["Cloud spend is up"],
    "decisions": ["Freeze new hires until Q1", "Renew the analytics contract"],
    "action_items": [
        {"action": "Draft the hiring freeze memo", "owner": "Mike", "due_date": "2026-09-15", "context": None},
    ],
    "open_questions": [],
    "risks": [],
    "next_steps": [],
}


def _add_meeting(store, sid, name, created_iso, duration, segments=None, notes=None, device="Laptop"):
    store.write_session_meta(
        sid,
        {"name": name, "started_wall": epoch(created_iso), "duration_sec": duration, "device": device},
    )
    if segments is not None:
        job_id = store.create_job(sid)
        store.update_job(job_id, state=wire.JobState.DONE, progress=1.0)
        md = "# Meeting transcript\n"
        store.write_transcript(job_id, md, json.dumps({"session": {}, "segments": segments}))
    if notes is not None:
        review = store.create_review(sid)
        claimed = store.claim_next_review()
        assert claimed and claimed["review_id"] == review["review_id"]
        store.complete_review(review["review_id"], dict(notes))


def seed(store) -> dict:
    _add_meeting(store, "m-plan", "Sprint planning", "2026-09-01T15:00:00Z", 1800, PLANNING_SEGMENTS, PLANNING_NOTES)
    _add_meeting(
        store, "m-budget", "Budget review", "2026-09-10T15:00:00Z", 2400,
        [seg(0, 5, "You", "Let's review the quarterly budget."), seg(6, 12, "Them", "Cloud spend is up.", "system")],
        BUDGET_NOTES,
    )
    _add_meeting(
        store, "m-standup", "Standup", "2026-09-20T15:00:00Z", 600,
        [seg(0, 4, "You", "Quick standup, nothing blocked.")],
    )
    _add_meeting(store, "m-raw", "Fresh recording", "2026-09-25T15:00:00Z", 300)
    return {"ids": ["m-plan", "m-budget", "m-standup", "m-raw"]}


def make_agent_app(tmp_path, monkeypatch, *, with_mcp: bool = True):
    """Returns ``(app, agent_lifespan)``; the lifespan is already composed into the app."""
    monkeypatch.setenv("MEETING_NOTES_TOKEN", WEB_TOKEN)
    app = create_app(data_root=str(tmp_path / "data"), enable_mcp=with_mcp)
    store = app.state.store
    seed(store)
    # create_app already installed agent access and composed its lifespan.
    return app, getattr(app.state, "agent_mcp", None)


def bearer(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}
