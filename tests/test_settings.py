"""Persistence and validation tests for operator-configured server settings."""

from __future__ import annotations

import pytest

from meeting_notes.server import settings


def _fields(**overrides):
    fields = {
        "model": "base.en",
        "beam_size": 5,
        "audio_retention_days": -1,
        "delete_audio_only_after_success": True,
        "retention_check_interval_minutes": 60,
    }
    fields.update(overrides)
    return fields


def test_ai_workflow_is_persisted_and_exposed(tmp_path):
    workflow = "# Custom workflow\n\nUse only the transcript."
    configured = settings.validate(_fields(ai_workflow=workflow))
    settings.save_settings(tmp_path, configured)

    loaded = settings.load_settings(tmp_path)
    assert loaded.ai_workflow == workflow
    assert loaded.to_dict()["ai_workflow"] == workflow


@pytest.mark.parametrize("workflow", ["", "   ", 42])
def test_ai_workflow_must_be_a_nonempty_string(workflow):
    with pytest.raises(settings.ValidationError, match="ai_workflow must not be empty"):
        settings.validate(_fields(ai_workflow=workflow))


def test_ai_workflow_has_a_size_limit():
    with pytest.raises(settings.ValidationError, match="ai_workflow must be"):
        settings.validate(_fields(ai_workflow="x" * (settings.MAX_AI_WORKFLOW_CHARS + 1)))


def test_claude_provider_is_accepted_and_persisted(tmp_path):
    configured = settings.validate(_fields(ai_provider="claude", claude_model="opus"))
    assert configured.ai_provider == "claude"
    assert configured.claude_model == "opus"

    settings.save_settings(tmp_path, configured)
    loaded = settings.load_settings(tmp_path)
    assert loaded.ai_provider == "claude"
    assert loaded.claude_model == "opus"


def test_claude_model_may_be_blank_for_account_default():
    configured = settings.validate(_fields(ai_provider="claude", claude_model=""))
    assert configured.claude_model == ""


@pytest.mark.parametrize("bad_model", ["opus; rm -rf /", "has space", "a" * 81])
def test_claude_model_rejects_invalid_names(bad_model):
    with pytest.raises(settings.ValidationError, match="claude_model must be a valid model name"):
        settings.validate(_fields(ai_provider="claude", claude_model=bad_model))


def test_appearance_defaults_to_system_and_round_trips(tmp_path):
    assert settings.Settings().appearance == "system"
    assert settings.validate(_fields()).appearance == "system"
    for value in settings.APPEARANCE_CHOICES:
        configured = settings.validate(_fields(appearance=value.upper()))
        assert configured.appearance == value
        settings.save_settings(tmp_path, configured)
        assert settings.load_settings(tmp_path).appearance == value
        assert settings.load_settings(tmp_path).to_dict()["appearance"] == value


def test_appearance_rejects_unknown_values_and_load_falls_back(tmp_path):
    with pytest.raises(settings.ValidationError, match="appearance must be"):
        settings.validate(_fields(appearance="neon"))
    (tmp_path / "settings.json").write_text('{"model": "base.en", "appearance": "neon"}', encoding="utf-8")
    assert settings.load_settings(tmp_path).appearance == "system"
