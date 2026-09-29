"""Tests for the web UI and its JSON API (meeting_notes.server.web / the
HTML + /v1/sessions + /v1/settings routes registered in app.py).

Follows test_server.py's own conventions: TestClient drives the whole app
in-process, StubTranscriber stands in for faster-whisper, and every test
gets an isolated data root via ``tmp_path``.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Dict, List

import pytest
from fastapi.testclient import TestClient

from meeting_notes import __version__, wire
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
    assert "Recent meetings" in resp.text
    # A thin shell: no session-specific markup server-side, just the script
    # that will fetch and render it.
    assert "/v1/sessions" in resp.text


def test_sidebar_pages_and_installer_are_rendered(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = make_app(tmp_path)
    client = TestClient(app)

    home = client.get("/")
    assert "Home" in home.text
    assert "Meetings" in home.text
    assert "Saved transcriptions" not in home.text
    assert "No meetings yet" in home.text
    assert 'href="/install"' in home.text
    assert "/v1/live" in home.text

    saved = client.get("/transcriptions")
    assert saved.status_code == 200
    assert '<ul class="mlist"' in saved.text
    assert "detail-overlay" in saved.text
    assert "Delete meeting" in saved.text


def test_home_live_cards_open_accessible_scrollable_overlay(tmp_path, monkeypatch):
    """The live preview is an interactive reader, not just a clipped card.

    Keep this contract test close to the web shell: browser-level tests exercise
    the same behaviour, but these attributes are easy to accidentally remove
    during a markup refactor.
    """
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    from meeting_notes.server.web import render_home_page

    home = render_home_page(token_configured=False)
    assert 'role="button" tabindex="0" data-live-id=' in home
    assert 'id="live-overlay" role="dialog" aria-modal="true"' in home
    assert 'id="live-transcript-scroll" tabindex="0"' in home
    assert "event.key === 'Enter'" in home
    assert "event.key === 'Escape'" in home
    assert "updateLiveOverlay();" in home
    assert 'id="live-name-form"' in home
    assert "PATCH" in home
    from meeting_notes.server.web import stylesheet_text

    assert "main { max-width:1240px; margin:0 auto" in stylesheet_text()
    assert 'href="/static/app.css?v=' in home


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


def test_audio_can_be_played_from_the_full_screen_detail_view(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = make_app(tmp_path, transcriber_factory=lambda **_kw: StubTranscriber({}))
    client = TestClient(app)
    _upload_finalize_and_wait(client, "sess-a", pcm=b"\x01\x02" * 100)

    resp = client.get("/sessions/sess-a/audio/mic")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("audio/wav")
    assert resp.content.startswith(b"RIFF")


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


def test_delete_session_moves_jobs_and_transcripts_to_trash(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    segments = {"mic": [Segment(start=0.0, end=1.0, text="Hello there", track="mic")]}
    app = make_app(tmp_path, transcriber_factory=lambda **_kw: StubTranscriber(segments))
    client = TestClient(app)
    job_id = _upload_finalize_and_wait(client, "sess-a")

    resp = client.post("/sessions/sess-a/delete", follow_redirects=False)
    assert resp.status_code in (302, 303)

    # Soft delete: the meeting leaves the live data, but jobs and transcripts
    # are kept in Recently deleted (see tests/test_trash.py for the round trip).
    store = app.state.store
    assert not store.session_dir("sess-a").exists()
    assert store.read_job(job_id) is None
    assert store.is_trashed("sess-a")
    assert (store.trash_dir / "sess-a" / "jobs" / f"{job_id}.transcript.json").is_file()
    assert client.get("/v1/sessions/sess-a").status_code == 404
    store.purge_trashed("sess-a")
    assert not store.is_trashed("sess-a")


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


def test_json_session_actions_support_the_desktop_and_overlay(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = make_app(tmp_path, transcriber_factory=lambda **_kw: StubTranscriber({}))
    client = TestClient(app)
    _upload_finalize_and_wait(client, "sess-a")

    queued = client.post("/v1/sessions/sess-a/retranscribe")
    assert queued.status_code == 200
    assert queued.json()["job_id"]

    deleted_audio = client.post("/v1/sessions/sess-a/delete-audio")
    assert deleted_audio.status_code == 200
    assert deleted_audio.json()["bytes_freed"] > 0

    deleted = client.delete("/v1/sessions/sess-a")
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True


def test_installer_embeds_saved_server_address(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = make_app(tmp_path)
    client = TestClient(app)
    settings = settings_mod.Settings(model="base.en", server_address="http://notes.lan:8000")
    settings_mod.save_settings(app.state.store.root, settings)

    resp = client.get("/install/client-agent.ps1")
    assert resp.status_code == 200
    assert "attachment" in resp.headers["content-disposition"]
    assert 'http://notes.lan:8000' in resp.text
    assert '.meeting-notes' in resp.text
    assert '#Requires -Version 5.1' in resp.text
    assert 'MeetingNotes.exe' in resp.text
    assert 'How to run Meeting Notes.txt' in resp.text
    assert 'GetFolderPath("Programs")' in resp.text
    assert 'GetFolderPath("Desktop")' in resp.text
    assert 'IconLocation = "$exe,0"' in resp.text
    assert 'New-Item -ItemType Directory -Path $desktopDir -Force' in resp.text
    assert 'Writing client configuration without replacing existing secrets' in resp.text
    assert '[IO.File]::WriteAllText' in resp.text
    assert 'pip install' not in resp.text
    assert 'ProgramFiles' not in resp.text
    assert 'New-Service' not in resp.text
    assert 'HKLM:' not in resp.text
    assert '-Verb RunAs' not in resp.text
    assert 'per-user install' in resp.text
    assert 'github.com' not in resp.text.lower()
    assert 'client-manifest.json' in resp.text
    assert 'Get-FileHash' in resp.text


def test_client_manifest_and_package_are_public_with_token(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_NOTES_TOKEN", "s3cret")
    app = make_app(tmp_path)
    package = Path(app.state.store.root) / "client" / "MeetingNotes-Windows.zip"
    package.parent.mkdir(parents=True)
    payload = b"test windows package"
    package.write_bytes(payload)
    client = TestClient(app)

    manifest = client.get("/install/client-manifest.json")
    assert manifest.status_code == 200
    body = manifest.json()
    assert body["url"] == "http://testserver/install/MeetingNotes-Windows.zip"
    assert body["sha256"] == hashlib.sha256(payload).hexdigest()
    assert body["size"] == len(payload)
    assert body["version"] == __version__
    installer = client.get(
        "/install/client-agent.ps1", headers={"Authorization": "Bearer s3cret"}
    )
    assert body["installer"]["url"] == "http://testserver/install/client-agent.ps1"
    assert body["installer"]["size"] == len(installer.content)
    assert body["installer"]["sha256"] == hashlib.sha256(installer.content).hexdigest()
    downloaded = client.get("/install/MeetingNotes-Windows.zip")
    assert downloaded.status_code == 200
    assert downloaded.content == payload


def test_client_manifest_and_package_404_when_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_NOTES_TOKEN", "s3cret")
    client = TestClient(make_app(tmp_path))
    assert client.get("/install/client-manifest.json").status_code == 404
    assert client.get("/install/MeetingNotes-Windows.zip").status_code == 404


def test_install_guide_explains_dependencies_launch_and_first_run(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = make_app(tmp_path)
    client = TestClient(app)
    settings = settings_mod.Settings(model="base.en", server_address="http://meeting.lan")
    settings_mod.save_settings(app.state.store.root, settings)

    resp = client.get("/install")

    assert resp.status_code == 200
    assert "self-contained" in resp.text
    assert "Python, Qt, NumPy" in resp.text
    assert "administrator" in resp.text
    assert "PowerShell 5.1" in resp.text
    assert "Start Menu" in resp.text
    assert "server token" in resp.text
    assert "http://meeting.lan" in resp.text
    assert '/install/client-agent.ps1' in resp.text


def test_install_guide_requires_login_but_bootstrap_scripts_are_public(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_NOTES_TOKEN", "s3cret")
    app = make_app(tmp_path)
    client = TestClient(app)

    guide = client.get("/install", follow_redirects=False)
    assert guide.status_code == 303
    assert guide.headers["location"] == "/login"

    installer = client.get("/install/client-agent.ps1", follow_redirects=False)
    uninstaller = client.get("/install/uninstall-client.ps1", follow_redirects=False)
    assert installer.status_code == 200
    assert uninstaller.status_code == 200
    assert "MEETING_NOTES_TOKEN" not in installer.text
    assert "MEETING_NOTES_TOKEN" not in uninstaller.text


def test_uninstaller_is_authenticated_and_preserves_settings_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    client = TestClient(make_app(tmp_path))
    resp = client.get("/install/uninstall-client.ps1")
    assert resp.status_code == 200
    assert "RemoveSettings" in resp.text
    assert "LOCALAPPDATA" in resp.text
    assert "meeting-notes" in resp.text
    assert "Recordings" in resp.text
    assert "StartsWith($installDir" in resp.text
    assert 'GetFolderPath("Desktop")' in resp.text
    assert 'Meeting Notes.lnk' in resp.text
    assert "-Verb RunAs" not in resp.text


def test_server_address_can_be_bootstrapped_from_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_NOTES_SERVER_ADDRESS", "http://10.11.12.129:8000/")

    settings = settings_mod.load_settings(tmp_path)

    assert settings.server_address == "http://10.11.12.129:8000"


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


def test_ai_provider_settings_round_trip_and_validation(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = make_app(tmp_path)
    client = TestClient(app)
    payload = {
        "model": "base.en",
        "beam_size": 5,
        "audio_retention_days": -1,
        "delete_audio_only_after_success": True,
        "retention_check_interval_minutes": 60,
        "ai_provider": "ollama",
        "codex_model": "gpt-test-codex",
        "ollama_base_url": "http://ollama:11434/",
        "ollama_model": "llama3.2",
    }
    response = client.put("/v1/settings", json=payload)
    assert response.status_code == 200
    settings = response.json()
    assert settings["ai_provider"] == "ollama"
    assert settings["codex_model"] == "gpt-test-codex"
    assert settings["ollama_base_url"] == "http://ollama:11434"
    assert settings["ollama_model"] == "llama3.2"

    custom = "# Local meeting rules\nOnly use the transcript."
    assert client.put("/v1/settings", json={**payload, "ai_workflow": custom}).status_code == 200
    assert client.put("/v1/settings", json=payload).json()["ai_workflow"] == custom

    bad = {**payload, "ai_provider": "openai", "ollama_base_url": "not-a-url"}
    assert client.put("/v1/settings", json=bad).status_code == 400
    bad = {**payload, "ollama_base_url": "file:///etc/passwd"}
    assert client.put("/v1/settings", json=bad).status_code == 400


def test_ai_provider_settings_accept_claude(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    client = TestClient(make_app(tmp_path))
    payload = {
        "model": "base.en",
        "beam_size": 5,
        "audio_retention_days": -1,
        "delete_audio_only_after_success": True,
        "retention_check_interval_minutes": 60,
        "ai_provider": "claude",
        "claude_model": "sonnet",
    }
    response = client.put("/v1/settings", json=payload)
    assert response.status_code == 200
    settings = response.json()
    assert settings["ai_provider"] == "claude"
    assert settings["claude_model"] == "sonnet"

    bad = {**payload, "claude_model": "not a valid model!!"}
    assert client.put("/v1/settings", json=bad).status_code == 400


def test_settings_page_renders_ai_provider_controls(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = make_app(tmp_path)
    response = TestClient(app).get("/settings")
    assert response.status_code == 200
    assert 'name="ai_provider"' in response.text
    assert "Codex / ChatGPT" in response.text
    assert "Ollama (local)" in response.text
    assert 'name="ollama_base_url"' in response.text
    assert 'name="codex_model"' in response.text
    assert 'id="ollama-model"' in response.text
    assert 'id="codex-connect"' in response.text
    assert 'id="codex-device-code"' in response.text
    assert "/v1/bridge/control/status" in response.text
    assert 'if (provider === "codex") refreshCodexStatus();' in response.text
    assert "if (connected && !codexModelsLoaded) loadProviderModels" in response.text
    assert "if (!connected && codexWasConnected) codexModelsLoaded = false" in response.text
    assert "Claude (subscription)" in response.text
    assert 'id="claude-settings"' in response.text
    assert 'id="claude-connect"' in response.text
    assert 'id="claude-disconnect"' in response.text
    assert 'id="claude-login-code-input"' in response.text
    assert 'id="claude-code-submit"' in response.text
    assert 'name="claude_model"' in response.text
    assert "/v1/bridge/control/login/code" in response.text


def test_transcriptions_auto_refresh_does_not_discard_loaded_pages(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    response = TestClient(make_app(tmp_path)).get("/transcriptions")
    assert response.status_code == 200
    assert "if(!currentSession && listState.page===1)loadRows(true)" in response.text


def test_meeting_notes_route_uses_the_unified_transcriptions_ui(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    response = TestClient(make_app(tmp_path)).get("/meeting-notes", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/meetings"


def test_transcriptions_render_upload_failures_and_pipeline_percentages(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    response = TestClient(make_app(tmp_path)).get("/transcriptions")
    assert response.status_code == 200
    assert 'label:"Upload failed"' in response.text
    assert "status.detail || row.latest_error" in response.text
    assert "Math.round(rawPct)" in response.text
    assert "job.progress*100" in response.text


def test_bridge_control_is_proxied_without_exposing_bridge_token(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_NOTES_TOKEN", "server-secret")
    monkeypatch.setenv("MEETING_NOTES_BRIDGE_CONTROL_URL", "http://bridge:8765")
    seen = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"provider": "codex", "state": "authenticated", "authenticated": True}

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            assert kwargs["timeout"] == 30.0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def request(self, method, url, **kwargs):
            seen.append((method, url, kwargs))
            return FakeResponse()

    monkeypatch.setattr("meeting_notes.server.app.httpx.AsyncClient", FakeAsyncClient)
    client = TestClient(make_app(tmp_path))
    headers = {"Authorization": "Bearer server-secret"}
    status = client.get("/v1/bridge/control/status", headers=headers)
    assert status.status_code == 200
    login = client.post("/v1/bridge/control/login", headers=headers)
    assert login.status_code == 200
    models = client.get("/v1/ai/models?provider=codex", headers=headers)
    assert models.status_code == 200
    assert [call[:2] for call in seen] == [
        ("GET", "http://bridge:8765/v1/bridge/control/status?provider=codex"),
        ("POST", "http://bridge:8765/v1/bridge/control/login"),
        ("POST", "http://bridge:8765/v1/bridge/control/models"),
    ]
    assert all(call[2]["headers"] == headers for call in seen)
    assert seen[0][2]["json"] is None
    assert seen[1][2]["json"] == {"provider": "codex"}
    assert seen[2][2]["json"] == {"provider": "codex"}


def test_ai_model_discovery_validates_provider_and_ollama_url(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_NOTES_TOKEN", "server-secret")
    client = TestClient(make_app(tmp_path))
    headers = {"Authorization": "Bearer server-secret"}
    assert client.get("/v1/ai/models?provider=unknown", headers=headers).status_code == 400
    assert client.get(
        "/v1/ai/models?provider=ollama&ollama_base_url=file:///etc/passwd",
        headers=headers,
    ).status_code == 400


def test_ai_model_discovery_accepts_claude(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_NOTES_TOKEN", "server-secret")
    monkeypatch.setenv("MEETING_NOTES_BRIDGE_CONTROL_URL", "http://bridge:8765")

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"provider": "claude", "models": [{"id": "sonnet", "name": "Sonnet"}]}

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def request(self, method, url, **kwargs):
            return FakeResponse()

    monkeypatch.setattr("meeting_notes.server.app.httpx.AsyncClient", FakeAsyncClient)
    client = TestClient(make_app(tmp_path))
    headers = {"Authorization": "Bearer server-secret"}
    response = client.get("/v1/ai/models?provider=claude", headers=headers)
    assert response.status_code == 200
    assert response.json()["models"][0]["id"] == "sonnet"


def test_bridge_control_login_requires_matching_provider_selected(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_NOTES_TOKEN", "server-secret")
    app = make_app(tmp_path)
    client = TestClient(app)
    headers = {"Authorization": "Bearer server-secret"}

    # Default provider is codex; a login-code submission is claude-only.
    response = client.post("/v1/bridge/control/login/code", json={"code": "abc"}, headers=headers)
    assert response.status_code == 409

    # Switch to ollama, which has neither a login nor a login code.
    client.put("/v1/settings", json={
        "model": "base.en", "beam_size": 5, "audio_retention_days": -1,
        "delete_audio_only_after_success": True, "retention_check_interval_minutes": 60,
        "ai_provider": "ollama", "ollama_base_url": "http://ollama:11434", "ollama_model": "llama3.2",
    }, headers=headers)
    assert client.post("/v1/bridge/control/login", headers=headers).status_code == 409
    assert client.post("/v1/bridge/control/logout", headers=headers).status_code == 409
    assert client.post(
        "/v1/bridge/control/login/code", json={"code": "abc"}, headers=headers
    ).status_code == 409


def test_bridge_control_login_code_is_proxied_when_claude_is_selected(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_NOTES_TOKEN", "server-secret")
    monkeypatch.setenv("MEETING_NOTES_BRIDGE_CONTROL_URL", "http://bridge:8765")
    app = make_app(tmp_path)
    client = TestClient(app)
    headers = {"Authorization": "Bearer server-secret"}
    assert client.put("/v1/settings", json={
        "model": "base.en", "beam_size": 5, "audio_retention_days": -1,
        "delete_audio_only_after_success": True, "retention_check_interval_minutes": 60,
        "ai_provider": "claude",
    }, headers=headers).status_code == 200

    seen = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"provider": "claude", "state": "awaiting_user", "authenticated": False}

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def request(self, method, url, **kwargs):
            seen.append((method, url, kwargs.get("json")))
            return FakeResponse()

    monkeypatch.setattr("meeting_notes.server.app.httpx.AsyncClient", FakeAsyncClient)
    response = client.post("/v1/bridge/control/login/code", json={"code": "the-code"}, headers=headers)
    assert response.status_code == 200
    assert seen == [("POST", "http://bridge:8765/v1/bridge/control/login/code",
                      {"provider": "claude", "code": "the-code"})]

    # An empty code is rejected before ever reaching the bridge.
    seen.clear()
    assert client.post("/v1/bridge/control/login/code", json={"code": "  "}, headers=headers).status_code == 400
    assert seen == []


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
