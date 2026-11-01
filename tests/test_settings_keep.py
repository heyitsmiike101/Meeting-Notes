"""A save that leaves a setting out keeps the stored value (one list: ``settings.KEEP_IF_MISSING``)."""

from __future__ import annotations

import dataclasses
import re

import pytest
from fastapi.testclient import TestClient

from meeting_notes.server import settings as settings_mod
from meeting_notes.server import web
from meeting_notes.server.app import create_app

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated:DeprecationWarning"
)
BASE_PUT = {"model": "small.en", "beam_size": 5, "audio_retention_days": -1, "retention_check_interval_minutes": 60}


@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = create_app(transcriber_factory=lambda **kw: None, data_root=str(tmp_path / "data"))
    with TestClient(app) as client:
        client.root = tmp_path / "data"
        yield client


def test_put_without_diarization_fields_keeps_the_stored_ones(app_client):
    stored = dataclasses.replace(
        settings_mod.load_settings(app_client.root), diarization_enabled=True,
        diarization_model="pyannote/custom-model", diarization_min_speakers=2, diarization_max_speakers=5)
    settings_mod.save_settings(app_client.root, stored)
    put = app_client.put("/v1/settings", json=BASE_PUT)
    assert put.status_code == 200
    after = settings_mod.load_settings(app_client.root)
    assert (after.diarization_enabled, after.diarization_model) == (True, "pyannote/custom-model")
    assert (after.diarization_min_speakers, after.diarization_max_speakers) == (2, 5)


def test_form_without_diarization_fields_keeps_the_stored_ones(app_client):
    stored = dataclasses.replace(
        settings_mod.load_settings(app_client.root), diarization_model="pyannote/custom-model",
        diarization_min_speakers=2, diarization_max_speakers=5)
    settings_mod.save_settings(app_client.root, stored)
    form = {"model": "small.en", "beam_size": "5", "audio_retention_days": "-1",
            "retention_check_interval_minutes": "60"}
    app_client.post("/settings", data=form)
    after = settings_mod.load_settings(app_client.root)
    assert (after.diarization_model, after.diarization_min_speakers, after.diarization_max_speakers) == (
        "pyannote/custom-model", 2, 5)


def test_keep_stored_does_not_change_the_payload_and_notion_differs_by_route():
    stored = settings_mod.Settings(notion_parents={"standard": "a" * 32}, notion_auto_types=["standard"],
                                   notion_auto_copy=True)
    payload = {"model": "x"}
    api = settings_mod.keep_stored(payload, stored, form=False)
    assert payload == {"model": "x"}
    assert api["notion_parents"] == stored.notion_parents and api["notion_auto_types"] == ["standard"]
    # API: notion_auto_copy sent alone is enough to leave notion_auto_types to it; the form's group is all or none.
    assert "notion_auto_types" not in settings_mod.keep_stored({"notion_auto_copy": False}, stored, form=False)
    assert settings_mod.keep_stored({"notion_auto_copy": False}, stored, form=True)["notion_auto_copy"] is True


def test_every_setting_is_kept_posted_by_the_form_or_deliberately_reset():
    """The next field added to ``Settings`` must be put in one of the lists, so it cannot silently reset."""
    fields = {f.name for f in dataclasses.fields(settings_mod.Settings)}
    keep = set(settings_mod.KEEP_IF_MISSING)
    special = set(settings_mod.KEPT_SPECIALLY)
    reset = set(settings_mod.RESET_IF_MISSING)
    assert not (keep & special or keep & reset or special & reset), "a setting is in two lists"
    missing = fields - keep - special - reset
    assert not missing, f"{sorted(missing)}: add each to KEEP_IF_MISSING (or KEPT_SPECIALLY / RESET_IF_MISSING)"
    assert (keep | special | reset) <= fields, "a list names a setting that no longer exists"
    # A deliberately reset setting must be one the web form always sends.
    page = web.render_settings_page(settings_mod.Settings(), token_configured=True)
    posted = set(re.findall(r'name="([a-z_]+)"', page))
    assert reset - {"revision"} <= posted | {"model"}, sorted(reset - posted)
    # Kept settings really are kept: every one survives a save that sends only the required fields.
    stored = settings_mod.Settings()
    kept = settings_mod.keep_stored({}, stored, form=False)
    assert keep <= set(kept)
