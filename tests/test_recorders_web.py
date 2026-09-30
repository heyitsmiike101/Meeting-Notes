"""The Recorders web page (live presence and remote control): markup, nav, script sanity, helpers."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from meeting_notes.server import web
from meeting_notes.server.settings import Settings
from meeting_notes.server.web import render_recorders_page, render_settings_page, stylesheet_text

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _page(**kw):
    return render_recorders_page(token_configured=True, **kw)


def _scripts(page: str):
    return re.findall(r"<script>(.*?)</script>", page, flags=re.S)


# -- markup ------------------------------------------------------------------


def test_recorders_page_has_nav_item_in_sidebar_and_tab_bar():
    page = _page()
    sidebar = page[page.index('<nav class="primary"'): page.index("</nav>", page.index('<nav class="primary"'))]
    tabbar = page[page.index('<nav class="tabbar"'): page.index("</nav>", page.index('<nav class="tabbar"'))]
    for nav in (sidebar, tabbar):
        assert '<a href="/recorders" aria-current="page">' in nav
        assert "<span>Recorders</span>" in nav
    # the tab bar keeps every item on one row: 5 tabs
    assert tabbar.count("<a href=") == 5
    # other pages list it too, without marking it current
    home = web.render_home_page(token_configured=True)
    assert '<a href="/recorders">' in home


def test_recorders_page_structure_and_empty_state():
    page = _page(appearance="dark")
    assert '<html lang="en" data-theme="dark">' in page
    assert "<title>Recorders</title>" in page
    assert "<h1>Recorders</h1>" in page
    assert 'id="rec-count" aria-live="polite"' in page
    assert 'id="rec-grid"' in page
    assert "No recorders are running" in page
    assert "Open Meeting Notes on a computer and it appears here." in page
    assert 'id="confirm-dialog"' in page  # Stop recording asks first
    assert 'id="rec-conn"' in page and "Reconnecting..." in page
    assert "<style>" not in page and "https://" not in page.replace("https:'", "")  # no CDN, no inline CSS


def test_recorders_page_talks_to_the_documented_endpoints():
    js = web._RECORDERS_JS
    assert "'/v1/recorders'" in js
    assert "'/v1/recorders/events'" in js or "/v1/recorders/events" in js
    assert "/commands" in js and "encodeURIComponent(id)" in js
    assert "new WebSocket(" in js and "wss:" in js
    assert "setInterval" in js and "fetch('/v1/recorders'" in js  # one fallback GET; ticking is local
    # every command in the shared protocol that the page sends is a real command
    from meeting_notes import remote

    sent = set(re.findall(r"recSend\(card, [^,]+, '([a-z_]+)'", js))
    assert {"start", "stop", "refresh_devices", "retry_uploads", "set_name"} <= sent
    assert sent <= set(remote.COMMANDS)
    # commands the page can send cover the whole UI surface
    for name in ("start", "stop", "mute", "unmute", "refresh_devices", "accept_call_prompt", "dismiss_call_prompt",
                 "keep_recording", "stop_suggested", "retry_uploads", "check_update", "install_update", "set_name"):
        assert name in js


def test_icons_used_by_the_page_exist_in_both_icon_tables():
    names = set(re.findall(r"icon\('([a-z-]+)'", web._RECORDERS_JS)) | set(
        re.findall(r"'(laptop|monitor|mic-off|speaker-off|mic|speaker)'", web._RECORDERS_JS)
    )
    names |= {"radio", "laptop", "mic-off", "speaker-off", "info", "alert", "check", "refresh"}
    for name in names:
        assert name in web._ICON_PATHS, name
    icons_js = (Path(web.__file__).parent / "static" / "icons.js").read_text(encoding="utf-8")
    payload = json.loads(icons_js[icons_js.index("{"): icons_js.rindex("}") + 1])
    assert payload == web._ICON_PATHS
    for name in ("laptop", "radio", "mic-off", "speaker-off", "info"):
        assert 'stroke-width' not in web._ICON_PATHS[name]  # weight comes from the stylesheet


def test_recorders_css_uses_tokens_only():
    css = stylesheet_text()
    start = css.index("/* ---- Recorders")
    block = css[start: css.index("@media (max-width:1100px)", start)]
    assert ".rec-card" in block and ".rec-meter" in block
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", block), "hard-coded hex in the recorders styles"
    assert "box-shadow" not in block  # cards are flat
    assert "uppercase" not in block and "letter-spacing" not in block
    assert "rec-mute.btn.icon-only" in css  # 40px on phone


# -- settings ----------------------------------------------------------------


def test_settings_recorders_section_is_now_a_link_to_the_page():
    page = render_settings_page(Settings(), token_configured=True)
    assert 'id="settings-recorders-heading">Recorders</h2>' in page
    assert 'href="#settings-recorders-heading"' in page
    assert 'href="/recorders"' in page
    assert "appear on the Recorders page while they are open" in page
    for gone in ("recorders-box", "loadRecorders", "renderRecorders", "/v1/clients", "Connected recorders"):
        assert gone not in page


# -- script ------------------------------------------------------------------


@needs_node
def test_inline_scripts_parse(tmp_path):
    for i, script in enumerate(_scripts(_page())):
        if not script.strip():
            continue
        path = tmp_path / f"s{i}.js"
        path.write_text(script, encoding="utf-8")
        done = subprocess.run([NODE, "--check", str(path)], capture_output=True, text=True)
        assert done.returncode == 0, done.stderr


def test_innerhtml_only_receives_static_templates_and_icons():
    """Server values reach the DOM through textContent/value only: every innerHTML write is a fixed template."""
    rhs = re.findall(r"\.innerHTML\s*=\s*([^;]+);", web._RECORDERS_JS)
    assert rhs
    for expr in rhs:
        assert re.fullmatch(r"REC_CARD_HTML|icon\([^)]*\)", expr.strip()), expr
    # the templates themselves never interpolate a variable holding item data
    template = re.search(r"var REC_CARD_HTML =(.*?);\n\nfunction recMakeCard", web._RECORDERS_JS, flags=re.S).group(1)
    assert "escapeHtml" not in template and "item" not in template and "it." not in template


HARNESS = r"""
const vm = require('vm'), fs = require('fs');
const ctx = {window: {MN_ICONS: {}}, console};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), ctx);
const out = vm.runInContext(fs.readFileSync(process.argv[3], 'utf8'), ctx);
process.stdout.write(JSON.stringify(out));
"""

CHECKS = r"""
(function () {
  var hostile = '<img src=x onerror=alert(1)>';
  var now = 1000000;
  var entry = {at: now - 5000, item: {device: hostile, state: {status: 'recording', meeting: {name: hostile, elapsed_sec: 2530}}}};
  var idle = {at: now, item: {device: 'x', state: {}}};
  var fin = {at: now - 5000, item: {device: 'x', state: {status: 'finishing', meeting: {elapsed_sec: 10}}}};
  return {
    clock: [recClock(0), recClock(2530), recClock(3600 + 61), recClock(-5), recClock('junk')],
    meter: [recMeter(0), recMeter(0.25), recMeter(1), recMeter(7), recMeter(-1), recMeter('x'), recMeter(null)],
    recording: recStatus(entry, now), idle: recStatus(idle, now), finishing: recStatus(fin, now),
    elapsedIdle: recElapsed(idle, now),
    upload: [recUploadLine({}), recUploadLine({pending: 2, failed: 1, awaiting_transcript: 1, current_percent: 40}),
             recUploadLine({pending: 1}), recUploadLine({failed: 3}), recUploadLine({awaiting_transcript: 2})],
    call: [recCallText({label: 'Teams', name: 'Budget review'}), recCallText({label: '', name: ''}), recCallText({label: 'Zoom', name: hostile})],
    toasts: [recToast('start', {}, hostile), recToast('mute', {track: 'mic'}, 'PC'), recToast('unmute', {track: 'system'}, 'PC'),
             recToast('check_update', {}, 'PC', {update: {available: true}}), recToast('check_update', {}, 'PC', {update: {available: false}})],
    state: recState({state: {control: {allowed: false}, banners: 'no', status: 'weird'}}),
    stateDefaults: recState(null),
    html: REC_CARD_HTML,
    escaped: escapeHtml(hostile)
  };
})()
"""


@needs_node
def test_pure_helpers_in_node(tmp_path):
    script = tmp_path / "page.js"
    script.write_text(web._JS_HELPERS + web._RECORDERS_JS, encoding="utf-8")
    checks = tmp_path / "checks.js"
    checks.write_text(CHECKS, encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    done = subprocess.run([NODE, str(harness), str(script), str(checks)], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)

    assert out["clock"] == ["00:00:00", "00:42:10", "01:01:01", "00:00:00", "00:00:00"]
    assert out["meter"][:3] == [0, 0.5, 1]
    assert out["meter"][3:] == [1, 0, 0, 0]
    # the recording clock adds the time since the frame arrived; idle and finishing do not tick
    assert out["recording"] == {"cls": "live", "label": "Recording 00:42:15"}
    assert out["idle"] == {"cls": "none", "label": "Idle"}
    assert out["finishing"] == {"cls": "running", "label": "Finishing"}
    assert out["elapsedIdle"] is None
    assert out["upload"][0] == {"text": "", "failed": False}
    assert out["upload"][1] == {"text": "2 uploads pending, 1 failed · uploading 40% · 1 awaiting transcript", "failed": True}
    assert out["upload"][2]["text"] == "1 upload pending" and out["upload"][2]["failed"] is False
    assert out["upload"][3]["text"] == "3 failed"
    assert out["upload"][4]["text"] == "2 awaiting transcript"
    assert out["call"][0] == "Teams call detected: Budget review"
    assert out["call"][1] == "Call detected"
    assert out["toasts"][1:] == [
        "Microphone muted on PC.",
        "Meeting audio unmuted on PC.",
        "An update is available for PC.",
        "PC is up to date.",
    ]
    assert out["state"]["allowed"] is False and out["state"]["status"] == "idle" and out["state"]["banners"] == []
    assert out["stateDefaults"]["allowed"] is True and out["stateDefaults"]["suggestion"] is None
    # hostile text stays plain data: helpers return it verbatim for textContent, and the template has none of it
    assert "<img" not in out["html"]
    assert "<img" not in out["escaped"] and "&lt;img" in out["escaped"]


def test_recorders_route_needs_web_auth_and_renders(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from meeting_notes.server.app import create_app

    monkeypatch.setenv("MEETING_NOTES_TOKEN", "t0k")
    with TestClient(create_app(data_root=str(tmp_path / "d"))) as c:
        r = c.get("/recorders", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"
        page = c.get("/recorders", headers={"Authorization": "Bearer t0k"})
        assert page.status_code == 200 and "<h1>Recorders</h1>" in page.text
