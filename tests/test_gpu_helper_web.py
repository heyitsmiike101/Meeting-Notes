"""GPU helper web UI: Settings block, form round-trip, Recorders section, pipeline texts, installer scripts."""

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
from meeting_notes.server.store import Store

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated:DeprecationWarning"
)
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = create_app(transcriber_factory=lambda **kw: None, data_root=str(tmp_path / "data"))
    with TestClient(app) as client:
        client.app_ = app
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


# -- settings block ----------------------------------------------------------


def test_settings_page_renders_gpu_helpers_block(app_client):
    page = app_client.get("/settings").text
    assert 'id="gpu-helpers-heading"' in page and ">GPU helpers<" in page
    assert "Use GPU helpers" in page
    assert 'name="compute_offload_enabled"' in page
    assert 'name="compute_claim_timeout_seconds"' in page and 'min="5" max="300"' in page
    assert "Wait for a helper (seconds)" in page
    assert 'name="diarization_enabled"' in page and ">Speaker labels<" in page
    assert 'name="diarization_model"' in page
    assert 'name="diarization_min_speakers"' in page and 'name="diarization_max_speakers"' in page
    assert "Speaker labels without a helper" in page
    assert "Run on this server (slow)" in page and ">Skip<" in page
    assert 'id="gpu-pack-btn"' in page and "Prepare download" in page
    assert "/v1/compute/pack/prepare" in page and "/v1/compute/pack/status" in page
    assert "Connected helpers" in page and 'id="gpu-helpers-box"' in page
    assert "/v1/compute/helpers/" in page  # revoke
    assert "tip-gpu-helpers" in page  # the "i" explaining a helper
    # off by default
    assert 'name="compute_offload_enabled" id="gpu-offload" value="on">' in page


def test_gpu_status_words_in_scripts():
    js = web._GPU_HELPERS_JS + web._GPU_SETTINGS_JS
    for text in ("Idle", "Working on ", "Paused", "Offline", "Not prepared", "Preparing", "Ready", "Out of date",
                 "Refresh download", "Error"):
        assert text in js


def test_settings_roundtrip_new_fields(app_client):
    r = app_client.post("/settings", data=_form(
        compute_offload_enabled="on", compute_claim_timeout_seconds="45", diarization_enabled="on",
        diarization_model="pyannote/x", diarization_min_speakers="2", diarization_max_speakers="5",
        diarization_without_helper="skip",
    ))
    assert r.status_code == 200 and "Settings saved." in r.text
    saved = settings_mod.load_settings(app_client.root)
    assert saved.compute_offload_enabled is True
    assert saved.compute_claim_timeout_seconds == 45
    assert saved.diarization_enabled is True and saved.diarization_model == "pyannote/x"
    assert (saved.diarization_min_speakers, saved.diarization_max_speakers) == (2, 5)
    assert saved.diarization_without_helper == "skip"
    page = app_client.get("/settings").text
    assert 'id="gpu-offload" value="on" checked>' in page
    assert 'value="45"' in page
    assert '<option value="skip" selected>' in page
    # unchecking the switches turns them off
    app_client.post("/settings", data=_form())
    saved = settings_mod.load_settings(app_client.root)
    assert saved.compute_offload_enabled is False and saved.diarization_enabled is False


def test_stale_form_without_gpu_fields_keeps_them(app_client):
    app_client.post("/settings", data=_form(
        compute_offload_enabled="on", compute_claim_timeout_seconds="60", diarization_enabled="on",
        diarization_without_helper="skip",
    ))
    stale = _form()
    del stale["compute_claim_timeout_seconds"], stale["diarization_without_helper"]
    assert app_client.post("/settings", data=stale).status_code == 200
    saved = settings_mod.load_settings(app_client.root)
    assert saved.compute_offload_enabled is True and saved.diarization_enabled is True
    assert saved.compute_claim_timeout_seconds == 60 and saved.diarization_without_helper == "skip"


def test_out_of_range_wait_is_refused(app_client):
    r = app_client.post("/settings", data=_form(compute_claim_timeout_seconds="1000"))
    assert "compute_claim_timeout_seconds" in r.text


def test_web_data_route_lists_helpers_and_pack(app_client):
    reg = app_client.app_.state.compute_registry
    made = reg.create_helper("Office PC", "inst-1")
    reg.touch(made["helper_id"], {"state": "idle", "gpu": {"name": "RTX 4070", "vram_mb": 12000, "driver": "555"}})
    gone = reg.create_helper("Old PC", "inst-2")
    reg.revoke_helper(gone["helper_id"])
    data = app_client.get("/settings/gpu-helpers").json()
    names = [h["name"] for h in data["helpers"]]
    assert names == ["Office PC"]
    h = data["helpers"][0]
    assert h["online"] is True and h["gpu"]["name"] == "RTX 4070"
    assert not any("key" in k or "lease" in k for k in h)
    assert data["pack"]["state"] in ("idle", "ready") and data["pack"]["has_pack"] is False


# -- recorders ---------------------------------------------------------------


def test_recorders_page_has_gpu_helpers_section():
    page = web.render_recorders_page(token_configured=True)
    assert 'id="gpu-helpers-heading"' in page and "GPU helpers" in page
    assert "/settings/gpu-helpers" in page and "gpuHelpersTable(list, false)" in page


@needs_node
def test_helper_status_wording_and_rows():
    script = (
        "function escapeHtml(s){return String(s==null?'':s).replace(/[&<>\"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[c];});}\n"
        "function badge(c,l,t){return '['+c+':'+l+']';}\n"
        "function agoText(){return '2 minutes ago';}\n"
        + web._GPU_HELPERS_JS
        + "\nvar out=[gpuHelperStatus({online:true,state:'idle'}).text,"
          "gpuHelperStatus({online:true,state:'busy',current_board:'M-0142',progress:0.42}).text,"
          "gpuHelperStatus({online:true,state:'idle',paused:true}).text,"
          "gpuHelperStatus({online:false,state:'offline'}).text,"
          "gpuHelpersTable([{helper_id:'a',name:'Office PC',gpu:{name:'RTX 4070'},online:true,state:'idle',last_seen:1}],true)];"
          "console.log(JSON.stringify(out));"
    )
    out = json.loads(subprocess.run([NODE, "-e", script], capture_output=True, text=True, encoding="utf-8", check=True).stdout)
    assert out[:4] == ["Idle", "Working on M-0142 · 42%", "Paused", "Offline"]
    assert "Office PC" in out[4] and "RTX 4070" in out[4] and "data-gpu-revoke=\"a\"" in out[4]
    assert "2 minutes ago" in out[4]


# -- pipeline "where it ran" ---------------------------------------------------


def _where_js(cases):
    src = web._JS_HELPERS
    start = src.index("function computeWhere(tr)")
    end = src.index("function processingState(row)")
    return src[start:end] + "\nconsole.log(JSON.stringify(" + json.dumps(cases) + ".map(computeWhere)));"


@needs_node
def test_pipeline_where_texts():
    def tr(state, percent, **compute):
        return {"state": state, "percent": percent, "compute": compute}

    cases = [
        tr("complete", 100, status="done", helper_name="Office PC", device="cuda", fallback_reason=None),
        tr("transcribing", 42.3, status="leased", helper_name="Office PC", device=None, fallback_reason=None),
        tr("pending", 0, status="offered", helper_name="", device=None, fallback_reason=None),
        tr("complete", 100, status="local", helper_name="", device=None, fallback_reason="no helper claimed in time"),
        tr("transcribing", 10, status="local", helper_name="", device=None, fallback_reason="helper went silent"),
        {"state": "complete", "percent": 100, "compute": None},
        {"state": "complete", "percent": 100},
    ]
    out = json.loads(subprocess.run([NODE, "-e", _where_js(cases)], capture_output=True, text=True, encoding="utf-8", check=True).stdout)
    assert out == [
        "Transcribed on Office PC (GPU)",
        "Transcribing on Office PC (GPU) · 42%",
        "Waiting for a GPU helper",
        "Transcribed on this server (GPU helper: no helper claimed in time)",
        "Transcribing on this server (GPU helper: helper went silent)",
        "",
        "",
    ]


def test_pipeline_exposes_only_display_fields():
    job = {
        "job_id": "j1", "state": "running", "progress": 0.4,
        "compute": {"status": "leased", "helper_name": "Office PC", "attempt": "secretattempt",
                    "lease_hash": "secrethash", "helper_id": "h1", "device": None, "fallback_reason": None},
    }
    pipeline = Store._pipeline({"upload": {"state": "complete"}}, [job])
    compute = pipeline["transcription"]["compute"]
    assert compute == {"status": "leased", "helper_name": "Office PC", "device": None, "fallback_reason": None}
    assert "secret" not in json.dumps(pipeline)
    assert Store._pipeline({}, [{"job_id": "j2", "state": "done"}])["transcription"]["compute"] is None


def test_meetings_pages_use_where_text():
    helpers = web._JS_HELPERS
    assert "st.where = computeWhere(" in helpers and "status.where" in helpers
    source = open(web.__file__, encoding="utf-8").read()
    assert "var where=computeWhere(transcription);" in source  # the meeting page checklist
    assert "cell-sub" in web.render_transcriptions_page(token_configured=True)


# -- installer / uninstaller ---------------------------------------------------


def test_uninstaller_removes_gpu_runtime_and_run_value():
    text = web.render_client_uninstaller()
    assert 'Remove-ItemProperty -Path "HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Run" -Name "MeetingNotes"' in text
    assert '"MeetingNotes-GPU"' in text
    assert "Remove-Item -LiteralPath $gpuRuntimeDir -Recurse -Force" in text
    assert "Stop-Process" in text
    assert "RunAs" not in text and "-Verb" not in text and "HKLM" not in text


def test_installer_stops_gpu_runtime_and_never_deletes_it():
    text = web.render_client_installer("http://server:8000")
    assert '"MeetingNotes-GPU"' in text and "Stop-Process" in text
    gpu_lines = [ln for ln in text.splitlines() if "gpuRuntimeDir" in ln]
    assert not any("Remove-Item" in ln for ln in gpu_lines)
    assert "gpu-runtime" not in text
