"""Every note type's name is editable, built-ins included (ids never change)."""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent))

from notion_helpers import Env, add_meeting  # noqa: E402

from meeting_notes.server import settings as settings_mod  # noqa: E402
from meeting_notes.server.web import render_settings_page  # noqa: E402
from tests.agent_helpers import bearer, make_agent_app  # noqa: E402
from tests.test_note_templates import H, _app, _fields, _notes  # noqa: E402

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated:DeprecationWarning"
)


def _quick():
    return {"id": "quick", "name": "Quick notes", "prompt": "q"}


def _webinar():
    return {"id": "webinar", "name": "Detailed webinar", "prompt": "w"}


# -- validation + persistence -----------------------------------------------------------------


def test_rename_each_builtin_including_standard(tmp_path):
    result = settings_mod.validate(_fields(
        standard_name="  Daily  ",
        note_templates=[{**_quick(), "name": "Brief"}, {**_webinar(), "name": "Deep dive"}],
    ))
    names = {t["id"]: t["name"] for t in result.all_templates()}
    assert names == {"standard": "Daily", "quick": "Brief", "webinar": "Deep dive"}
    assert all(t["builtin"] for t in result.all_templates())
    assert result.find_template("daily")["id"] == "standard"  # lookup by new name
    assert result.find_template("Standard") is None
    settings_mod.save_settings(tmp_path, result)
    loaded = settings_mod.load_settings(tmp_path)
    assert {t["id"]: t["name"] for t in loaded.all_templates()} == names
    assert loaded.to_dict()["standard_name"] == "Daily"


def test_old_settings_file_loads_with_default_names(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({"model": "base.en", "ai_workflow": "x"}), encoding="utf-8")
    loaded = settings_mod.load_settings(tmp_path)
    assert [t["name"] for t in loaded.all_templates()] == ["Standard", "Quick notes", "Detailed webinar"]
    assert loaded.standard_name == "Standard"


def test_builtin_missing_a_name_keeps_default():
    result = settings_mod.validate(_fields(note_templates=[{"id": "quick", "prompt": "q"}]))
    assert result.find_template("quick")["name"] == "Quick notes"


@pytest.mark.parametrize("fields,message", [
    ({"standard_name": "  "}, "name is required"),
    ({"standard_name": "x" * 61}, "60 characters"),
    ({"standard_name": "quick notes"}, "already in use"),
    ({"note_templates": [{**_quick(), "name": "standard"}]}, "already in use"),
    ({"note_templates": [{**_quick(), "name": "Same"}, {**_webinar(), "name": "same"}]}, "already in use"),
    ({"note_templates": [{**_quick(), "name": "Brief"}, {"id": "u", "name": "brief", "prompt": "p"}]}, "already in use"),
    ({"note_templates": [{**_quick(), "name": "x" * 61}]}, "60 characters"),
    ({"note_templates": [{"id": "u", "name": "Detailed Webinar", "prompt": "p"}]}, "already in use"),
])
def test_validation_rules_apply_to_builtin_names(fields, message):
    with pytest.raises(settings_mod.ValidationError, match=message):
        settings_mod.validate(_fields(**fields))


def test_freed_default_name_can_be_taken_by_a_user_type():
    result = settings_mod.validate(_fields(
        note_templates=[{**_quick(), "name": "Brief"}, {"id": "u", "name": "Quick notes", "prompt": "p"}]))
    assert result.find_template("u")["name"] == "Quick notes"
    assert result.find_template("quick")["name"] == "Brief"


def test_bad_or_clashing_stored_builtin_name_falls_back_to_default(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({
        "standard_name": "Mine",
        "note_templates": [{**_quick(), "name": "mine"}, {**_webinar(), "name": ""}],
    }), encoding="utf-8")
    loaded = settings_mod.load_settings(tmp_path)
    assert loaded.standard_name == "Mine"
    assert loaded.find_template("quick")["name"] == "Quick notes"
    assert loaded.find_template("webinar")["name"] == "Detailed webinar"


# -- REST / display ---------------------------------------------------------------------------


def test_rest_put_renames_and_omitted_name_keeps_it(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    client = TestClient(app)
    r = client.put("/v1/settings", headers=H, json=_fields(
        standard_name="Daily", note_templates=[{**_quick(), "name": "Brief"}, _webinar()]))
    assert r.status_code == 200 and r.json()["standard_name"] == "Daily"
    # An older caller that sends neither field changes nothing.
    again = client.put("/v1/settings", headers=H, json=_fields()).json()
    assert [t["name"] for t in again["templates"]] == ["Daily", "Brief", "Detailed webinar"]
    bad = client.put("/v1/settings", headers=H, json=_fields(standard_name="Brief"))
    assert bad.status_code == 400
    listing = client.get("/v1/note-templates", headers=H).json()
    assert [(t["id"], t["name"]) for t in listing["items"]] == [
        ("standard", "Daily"), ("quick", "Brief"), ("webinar", "Detailed webinar")]


def test_review_with_stale_stored_name_shows_current_name(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    client = TestClient(app)
    review = client.post("/v1/sessions/session-1/review?template=quick", headers=H).json()
    assert review["template_name"] == "Quick notes"
    claim = client.get("/v1/bridge/review/claim", headers=H).json()
    client.post(f"/v1/bridge/review/{claim['id']}/complete", headers=H, json={"notes": _notes()})
    client.put("/v1/settings", headers=H, json=_fields(note_templates=[{**_quick(), "name": "Brief"}, _webinar()]))
    detail = client.get(f"/v1/meeting-notes/{review['review_id']}", headers=H).json()
    assert detail["template"] == {"id": "quick", "name": "Brief"}  # the stored name is stale
    items = client.get("/v1/meeting-notes", headers=H).json()["items"]
    assert [i["template"] for i in items] == [{"id": "quick", "name": "Brief"}]


def test_review_with_stale_standard_name(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    client = TestClient(app)
    review = client.post("/v1/sessions/session-1/review", headers=H).json()
    assert review["template_name"] == "Standard"
    claim = client.get("/v1/bridge/review/claim", headers=H).json()
    client.post(f"/v1/bridge/review/{claim['id']}/complete", headers=H, json={"notes": _notes()})
    client.put("/v1/settings", headers=H, json=_fields(standard_name="Daily"))
    detail = client.get(f"/v1/meeting-notes/{review['review_id']}", headers=H).json()
    assert detail["template"] == {"id": "standard", "name": "Daily"}


def test_review_template_falls_back_to_stored_name_when_type_deleted():
    s = settings_mod.validate(_fields())
    assert s.review_template({"template_id": "gone", "template_name": "Old one"}) == {"id": "gone", "name": "Old one"}
    assert s.review_template({"template_id": "quick", "template_name": "Stale"})["name"] == "Quick notes"


def test_agent_listing_uses_current_names(tmp_path, monkeypatch):
    app, _ = make_agent_app(tmp_path, monkeypatch, with_mcp=False)
    key = bearer(app.state.agent_keys.create("r")["key"])
    c = TestClient(app)
    root = app.state.store.root
    settings_mod.save_settings(root, dataclasses.replace(settings_mod.load_settings(root), standard_name="Daily"))
    data = c.get("/api/v1/note-templates", headers=key).json()
    assert [(t["id"], t["name"]) for t in data["items"]][0] == ("standard", "Daily")


def test_settings_page_has_editable_builtin_names():
    s = settings_mod.validate(_fields(standard_name="Daily", note_templates=[{**_quick(), "name": "Brief"}, _webinar()]))
    page = render_settings_page(s, token_configured=True)
    assert "keep their name" not in page
    assert 'name="standard_name"' in page and 'value="Daily"' in page
    assert 'value="Brief"' in page
    assert "Reset name" in page
    start = page.index('data-id="quick"')
    card = page[start:page.index("</details>", start)]
    assert "readonly" not in card.split('class="style-prompt-input"')[0]
    assert 'data-default="Quick notes"' in card


def test_web_form_post_without_standard_name_keeps_it(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    client = TestClient(app)
    client.put("/v1/settings", headers=H, json=_fields(standard_name="Daily"))
    form = {"model": "base.en", "beam_size": "5", "audio_retention_days": "-1",
            "retention_check_interval_minutes": "60"}
    r = client.post("/settings", headers=H, data=form)
    assert r.status_code == 200
    assert settings_mod.load_settings(app.state.store.root).standard_name == "Daily"


# -- Notion month pages -----------------------------------------------------------------------


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    return Env(tmp_path)


def _rename(env, standard=None):
    """Save a renamed Standard; returns {id: (old, new)} like the settings save handler computes."""
    old = settings_mod.load_settings(env.store.root)
    new = dataclasses.replace(old, standard_name=standard or old.standard_name)
    settings_mod.save_settings(env.store.root, new)
    olds = {t["id"]: t["name"] for t in old.all_templates()}
    return {t["id"]: (olds[t["id"]], t["name"]) for t in new.all_templates() if olds[t["id"]] != t["name"]}


def _queue(env, changes):
    return [env.sync.queue_month_retitle(tid, old, new) for tid, (old, new) in changes.items()]


def test_rename_queues_background_job_that_retitles_month_pages(env):
    for sid, started in (("a", "2026-09-30 10:00"), ("b", "2026-10-02 10:00")):
        add_meeting(env.store, sid, sid.upper(), started)
    env.run()
    sep, octo = env.month_page("September-2026 Standard"), env.month_page("October-2026 Standard")
    assert _queue(env, _rename(env, "Daily")) == [2]
    assert env.sync.pending_count() == 2
    assert env.fake.nodes[sep]["title"] == "September-2026 Standard"  # nothing until the worker runs
    env.run()
    assert env.fake.nodes[sep]["title"] == "September-2026 Daily"
    assert env.fake.nodes[octo]["title"] == "October-2026 Daily"
    state = env.sync.state.read()
    assert {m["title"] for m in state["months"].values()} == {"September-2026 Daily", "October-2026 Daily"}
    assert {r["month_title"] for r in state["meetings"].values()} == {"September-2026 Daily", "October-2026 Daily"}
    assert env.sync.pending_count() == 0


def test_no_duplicate_month_page_after_rename(env):
    add_meeting(env.store, "a", "A", "2026-09-30 10:00")
    env.run()
    page = env.month_page("September-2026 Standard")
    _queue(env, _rename(env, "Daily"))
    env.run()
    add_meeting(env.store, "b", "B", "2026-09-30 11:00")
    env.run()
    assert env.fake.child_titles(env.root_page) == ["September-2026 Daily"]
    assert len(env.fake.toggles(page)) == 2


def test_new_export_before_job_runs_retitles_inline_without_duplicate(env):
    add_meeting(env.store, "a", "A", "2026-09-30 10:00")
    env.run()
    page = env.month_page("September-2026 Standard")
    _rename(env, "Daily")  # job never queued (e.g. Notion was disconnected at the time)
    add_meeting(env.store, "b", "B", "2026-09-30 11:00")
    env.run()
    assert env.fake.child_titles(env.root_page) == ["September-2026 Daily"]
    assert env.fake.nodes[page]["title"] == "September-2026 Daily"


def test_title_fallback_tries_new_then_old_name_when_state_is_lost(env):
    add_meeting(env.store, "a", "A", "2026-09-30 10:00")
    env.run()
    page = env.month_page("September-2026 Standard")
    _rename(env, "Daily")
    env.sync.state.update(lambda d: d["months"].clear())  # month page ids lost; old names remembered
    env.sync.state.update(lambda d: d["style_names"].update({"standard": ["Standard"]}))
    env.fake.requests.clear()
    add_meeting(env.store, "b", "B", "2026-09-30 11:00")
    env.run()
    assert env.fake.calls("POST", "/v1/pages") == []
    assert env.fake.child_titles(env.root_page) == ["September-2026 Daily"]
    assert env.fake.nodes[page]["title"] == "September-2026 Daily"


def test_title_fallback_prefers_page_with_new_title(env):
    old_page = env.fake.add_page("September-2026 Standard", env.root_page)
    new_page = env.fake.add_page("September-2026 Daily", env.root_page)
    _queue(env, _rename(env, "Daily"))
    add_meeting(env.store, "b", "B", "2026-09-30 11:00")
    env.run()
    assert env.fake.toggles(new_page) and not env.fake.toggles(old_page)


def test_retitle_job_retries_and_survives_restart(env):
    add_meeting(env.store, "a", "A", "2026-09-30 10:00")
    env.run()
    page = env.month_page("September-2026 Standard")
    env.fake.fail(503, times=999, when=lambda m, p: m == "PATCH" and "/v1/pages/" in p)
    _queue(env, _rename(env, "Daily"))
    env.run()
    assert env.fake.nodes[page]["title"] == "September-2026 Standard"  # failed once, queued for retry
    assert env.sync.pending_count() == 1
    env.fake.rules.clear()
    fresh = env.new_sync()  # "restart": jobs reload from disk
    fresh.tokens.set(env.fake.token)
    assert len(fresh.resume_interrupted()) == 1
    env.clock["t"] += 3600
    fresh.run_pending()
    assert env.fake.nodes[page]["title"] == "September-2026 Daily"


def test_retitle_skips_trashed_page(env):
    add_meeting(env.store, "a", "A", "2026-09-30 10:00")
    env.run()
    page = env.month_page("September-2026 Standard")
    env.fake.trash(page)
    _queue(env, _rename(env, "Daily"))
    env.run()
    assert env.fake.nodes[page]["title"] == "September-2026 Standard"
    assert env.sync.pending_count() == 0


def test_settings_save_queues_retitle_jobs(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(app.state.notion, "queue_month_retitle", lambda *a: calls.append(a) or 0)
    client = TestClient(app)
    client.put("/v1/settings", headers=H, json=_fields(
        standard_name="Daily", note_templates=[{**_quick(), "name": "Brief"}, _webinar()]))
    assert sorted(calls) == [("quick", "Quick notes", "Brief"), ("standard", "Standard", "Daily")]
    calls.clear()
    client.put("/v1/settings", headers=H, json=_fields(standard_name="Daily"))
    assert calls == []  # nothing renamed
