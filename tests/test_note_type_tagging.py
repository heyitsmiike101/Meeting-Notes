"""A recorder tags a meeting with a note type (``meta.note_type``): the server then writes notes of that type.

Covers the server rule in ``JobQueue._maybe_auto_queue_review`` (a valid tag always gets notes, even with
"auto-generate notes" off; an unknown or missing tag keeps the old behaviour; no AI provider means no notes),
that finalize keeps the field and tolerates unknown meta keys, and the ``caps`` the Recorders list carries.
"""

from __future__ import annotations

import logging
import wave

import pytest
from fastapi.testclient import TestClient

from meeting_notes import wire
from meeting_notes.server import settings as settings_mod
from meeting_notes.server import store as store_mod
from meeting_notes.server.jobs import JobQueue
from meeting_notes.transcribe.protocol import Segment
from tests.test_server import StubTranscriber, _upload_and_finalize, make_app, silence_pcm, wait_for_job_state

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated:DeprecationWarning"
)


def _fields(**overrides):
    base = {
        "model": "base.en",
        "beam_size": "5",
        "audio_retention_days": "-1",
        "retention_check_interval_minutes": "60",
    }
    base.update(overrides)
    return base


def _transcribed(tmp_path, meta_extra=None, **settings):
    """Run one meeting through the job worker with the given server settings; returns (store, reviews)."""
    store = store_mod.Store(str(tmp_path / "data"))
    settings_mod.save_settings(store.root, settings_mod.validate(_fields(**settings)))
    meta = {"created": "2026-09-20", "tracks": {"mic": {}}}
    meta.update(meta_extra or {})
    store.write_session_meta("s1", meta)
    wav_path = store.track_wav_path("s1", "mic")
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(wav_path), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(wire.STREAM_SAMPLE_RATE)
        fh.writeframes(b"\x00\x00" * wire.STREAM_SAMPLE_RATE)

    class Stub:
        def transcribe(self, wav_path, track):
            return [Segment(start=0.0, end=1.0, text="hi", track=track)]

    queue = JobQueue(store, lambda **_kw: Stub())
    queue._process(store.create_job("s1"))
    return store, store.list_reviews(session_id="s1")


def test_a_tagged_meeting_gets_notes_even_with_auto_generate_off(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="meeting_notes.server.jobs")
    _, reviews = _transcribed(tmp_path, {"note_type": "webinar"}, ai_provider="claude")
    (review,) = reviews
    assert (review["template_id"], review["template_name"]) == ("webinar", "Detailed webinar")
    assert "note type webinar" in caplog.text and "recorder chose" in caplog.text


def test_the_tag_beats_the_servers_default_type(tmp_path):
    _, reviews = _transcribed(
        tmp_path, {"note_type": "quick"}, ai_provider="claude", auto_generate_notes="on", default_template_id="webinar"
    )
    (review,) = reviews
    assert review["template_id"] == "quick"


def test_a_tag_that_is_a_note_type_name_still_resolves(tmp_path):
    _, reviews = _transcribed(tmp_path, {"note_type": "Quick notes"}, ai_provider="claude")
    assert [r["template_id"] for r in reviews] == ["quick"]


@pytest.mark.parametrize("tag", ["no-such-type", "", 7, None, ["quick"]])
def test_an_unknown_or_malformed_tag_keeps_the_old_behaviour_with_auto_off(tmp_path, tag):
    _, reviews = _transcribed(tmp_path, {"note_type": tag}, ai_provider="claude")
    assert reviews == []


def test_an_unknown_tag_with_auto_on_gets_the_default_type(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="meeting_notes.server.jobs")
    _, reviews = _transcribed(
        tmp_path, {"note_type": "gone"}, ai_provider="claude", auto_generate_notes="on", default_template_id="webinar"
    )
    (review,) = reviews
    assert review["template_id"] == "webinar"
    assert "auto-generate notes is on" in caplog.text


def test_an_untagged_meeting_follows_the_auto_generate_setting(tmp_path):
    assert _transcribed(tmp_path / "off", ai_provider="claude")[1] == []
    (review,) = _transcribed(tmp_path / "on", ai_provider="claude", auto_generate_notes="on")[1]
    assert review["template_id"] == "standard"


@pytest.mark.parametrize("auto", ["off", "on"])
def test_no_ai_provider_means_no_notes_even_when_tagged(tmp_path, auto):
    kw = {"auto_generate_notes": "on"} if auto == "on" else {}
    _, reviews = _transcribed(tmp_path, {"note_type": "quick"}, ai_provider="disabled", **kw)
    assert reviews == []


# -- through the real finalize route --------------------------------------------


def _finalize_with_meta(client, session_id, meta):
    pcm = silence_pcm(1.0)
    client.post(
        wire.track_upload_path(session_id, "mic"),
        content=pcm,
        headers={"Content-Type": "application/octet-stream", "X-Frames": str(len(pcm) // 2)},
    )
    body = {"meta": {"created": "2026-09-20", "tracks": {"mic": {}}, **meta}, "timing": {}, "settings": {}}
    resp = client.post(wire.finalize_path(session_id), json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()["job_id"]


def test_finalize_keeps_note_type_and_unknown_meta_keys_and_notes_follow(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    stub = StubTranscriber({"mic": [Segment(start=0.0, end=1.0, text="hello there", track="mic")]})
    app = make_app(tmp_path, transcriber_factory=lambda **_kw: stub)
    with TestClient(app) as client:
        job_id = _finalize_with_meta(client, "tagged", {"note_type": "quick", "from_the_future": {"x": 1}})
        assert wait_for_job_state(client, job_id, "done")["state"] == "done"
        meta = app.state.store.read_session_meta("tagged")
        assert meta["note_type"] == "quick" and meta["from_the_future"] == {"x": 1}
        (review,) = app.state.store.list_reviews(session_id="tagged")
        assert review["template_id"] == "quick"  # auto-generate is off by default: the tag alone asked for notes

        # an old recorder sends no note_type: nothing changes for it
        job_id = _finalize_with_meta(client, "untagged", {})
        assert wait_for_job_state(client, job_id, "done")["state"] == "done"
        assert "note_type" not in app.state.store.read_session_meta("untagged")
        assert app.state.store.list_reviews(session_id="untagged") == []


def test_a_resent_finalize_keeps_the_tag(tmp_path, monkeypatch):
    """A re-upload of a saved recording sends session.json's meta again: same tag, and still just the one review."""
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    stub = StubTranscriber({"mic": [Segment(start=0.0, end=1.0, text="hello there", track="mic")]})
    app = make_app(tmp_path, transcriber_factory=lambda **_kw: stub)
    with TestClient(app) as client:
        job_id = _finalize_with_meta(client, "again", {"note_type": "webinar"})
        wait_for_job_state(client, job_id, "done")
        again = _finalize_with_meta(client, "again", {"note_type": "webinar"})
        wait_for_job_state(client, again, "done")  # the audio went up again, so this is a fresh transcription
        assert app.state.store.latest_done_job("again")["job_id"] == again
        assert app.state.store.read_session_meta("again")["note_type"] == "webinar"
        assert len(app.state.store.list_reviews(session_id="again")) == 1


# -- the Recorders list carries each recorder's capabilities --------------------------


def test_the_recorder_item_lists_its_caps():
    from meeting_notes import remote
    from meeting_notes.server.recorders import _Recorder

    rec = _Recorder(
        None, instance_id="a" * 32, device="PC", platform_text="Windows 11", version="0.7.10", address="",
        state=remote.sanitize_state({}), now=0.0, caps=remote.clean_caps(["idle_levels", "note_type", "junk"]),
    )
    assert rec.item()["caps"] == ["idle_levels", "note_type"]
    old = _Recorder(None, instance_id="b" * 32, device="PC", platform_text="Windows 11", version="0.7.5", address="",
                    state=remote.sanitize_state({}), now=0.0)
    assert old.item()["caps"] == []
