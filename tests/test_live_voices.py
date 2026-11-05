"""Names in the live preview from voice (server/live_voices.py): order, matching, switches and the LivePreview hook."""

from __future__ import annotations

import dataclasses
import json
import math
import threading
import time

import pytest
from fastapi.testclient import TestClient

from meeting_notes.server import live as live_mod
from meeting_notes.server import live_voices as lv
from meeting_notes.server import people
from meeting_notes.server import settings as settings_mod
from meeting_notes.server import web
from meeting_notes.server.app import create_app
from meeting_notes.server.live_voices import LiveVoices, Person, match, order_people
from meeting_notes.server.people import TrackSpeakers
from meeting_notes.server.store import Store
from meeting_notes.transcribe.diarize import Diarization, SpeakerTurn
from meeting_notes.transcribe.protocol import Segment

DIM = 64
PCM = b"\x01\x00" * int(16000 * 3.5)  # 3.5 seconds, past MIN_SECONDS


def basis(i: int) -> list:
    v = [0.0] * DIM
    v[i] = 1.0
    return v


def mix(i: int, j: int, cos: float) -> list:
    """A unit vector whose cosine with ``basis(i)`` is ``cos``."""
    v = [0.0] * DIM
    v[i] = cos
    v[j] = math.sqrt(1.0 - cos * cos)
    return v


# -- ordering ---------------------------------------------------------------------------------------------------


def test_order_is_meetings_and_recency_ranks_added_ties_by_id():
    people_ = [
        Person("p1", "Ana", meetings=5, last_heard=100.0),   # meetings rank 0, recent rank 2 -> 2
        Person("p2", "Ben", meetings=3, last_heard=300.0),   # 1 + 0 -> 1
        Person("p3", "Cy", meetings=1, last_heard=200.0),    # 2 + 1 -> 3
        Person("p4", "Di", meetings=1, last_heard=50.0),     # 3 + 3 -> 6
    ]
    assert order_people(people_, []) == ["p2", "p1", "p3", "p4"]
    tied = [Person("p9", "Zed", 2, 10.0), Person("p8", "Yan", 2, 10.0)]
    assert order_people(tied, []) == ["p8", "p9"]


def test_people_heard_in_this_meeting_come_first_newest_first():
    people_ = [Person("p1", "Ana", 9, 900.0), Person("p2", "Ben", 1, 1.0), Person("p3", "Cy", 5, 500.0)]
    assert order_people(people_, ["p2", "p3"]) == ["p2", "p3", "p1"]
    assert order_people(people_, ["gone", "p3"]) == ["p3", "p1", "p2"]  # someone no longer in the roster is skipped


# -- matching ---------------------------------------------------------------------------------------------------


def scorer(scores):
    seen = []

    def score(pid):
        seen.append(pid)
        return scores[pid]

    return score, seen


def test_the_search_stops_at_the_first_sure_match():
    score, seen = scorer({"a": 0.5, "b": 0.8, "c": 0.99})
    assert match("system", ["a", "b", "c"], None, score) == "b"
    assert seen == ["a", "b"]  # the better "c" is never checked


def test_when_nobody_is_sure_everyone_is_scored_and_the_best_is_shown():
    score, seen = scorer({"a": 0.56, "b": 0.72, "c": 0.60})
    assert match("system", ["a", "b", "c"], None, score) == "b"
    assert seen == ["a", "b", "c"]


def test_a_best_score_below_show_names_nobody():
    score, seen = scorer({"a": 0.5, "b": 0.64})
    assert match("system", ["a", "b"], None, score) is None and seen == ["a", "b"]


def test_on_the_mic_your_own_voice_means_you():
    score, seen = scorer({"me": 0.66, "a": 0.99})
    assert match("mic", ["a", "me"], "me", score) is None
    assert seen == ["me"]  # you go first, and the others are not even scored


def test_on_the_mic_someone_else_needs_sure():
    score, _ = scorer({"me": 0.3, "a": 0.74, "b": 0.7})
    assert match("mic", ["a", "b"], "me", score) is None
    score, seen = scorer({"me": 0.3, "a": 0.6, "b": 0.75})
    assert match("mic", ["a", "b"], "me", score) == "b" and seen == ["me", "a", "b"]


def test_the_system_track_never_tries_you():
    score, seen = scorer({"me": 0.99, "a": 0.2})
    assert match("system", ["me", "a"], "me", score) is None
    assert seen == ["a"]


# -- the whole path, with a fake embedder and real people --------------------------------------------------------


def enable(store, **over):
    current = settings_mod.load_settings(store.root)
    settings_mod.save_settings(store.root, dataclasses.replace(
        current, diarization_enabled=True, voice_profiles_enabled=True, **over))


def turns(spec):
    out, t = [], 0.0
    for raw, seconds in spec.items():
        out.append(SpeakerTurn(t, t + seconds, raw))
        t += seconds
    return out


def make_session(store, sid):
    store.ensure_session_dir(sid)
    store.write_session_meta(sid, {"name": sid, "created": "2026-10-01", "tracks": {"system": {}, "mic": {}}})


def person_id(store, sid, label):
    vp = json.loads((store.root / "people" / "voiceprints" / f"{sid}.json").read_text(encoding="utf-8"))
    return next(a["person_id"] for a in vp["speakers"] if a["label"] == label)


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "data"))
    enable(s)
    # Meeting 1: you on the mic, three voices on the system track. Meeting 2: voice 1 again.
    make_session(s, "s1")
    mic = TrackSpeakers(Diarization(turns({"M0": 40}), {"M0": basis(0)}), labels={}, self_raw="M0", self_label="You")
    system = TrackSpeakers(Diarization(turns({"A": 30, "B": 30, "C": 30}),
                                       {"A": basis(1), "B": basis(2), "C": basis(3)}),
                           labels={"A": "Them 1", "B": "Them 2", "C": "Them 3"})
    people.record_meeting(s, "s1", "j1", {"mic": mic, "system": system})
    make_session(s, "s2")
    again = TrackSpeakers(Diarization(turns({"A": 30}), {"A": basis(1)}), labels={"A": "Them 1"})
    people.record_meeting(s, "s2", "j2", {"system": again})
    people.update_person(s.root, person_id(s, "s1", "Them 1"), {"name": "Ana Park"})
    people.update_person(s.root, person_id(s, "s1", "Them 2"), {"name": "Ben Ortiz"})
    return s  # "Them 3" stays unnamed, and so does you


class Rig:
    """A LiveVoices with a fake embedder that returns whatever ``vector`` is set to."""

    def __init__(self, store, **kw):
        self.vector = basis(1)
        self.embeds = 0
        self.fail = False

        def embed(audio):
            self.embeds += 1
            if self.fail:
                raise RuntimeError("model fell over")
            return self.vector

        self.voices = LiveVoices(store, embedder_loader=lambda: embed, **kw)

    def settle(self):
        assert self.voices._embed_ready.wait(5) and self.voices._roster_idle.wait(5)

    def warm(self, session="live1"):
        self.voices.identify(session, "system", PCM)  # first call starts the loads and answers nothing
        self.settle()

    def who(self, track="system", session="live1", pcm=PCM):
        return self.voices.identify(session, track, pcm)


def test_names_a_known_voice_and_nobody_unsure(store):
    rig = Rig(store)
    assert rig.who() == ""  # first call: the model and the people are still loading, so nothing yet
    rig.settle()
    assert rig.who() == "Ana Park"
    rig.vector = basis(2)
    assert rig.who() == "Ben Ortiz"
    rig.vector = mix(1, 9, 0.6)
    assert rig.who() == ""  # below SHOW
    rig.vector = mix(1, 9, 0.7)
    assert rig.who() == "Ana Park"  # between SHOW and SURE: shown, being the best


def test_unnamed_people_are_never_shown(store):
    rig = Rig(store)
    rig.warm()
    assert {p.name for p in rig.voices._roster.people} == {"Ana Park", "Ben Ortiz"}
    rig.vector = basis(3)  # the unnamed "Them 3"
    assert rig.who() == ""


def test_on_the_mic_you_are_the_default_and_others_need_sure(store):
    rig = Rig(store)
    rig.warm()
    assert rig.voices._roster.self_id  # you are kept even though unnamed
    rig.vector = basis(0)
    assert rig.who("mic") == "" and rig.who("system") == ""  # you on the mic is "You"; on the system track nobody
    rig.vector = basis(1)
    assert rig.who("mic") == "Ana Park"
    rig.vector = mix(1, 9, 0.7)
    assert rig.who("mic") == "" and rig.who("system") == "Ana Park"


def test_short_audio_gives_nothing_and_is_not_embedded(store):
    rig = Rig(store)
    rig.warm()
    before = rig.embeds
    assert rig.who(pcm=b"\x01\x00" * int(16000 * 2.9)) == "" and rig.embeds == before
    assert rig.who() == "Ana Park"


@pytest.mark.parametrize("over", [{"live_voice_names": False}, {"voice_profiles_enabled": False},
                                  {"diarization_enabled": False}])
def test_off_when_the_setting_or_voice_profiles_are_off(store, over):
    rig = Rig(store)
    rig.warm()
    assert rig.who() == "Ana Park"
    settings_mod.save_settings(store.root, dataclasses.replace(settings_mod.load_settings(store.root), **over))
    before = rig.embeds
    assert rig.who() == "" and rig.embeds == before  # not even embedded


def test_an_embedder_that_raises_gives_nothing(store):
    rig = Rig(store)
    rig.warm()
    rig.fail = True
    assert rig.who() == ""
    rig.fail = False
    assert rig.who() == "Ana Park"  # and it recovers


def test_a_model_that_will_not_load_stays_off_quietly(store, caplog):
    def broken():
        raise ImportError("no pyannote here")

    voices = LiveVoices(store, embedder_loader=broken)
    assert voices.identify("s", "system", PCM) == ""
    assert voices._embed_ready.wait(5) and voices._roster_idle.wait(5)
    assert voices.identify("s", "system", PCM) == "" and voices.identify("s", "system", PCM) == ""
    assert sum("live voice names are off" in r.message for r in caplog.records) == 1  # said once


def test_a_busy_embedder_skips_the_utterance_instead_of_waiting(store):
    rig = Rig(store)
    rig.warm()
    before = rig.embeds
    answers = []
    with rig.voices._embed_lock:  # another session is embedding right now
        worker = threading.Thread(target=lambda: answers.append(rig.who(session="live2")))
        worker.start()
        worker.join(3)
        assert not worker.is_alive()
    assert answers == [""] and rig.embeds == before


def test_the_roster_is_never_built_on_the_utterance_path(store):
    release = threading.Event()
    built_on = []
    rig = Rig(store)
    real = rig.voices._build_roster

    def slow(unless):
        built_on.append(threading.current_thread())
        release.wait(5)
        return real(unless)

    rig.voices._build_roster = slow
    started = time.monotonic()
    assert rig.who() == "" and time.monotonic() - started < 2  # did not wait for the build
    release.set()
    rig.settle()
    assert built_on and all(t is not threading.current_thread() for t in built_on)
    assert rig.who() == "Ana Park"


def test_the_roster_follows_the_people_and_keeps_the_last_good_one(store):
    rig = Rig(store)
    rig.warm()
    rig.vector = basis(3)
    assert rig.who() == ""
    people.update_person(store.root, person_id(store, "s1", "Them 3"), {"name": "Cy Wu"})
    rig.voices._roster_checked = 0.0  # the periodic check is due
    rig.who()
    rig.settle()
    assert rig.who() == "Cy Wu"
    revision = rig.voices._roster.revision
    rig.voices._build_roster = lambda unless: (_ for _ in ()).throw(OSError("disk"))
    rig.voices._roster_checked = 0.0
    rig.who()
    rig.settle()
    assert rig.voices._roster.revision == revision and rig.who() == "Cy Wu"


def test_people_heard_in_this_meeting_are_tried_first(store):
    rig = Rig(store)
    rig.warm()
    rig.vector = basis(2)
    assert rig.who(session="a") == "Ben Ortiz"
    assert rig.voices._heard["a"] == [person_id(store, "s1", "Them 2")]
    rig.voices.forget_session("a")
    assert "a" not in rig.voices._heard


# -- LivePreview hook ---------------------------------------------------------------------------------------------


class LocalTranscriber:
    def transcribe(self, wav_path, track):
        return [Segment(start=0.0, end=1.0, text="first", track=track), Segment(start=1.0, end=2.0, text="second", track=track)]


class StubVoices:
    def __init__(self, name="", boom=False):
        self.name, self.boom, self.calls, self.forgotten = name, boom, [], []

    def identify(self, session_id, track, pcm):
        self.calls.append((session_id, track, len(pcm)))
        if self.boom:
            raise RuntimeError("embedder exploded")
        return self.name

    def forget_session(self, session_id):
        self.forgotten.append(session_id)


def preview_with(monkeypatch, voices, chunks=1):
    preview = live_mod.LivePreview(lambda: LocalTranscriber(), voices=voices)
    pcm = b"\x01\x00" * 48000
    monkeypatch.setattr(live_mod._TrackBuffer, "take_mature_chunks",
                        lambda self, m: [(32000 * i, 32000 * i + len(pcm) // 2, pcm) for i in range(chunks)])
    preview.feed("s1", "system", pcm)
    return preview


def test_poll_puts_the_name_on_every_partial_of_the_chunk_once(monkeypatch):
    voices = StubVoices("Ana Park")
    partials = preview_with(monkeypatch, voices, chunks=2).poll("s1", "system")
    assert [p.speaker for p in partials] == ["Ana Park"] * 4
    assert len(voices.calls) == 2 and voices.calls[0][:2] == ("s1", "system")  # one ask per chunk, not per segment


def test_poll_leaves_speaker_empty_when_nobody_is_named_or_the_voices_fail(monkeypatch):
    assert [p.speaker for p in preview_with(monkeypatch, StubVoices("")).poll("s1", "system")] == ["", ""]
    out = preview_with(monkeypatch, StubVoices(boom=True)).poll("s1", "system")
    assert [(p.text, p.speaker) for p in out] == [("first", ""), ("second", "")]  # the preview still goes out
    assert [p.speaker for p in preview_with(monkeypatch, None).poll("s1", "system")] == ["", ""]


def test_poll_with_a_real_failing_embedder_still_sends_the_partials(monkeypatch, store):
    rig = Rig(store)
    rig.warm("s1")
    rig.fail = True
    out = preview_with(monkeypatch, rig.voices).poll("s1", "system")
    assert [(p.text, p.speaker) for p in out] == [("first", ""), ("second", "")]


def test_forgetting_a_session_reaches_the_voices(monkeypatch):
    voices = StubVoices()
    preview_with(monkeypatch, voices).forget_session("s1")
    assert voices.forgotten == ["s1"]


def test_the_partial_carries_speaker_on_the_wire():
    from meeting_notes import wire

    assert wire.to_json(wire.Partial(track="mic", start=0.0, end=1.0, text="hi"))["speaker"] == ""
    assert wire.to_json(wire.Partial(track="system", start=0.0, end=1.0, text="hi", speaker="Ana Park"))["speaker"] == "Ana Park"


# -- settings and the web page --------------------------------------------------------------------------------


def test_the_setting_defaults_on_round_trips_and_is_kept_when_left_out(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    assert settings_mod.Settings().live_voice_names is True
    assert "live_voice_names" in settings_mod.KEEP_IF_MISSING
    base = {"model": "small.en", "beam_size": 5, "audio_retention_days": -1, "retention_check_interval_minutes": 60}
    assert settings_mod.validate(base).live_voice_names is True
    assert settings_mod.validate({**base, "live_voice_names": "0"}).live_voice_names is False
    app = create_app(transcriber_factory=lambda **kw: None, data_root=str(tmp_path / "data"))
    with TestClient(app) as client:
        root = tmp_path / "data"
        assert client.get("/v1/settings").json()["live_voice_names"] is True
        assert client.put("/v1/settings", json={**base, "live_voice_names": False}).status_code == 200
        assert settings_mod.load_settings(root).live_voice_names is False
        assert client.get("/v1/settings").json()["live_voice_names"] is False
        assert client.put("/v1/settings", json=base).status_code == 200
        assert settings_mod.load_settings(root).live_voice_names is False  # left out of a save: kept
        page = client.get("/settings").text
        assert 'name="live_voice_names"' in page and "Name people in the live preview" in page
        assert 'id="live-voice-names" value="on">' in page  # off, so unchecked
    on = web.render_settings_page(settings_mod.Settings(), token_configured=True)
    assert 'id="live-voice-names" value="on" checked' in on


def test_the_web_live_lines_show_the_speaker_name_escaped():
    home = web.render_home_page(token_configured=False)
    assert "function liveWho(p) { return p.speaker ? String(p.speaker) :" in home
    assert "escapeHtml(liveWho(p))" in home  # a name is data: escaped where the line is built as HTML
    assert "who.textContent = liveWho(p) + ':'" in home  # and set as text where it is built as nodes


def test_the_client_passes_the_speaker_on_and_an_old_server_gives_none():
    from meeting_notes.client.streamer import LiveStreamer

    got = []
    streamer = LiveStreamer("http://meeting.lan", on_partial=got.append)
    streamer._handle_message(json.dumps({"type": "partial", "track": "system", "start": 1, "end": 2, "text": "hi",
                                         "speaker": "Ana Park"}))
    streamer._handle_message(json.dumps({"type": "partial", "track": "system", "start": 3, "end": 4, "text": "yo"}))
    assert [(p.text, p.speaker) for p in got] == [("hi", "Ana Park"), ("yo", "")]


def test_a_close_second_names_nobody():
    """Below SURE a mix of two voices can fit both: the best must clearly beat the next person."""
    scores = {"a": 0.70, "b": 0.66}
    assert lv.match("system", ["a", "b"], None, scores.__getitem__) is None
    scores = {"a": 0.70, "b": 0.55}
    assert lv.match("system", ["a", "b"], None, scores.__getitem__) == "a"


def test_a_voice_closest_to_yours_names_nobody_on_the_system_track():
    scores = {"me": 0.72, "a": 0.68}
    assert lv.match("system", ["me", "a"], "me", scores.__getitem__) is None


def test_a_failed_model_load_is_tried_again(monkeypatch):
    voices = lv.LiveVoices(object(), settings_loader=lambda: None, embedder_loader=lambda: 1 / 0)
    assert voices._want_embedder() is None
    assert voices._embed_ready.wait(5)
    assert voices._want_embedder() is None and voices._embed_state == "failed"  # not straight away
    voices._embedder_loader = lambda: "embed"
    voices._embed_failed -= lv.RETRY_LOAD_SECONDS + 1
    voices._want_embedder()
    assert voices._embed_ready.wait(5)
    assert voices._want_embedder() == "embed"
