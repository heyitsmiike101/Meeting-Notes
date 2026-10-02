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
    # the tab bar keeps every item on one row: 4 tabs (Install lives under Settings)
    assert tabbar.count("<a href=") == 4
    # other pages list it too, without marking it current
    home = web.render_home_page(token_configured=True)
    assert '<a href="/recorders">' in home


def test_recorders_page_structure_and_empty_state():
    page = _page(appearance="dark")
    assert '<html lang="en" data-theme="dark">' in page
    assert "<title>Meeting Notes | Recorders</title>" in page
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
    # the mute button is stretched to the level box beside it, and both are 40px on a phone
    assert ".rec-mute.btn { width:116px; height:auto; min-height:32px" in css
    assert "align-items:stretch" in block[block.index(".rec-track {"):][:200]
    phone = css[css.index("@media (max-width:860px)"): css.index("@media (prefers-reduced-motion")]
    assert ".rec-mute.btn { width:120px; min-height:40px; }" in phone and ".rec-meter { min-height:40px; }" in phone


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
        assert re.fullmatch(r"REC_CARD_HTML|REC_ROW_HTML|icon\([^)]*\)", expr.strip()), expr
    # the templates themselves never interpolate a variable holding item data
    template = re.search(r"var REC_CARD_HTML =(.*?);\n\nfunction recMakeCard", web._RECORDERS_JS, flags=re.S).group(1)
    assert "escapeHtml" not in template and "item" not in template and "it." not in template
    row_template = re.search(r"var REC_ROW_HTML =(.*?);\s*function recRowMake", web._RECORDERS_JS, flags=re.S).group(1)
    assert "escapeHtml" not in row_template and "row." not in row_template and "P." not in row_template


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
    recording: recStatus(entry, now), recordingClock: recClock(recElapsed(entry, now)), idle: recStatus(idle, now), finishing: recStatus(fin, now),
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
    assert out["recording"] == {"cls": "live", "label": "Recording"}  # the clock has its own readout
    assert out["recordingClock"] == "00:42:15"
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


# -- recordings panel --------------------------------------------------------


def test_recordings_panel_markup_and_card_button():
    page = _page()
    assert '<dialog class="dialog rec-panel" id="rec-panel" aria-labelledby="rp-title">' in page
    for needle in ('id="rp-title"', 'id="rp-summary" role="status" aria-live="polite"', 'id="rp-search"', 'id="rp-filter"',
                   'id="rp-refresh"', 'id="rp-all"', 'Select all visible', 'role="list"', 'id="rp-bulk"', 'id="rp-retry"',
                   "Delete from this computer", "All statuses", "Uploading or waiting", "Failed or invalid", 'aria-label="Close recordings"'):
        assert needle in page, needle
    assert 'id="rp-list" role="list"' in page and 'id="rp-body" aria-busy="true"' in page
    # the confirm dialog gained the optional red warning line, and the card template the Recordings button
    assert '<p class="dialog-warning" role="alert" hidden></p>' in web._CONFIRM_DIALOG_HTML
    assert 'data-act="recordings"' in web._RECORDERS_JS and "<span>Recordings</span>" in web._RECORDERS_JS


def test_recordings_panel_css_has_warn_badge_phone_rules_and_tokens_only():
    css = stylesheet_text()
    start = css.index("/* ---- Recorders")
    block = css[start: css.index("@media (max-width:1100px)", start)]
    for rule in (".badge.warn", ".badge.warn .dot", ".rec-panel", ".rp-row", ".rp-bulk", ".rp-state", ".rp-status .badge"):
        assert rule in block, rule
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", block) and "box-shadow" not in block
    assert "uppercase" not in block and "letter-spacing" not in block
    assert "var(--warning-fg)" in block
    phone = css[css.index("@media (max-width:860px)"): css.index("@media (prefers-reduced-motion")]
    assert ".rec-panel { width:100vw" in phone and ".rp-row { grid-template-columns:44px" in phone
    assert "min-height:44px" in phone[phone.index(".rp-actions .btn"):][:120]
    assert ".dialog-warning" in css and "var(--danger-fg)" in css[css.index(".dialog-warning"):][:400]
    assert ".dialog-warning:empty { display:none; }" in css


def test_recordings_panel_js_wiring():
    js = web._RECORDERS_JS
    assert "/recordings'" in js and "'/recordings/' + kind" in js
    assert "'reupload'" in js and "'delete'" in js
    assert "The server has no copy. This permanently removes the only copy (it goes to this computer" in js
    assert "Recorder went offline. Close this panel or wait for it to reconnect." in js
    assert "Remote control is turned off on this computer." in js
    assert "'/sessions/'" in js and "recSafeMeetingUrl" in js
    assert "Delete from this computer?" in js and "They stay on the server. This only frees space on this computer" in js
    assert "setTimeout(recPanelLoad, 4000)" in js  # auto-refresh only while something is in flight
    assert "recPanelOnFrame();" in js  # follows the recorder going away and coming back
    assert "warning: risky" in js
    # confirmDialog supports `warning`, in the shared helpers and the dialog markup
    helpers = web._JS_HELPERS_SRC
    assert "o.warning" in helpers and ".dialog-warning" in helpers
    assert 'class="dialog-warning"' in web._CONFIRM_DIALOG_HTML
    # server values only reach the markup through textContent: no innerHTML write with row data
    assert ".innerHTML = row" not in js and ".innerHTML = P" not in js


RP_CHECKS = r"""
(function () {
  function row(o) {
    return Object.assign({session_id: 'x', name: 'Weekly sync', started: 1759000000, duration_sec: 600,
      size_bytes: 1048576, valid: true, reason: null, active: false, queue: {state: 'not_queued'}, status: 'uploaded_ready',
      label: 'Uploaded · transcript ready', tone: 'ok', detail: null, server_has_copy: true, meeting_url: '/sessions/abc'}, o);
  }
  var rows = [
    row({name: 'Budget review', session_id: 'a'}),
    row({name: 'Board prep', status: 'not_on_server', label: 'Not on server', tone: 'warn', server_has_copy: false, meeting_url: null, session_id: 'b'}),
    row({name: 'Standup', status: 'in_trash', label: 'In server trash', tone: 'warn', server_has_copy: false, session_id: 'c'}),
    row({name: 'Client call', status: 'failed', label: 'Upload failed: timeout', tone: 'error', server_has_copy: false, session_id: 'd'}),
    row({name: 'Broken', status: 'invalid', label: "Can't upload: track mic.wav is empty", tone: 'error', valid: false, reason: 'track mic.wav is empty', server_has_copy: false, session_id: 'e'}),
    row({name: 'Live one', status: 'recording', label: 'Recording now', tone: 'info', active: true, server_has_copy: false, session_id: 'f'}),
    row({name: 'Sending', status: 'uploading', label: 'Uploading 40%', tone: 'info', server_has_copy: false, session_id: 'g'}),
    row({name: 'Queued', status: 'waiting', label: 'Waiting to upload', tone: 'info', server_has_copy: false, session_id: 'h'})
  ];
  var by = {uploaded_ready: 1, not_on_server: 1, in_trash: 1, failed: 1, invalid: 1, recording: 1, uploading: 1, waiting: 1};
  var ids = function (list) { return list.map(function (r) { return r.session_id; }).join(''); };
  var urls = ['/sessions/abc', '/sessions/a%20b', 'https://evil.test/sessions/x', '/sessions/../x', '/sessions/x"onmouseover="1', null, '//evil/sessions/x'];
  return {
    summary: recRowsSummary({total: 8, by_status: by}, 8),
    summaryClean: recRowsSummary({total: 1, by_status: {uploaded_ready: 1}}, 1),
    summaryEmpty: recRowsSummary({total: 0, by_status: {}}, 0),
    summaryPartial: recRowsSummary({total: 3, by_status: {partial: 1, not_on_server: 1, uploaded_ready: 1}}, 3),
    filterAll: ids(recFilterRows(rows, '', 'all')),
    filterUploaded: ids(recFilterRows(rows, '', 'uploaded')),
    filterMoving: ids(recFilterRows(rows, '', 'moving')),
    filterMissing: ids(recFilterRows(rows, '', 'missing')),
    filterFailed: ids(recFilterRows(rows, '', 'failed')),
    searchName: ids(recFilterRows(rows, 'BOARD', 'all')),
    searchTwoWords: ids(recFilterRows(rows, 'client timeout', 'all')),
    searchAndFilter: ids(recFilterRows(rows, 'budget', 'failed')),
    searchNone: recFilterRows(rows, 'zzz', 'all').length,
    searchDate: recFilterRows(rows, fmtDate(1759000000).split(',')[0], 'all').length,
    warnNone: recDeleteNeedsWarning([rows[0]]),
    warnSome: recDeleteNeedsWarning([rows[0], rows[1]]),
    warnEmpty: recDeleteNeedsWarning([]),
    dialogSafe: recDeleteDialog([rows[0]], 'Recycle Bin', 'PC'),
    dialogRisky: recDeleteDialog([rows[0], rows[1], rows[3]], 'Trash', 'Mac'),
    reupload: rows.map(function (r) { return recCanReupload(r).ok; }),
    del: rows.map(function (r) { return recCanDelete(r).ok; }),
    invalidWhy: recCanReupload(rows[4]).why,
    tones: ['ok', 'info', 'warn', 'error', 'muted', 'weird'].map(recToneClass),
    urls: urls.map(recSafeMeetingUrl),
    problems: [recProblem(200, {ok: true}), recProblem(404, {}), recProblem(409, {}), recProblem(504, {}), recProblem(429, {}),
               recProblem(200, {ok: false, code: 'remote_control_disabled', error: 'x'}), recProblem(200, {ok: false, error: 'Nope'}), recProblem(500, {detail: 'boom'})],
    msgReupload: recActionMessage('reupload', {queued: 2, results: [{session_id: 'a', ok: true}, {session_id: 'b', ok: true}]}, {}, 'PC'),
    msgRefused: recActionMessage('delete', {deleted: 1, results: [{session_id: 'a', ok: true}, {session_id: 'g', ok: false, code: 'uploading', error: 'This recording is uploading right now.'}, {session_id: 'h', ok: false, error: 'x'}]}, {g: 'Sending'}, 'PC'),
    msgAllRefused: recActionMessage('reupload', {queued: 0, results: [{session_id: 'f', ok: false, error: 'Still recording'}]}, {f: 'Live one'}, 'PC'),
    inflight: rows.map(recInFlight)
  };
})()
"""


@needs_node
def test_recordings_panel_helpers_in_node(tmp_path):
    script = tmp_path / "page.js"
    script.write_text(web._JS_HELPERS + web._RECORDERS_JS, encoding="utf-8")
    checks = tmp_path / "checks.js"
    checks.write_text(RP_CHECKS, encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    done = subprocess.run([NODE, str(harness), str(script), str(checks)], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)

    # summary: only the groups that need a look
    assert out["summary"] == "8 recordings · 1 not on server · 1 in server trash · 2 failed · 2 uploading"
    assert out["summaryClean"] == "1 recording"
    assert out["summaryEmpty"] == "0 recordings"
    assert out["summaryPartial"] == "3 recordings · 2 not on server"
    # filters by status group, then by search text (name, label, date); all words must match
    assert out["filterAll"] == "abcdefgh"
    assert out["filterUploaded"] == "a"
    assert out["filterMoving"] == "gh"
    assert out["filterMissing"] == "bc"
    assert out["filterFailed"] == "de"
    assert out["searchName"] == "b"
    assert out["searchTwoWords"] == "d"
    assert out["searchAndFilter"] == ""
    assert out["searchNone"] == 0 and out["searchDate"] == 8
    # the red warning is needed iff some selected row has no server copy
    assert out["warnNone"] is False and out["warnSome"] is True and out["warnEmpty"] is False
    safe, risky = out["dialogSafe"], out["dialogRisky"]
    assert safe["warning"] == "" and safe["danger"] is True and safe["title"] == "Delete from this computer?"
    assert safe["lead"] == "They stay on the server. This only frees space on this computer (it goes to this computer's Recycle Bin)."
    assert risky["warning"] == "The server has no copy. This permanently removes the only copy (it goes to this computer's Trash)."
    assert [i["name"] for i in risky["items"]] == ["Board prep", "Client call", "Budget review"]  # rows lacking a copy first
    assert [i["meta"] for i in risky["items"]][:2] == ["Not on server", "Not on server"]
    assert risky["confirmLabel"] == "Delete" and "2 of them are not on the server" in risky["lead"]
    # a..h: uploaded, not on server, in trash, failed, invalid, recording, uploading, waiting
    assert out["reupload"] == [True, True, True, True, False, False, False, True]
    assert out["del"] == [True, True, True, True, True, False, False, True]
    assert out["invalidWhy"] == "Cannot upload: track mic.wav is empty"
    assert out["tones"] == ["done", "running", "warn", "error", "none", "none"]
    assert out["urls"] == ["/sessions/abc", "/sessions/a%20b", None, None, None, None, None]
    probs = out["problems"]
    assert probs[0] is None
    assert probs[1]["offline"] is True and probs[2]["offline"] is True
    assert probs[1]["text"] == "Recorder went offline. Close this panel or wait for it to reconnect."
    assert "did not answer" in probs[3]["text"] and "busy" in probs[4]["text"]
    assert probs[5] == {"text": "Remote control is turned off on this computer.", "locked": True}
    assert probs[6]["text"] == "Nope" and probs[7]["text"] == "boom"
    assert out["msgReupload"]["text"] == "2 recordings queued for upload on PC."
    assert out["msgRefused"] == {
        "text": "Deleted 1 recording from PC. Could not delete Sending: This recording is uploading right now. (1 more could not be deleted.)",
        "error": False, "refused": 2,
    }
    assert out["msgAllRefused"]["error"] is True and "Could not re-upload Live one: Still recording." in out["msgAllRefused"]["text"]
    assert out["inflight"] == [False, False, False, False, False, False, True, True]


def test_recorders_route_needs_web_auth_and_renders(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from meeting_notes.server.app import create_app

    monkeypatch.setenv("MEETING_NOTES_TOKEN", "t0k")
    with TestClient(create_app(data_root=str(tmp_path / "d"))) as c:
        r = c.get("/recorders", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"
        page = c.get("/recorders", headers={"Authorization": "Bearer t0k"})
        assert page.status_code == 200 and "<h1>Recorders</h1>" in page.text


# -- idle level preview (0.7.7+) ----------------------------------------------------------------------


def test_preview_markup_css_and_wiring():
    js = web._RECORDERS_JS
    assert 'class="rec-meter-hint" data-r="meterHint" hidden' in js          # the one-line note under the bars
    # the page tells the server whether it is visible, on open, on a visibility change and as a heartbeat
    assert "type: 'watch', visible: !document.hidden" in js
    assert "ws.onopen = recSendWatch" in js and "addEventListener('visibilitychange', recSendWatch)" in js
    assert "setInterval(recSendWatch, 10000)" in js
    # idle levels arrive as compact frames and only repaint the bars (no full re-render per frame)
    assert "msg.type === 'levels'" in js and "recApplyLevels(msg)" in js
    css = stylesheet_text()
    block = css[css.index("/* ---- Recorders"): css.index("@media (max-width:1100px)", css.index("/* ---- Recorders"))]
    assert ".rec-track.preview .rec-fill" in block and ".rec-meter-hint" in block
    assert "transition-duration:.2s" in block[block.index(".rec-track.preview .rec-fill"):][:120]
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", block)


PREVIEW_CHECKS = r"""
(function () {
  function st(over) { return recState({state: over}); }
  var tracks = {mic: {connected: true, muted: false, level: 0.25, peak: 0.25}, system: {connected: true, muted: false, level: 0.5, peak: 0.5}};
  var supported = {supported: true, active: true, tracks: ['mic', 'system']};
  var macLike = {supported: true, active: true, tracks: ['mic']};
  var idle = function (pv, tr) { return st({status: 'idle', tracks: tr || tracks, preview: pv}); };
  // applying idle level frames to the stored state, without a card on the page
  var id = 'a'.repeat(32);
  recs.set(id, {item: {instance_id: id, state: {status: 'idle', tracks: {mic: {connected: true, level: 0}}}}, at: 5});
  recApplyLevels({type: 'levels', instance_id: id, tracks: {mic: 0.4, system: 0.2, junk: 'x'}});
  var afterIdle = JSON.parse(JSON.stringify(recs.get(id).item.state.tracks));
  recs.get(id).item.state.status = 'recording';
  recApplyLevels({type: 'levels', instance_id: id, tracks: {mic: 0.9}});     // a late idle frame is ignored while recording
  var afterRecording = recs.get(id).item.state.tracks.mic.level;
  recApplyLevels({type: 'levels', instance_id: 'b'.repeat(32), tracks: {mic: 1}});   // unknown recorder: harmless
  recApplyLevels({type: 'levels', instance_id: id, tracks: null});
  return {
    modes: {
      recording: [recMeterMode(st({status: 'recording', tracks: tracks}), 'mic'), recMeterMode(st({status: 'recording', tracks: {mic: {connected: true, muted: true}}}), 'mic')],
      preview: [recMeterMode(idle(supported), 'mic'), recMeterMode(idle(supported), 'system')],
      unsupported: [recMeterMode(idle({}), 'mic'), recMeterMode(idle({supported: false}), 'mic'), recMeterMode(idle(undefined), 'system')],
      mac: [recMeterMode(idle(macLike), 'mic'), recMeterMode(idle(macLike), 'system')],
      disconnected: recMeterMode(idle(supported, {mic: {connected: false, level: 0.3}}), 'mic'),
      finishing: recMeterMode(st({status: 'finishing', tracks: tracks, preview: supported}), 'mic')
    },
    hints: {
      preview: recMeterHint(idle(supported)),
      unsupported: recMeterHint(idle({})),
      mac: recMeterHint(idle(macLike)),
      recording: recMeterHint(st({status: 'recording', preview: supported})),
      finishing: recMeterHint(st({status: 'finishing'}))
    },
    state: [recState(null).preview, recState({state: {preview: supported}}).preview],
    afterIdle: afterIdle, afterRecording: afterRecording,
    levelsAt: recs.get(id).at
  };
})()
"""


@needs_node
def test_preview_helpers_in_node(tmp_path):
    script = tmp_path / "page.js"
    script.write_text(web._JS_HELPERS + web._RECORDERS_JS, encoding="utf-8")
    checks = tmp_path / "checks.js"
    checks.write_text(PREVIEW_CHECKS, encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    done = subprocess.run([NODE, str(harness), str(script), str(checks)], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    modes = out["modes"]
    assert modes["recording"] == ["live", "off"]                 # a muted track keeps its recording look
    assert modes["preview"] == ["preview", "preview"]
    assert modes["unsupported"] == ["off", "off", "off"]         # old recorder / setting off: empty bars, no fake motion
    assert modes["mac"] == ["preview", "off"]                    # macOS: microphone only
    assert modes["disconnected"] == "off" and modes["finishing"] == "off"
    hints = out["hints"]
    assert hints["preview"] == "Preview \u00b7 not recording"
    assert hints["unsupported"] == "Levels show while recording"
    assert hints["mac"] == "Preview \u00b7 not recording. Meeting audio shows while recording."
    assert hints["recording"] == "" and hints["finishing"] == ""
    assert out["state"][0] == {} and out["state"][1]["supported"] is True
    assert out["afterIdle"]["mic"]["level"] == 0.4 and out["afterIdle"]["mic"]["peak"] == 0.4
    assert out["afterIdle"]["system"]["level"] == 0.2 and "junk" not in out["afterIdle"]
    assert out["afterRecording"] == 0.4                          # unchanged by the late frame
    assert out["levelsAt"] == 5                                  # the recording clock's anchor is untouched


# -- the client's recording window, mirrored ---------------------------------------------------------


def test_recorder_card_follows_the_clients_recording_window_order():
    html = web._RECORDERS_JS[web._RECORDERS_JS.index("var REC_CARD_HTML"): web._RECORDERS_JS.index("function recMakeCard")]
    order = ['data-r="clock"', 'data-r="devMic"', 'data-r="startName"', 'data-act="start"', 'data-act="stop"',
             'class="rec-meters"', 'data-act="mute"', '<p class="rec-section">Live preview', 'data-r="preview"', 'class="rec-uploads"']
    positions = [html.index(needle) for needle in order]
    assert positions == sorted(positions), order
    assert "Start recording" in html and "Stop recording" in html
    # the transcript is built from DOM nodes, never innerHTML
    assert "liveFillLines(r.preview, recLiveFor(it)" in web._RECORDERS_JS
    assert "/v1/live" in web._RECORDERS_JS and "recLoadLive" in web._RECORDERS_JS


LIVE_CHECKS = r"""
(function () {
  var late = {session_id: 'a', device: 'PC', partials: [
    {track: 'system', start: 12, text: 'Them later'}, {track: 'mic', start: 3, text: 'You first <b>x</b>'},
    {track: 'system', start: 5, text: 'Them second'}]};
  recLive = [late, {session_id: 'b', device: 'Mac', partials: []}, {session_id: 'c', device: 'Mac', partials: []}];
  function item(over, device) { return {device: device || 'PC', state: Object.assign({status: 'recording', meeting: {session_id: null}}, over)}; }
  return {
    order: livePartials(late).map(function (p) { return p.text; }),
    html: liveLinesHtml(late), last: liveLinesHtml(late, 1), empty: liveLinesHtml({}),
    bySession: (recLiveFor(item({meeting: {session_id: 'b'}}, 'x')) || {}).session_id,
    byDevice: (recLiveFor(item({})) || {}).session_id,
    ambiguous: recLiveFor(item({}, 'Mac')),
    unknown: recLiveFor(item({meeting: {session_id: 'zzz'}})),
    idle: recLiveFor({device: 'PC', state: {status: 'idle'}}),
    line: [recStatusLine(recState({state: {status: 'recording', stream: 'connected', uploads: {pending: 2}}})),
           recStatusLine(recState({state: {status: 'recording', stream: 'disconnected'}})),
           recStatusLine(recState({state: {status: 'idle'}})),
           recStatusLine(recState({state: {status: 'recording', stream: 'Server rejected the token'}}))]
  };
})()
"""


@needs_node
def test_live_preview_helpers_in_node(tmp_path):
    script = tmp_path / "page.js"
    script.write_text(web._JS_HELPERS + web._RECORDERS_JS, encoding="utf-8")
    checks = tmp_path / "checks.js"
    checks.write_text(LIVE_CHECKS, encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    done = subprocess.run([NODE, str(harness), str(script), str(checks)], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    # meeting-time order, not arrival order (one track's lines can land 30-40 s late)
    assert out["order"] == ["You first <b>x</b>", "Them second", "Them later"]
    # the client's plain "You: ..." / "Them: ..." lines, text escaped
    assert out["html"].startswith('<p class="lp-line you"><span class="lp-who">You:</span> You first &lt;b&gt;x&lt;/b&gt;</p>')
    assert '<span class="lp-who">Them:</span> Them later' in out["html"] and "<b>" not in out["html"]
    assert out["last"].count("<p ") == 1 and "Them later" in out["last"]
    assert out["empty"] == ""
    # a recorder is matched to its live meeting by session id, else by computer name when that is unambiguous
    assert out["bySession"] == "b" and out["byDevice"] == "a"
    assert out["ambiguous"] is None and out["unknown"] is None and out["idle"] is None
    assert out["line"] == [
        "Recording. live preview connected  |  2 uploads pending",
        "Recording. server unreachable; recording locally and will upload later",
        "Ready.",
        "Recording. Server rejected the token",
    ]


def test_live_transcript_views_use_the_clients_plain_style():
    home = web.render_home_page(token_configured=False)
    assert "function livePartials" in home and "a.start || 0) - (b.start || 0)" in home  # meeting-time sort kept
    assert home.count("function livePartials") == 1
    assert "liveLinesHtml(item, limit)" in home and "A rough live transcript appears here" in home
    assert "speakerRow({mic: mic" not in home  # no avatars or times in the live views
    assert 'class="live-section">Live preview<' in home and 'id="live-transcript-content" class="live-preview"' in home
    # the overlay keeps the reader's place: follows new lines only when already at the bottom
    assert "wasAtBottom ? scroll.scrollHeight : oldTop" in home
