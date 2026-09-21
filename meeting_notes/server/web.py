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
.wrap { max-width: 900px; margin: 0 auto; padding: 16px; }
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
.main { margin-left: 232px; width: calc(100% - 232px); min-height: 100vh; }
.page-head { display:flex; justify-content:space-between; gap:16px; align-items:end; margin-bottom:18px; }
.page-head h1 { font-size:28px; margin:0; }
.eyebrow { color:var(--text-dim); text-transform:uppercase; letter-spacing:.08em; font-size:11px; }
.stat-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px; margin-bottom:18px; }
.stat { background:var(--panel); border:1px solid var(--border); border-radius:9px; padding:16px; }
.stat .value { font-size:26px; font-weight:700; }
.live-dot { display:inline-block; width:8px; height:8px; border-radius:50%; background:var(--bad); margin-right:7px; box-shadow:0 0 0 4px rgba(242,119,122,.12); }
.table-wrap { overflow:auto; border:1px solid var(--border); border-radius:9px; background:var(--panel); }
table { border-collapse:collapse; width:100%; }
th, td { padding:12px 14px; text-align:left; border-bottom:1px solid var(--border); white-space:nowrap; }
th { color:var(--text-dim); font-size:12px; font-weight:600; text-transform:uppercase; letter-spacing:.04em; }
tbody tr { cursor:pointer; }
tbody tr:hover { background:var(--panel-2); }
.overlay { position:fixed; inset:0; background:rgba(8,10,13,.96); z-index:100; display:none; overflow:auto; }
.overlay.open { display:block; }
.overlay-inner { max-width:1100px; margin:0 auto; min-height:100vh; padding:24px; }
.overlay-head { display:flex; align-items:center; gap:12px; margin-bottom:18px; }
.overlay-head .title { flex:1; }
.audio-grid { display:grid; grid-template-columns:1fr 1fr; gap:12px; margin:16px 0; }
.audio-card { background:var(--panel); border:1px solid var(--border); border-radius:8px; padding:12px; }
audio { width:100%; margin-top:8px; }
.install-button { position:fixed; right:22px; bottom:20px; z-index:40; box-shadow:0 8px 28px rgba(0,0,0,.35); }
@media (max-width:760px) {
  .sidebar { width:72px; padding:14px 8px; }
  .sidebar .brand { font-size:0; padding:6px 8px 18px; }
  .sidebar .brand:after { content:'MN'; font-size:16px; }
  .sidebar a.nav-item { font-size:0; }
  .sidebar a.nav-item:after { content:attr(data-short); font-size:12px; }
  .main { margin-left:72px; width:calc(100% - 72px); }
  .stat-grid, .audio-grid { grid-template-columns:1fr; }
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
  <div class="sidebar-bottom">
    {nav_link("/settings", "Settings", "settings", "Settings")}
    {logout}
  </div>
</aside>
<main class="main"><div class="wrap">
  {banner}
  {body}
</div></main>
<a class="btn install-button" href="/install/client-agent.ps1" download>Install client agent</a>
</div>
</body>
</html>"""


_JS_HELPERS = """
function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
    return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];
  });
}
function fmtDate(epochSeconds) {
  if (!epochSeconds) return "unknown date";
  var d = new Date(epochSeconds * 1000);
  return d.toLocaleString();
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
<div class="stat-grid">
  <div class="stat"><div class="eyebrow">Saved meetings</div><div class="value" id="total-count">—</div></div>
  <div class="stat"><div class="eyebrow">Live now</div><div class="value" id="live-count">0</div></div>
  <div class="stat"><div class="eyebrow">With audio</div><div class="value" id="audio-count">—</div></div>
</div>
<section id="live-section" style="display:none">
  <h2><span class="live-dot"></span>Live transcription</h2>
  <div id="live-list"></div>
</section>
<section>
  <div class="page-head"><div><div class="eyebrow">Latest activity</div><h1 style="font-size:20px">Recent transcriptions</h1></div>
    <a href="/transcriptions">View all</a></div>
  <div id="recent-list" class="row-list"><div class="empty">Loading…</div></div>
</section>
<script>
""" + _JS_HELPERS + """
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
    document.getElementById('live-count').textContent = data.total;
    var section = document.getElementById('live-section');
    section.style.display = data.total ? '' : 'none';
    document.getElementById('live-list').innerHTML = data.items.map(item => {
      var text = (item.partials || []).slice(-20).map(p => '<div class="segment"><div class="head"><span class="ts">[' + fmtDuration(p.start) + ']</span><span class="label ' + (p.track === 'mic' ? 'track-mic' : 'track-system') + '">' + (p.track === 'mic' ? 'You' : 'Them') + '</span></div><div>' + escapeHtml(p.text) + '</div></div>').join('');
      return '<div class="card"><h2>' + escapeHtml(item.name) + '</h2><div class="help">' + escapeHtml(item.device) + ' · started ' + fmtDate(item.started_wall) + '</div>' + (text || '<div class="empty">Listening for speech…</div>') + '</div>';
    }).join('');
  });
}
loadOverview(); loadLive(); setInterval(loadOverview, 10000); setInterval(loadLive, 2500);
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
<div class="table-wrap"><table>
  <thead><tr><th>Time</th><th>Device</th><th>Name</th><th>Length</th><th>Status</th><th>Audio</th></tr></thead>
  <tbody id="rows"><tr><td colspan="6" class="empty">Loading…</td></tr></tbody>
</table></div>
<footer class="pager"><button id="more" class="secondary" style="display:none">Load more</button></footer>

<div class="overlay" id="detail-overlay" role="dialog" aria-modal="true" aria-label="Meeting transcript">
  <div class="overlay-inner">
    <div class="overlay-head"><button class="secondary" id="close-overlay">← Back</button><div class="title"><div class="eyebrow" id="overlay-meta"></div><h1 id="overlay-title">Meeting</h1></div></div>
    <div id="audio-players" class="audio-grid"></div>
    <div class="actions">
      <button class="secondary" id="retranscribe">Retranscribe</button>
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

function tableRow(row) {
  return '<tr data-id="' + escapeHtml(row.session_id) + '"><td>' + fmtDate(row.created) + '</td><td>' + escapeHtml(row.device || row.platform || 'Unknown') + '</td><td><strong>' + escapeHtml(row.name || row.session_id) + '</strong></td><td>' + fmtDuration(row.duration_sec) + '</td><td>' + stateBadge(row) + '</td><td>' + (row.has_audio ? fmtBytes(row.audio_bytes) : 'Transcript only') + '</td></tr>';
}
function loadRows(reset) {
  if (reset) { listState.page=1; listState.loaded=0; document.getElementById('rows').innerHTML=''; }
  var url='/v1/sessions?page='+listState.page+'&per_page='+listState.perPage;
  var q=document.getElementById('q').value.trim(), state=document.getElementById('state').value;
  if(q) url+='&q='+encodeURIComponent(q); if(state) url+='&state='+encodeURIComponent(state);
  fetch(url,{credentials:'same-origin'}).then(r=>r.json()).then(data=>{
    listState.total=data.total;
    var rows=document.getElementById('rows');
    if(!data.items.length && !listState.loaded) rows.innerHTML='<tr><td colspan="6" class="empty">No transcriptions found.</td></tr>';
    else rows.insertAdjacentHTML('beforeend',data.items.map(tableRow).join(''));
    listState.loaded+=data.items.length;
    document.getElementById('more').style.display=listState.loaded<listState.total?'':'none';
  });
}
function renderTranscript(segments) {
  var root=document.getElementById('overlay-segments');
  if(!segments || !segments.length){root.innerHTML='<div class="empty">No final transcript yet.</div>';return;}
  root.innerHTML=segments.map(seg=>seg.in_gap?'<div class="gap-marker">[audio lost]</div>':'<div class="segment"><div class="head"><span class="ts">['+fmtDuration(seg.start)+']</span><span class="label '+(seg.track==='mic'?'track-mic':'track-system')+'">'+escapeHtml(seg.label)+'</span></div><div class="text'+(seg.approximate?' approximate':'')+'">'+escapeHtml(seg.text)+'</div></div>').join('');
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
    renderTranscript(data.segments);
  });
}
function closeOverlay(){currentSession=null;document.getElementById('detail-overlay').classList.remove('open');document.body.style.overflow='';history.replaceState(null,'','/transcriptions');}
function action(path,method,confirmText){if(!currentSession)return;if(confirmText&&!confirm(confirmText))return;return fetch('/v1/sessions/'+encodeURIComponent(currentSession)+path,{method:method||'POST',credentials:'same-origin'}).then(async r=>{if(!r.ok)throw new Error((await r.json()).detail||'Request failed');return r.json();});}
document.getElementById('rows').addEventListener('click',e=>{var row=e.target.closest('tr[data-id]');if(row)openSession(row.dataset.id);});
document.getElementById('close-overlay').onclick=closeOverlay;
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeOverlay();});
document.getElementById('retranscribe').onclick=()=>action('/retranscribe').then(()=>openSession(currentSession)).catch(e=>alert(e.message));
document.getElementById('delete-audio').onclick=()=>action('/delete-audio','POST','Delete the source audio? The transcript will remain.').then(()=>openSession(currentSession)).catch(e=>alert(e.message));
document.getElementById('delete-entry').onclick=()=>action('','DELETE','Delete this entire entry and transcript? This cannot be undone.').then(()=>{closeOverlay();loadRows(true);}).catch(e=>alert(e.message));
var debounce; document.getElementById('q').oninput=()=>{clearTimeout(debounce);debounce=setTimeout(()=>loadRows(true),250);};
document.getElementById('state').onchange=()=>loadRows(true); document.getElementById('more').onclick=()=>{listState.page++;loadRows(false);};
loadRows(true);
""" + f"if ({initial} !== null) openSession({initial});" + """
</script>
"""
    return _shell(
        "Saved transcriptions", body, token_configured=token_configured, active="transcriptions"
    )


def render_client_installer(server_address: str) -> str:
    """A configured Windows installer bootstrap downloaded from the web UI."""
    address = json.dumps(server_address.rstrip("/"))
    download = json.dumps(
        "https://github.com/heyitsmiike101/Meeting-Notes/releases/latest/download/MeetingNotes-Windows.zip"
    )
    return f'''$ErrorActionPreference = "Stop"
$serverAddress = {address}
$downloadUrl = {download}
$installDir = Join-Path $env:LOCALAPPDATA "MeetingNotes"
$archive = Join-Path $env:TEMP "MeetingNotes-Windows.zip"
Write-Host "Downloading Meeting Notes client..."
Invoke-WebRequest -Uri $downloadUrl -OutFile $archive
if (Test-Path -LiteralPath $installDir) {{ Remove-Item -LiteralPath $installDir -Recurse -Force }}
New-Item -ItemType Directory -Path $installDir -Force | Out-Null
Expand-Archive -LiteralPath $archive -DestinationPath $installDir -Force
$configDir = Join-Path $env:USERPROFILE ".meeting-notes"
New-Item -ItemType Directory -Path $configDir -Force | Out-Null
$config = @{{ server = @{{ url = $serverAddress; token = ""; live_preview = $true; auto_upload = $true }} }}
$config | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $configDir "config.json") -Encoding UTF8
$exe = Join-Path $installDir "MeetingNotes.exe"
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut((Join-Path ([Environment]::GetFolderPath("Desktop")) "Meeting Notes.lnk"))
$shortcut.TargetPath = $exe
$shortcut.WorkingDirectory = $installDir
$shortcut.Save()
Write-Host "Installed. Server: $serverAddress"
Start-Process -FilePath $exe
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
</script>
"""
    return _shell("Settings", body, token_configured=token_configured, active="settings")
