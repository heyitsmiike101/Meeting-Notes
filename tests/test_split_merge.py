"""Split a meeting (with recommended points) and combine meetings: logic, API, AI contract."""

from __future__ import annotations

import dataclasses
import json

import numpy as np
import pytest
from fastapi.testclient import TestClient

from meeting_notes import wire
from meeting_notes.server import settings as settings_mod
from meeting_notes.server import splitmerge as sm
from meeting_notes.server.app import create_app
from meeting_notes.timing import load_timing_log
from tests.split_helpers import CLOCK_SR, RATE, make_meeting, ramp, read_wav, seg, timing_entries

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated:DeprecationWarning"
)

TOKEN = "split-secret"
HDR = {"Authorization": f"Bearer {TOKEN}"}
T0 = 1_760_000_000.0  # a fixed "meeting started" epoch


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_NOTES_TOKEN", TOKEN)
    return create_app(data_root=str(tmp_path / "data"))


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def store(app):
    return app.state.store


def set_ai(store, provider):
    current = settings_mod.load_settings(store.root)
    settings_mod.save_settings(store.root, dataclasses.replace(current, ai_provider=provider))


def long_meeting(store, sid="long", *, silent=((100, 400),), gaps=(), started=T0, duration=600, segments=None):
    """600 s meeting; mic clock starts at t=1000, system clock 2 s later (so system frame 0 is session 2 s)."""
    mic = ramp(duration, silent=silent)
    system = ramp(duration - 2, silent=[(a - 2, b - 2) for a, b in silent], start_sec=0)
    late = (410, 590) if duration >= 600 else (210, 290)
    segments = segments if segments is not None else [
        seg(10, 90, "You", "Opening remarks"),
        seg(20, 80, "Them", "Replies", "system"),
        seg(*late, "You", "Second topic"),
    ]
    make_meeting(
        store, sid, "Long meeting", started, duration, segments, mic=mic, system=system,
        mic_timing=timing_entries(1000.0, duration, gaps=gaps),
        system_timing=timing_entries(1002.0, duration - 2, gaps=[(a - 2, b - 2, r) for a, b, r in gaps]),
    )


# -- suggestions ---------------------------------------------------------------------------------


def test_silence_on_both_tracks_is_suggested_at_its_midpoint(store):
    long_meeting(store)
    result = sm.suggest_points(sm.load_material(store, "long"))
    assert result["audio_scanned"] is True
    [s] = result["suggestions"]
    assert s["kind"] == "silence" and s["confidence"] == "high"
    assert s["time_sec"] == pytest.approx(250.0, abs=1.0)
    assert "silent" in s["reason"]


def test_silence_from_transcript_only_when_audio_is_gone(store):
    long_meeting(store)
    store.delete_session_audio("long")
    result = sm.suggest_points(sm.load_material(store, "long"))
    assert result["audio_scanned"] is False
    [s] = result["suggestions"]
    assert s["kind"] == "silence" and s["confidence"] == "medium"
    assert s["time_sec"] == pytest.approx(250.0, abs=1.0)  # midpoint of the 90 s..410 s transcript gap = 250


def test_quiet_transcript_but_loud_audio_is_low_confidence(store):
    long_meeting(store, silent=())
    [s] = sm.suggest_points(sm.load_material(store, "long"))["suggestions"]
    assert s["confidence"] == "low" and "not silent" in s["reason"]


def test_silence_shorter_than_two_minutes_is_not_suggested(store):
    long_meeting(store, silent=((100, 200),), segments=[seg(10, 95, "You", "a"), seg(205, 590, "You", "b")])
    assert sm.suggest_points(sm.load_material(store, "long"))["suggestions"] == []


def test_audio_loss_is_suggested_from_gaps_and_in_gap_segments(store):
    segs = [seg(10, 190, "You", "before"), seg(200, 260, "Them", "hallucinated", "system", in_gap=True), seg(270, 590, "You", "after")]
    long_meeting(store, silent=(), gaps=[(200, 260, "stall")], segments=segs)
    result = sm.suggest_points(sm.load_material(store, "long"))
    [s] = result["suggestions"]
    assert s["kind"] == "gap" and s["confidence"] == "high"
    assert s["time_sec"] == pytest.approx(230.0, abs=2.0)
    assert "lost audio" in s["reason"]


def test_late_attach_gap_is_a_low_confidence_suggestion_at_its_end(store):
    segs = [seg(40, 590, "You", "talk")]
    long_meeting(store, silent=(), gaps=[(0, 40, "late-attach")], segments=segs)
    [s] = sm.suggest_points(sm.load_material(store, "long"))["suggestions"]
    assert s["kind"] == "late_attach" and s["confidence"] == "low"
    assert s["time_sec"] == pytest.approx(40.0, abs=2.0)


def test_nearby_suggestions_collapse_to_the_strongest_and_results_are_sorted(store):
    segs = [seg(10, 90, "You", "a"), seg(410, 590, "You", "b")]
    long_meeting(store, segments=segs, gaps=[(250, 280, "stall")])  # 30 s gap sits inside the silence
    material = sm.load_material(store, "long")
    ai = [{"time_sec": 500.0, "title": "Budget"}, {"time_sec": 120.0, "title": "Kickoff"}]
    out = sm.suggest_points(material, ai)["suggestions"]
    assert [s["time_sec"] for s in out] == sorted(s["time_sec"] for s in out)
    assert sum(1 for s in out if abs(s["time_sec"] - 250) < 60) == 1  # silence (high) beat the gap (medium)
    assert {s["label"] for s in out if s["kind"] == "topic"} == {"Budget", "Kickoff"}


def test_suggestions_near_the_edges_are_dropped(store):
    long_meeting(store, silent=(), segments=[seg(0, 5, "You", "a"), seg(200, 300, "You", "b")])
    ai = [{"time_sec": 3.0, "title": "Too early"}, {"time_sec": 597.0, "title": "Too late"}, {"time_sec": 300.0, "title": "Fine"}]
    out = sm.suggest_points(sm.load_material(store, "long"), ai)["suggestions"]
    assert [s["label"] for s in out if s["kind"] == "topic"] == ["Fine"]


# -- split ---------------------------------------------------------------------------------------


def test_split_slices_audio_per_track_and_keeps_tracks_aligned(store):
    long_meeting(store, silent=(), duration=300)
    result = sm.split_session(store, "long", [100, 200])
    ids = [p["session_id"] for p in result["parts"]]
    assert ids == ["long_part1", "long_part2", "long_part3"]

    orig_mic = ramp(300)
    orig_sys = ramp(298)  # the system clock started 2 s into the session
    mic_parts = [read_wav(store.track_wav_path(i, "mic")) for i in ids]
    sys_parts = [read_wav(store.track_wav_path(i, "system")) for i in ids]
    for params, _ in mic_parts + sys_parts:
        assert params[:3] == (1, 2, RATE)  # mono, 16-bit, same rate
    assert [len(d) for _, d in mic_parts] == [100 * RATE] * 3
    # System frame 0 is session second 2: a cut at session 100 lands at system second 98.
    assert [len(d) for _, d in sys_parts] == [98 * RATE, 100 * RATE, 100 * RATE]
    assert np.array_equal(np.concatenate([d for _, d in mic_parts]), orig_mic)
    assert np.array_equal(np.concatenate([d for _, d in sys_parts]), orig_sys)
    assert np.array_equal(mic_parts[1][1], orig_mic[100 * RATE: 200 * RATE])


def test_split_shifts_transcript_segments_and_renders_a_complete_pipeline(store):
    segs = [
        seg(10, 40, "You", "first"), seg(95, 105, "Them", "straddles the cut", "system"),
        seg(130, 150, "You", "second"), seg(250, 290, "You", "third"), seg(210, 220, "Them", "gap", "system", in_gap=True),
    ]
    long_meeting(store, silent=(), duration=300, segments=segs)
    result = sm.split_session(store, "long", [100, 200])
    texts = []
    for part in result["parts"]:
        detail = store.session_detail(part["session_id"])
        assert detail["pipeline"]["transcription"]["state"] == "complete"
        assert detail["transcript_job_id"] and "Meeting transcript" in detail["markdown"]
        texts.append([(s["text"], s["start"], s["end"], s["in_gap"]) for s in detail["segments"]])
        assert all(0 <= s["start"] <= s["end"] <= 100 for s in detail["segments"])
    # the straddling segment's midpoint (100) belongs to part 2, clamped at that part's start
    assert [t[0] for t in texts[0]] == ["first"]
    assert [t[0] for t in texts[1]] == ["straddles the cut", "second"]
    assert texts[1][0][1:3] == (0, 5)  # 95..105 shifted by 100 and clamped at 0
    assert texts[1][1][1:3] == (30, 50)
    assert [t[0] for t in texts[2]] == ["gap", "third"] and texts[2][0][3] is True
    # transcript text is searchable for each part
    assert store.list_sessions(q="third")["items"][0]["session_id"] == "long_part3"


def test_split_creates_indexed_named_timed_sessions_and_trashes_the_original(store):
    long_meeting(store, silent=(), duration=300)
    result = sm.split_session(store, "long", [100, 200], names=["Kickoff", " ", None])
    assert store.is_trashed("long") and not store.session_exists("long")
    assert store.session_index_row("long") is None
    assert result["original"]["trashed"] is True
    listing = {i["session_id"]: i for i in store.list_sessions()["items"]}
    assert set(listing) == {"long_part1", "long_part2", "long_part3"}
    assert [listing[i]["name"] for i in ("long_part1", "long_part2", "long_part3")] == [
        "Kickoff", "Long meeting (part 2)", "Long meeting (part 3)",
    ]
    for n, pid in enumerate(("long_part1", "long_part2", "long_part3")):
        meta = store.read_session_meta(pid)
        assert meta["started_wall"] == pytest.approx(T0 + 100 * n)
        assert meta["duration_sec"] == pytest.approx(100.0)
        assert meta["split"] == {"from": "long", "from_name": "Long meeting", "part": n + 1, "of": 3, "offset_sec": 100.0 * n}
        assert listing[pid]["has_audio"] is True and listing[pid]["device"] == "Laptop"
        assert meta["created"].startswith("20")  # same ISO style as the original
        assert set(meta["tracks"]) == {"mic", "system"}
    # Timing logs keep the tracks' shared clock: frames restart at 0, monotonic times are the originals.
    mic = load_timing_log(store.track_timing_path("long_part2", "mic"))
    system = load_timing_log(store.track_timing_path("long_part2", "system"))
    assert mic.start_monotonic == pytest.approx(1100.0) and system.start_monotonic == pytest.approx(1100.0)
    assert mic.total_frames == pytest.approx(100 * CLOCK_SR, abs=CLOCK_SR * 0.01)
    part1_system = load_timing_log(store.track_timing_path("long_part1", "system"))
    assert part1_system.start_monotonic == pytest.approx(1002.0)  # offset preserved in part 1


def test_split_carries_audio_loss_gaps_into_the_right_part(store):
    long_meeting(store, silent=(), duration=300, gaps=[(150, 170, "stall")])
    sm.split_session(store, "long", [100])
    clock = load_timing_log(store.track_timing_path("long_part2", "mic"))
    [gap] = clock.gaps
    assert gap.frames_before == pytest.approx(50 * CLOCK_SR) and gap.frames_padded == pytest.approx(20 * CLOCK_SR)
    assert load_timing_log(store.track_timing_path("long_part1", "mic")).gaps == []


def test_split_without_audio_makes_transcript_only_parts(store):
    long_meeting(store, silent=(), duration=300)
    store.delete_session_audio("long")
    result = sm.split_session(store, "long", [150])
    assert [p["has_audio"] for p in result["parts"]] == [False, False]
    assert store.session_detail("long_part1")["pipeline"]["transcription"]["state"] == "complete"


def test_split_rejects_bad_points(store):
    long_meeting(store, silent=(), duration=300)
    for points, fragment in [
        ([], "non-empty"), ("x", "non-empty"), ([0], "outside"), ([300], "outside"), ([301], "outside"), ([-5], "outside"),
        ([5], "at least 10 seconds"), ([295], "at least 10 seconds"), ([100, 105], "at least 10 seconds"),
        (["a"], "number"), ([True], "number"), ([float("nan")], "number"),
    ]:
        with pytest.raises(sm.SplitMergeError) as err:
            sm.split_session(store, "long", points)
        assert fragment in str(err.value) and err.value.status == 400, points
    assert store.session_exists("long") and not store.is_trashed("long")
    with pytest.raises(sm.SplitMergeError, match="names"):
        sm.split_session(store, "long", [100], names=["a", "b", "c"])
    with pytest.raises(sm.SplitMergeError, match="200"):
        sm.split_session(store, "long", [100], names=["x" * 201])


def test_split_rejects_live_and_untranscribed_meetings(store):
    long_meeting(store, "live", duration=300)
    with pytest.raises(sm.SplitMergeError, match="still recording"):
        sm.split_session(store, "live", [100], is_live=lambda sid: sid == "live")
    long_meeting(store, "queued", duration=300)
    make_meeting(store, "pending", "Pending", T0, 300, [], mic=ramp(300), job_state=wire.JobState.RUNNING)
    with pytest.raises(sm.SplitMergeError, match="not finished transcribing") as err:
        sm.split_session(store, "pending", [100])
    assert err.value.status == 409
    make_meeting(store, "failed", "Failed", T0, 300, [], mic=ramp(300), job_state=wire.JobState.ERROR)
    with pytest.raises(sm.SplitMergeError, match="not finished"):
        sm.split_session(store, "failed", [100])
    make_meeting(store, "uploading", "Up", T0, 300, [seg(1, 2, "You", "x")], extra_meta={"upload": {"state": "uploading"}})
    with pytest.raises(sm.SplitMergeError, match="still uploading"):
        sm.split_session(store, "uploading", [100])
    with pytest.raises(sm.SplitMergeError) as err:
        sm.split_session(store, "missing", [100])
    assert err.value.status == 404


def test_split_ids_avoid_collisions_and_parts_can_be_split_again(store):
    long_meeting(store, silent=(), duration=300)
    first = sm.split_session(store, "long", [150])
    assert [p["session_id"] for p in first["parts"]] == ["long_part1", "long_part2"]
    again = sm.split_session(store, "long_part1", [75])
    assert [p["session_id"] for p in again["parts"]] == ["long_part1_part1", "long_part1_part2"]
    # an id whose natural part names are taken (here: sitting in trash) gets a distinct batch prefix
    long_meeting(store, "long", silent=(), duration=300)
    third = sm.split_session(store, "long", [150])
    assert [p["session_id"] for p in third["parts"]] == ["long-s2_part1", "long-s2_part2"]


def test_unsplit_restores_the_original_and_removes_the_parts(store):
    long_meeting(store, silent=(), duration=300)
    review = store.create_review("long")
    result = sm.split_session(store, "long", [100, 200])
    assert sm.unsplit(store, "long") == {"restored": "long", "removed": [p["session_id"] for p in result["parts"]]}
    assert store.session_exists("long") and not store.is_trashed("long")
    assert store.read_review(review["review_id"]) is not None  # notes came back with it
    assert [i["session_id"] for i in store.list_sessions()["items"]] == ["long"]
    assert store.list_trash() == []  # the parts are gone, not parked in Recently deleted
    with pytest.raises(sm.SplitMergeError) as err:
        sm.unsplit(store, "long")
    assert err.value.status == 404


def test_split_regenerates_notes_only_when_asked_and_ai_is_on(store):
    long_meeting(store, silent=(), duration=300)
    result = sm.split_session(store, "long", [150], regenerate_notes=True, ai_enabled=False)
    assert result["notes_queued"] == []
    sm.unsplit(store, "long")
    result = sm.split_session(store, "long", [150], regenerate_notes=True, ai_enabled=True)
    assert result["notes_queued"] == ["long_part1", "long_part2"]
    for pid in result["notes_queued"]:
        assert store.review_for_session(pid)["status"] == "queued"


# -- combine -------------------------------------------------------------------------------------


def three_meetings(store):
    """A (0-120 s), B (60 s later, 90 s), C (an hour later, 50 s, system track only)."""
    make_meeting(store, "a", "Kickoff", T0, 120, [seg(5, 30, "You", "a-one"), seg(40, 60, "Them", "a-two", "system")],
                 mic=ramp(120), system=ramp(120, start_sec=1000), device="Laptop")
    make_meeting(store, "b", "Kickoff again", T0 + 180, 90, [seg(5, 10, "You", "b-one")],
                 mic=ramp(90, start_sec=2000), system=ramp(90, start_sec=3000), device="Desk PC")
    make_meeting(store, "c", "Wrap up", T0 + 180 + 90 + 3600, 50, [seg(2, 8, "Them", "c-one", "system")],
                 system=ramp(50, start_sec=4000))


def test_combine_orders_by_start_time_fills_and_caps_the_gaps(store):
    three_meetings(store)
    result = sm.combine_sessions(store, ["c", "a", "b"])
    new_id = result["session_id"]
    assert new_id == "a_combined" and result["sources"] == ["a", "b", "c"]
    total = 120 + 60 + 90 + 600 + 50  # the one-hour gap before C is capped at ten minutes
    assert result["duration_sec"] == total
    params, mic = read_wav(store.track_wav_path(new_id, "mic"))
    assert params == (1, 2, RATE, total * RATE)
    expected_mic = np.zeros(total * RATE, dtype="<i2")
    expected_mic[: 120 * RATE] = ramp(120)
    expected_mic[180 * RATE: 270 * RATE] = ramp(90, start_sec=2000)
    assert np.array_equal(mic, expected_mic)  # silence fills the gaps, and C (no mic track) is silent
    _, system = read_wav(store.track_wav_path(new_id, "system"))
    expected_system = np.zeros(total * RATE, dtype="<i2")
    expected_system[: 120 * RATE] = ramp(120, start_sec=1000)
    expected_system[180 * RATE: 270 * RATE] = ramp(90, start_sec=3000)
    expected_system[870 * RATE: 920 * RATE] = ramp(50, start_sec=4000)
    assert np.array_equal(system, expected_system)


def test_combine_shifts_segments_and_marks_the_gaps(store):
    three_meetings(store)
    new_id = sm.combine_sessions(store, ["a", "b", "c"])["session_id"]
    detail = store.session_detail(new_id)
    assert detail["pipeline"]["transcription"]["state"] == "complete"
    real = [(s["text"], s["start"], s["end"]) for s in detail["segments"] if not s["in_gap"]]
    assert real == [("a-one", 5, 30), ("a-two", 40, 60), ("b-one", 185, 190), ("c-one", 872, 878)]
    gaps = [(s["start"], s["end"]) for s in detail["segments"] if s["in_gap"]]
    assert gaps == [(120, 180), (270, 870)]
    meta = detail["meta"]
    assert meta["combined_from"] == ["a", "b", "c"]
    assert [g["inserted_sec"] for g in meta["combine"]["gaps"]] == [60, 600]
    assert [g["capped"] for g in meta["combine"]["gaps"]] == [False, True]
    assert meta["combine"]["gaps"][1]["real_gap_sec"] == 3600
    assert meta["combine"]["devices"] == ["Laptop", "Desk PC"]  # differing devices are noted; the first is used
    assert meta["device"] == "Laptop" and meta["name"] == "Kickoff" and meta["started_wall"] == T0
    assert meta["duration_sec"] == 920 and set(meta["tracks"]) == {"mic", "system"}
    assert "Audio lost" not in detail["markdown"] and "audio lost for 60 seconds" in detail["markdown"]
    # the synthetic timing log places both gaps, so a later retranscribe marks them too
    clock = load_timing_log(store.track_timing_path(new_id, "mic"))
    assert clock.samplerate == RATE
    assert [round(g.frames_padded / clock.samplerate) for g in clock.gaps] == [60, 600]
    assert clock.total_frames == 920 * RATE


def test_combine_moves_originals_to_trash_and_indexes_the_result(store):
    three_meetings(store)
    sm.combine_sessions(store, ["a", "b"], name="  Kickoff, both halves  ")
    assert store.is_trashed("a") and store.is_trashed("b") and store.session_exists("c")
    items = {i["session_id"]: i for i in store.list_sessions()["items"]}
    assert set(items) == {"a_combined", "c"}
    assert items["a_combined"]["name"] == "Kickoff, both halves" and items["a_combined"]["has_audio"] is True
    assert {t["session_id"] for t in store.list_trash()} == {"a", "b"}
    assert store.list_sessions(q="b-one")["items"][0]["session_id"] == "a_combined"
    assert store.list_trash()[0]["deleted_via"] == "combine"


def test_combine_the_24_second_fragment_with_the_restart_a_minute_later(store):
    make_meeting(store, "frag", "Standup", T0, 24, [seg(2, 20, "You", "cut off mid")], mic=ramp(24), system=ramp(24))
    make_meeting(store, "rest", "Standup", T0 + 24 + 62, 300, [seg(3, 280, "You", "the real meeting")], mic=ramp(300), system=ramp(300))
    result = sm.combine_sessions(store, ["rest", "frag"])
    assert result["duration_sec"] == 24 + 62 + 300 and result["gaps"][0]["inserted_sec"] == 62
    assert store.read_session_meta(result["session_id"])["name"] == "Standup"


def test_combine_overlapping_recordings_insert_no_silence(store):
    make_meeting(store, "a", "A", T0, 100, [seg(1, 2, "You", "x")], mic=ramp(100))
    make_meeting(store, "b", "B", T0 + 95, 100, [seg(1, 2, "You", "y")], mic=ramp(100))
    result = sm.combine_sessions(store, ["a", "b"])
    assert result["duration_sec"] == 200 and result["gaps"] == []
    meta = store.read_session_meta(result["session_id"])
    assert meta["combine"]["parts"][1]["overlap"] is True


def test_combine_rejects_deleted_audio_with_a_clear_message(store):
    three_meetings(store)
    store.delete_session_audio("b")
    with pytest.raises(sm.SplitMergeError, match="audio was deleted for Kickoff again; combining needs audio") as err:
        sm.combine_sessions(store, ["a", "b"])
    assert err.value.status == 409
    assert store.session_exists("a") and store.session_exists("b")


def test_combine_validation(store):
    three_meetings(store)
    for ids, fragment in [
        (["a"], "at least two"), ("a,b", "at least two"), (["a", "a"], "distinct"), ([1, 2], "distinct"),
        (list("abcdefghijklmnopqrstuvwxyz"), "at most"),
    ]:
        with pytest.raises(sm.SplitMergeError, match=fragment):
            sm.combine_sessions(store, ids)
    with pytest.raises(sm.SplitMergeError) as err:
        sm.combine_sessions(store, ["a", "nope"])
    assert err.value.status == 404
    with pytest.raises(sm.SplitMergeError, match="still recording"):
        sm.combine_sessions(store, ["a", "b"], is_live=lambda sid: sid == "b")
    make_meeting(store, "p", "P", T0 + 9999, 30, [], mic=ramp(30), job_state=wire.JobState.QUEUED)
    with pytest.raises(sm.SplitMergeError, match="has not finished transcribing"):
        sm.combine_sessions(store, ["a", "p"])
    with pytest.raises(sm.SplitMergeError, match="200 characters"):
        sm.combine_sessions(store, ["a", "b"], name="x" * 201)
    assert not store.is_trashed("a")


def test_combine_rejects_mixed_audio_formats(store):
    make_meeting(store, "a", "A", T0, 30, [seg(1, 2, "You", "x")], mic=ramp(30))
    make_meeting(store, "b", "B", T0 + 100, 30, [seg(1, 2, "You", "y")], mic=ramp(30))
    from tests.split_helpers import write_wav
    write_wav(store.track_wav_path("b", "mic"), ramp(30, rate=8000), rate=8000)
    with pytest.raises(sm.SplitMergeError, match="different audio format"):
        sm.combine_sessions(store, ["a", "b"])


def test_uncombine_restores_every_original_and_removes_the_combined_meeting(store):
    three_meetings(store)
    new_id = sm.combine_sessions(store, ["a", "b"])["session_id"]
    assert sm.uncombine(store, new_id) == {"restored": ["a", "b"], "removed": new_id}
    assert store.session_exists("a") and store.session_exists("b") and not store.session_exists(new_id)
    assert store.list_trash() == []
    with pytest.raises(sm.SplitMergeError) as err:
        sm.uncombine(store, "c")
    assert err.value.status == 409


def test_uncombine_refuses_when_an_original_has_left_the_trash(store):
    three_meetings(store)
    new_id = sm.combine_sessions(store, ["a", "b"])["session_id"]
    store.purge_trashed("b")
    with pytest.raises(sm.SplitMergeError, match="no longer in Recently deleted"):
        sm.uncombine(store, new_id)
    assert store.session_exists(new_id) and store.is_trashed("a")


def test_combine_regenerates_notes_when_asked(store):
    three_meetings(store)
    result = sm.combine_sessions(store, ["a", "b"], regenerate_notes=True, ai_enabled=True)
    assert result["notes_queued"] == ["a_combined"]
    assert store.review_for_session("a_combined")["status"] == "queued"


# -- continuation hint -----------------------------------------------------------------------------


def test_continuation_hint_finds_adjacent_meetings_from_the_same_device(store):
    make_meeting(store, "first", "Weekly sync", T0, 24, [seg(1, 2, "You", "x")], device="Laptop")
    make_meeting(store, "second", "Weekly sync", T0 + 24 + 60, 600, [seg(1, 2, "You", "y")], device="Laptop")
    make_meeting(store, "other-device", "Elsewhere", T0 + 24 + 30, 100, [seg(1, 2, "You", "z")], device="Desk")
    make_meeting(store, "later", "Much later", T0 + 24 + 60 + 600 + 3600, 100, [seg(1, 2, "You", "w")], device="Laptop")
    second = sm.continuations(store, "second")
    assert second["previous"]["session_id"] == "first" and second["previous"]["gap_sec"] == pytest.approx(60, abs=1)
    assert second["next"] is None
    first = sm.continuations(store, "first")
    assert first["next"]["session_id"] == "second" and first["previous"] is None
    assert sm.continuations(store, "later") == {"previous": None, "next": None}


# -- HTTP API --------------------------------------------------------------------------------------


def test_every_new_route_requires_the_token(client, store):
    long_meeting(store, duration=300)
    for method, path, body in [
        ("get", "/v1/sessions/long/split-suggestions", None), ("post", "/v1/sessions/long/split-suggestions/ai", None),
        ("post", "/v1/sessions/long/split", {"points": [100]}), ("post", "/v1/sessions/long/unsplit", None),
        ("post", "/v1/sessions/combine", {"ids": ["a", "b"]}), ("post", "/v1/sessions/long/uncombine", None),
        ("get", "/v1/sessions/long/continuations", None),
    ]:
        response = getattr(client, method)(path, **({"json": body} if body is not None else {}))
        assert response.status_code == 401, path
    assert store.session_exists("long")


def test_split_api_round_trip_with_undo(client, store):
    long_meeting(store, silent=(), duration=300)
    bad = client.post("/v1/sessions/long/split", headers=HDR, json={"points": [3]})
    assert bad.status_code == 400 and "at least 10 seconds" in bad.json()["detail"]
    assert client.post("/v1/sessions/long/split", headers=HDR, json={"points": [100], "extra": 1}).status_code == 400
    assert client.post("/v1/sessions/long/split", headers=HDR, json={"points": [100], "regenerate_notes": "yes"}).status_code == 400
    assert client.post("/v1/sessions/long/split", headers=HDR, content=b"not json").status_code == 400
    ok = client.post("/v1/sessions/long/split", headers=HDR, json={"points": [100, 200], "names": ["A", "B", "C"]})
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert [p["name"] for p in body["parts"]] == ["A", "B", "C"]
    listed = client.get("/v1/sessions", headers=HDR).json()
    assert {i["session_id"] for i in listed["items"]} == {"long_part1", "long_part2", "long_part3"}
    assert client.get("/v1/sessions/long", headers=HDR).status_code == 404
    trash = client.get("/v1/trash", headers=HDR).json()
    assert [t["session_id"] for t in trash["items"]] == ["long"]
    undo = client.post(body["undo"], headers=HDR)
    assert undo.status_code == 200 and undo.json()["restored"] == "long"
    assert client.get("/v1/sessions/long", headers=HDR).status_code == 200
    assert client.get("/v1/sessions/long_part2", headers=HDR).status_code == 404
    assert client.post("/v1/sessions/long/unsplit", headers=HDR).status_code == 404


def test_split_api_rejects_a_live_meeting(app, client, store):
    long_meeting(store, duration=300)
    app.state.live_sessions["long"] = {"session_id": "long"}
    response = client.post("/v1/sessions/long/split", headers=HDR, json={"points": [100]})
    assert response.status_code == 409 and "still recording" in response.json()["detail"]
    app.state.live_sessions.clear()


def test_combine_api_round_trip_with_undo(client, store):
    three_meetings(store)
    bad = client.post("/v1/sessions/combine", headers=HDR, json={"ids": ["a"]})
    assert bad.status_code == 400
    store.delete_session_audio("c")
    gone = client.post("/v1/sessions/combine", headers=HDR, json={"ids": ["a", "c"]})
    assert gone.status_code == 409 and "audio was deleted for Wrap up" in gone.json()["detail"]
    ok = client.post("/v1/sessions/combine", headers=HDR, json={"ids": ["b", "a"], "name": "Both", "regenerate_notes": True})
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["name"] == "Both" and body["sources"] == ["a", "b"]
    assert client.get(f"/v1/sessions/{body['session_id']}", headers=HDR).json()["meta"]["name"] == "Both"
    assert len(client.get("/v1/trash", headers=HDR).json()["items"]) == 2
    undo = client.post(body["undo"], headers=HDR)
    assert undo.status_code == 200 and undo.json()["restored"] == ["a", "b"]
    assert client.get("/v1/sessions/a", headers=HDR).status_code == 200


def test_suggestions_api_and_continuations_api(client, store):
    long_meeting(store)
    data = client.get("/v1/sessions/long/split-suggestions", headers=HDR).json()
    assert data["duration_sec"] == 600 and data["ai_available"] is True
    assert data["ai"] == {"status": "none", "suggestions": []}
    assert data["suggestions"][0]["kind"] == "silence"
    assert set(data["suggestions"][0]) == {"time_sec", "reason", "confidence", "label", "kind"}
    assert client.get("/v1/sessions/nope/split-suggestions", headers=HDR).status_code == 404
    assert client.get("/v1/sessions/long/continuations", headers=HDR).json() == {"previous": None, "next": None}


def test_agent_keys_cannot_reach_split_or_combine(app, client, store):
    long_meeting(store, duration=300)
    key = client.post("/v1/agent-keys", headers=HDR, json={"name": "bot", "scopes": ["read", "write"]})
    assert key.status_code == 201
    token = key.json()["key"]
    agent = {"Authorization": f"Bearer {token}"}
    assert client.post("/v1/sessions/long/split", headers=agent, json={"points": [100]}).status_code == 403
    assert client.post("/v1/sessions/combine", headers=agent, json={"ids": ["a", "b"]}).status_code == 403
    assert client.get("/v1/sessions/long/split-suggestions", headers=agent).status_code == 403
    assert store.session_exists("long")


# -- optional AI topic shifts (fake bridge) ---------------------------------------------------------


def test_ai_suggestions_are_optional_async_and_follow_the_bridge_contract(client, store):
    long_meeting(store)
    set_ai(store, "disabled")
    off = client.post("/v1/sessions/long/split-suggestions/ai", headers=HDR)
    assert off.status_code == 409 and "AI provider" in off.json()["detail"]
    assert client.get("/v1/sessions/long/split-suggestions", headers=HDR).json()["ai_available"] is False
    set_ai(store, "ollama")

    queued = client.post("/v1/sessions/long/split-suggestions/ai", headers=HDR)
    assert queued.status_code == 200 and queued.json()["status"] == "queued"
    job_id = queued.json()["job_id"]
    assert client.post("/v1/sessions/long/split-suggestions/ai", headers=HDR).json()["job_id"] == job_id  # idempotent
    assert client.get("/v1/sessions/long/split-suggestions", headers=HDR).json()["ai"]["status"] == "queued"

    # A bridge that predates this kind never asks for it, so never receives it ...
    assert client.get("/v1/bridge/review/claim?worker_id=old", headers=HDR).status_code == 204
    # ... one that does gets it, with its own transcript and workflow.
    claim = client.get("/v1/bridge/review/claim?worker_id=new&kinds=notes,split_suggestions", headers=HDR)
    assert claim.status_code == 200
    job = claim.json()
    assert job["id"] == job_id and job["kind"] == "split_suggestions" and job["provider"]["name"] == "ollama"
    transcript = client.get(job["transcript_url"], headers=HDR).text
    assert "[410.0s] You: Second topic" in transcript and "[10.0s] You: Opening remarks" in transcript
    workflow = client.get(job["workflow_url"], headers=HDR)
    assert "time_sec" in workflow.text
    assert client.get("/v1/sessions/long/split-suggestions", headers=HDR).json()["ai"]["status"] == "running"

    bad = client.post(f"/v1/bridge/split-suggestions/{job_id}/complete", headers=HDR, json={"suggestions": [{"time_sec": "x", "title": "t"}]})
    assert bad.status_code == 400
    done = client.post(
        f"/v1/bridge/split-suggestions/{job_id}/complete", headers=HDR,
        json={"suggestions": [{"time_sec": 120, "title": "Kickoff"}, {"time_sec": 500.5, "title": "Budget"}]},
    )
    assert done.status_code == 200 and done.json()["status"] == "done"
    data = client.get("/v1/sessions/long/split-suggestions", headers=HDR).json()
    assert data["ai"]["status"] == "done"
    topics = [s for s in data["suggestions"] if s["kind"] == "topic"]
    assert [(s["time_sec"], s["label"]) for s in topics] == [(120.0, "Kickoff"), (500.5, "Budget")]
    assert client.post(f"/v1/bridge/split-suggestions/{job_id}/complete", headers=HDR, json={"suggestions": []}).status_code == 409

    # a fresh transcript makes the old answer stale
    new_job = store.create_job("long")
    store.write_transcript(new_job, "# x", json.dumps({"segments": [seg(1, 2, "You", "new")]}))
    store.update_job(new_job, state=wire.JobState.DONE, progress=1.0)
    assert client.get("/v1/sessions/long/split-suggestions", headers=HDR).json()["ai"]["status"] == "none"


def test_ai_job_failure_is_reported_and_can_be_requeued(client, store):
    long_meeting(store)
    set_ai(store, "claude")
    job_id = client.post("/v1/sessions/long/split-suggestions/ai", headers=HDR).json()["job_id"]
    client.get("/v1/bridge/review/claim?kinds=split_suggestions", headers=HDR)
    failed = client.post(f"/v1/bridge/split-suggestions/{job_id}/failure", headers=HDR, json={"error": "model unavailable"})
    assert failed.status_code == 200
    ai = client.get("/v1/sessions/long/split-suggestions", headers=HDR).json()["ai"]
    assert ai["status"] == "error" and ai["error"] == "model unavailable"
    retry = client.post("/v1/sessions/long/split-suggestions/ai", headers=HDR).json()
    assert retry["status"] == "queued" and retry["job_id"] != job_id
    assert client.post(f"/v1/bridge/split-suggestions/{job_id}/failure", headers=HDR, json={"error": ""}).status_code in (400, 409)


def test_bridge_worker_handles_the_split_kind_end_to_end(app, client, store, monkeypatch):
    """The real ``BridgeWorker`` against the real server routes, with the model call faked."""
    import httpx
    from meeting_notes.bridge import BridgeConfig, BridgeWorker

    long_meeting(store)
    set_ai(store, "ollama")
    client.post("/v1/sessions/long/split-suggestions/ai", headers=HDR)

    def handler(request: httpx.Request) -> httpx.Response:
        response = client.request(
            request.method, request.url.raw_path.decode(), headers={"Authorization": f"Bearer {TOKEN}"},
            content=request.content or None,
        )
        return httpx.Response(response.status_code, content=response.content, headers={"content-type": response.headers["content-type"]} if response.content else {})

    worker = BridgeWorker(BridgeConfig("http://server", TOKEN, poll_seconds=0.1, provider="ollama"))
    worker.client = httpx.Client(transport=httpx.MockTransport(handler))
    seen = {}

    def fake_provider(name, transcript, workflow, output, schema, provider):
        seen["prompt"] = worker._prompt()
        seen["schema"] = json.loads(schema.read_text())
        seen["transcript"] = transcript.read_text()
        output.write_text(json.dumps({"suggestions": [{"time_sec": 410.0, "title": "Second topic"}]}))

    monkeypatch.setattr(worker, "_run_provider", fake_provider)
    worker.run(once=True)
    assert "topic changes" in seen["prompt"] and "untrusted" in seen["prompt"]
    assert "suggestions" in seen["schema"]["properties"] and "[410.0s]" in seen["transcript"]
    data = client.get("/v1/sessions/long/split-suggestions", headers=HDR).json()
    assert data["ai"]["status"] == "done" and data["ai"]["suggestions"] == [{"time_sec": 410.0, "title": "Second topic"}]
    assert worker._prompt_override is None  # notes jobs afterwards use the notes prompt again
    assert "topic changes" not in worker._prompt()


# -- web UI markup / script ------------------------------------------------------------------------


def _page():
    from meeting_notes.server import web

    return web.render_transcriptions_page(token_configured=True, ai_enabled=True)


def test_meetings_page_has_split_and_combine_entry_points_and_dialogs():
    page = _page()
    assert 'id="split-meeting"' in page and "Split meeting…" in page  # "…" menu item
    assert 'id="bulk-combine"' in page and ">Combine<" in page  # bulk bar
    for dom_id in (
        "split-dialog", "split-tl", "split-ai", "split-time", "split-sugs", "split-parts", "split-lines", "split-regen",
        "split-go", "combine-dialog", "combine-list", "combine-name", "combine-regen", "combine-go", "continue-hint",
        "continue-combine", "continue-dismiss",
    ):
        assert f'id="{dom_id}"' in page, dom_id
    assert "/static/splitmerge.css?v=" in page
    assert "Regenerate notes for each part" in page and "Combine…" in page
    assert "'/v1/sessions/combine'" in page and "/unsplit'" in page and "/uncombine'" in page


def test_split_merge_css_is_served_and_uses_only_existing_tokens(client):
    from meeting_notes.server import web

    served = client.get("/static/splitmerge.css")
    assert served.status_code == 200 and served.headers["content-type"].startswith("text/css")
    css = served.text
    app_css = web.stylesheet_text()
    import re

    used = set(re.findall(r"var\((--[a-z0-9-]+)", css)) - {"--lanes"}
    defined = set(re.findall(r"(--[a-z0-9-]+)\s*:", app_css)) | {"--lane-w"}
    assert used <= defined, used - defined
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b", css)  # no hard-coded colours (DESIGN.md)


def test_split_merge_script_parses_in_node(tmp_path):
    import re
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    for i, script in enumerate(re.findall(r"<script>(.*?)</script>", _page(), re.S)):
        path = tmp_path / f"meetings-{i}.js"
        path.write_text(script, encoding="utf-8")
        result = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr[:400]


def test_icons_for_split_and_merge_exist_in_both_icon_tables():
    from meeting_notes.server import web

    js = (web._STATIC_DIR / "icons.js").read_text(encoding="utf-8")
    for name in ("split", "merge"):
        assert name in web._ICON_PATHS and f'"{name}":' in js
