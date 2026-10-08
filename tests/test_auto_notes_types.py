"""Per note type "Generate notes automatically" (``Settings.auto_notes_types``): settings load/migration,
save semantics, the job rule, uploaded transcripts, the web Settings card and the API."""

from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

from meeting_notes.server import settings as settings_mod
from meeting_notes.server.app import create_app
from tests.test_note_type_tagging import _fields, _transcribed

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated:DeprecationWarning"
)

ALL = ["standard", "quick", "webinar"]
H = {"Authorization": "Bearer tok"}


def _write_raw(root, **extra):
    (root / "settings.json").write_text(json.dumps({"model": "base.en", **extra}), encoding="utf-8")


# -- settings ---------------------------------------------------------------------------------------------


def test_defaults_and_a_missing_key_mean_every_type(tmp_path):
    assert settings_mod.Settings().auto_notes_types == ALL
    _write_raw(tmp_path, ai_provider="claude", auto_generate_notes=False)  # the old global key is ignored
    assert settings_mod.load_settings(tmp_path).auto_notes_types == ALL


def test_an_explicit_list_loads_and_unknown_ids_are_dropped(tmp_path):
    _write_raw(tmp_path, auto_notes_types=["quick", "gone", "standard"])
    assert settings_mod.load_settings(tmp_path).auto_notes_types == ["standard", "quick"]
    _write_raw(tmp_path, auto_notes_types=[])
    assert settings_mod.load_settings(tmp_path).auto_notes_types == []


def test_validate_accepts_a_list_or_json_text_and_rejects_junk():
    assert settings_mod.validate(_fields(auto_notes_types=["quick"])).auto_notes_types == ["quick"]
    assert settings_mod.validate(_fields(auto_notes_types='["webinar", "standard"]')).auto_notes_types == [
        "standard", "webinar"]
    assert settings_mod.validate(_fields()).auto_notes_types == ALL
    with pytest.raises(settings_mod.ValidationError, match="auto_notes_types"):
        settings_mod.validate(_fields(auto_notes_types="{nope"))
    with pytest.raises(settings_mod.ValidationError, match="auto_notes_types"):
        settings_mod.validate(_fields(auto_notes_types=5))


def test_a_new_note_type_starts_on_and_a_deleted_one_is_gone():
    new = {"name": "Customer call", "prompt": "Summarize."}
    made = settings_mod.validate(_fields(auto_notes_types=["quick"], note_templates=[new]))
    (custom,) = [t["id"] for t in made.note_templates if t["name"] == "Customer call"]
    assert custom in made.auto_notes_types and "quick" in made.auto_notes_types
    assert "webinar" not in made.auto_notes_types  # a built-in left out of the list stays off
    gone = settings_mod.validate(_fields(auto_notes_types=[custom, "quick"], note_templates=[]))
    assert custom not in gone.auto_notes_types


# -- the job rule -----------------------------------------------------------------------------------------


def test_a_tagged_type_that_is_off_gets_no_notes(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="meeting_notes.server.jobs")
    _, reviews = _transcribed(
        tmp_path, {"note_type": "quick"}, ai_provider="claude", auto_notes_types=["standard", "webinar"]
    )
    assert reviews == []
    assert "not queueing meeting notes" in caplog.text and "quick" in caplog.text


def test_a_tagged_type_that_is_on_gets_notes_even_if_the_default_is_off(tmp_path):
    _, reviews = _transcribed(
        tmp_path, {"note_type": "quick"}, ai_provider="claude", auto_notes_types=["quick"]
    )
    assert [r["template_id"] for r in reviews] == ["quick"]


def test_an_untagged_meeting_follows_the_default_types_flag(tmp_path):
    _, off = _transcribed(tmp_path / "a", ai_provider="claude", default_template_id="webinar",
                          auto_notes_types=["standard", "quick"])
    assert off == []
    _, on = _transcribed(tmp_path / "b", ai_provider="claude", default_template_id="webinar",
                         auto_notes_types=["webinar"])
    assert [r["template_id"] for r in on] == ["webinar"]


def test_provider_disabled_overrides_an_on_type(tmp_path):
    _, reviews = _transcribed(tmp_path, ai_provider="disabled", auto_notes_types=ALL)
    assert reviews == []


# -- uploaded transcripts ---------------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_NOTES_TOKEN", "tok")
    app = create_app(
        transcriber_factory=lambda **_: (_ for _ in ()).throw(AssertionError("nothing may be transcribed")),
        data_root=str(tmp_path / "data"),
        media_root=str(tmp_path / "media"),
    )
    with TestClient(app) as c:
        c.app_ref = app
        yield c


def _save(client, **fields):
    settings_mod.save_settings(client.app_ref.state.store.root, settings_mod.validate(_fields(**fields)))


def test_an_uploaded_transcript_follows_the_same_rule(client):
    store = client.app_ref.state.store
    _save(client, ai_provider="claude", auto_notes_types=["quick"])  # the default type (standard) is off
    off = client.post("/v1/sessions/transcript", headers=H, json={"text": "Jane: We decided to launch."}).json()
    assert off["notes"] is None and store.list_reviews(session_id=off["session_id"]) == []
    _save(client, ai_provider="claude", auto_notes_types=["standard"])
    on = client.post("/v1/sessions/transcript", headers=H, json={"text": "Jane: We decided to launch."}).json()
    assert on["notes"] == "queued"


# -- save semantics, web and API --------------------------------------------------------------------------


def test_a_form_or_api_save_that_omits_the_key_keeps_the_stored_list(client):
    store = client.app_ref.state.store
    _save(client, ai_provider="claude", auto_notes_types=["quick"])
    form = {"model": "base.en", "beam_size": "5", "audio_retention_days": "-1",
            "retention_check_interval_minutes": "60", "ai_provider": "claude"}
    assert "Settings saved" in client.post("/settings", data=form, headers=H).text
    assert settings_mod.load_settings(store.root).auto_notes_types == ["quick"]
    body = {"model": "base.en", "beam_size": 5, "audio_retention_days": -1,
            "retention_check_interval_minutes": 60, "ai_provider": "claude"}
    got = client.put("/v1/settings", headers=H, json=body)
    assert got.status_code == 200 and got.json()["auto_notes_types"] == ["quick"]
    got = client.put("/v1/settings", headers=H, json={**body, "auto_notes_types": []})
    assert got.json()["auto_notes_types"] == []
    assert client.get("/v1/settings", headers=H).json()["auto_notes_types"] == []


def test_an_omitting_save_that_adds_a_note_type_turns_it_on(client):
    _save(client, ai_provider="claude", auto_notes_types=["quick"])
    body = {"model": "base.en", "beam_size": 5, "audio_retention_days": -1,
            "retention_check_interval_minutes": 60, "ai_provider": "claude",
            "note_templates": [{"id": "tabc123", "name": "Sales", "prompt": "p"}]}
    got = client.put("/v1/settings", headers=H, json=body).json()
    assert got["auto_notes_types"] == ["quick", "tabc123"]


def test_web_settings_renders_a_checkbox_per_type_and_round_trips(client):
    _save(client, ai_provider="claude", auto_notes_types=["quick"])
    page = client.get("/settings", headers=H).text
    assert "Generate notes automatically" in page
    assert page.count('class="style-auto-notes"') == 4  # Standard, Quick, Webinar + the blank template
    assert 'name="auto_notes_types"' in page
    form = {"model": "base.en", "beam_size": "5", "audio_retention_days": "-1",
            "retention_check_interval_minutes": "60", "ai_provider": "claude",
            "auto_notes_types": json.dumps(["webinar", "standard"])}
    assert "Settings saved" in client.post("/settings", data=form, headers=H).text
    assert settings_mod.load_settings(client.app_ref.state.store.root).auto_notes_types == ["standard", "webinar"]


def test_note_templates_api_carries_the_flag(client):
    _save(client, ai_provider="claude", auto_notes_types=["quick"])
    items = client.get("/v1/note-templates", headers=H).json()["items"]
    assert {i["id"]: i["auto_notes"] for i in items} == {"standard": False, "quick": True, "webinar": False}
