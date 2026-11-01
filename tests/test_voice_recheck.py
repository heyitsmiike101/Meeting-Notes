"""Nightly voice recheck: which meetings it looks at, the schedule, the worker, settings and the API."""

from __future__ import annotations

import dataclasses
import time

import pytest
from fastapi.testclient import TestClient

from meeting_notes.server import people
from meeting_notes.server import settings as settings_mod
from meeting_notes.server import voice_recheck
from meeting_notes.server.app import create_app
from meeting_notes.server.store import Store
from tests.test_people import appearance, basis, enable, make_session, mix, record

DAY = 86400.0


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "data"))
    enable(s)
    return s


def meeting(store, sid, spec, days_ago):
    make_session(store, sid, created=iso(time.time() - days_ago * DAY))
    record(store, sid, spec)


# -- which meetings ---------------------------------------------------------------------------------------------


def test_candidates_are_recent_meetings_with_an_unnamed_or_suggested_voice(store):
    meeting(store, "recent", {"A": ("Them 1", basis(0), 30)}, 2)
    meeting(store, "old", {"A": ("Them 1", basis(1), 30)}, 40)
    meeting(store, "short", {"A": ("Them 1", basis(2), 3)}, 1)       # too little speech to link at all
    meeting(store, "asked", {"A": ("Them 1", mix(0, 3, 0.6), 30)}, 3)  # "sounds like Person 1?"
    assert appearance(store, "asked", "Them 1")["link"] == "suggested"
    since = time.time() - 30 * DAY
    assert people.recheck_candidates(store, since) == ["recent", "asked"]
    assert set(people.recheck_candidates(store, 0)) == {"recent", "old", "asked"}


def test_named_and_confirmed_voices_are_not_rechecked(store):
    meeting(store, "named", {"A": ("Them 1", basis(0), 30)}, 1)
    meeting(store, "confirmed", {"A": ("Them 1", basis(1), 30)}, 1)
    people.update_person(store.root, appearance(store, "named", "Them 1")["person_id"], {"name": "Ann"})
    pid = appearance(store, "confirmed", "Them 1")["person_id"]
    people.appearance_action(store.root, "confirmed", "Them 1", "assign", pid)  # kept unnamed, but pinned by hand
    assert people.recheck_candidates(store, 0) == []


def test_trashed_meetings_are_left_out(store):
    meeting(store, "gone", {"A": ("Them 1", basis(0), 30)}, 1)
    store.trash_session("gone")
    assert people.recheck_candidates(store, 0) == []


def test_run_recheck_reprofiles_each_candidate_and_records_the_run(store, monkeypatch):
    meeting(store, "a", {"A": ("Them 1", basis(0), 30)}, 1)
    meeting(store, "b", {"A": ("Them 1", basis(1), 30)}, 5)
    meeting(store, "old", {"A": ("Them 1", basis(2), 30)}, 45)
    seen = []
    real = people.reprofile

    def spy(st, sid):
        seen.append(sid)
        out = real(st, sid)
        return {**out, "changed": 2} if sid == "b" else out

    monkeypatch.setattr(people, "reprofile", spy)
    settings = settings_mod.load_settings(store.root)
    result = voice_recheck.run_recheck(store, settings, trigger="manual")
    assert seen == ["a", "b"]
    assert (result["meetings"], result["changed_meetings"], result["changed_speakers"]) == (2, 1, 2)
    state = voice_recheck.read_state(store.root)
    assert state["last_run"] == result["ran"] and state["last_result"]["trigger"] == "manual"


def test_run_recheck_on_real_data_keeps_the_links(store):
    meeting(store, "a", {"A": ("Them 1", basis(0), 30)}, 1)
    before = appearance(store, "a", "Them 1")["person_id"]
    result = voice_recheck.run_recheck(store, settings_mod.load_settings(store.root))
    assert result["meetings"] == 1 and result["changed_meetings"] == 0 and result["failed"] == 0
    assert appearance(store, "a", "Them 1")["person_id"] == before


# -- the schedule -----------------------------------------------------------------------------------------------


def at(day_offset: int, hour: int, minute: int = 0) -> float:
    base = time.localtime()
    return time.mktime((base.tm_year, base.tm_mon, base.tm_mday + day_offset, hour, minute, 0, 0, 0, -1))


def test_last_and_next_scheduled():
    assert voice_recheck.last_scheduled(2, at(0, 3)) == at(0, 2)
    assert voice_recheck.last_scheduled(2, at(0, 1)) == at(-1, 2)
    assert voice_recheck.next_scheduled(2, at(0, 1)) == at(0, 2)
    assert voice_recheck.next_scheduled(2, at(0, 3)) == at(1, 2)


def test_is_due_once_per_night():
    assert voice_recheck.is_due(2, at(-1, 2, 5), at(0, 2, 1))      # ran last night, tonight's hour has come
    assert not voice_recheck.is_due(2, at(0, 2, 1), at(0, 9))      # already ran tonight
    assert not voice_recheck.is_due(2, at(0, 1), at(0, 1, 30))     # tonight's hour not reached yet
    assert voice_recheck.is_due(2, at(-3, 2, 1), at(0, 0, 30))     # server was down: catch up


def test_worker_arms_first_then_runs_at_the_hour(store):
    meeting(store, "a", {"A": ("Them 1", basis(0), 30)}, 1)
    worker = voice_recheck.VoiceRecheckWorker(store)
    hour = settings_mod.load_settings(store.root).voice_recheck_hour
    armed_at = at(0, hour) - 3600
    assert worker._tick(armed_at) is None                               # first start: arm, don't run
    assert voice_recheck.read_state(store.root) == {"armed": armed_at}
    assert worker._tick(at(0, hour) - 60) is None                       # still before the hour
    assert worker._tick(at(0, hour) + 60)["meetings"] == 1              # the hour came
    assert worker._tick(at(0, hour) + 120) is None                      # once a night


def test_worker_does_nothing_while_off(store):
    current = settings_mod.load_settings(store.root)
    settings_mod.save_settings(store.root, dataclasses.replace(current, voice_recheck_enabled=False))
    assert voice_recheck.VoiceRecheckWorker(store)._tick() is None
    assert voice_recheck.read_state(store.root) == {}


# -- settings ---------------------------------------------------------------------------------------------------


def test_settings_defaults_and_validation():
    s = settings_mod.Settings()
    assert (s.voice_recheck_enabled, s.voice_recheck_hour, s.voice_recheck_days) == (True, 2, 30)
    base = settings_mod.Settings().to_dict()
    base.update(model="small.en", audio_retention_days=30, retention_check_interval_minutes=60)
    for key in ("voice_recheck_enabled", "voice_recheck_hour", "voice_recheck_days"):
        base.pop(key)
    got = settings_mod.validate(base)
    assert (got.voice_recheck_enabled, got.voice_recheck_hour, got.voice_recheck_days) == (True, 2, 30)
    got = settings_mod.validate({**base, "voice_recheck_enabled": "0", "voice_recheck_hour": "23",
                                 "voice_recheck_days": "90"})
    assert (got.voice_recheck_enabled, got.voice_recheck_hour, got.voice_recheck_days) == (False, 23, 90)
    for bad in ({"voice_recheck_hour": "24"}, {"voice_recheck_hour": "-1"}, {"voice_recheck_days": "0"},
                {"voice_recheck_days": "366"}):
        with pytest.raises(settings_mod.ValidationError):
            settings_mod.validate({**base, **bad})


# -- HTTP -------------------------------------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_NOTES_TOKEN", "")
    app = create_app(data_root=str(tmp_path / "data"))
    with TestClient(app) as c:
        yield c, app.state.store


def test_api_status_and_run_now(client):
    c, store = client
    assert c.post("/v1/voice-recheck").status_code == 409                  # voice profiles off
    enable(store)
    meeting(store, "a", {"A": ("Them 1", basis(0), 30)}, 1)
    status = c.get("/v1/voice-recheck").json()
    assert status["enabled"] and status["days"] == 30 and status["next_run"] and status["last_run"] is None
    ran = c.post("/v1/voice-recheck").json()
    assert ran["meetings"] == 1 and ran["trigger"] == "manual"
    assert c.get("/v1/voice-recheck").json()["last_result"]["meetings"] == 1


def test_settings_page_shows_the_schedule_and_saves_keep_it(client):
    c, store = client
    enable(store)
    page = c.get("/settings").text
    assert 'name="voice_recheck_hour"' in page and 'name="voice_recheck_days"' in page and "voice-recheck-now" in page
    current = {**c.get("/v1/settings").json(), "model": "small.en"}
    c.put("/v1/settings", json={**current, "voice_recheck_hour": 4, "voice_recheck_days": 14}).raise_for_status()
    current = c.get("/v1/settings").json()
    payload = {k: v for k, v in current.items() if not k.startswith("voice_recheck")}
    c.put("/v1/settings", json=payload).raise_for_status()                  # an older caller leaves them alone
    after = settings_mod.load_settings(store.root)
    assert (after.voice_recheck_hour, after.voice_recheck_days, after.voice_recheck_enabled) == (4, 14, True)
