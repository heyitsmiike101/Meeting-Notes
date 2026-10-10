"""Note templates ("note styles"): settings migration and validation, selection
flowing into the bridge, the recorded style, the REST/MCP agent API and the UI."""

from __future__ import annotations

import json
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from meeting_notes import review_contract, wire
from meeting_notes.bridge import BridgeConfig, BridgeWorker
from meeting_notes.server import settings as settings_mod
from meeting_notes.server import store as store_mod
from meeting_notes.server.app import create_app
from meeting_notes.server.jobs import JobQueue
from meeting_notes.server.web import render_settings_page, render_transcriptions_page
from meeting_notes.transcribe.protocol import Segment
from tests.agent_helpers import bearer, make_agent_app

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated:DeprecationWarning"
)

TOKEN = "tpl-secret"
H = {"Authorization": f"Bearer {TOKEN}"}


def _fields(**overrides):
    base = {
        "model": "base.en",
        "beam_size": "5",
        "audio_retention_days": "-1",
        "retention_check_interval_minutes": "60",
    }
    base.update(overrides)
    return base


def _notes():
    return {
        "title": "Planning", "summary": "Planned the release.", "meeting_notes": "",
        "participants": [], "key_points": [], "decisions": [], "action_items": [],
        "open_questions": [], "risks": [], "next_steps": [],
    }


def _app(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_NOTES_TOKEN", TOKEN)
    app = create_app(data_root=str(tmp_path / "data"))
    store = app.state.store
    store.write_session_meta("session-1", {"name": "Planning"})
    job_id = store.create_job("session-1")
    store.update_job(job_id, state=wire.JobState.DONE, progress=1.0)
    store.write_transcript(job_id, "# Planning", json.dumps({"segments": [{"text": "Ship Friday"}]}))
    return app


def _prompt(app, template_id):
    # The workflow endpoint serves the prompt stripped (review_contract.workflow_text).
    return settings_mod.load_settings(app.state.store.root).find_template(template_id)["prompt"].strip()


# -- settings: migration ----------------------------------------------------------------


def test_old_settings_json_with_only_ai_workflow_migrates_unchanged(tmp_path):
    (tmp_path / "settings.json").write_text(
        json.dumps({"model": "base.en", "ai_workflow": "# My tuned prompt\n\nKeep it tight."}),
        encoding="utf-8",
    )
    loaded = settings_mod.load_settings(tmp_path)
    assert loaded.ai_workflow == "# My tuned prompt\n\nKeep it tight."
    templates = loaded.all_templates()
    assert [t["id"] for t in templates] == ["standard", "quick", "webinar"]
    assert [t["name"] for t in templates] == ["Standard", "Quick notes", "Detailed webinar"]
    assert all(t["builtin"] for t in templates)
    # Standard IS ai_workflow, and it is the default, so behaviour is unchanged.
    assert templates[0]["prompt"] == loaded.ai_workflow
    assert loaded.default_template_id == "standard"
    assert loaded.default_template()["prompt"] == "# My tuned prompt\n\nKeep it tight."
    # Saving keeps ai_workflow readable for older tooling.
    settings_mod.save_settings(tmp_path, loaded)
    assert json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))["ai_workflow"] == loaded.ai_workflow


def test_fresh_defaults_standard_is_the_packaged_workflow():
    fresh = settings_mod.Settings()
    assert fresh.all_templates()[0]["prompt"] == fresh.ai_workflow
    assert fresh.ai_workflow == review_contract.workflow_text(None)
    assert fresh.to_dict()["default_template_id"] == "standard"
    assert [t["id"] for t in fresh.to_dict()["templates"]] == ["standard", "quick", "webinar"]


@pytest.mark.parametrize("tid", ["quick", "webinar"])
def test_builtin_prompts_keep_the_output_contract(tid):
    prompt = settings_mod.Settings().find_template(tid)["prompt"]
    for field in review_contract.output_schema()["required"]:
        assert f"`{field}`" in prompt, field
    assert "null" in prompt  # owner / due date rule
    assert "they talked about" in prompt  # writing-style rule carried over
    assert "### " in prompt and "- " in prompt


def test_webinar_prompt_covers_its_sections():
    text = settings_mod.Settings().find_template("webinar")["prompt"]
    for needle in ("Q&A", "Resources and links", "demos", "statistics"):
        assert needle in text


def test_corrupt_stored_templates_are_skipped_and_builtins_restored(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({
        "model": "base.en",
        "default_template_id": "gone",
        "note_templates": [
            "junk", {"id": "ok1", "name": "Customer call", "prompt": "Do it."},
            {"id": "bad id!", "name": "X", "prompt": "p"}, {"id": "noprompt", "name": "Y", "prompt": " "},
            {"id": "quick", "name": "Renamed", "prompt": "Edited quick prompt"},
            {"id": "standard", "name": "Standard", "prompt": "ignored"},
        ],
    }), encoding="utf-8")
    loaded = settings_mod.load_settings(tmp_path)
    assert [t["id"] for t in loaded.note_templates] == ["quick", "webinar", "ok1"]
    quick = loaded.find_template("quick")
    assert quick["name"] == "Renamed" and quick["prompt"] == "Edited quick prompt"
    assert loaded.default_template_id == "standard"  # unknown default falls back


# -- settings: validation -----------------------------------------------------------------


def test_validate_accepts_and_persists_user_templates(tmp_path):
    result = settings_mod.validate(_fields(
        default_template_id="mine",
        note_templates=[{"id": "mine", "name": "  Customer call ", "prompt": "  Focus on asks.  "}],
    ))
    assert [t["id"] for t in result.note_templates] == ["quick", "webinar", "mine"]
    mine = result.find_template("customer call")  # by name, case-insensitive
    assert mine["id"] == "mine" and mine["name"] == "Customer call" and mine["prompt"] == "Focus on asks."
    assert result.default_template()["id"] == "mine"
    settings_mod.save_settings(tmp_path, result)
    assert settings_mod.load_settings(tmp_path).find_template("mine")["prompt"] == "Focus on asks."


def test_validate_assigns_an_id_to_a_new_template():
    result = settings_mod.validate(_fields(note_templates=[{"name": "Fresh", "prompt": "p"}]))
    fresh = result.find_template("Fresh")
    assert fresh["id"] not in ("standard", "quick", "webinar") and fresh["id"]


def test_validate_accepts_form_style_json_string():
    payload = json.dumps([{"id": "t1", "name": "From form", "prompt": "p"}])
    assert settings_mod.validate(_fields(note_templates=payload)).find_template("t1")["name"] == "From form"


@pytest.mark.parametrize("templates,message", [
    ([{"id": "a", "name": "", "prompt": "p"}], "name is required"),
    ([{"id": "a", "name": "   ", "prompt": "p"}], "name is required"),
    ([{"id": "a", "name": "x" * 61, "prompt": "p"}], "60 characters"),
    ([{"id": "a", "name": "Same", "prompt": "p"}, {"id": "b", "name": "same", "prompt": "q"}], "already in use"),
    ([{"id": "a", "name": "quick NOTES", "prompt": "p"}], "already in use"),
    ([{"id": "a", "name": "Standard", "prompt": "p"}], "already in use"),
    ([{"id": "a", "name": "A", "prompt": ""}], "prompt is required"),
    ([{"id": "a", "name": "A", "prompt": "  "}], "prompt is required"),
    ([{"id": "a", "name": "A", "prompt": "x" * (settings_mod.MAX_AI_WORKFLOW_CHARS + 1)}], "characters or fewer"),
    ([{"id": "a", "name": "A", "prompt": "p"}, {"id": "a", "name": "B", "prompt": "p"}], "duplicate"),
    ([{"id": "Bad Id", "name": "A", "prompt": "p"}], "invalid note template id"),
    ([{"id": "quick", "name": "Quick notes", "prompt": ""}], "prompt is required"),
    (["nope"], "must be an object"),
    ("{not json", "valid JSON"),
    ({"a": 1}, "must be a list"),
])
def test_validate_rejects_bad_templates(templates, message):
    with pytest.raises(settings_mod.ValidationError, match=message):
        settings_mod.validate(_fields(note_templates=templates))


def test_validate_limits_the_number_of_user_templates():
    many = [{"id": f"t{i}", "name": f"Style {i}", "prompt": "p"} for i in range(settings_mod.MAX_USER_TEMPLATES + 1)]
    with pytest.raises(settings_mod.ValidationError, match="at most"):
        settings_mod.validate(_fields(note_templates=many))


def test_default_template_must_exist():
    with pytest.raises(settings_mod.ValidationError, match="default_template_id"):
        settings_mod.validate(_fields(default_template_id="nope"))


def test_builtins_cannot_be_deleted():
    result = settings_mod.validate(_fields(note_templates=[]))
    assert [t["id"] for t in result.note_templates] == ["quick", "webinar"]
    assert result.find_template("quick")["prompt"] == settings_mod.Settings().find_template("quick")["prompt"]
    renamed = settings_mod.validate(_fields(note_templates=[{"id": "quick", "name": "Mine", "prompt": "custom"}]))
    assert renamed.find_template("quick")["name"] == "Mine"
    assert renamed.find_template("quick")["prompt"] == "custom"


def test_standard_is_edited_through_ai_workflow_only():
    result = settings_mod.validate(_fields(
        ai_workflow="new standard",
        note_templates=[{"id": "standard", "name": "Standard", "prompt": "stale echo"}],
    ))
    assert result.ai_workflow == "new standard"
    assert result.find_template("standard")["prompt"] == "new standard"
    assert all(t["id"] != "standard" for t in result.note_templates)


def test_omitted_templates_mean_builtins_and_default_standard():
    result = settings_mod.validate(_fields())
    assert [t["id"] for t in result.note_templates] == ["quick", "webinar"]
    assert result.default_template_id == "standard"


# -- REST settings -------------------------------------------------------------------------


def test_put_settings_from_an_older_client_keeps_templates(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    client = TestClient(app)
    body = _fields(
        default_template_id="mine",
        note_templates=[{"id": "mine", "name": "Mine", "prompt": "my prompt"}],
    )
    assert client.put("/v1/settings", headers=H, json=body).status_code == 200
    older = _fields(ai_workflow="edited standard")  # no template keys at all
    saved = client.put("/v1/settings", headers=H, json=older).json()
    assert saved["default_template_id"] == "mine"
    assert any(t["id"] == "mine" for t in saved["note_templates"])
    assert saved["ai_workflow"] == "edited standard"
    assert [t["id"] for t in saved["templates"]][:1] == ["standard"]
    bad = client.put("/v1/settings", headers=H, json=_fields(note_templates=[{"name": "", "prompt": "p"}]))
    assert bad.status_code == 400 and "name is required" in bad.json()["detail"]


# -- selection -> review -> bridge payload -----------------------------------------------------


def test_note_templates_endpoint_lists_styles_without_prompts(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    client = TestClient(app)
    assert client.get("/v1/note-templates").status_code == 401
    data = client.get("/v1/note-templates", headers=H).json()
    assert data["default_template_id"] == "standard"
    assert [(t["id"], t["builtin"], t["default"]) for t in data["items"]] == [
        ("standard", True, True), ("quick", True, False), ("webinar", True, False)]
    assert all("prompt" not in t for t in data["items"])


def test_queue_without_template_records_the_default(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    client = TestClient(app)
    review = client.post("/v1/sessions/session-1/review", headers=H).json()
    assert (review["template_id"], review["template_name"]) == ("standard", "Standard")


def test_queue_with_template_by_id_or_name_and_unknown_is_400(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    client = TestClient(app)
    by_id = client.post("/v1/sessions/session-1/review?template=quick", headers=H).json()
    assert by_id["template_id"] == "quick" and by_id["template_name"] == "Quick notes"
    by_name = client.post("/v1/sessions/session-1/review?force=true&template=detailed%20webinar", headers=H).json()
    assert by_name["template_id"] == "webinar"
    bad = client.post("/v1/sessions/session-1/review?force=true&template=nope", headers=H)
    assert bad.status_code == 400 and "unknown note template" in bad.json()["detail"]


def test_default_template_setting_applies_when_none_is_chosen(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    client = TestClient(app)
    client.put("/v1/settings", headers=H, json=_fields(default_template_id="quick"))
    review = client.post("/v1/sessions/session-1/review", headers=H).json()
    assert review["template_id"] == "quick"
    assert client.get("/v1/note-templates", headers=H).json()["default_template_id"] == "quick"


def test_non_force_repeat_keeps_the_existing_reviews_template(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    client = TestClient(app)
    first = client.post("/v1/sessions/session-1/review?template=quick", headers=H).json()
    again = client.post("/v1/sessions/session-1/review?template=webinar", headers=H).json()
    assert again["review_id"] == first["review_id"] and again["template_id"] == "quick"


def test_claim_points_the_bridge_at_that_reviews_prompt(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    client = TestClient(app)
    review = client.post("/v1/sessions/session-1/review?template=quick", headers=H).json()
    claim = client.get("/v1/bridge/review/claim", headers=H).json()
    assert claim["workflow_url"] == f"/v1/bridge/review/{review['review_id']}/workflow.md"
    served = client.get(claim["workflow_url"], headers=H)
    assert served.status_code == 200
    assert served.text == _prompt(app, "quick")
    # The global URL is still Standard (= ai_workflow), unchanged for old tooling.
    assert client.get("/v1/bridge/workflow.md", headers=H).text == _prompt(app, "standard")
    assert client.get("/v1/bridge/workflow.md?template=quick", headers=H).text == _prompt(app, "quick")
    assert client.get("/v1/bridge/workflow.md?template=nope", headers=H).status_code == 404
    assert client.get("/v1/bridge/review/nope00/workflow.md", headers=H).status_code == 404
    assert client.get(claim["workflow_url"]).status_code == 401


def test_edits_to_a_template_apply_to_a_review_still_queued(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    client = TestClient(app)
    client.post("/v1/sessions/session-1/review?template=quick", headers=H)
    client.put("/v1/settings", headers=H, json=_fields(
        note_templates=[{"id": "quick", "name": "Quick notes", "prompt": "Shorter still."}]))
    claim = client.get("/v1/bridge/review/claim", headers=H).json()
    assert client.get(claim["workflow_url"], headers=H).text == "Shorter still."


def test_bridge_worker_receives_the_selected_template_prompt(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    client = TestClient(app)
    queued = client.post("/v1/sessions/session-1/review?template=webinar", headers=H).json()
    worker = BridgeWorker(BridgeConfig("http://testserver", token=TOKEN, worker_id="tpl"))
    worker.client = client
    seen = {}

    def fake_codex(command, *, cwd, env, check):
        seen["workflow"] = Path(cwd, "workflow.md").read_text(encoding="utf-8")
        Path(command[command.index("-o") + 1]).write_text(json.dumps(_notes()), encoding="utf-8")
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr("meeting_notes.bridge.subprocess.run", fake_codex)
    worker.run(once=True)
    assert seen["workflow"] == _prompt(app, "webinar")
    client = TestClient(app)  # the worker closed the one it borrowed
    detail = client.get(f"/v1/meeting-notes/{queued['review_id']}", headers=H).json()
    assert detail["status"] == "done"
    assert detail["template"] == {"id": "webinar", "name": "Detailed webinar"}
    assert detail["note"]["template"] == detail["template"]
    item = client.get("/v1/meeting-notes", headers=H).json()["items"][0]
    assert item["template"] == {"id": "webinar", "name": "Detailed webinar"}


def test_review_without_a_template_is_stamped_with_the_default_on_claim(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    client = TestClient(app)
    store = app.state.store
    legacy = store.create_review("session-1")  # e.g. the split/merge path
    assert legacy["template_id"] is None
    detail = client.get(f"/v1/meeting-notes/{legacy['review_id']}", headers=H).json()
    assert detail["template"] is None  # legacy records show no style
    claim = client.get("/v1/bridge/review/claim", headers=H).json()
    assert client.get(claim["workflow_url"], headers=H).text == _prompt(app, "standard")
    assert store.read_review(legacy["review_id"])["template_id"] == "standard"


def test_deleted_template_falls_back_to_default_prompt_but_keeps_its_name(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    client = TestClient(app)
    client.put("/v1/settings", headers=H, json=_fields(
        note_templates=[{"id": "mine", "name": "Customer call", "prompt": "Asks only."}]))
    review = client.post("/v1/sessions/session-1/review?template=mine", headers=H).json()
    claim = client.get("/v1/bridge/review/claim", headers=H).json()
    assert client.get(claim["workflow_url"], headers=H).text == "Asks only."
    client.post(f"/v1/bridge/review/{claim['id']}/complete", headers=H, json={"notes": _notes()})
    # Delete the template (and rename nothing else).
    client.put("/v1/settings", headers=H, json=_fields(note_templates=[]))
    detail = client.get(f"/v1/meeting-notes/{review['review_id']}", headers=H).json()
    assert detail["template"] == {"id": "mine", "name": "Customer call"}
    # Regenerating without choosing uses the default prompt rather than failing.
    assert client.post(f"/v1/meeting-notes/{review['review_id']}/retry", headers=H).status_code == 200
    claim2 = client.get("/v1/bridge/review/claim", headers=H).json()
    assert client.get(claim2["workflow_url"], headers=H).text == _prompt(app, "standard")


def test_renamed_template_shows_its_new_name(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    client = TestClient(app)
    client.put("/v1/settings", headers=H, json=_fields(
        note_templates=[{"id": "mine", "name": "Old name", "prompt": "p"}]))
    review = client.post("/v1/sessions/session-1/review?template=mine", headers=H).json()
    client.put("/v1/settings", headers=H, json=_fields(
        note_templates=[{"id": "mine", "name": "New name", "prompt": "p"}]))
    detail = client.get(f"/v1/meeting-notes/{review['review_id']}", headers=H).json()
    assert detail["template"] == {"id": "mine", "name": "New name"}


def test_retry_can_switch_template_and_no_body_keeps_it(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    client = TestClient(app)
    review = client.post("/v1/sessions/session-1/review?template=quick", headers=H).json()
    rid = review["review_id"]
    claim = client.get("/v1/bridge/review/claim", headers=H).json()
    client.post(f"/v1/bridge/review/{claim['id']}/complete", headers=H, json={"notes": _notes()})

    kept = client.post(f"/v1/meeting-notes/{rid}/retry", headers=H)
    assert kept.status_code == 200 and kept.json()["template_id"] == "quick"
    client.get("/v1/bridge/review/claim", headers=H)
    client.post(f"/v1/bridge/review/{rid}/complete", headers=H, json={"notes": _notes()})

    switched = client.post(f"/v1/meeting-notes/{rid}/retry", headers=H, json={"template": "Detailed webinar"})
    assert switched.status_code == 200
    assert (switched.json()["template_id"], switched.json()["template_name"]) == ("webinar", "Detailed webinar")
    client.get("/v1/bridge/review/claim", headers=H)
    client.post(f"/v1/bridge/review/{rid}/complete", headers=H, json={"notes": _notes()})

    assert client.post(f"/v1/meeting-notes/{rid}/retry", headers=H, json={"template": "nope"}).status_code == 400
    assert client.post(f"/v1/meeting-notes/{rid}/retry", headers=H, content="[1]").status_code == 400
    assert client.post(f"/v1/meeting-notes/{rid}/retry", headers=H, content="{bad").status_code == 400


# -- automatic notes ------------------------------------------------------------------------------


def test_automatic_notes_use_the_default_template(tmp_path):
    store = store_mod.Store(str(tmp_path / "data"))
    settings_mod.save_settings(store.root, settings_mod.validate(_fields(
        ai_provider="claude", default_template_id="webinar")))
    store.write_session_meta("s1", {"created": "2026-09-20", "tracks": {"mic": {}}})
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
    (review,) = store.list_reviews(session_id="s1")
    assert (review["template_id"], review["template_name"]) == ("webinar", "Detailed webinar")


# -- agent REST + MCP -------------------------------------------------------------------------------


@pytest.fixture
def agent(tmp_path, monkeypatch):
    app, _ = make_agent_app(tmp_path, monkeypatch, with_mcp=False)
    keys = app.state.agent_keys
    return {
        "client": TestClient(app), "store": app.state.store, "app": app,
        "read": bearer(keys.create("r")["key"]), "write": bearer(keys.create("w", ["read", "write"])["key"]),
    }


def test_agent_lists_note_templates(agent):
    c = agent["client"]
    assert c.get("/api/v1/note-templates").status_code == 401
    data = c.get("/api/v1/note-templates", headers=agent["read"]).json()
    assert data["default_template_id"] == "standard"
    assert [t["id"] for t in data["items"]] == ["standard", "quick", "webinar"]
    assert "meeting_notes_list_note_templates" in {t["name"] for t in __import__(
        "meeting_notes.server.agent.spec", fromlist=["TOOLS"]).TOOLS}


def test_agent_generate_with_template_and_unchanged_default(agent):
    c, store = agent["client"], agent["store"]
    plain = c.post("/api/v1/meetings/m-standup/notes/generate", headers=agent["write"]).json()
    assert plain["template"] == {"id": "standard", "name": "Standard"}
    by_name = c.post(
        "/api/v1/meetings/m-standup/notes/generate?force=true&template=quick%20notes", headers=agent["write"]).json()
    assert by_name["template"] == {"id": "quick", "name": "Quick notes"}
    assert store.read_review(by_name["review_id"])["template_id"] == "quick"
    by_id = c.post(
        "/api/v1/meetings/m-standup/notes/generate?force=true&template=webinar", headers=agent["write"]).json()
    assert by_id["template"]["id"] == "webinar"
    bad = c.post("/api/v1/meetings/m-standup/notes/generate?template=nope", headers=agent["write"])
    assert bad.status_code == 400 and bad.json()["code"] == "unknown_template"
    # Read keys still cannot generate.
    assert c.post("/api/v1/meetings/m-standup/notes/generate?template=quick", headers=agent["read"]).status_code == 403


def test_agent_notes_report_their_template(agent):
    c = agent["client"]
    done = next(m for m in c.get("/api/v1/meetings", headers=agent["read"]).json()["items"] if m["has_notes"])
    notes = c.get(f"/api/v1/meetings/{done['id']}/notes", headers=agent["read"]).json()
    assert "template" in notes  # None for notes made before templates existed
    meeting = c.get(f"/api/v1/meetings/{done['id']}", headers=agent["read"]).json()
    assert "notes_template" in meeting


def test_mcp_generate_accepts_template_and_lists_templates(tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    from tests.test_agent_mcp import Rpc, tool_error, tool_json

    app, _ = make_agent_app(tmp_path, monkeypatch)
    key = app.state.agent_keys.create("mcp-writer", ["read", "write"])["key"]
    with TestClient(app) as client:
        rpc = Rpc(client, key)
        listed = tool_json(rpc.tool("meeting_notes_list_note_templates"))
        assert [t["id"] for t in listed["items"]] == ["standard", "quick", "webinar"]
        queued = tool_json(rpc.tool(
            "meeting_notes_generate_notes", {"meeting_id": "m-standup", "template": "Quick notes"}))
        assert queued["template"] == {"id": "quick", "name": "Quick notes"}
        assert app.state.store.read_review(queued["review_id"])["template_id"] == "quick"
        err = tool_error(rpc.tool(
            "meeting_notes_generate_notes", {"meeting_id": "m-standup", "template": "nope", "force": True}))
        assert err["code"] == "unknown_template"


# -- web UI ---------------------------------------------------------------------------------------------


def test_settings_page_renders_the_templates_editor():
    page = render_settings_page(settings_mod.Settings(), token_configured=True)
    assert 'name="default_template_id"' in page and "Default note type" in page
    assert 'name="note_templates"' in page
    assert 'textarea name="ai_workflow"' in page  # Standard's prompt is still ai_workflow
    for name in ("Standard", "Quick notes", "Detailed webinar"):
        assert f'<span class="style-title">{name}</span>' in page
    assert page.count('class="style-badge">Built-in') == 3
    assert 'id="style-add"' in page and '<template id="style-tpl">' in page
    # Only user styles get a delete button (the one in the blank <template>).
    assert page.count("style-delete") >= 1
    with_user = settings_mod.validate(_fields(
        note_templates=[{"id": "mine", "name": 'Call <b>"x"</b>', "prompt": "p"}]))
    page2 = render_settings_page(with_user, token_configured=True)
    assert "Call &lt;b&gt;" in page2 and "<b>" not in page2.split('id="style-list"')[1].split("</details>")[3]
    assert 'id="settings-speakers-heading"' not in page
    assert "#settings-speakers-heading" not in page
    # Each note type card shows both roles: the summary prompt and the Notion destination.
    assert 'id="settings-notetypes-heading">Note types</h2>' in page and "<span>Add note type</span>" in page
    assert "A note type sets how the summary is written and where it&#x27;s saved in Notion" in page or         "A note type sets how the summary is written and where it's saved in Notion" in page
    assert '<span class="name">Summary instructions</span>' in page
    assert "What the AI writes for this type of meeting." in page
    assert '<span class="name">Save to Notion</span>' in page
    assert "Notes of this type go into month pages under the Notion page you choose." in page
    assert '<span class="style-sum">· Not saved to Notion</span>' in page
    assert "Note style" not in page and "Add style" not in page and "Delete style" not in page


def test_settings_card_summary_shows_the_notion_page_when_set():
    page_id = "a" * 32
    st = settings_mod.Settings(notion_parents={"standard": page_id})
    page = render_settings_page(st, token_configured=True)
    assert '<span class="style-sum">· Notion page set</span>' in page
    assert f'href="https://www.notion.so/{page_id}"' in page


def test_settings_form_saves_templates_and_default(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = create_app(data_root=str(tmp_path / "data"))
    client = TestClient(app)
    form = _fields(
        ai_provider="claude",
        ai_workflow="edited standard prompt",
        default_template_id="mine",
        note_templates=json.dumps([
            {"id": "quick", "name": "Quick notes", "prompt": "quick v2"},
            {"id": "webinar", "name": "Detailed webinar", "prompt": "web v2"},
            {"id": "mine", "name": "Customer call", "prompt": "asks only"},
        ]),
    )
    assert "Settings saved" in client.post("/settings", data=form).text
    saved = settings_mod.load_settings(app.state.store.root)
    assert saved.ai_workflow == "edited standard prompt"
    assert saved.default_template_id == "mine"
    assert saved.find_template("quick")["prompt"] == "quick v2"
    assert saved.find_template("mine")["name"] == "Customer call"
    page = client.get("/settings").text
    assert "Customer call" in page and "edited standard prompt" in page
    bad = client.post("/settings", data={**form, "note_templates": json.dumps([{"name": "", "prompt": "p"}])})
    assert "name is required" in bad.text
    assert settings_mod.load_settings(app.state.store.root).find_template("mine") is not None


def test_settings_form_without_template_fields_keeps_them(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = create_app(data_root=str(tmp_path / "data"))
    settings_mod.save_settings(app.state.store.root, settings_mod.validate(_fields(
        default_template_id="mine", note_templates=[{"id": "mine", "name": "Mine", "prompt": "p"}])))
    client = TestClient(app)
    assert "Settings saved" in client.post("/settings", data=_fields(ai_provider="claude")).text
    kept = settings_mod.load_settings(app.state.store.root)
    assert kept.default_template_id == "mine" and kept.find_template("mine")["prompt"] == "p"


def test_settings_form_without_speaker_fields_keeps_stored_values_and_diarization_off(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = create_app(data_root=str(tmp_path / "data"))
    settings_mod.save_settings(app.state.store.root, settings_mod.Settings(
        model="base.en", diarization_enabled=True, diarization_model="custom/model",
        diarization_min_speakers=2, diarization_max_speakers=5))
    client = TestClient(app)
    page = client.get("/settings").text
    for name in ("diarization_enabled", "diarization_model", "diarization_min_speakers", "diarization_max_speakers"):
        assert f'name="{name}"' not in page
    assert "Remote speaker labels" not in page
    # The rendered form no longer carries any of those fields.
    assert "Settings saved" in client.post("/settings", data=_fields(ai_provider="claude")).text
    after = settings_mod.load_settings(app.state.store.root)
    assert after.diarization_enabled is False
    assert (after.diarization_model, after.diarization_min_speakers, after.diarization_max_speakers) == (
        "custom/model", 2, 5)
    # A fresh install with no stored values ends with the defaults, also off.
    app2 = create_app(data_root=str(tmp_path / "data2"))
    assert "Settings saved" in TestClient(app2).post("/settings", data=_fields()).text
    fresh = settings_mod.load_settings(app2.state.store.root)
    assert fresh.diarization_enabled is False
    assert (fresh.diarization_min_speakers, fresh.diarization_max_speakers) == (1, 8)


def test_meeting_view_has_a_style_picker_wired_to_generate_and_regenerate():
    page = render_transcriptions_page(token_configured=True, ai_enabled=True)
    assert 'id="notes-template"' in page and 'id="notes-regen"' in page
    assert "/v1/note-templates" in page
    assert "action('/review'+templateQuery())" in page
    assert "JSON.stringify({template:sel.value})" in page
    assert 'id="overlay-style"' in page and "tpl&&tpl.name" in page  # the note type shows in the header
    assert "'Note type: '+tpl.name" in page
    assert 'for="notes-template">Note type</label>' in page and "Note style" not in page
    # The Notion destination (parent page > month page) sits with the picker and follows the selection.
    assert 'id="notion-box"' in page and page.index('id="type-bar"') < page.index('id="notion-box"') < page.index('class="doc-wrap"')
    assert "notionPathHtml" in page and "'Parent page'" in page and "Not saved to Notion" in page
    assert "'/notion'+tq" in page and "?template=" in page  # status is requested for the selected note type
    assert "templateSelect().addEventListener('change',function(){updateRegenButton();updateNoteDest();})" in page
    assert "In Notion" in page and "Sending…" in page and "Send to Notion" in page
    assert "moves the Notion copy" in page
    # The meetings-list Generate button sends no template, so the server default applies.
    assert "'/review', {method:'POST', credentials:'same-origin'}" in page


def test_meeting_menu_always_offers_combine_with_a_picker():
    page = render_transcriptions_page(token_configured=True, ai_enabled=True)
    assert 'id="combine-meeting"' in page and "Combine with another meeting…" in page
    assert 'id="pick-dialog"' in page and 'id="pick-q"' in page and 'id="pick-list"' in page
    # Nearby meetings first, the continuation suggestion highlighted, blockers explained.
    assert "Close in time" in page and "Suggested" in page and "newest first" in page
    assert "This meeting has not finished transcribing" in page and "Audio was deleted for this meeting" in page
    assert "openCombine(ids)" in page and "[currentSession].concat(pk.chosen)" in page
