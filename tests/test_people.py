"""Voice profiles: label helpers, embedding extraction, matching, mic mapping, overlay and edits (unit level)."""

from __future__ import annotations

import dataclasses
import json
import math
import wave
from types import SimpleNamespace

import pytest

from meeting_notes import wire
from meeting_notes.server import people
from meeting_notes.server import settings as settings_mod
from meeting_notes.server.people import TrackSpeakers
from meeting_notes.server.store import Store
from meeting_notes.transcribe.diarize import Diarization, SpeakerTurn, assign_labels, assign_speakers, speaker_labels
from meeting_notes.transcribe.protocol import Segment
from meeting_notes.transcribe.pyannote_backend import extract_embeddings

DIM = 64


def basis(i: int) -> list:
    v = [0.0] * DIM
    v[i] = 1.0
    return v


def mix(i: int, j: int, cos: float) -> list:
    """A unit vector whose cosine with ``basis(i)`` is ``cos`` (the rest points along ``basis(j)``)."""
    v = [0.0] * DIM
    v[i] = cos
    v[j] = math.sqrt(1.0 - cos * cos)
    return v


def turn(start, end, speaker):
    return SpeakerTurn(start, end, speaker)


# -- labels ---------------------------------------------------------------------------------------------------


def test_speaker_labels_number_by_first_appearance():
    turns = [turn(5, 9, "B"), turn(0, 4, "A"), turn(10, 12, "C"), turn(13, 14, "A")]
    assert speaker_labels(turns, "Them") == {"A": "Them 1", "B": "Them 2", "C": "Them 3"}
    assert speaker_labels([], "Them") == {}


def test_assign_speakers_is_assign_labels_with_the_default_map():
    segs = [Segment(0, 2, "a", "system"), Segment(5, 8, "b", "system"), Segment(20, 21, "c", "system")]
    turns = [turn(0, 3, "A"), turn(4, 9, "B")]
    assert assign_speakers(segs, turns) == assign_labels(segs, turns, speaker_labels(turns, "Them"))
    assert [s.speaker for s in assign_speakers(segs, turns)] == ["Them 1", "Them 2", None]


def test_assign_labels_ignores_turns_that_are_not_in_the_map():
    segs = [Segment(0, 4, "a", "mic"), Segment(10, 14, "b", "mic")]
    turns = [turn(0, 4, "ME"), turn(10, 14, "ROOM")]
    out = assign_labels(segs, turns, {"ROOM": "In room 1"})
    assert [s.speaker for s in out] == [None, "In room 1"]  # the first keeps the track's own label


# -- embedding extraction -------------------------------------------------------------------------------------


class FakeAnnotation:
    def __init__(self, labels):
        self._labels = labels

    def labels(self):
        return list(self._labels)


def fake_output(labels, matrix):
    return SimpleNamespace(speaker_diarization=FakeAnnotation(labels), speaker_embeddings=matrix)


def test_extract_embeddings_maps_rows_by_label_and_normalises():
    np = pytest.importorskip("numpy")
    out = fake_output(["SPEAKER_00", "SPEAKER_01"], np.array([[3.0, 4.0], [0.0, 2.0]]))
    found = extract_embeddings(out, {"SPEAKER_00", "SPEAKER_01"})
    assert found["SPEAKER_00"] == pytest.approx([0.6, 0.8])
    assert found["SPEAKER_01"] == pytest.approx([0.0, 1.0])


def test_extract_embeddings_keeps_only_speakers_with_turns():
    np = pytest.importorskip("numpy")
    out = fake_output(["A", "B"], np.array([[1.0, 0.0], [0.0, 1.0]]))
    assert list(extract_embeddings(out, {"B"})) == ["B"]


@pytest.mark.parametrize("make", [
    lambda np: fake_output(["A", "B"], np.array([[1.0, 0.0]])),              # row count differs from the labels
    lambda np: fake_output(["A"], np.array([1.0, 0.0])),                     # not a matrix
    lambda np: fake_output(["A"], None),                                     # no embeddings at all
    lambda np: ([], None),                                                   # a legacy tuple
    lambda np: SimpleNamespace(speaker_embeddings=np.array([[1.0]])),       # no diarization to take labels from
    lambda np: fake_output(["A"], np.zeros((1, 0))),                         # empty vectors
])
def test_extract_embeddings_gives_nothing_for_unexpected_shapes(make):
    np = pytest.importorskip("numpy")
    assert extract_embeddings(make(np), {"A", "B"}) == {}


def test_extract_embeddings_drops_non_finite_and_zero_rows():
    np = pytest.importorskip("numpy")
    out = fake_output(["A", "B", "C"], np.array([[float("nan"), 1.0], [0.0, 0.0], [1.0, 1.0]]))
    assert list(extract_embeddings(out, {"A", "B", "C"})) == ["C"]


# -- helpers for the store-backed tests -----------------------------------------------------------------------


def enable(store, on=True):
    current = settings_mod.load_settings(store.root)
    settings_mod.save_settings(
        store.root, dataclasses.replace(current, diarization_enabled=on, voice_profiles_enabled=on))


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "data"))
    enable(s)
    return s


def make_session(store, sid, name=None, created="2026-10-01"):
    store.ensure_session_dir(sid)
    store.write_session_meta(sid, {"name": name or sid, "created": created, "tracks": {"system": {}, "mic": {}}})


def speakers_for(track, spec, *, prefix_labels=True):
    """``spec``: ``{raw: (label, vector, seconds)}`` -> ``TrackSpeakers`` with back-to-back turns."""
    turns, vectors, labels, t = [], {}, {}, 0.0
    for raw, (label, vec, seconds) in spec.items():
        turns.append(turn(t, t + seconds, raw))
        t += seconds
        vectors[raw] = vec
        labels[raw] = label
    return TrackSpeakers(Diarization(turns, vectors), labels)


def record(store, sid, spec, track="system", job="j1", wav_paths=None):
    make_session(store, sid) if not store.session_exists(sid) else None
    people.record_meeting(store, sid, job, {track: speakers_for(track, spec)}, wav_paths)


def vp(store, sid):
    return json.loads((store.root / "people" / "voiceprints" / f"{sid}.json").read_text(encoding="utf-8"))


def appearance(store, sid, label):
    return next(a for a in vp(store, sid)["speakers"] if a["label"] == label)


# -- off / on ---------------------------------------------------------------------------------------------------


def test_enabled_needs_both_switches():
    assert not people.enabled(SimpleNamespace(voice_profiles_enabled=False, diarization_enabled=True))
    assert not people.enabled(SimpleNamespace(voice_profiles_enabled=True, diarization_enabled=False))
    assert people.enabled(SimpleNamespace(voice_profiles_enabled=True, diarization_enabled=True))
    assert not settings_mod.Settings().voice_profiles_enabled


def test_nothing_is_stored_while_off(tmp_path):
    store = Store(str(tmp_path / "data"))
    enable(store, on=False)
    make_session(store, "s1")
    people.record_meeting(store, "s1", "j1", {"system": speakers_for("system", {"A": ("Them 1", basis(0), 30)})})
    assert not (store.root / "people").exists()
    assert people.overlay(store.root, "s1") == {}


# -- matching -------------------------------------------------------------------------------------------------


def test_first_meeting_creates_unnamed_people_with_numbers(store):
    record(store, "s1", {"A": ("Them 1", basis(0), 30), "B": ("Them 2", basis(1), 20)})
    shown = people.overlay(store.root, "s1")
    assert {k: v["display"] for k, v in shown.items()} == {"Them 1": "Person 1", "Them 2": "Person 2"}
    assert all(not v["named"] for v in shown.values())
    assert shown["Them 1"]["link"] == "auto" and shown["Them 1"]["person_id"].startswith("p_")


def test_same_voice_links_to_the_same_person_above_auto(store):
    record(store, "s1", {"A": ("Them 1", basis(0), 30)})
    record(store, "s2", {"X": ("Them 1", mix(0, 1, 0.8), 30)}, job="j2")
    a, b = appearance(store, "s1", "Them 1"), appearance(store, "s2", "Them 1")
    assert a["person_id"] == b["person_id"] and b["link"] == "auto" and b["score"] == pytest.approx(0.8, abs=1e-3)
    assert len(people.list_people(store)) == 1


def test_between_suggest_and_auto_makes_a_new_person_with_a_suggestion(store):
    record(store, "s1", {"A": ("Them 1", basis(0), 30)})
    record(store, "s2", {"X": ("Them 1", mix(0, 1, 0.6), 30)}, job="j2")
    a, b = appearance(store, "s1", "Them 1"), appearance(store, "s2", "Them 1")
    assert b["person_id"] != a["person_id"]
    assert b["link"] == "suggested" and b["suggested_person_id"] == a["person_id"]
    over = people.overlay(store.root, "s2")["Them 1"]
    assert over["suggested_person_id"] == a["person_id"] and over["suggested_display"] == "Person 1"
    assert [s["label"] for s in people.suggestions(store.root, "s2")] == ["Them 1"]


def test_below_suggest_is_just_a_new_person(store):
    record(store, "s1", {"A": ("Them 1", basis(0), 30)})
    record(store, "s2", {"X": ("Them 1", mix(0, 1, 0.3), 30)}, job="j2")
    b = appearance(store, "s2", "Them 1")
    assert b["link"] == "auto" and b["suggested_person_id"] is None
    assert len(people.list_people(store)) == 2


def test_two_speakers_of_one_meeting_never_share_a_person(store):
    record(store, "s1", {"A": ("Them 1", basis(0), 30)})
    record(store, "s2", {"X": ("Them 1", mix(0, 1, 0.95), 30), "Y": ("Them 2", mix(0, 2, 0.9), 40)}, job="j2")
    x, y = appearance(store, "s2", "Them 1"), appearance(store, "s2", "Them 2")
    assert x["person_id"] != y["person_id"]
    assert appearance(store, "s1", "Them 1")["person_id"] == x["person_id"]  # the closer one got the match


def test_short_speakers_get_no_person(store):
    record(store, "s1", {"A": ("Them 1", basis(0), 30), "B": ("Them 2", basis(1), people.MIN_SPEECH - 1)})
    assert appearance(store, "s1", "Them 2")["person_id"] is None
    assert list(people.overlay(store.root, "s1")) == ["Them 1"]
    assert len(people.list_people(store)) == 1


def test_centroid_is_weighted_by_speech():
    heavy = people._centroid([(basis(0), 10.0), (basis(1), 30.0)])
    assert people.cosine(heavy, basis(1)) > people.cosine(heavy, basis(0))
    assert math.isclose(sum(x * x for x in heavy), 1.0)


def test_unusable_vectors_are_skipped(store):
    record(store, "s1", {"A": ("Them 1", [float("nan")] * DIM, 30), "B": ("Them 2", [0.0] * DIM, 30),
                         "C": ("Them 3", basis(2), 30)})
    assert [a["label"] for a in vp(store, "s1")["speakers"]] == ["Them 3"]


# -- mic mapping ----------------------------------------------------------------------------------------------


def mic_turns(spec):
    """``{raw: seconds}`` -> back-to-back turns."""
    turns, t = [], 0.0
    for raw, seconds in spec.items():
        turns.append(turn(t, t + seconds, raw))
        t += seconds
    return turns


def test_mic_self_is_the_cluster_closest_to_the_self_centroid():
    emb = {"M0": basis(0), "M1": basis(1)}
    # M0 talks more, but the self centroid points at M1
    mapping = people.mic_mapping(mic_turns({"M0": 50, "M1": 20}), emb, {}, basis(1))
    assert mapping.self_raw == "M1" and mapping.labels == {"M0": "In room 1"}


def test_mic_self_falls_back_to_most_speech_when_nothing_is_close():
    emb = {"M0": basis(0), "M1": basis(1)}
    mapping = people.mic_mapping(mic_turns({"M0": 20, "M1": 50}), emb, {}, basis(5))
    assert mapping.self_raw == "M1" and mapping.labels == {"M0": "In room 1"}
    assert people.mic_mapping(mic_turns({"M0": 20, "M1": 50}), emb, {}, None).self_raw == "M1"


def test_mic_folds_echo_and_short_clusters_into_self():
    emb = {"ME": basis(0), "ECHO": mix(3, 4, 1.0), "SHORT": basis(1), "ROOM": basis(2)}
    system = {"Them 1": mix(3, 5, 0.9)}  # ECHO is cos 0.9 to a far-end speaker
    mapping = people.mic_mapping(mic_turns({"ME": 60, "ECHO": 30, "SHORT": 4, "ROOM": 25}), emb, system, None)
    assert mapping.self_raw == "ME"
    assert sorted(mapping.folded) == ["ECHO", "SHORT"]
    assert mapping.labels == {"ROOM": "In room 1"}


def test_mic_rooms_are_numbered_by_first_appearance():
    emb = {"ME": basis(0), "R1": basis(1), "R2": basis(2)}
    turns = [turn(0, 5, "R2"), turn(5, 40, "ME"), turn(40, 60, "R1"), turn(60, 80, "R2")]
    mapping = people.mic_mapping(turns, emb, {}, None)
    assert mapping.labels == {"R2": "In room 1", "R1": "In room 2"}


def test_mic_without_embeddings_stays_one_voice():
    mapping = people.mic_mapping(mic_turns({"M0": 50, "M1": 40}), {}, {}, None)
    assert mapping.labels == {} and mapping.self_raw is None


def test_mic_self_is_linked_to_the_self_person_as_confirmed(store):
    mic = TrackSpeakers(Diarization(mic_turns({"M0": 40, "M1": 30}), {"M0": basis(0), "M1": basis(1)}),
                        labels={"M1": "In room 1"}, self_raw="M0", self_label="You")
    make_session(store, "s1")
    people.record_meeting(store, "s1", "j1", {"mic": mic})
    me = appearance(store, "s1", "You")
    assert me["link"] == "confirmed" and me["track"] == "mic"
    over = people.overlay(store.root, "s1")
    assert over["You"]["display"] == "You" and over["In room 1"]["display"] == "Person 1"  # self takes no number
    # next meeting: the mic self maps to the same person via the self centroid
    make_session(store, "s2")
    people.record_meeting(store, "s2", "j2", {"mic": dataclasses.replace(mic)})
    assert appearance(store, "s2", "You")["person_id"] == me["person_id"]
    assert people.map_mic(store, "s3", Diarization(mic_turns({"A": 20, "B": 50}), {"A": basis(0), "B": basis(7)}), {}).self_raw == "A"


# -- overlay ----------------------------------------------------------------------------------------------------


def test_overlay_uses_names_and_hides_unnamed_from_the_named_view(store):
    record(store, "s1", {"A": ("Them 1", basis(0), 30), "B": ("Them 2", basis(1), 30)})
    pid = appearance(store, "s1", "Them 1")["person_id"]
    people.update_person(store.root, pid, {"name": "  Sarah   Cole ", "company": "Acme"})
    over = people.overlay(store.root, "s1")
    assert over["Them 1"]["display"] == "Sarah Cole" and over["Them 1"]["named"]
    assert over["Them 2"]["display"] == "Person 2" and not over["Them 2"]["named"]
    assert people.named_labels(store.root, "s1") == {"Them 1": "Sarah Cole"}
    md = "**[00:01] Them 1:** hi\n\n**[00:05] Them 2:** yo\n\n**[00:09] You:** ok\n"
    out = people.apply_names_markdown(store.root, "s1", md)
    assert "**[00:01] Sarah Cole:** hi" in out and "**[00:05] Them 2:** yo" in out and "**[00:09] You:** ok" in out
    segs = [{"label": "Them 1", "text": "x"}, {"label": "Them 2", "text": "y"}]
    assert [s["label"] for s in people.apply_names(store.root, "s1", segs)] == ["Sarah Cole", "Them 2"]


def test_overlay_is_empty_when_off(store):
    record(store, "s1", {"A": ("Them 1", basis(0), 30)})
    enable(store, on=False)
    assert people.overlay(store.root, "s1") == {} and people.suggestions(store.root, "s1") == []
    assert people.apply_names_markdown(store.root, "s1", "**[00:01] Them 1:** hi") == "**[00:01] Them 1:** hi"
    assert people.prompt_section(store.root, "s1") == ""


def test_prompt_section_lists_named_speakers_and_keep_self_leaves_the_recorder_as_you(store):
    mic = TrackSpeakers(Diarization(mic_turns({"M0": 40, "M1": 30}), {"M0": basis(0), "M1": basis(1)}),
                        labels={"M1": "In room 1"}, self_raw="M0", self_label="You")
    make_session(store, "s1")
    people.record_meeting(store, "s1", "j1", {"mic": mic, "system": speakers_for(
        "system", {"A": ("Them 1", basis(5), 30), "B": ("Them 2", basis(6), 30)})})
    assert people.prompt_section(store.root, "s1") == ""  # nobody named: nothing to tell the AI
    people.update_person(store.root, appearance(store, "s1", "Them 1")["person_id"], {"name": "Sarah Cole", "company": "Acme"})
    section = people.prompt_section(store.root, "s1")
    assert section.startswith("\n\n## Identified speakers\n\n") and "Sarah Cole (Acme)" in section
    assert "Them 2" not in section.split("Any other label")[0] and "recorder" not in section
    # the self profile named: the section says who "You" is, the AI's transcript keeps "You"
    me = appearance(store, "s1", "You")["person_id"]
    people.update_person(store.root, me, {"name": "Mike Fuentes"})
    section = people.prompt_section(store.root, "s1")
    assert '"You" (the recorder) is Mike Fuentes' in section and "Sarah Cole (Acme)" in section
    assert people.named_labels(store.root, "s1") == {"Them 1": "Sarah Cole", "You": "Mike Fuentes"}
    assert people.named_labels(store.root, "s1", keep_self=True) == {"Them 1": "Sarah Cole"}
    md = "**[00:01] Them 1:** hi\n\n**[00:09] You:** ok\n"
    assert "**[00:09] Mike Fuentes:** ok" in people.apply_names_markdown(store.root, "s1", md)
    assert "**[00:09] You:** ok" in people.apply_names_markdown(store.root, "s1", md, keep_self=True)
    assert [s["label"] for s in people.apply_names(store.root, "s1", [{"label": "You"}], keep_self=True)] == ["You"]
    # a self profile literally called "You" adds no recorder line
    people.update_person(store.root, me, {"name": "You"})
    assert "recorder" not in people.prompt_section(store.root, "s1")


# -- edits ------------------------------------------------------------------------------------------------------


def test_update_validates_and_cleans(store):
    record(store, "s1", {"A": ("Them 1", basis(0), 30)})
    pid = appearance(store, "s1", "Them 1")["person_id"]
    done = people.update_person(store.root, pid, {"name": "Ann", "email": " ann@example.com ", "company": "X\nY"})
    assert (done["name"], done["email"], done["company"]) == ("Ann", "ann@example.com", "X Y")
    for bad in ("nope", "a@b", "a b@c.de", "x" * 250 + "@example.com"):
        with pytest.raises(ValueError):
            people.update_person(store.root, pid, {"email": bad})
    assert people.update_person(store.root, pid, {"email": ""})["email"] == ""
    with pytest.raises(people.PersonNotFound):
        people.update_person(store.root, "p_missing", {"name": "x"})


def test_merge_moves_appearances_and_fills_blank_fields(store):
    record(store, "s1", {"A": ("Them 1", basis(0), 30)})
    record(store, "s2", {"X": ("Them 1", basis(1), 30)}, job="j2")
    a, b = appearance(store, "s1", "Them 1")["person_id"], appearance(store, "s2", "Them 1")["person_id"]
    people.update_person(store.root, a, {"name": "Ann"})
    people.update_person(store.root, b, {"company": "Acme", "name": "Other"})
    merged = people.merge_people(store.root, b, a)
    assert (merged["name"], merged["company"]) == ("Ann", "Acme")
    assert appearance(store, "s2", "Them 1")["person_id"] == a and appearance(store, "s2", "Them 1")["link"] == "confirmed"
    assert [p["id"] for p in people.list_people(store)] == [a]
    with pytest.raises(ValueError):
        people.merge_people(store.root, a, a)
    with pytest.raises(people.PersonNotFound):
        people.merge_people(store.root, "p_missing", a)


def test_merging_the_self_profile_away_is_refused(store):
    mic = TrackSpeakers(Diarization(mic_turns({"M0": 40}), {"M0": basis(0)}), self_raw="M0")
    make_session(store, "s1")
    people.record_meeting(store, "s1", "j1", {"mic": mic})
    record(store, "s2", {"A": ("Them 1", basis(1), 30)}, job="j2")
    me, other = appearance(store, "s1", "You")["person_id"], appearance(store, "s2", "Them 1")["person_id"]
    with pytest.raises(ValueError):
        people.merge_people(store.root, me, other)
    people.merge_people(store.root, other, me)  # the other way round is fine
    assert appearance(store, "s2", "Them 1")["person_id"] == me


def test_appearance_actions(store):
    record(store, "s1", {"A": ("Them 1", basis(0), 30)})
    record(store, "s2", {"X": ("Them 1", mix(0, 1, 0.6), 30)}, job="j2")
    first, second = appearance(store, "s1", "Them 1")["person_id"], appearance(store, "s2", "Them 1")["person_id"]
    # "Not them" keeps the new person and drops only the question
    people.appearance_action(store.root, "s2", "Them 1", "reject")
    assert appearance(store, "s2", "Them 1")["suggested_person_id"] is None
    assert appearance(store, "s2", "Them 1")["person_id"] == second
    with pytest.raises(ValueError):
        people.appearance_action(store.root, "s2", "Them 1", "confirm")  # nothing left to accept
    # accept a suggestion
    record(store, "s3", {"X": ("Them 1", mix(0, 2, 0.6), 30)}, job="j3")
    people.appearance_action(store.root, "s3", "Them 1", "confirm")
    got = appearance(store, "s3", "Them 1")
    assert got["person_id"] in {first, second} and got["link"] == "confirmed" and got["suggested_person_id"] is None
    # assign, then unlink into a brand-new person
    people.appearance_action(store.root, "s2", "Them 1", "assign", first)
    assert appearance(store, "s2", "Them 1")["person_id"] == first and appearance(store, "s2", "Them 1")["link"] == "confirmed"
    people.appearance_action(store.root, "s2", "Them 1", "unlink")
    fresh = appearance(store, "s2", "Them 1")["person_id"]
    assert fresh not in (first, second) and appearance(store, "s2", "Them 1")["link"] == "confirmed"
    with pytest.raises(people.PersonNotFound):
        people.appearance_action(store.root, "s2", "No such label", "unlink")
    with pytest.raises(people.PersonNotFound):
        people.appearance_action(store.root, "s2", "Them 1", "assign", "p_missing")
    with pytest.raises(ValueError):
        people.appearance_action(store.root, "s2", "Them 1", "explode")


def test_unlinking_the_last_appearance_garbage_collects_the_old_person(store):
    record(store, "s1", {"A": ("Them 1", basis(0), 30)})
    old = appearance(store, "s1", "Them 1")["person_id"]
    people.appearance_action(store.root, "s1", "Them 1", "unlink")
    assert old not in [p["id"] for p in people.list_people(store)]


def test_delete_person_keeps_appearances_with_plain_labels(store):
    record(store, "s1", {"A": ("Them 1", basis(0), 30), "B": ("Them 2", basis(1), 30)})
    pid = appearance(store, "s1", "Them 1")["person_id"]
    people.delete_person(store.root, pid)
    assert appearance(store, "s1", "Them 1")["person_id"] is None
    assert list(people.overlay(store.root, "s1")) == ["Them 2"]
    with pytest.raises(people.PersonNotFound):
        people.delete_person(store.root, pid)


def test_delete_all_removes_everything(store):
    record(store, "s1", {"A": ("Them 1", basis(0), 30)})
    assert (store.root / "people").exists()
    people.delete_all(store.root)
    assert not (store.root / "people").exists()
    assert people.list_people(store) == [] and people.overlay(store.root, "s1") == {}


# -- retranscribe -----------------------------------------------------------------------------------------------


def test_retranscribe_replaces_the_voiceprint_and_carries_confirmed_decisions(store):
    record(store, "s1", {"A": ("Them 1", basis(0), 30), "B": ("Them 2", basis(1), 30)})
    chosen = appearance(store, "s1", "Them 2")["person_id"]
    other = appearance(store, "s1", "Them 1")["person_id"]
    people.update_person(store.root, other, {"name": "Ann"})
    people.appearance_action(store.root, "s1", "Them 2", "assign", other)  # Mike decided: Them 2 is Ann
    assert chosen not in [p["id"] for p in people.list_people(store)]
    # the new transcript numbers the same voices the other way round
    record(store, "s1", {"B2": ("Them 1", mix(1, 3, 0.9), 30), "A2": ("Them 2", basis(0), 30)}, job="j2")
    assert vp(store, "s1")["job_id"] == "j2"
    assert appearance(store, "s1", "Them 1")["person_id"] == other and appearance(store, "s1", "Them 1")["link"] == "confirmed"
    assert appearance(store, "s1", "Them 2")["person_id"] != other  # one person per meeting


def test_retranscribe_without_embeddings_drops_the_stale_voiceprint(store):
    record(store, "s1", {"A": ("Them 1", basis(0), 30)})
    people.record_meeting(store, "s1", "j2", {})
    assert not (store.root / "people" / "voiceprints" / "s1.json").exists()
    assert people.list_people(store) == [] and people.overlay(store.root, "s1") == {}


def test_a_speaker_without_a_vector_is_left_out(store):
    ts = TrackSpeakers(Diarization([turn(0, 30, "A")], {}), {"A": "Them 1"})
    make_session(store, "s1")
    people.record_meeting(store, "s1", "j1", {"system": ts})
    assert not (store.root / "people" / "voiceprints" / "s1.json").exists()


# -- trash, purge, delete ---------------------------------------------------------------------------------------


def test_trashed_meetings_are_hidden_and_restore_brings_them_back(store):
    record(store, "s1", {"A": ("Them 1", basis(0), 30)})
    record(store, "s2", {"X": ("Them 1", basis(0), 30)}, job="j2")
    assert people.list_people(store)[0]["meetings"] == 2
    store.trash_session("s2")
    assert people.list_people(store)[0]["meetings"] == 1
    # a trashed meeting's voices no longer shape the matching either
    store.trash_session("s1")
    assert people.list_people(store) == []
    store.restore_session("s1")
    store.restore_session("s2")
    assert people.list_people(store)[0]["meetings"] == 2


def test_record_skips_a_trashed_meeting(store):
    make_session(store, "s1")
    store.trash_session("s1")
    people.record_meeting(store, "s1", "j1", {"system": speakers_for("system", {"A": ("Them 1", basis(0), 30)})})
    assert not (store.root / "people" / "voiceprints" / "s1.json").exists()


def test_purge_forgets_the_session_and_collects_unnamed_people(store):
    record(store, "s1", {"A": ("Them 1", basis(0), 30), "B": ("Them 2", basis(1), 30)})
    named = appearance(store, "s1", "Them 1")["person_id"]
    people.update_person(store.root, named, {"name": "Ann"})
    store.trash_session("s1")
    assert store.purge_trashed("s1")
    assert not (store.root / "people" / "voiceprints" / "s1.json").exists()
    assert not (store.root / "people" / "clips" / "s1").exists()
    left = json.loads((store.root / "people" / "people.json").read_text(encoding="utf-8"))["people"]
    assert [p["name"] for p in left] == ["Ann"]  # the unnamed one is gone, the named one stays
    assert people.list_people(store)[0]["meetings"] == 0


def test_expired_trash_and_hard_delete_forget_too(store):
    record(store, "s1", {"A": ("Them 1", basis(0), 30)})
    record(store, "s2", {"X": ("Them 1", basis(5), 30)}, job="j2")
    store.trash_session("s1", now=1000.0)
    assert store.purge_expired_trash(now=1000.0 + 40 * 86400) == ["s1"]
    assert not (store.root / "people" / "voiceprints" / "s1.json").exists()
    store.delete_session("s2")
    assert not (store.root / "people" / "voiceprints" / "s2.json").exists()
    assert json.loads((store.root / "people" / "people.json").read_text(encoding="utf-8"))["people"] == []


# -- clips ------------------------------------------------------------------------------------------------------


def write_wav(path, seconds, rate=16000):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x01\x00" * int(seconds * rate))


def test_clips_are_cut_from_the_longest_turns(store, tmp_path):
    wav = tmp_path / "system.wav"
    write_wav(wav, 120)
    make_session(store, "s1")
    turns = [turn(0, 40, "A"), turn(50, 53, "A"), turn(60, 70, "A"), turn(80, 80.4, "A"), turn(90, 105, "A")]
    ts = TrackSpeakers(Diarization(turns, {"A": basis(0)}), {"A": "Them 1"})
    people.record_meeting(store, "s1", "j1", {"system": ts}, {"system": wav})
    clips = appearance(store, "s1", "Them 1")["clips"]
    assert clips == ["them-1-1.wav", "them-1-2.wav", "them-1-3.wav"]
    lengths = []
    for name in clips:
        with wave.open(str(store.root / "people" / "clips" / "s1" / name), "rb") as w:
            assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2)
            lengths.append(w.getnframes() / 16000)
    assert lengths == [pytest.approx(12.0), pytest.approx(12.0), pytest.approx(10.0)]  # longest first, capped at 12 s
    pid = appearance(store, "s1", "Them 1")["person_id"]
    assert people.clip_path(store, pid, "s1", "them-1-1.wav").is_file()
    assert people.clip_path(store, pid, "s1", "../x.wav") is None
    assert people.clip_path(store, "p_other", "s1", "them-1-1.wav") is None
    assert people.get_person(store, pid)["samples"][0]["url"] == f"/v1/people/{pid}/clips/s1/them-1-1.wav"


def test_missing_audio_just_means_no_clips(store, tmp_path):
    record(store, "s1", {"A": ("Them 1", basis(0), 30)}, wav_paths={"system": tmp_path / "gone.wav"})
    assert appearance(store, "s1", "Them 1")["clips"] == []


# -- list, profile, quotes --------------------------------------------------------------------------------------


def finish_transcript(store, sid, segments, job_id=None):
    job_id = store.create_job(sid)
    store.update_job(job_id, state=wire.JobState.DONE)
    store.write_transcript(job_id, "md", json.dumps({"segments": segments}))
    return job_id


def test_list_sorts_and_filters(store):
    record(store, "s1", {"A": ("Them 1", basis(0), 100), "B": ("Them 2", basis(1), 40)})
    record(store, "s2", {"X": ("Them 1", basis(1), 70), "Y": ("Them 2", basis(2), 30)}, job="j2")
    store.write_session_meta("s2", {"name": "later", "created": "2026-10-20", "tracks": {}})
    ids = {a["label"]: appearance(store, "s1", a["label"])["person_id"] for a in vp(store, "s1")["speakers"]}
    people.update_person(store.root, ids["Them 2"], {"name": "Zed", "company": "Acme"})
    talking = people.list_people(store, "talking")
    assert [p["display"] for p in talking] == ["Zed", "Person 1", "Person 3"]
    assert talking[0]["talk_seconds"] == 110.0 and talking[0]["meetings"] == 2  # 40 + 70 s: matched across meetings
    assert [p["display"] for p in people.list_people(store, "alpha")] == ["Zed", "Person 1", "Person 3"]  # named first
    recent = people.list_people(store, "recent")
    assert recent[-1]["display"] == "Person 1" and recent[0]["last_heard"] > recent[-1]["last_heard"]
    assert [p["display"] for p in people.list_people(store, q="acme")] == ["Zed"]


def test_profile_has_appearances_samples_and_quotes(store):
    record(store, "s1", {"A": ("Them 1", basis(0), 30), "B": ("Them 2", basis(1), 30)})
    record(store, "s2", {"X": ("Them 1", basis(0), 30)}, job="j2")
    for sid in ("s1", "s2"):
        finish_transcript(store, sid, [
            {"start": 1.0, "end": 3.0, "track": "system", "label": "Them 1", "text": f"budget talk {sid}"},
            {"start": 5.0, "end": 6.0, "track": "system", "label": "Them 2", "text": "not me"},
            {"start": 9.0, "end": 12.0, "track": "mic", "label": "You", "text": "mine"}])
    pid = appearance(store, "s1", "Them 1")["person_id"]
    profile = people.get_person(store, pid)
    assert profile["meetings"] == 2 and len(profile["appearances"]) == 2 and profile["display"] == "Person 1"
    assert {q["text"] for q in profile["quotes"]} == {"budget talk s1", "budget talk s2"}
    assert {q["session_id"] for q in profile["quotes"]} == {"s1", "s2"}
    only = people.quotes(store, pid, meeting_id="s1")
    assert [q["text"] for q in only] == ["budget talk s1"]
    assert people.quotes(store, pid, q="BUDGET", limit=1)[0]["text"].startswith("budget talk")
    assert people.quotes(store, pid, q="nothing like this") == []
    assert "embedding" not in json.dumps(profile)
    with pytest.raises(people.PersonNotFound):
        people.get_person(store, "p_missing")
