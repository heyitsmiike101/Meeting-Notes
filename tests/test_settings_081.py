"""0.8.1 settings: model labels, GPU models, live on helper, effort, revision conflicts, explicit checkboxes, pages."""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from meeting_notes.server import settings as settings_mod
from meeting_notes.server import web
from meeting_notes.server.app import create_app

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated:DeprecationWarning"
)
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")

BASE_PUT = {"model": "small.en", "beam_size": 5, "audio_retention_days": -1, "retention_check_interval_minutes": 60}


@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = create_app(transcriber_factory=lambda **kw: None, data_root=str(tmp_path / "data"))
    with TestClient(app) as client:
        client.root = tmp_path / "data"
        yield client


def _form(**over):
    base = {
        "model": "small.en", "beam_size": "5", "audio_retention_days": "-1",
        "retention_check_interval_minutes": "60", "ai_provider": "disabled", "appearance": "system",
        "compute_claim_timeout_seconds": "30", "diarization_without_helper": "run_here",
    }
    base.update(over)
    return base


def _stored(client):
    return settings_mod.load_settings(client.root)


# -- model names ---------------------------------------------------------------------------


def test_model_labels_and_defaults():
    labels = settings_mod.MODEL_LABELS
    assert [labels[k] for k in ("tiny.en", "base.en", "small.en", "large-v3-turbo")] == ["Fastest", "Good", "Better", "Best"]
    assert labels["same"] == "Same as the transcript model"
    d = settings_mod.Settings()
    assert (d.gpu_model, d.gpu_live_model, d.live_on_helper) == ("large-v3-turbo", "small.en", True)
    assert d.gpu_live_model_choices() == ["tiny.en", "base.en", "small.en", "large-v3-turbo"]
    assert d.gpu_model_choices() == ["base.en", "small.en", "large-v3-turbo"]
    assert settings_mod.model_label("my-model") == "my-model"
    assert settings_mod.model_label("base.en").startswith("Good (")


def test_dropdowns_show_labels_and_unknown_ids(app_client):
    app_client.post("/settings", data=_form(model="my-custom", gpu_model="gpu-custom"))
    page = app_client.get("/settings").text
    for label in ("Good (", "Better (", "Best (", "Fastest ("):
        assert f">{label}" in page
    assert ">Same as the transcript model<" in page
    assert '<option value="my-custom" selected>my-custom</option>' in page
    assert '<option value="gpu-custom" selected>gpu-custom</option>' in page
    assert ">base.en<" not in page and ">small.en<" not in page


def test_gpu_models_and_live_on_helper_roundtrip_and_keep(app_client):
    r = app_client.post("/settings", data=_form(gpu_model="small.en", gpu_live_model="tiny.en", live_on_helper="0"))
    assert "Settings saved." in r.text
    s = _stored(app_client)
    assert (s.gpu_model, s.gpu_live_model, s.live_on_helper) == ("small.en", "tiny.en", False)
    app_client.post("/settings", data=_form())  # none of the three on the form: kept
    s = _stored(app_client)
    assert (s.gpu_model, s.gpu_live_model, s.live_on_helper) == ("small.en", "tiny.en", False)
    put = app_client.put("/v1/settings", json=BASE_PUT)
    assert put.status_code == 200
    assert put.json()["gpu_model"] == "small.en" and put.json()["live_on_helper"] is False
    assert app_client.put("/v1/settings", json={**BASE_PUT, "gpu_model": "bad name!"}).status_code == 400


# -- effort -----------------------------------------------------------------------------------


def test_effort_roundtrip_validation_and_page(app_client):
    page = app_client.get("/settings").text
    assert 'name="claude_effort"' in page and 'name="codex_effort"' in page
    assert "Higher effort is slower and uses more of your plan" in page
    assert page.count("Effort</span>") == 2  # Ollama has none
    r = app_client.post("/settings", data=_form(ai_provider="claude", claude_effort="xhigh", codex_effort="minimal"))
    assert "Settings saved." in r.text
    s = _stored(app_client)
    assert (s.claude_effort, s.codex_effort) == ("xhigh", "minimal")
    app_client.post("/settings", data=_form(ai_provider="claude"))  # omitted: kept
    s = _stored(app_client)
    assert (s.claude_effort, s.codex_effort) == ("xhigh", "minimal")
    assert "claude_effort" in app_client.post("/settings", data=_form(claude_effort="extreme")).text
    assert app_client.get("/v1/settings").json()["claude_effort"] == "xhigh"
    with pytest.raises(settings_mod.ValidationError):
        settings_mod.validate({**BASE_PUT, "codex_effort": "max"})  # not a Codex level
    assert settings_mod.validate({**BASE_PUT, "claude_effort": "max"}).claude_effort == "max"
    assert settings_mod.validate(BASE_PUT).codex_effort == ""


# -- revision and explicit checkboxes -------------------------------------------------------------


def test_stale_form_is_refused_without_partial_save(app_client):
    page = app_client.get("/settings").text
    assert 'name="revision" value="0"' in page
    first = app_client.post("/settings", data=_form(revision="0", beam_size="7"))
    assert "Settings saved." in first.text and _stored(app_client).revision == 1
    stale = app_client.post("/settings", data=_form(revision="0", beam_size="9", model="base.en"))
    assert "Settings were changed somewhere else; nothing was saved. Reload to see them." in stale.text
    s = _stored(app_client)
    assert s.beam_size == 7 and s.model == "small.en" and s.revision == 1
    assert "Settings saved." in app_client.post("/settings", data=_form(revision="1", beam_size="8")).text
    assert "Settings saved." in app_client.post("/settings", data=_form(beam_size="6")).text  # no revision sent
    assert _stored(app_client).revision == 3


def test_put_with_stale_revision_is_409(app_client):
    body = {**app_client.get("/v1/settings").json(), "model": "small.en"}
    assert body["revision"] == 0
    ok = app_client.put("/v1/settings", json={**body, "beam_size": 3})
    assert ok.status_code == 200 and ok.json()["revision"] == 1
    stale = app_client.put("/v1/settings", json={**body, "beam_size": 4})
    assert stale.status_code == 409 and "changed somewhere else" in stale.json()["detail"]
    assert _stored(app_client).beam_size == 3
    put = app_client.put("/v1/settings", json={**body, "revision": 1, "compute_offload_enabled": True})
    assert put.status_code == 200, put.text
    # no revision at all still works, and an omitted boolean keeps its stored value
    r = app_client.put("/v1/settings", json=BASE_PUT)
    assert r.status_code == 200 and r.json()["compute_offload_enabled"] is True


def test_absent_checkbox_fields_keep_stored_and_explicit_zero_turns_off(app_client):
    app_client.post("/settings", data=_form(
        compute_offload_enabled="on", diarization_enabled="on", live_on_helper="on",
        delete_audio_only_after_success="on",
    ))
    app_client.post("/settings", data=_form())  # a script that carries none of the checkboxes
    s = _stored(app_client)
    assert (s.compute_offload_enabled, s.diarization_enabled, s.live_on_helper, s.delete_audio_only_after_success) == (True,) * 4
    app_client.post("/settings", data=_form(
        compute_offload_enabled="0", diarization_enabled="0", live_on_helper="0", delete_audio_only_after_success="0",
    ))
    s = _stored(app_client)
    assert (s.compute_offload_enabled, s.diarization_enabled, s.live_on_helper, s.delete_audio_only_after_success) == (False,) * 4


def test_every_checkbox_posts_a_hidden_zero_before_it(app_client):
    page = app_client.get("/settings").text
    for name in ("compute_offload_enabled", "diarization_enabled", "live_on_helper", "delete_audio_only_after_success"):
        hidden = page.index(f'<input type="hidden" name="{name}" value="0">')
        box = page.index(f'<input type="checkbox" name="{name}"')
        assert 0 < box - hidden < 120, name


def test_appearance_switch_keeps_the_revision(app_client):
    before = _stored(app_client).revision
    assert app_client.put("/v1/appearance", json={"appearance": "dark"}).status_code == 200
    assert _stored(app_client).revision == before


# -- pages ----------------------------------------------------------------------------------------


def test_settings_has_five_pages_in_one_form(app_client):
    page = app_client.get("/settings").text
    start = page.index('<nav class="settings-nav"')
    nav = page[start:page.index("</nav>", start)]
    found = re.findall(r'<a href="#([a-z]+)" data-page="[a-z]+">([^<]+)</a>', nav)
    assert found == [
        ("general", "General"), ("transcription", "Transcription &amp; speakers"), ("notes", "Meeting notes"),
        ("access", "Notion &amp; AI access"), ("recorders", "Recorders &amp; install"),
    ]
    assert page.count('<form method="post" action="/settings"') == 1
    assert "mn-settings-page" in page and "localStorage" in page
    for heading in (
        "settings-appearance-heading", "settings-retention-heading", "settings-index-heading",
        "settings-transcription-heading", "settings-live-heading", "settings-speaker-labels-heading",
        "settings-names-heading", "settings-ai-heading", "settings-notetypes-heading", "settings-notion-heading",
        "settings-agents-heading", "settings-install-heading", "settings-recorders-heading",
        "settings-logs-heading", "gpu-helpers-heading",
    ):
        assert f'id="{heading}"' in page, heading
    for label in (
        "Transcript model &mdash; this server", "Transcript model &mdash; GPU helper",
        "Live model &mdash; this server", "Live model &mdash; GPU helper", "Send live transcription to GPU helpers",
    ):
        assert label in page, label
    # each section is on the page the contract names
    def page_of(heading):
        m = re.search(r'data-page="([a-z]+)"[^>]*aria-labelledby="%s"' % heading, page)
        return m.group(1)
    assert page_of("settings-speaker-labels-heading") == "transcription"
    assert page_of("settings-names-heading") == "transcription"
    assert page_of("gpu-helpers-heading") == "recorders" and page_of("settings-logs-heading") == "recorders"
    assert page_of("settings-agents-heading") == "access" and page_of("settings-ai-heading") == "notes"


def test_recorders_page_has_install_link():
    page = web.render_recorders_page(token_configured=True)
    assert 'id="rec-install" href="/install"' in page and "Install a recorder" in page


@needs_node
def test_helper_live_status_text():
    script = (
        "function escapeHtml(s){return String(s);}\n"
        + web._GPU_HELPERS_JS
        + "\nconsole.log(JSON.stringify([gpuLiveText({live:{connected:true,served:5,avg_ms:400}}),"
          "gpuLiveText({live:{connected:false}}),gpuLiveText({}),gpuLiveText({live:{connected:true,served:0,avg_ms:null}})]));"
    )
    out = json.loads(subprocess.run([NODE, "-e", script], capture_output=True, text=True, encoding="utf-8", check=True).stdout)
    assert out == ["Live: on · avg 0.4 s", "Live: off", "", "Live: on"]
