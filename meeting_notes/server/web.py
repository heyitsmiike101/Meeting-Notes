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
"""


def _shell(title: str, body: str, *, token_configured: bool, active: str = "") -> str:
    def nav_link(href: str, label: str, key: str) -> str:
        return f'<a href="{href}">{label}</a>' if key != active else f"<strong>{label}</strong>"

    logout = ""
    if token_configured:
        logout = (
            '<form method="post" action="/logout">'
            '<button type="submit" class="secondary">Log out</button></form>'
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
<nav class="top">
  <span class="brand">meeting-notes</span>
  {nav_link("/", "Sessions", "sessions")}
  {nav_link("/settings", "Settings", "settings")}
  {logout}
</nav>
<div class="wrap">
{banner}
{body}
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
    message_html = f'<div class="banner" style="color:var(--good);border-color:var(--good)">{html.escape(message)}</div>' if message else ""
    error_html = f'<p class="error-text">{html.escape(error)}</p>' if error else ""

    body = f"""
<h1>Settings</h1>
{message_html}
<div class="card">
  {error_html}
  <form method="post" action="/settings">
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

    <label class="field">
      <span class="name">Audio retention (days)</span>
      <input type="number" name="audio_retention_days" min="-1" value="{settings.audio_retention_days}">
    </label>
    <p class="help">-1 keeps audio forever. 0 deletes it as soon as the transcript is
    done. Any other number deletes it that many days after the meeting.</p>

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
