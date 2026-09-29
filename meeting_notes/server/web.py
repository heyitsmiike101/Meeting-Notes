"""HTML rendering for the browser-facing side of the server.

No template engine (Jinja2 is deliberately not a dependency here -- see
``ARCHITECTURE.md``): every page is built by small Python functions that
return strings, escaping anything server-rendered with ``html.escape``. The
meetings library and the meeting sheet are thin shells around inline vanilla
JS that fetches the JSON API (``/v1/sessions``, ``/v1/sessions/{id}``) and
renders client-side: the JSON API is what stays correct as the number of
sessions grows, so the HTML side defers to it rather than re-deriving its own
(unpaginated, un-indexed) view of the same data.

``app.py`` only calls the ``render_*`` functions and wires them to routes; it
never builds HTML itself, so every string of markup lives in exactly one
place.

Visual world (see ``.impeccable/surfaces/meeting-notes-server-web-py.md``): a
studio track sheet. Console-graphite chrome (the top console bar, sheet header
strips, bulk and save bars) around tape-box card stock; kraft for spines, rules
and secondary panels; grease-pencil red is the only accent. Barlow and Barlow
Condensed are self-hosted from ``/static/fonts``.
"""

from __future__ import annotations

import hashlib
import html
import json
from pathlib import Path
from typing import Optional

from meeting_notes import __version__

# -- icons ----------------------------------------------------------------
# One stroke (1.75 on a 24 grid, round caps and joins) for every icon; the
# same table is handed to the page JS so scripted markup draws the same set.

_ICON_PATHS = {
    "reel": '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="2.4"/><path d="M12 3v6.6M4.2 16.5l5.7-3.3M19.8 16.5l-5.7-3.3"/>',
    "shelf": '<path d="M4 8h4v12H4zM10 4h4v16h-4zM16 10h4v10h-4z"/>',
    "home": '<path d="M4 11l8-7 8 7M6 10v10h12V10"/>',
    "sliders": '<path d="M4 7h9M17 7h3M4 17h3M11 17h9"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="17" r="2"/>',
    "download": '<path d="M12 4v11M7.5 10.5L12 15l4.5-4.5M5 20h14"/>',
    "logout": '<path d="M10 4H5v16h5M15 8l4 4-4 4M19 12H9"/>',
    "back": '<path d="M19 12H5M11 6l-6 6 6 6"/>',
    "open": '<path d="M5 12h14M13 6l6 6-6 6"/>',
    "edit": '<path d="M4 20l1-4L16 5l3 3L8 19zM14 7l3 3"/>',
    "copy": '<rect x="8" y="8" width="12" height="12" rx="1.5"/><path d="M16 8V5.5A1.5 1.5 0 0 0 14.5 4h-9A1.5 1.5 0 0 0 4 5.5v9A1.5 1.5 0 0 0 5.5 16H8"/>',
    "refresh": '<path d="M20 12a8 8 0 1 1-2.4-5.7M20 4v4.5h-4.5"/>',
    "search": '<circle cx="11" cy="11" r="6.5"/><path d="M16 16l4.5 4.5"/>',
    "notes": '<path d="M6 4h9l3 3v13H6zM9 11h6M9 15h6"/>',
    "mic": '<rect x="9" y="4" width="6" height="10" rx="3"/><path d="M6 11a6 6 0 0 0 12 0M12 17v3"/>',
    "speaker": '<path d="M5 9.5h3.5L13 6v12l-4.5-3.5H5zM16.5 9a4 4 0 0 1 0 6"/>',
    "alert": '<path d="M12 4l9 16H3zM12 10v4M12 17v.01"/>',
    "check": '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    "key": '<circle cx="8" cy="15" r="4"/><path d="M11 12l8-8M16 7l3 3M14 9l2 2"/>',
}


def _icon(name: str, size: int = 18) -> str:
    return (
        f'<svg class="ic" viewBox="0 0 24 24" width="{size}" height="{size}" '
        f'aria-hidden="true" focusable="false">{_ICON_PATHS[name]}</svg>'
    )


# -- shared shell -------------------------------------------------------

_STATIC_DIR = Path(__file__).resolve().parent / "static"


def _asset_href(name: str) -> str:
    """``/static/<name>?v=<content hash>``: cached for a year, busted when the file changes."""
    try:
        digest = hashlib.sha1((_STATIC_DIR / name).read_bytes()).hexdigest()[:10]
    except OSError:
        digest = __version__
    return f"/static/{name}?v={digest}"


_CSS_HREF = _asset_href("app.css")
_ICONS_SRC = _asset_href("icons.js")


def stylesheet_text() -> str:
    """The shared stylesheet served at ``/static/app.css`` (tests and tooling read it here)."""
    return (_STATIC_DIR / "app.css").read_text(encoding="utf-8")


_NAV = (
    ("/meetings", "Meetings", "transcriptions", "shelf"),
    ("/", "Home", "home", "home"),
    ("/settings", "Settings", "settings", "sliders"),
    ("/install", "Install", "install", "download"),
)
_ACTIVE_ALIASES = {"sessions": "transcriptions", "meeting-notes": "transcriptions"}


def _shell(
    title: str,
    body: str,
    *,
    token_configured: bool,
    active: str = "",
    console_search: str = "",
    main_class: str = "",
    nav: bool = True,
) -> str:
    active = _ACTIVE_ALIASES.get(active, active)
    nav_html = ""
    logout = ""
    if nav:
        current = ' aria-current="page"'
        links = "".join(
            f'<a href="{href}"{current if key == active else ""}>'
            f"{_icon(icon)}<span>{label}</span></a>"
            for href, label, key, icon in _NAV
        )
        nav_html = f'<nav class="primary" aria-label="Primary">{links}</nav>'
        if token_configured:
            logout = (
                '<form method="post" action="/logout">'
                f'<button type="submit" class="ghost logout" aria-label="Log out">{_icon("logout")}'
                '<span class="lbl">Log out</span></button></form>'
            )

    banner = ""
    if not token_configured:
        banner = (
            f'<div class="banner">{_icon("alert")}<span>No MEETING_NOTES_TOKEN is configured -- '
            "this server accepts requests from anyone who can reach it. "
            "Fine for a quick local test, not recommended left that way on a "
            "shared network.</span></div>"
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#1d1f22">
<title>{html.escape(title)}</title>
<link rel="preload" href="/static/fonts/barlow-latin-400.woff2" as="font" type="font/woff2" crossorigin>
<link rel="stylesheet" href="{_CSS_HREF}">
<script src="{_ICONS_SRC}"></script>
</head>
<body>
<a class="skip" href="#main">Skip to content</a>
<header class="console"><div class="console-in">
  <a class="brand" href="/" aria-label="Meeting Notes home">{_icon("reel", 26)}<span>Meeting Notes</span></a>
  {nav_html}
  {console_search}
  <div class="console-tools">
    <span class="app-version" aria-label="Meeting Notes version">v{html.escape(__version__)}</span>
    {logout}
  </div>
</div></header>
<main id="main" class="{main_class}">
  {banner}
  {body}
</main>
<div class="toast" id="page-toast" role="status" aria-live="polite"></div>
</body>
</html>"""


_JS_HELPERS_SRC = r"""
var ICONS = window.MN_ICONS || {};
function icon(name, size) {
  size = size || 18;
  return '<svg class="ic" viewBox="0 0 24 24" width="' + size + '" height="' + size + '" aria-hidden="true" focusable="false">' + (ICONS[name] || '') + '</svg>';
}
var TICKS = {
  done: '<rect x="1.5" y="1.5" width="13" height="13" rx="1.5"/><path d="M4.6 8.4l2.3 2.3 4.5-4.9"/>',
  error: '<rect x="1.5" y="1.5" width="13" height="13" rx="1.5"/><path class="mark" d="M5.3 5.3l5.4 5.4M10.7 5.3l-5.4 5.4"/>',
  run: '<rect x="1.5" y="1.5" width="13" height="13" rx="1.5"/><path class="half" d="M2.4 13.6L13.6 2.4v11.2z"/>',
  queue: '<rect x="1.5" y="1.5" width="13" height="13" rx="1.5" stroke-dasharray="2.6 2.2"/>',
  none: '<rect x="1.5" y="1.5" width="13" height="13" rx="1.5"/>'
};
function tick(kind) {
  var cls = kind === 'error' ? 'tk fill' : (kind === 'none' ? 'tk none' : 'tk');
  return '<svg class="' + cls + '" viewBox="0 0 16 16" aria-hidden="true" focusable="false">' + (TICKS[kind] || TICKS.none) + '</svg>';
}
var toastTimer = null;
function notify(message, kind) {
  var open = document.querySelector('.overlay.open');
  var el = (open && open.querySelector('.toast')) || document.getElementById('page-toast');
  if (!el) return;
  el.className = 'toast' + (kind === 'error' ? ' error' : '');
  el.textContent = message;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(function () { el.textContent = ''; }, kind === 'error' ? 9000 : 4500);
}
function copyText(text) {
  var previous = document.activeElement;
  function legacy() {
    try {
      var ta = document.createElement('textarea');
      ta.value = text; ta.setAttribute('readonly', ''); ta.style.position = 'fixed'; ta.style.opacity = '0';
      document.body.appendChild(ta); ta.select();
      var ok = document.execCommand('copy');
      document.body.removeChild(ta);
      if (previous && previous.focus) previous.focus();
      return ok;
    } catch (_) { return false; }
  }
  if (navigator.clipboard && window.isSecureContext) {
    return navigator.clipboard.writeText(text).then(function () { return true; }, function () { return legacy(); });
  }
  return Promise.resolve(legacy());
}
function trapFocus(event, root) {
  if (event.key !== 'Tab') return;
  var focusable = Array.from(root.querySelectorAll('button:not([disabled]),a[href],input:not([disabled]),select:not([disabled]),textarea:not([disabled]),audio[controls],summary,[tabindex="0"]')).filter(function (el) {
    return el.tabIndex >= 0 && el.getClientRects().length > 0;
  });
  if (!focusable.length) return;
  var first = focusable[0], last = focusable[focusable.length - 1];
  if (event.shiftKey && (document.activeElement === first || !root.contains(document.activeElement))) { event.preventDefault(); last.focus(); }
  else if (!event.shiftKey && (document.activeElement === last || !root.contains(document.activeElement))) { event.preventDefault(); first.focus(); }
}
function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
    return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];
  });
}
function fmtDate(value, relative) {
  // One compact formatter for every date: "Sep 28, 3:07 PM"; the year only when it is not this year;
  // with `relative`, "Today 3:07 PM" / "Yesterday 3:07 PM".
  if (!value) return "unknown date";
  var numeric = typeof value === "number" || /^[0-9]+([.][0-9]+)?$/.test(String(value));
  var raw = numeric ? Number(value) : value;
  var dateOnly = !numeric && /^[0-9]{4}-[0-9]{2}-[0-9]{2}$/.test(String(value));
  // Server indexes use epoch seconds while session metadata uses ISO-8601.
  // Also tolerate millisecond epochs from future integrations.
  var d = dateOnly ? new Date(Number(raw.slice(0, 4)), Number(raw.slice(5, 7)) - 1, Number(raw.slice(8, 10))) : new Date(numeric && raw < 100000000000 ? raw * 1000 : raw);
  if (isNaN(d.getTime())) return "unknown date";
  var now = new Date();
  var time = dateOnly ? "" : d.toLocaleTimeString(undefined, {hour: "numeric", minute: "2-digit"});
  function dayKey(x) { return x.getFullYear() * 10000 + x.getMonth() * 100 + x.getDate(); }
  if (relative) {
    var yesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
    if (dayKey(d) === dayKey(now)) return "Today" + (time ? " " + time : "");
    if (dayKey(d) === dayKey(yesterday)) return "Yesterday" + (time ? " " + time : "");
  }
  var opts = {month: "short", day: "numeric"};
  if (d.getFullYear() !== now.getFullYear()) opts.year = "numeric";
  return d.toLocaleDateString(undefined, opts) + (time ? ", " + time : "");
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
  if (!state) return '<span class="badge none">' + tick('none') + 'No job yet</span>';
  if (state === "running" || state === "queued") {
    var pct = row.latest_progress != null ? Math.round(row.latest_progress * 100) + "%" : "";
    return '<span class="badge ' + state + '">' + tick(state === "running" ? 'run' : 'queue') + state.charAt(0).toUpperCase() + state.slice(1) + (pct ? " " + pct : "") + '</span>';
  }
  if (state === "error") {
    var msg = row.latest_error ? ": " + escapeHtml(row.latest_error) : "";
    return '<span class="badge error">' + tick('error') + 'Error' + msg + '</span>';
  }
  return '<span class="badge done">' + tick('done') + 'Done</span>';
}
function processingState(row) {
  var pipeline = row.pipeline || {}, upload = pipeline.upload || {}, transcription = pipeline.transcription || {};
  var uploadState = String(upload.state || "").toLowerCase(), transcriptionState = String(transcription.state || "").toLowerCase();
  function complete(s) { return s === "complete" || s === "completed" || s === "done" || s === "uploaded" || s === "ready"; }
  if (uploadState.indexOf("error") >= 0 || uploadState.indexOf("fail") >= 0) {
    return {key:"error", label:"Upload failed", pct:null, detail:upload.error || ""};
  }
  if (uploadState && !complete(uploadState)) {
    return {key:uploadState.indexOf("upload") >= 0 ? "uploading" : "upload", label:uploadState.indexOf("upload") >= 0 ? "Uploading" : "Pending end of meeting", pct:uploadState === "pending" ? null : upload.percent};
  }
  if (transcriptionState) {
    if (complete(transcriptionState)) return {key:"complete", label:"Complete", pct:100};
    if (transcriptionState.indexOf("error") >= 0 || transcriptionState.indexOf("fail") >= 0) return {key:"error", label:"Needs attention", pct:null};
    if (transcriptionState.indexOf("transcrib") >= 0 || transcriptionState === "running") return {key:"transcribing", label:"Transcribing", pct:transcription.percent};
    return {key:"queued", label:"Queued", pct:transcription.percent};
  }
  var state = row.latest_state;
  if (!row.has_audio) return {key:"upload", label:"Pending end of meeting", pct:null};
  if (state === "running") return {key:"transcribing", label:"Transcribing", pct:row.latest_progress == null ? null : row.latest_progress * 100};
  if (state === "queued") return {key:"queued", label:"Queued", pct:row.latest_progress == null ? null : row.latest_progress * 100};
  if (state === "error") return {key:"error", label:"Needs attention", pct:null};
  if (state === "done") return {key:"complete", label:"Complete", pct:100};
  return {key:"upload", label:"Ready to transcribe", pct:null};
}
function processingBadge(row) {
  var status = processingState(row), rawPct = status.pct, pct = rawPct == null || status.key === "complete" ? "" : " " + Math.round(rawPct) + "%";
  var cls = status.key === "complete" ? "done" : (status.key === "error" ? "error" : (status.key === "transcribing" || status.key === "queued" || status.key === "uploading" ? "running" : ""));
  var kind = status.key === "complete" ? "done" : (status.key === "error" ? "error" : (status.key === "transcribing" || status.key === "uploading" ? "run" : "queue"));
  var errorDetail = status.detail || row.latest_error || "";
  var title = status.key === "error" && errorDetail ? ' title="' + escapeHtml(errorDetail) + '"' : '';
  return '<span class="badge ' + cls + '"' + title + '>' + tick(kind) + escapeHtml(status.label) + pct + '</span>';
}
"""

_JS_HELPERS = _JS_HELPERS_SRC


def render_login_page(error: bool = False) -> str:
    error_html = (
        '<p class="error-text" role="alert">Invalid token. Check it and try again.</p>' if error else ""
    )
    body = f"""
<div class="login-wrap">
  <div class="login-card">
    <header><span class="legend">Console sign-in</span></header>
    <div class="inner">
      <h1>Sign in</h1>
      {error_html}
      <form method="post" action="/login">
        <label class="field">
          <span class="name">Server token</span>
          <input type="password" name="token" autofocus required autocomplete="current-password"{' aria-invalid="true"' if error else ""}>
        </label>
        <button type="submit">Sign in</button>
      </form>
    </div>
  </div>
</div>
"""
    return _shell("Sign in", body, token_configured=True, nav=False)


def render_home_page(*, token_configured: bool) -> str:
    skeleton = "".join(
        '<div class="spine skel" aria-hidden="true"><span class="no"></span><span class="skel-bar"></span></div>'
        for _ in range(4)
    )
    body = (
        f"""
<div class="page-head">
  <h1>Home</h1>
  <dl class="readout" aria-label="Shelf readout">
    <div><dt>Meetings</dt><dd id="total-count">—</dd></div>
    <div><dt>Live now</dt><dd id="live-count">0</dd></div>
    <div><dt>With audio</dt><dd id="audio-count">—</dd></div>
  </dl>
</div>
<section id="live-section" style="display:none" aria-labelledby="live-heading">
  <div class="sec-head"><h2 id="live-heading"><span class="live-dot" aria-hidden="true"></span>Live transcription</h2></div>
  <div id="live-list" class="live-list"></div>
</section>
<div class="home-grid">
  <section aria-labelledby="recent-heading">
    <div class="sec-head"><h2 id="recent-heading">Recent meetings</h2><a href="/meetings">View all meetings</a></div>
    <div id="recent-list" class="spines" aria-busy="true">{skeleton}</div>
  </section>
  <section class="panel" aria-labelledby="upload-heading">
    <h2 id="upload-heading">Upload a meeting recording</h2>
    <p class="help">Drop in an audio file and Meeting Notes will upload and transcribe it. MP3, WAV, M4A, MP4, FLAC, OGG, OGA, Opus, AAC, and WebM are supported.</p>
    <form id="recording-upload" class="upload-form">
      <label class="field"><span class="name">Recording</span><input id="recording-file" type="file" accept="audio/*,.mp3,.wav,.m4a,.mp4,.flac,.ogg,.oga,.opus,.aac,.webm" required></label>
      <label class="field"><span class="name">Meeting name <span class="help" style="display:inline;margin:0">(optional)</span></span><input id="recording-name" type="text" maxlength="200" placeholder="e.g. Weekly standup"></label>
      <button type="submit" id="upload-submit">Upload recording</button>
    </form>
    <div class="upload-status" id="upload-status" role="status" aria-live="polite"></div>
    <div class="progress-track" id="upload-progress-track" hidden><i id="upload-progress"></i></div>
  </section>
</div>
<script>
"""
        + _JS_HELPERS
        + r"""
var uploadForm = document.getElementById('recording-upload');
uploadForm.addEventListener('submit', function(event) {
  event.preventDefault();
  var file = document.getElementById('recording-file').files[0];
  var status = document.getElementById('upload-status'), submit = document.getElementById('upload-submit');
  var track = document.getElementById('upload-progress-track'), bar = document.getElementById('upload-progress');
  if (!file) return;
  var xhr = new XMLHttpRequest(), form = new FormData();
  form.append('file', file); form.append('name', document.getElementById('recording-name').value.trim());
  status.classList.remove('err');
  submit.disabled = true; track.hidden = false; bar.style.transform = 'scaleX(0)'; status.textContent = 'Uploading ' + file.name + '…';
  xhr.upload.addEventListener('progress', function(e) { if (e.lengthComputable) { var pct = Math.round(e.loaded / e.total * 100); bar.style.transform = 'scaleX(' + (pct / 100) + ')'; status.textContent = 'Uploading… ' + pct + '%'; } });
  xhr.addEventListener('load', function() {
    submit.disabled = false;
    var data = {}; try { data = JSON.parse(xhr.responseText || '{}'); } catch (_) {}
    if (xhr.status < 200 || xhr.status >= 300) { status.classList.add('err'); status.textContent = data.detail || 'Upload failed. Please try again.'; return; }
    bar.style.transform = 'scaleX(1)'; status.textContent = 'Upload complete. Transcription queued' + (data.session_id ? ' — opening meeting…' : '.');
    if (data.session_id) setTimeout(function() { location.href = '/sessions/' + encodeURIComponent(data.session_id); }, 500);
  });
  xhr.addEventListener('error', function() { submit.disabled = false; status.classList.add('err'); status.textContent = 'Upload failed. Check the server connection and try again.'; });
  xhr.open('POST', '/v1/uploads'); xhr.withCredentials = true; xhr.send(form);
});
var liveItems = [];
var activeLiveId = null;
var liveOverlayPreviousFocus = null;

function liveText(item) {
  return (item.partials || []).slice(-20).map(function (p) {
    var mic = p.track === 'mic';
    return '<div class="segment lane-' + (mic ? 'mic' : 'system') + '"><span class="ts">' + fmtDuration(p.start) + '</span><span class="label"><i class="sw' + (mic ? '' : ' them') + '"></i>' + (mic ? 'You' : 'Them') + '</span><div class="text">' + escapeHtml(p.text) + '</div></div>';
  }).join('');
}
function liveCard(item) {
  return '<div class="live-card" role="button" tabindex="0" data-live-id="' + escapeHtml(item.session_id) + '" aria-label="Open live transcript for ' + escapeHtml(item.name) + '"><span class="open-hint">Open transcript ' + icon('open', 16) + '</span><h2>' + escapeHtml(item.name) + '</h2><div class="help">' + escapeHtml(item.device) + ' · started ' + fmtDate(item.started_wall) + '</div>' + (liveText(item) || '<div class="empty">Listening for speech…</div>') + '</div>';
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
  var next = liveText(item) || '<div class="empty">Listening for speech…</div>';
  if (content._lastMarkup === next) return;
  content.innerHTML = next; content._lastMarkup = next;
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
  var overlay = document.getElementById('live-overlay');
  if (!overlay.classList.contains('open')) return;
  if (event.key === 'Escape') closeLive(false);
  else trapFocus(event, overlay);
});
window.addEventListener('popstate', function () { closeLive(true); });
function spineRow(row) {
  var notesReady = row.review && row.review.status === 'done';
  return '<a class="spine" href="/sessions/' + encodeURIComponent(row.session_id) + '"><span class="no">' + escapeHtml(row.board || '') + '</span><span class="main"><strong>' + escapeHtml(row.name || row.session_id) + '</strong><small>' + fmtDate(row.created, true) + ' · ' + escapeHtml(row.device || 'Unknown device') + '</small></span><span class="ticks">' + stateBadge(row) + (notesReady ? '<span class="badge done">' + tick('done') + 'Notes ready</span>' : '') + '</span></a>';
}
function loadOverview() {
  fetch('/v1/sessions?per_page=8', {credentials:'same-origin'}).then(function (r) { if (!r.ok) throw new Error('load failed'); return r.json(); }).then(function (data) {
    document.getElementById('total-count').textContent = data.total;
    document.getElementById('audio-count').textContent = data.audio_total == null ? '—' : data.audio_total;
    var recent = document.getElementById('recent-list');
    var markup = data.items.length ? data.items.map(spineRow).join('') : '<div class="empty-teach"><h2>No meetings yet.</h2><p>Record a meeting with the Windows client, or upload a recording here. Finished meetings land on your shelf with their notes.</p><div class="row"><a class="btn" href="/install">Install the Windows client</a></div></div>';
    if (recent._lastMarkup !== markup) { recent.innerHTML = markup; recent._lastMarkup = markup; }
    recent.setAttribute('aria-busy', 'false');
  }).catch(function () {
    var recent = document.getElementById('recent-list');
    if (recent._lastMarkup == null) { recent.innerHTML = '<div class="empty">Could not load recent meetings. Retrying…</div>'; recent.setAttribute('aria-busy', 'false'); }
  });
}
function loadLive() {
  fetch('/v1/live', {credentials:'same-origin'}).then(function (r) { if (!r.ok) throw new Error('load failed'); return r.json(); }).then(function (data) {
    liveItems = data.items || [];
    document.getElementById('live-count').textContent = data.total;
    var section = document.getElementById('live-section');
    section.style.display = data.total ? '' : 'none';
    var list=document.getElementById('live-list'), markup=liveItems.map(liveCard).join('');
    if(list._lastMarkup!==markup){list.innerHTML=markup;list._lastMarkup=markup;bindLiveCards();}
    updateLiveOverlay();
  }).catch(function () {});
}
loadOverview(); loadLive(); setInterval(loadOverview, 10000); setInterval(loadLive, 2500);
</script>
<div class="overlay live-overlay" id="live-overlay" role="dialog" aria-modal="true" aria-hidden="true" aria-labelledby="live-overlay-title">
  <div class="overlay-inner"><div class="sheet">
    <div class="sheet-head"><button class="ghost" id="live-close" type="button" aria-label="Close live transcript">"""
        + _icon("back")
        + r"""<span>Back</span></button><div class="title"><h1 id="live-overlay-title">Live transcript</h1><div class="meta" id="live-overlay-meta"></div></div></div>
    <div class="sheet-body live-body">
      <form class="rename on-card" id="live-name-form" style="padding:16px 32px 6px"><label class="sr-only" for="live-name">Meeting name</label><input type="text" id="live-name" maxlength="200" autocomplete="off" required placeholder="Meeting name"><button type="submit">Save name</button><span class="help" id="live-name-status" role="status" style="margin:0"></span></form>
      <div class="live-transcript-scroll" id="live-transcript-scroll" tabindex="0" aria-label="Live transcript text" aria-live="polite"><div id="live-transcript-content"></div></div>
    </div>
  </div></div>
</div>
<script>
document.getElementById('live-close').addEventListener('click', function () { closeLive(false); });
document.getElementById('live-overlay').addEventListener('click', function (event) { if (event.target === this || event.target.classList.contains('overlay-inner')) closeLive(false); });
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
    )
    return _shell("Home", body, token_configured=token_configured, active="home")



# -- meetings (formerly "saved transcriptions") ---------------------------------------------------


def render_transcriptions_page(
    *,
    token_configured: bool,
    initial_session_id: Optional[str] = None,
    initial_view: Optional[str] = None,
    ai_enabled: bool = True,
) -> str:
    """The Meetings page: the shelf (list) and the track sheet (detail overlay).

    ``initial_view="notes"`` opens ``initial_session_id`` straight onto its
    meeting notes (the server knows the notes are finished). ``ai_enabled``
    gates the per-row "Generate" button shown for meetings without notes.

    Everything on the shelf comes from ``GET /v1/sessions`` (search, state
    filter and pagination included) and the sheet from
    ``GET /v1/sessions/{id}``, so the page never has to know how many
    meetings exist. The session strip is drawn from that detail response's
    ``segments`` (``start``/``end`` seconds plus ``track``/``label``).
    """
    initial = json.dumps(initial_session_id)
    initial_view_js = json.dumps(initial_view)
    console_search = (
        '<div class="console-search">'
        + _icon("search")
        + '<label class="sr-only" for="q">Search meetings</label>'
        '<input type="text" id="q" autocomplete="off" enterkeyhint="search" '
        'placeholder="Search names, transcripts or a board number like M-0142" '
        'data-full="Search names, transcripts or a board number like M-0142" data-short="Search or M-0142">'
        "</div>"
    )
    skeleton = "".join(
        '<tr class="skel" aria-hidden="true"><td colspan="8"><span class="skel-bar"></span></td></tr>'
        for _ in range(7)
    )
    body = (
        f"""
<div class="page-head">
  <h1>Meetings</h1>
  <div class="inline-actions">
    <span class="toolbar-note" id="shelf-count" style="margin:0" aria-live="polite"></span>
    <label class="sr-only" for="state">Filter by state</label>
    <select id="state" style="width:auto"><option value="">All states</option><option value="done">Complete</option><option value="running">Running</option><option value="queued">Queued</option><option value="error">Error</option></select>
  </div>
</div>
<p class="toolbar-note">Tick meetings to build notes, retranscribe or delete in bulk. Open a meeting to read its notes and jump around its transcript.</p>
<div class="list-error" id="list-error" role="alert" hidden><span>Could not load the meetings list. It will retry on its own.</span><button type="button" class="secondary" id="list-retry">Try again</button></div>
<div class="shelf"><table>
  <thead><tr><th class="select-cell"><input id="select-all" type="checkbox" aria-label="Select all visible meetings"></th><th class="board-cell"><span class="lane-no">1</span>Board</th><th><span class="lane-no">2</span>Meeting</th><th class="length-cell"><span class="lane-no">3</span>Length</th><th class="transcription-cell"><span class="lane-no">4</span>Transcript</th><th class="notes-cell"><span class="lane-no">5</span>Notes</th><th class="recording-cell"><span class="lane-no">6</span>Recording</th><th class="open-cell"><span class="sr-only">Open</span></th></tr></thead>
  <tbody id="rows" aria-busy="true">{skeleton}</tbody>
</table></div>
<footer class="pager"><button id="more" class="secondary" style="display:none">Load more</button></footer>
<div class="bulk-actions" aria-label="Bulk actions">
  <span class="selection-count" id="selection-count" role="status">Select meetings for bulk actions</span>
  <button class="primary-action" id="bulk-build" disabled>Build meeting notes</button>
  <button class="secondary" id="bulk-retranscribe" disabled>Retranscribe</button>
  <button class="danger" id="bulk-delete-audio" disabled>Delete audio</button>
  <button class="danger" id="bulk-delete" disabled>Delete</button>
</div>

<div class="overlay" id="detail-overlay" role="dialog" aria-modal="true" aria-hidden="true" aria-labelledby="overlay-title">
  <div class="overlay-inner"><div class="sheet" id="sheet" data-view="transcript">
    <div class="sheet-head">
      <button class="ghost" id="close-overlay" type="button">{_icon("back")}<span>Meetings</span></button>
      <span class="board-chip" id="overlay-board" hidden></span>
      <div class="title">
        <h1 id="overlay-title">Meeting</h1>
        <div class="meta" id="overlay-meta"></div>
        <form class="rename" id="rename-form" hidden><label class="sr-only" for="rename-input">Meeting name</label><input type="text" id="rename-input" maxlength="200" autocomplete="off" required><button type="submit" class="primary-action">Save name</button><button type="button" class="ghost" id="rename-cancel">Cancel</button><p class="err" id="rename-error" role="alert"></p></form>
      </div>
      <button class="ghost" id="edit-meeting-name" type="button" aria-expanded="false">{_icon("edit")}<span>Edit name</span></button>
    </div>
    <div class="sheet-body" id="sheet-body">
      <section class="strip-wrap" id="strip-wrap" aria-label="Session strip" hidden>
        <div class="strip-head"><span class="legend">Session strip</span><span class="strip-help">Each block is one spoken segment, drawn to scale. Select one to jump to it.</span><span class="legend" id="strip-total"></span></div>
        <div class="strip" id="session-strip" role="group" aria-label="Session strip, one block per transcript segment. Arrow keys move between blocks, Enter shows the segment in the transcript."></div>
      </section>
      <div class="view-switch">
        <div class="tabs" role="group" aria-label="Meeting views">
          <button id="queue-review" type="button">Build meeting notes</button>
          <button class="secondary active" id="show-transcript" type="button">Transcript</button>
        </div>
        <span class="help" id="review-status" role="status"></span>
      </div>
      <section class="transcript-pane" id="transcript-pane" aria-label="Transcript"><div id="overlay-segments"></div></section>
      <section class="notes-head on-card" id="notes-pane" hidden>
        <div class="notes-toolbar">
          <div class="titling"><h2 id="notes-title">Meeting summary</h2><form class="rename" id="summary-rename-form" hidden><label class="sr-only" for="summary-rename-input">Meeting summary name</label><input type="text" id="summary-rename-input" maxlength="200" autocomplete="off" required><button type="submit">Save name</button><button type="button" class="secondary" id="summary-rename-cancel">Cancel</button><p class="err" id="summary-rename-error" role="alert"></p></form><div class="help" id="notes-meta"></div></div>
          <div class="notes-actions">
            <button class="secondary" id="notes-copy" type="button">{_icon("copy")}<span>Copy</span></button>
            <button class="secondary" id="notes-download" type="button">{_icon("download")}<span>Download .md</span></button>
            <button class="secondary" id="edit-summary-name" type="button" aria-expanded="false">{_icon("edit")}<span>Edit summary name</span></button>
            <button class="secondary" id="notes-retry" type="button">{_icon("refresh")}<span>Regenerate notes</span></button>
          </div>
        </div>
        <div id="notes-state" class="notes-state" role="status"></div>
      </section>
      <section id="notes-document-pane" hidden><article id="notes-document" class="notes-doc" aria-label="Meeting summary"></article></section>
      <details class="extras" id="meeting-extras"><summary>Recording and processing details</summary><div class="extras-body">
        <section aria-labelledby="transcription-progress-heading"><h2 id="transcription-progress-heading" style="font-size:17px">Processing status</h2><ol class="checklist" id="transcription-checklist"></ol></section>
        <div id="audio-players" class="audio-grid"></div>
        <div class="actions">
          <button class="secondary" id="retranscribe">Retranscribe</button>
          <button class="danger" id="delete-audio">Delete audio</button>
          <button class="danger" id="delete-entry">Delete entire entry</button>
        </div>
      </div></details>
    </div>
    <div class="toast" role="status" aria-live="polite"></div>
  </div></div>
</div>
<script>
"""
        + _JS_HELPERS
        + "\n"
        + f"var aiEnabled = {json.dumps(bool(ai_enabled))};"
        + r"""
var listState = {page:1, perPage:50, loaded:0, total:0};
var currentSession = null;
var detailPollTimer = null;
var detailRequest = 0;
var currentReview = null;
var notesPollTimer = null;
var currentMarkdown = '';
var notesRequest = 0;
var detailReturnFocus = null, detailReturnKey = null;
var transcriptExplicit = false;
var pendingNotesDefault = false;
var stripState = {data:null, drawnFor:null, hit:null};
var renameMeeting = null, renameSummary = null;
var sheetEl = document.getElementById('sheet');

function skeletonLines(){return '<div class="notes-loading" aria-hidden="true"><span class="skel-bar" style="width:70%"></span><span class="skel-bar"></span><span class="skel-bar" style="width:85%"></span><span class="skel-bar" style="width:55%"></span></div><span class="sr-only">Loading…</span>';}
function notesBadge(row) {
  var status=String((row.review||{}).status||'none').toLowerCase();
  if(status==='done')return '<span class="badge done">'+tick('done')+'Notes ready</span>';
  if(status==='running'||status==='queued')return '<span class="badge running">'+tick('run')+'Building notes</span>';
  if(status==='error')return '<span class="badge error">'+tick('error')+'Notes need attention</span>';
  var generate=(aiEnabled&&processingState(row).key==='complete')?'<button type="button" class="secondary notes-generate" data-generate="'+escapeHtml(row.session_id)+'" aria-label="Generate meeting notes for '+escapeHtml(row.name||row.session_id)+'">Generate</button>':'';
  return '<span class="notes-none badge none">'+tick('none')+'Not created</span>'+generate;
}
function generateNotes(btn) {
  if (btn.disabled) return;
  btn.disabled = true; btn.textContent = 'Queuing…';
  fetch('/v1/sessions/'+encodeURIComponent(btn.dataset.generate)+'/review', {method:'POST', credentials:'same-origin'})
    .then(function(r) { if (!r.ok) throw new Error('Unable to queue meeting notes'); return r.json(); })
    .then(function() { var cell=btn.closest('.notes-cell'); if(cell)cell.innerHTML=notesBadge({review:{status:'queued'}}); return loadRows(true); })
    .catch(function(e) { btn.disabled = false; btn.textContent = 'Generate'; notify(e.message,'error'); });
}
function lengthBar(row) {
  var d=Math.max(0,Number(row.duration_sec)||0);
  return '<div class="lenbar" data-d="'+d+'" style="--d:'+d+'"><span class="gauge" aria-hidden="true"><i></i></span><span class="len-time">'+(d?fmtDuration(d):'—')+'</span></div>';
}
function tableRow(row) {
  var name=escapeHtml(row.name||row.session_id),status=String((row.review||{}).status||'none').toLowerCase();
  return '<tr aria-label="Open '+name+'" data-id="'+escapeHtml(row.session_id)+'" class="'+(status==='done'?'notes-ready':(status==='queued'||status==='running'?'notes-pending':''))+'"><td class="select-cell"><input class="row-select" type="checkbox" value="'+escapeHtml(row.session_id)+'" aria-label="Select '+name+'"></td><td class="board-cell">'+escapeHtml(row.board||'')+'</td><td class="meeting-cell"><strong>'+name+'</strong><small><span class="sub-board">'+escapeHtml(row.board||'')+'</span><span class="sub-date">'+fmtDate(row.created,true)+'</span> · '+escapeHtml(row.device||row.platform||'Unknown device')+'</small></td><td class="length-cell">'+lengthBar(row)+'</td><td class="transcription-cell">'+processingBadge(row)+'</td><td class="notes-cell">'+notesBadge(row)+'</td><td class="recording-cell">'+(row.has_audio?fmtBytes(row.audio_bytes):'No audio')+'</td><td class="open-cell"><button type="button" class="row-open" aria-label="Open '+name+'"><span class="lbl">Open</span>'+icon('open',14)+'</button></td></tr>';
}
function emptyRows() {
  var q=document.getElementById('q').value.trim(), st=document.getElementById('state').value;
  if(q||st)return '<tr class="state-row"><td colspan="8" class="empty"><p><strong>No meetings found.</strong> Nothing on the shelf matches '+(q?'&quot;'+escapeHtml(q)+'&quot;':'that state')+'.</p><button type="button" class="secondary" id="clear-filters">Clear search and filter</button></td></tr>';
  return '<tr class="state-row"><td colspan="8"><div class="empty-teach"><h2>No meetings on the shelf yet</h2><p>Each recording you make becomes a meeting here, with its transcript and notes.</p><ol><li>Install the Windows client and record a call, or</li><li>upload an audio file from Home.</li></ol><div class="row"><a class="btn" href="/install">Install the Windows client</a><a class="btn secondary" href="/">Upload a recording</a></div></div></td></tr>';
}
function selectedIds() { return Array.from(document.querySelectorAll('.row-select:checked')).map(function(el) { return el.value; }); }
function updateSelection() {
  var ids = selectedIds(), disabled = !ids.length;
  document.getElementById('selection-count').textContent = ids.length ? ids.length + ' selected' : 'Select meetings for bulk actions';
  document.querySelector('.bulk-actions').classList.toggle('has-selection',!!ids.length);
  ['bulk-build','bulk-retranscribe','bulk-delete-audio','bulk-delete'].forEach(function(id) { document.getElementById(id).disabled = disabled; });
  var all = document.querySelectorAll('.row-select'), master = document.getElementById('select-all');
  master.checked = !!all.length && ids.length === all.length;
  master.indeterminate = ids.length > 0 && ids.length < all.length;
}
function runBulk(path, method, confirmText) {
  var ids = selectedIds(); if (!ids.length) return;
  if (confirmText && !confirm(confirmText)) return;
  var buttons = ['bulk-build','bulk-retranscribe','bulk-delete-audio','bulk-delete']; buttons.forEach(function(id) { document.getElementById(id).disabled = true; });
  Promise.allSettled(ids.map(function(id) { return fetch('/v1/sessions/'+encodeURIComponent(id)+path, {method:method, credentials:'same-origin'}).then(function(r) { if (!r.ok) throw new Error('Action failed for '+id); return r; }); }))
    .then(function(results) { return loadRows(true).then(function(refreshed) { var failed=results.filter(function(result){return result.status==='rejected';}); if(failed.length)notify(failed.length+' of '+ids.length+' actions failed.','error'); else notify('Done for '+ids.length+' meeting'+(ids.length===1?'':'s')+'.'); if(!refreshed)notify('Could not refresh the meetings list. Please try again.','error'); }); });
}
function scaleBars() {
  var rows=document.getElementById('rows'), max=0;
  rows.querySelectorAll('.lenbar').forEach(function(bar){max=Math.max(max,Number(bar.dataset.d)||0);});
  rows.style.setProperty('--max',Math.max(1,max));
}
function loadRows(reset) {
  var preservedSelection = reset ? selectedIds() : [];
  var previousLoaded = listState.loaded, hadRows = previousLoaded > 0;
  if (reset) { listState.page=1; listState.loaded=0; }
  var url='/v1/sessions?page='+listState.page+'&per_page='+listState.perPage;
  var q=document.getElementById('q').value.trim(), state=document.getElementById('state').value;
  if(q) url+='&q='+encodeURIComponent(q); if(state) url+='&state='+encodeURIComponent(state);
  return fetch(url,{credentials:'same-origin'}).then(r=>{if(r.status===401||r.status===403){window.location='/login';throw new Error('Signed out');}if(!r.ok)throw new Error('Unable to load meetings');return r.json();}).then(data=>{
    listState.total=data.total;
    var rows=document.getElementById('rows');
    document.getElementById('list-error').hidden=true;
    if(!hadRows && reset)rows.innerHTML='';
    // Keep existing row nodes during polling. Clearing this tbody every five
    // seconds made the entire table visibly flash, especially on slower PCs.
    if (reset && hadRows) {
      var fresh = {}; data.items.forEach(function(item) { fresh[item.session_id] = item; });
      var focusedCheckbox = document.activeElement && document.activeElement.classList.contains('row-select') ? document.activeElement.value : null;
      Array.from(rows.querySelectorAll('tr[data-id]')).forEach(function(oldRow) {
        var id = oldRow.dataset.id, item = fresh[id];
        if (item) { var scratch=document.createElement('tbody');scratch.innerHTML=tableRow(item);var replacement=scratch.firstElementChild;if(oldRow.innerHTML!==replacement.innerHTML)oldRow.replaceChildren.apply(oldRow,Array.from(replacement.childNodes));oldRow.className=replacement.className;oldRow.setAttribute('aria-label',replacement.getAttribute('aria-label'));delete fresh[id]; }
        else oldRow.remove();
      });
      var additions = Object.keys(fresh).map(function(id) { return tableRow(fresh[id]); }).join('');
      var emptyRow=rows.querySelector('tr:not([data-id])'); if(emptyRow&&additions)emptyRow.remove();
      if (additions) rows.insertAdjacentHTML('afterbegin', additions);
      if(!rows.querySelector('tr[data-id]'))rows.innerHTML=emptyRows();
    } else if (!data.items.length && !listState.loaded) rows.innerHTML=emptyRows();
    else rows.insertAdjacentHTML('beforeend',data.items.map(tableRow).join(''));
    if (preservedSelection.length) document.querySelectorAll('.row-select').forEach(function(box) { box.checked = preservedSelection.indexOf(box.value) >= 0; });
    if (focusedCheckbox) { var focusedRow=Array.from(rows.querySelectorAll('tr[data-id]')).find(function(row){return row.dataset.id===focusedCheckbox;});if(focusedRow&&document.activeElement!==focusedRow.querySelector('.row-select'))focusedRow.querySelector('.row-select').focus(); }
    listState.loaded+=data.items.length;
    document.getElementById('more').style.display=listState.loaded<listState.total?'':'none';
    document.getElementById('shelf-count').textContent=data.total+(data.total===1?' meeting':' meetings');
    rows.setAttribute('aria-busy','false');
    scaleBars();
    updateSelection();
    return true;
  }).catch(function(){
    if(reset)listState.loaded=previousLoaded;
    var rows=document.getElementById('rows');
    if(!rows.querySelector('tr[data-id]'))rows.innerHTML='<tr class="state-row"><td colspan="8" class="empty">The meetings list could not be loaded.</td></tr>';
    rows.setAttribute('aria-busy','false');
    document.getElementById('list-error').hidden=false;
    updateSelection();
    return false;
  });
}
function renderTranscript(segments) {
  var root=document.getElementById('overlay-segments');
  var markup=(!segments||!segments.length)?'<div class="empty">No final transcript yet.</div>':segments.map(function(seg,i){
    if(seg.in_gap)return '<div class="gap-marker" id="seg-'+i+'">Audio lost from '+fmtDuration(seg.start)+' to '+fmtDuration(seg.end)+'</div>';
    var mic=seg.track==='mic';
    return '<div class="segment lane-'+(mic?'mic':'system')+'" id="seg-'+i+'" data-seg="'+i+'" tabindex="-1"><span class="ts">'+fmtDuration(seg.start)+'</span><span class="label"><i class="sw'+(mic?'':' them')+'"></i>'+escapeHtml(seg.label)+'</span><div class="text'+(seg.approximate?' approximate':'')+'">'+escapeHtml(seg.text)+'</div></div>';
  }).join('');
  if(root._lastMarkup!==markup){root.innerHTML=markup;root._lastMarkup=markup;if(stripState.hit!=null){var again=document.getElementById('seg-'+stripState.hit);if(again)again.classList.add('hit');}}
}
/* Session strip: one block per timed transcript segment, positioned by its
   start and length on a shared time axis (from GET /v1/sessions/{id}
   segments: start, end, track, label, in_gap; total = meta.duration_sec or
   the last segment end). Hidden when no segment carries usable timing. */
function fmtAxis(t){
  if(t===0)return '0';
  if(t%3600===0)return (t/3600)+'h';
  if(t%60===0)return t<3600?(t/60)+'m':Math.floor(t/3600)+'h'+((t%3600)/60)+'m';
  return t<60?t+'s':Math.floor(t/60)+'m'+(t%60)+'s';
}
function pruneAxis(){
  // Never overlap: keep a label only if it clears the previous kept label by 8px.
  var last=-Infinity, spans=document.querySelectorAll('#session-strip .axis span');
  spans.forEach(function(span){span.hidden=false;});
  spans.forEach(function(span){var r=span.getBoundingClientRect();if(r.left<last+8){span.hidden=true;}else{last=r.right;}});
}
function segLane(seg){return seg.label||(seg.track==='mic'?'You':'Them');}
function niceStep(total,maxTicks){var steps=[15,30,60,120,300,600,900,1200,1800,3600,7200,14400,28800];for(var i=0;i<steps.length;i++){if(total/steps[i]<=maxTicks)return steps[i];}return steps[steps.length-1];}
function validSpan(s){return s&&typeof s.start==='number'&&isFinite(s.start)&&typeof s.end==='number'&&isFinite(s.end)&&s.end>s.start&&s.start>=0;}
function renderStrip(data) {
  var wrap=document.getElementById('strip-wrap'), root=document.getElementById('session-strip');
  var segments=(data&&data.segments)||[], meta=(data&&data.meta)||{};
  stripState.data=data;
  var blocks=[], gaps=[], maxEnd=0;
  segments.forEach(function(s,i){if(!validSpan(s))return;maxEnd=Math.max(maxEnd,s.end);if(s.in_gap)gaps.push(s);else blocks.push({i:i,s:s});});
  var total=Math.max(Number(meta.duration_sec)||0,maxEnd);
  if(!blocks.length||total<=0){wrap.hidden=true;root.innerHTML='';root._lastMarkup=null;return;}
  wrap.hidden=false;
  var lanes=[],byLabel={};
  blocks.forEach(function(b){var mic=b.s.track==='mic',label=segLane(b.s);if(!byLabel[label]){byLabel[label]={label:label,mic:mic,items:[]};lanes.push(byLabel[label]);}byLabel[label].items.push(b);});
  lanes.sort(function(a,b){return (b.mic?1:0)-(a.mic?1:0);});
  if(lanes.length>6){var you={label:'You',mic:true,items:[]},them={label:'Them',mic:false,items:[]};lanes.forEach(function(l){(l.mic?you:them).items=(l.mic?you:them).items.concat(l.items);});lanes=[you,them].filter(function(l){return l.items.length;});}
  var laneW=parseInt(getComputedStyle(root).getPropertyValue('--lane-w'),10)||96;
  var plotW=root.getBoundingClientRect().width-laneW, maxTicks=Math.max(2,Math.floor(plotW/72)), step=niceStep(total,maxTicks);
  function pct(v){return (v/total*100).toFixed(3)+'%';}
  var gapMarkup=gaps.map(function(g){return '<span class="gap-band" style="left:'+pct(g.start)+';width:'+pct(g.end-g.start)+'" title="Audio lost from '+fmtDuration(g.start)+' to '+fmtDuration(g.end)+'"></span>';}).join('');
  var first=true;
  var markup=lanes.map(function(l){
    var blks=l.items.map(function(b){
      var s=b.s, tab=first?0:-1;first=false;
      return '<button type="button" class="blk'+(l.mic?'':' them')+(stripState.hit===b.i?' on':'')+'" data-seg="'+b.i+'" data-start="'+s.start+'" tabindex="'+tab+'" style="left:'+pct(s.start)+';width:'+pct(s.end-s.start)+'" aria-label="'+escapeHtml(l.label)+', '+fmtDuration(s.start)+' to '+fmtDuration(s.end)+'. Show in transcript."></button>';
    }).join('');
    return '<div class="lane"><div class="lane-label" title="'+escapeHtml(l.label)+'"><i class="sw'+(l.mic?'':' them')+'"></i><span>'+escapeHtml(l.label)+'</span></div><div class="lane-track">'+gapMarkup+blks+'</div></div>';
  }).join('');
  var ticks='';for(var t=0;t<=total;t+=step){ticks+='<span'+(t/total>0.96?' class="end"':'')+' style="left:'+pct(t)+'">'+fmtAxis(t)+'</span>';}
  markup+='<div class="axis" aria-hidden="true">'+ticks+'</div>';
  var draw=stripState.drawnFor!==currentSession;
  if(root._lastMarkup!==markup){
    root.style.setProperty('--total',total);root.style.setProperty('--step',step);
    root.innerHTML=markup;root._lastMarkup=markup;
    if(draw){root.classList.add('strip-draw');setTimeout(function(){root.classList.remove('strip-draw');},900);}
  }
  stripState.drawnFor=currentSession;
  pruneAxis();
  document.getElementById('strip-total').textContent=fmtDuration(total)+' total';
}
function jumpToSegment(index) {
  transcriptExplicit=true;notesRequest++;if(notesPollTimer)clearTimeout(notesPollTimer);notesPollTimer=null;
  setDetailView('transcript');
  document.querySelectorAll('.segment.hit').forEach(function(el){el.classList.remove('hit');});
  document.querySelectorAll('.blk.on').forEach(function(el){el.classList.remove('on');});
  stripState.hit=index;
  var blk=document.querySelector('.blk[data-seg="'+index+'"]');if(blk)blk.classList.add('on');
  var el=document.getElementById('seg-'+index);if(!el)return;
  el.classList.add('hit');
  var reduce=window.matchMedia&&window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  el.scrollIntoView({block:'center',behavior:reduce?'auto':'smooth'});
  el.focus({preventScroll:true});
}
function onStripKey(e) {
  var blk=e.target.closest&&e.target.closest('.blk');if(!blk)return;
  var all=Array.from(document.querySelectorAll('#session-strip .blk')).sort(function(a,b){return (Number(a.dataset.start)-Number(b.dataset.start))||(all_index(a)-all_index(b));});
  function all_index(el){return Array.prototype.indexOf.call(el.parentNode.children,el);}
  var i=all.indexOf(blk),next=null;
  if(e.key==='ArrowRight')next=all[Math.min(all.length-1,i+1)];
  else if(e.key==='ArrowLeft')next=all[Math.max(0,i-1)];
  else if(e.key==='Home')next=all[0];
  else if(e.key==='End')next=all[all.length-1];
  else if(e.key==='ArrowUp'||e.key==='ArrowDown'){
    var lanes=Array.from(document.querySelectorAll('#session-strip .lane-track')),li=lanes.indexOf(blk.parentNode),target=lanes[li+(e.key==='ArrowDown'?1:-1)];
    if(target){var start=Number(blk.dataset.start),best=null,dist=Infinity;target.querySelectorAll('.blk').forEach(function(c){var d=Math.abs(Number(c.dataset.start)-start);if(d<dist){dist=d;best=c;}});next=best;}
  } else return;
  e.preventDefault();
  if(next&&next!==blk){blk.tabIndex=-1;next.tabIndex=0;next.focus();}
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
  var uploadDetail=uploadError?('Failed'+(upload.error?': '+escapeHtml(upload.error):'')):(uploadComplete?'Complete':(uploadState==='pending'?'Pending end of meeting':(uploadProgress==null?'Uploading':'Uploading '+percent(uploadProgress))));
  var transcribeQueued=uploadComplete&&(transcribeState==='queued'||transcribeState==='pending');
  var transcribeDetail=transcribed?'Complete':(transcribeError?'Needs attention':(transcribing?percent(transcribeProgress):(transcribeQueued?'Queued':'Waiting')));
  var c1=uploadComplete ? 'complete' : (uploadError || uploadState==='uploading' ? 'active' : ''), c2=transcribeError ? 'active' : (transcribing || transcribeQueued ? 'active' : (transcribed ? 'complete' : '')), c3=transcribed ? 'complete' : '';
  document.getElementById('transcription-checklist').innerHTML = '<li class="' + c1 + '">' + tick(uploadComplete?'done':(uploadError?'error':(uploadState==='uploading'?'run':'none'))) + '<span>Upload audio</span><span class="detail">' + uploadDetail + '</span></li>' +
    '<li class="' + c2 + '">' + tick(transcribed?'done':(transcribeError?'error':(transcribing?'run':(transcribeQueued?'queue':'none')))) + '<span>Transcribe recording</span><span class="detail">' + transcribeDetail + '</span></li>' +
    '<li class="' + c3 + '">' + tick(transcribed?'done':'none') + '<span>Transcript ready</span><span class="detail">' + (transcribed ? 'Complete' : 'Pending') + '</span></li>';
}
function openSession(id, hintView) {
  if (detailPollTimer) clearTimeout(detailPollTimer);
  detailPollTimer=null;
  var request=++detailRequest;
  var changingSession=currentSession!==id;
  currentSession=id;
  if(changingSession){
    detailReturnFocus=document.activeElement;detailReturnKey=(function(el){var row=el&&el.closest?el.closest('tr[data-id]'):null;return row?{id:row.dataset.id,open:el.classList.contains('row-open')}:null;})(detailReturnFocus);notesRequest++;currentReview=null;currentMarkdown='';transcriptExplicit=false;pendingNotesDefault=hintView==='notes';
    if(notesPollTimer)clearTimeout(notesPollTimer);notesPollTimer=null;
    if(renameMeeting)renameMeeting.close(false);if(renameSummary)renameSummary.close(false);
    stripState.drawnFor=null;stripState.hit=null;stripState.data=null;
    document.getElementById('overlay-title').textContent='Loading meeting…';document.getElementById('overlay-meta').textContent='';document.getElementById('overlay-board').hidden=true;
    var segRoot=document.getElementById('overlay-segments');segRoot.innerHTML=skeletonLines();segRoot._lastMarkup=null;
    document.getElementById('strip-wrap').hidden=true;
    document.getElementById('notes-title').textContent='Meeting summary';document.getElementById('notes-meta').textContent='';document.getElementById('notes-state').textContent='';
    document.getElementById('notes-document').innerHTML=skeletonLines();
    document.getElementById('notes-download').disabled=true;document.getElementById('notes-copy').disabled=true;document.getElementById('edit-summary-name').disabled=true;
    document.getElementById('queue-review').disabled=true;document.getElementById('queue-review').textContent='Build meeting notes';document.getElementById('review-status').textContent='';
    document.getElementById('meeting-extras').open=false;
    setDetailView(hintView==='notes'?'notes':'transcript');
  }
  var overlay=document.getElementById('detail-overlay');overlay.classList.add('open');overlay.setAttribute('aria-hidden','false');document.body.style.overflow='hidden';
  if(changingSession)document.getElementById('close-overlay').focus();
  history.replaceState(null,'','/sessions/'+encodeURIComponent(id));
  fetch('/v1/sessions/'+encodeURIComponent(id),{credentials:'same-origin'}).then(r=>{if(!r.ok)throw new Error('Unable to load transcription');return r.json();}).then(data=>{
    if (currentSession !== id || request !== detailRequest) return;
    document.getElementById('transcription-progress-heading').textContent='Processing status';
    var meta=data.meta||{}; document.getElementById('overlay-title').textContent=meta.name||id;
    var chip=document.getElementById('overlay-board');chip.textContent=data.board||'';chip.hidden=!data.board;
    document.getElementById('overlay-meta').textContent=fmtDate(meta.created)+' · '+(meta.device||meta.platform||'Unknown device')+' · '+fmtDuration(meta.duration_sec);
    var players=[]; var tracks=meta.tracks||{};
    ['mic','system'].forEach(track=>{if(data.has_audio && tracks[track]) players.push('<div class="audio-card"><strong>'+icon(track==='mic'?'mic':'speaker',16)+(track==='mic'?'You · microphone':'Them · system audio')+'</strong><audio controls preload="metadata" src="/sessions/'+encodeURIComponent(id)+'/audio/'+track+'"></audio></div>');});
    var audioRoot=document.getElementById('audio-players'),audioMarkup=players.join('') || '<div class="empty">Audio has been removed.</div>';
    if(audioRoot._lastMarkup!==audioMarkup){audioRoot.innerHTML=audioMarkup;audioRoot._lastMarkup=audioMarkup;}
    document.getElementById('retranscribe').disabled=!data.has_audio; document.getElementById('delete-audio').disabled=!data.has_audio;
    renderProcessingChecklist(data);
    var review=data.review||data.review_status||{}; currentReview=review.review_id||review.id||null;
    var reviewStatus=String(review.status||'none').toLowerCase();
    document.getElementById('review-status').textContent=reviewStatus==='done'?'Notes ready':(reviewStatus==='queued'||reviewStatus==='running'?'Building notes…':(reviewStatus==='error'?'Notes need attention':''));
    document.getElementById('queue-review').disabled=false;
    document.getElementById('queue-review').textContent=currentReview?'Meeting notes':'Build meeting notes';
    renderTranscript(data.segments);
    renderStrip(data);
    var state=data.pipeline||{}, upload=state.upload||{}, transcription=state.transcription||{};
    if(changingSession)document.getElementById('meeting-extras').open=!!(upload.state==='pending'||upload.state==='uploading'||transcription.state==='pending'||transcription.state==='transcribing');
    if(currentReview&&reviewStatus==='done'&&!transcriptExplicit&&(pendingNotesDefault||document.getElementById('transcript-pane').hidden===false)){pendingNotesDefault=false;showNotes();}
    else if(pendingNotesDefault){pendingNotesDefault=false;if(!transcriptExplicit)setDetailView('transcript');}
    if (currentSession === id && (upload.state==='pending' || upload.state==='uploading' || transcription.state==='pending' || transcription.state==='transcribing' || ((reviewStatus==='queued'||reviewStatus==='running')&&!document.getElementById('transcript-pane').hidden))) detailPollTimer=setTimeout(function(){if(currentSession===id)openSession(id);},3000);
    else if (currentSession === id) loadRows(true);
  }).catch(function(){if(currentSession!==id||request!==detailRequest)return;document.getElementById('transcription-progress-heading').textContent='Processing status · reconnecting…';detailPollTimer=setTimeout(function(){if(currentSession===id)openSession(id);},3000);});
}
function setDetailView(view){var notes=view==='notes';sheetEl.dataset.view=notes?'notes':'transcript';document.getElementById('transcript-pane').hidden=notes;document.getElementById('notes-pane').hidden=!notes;document.getElementById('notes-document-pane').hidden=!notes;document.getElementById('queue-review').classList.toggle('active',notes);document.getElementById('show-transcript').classList.toggle('active',!notes);document.getElementById('queue-review').setAttribute('aria-pressed',String(notes));document.getElementById('show-transcript').setAttribute('aria-pressed',String(!notes));}
function restoreFocus(){
  // Polling rewrites row cells, so the element focused before opening may be gone: find its replacement.
  if(detailReturnFocus&&detailReturnFocus.isConnected){detailReturnFocus.focus();return;}
  if(!detailReturnKey)return;
  var row=Array.from(document.querySelectorAll('#rows tr[data-id]')).find(function(r){return r.dataset.id===detailReturnKey.id;});
  var target=row&&(detailReturnKey.open?row.querySelector('.row-open'):row);
  if(target)target.focus();
}
function closeOverlay(){currentSession=null;currentReview=null;detailRequest++;notesRequest++;if(detailPollTimer)clearTimeout(detailPollTimer);if(notesPollTimer)clearTimeout(notesPollTimer);detailPollTimer=null;notesPollTimer=null;if(renameMeeting)renameMeeting.close(false);if(renameSummary)renameSummary.close(false);stripState.drawnFor=null;stripState.hit=null;var overlay=document.getElementById('detail-overlay');overlay.classList.remove('open');overlay.setAttribute('aria-hidden','true');document.body.style.overflow='';history.replaceState(null,'','/meetings');restoreFocus();detailReturnFocus=null;detailReturnKey=null;}
function noteValues(value){return Array.isArray(value)?value:(value==null?[]:[value]);}
function noteText(value){if(value==null)return '';if(typeof value!=='object')return String(value);var text=String(value.action||value.task||value.text||value.title||value.point||value.decision||value.question||value.risk||value.step||'');if(value.owner)text+=' — Owner: '+value.owner;if(value.due_date||value.due)text+=' — Due: '+(value.due_date||value.due);if(value.context)text+=' — '+value.context;return text;}
function personText(value){if(value!=null&&typeof value==='object')return String(value.name||value.email||noteText(value));return value==null?'':String(value);}
function buildMarkdown(note,title){var sections=[['Summary',note.summary||note.overview],['Meeting notes',note.polished_meeting_notes||note.polished_notes||note.meeting_notes||note.narrative||note.notes],['Key points',note.key_points||note.keyPoints],['Decisions',note.decisions],['Action items',note.action_items||note.actionItems||note.actions],['Open questions',note.open_questions||note.openQuestions||note.questions],['Risks',note.risks],['Next steps',note.next_steps||note.nextSteps],['Participants',note.participants||note.attendees]],filled=sections.filter(function(s){return noteValues(s[1]).map(noteText).some(function(v){return v.trim();});}),empty=sections.filter(function(s){return !noteValues(s[1]).map(noteText).some(function(v){return v.trim();});});function section(s){var values=noteValues(s[1]).map(noteText).filter(function(v){return v.trim();}),prose=s[0]==='Summary'||s[0]==='Meeting notes';return '## '+s[0]+'\n'+(values.length?(prose?values.join('\n\n'):values.map(function(v){return '- '+v;}).join('\n')):'')+'\n';}return ('# '+title+'\n\n'+filled.map(section).join('\n')+(empty.length?'\n---\n\n'+empty.map(section).join('\n'):'' )).trim()+'\n';}
function renderNotes(data){
  var note=data.note||data.meeting_note||data, meta=note.meta||note, title=note.title||meta.title||meta.name||'Meeting summary';
  var status=String(note.status||data.status||'').toLowerCase();
  document.getElementById('notes-title').textContent=title; document.getElementById('notes-meta').textContent=[fmtDate(meta.created||meta.meeting_time||meta.started),meta.device||meta.platform].filter(Boolean).join(' · '); document.getElementById('notes-state').textContent=status==='done'?'':(status==='queued'||status==='running'?'Building meeting notes…':(status==='error'?'Notes need attention. Use Regenerate notes to try again.':''));
  if((status==='queued'||status==='running')&&!note.summary){document.getElementById('notes-document').innerHTML='<p class="notes-empty-state">The summary is being prepared. You can return to the transcript while it runs.</p>'+skeletonLines();currentMarkdown='';document.getElementById('notes-download').disabled=true;document.getElementById('notes-copy').disabled=true;document.getElementById('edit-summary-name').disabled=true;return;}
  var S={summary:note.summary||note.overview,body:note.polished_meeting_notes||note.polished_notes||note.meeting_notes||note.narrative||note.notes,points:note.key_points||note.keyPoints,decisions:note.decisions,actions:note.action_items||note.actionItems||note.actions,questions:note.open_questions||note.openQuestions||note.questions,risks:note.risks,steps:note.next_steps||note.nextSteps,people:note.participants||note.attendees};
  function has(value,mapper){return noteValues(value).map(mapper||noteText).some(function(v){return String(v).trim();});}
  function list(value,mapper){var values=noteValues(value).map(mapper||noteText).filter(function(v){return String(v).trim();});return values.length===1?'<p>'+escapeHtml(values[0])+'</p>':'<ul>'+values.map(function(v){return '<li>'+escapeHtml(v)+'</li>';}).join('')+'</ul>';}
  function actions(value){return '<div class="action-list">'+noteValues(value).map(function(raw){var action=typeof raw==='object'&&raw?raw:{action:raw},label=action.action||action.task||action.text||'',chips=[];if(!label)return '';if(action.owner)chips.push('<span class="chip"><b>Owner</b> '+escapeHtml(action.owner)+'</span>');if(action.due_date||action.due)chips.push('<span class="chip"><b>Due</b> '+escapeHtml(action.due_date||action.due)+'</span>');return '<div class="action-item"><span class="box" aria-hidden="true"></span><div class="what">'+escapeHtml(label)+'</div>'+(chips.length?'<div class="chips">'+chips.join('')+'</div>':'')+(action.context?'<div class="context">'+escapeHtml(action.context)+'</div>':'')+'</div>';}).join('')+'</div>';}
  var main=[['Summary',S.summary,'lead',noteText],['Decisions',S.decisions,'',noteText],['Action items',S.actions,'actions',noteText],['Key points',S.points,'',noteText],['Meeting notes',S.body,'',noteText]];
  var rail=[['Participants',S.people,'',personText],['Open questions',S.questions,'',noteText],['Risks',S.risks,'',noteText],['Next steps',S.steps,'',noteText]];
  function section(s){var cls='notes-section'+(s[2]==='lead'?' lead':'');return '<section class="'+cls+'"><h3>'+escapeHtml(s[0])+'</h3>'+(s[2]==='actions'?actions(s[1]):list(s[1],s[3]))+'</section>';}
  var missing=main.concat(rail).filter(function(s){return !has(s[1],s[3]);}).map(function(s){return s[0];});
  var mainMarkup=main.filter(function(s){return has(s[1],s[3]);}).map(section).join(''), railMarkup=rail.filter(function(s){return has(s[1],s[3]);}).map(section).join('');
  var doc=document.getElementById('notes-document');
  doc.style.gridTemplateColumns=railMarkup?'':'minmax(0,72ch)';
  doc.innerHTML='<div class="notes-main">'+(mainMarkup||'<p class="notes-empty-state">No notes were recorded for this meeting yet.</p>')+(missing.length?'<p class="notes-none-line">Nothing recorded for: '+escapeHtml(missing.join(', '))+'.</p>':'')+'</div>'+(railMarkup?'<aside class="notes-rail" aria-label="Participants and follow-ups">'+railMarkup+'</aside>':'');
  currentMarkdown=buildMarkdown(note,title);
  document.getElementById('notes-download').disabled=!note.summary;
  document.getElementById('notes-copy').disabled=!note.summary;
  document.getElementById('edit-summary-name').disabled=!note.summary;
}
function showNotes(refresh){
  if(!currentReview){var session=currentSession, request=++notesRequest;document.getElementById('review-status').textContent='Starting notes…';action('/review').then(function(result){if(session!==currentSession||request!==notesRequest)return;currentReview=result.review_id||result.id||null;document.getElementById('review-status').textContent='Building notes…';document.getElementById('queue-review').textContent='Meeting notes';showNotes();loadRows(true);}).catch(function(e){if(session===currentSession&&request===notesRequest){document.getElementById('review-status').textContent='';notify(e.message,'error');}}); return; }
  transcriptExplicit=false;setDetailView('notes');
  if(!refresh){currentMarkdown='';document.getElementById('notes-download').disabled=true;document.getElementById('notes-copy').disabled=true;document.getElementById('edit-summary-name').disabled=true;document.getElementById('notes-document').innerHTML=skeletonLines();}
  var session=currentSession, review=currentReview, request=++notesRequest;
  fetch('/v1/meeting-notes/'+encodeURIComponent(review),{credentials:'same-origin'}).then(function(r){if(!r.ok)throw new Error('Unable to load meeting notes');return r.json();}).then(function(data){if(session!==currentSession||review!==currentReview||request!==notesRequest)return;renderNotes(data);var status=String((data.note||data).status||data.status||'').toLowerCase();document.getElementById('review-status').textContent=status==='done'?'Notes ready':(status==='error'?'Notes need attention':'Building notes…');if(status==='done'||status==='error')loadRows(true);if((status==='queued'||status==='running')&&currentReview){if(notesPollTimer)clearTimeout(notesPollTimer);notesPollTimer=setTimeout(function(){if(session===currentSession&&review===currentReview)showNotes(true);},3000);}}).catch(function(e){if(session!==currentSession||review!==currentReview||request!==notesRequest)return;document.getElementById('notes-state').textContent=e.message+' · retrying…';notesPollTimer=setTimeout(function(){if(session===currentSession&&review===currentReview)showNotes(true);},3000);});
}
function saveName(url, value, field){return fetch(url,{method:'PATCH',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({[field]:value})}).then(function(r){if(!r.ok)throw new Error('Unable to save name');return r.json();});}
function action(path,method,confirmText){if(!currentSession)return;if(confirmText&&!confirm(confirmText))return;return fetch('/v1/sessions/'+encodeURIComponent(currentSession)+path,{method:method||'POST',credentials:'same-origin'}).then(async r=>{if(!r.ok)throw new Error((await r.json()).detail||'Request failed');return r.json();});}
/* Inline rename: the heading swaps for a small form; errors show beside it. */
function bindRename(o){
  function openForm(){o.error.textContent='';o.input.value=o.current();o.heading.hidden=true;o.form.hidden=false;o.btn.setAttribute('aria-expanded','true');o.input.focus();o.input.select();}
  function closeForm(refocus){o.form.hidden=true;o.heading.hidden=false;o.btn.setAttribute('aria-expanded','false');if(refocus)o.btn.focus();}
  o.btn.onclick=function(){if(o.form.hidden)openForm();else closeForm(true);};
  o.cancel.onclick=function(){closeForm(true);};
  o.input.addEventListener('keydown',function(e){if(e.key==='Escape'){e.stopPropagation();e.preventDefault();closeForm(true);}});
  o.form.addEventListener('submit',function(e){e.preventDefault();var name=o.input.value.trim();if(!name){o.error.textContent='Enter a name to save.';return;}var submit=o.form.querySelector('[type=submit]');submit.disabled=true;o.error.textContent='';Promise.resolve(o.save(name)).then(function(){closeForm(true);}).catch(function(err){o.error.textContent=(err&&err.message)||'Could not save the name. Try again.';}).finally(function(){submit.disabled=false;});});
  return {close:closeForm};
}
renameMeeting=bindRename({btn:document.getElementById('edit-meeting-name'),form:document.getElementById('rename-form'),input:document.getElementById('rename-input'),cancel:document.getElementById('rename-cancel'),error:document.getElementById('rename-error'),heading:document.getElementById('overlay-title'),current:function(){return document.getElementById('overlay-title').textContent;},save:function(name){var session=currentSession;if(!session)return Promise.reject(new Error('No meeting is open.'));return saveName('/v1/sessions/'+encodeURIComponent(session),name,'name').then(function(){if(session===currentSession)document.getElementById('overlay-title').textContent=name;loadRows(true);});}});
renameSummary=bindRename({btn:document.getElementById('edit-summary-name'),form:document.getElementById('summary-rename-form'),input:document.getElementById('summary-rename-input'),cancel:document.getElementById('summary-rename-cancel'),error:document.getElementById('summary-rename-error'),heading:document.getElementById('notes-title'),current:function(){return document.getElementById('notes-title').textContent;},save:function(name){var session=currentSession,review=currentReview;if(!review)return Promise.reject(new Error('No summary to rename yet.'));return saveName('/v1/meeting-notes/'+encodeURIComponent(review),name,'title').then(function(){if(session!==currentSession||review!==currentReview)return;document.getElementById('notes-title').textContent=name;showNotes(true);});}});
document.getElementById('rows').addEventListener('click',e=>{if(e.target.closest('#clear-filters')){document.getElementById('q').value='';document.getElementById('state').value='';loadRows(true);return;}var gen=e.target.closest('.notes-generate');if(gen){e.stopPropagation();generateNotes(gen);return;}var opener=e.target.closest('.row-open');if(opener){var target=opener.closest('tr[data-id]');if(target)openSession(target.dataset.id,target.classList.contains('notes-ready')?'notes':'');return;}if(e.target.classList.contains('select-cell')){var cb=e.target.querySelector('.row-select');if(cb){cb.checked=!cb.checked;updateSelection();}return;}if(e.target.closest('input,button,a')){updateSelection();return;}var row=e.target.closest('tr[data-id]');if(row)openSession(row.dataset.id,row.classList.contains('notes-ready')?'notes':'');});
document.getElementById('rows').addEventListener('keydown',function(e){var row=e.target.closest('tr[data-id]');if(e.target===row&&(e.key==='Enter'||e.key===' ')){e.preventDefault();openSession(row.dataset.id,row.classList.contains('notes-ready')?'notes':'');}});
document.getElementById('session-strip').addEventListener('click',function(e){var blk=e.target.closest('.blk');if(blk)jumpToSegment(Number(blk.dataset.seg));});
document.getElementById('session-strip').addEventListener('keydown',onStripKey);
var stripResize;window.addEventListener('resize',function(){clearTimeout(stripResize);stripResize=setTimeout(function(){if(currentSession&&stripState.data)renderStrip(stripState.data);},150);});
document.getElementById('close-overlay').onclick=closeOverlay;
document.getElementById('detail-overlay').addEventListener('click',function(e){if(e.target===this||e.target.classList.contains('overlay-inner'))closeOverlay();});
document.addEventListener('keydown',function(e){var overlay=document.getElementById('detail-overlay');if(!overlay.classList.contains('open'))return;if(e.key==='Escape'){closeOverlay();return;}trapFocus(e,overlay);});
document.getElementById('retranscribe').onclick=()=>{var pending=action('/retranscribe');if(pending)pending.then(()=>openSession(currentSession)).catch(e=>notify(e.message,'error'));};
document.getElementById('queue-review').onclick=showNotes;
document.getElementById('notes-retry').onclick=function(){if(!currentReview)return;var session=currentSession,review=currentReview;document.getElementById('notes-state').textContent='Queued for regeneration…';fetch('/v1/meeting-notes/'+encodeURIComponent(review)+'/retry',{method:'POST',credentials:'same-origin'}).then(function(r){if(!r.ok)throw new Error('Unable to queue regeneration');return r.json();}).then(function(){if(session===currentSession&&review===currentReview){showNotes();loadRows(true);}}).catch(function(e){if(session===currentSession&&review===currentReview)document.getElementById('notes-state').textContent=e.message;});};
document.getElementById('show-transcript').onclick=function(){transcriptExplicit=true;notesRequest++;if(notesPollTimer)clearTimeout(notesPollTimer);notesPollTimer=null;setDetailView('transcript');};
document.getElementById('notes-download').onclick=function(){if(!currentMarkdown)return;var blob=new Blob([currentMarkdown],{type:'text/markdown;charset=utf-8'}),url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download=(document.getElementById('notes-title').textContent.trim().replace(/[\/:*?"<>|]+/g,'-').slice(0,100)||'meeting-notes')+'.md';link.click();setTimeout(function(){URL.revokeObjectURL(url);},1000);};
document.getElementById('notes-copy').onclick=function(){if(!currentMarkdown)return;copyText(currentMarkdown).then(function(ok){notify(ok?'Meeting notes copied.':'Could not copy. Use Download .md instead.',ok?'':'error');});};
document.getElementById('delete-audio').onclick=()=>{var pending=action('/delete-audio','POST','Delete the source audio? The transcript will remain.');if(pending)pending.then(()=>openSession(currentSession)).catch(e=>notify(e.message,'error'));};
document.getElementById('delete-entry').onclick=()=>{var pending=action('','DELETE','Delete this entire entry and transcript? This cannot be undone.');if(pending)pending.then(()=>{closeOverlay();loadRows(true);}).catch(e=>notify(e.message,'error'));};
document.getElementById('select-all').onchange=function(e){document.querySelectorAll('.row-select').forEach(function(box){box.checked=e.target.checked;});updateSelection();};
document.getElementById('bulk-build').onclick=function(){runBulk('/review','POST');};
document.getElementById('bulk-retranscribe').onclick=function(){runBulk('/retranscribe','POST');};
document.getElementById('bulk-delete-audio').onclick=function(){runBulk('/delete-audio','POST','Delete the recorded audio for the selected meetings? Transcripts and notes are kept. This cannot be undone.');};
document.getElementById('bulk-delete').onclick=function(){runBulk('','DELETE','Delete the selected entries and transcripts? This cannot be undone.');};
document.getElementById('list-retry').onclick=function(){loadRows(true);};
var debounce; document.getElementById('q').oninput=()=>{clearTimeout(debounce);debounce=setTimeout(()=>loadRows(true),250);};
document.getElementById('state').onchange=()=>loadRows(true); document.getElementById('more').onclick=()=>{listState.page++;loadRows(false);};
var qBox=document.getElementById('q');function setPlaceholder(){qBox.placeholder=window.matchMedia('(max-width:760px)').matches?qBox.dataset.short:qBox.dataset.full;}
setPlaceholder();window.matchMedia('(max-width:760px)').addEventListener('change',setPlaceholder);
loadRows(true);
// Once the operator has loaded additional pages, keep that expanded result
// set stable. A page-1 refresh would otherwise discard later pages and their
// selections every five seconds, making "Load more" effectively unusable.
setInterval(function(){if(!currentSession && listState.page===1)loadRows(true);},5000);
"""
        + f"if ({initial} !== null) openSession({initial}, {initial_view_js});"
        + """
</script>
"""
    )
    return _shell(
        "Meetings",
        body,
        token_configured=token_configured,
        active="transcriptions",
        console_search=console_search,
        main_class="meetings-page",
    )


def render_meeting_notes_page(*, token_configured: bool) -> str:
    """Meeting-notes library and detail overlay.

    The page deliberately treats every field returned by the review service as
    untrusted plain text.  In particular, model output is never assigned to
    ``innerHTML`` without passing through ``escapeHtml``.
    """
    body = """
<div class="page-head"><h1>Meeting notes</h1>
  <div><div class="help">Meetings appear here after you select Build Meeting Notes.</div><a class="btn secondary" href="/v1/bridge/workflow.md" download>Download AI workflow</a></div></div>
<div class="table-wrap"><table>
  <thead><tr><th>Meeting</th><th>Date</th><th>Participants</th><th>Status</th><th>Updated</th></tr></thead>
  <tbody id="notes-rows"><tr><td colspan="5" class="empty">Loading…</td></tr></tbody>
</table></div>
<footer class="pager"><button id="notes-more" class="secondary" style="display:none">Load more</button></footer>

<div class="overlay" id="notes-overlay" role="dialog" aria-modal="true" aria-label="Meeting notes">
  <div class="overlay-inner">
    <div class="overlay-head"><button class="secondary" id="notes-close">Back</button><div class="title"><div class="help" id="notes-meta"></div><h1 id="notes-title">Meeting notes</h1></div><button class="secondary" id="notes-retry">Regenerate notes</button></div>
    <div id="notes-state" class="help" role="status"></div>
    <section class="card notes-hero"><div class="notes-toolbar"><div style="flex:1"><h2 id="notes-hero-title">Meeting summary</h2><div class="help" id="notes-hero-meta" style="margin:0"></div></div><button class="secondary" id="notes-download">Download .md</button></div></section>
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
  fetch('/v1/meeting-notes?page='+notesState.page+'&per_page='+notesState.perPage,{credentials:'same-origin'}).then(function(r){if(r.status===401||r.status===403){location='/login';return null;}return r.json();}).then(function(data){if(!data)return;var items=data.items||data.meeting_notes||data.notes||[];notesState.total=data.total==null?items.length:data.total;var root=document.getElementById('notes-rows');if(!items.length&&!notesState.loaded)root.innerHTML='<tr><td colspan="5" class="empty">No meeting notes yet. Select Build Meeting Notes from a transcription.</td></tr>';else root.insertAdjacentHTML('beforeend',items.map(noteRow).join(''));notesState.loaded+=items.length;document.getElementById('notes-more').style.display=notesState.loaded<notesState.total?'':'none';}).catch(function(){document.getElementById('notes-rows').innerHTML='<tr><td colspan="5" class="error-text">Unable to load meeting notes.</td></tr>';});
}
function renderNotes(data){
  var n=data.note||data.meeting_note||data; var meta=n.meta||n;
  document.getElementById('notes-title').textContent=n.title||meta.title||meta.name||'Meeting notes';
  document.getElementById('notes-meta').textContent=[fmtDate(meta.created||meta.meeting_time||meta.started),meta.device||meta.platform].filter(Boolean).join(' · ');
  document.getElementById('notes-hero-title').textContent=n.title||meta.title||meta.name||'Meeting notes';
  document.getElementById('notes-hero-meta').textContent=[fmtDate(meta.created||meta.meeting_time||meta.started),meta.device||meta.platform].filter(Boolean).join(' · ');
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
document.getElementById('notes-close').onclick=closeNote;document.getElementById('notes-overlay').addEventListener('click',function(e){if(e.target===this||e.target.classList.contains('overlay-inner'))closeNote();});document.addEventListener('keydown',function(e){if(e.key==='Escape'&&document.getElementById('notes-overlay').classList.contains('open'))closeNote();});
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
    copy_icon = _icon("copy")
    body = (
        fr"""
<div class="page-head"><h1>Install Meeting Notes</h1></div>
<div class="doc">
<section>
  <h2>What the installer includes</h2>
  <p>The Windows package is self-contained. It includes Python, Qt, NumPy,
  SoundCard, the HTTP client, WebSocket support, and their native runtime files.
  You do not need Python, pip, a compiler, an audio driver, or administrator access.</p>
  <p class="help">Requirements: 64-bit Windows 10 or 11, PowerShell 5.1 or newer,
  and access to <strong>{address}</strong> on your LAN.</p>
</section>
<section>
  <h2>Install</h2>
  <p><strong>Fastest option: run this one-step command in PowerShell.</strong> It downloads the installer directly from this server and runs it for the current Windows user.</p>
  <pre class="command" id="cmd-oneline" tabindex="0">powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "irm '{address}/install/client-agent.ps1' | iex"</pre>
  <div class="copy-row"><button type="button" class="secondary" data-copy-target="cmd-oneline">{copy_icon}<span>Copy command</span></button></div>
  <ol>
    <li><a class="btn" href="/install/client-agent.ps1" download>Download installer</a></li>
    <li>Open PowerShell normally. Administrator mode is not required.</li>
    <li>Run the downloaded script:</li>
  </ol>
  <pre class="command" id="cmd-file" tabindex="0">powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$env:USERPROFILE\Downloads\Install-MeetingNotes.ps1"</pre>
  <div class="copy-row"><button type="button" class="secondary" data-copy-target="cmd-file">{copy_icon}<span>Copy command</span></button></div>
  <p class="help">If your browser renamed the file, use its actual filename. Re-running
  the installer upgrades the application and preserves your existing server token,
  recording folder, and client settings.</p>
</section>
<section>
  <h2>Uninstall</h2>
  <p><a class="btn secondary" href="/install/uninstall-client.ps1" download>Download uninstaller</a></p>
  <p class="help">Run it in normal PowerShell to remove the per-user application and shortcuts.
  Recordings and <code>%USERPROFILE%\.meeting-notes</code> settings are preserved by default.
  Add <code>-RemoveSettings</code> only when you also want to remove client settings.</p>
</section>
<section>
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
</section>
<section>
  <h2>Troubleshooting</h2>
  <ul>
    <li>If Windows blocks the download, keep the file only if it came from this server page.</li>
    <li>If the app says the server rejected the token, copy the web-login token again in Settings.</li>
    <li>Remote Desktop may not expose a microphone. Test once from the physical Windows session.</li>
    <li>The installer checks the server before launching and prints a warning if the LAN address is unavailable.</li>
    <li>To send diagnostics for troubleshooting, open <strong>Logs</strong> in the app and choose <strong>Send to server</strong>. Uploads are listed under Settings, Client logs.</li>
    <li>The installer and uninstaller never delete recordings. If your recordings folder sits inside the app folder they stop with a message and change nothing.</li>
  </ul>
</section>
</div>
<script>
"""
        + _JS_HELPERS
        + r"""
document.querySelectorAll('[data-copy-target]').forEach(function (btn) {
  btn.addEventListener('click', function () {
    var target = document.getElementById(btn.dataset.copyTarget);
    if (!target) return;
    copyText(target.textContent).then(function (ok) { notify(ok ? 'Command copied.' : 'Could not copy. Select the command and copy it by hand.', ok ? '' : 'error'); });
  });
});
</script>
"""
    )
    return _shell("Install client", body, token_configured=token_configured, active="install")


# PowerShell shared by the installer and the uninstaller. Both scripts define
# $installDir before this runs. __ACTION__ is "installer" or "uninstaller".
_RECORDINGS_GUARD_PS = r'''
# --- Recordings safety guard -------------------------------------------------
# Runs first: before any process is stopped and before any file is touched.
# Replacing or removing the app folder must never take recordings with it.
function Get-NormalizedPath([string]$path) {
    return [IO.Path]::GetFullPath($path).TrimEnd('\', '/')
}

function Test-PathInside([string]$path, [string]$folder) {
    $candidate = Get-NormalizedPath $path
    $container = Get-NormalizedPath $folder
    return ($candidate.Equals($container, [StringComparison]::OrdinalIgnoreCase) -or
        $candidate.StartsWith($container + "\", [StringComparison]::OrdinalIgnoreCase))
}

function Assert-RecordingsAreSafe {
    # Where the app saves recordings: save_dir in the user's config.json
    # (UTF-8), else the default folder in the user profile.
    $saveDir = Join-Path $env:USERPROFILE "Meeting Notes"
    $configFile = Join-Path (Join-Path $env:USERPROFILE ".meeting-notes") "config.json"
    if (Test-Path -LiteralPath $configFile) {
        try {
            $cfg = [IO.File]::ReadAllText($configFile, [Text.Encoding]::UTF8) | ConvertFrom-Json
            $configured = [string]$cfg.save_dir
            if (-not [string]::IsNullOrWhiteSpace($configured)) { $saveDir = $configured.Trim() }
        } catch { }
    }
    try {
        if ($saveDir -eq "~" -or $saveDir.StartsWith("~\") -or $saveDir.StartsWith("~/")) {
            $saveDir = $env:USERPROFILE + $saveDir.Substring(1)
        }
        # The shortcut starts the app inside the install folder, so a relative
        # save folder resolves there.
        if (-not [IO.Path]::IsPathRooted($saveDir)) { $saveDir = Join-Path $installDir $saveDir }
        $saveDir = Get-NormalizedPath $saveDir
    } catch { }

    # The install folder and any leftover MeetingNotes.old-* folders are what
    # the scripts remove.
    $roots = @($installDir)
    $parent = Split-Path -Parent $installDir
    if (Test-Path -LiteralPath $parent) {
        $roots += @(Get-ChildItem -LiteralPath $parent -Directory -Filter "MeetingNotes.old-*" -ErrorAction SilentlyContinue |
            ForEach-Object { $_.FullName })
    }

    foreach ($root in $roots) {
        $inside = $false
        try { $inside = Test-PathInside $saveDir $root } catch { }
        if ($inside) {
            throw "Your recordings folder is inside the app folder ($saveDir). Removing the app folder would delete your recordings. Open Meeting Notes and use Move recordings, or change the folder in Settings, then run the __ACTION__ again. Nothing was changed."
        }
    }

    # Belt and braces: never remove a folder that holds recordings or the
    # upload queue, wherever the config says recordings live.
    foreach ($root in $roots) {
        if (-not (Test-Path -LiteralPath $root)) { continue }
        $wav = Get-ChildItem -LiteralPath $root -Recurse -Force -File -Filter "*.wav" -ErrorAction SilentlyContinue |
            Select-Object -First 1
        $queue = Get-ChildItem -LiteralPath $root -Recurse -Force -Directory -Filter ".upload-queue" -ErrorAction SilentlyContinue |
            Select-Object -First 1
        $found = if ($null -ne $wav) { $wav.FullName } elseif ($null -ne $queue) { $queue.FullName } else { $null }
        if ($found) {
            throw "Recordings were found inside the app folder ($found). Removing the app folder would delete them. Open Meeting Notes and use Move recordings, or change the folder in Settings, then run the __ACTION__ again. Nothing was changed."
        }
    }
    return $saveDir
}
'''


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

__RECORDINGS_GUARD__
$recordingsDir = Assert-RecordingsAreSafe

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
    # Swap folders instead of deleting in place: if any file of the old
    # install is locked (antivirus, Explorer), the rename fails before
    # anything is removed and the old version keeps working.
    $previous = "$installDir.old-" + [Guid]::NewGuid().ToString("N")
    if (Test-Path -LiteralPath $installDir) {
        try {
            Rename-Item -LiteralPath $installDir -NewName (Split-Path -Leaf $previous)
        } catch {
            throw "Meeting Notes could not be updated because a file in $installDir is in use. Close any program using it and run the installer again. The installed version was left unchanged."
        }
    }
    try {
        Move-Item -LiteralPath $staging -Destination $installDir
    } catch {
        if (Test-Path -LiteralPath $previous) { Rename-Item -LiteralPath $previous -NewName (Split-Path -Leaf $installDir) }
        throw
    }
    # Best effort: remove this and any earlier leftovers of previous versions.
    Get-ChildItem -LiteralPath (Split-Path -Parent $installDir) -Directory -Filter "MeetingNotes.old-*" -ErrorAction SilentlyContinue |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }

    Write-Step "Writing client configuration without replacing existing secrets"
    New-Item -ItemType Directory -Path $configDir -Force | Out-Null
    $config = $null
    if (Test-Path -LiteralPath $configPath) {
        try {
            # Windows PowerShell 5.1 reads BOM-less files as ANSI, which would
            # corrupt non-ASCII device names and folders on every update.
            $config = [IO.File]::ReadAllText($configPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
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
    script = script.replace("__RECORDINGS_GUARD__", _RECORDINGS_GUARD_PS.replace("__ACTION__", "installer"))
    return script.replace("__SERVER_ADDRESS__", address).replace("__MANIFEST_URL__", manifest)


def render_client_uninstaller() -> str:
    """Generate a non-elevated, per-user Windows client removal script."""
    script = r'''#Requires -Version 5.1
[CmdletBinding()]
param([switch]$RemoveSettings)

$ErrorActionPreference = "Stop"
$installDir = Join-Path $env:LOCALAPPDATA "MeetingNotes"
$settingsDir = Join-Path $env:USERPROFILE ".meeting-notes"
$desktopShortcut = Join-Path ([Environment]::GetFolderPath("Desktop")) "Meeting Notes.lnk"
$startMenuShortcut = Join-Path ([Environment]::GetFolderPath("Programs")) "Meeting Notes.lnk"

__RECORDINGS_GUARD__
$recordingsDir = Assert-RecordingsAreSafe

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
Get-ChildItem -LiteralPath (Split-Path -Parent $installDir) -Directory -Filter "MeetingNotes.old-*" -ErrorAction SilentlyContinue |
    ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }
foreach ($shortcut in @($desktopShortcut, $startMenuShortcut)) {
    if (Test-Path -LiteralPath $shortcut) { Remove-Item -LiteralPath $shortcut -Force }
}
if ($RemoveSettings -and (Test-Path -LiteralPath $settingsDir) -and (Test-PathInside $recordingsDir $settingsDir)) {
    # The recordings folder lives inside the settings folder: never delete it.
    Write-Warning "Client settings were kept because your recordings folder is inside $settingsDir."
} elseif ($RemoveSettings -and (Test-Path -LiteralPath $settingsDir)) {
    Remove-Item -LiteralPath $settingsDir -Recurse -Force
    Write-Host "Removed client settings."
} else {
    Write-Host "Recordings and client settings were preserved."
}
Write-Host "Meeting Notes was uninstalled for this Windows user."
'''
    return script.replace("__RECORDINGS_GUARD__", _RECORDINGS_GUARD_PS.replace("__ACTION__", "uninstaller"))


# Compatibility names kept for callers/tests from the first web UI.
def render_sessions_page(*, token_configured: bool) -> str:
    return render_transcriptions_page(token_configured=token_configured)


def render_session_detail_page(session_id: str, *, token_configured: bool) -> str:
    return render_transcriptions_page(
        token_configured=token_configured, initial_session_id=session_id
    )


# -- settings ---------------------------------------------------------------


# Settings sections that act immediately through the API (not through the form's
# Save button), so they live outside the <form>: AI access keys and client logs.
_SETTINGS_IMMEDIATE_HTML = r"""
<div class="settings-sheet immediate">
  <section class="sect" aria-labelledby="settings-agents-heading">
    <h2 id="settings-agents-heading">AI access</h2>
    <div class="sect-body">
    <p class="help">Agents such as Claude can read your meetings, notes, transcripts, action items and decisions
    with their own key. A key is separate from your sign-in token, never opens this website, and cannot delete
    anything. Creating and revoking keys takes effect immediately; it does not wait for Save settings.</p>
    <section class="reveal" id="key-reveal" aria-labelledby="key-reveal-heading" hidden>
      <header>__ICON_KEY__<h3 id="key-reveal-heading" tabindex="-1">Copy your new key now</h3></header>
      <div class="reveal-body">
        <p>This is the only time the full key is shown. Store it somewhere safe. If you lose it, revoke it and create another.</p>
        <div class="field-name">API key</div>
        <pre class="command" id="reveal-key" tabindex="0"></pre>
        <div class="copy-row"><button type="button" class="secondary" data-copy-target="reveal-key">__ICON_COPY__<span>Copy key</span></button></div>
        <div class="field-name">Add it to Claude Code</div>
        <pre class="command" id="reveal-cmd" tabindex="0"></pre>
        <div class="copy-row"><button type="button" class="secondary" data-copy-target="reveal-cmd">__ICON_COPY__<span>Copy command</span></button></div>
        <div class="field-name">For other agents (no key needed to read these)</div>
        <ul class="reveal-links">
          <li><a href="/api/v1/manifest" target="_blank" rel="noopener">/api/v1/manifest</a> machine-readable description</li>
          <li><a href="/llms.txt" target="_blank" rel="noopener">/llms.txt</a> short guide for language models</li>
          <li><a href="/api-docs.md" target="_blank" rel="noopener">/api-docs.md</a> full reference</li>
        </ul>
        <button type="button" class="secondary" id="key-reveal-done">I have saved the key</button>
      </div>
    </section>
    <div id="keys-box" aria-live="polite"><p class="help" role="status">Loading keys...</p></div>
    <div class="subsect">
      <h3 class="subsect-title">Create a key</h3>
      <form id="key-form" novalidate>
        <label class="field">
          <span class="name">Key name</span>
          <input type="text" id="key-name" maxlength="80" autocomplete="off" placeholder="Claude Code on my laptop">
        </label>
        <label class="checkbox">
          <input type="checkbox" id="key-write">
          <span>Allow writes (build notes, rename meetings)</span>
        </label>
        <p class="help">Unchecked, the key can only read. Even with writes on, a key can never delete anything.</p>
        <p class="error-text" id="key-error" role="alert" hidden></p>
        <p><button type="submit" class="secondary" id="key-create">__ICON_KEY__<span>Create key</span></button></p>
      </form>
    </div>
    </div>
  </section>
  <section class="sect" aria-labelledby="settings-logs-heading">
    <h2 id="settings-logs-heading">Client logs</h2>
    <div class="sect-body">
    <p class="help">Diagnostic bundles sent from the Windows app (Logs, then Send to server). The app redacts the
    sign-in token before sending. The newest 20 bundles per computer are kept.</p>
    <div id="logs-box" aria-live="polite"><p class="help" role="status">Loading logs...</p></div>
    <div class="inline-actions"><button type="button" class="secondary" id="logs-refresh">__ICON_REFRESH__<span>Refresh</span></button></div>
    </div>
  </section>
</div>
"""

_SETTINGS_IMMEDIATE_JS = r"""
(function () {
  var SAVED_ADDRESS = __SERVER_ADDRESS_JSON__;
  function el(id) { return document.getElementById(id); }
  function base() { return String(SAVED_ADDRESS || location.origin).replace(/\/+$/, ''); }
  function apiError(status, data) {
    if (status === 401 || status === 403) return 'Your sign-in has expired. Reload the page and sign in again.';
    return (data && typeof data.detail === 'string' && data.detail) || 'Something went wrong (HTTP ' + status + ').';
  }
  function api(method, url, body) {
    var options = {method: method, credentials: 'same-origin'};
    if (body) { options.headers = {'Content-Type': 'application/json'}; options.body = JSON.stringify(body); }
    return fetch(url, options).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (!r.ok) throw new Error(apiError(r.status, data));
        return data;
      });
    }, function () { throw new Error('Could not reach the server. Check your connection and try again.'); });
  }
  function loadError(message, retryAttr) {
    return '<p class="error-text" role="alert">' + escapeHtml(message) + '</p><p><button type="button" class="secondary" ' + retryAttr + '>Try again</button></p>';
  }

  // ---- AI access keys ----
  var keysBox = el('keys-box'), reveal = el('key-reveal');
  function renderKeys(items) {
    if (!items.length) {
      keysBox.innerHTML = '<div class="ledger-empty"><h3>No keys yet</h3><p>Create a key below, then paste it into the agent\'s setup and it can start reading your meetings. Give each agent its own key so you can revoke one without breaking the others.</p></div>';
      return;
    }
    items = items.slice().sort(function (a, b) { return (b.created_at || 0) - (a.created_at || 0); });
    var rows = items.map(function (k) {
      var revoked = !!k.revoked_at, write = (k.scopes || []).indexOf('write') >= 0;
      var status = revoked ? '<span class="badge none">' + tick('none') + 'Revoked</span>' : '<span class="badge done">' + tick('done') + 'Active</span>';
      var action = revoked ? '' : '<button type="button" class="danger row-btn" data-revoke="' + escapeHtml(k.id) + '" data-name="' + escapeHtml(k.name) + '">Revoke<span class="sr-only"> ' + escapeHtml(k.name) + '</span></button>';
      return '<tr' + (revoked ? ' class="revoked"' : '') + '>' +
        '<td class="k-name">' + escapeHtml(k.name) + '</td>' +
        '<td class="k-prefix"><code>' + escapeHtml(k.prefix) + '&hellip;</code></td>' +
        '<td class="k-access">' + (write ? 'Read + write' : 'Read') + '</td>' +
        '<td class="k-created" data-label="Created">' + escapeHtml(fmtDate(k.created_at, true)) + '</td>' +
        '<td class="k-used" data-label="Last used">' + (k.last_used_at ? escapeHtml(fmtDate(k.last_used_at, true)) : 'Never') + '</td>' +
        '<td class="k-status">' + status + '</td>' +
        '<td class="k-act">' + action + '</td></tr>';
    }).join('');
    keysBox.innerHTML = '<div class="ledger-wrap"><table class="ledger keys"><thead><tr><th>Name</th><th>Key</th><th>Access</th><th>Created</th><th>Last used</th><th>Status</th><th><span class="sr-only">Actions</span></th></tr></thead><tbody>' + rows + '</tbody></table></div>';
  }
  function loadKeys() {
    return api('GET', '/v1/agent-keys').then(function (data) { renderKeys(Array.isArray(data.items) ? data.items : []); })
      .catch(function (e) { keysBox.innerHTML = loadError(e.message, 'data-retry="keys"'); });
  }
  function showReveal(record) {
    el('reveal-key').textContent = record.key;
    el('reveal-cmd').textContent = 'claude mcp add --transport http meeting-notes ' + base() + '/mcp --header "Authorization: Bearer ' + record.key + '"';
    reveal.hidden = false;
    var heading = el('key-reveal-heading');
    heading.focus({preventScroll: true});
    reveal.scrollIntoView({block: 'nearest', behavior: 'smooth'});
  }
  function hideReveal() {
    reveal.hidden = true;
    el('reveal-key').textContent = '';
    el('reveal-cmd').textContent = '';
    el('key-name').focus();
  }
  reveal.querySelectorAll('[data-copy-target]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var target = el(btn.dataset.copyTarget);
      if (!target) return;
      copyText(target.textContent).then(function (ok) { notify(ok ? 'Copied.' : 'Could not copy. Select the text and copy it by hand.', ok ? '' : 'error'); });
    });
  });
  el('key-reveal-done').addEventListener('click', hideReveal);
  keysBox.addEventListener('click', function (event) {
    if (event.target.closest('[data-retry]')) { loadKeys(); return; }
    var button = event.target.closest('[data-revoke]');
    if (!button) return;
    var name = button.dataset.name;
    if (!confirm('Revoke "' + name + '"?\n\nAny agent using this key loses access immediately. This cannot be undone.')) return;
    button.disabled = true;
    api('DELETE', '/v1/agent-keys/' + encodeURIComponent(button.dataset.revoke))
      .then(function () { notify('Key revoked.'); return loadKeys(); })
      .catch(function (e) { button.disabled = false; notify(e.message, 'error'); });
  });
  el('key-form').addEventListener('submit', function (event) {
    event.preventDefault();
    var input = el('key-name'), error = el('key-error'), button = el('key-create'), name = input.value.trim();
    function fail(message) { error.textContent = message; error.hidden = false; input.setAttribute('aria-invalid', 'true'); input.focus(); }
    error.hidden = true; input.removeAttribute('aria-invalid');
    if (!name) { fail('Name the key so you can tell it apart later, for example "Claude Code on my laptop".'); return; }
    button.disabled = true; button.classList.add('is-busy');
    api('POST', '/v1/agent-keys', {name: name, scopes: el('key-write').checked ? ['read', 'write'] : ['read']})
      .then(function (record) {
        input.value = ''; el('key-write').checked = false;
        showReveal(record);
        return loadKeys();
      })
      .catch(function (e) { fail(e.message); })
      .finally(function () { button.disabled = false; button.classList.remove('is-busy'); });
  });
  el('key-name').addEventListener('input', function () { el('key-error').hidden = true; this.removeAttribute('aria-invalid'); });

  // ---- Client logs ----
  var logsBox = el('logs-box');
  function renderLogs(items) {
    if (!items.length) {
      logsBox.innerHTML = '<div class="ledger-empty"><h3>Nothing sent yet</h3><p>In the Windows app, open Logs and choose Send to server. The bundle appears here with a download link, ready to open when something misbehaves.</p></div>';
      return;
    }
    var rows = items.map(function (item) {
      var when = fmtDate(item.received_at, true);
      return '<tr>' +
        '<td class="l-device">' + escapeHtml(item.device) + '</td>' +
        '<td class="l-when" title="' + escapeHtml(fmtDate(item.received_at)) + '">' + escapeHtml(when) + '</td>' +
        '<td class="l-size">' + escapeHtml(fmtBytes(item.size)) + '</td>' +
        '<td class="l-act"><a class="btn secondary row-btn" href="' + escapeHtml(item.url) + '" download>' + icon('download', 16) + '<span>Download<span class="sr-only"> log bundle from ' + escapeHtml(item.device) + ', ' + escapeHtml(when) + '</span></span></a></td></tr>';
    }).join('');
    logsBox.innerHTML = '<div class="ledger-wrap"><table class="ledger logs"><thead><tr><th>Computer</th><th>Received</th><th>Size</th><th><span class="sr-only">Download</span></th></tr></thead><tbody>' + rows + '</tbody></table></div>';
  }
  function loadLogs() {
    return api('GET', '/v1/client-logs').then(function (data) { renderLogs(Array.isArray(data.items) ? data.items : []); })
      .catch(function (e) { logsBox.innerHTML = loadError(e.message, 'data-retry="logs"'); });
  }
  logsBox.addEventListener('click', function (event) { if (event.target.closest('[data-retry]')) loadLogs(); });
  el('logs-refresh').addEventListener('click', function () {
    var button = this;
    button.disabled = true;
    loadLogs().then(function () { notify('Client logs refreshed.'); }).finally(function () { button.disabled = false; });
  });

  loadKeys();
  loadLogs();
})();
"""


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
    auto_notes_checked = "checked" if settings.auto_generate_notes else ""
    ai_options = "".join(
        f'<option value="{choice}"{" selected" if choice == settings.ai_provider else ""}>{label}</option>'
        for choice, label in (
            ("disabled", "Disabled"), ("codex", "Codex / ChatGPT"),
            ("claude", "Claude (subscription)"), ("ollama", "Ollama (local)"),
        )
    )
    codex_model_options = '<option value="">Account default</option>'
    if settings.codex_model:
        escaped = html.escape(settings.codex_model)
        codex_model_options += f'<option value="{escaped}" selected>{escaped}</option>'
    claude_model_options = '<option value="">Account default</option>'
    if settings.claude_model:
        escaped_claude = html.escape(settings.claude_model)
        claude_model_options += f'<option value="{escaped_claude}" selected>{escaped_claude}</option>'
    escaped_ollama_model = html.escape(settings.ollama_model)
    ollama_model_options = (
        f'<option value="{escaped_ollama_model}" selected>{escaped_ollama_model}</option>'
        if settings.ollama_model else ""
    )
    message_html = (
        f'<div class="banner ok" role="status">{_icon("check")}<span>{html.escape(message)}</span></div>'
        if message else ""
    )
    error_html = (
        f'<div class="banner err" role="alert">{_icon("alert")}<span>{html.escape(error)}</span></div>'
        if error else ""
    )

    immediate_html = (
        _SETTINGS_IMMEDIATE_HTML.replace("__ICON_KEY__", _icon("key"))
        .replace("__ICON_COPY__", _icon("copy"))
        .replace("__ICON_REFRESH__", _icon("refresh"))
    )
    immediate_js = _SETTINGS_IMMEDIATE_JS.replace(
        "__SERVER_ADDRESS_JSON__", json.dumps(settings.server_address.strip().rstrip("/")).replace("</", "<\\/")
    )

    body = f"""
<div class="page-head"><h1>Settings</h1></div>
{message_html}
{error_html}
<div class="settings-layout">
  <nav class="settings-nav" aria-label="Settings sections"><a href="#settings-install-heading">Installation</a><a href="#settings-transcription-heading">Transcription</a><a href="#settings-ai-heading">Meeting notes AI</a><a href="#settings-speakers-heading">Speaker labels</a><a href="#settings-retention-heading">Audio retention</a><a href="#settings-index-heading">Search index</a><a href="#settings-agents-heading">AI access</a><a href="#settings-logs-heading">Client logs</a></nav>
  <div class="settings-main">
  <form method="post" action="/settings" class="settings-sheet">
  <section class="sect" aria-labelledby="settings-install-heading">
    <h2 id="settings-install-heading">Server and client installation</h2>
    <div class="sect-body">
    <label class="field">
      <span class="name">Server address</span>
      <input type="text" name="server_address"
             placeholder="http://meeting-server.local:8000"
             value="{html.escape(settings.server_address)}">
    </label>
    <p class="help">The LAN address embedded into the client-agent installer.
    Leave blank to use the address in the browser when the installer is downloaded.</p>
    </div>
  </section>

  <section class="sect" aria-labelledby="settings-transcription-heading">
    <h2 id="settings-transcription-heading">Transcription</h2>
    <div class="sect-body">
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
    </div>
  </section>

  <section class="sect" aria-labelledby="settings-ai-heading">
    <h2 id="settings-ai-heading">Meeting notes AI</h2>
    <div class="sect-body">
    <label class="field">
      <span class="name">Provider</span>
      <select name="ai_provider" id="ai-provider">{ai_options}</select>
    </label>
    <p class="help">Choosing a provider enables the queued review button for meetings
    you send for review; turn on the option below to build notes for new meetings automatically.
    Codex / ChatGPT uses the server-side bridge login. Claude uses the bridge's
    Claude Code CLI signed in with your Claude Pro/Max subscription.</p>
    <label class="checkbox">
      <input type="checkbox" name="auto_generate_notes" value="on" {auto_notes_checked}>
      <span>Automatically build meeting notes for new meetings</span>
    </label>
    <p class="help">Only applies when an AI provider is selected. Re-transcribing an
    existing meeting never builds notes on its own.</p>
    <div id="codex-settings" class="subsect">
      <div class="row">
        <div><strong>ChatGPT connection</strong><div class="help" id="codex-auth-status" role="status">Checking bridge…</div></div>
        <div class="inline-actions"><button type="button" class="secondary" id="codex-connect">Connect ChatGPT</button><button type="button" class="danger" id="codex-disconnect" style="display:none">Disconnect</button></div>
      </div>
      <div id="codex-device-login" style="display:none">
        <p>Open <a id="codex-login-url" href="https://auth.openai.com/codex/device" target="_blank" rel="noopener">OpenAI device sign-in</a> and enter this one-time code:</p>
        <p><code id="codex-device-code" style="font-size:20px;user-select:all"></code></p>
      </div>
      <label class="field">
        <span class="name">ChatGPT model</span>
        <select name="codex_model" id="codex-model">{codex_model_options}</select>
      </label>
      <div class="help" id="codex-model-status" role="status">Connect ChatGPT to load available models.</div>
    </div>
    <div id="claude-settings" class="subsect">
      <div class="row">
        <div><strong>Claude connection</strong><div class="help" id="claude-auth-status" role="status">Checking bridge…</div></div>
        <div class="inline-actions"><button type="button" class="secondary" id="claude-connect">Connect Claude</button><button type="button" class="danger" id="claude-disconnect" style="display:none">Disconnect</button></div>
      </div>
      <div id="claude-login-panel" style="display:none">
        <p>Open <a id="claude-login-url" href="#" target="_blank" rel="noopener">the Claude sign-in link</a>,
        approve access there, then paste the code shown back here.</p>
        <label class="field">
          <span class="name">Authorization code</span>
          <input type="text" id="claude-login-code-input" autocomplete="off">
        </label>
        <p><button type="button" class="secondary" id="claude-code-submit">Submit code</button></p>
      </div>
      <label class="field">
        <span class="name">Claude model</span>
        <select name="claude_model" id="claude-model">{claude_model_options}</select>
      </label>
      <div class="help" id="claude-model-status" role="status">Connect Claude to load available models.</div>
    </div>
    <div id="ollama-settings" class="subsect">
      <label class="field">
        <span class="name">Ollama base URL</span>
        <input type="url" name="ollama_base_url" value="{html.escape(settings.ollama_base_url)}"
               placeholder="http://ollama:11434">
      </label>
      <label class="field">
        <span class="name">Ollama model</span>
        <select name="ollama_model" id="ollama-model">{ollama_model_options}</select>
      </label>
      <div class="inline-actions"><button type="button" class="secondary" id="ollama-model-refresh">Load available models</button>
      <span class="help" id="ollama-model-status" role="status" style="margin:0"></span></div>
      <p class="help" style="margin-top:12px">The Ollama service must be reachable from the server or bridge container.</p>
    </div>

    <details class="workflow" id="ai-workflow"><summary id="ai-workflow-heading">AI workflow prompt <small>View or edit the instructions used to create meeting notes</small></summary><div class="workflow-body">
      <p class="help">These instructions guide generated meeting summaries. They do not rename the saved meeting.</p>
      <label class="field">
        <span class="name">Meeting notes prompt</span>
        <textarea name="ai_workflow" rows="18" style="resize:vertical">{html.escape(settings.ai_workflow)}</textarea>
      </label>
      <a class="btn secondary" href="/v1/bridge/workflow.md" download>Download AI workflow</a>
    </div></details>
    </div>
  </section>

  <section class="sect" aria-labelledby="settings-speakers-heading">
    <h2 id="settings-speakers-heading">Remote speaker labels</h2>
    <div class="sect-body">
    <label class="checkbox">
      <input type="checkbox" name="diarization_enabled" value="on" {diarization_checked}>
      <span>Distinguish speakers within the system-audio track</span>
    </label>
    <p class="help">Optional and compute-heavy. Requires the diarization extra,
    ffmpeg, acceptance of the model terms, and HUGGINGFACE_TOKEN on the server.</p>
    <label class="field">
      <span class="name">Diarization model</span>
      <input type="text" name="diarization_model" value="{html.escape(settings.diarization_model)}">
    </label>
    <div class="two-up">
      <label class="field"><span class="name">Minimum speakers</span>
        <input type="number" name="diarization_min_speakers" min="1" value="{settings.diarization_min_speakers}">
      </label>
      <label class="field"><span class="name">Maximum speakers</span>
        <input type="number" name="diarization_max_speakers" min="1" value="{settings.diarization_max_speakers}">
      </label>
    </div>
    </div>
  </section>
  <section class="sect" aria-labelledby="settings-retention-heading">
    <h2 id="settings-retention-heading">Audio retention</h2>
    <div class="sect-body">
    <label class="field">
      <span class="name">Audio retention (days)</span>
      <input type="number" name="audio_retention_days" min="-1" value="{settings.audio_retention_days}">
    </label>
    <p class="help">Set 0 to delete audio immediately after transcription, a positive
    number to retain it for that many days, or -1 to keep it forever.</p>

    <label class="checkbox">
      <input type="checkbox" name="delete_audio_only_after_success" value="on" {checked}>
      <span>Only delete audio once a transcription has succeeded</span>
    </label>

    <label class="field" style="margin-top:14px">
      <span class="name">Retention check interval (minutes)</span>
      <input type="number" name="retention_check_interval_minutes" min="1"
             value="{settings.retention_check_interval_minutes}">
    </label>
    </div>
  </section>
  <section class="sect" aria-labelledby="settings-index-heading">
    <h2 id="settings-index-heading">Search index</h2>
    <div class="sect-body">
    <p class="help">Rebuilds the session/transcript search index from what's actually on
    disk. Safe to run any time; only needed if the index looks stale or missing
    (e.g. after restoring the data volume from a backup).</p>
    <div class="inline-actions"><button type="button" class="secondary" id="reindex-btn">Rebuild index now</button>
    <span id="reindex-status" class="help" role="status" style="margin:0"></span></div>
    </div>
  </section>
  <div class="save-bar">
    <button type="submit">Save settings</button><span class="help">Changes take effect after saving.</span>
  </div>
  </form>
  {immediate_html}
  </div>
</div>
<script>
{_JS_HELPERS}
{immediate_js}
</script>
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
  document.getElementById("claude-settings").style.display = provider === "claude" ? "block" : "none";
  if (provider === "codex") refreshCodexStatus();
  if (provider === "claude") refreshClaudeStatus();
}}
var modelLoads = {{codex:false, ollama:false, claude:false}}, codexModelsLoaded = false, claudeModelsLoaded = false;
function loadProviderModels(provider) {{
  if (modelLoads[provider]) return;
  modelLoads[provider] = true;
  var select = document.getElementById(provider === "codex" ? "codex-model" : provider === "claude" ? "claude-model" : "ollama-model");
  var status = document.getElementById(provider === "codex" ? "codex-model-status" : provider === "claude" ? "claude-model-status" : "ollama-model-status");
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
    if (provider === "codex" || provider === "claude") select.add(new Option("Account default", ""));
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
    }} else if (provider === "claude") {{
      claudeModelsLoaded = true;
      if (claudePoll) {{ clearInterval(claudePoll); claudePoll = null; }}
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
var claudePoll = null, claudeWasConnected = false;
function renderClaudeStatus(data) {{
  var state = data.state || "unavailable", connected = !!data.authenticated;
  if (!connected && claudeWasConnected) claudeModelsLoaded = false;
  claudeWasConnected = connected;
  document.getElementById("claude-auth-status").textContent = connected ? "Connected to Claude" : state.replace(/_/g, " ");
  document.getElementById("claude-connect").style.display = connected ? "none" : "";
  document.getElementById("claude-disconnect").style.display = connected ? "" : "none";
  var url = String(data.login_url || "");
  var panel = document.getElementById("claude-login-panel");
  panel.style.display = (!connected && url && state === "awaiting_user") ? "block" : "none";
  if (url.indexOf("https://claude.com/") === 0 || url.indexOf("https://claude.ai/") === 0 || url.indexOf("https://platform.claude.com/") === 0) {{
    document.getElementById("claude-login-url").href = url;
  }}
  if (connected && !claudeModelsLoaded) loadProviderModels("claude");
  if (connected && claudeModelsLoaded && claudePoll) {{ clearInterval(claudePoll); claudePoll = null; }}
}}
function refreshClaudeStatus() {{
  if (document.getElementById("ai-provider").value !== "claude") return;
  fetch("/v1/bridge/control/status", {{credentials:"same-origin"}}).then(function(r) {{ if(!r.ok) throw new Error("Bridge unavailable"); return r.json(); }}).then(renderClaudeStatus).catch(function(e) {{ document.getElementById("claude-auth-status").textContent = e.message; }});
}}
document.getElementById("claude-connect").addEventListener("click", function() {{
  document.getElementById("claude-auth-status").textContent = "Starting sign-in…";
  fetch("/v1/bridge/control/login", {{method:"POST",credentials:"same-origin"}}).then(function(r) {{ return r.json().then(function(d) {{ if(!r.ok) throw new Error(d.detail||"Unable to start login"); return d; }}); }}).then(function(data) {{ renderClaudeStatus(data); if(!claudePoll) claudePoll=setInterval(refreshClaudeStatus,1500); }}).catch(function(e) {{ document.getElementById("claude-auth-status").textContent=e.message; }});
}});
document.getElementById("claude-disconnect").addEventListener("click", function() {{
  fetch("/v1/bridge/control/logout", {{method:"POST",credentials:"same-origin"}}).then(function(r) {{ return r.json().then(function(d) {{ if(!r.ok) throw new Error(d.detail||"Unable to disconnect"); return d; }}); }}).then(renderClaudeStatus).catch(function(e) {{ document.getElementById("claude-auth-status").textContent=e.message; }});
}});
document.getElementById("claude-code-submit").addEventListener("click", function() {{
  var input = document.getElementById("claude-login-code-input"), code = input.value;
  document.getElementById("claude-auth-status").textContent = "Submitting code…";
  fetch("/v1/bridge/control/login/code", {{method:"POST",credentials:"same-origin",headers:{{"Content-Type":"application/json"}},body:JSON.stringify({{code:code}})}}).then(function(r) {{ return r.json().then(function(d) {{ if(!r.ok) throw new Error(d.detail||"Unable to submit code"); return d; }}); }}).then(function(data) {{ input.value = ""; renderClaudeStatus(data); }}).catch(function(e) {{ document.getElementById("claude-auth-status").textContent=e.message; }});
}});
document.getElementById("ollama-model-refresh").addEventListener("click", function() {{ loadProviderModels("ollama"); }});
document.getElementById("ai-provider").addEventListener("change", updateAiFields);
updateAiFields();
(function () {{
  var links = Array.from(document.querySelectorAll(".settings-nav a"));
  var targets = links.map(function (a) {{ return document.getElementById(a.getAttribute("href").slice(1)); }});
  var nav = document.querySelector(".settings-nav");
  function spy() {{
    var line = window.innerHeight * 0.3, current = 0;
    targets.forEach(function (t, i) {{ if (t && t.getBoundingClientRect().top <= line) current = i; }});
    if (window.scrollY < 8) current = 0;
    else if (window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 4) current = links.length - 1;
    links.forEach(function (a, i) {{ if (i === current) a.setAttribute("aria-current", "true"); else a.removeAttribute("aria-current"); }});
    var active = links[current];
    if (active && nav.scrollWidth > nav.clientWidth) nav.scrollLeft = active.offsetLeft - (nav.clientWidth - active.clientWidth) / 2;
  }}
  window.addEventListener("scroll", spy, {{passive: true}});
  window.addEventListener("resize", spy);
  spy();
}})();
</script>
"""
    return _shell("Settings", body, token_configured=token_configured, active="settings", main_class="settings-page")
