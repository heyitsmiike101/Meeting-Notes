"""Start, end and talk time on meeting notes; the transcript's Download .md."""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from meeting_notes import wire
from meeting_notes.server import meeting_stats as ms
from meeting_notes.server import notion_blocks as nb
from meeting_notes.server.app import create_app

from notion_helpers import TZ, epoch, notes

TOKEN = "stats-secret"
H = {"Authorization": f"Bearer {TOKEN}"}

SEGMENTS = [
    {"start": 0.0, "end": 30.0, "text": "hi", "track": "mic", "speaker": "You"},
    {"start": 30.0, "end": 120.0, "text": "hello", "track": "system", "speaker": "Them"},
    {"start": 110.0, "end": 140.0, "text": "overlap", "track": "mic", "speaker": "You"},
]


# -- the numbers -----------------------------------------------------------------------------------


def test_talk_time_counts_seconds_of_speech_per_side():
    talk = ms.talk_time(SEGMENTS)
    assert talk == {"me_sec": 60.0, "them_sec": 90.0, "me_pct": 40, "them_pct": 60}


def test_talk_time_percentages_always_add_up():
    talk = ms.talk_time([{"start": 0, "end": 1, "track": "mic"}, {"start": 0, "end": 2, "track": "system"}])
    assert talk["me_pct"] + talk["them_pct"] == 100 and talk["me_pct"] == 33


def test_talk_time_uses_the_speaker_label_when_there_is_no_track():
    talk = ms.talk_time([{"start": 0, "end": 10, "speaker": "You"}, {"start": 10, "end": 40, "speaker": "Them"}])
    assert talk["me_pct"] == 25


def test_talk_time_is_none_when_nobody_can_be_placed():
    assert ms.talk_time([]) is None
    assert ms.talk_time([{"start": 0, "end": 5, "speaker": "Alice"}]) is None  # imported, named speakers
    assert ms.talk_time([{"start": 5, "end": 5, "track": "mic"}, {"start": "x", "end": 9, "track": "mic"}]) is None


def test_meeting_stats_start_end_and_duration():
    st = ms.meeting_stats({"started_wall": 1000.0, "duration_sec": 600}, SEGMENTS)
    assert st["started"] == 1000.0 and st["ended"] == 1600.0 and st["duration_sec"] == 600.0
    assert st["talk"]["me_pct"] == 40


def test_meeting_stats_falls_back_to_created_and_last_segment():
    created = "2026-10-13T15:30:00"
    st = ms.meeting_stats({"created": created}, SEGMENTS)
    assert st["started"] == datetime.fromisoformat(created).timestamp()
    assert st["duration_sec"] == 140.0 and st["ended"] == st["started"] + 140.0


def test_meeting_stats_with_nothing_known():
    assert ms.meeting_stats({}, []) == {"started": None, "ended": None, "duration_sec": None, "talk": None}
    assert ms.meeting_stats(None, None)["talk"] is None


def test_detail_lines_read_naturally():
    st = {"started": epoch("2026-10-13 15:30"), "ended": epoch("2026-10-13 16:23"), "duration_sec": 3180,
          "talk": {"me_sec": 600.0, "them_sec": 1500.0, "me_pct": 29, "them_pct": 71}}
    assert ms.detail_lines(st, TZ) == [
        "Start: Tue Oct 13, 3:30 PM",
        "End: 4:23 PM (53 min)",
        "Talk time: you 29% (10 min), them 71% (25 min)",
    ]


def test_detail_lines_name_the_day_when_a_meeting_crosses_midnight():
    st = {"started": epoch("2026-10-13 23:30"), "ended": epoch("2026-10-14 00:45"), "duration_sec": 4500, "talk": None}
    assert ms.detail_lines(st, TZ)[1] == "End: Wed Oct 14, 12:45 AM (1 h 15 min)"


@pytest.mark.parametrize("seconds,text", [(None, ""), (20, "under a minute"), (60, "1 min"), (3600, "1 h"), (3900, "1 h 5 min")])
def test_duration_text(seconds, text):
    assert ms.duration_text(seconds) == text


def test_notion_copy_has_a_meeting_details_section():
    blocks = nb.notes_to_blocks(notes(), details=["Start: Tue Oct 13, 3:30 PM", "Talk time: you 40%"])
    text = json.dumps(blocks)
    assert "Meeting details" in text and "Talk time: you 40%" in text
    assert text.index("Meeting details") < text.index("Participants")
    assert "Meeting details" not in json.dumps(nb.notes_to_blocks(notes()))  # no details: no empty section


# -- the server --------------------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_NOTES_TOKEN", TOKEN)
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    app = create_app(data_root=str(tmp_path / "data"), enable_mcp=False)
    store = app.state.store
    store.write_session_meta("weekly", {"name": "Weekly sync", "started_wall": epoch("2026-10-13 15:30"),
                                        "duration_sec": 3180})
    job = store.create_job("weekly")
    store.update_job(job, state=wire.JobState.DONE, progress=1.0)
    store.write_transcript(job, "# Meeting transcript\n\nDate: 2026-10-13\n\n**You:** hi\n",
                           json.dumps({"session": {}, "segments": SEGMENTS}))
    review = store.create_review("weekly", force=True)
    store.claim_next_review()
    store.complete_review(review["review_id"], notes(title="Weekly sync"))
    with TestClient(app) as client:
        yield client, review["review_id"]


def test_notes_api_reports_the_stats(env):
    client, review_id = env
    data = client.get(f"/v1/meeting-notes/{review_id}", headers=H).json()
    for stats in (data["stats"], data["note"]["stats"]):
        assert stats["started"] == epoch("2026-10-13 15:30")
        assert stats["ended"] == epoch("2026-10-13 15:30") + 3180
        assert stats["talk"] == {"me_sec": 60.0, "them_sec": 90.0, "me_pct": 40, "them_pct": 60}


def test_transcript_download_is_named_after_the_meeting_and_has_the_details(env):
    client, _ = env
    r = client.get("/sessions/weekly/transcript.md", headers=H)
    assert r.status_code == 200
    body = r.text
    assert body.startswith("# Weekly sync: transcript\n\n## Meeting details\n- Start: ")
    assert "- Talk time: you 40% (1 min), them 60% (2 min)" in body
    assert "**You:** hi" in body  # the transcript itself is untouched
    cd = r.headers["content-disposition"]
    assert 'filename="Weekly sync - transcript.md"' in cd and "filename*=UTF-8''Weekly%20sync%20-%20transcript.md" in cd


def test_transcript_download_keeps_working_without_a_name_or_stats(env, tmp_path):
    client, _ = env
    store = client.app.state.store
    store.write_session_meta("bare", {})
    job = store.create_job("bare")
    store.update_job(job, state=wire.JobState.DONE, progress=1.0)
    store.write_transcript(job, "# Meeting transcript\n\nhello\n", json.dumps({"session": {}, "segments": []}))
    r = client.get("/sessions/bare/transcript.md", headers=H)
    assert r.status_code == 200 and r.text == "# Meeting transcript\n\nhello\n"
    assert 'filename="bare - transcript.md"' in r.headers["content-disposition"]


def test_meeting_page_has_the_transcript_download_button_and_facts(env):
    client, _ = env
    page = client.get("/sessions/weekly", headers=H).text
    assert 'id="transcript-download"' in page and "transcript-only" in page
    assert "function meetingFacts" in page and "meetingFactsMarkup(note.stats||data.stats)" in page


# -- note type on the lists ---------------------------------------------------------------------------


def test_list_shows_the_applied_or_planned_note_type(env):
    client, _ = env
    store = client.app.state.store
    store.write_session_meta("tagged", {"name": "Standup", "started_wall": epoch("2026-10-13 09:00"),
                                        "duration_sec": 600, "note_type": "quick"})
    store.write_session_meta("untagged", {"name": "Chat", "started_wall": epoch("2026-10-13 08:00"),
                                          "duration_sec": 600})
    items = {i["session_id"]: i for i in client.get("/v1/sessions", headers=H).json()["items"]}
    assert items["weekly"]["note_type"] == {"id": "standard", "name": "Standard", "applied": True}
    assert items["tagged"]["note_type"] == {"id": "quick", "name": "Quick notes", "applied": False}
    assert items["untagged"]["note_type"]["applied"] is False and items["untagged"]["note_type"]["id"] == "standard"
    assert "note_type_tag" not in items["weekly"]


def test_list_pages_draw_the_note_type_chip(env):
    client, _ = env
    for path in ("/", "/transcriptions"):
        page = client.get(path, headers=H).text
        assert "function typeChip" in page
        # Home keeps the chip; the Meetings table has a plain-text Note type cell.
        assert ("typeChip(row) +" in page) if path == "/" else ("typeCell(row)" in page and "typeChip(row) +" not in page)


def test_filter_by_note_type(env):
    client, review_id = env
    store = client.app.state.store
    store.write_session_meta("tagged", {"name": "Standup", "started_wall": epoch("2026-10-13 09:00"),
                                        "duration_sec": 600, "note_type": "quick"})
    store.write_session_meta("gone", {"name": "Old", "started_wall": epoch("2026-10-13 07:00"),
                                      "duration_sec": 600, "note_type": "t-deleted"})  # unknown type: default

    def ids(note_type):
        data = client.get(f"/v1/sessions?note_type={note_type}", headers=H).json()
        return sorted(i["session_id"] for i in data["items"]), data["total"]

    assert ids("quick") == (["tagged"], 1)
    assert ids("standard") == (["gone", "weekly"], 2)
    assert ids("webinar") == ([], 0)
    assert len(client.get("/v1/sessions", headers=H).json()["items"]) == 3
    # Notes made with another type move the meeting: the notes' type wins over the recorder's tag.
    store.retry_review(review_id, template={"id": "quick", "name": "Quick notes"})
    assert ids("quick") == (["tagged", "weekly"], 2)


def test_an_older_index_gets_the_note_type_columns_and_is_rebuilt(tmp_path):
    import sqlite3

    from meeting_notes.server.store import Store

    root = tmp_path / "data"
    store = Store(root)
    store.write_session_meta("a", {"name": "A", "started_wall": 1000.0, "note_type": "quick"})
    del store
    # Simulate an index made by an older release: no note_type_tag / template columns.
    con = sqlite3.connect(str(root / "index.sqlite"))
    con.execute("ALTER TABLE sessions DROP COLUMN note_type_tag")
    con.execute("ALTER TABLE review_statuses DROP COLUMN template_id")
    con.execute("ALTER TABLE review_statuses DROP COLUMN template_name")
    con.commit()
    con.close()
    store = Store(root)
    result = store.list_sessions(note_type="quick", note_type_ids=["standard", "quick"], default_note_type="standard")
    assert [i["session_id"] for i in result["items"]] == ["a"]


def test_meetings_page_has_the_note_type_filter(env):
    client, _ = env
    page = client.get("/transcriptions", headers=H).text
    assert 'id="type-filter"' in page and "note_type=" in page and "function fillTypeFilter" in page


def test_meeting_box_is_bar_and_key_with_numbers_on_hover(env):
    client, _ = env
    page = client.get("/sessions/weekly", headers=H).text
    assert 'class="talk" tabindex="0"' in page and "talk-tip" in page and 'role="tooltip"' in page
    assert "<span>notes.md</span>" in page and "<span>transcript.md</span>" in page
    assert "Download .md" not in page
    css = client.get("/static/app.css").text
    assert ".talk:hover .talk-tip" in css and ".talk:focus" in css
    assert "#transcript-download span" in css  # the file names stay visible on a phone
