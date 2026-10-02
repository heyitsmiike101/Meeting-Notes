"""Shared setup for the Notion export tests: a real Store, a FakeNotion, a NotionSync."""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timedelta, timezone

from meeting_notes import wire
from meeting_notes.server import settings as settings_mod
from meeting_notes.server.notion import NotionSync
from meeting_notes.server.notion_api import normalize_page_id
from meeting_notes.server.store import Store

from fake_notion import FakeNotion

TZ = timezone(timedelta(hours=-4))  # the "server's local zone" used by every test


def epoch(text: str) -> float:
    """``2026-09-30 14:05`` in the test zone (UTC-4) as an epoch."""
    return datetime.fromisoformat(text).replace(tzinfo=TZ).timestamp()


def notes(title="Notes", summary="A summary.", body="Some **detailed** notes.", **extra):
    base = {
        "title": title, "summary": summary, "meeting_notes": body, "participants": ["Mike"],
        "key_points": ["A key point"], "decisions": ["A decision"],
        "action_items": [{"action": "Do the thing", "owner": "Mike", "due_date": "2026-10-05", "context": None}],
        "open_questions": [], "risks": [], "next_steps": [],
    }
    base.update(extra)
    return base


def add_meeting(store: Store, sid: str, name: str, started: str, *, payload=None, template=None,
                complete=True, queue_review=True) -> dict:
    """A meeting with a finished transcript; with ``complete`` also finished notes (fires the listeners)."""
    store.write_session_meta(sid, {"name": name, "started_wall": epoch(started), "duration_sec": 600})
    job_id = store.create_job(sid)
    store.update_job(job_id, state=wire.JobState.DONE, progress=1.0)
    store.write_transcript(job_id, "# t", json.dumps({"session": {}, "segments": []}))
    if not queue_review:
        return {}
    review = store.create_review(sid, force=True, template=template)
    if complete:
        complete_notes(store, review["review_id"], payload or notes(title=name))
    return review


def complete_notes(store: Store, review_id: str, payload: dict) -> None:
    claimed = store.claim_next_review()
    assert claimed and claimed["review_id"] == review_id, "expected to claim the review we queued"
    store.complete_review(review_id, payload)


def set_notion_settings(store: Store, **parents_and_flags) -> None:
    """``set_notion_settings(store, parents={"standard": uuid}, auto=True)``."""
    current = settings_mod.load_settings(store.root)
    parents = {k: normalize_page_id(v) for k, v in (parents_and_flags.get("parents") or {}).items()}
    settings_mod.save_settings(
        store.root,
        dataclasses.replace(current, notion_parents=parents, notion_auto_copy=parents_and_flags.get("auto", True),
                            notion_auto_types=(
                                [t["id"] for t in current.all_templates()] if parents_and_flags.get("auto", True) else []
                            ),
                            server_address=parents_and_flags.get("address", "http://meeting.lan")),
    )


class Env:
    """Everything a test needs, wired together."""

    def __init__(self, tmp_path, *, auto=True):
        self.store = Store(str(tmp_path / "data"))
        self.fake = FakeNotion()
        self.sleeps: list = []
        self.clock = {"t": 1_800_000_000.0}
        self.sync = self.new_sync()
        self.sync.tokens.set(self.fake.token)
        self.root_page = self.fake.add_page("Meeting notes root")
        self.webinar_page = self.fake.add_page("Webinar root")
        set_notion_settings(
            self.store, parents={"standard": self.root_page, "webinar": self.webinar_page}, auto=auto
        )

    def new_sync(self) -> NotionSync:
        return NotionSync(
            self.store, transport=self.fake.transport(), sleep=self.sleeps.append, min_interval=0.0, tz=TZ,
            now=lambda: self.clock["t"], backoff=lambda attempt: 10.0 * attempt,
        )

    def run(self) -> int:
        return self.sync.run_pending()

    def month_page(self, title: str) -> str:
        pid = self.fake.find_page(title)
        assert pid, f"no page titled {title!r}; have {[n['title'] for n in self.fake.nodes.values() if n['type']=='page']}"
        return pid
