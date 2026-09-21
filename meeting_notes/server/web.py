"""HTML rendering for the browser-facing side of the server.

No template engine (Jinja2 is deliberately not a dependency here -- see
``ARCHITECTURE.md``): every page is built by small Python functions that
return strings, escaping anything server-rendered with ``html.escape``. The
session list and transcript pages are thin shells around inline vanilla JS
that fetches the JSON API (``/v1/sessions``, ``/v1/sessions/{id}``) and
renders client-side -- see those functions' docstrings for why: the JSON API
is what stays correct as the number of sessions grows, so the HTML side has
to defer to it rather than re-deriving its own (unpaginated, un-indexed) view
of the same data.

``app.py`` only calls the ``render_*`` functions and wires them to routes; it
never builds HTML itself, so every string of markup lives in exactly one
place.
"""

from __future__ import annotations

import html
import json
from typing import Optional

from meeting_notes import __version__

# -- shared shell -------------------------------------------------------

_STYLE = """
:root {
  color-scheme: dark;
  --bg: #14161a;
  --panel: #1c1f26;
  --panel-2: #22262f;
  --border: #2c313c;
  --text: #e6e9ef;
  --text-dim: #98a0b3;
  --accent: #6ea8fe;
  --accent-dim: #3a6bb0;
  --good: #4fd18b;
  --bad: #f2777a;
  --warn: #e7c66b;
  --mic: #6ea8fe;
  --system: #d59bf6;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font: 15px/1.5 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
}
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
.wrap { width: 100%; max-width: 1400px; margin: 0 auto; padding: 16px 24px; min-width: 0; }
nav.top {
  display: flex; align-items: center; gap: 16px;
  padding: 12px 16px; border-bottom: 1px solid var(--border);
  background: var(--panel); flex-wrap: wrap;
}
nav.top .brand { font-weight: 600; margin-right: auto; }
nav.top form { margin: 0; }
.banner {
  background: #4a3a17; color: var(--warn); border: 1px solid #6b5423;
  padding: 8px 12px; border-radius: 6px; margin: 12px 0; font-size: 13px;
}
.card {
  background: var(--panel); border: 1px solid var(--border); border-radius: 8px;
  padding: 16px; margin-bottom: 16px;
}
h1 { font-size: 20px; margin: 0 0 12px; }
h2 { font-size: 16px; margin: 0 0 8px; }
.controls { display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; }
input[type=text], input[type=password], input[type=number], select {
  background: var(--panel-2); color: var(--text); border: 1px solid var(--border);
  border-radius: 6px; padding: 8px 10px; font-size: 14px;
}
input[type=text].search { flex: 1; min-width: 160px; }
label.field { display: block; margin-bottom: 14px; }
label.field span.name { display: block; margin-bottom: 4px; color: var(--text-dim); font-size: 13px; }
label.checkbox { display: flex; align-items: center; gap: 8px; margin-bottom: 14px; cursor: pointer; }
button, .btn {
  background: var(--accent-dim); color: #fff; border: none; border-radius: 6px;
  padding: 8px 14px; font-size: 14px; cursor: pointer;
}
button:hover, .btn:hover { background: var(--accent); text-decoration: none; }
button.secondary, .btn.secondary { background: var(--panel-2); border: 1px solid var(--border); }
button.danger, .btn.danger { background: #5a2a2c; }
button.danger:hover, .btn.danger:hover { background: #7a3639; }
.row-list { display: flex; flex-direction: column; gap: 8px; }
.session-row {
  display: flex; justify-content: space-between; align-items: center; gap: 12px;
  padding: 10px 12px; border: 1px solid var(--border); border-radius: 6px;
  background: var(--panel-2); flex-wrap: wrap;
}
.session-row .name { font-weight: 600; }
.session-row .meta { color: var(--text-dim); font-size: 13px; }
.badge {
  display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 12px;
  border: 1px solid var(--border);
}
.badge.done { color: var(--good); border-color: var(--good); }
.badge.error { color: var(--bad); border-color: var(--bad); }
.badge.running, .badge.queued { color: var(--warn); border-color: var(--warn); }
.badge.audio { color: var(--text-dim); }
.segment { margin-bottom: 10px; }
.segment .head { font-size: 13px; margin-bottom: 2px; }
.segment .ts { color: var(--text-dim); margin-right: 6px; }
.segment .label.track-mic { color: var(--mic); font-weight: 600; }
.segment .label.track-system { color: var(--system); font-weight: 600; }
.segment .label { font-weight: 600; }
.segment .text.approximate { opacity: 0.85; font-style: italic; }
.gap-marker { color: var(--text-dim); font-style: italic; margin: 10px 0; }
.actions { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 12px; }
.error-text { color: var(--bad); font-size: 13px; }
.help { color: var(--text-dim); font-size: 13px; margin-top: -8px; margin-bottom: 14px; }
footer.pager { display: flex; justify-content: center; margin-top: 12px; }
.empty { color: var(--text-dim); padding: 24px; text-align: center; }
.app-shell { min-height: 100vh; display: flex; }
.sidebar {
  position: fixed; inset: 0 auto 0 0; width: 232px; padding: 22px 14px;
  background: #101218; border-right: 1px solid var(--border); display: flex;
  flex-direction: column; z-index: 20;
}
.sidebar .brand { font-size: 18px; font-weight: 700; padding: 0 12px 22px; }
.sidebar a.nav-item, .sidebar button.nav-item {
  display: block; width: 100%; padding: 10px 12px; margin: 2px 0; border-radius: 7px;
  color: var(--text-dim); background: transparent; border: 0; text-align: left;
}
.sidebar a.nav-item:hover, .sidebar a.nav-item.active { color: var(--text); background: var(--panel-2); text-decoration: none; }
.sidebar-bottom { margin-top: auto; }
.app-version { color:var(--text-dim); font-size:11px; padding:10px 12px 0; opacity:.75; }
.notes-copy { white-space:pre-wrap; }
.main { margin-left: 232px; width: calc(100% - 232px); min-width: 0; min-height: 100vh; }
.page-head { display:flex; justify-content:space-between; gap:16px; align-items:end; margin-bottom:18px; }
.page-head h1 { font-size:28px; margin:0; }
.eyebrow { color:var(--text-dim); text-transform:uppercase; letter-spacing:.08em; font-size:11px; }
.stat-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px; margin-bottom:18px; }
.stat { background:var(--panel); border:1px solid var(--border); border-radius:9px; padding:16px; }
.stat .value { font-size:26px; font-weight:700; }
.live-dot { display:inline-block; width:8px; height:8px; border-radius:50%; background:var(--bad); margin-right:7px; box-shadow:0 0 0 4px rgba(242,119,122,.12); }
.table-wrap { overflow:auto; border:1px solid var(--border); border-radius:9px; background:var(--panel); }
table { border-collapse:collapse; width:100%; }
th, td { padding:12px 14px; text-align:left; border-bottom:1px solid var(--border); white-space:normal; overflow-wrap:anywhere; }
th { color:var(--text-dim); font-size:12px; font-weight:600; text-transform:uppercase; letter-spacing:.04em; }
tbody tr { cursor:pointer; }
tbody tr:hover { background:var(--panel-2); }
.overlay { position:fixed; inset:0; background:rgba(8,10,13,.96); z-index:100; display:none; overflow:auto; }
.overlay.open { display:block; }
.overlay-inner { width:100%; max-width:1400px; margin:0 auto; min-height:100vh; padding:24px; }
.overlay-head { display:flex; align-items:center; gap:12px; margin-bottom:18px; }
.overlay-head .title { flex:1; }
.live-card { width:100%; text-align:left; color:var(--text); cursor:pointer; }
.live-card:hover, .live-card:focus-visible { border-color:var(--accent); outline:2px solid var(--accent-dim); outline-offset:2px; }
.live-card .open-hint { color:var(--accent); font-size:13px; float:right; }
.live-transcript-scroll { max-height:calc(100vh - 190px); overflow-y:auto; padding:18px; background:var(--panel); border:1px solid var(--border); border-radius:9px; }
.live-overlay { background:rgba(8,10,13,.98); }
.live-overlay .overlay-inner { max-width:1400px; }
body.overlay-open { overflow:hidden; }
.audio-grid { display:grid; grid-template-columns:1fr 1fr; gap:12px; margin:16px 0; }
.audio-card { background:var(--panel); border:1px solid var(--border); border-radius:8px; padding:12px; }
audio { width:100%; margin-top:8px; }
.upload-card { background:linear-gradient(135deg,#1d2a3e 0%,var(--panel) 58%); border:1px solid #385477; border-radius:12px; padding:22px; margin-bottom:22px; }
.upload-card h2 { font-size:18px; margin-bottom:4px; }
.upload-form { display:grid; grid-template-columns:minmax(0,1fr) 180px auto; gap:10px; align-items:end; margin-top:16px; }
.upload-form input[type=file] { width:100%; padding:10px; color:var(--text); background:var(--panel-2); border:1px dashed #5e759b; border-radius:7px; }
.upload-form input[type=file]::file-selector-button { background:var(--accent-dim); color:#fff; border:0; border-radius:5px; padding:7px 10px; margin-right:8px; cursor:pointer; }
.upload-status { margin-top:12px; min-height:20px; color:var(--text-dim); }
.progress-track { height:6px; background:#11151d; border-radius:99px; overflow:hidden; margin-top:8px; }
.progress-track > i { display:block; height:100%; width:0; background:var(--accent); transition:width .2s ease; }
.bulk-actions { display:flex; align-items:center; gap:8px; flex-wrap:wrap; margin:14px 0; padding:10px 12px; background:var(--panel); border:1px solid var(--border); border-radius:9px; }
.bulk-actions .selection-count { color:var(--text-dim); margin-right:auto; }
.bulk-actions button:disabled { opacity:.45; cursor:not-allowed; }
.select-cell { width:42px; text-align:center; }
input[type=checkbox] { accent-color:var(--accent); width:16px; height:16px; }
.checklist { list-style:none; padding:0; margin:16px 0 0; display:grid; gap:8px; }
.checklist li { display:flex; align-items:center; gap:9px; color:var(--text-dim); }
.checklist li::before { content:'○'; color:var(--text-dim); font-size:18px; line-height:1; }
.checklist li.complete { color:var(--good); }
.checklist li.complete::before { content:'✓'; color:var(--good); }
.checklist li.active { color:var(--warn); }
.checklist li.active::before { content:'◌'; color:var(--warn); }
.checklist .detail { margin-left:auto; font-size:12px; }
.markdown-document { white-space:pre-wrap; font:14px/1.65 ui-monospace,SFMono-Regular,Consolas,"Liberation Mono",monospace; background:#10131a; border:1px solid var(--border); border-radius:9px; padding:20px; min-height:220px; color:#dce7f7; overflow:auto; }
.notes-document { max-width:900px; margin:0 auto; }
.notes-section { padding:20px 0; border-bottom:1px solid var(--border); overflow-wrap:anywhere; }
.notes-section:first-child { padding-top:4px; }
.notes-section h2 { color:#f2f5fb; font-size:18px; margin-bottom:10px; }
.notes-section p { margin:0; white-space:pre-wrap; }
.notes-section ul { margin:0; padding-left:22px; }
.notes-empty { color:var(--text-dim); }
.notes-empty-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:0 22px; }
.notes-empty h2 { color:var(--text-dim); font-size:15px; }
.action-list { display:grid; gap:9px; }
.action-item { padding:12px 14px; border:1px solid var(--border); border-radius:8px; background:var(--panel-2); overflow-wrap:anywhere; }
.action-item .chips { display:flex; gap:6px; flex-wrap:wrap; margin-top:8px; }
.action-item .chip { color:var(--text-dim); border:1px solid var(--border); border-radius:999px; padding:2px 8px; font-size:12px; }
.notes-hero { background:linear-gradient(135deg,#24375d,#1b202b 70%); border-color:#3f5e8e; }
.notes-toolbar { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
.install-button { position:fixed; right:22px; bottom:20px; z-index:40; box-shadow:0 8px 28px rgba(0,0,0,.35); }
pre.command { background:#0d0f14; border:1px solid var(--border); border-radius:7px; padding:12px; overflow:auto; color:var(--text); }
@media (max-width:760px) {
  .sidebar { width:72px; padding:14px 8px; }
  .sidebar .brand { font-size:0; padding:6px 8px 18px; }
  .sidebar .brand:after { content:'MN'; font-size:16px; }
  .sidebar a.nav-item { font-size:0; }
  .sidebar a.nav-item:after { content:attr(data-short); font-size:12px; }
  .app-version { padding:8px 4px 0; text-align:center; }
  .main { margin-left:72px; width:calc(100% - 72px); }
  .stat-grid, .audio-grid, .upload-form { grid-template-columns:1fr; }
  .overlay-inner { padding:16px; }
  .overlay-head { align-items:flex-start; flex-wrap:wrap; }
  .overlay-head .title { min-width:calc(100% - 60px); }
  .overlay .actions { flex-direction:column; align-items:stretch; }
  .overlay .actions button { width:100%; }
}
"""


def _shell(title: str, body: str, *, token_configured: bool, active: str = "") -> str:
    def nav_link(href: str, label: str, key: str, short: str) -> str:
        selected = " active" if key == active else ""
        return f'<a class="nav-item{selected}" data-short="{short}" href="{href}">{label}</a>'

    logout = ""
    if token_configured:
        logout = (
            '<form method="post" action="/logout">'
            '<button type="submit" class="nav-item">Log out</button></form>'
        )

    banner = ""
    if not token_configured:
        banner = (
            '<div class="banner">No MEETING_NOTES_TOKEN is configured -- '
            "this server accepts requests from anyone who can reach it. "
            "Fine for a quick local test, not recommended left that way on a "
            "shared network.</div>"
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>{_STYLE}</style>
</head>
<body>
<div class="app-shell">
<aside class="sidebar">
  <div class="brand">Meeting Notes</div>
  {nav_link("/", "Home", "home", "Home")}
  <span aria-label="Sessions">{nav_link("/transcriptions", "Saved transcriptions", "transcriptions", "Saved")}</span>
  <span aria-label="Meeting notes">{nav_link("/meeting-notes", "Meeting notes", "meeting-notes", "Notes")}</span>
  <div class="sidebar-bottom">
    {nav_link("/settings", "Settings", "settings", "Settings")}
    {logout}
    <div class="app-version" aria-label="Meeting Notes version">v{html.escape(__version__)}</div>
  </div>
</aside>
<main class="main"><div class="wrap">
  {banner}
  {body}
</div></main>
<a class="btn install-button" href="/install">Install client agent</a>
</div>
</body>
</html>"""


_JS_HELPERS = """
function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
    return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];
  });
}
function fmtDate(value) {
  if (!value) return "unknown date";
  var numeric = typeof value === "number" || /^[0-9]+([.][0-9]+)?$/.test(String(value));
  var raw = numeric ? Number(value) : value;
  // Server indexes use epoch seconds while session metadata uses ISO-8601.
  // Also tolerate millisecond epochs from future integrations.
  var d = new Date(numeric && raw < 100000000000 ? raw * 1000 : raw);
  return isNaN(d.getTime()) ? "unknown date" : d.toLocaleString();
}
function fmtDuration(seconds) {
  seconds = Math.max(0, Math.round(seconds || 0));
  var h = Math.floor(seconds / 3600), m = Math.floor((seconds % 3600) / 60), s = seconds % 60;
  function two(n) { return (n < 10 ? "0" : "") + n; }
  return h > 0 ? (h + ":" + two(m) + ":" + two(s)) : (m + ":" + two(s));
}
function fmtBytes(bytes) {
  if (!bytes) return "0 B";
  var units = ["B", "KB", "MB", "GB"];
  var i = 0, n = bytes;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return n.toFixed(i === 0 ? 0 : 1) + " " + units[i];
}
function stateBadge(row) {
  var state = row.latest_state;
  if (!state) return '<span class="badge">no job yet</span>';
  if (state === "running" || state === "queued") {
    var pct = row.latest_progress != null ? Math.round(row.latest_progress * 100) + "%" : "";
    return '<span class="badge ' + state + '">' + state + (pct ? " " + pct : "") + '</span>';
  }
  if (state === "error") {
    var msg = row.latest_error ? ": " + escapeHtml(row.latest_error) : "";
    return '<span class="badge error">error' + msg + '</span>';
  }
  return '<span class="badge done">done</span>';
}
function processingState(row) {
  var pipeline = row.pipeline || {}, upload = pipeline.upload || {}, transcription = pipeline.transcription || {};
  var uploadState = String(upload.state || "").toLowerCase(), transcriptionState = String(transcription.state || "").toLowerCase();
  function complete(s) { return s === "complete" || s === "completed" || s === "done" || s === "uploaded" || s === "ready"; }
  if (uploadState.indexOf("error") >= 0 || uploadState.indexOf("fail") >= 0) {
    return {key:"error", label:"Upload failed", pct:null, detail:upload.error || ""};
  }
  if (uploadState && !complete(uploadState)) {
    return {key:uploadState.indexOf("upload") >= 0 ? "uploading" : "upload", label:uploadState.indexOf("upload") >= 0 ? "Uploading" : "Upload pending", pct:upload.percent};
  }
  if (transcriptionState) {
    if (complete(transcriptionState)) return {key:"complete", label:"Complete", pct:100};
    if (transcriptionState.indexOf("error") >= 0 || transcriptionState.indexOf("fail") >= 0) return {key:"error", label:"Needs attention", pct:null};
    if (transcriptionState.indexOf("transcrib") >= 0 || transcriptionState === "running") return {key:"transcribing", label:"Transcribing", pct:transcription.percent};
    return {key:"queued", label:"Queued", pct:transcription.percent};
  }
  var state = row.latest_state;
  if (!row.has_audio) return {key:"upload", label:"Upload pending", pct:null};
  if (state === "running") return {key:"transcribing", label:"Transcribing", pct:row.latest_progress == null ? null : row.latest_progress * 100};
  if (state === "queued") return {key:"queued", label:"Queued", pct:row.latest_progress == null ? null : row.latest_progress * 100};
  if (state === "error") return {key:"error", label:"Needs attention", pct:null};
  if (state === "done") return {key:"complete", label:"Complete", pct:100};
  return {key:"upload", label:"Ready to transcribe", pct:null};
}
function processingBadge(row) {
  var status = processingState(row), rawPct = status.pct, pct = rawPct == null ? "" : " " + Math.round(rawPct) + "%";
  var cls = status.key === "complete" ? "done" : (status.key === "error" ? "error" : (status.key === "transcribing" || status.key === "queued" || status.key === "uploading" ? "running" : ""));
  var errorDetail = status.detail || row.latest_error || "";
  var detail = status.key === "error" && errorDetail ? ": " + escapeHtml(errorDetail) : "";
  return '<span class="badge ' + cls + '">' + escapeHtml(status.label) + pct + detail + '</span>';
}
"""


def render_login_page(error: bool = False) -> str:
    error_html = '<p class="error-text">Invalid token.</p>' if error else ""
    body = f"""
<div class="card" style="max-width:360px;margin:48px auto;">
  <h1>Sign in</h1>
  {error_html}
  <form method="post" action="/login">
    <label class="field">
      <span class="name">Server token</span>
      <input type="password" name="token" autofocus required style="width:100%">
    </label>
    <button type="submit">Sign in</button>
  </form>
</div>
"""
    return _shell("Sign in", body, token_configured=True)


def render_home_page(*, token_configured: bool) -> str:
    body = """
<div class="page-head"><div><div class="eyebrow">Overview</div><h1>Home</h1></div></div>
<section class="upload-card" aria-labelledby="upload-heading">
  <div class="eyebrow">Bring a recording</div>
  <h2 id="upload-heading">Upload a meeting recording</h2>
  <p class="help">Drop in an audio file and Meeting Notes will upload and transcribe it. MP3, WAV, M4A, FLAC, OGG, OPUS, AAC, and WebM are supported.</p>
  <form id="recording-upload" class="upload-form">
    <label class="field" style="margin:0"><span class="name">Recording</span><input id="recording-file" type="file" accept="audio/*,.mp3,.wav,.m4a,.flac,.ogg,.opus,.aac,.webm" required></label>
    <label class="field" style="margin:0"><span class="name">Meeting name <span class="help">(optional)</span></span><input id="recording-name" type="text" maxlength="200" placeholder="e.g. Weekly standup"></label>
    <button type="submit" id="upload-submit">Upload recording</button>
  </form>
  <div class="upload-status" id="upload-status" role="status" aria-live="polite"></div>
  <div class="progress-track" id="upload-progress-track" hidden><i id="upload-progress"></i></div>
</section>
<div class="stat-grid">
  <div class="stat"><div class="eyebrow">Saved meetings</div><div class="value" id="total-count">—</div></div>
  <div class="stat"><div class="eyebrow">Live now</div><div class="value" id="live-count">0</div></div>
  <div class="stat"><div class="eyebrow">With audio</div><div class="value" id="audio-count">—</div></div>
</div>
<section id="live-section" style="display:none">
  <h2><span class="live-dot"></span>Live transcription</h2>
  <div id="live-list" class="row-list"></div>
</section>
<section>
  <div class="page-head"><div><div class="eyebrow">Latest activity</div><h1 style="font-size:20px">Recent transcriptions</h1></div>
    <a href="/transcriptions">View all</a></div>
  <div id="recent-list" class="row-list"><div class="empty">Loading…</div></div>
</section>
<script>
""" + _JS_HELPERS + """
var uploadForm = document.getElementById('recording-upload');
uploadForm.addEventListener('submit', function(event) {
  event.preventDefault();
  var file = document.getElementById('recording-file').files[0];
  var status = document.getElementById('upload-status'), submit = document.getElementById('upload-submit');
  var track = document.getElementById('upload-progress-track'), bar = document.getElementById('upload-progress');
  if (!file) return;
  var xhr = new XMLHttpRequest(), form = new FormData();
  form.append('file', file); form.append('name', document.getElementById('recording-name').value.trim());
  submit.disabled = true; track.hidden = false; bar.style.width = '0%'; status.textContent = 'Uploading ' + file.name + '…';
  xhr.upload.addEventListener('progress', function(e) { if (e.lengthComputable) { var pct = Math.round(e.loaded / e.total * 100); bar.style.width = pct + '%'; status.textContent = 'Uploading… ' + pct + '%'; } });
  xhr.addEventListener('load', function() {
    submit.disabled = false;
    var data = {}; try { data = JSON.parse(xhr.responseText || '{}'); } catch (_) {}
    if (xhr.status < 200 || xhr.status >= 300) { status.textContent = data.detail || 'Upload failed. Please try again.'; return; }
    bar.style.width = '100%'; status.textContent = 'Upload complete. Transcription queued' + (data.session_id ? ' — opening saved transcription…' : '.');
    if (data.session_id) setTimeout(function() { location.href = '/sessions/' + encodeURIComponent(data.session_id); }, 500);
  });
  xhr.addEventListener('error', function() { submit.disabled = false; status.textContent = 'Upload failed. Check the server connection and try again.'; });
  xhr.open('POST', '/v1/uploads'); xhr.withCredentials = true; xhr.send(form);
});
var liveItems = [];
var activeLiveId = null;
var liveOverlayPreviousFocus = null;

function liveText(item) {
  return (item.partials || []).slice(-20).map(function (p) {
    return '<div class="segment"><div class="head"><span class="ts">[' + fmtDuration(p.start) + ']</span><span class="label ' + (p.track === 'mic' ? 'track-mic' : 'track-system') + '">' + (p.track === 'mic' ? 'You' : 'Them') + '</span></div><div>' + escapeHtml(p.text) + '</div></div>';
  }).join('');
}
function liveCard(item) {
  return '<div class="card live-card" role="button" tabindex="0" data-live-id="' + escapeHtml(item.session_id) + '" aria-label="Open live transcript for ' + escapeHtml(item.name) + '"><span class="open-hint">Open transcript →</span><h2>' + escapeHtml(item.name) + '</h2><div class="help">' + escapeHtml(item.device) + ' · started ' + fmtDate(item.started_wall) + '</div>' + (liveText(item) || '<div class="empty">Listening for speech…</div>') + '</div>';
}
function liveItem(id) {
  return liveItems.find(function (item) { return item.session_id === id; });
}
function renderLiveOverlay(item, preservePosition) {
  var scroll = document.getElementById('live-transcript-scroll');
  var wasAtBottom = scroll && (scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 24);
  var oldTop = scroll ? scroll.scrollTop : 0;
  var content = document.getElementById('live-transcript-content');
  if (!content || !item) return;
  content.innerHTML = liveText(item) || '<div class="empty">Listening for speech…</div>';
  if (scroll && preservePosition) {
    scroll.scrollTop = wasAtBottom ? scroll.scrollHeight : oldTop;
  } else if (scroll) {
    scroll.scrollTop = scroll.scrollHeight;
  }
}
function openLive(item) {
  if (!item) return;
  activeLiveId = item.session_id;
  liveOverlayPreviousFocus = document.activeElement;
  var overlay = document.getElementById('live-overlay');
  document.getElementById('live-overlay-title').textContent = item.name || 'Live transcript';
  document.getElementById('live-name').value = item.name || '';
  document.getElementById('live-overlay-meta').textContent = (item.device || 'Unknown device') + ' · started ' + fmtDate(item.started_wall);
  renderLiveOverlay(item, false);
  overlay.classList.add('open');
  overlay.setAttribute('aria-hidden', 'false');
  document.body.classList.add('overlay-open');
  history.pushState({liveTranscript: item.session_id}, '', '#live-' + encodeURIComponent(item.session_id));
  document.getElementById('live-close').focus();
}
function closeLive(fromHistory) {
  var overlay = document.getElementById('live-overlay');
  if (!overlay.classList.contains('open')) return;
  overlay.classList.remove('open');
  overlay.setAttribute('aria-hidden', 'true');
  document.body.classList.remove('overlay-open');
  activeLiveId = null;
  if (!fromHistory && location.hash.indexOf('#live-') === 0) history.back();
  if (liveOverlayPreviousFocus && liveOverlayPreviousFocus.focus) liveOverlayPreviousFocus.focus();
}
function bindLiveCards() {
  document.querySelectorAll('[data-live-id]').forEach(function (card) {
    card.addEventListener('click', function () { openLive(liveItem(card.dataset.liveId)); });
    card.addEventListener('keydown', function (event) {
      if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); openLive(liveItem(card.dataset.liveId)); }
    });
  });
}
function updateLiveOverlay() {
  var item = activeLiveId && liveItem(activeLiveId);
  if (!item) return;
  document.getElementById('live-overlay-title').textContent = item.name || 'Live transcript';
  var nameInput = document.getElementById('live-name');
  if (nameInput && document.activeElement !== nameInput) nameInput.value = item.name || '';
  document.getElementById('live-overlay-meta').textContent = (item.device || 'Unknown device') + ' · started ' + fmtDate(item.started_wall);
  renderLiveOverlay(item, true);
}
document.addEventListener('keydown', function (event) {
  if (event.key === 'Escape') closeLive(false);
});
window.addEventListener('popstate', function () { closeLive(true); });
function loadOverview() {
  fetch('/v1/sessions?per_page=8', {credentials:'same-origin'}).then(r => r.json()).then(data => {
    document.getElementById('total-count').textContent = data.total;
    document.getElementById('audio-count').textContent = data.items.filter(x => x.has_audio).length + (data.total > data.items.length ? '+' : '');
    var recent = document.getElementById('recent-list');
    recent.innerHTML = data.items.length ? data.items.map(row =>
      '<a class="session-row" href="/sessions/' + encodeURIComponent(row.session_id) + '"><div><div class="name">' + escapeHtml(row.name || row.session_id) + '</div><div class="meta">' + fmtDate(row.created) + ' · ' + escapeHtml(row.device || 'Unknown device') + '</div></div><div>' + stateBadge(row) + '</div></a>'
    ).join('') : '<div class="empty">No saved transcriptions yet.</div>';
  });
}
function loadLive() {
  fetch('/v1/live', {credentials:'same-origin'}).then(r => r.json()).then(data => {
    liveItems = data.items || [];
    document.getElementById('live-count').textContent = data.total;
    var section = document.getElementById('live-section');
    section.style.display = data.total ? '' : 'none';
    document.getElementById('live-list').innerHTML = liveItems.map(liveCard).join('');
    bindLiveCards();
    updateLiveOverlay();
  });
}
loadOverview(); loadLive(); setInterval(loadOverview, 10000); setInterval(loadLive, 2500);
</script>
<div class="overlay live-overlay" id="live-overlay" role="dialog" aria-modal="true" aria-hidden="true" aria-labelledby="live-overlay-title">
  <div class="overlay-inner">
    <div class="overlay-head"><button class="secondary" id="live-close" type="button" aria-label="Close live transcript">← Back</button><div class="title"><h1 id="live-overlay-title">Live transcript</h1><div class="help" id="live-overlay-meta"></div></div></div>
    <form class="controls" id="live-name-form"><label class="field" style="flex:1;min-width:220px;margin:0"><span class="name">Meeting name</span><input type="text" id="live-name" maxlength="200" autocomplete="off" required></label><button type="submit">Save name</button><span class="help" id="live-name-status" role="status" style="align-self:end;margin:0"></span></form>
    <div class="live-transcript-scroll" id="live-transcript-scroll" tabindex="0" aria-label="Live transcript text" aria-live="polite"><div id="live-transcript-content"></div></div>
  </div>
</div>
<script>
document.getElementById('live-close').addEventListener('click', function () { closeLive(false); });
document.getElementById('live-overlay').addEventListener('click', function (event) { if (event.target === this) closeLive(false); });
document.getElementById('live-name-form').addEventListener('submit', function (event) {
  event.preventDefault();
  var item = activeLiveId && liveItem(activeLiveId);
  var input = document.getElementById('live-name');
  var status = document.getElementById('live-name-status');
  if (!item || !input.value.trim()) return;
  status.textContent = 'Saving…';
  fetch('/v1/live/' + encodeURIComponent(item.session_id), {method:'PATCH', credentials:'same-origin', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:input.value.trim()})})
    .then(function (response) { return response.json().then(function (data) { if (!response.ok) throw new Error(data.detail || 'Could not save name'); return data; }); })
    .then(function (data) { item.name = data.name; status.textContent = 'Saved'; updateLiveOverlay(); document.getElementById('live-list').innerHTML = liveItems.map(liveCard).join(''); bindLiveCards(); })
    .catch(function (error) { status.textContent = error.message; });
});
</script>
"""
    return _shell("Home", body, token_configured=token_configured, active="home")


# -- sessions list --------------------------------------------------------


def render_sessions_page(*, token_configured: bool) -> str:
    """Thin shell: no session data is rendered server-side at all. Everything
    -- the list, search, state filter, and pagination -- comes from
    ``GET /v1/sessions`` via the inline script below. That's what lets this
    page stay fast and correct as the number of sessions grows into the
    thousands: the page itself never has to know how many sessions exist,
    only how to ask for one page of them.
    """
    body = """
<h1>Sessions</h1>
<div class="controls">
  <input type="text" class="search" id="q" placeholder="Search name or transcript text...">
  <select id="state">
    <option value="">Any state</option>
    <option value="queued">Queued</option>
    <option value="running">Running</option>
    <option value="done">Done</option>
    <option value="error">Error</option>
  </select>
</div>
<div id="list" class="row-list"></div>
<footer class="pager">
  <button id="more" class="secondary" style="display:none">Load more</button>
</footer>
<script>
""" + _JS_HELPERS + """
var state = { q: "", jobState: "", page: 1, perPage: 50, total: 0, loaded: 0 };

function rowHtml(row) {
  var audio = row.has_audio
    ? '<span class="badge audio">audio ' + fmtBytes(row.audio_bytes) + '</span>'
    : '<span class="badge audio">no audio</span>';
  return '<div class="session-row">' +
    '<div>' +
      '<div class="name"><a href="/sessions/' + encodeURIComponent(row.session_id) + '">' +
        escapeHtml(row.name || row.session_id) + '</a></div>' +
      '<div class="meta">' + fmtDate(row.created) +
        (row.duration_sec ? ' &middot; ' + fmtDuration(row.duration_sec) : '') + '</div>' +
    '</div>' +
    '<div>' + stateBadge(row) + ' ' + audio + '</div>' +
  '</div>';
}

function load(reset) {
  if (reset) { state.page = 1; state.loaded = 0; document.getElementById("list").innerHTML = ""; }
  var url = "/v1/sessions?page=" + state.page + "&per_page=" + state.perPage;
  if (state.q) url += "&q=" + encodeURIComponent(state.q);
  if (state.jobState) url += "&state=" + encodeURIComponent(state.jobState);
  fetch(url, { credentials: "same-origin" }).then(function (r) {
    if (r.status === 401 || r.status === 403) { window.location = "/login"; return null; }
    return r.json();
  }).then(function (data) {
    if (!data) return;
    state.total = data.total;
    var list = document.getElementById("list");
    if (data.items.length === 0 && state.loaded === 0) {
      list.innerHTML = '<div class="empty">No sessions yet.</div>';
    } else {
      data.items.forEach(function (row) { list.insertAdjacentHTML("beforeend", rowHtml(row)); });
    }
    state.loaded += data.items.length;
    document.getElementById("more").style.display = state.loaded < state.total ? "" : "none";
  });
}

var qEl = document.getElementById("q");
var debounceTimer = null;
qEl.addEventListener("input", function () {
  state.q = qEl.value;
  clearTimeout(debounceTimer);
  debounceTimer = setTimeout(function () { load(true); }, 300);
});
document.getElementById("state").addEventListener("change", function (e) {
  state.jobState = e.target.value;
  load(true);
});
document.getElementById("more").addEventListener("click", function () {
  state.page += 1;
  load(false);
});
load(true);
</script>
"""
    return _shell("Sessions", body, token_configured=token_configured, active="sessions")


# -- session detail ---------------------------------------------------------


def render_session_detail_page(session_id: str, *, token_configured: bool) -> str:
    """Thin shell around ``GET /v1/sessions/{id}``: meta, jobs and segments
    are all fetched and rendered client-side, same rationale as the sessions
    list. The action buttons (download/delete/retranscribe) are ordinary
    forms posting to the existing HTML routes -- those are one-off actions on
    a single, already-known session id, not a listing, so there's nothing for
    them to gain from going through JS/JSON.
    """
    # session_id only ever reaches here after store_mod.is_safe_id has
    # already validated it (see app.py's route), and that charset excludes
    # quotes/angle brackets -- but escaping on the way into HTML/JS string
    # literals costs nothing and means this doesn't depend on staying that
    # way forever.
    safe_id = html.escape(session_id)
    js_id = json.dumps(session_id)
    body = f"""
<p><a href="/">&larr; All sessions</a></p>
<div class="card">
  <h1 id="title">{safe_id}</h1>
  <div id="meta" class="help">Loading...</div>
</div>
<div class="card" id="transcript-card">
  <h2>Transcript</h2>
  <div id="segments"></div>
</div>
<div class="actions">
  <a class="btn secondary" href="/sessions/{safe_id}/transcript.md">Download .md</a>
  <a class="btn secondary" href="/sessions/{safe_id}/transcript.json">Download .json</a>
  <form method="post" action="/sessions/{safe_id}/retranscribe" id="retranscribe-form" style="display:none">
    <button type="submit" class="secondary">Re-run transcription</button>
  </form>
  <form method="post" action="/sessions/{safe_id}/delete-audio" id="delete-audio-form" style="display:none"
        onsubmit="return confirm('Delete this session\\'s audio? The transcript is kept.');">
    <button type="submit" class="danger">Delete audio</button>
  </form>
  <form method="post" action="/sessions/{safe_id}/delete"
        onsubmit="return confirm('Delete this whole session, including its transcript? This cannot be undone.');">
    <button type="submit" class="danger">Delete session</button>
  </form>
</div>
<script>
""" + _JS_HELPERS + f"""
var sessionId = {js_id};
var pollTimer = null;

function labelClass(track) {{
  return track === "mic" ? "track-mic" : (track === "system" ? "track-system" : "");
}}

function renderSegments(segments) {{
  var el = document.getElementById("segments");
  if (!segments || segments.length === 0) {{
    el.innerHTML = '<div class="empty">No transcript yet.</div>';
    return;
  }}
  var html = "";
  var i = 0;
  while (i < segments.length) {{
    var seg = segments[i];
    if (seg.in_gap) {{
      var j = i;
      while (j < segments.length && segments[j].in_gap) j++;
      var lost = Math.max(0, Math.round(segments[j - 1].end - seg.start));
      html += '<div class="gap-marker">[audio lost for ' + lost + ' seconds]</div>';
      i = j;
      continue;
    }}
    var label = seg.label;
    var j2 = i;
    var texts = [];
    var approx = false;
    while (j2 < segments.length && !segments[j2].in_gap && segments[j2].label === label) {{
      texts.push(segments[j2].text);
      approx = approx || !!segments[j2].approximate;
      j2++;
    }}
    var ts = fmtDuration(seg.start);
    html += '<div class="segment">' +
      '<div class="head"><span class="ts">[' + ts + ']</span>' +
      '<span class="label ' + labelClass(seg.track) + '">' + escapeHtml(label) + '</span></div>' +
      '<div class="text' + (approx ? ' approximate' : '') + '">' +
      escapeHtml(texts.join(" ")) + '</div></div>';
    i = j2;
  }}
  el.innerHTML = html;
}}

function renderMeta(data) {{
  document.getElementById("title").textContent = (data.meta && data.meta.name) || sessionId;
  var bits = [];
  var created = data.meta && (data.meta.created || data.meta.date);
  if (created) bits.push(String(created));
  if (data.meta && data.meta.duration_sec) bits.push(fmtDuration(data.meta.duration_sec));
  var job = (data.jobs && data.jobs[0]) || null;
  if (job) bits.push(stateBadge({{
    latest_state: job.state, latest_progress: job.progress, latest_error: job.error
  }}));
  bits.push(data.has_audio ? ("audio: " + fmtBytes(data.audio_bytes)) : "no audio");
  document.getElementById("meta").innerHTML = bits.join(" &middot; ");

  document.getElementById("retranscribe-form").style.display = data.has_audio ? "" : "none";
  document.getElementById("delete-audio-form").style.display = data.has_audio ? "" : "none";

  renderSegments(data.segments);

  if (job && (job.state === "queued" || job.state === "running")) {{
    if (!pollTimer) pollTimer = setTimeout(load, 3000);
  }} else if (pollTimer) {{
    clearTimeout(pollTimer);
    pollTimer = null;
  }}
}}

function load() {{
  fetch("/v1/sessions/" + encodeURIComponent(sessionId), {{ credentials: "same-origin" }})
    .then(function (r) {{
      if (r.status === 401 || r.status === 403) {{ window.location = "/login"; return null; }}
      if (r.status === 404) {{
        document.getElementById("meta").textContent = "Session not found.";
        return null;
      }}
      return r.json();
    }})
    .then(function (data) {{ if (data) renderMeta(data); }});
}}
load();
</script>
"""
    return _shell(f"Session {session_id}", body, token_configured=token_configured, active="sessions")


# -- saved transcriptions ---------------------------------------------------


def render_transcriptions_page(
    *, token_configured: bool, initial_session_id: Optional[str] = None
) -> str:
    initial = json.dumps(initial_session_id)
    body = """
<div class="page-head"><div><div class="eyebrow">Library</div><h1>Saved transcriptions</h1></div></div>
<div class="controls">
  <input type="text" class="search" id="q" placeholder="Search names and transcript text…">
  <select id="state"><option value="">All states</option><option value="done">Complete</option><option value="running">Running</option><option value="queued">Queued</option><option value="error">Error</option></select>
</div>
<div class="bulk-actions" aria-label="Bulk actions">
  <span class="selection-count" id="selection-count">0 selected</span>
  <button class="secondary" id="bulk-build" disabled>Build Meeting Notes</button>
  <button class="secondary" id="bulk-retranscribe" disabled>Retranscribe</button>
  <button class="danger" id="bulk-delete" disabled>Delete</button>
</div>
<div class="table-wrap"><table>
  <thead><tr><th class="select-cell"><input id="select-all" type="checkbox" aria-label="Select all visible transcriptions"></th><th>Time</th><th>Device</th><th>Name</th><th>Length</th><th>Status</th><th>Audio</th></tr></thead>
  <tbody id="rows"><tr><td colspan="7" class="empty">Loading…</td></tr></tbody>
</table></div>
<footer class="pager"><button id="more" class="secondary" style="display:none">Load more</button></footer>

<div class="overlay" id="detail-overlay" role="dialog" aria-modal="true" aria-label="Meeting transcript">
  <div class="overlay-inner">
    <div class="overlay-head"><button class="secondary" id="close-overlay">← Back</button><div class="title"><div class="eyebrow" id="overlay-meta"></div><h1 id="overlay-title">Meeting</h1></div></div>
    <section class="card" aria-labelledby="transcription-progress-heading"><h2 id="transcription-progress-heading">Processing status</h2><ol class="checklist" id="transcription-checklist"></ol></section>
    <div id="audio-players" class="audio-grid"></div>
    <div class="actions">
      <button class="secondary" id="retranscribe">Retranscribe</button>
      <button class="secondary" id="queue-review">Build Meeting Notes</button>
      <span class="help" id="review-status" role="status"></span>
      <button class="danger" id="delete-audio">Delete audio</button>
      <button class="danger" id="delete-entry">Delete entire entry</button>
    </div>
    <div class="card" style="margin-top:18px"><h2>Transcript</h2><div id="overlay-segments"></div></div>
  </div>
</div>
<script>
""" + _JS_HELPERS + """
var listState = {page:1, perPage:50, loaded:0, total:0};
var currentSession = null;
var detailPollTimer = null;

function tableRow(row) {
  return '<tr data-id="' + escapeHtml(row.session_id) + '"><td class="select-cell"><input class="row-select" type="checkbox" value="' + escapeHtml(row.session_id) + '" aria-label="Select ' + escapeHtml(row.name || row.session_id) + '"></td><td>' + fmtDate(row.created) + '</td><td>' + escapeHtml(row.device || row.platform || 'Unknown') + '</td><td><strong>' + escapeHtml(row.name || row.session_id) + '</strong></td><td>' + fmtDuration(row.duration_sec) + '</td><td>' + processingBadge(row) + '</td><td>' + (row.has_audio ? fmtBytes(row.audio_bytes) : 'Transcript only') + '</td></tr>';
}
function selectedIds() { return Array.from(document.querySelectorAll('.row-select:checked')).map(function(el) { return el.value; }); }
function updateSelection() {
  var ids = selectedIds(), disabled = !ids.length;
  document.getElementById('selection-count').textContent = ids.length + ' selected';
  ['bulk-build','bulk-retranscribe','bulk-delete'].forEach(function(id) { document.getElementById(id).disabled = disabled; });
  var all = document.querySelectorAll('.row-select'); document.getElementById('select-all').checked = !!all.length && ids.length === all.length;
}
function runBulk(path, method, confirmText) {
  var ids = selectedIds(); if (!ids.length) return;
  if (confirmText && !confirm(confirmText)) return;
  var buttons = ['bulk-build','bulk-retranscribe','bulk-delete']; buttons.forEach(function(id) { document.getElementById(id).disabled = true; });
  Promise.all(ids.map(function(id) { return fetch('/v1/sessions/'+encodeURIComponent(id)+path, {method:method, credentials:'same-origin'}).then(function(r) { if (!r.ok) throw new Error('One or more actions failed'); return r; }); }))
    .then(function() { loadRows(true); })
    .catch(function(error) { alert(error.message); updateSelection(); });
}
function loadRows(reset) {
  var preservedSelection = reset ? selectedIds() : [];
  if (reset) { listState.page=1; listState.loaded=0; document.getElementById('rows').innerHTML=''; document.getElementById('select-all').checked=false; }
  var url='/v1/sessions?page='+listState.page+'&per_page='+listState.perPage;
  var q=document.getElementById('q').value.trim(), state=document.getElementById('state').value;
  if(q) url+='&q='+encodeURIComponent(q); if(state) url+='&state='+encodeURIComponent(state);
  fetch(url,{credentials:'same-origin'}).then(r=>r.json()).then(data=>{
    listState.total=data.total;
    var rows=document.getElementById('rows');
    if(!data.items.length && !listState.loaded) rows.innerHTML='<tr><td colspan="7" class="empty">No transcriptions found.</td></tr>';
    else rows.insertAdjacentHTML('beforeend',data.items.map(tableRow).join(''));
    if (preservedSelection.length) document.querySelectorAll('.row-select').forEach(function(box) { box.checked = preservedSelection.indexOf(box.value) >= 0; });
    listState.loaded+=data.items.length;
    document.getElementById('more').style.display=listState.loaded<listState.total?'':'none';
    updateSelection();
  });
}
function renderTranscript(segments) {
  var root=document.getElementById('overlay-segments');
  if(!segments || !segments.length){root.innerHTML='<div class="empty">No final transcript yet.</div>';return;}
  root.innerHTML=segments.map(seg=>seg.in_gap?'<div class="gap-marker">[audio lost]</div>':'<div class="segment"><div class="head"><span class="ts">['+fmtDuration(seg.start)+']</span><span class="label '+(seg.track==='mic'?'track-mic':'track-system')+'">'+escapeHtml(seg.label)+'</span></div><div class="text'+(seg.approximate?' approximate':'')+'">'+escapeHtml(seg.text)+'</div></div>').join('');
}
function renderProcessingChecklist(data) {
  var pipeline=data.pipeline||{}, upload=pipeline.upload||{}, transcription=pipeline.transcription||{}, job=(data.jobs||[])[0]||{};
  var uploadState=String(upload.state||data.upload_state||data.upload_status||(data.has_audio?'complete':'pending')).toLowerCase();
  var transcribeState=String(transcription.state||job.state||'pending').toLowerCase();
  var uploadProgress=upload.percent==null?(data.upload_progress==null?null:data.upload_progress):upload.percent;
  var transcribeProgress=transcription.percent==null?(job.progress==null?null:job.progress*100):transcription.percent;
  function complete(s){return s==='complete'||s==='completed'||s==='done'||s==='uploaded'||s==='ready';}
  var uploadComplete=complete(uploadState)||(!upload.state&&data.has_audio), uploadError=uploadState.indexOf('error')>=0||uploadState.indexOf('fail')>=0, transcribed=complete(transcribeState), transcribeError=transcribeState.indexOf('error')>=0||transcribeState.indexOf('fail')>=0, transcribing=transcribeState.indexOf('transcrib')>=0||transcribeState==='running';
  function percent(value){return value==null?'':Math.round(value)+'%';}
  var uploadDetail=uploadError?('Failed'+(upload.error?': '+escapeHtml(upload.error):'')):(uploadComplete?'Complete':(uploadProgress==null?(uploadState.indexOf('upload')>=0?'Uploading':'Pending'):percent(uploadProgress)));
  var transcribeDetail=transcribed?'Complete':(transcribeError?'Needs attention':(transcribing?percent(transcribeProgress):(transcribeState==='queued'?'Queued':'Waiting')));
  document.getElementById('transcription-checklist').innerHTML = '<li class="' + (uploadComplete ? 'complete' : (uploadError || uploadProgress != null ? 'active' : '')) + '"><span>Upload audio</span><span class="detail">' + uploadDetail + '</span></li>' +
    '<li class="' + (transcribeError ? 'active' : (transcribing || transcribeState === 'queued' ? 'active' : (transcribed ? 'complete' : ''))) + '"><span>Transcribe recording</span><span class="detail">' + transcribeDetail + '</span></li>' +
    '<li class="' + (transcribed ? 'complete' : '') + '"><span>Transcript ready</span><span class="detail">' + (transcribed ? 'Complete' : 'Pending') + '</span></li>';
}
function openSession(id) {
  currentSession=id;
  document.getElementById('detail-overlay').classList.add('open'); document.body.style.overflow='hidden';
  history.replaceState(null,'','/sessions/'+encodeURIComponent(id));
  fetch('/v1/sessions/'+encodeURIComponent(id),{credentials:'same-origin'}).then(r=>r.json()).then(data=>{
    var meta=data.meta||{}; document.getElementById('overlay-title').textContent=meta.name||id;
    document.getElementById('overlay-meta').textContent=(meta.created||'')+' · '+(meta.device||meta.platform||'Unknown device')+' · '+fmtDuration(meta.duration_sec);
    var players=[]; var tracks=meta.tracks||{};
    ['mic','system'].forEach(track=>{if(data.has_audio && tracks[track]) players.push('<div class="audio-card"><strong>'+(track==='mic'?'You · microphone':'Them · system audio')+'</strong><audio controls preload="metadata" src="/sessions/'+encodeURIComponent(id)+'/audio/'+track+'"></audio></div>');});
    document.getElementById('audio-players').innerHTML=players.join('') || '<div class="empty">Audio has been removed.</div>';
    document.getElementById('retranscribe').disabled=!data.has_audio; document.getElementById('delete-audio').disabled=!data.has_audio;
    renderProcessingChecklist(data);
    var review=data.review||data.review_status||{};
    document.getElementById('review-status').textContent=review.status||'';
    document.getElementById('queue-review').disabled=review.status==='queued'||review.status==='running';
    renderTranscript(data.segments);
    if (detailPollTimer) clearTimeout(detailPollTimer);
    if (currentSession === id && data.jobs && data.jobs[0] && (data.jobs[0].state === 'queued' || data.jobs[0].state === 'running')) detailPollTimer=setTimeout(function(){openSession(id);},3000);
    else if (currentSession === id) loadRows(true);
  });
}
function closeOverlay(){currentSession=null;if(detailPollTimer)clearTimeout(detailPollTimer);detailPollTimer=null;document.getElementById('detail-overlay').classList.remove('open');document.body.style.overflow='';history.replaceState(null,'','/transcriptions');}
function action(path,method,confirmText){if(!currentSession)return;if(confirmText&&!confirm(confirmText))return;return fetch('/v1/sessions/'+encodeURIComponent(currentSession)+path,{method:method||'POST',credentials:'same-origin'}).then(async r=>{if(!r.ok)throw new Error((await r.json()).detail||'Request failed');return r.json();});}
document.getElementById('rows').addEventListener('click',e=>{if(e.target.closest('input,button,a')){updateSelection();return;}var row=e.target.closest('tr[data-id]');if(row)openSession(row.dataset.id);});
document.getElementById('close-overlay').onclick=closeOverlay;
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeOverlay();});
document.getElementById('retranscribe').onclick=()=>action('/retranscribe').then(()=>openSession(currentSession)).catch(e=>alert(e.message));
document.getElementById('queue-review').onclick=()=>action('/review').then(data=>{document.getElementById('review-status').textContent=(data.status||'queued');document.getElementById('queue-review').disabled=true;}).catch(e=>alert(e.message));
document.getElementById('delete-audio').onclick=()=>action('/delete-audio','POST','Delete the source audio? The transcript will remain.').then(()=>openSession(currentSession)).catch(e=>alert(e.message));
document.getElementById('delete-entry').onclick=()=>action('','DELETE','Delete this entire entry and transcript? This cannot be undone.').then(()=>{closeOverlay();loadRows(true);}).catch(e=>alert(e.message));
document.getElementById('select-all').onchange=function(e){document.querySelectorAll('.row-select').forEach(function(box){box.checked=e.target.checked;});updateSelection();};
document.getElementById('bulk-build').onclick=function(){runBulk('/review','POST');};
document.getElementById('bulk-retranscribe').onclick=function(){runBulk('/retranscribe','POST');};
document.getElementById('bulk-delete').onclick=function(){runBulk('','DELETE','Delete the selected entries and transcripts? This cannot be undone.');};
var debounce; document.getElementById('q').oninput=()=>{clearTimeout(debounce);debounce=setTimeout(()=>loadRows(true),250);};
document.getElementById('state').onchange=()=>loadRows(true); document.getElementById('more').onclick=()=>{listState.page++;loadRows(false);};
loadRows(true);
// Once the operator has loaded additional pages, keep that expanded result
// set stable. A page-1 refresh would otherwise discard later pages and their
// selections every five seconds, making "Load more" effectively unusable.
setInterval(function(){if(!currentSession && listState.page===1)loadRows(true);},5000);
""" + f"if ({initial} !== null) openSession({initial});" + """
</script>
"""
    return _shell(
        "Saved transcriptions", body, token_configured=token_configured, active="transcriptions"
    )


def render_meeting_notes_page(*, token_configured: bool) -> str:
    """Meeting-notes library and detail overlay.

    The page deliberately treats every field returned by the review service as
    untrusted plain text.  In particular, model output is never assigned to
    ``innerHTML`` without passing through ``escapeHtml``.
    """
    body = """
<div class="page-head"><div><div class="eyebrow">AI review</div><h1>Meeting notes</h1></div>
  <div><div class="help">Only meetings you explicitly queue for review appear here.</div><a class="btn secondary" href="/v1/bridge/workflow.md" download>Download AI workflow</a></div></div>
<div class="table-wrap"><table>
  <thead><tr><th>Meeting</th><th>Date</th><th>Participants</th><th>Status</th><th>Updated</th></tr></thead>
  <tbody id="notes-rows"><tr><td colspan="5" class="empty">Loading…</td></tr></tbody>
</table></div>
<footer class="pager"><button id="notes-more" class="secondary" style="display:none">Load more</button></footer>

<div class="overlay" id="notes-overlay" role="dialog" aria-modal="true" aria-label="Meeting notes">
  <div class="overlay-inner">
    <div class="overlay-head"><button class="secondary" id="notes-close">← Back</button><div class="title"><div class="eyebrow" id="notes-meta"></div><h1 id="notes-title">Meeting notes</h1></div><button class="secondary" id="notes-retry">Regenerate notes</button></div>
    <div id="notes-state" class="help" role="status"></div>
    <section class="card notes-hero"><div class="notes-toolbar"><div style="flex:1"><div class="eyebrow">Ready to share</div><h2>Meeting summary</h2><div class="help" style="margin:0">A clean Markdown document with completed sections first and placeholders collected at the bottom.</div></div><button class="secondary" id="notes-download">Download .md</button></div></section>
    <section class="card"><article id="notes-document" class="notes-document" aria-label="Meeting summary">Loading meeting summary…</article><pre id="notes-markdown" class="markdown-document" aria-label="Meeting summary Markdown" hidden></pre></section>
    <div hidden aria-hidden="true">
      <div id="notes-summary"></div><div id="notes-narrative"></div><div id="notes-points"></div><div id="notes-decisions"></div><div id="notes-actions"></div><div id="notes-questions"></div><div id="notes-risks"></div><div id="notes-next-steps"></div><div id="notes-participants"></div>
      <!-- Source section labels retained for screen-reader compatibility: Key points, Decisions, Action items, Open questions, Risks, Next steps, Participants. -->
    </div>
    <details class="card"><summary><strong>Transcript</strong> <span class="help">(collapsed)</span></summary><div id="notes-transcript" style="margin-top:14px"></div></details>
  </div>
</div>
<script>
""" + _JS_HELPERS + """
var notesState={page:1,perPage:50,total:0,loaded:0,current:null,markdown:''};
function text(v){return escapeHtml(v==null?'':v);}
function arrayOf(v){return Array.isArray(v)?v:(v==null?[]:[v]);}
function itemText(item){
  if(item==null)return '';
  if(typeof item!=='object')return String(item);
  return String(item.text||item.title||item.point||item.decision||item.risk||item.question||item.step||item.action||'');
}
function listHtml(items, empty){
  items=arrayOf(items).filter(function(x){return x!=null&&String(x).trim()!=='';});
  return items.length ? '<ul>'+items.map(function(x){return '<li>'+text(itemText(x))+'</li>';}).join('')+'</ul>' : '<div class="empty">'+text(empty||'None recorded.')+'</div>';
}
function setList(id, items, empty){
  var root=document.getElementById(id);root.replaceChildren();items=arrayOf(items).filter(function(x){return itemText(x).trim()!=='';});
  if(!items.length){var emptyNode=document.createElement('div');emptyNode.className='empty';emptyNode.textContent=empty||'None recorded.';root.appendChild(emptyNode);return;}
  var list=document.createElement('ul');items.forEach(function(item){var li=document.createElement('li');li.textContent=itemText(item);list.appendChild(li);});root.appendChild(list);
}
function markdownValue(value){
  if(value==null)return '';
  if(Array.isArray(value))return value.map(itemText).filter(function(v){return v.trim()!=='';}).join('\\n');
  return itemText(value).trim();
}
function buildMarkdown(n, meta){
  var title=n.title||meta.title||meta.name||'Meeting summary', people=arrayOf(n.participants||n.attendees).map(function(p){return typeof p==='object'?(p.name||p.email||''):p;}).filter(Boolean);
  var actions=arrayOf(n.action_items||n.actionItems||n.actions).map(function(raw){var a=typeof raw==='object'?raw:{action:raw};var line=a.action||a.task||a.text||'';if(a.owner)line+=' — Owner: '+a.owner;if(a.due||a.due_date)line+=' — Due: '+(a.due||a.due_date);return line;}).filter(Boolean);
  var sections=[
    ['Summary',n.summary||n.overview],['Meeting notes',n.polished_meeting_notes||n.polished_notes||n.meeting_notes||n.narrative||n.notes],
    ['Key points',markdownValue(n.key_points||n.keyPoints)],['Decisions',markdownValue(n.decisions)],['Action items',actions.join('\\n')],
    ['Open questions',markdownValue(n.open_questions||n.openQuestions||n.questions)],['Risks',markdownValue(n.risks||n.risk_items||n.riskItems)],
    ['Next steps',markdownValue(n.next_steps||n.nextSteps||n.follow_ups||n.followUps)],['Participants',people.join('\\n')]
  ];
  var filled=sections.filter(function(s){return String(s[1]||'').trim()!=='';}), empty=sections.filter(function(s){return String(s[1]||'').trim()==='';});
  function render(section){var lines=String(section[1]||'').trim().split('\\n').filter(function(line){return line.trim()!=='';}), prose=section[0]==='Summary'||section[0]==='Meeting notes';var value=prose?lines.join('\\n\\n'):lines.map(function(line){return section[0]==='Action items'?'- [ ] '+line:'- '+line;}).join('\\n');return '## '+section[0]+'\\n'+value+'\\n';}
  var output='# '+title+'\\n\\n'+filled.map(render).join('\\n');
  if(empty.length)output+='\\n---\\n\\n'+empty.map(render).join('\\n');
  return output.trim()+'\\n';
}
function renderNotesDocument(n, meta){
  var actionValues=arrayOf(n.action_items||n.actionItems||n.actions), people=arrayOf(n.participants||n.attendees).map(function(p){return typeof p==='object'?(p.name||p.email||''):p;}).filter(Boolean);
  var sections=[
    ['Summary',n.summary||n.overview],['Meeting notes',n.polished_meeting_notes||n.polished_notes||n.meeting_notes||n.narrative||n.notes],
    ['Key points',n.key_points||n.keyPoints],['Decisions',n.decisions],['Action items',actionValues],
    ['Open questions',n.open_questions||n.openQuestions||n.questions],['Risks',n.risks||n.risk_items||n.riskItems],
    ['Next steps',n.next_steps||n.nextSteps||n.follow_ups||n.followUps],['Participants',people]
  ];
  function values(value){return arrayOf(value).map(itemText).filter(function(v){return String(v).trim()!=='';});}
  function actionMarkup(items){var rows=items.map(function(raw){var a=typeof raw==='object'?raw:{action:raw};var label=a.action||a.task||a.text||'';if(!label)return '';var chips=[];if(a.owner||a.assignee)chips.push('<span class="chip">Owner: '+text(a.owner||a.assignee)+'</span>');if(a.due||a.due_date)chips.push('<span class="chip">Due: '+text(a.due||a.due_date)+'</span>');return '<div class="action-item"><div>'+text(label)+'</div>'+(chips.length?'<div class="chips">'+chips.join('')+'</div>':'')+'</div>';}).filter(Boolean);return rows.length?'<div class="action-list">'+rows.join('')+'</div>':'';}
  function sectionMarkup(section, empty){var title=section[0], vals=values(section[1]);if(!vals.length)return '<section class="notes-section notes-empty"><h2>'+text(title)+'</h2><p>Nothing recorded yet.</p></section>';var body=title==='Action items'?actionMarkup(section[1]):(vals.length===1?'<p>'+text(vals[0])+'</p>':'<ul>'+vals.map(function(v){return '<li>'+text(v)+'</li>';}).join('')+'</ul>');return '<section class="notes-section"><h2>'+text(title)+'</h2>'+body+'</section>';}
  var filled=sections.filter(function(s){return values(s[1]).length;}), empty=sections.filter(function(s){return !values(s[1]).length;}), root=document.getElementById('notes-document');
  root.innerHTML=filled.map(function(s){return sectionMarkup(s,false);}).join('')+(empty.length?'<div class="notes-empty-grid" aria-label="Empty sections">'+empty.map(function(s){return sectionMarkup(s,true);}).join('')+'</div>':'');
}
function noteRow(row){
  var id=row.review_id||row.id||row.session_id||'';
  var participants=arrayOf(row.participants||row.attendees).map(function(x){return typeof x==='object'?(x.name||x.email||''):x;}).filter(Boolean);
  var title=row.title||row.name||row.session_name||'Untitled meeting';
  return '<tr data-id="'+text(id)+'" tabindex="0" role="button" aria-label="Open '+text(title)+'"><td><strong>'+text(title)+'</strong><div class="help" style="margin:3px 0 0">Open summary →</div></td><td>'+text(fmtDate(row.created||row.meeting_time||row.started))+'</td><td>'+text(participants.join(', ')||'—')+'</td><td><span class="badge '+text(row.status||'queued')+'">'+text(row.status||'queued')+'</span></td><td>'+text(fmtDate(row.updated||row.completed_at))+'</td></tr>';
}
function loadNotes(reset){
  if(reset){notesState.page=1;notesState.loaded=0;document.getElementById('notes-rows').innerHTML='';}
  fetch('/v1/meeting-notes?page='+notesState.page+'&per_page='+notesState.perPage,{credentials:'same-origin'}).then(function(r){if(r.status===401||r.status===403){location='/login';return null;}return r.json();}).then(function(data){if(!data)return;var items=data.items||data.meeting_notes||data.notes||[];notesState.total=data.total==null?items.length:data.total;var root=document.getElementById('notes-rows');if(!items.length&&!notesState.loaded)root.innerHTML='<tr><td colspan="5" class="empty">No meetings queued for review.</td></tr>';else root.insertAdjacentHTML('beforeend',items.map(noteRow).join(''));notesState.loaded+=items.length;document.getElementById('notes-more').style.display=notesState.loaded<notesState.total?'':'none';}).catch(function(){document.getElementById('notes-rows').innerHTML='<tr><td colspan="5" class="error-text">Unable to load meeting notes.</td></tr>';});
}
function renderNotes(data){
  var n=data.note||data.meeting_note||data; var meta=n.meta||n;
  document.getElementById('notes-title').textContent=n.title||meta.title||meta.name||'Meeting notes';
  document.getElementById('notes-meta').textContent=[fmtDate(meta.created||meta.meeting_time||meta.started),meta.device||meta.platform].filter(Boolean).join(' · ');
  document.getElementById('notes-state').textContent=n.status||'';
  document.getElementById('notes-summary').textContent=n.summary||n.overview||'No summary was generated.';
  document.getElementById('notes-narrative').textContent=n.polished_meeting_notes||n.polished_notes||n.meeting_notes||n.narrative||n.notes||'No detailed meeting notes were generated.';
  setList('notes-points',n.key_points||n.keyPoints,'No key points recorded.');
  setList('notes-decisions',n.decisions,'No decisions recorded.');
  var actions=arrayOf(n.action_items||n.actionItems||n.actions),actionRoot=document.getElementById('notes-actions');actionRoot.replaceChildren();
  if(actions.length){actions.forEach(function(raw){var a=typeof raw==='object'?raw:{action:raw};var tr=document.createElement('tr');[a.action||a.task||a.text||'',a.owner||a.assignee||'—',a.due||a.due_date||'—'].forEach(function(value){var td=document.createElement('td');td.textContent=value;tr.appendChild(td);});actionRoot.appendChild(tr);});}else{var emptyAction=document.createElement('tr'),emptyCell=document.createElement('td');emptyCell.colSpan=3;emptyCell.className='empty';emptyCell.textContent='No action items recorded.';emptyAction.appendChild(emptyCell);actionRoot.appendChild(emptyAction);}
  setList('notes-questions',n.open_questions||n.openQuestions||n.questions,'No open questions recorded.');
  setList('notes-risks',n.risks||n.risk_items||n.riskItems,'No risks recorded.');
  setList('notes-next-steps',n.next_steps||n.nextSteps||n.follow_ups||n.followUps,'No next steps recorded.');
  var people=arrayOf(n.participants||n.attendees).map(function(p){return typeof p==='object'?(p.name||p.email||''):p;});setList('notes-participants',people,'No participants recorded.');
  notesState.markdown=buildMarkdown(n,meta); document.getElementById('notes-markdown').textContent=notesState.markdown; renderNotesDocument(n,meta);
  var segments=n.transcript||n.segments||[],transcriptRoot=document.getElementById('notes-transcript');transcriptRoot.replaceChildren();
  if(segments.length){segments.forEach(function(s){var segment=document.createElement('div');segment.className='segment';var head=document.createElement('div');head.className='head';var ts=document.createElement('span');ts.className='ts';ts.textContent='['+fmtDuration(s.start||s.start_sec)+']';var label=document.createElement('span');label.className='label';label.textContent=s.speaker||s.label||s.track||'Speaker';head.append(ts,label);var content=document.createElement('div');content.textContent=s.text||s.content||'';segment.append(head,content);transcriptRoot.appendChild(segment);});}else{var emptyTranscript=document.createElement('div');emptyTranscript.className='empty';emptyTranscript.textContent='Transcript unavailable.';transcriptRoot.appendChild(emptyTranscript);}
}
function openNote(id){notesState.current=id;document.getElementById('notes-overlay').classList.add('open');document.body.style.overflow='hidden';fetch('/v1/meeting-notes/'+encodeURIComponent(id),{credentials:'same-origin'}).then(function(r){if(!r.ok)throw new Error('Unable to load notes');return r.json();}).then(renderNotes).catch(function(e){document.getElementById('notes-state').textContent=e.message;});}
function closeNote(){notesState.current=null;document.getElementById('notes-overlay').classList.remove('open');document.body.style.overflow='';}
document.getElementById('notes-rows').addEventListener('click',function(e){var row=e.target.closest('tr[data-id]');if(row)openNote(row.dataset.id);});
document.getElementById('notes-rows').addEventListener('keydown',function(e){var row=e.target.closest('tr[data-id]');if(row&&(e.key==='Enter'||e.key===' ')){e.preventDefault();openNote(row.dataset.id);}});
document.getElementById('notes-close').onclick=closeNote;document.addEventListener('keydown',function(e){if(e.key==='Escape')closeNote();});
document.getElementById('notes-more').onclick=function(){notesState.page++;loadNotes(false);};
document.getElementById('notes-retry').onclick=function(){if(!notesState.current)return;document.getElementById('notes-state').textContent='Queued for regeneration…';fetch('/v1/meeting-notes/'+encodeURIComponent(notesState.current)+'/retry',{method:'POST',credentials:'same-origin'}).then(function(r){if(!r.ok)throw new Error('Unable to queue regeneration');return r.json();}).then(function(d){document.getElementById('notes-state').textContent=d.status||'queued';}).catch(function(e){document.getElementById('notes-state').textContent=e.message;});};
document.getElementById('notes-download').onclick=function(){if(!notesState.markdown)return;var blob=new Blob([notesState.markdown],{type:'text/markdown;charset=utf-8'}),url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download='meeting-notes.md';link.click();setTimeout(function(){URL.revokeObjectURL(url);},1000);};
loadNotes(true);
</script>
"""
    return _shell("Meeting notes", body, token_configured=token_configured, active="meeting-notes")


def render_install_page(server_address: str, *, token_configured: bool) -> str:
    """Human-readable Windows installation and first-run guide."""
    address = html.escape(server_address.rstrip("/"))
    body = fr"""
<div class="page-head"><div><div class="eyebrow">Windows client</div><h1>Install Meeting Notes</h1></div></div>
<div class="card">
  <h2>What the installer includes</h2>
  <p>The Windows package is self-contained. It includes Python, Qt, NumPy,
  SoundCard, the HTTP client, WebSocket support, and their native runtime files.
  You do not need Python, pip, a compiler, an audio driver, or administrator access.</p>
  <p class="help">Requirements: 64-bit Windows 10 or 11, PowerShell 5.1 or newer,
  and access to <strong>{address}</strong> on your LAN.</p>
</div>
<div class="card">
  <h2>Install</h2>
  <p><strong>Fastest option: run this one-step command in PowerShell.</strong> It downloads the installer directly from this server and runs it for the current Windows user.</p>
  <pre class="command">powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "irm '{address}/install/client-agent.ps1' | iex"</pre>
  <ol>
    <li><a class="btn" href="/install/client-agent.ps1" download>Download installer</a></li>
    <li>Open PowerShell normally. Administrator mode is not required.</li>
    <li>Run the downloaded script:</li>
  </ol>
  <pre class="command">powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$env:USERPROFILE\Downloads\Install-MeetingNotes.ps1"</pre>
  <p class="help">If your browser renamed the file, use its actual filename. Re-running
  the installer upgrades the application and preserves your existing server token,
  recording folder, and client settings.</p>
</div>
<div class="card">
  <h2>Uninstall</h2>
  <p><a class="btn secondary" href="/install/uninstall-client.ps1" download>Download uninstaller</a></p>
  <p class="help">Run it in normal PowerShell to remove the per-user application and shortcuts.
  Recordings and <code>%USERPROFILE%\.meeting-notes</code> settings are preserved by default.
  Add <code>-RemoveSettings</code> only when you also want to remove client settings.</p>
</div>
<div class="card">
  <h2>First run</h2>
  <ol>
    <li>Launch <strong>Meeting Notes</strong> from the Start Menu or desktop shortcut.</li>
    <li>Open <strong>Settings</strong>. The Server URL should already be <strong>{address}</strong>.</li>
    <li>Enter the same server token used to sign in to this website, then save.</li>
    <li>Confirm the microphone and system-audio devices shown in the main window.</li>
    <li>Enter an optional meeting name and select <strong>Start recording</strong>.</li>
    <li>Select <strong>Stop recording</strong> when finished. Upload and transcription happen automatically.</li>
  </ol>
  <p>The installer also writes <code>How to run Meeting Notes.txt</code> into the
  application folder at <code>%LOCALAPPDATA%\MeetingNotes</code>.</p>
</div>
<div class="card">
  <h2>Troubleshooting</h2>
  <ul>
    <li>If Windows blocks the download, keep the file only if it came from this server page.</li>
    <li>If the app says the server rejected the token, copy the web-login token again in Settings.</li>
    <li>Remote Desktop may not expose a microphone. Test once from the physical Windows session.</li>
    <li>The installer checks the server before launching and prints a warning if the LAN address is unavailable.</li>
  </ul>
</div>
"""
    return _shell("Install client", body, token_configured=token_configured)


def render_client_installer(server_address: str) -> str:
    """A dependency-complete, configured Windows installer bootstrap."""
    address = json.dumps(server_address.rstrip("/"))
    manifest = json.dumps(server_address.rstrip("/") + "/install/client-manifest.json")
    script = r'''#Requires -Version 5.1
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$serverAddress = __SERVER_ADDRESS__
$manifestUrl = __MANIFEST_URL__
$installDir = Join-Path $env:LOCALAPPDATA "MeetingNotes"
$configDir = Join-Path $env:USERPROFILE ".meeting-notes"
$configPath = Join-Path $configDir "config.json"
$tempDir = Join-Path ([IO.Path]::GetTempPath()) ("MeetingNotes-" + [Guid]::NewGuid().ToString("N"))
$archive = Join-Path $tempDir "MeetingNotes-Windows.zip"
$expanded = Join-Path $tempDir "expanded"
$staging = "$installDir.new"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)

# This is intentionally a per-user install. Do not add elevation, drivers,
# services, HKLM writes, or Program Files paths: Windows WASAPI loopback works
# without them and meetings must remain recordable by a standard user account.

function Write-Step([string]$message) {
    Write-Host "`n==> $message" -ForegroundColor Cyan
}

function Set-DefaultProperty($object, [string]$name, $value) {
    if ($null -eq $object.PSObject.Properties[$name]) {
        $object | Add-Member -MemberType NoteProperty -Name $name -Value $value
    }
}

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw "This installer is for Windows only."
}
if (-not [Environment]::Is64BitOperatingSystem) {
    throw "Meeting Notes requires 64-bit Windows 10 or 11."
}
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

try {
    Write-Step "Downloading the self-contained Meeting Notes client"
    New-Item -ItemType Directory -Path $expanded -Force | Out-Null
    $manifest = Invoke-RestMethod -UseBasicParsing -Uri $manifestUrl
    if (-not $manifest.url -or -not $manifest.sha256 -or $manifest.size -lt 1) {
        throw "The server returned an invalid client manifest."
    }
    $downloadUrl = [Uri]$manifest.url
    $expectedSize = [Int64]$manifest.size
    $expectedHash = ([string]$manifest.sha256).ToLowerInvariant()
    if ($downloadUrl.Scheme -ne "http" -and $downloadUrl.Scheme -ne "https") {
        throw "The client package URL is invalid."
    }
    if ($downloadUrl.Host -ne ([Uri]$manifestUrl).Host -or $downloadUrl.Port -ne ([Uri]$manifestUrl).Port) {
        throw "The client package URL must be hosted by the same server."
    }
    Invoke-WebRequest -UseBasicParsing -Uri $downloadUrl.AbsoluteUri -OutFile $archive
    $actualSize = (Get-Item -LiteralPath $archive).Length
    if ($actualSize -ne $expectedSize) {
        throw "The downloaded package size does not match the server manifest."
    }
    $actualHash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne $expectedHash) {
        throw "The downloaded package hash does not match the server manifest."
    }
    Expand-Archive -LiteralPath $archive -DestinationPath $expanded -Force
    $sourceExe = Get-ChildItem -LiteralPath $expanded -Filter "MeetingNotes.exe" -File -Recurse |
        Select-Object -First 1
    if ($null -eq $sourceExe) {
        throw "The release package does not contain MeetingNotes.exe."
    }

    Write-Step "Installing the application and bundled dependencies"
    Get-Process -Name "MeetingNotes" -ErrorAction SilentlyContinue | ForEach-Object {
        try {
            if ($_.Path -and $_.Path.StartsWith($installDir, [StringComparison]::OrdinalIgnoreCase)) {
                Stop-Process -Id $_.Id -Force
                $_.WaitForExit(5000)
            }
        } catch { }
    }
    if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force }
    New-Item -ItemType Directory -Path $staging -Force | Out-Null
    Copy-Item -Path (Join-Path $sourceExe.DirectoryName "*") -Destination $staging -Recurse -Force
    Get-ChildItem -LiteralPath $staging -Recurse -File | Unblock-File -ErrorAction SilentlyContinue
    if (-not (Test-Path -LiteralPath (Join-Path $staging "MeetingNotes.exe"))) {
        throw "The staged application failed validation."
    }
    if (Test-Path -LiteralPath $installDir) { Remove-Item -LiteralPath $installDir -Recurse -Force }
    Move-Item -LiteralPath $staging -Destination $installDir

    Write-Step "Writing client configuration without replacing existing secrets"
    New-Item -ItemType Directory -Path $configDir -Force | Out-Null
    $config = $null
    if (Test-Path -LiteralPath $configPath) {
        try {
            $config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
        } catch {
            $backup = "$configPath.invalid-$(Get-Date -Format yyyyMMdd-HHmmss)"
            Copy-Item -LiteralPath $configPath -Destination $backup
            Write-Warning "The previous config was invalid and was backed up to $backup"
        }
    }
    if ($null -eq $config) { $config = [PSCustomObject]@{} }
    if ($null -eq $config.PSObject.Properties["server"]) {
        $config | Add-Member -MemberType NoteProperty -Name "server" -Value ([PSCustomObject]@{})
    }
    Set-DefaultProperty $config.server "token" ""
    Set-DefaultProperty $config.server "live_preview" $true
    Set-DefaultProperty $config.server "auto_upload" $true
    if ($null -eq $config.server.PSObject.Properties["url"]) {
        $config.server | Add-Member -MemberType NoteProperty -Name "url" -Value $serverAddress
    } else {
        $config.server.url = $serverAddress
    }
    [IO.File]::WriteAllText($configPath, ($config | ConvertTo-Json -Depth 8), $utf8NoBom)

    $exe = Join-Path $installDir "MeetingNotes.exe"
    $guidePath = Join-Path $installDir "How to run Meeting Notes.txt"
    $guide = @"
MEETING NOTES - HOW TO RUN

1. Open Meeting Notes from the Start Menu or desktop shortcut.
2. Select Settings.
3. Confirm the Server URL is: $serverAddress
4. Enter the same server token used to sign in to the Meeting Notes website.
5. Choose where recordings should be saved, then select Save.
6. Confirm the microphone and system-audio devices shown in the main window.
7. Enter a meeting name (optional) and select Start recording.
8. Select Stop recording when the meeting ends. Upload and transcription are automatic.

The client is self-contained. Python, Qt, NumPy, SoundCard, HTTP, WebSocket,
and native runtime dependencies are included. No driver or administrator access
is required on 64-bit Windows 10 or 11.

Configuration: $configPath
Application:   $installDir
Server:        $serverAddress

If Remote Desktop does not expose a microphone, verify once from the physical
Windows session. If the server rejects the token, update it under Settings.
"@
    [IO.File]::WriteAllText($guidePath, $guide, $utf8NoBom)

    Write-Step "Creating Start Menu and desktop shortcuts"
    # Resolve the shell folders for the current user.  In particular, do not
    # use a hard-coded Desktop path: OneDrive-backed profiles return their
    # redirected Desktop folder from GetFolderPath.  Creating the folders is
    # harmless and keeps a partially fresh profile from losing the shortcut.
    $desktopDir = [Environment]::GetFolderPath("Desktop")
    $programsDir = [Environment]::GetFolderPath("Programs")
    New-Item -ItemType Directory -Path $desktopDir -Force | Out-Null
    New-Item -ItemType Directory -Path $programsDir -Force | Out-Null
    $shortcutPaths = @(
        (Join-Path $desktopDir "Meeting Notes.lnk"),
        (Join-Path $programsDir "Meeting Notes.lnk")
    )
    $shell = New-Object -ComObject WScript.Shell
    foreach ($shortcutPath in $shortcutPaths) {
        $shortcut = $shell.CreateShortcut($shortcutPath)
        $shortcut.TargetPath = $exe
        $shortcut.WorkingDirectory = $installDir
        $shortcut.Description = "Record and transcribe meetings"
        # MeetingNotes.exe carries the embedded release icon, so both the
        # desktop shortcut and the taskbar process show the same branding.
        $shortcut.IconLocation = "$exe,0"
        $shortcut.Save()
    }

    Write-Step "Checking the transcription server"
    try {
        $health = Invoke-RestMethod -UseBasicParsing -Uri ($serverAddress + "/health") -TimeoutSec 10
        if ($health.status -eq "ok") {
            Write-Host "Server is reachable: $serverAddress" -ForegroundColor Green
        } else {
            Write-Warning "The server responded but did not report healthy status."
        }
    } catch {
        Write-Warning "The client was installed, but the server is not reachable yet: $serverAddress"
    }

    Write-Step "Installation complete"
    Write-Host "Launch from the Start Menu or desktop shortcut."
    Write-Host "On first run, open Settings and enter the server login token."
    Write-Host "A copy of the guide is at: $guidePath"
    $process = Start-Process -FilePath $exe -PassThru
    Start-Sleep -Seconds 2
    if ($process.HasExited) {
        Write-Warning "Meeting Notes exited during startup. Re-run the installer or report the startup failure."
    }
} finally {
    if (Test-Path -LiteralPath $tempDir) { Remove-Item -LiteralPath $tempDir -Recurse -Force }
    if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force }
}
'''
    return script.replace("__SERVER_ADDRESS__", address).replace("__MANIFEST_URL__", manifest)


def render_client_uninstaller() -> str:
    """Generate a non-elevated, per-user Windows client removal script."""
    return r'''#Requires -Version 5.1
[CmdletBinding()]
param([switch]$RemoveSettings)

$ErrorActionPreference = "Stop"
$installDir = Join-Path $env:LOCALAPPDATA "MeetingNotes"
$settingsDir = Join-Path $env:USERPROFILE ".meeting-notes"
$desktopShortcut = Join-Path ([Environment]::GetFolderPath("Desktop")) "Meeting Notes.lnk"
$startMenuShortcut = Join-Path ([Environment]::GetFolderPath("Programs")) "Meeting Notes.lnk"

Write-Host "Stopping Meeting Notes processes installed under $installDir..."
Get-Process -Name "MeetingNotes" -ErrorAction SilentlyContinue | ForEach-Object {
    try {
        if ($_.Path -and $_.Path.StartsWith($installDir, [StringComparison]::OrdinalIgnoreCase)) {
            Stop-Process -Id $_.Id -Force
            $_.WaitForExit(5000)
        }
    } catch { }
}

if (Test-Path -LiteralPath $installDir) {
    Remove-Item -LiteralPath $installDir -Recurse -Force
}
foreach ($shortcut in @($desktopShortcut, $startMenuShortcut)) {
    if (Test-Path -LiteralPath $shortcut) { Remove-Item -LiteralPath $shortcut -Force }
}
if ($RemoveSettings -and (Test-Path -LiteralPath $settingsDir)) {
    Remove-Item -LiteralPath $settingsDir -Recurse -Force
    Write-Host "Removed client settings."
} else {
    Write-Host "Recordings and client settings were preserved."
}
Write-Host "Meeting Notes was uninstalled for this Windows user."
'''


# Compatibility names kept for callers/tests from the first web UI.
def render_sessions_page(*, token_configured: bool) -> str:
    return render_transcriptions_page(token_configured=token_configured)


def render_session_detail_page(session_id: str, *, token_configured: bool) -> str:
    return render_transcriptions_page(
        token_configured=token_configured, initial_session_id=session_id
    )


# -- settings ---------------------------------------------------------------


def render_settings_page(
    settings,
    *,
    token_configured: bool,
    message: Optional[str] = None,
    error: Optional[str] = None,
) -> str:
    options = "".join(
        f'<option value="{html.escape(choice)}"'
        f'{" selected" if choice == settings.model else ""}>{html.escape(choice)}</option>'
        for choice in settings.model_choices()
    )
    checked = "checked" if settings.delete_audio_only_after_success else ""
    diarization_checked = "checked" if settings.diarization_enabled else ""
    ai_options = "".join(
        f'<option value="{choice}"{" selected" if choice == settings.ai_provider else ""}>{label}</option>'
        for choice, label in (("disabled", "Disabled"), ("codex", "Codex / ChatGPT"), ("ollama", "Ollama (local)"))
    )
    codex_model_options = '<option value="">Account default</option>'
    if settings.codex_model:
        escaped = html.escape(settings.codex_model)
        codex_model_options += f'<option value="{escaped}" selected>{escaped}</option>'
    escaped_ollama_model = html.escape(settings.ollama_model)
    ollama_model_options = (
        f'<option value="{escaped_ollama_model}" selected>{escaped_ollama_model}</option>'
        if settings.ollama_model else ""
    )
    message_html = f'<div class="banner" style="color:var(--good);border-color:var(--good)">{html.escape(message)}</div>' if message else ""
    error_html = f'<p class="error-text">{html.escape(error)}</p>' if error else ""

    body = f"""
<h1>Settings</h1>
{message_html}
<div class="card">
  {error_html}
  <form method="post" action="/settings">
    <h2>Server and client installation</h2>
    <label class="field">
      <span class="name">Server address</span>
      <input type="text" name="server_address" style="width:100%"
             placeholder="http://meeting-server.local:8000"
             value="{html.escape(settings.server_address)}">
    </label>
    <p class="help">The LAN address embedded into the client-agent installer.
    Leave blank to use the address in the browser when the installer is downloaded.</p>

    <h2>Transcription</h2>
    <label class="field">
      <span class="name">Model</span>
      <select name="model">{options}</select>
    </label>
    <p class="help">The faster-whisper model used for the final transcription pass.
    Changing this takes effect on the next job -- no restart needed.</p>

    <h2>Meeting notes AI</h2>
    <label class="field">
      <span class="name">Provider</span>
      <select name="ai_provider" id="ai-provider">{ai_options}</select>
    </label>
    <p class="help">Transcriptions are never summarized automatically. Choosing a provider
    enables the queued review button for meetings you explicitly send for review.
    Codex / ChatGPT uses the server-side bridge login.</p>
    <div id="codex-settings" class="card">
      <div class="page-head">
        <div><strong>ChatGPT connection</strong><div class="help" id="codex-auth-status" role="status">Checking bridge…</div></div>
        <div style="display:flex;gap:8px"><button type="button" class="secondary" id="codex-connect">Connect ChatGPT</button><button type="button" class="danger" id="codex-disconnect" style="display:none">Disconnect</button></div>
      </div>
      <div id="codex-device-login" style="display:none">
        <p>Open <a id="codex-login-url" href="https://auth.openai.com/codex/device" target="_blank" rel="noopener">OpenAI device sign-in</a> and enter this one-time code:</p>
        <code id="codex-device-code" style="font-size:20px;user-select:all"></code>
      </div>
      <label class="field">
        <span class="name">ChatGPT model</span>
        <select name="codex_model" id="codex-model">{codex_model_options}</select>
      </label>
      <div class="help" id="codex-model-status" role="status">Connect ChatGPT to load available models.</div>
    </div>
    <div id="ollama-settings">
      <label class="field">
        <span class="name">Ollama base URL</span>
        <input type="url" name="ollama_base_url" value="{html.escape(settings.ollama_base_url)}"
               placeholder="http://ollama:11434">
      </label>
      <label class="field">
        <span class="name">Ollama model</span>
        <select name="ollama_model" id="ollama-model">{ollama_model_options}</select>
      </label>
      <button type="button" class="secondary" id="ollama-model-refresh">Load available models</button>
      <span class="help" id="ollama-model-status" role="status"></span>
      <p class="help">The Ollama service must be reachable from the server or bridge container.</p>
    </div>

    <label class="field">
      <span class="name">Beam size</span>
      <input type="number" name="beam_size" min="1" value="{settings.beam_size}">
    </label>

    <h2>Remote speaker labels</h2>
    <label class="checkbox">
      <input type="checkbox" name="diarization_enabled" value="on" {diarization_checked}>
      Distinguish speakers within the system-audio track
    </label>
    <p class="help">Optional and compute-heavy. Requires the diarization extra,
    ffmpeg, acceptance of the model terms, and HUGGINGFACE_TOKEN on the server.</p>
    <label class="field">
      <span class="name">Diarization model</span>
      <input type="text" name="diarization_model" value="{html.escape(settings.diarization_model)}">
    </label>
    <div class="controls">
      <label class="field"><span class="name">Minimum speakers</span>
        <input type="number" name="diarization_min_speakers" min="1" value="{settings.diarization_min_speakers}">
      </label>
      <label class="field"><span class="name">Maximum speakers</span>
        <input type="number" name="diarization_max_speakers" min="1" value="{settings.diarization_max_speakers}">
      </label>
    </div>
    <h2>Audio retention</h2>
    <label class="field">
      <span class="name">Audio retention (days)</span>
      <input type="number" name="audio_retention_days" min="-1" value="{settings.audio_retention_days}">
    </label>
    <p class="help">Set 0 to delete audio immediately after transcription, a positive
    number to retain it for that many days, or -1 to keep it forever.</p>

    <label class="checkbox">
      <input type="checkbox" name="delete_audio_only_after_success" value="on" {checked}>
      Only delete audio once a transcription has succeeded
    </label>

    <label class="field">
      <span class="name">Retention check interval (minutes)</span>
      <input type="number" name="retention_check_interval_minutes" min="1"
             value="{settings.retention_check_interval_minutes}">
    </label>

    <button type="submit">Save settings</button>
  </form>
</div>
<div class="card">
  <h2>Search index</h2>
  <p class="help">Rebuilds the session/transcript search index from what's actually on
  disk. Safe to run any time; only needed if the index looks stale or missing
  (e.g. after restoring the data volume from a backup).</p>
  <button type="button" class="secondary" id="reindex-btn">Rebuild index now</button>
  <span id="reindex-status" class="help"></span>
</div>
<script>
document.getElementById("reindex-btn").addEventListener("click", function () {{
  var status = document.getElementById("reindex-status");
  status.textContent = "Rebuilding...";
  fetch("/v1/reindex", {{ method: "POST", credentials: "same-origin" }})
    .then(function (r) {{ return r.json(); }})
    .then(function (data) {{ status.textContent = "Indexed " + data.reindexed + " session(s)."; }})
    .catch(function () {{ status.textContent = "Rebuild failed."; }});
}});
function updateAiFields() {{
  var provider = document.getElementById("ai-provider").value;
  document.getElementById("ollama-settings").style.display = provider === "ollama" ? "block" : "none";
  document.getElementById("codex-settings").style.display = provider === "codex" ? "block" : "none";
  if (provider === "codex") refreshCodexStatus();
}}
var modelLoads = {{codex:false, ollama:false}}, codexModelsLoaded = false;
function loadProviderModels(provider) {{
  if (modelLoads[provider]) return;
  modelLoads[provider] = true;
  var select = document.getElementById(provider === "codex" ? "codex-model" : "ollama-model");
  var status = document.getElementById(provider === "codex" ? "codex-model-status" : "ollama-model-status");
  var selected = select.value, url = "/v1/ai/models?provider=" + encodeURIComponent(provider);
  if (provider === "ollama") {{
    url += "&ollama_base_url=" + encodeURIComponent(document.querySelector('[name="ollama_base_url"]').value);
  }}
  status.textContent = "Loading available models…";
  fetch(url, {{credentials:"same-origin"}}).then(function(r) {{
    return r.json().then(function(d) {{ if(!r.ok) throw new Error(d.detail || "Unable to load models"); return d; }});
  }}).then(function(data) {{
    var models = Array.isArray(data.models) ? data.models : [];
    select.replaceChildren();
    if (provider === "codex") select.add(new Option("Account default", ""));
    models.forEach(function(model) {{
      var value = String(model.id || ""), label = String(model.name || value);
      if (value) select.add(new Option(label, value));
    }});
    if (selected && !Array.from(select.options).some(function(option) {{ return option.value === selected; }})) {{
      select.add(new Option(selected, selected));
    }}
    select.value = selected;
    if (provider === "codex") {{
      codexModelsLoaded = true;
      if (codexPoll) {{ clearInterval(codexPoll); codexPoll = null; }}
    }}
    status.textContent = models.length ? models.length + " model(s) available." : "No models reported by provider.";
  }}).catch(function(error) {{ status.textContent = error.message; }}).finally(function() {{ modelLoads[provider] = false; }});
}}
var codexPoll = null, codexWasConnected = false;
function renderCodexStatus(data) {{
  var state = data.state || "unavailable", connected = !!data.authenticated;
  if (!connected && codexWasConnected) codexModelsLoaded = false;
  codexWasConnected = connected;
  document.getElementById("codex-auth-status").textContent = connected ? "Connected to ChatGPT" : state.replace(/_/g, " ");
  document.getElementById("codex-connect").style.display = connected ? "none" : "";
  document.getElementById("codex-disconnect").style.display = connected ? "" : "none";
  var login = document.getElementById("codex-device-login"), code = data.device_code || "";
  login.style.display = code ? "block" : "none";
  document.getElementById("codex-device-code").textContent = code;
  var url = String(data.login_url || "");
  if (url.indexOf("https://auth.openai.com/") === 0) document.getElementById("codex-login-url").href = url;
  if (connected && !codexModelsLoaded) loadProviderModels("codex");
  if (connected && codexModelsLoaded && codexPoll) {{ clearInterval(codexPoll); codexPoll = null; }}
}}
function refreshCodexStatus() {{
  if (document.getElementById("ai-provider").value !== "codex") return;
  fetch("/v1/bridge/control/status", {{credentials:"same-origin"}}).then(function(r) {{ if(!r.ok) throw new Error("Bridge unavailable"); return r.json(); }}).then(renderCodexStatus).catch(function(e) {{ document.getElementById("codex-auth-status").textContent = e.message; }});
}}
document.getElementById("codex-connect").addEventListener("click", function() {{
  document.getElementById("codex-auth-status").textContent = "Starting secure device sign-in…";
  fetch("/v1/bridge/control/login", {{method:"POST",credentials:"same-origin"}}).then(function(r) {{ return r.json().then(function(d) {{ if(!r.ok) throw new Error(d.detail||"Unable to start login"); return d; }}); }}).then(function(data) {{ renderCodexStatus(data); if(!codexPoll) codexPoll=setInterval(refreshCodexStatus,1500); }}).catch(function(e) {{ document.getElementById("codex-auth-status").textContent=e.message; }});
}});
document.getElementById("codex-disconnect").addEventListener("click", function() {{
  fetch("/v1/bridge/control/logout", {{method:"POST",credentials:"same-origin"}}).then(function(r) {{ return r.json().then(function(d) {{ if(!r.ok) throw new Error(d.detail||"Unable to disconnect"); return d; }}); }}).then(renderCodexStatus).catch(function(e) {{ document.getElementById("codex-auth-status").textContent=e.message; }});
}});
document.getElementById("ollama-model-refresh").addEventListener("click", function() {{ loadProviderModels("ollama"); }});
document.getElementById("ai-provider").addEventListener("change", updateAiFields);
updateAiFields();
</script>
"""
    return _shell("Settings", body, token_configured=token_configured, active="settings")
