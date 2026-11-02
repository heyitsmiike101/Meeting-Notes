"""Name unnamed voices from introductions: the queue, the claim order, the checks on the answer, how a name is applied
(named / merged / "sounds like" / suggested), undo, and the bridge's third job kind."""

from __future__ import annotations

import dataclasses
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from meeting_notes import review_contract, wire
from meeting_notes.bridge import BridgeConfig, BridgeWorker, CLAIM_KINDS, INTRO_KIND
from meeting_notes.server import intro_names as intro
from meeting_notes.server import names as names_mod
from meeting_notes.server import people
from meeting_notes.server import settings as settings_mod
from meeting_notes.server.app import create_app
from meeting_notes.transcribe.diarize import Diarization
from tests.test_people import basis, make_session, speakers_for, turn
from tests.test_people_jobs import finalize, make_rig
from meeting_notes.server.people import TrackSpeakers

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated:DeprecationWarning"
)

TOKEN = "intro-secret"
HDR = {"Authorization": f"Bearer {TOKEN}"}
KINDS = "notes,split_suggestions,intro_names"
EXPIRED = "claude exited with status 1: Failed to authenticate: OAuth session expired and could not be refreshed"

HELLO = [
    (1.0, "Them 1", "Hi everyone, I'm Dana from Acme, glad to be here."),
    (6.0, "You", "Welcome Dana. And who else is on?"),
    (9.0, "Them 2", "Morning, this is the rest of the team."),
]


def wait_for(fn, timeout=5.0):
    """The transcript job is marked done just before the follow-up work is queued: poll instead of asserting."""
    deadline = time.monotonic() + timeout
    while True:
        value = fn()
        if value or time.monotonic() >= deadline:
            return value
        time.sleep(0.02)


class Env:
    def __init__(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MEETING_NOTES_TOKEN", TOKEN)
        self.app = create_app(data_root=str(tmp_path / "data"))
        self.client = TestClient(self.app)
        self.store = self.app.state.store
        self.root = self.store.root
        self.queue = self.app.state.intro_names
        self.rechecks = []
        monkeypatch.setattr(self.app.state.voice_recheck, "request_run", lambda trigger="person": self.rechecks.append(trigger))
        self.configure(voice_profiles_enabled=True, diarization_enabled=True, ai_provider="claude")

    def configure(self, **fields):
        current = settings_mod.load_settings(self.root)
        settings_mod.save_settings(self.root, dataclasses.replace(current, **fields))

    def meeting(self, sid, voices=("Them 1", "Them 2"), lines=HELLO, *, with_self=True, job="j1", first=0):
        """A finished meeting: voiceprints (one voice per label; "You" is the recorder) and a stored transcript.
        ``first``: the first embedding axis used, so two meetings can have voices that do not match."""
        if not self.store.session_exists(sid):
            make_session(self.store, sid)
        self.voices(sid, voices, with_self=with_self, job=job, first=first)
        return self.transcript(sid, lines)

    def voices(self, sid, voices, *, with_self=True, job="j1", first=0):
        spec = {f"S{first + i}": (label, basis(first + i), 30) for i, label in enumerate(voices)}
        speakers = {"system": speakers_for("system", spec)}
        if with_self:
            speakers["mic"] = TrackSpeakers(Diarization([turn(0, 30, "ME")], {"ME": basis(40)}), {}, self_raw="ME")
        people.record_meeting(self.store, sid, job, speakers)

    def transcript(self, sid, lines):
        job = self.store.create_job(sid)
        segs = [{"start": t, "end": t + 3, "track": "system", "label": label, "text": text, "in_gap": False}
                for t, label, text in lines]
        self.store.write_transcript(job, "# t", json.dumps({"segments": segs}))
        self.store.update_job(job, state=wire.JobState.DONE, progress=1.0)
        return job

    def person(self, sid, label):
        return people.overlay(self.root, sid)[label]

    def row(self, sid, label):
        vp = json.loads((self.root / "people" / "voiceprints" / f"{sid}.json").read_text(encoding="utf-8"))
        return next(a for a in vp["speakers"] if a["label"] == label)

    def claim(self, kinds=KINDS):
        return self.client.get(f"/v1/bridge/review/claim?worker_id=w&kinds={kinds}", headers=HDR)

    def complete(self, job_id, names):
        return self.client.post(f"/v1/bridge/intro-names/{job_id}/complete", headers=HDR, json={"names": names})

    def run_job(self, sid, tj, names):
        """Queue the meeting's job, claim it as a bridge would and post ``names``; the posted job's public form."""
        job = self.queue.enqueue(sid, tj)
        claimed = self.claim().json()
        assert claimed["id"] == job["job_id"] and claimed["kind"] == "intro_names"
        done = self.complete(claimed["id"], names)
        assert done.status_code == 200, done.text
        return done.json()


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


def entry(label="Them 1", name="Dana", quote="I'm Dana from Acme", confidence="high", company="Acme"):
    return {"label": label, "name": name, "company": company, "quote": quote, "confidence": confidence}


# -- when a job is queued ---------------------------------------------------------------------------------------------


def test_a_meeting_with_an_unnamed_voice_gets_a_job(env):
    tj = env.meeting("m1")
    job = intro.maybe_enqueue(env.queue, env.store, "m1", tj)
    assert job and job["status"] == "queued" and job["transcript_job_id"] == tj
    assert (env.root / "intro_names" / f"{job['job_id']}.json").is_file()
    assert intro.maybe_enqueue(env.queue, env.store, "m1", tj)["job_id"] == job["job_id"]  # the same transcript: one job


@pytest.mark.parametrize("fields", [
    {"intro_naming": False},
    {"voice_profiles_enabled": False},
    {"diarization_enabled": False},
    {"ai_provider": "disabled"},
])
def test_no_job_when_a_gate_is_off(env, fields):
    tj = env.meeting("m1")
    env.configure(**fields)
    assert intro.maybe_enqueue(env.queue, env.store, "m1", tj) is None
    assert not (env.root / "intro_names").exists()


def test_no_job_without_an_unnamed_voice(env):
    tj = env.meeting("m1")
    people.update_person(env.root, env.person("m1", "Them 1")["person_id"], {"name": "Dana"})
    people.update_person(env.root, env.person("m1", "Them 2")["person_id"], {"name": "Lee"})
    assert intro.maybe_enqueue(env.queue, env.store, "m1", tj) is None  # "You" is the recorder: never a candidate
    only_me = env.meeting("m2", voices=())
    assert intro.maybe_enqueue(env.queue, env.store, "m2", only_me) is None


def test_a_failure_while_queueing_never_escapes(env, monkeypatch):
    tj = env.meeting("m1")
    monkeypatch.setattr(env.queue, "enqueue", lambda *a: (_ for _ in ()).throw(OSError("disk")))
    assert intro.maybe_enqueue(env.queue, env.store, "m1", tj) is None


def test_unnamed_voices_are_the_candidates(env):
    env.meeting("m1", voices=("Them 1", "Them 2", "Them 3"))
    people.update_person(env.root, env.person("m1", "Them 2")["person_id"], {"name": "Lee Park"})
    assert people.intro_candidates(env.root, "m1") == {
        "unnamed": ["Them 1", "Them 3"], "named": {"Them 2": "Lee Park"}, "self": ["You"]}


def test_a_newer_transcript_supersedes_the_old_job(env):
    tj1 = env.meeting("m1")
    old = env.queue.enqueue("m1", tj1)
    tj2 = env.transcript("m1", HELLO)
    new = env.queue.enqueue("m1", tj2)
    assert new["job_id"] != old["job_id"]
    assert env.queue.read(old["job_id"])["status"] == "superseded"
    assert env.queue.latest("m1")["job_id"] == new["job_id"]
    assert env.claim().json()["id"] == new["job_id"]  # the old one is never claimed
    assert env.complete(old["job_id"], []).status_code == 409


def test_a_result_for_an_outdated_transcript_is_dropped(env):
    tj1 = env.meeting("m1")
    job = env.queue.enqueue("m1", tj1)
    assert env.claim().json()["id"] == job["job_id"]
    env.transcript("m1", HELLO)  # a re-run finished while the AI worked
    done = env.complete(job["job_id"], [entry()]).json()
    assert done["status"] == "done" and done["applied"] == []
    assert env.person("m1", "Them 1")["named"] is False


# -- what the AI reads ------------------------------------------------------------------------------------------------


def test_the_transcript_has_a_header_and_the_early_lines(env):
    tj = env.meeting("m1", voices=("Them 1", "Them 2", "Them 3"))
    people.update_person(env.root, env.person("m1", "Them 3")["person_id"], {"name": "Lee Park"})
    text = intro.transcript_text(env.store, "m1", tj)
    head, _, body = text.partition("# Transcript excerpt")
    assert "Unnamed voices (the only labels you may name): Them 1, Them 2" in head
    assert "Them 3 = Lee Park" in head and '"You" is the person who recorded the meeting' in head
    assert "[1.0s] Them 1: Hi everyone, I'm Dana from Acme" in body and "[6.0s] You: Welcome Dana" in body


def test_later_lines_that_look_like_introductions_are_added_and_the_rest_dropped(env):
    lines = HELLO + [
        (1200.0, "Them 1", "Let us talk about the budget."),
        (1210.0, "Them 2", "Sure, the numbers look fine."),
        (1500.0, "Them 2", "Before we go on, this is Priya, she runs ops."),
        (1503.0, "Them 1", "Hello all."),
        (2400.0, "Them 1", "Thanks, that is all."),
        (2403.0, "Them 2", "Bye."),
    ]
    tj = env.meeting("m1", lines=lines)
    text = intro.transcript_text(env.store, "m1", tj)
    assert "this is Priya, she runs ops" in text and "[1503.0s] Them 1: Hello all." in text  # the line and its neighbours
    assert "[1210.0s] Them 2: Sure, the numbers" in text  # the neighbour before the introduction line
    assert "budget" not in text and "Thanks, that is all" not in text


def test_the_excerpt_is_capped(env):
    lines = [(float(i), "Them 1", "word " * 150) for i in range(0, 800, 2)]  # ~800 chars each, all in the first 15 min
    tj = env.meeting("m1", lines=lines)
    body = intro.transcript_text(env.store, "m1", tj).partition("# Transcript excerpt")[2]
    assert len(body) <= intro.MAX_CHARS + 200
    assert "[0.0s]" in body and "[798.0s]" not in body


# -- claim order ------------------------------------------------------------------------------------------------------


def test_claim_order_is_split_then_notes_then_intro(env):
    tj = env.meeting("m1")
    env.configure(ai_provider="ollama")
    intro_job = env.queue.enqueue("m1", tj)
    review = env.store.create_review("m1")
    split_job = env.app.state.split_ai.enqueue("m1", tj)
    first = env.claim().json()
    assert first["kind"] == "split_suggestions" and first["id"] == split_job["job_id"]
    second = env.claim().json()
    assert second["id"] == review["review_id"] and "kind" not in second  # the queued notes come before intro naming
    third = env.claim().json()
    assert third["kind"] == "intro_names" and third["id"] == intro_job["job_id"] and third["session_id"] == "m1"
    assert third["transcript_url"] == f"/v1/bridge/intro-names/{intro_job['job_id']}/transcript"
    assert third["workflow_url"] == "/v1/bridge/intro-names/workflow.md"
    assert third["provider"]["name"] == "ollama"
    assert env.claim().status_code == 204


def test_a_bridge_that_does_not_ask_never_gets_an_intro_job(env):
    tj = env.meeting("m1")
    env.queue.enqueue("m1", tj)
    assert env.client.get("/v1/bridge/review/claim?worker_id=old", headers=HDR).status_code == 204
    assert env.claim("notes,split_suggestions").status_code == 204
    assert env.queue.latest("m1")["status"] == "queued"
    assert env.claim().status_code == 200


def test_disabled_provider_and_expired_login_hold_intro_jobs_back(env):
    tj = env.meeting("m1")
    job = env.queue.enqueue("m1", tj)
    env.app.state.ai_login_failed(EXPIRED)
    assert env.claim().status_code == 204
    from meeting_notes.server import ai_health
    ai_health.mark_ok(env.root, "claude")
    env.configure(ai_provider="disabled")
    assert env.claim().status_code == 204
    env.configure(ai_provider="claude")
    assert env.claim().json()["id"] == job["job_id"]


def test_a_stale_running_job_is_offered_again_and_a_deleted_meeting_is_dropped(env, monkeypatch):
    tj = env.meeting("m1")
    job = env.queue.enqueue("m1", tj)
    assert env.claim().status_code == 200
    assert env.claim().status_code == 204  # running
    monkeypatch.setattr(intro.IntroNamesQueue, "STALE_SECONDS", 0)
    assert env.claim().json()["id"] == job["job_id"]
    assert env.queue.read(job["job_id"])["status"] == "running"
    monkeypatch.setattr(env.store, "session_exists", lambda sid: False)
    assert env.claim().status_code == 204
    assert env.queue.read(job["job_id"])["status"] == "error"


def test_the_workflow_and_transcript_routes(env):
    tj = env.meeting("m1")
    job = env.queue.enqueue("m1", tj)
    workflow = env.client.get("/v1/bridge/intro-names/workflow.md", headers=HDR)
    assert workflow.status_code == 200 and "Never name \"You\"" in workflow.text and '"names"' in workflow.text
    text = env.client.get(f"/v1/bridge/intro-names/{job['job_id']}/transcript", headers=HDR)
    assert text.status_code == 200 and "Unnamed voices" in text.text and "Dana" in text.text
    assert env.client.get("/v1/bridge/intro-names/nope/transcript", headers=HDR).status_code == 404
    assert env.client.get(f"/v1/bridge/intro-names/{job['job_id']}/transcript").status_code in (401, 403)


def test_failure_marks_the_job_and_a_login_failure_holds_the_queue(env):
    tj = env.meeting("m1")
    job = env.queue.enqueue("m1", tj)
    env.claim()
    bad = env.client.post(f"/v1/bridge/intro-names/{job['job_id']}/failure", headers=HDR, json={"error": ""})
    assert bad.status_code == 400
    failed = env.client.post(f"/v1/bridge/intro-names/{job['job_id']}/failure", headers=HDR, json={"error": EXPIRED})
    assert failed.status_code == 200 and failed.json()["status"] == "error"
    from meeting_notes.server import ai_health
    assert ai_health.is_expired(env.root, "claude")
    again = env.client.post(f"/v1/bridge/intro-names/{job['job_id']}/failure", headers=HDR, json={"error": "x"})
    assert again.status_code == 409


# -- checks on the answer ---------------------------------------------------------------------------------------------


def test_bad_shapes_are_refused(env):
    tj = env.meeting("m1")
    job = env.queue.enqueue("m1", tj)
    env.claim()
    for body in ({"names": "x"}, {"names": [{"label": "Them 1"}]}, {"names": [entry(confidence="sure")]}, {"x": 1},
                 {"names": [{**entry(), "extra": 1}]}):
        resp = env.client.post(f"/v1/bridge/intro-names/{job['job_id']}/complete", headers=HDR, json=body)
        assert resp.status_code == 400, body
    assert env.queue.read(job["job_id"])["status"] == "running"


def test_validation_drops_entries_that_do_not_hold(env):
    tj = env.meeting("m1", voices=("Them 1", "Them 2", "Them 3"))
    people.update_person(env.root, env.person("m1", "Them 3")["person_id"], {"name": "Lee Park"})
    result = {"names": [
        entry(),                                                                    # fine
        entry(label="Them 2", name="Priya", quote="I'm Dana from Acme"),            # the name is not in the quote
        entry(label="You", name="Dana", quote="I'm Dana from Acme"),                # the recorder
        entry(label="Them 3", name="Dana", quote="I'm Dana from Acme"),             # already named
        entry(label="Them 9", name="Dana", quote="I'm Dana from Acme"),             # not a voice of this meeting
        entry(label="Them 2", name="Dana", quote="Dana said she was never here"),   # not in the transcript
        entry(label="Them 2", name="", quote="I'm Dana from Acme"),                 # empty name
        entry(label="Them 2", name="D" * 80, quote="I'm Dana from Acme"),           # too long
    ]}
    kept = intro.validate_names(env.store, "m1", tj, result)
    assert [(k["label"], k["name"]) for k in kept] == [("Them 1", "Dana")]


def test_validation_ignores_case_spacing_and_punctuation_and_keeps_one_entry_per_label(env):
    tj = env.meeting("m1")
    result = {"names": [
        entry(label="Them 1", name="  dana  ", quote="HI EVERYONE,   i'm dana from acme!!", company=" Acme  Corp "),
        entry(label="Them 1", name="Dana", quote="I'm Dana from Acme"),
        entry(label="Them 2", name="Rest", quote="this is the rest of the team", confidence="low", company=None),
    ]}
    kept = intro.validate_names(env.store, "m1", tj, result)
    assert [(k["label"], k["name"], k["company"], k["confidence"]) for k in kept] == [
        ("Them 1", "dana", "Acme Corp", "high"), ("Them 2", "Rest", "", "low")]


def test_a_quote_from_a_line_the_ai_never_saw_is_dropped(env):
    lines = HELLO + [(3000.0, "Them 2", "Funny story, I'm Priya from Globex and I left early.")]
    tj = env.meeting("m1", lines=lines)
    text = intro.transcript_text(env.store, "m1", tj)
    assert "Priya" in text  # this late line looks like an introduction, so it is in the excerpt
    kept = intro.validate_names(env.store, "m1", tj, {"names": [entry(label="Them 2", name="Priya", quote="I'm Priya from Globex")]})
    assert len(kept) == 1
    plain = HELLO + [(3000.0, "Them 2", "The totals were Priya Globex and then lunch.")]
    tj2 = env.transcript("m1", plain)
    assert not intro.validate_names(env.store, "m1", tj2, {"names": [entry(label="Them 2", name="Priya", quote="Priya Globex and then lunch")]})


# -- applying a name --------------------------------------------------------------------------------------------------


def test_a_sure_name_with_no_match_names_the_voice(env):
    tj = env.meeting("m1")
    done = env.run_job("m1", tj, [entry()])
    assert done["status"] == "done" and done["applied"][0]["outcome"] == "named"
    shown = env.person("m1", "Them 1")
    assert shown["display"] == "Dana" and shown["named"] and shown["company"] == "Acme"
    assert env.person("m1", "Them 2")["named"] is False
    person = people.get_person(env.store, shown["person_id"])
    (origin,) = person["named_from"]
    assert (origin["session_id"], origin["label"], origin["mode"]) == ("m1", "Them 1", "named")
    assert origin["quote"] == "I'm Dana from Acme"
    assert "Dana" in names_mod.all_names(env.root)  # a known name now
    assert env.rechecks == ["person"]  # other meetings are asked to match the voice again


def test_a_sure_name_that_is_a_person_without_a_voice_merges_into_them(env):
    dana = people.add_person(env.root, "Dana Reyes", "Acme Corp")
    tj = env.meeting("m1")
    p9 = env.person("m1", "Them 1")["person_id"]
    done = env.run_job("m1", tj, [entry(name="Dana Reyes", quote="I'm Dana from Acme")])
    assert done["applied"][0]["outcome"] == "merged" and done["applied"][0]["person_id"] == dana["id"]
    shown = env.person("m1", "Them 1")
    assert shown["person_id"] == dana["id"] and shown["display"] == "Dana Reyes" and shown["link"] == "confirmed"
    assert p9 not in {p["id"] for p in people.list_people(env.store)}  # the unnamed one is gone, Dana keeps her id
    assert people.get_person(env.store, dana["id"])["named_from"][0]["mode"] == "merged"
    assert env.rechecks == ["person"]


def test_a_first_name_alone_is_the_one_person_with_that_first_name(env):
    dana = people.add_person(env.root, "Dana Reyes", "Acme Corp")
    tj = env.meeting("m1")
    done = env.run_job("m1", tj, [entry(name="Dana", quote="I'm Dana from Acme")])
    assert done["applied"][0]["outcome"] == "merged" and done["applied"][0]["person_id"] == dana["id"]
    assert env.person("m1", "Them 1")["display"] == "Dana Reyes"


def test_a_first_name_two_people_share_is_only_a_suggestion(env):
    people.add_person(env.root, "Dana Reyes")
    people.add_person(env.root, "Dana Cole")
    tj = env.meeting("m1")
    p9 = env.person("m1", "Them 1")["person_id"]
    done = env.run_job("m1", tj, [entry(name="Dana", quote="I'm Dana from Acme")])
    assert done["applied"][0]["outcome"] == "intro_suggestion"
    shown = env.person("m1", "Them 1")
    assert shown["person_id"] == p9 and shown["named"] is False  # no third "Dana", nobody merged
    assert env.rechecks == []


def test_a_name_that_already_has_a_voice_becomes_a_sounds_like_question(env):
    env.meeting("old", voices=("Them 1",), lines=HELLO)
    dana = env.person("old", "Them 1")["person_id"]
    people.update_person(env.root, dana, {"name": "Dana Reyes"})
    tj = env.meeting("m1", job="j2", first=10)  # (other embeddings than "old", so no voice match)
    before = env.person("m1", "Them 1")["person_id"]
    done = env.run_job("m1", tj, [entry(name="Dana Reyes", quote="I'm Dana from Acme")])
    assert done["applied"][0]["outcome"] == "suggested"
    shown = env.person("m1", "Them 1")
    assert shown["person_id"] == before and shown["named"] is False  # nobody was merged or renamed
    assert shown["suggested_person_id"] == dana and shown["link"] == "suggested"
    assert people.suggestions(env.root, "m1")[0]["suggested_display"] == "Dana Reyes"
    assert env.rechecks == []  # nothing was named


def test_a_suggestion_the_user_already_rejected_is_not_asked_again(env):
    env.meeting("old", voices=("Them 1",))
    dana = env.person("old", "Them 1")["person_id"]
    people.update_person(env.root, dana, {"name": "Dana Reyes"})
    tj = env.meeting("m1", job="j2", first=10)
    with people._lock:  # the user said "not them" to Dana for this voice
        vp = people._vps(env.root)["m1"]
        next(a for a in vp["speakers"] if a["label"] == "Them 1")["rejected_person_ids"] = [dana]
        people._put_vp(env.root, vp)
    done = env.run_job("m1", tj, [entry(name="Dana Reyes", quote="I'm Dana from Acme")])
    assert done["applied"][0]["outcome"] == "skipped:not them"
    assert env.person("m1", "Them 1")["suggested_person_id"] is None


def test_the_recorders_own_voice_is_never_named(env):
    tj = env.meeting("m1")
    assert intro.validate_names(env.store, "m1", tj, {"names": [entry(label="You", quote="Welcome Dana")]}) == []
    # ... and even a direct call that reached the recorder's person changes nothing
    out = people.apply_intro_names(env.root, "m1", [{**entry(label="You"), "company": ""}])
    assert out[0]["outcome"] == "skipped:no person" and env.person("m1", "You")["display"] == "You"


def test_an_already_named_person_is_left_alone(env):
    env.meeting("m1")
    people.update_person(env.root, env.person("m1", "Them 1")["person_id"], {"name": "Lee Park"})
    out = people.apply_intro_names(env.root, "m1", [{**entry(), "company": ""}])
    assert out[0]["outcome"] == "skipped:already named" and env.person("m1", "Them 1")["display"] == "Lee Park"


def test_a_voice_the_user_confirmed_is_left_alone(env):
    env.meeting("m1")
    carol = people.add_person(env.root, "Carol Voiceless")
    people.appearance_action(env.root, "m1", "Them 1", "assign", carol["id"])
    people.update_person(env.root, carol["id"], {"name": ""})  # (an unnamed person, but the link is the user's)
    out = people.apply_intro_names(env.root, "m1", [{**entry(), "company": ""}])
    assert out[0]["outcome"] == "skipped:confirmed" and env.person("m1", "Them 1")["named"] is False


def test_the_meeting_notes_are_never_touched(env):
    tj = env.meeting("m1")
    review = env.store.create_review("m1")
    env.queue.enqueue("m1", tj)
    assert env.claim().json()["id"] == review["review_id"]  # the notes review comes first
    snapshot = env.store.read_review(review["review_id"])
    job = env.claim().json()
    assert job["kind"] == "intro_names" and env.complete(job["id"], [entry()]).status_code == 200
    assert env.store.read_review(review["review_id"]) == snapshot  # naming left the notes exactly as they were
    assert env.person("m1", "Them 1")["display"] == "Dana"


# -- low confidence: a suggestion the user answers ----------------------------------------------------------------------


def low(**over):
    return entry(confidence="low", **over)


def test_a_low_confidence_name_is_only_a_suggestion(env):
    tj = env.meeting("m1")
    done = env.run_job("m1", tj, [low()])
    assert done["applied"][0]["outcome"] == "intro_suggestion"
    shown = env.person("m1", "Them 1")
    assert shown["named"] is False
    assert env.row("m1", "Them 1")["intro_suggestion"] == {"name": "Dana", "company": "Acme", "quote": "I'm Dana from Acme"}
    person = people.get_person(env.store, shown["person_id"])
    assert person["intro_suggestion"]["name"] == "Dana" and person["intro_suggestion"]["session_id"] == "m1"
    assert [p for p in people.list_people(env.store) if p["id"] == shown["person_id"]][0]["intro_name"] == "Dana"
    assert env.rechecks == []


def post_intro(env, action, label="Them 1", sid="m1"):
    return env.client.post("/v1/people/appearances/intro", headers=HDR, json={"session_id": sid, "label": label, "action": action})


def test_use_this_name_names_the_person_and_records_where_it_came_from(env):
    tj = env.meeting("m1")
    env.run_job("m1", tj, [low()])
    resp = post_intro(env, "use")
    assert resp.status_code == 200 and resp.json()["outcome"] == "named"
    shown = env.person("m1", "Them 1")
    assert shown["display"] == "Dana" and shown["company"] == "Acme"
    assert "intro_suggestion" not in env.row("m1", "Them 1")
    assert people.get_person(env.store, shown["person_id"])["named_from"][0]["quote"] == "I'm Dana from Acme"
    assert env.rechecks == ["person"]
    assert post_intro(env, "use").status_code == 400  # nothing left to use


def test_not_them_dismisses_and_a_rerun_never_offers_it_again(env):
    tj = env.meeting("m1")
    env.run_job("m1", tj, [low()])
    assert post_intro(env, "dismiss").status_code == 200
    assert "intro_suggestion" not in env.row("m1", "Them 1") and env.row("m1", "Them 1")["rejected_intro_names"] == ["Dana"]
    assert env.rechecks == []
    tj2 = env.transcript("m1", HELLO)
    done = env.run_job("m1", tj2, [entry()])
    assert done["applied"][0]["outcome"] == "skipped:undone" and env.person("m1", "Them 1")["named"] is False


def test_intro_actions_validate_their_input(env):
    env.meeting("m1")
    assert post_intro(env, "bogus").status_code == 400
    assert post_intro(env, "undo").status_code == 400  # nothing was named from an introduction
    assert post_intro(env, "undo", label="Them 7").status_code == 404
    assert post_intro(env, "use", sid="../x").status_code == 400
    env.configure(voice_profiles_enabled=False)
    assert post_intro(env, "undo").status_code == 404  # People is off


# -- undo -------------------------------------------------------------------------------------------------------------


def rerun(env, sid, names, job="j9"):
    """The meeting is processed again (same voices): fresh voiceprint, new transcript, new intro job."""
    env.voices(sid, ("Them 1", "Them 2"), job=job)
    tj = env.transcript(sid, HELLO)
    return env.run_job(sid, tj, names)


def test_undo_of_a_plain_naming_clears_the_name_and_a_rerun_does_not_bring_it_back(env):
    tj = env.meeting("m1")
    env.run_job("m1", tj, [entry()])
    pid = env.person("m1", "Them 1")["person_id"]
    resp = post_intro(env, "undo")
    assert resp.status_code == 200 and resp.json() == {"mode": "named", "person_id": pid}
    shown = env.person("m1", "Them 1")
    assert shown["named"] is False and shown["person_id"] == pid
    person = people.get_person(env.store, pid)
    assert person["company"] == "" and person["named_from"] == []
    assert "Dana" not in names_mod.all_names(env.root)
    assert env.row("m1", "Them 1")["rejected_intro_names"] == ["Dana"]
    assert env.rechecks == ["person", "person"]  # naming, then taking it back
    done = rerun(env, "m1", [entry()])
    assert done["applied"][0]["outcome"] == "skipped:undone"
    assert env.person("m1", "Them 1")["named"] is False


def test_undo_of_a_merge_unlinks_the_voice_into_a_new_unnamed_person(env):
    dana = people.add_person(env.root, "Dana Reyes")
    tj = env.meeting("m1")
    env.run_job("m1", tj, [entry(name="Dana Reyes")])
    assert env.person("m1", "Them 1")["person_id"] == dana["id"]
    resp = post_intro(env, "undo")
    assert resp.status_code == 200 and resp.json()["mode"] == "merged"
    shown = env.person("m1", "Them 1")
    assert shown["person_id"] != dana["id"] and shown["named"] is False  # a new unnamed person
    kept = people.get_person(env.store, dana["id"])
    assert kept["name"] == "Dana Reyes" and kept["named_from"] == [] and kept["meetings"] == 0  # Dana keeps her name
    done = rerun(env, "m1", [entry(name="Dana Reyes")])
    assert done["applied"][0]["outcome"] == "skipped:undone"
    assert env.person("m1", "Them 1")["person_id"] != dana["id"]


def test_a_rerun_with_no_undo_names_the_new_voice_again(env):
    tj = env.meeting("m1")
    env.run_job("m1", tj, [entry()])
    done = rerun(env, "m1", [entry()])
    assert done["applied"][0]["outcome"] == "merged"  # Dana (named, no other voice) is the person the name resolves to
    shown = env.person("m1", "Them 1")
    assert shown["display"] == "Dana" and len([p for p in people.list_people(env.store) if p["name"] == "Dana"]) == 1


def test_reprofile_keeps_a_voice_named_from_an_introduction(env):
    tj = env.meeting("m1")
    env.run_job("m1", tj, [entry()])
    pid = env.person("m1", "Them 1")["person_id"]
    people.reprofile(env.store, "m1")
    assert env.person("m1", "Them 1")["person_id"] == pid and env.person("m1", "Them 1")["display"] == "Dana"


def test_combining_keeps_where_a_name_came_from(env):
    tj = env.meeting("m1")
    env.run_job("m1", tj, [entry()])
    pid = env.person("m1", "Them 1")["person_id"]
    other = people.add_person(env.root, "Dana R.")
    people.merge_people(env.root, pid, other["id"])
    assert people.get_person(env.store, other["id"])["named_from"][0]["label"] == "Them 1"


# -- the pages ---------------------------------------------------------------------------------------------------------


def test_the_person_page_and_list_show_the_introduction_controls(env):
    from meeting_notes.server import web

    page = web.render_person_page("p_1", token_configured=True)
    for needle in ("Named from introduction", "Undo", "Introduced as", "Use this name", "Not them",
                   "/v1/people/appearances/intro", 'id="person-intro"'):
        assert needle in page, needle
    assert "Introduced as" in web.render_people_page(token_configured=True)


def test_settings_toggle_and_pipeline_step(env):
    from meeting_notes.server import web

    page = web.render_settings_page(settings_mod.load_settings(env.root), token_configured=True)
    assert 'name="intro_naming"' in page and "Name new voices from introductions" in page and 'id="ing-intro"' in page
    assert "It never changes the notes" in page
    off = web.render_settings_page(dataclasses.replace(settings_mod.load_settings(env.root), intro_naming=False), token_configured=True)
    assert 'id="intro-naming" value="on" checked' in page and 'id="intro-naming" value="on">' in off
    saved = env.client.put("/v1/settings", headers=HDR, json={
        "model": "small.en", "beam_size": 5, "audio_retention_days": -1, "retention_check_interval_minutes": 60,
        "intro_naming": False})
    assert saved.status_code == 200 and settings_mod.load_settings(env.root).intro_naming is False
    env.client.put("/v1/settings", headers=HDR, json={
        "model": "small.en", "beam_size": 5, "audio_retention_days": -1, "retention_check_interval_minutes": 60})
    assert settings_mod.load_settings(env.root).intro_naming is False  # left out of a save: kept


# -- the bridge's third job kind -------------------------------------------------------------------------------------


def test_the_bridge_asks_for_all_three_kinds():
    assert CLAIM_KINDS == "notes,split_suggestions,intro_names"
    seen = {}

    def handler(request: httpx.Request):
        seen["params"] = dict(request.url.params)
        return httpx.Response(204)

    worker = BridgeWorker(BridgeConfig("http://server", "t", worker_id="w"))
    worker.client = httpx.Client(transport=httpx.MockTransport(handler))
    assert worker.claim() is None and seen["params"]["kinds"] == KINDS


def test_the_bridge_routes_an_intro_job_to_its_own_prompt_schema_validator_and_paths(env, monkeypatch):
    """The real ``BridgeWorker`` against the real server routes, with the model call faked."""
    tj = env.meeting("m1")
    env.queue.enqueue("m1", tj)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        response = env.client.request(
            request.method, request.url.raw_path.decode(), headers=HDR, content=request.content or None)
        return httpx.Response(response.status_code, content=response.content,
                              headers={"content-type": response.headers["content-type"]} if response.content else {})

    worker = BridgeWorker(BridgeConfig("http://server", TOKEN, poll_seconds=0.1, provider="ollama"))
    worker.client = httpx.Client(transport=httpx.MockTransport(handler))
    seen = {}

    def fake_provider(name, transcript, workflow, output, schema, provider):
        seen.update(prompt=worker._prompt(), schema=json.loads(schema.read_text()), transcript=transcript.read_text(),
                    workflow=workflow.read_text())
        output.write_text(json.dumps({"names": [entry()]}))

    monkeypatch.setattr(worker, "_run_provider", fake_provider)
    worker.run(once=True)
    assert "introduce themselves" in seen["prompt"] and "untrusted" in seen["prompt"]
    assert seen["schema"] == review_contract.intro_output_schema() and "names" in seen["schema"]["properties"]
    assert "Unnamed voices" in seen["transcript"] and "[1.0s] Them 1: Hi everyone" in seen["transcript"]
    assert "Never name \"You\"" in seen["workflow"]
    assert ("POST", f"/v1/bridge/intro-names/{env.queue.latest('m1')['job_id']}/complete") in calls
    assert not any("/failure" in path for _m, path in calls)
    assert env.person("m1", "Them 1")["display"] == "Dana"
    assert worker._prompt_override is None and "introduce themselves" not in worker._prompt()


def test_an_invalid_intro_result_posts_to_the_intro_failure_path(env, monkeypatch):
    tj = env.meeting("m1")
    job = env.queue.enqueue("m1", tj)
    posted = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/bridge/review/claim":
            return httpx.Response(200, json={"id": job["job_id"], "kind": INTRO_KIND, "transcript": "x", "workflow": "y"})
        posted.append((request.url.path, json.loads(request.content or b"{}")))
        return httpx.Response(200, json={})

    worker = BridgeWorker(BridgeConfig("http://server", TOKEN, provider="ollama"))
    worker.client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(worker, "_run_provider", lambda *a: a[3].write_text(json.dumps({"names": [{"label": "x"}]})))
    with pytest.raises(Exception):
        worker.run(once=True)
    assert posted[0][0] == f"/v1/bridge/intro-names/{job['job_id']}/failure" and "name" in posted[0][1]["error"]


def test_the_contract_validator():
    ok = {"names": [{"label": "Them 1", "name": "Dana", "company": None, "quote": "I'm Dana", "confidence": "low"},
                    {"label": "Them 2", "name": "Lee", "quote": "Lee here", "confidence": "high"}]}
    assert review_contract.validate_intro_names(ok) == ok
    assert review_contract.validate_intro_names({"names": []}) == {"names": []}
    for bad in ({"names": [{"label": "a", "name": "b", "quote": "c", "confidence": "maybe"}]},
                {"names": [{"label": "a", "name": " ", "quote": "c", "confidence": "high"}]},
                {"names": [{"label": "a", "name": "b", "quote": "c", "confidence": "high", "company": 3}]}, []):
        with pytest.raises(review_contract.ReviewValidationError):
            review_contract.validate_intro_names(bad)
    schema = review_contract.intro_output_schema()
    assert schema["properties"]["names"]["items"]["required"] == ["label", "name", "company", "quote", "confidence"]
    assert "introduce themselves" in review_contract.intro_prompt_text() and review_contract.intro_workflow_text()


# -- queued from the job pipeline ---------------------------------------------------------------------------------------


def latest_job(rig, sid):
    return wait_for(lambda: rig.queue.intro_names.latest(sid))


def test_a_new_meeting_queues_the_job_after_its_speakers_are_recorded(tmp_path, monkeypatch):
    rig = make_rig(tmp_path, monkeypatch)
    done = rig.wait_state(finalize(rig.client, "c1"))
    job = latest_job(rig, "c1")
    assert job and job["status"] == "queued" and job["transcript_job_id"] == done["job_id"]
    assert people.intro_candidates(rig.store.root, "c1")["unnamed"]


@pytest.mark.parametrize("fields", [{"intro_naming": False}, {"ai_provider": "disabled"}, {"voice_profiles_enabled": False}])
def test_no_job_from_the_pipeline_when_a_gate_is_off(tmp_path, monkeypatch, fields):
    rig = make_rig(tmp_path, monkeypatch, **fields)
    rig.wait_state(finalize(rig.client, "c1"))
    time.sleep(0.3)
    assert rig.queue.intro_names.latest("c1") is None


def test_a_retranscribe_and_find_speakers_again_supersede_the_job(tmp_path, monkeypatch):
    rig = make_rig(tmp_path, monkeypatch)
    rig.wait_state(finalize(rig.client, "c1"))
    first = latest_job(rig, "c1")
    resp = rig.client.post("/v1/sessions/c1/retranscribe")
    assert resp.status_code in (200, 202), resp.text
    second_tj = rig.wait_state(resp.json()["job_id"])["job_id"]
    second = wait_for(lambda: (j := rig.queue.intro_names.latest("c1")) and j["job_id"] != first["job_id"] and j)
    assert second and second["transcript_job_id"] == second_tj
    assert rig.queue.intro_names.read(first["job_id"])["status"] == "superseded"
    found = rig.client.post("/v1/sessions/c1/find-speakers")
    assert found.status_code == 200, found.text
    third_tj = rig.wait_state(found.json()["job_id"])["job_id"]
    third = wait_for(lambda: (j := rig.queue.intro_names.latest("c1")) and j["job_id"] != second["job_id"] and j)
    assert third and third["transcript_job_id"] == third_tj
    assert rig.queue.intro_names.read(second["job_id"])["status"] == "superseded"


def test_a_queued_notes_review_is_claimed_before_the_pipeline_job(tmp_path, monkeypatch):
    rig = make_rig(tmp_path, monkeypatch)
    rig.wait_state(finalize(rig.client, "c1"))
    assert latest_job(rig, "c1")
    wait_for(lambda: rig.store.list_reviews("c1"))
    got = rig.client.get(f"/v1/bridge/review/claim?worker_id=w&kinds={KINDS}").json()
    assert got.get("kind") is None and got["session_id"] == "c1"  # the notes
    assert rig.client.get(f"/v1/bridge/review/claim?worker_id=w&kinds={KINDS}").json()["kind"] == "intro_names"
