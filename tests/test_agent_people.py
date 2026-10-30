"""Agent people tools: list/get/quotes/who-said over REST and MCP, the 409 while voice profiles are off, and
display names in transcripts and search once they are on."""

from __future__ import annotations

import dataclasses

import pytest
from fastapi.testclient import TestClient

from meeting_notes.server import people
from meeting_notes.server import settings as settings_mod
from meeting_notes.server.agent import spec
from tests.agent_helpers import bearer, make_agent_app
from tests.test_people import basis, speakers_for

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated:DeprecationWarning"
)

PEOPLE_TOOLS = (
    "meeting_notes_list_people",
    "meeting_notes_get_person",
    "meeting_notes_person_quotes",
    "meeting_notes_who_said",
)


def set_enabled(store, on=True):
    current = settings_mod.load_settings(store.root)
    settings_mod.save_settings(
        store.root, dataclasses.replace(current, diarization_enabled=on, voice_profiles_enabled=on))


def record(store, sid, job, system, mic=None):
    tracks = {"system": speakers_for("system", system)}
    if mic:
        tracks["mic"] = speakers_for("mic", mic)
        tracks["mic"].self_raw, tracks["mic"].self_label = next(iter(mic)), "You"
    people.record_meeting(store, sid, job, tracks)


@pytest.fixture
def env(tmp_path, monkeypatch):
    app, _ = make_agent_app(tmp_path, monkeypatch, with_mcp=False)
    store = app.state.store
    key = app.state.agent_keys.create("reader")["key"]
    return {"app": app, "store": store, "client": TestClient(app), "h": bearer(key)}


@pytest.fixture
def on(env):
    """Voice profiles on, with the seeded meetings recorded: one far-end voice in m-plan and m-budget (Sarah),
    another in m-standup, and the user's own mic voice in m-plan."""
    store = env["store"]
    set_enabled(store)
    me = {"M": ("You", basis(5), 30)}
    record(store, "m-plan", "j1", {"A": ("Them", basis(0), 30)}, me)
    record(store, "m-budget", "j2", {"A": ("Them", basis(0), 30)})
    record(store, "m-standup", "j3", {"B": ("Them", basis(1), 20)})
    listed = people.list_people(store)
    sarah = next(p for p in listed if p["meetings"] == 2 and not p["is_self"])
    other = next(p for p in listed if p["meetings"] == 1 and not p["is_self"])
    people.update_person(store.root, sarah["id"], {"name": "Sarah Lee", "company": "Acme"})
    env.update(sarah=sarah["id"], other=other["id"])
    return env


def get(env, path, **params):
    return env["client"].get(path, headers=env["h"], params=params)


# -- off -----------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/api/v1/people", "/api/v1/people/p1", "/api/v1/people/p1/quotes",
                                  "/api/v1/who-said?q=beta"])
def test_every_people_route_is_409_while_off(env, path):
    r = env["client"].get(path, headers=env["h"])
    assert r.status_code == 409
    assert r.json()["code"] == "voice_profiles_off"


def test_voice_profiles_without_diarization_are_still_off(env):
    current = settings_mod.load_settings(env["store"].root)
    settings_mod.save_settings(env["store"].root, dataclasses.replace(
        current, voice_profiles_enabled=True, diarization_enabled=False))
    assert get(env, "/api/v1/people").status_code == 409


def test_transcript_and_search_are_unchanged_while_off(env):
    r = get(env, "/api/v1/meetings/m-plan/transcript")
    seg = r.json()["segments"][1]
    assert set(seg) == {"start", "end", "speaker", "text"} and seg["speaker"] == "Them"
    hit = get(env, "/api/v1/search", q="migration").json()["items"][0]["matches"][0]
    assert "person_id" not in hit


# -- list ----------------------------------------------------------------------------


def test_list_people_rows_and_sorts(on):
    body = get(on, "/api/v1/people").json()
    assert body["sort"] == "talking" and body["count"] == 3
    # the user's own voice (30s) and Sarah (60s) and the standup voice (20s), most talking first
    assert [p["id"] for p in body["items"]][0] == on["sarah"]
    assert [p["talk_seconds"] for p in body["items"]] == [60.0, 30.0, 20.0]
    sarah = body["items"][0]
    assert sarah["name"] == "Sarah Lee" and sarah["company"] == "Acme" and sarah["meetings"] == 2
    assert sarah["email"] is None and sarah["last_heard"].startswith("2026-09-10")
    flat = str(body)
    assert "embedding" not in flat and "clips" not in flat

    recent = get(on, "/api/v1/people", sort="recent").json()["items"]
    assert recent[0]["id"] == on["other"]  # heard in the newest meeting (m-standup)
    alpha = get(on, "/api/v1/people", sort="alpha").json()["items"]
    assert alpha[0]["name"] == "Sarah Lee"  # named first, then the numbered ones

    assert [p["name"] for p in get(on, "/api/v1/people", q="acme").json()["items"]] == ["Sarah Lee"]
    assert get(on, "/api/v1/people", sort="nope").json()["code"] == "invalid_sort"


# -- get -----------------------------------------------------------------------------


def test_get_person_by_id_and_by_name(on):
    by_id = get(on, f"/api/v1/people/{on['sarah']}")
    assert by_id.status_code == 200
    body = by_id.json()
    assert body["name"] == "Sarah Lee"
    assert [(a["meeting_id"], a["meeting_name"], a["label"], a["talk_seconds"]) for a in body["appearances"]] == [
        ("m-budget", "Budget review", "Them", 30.0), ("m-plan", "Sprint planning", "Them", 30.0)]
    assert body["appearances"][0]["date"].startswith("2026-09-10") and body["appearances"][0]["link"]
    flat = str(body)
    assert "embedding" not in flat and "clips" not in flat and "samples" not in flat and "raw" not in flat
    assert get(on, "/api/v1/people/sarah lee").json() == body  # case-insensitive display name


def test_get_person_unknown_and_ambiguous(on):
    assert get(on, "/api/v1/people/nobody").status_code == 404
    people.update_person(on["store"].root, on["other"], {"name": "sarah lee"})
    r = get(on, "/api/v1/people/Sarah Lee")
    assert r.status_code == 400 and r.json()["code"] == "ambiguous_person"
    assert on["sarah"] in r.json()["detail"] and on["other"] in r.json()["detail"]
    assert get(on, f"/api/v1/people/{on['other']}").status_code == 200  # an id always works


# -- quotes --------------------------------------------------------------------------


def test_person_quotes_filters(on):
    body = get(on, f"/api/v1/people/{on['sarah']}/quotes").json()
    assert body["person"] == {"id": on["sarah"], "name": "Sarah Lee"}
    texts = [(q["meeting_id"], q["start"], q["text"]) for q in body["items"]]
    assert texts == [
        ("m-budget", 6.0, "Cloud spend is up."),
        ("m-plan", 40.0, "Budget is the main risk for the beta."),
        ("m-plan", 8.5, "I can take the migration work by Friday."),
    ]  # newest meeting first; the in_gap segment is not a quote
    assert body["items"][0]["meeting_name"] == "Budget review"

    only = get(on, "/api/v1/people/Sarah Lee/quotes", meeting_id="m-plan").json()["items"]
    assert {q["meeting_id"] for q in only} == {"m-plan"}
    assert [q["text"] for q in get(on, f"/api/v1/people/{on['sarah']}/quotes", q="MIGRATION").json()["items"]] == [
        "I can take the migration work by Friday."]
    assert get(on, f"/api/v1/people/{on['sarah']}/quotes", limit=1).json()["count"] == 1
    assert get(on, f"/api/v1/people/{on['sarah']}/quotes", meeting_id="nope").status_code == 404


# -- who said ------------------------------------------------------------------------


def test_who_said(on):
    body = get(on, "/api/v1/who-said", q="BETA").json()
    rows = [(i["meeting_id"], i["speaker"], i["person_id"]) for i in body["items"]]
    assert rows == [("m-plan", "You", rows[0][2]), ("m-plan", "Sarah Lee", on["sarah"]), ]
    assert rows[0][2] and rows[0][2] != on["sarah"]  # the user's own voice is a person too
    assert body["items"][1]["start"] == 40.0 and body["items"][1]["meeting_name"] == "Sprint planning"
    assert all("hallucinated" not in i["text"] for i in get(on, "/api/v1/who-said", q="silence").json()["items"])
    assert get(on, "/api/v1/who-said", q="beta", meeting_id="m-budget").json()["count"] == 0
    assert get(on, "/api/v1/who-said", q="beta", limit=1).json()["count"] == 1
    assert get(on, "/api/v1/who-said").json()["code"] == "missing_query"


def test_who_said_person_id_is_null_for_an_unlinked_speaker(on):
    # a far-end voice with too little speech gets no person: the label stays as it was
    record(on["store"], "m-plan", "j9", {"A": ("Them", basis(0), 5)})
    item = next(i for i in get(on, "/api/v1/who-said", q="migration").json()["items"])
    assert item["speaker"] == "Them" and item["person_id"] is None


# -- names in transcripts and search -----------------------------------------------------


def test_transcript_uses_display_names_and_accepts_them_as_filter(on):
    body = get(on, "/api/v1/meetings/m-plan/transcript").json()
    assert "Sarah Lee" in body["speakers"] and "Them" not in body["speakers"]
    assert all("person_id" in s for s in body["segments"])
    for speaker in ("Sarah Lee", "sarah lee", "Them", on["sarah"]):
        got = get(on, "/api/v1/meetings/m-plan/transcript", speaker=speaker).json()
        assert [s["start"] for s in got["segments"]] == [8.5, 40.0], speaker
    text = get(on, "/api/v1/meetings/m-plan/transcript", format="text").text
    assert "Sarah Lee: I can take the migration" in text
    assert "Sarah Lee" in get(on, "/api/v1/meetings/m-plan").json()["transcript"]["speakers"]
    hit = next(m for m in get(on, "/api/v1/search", q="migration").json()["items"][0]["matches"]
               if m["source"] == "transcript")
    assert hit["speaker"] == "Sarah Lee" and hit["person_id"] == on["sarah"]


# -- MCP -----------------------------------------------------------------------------------


def test_mcp_tools_match_spec_and_return_the_rest_data(tmp_path, monkeypatch):
    pytest.importorskip("mcp", reason="the optional 'mcp' package is not installed")
    from tests.test_agent_mcp import Rpc, tool_error, tool_json

    app, _ = make_agent_app(tmp_path, monkeypatch)
    store = app.state.store
    read = app.state.agent_keys.create("mcp-reader")["key"]
    with TestClient(app) as client:
        rpc = Rpc(client, read)
        tools = {t["name"]: t for t in rpc.call("tools/list")["result"]["tools"]}
        assert sorted(tools) == sorted(t["name"] for t in spec.TOOLS)
        for name in PEOPLE_TOOLS:
            assert name in tools and "voice profiles are on" in tools[name]["description"]
        assert "person name" in tools["meeting_notes_get_transcript"]["description"]
        assert set(tools["meeting_notes_person_quotes"]["inputSchema"]["properties"]) == {
            "person", "meeting_id", "q", "limit"}
        assert {r["tool"] for r in spec.ROUTES if r.get("tool") in PEOPLE_TOOLS} == set(PEOPLE_TOOLS)

        assert tool_error(rpc.tool("meeting_notes_list_people"))["code"] == "voice_profiles_off"
        assert tool_error(rpc.tool("meeting_notes_who_said", {"q": "beta"}))["code"] == "voice_profiles_off"

        set_enabled(store)
        record(store, "m-plan", "j1", {"A": ("Them", basis(0), 30)})
        listed = people.list_people(store)
        sarah = listed[0]["id"]
        people.update_person(store.root, sarah, {"name": "Sarah Lee"})
        headers = {"Authorization": f"Bearer {read}"}

        mcp_list = tool_json(rpc.tool("meeting_notes_list_people"))
        assert mcp_list == client.get("/api/v1/people", headers=headers).json()
        assert tool_json(rpc.tool("meeting_notes_get_person", {"person": "sarah lee"}))["id"] == sarah
        quotes = tool_json(rpc.tool("meeting_notes_person_quotes", {"person": sarah, "q": "budget"}))
        assert [q["text"] for q in quotes["items"]] == ["Budget is the main risk for the beta."]
        said = tool_json(rpc.tool("meeting_notes_who_said", {"q": "migration"}))
        assert said["items"][0]["speaker"] == "Sarah Lee" and said["items"][0]["person_id"] == sarah
        assert tool_error(rpc.tool("meeting_notes_get_person", {"person": "nobody"}))["code"] == "unknown_person"
