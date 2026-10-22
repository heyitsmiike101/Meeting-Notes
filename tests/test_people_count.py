"""Per-meeting people count: how many people are in the meeting, including the user.

It rides in the session metadata (``people_count``, 1-50), and the server uses it to pin the speaker count for
diarization: a call's system track holds everyone but the user, an in-person/phone recording everyone.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from meeting_notes import wire
from meeting_notes.server.jobs import JobQueue, Prepared, diarize_call, speaker_pin
from meeting_notes.transcribe.diarize import SpeakerTurn
from tests.compute_helpers import Rig
from tests.test_in_person_speakers import labels
from tests.test_server import make_app, silence_pcm


def _finalize(client, sid, meta_extra, tracks=("mic", "system")):
    for track in tracks:
        pcm = silence_pcm(10.0)
        resp = client.post(
            wire.track_upload_path(sid, track), content=pcm,
            headers={"Content-Type": "application/octet-stream", "X-Frames": str(len(pcm) // 2)})
        assert resp.status_code == 200, resp.text
    meta = {"created": "2026-10-22", "tracks": {t: {} for t in tracks}, **meta_extra}
    resp = client.post(wire.finalize_path(sid), json={"meta": meta, "timing": {}, "settings": {}})
    assert resp.status_code == 200, resp.text
    return resp.json()["job_id"]


# -- the pin rule ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("meta, expected", [
    ({}, (False, None)),
    ({"people_count": None}, (False, None)),
    ({"people_count": 0}, (False, None)),
    ({"people_count": 51}, (False, None)),
    ({"people_count": "3"}, (False, None)),
    ({"people_count": 1}, (True, None)),   # just you: nothing to separate
    ({"people_count": 2}, (True, None)),   # one other person: one speaker
    ({"people_count": 3}, (False, 2)),
    ({"people_count": 6}, (False, 5)),
    ({"people_count": 1, "in_person": True}, (True, None)),
    ({"people_count": 2, "in_person": True}, (False, 2)),
    ({"people_count": 6, "in_person": True}, (False, 6)),
])
def test_speaker_pin(meta, expected):
    assert speaker_pin(meta) == expected


def _capture(meta, **current):
    base = dict(model="small.en", beam_size=5, diarization_enabled=True, diarization_model="d",
                diarization_min_speakers=1, diarization_max_speakers=8, in_person_diarization="on")
    base.update(current)
    prepared = SimpleNamespace(settings={"transcriber": {"hotwords": None}}, tracks=["mic", "system"],
                               meta=meta, in_person=bool(meta.get("in_person")))
    return JobQueue.__new__(JobQueue)._capture_compute_settings(SimpleNamespace(**base), prepared)["diarization"]


@pytest.mark.parametrize("meta, enabled, low, high", [
    ({}, True, 1, 8),                          # unset: today's behaviour
    ({"people_count": 1}, False, 1, 8),        # one person: no speaker separation
    ({"people_count": 2}, False, 1, 8),        # a 1:1: the other side is one speaker
    ({"people_count": 6}, True, 5, 5),         # a call of six: five others
    ({"people_count": 4, "in_person": True}, True, 4, 4),
    ({"people_count": 1, "in_person": True}, False, 1, 8),
])
def test_helper_job_diarization_dict(meta, enabled, low, high):
    assert _capture(meta) == {"enabled": enabled, "model": "d", "min_speakers": low, "max_speakers": high}


def test_diarize_call_passes_the_pin_only_where_accepted():
    seen = {}

    class New:
        def diarize(self, wav, progress=None, min_speakers=None, max_speakers=None):
            seen["new"] = (min_speakers, max_speakers)
            return []

    class Old:
        def diarize(self, wav):
            seen["old"] = True
            return []

    diarize_call(New(), "x.wav", progress=lambda f: None, pin=3)
    diarize_call(Old(), "x.wav", progress=lambda f: None, pin=3)
    assert seen == {"new": (3, 3), "old": True}
    diarize_call(New(), "x.wav", pin=None)
    assert seen["new"] == (None, None)


# -- server CPU path -----------------------------------------------------------------------------------------


class _Diar:
    def __init__(self):
        self.calls = []

    def diarize(self, wav_path, progress=None, min_speakers=None, max_speakers=None):
        self.calls.append((min_speakers, max_speakers))
        return [SpeakerTurn(0.0, 1.0, "A")]


@pytest.mark.parametrize("people, in_person, expected_calls", [
    (None, False, [(None, None)]),
    (1, False, []),
    (2, False, []),
    (6, False, [(5, 5)]),
    (4, True, [(4, 4)]),
    (1, True, []),
])
def test_local_diarization_uses_the_people_count(tmp_path, monkeypatch, people, in_person, expected_calls):
    diar = _Diar()
    rig = Rig(tmp_path, monkeypatch, offload=False, diarizer_factory=lambda: diar)
    meta = {} if people is None else {"people_count": people}
    if in_person:
        meta.update(in_person=True, source="mobile")
    job = rig.wait_state(_finalize(rig.client, "pc", meta, tracks=("system",) if in_person else ("mic", "system")))
    assert job["state"] == wire.JobState.DONE
    assert diar.calls == expected_calls
    if not expected_calls:  # one speaker: the track keeps its plain label
        assert "Them" in labels(rig, job["job_id"]) or "Speaker" in labels(rig, job["job_id"])


def test_helper_offer_carries_the_pin(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, diarization_enabled=True)
    assert rig.poll().status_code == 204
    job_id = _finalize(rig.client, "pc1", {"people_count": 3})
    rig.wait_compute(job_id, "offered")
    diar = rig.job(job_id)["compute"]["settings"]["diarization"]
    assert (diar["enabled"], diar["min_speakers"], diar["max_speakers"]) == (True, 2, 2)


# -- metadata round trip ---------------------------------------------------------------------------------------


def test_finalize_stores_the_people_count(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = make_app(tmp_path)
    client = TestClient(app)
    _finalize(client, "f1", {"people_count": 3})
    assert app.state.store.read_session_meta("f1")["people_count"] == 3
    _finalize(client, "f2", {"people_count": 99})  # unusable: automatic, never a failed finalize
    assert "people_count" not in app.state.store.read_session_meta("f2")
    _finalize(client, "f3", {})
    assert "people_count" not in app.state.store.read_session_meta("f3")


def test_live_people_count_edit_lands_in_the_finalized_meta(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = make_app(tmp_path)
    client = TestClient(app)
    hello = wire.Hello(session_id="live-pc", name="Untitled", tracks=["mic", "system"])
    with client.websocket_connect(wire.STREAM) as ws:
        ws.send_json(wire.to_json(hello))
        resp = client.patch("/v1/live/live-pc", json={"people_count": 4})
        assert resp.status_code == 200 and resp.json()["people_count"] == 4
        assert client.get("/v1/live").json()["items"][0]["people_count"] == 4
        assert client.patch("/v1/live/live-pc", json={"people_count": 0}).status_code == 400
        assert app.state.store.read_session_meta("live-pc") == {}
    _finalize(client, "live-pc", {"people_count": 2})  # the live edit is the newer choice
    assert app.state.store.read_session_meta("live-pc")["people_count"] == 4


def test_saved_meeting_people_count_api(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = make_app(tmp_path)
    client = TestClient(app)
    _finalize(client, "api1", {"people_count": 3})
    url = "/v1/sessions/api1"
    assert client.patch(url, json={"people_count": 5}).json()["people_count"] == 5
    assert client.get(url).json()["meta"]["people_count"] == 5
    assert client.patch(url, json={"name": "Named", "people_count": 2}).status_code == 200
    meta = client.get(url).json()["meta"]
    assert (meta["name"], meta["people_count"]) == ("Named", 2)
    for bad in (0, 51, "3", 2.5, True, [], {}):
        assert client.patch(url, json={"people_count": bad}).status_code == 400, bad
    assert client.get(url).json()["meta"]["people_count"] == 2
    assert client.patch(url, json={"people_count": None}).status_code == 200  # clears
    assert "people_count" not in client.get(url).json()["meta"]
    assert client.patch(url, json={}).status_code == 400
    assert client.patch("/v1/sessions/missing", json={"people_count": 3}).status_code == 404
    # an edit made on the saved meeting beats a recorder's later finalize (e.g. a re-upload)
    client.patch(url, json={"people_count": 7})
    _finalize(client, "api1", {"people_count": 3})
    assert app.state.store.read_session_meta("api1")["people_count"] == 7


# -- web and mobile pages --------------------------------------------------------------------------------------


def test_meeting_page_has_the_people_control(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    client = TestClient(make_app(tmp_path))
    page = client.get("/meetings").text
    for needle in ('id="edit-people"', "Set number of people", 'id="people-form"', 'id="people-input"',
                   'id="overlay-people"', "Identify the speakers again?"):
        assert needle in page, needle


def test_mobile_recorder_page_has_the_people_input(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    client = TestClient(make_app(tmp_path))
    page = client.get("/record/").text
    assert 'id="mr-people"' in page and "People" in page
    js = client.get("/record/app.js").text
    assert "people_count" in js and "mr-people" in js


def test_mobile_session_carries_the_people_count(tmp_path):
    from tests.test_mobile_upload import Env

    env = Env(tmp_path)
    assert env.create("m-pc", people_count=3).status_code == 201
    assert env.store.read_session_meta("m-pc")["people_count"] == 3
    assert env.create("m-bad", people_count=500).status_code == 201
    assert "people_count" not in env.store.read_session_meta("m-bad")
