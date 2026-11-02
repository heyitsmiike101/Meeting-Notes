"""Archive named people and forget unnamed voices over time (people.cleanup, run with the nightly voice recheck)."""

from __future__ import annotations

import dataclasses
import time

import pytest
from fastapi.testclient import TestClient

from meeting_notes.server import people
from meeting_notes.server import settings as settings_mod
from meeting_notes.server import voice_recheck
from meeting_notes.server.app import create_app
from meeting_notes.server.people import TrackSpeakers
from meeting_notes.server.store import Store
from meeting_notes.transcribe.diarize import Diarization
from tests.test_people import appearance, basis, enable, make_session, mix, record, turn, vp, write_wav

DAY = 86400.0


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "data"))
    enable(s)
    return s


def meeting(store, sid, spec, days_ago, **kwargs):
    make_session(store, sid, created=iso(time.time() - days_ago * DAY))
    record(store, sid, spec, **kwargs)


def cfg(store, archive=180, forget=90):
    return dataclasses.replace(settings_mod.load_settings(store.root), people_archive_days=archive,
                               people_forget_days=forget)


def named(store, sid, label, name):
    pid = appearance(store, sid, label)["person_id"]
    people.update_person(store.root, pid, {"name": name})
    return pid


def person(store, pid):
    return next((p for p in people._load(store.root)["people"] if p["id"] == pid), None)


# -- archive ----------------------------------------------------------------------------------------------------


def test_named_person_not_heard_in_the_window_is_archived(store):
    meeting(store, "old", {"A": ("Them 1", basis(0), 30)}, 200)
    meeting(store, "new", {"A": ("Them 1", basis(1), 30)}, 10)
    ann, bob = named(store, "old", "Them 1", "Ann"), named(store, "new", "Them 1", "Bob")
    out = people.cleanup(store, cfg(store))
    assert out["archived"] == [{"id": ann, "name": "Ann"}] and out["restored"] == [] and out["forgotten"] == 0
    assert person(store, ann)["archived"]["auto"] is True and person(store, ann)["archived"]["at"] > 0
    assert "archived" not in person(store, bob)
    assert {p["id"]: p["archived"] is not None for p in people.list_people(store)} == {ann: True, bob: False}


def test_nobody_is_archived_inside_the_window(store):
    meeting(store, "m", {"A": ("Them 1", basis(0), 30)}, 179)
    ann = named(store, "m", "Them 1", "Ann")
    assert people.cleanup(store, cfg(store))["archived"] == []
    assert "archived" not in person(store, ann)


def test_a_trashed_meeting_does_not_count_as_heard(store):
    meeting(store, "old", {"A": ("Them 1", basis(0), 30)}, 200)
    meeting(store, "recent", {"A": ("Them 1", basis(0), 30)}, 2)
    ann = named(store, "old", "Them 1", "Ann")
    assert appearance(store, "recent", "Them 1")["person_id"] == ann
    assert people.cleanup(store, cfg(store))["archived"] == []
    store.trash_session("recent")
    assert [a["id"] for a in people.cleanup(store, cfg(store))["archived"]] == [ann]


def test_you_are_never_archived(store):
    make_session(store, "mine", created=iso(time.time() - 400 * DAY))
    ts = TrackSpeakers(Diarization([turn(0, 30, "A")], {"A": basis(0)}), {}, self_raw="A")
    people.record_meeting(store, "mine", "j1", {"mic": ts})
    me = next(p for p in people._load(store.root)["people"] if p["is_self"])
    people.update_person(store.root, me["id"], {"name": "Mike"})
    out = people.cleanup(store, cfg(store))
    assert out["archived"] == [] and "archived" not in person(store, me["id"])
    with pytest.raises(ValueError):
        people.set_archived(store.root, me["id"], True)


def test_archived_people_still_match_voices_and_come_back_when_heard(store):
    meeting(store, "old", {"A": ("Them 1", basis(0), 30)}, 200)
    ann = named(store, "old", "Them 1", "Ann")
    people.cleanup(store, cfg(store))
    assert person(store, ann)["archived"]
    data = people._load(store.root)
    assert ann in people._models(people._visible(store), data)         # still part of the matching
    meeting(store, "again", {"X": ("Them 1", mix(0, 1, 0.8), 30)}, 0)
    assert appearance(store, "again", "Them 1")["person_id"] == ann
    assert "archived" not in person(store, ann)                         # heard in a new meeting: restored


def test_cleanup_restores_anyone_heard_since_they_were_archived(store):
    meeting(store, "m", {"A": ("Them 1", basis(0), 30)}, 5)
    ann = named(store, "m", "Them 1", "Ann")
    people.set_archived(store.root, ann, True, now=time.time() - 400 * DAY)   # archived long before that meeting
    out = people.cleanup(store, cfg(store))
    assert out["restored"] == [{"id": ann, "name": "Ann"}] and "archived" not in person(store, ann)


def test_a_manual_archive_survives_the_cleanup_until_they_are_heard_again(store):
    meeting(store, "m", {"A": ("Them 1", basis(0), 30)}, 5)
    ann = named(store, "m", "Them 1", "Ann")
    people.set_archived(store.root, ann, True)
    out = people.cleanup(store, cfg(store))
    assert out["restored"] == [] and person(store, ann)["archived"]["auto"] is False
    meeting(store, "next", {"A": ("Them 1", basis(0), 30)}, 0)
    assert "archived" not in person(store, ann)


def test_confirming_a_voice_for_an_archived_person_restores_them(store):
    meeting(store, "old", {"A": ("Them 1", basis(0), 30)}, 200)
    ann = named(store, "old", "Them 1", "Ann")
    people.cleanup(store, cfg(store))
    meeting(store, "other", {"A": ("Them 1", basis(5), 30)}, 1)
    people.appearance_action(store.root, "other", "Them 1", "assign", ann)
    assert "archived" not in person(store, ann)


def test_archive_days_zero_never_archives(store):
    meeting(store, "old", {"A": ("Them 1", basis(0), 30)}, 900)
    ann = named(store, "old", "Them 1", "Ann")
    assert people.cleanup(store, cfg(store, archive=0))["archived"] == []
    assert "archived" not in person(store, ann)


# -- forget -----------------------------------------------------------------------------------------------------


def test_an_old_unnamed_voice_is_forgotten_with_its_clips_and_the_rest_of_the_meeting_stays(store, tmp_path):
    wav = tmp_path / "system.wav"
    write_wav(wav, 120)
    meeting(store, "m", {"A": ("Them 1", basis(0), 30), "B": ("Them 2", basis(1), 30)}, 100,
            wav_paths={"system": wav})
    meeting(store, "other", {"A": ("Them 1", basis(0), 30)}, 100)    # the same unnamed voice elsewhere
    bea = named(store, "m", "Them 2", "Bea")
    gone = appearance(store, "m", "Them 1")["person_id"]
    clips = store.root / "people" / "clips" / "m"
    assert any(n.startswith("them-1-") for n in (p.name for p in clips.iterdir()))
    out = people.cleanup(store, cfg(store))
    assert out["forgotten"] == 1 and out["archived"] == []
    assert [a["label"] for a in vp(store, "m")["speakers"]] == ["Them 2"]
    assert [p.name for p in clips.iterdir() if p.name.startswith("them-1-")] == []
    assert any(p.name.startswith("them-2-") for p in clips.iterdir())      # the other speaker's samples stay
    assert not (store.root / "people" / "voiceprints" / "other.json").exists()   # nothing left in that one
    assert person(store, gone) is None and person(store, bea)["name"] == "Bea"
    shown = people.overlay(store.root, "m")
    assert "Them 1" not in shown and shown["Them 2"]["display"] == "Bea"   # the transcript label shows again
    assert store.session_exists("m") and store.session_exists("other")


def test_a_recently_heard_or_named_or_detailed_unnamed_voice_is_kept(store):
    meeting(store, "m", {"A": ("Them 1", basis(0), 30), "B": ("Them 2", basis(1), 30), "C": ("Them 3", basis(2), 30)}, 100)
    meeting(store, "recent", {"A": ("Them 1", basis(0), 30)}, 5)
    c = appearance(store, "m", "Them 3")["person_id"]
    people.update_person(store.root, c, {"email": "c@example.com"})
    out = people.cleanup(store, cfg(store, archive=0))
    assert out["forgotten"] == 1                                            # only Them 2
    assert sorted(a["label"] for a in vp(store, "m")["speakers"]) == ["Them 1", "Them 3"]


def test_a_voice_with_a_question_pending_is_kept(store):
    meeting(store, "intro", {"A": ("Them 1", basis(0), 30)}, 100)
    meeting(store, "first", {"A": ("Them 1", basis(1), 30)}, 150)
    meeting(store, "second", {"A": ("Them 1", mix(1, 4, 0.6), 30)}, 120)    # "sounds like" the one in "first"
    assert appearance(store, "second", "Them 1")["link"] == "suggested"
    row = vp(store, "intro")
    row["speakers"][0]["intro_suggestion"] = {"name": "Dana", "company": "", "quote": "I'm Dana"}
    people._put_vp(store.root, row)
    out = people.cleanup(store, cfg(store))
    # the introduction question, the suggestion itself, and the person the suggestion points at all stay
    assert out["forgotten"] == 0
    assert {sid: len(vp(store, sid)["speakers"]) for sid in ("intro", "first", "second")} == {"intro": 1, "first": 1, "second": 1}


def test_a_voice_archived_by_hand_is_not_forgotten(store):
    meeting(store, "m", {"A": ("Them 1", basis(0), 30)}, 100)
    pid = appearance(store, "m", "Them 1")["person_id"]
    people.set_archived(store.root, pid, True)
    assert people.cleanup(store, cfg(store))["forgotten"] == 0 and person(store, pid)


def test_forget_days_zero_never_forgets(store):
    meeting(store, "m", {"A": ("Them 1", basis(0), 30)}, 900)
    assert people.cleanup(store, cfg(store, forget=0))["forgotten"] == 0
    assert len(vp(store, "m")["speakers"]) == 1


# -- the result ---------------------------------------------------------------------------------------------------


def test_the_last_cleanup_is_recorded_only_when_something_happened(store):
    meeting(store, "old", {"A": ("Them 1", basis(0), 30)}, 200)
    meeting(store, "m", {"A": ("Them 1", basis(3), 30)}, 100)
    ann = named(store, "old", "Them 1", "Ann")
    assert people.last_cleanup(store.root) is None
    assert people.cleanup(store, cfg(store, archive=0, forget=0))["archived"] == []
    assert people.last_cleanup(store.root) is None
    out = people.cleanup(store, cfg(store), now=1_800_000_000.0)
    saved = people.last_cleanup(store.root)
    assert saved == out and saved["at"] == 1_800_000_000.0
    assert saved["archived"] == [{"id": ann, "name": "Ann"}] and saved["forgotten"] == 1
    people.cleanup(store, cfg(store))                                       # a quiet night keeps the last news
    assert people.last_cleanup(store.root) == saved


def test_the_recheck_runs_the_cleanup_and_reports_it(store):
    meeting(store, "old", {"A": ("Them 1", basis(0), 30)}, 200)
    ann = named(store, "old", "Them 1", "Ann")
    result = voice_recheck.run_recheck(store, cfg(store), trigger="manual")
    assert result["cleanup"]["archived"] == [{"id": ann, "name": "Ann"}]
    status = voice_recheck.status(store.root, cfg(store))
    assert status["last_cleanup"]["archived"] == [{"id": ann, "name": "Ann"}]
    selected = voice_recheck.run_recheck(store, cfg(store), trigger="selected", session_ids=["old"])
    assert selected["cleanup"] is None


# -- settings -----------------------------------------------------------------------------------------------------


def test_settings_defaults_validation_and_keep_if_missing():
    defaults = settings_mod.Settings()
    assert (defaults.people_archive_days, defaults.people_forget_days) == (180, 90)
    base = settings_mod.Settings().to_dict()
    base.update(model="small.en", audio_retention_days=30, retention_check_interval_minutes=60)
    got = settings_mod.validate({**base, "people_archive_days": "0", "people_forget_days": "3650"})
    assert (got.people_archive_days, got.people_forget_days) == (0, 3650)
    for bad in ({"people_archive_days": "-1"}, {"people_forget_days": "3651"}, {"people_archive_days": "x"}):
        with pytest.raises(settings_mod.ValidationError):
            settings_mod.validate({**base, **bad})
    stored = dataclasses.replace(defaults, people_archive_days=30, people_forget_days=7)
    kept = settings_mod.keep_stored({}, stored, form=False)
    assert (kept["people_archive_days"], kept["people_forget_days"]) == (30, 7)


# -- HTTP ---------------------------------------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_NOTES_TOKEN", "")
    app = create_app(data_root=str(tmp_path / "data"))
    with TestClient(app) as c:
        enable(app.state.store)
        yield c, app.state.store


def test_archive_and_restore_routes(client):
    c, store = client
    meeting(store, "m", {"A": ("Them 1", basis(0), 30)}, 3)
    ann = named(store, "m", "Them 1", "Ann")
    r = c.post(f"/v1/people/{ann}/archive", json={"archived": True})
    assert r.status_code == 200 and r.json()["archived"]["auto"] is False
    assert [p["archived"] is not None for p in c.get("/v1/people").json()["people"]] == [True]
    r = c.post(f"/v1/people/{ann}/archive", json={"archived": False})
    assert r.status_code == 200 and r.json()["archived"] is None
    assert c.post(f"/v1/people/{ann}/archive", json={"archived": "yes"}).status_code == 400
    assert c.post("/v1/people/p_unknown/archive", json={"archived": True}).status_code == 404


def test_archiving_you_is_refused(client):
    c, store = client
    make_session(store, "mine")
    ts = TrackSpeakers(Diarization([turn(0, 30, "A")], {"A": basis(0)}), {}, self_raw="A")
    people.record_meeting(store, "mine", "j1", {"mic": ts})
    me = next(p for p in people._load(store.root)["people"] if p["is_self"])
    r = c.post(f"/v1/people/{me['id']}/archive", json={"archived": True})
    assert r.status_code == 400 and "archived" not in person(store, me["id"])


def test_the_voice_recheck_status_and_pages_carry_the_cleanup(client):
    c, store = client
    meeting(store, "old", {"A": ("Them 1", basis(0), 30)}, 200)
    ann = named(store, "old", "Them 1", "Ann")
    ran = c.post("/v1/voice-recheck").json()
    assert ran["cleanup"]["archived"] == [{"id": ann, "name": "Ann"}]
    assert c.get("/v1/voice-recheck").json()["last_cleanup"]["archived"] == [{"id": ann, "name": "Ann"}]
    assert "people-view" in c.get("/people").text and "Last cleanup" in c.get("/people").text
    assert 'id="archive-person"' in c.get(f"/people/{ann}").text
    settings_page = c.get("/settings").text
    assert 'name="people_archive_days"' in settings_page and 'name="people_forget_days"' in settings_page
    c.put("/v1/settings", json={**c.get("/v1/settings").json(), "model": "small.en", "people_archive_days": 60}).raise_for_status()
    assert settings_mod.load_settings(store.root).people_archive_days == 60
