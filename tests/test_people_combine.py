"""People: combine, better voice pickup (weighting, exemplars, learning back), companies, names inside People,
and linking participants in the notes to People."""

from __future__ import annotations

import dataclasses
import json
import math
import re
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from meeting_notes.server import names as names_mod
from meeting_notes.server import people
from meeting_notes.server import settings as settings_mod
from meeting_notes.server import web
from meeting_notes.server.app import create_app
from meeting_notes.server.people import TrackSpeakers
from meeting_notes.transcribe.diarize import Diarization, SpeakerTurn

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated:DeprecationWarning"
)
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")
DIM = 64


def basis(i):
    v = [0.0] * DIM
    v[i] = 1.0
    return v


def mix(i, j, cos):
    v = [0.0] * DIM
    v[i] = cos
    v[j] = math.sqrt(1.0 - cos * cos)
    return v


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = create_app(transcriber_factory=lambda **kw: None, data_root=str(tmp_path / "data"))
    with TestClient(app) as c:
        c.root = tmp_path / "data"
        current = settings_mod.load_settings(c.root)
        settings_mod.save_settings(
            c.root, dataclasses.replace(current, voice_profiles_enabled=True, diarization_enabled=True))
        c.store = app.state.store if hasattr(app.state, "store") else None
        yield c


@pytest.fixture()
def store(client):
    from meeting_notes.server.store import Store
    return Store(str(client.root))


def record(store, sid, spec, track="system"):
    if not store.session_exists(sid):
        store.ensure_session_dir(sid)
        store.write_session_meta(sid, {"name": sid, "created": "2026-10-01", "tracks": {"system": {}, "mic": {}}})
    turns, vectors, labels, t = [], {}, {}, 0.0
    for raw, (label, vec, seconds) in spec.items():
        turns.append(SpeakerTurn(t, t + seconds, raw))
        t += seconds
        vectors[raw] = vec
        labels[raw] = label
    people.record_meeting(store, sid, "j1", {track: TrackSpeakers(Diarization(turns, vectors), labels)})


def row(store, sid, label="Them 1"):
    vp = json.loads((store.root / "people" / "voiceprints" / f"{sid}.json").read_text(encoding="utf-8"))
    return next(a for a in vp["speakers"] if a["label"] == label)


def three(store):
    """Three different voices in three meetings -> three unnamed people."""
    for n, sid in enumerate(("s1", "s2", "s3")):
        record(store, sid, {"A": ("Them 1", basis(n), 30)})
    return [row(store, sid)["person_id"] for sid in ("s1", "s2", "s3")]


# -- combine ----------------------------------------------------------------------------------------------------


def test_combine_moves_everyone_into_the_kept_person(client, store):
    a, b, c = three(store)
    people.update_person(store.root, b, {"name": "Bea", "company": "Acme"})
    r = client.post("/v1/people/combine", json={"ids": [a, b, c], "into": b})
    assert r.status_code == 200 and r.json()["id"] == b and r.json()["meetings"] == 3
    assert {row(store, sid)["person_id"] for sid in ("s1", "s2", "s3")} == {b}
    assert all(row(store, sid)["link"] == "confirmed" for sid in ("s1", "s3"))  # the moved ones
    assert [p["id"] for p in people.list_people(store)] == [b]


def test_combine_input_checks(client, store):
    a, b, c = three(store)
    post = lambda body: client.post("/v1/people/combine", json=body)  # noqa: E731
    assert post({"ids": [a], "into": a}).status_code == 400
    assert post({"ids": [a, a], "into": a}).status_code == 400          # the same person twice is one person
    assert post({"ids": [a, b], "into": c}).status_code == 400          # into must be one of the chosen
    assert post({"ids": [a, "p_nope"], "into": a}).status_code == 404
    assert post({"ids": "x", "into": a}).status_code == 400
    assert post({"ids": [a, b]}).status_code == 400
    assert len(people.list_people(store)) == 3                           # nothing changed by the failures


def test_combine_keeps_the_you_profile(client, store):
    a, b, c = three(store)
    with people._lock:
        data = people._load(store.root)
        me = people._self_person(data)["id"]
        people._save(store.root, data)
    r = client.post("/v1/people/combine", json={"ids": [me, a], "into": a})
    assert r.status_code == 400 and "keep it" in r.json()["detail"]
    assert client.post("/v1/people/combine", json={"ids": [me, a, b, c], "into": me}).status_code == 200
    assert {p["id"] for p in people.list_people(store)} == {me}


def test_combine_adds_the_other_names_as_spellings(client, store):
    a, b, _ = three(store)
    people.update_person(store.root, a, {"name": "Robert"})
    people.update_person(store.root, b, {"name": "Bob"})
    people.combine_people(store.root, [a, b], a)
    data = names_mod.load(store.root)
    assert {"wrong": "Bob", "right": "Robert"} in data["name_corrections"]
    assert people.lookup(store.root, ["Bob"])["Bob"]["person_id"] == a


def test_person_page_says_combine_not_merge():
    page = web.render_person_page("p_x", token_configured=True)
    assert "Combine with..." in page and "Merge with" not in page and "Merge" not in page.replace("/merge", "")
    assert "use Combine with... below" in page


def test_people_list_page_has_selection_and_combine_dialog():
    page = web.render_people_page(token_configured=True)
    for needle in ('id="select-all"', 'class="row-select"', 'id="bulk-combine"', 'id="combine-dialog"',
                   "/v1/people/combine", "Who should be kept?", 'id="add-dialog"', "Add a person", "No voice yet"):
        assert needle in page, needle


# -- weighting, exemplars, drift --------------------------------------------------------------------------------


def vps(rows):
    return {"s%d" % i: {"speakers": [r]} for i, r in enumerate(rows)}


def ap(pid, vec, sec, link):
    return {"person_id": pid, "embedding": vec, "speech_sec": sec, "link": link}


def test_confirmed_counts_double_and_each_meeting_is_capped():
    data = {"people": [{"id": "p"}]}
    # equal speech: the confirmed vector pulls the centroid twice as hard
    m = people._models(vps([ap("p", basis(0), 30, "auto"), ap("p", mix(0, 1, 0.8), 30, "confirmed")]), data)["p"]
    plain = people._centroid([(basis(0), 30), (mix(0, 1, 0.8), 30)])
    assert people.cosine(m["centroid"], mix(0, 1, 0.8)) > people.cosine(plain, mix(0, 1, 0.8))
    # a 3000 s meeting weighs the same as a 600 s one
    capped = people._models(vps([ap("p", basis(0), 3000, "auto"), ap("p", basis(1), 600, "auto")]), data)["p"]
    assert people.cosine(capped["centroid"], basis(0)) == pytest.approx(people.cosine(capped["centroid"], basis(1)))


def test_auto_links_far_from_the_confirmed_voice_are_left_out():
    data = {"people": [{"id": "p"}]}
    rows = [ap("p", basis(0), 60, "confirmed"), ap("p", basis(0), 20, "auto"), ap("p", basis(5), 500, "auto")]
    m = people._models(vps(rows), data)["p"]
    assert m["meetings"] == 2 and m["confirmed"] == 1
    assert people.cosine(m["centroid"], basis(5)) < 0.01
    # without anything confirmed nothing is excluded
    m = people._models(vps(rows[1:]), data)["p"]
    assert m["meetings"] == 2 and m["confirmed"] == 0


def test_a_voice_on_another_setup_matches_through_a_confirmed_exemplar(store):
    record(store, "s1", {"A": ("Them 1", basis(0), 600)})
    record(store, "s2", {"A": ("Them 1", basis(1), 10)})
    pid = row(store, "s1")["person_id"]
    people.appearance_action(store.root, "s1", "Them 1", "assign", pid)
    people.appearance_action(store.root, "s2", "Them 1", "assign", pid)
    centroid = people._models(people._visible(store), people._load(store.root))[pid]["centroid"]
    assert people.cosine(centroid, basis(1)) < people.SUGGEST      # the profile as a whole does not sound like it ...
    record(store, "s3", {"A": ("Them 1", basis(1), 30)})
    r = row(store, "s3")
    assert r["person_id"] == pid and r["link"] == "auto"           # ... but one confirmed voice does


# -- learning back and rejections -------------------------------------------------------------------------------


def learn_setup(store):
    record(store, "s1", {"A": ("Them 1", basis(0), 30)})
    record(store, "s3", {"A": ("Them 1", mix(0, 1, 0.6), 30)})   # sounds a bit like s1: a question, a new person
    record(store, "s2", {"A": ("Them 1", mix(0, 1, 0.3), 30)})   # sounds like s3's person, not like s1
    return row(store, "s1")["person_id"], row(store, "s2")["person_id"]


def test_confirming_a_voice_makes_other_unnamed_voices_ask_if_they_are_the_same(store):
    ann, other = learn_setup(store)
    people.update_person(store.root, ann, {"name": "Ann"})
    assert row(store, "s2")["suggested_person_id"] is None
    people.appearance_action(store.root, "s3", "Them 1", "confirm")
    r = row(store, "s2")
    assert r["link"] == "suggested" and r["suggested_person_id"] == ann and r["person_id"] == other  # asked, not moved
    assert [s["suggested_display"] for s in people.suggestions(store.root, "s2")] == ["Ann"]


def test_not_them_is_remembered(store):
    ann, other = learn_setup(store)
    people.update_person(store.root, ann, {"name": "Ann"})
    people.appearance_action(store.root, "s3", "Them 1", "confirm")
    people.appearance_action(store.root, "s2", "Them 1", "reject")
    assert row(store, "s2")["rejected_person_ids"] == [ann] and row(store, "s2")["suggested_person_id"] is None
    assert people.refresh_suggestions(store, ann) == 0
    people.reprofile(store, "s2")
    r = row(store, "s2")
    assert r["suggested_person_id"] is None and r["rejected_person_ids"] == [ann]


def test_refresh_only_asks_about_unnamed_voices_without_a_pending_question(store):
    ann, other = learn_setup(store)
    people.update_person(store.root, ann, {"name": "Ann"})
    people.appearance_action(store.root, "s3", "Them 1", "confirm")
    assert row(store, "s2")["suggested_person_id"] == ann
    assert people.refresh_suggestions(store, ann) == 0                  # already asked
    people.update_person(store.root, other, {"name": "Cy"})             # named people are never asked about
    people.appearance_action(store.root, "s2", "Them 1", "reject")
    assert people.refresh_suggestions(store.root, ann) == 0             # a data root works too
    assert people.refresh_suggestions(store, "p_missing") == 0


def test_refresh_skips_trashed_meetings(store):
    ann, other = learn_setup(store)
    people.update_person(store.root, ann, {"name": "Ann"})
    store.trash_session("s2") if hasattr(store, "trash_session") else shutil.rmtree(store.session_dir("s2"))
    people.appearance_action(store.root, "s3", "Them 1", "confirm")
    assert row(store, "s2")["suggested_person_id"] is None


# -- companies --------------------------------------------------------------------------------------------------


def test_companies_endpoint_is_distinct_sorted_and_case_insensitive(client, store):
    a, b, c = three(store)
    people.update_person(store.root, a, {"company": "zeta labs"})
    people.update_person(store.root, b, {"company": "Acme"})
    people.update_person(store.root, c, {"company": "ACME"})
    r = client.get("/v1/people/companies")
    assert r.status_code == 200 and r.json() == {"items": ["Acme", "zeta labs"]}


def test_companies_endpoint_is_off_when_people_is_off(client):
    current = settings_mod.load_settings(client.root)
    settings_mod.save_settings(client.root, dataclasses.replace(current, voice_profiles_enabled=False))
    assert client.get("/v1/people/companies").status_code == 404


def test_company_field_is_a_dropdown_with_none_and_add_new():
    page = web.render_person_page("p_x", token_configured=True)
    assert 'id="f-company-sel"' in page and '<option value="">None</option>' in page
    assert "Add a new company..." in page and "/v1/people/companies" in page
    assert re.search(r'id="f-company"[^>]*hidden', page)
    assert "$('f-company').focus()" in page and "companyValue()" in page


# -- names live in People ---------------------------------------------------------------------------------------


def test_add_person_by_name_has_no_voice_and_is_a_known_name(client, store):
    r = client.post("/v1/people", json={"name": "  Tara   Nguyen ", "company": "Acme"})
    assert r.status_code == 200 and r.json()["name"] == "Tara Nguyen" and r.json()["meetings"] == 0
    assert "Tara Nguyen" in names_mod.load(store.root)["known_names"]
    assert client.post("/v1/people", json={"name": "tara nguyen"}).status_code == 400
    assert client.post("/v1/people", json={"name": "  "}).status_code == 400
    assert [p["display"] for p in client.get("/v1/people").json()["people"]] == ["Tara Nguyen"]


def test_known_names_become_people_once(client, store):
    names_mod.add_name(store.root, "Old Name")
    names_mod.add_name(store.root, "Other One")
    listed = client.get("/v1/people").json()["people"]
    assert sorted(p["display"] for p in listed) == ["Old Name", "Other One"]
    assert len(client.get("/v1/people").json()["people"]) == 2          # idempotent
    assert people.sync_known_names(store.root) == 0
    assert "Old Name" in names_mod.load(store.root)["known_names"]       # names.json keeps working


def test_deleting_a_person_forgets_the_name(client, store):
    pid = client.post("/v1/people", json={"name": "Gone"}).json()["id"]
    assert client.delete(f"/v1/people/{pid}").status_code == 200
    assert client.get("/v1/people").json()["people"] == []


def test_named_people_feed_the_prompt_and_hotwords(store):
    people.add_person(store.root, "Priya Shah")
    (store.root / "names.json").unlink(missing_ok=True)                 # only the person profile knows the name
    assert "Priya Shah" in names_mod.prompt_section(store.root)
    assert "Priya Shah" in names_mod.hotwords(store.root)


def test_also_heard_as_follows_a_rename(client, store):
    pid = client.post("/v1/people", json={"name": "Tara Nguyen"}).json()["id"]
    r = client.post(f"/v1/people/{pid}/aliases", json={"wrong": "Tara When"})
    assert r.status_code == 200 and r.json()["aliases"] == ["Tara When"]
    assert client.get(f"/v1/people/{pid}").json()["aliases"] == ["Tara When"]
    client.patch(f"/v1/people/{pid}", json={"name": "Tara Ngo"})
    assert client.get(f"/v1/people/{pid}").json()["aliases"] == ["Tara When"]
    assert {"wrong": "Tara When", "right": "Tara Ngo"} in names_mod.load(store.root)["name_corrections"]
    assert client.delete(f"/v1/people/{pid}/aliases", params={"wrong": "Tara When"}).json()["aliases"] == []


def test_alias_needs_a_named_person(client, store):
    a, _, _ = three(store)
    assert client.post(f"/v1/people/{a}/aliases", json={"wrong": "x"}).status_code == 400
    assert client.post("/v1/people/p_nope/aliases", json={"wrong": "x"}).status_code == 404


def test_voice_profile_summary_on_the_person(client, store):
    record(store, "s1", {"A": ("Them 1", basis(0), 30)})
    pid = row(store, "s1")["person_id"]
    assert client.get(f"/v1/people/{pid}").json()["voice_profile"] == {"meetings": 1, "confirmed": 0}
    people.appearance_action(store.root, "s1", "Them 1", "assign", pid)
    assert client.get(f"/v1/people/{pid}").json()["voice_profile"] == {"meetings": 1, "confirmed": 1}
    page = web.render_person_page("p_x", token_configured=True)
    assert "Voice profile from " in page and "confirmed by you" in page and "Also heard as" in page


# -- participants in the notes link to People -------------------------------------------------------------------


def test_lookup_matches_names_and_spellings_case_insensitively(client, store):
    pid = client.post("/v1/people", json={"name": "Tara Nguyen", "company": "Acme"}).json()["id"]
    client.post(f"/v1/people/{pid}/aliases", json={"wrong": "Tara When"})
    items = client.get("/v1/people/lookup", params=[("names", "tara nguyen"), ("names", "TARA WHEN"),
                                                    ("names", "Nobody Known")]).json()["items"]
    assert items["tara nguyen"]["person_id"] == pid and items["tara nguyen"]["via"] == "name"
    assert items["tara nguyen"]["company"] == "Acme" and items["tara nguyen"]["is_self"] is False
    assert items["TARA WHEN"]["person_id"] == pid and items["TARA WHEN"]["via"] == "alias"
    assert "Nobody Known" not in items


def test_lookup_is_404_while_people_is_off(client):
    current = settings_mod.load_settings(client.root)
    settings_mod.save_settings(client.root, dataclasses.replace(current, voice_profiles_enabled=False))
    assert client.get("/v1/people/lookup", params={"names": "x"}).status_code == 404


def test_notes_pages_link_participants():
    meetings = web.render_transcriptions_page(token_configured=True, voice_profiles_enabled=True,
                                              diarization_enabled=True)
    for needle in ("data-pname", "npDecorate", "Not in People", "/v1/people/lookup", "Add to People",
                   "Link to someone...", 'id="np-dialog"', 'id="ppick-dialog"', "namedOnly"):
        assert needle in meetings, needle
    # the meeting page's Participants rail merges the voices heard (notes API "people") with the AI's names
    for needle in ("function participantRows(value,heard)", "data.people", "Heard by voice", "Not named yet",
                   "Your voice", "Written as ", "resolve('cached')",
                   # an unnamed voice or a voice-less name links to a person: combine, then redraw
                   "data-np-same", "Link to a person…", "Same person as…", "'/merge'", "withSelf:true",
                   "o.withSelf && p.is_self"):
        assert needle in meetings, needle
    off = web.render_transcriptions_page(token_configured=True)
    assert 'id="np-dialog"' not in off and 'id="ppick-dialog"' not in off


@needs_node
@pytest.mark.parametrize("render", [
    lambda: web.render_people_page(token_configured=True),
    lambda: web.render_person_page("p_x", token_configured=True),
    lambda: web.render_transcriptions_page(token_configured=True, voice_profiles_enabled=True, diarization_enabled=True),
])
def test_page_scripts_parse(render, tmp_path):
    scripts = re.findall(r"<script>(.*?)</script>", render(), flags=re.S)
    assert scripts
    for i, src in enumerate(scripts):
        path = tmp_path / f"s{i}.js"
        path.write_text(src, encoding="utf-8")
        result = subprocess.run([NODE, "--check", str(path)], capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
