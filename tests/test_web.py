"""Tests for the web UI and its JSON API (meeting_notes.server.web / the
HTML + /v1/sessions + /v1/settings routes registered in app.py).

Follows test_server.py's own conventions: TestClient drives the whole app
in-process, StubTranscriber stands in for faster-whisper, and every test
gets an isolated data root via ``tmp_path``.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Dict, List

import pytest
from fastapi.testclient import TestClient

from meeting_notes import wire
from meeting_notes.server import auth
from meeting_notes.server import settings as settings_mod
from meeting_notes.server.app import create_app
from meeting_notes.transcribe.protocol import Segment

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated:DeprecationWarning"
)


class StubTranscriber:
    def __init__(self, segments_by_track: Dict[str, List[Segment]]):
        self.segments_by_track = segments_by_track

    def transcribe(self, wav_path: Path, track: str) -> List[Segment]:
        return list(self.segments_by_track.get(track, ()))


def make_app(tmp_path, *, transcriber_factory=None):
    return create_app(transcriber_factory=transcriber_factory, data_root=str(tmp_path / "data"))


def wait_for_job_state(client: TestClient, job_id: str, state: str, *, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    last = {}
    while time.monotonic() < deadline:
        resp = client.get(wire.job_path(job_id))
        assert resp.status_code == 200
        last = resp.json()
        if last["state"] in (state, wire.JobState.ERROR):
            return last
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} never reached {state!r}, last seen: {last}")


def _upload_finalize_and_wait(client: TestClient, session_id: str, *, pcm=None, meta=None) -> str:
    pcm = pcm if pcm is not None else b"\x00\x00" * wire.STREAM_SAMPLE_RATE
    resp = client.post(
        wire.track_upload_path(session_id, "mic"),
        content=pcm,
        headers={"Content-Type": "application/octet-stream", "X-Frames": str(len(pcm) // 2)},
    )
    assert resp.status_code == 200, resp.text

    frames = len(pcm) // 2
    timing = {
        "mic": [
            {"event": "open", "segment": 0, "frames": 0, "t": 1000.0, "wall": 0.0,
             "samplerate": wire.STREAM_SAMPLE_RATE, "channels": 1, "device": "mic"},
            {"event": "close", "segment": 0, "frames": frames, "t": 1000.0 + frames / wire.STREAM_SAMPLE_RATE},
        ]
    }
    body = {
        "meta": meta or {"created": "2026-09-20", "name": session_id, "tracks": {"mic": {}}},
        "timing": timing,
        "settings": {},
    }
    resp = client.post(wire.finalize_path(session_id), json=body)
    assert resp.status_code == 200, resp.text
    job_id = resp.json()["job_id"]
    wait_for_job_state(client, job_id, wire.JobState.DONE)
    return job_id


# -- sessions list: HTML shell + JSON API --------------------------------


def test_sessions_page_renders_when_empty(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = make_app(tmp_path)
    client = TestClient(app)

    resp = client.get("/")
    assert resp.status_code == 200
    assert "Sessions" in resp.text
    # A thin shell: no session-specific markup server-side, just the script
    # that will fetch and render it.
    assert "/v1/sessions" in resp.text


def test_v1_sessions_lists_populated_sessions_newest_first(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    segments = {"mic": [Segment(start=0.0, end=1.0, text="Hello there", track="mic")]}
    app = make_app(tmp_path, transcriber_factory=lambda **_kw: StubTranscriber(segments))
    client = TestClient(app)

    _upload_finalize_and_wait(client, "sess-a")
    time.sleep(0.01)
    _upload_finalize_and_wait(client, "sess-b")

    resp = client.get("/v1/sessions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert [item["session_id"] for item in body["items"]] == ["sess-b", "sess-a"]
    assert body["items"][0]["latest_state"] == wire.JobState.DONE
    assert body["items"][0]["has_audio"] is True


def test_session_created_prefers_the_recorders_epoch_over_its_local_iso_time(tmp_path, monkeypatch):
    """Seen on a real run: the recorder writes ``created`` as its LOCAL wall
    time with no zone, and parsing that on a UTC server put every session
    four hours early. ``started_wall`` is a real epoch and must win."""
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = make_app(tmp_path, transcriber_factory=lambda **_kw: StubTranscriber({}))
    client = TestClient(app)

    epoch = 1_789_954_782.0  # what the recorder's clock actually said
    _upload_finalize_and_wait(
        client,
        "sess-local",
        meta={"created": "2026-09-20T21:39:42", "started_wall": epoch, "name": "x", "tracks": {"mic": {}}},
    )

    item = client.get("/v1/sessions").json()["items"][0]
    assert item["created"] == epoch


def test_v1_sessions_pagination_and_search(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    segments = {"mic": [Segment(start=0.0, end=1.0, text="budget review", track="mic")]}
    app = make_app(tmp_path, transcriber_factory=lambda **_kw: StubTranscriber(segments))
    client = TestClient(app)

    for i in range(3):
        _upload_finalize_and_wait(client, f"sess-{i}")

    resp = client.get("/v1/sessions", params={"page": 1, "per_page": 2})
    body = resp.json()
    assert body["total"] == 3
    assert len(body["items"]) == 2

    resp = client.get("/v1/sessions", params={"q": "budget"})
    body = resp.json()
    assert body["total"] == 3  # all three transcripts mention "budget review"

    resp = client.get("/v1/sessions", params={"q": "nonexistent-xyz"})
    assert resp.json()["total"] == 0


# -- transcript detail ----------------------------------------------------


def test_v1_session_detail_includes_meta_jobs_and_segments(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    segments = {"mic": [Segment(start=0.0, end=1.0, text="Hello there", track="mic")]}
    app = make_app(tmp_path, transcriber_factory=lambda **_kw: StubTranscriber(segments))
    client = TestClient(app)
    _upload_finalize_and_wait(client, "sess-a")

    resp = client.get("/v1/sessions/sess-a")
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == "sess-a"
    assert body["meta"]["name"] == "sess-a"
    assert len(body["jobs"]) == 1
    assert body["jobs"][0]["state"] == wire.JobState.DONE
    assert body["segments"][0]["text"] == "Hello there"
    assert body["has_audio"] is True


def test_v1_session_detail_404s_for_unknown_session(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = make_app(tmp_path)
    client = TestClient(app)
    assert client.get("/v1/sessions/does-not-exist").status_code == 404


def test_session_detail_page_renders_the_shell(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = make_app(tmp_path)
    client = TestClient(app)
    resp = client.get("/sessions/sess-a")
    assert resp.status_code == 200
    assert "/v1/sessions/" in resp.text


# -- downloads --------------------------------------------------------------


def test_download_transcript_md_and_json(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    segments = {"mic": [Segment(start=0.0, end=1.0, text="Hello there", track="mic")]}
    app = make_app(tmp_path, transcriber_factory=lambda **_kw: StubTranscriber(segments))
    client = TestClient(app)
    _upload_finalize_and_wait(client, "sess-a")

    resp = client.get("/sessions/sess-a/transcript.md")
    assert resp.status_code == 200
    assert "Hello there" in resp.text
    assert "attachment" in resp.headers["content-disposition"]

    resp = client.get("/sessions/sess-a/transcript.json")
    assert resp.status_code == 200
    payload = json.loads(resp.text)
    assert payload["segments"][0]["text"] == "Hello there"


def test_download_before_any_completed_job_404s(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = make_app(tmp_path)
    client = TestClient(app)
    assert client.get("/sessions/does-not-exist/transcript.md").status_code == 404


# -- delete-audio / delete / retranscribe ------------------------------------


def test_delete_audio_keeps_session_json_and_transcript(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    segments = {"mic": [Segment(start=0.0, end=1.0, text="Hello there", track="mic")]}
    app = make_app(tmp_path, transcriber_factory=lambda **_kw: StubTranscriber(segments))
    client = TestClient(app)
    job_id = _upload_finalize_and_wait(client, "sess-a")

    store = app.state.store
    assert store.track_wav_path("sess-a", "mic").exists()

    resp = client.post("/sessions/sess-a/delete-audio", follow_redirects=False)
    assert resp.status_code in (302, 303)

    assert not store.track_wav_path("sess-a", "mic").exists()
    assert not store.track_raw_path("sess-a", "mic").exists()
    assert store.session_meta_path("sess-a").exists()
    assert store.read_transcript(job_id) is not None


def test_delete_session_removes_jobs_and_transcripts(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    segments = {"mic": [Segment(start=0.0, end=1.0, text="Hello there", track="mic")]}
    app = make_app(tmp_path, transcriber_factory=lambda **_kw: StubTranscriber(segments))
    client = TestClient(app)
    job_id = _upload_finalize_and_wait(client, "sess-a")

    resp = client.post("/sessions/sess-a/delete", follow_redirects=False)
    assert resp.status_code in (302, 303)

    store = app.state.store
    assert not store.session_dir("sess-a").exists()
    assert store.read_job(job_id) is None
    assert store.read_transcript(job_id) is None
    assert client.get("/v1/sessions/sess-a").status_code == 404


def test_retranscribe_enqueues_a_new_job(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    segments = {"mic": [Segment(start=0.0, end=1.0, text="Hello there", track="mic")]}
    app = make_app(tmp_path, transcriber_factory=lambda **_kw: StubTranscriber(segments))
    client = TestClient(app)
    _upload_finalize_and_wait(client, "sess-a")

    resp = client.post("/sessions/sess-a/retranscribe", follow_redirects=False)
    assert resp.status_code in (302, 303)

    store = app.state.store
    jobs = store.jobs_for_session("sess-a")
    assert len(jobs) == 2


def test_retranscribe_without_audio_is_rejected(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    segments = {"mic": [Segment(start=0.0, end=1.0, text="Hello there", track="mic")]}
    app = make_app(tmp_path, transcriber_factory=lambda **_kw: StubTranscriber(segments))
    client = TestClient(app)
    _upload_finalize_and_wait(client, "sess-a")
    client.post("/sessions/sess-a/delete-audio")

    resp = client.post("/sessions/sess-a/retranscribe")
    assert resp.status_code == 400


# -- login / cookie auth -------------------------------------------------


def test_html_route_redirects_to_login_when_token_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_NOTES_TOKEN", "s3cret")
    app = make_app(tmp_path)
    client = TestClient(app)

    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_login_sets_cookie_and_grants_access(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_NOTES_TOKEN", "s3cret")
    app = make_app(tmp_path)
    client = TestClient(app)

    resp = client.post("/login", data={"token": "wrong"}, follow_redirects=False)
    assert resp.status_code == 303
    assert "error" in resp.headers["location"]

    resp = client.post("/login", data={"token": "s3cret"}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    assert auth.WEB_TOKEN_COOKIE in resp.cookies

    # The cookie set by login now grants access to the HTML pages...
    resp = client.get("/")
    assert resp.status_code == 200

    # ...and to the JSON API too, with no Authorization header at all.
    resp = client.get("/v1/sessions")
    assert resp.status_code == 200


def test_logout_clears_the_cookie(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_NOTES_TOKEN", "s3cret")
    app = make_app(tmp_path)
    client = TestClient(app)
    client.post("/login", data={"token": "s3cret"})
    assert client.get("/").status_code == 200

    client.post("/logout")
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303


def test_no_token_configured_leaves_html_routes_open(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = make_app(tmp_path)
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "No MEETING_NOTES_TOKEN" in resp.text


# -- settings -----------------------------------------------------------


def test_settings_page_get_renders_current_values(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    monkeypatch.setenv("MEETING_NOTES_MODEL", "base.en")
    app = make_app(tmp_path)
    client = TestClient(app)
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert "base.en" in resp.text


def test_settings_post_round_trips_and_persists(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = make_app(tmp_path)
    client = TestClient(app)

    resp = client.post(
        "/settings",
        data={
            "model": "small.en",
            "beam_size": "3",
            "audio_retention_days": "7",
            "retention_check_interval_minutes": "30",
            # delete_audio_only_after_success omitted -> unchecked -> False
        },
    )
    assert resp.status_code == 200
    assert "Settings saved" in resp.text

    saved = settings_mod.load_settings(app.state.store.root)
    assert saved.model == "small.en"
    assert saved.beam_size == 3
    assert saved.audio_retention_days == 7
    assert saved.retention_check_interval_minutes == 30
    assert saved.delete_audio_only_after_success is False


def test_settings_post_rejects_invalid_values(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = make_app(tmp_path)
    client = TestClient(app)

    resp = client.post(
        "/settings",
        data={
            "model": "small.en",
            "beam_size": "not-a-number",
            "audio_retention_days": "7",
            "retention_check_interval_minutes": "30",
        },
    )
    assert resp.status_code == 200
    assert "must be a whole number" in resp.text


def test_v1_settings_get_and_put_round_trip(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = make_app(tmp_path)
    client = TestClient(app)

    resp = client.get("/v1/settings")
    assert resp.status_code == 200
    assert "model_choices" in resp.json()

    resp = client.put(
        "/v1/settings",
        json={
            "model": "large-v3-turbo",
            "beam_size": 2,
            "audio_retention_days": 0,
            "delete_audio_only_after_success": True,
            "retention_check_interval_minutes": 15,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["model"] == "large-v3-turbo"

    resp = client.get("/v1/settings")
    assert resp.json()["model"] == "large-v3-turbo"


def test_v1_settings_put_rejects_invalid_payload(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = make_app(tmp_path)
    client = TestClient(app)
    resp = client.put("/v1/settings", json={"model": "", "beam_size": 5,
                                             "audio_retention_days": -1,
                                             "retention_check_interval_minutes": 60,
                                             "delete_audio_only_after_success": True})
    assert resp.status_code == 400


def test_model_change_resets_live_preview_transcriber_and_is_reported_by_health(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    monkeypatch.setenv("MEETING_NOTES_MODEL", "base.en")
    app = make_app(tmp_path)  # no explicit transcriber_factory -> settings-driven
    client = TestClient(app)

    resp = client.get(wire.HEALTH)
    assert resp.json()["model"] == "base.en"

    live_preview = app.state.live_preview
    # Force the cache to look "populated" so we can prove reset clears it.
    live_preview._transcriber = object()

    resp = client.put(
        "/v1/settings",
        json={
            "model": "small.en",
            "beam_size": 5,
            "audio_retention_days": -1,
            "delete_audio_only_after_success": True,
            "retention_check_interval_minutes": 60,
        },
    )
    assert resp.status_code == 200

    assert live_preview._transcriber is None
    resp = client.get(wire.HEALTH)
    assert resp.json()["model"] == "small.en"


# -- JSON API auth --------------------------------------------------------


def test_v1_sessions_requires_bearer_auth_when_token_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_NOTES_TOKEN", "s3cret")
    app = make_app(tmp_path)
    client = TestClient(app)

    assert client.get("/v1/sessions").status_code == 401
    assert client.get("/v1/sessions", headers={"Authorization": "Bearer wrong"}).status_code == 403
    assert client.get("/v1/sessions", headers={"Authorization": "Bearer s3cret"}).status_code == 200
