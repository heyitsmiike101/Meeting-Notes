"""The live transcript's model and beam size are server Settings, like the final transcript's."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from meeting_notes.server import live as live_mod
from meeting_notes.server import settings as settings_mod
from meeting_notes.server.app import create_app

H = {"Authorization": "Bearer live-model-token"}


def test_defaults_and_env_start_value(monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_LIVE_MODEL", raising=False)
    s = settings_mod.Settings(model="small.en")
    assert s.live_model == "base.en" and s.live_beam_size == 1 and s.live_model_name() == "base.en"
    monkeypatch.setenv("MEETING_NOTES_LIVE_MODEL", "same")
    s = settings_mod.Settings(model="small.en")
    assert s.live_model == settings_mod.LIVE_SAME and s.live_model_name() == "small.en"
    assert live_mod.live_model_choice(s) is None
    assert live_mod.live_model_choice(settings_mod.Settings(model="small.en", live_model="tiny.en")) == "tiny.en"


def test_validate_and_choices():
    base = {"model": "small.en", "beam_size": "5", "audio_retention_days": "-1", "retention_check_interval_minutes": "60"}
    s = settings_mod.validate({**base, "live_model": "tiny.en", "live_beam_size": "2"})
    assert (s.live_model, s.live_beam_size) == ("tiny.en", 2)
    with pytest.raises(settings_mod.ValidationError):
        settings_mod.validate({**base, "live_model": "base.en", "live_beam_size": "0"})
    assert settings_mod.Settings(live_model="custom-model").live_model_choices()[-1] == "custom-model"
    assert settings_mod.Settings().live_model_choices()[0] == "same"


class _Fake:
    def __init__(self, **opts):
        self.opts = opts

    def load(self):
        pass


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_NOTES_TOKEN", "live-model-token")
    monkeypatch.setenv("MEETING_NOTES_MODEL", "small.en")
    monkeypatch.delenv("MEETING_NOTES_LIVE_MODEL", raising=False)
    monkeypatch.setattr("meeting_notes.transcribe.protocol.get_transcriber", lambda name, **o: _Fake(**o))
    return create_app(data_root=str(tmp_path / "data"), enable_mcp=False)


def test_settings_page_shows_live_fields_with_explanation(app):
    with TestClient(app) as c:
        page = c.get("/settings", headers=H).text
    assert 'name="live_model"' in page and 'name="live_beam_size"' in page and "Live transcription" in page
    assert "Same as the transcript model" in page and ">Good (145 MB" in page
    assert page.count('class="info-tip-btn"') >= 2 and "How many alternative wordings the model weighs" in page


def test_saving_the_live_model_applies_to_the_next_live_transcriber(app):
    live = app.state.live_preview
    with TestClient(app) as c:
        assert live.transcriber_factory().opts["model_size"] == "base.en"
        form = {"model": "small.en", "beam_size": "5", "audio_retention_days": "-1",
                "retention_check_interval_minutes": "60", "live_model": "tiny.en", "live_beam_size": "2"}
        r = c.post("/settings", data=form, headers=H)
        assert r.status_code == 200 and "Settings saved." in r.text
        t = live.transcriber_factory()
        assert t.opts["model_size"] == "tiny.en" and t.opts["beam_size"] == 2
        c.post("/settings", data={**form, "live_model": "same"}, headers=H)
        t = live.transcriber_factory()
        assert t.opts.get("model_size") == "small.en"  # the final model


def test_a_save_without_the_live_fields_keeps_them(app):
    with TestClient(app) as c:
        base = {"model": "small.en", "beam_size": "5", "audio_retention_days": "-1", "retention_check_interval_minutes": "60"}
        c.post("/settings", data={**base, "live_model": "tiny.en", "live_beam_size": "3"}, headers=H)
        c.post("/settings", data=base, headers=H)  # an older page without the live fields
        s = settings_mod.load_settings(app.state.store.root)
        assert (s.live_model, s.live_beam_size) == ("tiny.en", 3)
