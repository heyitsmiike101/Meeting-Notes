"""A transcript the person already has (Teams, Zoom, a .txt/.vtt/.srt file, pasted text) becomes a meeting.

``POST /v1/sessions/transcript`` makes a session with no audio and one finished transcript job: nothing is
transcribed, and everything downstream (web page, search, notes, retranscribe guard) treats it like any meeting.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from meeting_notes.server import settings as settings_mod
from meeting_notes.server import transcript_import as ti
from meeting_notes.server.app import create_app

H = {"Authorization": "Bearer tok"}
STARTED = "2026-09-28T14:30:00Z"  # 1790605800


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_NOTES_TOKEN", "tok")
    app = create_app(
        transcriber_factory=lambda **_: (_ for _ in ()).throw(AssertionError("nothing may be transcribed")),
        data_root=str(tmp_path / "data"),
        media_root=str(tmp_path / "media"),
    )
    with TestClient(app) as c:
        c.app_ref = app
        yield c


def _post(client, **body):
    return client.post("/v1/sessions/transcript", headers=H, json=body)


def _detail(client, session_id):
    return client.get(f"/v1/sessions/{session_id}", headers=H).json()


# -- parsing ---------------------------------------------------------------------------------------------


def test_plain_paragraphs_get_estimated_times_and_are_flagged_approximate():
    parsed = ti.parse_transcript("We agreed to ship on Friday.\n\nBob owns the release notes and the rollout plan.")
    assert parsed.format == "plain" and parsed.approximate and not parsed.timed
    assert [s["text"] for s in parsed.segments][0].startswith("We agreed")
    assert all(s["approximate"] for s in parsed.segments)
    assert parsed.segments[0]["start"] == 0 and parsed.segments[1]["start"] == parsed.segments[0]["end"]
    assert parsed.duration == parsed.segments[-1]["end"]


def test_speaker_prefixed_lines_keep_their_labels():
    parsed = ti.parse_transcript("Jane: Hello team\nBob: Hi Jane\nand a second line\nJane: Let us begin")
    assert [s["label"] for s in parsed.segments] == ["Jane", "Bob", "Jane"]
    assert parsed.segments[1]["text"] == "Hi Jane and a second line"
    assert parsed.speakers == ["Jane", "Bob"]


def test_vtt_keeps_timings_and_voice_tag_speakers():
    vtt = (
        "WEBVTT\n\n1\n00:00:01.000 --> 00:00:04.000\n<v Jane Doe>Hello everyone</v>\n\n"
        "2\n00:00:04.500 --> 00:00:08.250\n<v Bob>Hi Jane</v>\n"
    )
    parsed = ti.parse_transcript(vtt, "teams.vtt")
    assert parsed.format == "vtt" and parsed.timed and not parsed.approximate
    first, second = parsed.segments
    assert (first["start"], first["end"], first["label"], first["text"]) == (1.0, 4.0, "Jane Doe", "Hello everyone")
    assert (second["start"], second["end"], second["label"]) == (4.5, 8.25, "Bob")
    assert not first["approximate"] and parsed.duration == 8.25


def test_srt_with_comma_milliseconds_and_speaker_prefix():
    srt = "1\n00:00:01,000 --> 00:00:04,000\nJane: Hello there\n\n2\n01:00:05,500 --> 01:00:07,000\nBob: Much later\n"
    parsed = ti.parse_transcript(srt)
    assert parsed.format == "srt"
    assert [(s["start"], s["label"], s["text"]) for s in parsed.segments] == [
        (1.0, "Jane", "Hello there"),
        (3605.5, "Bob", "Much later"),
    ]


def test_timestamped_lines_with_speakers_and_wrapped_continuations():
    text = "[00:00:05] Jane: Hello\n[00:00:12] Bob: Hi there\nstill Bob talking\n[00:01:00] Jane: bye"
    parsed = ti.parse_transcript(text)
    assert parsed.format == "timestamped" and parsed.timed
    assert [s["start"] for s in parsed.segments] == [5.0, 12.0, 60.0]
    assert parsed.segments[1]["text"] == "Hi there still Bob talking"
    assert parsed.segments[0]["end"] <= 12.0  # never runs into the next segment


def test_teams_copy_paste_shape_speaker_then_time_then_text():
    text = "Jane Doe   0:05\nHello team\nBob Roe   0:20\nHi\nJane Doe   1:02:03\nLong gap later\n"
    parsed = ti.parse_transcript(text)
    assert [(s["label"], s["start"]) for s in parsed.segments] == [("Jane Doe", 5.0), ("Bob Roe", 20.0), ("Jane Doe", 3723.0)]


def test_empty_and_oversized_text_are_rejected():
    with pytest.raises(ti.TranscriptError, match="empty"):
        ti.parse_transcript("  \n \ufeff ")
    with pytest.raises(ti.TranscriptError, match="exceeds"):
        ti.parse_transcript("x" * (ti.MAX_TRANSCRIPT_BYTES + 1))


def test_a_huge_single_paragraph_is_split_into_readable_segments():
    parsed = ti.parse_transcript("word " * 3000)
    assert len(parsed.segments) > 1
    assert all(len(s["text"]) <= ti.MAX_CHUNK_CHARS for s in parsed.segments)


# -- endpoint --------------------------------------------------------------------------------------------


def test_pasted_text_creates_a_finished_meeting_without_audio_or_transcription(client):
    response = _post(client, name="Standup", started_at=STARTED, source="pasted",
                     text="Jane: We ship Friday.\nBob: I will write the release notes.")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["state"] == "done" and body["segments"] == 2 and body["speakers"] == ["Jane", "Bob"]
    assert body["approximate"] is True and body["format"] == "plain"
    assert body["pipeline"]["state"] == "complete"

    detail = _detail(client, body["session_id"])
    assert detail["meta"]["name"] == "Standup"
    assert detail["meta"]["source"] == "transcript"
    assert detail["meta"]["started_wall"] == 1790605800
    assert detail["meta"]["created"] == "2026-09-28T14:30:00Z"
    assert detail["meta"]["transcript_upload"]["source"] == "pasted"
    assert detail["has_audio"] is False
    assert [s["label"] for s in detail["segments"]] == ["Jane", "Bob"]
    assert detail["jobs"][0]["state"] == "done" and detail["jobs"][0]["settings"]["transcript_upload"] is True
    assert "Estimated" in detail["markdown"] or "estimated" in detail["markdown"]
    assert "**[00:00:00] Jane:** We ship Friday." in detail["markdown"]


def test_vtt_file_upload_keeps_times_and_filename_names_the_meeting(client):
    vtt = "WEBVTT\n\n00:00:02.000 --> 00:00:05.000\n<v Ana>Kickoff</v>\n"
    body = _post(client, text=vtt, source="file", filename="C:\\Users\\me\\Q3 planning.vtt", started_at=1790605800).json()
    detail = _detail(client, body["session_id"])
    assert detail["meta"]["name"] == "Q3 planning"
    assert detail["meta"]["transcript_upload"]["filename"] == "Q3 planning.vtt"
    assert detail["meta"]["transcript_upload"]["format"] == "vtt"
    assert detail["segments"][0]["start"] == 2.0 and detail["segments"][0]["approximate"] is False
    assert body["approximate"] is False


def test_default_name_when_none_is_given(client):
    body = _post(client, text="Just some notes from the call.", started_at=STARTED).json()
    assert _detail(client, body["session_id"])["meta"]["name"] == "Pasted transcript 2026-09-28 14:30"


def test_started_at_defaults_to_now_and_accepts_offsets_and_epochs(client):
    now = _detail(client, _post(client, text="hello world, this is a test").json()["session_id"])["meta"]["started_wall"]
    assert now > 1_700_000_000
    offset = _post(client, text="hello world", started_at="2026-09-28T10:30:00-04:00").json()["session_id"]
    assert _detail(client, offset)["meta"]["started_wall"] == 1790605800


@pytest.mark.parametrize(
    "payload, status, fragment",
    [
        ({"text": ""}, 400, "text is required"),
        ({}, 400, "text is required"),
        ({"text": 5}, 400, "text is required"),
        ({"text": "hello", "source": "email"}, 400, "source must be one of"),
        ({"text": "hello", "started_at": "yesterday"}, 400, "started_at must be"),
        ({"text": "hello", "started_at": True}, 400, "started_at must be"),
        ({"text": "hello", "name": 3}, 400, "name must be a string"),
        ({"text": "hello", "filename": ["a"]}, 400, "filename must be a string"),
    ],
)
def test_validation_errors_are_clear(client, payload, status, fragment):
    response = client.post("/v1/sessions/transcript", headers=H, json=payload)
    assert response.status_code == status
    assert fragment in response.json()["detail"]


def test_body_must_be_a_json_object(client):
    assert client.post("/v1/sessions/transcript", headers=H, content="nope").status_code == 400
    assert client.post("/v1/sessions/transcript", headers=H, json=["a"]).status_code == 400


def test_size_limit_gives_413_with_the_limit_in_the_message(client):
    big = _post(client, text="a " * (ti.MAX_TRANSCRIPT_BYTES // 2 + 10))
    assert big.status_code == 413
    assert "2 MB" in big.json()["detail"]
    enormous = client.post("/v1/sessions/transcript", headers=H, content=b'{"text": "' + b"a" * (10 * 1024 * 1024) + b'"}')
    assert enormous.status_code == 413
    # Nothing was stored for a refused upload.
    assert client.get("/v1/sessions", headers=H).json()["total"] == 0


def test_requires_the_client_token(client):
    assert client.post("/v1/sessions/transcript", json={"text": "hi there"}).status_code == 401
    assert client.post("/v1/sessions/transcript", headers={"Authorization": "Bearer wrong"}, json={"text": "hi"}).status_code == 403


def test_a_recorder_outside_the_compat_window_is_told_to_update(client):
    old = client.post(
        "/v1/sessions/transcript", headers={**H, "X-Meeting-Notes-Client": "0.6.1; windows"}, json={"text": "hello there"}
    )
    assert old.status_code == 426
    current = client.post(
        "/v1/sessions/transcript", headers={**H, "X-Meeting-Notes-Client": "0.7.8; windows"}, json={"text": "hello there"}
    )
    assert current.status_code == 201
    meta = _detail(client, current.json()["session_id"])["meta"]
    assert meta["client"] == {"version": "0.7.8", "platform": "windows"}


# -- how it behaves like any meeting ---------------------------------------------------------------------


def test_it_is_listed_searchable_and_downloadable(client):
    body = _post(client, name="Roadmap sync", text="Jane: The zanzibar migration is on track.", started_at=STARTED).json()
    listing = client.get("/v1/sessions", headers=H).json()
    row = next(item for item in listing["items"] if item["session_id"] == body["session_id"])
    assert row["name"] == "Roadmap sync" and row["has_audio"] is False
    assert row["pipeline"]["transcription"]["state"] == "complete"
    found = client.get("/v1/sessions?q=zanzibar", headers=H).json()
    assert [i["session_id"] for i in found["items"]] == [body["session_id"]]
    md = client.get(f"/sessions/{body['session_id']}/transcript.md", headers=H)
    assert md.status_code == 200 and "zanzibar" in md.text
    js = json.loads(client.get(f"/sessions/{body['session_id']}/transcript.json", headers=H).text)
    assert js["segments"][0]["label"] == "Jane"


def test_the_web_page_renders_and_says_transcript_uploaded_instead_of_audio(client):
    body = _post(client, name="Webby", text="hello there everyone").json()
    page = client.get(f"/sessions/{body['session_id']}", headers=H)
    assert page.status_code == 200
    assert "Transcript uploaded" in page.text  # the audio card text for these meetings


def test_retranscribe_is_refused_with_a_clear_reason(client):
    sid = _post(client, text="hello there everyone").json()["session_id"]
    response = client.post(f"/v1/sessions/{sid}/retranscribe", headers=H)
    assert response.status_code == 400
    assert "uploaded as a transcript" in response.json()["detail"]


def test_notes_are_queued_with_the_default_note_type_when_auto_generate_is_on(client, tmp_path):
    store = client.app_ref.state.store
    fields = {
        "ai_provider": "claude",
        "auto_generate_notes": "on",
        "default_template_id": "webinar",
    }
    from tests.test_note_templates import _fields

    settings_mod.save_settings(store.root, settings_mod.validate(_fields(**fields)))
    body = _post(client, text="Jane: We decided to launch.").json()
    assert body["notes"] == "queued"
    (review,) = store.list_reviews(session_id=body["session_id"])
    assert review["status"] == "queued"
    assert (review["template_id"], review["template_name"]) == ("webinar", "Detailed webinar")


def test_no_notes_are_queued_when_auto_generate_is_off(client):
    body = _post(client, text="Jane: We decided to launch.").json()
    assert body["notes"] is None
    assert client.app_ref.state.store.list_reviews(session_id=body["session_id"]) == []
    # ...and the Notes button on the web page can still queue them on demand.
    queued = client.post(f"/v1/sessions/{body['session_id']}/review", headers=H, json={})
    assert queued.status_code in (200, 201, 202)


def test_it_can_be_deleted_and_restored_like_any_meeting(client):
    sid = _post(client, text="hello there everyone").json()["session_id"]
    assert client.delete(f"/v1/sessions/{sid}", headers=H).status_code in (200, 204)
    assert client.get("/v1/sessions", headers=H).json()["total"] == 0
    assert client.post(f"/v1/trash/{sid}/restore", headers=H).status_code == 200
    assert _detail(client, sid)["segments"]


def test_a_timed_transcript_can_be_split_like_any_meeting(client):
    text = "\n".join(f"[00:{m:02d}:00] Jane: minute {m} says something and then some more words" for m in range(0, 50))
    body = _post(client, name="Long call", text=text).json()
    assert body["format"] == "timestamped" and body["duration_sec"] > 2900
    split = client.post(f"/v1/sessions/{body['session_id']}/split", headers=H, json={"points": [1500]})
    assert split.status_code == 200, split.text
    parts = split.json()["parts"]
    assert [p["name"] for p in parts] == ["Long call (part 1)", "Long call (part 2)"]
    assert all(p["has_audio"] is False and p["segments"] > 5 for p in parts)


def test_home_page_has_the_transcript_form_and_posts_to_the_endpoint(client):
    page = client.get("/", headers=H).text
    assert 'id="tab-transcript"' in page and 'id="transcript-upload"' in page
    assert 'id="transcript-file"' in page and 'id="transcript-text"' in page and 'id="transcript-when"' in page
    assert "/v1/sessions/transcript" in page
    assert 'id="recording-upload"' in page  # the audio form is still there
