"""Live preview: shorter passes, shorter pieces, and its own lighter model (0.7.16)."""

from __future__ import annotations

import pytest

from meeting_notes.server import live as live_mod
from meeting_notes.server.app import create_app


def test_live_timing_is_short():
    assert live_mod.LIVE_INTERVAL <= 2.0
    assert live_mod.LIVE_MAX_UTTERANCE <= 10.0
    assert live_mod.LIVE_BEAM_SIZE == 1


@pytest.mark.parametrize("env,expected", [(None, "base.en"), ("tiny.en", "tiny.en"), ("same", None), ("", "base.en")])
def test_live_model_choice(monkeypatch, env, expected):
    if env is None:
        monkeypatch.delenv("MEETING_NOTES_LIVE_MODEL", raising=False)
    else:
        monkeypatch.setenv("MEETING_NOTES_LIVE_MODEL", env)
    assert live_mod.live_model_choice() == expected


class _FakeTranscriber:
    def __init__(self, fail_load=False, **opts):
        self.opts, self.fail_load = opts, fail_load

    def load(self):
        if self.fail_load:
            raise RuntimeError("model not in cache")


def _live_factory(tmp_path, monkeypatch, fail_models=()):
    calls = []

    def fake_get(name, **opts):
        calls.append(opts)
        return _FakeTranscriber(fail_load=opts.get("model_size") in fail_models, **opts)

    monkeypatch.setattr("meeting_notes.transcribe.protocol.get_transcriber", fake_get)
    monkeypatch.setenv("MEETING_NOTES_MODEL", "small.en")
    monkeypatch.delenv("MEETING_NOTES_LIVE_MODEL", raising=False)
    app = create_app(data_root=str(tmp_path / "data"), enable_mcp=False)
    return app.state.live_preview.transcriber_factory, calls


def test_live_uses_the_lighter_model_with_greedy_decoding(tmp_path, monkeypatch):
    factory, calls = _live_factory(tmp_path, monkeypatch)
    t = factory()
    assert t.opts["model_size"] == "base.en" and t.opts["beam_size"] == 1 and t.opts["word_timestamps"] is False


def test_live_falls_back_to_the_configured_model(tmp_path, monkeypatch):
    factory, calls = _live_factory(tmp_path, monkeypatch, fail_models=("base.en",))
    t = factory()
    assert t.opts.get("model_size") == "small.en" and t.opts["beam_size"] == 1
