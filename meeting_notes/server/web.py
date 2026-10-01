"""HTML rendering for the browser-facing side of the server.

No template engine (Jinja2 is deliberately not a dependency here -- see
``ARCHITECTURE.md``): every page is built by small Python functions that
return strings, escaping anything server-rendered with ``html.escape``. The
meetings library and the meeting view are thin shells around inline vanilla
JS that fetches the JSON API (``/v1/sessions``, ``/v1/sessions/{id}``) and
renders client-side: the JSON API is what stays correct as the number of
sessions grows, so the HTML side defers to it rather than re-deriving its own
(unpaginated, un-indexed) view of the same data.

``app.py`` only calls the ``render_*`` functions and wires them to routes; it
never builds HTML itself, so every string of markup lives in exactly one
place.

Visual system (see ``.impeccable/surfaces/meeting-notes-server-web-py.md``):
the category standard, played straight. A slim left sidebar, a calm list of
meetings and a Notion-style document view, in neutral greys with one blue
accent. Light, dark and system themes are all designed (``data-theme`` on
``<html>`` is rendered server-side from the ``appearance`` setting, so there is
no flash). Inter is self-hosted from ``/static/fonts``.
"""

from __future__ import annotations

import hashlib
import html
import json
from pathlib import Path
from typing import Optional

from meeting_notes import __version__
from meeting_notes.server import settings as settings_mod
from meeting_notes.server import splitmerge_ui
from meeting_notes.server.store import TRASH_RETENTION_DAYS

# -- icons ----------------------------------------------------------------
# One stroke (Lucide-style outline on a 24 grid, round caps and joins, drawn at
# 1.5px by the stylesheet) for every icon; the same table is handed to the page
# JS so scripted markup draws the same set.

_ICON_PATHS = {
    "mark": '<path d="M2 10v3M6 6v11M10 3v18M14 8v7M18 5v13M22 10v3"/>',
    "list": '<path d="M3 5h.01M3 12h.01M3 19h.01M8 5h13M8 12h13M8 19h13"/>',
    "home": '<path d="M15 21v-8a1 1 0 0 0-1-1h-4a1 1 0 0 0-1 1v8"/><path d="M3 10a2 2 0 0 1 .709-1.528l7-5.999a2 2 0 0 1 2.582 0l7 5.999A2 2 0 0 1 21 10v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    "sliders": '<path d="M21 4h-7M10 4H3M21 12h-9M8 12H3M21 20h-5M12 20H3M14 2v4M8 10v4M16 18v4"/>',
    "download": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/><path d="M12 15V3"/>',
    "logout": '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5"/><path d="M21 12H9"/>',
    "back": '<path d="m12 19-7-7 7-7"/><path d="M19 12H5"/>',
    "open": '<path d="m9 18 6-6-6-6"/>',
    "edit": '<path d="M21.174 6.812a1 1 0 0 0-3.986-3.987L3.842 16.174a2 2 0 0 0-.5.83l-1.321 4.352a.5.5 0 0 0 .623.622l4.353-1.32a2 2 0 0 0 .83-.497z"/><path d="m15 5 4 4"/>',
    "copy": '<rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>',
    "refresh": '<path d="M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/>',
    "search": '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
    "notes": '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/><path d="M10 9H8M16 13H8M16 17H8"/>',
    "mic": '<path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><path d="M12 19v3"/>',
    "speaker": '<path d="M11 4.702a.705.705 0 0 0-1.203-.498L6.413 7.587A1.4 1.4 0 0 1 5.416 8H3a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h2.416a1.4 1.4 0 0 1 .997.413l3.383 3.384A.705.705 0 0 0 11 19.298z"/><path d="M16 9a5 5 0 0 1 0 6"/><path d="M19.364 18.364a9 9 0 0 0 0-12.728"/>',
    "alert": '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
    "check": '<path d="M20 6 9 17l-5-5"/>',
    "key": '<path d="m15.5 7.5 2.3 2.3a1 1 0 0 0 1.4 0l2.1-2.1a1 1 0 0 0 0-1.4L19 4"/><path d="m21 2-9.6 9.6"/><circle cx="7.5" cy="15.5" r="5.5"/>',
    "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/>',
    "moon": '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>',
    "monitor": '<rect width="20" height="14" x="2" y="3" rx="2"/><path d="M8 21h8M12 17v4"/>',
    "more": '<circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/><circle cx="5" cy="12" r="1"/>',
    "trash": '<path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/>',
    "sparkles": '<path d="M9.937 15.5A2 2 0 0 0 8.5 14.063l-6.135-1.582a.5.5 0 0 1 0-.962L8.5 9.936A2 2 0 0 0 9.937 8.5l1.582-6.135a.5.5 0 0 1 .963 0L14.063 8.5A2 2 0 0 0 15.5 9.937l6.135 1.581a.5.5 0 0 1 0 .964L15.5 14.063a2 2 0 0 0-1.437 1.437l-1.582 6.135a.5.5 0 0 1-.963 0z"/>',
    "split": '<path d="M16 3h5v5"/><path d="M8 3H3v5"/><path d="M12 22v-8.3a4 4 0 0 0-1.172-2.872L3 3"/><path d="m15 9 6-6"/>',
    "merge": '<path d="m8 6 4-4 4 4"/><path d="M12 2v10.3a4 4 0 0 1-1.172 2.872L4 22"/><path d="m20 22-5-5"/>',
    "upload": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m17 8-5-5-5 5"/><path d="M12 3v12"/>',
    "x": '<path d="M18 6 6 18M6 6l12 12"/>',
    "laptop": '<path d="M18 5a2 2 0 0 1 2 2v8.526a2 2 0 0 0 .212.897l1.068 2.127a1 1 0 0 1-.9 1.45H3.62a1 1 0 0 1-.9-1.45l1.068-2.127A2 2 0 0 0 4 15.526V7a2 2 0 0 1 2-2z"/><path d="M20.054 15.987H3.946"/>',
    "radio": '<path d="M4.9 19.1C1 15.2 1 8.8 4.9 4.9"/><path d="M7.8 16.2c-2.3-2.3-2.3-6.1 0-8.5"/><circle cx="12" cy="12" r="2"/><path d="M16.2 7.8c2.3 2.3 2.3 6.1 0 8.5"/><path d="M19.1 4.9C23 8.8 23 15.1 19.1 19"/>',
    "mic-off": '<path d="M12 19v3"/><path d="M15 9.34V5a3 3 0 0 0-5.68-1.33"/><path d="M16.95 16.95A7 7 0 0 1 5 12v-2"/><path d="M18.89 13.23A7 7 0 0 0 19 12v-2"/><path d="m2 2 20 20"/><path d="M9 9v3a3 3 0 0 0 5.12 2.12"/>',
    "speaker-off": '<path d="M11 4.702a.705.705 0 0 0-1.203-.498L6.413 7.587A1.4 1.4 0 0 1 5.416 8H3a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h2.416a1.4 1.4 0 0 1 .997.413l3.383 3.384A.705.705 0 0 0 11 19.298z"/><path d="m22 9-6 6"/><path d="m16 9 6 6"/>',
    "info": '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>',
}


def _icon(name: str, size: int = 16) -> str:
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
    ("/meetings", "Meetings", "transcriptions", "list"),
    ("/", "Home", "home", "home"),
    ("/recorders", "Recorders", "recorders", "radio"),
    ("/settings", "Settings", "settings", "sliders"),
    ("/install", "Install", "install", "download"),
)
_ACTIVE_ALIASES = {"sessions": "transcriptions", "meeting-notes": "transcriptions"}

# Theme choices: (value stored in settings.appearance, label, icon).
_APPEARANCES = (("system", "System", "monitor"), ("light", "Light", "sun"), ("dark", "Dark", "moon"))
_APPEARANCE_VALUES = tuple(value for value, _label, _icon_name in _APPEARANCES)
_THEME_COLORS = {"light": "#ffffff", "dark": "#0f1012"}


def _normalize_appearance(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if text in _APPEARANCE_VALUES else "system"


def _theme_color_metas(appearance: str) -> str:
    if appearance == "system":
        return (
            f'<meta name="theme-color" content="{_THEME_COLORS["light"]}" media="(prefers-color-scheme: light)">\n'
            f'<meta name="theme-color" content="{_THEME_COLORS["dark"]}" media="(prefers-color-scheme: dark)">'
        )
    return f'<meta name="theme-color" content="{_THEME_COLORS[appearance]}">'


def _appearance_control(group: str, appearance: str, *, compact: bool = False) -> str:
    """Segmented System / Light / Dark control (radios: native arrow keys, form-submittable)."""
    options = "".join(
        f'<label class="seg-opt" title="{label}"><input type="radio" name="{group}" value="{value}" '
        f'data-appearance{" checked" if value == appearance else ""}>'
        f'{_icon(icon_name)}<span class="{"sr-only" if compact else "seg-label"}">{label}</span></label>'
        for value, label, icon_name in _APPEARANCES
    )
    return f'<div class="seg{" seg-compact" if compact else ""}" role="radiogroup" aria-label="Appearance">{options}</div>'


# Theme switch (no flash: <html data-theme> is already correct when the page
# arrives) and the "/" search shortcut.
_SHELL_JS = r"""
(function () {
  var root = document.documentElement, COLORS = {light: '#ffffff', dark: '#0f1012'};
  function setMetas(value) {
    document.querySelectorAll('meta[name="theme-color"]').forEach(function (m) { m.remove(); });
    (value === 'system' ? [['light', '(prefers-color-scheme: light)'], ['dark', '(prefers-color-scheme: dark)']] : [[value, '']]).forEach(function (pair) {
      var meta = document.createElement('meta'); meta.name = 'theme-color'; meta.content = COLORS[pair[0]];
      if (pair[1]) meta.media = pair[1];
      document.head.appendChild(meta);
    });
  }
  function apply(value) {
    root.dataset.theme = value; setMetas(value);
    document.querySelectorAll('[data-appearance]').forEach(function (radio) { radio.checked = radio.value === value; });
  }
  document.addEventListener('change', function (event) {
    var target = event.target;
    if (!target || !target.matches || !target.matches('[data-appearance]')) return;
    var value = target.value; apply(value);
    fetch('/v1/appearance', {method: 'PUT', credentials: 'same-origin', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({appearance: value})})
      .then(function (r) { if (!r.ok) throw new Error('save failed'); })
      .catch(function () { if (window.notify) notify('Could not save the theme. It will reset when you reload.', 'error'); });
  });
  document.addEventListener('keydown', function (event) {
    if (event.key !== '/' || event.ctrlKey || event.metaKey || event.altKey) return;
    var tag = (event.target && event.target.tagName) || '';
    if (/^(INPUT|TEXTAREA|SELECT)$/.test(tag) || (event.target && event.target.isContentEditable)) return;
    var box = document.getElementById('q');
    if (box) { event.preventDefault(); box.focus(); box.select(); }
  });
})();
"""


def _page_title(subpage: str) -> str:
    """Every page's browser tab title: ``Meeting Notes | <Subpage>``."""
    return f"Meeting Notes | {subpage}"


def _shell(
    title: str,
    body: str,
    *,
    token_configured: bool,
    active: str = "",
    main_class: str = "",
    nav: bool = True,
    appearance: str = "system",
) -> str:
    active = _ACTIVE_ALIASES.get(active, active)
    appearance = _normalize_appearance(appearance)
    sidebar = ""
    tabbar = ""
    if nav:
        current = ' aria-current="page"'
        links = "".join(
            f'<a href="{href}"{current if key == active else ""}>'
            f"{_icon(icon)}<span>{label}</span></a>"
            for href, label, key, icon in _NAV
        )
        tab_links = "".join(
            f'<a href="{href}"{current if key == active else ""}>'
            f"{_icon(icon, 20)}<span>{label}</span></a>"
            for href, label, key, icon in _NAV
        )
        logout = ""
        if token_configured:
            logout = (
                '<form method="post" action="/logout" class="logout-form">'
                f'<button type="submit" class="btn ghost logout" aria-label="Log out" title="Log out">{_icon("logout")}'
                '<span class="lbl">Log out</span></button></form>'
            )
        sidebar = f"""<aside class="sidebar">
  <a class="brand" href="/" aria-label="Meeting Notes home"><span class="mark">{_icon("mark", 16)}</span><span class="brand-name">Meeting Notes</span></a>
  <form class="side-search" role="search" action="/meetings" method="get">
    {_icon("search")}
    <label class="sr-only" for="q">Search meetings</label>
    <input type="text" id="q" name="q" autocomplete="off" enterkeyhint="search"
           placeholder="Search or M-0142" title="Search names, transcripts, or a meeting number like M-0142"
           data-short="Search or M-0142" data-full="Search meetings">
    <kbd aria-hidden="true">/</kbd>
  </form>
  <nav class="primary" aria-label="Primary">{links}</nav>
  <div class="side-foot">
    {_appearance_control("appearance-quick", appearance, compact=True)}
    <span class="app-version" aria-label="Meeting Notes version">v{html.escape(__version__)}</span>
    {logout}
  </div>
</aside>"""
        tabbar = f'<nav class="tabbar" aria-label="Main">{tab_links}</nav>'

    banner = ""
    if not token_configured:
        banner = (
            f'<div class="banner warn">{_icon("alert")}<span>No MEETING_NOTES_TOKEN is configured -- '
            "this server accepts requests from anyone who can reach it. "
            "Fine for a quick local test, not recommended left that way on a "
            "shared network.</span></div>"
        )

    return f"""<!DOCTYPE html>
<html lang="en" data-theme="{appearance}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
{_theme_color_metas(appearance)}
<title>{html.escape(_page_title(title))}</title>
<link rel="preload" href="/static/fonts/inter-latin-wght-normal.woff2" as="font" type="font/woff2" crossorigin>
<link rel="stylesheet" href="{_CSS_HREF}">
<script src="{_ICONS_SRC}"></script>
</head>
<body>
<a class="skip" href="#main">Skip to content</a>
<div class="app{"" if nav else " no-nav"}">
{sidebar}
<div class="app-main">
<main id="main" class="{main_class}">
  {banner}
  {body}
</main>
</div>
{tabbar}
</div>
<div class="toast" id="page-toast" role="status" aria-live="polite"></div>
<script>{_SHELL_JS if nav else ""}</script>
</body>
</html>"""


_JS_HELPERS_SRC = r"""
var ICONS = window.MN_ICONS || {};
function icon(name, size) {
  size = size || 16;
  return '<svg class="ic" viewBox="0 0 24 24" width="' + size + '" height="' + size + '" aria-hidden="true" focusable="false">' + (ICONS[name] || '') + '</svg>';
}
function dot() { return '<i class="dot" aria-hidden="true"></i>'; }
function badge(cls, label, title) {
  return '<span class="badge ' + cls + '"' + (title ? ' title="' + escapeHtml(title) + '"' : '') + '>' + dot() + '<span class="badge-text">' + escapeHtml(label) + '</span></span>';
}
var toastTimer = null;
// notify(message, kind, action): action = {label, onClick} (or {label, href} for a link) adds a button to the toast (e.g. Undo)
// and keeps it up longer.
function notify(message, kind, action) {
  var el = document.getElementById('page-toast');
  if (!el) return;
  el.className = 'toast' + (kind === 'error' ? ' error' : '') + (action ? ' has-action' : '');
  el.textContent = message;
  if (action && action.label) {
    var btn = document.createElement(action.href ? 'a' : 'button');
    if (action.href) btn.href = action.href; else btn.type = 'button';
    btn.className = 'toast-action'; btn.textContent = action.label;
    btn.addEventListener('click', function () {
      clearTimeout(toastTimer); el.textContent = ''; el.className = 'toast';
      if (action.onClick) action.onClick();
    });
    el.appendChild(btn);
  }
  clearTimeout(toastTimer);
  toastTimer = setTimeout(function () { el.textContent = ''; el.className = 'toast'; }, action ? 12000 : (kind === 'error' ? 9000 : 4500));
}
// confirmDialog({title, lead, warning, items:[{name, meta}], itemsLabel, note, confirmLabel, danger}) -> Promise<boolean>.
// `warning` is an optional red alert line under the lead (e.g. "This permanently removes the only copy").
// Needs the #confirm-dialog markup (_CONFIRM_DIALOG_HTML). Focus starts on Cancel: nothing is
// confirmed by an accidental Enter.
function confirmDialog(o) {
  var dlg = document.getElementById('confirm-dialog');
  if (!dlg || !dlg.showModal) {
    return Promise.resolve(window.confirm([o.title, o.lead, o.warning].concat((o.items || []).map(function (i) { return '- ' + i.name; })).filter(Boolean).join('\n')));
  }
  return new Promise(function (resolve) {
    dlg.querySelector('.dialog-title').textContent = o.title || '';
    dlg.querySelector('.dialog-lead').textContent = o.lead || '';
    dlg.querySelector('.dialog-note').textContent = o.note || '';
    var warn = dlg.querySelector('.dialog-warning');
    if (warn) {
      warn.textContent = '';
      if (o.warning) {
        var wi = document.createElement('span'), wt = document.createElement('span');
        wi.className = 'dw-ic'; wi.innerHTML = icon('alert');
        wt.textContent = o.warning;
        warn.appendChild(wi); warn.appendChild(wt);
      }
      warn.hidden = !o.warning;
    }
    var list = dlg.querySelector('.dialog-items'), items = o.items || [];
    list.innerHTML = items.map(function (it) {
      return '<li><span class="di-name">' + escapeHtml(it.name) + '</span><span class="di-meta">' + escapeHtml(it.meta || '') + '</span></li>';
    }).join('');
    list.hidden = !items.length;
    list.setAttribute('aria-label', o.itemsLabel || 'Meetings');
    list.scrollTop = 0;
    var ok = dlg.querySelector('[data-dialog-ok]');
    ok.textContent = o.confirmLabel || 'Confirm';
    ok.className = 'btn ' + (o.danger === false ? 'primary' : 'danger');
    dlg.onclose = function () { dlg.onclose = null; resolve(dlg.returnValue === 'ok'); };
    dlg.onclick = function (e) { if (e.target === dlg) dlg.close('cancel'); };
    dlg.returnValue = '';
    dlg.showModal();
    dlg.querySelector('[data-dialog-cancel]').focus();
  });
}
function meetingSummary(row) {
  var d = Math.max(0, Number(row.duration_sec) || 0);
  return {name: row.name || row.session_id, meta: [fmtDate(row.created, true), d ? fmtDuration(d) : ''].filter(Boolean).join(' · ')};
}
function agoText(ts) {
  var s = Math.max(0, Date.now() / 1000 - Number(ts || 0));
  if (s < 60) return 'just now';
  if (s < 3600) { var m = Math.floor(s / 60); return m + (m === 1 ? ' minute ago' : ' minutes ago'); }
  if (s < 86400) { var h = Math.floor(s / 3600); return h + (h === 1 ? ' hour ago' : ' hours ago'); }
  var d = Math.floor(s / 86400); return d + (d === 1 ? ' day ago' : ' days ago');
}
function daysLeftText(n) { return n <= 0 ? 'Removed soon' : n + (n === 1 ? ' day left' : ' days left'); }
function plural(n, one, many) { return n + ' ' + (n === 1 ? one : many); }
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
// ---- Transcript upload: shared by Home's "Add a meeting" panel and the Meetings page dialog ----
var TRANSCRIPT_MAX_BYTES = 2 * 1024 * 1024;
function localInputValue(date) {
  function two(n) { return String(n).padStart(2, '0'); }
  return date.getFullYear() + '-' + two(date.getMonth() + 1) + '-' + two(date.getDate()) + 'T' + two(date.getHours()) + ':' + two(date.getMinutes());
}
// readTranscriptFile(file, cb): cb(errorMessage) or cb(null, {text, filename, name, when}); `name` is the file name
// without its extension and `when` the file's modified time as a datetime-local value (both used as form defaults).
function readTranscriptFile(file, cb) {
  if (file.size > TRANSCRIPT_MAX_BYTES) { cb('That file is over the 2 MB limit for transcripts.'); return; }
  var reader = new FileReader();
  reader.onload = function () {
    cb(null, {text: String(reader.result || ''), filename: file.name, name: file.name.replace(/[.][^.]+$/, ''), when: file.lastModified ? localInputValue(new Date(file.lastModified)) : ''});
  };
  reader.onerror = function () { cb('Could not read that file.'); };
  reader.readAsText(file);
}
// buildTranscriptPayload({text, name, filename, when}) -> {payload} or {error}. `when` is a datetime-local value.
function buildTranscriptPayload(o) {
  var text = o.text || '';
  if (!text.trim()) return {error: 'Choose a file or paste the transcript first.'};
  var bytes = typeof Blob === 'function' ? new Blob([text]).size : text.length;
  if (bytes > TRANSCRIPT_MAX_BYTES) return {error: 'That transcript is over the 2 MB limit.'};
  if (!o.when) return {error: 'Enter the date and time of the meeting.'};
  var when = new Date(o.when);
  if (isNaN(when.getTime())) return {error: 'Enter a valid date and time.'};
  var payload = {text: text, name: String(o.name || '').trim(), source: o.filename ? 'file' : 'pasted', started_at: Math.floor(when.getTime() / 1000)};
  if (o.filename) payload.filename = o.filename;
  return {payload: payload};
}
// postTranscript(payload) -> Promise<{session_id, ...}>; rejects with an Error whose message is fit to show the user.
function postTranscript(payload) {
  return fetch('/v1/sessions/transcript', {method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)})
    .then(function (r) { return r.json().catch(function () { return {}; }).then(function (data) { return {ok: r.ok, data: data}; }); },
          function () { throw new Error('Could not reach the server. Check the connection and try again.'); })
    .then(function (res) {
      if (!res.ok) throw new Error(typeof res.data.detail === 'string' && res.data.detail ? res.data.detail : 'Could not add the transcript. Please try again.');
      return res.data;
    });
}
// Small, safe Markdown renderer for AI-generated notes. Everything is escaped first (escapeHtml), then a strict
// subset is converted: paragraphs, #..#### headings, one-level ul/ol, **bold**, *italic*/_italic_, `code`,
// [text](http/https url). Raw HTML can never pass through; other link schemes stay plain text.
function mdEmphasis(s) {
  s = s.replace(/\*\*(?=\S)([\s\S]*?\S)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|[^\w])__(?=\S)([\s\S]*?\S)__(?!\w)/g, "$1<strong>$2</strong>");
  s = s.replace(/(^|[^*])\*([^\s*](?:[^*\n]*?[^\s*])?)\*(?!\*)/g, "$1<em>$2</em>");
  s = s.replace(/(^|[^\w])_([^\s_](?:[^_\n]*?[^\s_])?)_(?!\w)/g, "$1<em>$2</em>");
  return s;
}
function mdInline(raw) {
  var stash = [];
  function hold(html) { stash.push(html); return "\u0000" + (stash.length - 1) + "\u0001"; }
  var s = escapeHtml(String(raw == null ? "" : raw).replace(/[\u0000\u0001]/g, ""));
  s = s.replace(/`([^`\n]+)`/g, function (m, code) { return hold("<code>" + code + "</code>"); });
  s = s.replace(/\[([^\]\n]+)\]\(([^\s()]+)\)/g, function (m, label, url) {
    if (!/^https?:\/\/[^\s<>"']+$/i.test(url)) return m;
    return hold('<a href="' + url + '" rel="noopener noreferrer" target="_blank">' + mdEmphasis(label) + "</a>");
  });
  s = mdEmphasis(s);
  for (var i = 0; i < 3 && s.indexOf("\u0000") >= 0; i++) {
    s = s.replace(/\u0000(\d+)\u0001/g, function (m, n) { return stash[+n]; });
  }
  return s;
}
function renderMarkdown(src) {
  var lines = String(src == null ? "" : src).replace(/\r\n?/g, "\n").split("\n");
  var levels = [];
  lines.forEach(function (l) {
    var m = /^ {0,3}(#{1,4})\s+\S/.exec(l);
    if (m && levels.indexOf(m[1].length) < 0) levels.push(m[1].length);
  });
  levels.sort(function (a, b) { return a - b; }); // document outline: shallowest heading -> h4, next -> h5 (sections use h3)
  var out = [], para = [], list = null, blank = false;
  function flushPara() {
    if (!para.length) return;
    var raw = "";
    para.forEach(function (l, i) {
      if (i) raw += /\s{2,}$/.test(para[i - 1]) ? "\n" : " ";
      raw += l.trim();
    });
    para = [];
    var cls = /^(\*\*|__)[^*_\n]+\1[:.]?$/.test(raw) ? ' class="md-sub"' : (/^(\*\*|__)[^*_\n]+\1/.test(raw) ? ' class="md-lead"' : "");
    out.push("<p" + cls + ">" + mdInline(raw).replace(/\n/g, "<br>") + "</p>");
  }
  function listHtml(l) {
    return "<" + l.type + ">" + l.items.map(function (it) {
      return "<li>" + mdInline(it.text) + (it.sub ? listHtml(it.sub) : "") + "</li>";
    }).join("") + "</" + l.type + ">";
  }
  function flushList() { if (list) { out.push(listHtml(list)); list = null; } }
  lines.forEach(function (line) {
    if (!line.trim()) { flushPara(); blank = true; return; }
    var h = /^ {0,3}(#{1,4})\s+(.+?)\s*#*\s*$/.exec(line);
    if (h) {
      flushPara(); flushList(); blank = false;
      var lv = Math.min(4 + Math.max(levels.indexOf(h[1].length), 0), 6);
      out.push("<h" + lv + ">" + mdInline(h[2]) + "</h" + lv + ">");
      return;
    }
    if (/^ {0,3}([-*_])( *\1){2,} *$/.test(line)) { flushPara(); flushList(); blank = false; out.push("<hr>"); return; }
    var m = /^(\s*)([-*+]|\d{1,3}[.)])\s+(.*)$/.exec(line);
    if (m) {
      flushPara();
      var type = /^\d/.test(m[2]) ? "ol" : "ul", indent = m[1].replace(/\t/g, "    ").length;
      if (indent >= 2 && list && list.items.length) {
        var last = list.items[list.items.length - 1];
        if (!last.sub) last.sub = { type: type, items: [] };
        last.sub.items.push({ text: m[3] });
      } else {
        if (list && list.type !== type) flushList();
        if (!list) list = { type: type, items: [] };
        list.items.push({ text: m[3], sub: null });
      }
      blank = false;
      return;
    }
    if (list && !blank && /^\s+\S/.test(line)) {
      var top = list.items[list.items.length - 1], tgt = top.sub ? top.sub.items[top.sub.items.length - 1] : top;
      tgt.text += " " + line.trim();
      return;
    }
    flushList(); blank = false; para.push(line);
  });
  flushPara(); flushList();
  return out.join("");
}
function mdBlock(src) { return '<div class="md">' + renderMarkdown(src) + "</div>"; }
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
/* Meetings list helpers (pure, so they can be tested under node).
   meetingTime: a row's start time in epoch seconds, NaN when unknown.
   sortMeetings: newest first, ties broken by id descending -- the same total order the server uses
   (ORDER BY created DESC, session_id DESC), so pages and live updates always agree.
   groupMeetingsByDay: consecutive rows that share a calendar day in the browser's local timezone. */
function meetingTime(row) {
  var v = row && row.created;
  if (v == null || v === "") return NaN;
  if (typeof v === "number" || /^[0-9]+([.][0-9]+)?$/.test(String(v))) { var n = Number(v); return n >= 100000000000 ? n / 1000 : n; }
  var s = String(v), m = /^([0-9]{4})-([0-9]{2})-([0-9]{2})$/.exec(s);
  var ms = m ? new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])).getTime() : Date.parse(s);
  return isNaN(ms) ? NaN : ms / 1000;
}
function sortMeetings(rows) {
  return rows.slice().sort(function (a, b) {
    var ta = meetingTime(a), tb = meetingTime(b), na = isNaN(ta), nb = isNaN(tb);
    if (na !== nb) return na ? 1 : -1;
    if (!na && ta !== tb) return tb - ta;
    var ia = String(a.session_id), ib = String(b.session_id);
    return ia < ib ? 1 : (ia > ib ? -1 : 0);
  });
}
function dayKey(ts) {
  if (ts == null || isNaN(ts)) return "unknown";
  var d = new Date(ts * 1000);
  function two(n) { return (n < 10 ? "0" : "") + n; }
  return d.getFullYear() + "-" + two(d.getMonth() + 1) + "-" + two(d.getDate());
}
function dayLabel(ts, now, locale) {
  if (ts == null || isNaN(ts)) return "Unknown date";
  now = now || new Date();
  var d = new Date(ts * 1000), key = dayKey(ts);
  if (key === dayKey(now.getTime() / 1000)) return "Today";
  if (key === dayKey(new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1).getTime() / 1000)) return "Yesterday";
  var opts = {weekday: "long", month: "short", day: "numeric"};
  if (d.getFullYear() !== now.getFullYear()) opts.year = "numeric";
  return d.toLocaleDateString(locale, opts);
}
function groupMeetingsByDay(rows, now, locale) {
  var groups = [];
  rows.forEach(function (row) {
    var ts = meetingTime(row), key = dayKey(ts), last = groups[groups.length - 1];
    if (!last || last.key !== key) { last = {key: key, label: dayLabel(ts, now, locale), rows: []}; groups.push(last); }
    last.rows.push(row);
  });
  return groups;
}
function fmtBytes(bytes) {
  if (!bytes) return "0 B";
  var units = ["B", "KB", "MB", "GB"];
  var i = 0, n = bytes;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return n.toFixed(i === 0 ? 0 : 1) + " " + units[i];
}
function initials(label) {
  var parts = String(label || '').trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return '?';
  if (parts.length === 1) return parts[0].charAt(0).toUpperCase();
  var last = parts[parts.length - 1];
  return (parts[0].charAt(0) + (/^[0-9]+$/.test(last) ? last : last.charAt(0))).toUpperCase().slice(0, 2);
}
/* One transcript row: avatar, speaker name, timestamp, text. You and Them differ only in the
   avatar tint and the name weight, not in colour lanes. */
function speakerRow(o) {
  return '<div class="segment ' + (o.mic ? 'you' : 'them') + '"' + (o.attrs ? ' ' + o.attrs : '') + '><span class="avatar" aria-hidden="true">' + escapeHtml(initials(o.label)) + '</span><div class="seg-body"><div class="seg-head"><span class="label">' + escapeHtml(o.label) + '</span><span class="ts">' + fmtDuration(o.start) + '</span></div><div class="text' + (o.approximate ? ' approximate' : '') + '">' + escapeHtml(o.text) + '</div></div></div>';
}
function stateBadge(row) {
  var state = row.latest_state;
  if (!state) return badge('none', 'No job yet');
  if (state === "running" || state === "queued") {
    var pct = row.latest_progress != null ? Math.round(row.latest_progress * 100) + "%" : "";
    return badge('running', state.charAt(0).toUpperCase() + state.slice(1) + (pct ? " " + pct : ""));
  }
  if (state === "error") {
    return badge('error', 'Error' + (row.latest_error ? ": " + row.latest_error : ""));
  }
  return badge('done', 'Done');
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
  var cls = status.key === "complete" ? "done" : (status.key === "error" ? "error" : (status.key === "transcribing" || status.key === "queued" || status.key === "uploading" ? "running" : "none"));
  var errorDetail = status.detail || row.latest_error || "";
  return badge(cls, status.label + pct, status.key === "error" ? errorDetail : "");
}
"""

_JS_HELPERS = _JS_HELPERS_SRC

# Native <dialog> used by confirmDialog() (JS helpers above); styled by .dialog in app.css.
_NOTE_TYPE_TIP = (
    "A note type decides two things: how the AI writes the summary, and which Notion page the notes are saved under. "
    "Changing the type and regenerating rewrites the summary and moves the Notion copy to that type's page."
)

_CONFIRM_DIALOG_HTML = """<dialog class="dialog" id="confirm-dialog" aria-labelledby="confirm-title" aria-describedby="confirm-lead">
  <form method="dialog" class="dialog-form">
    <h2 class="dialog-title" id="confirm-title"></h2>
    <p class="dialog-lead" id="confirm-lead"></p>
    <p class="dialog-warning" role="alert" hidden></p>
    <ul class="dialog-items" tabindex="0" aria-label="Meetings" hidden></ul>
    <p class="dialog-note"></p>
    <div class="dialog-foot">
      <button type="submit" value="cancel" class="btn secondary" data-dialog-cancel>Cancel</button>
      <button type="submit" value="ok" class="btn danger" data-dialog-ok>Confirm</button>
    </div>
  </form>
</dialog>"""


def render_login_page(error: bool = False, *, appearance: str = "system") -> str:
    error_html = (
        '<p class="error-text" role="alert">Invalid token. Check it and try again.</p>' if error else ""
    )
    body = f"""
<div class="login-wrap">
  <div class="login-card">
    <span class="mark login-mark">{_icon("mark", 20)}</span>
    <h1>Sign in to Meeting Notes</h1>
    <form method="post" action="/login">
      <label class="field">
        <span class="name">Server token</span>
        <input type="password" name="token" autofocus required autocomplete="current-password"{' aria-invalid="true"' if error else ""}>
      </label>
      {error_html}
      <button type="submit" class="btn primary block">Sign in</button>
    </form>
  </div>
</div>
"""
    return _shell("Sign in", body, token_configured=True, nav=False, main_class="auth-page", appearance=appearance)


def render_home_page(*, token_configured: bool, appearance: str = "system") -> str:
    skeleton = "".join(
        '<li class="mrow skel" aria-hidden="true"><span class="skel-bar w1"></span><span class="skel-bar w2"></span></li>'
        for _ in range(4)
    )
    body = (
        f"""
<div class="page">
<header class="page-head">
  <h1>Home</h1>
  <p class="summary-line" aria-label="Library summary"><span id="total-count">—</span> meetings · <span id="live-count">0</span> live · <span id="audio-count">—</span> with audio</p>
</header>
<section id="live-section" style="display:none" aria-labelledby="live-heading">
  <div class="sec-head"><h2 id="live-heading"><span class="live-dot" aria-hidden="true"></span>Live transcription</h2></div>
  <div id="live-list" class="live-list"></div>
</section>
<div class="home-grid">
  <section aria-labelledby="recent-heading">
    <div class="sec-head"><h2 id="recent-heading">Recent meetings</h2><a href="/meetings">View all meetings</a></div>
    <ul id="recent-list" class="mlist" aria-busy="true">{skeleton}</ul>
  </section>
  <section class="side-panel" aria-labelledby="upload-heading">
    <h2 id="upload-heading">Add a meeting</h2>
    <div class="tabs upload-tabs" role="group" aria-label="What to add">
      <button class="active" id="tab-recording" type="button" aria-pressed="true">Recording</button>
      <button id="tab-transcript" type="button" aria-pressed="false">Transcript</button>
    </div>
    <p class="help" id="upload-help">Drop in an audio file and Meeting Notes will upload and transcribe it. MP3, WAV, M4A, MP4, FLAC, OGG, OGA, Opus, AAC, and WebM are supported.</p>
    <form id="recording-upload" class="upload-form">
      <label class="field"><span class="name">Recording</span><input id="recording-file" type="file" accept="audio/*,.mp3,.wav,.m4a,.mp4,.flac,.ogg,.oga,.opus,.aac,.webm" required></label>
      <label class="field"><span class="name">Meeting name <span class="optional">(optional)</span></span><input id="recording-name" type="text" maxlength="200" placeholder="e.g. Weekly standup"></label>
      <button type="submit" class="btn primary" id="upload-submit">Upload recording</button>
    </form>
    <div class="upload-status" id="upload-status" role="status" aria-live="polite"></div>
    <div class="progress-track" id="upload-progress-track" hidden><i id="upload-progress"></i></div>
    <form id="transcript-upload" class="upload-form" hidden>
      <label class="field"><span class="name">Transcript file <span class="optional">(.txt, .vtt or .srt)</span></span><input id="transcript-file" type="file" accept=".txt,.vtt,.srt,text/plain,text/vtt"></label>
      <label class="field"><span class="name">Or paste the transcript</span><textarea id="transcript-text" rows="6" placeholder="Jane: Hello everyone&#10;Bob: Thanks for joining"></textarea></label>
      <label class="field"><span class="name">Meeting name <span class="optional">(optional)</span></span><input id="transcript-name" type="text" maxlength="200" placeholder="e.g. Weekly standup"></label>
      <label class="field"><span class="name">Date and time</span><input id="transcript-when" type="datetime-local" required></label>
      <button type="submit" class="btn primary" id="transcript-submit">Upload transcript</button>
    </form>
    <div class="upload-status" id="transcript-status" role="status" aria-live="polite" hidden></div>
  </section>
</div>
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
var uploadHelp = {
  recording: 'Drop in an audio file and Meeting Notes will upload and transcribe it. MP3, WAV, M4A, MP4, FLAC, OGG, OGA, Opus, AAC, and WebM are supported.',
  transcript: 'Already have a transcript, from Teams, Zoom or a text file? Add it here: nothing is transcribed again. Times and speaker names are kept when the text has them.'
};
function showUploadTab(which) {
  var transcript = which === 'transcript';
  document.getElementById('recording-upload').hidden = transcript;
  document.getElementById('transcript-upload').hidden = !transcript;
  document.getElementById('upload-status').hidden = transcript;
  document.getElementById('transcript-status').hidden = !transcript;
  document.getElementById('tab-recording').classList.toggle('active', !transcript);
  document.getElementById('tab-transcript').classList.toggle('active', transcript);
  document.getElementById('tab-recording').setAttribute('aria-pressed', String(!transcript));
  document.getElementById('tab-transcript').setAttribute('aria-pressed', String(transcript));
  document.getElementById('upload-help').textContent = uploadHelp[which];
}
document.getElementById('tab-recording').addEventListener('click', function() { showUploadTab('recording'); });
document.getElementById('tab-transcript').addEventListener('click', function() { showUploadTab('transcript'); });
var transcriptForm = document.getElementById('transcript-upload'), transcriptFileName = '';
document.getElementById('transcript-when').value = localInputValue(new Date());
document.getElementById('transcript-file').addEventListener('change', function() {
  var file = this.files[0], input = this, status = document.getElementById('transcript-status');
  status.classList.remove('err'); status.textContent = '';
  if (!file) { transcriptFileName = ''; return; }
  readTranscriptFile(file, function(error, info) {
    if (error) { input.value = ''; transcriptFileName = ''; status.classList.add('err'); status.textContent = error; return; }
    transcriptFileName = info.filename;
    document.getElementById('transcript-text').value = info.text;
    var name = document.getElementById('transcript-name');
    if (!name.value.trim()) name.value = info.name;
    if (info.when) document.getElementById('transcript-when').value = info.when;
  });
});
transcriptForm.addEventListener('submit', function(event) {
  event.preventDefault();
  var status = document.getElementById('transcript-status'), submit = document.getElementById('transcript-submit');
  status.classList.remove('err');
  var built = buildTranscriptPayload({text: document.getElementById('transcript-text').value, name: document.getElementById('transcript-name').value, filename: transcriptFileName, when: document.getElementById('transcript-when').value});
  if (built.error) { status.classList.add('err'); status.textContent = built.error; return; }
  submit.disabled = true; status.textContent = 'Adding the transcript…';
  postTranscript(built.payload).then(function(data) {
    submit.disabled = false;
    status.textContent = 'Transcript added — opening meeting…';
    setTimeout(function() { location.href = '/sessions/' + encodeURIComponent(data.session_id); }, 400);
  }, function(err) { submit.disabled = false; status.classList.add('err'); status.textContent = err.message; });
});
var liveItems = [];
var activeLiveId = null;
var liveOverlayPreviousFocus = null;

function liveText(item) {
  return (item.partials || []).slice(-20).map(function (p) {
    var mic = p.track === 'mic';
    return speakerRow({mic: mic, label: mic ? 'You' : 'Them', start: p.start, text: p.text});
  }).join('');
}
function liveCard(item) {
  return '<div class="live-card" role="button" tabindex="0" data-live-id="' + escapeHtml(item.session_id) + '" aria-label="Open live transcript for ' + escapeHtml(item.name) + '"><div class="live-card-head"><span class="badge live">' + dot() + 'Live</span><h3>' + escapeHtml(item.name) + '</h3><span class="open-hint">Open transcript ' + icon('open') + '</span></div><div class="help">' + escapeHtml(item.device) + ' · started ' + fmtDate(item.started_wall) + '</div>' + (liveText(item) || '<div class="empty">Listening for speech…</div>') + '</div>';
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
function recentRow(row) {
  var notesReady = row.review && row.review.status === 'done';
  var length = Number(row.duration_sec) > 0 ? ' · ' + fmtDuration(row.duration_sec) : '';
  return '<li class="mrow"><a class="mrow-main" href="/sessions/' + encodeURIComponent(row.session_id) + '"><span class="mrow-title">' + escapeHtml(row.name || row.session_id) + '</span><span class="mrow-sub">' + fmtDate(row.created, true) + ' · ' + escapeHtml(row.device || 'Unknown device') + length + '</span></a><span class="mrow-badges">' + processingBadge(row) + (notesReady ? badge('done', 'Notes ready') : '') + '</span></li>';
}
function loadOverview() {
  fetch('/v1/sessions?per_page=8', {credentials:'same-origin'}).then(function (r) { if (!r.ok) throw new Error('load failed'); return r.json(); }).then(function (data) {
    document.getElementById('total-count').textContent = data.total;
    document.getElementById('audio-count').textContent = data.audio_total == null ? '—' : data.audio_total;
    var recent = document.getElementById('recent-list');
    var markup = data.items.length ? data.items.map(recentRow).join('') : '<li class="empty-teach"><h3>No meetings yet</h3><p>Record a meeting with the Windows client, or upload a recording here. Finished meetings appear in your library with their notes.</p><div class="row"><a class="btn primary" href="/install">Install the Windows client</a></div></li>';
    if (recent._lastMarkup !== markup) { recent.innerHTML = markup; recent._lastMarkup = markup; }
    recent.setAttribute('aria-busy', 'false');
  }).catch(function () {
    var recent = document.getElementById('recent-list');
    if (recent._lastMarkup == null) { recent.innerHTML = '<li class="empty">Could not load recent meetings. Retrying…</li>'; recent.setAttribute('aria-busy', 'false'); }
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
    <div class="doc-bar"><button class="btn ghost" id="live-close" type="button" aria-label="Close live transcript">"""
        + _icon("back")
        + r"""<span>Back</span></button></div>
    <div class="doc-scroll"><div class="doc-wrap">
      <header class="doc-title"><h1 id="live-overlay-title">Live transcript</h1><div class="meta" id="live-overlay-meta"></div></header>
      <form class="rename-live" id="live-name-form"><label class="sr-only" for="live-name">Meeting name</label><input type="text" id="live-name" maxlength="200" autocomplete="off" required placeholder="Meeting name"><button type="submit" class="btn secondary">Save name</button><span class="help" id="live-name-status" role="status"></span></form>
      <div class="live-transcript-scroll" id="live-transcript-scroll" tabindex="0" aria-label="Live transcript text" aria-live="polite"><div id="live-transcript-content" class="transcript"></div></div>
    </div></div>
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
    return _shell("Home", body, token_configured=token_configured, active="home", appearance=appearance)



# -- meetings (formerly "saved transcriptions") ---------------------------------------------------


def render_transcriptions_page(
    *,
    token_configured: bool,
    initial_session_id: Optional[str] = None,
    initial_view: Optional[str] = None,
    ai_enabled: bool = True,
    appearance: str = "system",
    page_title: Optional[str] = None,
) -> str:
    """The Meetings page: the list, and the meeting document (detail overlay).

    ``initial_view="notes"`` opens ``initial_session_id`` straight onto its
    meeting notes (the server knows the notes are finished). ``ai_enabled``
    gates the per-row "Generate" button shown for meetings without notes.

    Everything in the list comes from ``GET /v1/sessions`` (search, state
    filter and pagination included) and the document from
    ``GET /v1/sessions/{id}``, so the page never has to know how many
    meetings exist. The timeline is drawn from that detail response's
    ``segments`` (``start``/``end`` seconds plus ``track``/``label``).
    """
    initial = json.dumps(initial_session_id)
    initial_view_js = json.dumps(initial_view)
    skeleton = "".join(
        '<li class="mrow skel" aria-hidden="true"><span class="skel-bar w1"></span><span class="skel-bar w2"></span></li>'
        for _ in range(7)
    )
    body = (
        f"""
<div class="page list-page">
<header class="page-head">
  <h1>Meetings</h1>
  <span class="count" id="meeting-count" aria-live="polite"></span>
  <div class="head-tools">
    <button type="button" class="btn secondary" id="open-transcript-upload" aria-haspopup="dialog">{_icon("upload")}<span>Upload transcript</span></button>
    <a class="btn ghost" href="/meetings/trash" id="open-trash">{_icon("trash")}<span>Recently deleted</span></a>
    <label class="sr-only" for="state">Filter by state</label>
    <select id="state"><option value="">All states</option><option value="done">Complete</option><option value="running">Running</option><option value="queued">Queued</option><option value="error">Error</option></select>
  </div>
</header>
<div class="list-error" id="list-error" role="alert" hidden><span>Could not load the meetings list. It will retry on its own.</span><button type="button" class="btn secondary" id="list-retry">Try again</button></div>
<div class="mlist-head" aria-hidden="false">
  <label class="sel"><input id="select-all" type="checkbox" aria-label="Select all visible meetings"></label>
  <span class="col-name">Name</span><span class="col-status">Transcript</span><span class="col-notes">Notes</span>
</div>
<ul class="mlist" id="rows" aria-busy="true">{skeleton}</ul>
<footer class="pager"><button id="more" class="btn secondary" style="display:none">Load more</button></footer>
</div>
<div class="bulk-actions" role="region" aria-label="Bulk actions">
  <span class="selection-count" id="selection-count" role="status">Select meetings for bulk actions</span>
  <span class="bulk-buttons">
    <button class="btn primary" id="bulk-build" disabled>Build meeting notes</button>
    <button class="btn secondary" id="bulk-notion" disabled title="Copies each meeting's notes to the Notion page set for its note type">Send to Notion</button>
    <button class="btn secondary" id="bulk-retranscribe" disabled>Retranscribe</button>
    <button class="btn secondary" id="bulk-combine" disabled title="Select two or more meetings to combine">Combine</button>
    <button class="btn danger" id="bulk-delete-audio" disabled>Delete audio</button>
    <button class="btn danger" id="bulk-delete" disabled>Delete</button>
  </span>
  <button class="btn ghost icon-only" id="bulk-clear" type="button" aria-label="Clear selection" title="Clear selection">{_icon("x")}</button>
</div>

{_CONFIRM_DIALOG_HTML}
<dialog class="dialog tu-dialog" id="tu-dialog" aria-labelledby="tu-title" aria-describedby="tu-lead">
  <form class="dialog-form" id="tu-form" novalidate>
    <h2 class="dialog-title" id="tu-title">Upload a transcript</h2>
    <p class="dialog-lead" id="tu-lead">Already have a transcript, from Teams, Zoom or a text file? Add it here: nothing is transcribed again. Times and speaker names are kept when the text has them.</p>
    <label class="field"><span class="name">Transcript file <span class="optional">(.txt, .vtt or .srt)</span></span><input id="tu-file" type="file" accept=".txt,.vtt,.srt,text/plain,text/vtt"></label>
    <label class="field"><span class="name">Or paste the transcript</span><textarea id="tu-text" rows="7" placeholder="Jane: Hello everyone&#10;Bob: Thanks for joining"></textarea></label>
    <label class="field"><span class="name">Meeting name <span class="optional">(optional)</span></span><input id="tu-name" type="text" maxlength="200" autocomplete="off" placeholder="e.g. Weekly standup"></label>
    <label class="field"><span class="name">Date and time</span><input id="tu-when" type="datetime-local" required></label>
    <p class="err" id="tu-error" role="alert"></p>
    <div class="dialog-foot">
      <button type="button" class="btn secondary" id="tu-cancel">Cancel</button>
      <button type="submit" class="btn primary" id="tu-submit">Upload transcript</button>
    </div>
  </form>
</dialog>
{splitmerge_ui.dialogs_html(_icon)}
<div class="overlay" id="detail-overlay" role="dialog" aria-modal="true" aria-hidden="true" aria-labelledby="overlay-title">
  <div class="overlay-inner"><div class="sheet" id="sheet" data-view="transcript">
    <div class="doc-bar">
      <button class="btn ghost" id="close-overlay" type="button" aria-label="Back to meetings">{_icon("back")}<span>Meetings</span></button>
      <span class="doc-status" id="review-status" role="status"></span>
      <div class="doc-actions">
        <button class="btn secondary notes-only" id="notes-copy" type="button" aria-label="Copy notes as Markdown">{_icon("copy")}<span>Copy</span></button>
        <button class="btn secondary notes-only" id="notes-download" type="button" aria-label="Download notes as Markdown">{_icon("download")}<span>Download .md</span></button>
        <div class="menu-wrap">
          <button class="btn ghost icon-only" id="doc-more" type="button" aria-haspopup="menu" aria-expanded="false" aria-controls="doc-menu" aria-label="More actions" title="More actions">{_icon("more")}</button>
          <div class="menu" id="doc-menu" role="menu" aria-label="Meeting actions" hidden>
            <button type="button" role="menuitem" id="edit-meeting-name" aria-expanded="false">{_icon("edit")}<span>Rename meeting</span></button>
            <button type="button" role="menuitem" id="edit-summary-name" aria-expanded="false">{_icon("edit")}<span>Rename summary</span></button>
            <button type="button" role="menuitem" id="notes-retry">{_icon("refresh")}<span>Regenerate notes</span></button>
            <button type="button" role="menuitem" id="retranscribe">{_icon("refresh")}<span>Retranscribe</span></button>
            <button type="button" role="menuitem" id="split-meeting">{_icon("split")}<span>Split meeting…</span></button>
            <button type="button" role="menuitem" id="combine-meeting">{_icon("merge")}<span>Combine with another meeting…</span></button>
            <div class="menu-sep" role="separator"></div>
            <button type="button" role="menuitem" class="danger-item" id="delete-audio">{_icon("trash")}<span>Delete audio</span></button>
            <button type="button" role="menuitem" class="danger-item" id="delete-entry">{_icon("trash")}<span>Delete meeting</span></button>
          </div>
        </div>
      </div>
    </div>
    <div class="doc-scroll" id="sheet-body">
    <div class="type-bar" id="type-bar" hidden>
      <div class="type-row">
        <label class="type-label" for="notes-template">Note type</label>
        <select id="notes-template" class="style-select" title="The note type decides how the summary is written and where it is saved in Notion" hidden></select>
        <button class="btn secondary sm notes-only" id="notes-regen" type="button" hidden title="{_NOTE_TYPE_TIP}">{_icon("refresh")}<span>Regenerate</span></button>
      </div>
      <div class="notion-box" id="notion-box" title="{_NOTE_TYPE_TIP}" hidden></div>
    </div><div class="doc-wrap">
      <header class="doc-title">
        <h1 id="overlay-title" title="Click to rename">Meeting</h1>
        <form class="rename" id="rename-form" hidden><label class="sr-only" for="rename-input">Meeting name</label><input type="text" id="rename-input" maxlength="200" autocomplete="off" required><button type="submit" class="btn primary">Save name</button><button type="button" class="btn ghost" id="rename-cancel">Cancel</button><p class="err" id="rename-error" role="alert"></p></form>
        <div class="meta"><span id="overlay-meta"></span><span class="mid" id="overlay-board" hidden></span><span class="mid" id="overlay-style" title="The note type used for these notes: it set how they were written and where they are saved in Notion" hidden></span></div>
      </header>
      {splitmerge_ui.continuation_hint_html(_icon("merge"), _icon("x"))}
      <div class="tabs" role="group" aria-label="Meeting views">
        <button id="queue-review" type="button" aria-pressed="false">Notes</button>
        <button class="active" id="show-transcript" type="button" aria-pressed="true">Transcript</button>
      </div>
      <section class="strip-wrap" id="strip-wrap" aria-label="Session timeline" hidden>
        <div class="strip-head"><span class="strip-title">Timeline</span><span class="strip-help">Select a block to jump to that moment in the transcript.</span><span class="strip-total" id="strip-total"></span></div>
        <div class="strip" id="session-strip" role="group" aria-label="Session timeline, one block per transcript segment. Arrow keys move between blocks, Enter shows the segment in the transcript."></div>
      </section>
      <section class="transcript-pane" id="transcript-pane" aria-label="Transcript"><div id="overlay-segments" class="transcript"></div></section>
      <section class="notes-head" id="notes-pane" hidden>
        <h2 id="notes-title">Meeting summary</h2>
        <form class="rename" id="summary-rename-form" hidden><label class="sr-only" for="summary-rename-input">Meeting summary name</label><input type="text" id="summary-rename-input" maxlength="200" autocomplete="off" required><button type="submit" class="btn primary">Save name</button><button type="button" class="btn ghost" id="summary-rename-cancel">Cancel</button><p class="err" id="summary-rename-error" role="alert"></p></form>
        <div class="help notes-meta" id="notes-meta"></div>
        <div id="notes-state" class="notes-state" role="status"></div>
      </section>
      <section id="notes-document-pane" hidden><article id="notes-document" class="notes-doc" aria-label="Meeting summary"></article></section>
      <details class="extras" id="meeting-extras"><summary>Recording and processing details</summary><div class="extras-body">
        <section aria-labelledby="transcription-progress-heading"><h2 id="transcription-progress-heading">Processing status</h2><ol class="checklist" id="transcription-checklist"></ol></section>
        <div id="audio-players" class="audio-grid"></div>
      </div></details>
    </div></div>
  </div></div>
</div>
<script>
"""
        + _JS_HELPERS
        + "\n"
        + f"var aiEnabled = {json.dumps(bool(ai_enabled))};"
        + r"""
var listState = {page:1, perPage:50, loaded:0, total:0, items:[], seq:0};
var rowInfo = {};
var currentSession = null;
var detailPollTimer = null;
var detailRequest = 0;
var currentReview = null;
var noteTemplates = [], defaultTemplateId = '', reviewTemplateId = '';
var notesPollTimer = null;
var currentMarkdown = '';
var notesRequest = 0;
var notionTimer = null, notionRequest = 0;
var detailReturnFocus = null, detailReturnKey = null;
var transcriptExplicit = false;
var pendingNotesDefault = false;
var stripState = {data:null, drawnFor:null, hit:null};
var renameMeeting = null, renameSummary = null;
var sheetEl = document.getElementById('sheet');
var menuEl = document.getElementById('doc-menu'), moreBtn = document.getElementById('doc-more');

function skeletonLines(){return '<div class="notes-loading" aria-hidden="true"><span class="skel-bar" style="width:70%"></span><span class="skel-bar"></span><span class="skel-bar" style="width:85%"></span><span class="skel-bar" style="width:55%"></span></div><span class="sr-only">Loading…</span>';}
function notesBadge(row) {
  var status=String((row.review||{}).status||'none').toLowerCase();
  if(status==='done')return badge('done','Notes ready');
  if(status==='running'||status==='queued')return badge('running','Building notes');
  if(status==='error')return badge('error','Notes need attention');
  var generate=(aiEnabled&&processingState(row).key==='complete')?'<button type="button" class="btn secondary sm notes-generate" data-generate="'+escapeHtml(row.session_id)+'" aria-label="Generate meeting notes for '+escapeHtml(row.name||row.session_id)+'">'+icon('sparkles',14)+'<span>Generate</span></button>':'';
  return badge('none notes-none','Not created')+generate;
}
function generateNotes(btn) {
  if (btn.disabled) return;
  btn.disabled = true; btn.querySelector('span').textContent = 'Queuing…';
  fetch('/v1/sessions/'+encodeURIComponent(btn.dataset.generate)+'/review', {method:'POST', credentials:'same-origin'})
    .then(function(r) { if (!r.ok) throw new Error('Unable to queue meeting notes'); return r.json(); })
    .then(function() { var cell=btn.closest('.mrow-notes'); if(cell)cell.innerHTML=notesBadge({review:{status:'queued'}}); return loadRows(true); })
    .catch(function(e) { btn.disabled = false; btn.querySelector('span').textContent = 'Generate'; notify(e.message,'error'); });
}
function notionChip(row) {
  var n=row.notion;if(!n||!n.state)return '';
  if(n.state==='copied'){var href=n.url||'';return href?'<a class="badge done notion-chip" href="'+escapeHtml(href)+'" target="_blank" rel="noopener" title="Open this meeting in Notion">'+dot()+'<span class="badge-text">In Notion</span></a>':badge('done notion-chip','In Notion');}
  if(n.state==='pending')return badge('running notion-chip',n.retrying?'Retrying…':'Sending…',n.retrying&&n.error?n.error:'Sending to Notion');
  return '<a class="badge error notion-chip row-open" href="/sessions/'+encodeURIComponent(row.session_id)+'" title="'+escapeHtml(n.error||'The copy to Notion failed. Open the meeting to retry.')+'">'+dot()+'<span class="badge-text">Notion failed</span></a>';
}
function meetingRow(row) {
  var name=escapeHtml(row.name||row.session_id),status=String((row.review||{}).status||'none').toLowerCase(),id=escapeHtml(row.session_id);
  var d=Math.max(0,Number(row.duration_sec)||0);
  var sub=fmtDate(row.created,true)+' · '+escapeHtml(row.device||row.platform||'Unknown device')+(d?' · '+fmtDuration(d):'');
  return '<li class="mrow'+(status==='done'?' notes-ready':(status==='queued'||status==='running'?' notes-pending':''))+'" data-id="'+id+'"><label class="sel"><span class="row-ic">'+icon('notes')+'</span><input class="row-select" type="checkbox" value="'+id+'" aria-label="Select '+name+'"></label><div class="mrow-main"><a class="row-open mrow-title" href="/sessions/'+encodeURIComponent(row.session_id)+'">'+name+'</a><span class="mrow-sub">'+sub+'<span class="sub-id"> · <span class="mid">'+escapeHtml(row.board||'')+'</span></span><span class="sub-audio"> · '+(row.has_audio?fmtBytes(row.audio_bytes):'No audio')+'</span></span></div><div class="mrow-status">'+processingBadge(row)+'</div><div class="mrow-notes">'+notesBadge(row)+notionChip(row)+'</div><span class="mrow-go" aria-hidden="true">'+icon('open')+'</span></li>';
}
function emptyRows() {
  var q=document.getElementById('q').value.trim(), st=document.getElementById('state').value;
  if(q||st)return '<li class="state-row"><div class="empty"><p><strong>No meetings found.</strong> Nothing matches '+(q?'&quot;'+escapeHtml(q)+'&quot;':'that state')+'.</p><button type="button" class="btn secondary" id="clear-filters">Clear search and filter</button></div></li>';
  return '<li class="state-row"><div class="empty-teach"><h2>No meetings yet</h2><p>Each recording you make becomes a meeting here, with its transcript and notes.</p><ol><li>Install the Windows client and record a call, or</li><li>upload an audio file from Home.</li></ol><div class="row"><a class="btn primary" href="/install">Install the Windows client</a><a class="btn secondary" href="/">Upload a recording</a></div></div></li>';
}
function selectedIds() { return Array.from(document.querySelectorAll('.row-select:checked')).map(function(el) { return el.value; }); }
function updateSelection() {
  var ids = selectedIds(), disabled = !ids.length;
  document.getElementById('selection-count').textContent = ids.length ? ids.length + ' selected' : 'Select meetings for bulk actions';
  document.querySelector('.bulk-actions').classList.toggle('has-selection',!!ids.length);
  document.getElementById('rows').classList.toggle('has-sel',!!ids.length);
  document.body.classList.toggle('has-bulk',!!ids.length);
  ['bulk-build','bulk-notion','bulk-retranscribe','bulk-delete-audio','bulk-delete'].forEach(function(id) { document.getElementById(id).disabled = disabled; });
  var all = document.querySelectorAll('.row-select'), master = document.getElementById('select-all');
  master.checked = !!all.length && ids.length === all.length;
  master.indeterminate = ids.length > 0 && ids.length < all.length;
}
function meetingItems(ids) { return ids.map(function(id) { return meetingSummary(rowInfo[id] || {session_id:id}); }); }
function deleteAudioDialog(ids) {
  return confirmDialog({
    title: ids.length === 1 ? 'Delete the audio for this meeting?' : 'Delete the audio for ' + ids.length + ' meetings?',
    lead: 'The recordings are removed from the server. Transcripts and notes are kept. This cannot be undone.',
    items: meetingItems(ids), confirmLabel: 'Delete audio'
  });
}
function trashDialog(ids) {
  return confirmDialog({
    title: ids.length === 1 ? 'Move this meeting to Recently deleted?' : 'Move ' + ids.length + ' meetings to Recently deleted?',
    lead: 'You can restore ' + (ids.length === 1 ? 'it' : 'them') + ' from Recently deleted for 30 days. After that ' + (ids.length === 1 ? 'it is' : 'they are') + ' removed permanently.',
    items: meetingItems(ids), confirmLabel: 'Move to Recently deleted'
  });
}
// Soft delete: the server moves each meeting to Recently deleted; the toast offers Undo.
function moveToTrash(ids, via) {
  return Promise.allSettled(ids.map(function(id) {
    return fetch('/v1/sessions/'+encodeURIComponent(id)+'?via='+via, {method:'DELETE', credentials:'same-origin'}).then(function(r) { if (!r.ok) throw new Error('Delete failed'); return id; });
  })).then(function(results) {
    var done = results.filter(function(r) { return r.status === 'fulfilled'; }).map(function(r) { return r.value; });
    var failed = ids.length - done.length;
    return loadRows(true).then(function(refreshed) {
      var message = done.length ? plural(done.length, 'meeting', 'meetings') + ' moved to Recently deleted' + (failed ? '. ' + failed + ' could not be deleted.' : '') : failed + ' could not be deleted.';
      if (done.length) notify(message, '', {label:'Undo', onClick:function() { undoTrash(done); }});
      else notify(message, 'error');
      if (!refreshed) notify('Could not refresh the meetings list. Please try again.', 'error');
    });
  });
}
function undoTrash(ids) {
  Promise.allSettled(ids.map(function(id) {
    return fetch('/v1/trash/'+encodeURIComponent(id)+'/restore', {method:'POST', credentials:'same-origin'}).then(function(r) { if (!r.ok) throw new Error('Restore failed'); });
  })).then(function(results) {
    var failed = results.filter(function(r) { return r.status === 'rejected'; }).length;
    return loadRows(true).then(function() {
      if (failed) notify(failed + ' could not be restored. Find ' + (failed === 1 ? 'it' : 'them') + ' in Recently deleted.', 'error');
      else notify(ids.length === 1 ? 'Meeting restored.' : ids.length + ' meetings restored.');
    });
  });
}
function runBulk(path, method, confirmed) {
  var ids = selectedIds(); if (!ids.length) return;
  var buttons = ['bulk-build','bulk-notion','bulk-retranscribe','bulk-delete-audio','bulk-delete']; buttons.forEach(function(id) { document.getElementById(id).disabled = true; });
  Promise.allSettled(ids.map(function(id) { return fetch('/v1/sessions/'+encodeURIComponent(id)+path, {method:method, credentials:'same-origin'}).then(function(r) { if (!r.ok) throw new Error('Action failed for '+id); return r; }); }))
    .then(function(results) { return loadRows(true).then(function(refreshed) { var failed=results.filter(function(result){return result.status==='rejected';}); if(failed.length)notify(failed.length+' of '+ids.length+' actions failed.','error'); else notify('Done for '+ids.length+' meeting'+(ids.length===1?'':'s')+'.'); if(!refreshed)notify('Could not refresh the meetings list. Please try again.','error'); }); });
}
function bulkNotion() {
  var ids = selectedIds(); if (!ids.length) return;
  var withNotes = ids.filter(function(id) { var row = rowInfo[id]; return row && row.review && String(row.review.status || '').toLowerCase() === 'done'; });
  var skipped = ids.length - withNotes.length;
  if (!withNotes.length) { notify('None of the selected meetings have notes yet.', 'error'); return; }
  var button = document.getElementById('bulk-notion'); button.disabled = true;
  Promise.allSettled(withNotes.map(function(id) {
    return fetch('/v1/sessions/' + encodeURIComponent(id) + '/notion', {method:'POST', credentials:'same-origin'}).then(function(r) {
      if (r.ok) return r;
      return r.json().catch(function() { return {}; }).then(function(d) { throw new Error((d && typeof d.detail === 'string' && d.detail) || ('Could not send to Notion (HTTP ' + r.status + ').')); });
    });
  })).then(function(results) {
    var failed = results.filter(function(x) { return x.status === 'rejected'; }), sent = withNotes.length - failed.length;
    if (sent) notify('Sent ' + sent + ' to Notion.' + (skipped ? ' ' + skipped + ' skipped (no notes).' : ''));
    else if (skipped) notify(skipped + ' skipped (no notes).');
    if (failed.length) notify(failed.length + ' failed: ' + (failed[0].reason && failed[0].reason.message || 'Could not send to Notion.'), 'error');
    updateSelection(); loadRows(true);
  });
}
function dayHeadMarkup(group) { return '<li class="day-head" data-day="'+escapeHtml(group.key)+'"><h2 class="day-title">'+escapeHtml(group.label)+'</h2></li>'; }
// One flat list: a day header, then that day's meetings, newest first. Existing nodes are kept and moved into
// place (never cleared) so the five-second refresh does not flash, and a meeting whose start time changed moves.
function renderMeetingList() {
  var rows = document.getElementById('rows');
  if (!listState.items.length) { rows.innerHTML = emptyRows(); return; }
  var desired = [];
  groupMeetingsByDay(sortMeetings(listState.items), new Date()).forEach(function(group) {
    desired.push({key:'d:'+group.key, html:dayHeadMarkup(group)});
    group.rows.forEach(function(item) { desired.push({key:'r:'+item.session_id, html:meetingRow(item)}); });
  });
  var existing = {}, stale = [];
  Array.from(rows.children).forEach(function(el) { if (el.dataset.key) existing[el.dataset.key] = el; else stale.push(el); });
  var cursor = rows.firstElementChild;
  desired.forEach(function(entry) {
    var scratch = document.createElement('ul'); scratch.innerHTML = entry.html;
    var replacement = scratch.firstElementChild, oldRow = existing[entry.key];
    if (oldRow) {
      delete existing[entry.key];
      var oldBox = oldRow.querySelector('.row-select'), wasChecked = oldBox && oldBox.checked;
      if (oldRow.innerHTML !== replacement.innerHTML) oldRow.replaceChildren.apply(oldRow, Array.from(replacement.childNodes));
      if (wasChecked) oldRow.querySelector('.row-select').checked = true;
      oldRow.className=replacement.className;
    } else { oldRow = replacement; oldRow.dataset.key = entry.key; }
    if (oldRow === cursor) cursor = cursor.nextElementSibling; else rows.insertBefore(oldRow, cursor);
  });
  Object.keys(existing).forEach(function(key) { existing[key].remove(); });
  stale.forEach(function(el) { el.remove(); });
}
function loadRows(reset) {
  var preservedSelection = reset ? selectedIds() : [];
  var previousLoaded = listState.loaded, hadRows = previousLoaded > 0;
  var ticket = ++listState.seq;
  if (reset) { listState.page=1; listState.loaded=0; }
  var url='/v1/sessions?page='+listState.page+'&per_page='+listState.perPage;
  var q=document.getElementById('q').value.trim(), state=document.getElementById('state').value;
  if(q) url+='&q='+encodeURIComponent(q); if(state) url+='&state='+encodeURIComponent(state);
  return fetch(url,{credentials:'same-origin'}).then(r=>{if(r.status===401||r.status===403){window.location='/login';throw new Error('Signed out');}if(!r.ok)throw new Error('Unable to load meetings');return r.json();}).then(data=>{
    if (ticket !== listState.seq) return true; // a newer request is in flight; its answer wins
    listState.total=data.total;
    data.items.forEach(function(item){rowInfo[item.session_id]=item;});
    var rows=document.getElementById('rows');
    document.getElementById('list-error').hidden=true;
    // Keep existing row nodes during polling. Clearing this list every five
    // seconds made the whole list visibly flash, especially on slower PCs.
    var focusedCheckbox = document.activeElement && document.activeElement.classList.contains('row-select') ? document.activeElement.value : null;
    // A later page can overlap the previous one when meetings arrive in between: merge by id, then re-sort.
    var byId = {}; (reset ? [] : listState.items).concat(data.items).forEach(function(item) { byId[item.session_id] = item; });
    listState.items = Object.keys(byId).map(function(id) { return byId[id]; });
    renderMeetingList();
    if (preservedSelection.length) document.querySelectorAll('.row-select').forEach(function(box) { box.checked = preservedSelection.indexOf(box.value) >= 0; });
    if (focusedCheckbox) { var focusedRow=Array.from(rows.querySelectorAll('li[data-id]')).find(function(row){return row.dataset.id===focusedCheckbox;});if(focusedRow&&document.activeElement!==focusedRow.querySelector('.row-select'))focusedRow.querySelector('.row-select').focus(); }
    listState.loaded+=data.items.length;
    document.getElementById('more').style.display=listState.loaded<listState.total?'':'none';
    document.getElementById('meeting-count').textContent=data.total+(data.total===1?' meeting':' meetings');
    rows.setAttribute('aria-busy','false');
    updateSelection();
    return true;
  }).catch(function(){
    if (ticket !== listState.seq) return false;
    if(reset)listState.loaded=previousLoaded;
    var rows=document.getElementById('rows');
    if(!rows.querySelector('li[data-id]'))rows.innerHTML='<li class="state-row"><div class="empty">The meetings list could not be loaded.</div></li>';
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
    return speakerRow({mic:seg.track==='mic',label:seg.label||(seg.track==='mic'?'You':'Them'),start:seg.start,text:seg.text,approximate:seg.approximate,attrs:'id="seg-'+i+'" data-seg="'+i+'" tabindex="-1"'});
  }).join('');
  if(root._lastMarkup!==markup){root.innerHTML=markup;root._lastMarkup=markup;if(stripState.hit!=null){var again=document.getElementById('seg-'+stripState.hit);if(again)again.classList.add('hit');}}
}
/* Session timeline: one block per timed transcript segment, positioned by its
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
    if(draw){root.classList.add('strip-draw');setTimeout(function(){root.classList.remove('strip-draw');},400);}
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
  if((data.meta||{}).source==='transcript'){document.getElementById('transcription-checklist').innerHTML='<li class="complete">'+dot()+'<span>Transcript uploaded</span><span class="detail">Complete</span></li><li class="complete">'+dot()+'<span>Transcript ready</span><span class="detail">Complete</span></li>';return;}
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
  var c1=uploadComplete ? 'complete' : (uploadError ? 'error' : (uploadState==='uploading' ? 'active' : '')), c2=transcribeError ? 'error' : (transcribing || transcribeQueued ? 'active' : (transcribed ? 'complete' : '')), c3=transcribed ? 'complete' : '';
  document.getElementById('transcription-checklist').innerHTML = '<li class="' + c1 + '">' + dot() + '<span>Upload audio</span><span class="detail">' + uploadDetail + '</span></li>' +
    '<li class="' + c2 + '">' + dot() + '<span>Transcribe recording</span><span class="detail">' + transcribeDetail + '</span></li>' +
    '<li class="' + c3 + '">' + dot() + '<span>Transcript ready</span><span class="detail">' + (transcribed ? 'Complete' : 'Pending') + '</span></li>';
}
function openSession(id, hintView) {
  if (detailPollTimer) clearTimeout(detailPollTimer);
  detailPollTimer=null;
  var request=++detailRequest;
  var changingSession=currentSession!==id;
  currentSession=id;
  if(changingSession){
    detailReturnFocus=document.activeElement;detailReturnKey=(function(el){var row=el&&el.closest?el.closest('li[data-id]'):null;return row?{id:row.dataset.id,open:el.classList.contains('row-open')}:null;})(detailReturnFocus);notesRequest++;currentReview=null;currentMarkdown='';transcriptExplicit=false;pendingNotesDefault=hintView==='notes';
    if(notesPollTimer)clearTimeout(notesPollTimer);notesPollTimer=null;
    if(renameMeeting)renameMeeting.close(false);if(renameSummary)renameSummary.close(false);
    closeMenu(false);
    stripState.drawnFor=null;stripState.hit=null;stripState.data=null;
    setDocTitle('');document.getElementById('overlay-title').textContent='Loading meeting…';document.getElementById('overlay-meta').textContent='';document.getElementById('overlay-board').hidden=true;
    var segRoot=document.getElementById('overlay-segments');segRoot.innerHTML=skeletonLines();segRoot._lastMarkup=null;
    document.getElementById('strip-wrap').hidden=true;
    document.getElementById('notes-title').textContent='Meeting summary';document.getElementById('notes-meta').textContent='';document.getElementById('notes-state').textContent='';
    clearNotion();
    document.getElementById('notes-document').innerHTML=skeletonLines();
    document.getElementById('notes-download').disabled=true;document.getElementById('notes-copy').disabled=true;document.getElementById('edit-summary-name').disabled=true;document.getElementById('notes-retry').disabled=true;
    document.getElementById('queue-review').disabled=true;document.getElementById('queue-review').textContent='Notes';document.getElementById('review-status').textContent='';
    document.getElementById('meeting-extras').open=false;
    reviewTemplateId='';setTemplateSelect('');document.getElementById('overlay-style').hidden=true;
    setDetailView(hintView==='notes'?'notes':'transcript');
  }
  var overlay=document.getElementById('detail-overlay');overlay.classList.add('open');overlay.setAttribute('aria-hidden','false');document.body.style.overflow='hidden';
  if(changingSession)document.getElementById('close-overlay').focus();
  history.replaceState(null,'','/sessions/'+encodeURIComponent(id));
  fetch('/v1/sessions/'+encodeURIComponent(id),{credentials:'same-origin'}).then(r=>{if(!r.ok)throw new Error('Unable to load transcription');return r.json();}).then(data=>{
    if (currentSession !== id || request !== detailRequest) return;
    document.getElementById('transcription-progress-heading').textContent='Processing status';
    var meta=data.meta||{}; document.getElementById('overlay-title').textContent=meta.name||id;setDocTitle(meta.name||id);
    rowInfo[id]=Object.assign({session_id:id,created:meta.started_wall||meta.created,duration_sec:meta.duration_sec},rowInfo[id]||{},{name:meta.name||id},data.pipeline?{pipeline:data.pipeline,has_audio:data.has_audio}:{});if(!notionTimer&&!document.getElementById('notion-box').innerHTML)loadNotion();
    var chip=document.getElementById('overlay-board');chip.textContent=data.board||'';chip.hidden=!data.board;
    document.getElementById('overlay-meta').textContent=[fmtDate(meta.created),fmtDuration(meta.duration_sec),meta.device||meta.platform||'Unknown device'].join(' · ');
    var players=[]; var tracks=meta.tracks||{};
    ['mic','system'].forEach(track=>{if(data.has_audio && tracks[track]) players.push('<div class="audio-card"><strong>'+icon(track==='mic'?'mic':'speaker')+(track==='mic'?'You · microphone':'Them · system audio')+'</strong><audio controls preload="metadata" src="/sessions/'+encodeURIComponent(id)+'/audio/'+track+'"></audio></div>');});
    var audioRoot=document.getElementById('audio-players'),audioMarkup=players.join('') || '<div class="empty">'+(meta.source==='transcript'?'Transcript uploaded. There is no audio for this meeting.':'Audio has been removed.')+'</div>';
    if(audioRoot._lastMarkup!==audioMarkup){audioRoot.innerHTML=audioMarkup;audioRoot._lastMarkup=audioMarkup;}
    document.getElementById('retranscribe').disabled=!data.has_audio; document.getElementById('delete-audio').disabled=!data.has_audio;
    renderProcessingChecklist(data);
    var review=data.review||data.review_status||{}; currentReview=review.review_id||review.id||null;
    var reviewStatus=String(review.status||'none').toLowerCase();
    document.getElementById('review-status').textContent=reviewStatus==='done'?'Notes ready':(reviewStatus==='queued'||reviewStatus==='running'?'Building notes…':(reviewStatus==='error'?'Notes need attention':''));
    document.getElementById('queue-review').disabled=false;
    document.getElementById('notes-retry').disabled=!currentReview;
    document.getElementById('queue-review').textContent=currentReview?'Notes':'Generate notes';
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
  var row=Array.from(document.querySelectorAll('#rows li[data-id]')).find(function(r){return r.dataset.id===detailReturnKey.id;});
  var target=row&&(detailReturnKey.open?row.querySelector('.row-open'):row.querySelector('.row-open, .row-select'));
  if(target)target.focus();
}
function setDocTitle(name){document.title='Meeting Notes | '+(name||'Meeting');}
function closeOverlay(){document.title='Meeting Notes | Meetings';clearNotion();currentSession=null;currentReview=null;detailRequest++;notesRequest++;if(detailPollTimer)clearTimeout(detailPollTimer);if(notesPollTimer)clearTimeout(notesPollTimer);detailPollTimer=null;notesPollTimer=null;if(renameMeeting)renameMeeting.close(false);if(renameSummary)renameSummary.close(false);closeMenu(false);stripState.drawnFor=null;stripState.hit=null;var overlay=document.getElementById('detail-overlay');overlay.classList.remove('open');overlay.setAttribute('aria-hidden','true');document.body.style.overflow='';history.replaceState(null,'','/meetings');restoreFocus();detailReturnFocus=null;detailReturnKey=null;}
function noteValues(value){return Array.isArray(value)?value:(value==null?[]:[value]);}
function noteText(value){if(value==null)return '';if(typeof value!=='object')return String(value);var text=String(value.action||value.task||value.text||value.title||value.point||value.decision||value.question||value.risk||value.step||'');if(value.owner)text+=' — Owner: '+value.owner;if(value.due_date||value.due)text+=' — Due: '+(value.due_date||value.due);if(value.context)text+=' — '+value.context;return text;}
function personText(value){if(value!=null&&typeof value==='object')return String(value.name||value.email||noteText(value));return value==null?'':String(value);}
function dueText(value){var text=fmtDate(value);return text==='unknown date'?String(value):text;}
function buildMarkdown(note,title){var sections=[['Summary',note.summary||note.overview],['Meeting notes',note.polished_meeting_notes||note.polished_notes||note.meeting_notes||note.narrative||note.notes],['Key points',note.key_points||note.keyPoints],['Decisions',note.decisions],['Action items',note.action_items||note.actionItems||note.actions],['Open questions',note.open_questions||note.openQuestions||note.questions],['Risks',note.risks],['Next steps',note.next_steps||note.nextSteps],['Participants',note.participants||note.attendees]],filled=sections.filter(function(s){return noteValues(s[1]).map(noteText).some(function(v){return v.trim();});}),empty=sections.filter(function(s){return !noteValues(s[1]).map(noteText).some(function(v){return v.trim();});});function section(s){var values=noteValues(s[1]).map(noteText).filter(function(v){return v.trim();}),prose=s[0]==='Summary'||s[0]==='Meeting notes';return '## '+s[0]+'\n'+(values.length?(prose?values.join('\n\n'):values.map(function(v){return '- '+v;}).join('\n')):'')+'\n';}return ('# '+title+'\n\n'+filled.map(section).join('\n')+(empty.length?'\n---\n\n'+empty.map(section).join('\n'):'' )).trim()+'\n';}
function clearNotion(){notionRequest++;if(notionTimer)clearTimeout(notionTimer);notionTimer=null;var box=document.getElementById('notion-box');if(box){box.hidden=true;box.innerHTML='';}}
function notionLink(url,label){return url?'<a href="'+escapeHtml(url)+'" target="_blank" rel="noopener">'+escapeHtml(label)+'</a>':'<span>'+escapeHtml(label)+'</span>';}
function notionPathHtml(d){
  if(!d||!d.parent)return '';
  var out=[notionLink(d.parent.url,d.parent.title||'Parent page')];
  if(d.month)out.push(notionLink(d.month.url,d.month.title||'Month page'));
  return '<span class="notion-path" id="notes-dest">'+out.join('<span class="notion-sep" aria-hidden="true">\u203a</span>')+'</span>';
}
function renderNotion(st,session){
  var box=document.getElementById('notion-box'),state=st.state||'none',parts=['<span class="notion-label">'+icon('notes',14)+'<span>Notion</span></span>'];
  var dest=st.destination||{},path=notionPathHtml(dest),canSend=!!st.can_send,reason=st.reason||'';
  var noNotes=!canSend&&/no notes yet/i.test(reason);
  var sel=templateSelect(),have=st.copied_style||reviewTemplateId,mismatch=!!(sel&&!sel.hidden&&have&&sel.value&&sel.value!==have);
  if(path)parts.push(path);else parts.push('<span class="notion-none" id="notes-dest">Not saved to Notion</span>');
  if(mismatch){/* the selection is not what is saved yet: show where it WOULD go, not the current status */}
  else if(state==='copied'){parts.push(badge('done','In Notion'));if(st.url)parts.push('<a class="notion-open" href="'+escapeHtml(st.url)+'" target="_blank" rel="noopener">Open</a>');}
  else if(state==='pending'){parts.push(badge('running',st.retrying?'Retrying…':'Sending…'));}
  else if(state==='failed'){parts.push(badge('error','Failed',st.error||''));if(st.error)parts.push('<span class="error-text notion-error">'+escapeHtml(st.error)+'</span>');}
  else if(!noNotes)parts.push(badge('none','Not in Notion'));
  if(st.warning)parts.push(badge('warn',st.warning));
  if(mismatch&&path)parts.push('<span class="help notion-reason">'+(state==='copied'?'After you regenerate, the Notion copy moves here.':'Regenerate to save to this page.')+'</span>');
  if(state!=='pending'&&!noNotes&&!mismatch){
    var label=state==='none'?'Send to Notion':(state==='failed'?'Retry':'Send again');
    parts.push('<button type="button" class="btn secondary sm" id="notion-send"'+(canSend?'':' disabled')+(!canSend&&reason?' title="'+escapeHtml(reason)+'"':' title="Copies the notes to the Notion page set for the note type of this meeting"')+'>'+escapeHtml(label)+'</button>');
    if(state==='none'&&!canSend&&reason)parts.push('<span class="help notion-reason">'+escapeHtml(reason)+'</span>');
  }
  box.innerHTML=parts.join('');box.hidden=false;
  var send=document.getElementById('notion-send');
  if(send)send.onclick=function(){
    if(session!==currentSession)return;send.disabled=true;
    box.innerHTML='<span class="notion-label">'+icon('notes',14)+'<span>Notion</span></span>'+badge('running','Sending…');
    fetch('/v1/sessions/'+encodeURIComponent(session)+'/notion',{method:'POST',credentials:'same-origin'}).then(function(r){if(r.ok)return r.json();return r.json().catch(function(){return {};}).then(function(d){throw new Error((d&&typeof d.detail==='string'&&d.detail)||'Could not send to Notion.');});}).then(function(){if(session===currentSession)loadNotion();}).catch(function(e){notify(e.message,'error');if(session===currentSession)loadNotion();});
  };
  if(state==='pending'){if(notionTimer)clearTimeout(notionTimer);notionTimer=setTimeout(function(){if(session===currentSession)loadNotion();},3000);}
}
function loadNotion(){
  var session=currentSession;if(!session)return;
  if(notionTimer)clearTimeout(notionTimer);notionTimer=null;
  var request=++notionRequest;
  var tsel=templateSelect(),tq=tsel&&!tsel.hidden&&tsel.value?'?template='+encodeURIComponent(tsel.value):'';fetch('/v1/sessions/'+encodeURIComponent(session)+'/notion'+tq,{credentials:'same-origin'}).then(function(r){if(!r.ok)throw new Error('Unable to load Notion status');return r.json();}).then(function(st){if(session!==currentSession||request!==notionRequest)return;renderNotion(st,session);}).catch(function(){if(session!==currentSession||request!==notionRequest)return;document.getElementById('notion-box').hidden=true;});
}
function renderNotes(data){
  var note=data.note||data.meeting_note||data, meta=note.meta||note, title=note.title||meta.title||meta.name||'Meeting summary';
  var status=String(note.status||data.status||'').toLowerCase();
  document.getElementById('notes-title').textContent=title; var tpl=data.template||note.template||null;reviewTemplateId=tpl&&tpl.id?tpl.id:'';setTemplateSelect(reviewTemplateId);var styleChip=document.getElementById('overlay-style');styleChip.textContent=tpl&&tpl.name?'Note type: '+tpl.name:'';styleChip.hidden=!(tpl&&tpl.name);document.getElementById('notes-pane').classList.toggle('same',title===document.getElementById('overlay-title').textContent);document.getElementById('notes-meta').textContent=[fmtDate(meta.created||meta.meeting_time||meta.started),meta.device||meta.platform,tpl&&tpl.name].filter(Boolean).join(' · '); document.getElementById('notes-state').textContent=status==='done'?'':(status==='queued'||status==='running'?'Building meeting notes…':(status==='error'?'Notes need attention. Use Regenerate notes to try again.':''));
  if(status==='done')loadNotion();else clearNotion();
  if((status==='queued'||status==='running')&&!note.summary){document.getElementById('notes-document').className='notes-doc no-rail';document.getElementById('notes-document').innerHTML='<p class="notes-empty-state">The summary is being prepared. You can return to the transcript while it runs.</p>'+skeletonLines();currentMarkdown='';document.getElementById('notes-download').disabled=true;document.getElementById('notes-copy').disabled=true;document.getElementById('edit-summary-name').disabled=true;return;}
  var S={summary:note.summary||note.overview,body:note.polished_meeting_notes||note.polished_notes||note.meeting_notes||note.narrative||note.notes,points:note.key_points||note.keyPoints,decisions:note.decisions,actions:note.action_items||note.actionItems||note.actions,questions:note.open_questions||note.openQuestions||note.questions,risks:note.risks,steps:note.next_steps||note.nextSteps,people:note.participants||note.attendees};
  function has(value,mapper){return noteValues(value).map(mapper||noteText).some(function(v){return String(v).trim();});}
  function list(value,mapper,prose){var values=noteValues(value).map(mapper||noteText).filter(function(v){return String(v).trim();});if(prose)return mdBlock(values.join('\n\n'));return values.length===1?'<p>'+mdInline(values[0])+'</p>':'<ul>'+values.map(function(v){return '<li>'+mdInline(v)+'</li>';}).join('')+'</ul>';}
  function people(value){return '<ul class="people">'+noteValues(value).map(personText).filter(function(v){return v.trim();}).map(function(name){return '<li><span class="avatar" aria-hidden="true">'+escapeHtml(initials(name))+'</span><span>'+escapeHtml(name)+'</span></li>';}).join('')+'</ul>';}
  function actions(value){return '<ul class="action-list">'+noteValues(value).map(function(raw){var action=typeof raw==='object'&&raw?raw:{action:raw},label=action.action||action.task||action.text||'',pills=[];if(!label)return '';if(action.owner)pills.push('<span class="pill owner" title="Owner">'+escapeHtml(action.owner)+'</span>');if(action.due_date||action.due)pills.push('<span class="pill due" title="Due date">Due '+escapeHtml(dueText(action.due_date||action.due))+'</span>');return '<li class="action-item"><span class="box" aria-hidden="true"></span><div class="action-body"><div class="what">'+mdInline(label)+'</div>'+(pills.length?'<div class="pills">'+pills.join('')+'</div>':'')+(action.context?'<div class="context">'+mdInline(action.context)+'</div>':'')+'</div></li>';}).join('')+'</ul>';}
  var main=[['Summary',S.summary,'lead',noteText],['Decisions',S.decisions,'',noteText],['Action items',S.actions,'actions',noteText],['Key points',S.points,'',noteText],['Meeting notes',S.body,'',noteText]];
  var rail=[['Participants',S.people,'people',personText],['Open questions',S.questions,'',noteText],['Risks',S.risks,'',noteText],['Next steps',S.steps,'',noteText]];
  function section(s){var cls='notes-section'+(s[2]==='lead'?' lead':'');return '<section class="'+cls+'"><h3>'+escapeHtml(s[0])+'</h3>'+(s[2]==='actions'?actions(s[1]):(s[2]==='people'?people(s[1]):list(s[1],s[3],s[0]==='Summary'||s[0]==='Meeting notes')))+'</section>';}
  var missing=main.concat(rail).filter(function(s){return !has(s[1],s[3]);}).map(function(s){return s[0];});
  var mainMarkup=main.filter(function(s){return has(s[1],s[3]);}).map(section).join(''), railMarkup=rail.filter(function(s){return has(s[1],s[3]);}).map(section).join('');
  var doc=document.getElementById('notes-document');
  doc.className='notes-doc'+(railMarkup?'':' no-rail');
  doc.innerHTML='<div class="notes-main">'+(mainMarkup||'<p class="notes-empty-state">No notes were recorded for this meeting yet.</p>')+(missing.length?'<p class="notes-none-line">Nothing recorded for: '+escapeHtml(missing.join(', '))+'.</p>':'')+'</div>'+(railMarkup?'<aside class="notes-rail" aria-label="Details: participants and follow-ups">'+railMarkup+'</aside>':'');
  currentMarkdown=buildMarkdown(note,title);
  document.getElementById('notes-download').disabled=!note.summary;
  document.getElementById('notes-copy').disabled=!note.summary;
  document.getElementById('edit-summary-name').disabled=!note.summary;
}
function showNotes(refresh){
  if(!currentReview){var session=currentSession, request=++notesRequest;document.getElementById('review-status').textContent='Starting notes…';action('/review'+templateQuery()).then(function(result){if(session!==currentSession||request!==notesRequest)return;currentReview=result.review_id||result.id||null;reviewTemplateId=(result.template_id||'');document.getElementById('review-status').textContent='Building notes…';document.getElementById('queue-review').textContent='Notes';document.getElementById('notes-retry').disabled=false;showNotes();loadRows(true);}).catch(function(e){if(session===currentSession&&request===notesRequest){document.getElementById('review-status').textContent='';notify(e.message,'error');}}); return; }
  transcriptExplicit=false;setDetailView('notes');
  if(!refresh){currentMarkdown='';document.getElementById('notes-download').disabled=true;document.getElementById('notes-copy').disabled=true;document.getElementById('edit-summary-name').disabled=true;document.getElementById('notes-document').className='notes-doc no-rail';document.getElementById('notes-document').innerHTML=skeletonLines();}
  var session=currentSession, review=currentReview, request=++notesRequest;
  fetch('/v1/meeting-notes/'+encodeURIComponent(review),{credentials:'same-origin'}).then(function(r){if(!r.ok)throw new Error('Unable to load meeting notes');return r.json();}).then(function(data){if(session!==currentSession||review!==currentReview||request!==notesRequest)return;renderNotes(data);var status=String((data.note||data).status||data.status||'').toLowerCase();document.getElementById('review-status').textContent=status==='done'?'Notes ready':(status==='error'?'Notes need attention':'Building notes…');if(status==='done'||status==='error')loadRows(true);if((status==='queued'||status==='running')&&currentReview){if(notesPollTimer)clearTimeout(notesPollTimer);notesPollTimer=setTimeout(function(){if(session===currentSession&&review===currentReview)showNotes(true);},3000);}}).catch(function(e){if(session!==currentSession||review!==currentReview||request!==notesRequest)return;document.getElementById('notes-state').textContent=e.message+' · retrying…';notesPollTimer=setTimeout(function(){if(session===currentSession&&review===currentReview)showNotes(true);},3000);});
}
function saveName(url, value, field){return fetch(url,{method:'PATCH',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({[field]:value})}).then(function(r){if(!r.ok)throw new Error('Unable to save name');return r.json();});}
function templateSelect(){return document.getElementById('notes-template');}
function templateQuery(){var sel=templateSelect();return sel&&!sel.hidden&&sel.value?'?template='+encodeURIComponent(sel.value):'';}
function setTemplateSelect(id){var sel=templateSelect();if(!sel||sel.hidden)return;var want=id||defaultTemplateId;if(Array.prototype.some.call(sel.options,function(o){return o.value===want;}))sel.value=want;updateRegenButton();}
function updateNoteDest(){if(currentSession)loadNotion();}
function updateRegenButton(){var sel=templateSelect(),btn=document.getElementById('notes-regen');if(!sel||!btn)return;btn.hidden=!(currentReview&&!sel.hidden&&reviewTemplateId&&sel.value!==reviewTemplateId);}
function loadNoteTemplates(){fetch('/v1/note-templates',{credentials:'same-origin'}).then(function(r){if(!r.ok)throw new Error('templates');return r.json();}).then(function(data){noteTemplates=data.items||[];defaultTemplateId=data.default_template_id||'';var sel=templateSelect();if(!sel)return;sel.innerHTML=noteTemplates.map(function(t){return '<option value="'+escapeHtml(t.id)+'">'+escapeHtml(t.name)+'</option>';}).join('');sel.hidden=noteTemplates.length<2;document.getElementById('type-bar').hidden=sel.hidden;sel.value=defaultTemplateId;setTemplateSelect(reviewTemplateId);updateNoteDest();}).catch(function(){});}
function regenerateNotes(){if(!currentReview)return;var session=currentSession,review=currentReview,sel=templateSelect(),body=sel&&!sel.hidden&&sel.value?JSON.stringify({template:sel.value}):'';document.getElementById('notes-state').textContent='Queued for regeneration…';document.getElementById('notes-regen').hidden=true;fetch('/v1/meeting-notes/'+encodeURIComponent(review)+'/retry',{method:'POST',credentials:'same-origin',headers:body?{'Content-Type':'application/json'}:{},body:body||undefined}).then(function(r){if(!r.ok)return r.json().then(function(d){throw new Error(d.detail||'Unable to queue regeneration');});return r.json();}).then(function(){if(session===currentSession&&review===currentReview){showNotes();loadRows(true);}}).catch(function(e){if(session===currentSession&&review===currentReview){document.getElementById('notes-state').textContent=e.message;updateRegenButton();}});}
function action(path,method){if(!currentSession)return;return fetch('/v1/sessions/'+encodeURIComponent(currentSession)+path,{method:method||'POST',credentials:'same-origin'}).then(async r=>{if(!r.ok)throw new Error((await r.json()).detail||'Request failed');return r.json();});}
/* Inline rename: the heading swaps for a small form; errors show beside it. */
function bindRename(o){
  function openForm(){o.error.textContent='';o.input.value=o.current();o.heading.hidden=true;o.form.hidden=false;o.btn.setAttribute('aria-expanded','true');o.input.focus();o.input.select();}
  function closeForm(refocus){o.form.hidden=true;o.heading.hidden=false;o.btn.setAttribute('aria-expanded','false');if(refocus){o.btn.focus();if(document.activeElement!==o.btn)moreBtn.focus();}}
  o.btn.onclick=function(){if(o.form.hidden)openForm();else closeForm(true);};
  o.cancel.onclick=function(){closeForm(true);};
  o.input.addEventListener('keydown',function(e){if(e.key==='Escape'){e.stopPropagation();e.preventDefault();closeForm(true);}});
  o.form.addEventListener('submit',function(e){e.preventDefault();var name=o.input.value.trim();if(!name){o.error.textContent='Enter a name to save.';return;}var submit=o.form.querySelector('[type=submit]');submit.disabled=true;o.error.textContent='';Promise.resolve(o.save(name)).then(function(){closeForm(true);}).catch(function(err){o.error.textContent=(err&&err.message)||'Could not save the name. Try again.';}).finally(function(){submit.disabled=false;});});
  return {close:closeForm};
}
/* Overflow menu: real menu semantics (arrow keys, Home/End, Escape, outside click). */
function menuItems(){return Array.from(menuEl.querySelectorAll('[role=menuitem]:not([disabled])'));}
function openMenu(){menuEl.hidden=false;moreBtn.setAttribute('aria-expanded','true');var items=menuItems();if(items.length)items[0].focus();}
function closeMenu(refocus){if(menuEl.hidden)return;menuEl.hidden=true;moreBtn.setAttribute('aria-expanded','false');if(refocus)moreBtn.focus();}
moreBtn.onclick=function(){if(menuEl.hidden)openMenu();else closeMenu(true);};
menuEl.addEventListener('keydown',function(e){
  var items=menuItems(),i=items.indexOf(document.activeElement);
  if(e.key==='ArrowDown'){e.preventDefault();items[(i+1)%items.length].focus();}
  else if(e.key==='ArrowUp'){e.preventDefault();items[(i-1+items.length)%items.length].focus();}
  else if(e.key==='Home'){e.preventDefault();items[0].focus();}
  else if(e.key==='End'){e.preventDefault();items[items.length-1].focus();}
  else if(e.key==='Escape'){e.preventDefault();e.stopPropagation();closeMenu(true);}
  else if(e.key==='Tab'){closeMenu(false);}
});
menuEl.addEventListener('click',function(e){if(e.target.closest('[role=menuitem]'))closeMenu(false);});
document.addEventListener('click',function(e){if(!menuEl.hidden&&!menuEl.contains(e.target)&&!moreBtn.contains(e.target))closeMenu(false);});
document.getElementById('overlay-title').addEventListener('click',function(){var btn=document.getElementById('edit-meeting-name');if(currentSession&&document.getElementById('rename-form').hidden)btn.click();});
renameMeeting=bindRename({btn:document.getElementById('edit-meeting-name'),form:document.getElementById('rename-form'),input:document.getElementById('rename-input'),cancel:document.getElementById('rename-cancel'),error:document.getElementById('rename-error'),heading:document.getElementById('overlay-title'),current:function(){return document.getElementById('overlay-title').textContent;},save:function(name){var session=currentSession;if(!session)return Promise.reject(new Error('No meeting is open.'));return saveName('/v1/sessions/'+encodeURIComponent(session),name,'name').then(function(){if(session===currentSession){document.getElementById('overlay-title').textContent=name;setDocTitle(name);}loadRows(true);});}});
renameSummary=bindRename({btn:document.getElementById('edit-summary-name'),form:document.getElementById('summary-rename-form'),input:document.getElementById('summary-rename-input'),cancel:document.getElementById('summary-rename-cancel'),error:document.getElementById('summary-rename-error'),heading:document.getElementById('notes-title'),current:function(){return document.getElementById('notes-title').textContent;},save:function(name){var session=currentSession,review=currentReview;if(!review)return Promise.reject(new Error('No summary to rename yet.'));return saveName('/v1/meeting-notes/'+encodeURIComponent(review),name,'title').then(function(){if(session!==currentSession||review!==currentReview)return;document.getElementById('notes-title').textContent=name;showNotes(true);});}});
function openFromRow(row){openSession(row.dataset.id,row.classList.contains('notes-ready')?'notes':'');}
document.getElementById('rows').addEventListener('click',e=>{if(e.target.closest('#clear-filters')){document.getElementById('q').value='';document.getElementById('state').value='';loadRows(true);return;}var gen=e.target.closest('.notes-generate');if(gen){e.stopPropagation();generateNotes(gen);return;}var opener=e.target.closest('a.row-open');if(opener){if(e.metaKey||e.ctrlKey||e.shiftKey||e.altKey||e.button!==0)return;e.preventDefault();var target=opener.closest('li[data-id]');if(target)openFromRow(target);return;}if(e.target.closest('input,button,a')){updateSelection();return;}if(e.target.closest('label.sel')){updateSelection();return;}var row=e.target.closest('li[data-id]');if(row)openFromRow(row);});
document.getElementById('session-strip').addEventListener('click',function(e){var blk=e.target.closest('.blk');if(blk)jumpToSegment(Number(blk.dataset.seg));});
document.getElementById('session-strip').addEventListener('keydown',onStripKey);
var stripResize;window.addEventListener('resize',function(){clearTimeout(stripResize);stripResize=setTimeout(function(){if(currentSession&&stripState.data)renderStrip(stripState.data);},150);});
document.getElementById('close-overlay').onclick=closeOverlay;
document.getElementById('detail-overlay').addEventListener('click',function(e){if(e.target===this||e.target.classList.contains('overlay-inner'))closeOverlay();});
document.addEventListener('keydown',function(e){var overlay=document.getElementById('detail-overlay');if(!overlay.classList.contains('open')||document.getElementById('confirm-dialog').open||tuDlg.open)return;if(e.key==='Escape'){if(!menuEl.hidden){closeMenu(true);return;}closeOverlay();return;}trapFocus(e,overlay);});
document.getElementById('retranscribe').onclick=()=>{var pending=action('/retranscribe');if(pending)pending.then(()=>openSession(currentSession)).catch(e=>notify(e.message,'error'));};
document.getElementById('queue-review').onclick=showNotes;
document.getElementById('notes-retry').onclick=regenerateNotes;
document.getElementById('notes-regen').onclick=regenerateNotes;
templateSelect().addEventListener('change',function(){updateRegenButton();updateNoteDest();});
if(aiEnabled)loadNoteTemplates();
document.getElementById('show-transcript').onclick=function(){transcriptExplicit=true;notesRequest++;if(notesPollTimer)clearTimeout(notesPollTimer);notesPollTimer=null;setDetailView('transcript');};
document.getElementById('notes-download').onclick=function(){if(!currentMarkdown)return;var blob=new Blob([currentMarkdown],{type:'text/markdown;charset=utf-8'}),url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download=(document.getElementById('notes-title').textContent.trim().replace(/[\/:*?"<>|]+/g,'-').slice(0,100)||'meeting-notes')+'.md';link.click();setTimeout(function(){URL.revokeObjectURL(url);},1000);};
document.getElementById('notes-copy').onclick=function(){if(!currentMarkdown)return;copyText(currentMarkdown).then(function(ok){notify(ok?'Meeting notes copied.':'Could not copy. Use Download .md instead.',ok?'':'error');});};
document.getElementById('delete-audio').onclick=()=>{var id=currentSession;if(!id)return;deleteAudioDialog([id]).then(ok=>{if(!ok||currentSession!==id)return;var pending=action('/delete-audio','POST');if(pending)pending.then(()=>openSession(id)).catch(e=>notify(e.message,'error'));});};
document.getElementById('delete-entry').onclick=()=>{var id=currentSession;if(!id)return;trashDialog([id]).then(ok=>{if(!ok||currentSession!==id)return;closeOverlay();moveToTrash([id],'web');});};
document.getElementById('select-all').onchange=function(e){document.querySelectorAll('.row-select').forEach(function(box){box.checked=e.target.checked;});updateSelection();};
document.getElementById('bulk-build').onclick=function(){runBulk('/review','POST');};
document.getElementById('bulk-notion').onclick=function(){bulkNotion();};
document.getElementById('bulk-retranscribe').onclick=function(){runBulk('/retranscribe','POST');};
document.getElementById('bulk-delete-audio').onclick=function(){var ids=selectedIds();if(!ids.length)return;deleteAudioDialog(ids).then(function(ok){if(ok)runBulk('/delete-audio','POST');});};
document.getElementById('bulk-delete').onclick=function(){var ids=selectedIds();if(!ids.length)return;trashDialog(ids).then(function(ok){if(ok)moveToTrash(ids,'bulk');});};
document.getElementById('bulk-clear').onclick=function(){document.querySelectorAll('.row-select').forEach(function(box){box.checked=false;});updateSelection();};
document.getElementById('list-retry').onclick=function(){loadRows(true);};
var qBox=document.getElementById('q'), debounce;
var initialQuery=new URLSearchParams(location.search).get('q'); if(initialQuery)qBox.value=initialQuery;
qBox.oninput=()=>{clearTimeout(debounce);debounce=setTimeout(()=>loadRows(true),250);};
qBox.form.addEventListener('submit',function(e){e.preventDefault();clearTimeout(debounce);loadRows(true);});
document.getElementById('state').onchange=()=>loadRows(true); document.getElementById('more').onclick=()=>{listState.page++;loadRows(false);};
function setPlaceholder(){qBox.placeholder=window.matchMedia('(max-width:860px)').matches?qBox.dataset.full:qBox.dataset.short;}
setPlaceholder();window.matchMedia('(max-width:860px)').addEventListener('change',setPlaceholder);
var tuDlg = document.getElementById('tu-dialog'), tuFileName = '', tuBusy = false;
function tuError(msg) { document.getElementById('tu-error').textContent = msg || ''; }
document.getElementById('open-transcript-upload').onclick = function() {
  document.getElementById('tu-form').reset(); tuFileName = ''; tuError('');
  document.getElementById('tu-when').value = localInputValue(new Date());
  document.getElementById('tu-submit').disabled = false; tuBusy = false;
  tuDlg.showModal(); document.getElementById('tu-file').focus();
};
document.getElementById('tu-cancel').onclick = function() { tuDlg.close('cancel'); };
document.getElementById('tu-file').addEventListener('change', function() {
  var file = this.files[0], input = this; tuError('');
  if (!file) { tuFileName = ''; return; }
  readTranscriptFile(file, function(error, info) {
    if (error) { input.value = ''; tuFileName = ''; tuError(error); return; }
    tuFileName = info.filename;
    document.getElementById('tu-text').value = info.text;
    var name = document.getElementById('tu-name');
    if (!name.value.trim()) name.value = info.name;
    if (info.when) document.getElementById('tu-when').value = info.when;
  });
});
document.getElementById('tu-form').addEventListener('submit', function(event) {
  event.preventDefault();
  if (tuBusy) return;
  var built = buildTranscriptPayload({text: document.getElementById('tu-text').value, name: document.getElementById('tu-name').value, filename: tuFileName, when: document.getElementById('tu-when').value});
  if (built.error) { tuError(built.error); return; }
  var submit = document.getElementById('tu-submit');
  tuError(''); tuBusy = true; submit.disabled = true; submit.textContent = 'Uploading…';
  postTranscript(built.payload).then(function(data) {
    tuBusy = false; submit.textContent = 'Upload transcript'; tuDlg.close('done');
    notify('Transcript added.', null, {label: 'Open meeting', href: '/sessions/' + encodeURIComponent(data.session_id)});
    return loadRows(true);
  }, function(err) { tuBusy = false; submit.disabled = false; submit.textContent = 'Upload transcript'; tuError(err.message); });
});
loadRows(true);
// Once the operator has loaded additional pages, keep that expanded result
// set stable. A page-1 refresh would otherwise discard later pages and their
// selections every five seconds, making "Load more" effectively unusable.
setInterval(function(){if(!currentSession && listState.page===1)loadRows(true);},5000);
"""
        + splitmerge_ui.JS
        + f"if ({initial} !== null) openSession({initial}, {initial_view_js});"
        + """
</script>
"""
    )
    return _shell(
        page_title or "Meetings",
        body,
        token_configured=token_configured,
        active="transcriptions",
        main_class="meetings-page",
        appearance=appearance,
    )


def render_trash_page(*, token_configured: bool, appearance: str = "system") -> str:
    """Recently deleted: meetings moved to trash, restorable for 30 days.

    Rendered client-side from ``GET /v1/trash``; restore is
    ``POST /v1/trash/{id}/restore``, permanent delete ``DELETE /v1/trash/{id}``,
    and Empty trash ``POST /v1/trash/empty``.
    """
    body = (
        f"""
<div class="page trash-page">
<header class="page-head">
  <a class="btn ghost" href="/meetings" id="trash-back">{_icon("back")}<span>Meetings</span></a>
  <h1>Recently deleted</h1>
  <span class="count" id="trash-count" aria-live="polite"></span>
  <div class="head-tools"><button type="button" class="btn danger" id="empty-trash" hidden>Empty trash</button></div>
</header>
<p class="help trash-help" id="trash-help" hidden>Deleted meetings stay here for {TRASH_RETENTION_DAYS} days, then they are removed permanently.</p>
<div class="list-error" id="trash-error" role="alert" hidden><span>Could not load Recently deleted. Try again in a moment.</span><button type="button" class="btn secondary" id="trash-retry">Try again</button></div>
<ul class="mlist" id="trash-rows" aria-busy="true"></ul>
</div>
{_CONFIRM_DIALOG_HTML}
<script>
"""
        + _JS_HELPERS
        + f"var RETENTION_DAYS = {TRASH_RETENTION_DAYS};"
        + r"""
var trashItems = [];
function trashRow(item) {
  var s = meetingSummary(item), id = escapeHtml(item.session_id), name = escapeHtml(s.name);
  var what = [s.meta, item.has_audio ? fmtBytes(item.audio_bytes) + ' audio' : 'No audio'].filter(Boolean).join(' · ');
  var when = 'Deleted ' + agoText(item.deleted_at) + ' · ' + daysLeftText(item.days_left);
  return '<li class="mrow trash-row" data-id="' + id + '"><div class="mrow-main"><span class="mrow-title">' + name + '</span><span class="mrow-sub"><span>' + escapeHtml(what) + '</span><span class="tr-sep"> · </span><span class="tr-when">' + escapeHtml(when) + '</span></span></div>'
    + '<div class="trash-actions"><button type="button" class="btn primary sm" data-restore="' + id + '" aria-label="Restore ' + name + '">Restore</button>'
    + '<button type="button" class="btn danger sm" data-purge="' + id + '" aria-label="Delete ' + name + ' permanently">Delete permanently</button></div></li>';
}
function renderTrash(items) {
  trashItems = items;
  var rows = document.getElementById('trash-rows');
  document.getElementById('trash-count').textContent = items.length ? plural(items.length, 'meeting', 'meetings') : '';
  document.getElementById('empty-trash').hidden = !items.length;
  document.getElementById('trash-help').hidden = !items.length;
  rows.innerHTML = items.length ? items.map(trashRow).join('') : '<li class="state-row"><div class="empty-teach"><h2>Nothing in Recently deleted</h2><p>Meetings you delete stay here for ' + RETENTION_DAYS + ' days, so you can restore them. After that they are removed permanently.</p><a class="btn secondary" href="/meetings">Back to Meetings</a></div></li>';
  rows.setAttribute('aria-busy', 'false');
}
function loadTrash() {
  return fetch('/v1/trash', {credentials: 'same-origin'}).then(function (r) {
    if (r.status === 401 || r.status === 403) { window.location = '/login'; throw new Error('Signed out'); }
    if (!r.ok) throw new Error('Unable to load');
    return r.json();
  }).then(function (data) { document.getElementById('trash-error').hidden = true; renderTrash(data.items || []); return true; })
    .catch(function () { document.getElementById('trash-error').hidden = false; document.getElementById('trash-rows').setAttribute('aria-busy', 'false'); return false; });
}
function findTrash(id) { return trashItems.filter(function (i) { return i.session_id === id; })[0] || {session_id: id}; }
function trashCall(id, method, path) {
  return fetch('/v1/trash/' + encodeURIComponent(id) + path, {method: method, credentials: 'same-origin'}).then(function (r) {
    if (!r.ok) return r.json().catch(function () { return {}; }).then(function (b) { throw new Error(b.detail || 'Request failed'); });
  });
}
document.getElementById('trash-rows').addEventListener('click', function (e) {
  var restore = e.target.closest('[data-restore]'), purge = e.target.closest('[data-purge]');
  if (restore) {
    var id = restore.dataset.restore, item = findTrash(id);
    restore.disabled = true;
    trashCall(id, 'POST', '/restore').then(function () { notify('Restored ' + meetingSummary(item).name + '.'); return loadTrash(); })
      .catch(function (err) { restore.disabled = false; notify(err.message, 'error'); });
  } else if (purge) {
    var pid = purge.dataset.purge, pitem = findTrash(pid);
    confirmDialog({title: 'Delete this meeting permanently?', lead: 'Its recording, transcript and notes are removed for good. This cannot be undone.', items: [meetingSummary(pitem)], confirmLabel: 'Delete permanently'}).then(function (ok) {
      if (!ok) return;
      trashCall(pid, 'DELETE', '').then(function () { notify('Deleted permanently.'); return loadTrash(); }).catch(function (err) { notify(err.message, 'error'); });
    });
  }
});
document.getElementById('empty-trash').onclick = function () {
  if (!trashItems.length) return;
  confirmDialog({title: 'Empty trash?', lead: plural(trashItems.length, 'meeting is', 'meetings are') + ' removed permanently, with recordings, transcripts and notes. This cannot be undone.', items: trashItems.map(meetingSummary), confirmLabel: 'Delete ' + (trashItems.length === 1 ? 'permanently' : 'all permanently')}).then(function (ok) {
    if (!ok) return;
    fetch('/v1/trash/empty', {method: 'POST', credentials: 'same-origin'}).then(function (r) { if (!r.ok) throw new Error('Could not empty the trash'); return r.json(); })
      .then(function (d) { notify(plural(d.purged, 'meeting', 'meetings') + ' deleted permanently.'); return loadTrash(); })
      .catch(function (err) { notify(err.message, 'error'); });
  });
};
document.getElementById('trash-retry').onclick = loadTrash;
loadTrash();
</script>
"""
    )
    return _shell(
        "Recently deleted",
        body,
        token_configured=token_configured,
        active="transcriptions",
        main_class="meetings-page",
        appearance=appearance,
    )


_RECORDERS_JS = r"""
/* Recorders page. Every server value goes into the DOM with textContent / value / setAttribute, never
   into markup; innerHTML only ever receives the static templates and icon() output below. */
var REC_TRACKS = [
  {key: 'mic', label: 'You', what: 'Microphone', mute: 'Mute your microphone', on: 'mic', off: 'mic-off'},
  {key: 'system', label: 'Them', what: 'Meeting audio', mute: 'Mute meeting audio', on: 'speaker', off: 'speaker-off'}
];
var recs = new Map();        // instance_id -> {item, at}  (at = Date.now() when the frame arrived)
var recCards = new Map();    // instance_id -> card element
var recReady = false, recSocket = null, recBackoff = 1000, recReconnect = null, recEverClosed = false;

function recP2(n) { return (n < 10 ? '0' : '') + n; }
function recClock(sec) {
  sec = Math.max(0, Math.floor(Number(sec) || 0));
  return recP2(Math.floor(sec / 3600)) + ':' + recP2(Math.floor(sec % 3600 / 60)) + ':' + recP2(sec % 60);
}
function recState(item) {
  var s = (item && item.state) || {};
  return {
    status: s.status === 'recording' || s.status === 'finishing' ? s.status : 'idle',
    meeting: s.meeting || {},
    tracks: s.tracks || {},
    banners: Array.isArray(s.banners) ? s.banners : [],
    update: s.update || {},
    uploads: s.uploads || {},
    call: s.call || {},
    suggestion: s.suggestion || null,
    preview: s.preview || {},
    allowed: !(s.control && s.control.allowed === false)
  };
}
/* What one track's bar shows. 'live' while recording, 'preview' while idle and the recorder can show
   input before recording (0.7.7+, setting on, device connected, track meterable), else 'off' (empty). */
function recMeterMode(s, key) {
  var tr = (s.tracks && s.tracks[key]) || {}, pv = s.preview || {};
  if (s.status === 'recording') return tr.connected && !tr.muted ? 'live' : 'off';
  if (s.status !== 'idle' || pv.supported !== true || !tr.connected) return 'off';
  return Array.isArray(pv.tracks) && pv.tracks.indexOf(key) < 0 ? 'off' : 'preview';
}
/* The one-line note under the bars: says why they are dimmed or empty. Hidden while recording. */
function recMeterHint(s) {
  if (s.status !== 'idle') return '';
  var pv = s.preview || {};
  if (pv.supported !== true) return 'Levels show while recording';
  var partial = Array.isArray(pv.tracks) && pv.tracks.length < REC_TRACKS.length;
  return 'Preview \u00b7 not recording' + (partial ? '. Meeting audio shows while recording.' : '');
}
function recElapsed(entry, now) {
  var s = recState(entry.item), e = s.meeting.elapsed_sec;
  if (e == null || isNaN(Number(e))) return null;
  return Number(e) + (s.status === 'recording' ? Math.max(0, (now - entry.at) / 1000) : 0);
}
function recStatus(entry, now) {
  var st = recState(entry.item).status, el = recElapsed(entry, now);
  if (st === 'recording') return {cls: 'live', label: el == null ? 'Recording' : 'Recording ' + recClock(el)};
  if (st === 'finishing') return {cls: 'running', label: 'Finishing'};
  return {cls: 'none', label: 'Idle'};
}
/* Perceptual meter: linear 0..1 level -> bar fraction, so quiet speech is still visible. */
function recMeter(level) {
  var n = Number(level);
  return isNaN(n) ? 0 : Math.sqrt(Math.min(1, Math.max(0, n)));
}
function recUploadLine(u) {
  u = u || {};
  var pending = Number(u.pending) || 0, failed = Number(u.failed) || 0, waiting = Number(u.awaiting_transcript) || 0, parts = [], first = [];
  if (pending > 0) first.push(plural(pending, 'upload', 'uploads') + ' pending');
  if (failed > 0) first.push(failed + ' failed');
  if (first.length) parts.push(first.join(', '));
  if (u.current_percent != null && !isNaN(Number(u.current_percent))) parts.push('uploading ' + Math.round(Number(u.current_percent)) + '%');
  if (waiting > 0) parts.push(waiting + ' awaiting transcript');
  return {text: parts.join(' · '), failed: failed > 0};
}
function recCallText(prompt) {
  var label = String((prompt && prompt.label) || '').trim(), name = String((prompt && prompt.name) || '').trim();
  return (label ? label + ' call' : 'Call') + ' detected' + (name ? ': ' + name : '');
}
function recToast(cmd, args, device, state) {
  var d = device || 'the recorder', track = args && args.track === 'mic' ? 'Microphone' : 'Meeting audio';
  switch (cmd) {
    case 'start': case 'accept_call_prompt': return 'Recording started on ' + d + '.';
    case 'stop': case 'stop_suggested': return 'Recording stopped on ' + d + '.';
    case 'mute': return track + ' muted on ' + d + '.';
    case 'unmute': return track + ' unmuted on ' + d + '.';
    case 'refresh_devices': return 'Devices refreshed on ' + d + '.';
    case 'dismiss_call_prompt': return 'Call prompt dismissed on ' + d + '.';
    case 'keep_recording': return 'Still recording on ' + d + '.';
    case 'retry_uploads': return 'Retrying uploads on ' + d + '.';
    case 'check_update': return state && state.update && state.update.available ? 'An update is available for ' + d + '.' : d + ' is up to date.';
    case 'install_update': return 'Updating ' + d + '. The app restarts when it is done.';
    case 'set_name': return 'Meeting renamed on ' + d + '.';
  }
  return 'Done.';
}
function recSorted() {
  return Array.from(recs.keys()).sort(function (a, b) {
    var da = String(recs.get(a).item.device || '').toLowerCase(), db = String(recs.get(b).item.device || '').toLowerCase();
    return da < db ? -1 : da > db ? 1 : a < b ? -1 : a > b ? 1 : 0;
  });
}
function recSet(el, text) { text = text == null ? '' : String(text); if (el.textContent !== text) el.textContent = text; }
function recSetIcon(el, name, size) { if (el._ic !== name) { el._ic = name; el.innerHTML = icon(name, size || 16); } }

var REC_CARD_HTML =
  '<header class="rec-head"><span class="rec-plat" data-r="plat" aria-hidden="true"></span>'
  + '<div><h2 class="rec-device" data-r="device"></h2>'
  + '<p class="rec-meta"><span data-r="platform"></span><span class="rec-version" data-r="version"></span>'
  + '<span class="badge info" data-r="behind" hidden>' + dot() + '<span class="badge-text">Update available</span></span>'
  + '<span class="badge error" data-r="outdated" hidden title="This app is too old for the server. New uploads are refused until it is updated.">' + dot() + '<span class="badge-text">Not supported</span></span></p></div>'
  + '<span class="badge rec-status" data-r="status">' + dot() + '<span class="badge-text" data-r="statusText"></span></span></header>'
  + '<div class="banner" data-r="locked" hidden>' + icon('info') + '<span>Remote control is turned off on this computer.</span></div>'
  + '<div class="rec-banners" data-r="banners"></div>'
  + '<div class="rec-prompt" data-r="call" hidden><p class="rec-prompt-text" data-r="callText"></p>'
  + '<div class="rec-prompt-actions"><button type="button" class="btn primary" data-act="accept_call">Record</button>'
  + '<button type="button" class="btn secondary" data-act="dismiss_call">Not now</button></div></div>'
  + '<div class="rec-prompt" data-r="suggest" hidden><p class="rec-prompt-text" data-r="suggestText"></p><p class="rec-prompt-sub" data-r="suggestSub" hidden></p>'
  + '<div class="rec-prompt-actions"><button type="button" class="btn danger" data-act="stop_suggested">Stop recording</button>'
  + '<button type="button" class="btn secondary" data-act="keep">Keep recording</button></div></div>'
  + '<div class="rec-live" data-r="live" hidden><input type="text" data-r="liveName" maxlength="200" autocomplete="off" aria-label="Meeting name" placeholder="Untitled meeting" title="Rename this meeting"></div>'
  + '<div class="rec-meters" data-r="meters">' + REC_TRACKS.map(function (t) {
    return '<div class="rec-track" data-track="' + t.key + '"><span class="rec-track-label">' + t.label + '</span>'
      + '<div class="rec-meter" aria-hidden="true"><i class="rec-fill"></i><i class="rec-peak"></i></div>'
      + '<button type="button" class="btn secondary icon-only rec-mute" data-act="mute" data-track="' + t.key + '" aria-pressed="false" aria-label="' + t.mute + '" title="' + t.mute + '"><span data-r="muteIc"></span></button>'
      + '<p class="rec-track-sub"><span class="rec-sub-dev" data-r="dev"></span><span class="rec-sub-warn" data-r="warn" hidden>' + icon('alert', 14) + '<span data-r="warnText"></span></span></p></div>';
  }).join('') + '</div>'
  + '<p class="rec-meter-hint" data-r="meterHint" hidden></p>'
  + '<p class="rec-uploads" data-r="uploads" hidden></p>'
  + '<div class="rec-start" data-r="startRow"><input type="text" data-r="startName" maxlength="200" autocomplete="off" aria-label="Meeting name" placeholder="Meeting name (optional)">'
  + '<button type="button" class="btn primary" data-act="start" data-r="startBtn">Start recording</button></div>'
  + '<div class="rec-actions" data-r="stopRow" hidden><button type="button" class="btn danger" data-act="stop">Stop recording</button></div>'
  + '<div class="rec-actions">'
  + '<button type="button" class="btn secondary" data-act="recordings">' + icon('list') + '<span>Recordings</span></button>'
  + '<button type="button" class="btn secondary" data-act="refresh">' + icon('refresh') + '<span>Refresh devices</span></button>'
  + '<button type="button" class="btn secondary" data-act="retry" data-r="retryBtn" hidden>Retry uploads</button>'
  + '<button type="button" class="btn ghost" data-act="update" data-r="updateBtn">Check for updates</button></div>';

function recMakeCard(id) {
  var card = document.createElement('article');
  card.className = 'rec-card';
  card.dataset.id = id;
  card.innerHTML = REC_CARD_HTML;
  var r = {};
  card.querySelectorAll('[data-r]').forEach(function (el) {
    if (el.dataset.r === 'muteIc' || el.dataset.r === 'dev' || el.dataset.r === 'warn' || el.dataset.r === 'warnText') return;
    r[el.dataset.r] = el;
  });
  r.tracks = {};
  card.querySelectorAll('.rec-track').forEach(function (t) {
    r.tracks[t.dataset.track] = {
      wrap: t, fill: t.querySelector('.rec-fill'), peak: t.querySelector('.rec-peak'), mute: t.querySelector('.rec-mute'),
      muteIc: t.querySelector('[data-r="muteIc"]'), dev: t.querySelector('[data-r="dev"]'),
      warn: t.querySelector('[data-r="warn"]'), warnText: t.querySelector('[data-r="warnText"]')
    };
  });
  r.actBtns = {};
  card.querySelectorAll('[data-act]').forEach(function (b) { if (!b.dataset.track) r.actBtns[b.dataset.act] = b; });
  card._r = r; card._busy = {}; card._nameDirty = false;
  return card;
}
function recBannerNode(b) {
  var level = b.level === 'error' ? 'err' : b.level === 'warn' ? 'warn' : b.level === 'ok' ? 'ok' : '';
  var node = document.createElement('div');
  node.className = 'banner' + (level ? ' ' + level : '');
  node.innerHTML = icon(level === 'err' || level === 'warn' ? 'alert' : level === 'ok' ? 'check' : 'info');
  var span = document.createElement('span');
  span.textContent = b.text || '';
  node.appendChild(span);
  return node;
}
/* Buttons: disabled unless allowed and `on`; busy while their command is in flight. */
function recBtn(card, btn, key, on, title) {
  var busy = !!card._busy[key], locked = !recState(recs.get(card.dataset.id).item).allowed;
  btn.disabled = locked || busy || !on;
  btn.classList.toggle('is-busy', busy);
  if (title && on === false && !locked) btn.title = title; else if (btn.dataset.title) btn.title = btn.dataset.title; else btn.removeAttribute('title');
}

/* Bars only (also called on every idle ``levels`` frame, so it must stay light). */
function recUpdateMeters(card, s) {
  var r = card._r;
  REC_TRACKS.forEach(function (t) {
    var tr = s.tracks[t.key] || {}, d = r.tracks[t.key], mode = recMeterMode(s, t.key), shown = mode !== 'off';
    d.fill.style.transform = 'scaleX(' + (shown ? recMeter(tr.level) : 0).toFixed(3) + ')';
    d.peak.style.left = 'calc(' + ((mode === 'live' ? recMeter(tr.peak) : 0) * 100).toFixed(1) + '% - 2px)';
    d.wrap.classList.toggle('live', mode === 'live');
    d.wrap.classList.toggle('preview', mode === 'preview');
    d.wrap.classList.toggle('muted', !!tr.muted);
  });
  var hint = recMeterHint(s);
  r.meterHint.hidden = !hint;
  recSet(r.meterHint, hint);
}

function recUpdateCard(card, entry, now) {
  now = now || Date.now();
  var it = entry.item, s = recState(it), r = card._r;
  var rec = s.status === 'recording', fin = s.status === 'finishing', idle = s.status === 'idle';
  card.dataset.status = s.status;
  recSet(r.device, it.device || 'Unknown computer');
  r.device.title = it.device || '';
  recSetIcon(r.plat, it.platform === 'macos' ? 'laptop' : 'monitor', 20);
  recSet(r.platform, it.platform_text || '');
  r.platform.hidden = !it.platform_text;
  recSet(r.version, it.version ? 'v' + it.version : 'Version unknown');
  r.behind.hidden = !(it.behind || s.update.available);
  r.outdated.hidden = !it.outdated;
  var st = recStatus(entry, now);
  r.status.className = 'badge rec-status ' + st.cls;
  recSet(r.statusText, st.label);
  r.locked.hidden = s.allowed;

  // banners (skip "update available": the card has its own update control)
  var banners = s.banners.filter(function (b) { return !(b && b.id === 'update_available' && s.update.available); });
  var sig = JSON.stringify(banners.map(function (b) { return [b.level, b.text]; }));
  if (r.banners._sig !== sig) {
    r.banners._sig = sig;
    r.banners.textContent = '';
    banners.forEach(function (b) { r.banners.appendChild(recBannerNode(b)); });
  }

  // call prompt and stop suggestion
  var prompt = s.call.prompt, sug = s.suggestion;
  r.call.hidden = !prompt;
  if (prompt) recSet(r.callText, recCallText(prompt));
  r.suggest.hidden = !sug;
  if (sug) {
    recSet(r.suggestText, sug.title || 'Stop recording?');
    var left = sug.seconds_left == null ? null : Math.max(0, Math.round(Number(sug.seconds_left) - (now - entry.at) / 1000));
    r.suggestSub.hidden = left == null;
    if (left != null) recSet(r.suggestSub, 'Stops in ' + plural(left, 'second', 'seconds') + '.');
  }
  recBtn(card, r.actBtns.accept_call, 'accept_call_prompt', true);
  recBtn(card, r.actBtns.dismiss_call, 'dismiss_call_prompt', true);
  recBtn(card, r.actBtns.stop_suggested, 'stop_suggested', true);
  recBtn(card, r.actBtns.keep, 'keep_recording', true);

  // meeting name (recording: editable; typed text is kept while frames arrive)
  r.live.hidden = idle;
  if (!idle) {
    var name = s.meeting.name || '';
    if (!card._nameDirty && document.activeElement !== r.liveName && r.liveName.value !== name) r.liveName.value = name;
    r.liveName.disabled = !s.allowed || fin;
  }

  // level meters
  recUpdateMeters(card, s);
  REC_TRACKS.forEach(function (t) {
    var tr = s.tracks[t.key] || {}, d = r.tracks[t.key], connected = !!tr.connected, muted = !!tr.muted;
    d.mute.setAttribute('aria-pressed', muted ? 'true' : 'false');
    recSetIcon(d.muteIc, muted ? t.off : t.on, 16);
    d.mute.dataset.title = t.mute + (muted ? ' (muted)' : '');
    recBtn(card, d.mute, (muted ? 'unmute' : 'mute') + t.key, rec);
    d.mute.title = d.mute.dataset.title;
    recSet(d.dev, tr.device || (connected ? 'Default device' : ''));
    d.warn.hidden = connected && !tr.degraded;
    if (!d.warn.hidden) recSet(d.warnText, !connected ? 'Not connected' : 'Degraded audio');
  });

  // uploads
  var up = recUploadLine(s.uploads);
  r.uploads.hidden = !up.text;
  recSet(r.uploads, up.text);
  r.uploads.classList.toggle('has-failed', up.failed);

  // actions
  r.startRow.hidden = !idle;
  r.actBtns.start.className = 'btn ' + (prompt ? 'secondary' : 'primary');
  r.startName.disabled = !s.allowed;
  recBtn(card, r.actBtns.start, 'start', idle);
  r.stopRow.hidden = !rec || !!sug;
  recBtn(card, r.actBtns.stop, 'stop', rec);
  recBtn(card, r.actBtns.refresh, 'refresh_devices', true);
  var pend = (Number(s.uploads.pending) || 0) + (Number(s.uploads.failed) || 0);
  r.retryBtn.hidden = pend <= 0;
  recBtn(card, r.retryBtn, 'retry_uploads', true);
  var ub = r.updateBtn, upd = s.update;
  if (upd.installing) {
    ub.className = 'btn secondary'; ub.dataset.cmd = 'install_update'; recSet(ub, 'Updating...');
    recBtn(card, ub, 'install_update', false);
  } else if (upd.available) {
    ub.className = 'btn secondary'; ub.dataset.cmd = 'install_update';
    recSet(ub, upd.version ? 'Update to v' + upd.version : 'Update');
    recBtn(card, ub, 'install_update', idle, 'Finish the recording before updating.');
  } else {
    ub.className = 'btn ghost'; ub.dataset.cmd = 'check_update'; recSet(ub, 'Check for updates');
    recBtn(card, ub, 'check_update', true);
  }
}

function recRender() {
  var grid = document.getElementById('rec-grid'), now = Date.now(), ids = recSorted();
  recCards.forEach(function (card, id) {
    if (!recs.has(id)) { card.remove(); recCards.delete(id); }
  });
  ids.forEach(function (id, i) {
    var card = recCards.get(id);
    if (!card) { card = recMakeCard(id); recCards.set(id, card); }
    if (grid.children[i] !== card) grid.insertBefore(card, grid.children[i] || null);
    recUpdateCard(card, recs.get(id), now);
  });
  var n = ids.length, live = ids.filter(function (id) { return recState(recs.get(id).item).status === 'recording'; }).length;
  recSet(document.getElementById('rec-count'), recReady ? plural(n, 'recorder', 'recorders') + (live ? ' · ' + live + ' recording' : '') : '');
  document.getElementById('rec-empty').hidden = !(recReady && n === 0);
  document.getElementById('rec-loading').hidden = recReady;
  grid.hidden = n === 0;
  grid.setAttribute('aria-busy', recReady ? 'false' : 'true');
}

function recSetConn(ok) {
  var note = document.getElementById('rec-conn');
  note.hidden = ok || !recEverClosed;
}
function recApply(msg) {
  if (!msg || typeof msg !== 'object') return;
  if (msg.type === 'snapshot' && Array.isArray(msg.items)) {
    var now = Date.now();
    recs = new Map();
    msg.items.forEach(function (it) { if (it && it.instance_id) recs.set(it.instance_id, {item: it, at: now}); });
    recReady = true;
  } else if (msg.type === 'upsert' && msg.item && msg.item.instance_id) {
    recs.set(msg.item.instance_id, {item: msg.item, at: Date.now()});
  } else if (msg.type === 'remove' && msg.instance_id) {
    recs.delete(msg.instance_id);
  } else if (msg.type === 'levels' && msg.instance_id) {
    recApplyLevels(msg);   // idle level preview: bars only, no re-render
    return;
  } else { return; }
  recRender();
  recPanelOnFrame();
}
function recApplyLevels(msg) {
  var entry = recs.get(msg.instance_id);
  if (!entry || !msg.tracks || typeof msg.tracks !== 'object') return;
  var st = entry.item.state || (entry.item.state = {});
  if (st.status && st.status !== 'idle') return;
  st.tracks = st.tracks || {};
  REC_TRACKS.forEach(function (t) {
    var v = msg.tracks[t.key];
    if (typeof v !== 'number' || isNaN(v)) return;
    var tr = st.tracks[t.key] || (st.tracks[t.key] = {});
    tr.level = v; tr.peak = v;
  });
  var card = recCards.get(msg.instance_id);
  if (card) recUpdateMeters(card, recState(entry.item));
}
/* Tells the server whether this page is visible: recorders only stream idle levels while someone looks. */
function recSendWatch() {
  if (!recSocket || recSocket.readyState !== 1) return;
  try { recSocket.send(JSON.stringify({type: 'watch', visible: !document.hidden})); } catch (_) {}
}
function recCheckAuth() {
  return fetch('/v1/recorders', {credentials: 'same-origin'}).then(function (r) {
    if (r.status === 401 || r.status === 403) window.location = '/login';
  }).catch(function () {});
}
function recConnect() {
  var ws, proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  try { ws = new WebSocket(proto + '//' + location.host + '/v1/recorders/events'); } catch (_) { recScheduleReconnect(); return; }
  recSocket = ws;
  ws.onopen = recSendWatch;
  ws.onmessage = function (event) {
    var msg; try { msg = JSON.parse(event.data); } catch (_) { return; }
    if (msg && msg.type === 'snapshot') { recBackoff = 1000; recSetConn(true); }
    recApply(msg);
  };
  ws.onerror = function () { try { ws.close(); } catch (_) {} };
  ws.onclose = function () {
    if (recSocket !== ws) return;
    recSocket = null; recEverClosed = true; recSetConn(false);
    recScheduleReconnect();
    recCheckAuth();
  };
}
function recScheduleReconnect() {
  clearTimeout(recReconnect);
  recReconnect = setTimeout(recConnect, recBackoff);
  recBackoff = Math.min(15000, recBackoff * 2);
}

function recPost(id, command, args) {
  return fetch('/v1/recorders/' + encodeURIComponent(id) + '/commands', {
    method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({command: command, args: args || {}})
  }).then(function (r) {
    if (r.status === 401 || r.status === 403) { window.location = '/login'; throw new Error('Signed out'); }
    if (r.status === 404) throw new Error('That recorder is no longer connected.');
    if (!r.ok) {
      return r.json().catch(function () { return {}; }).then(function (b) {
        throw new Error(typeof b.detail === 'string' && b.detail ? b.detail : 'The recorder did not respond.');
      });
    }
    return r.json();
  });
}
/* Sends one command; resolves true on success. Busy state comes from card._busy[key]. */
function recSend(card, key, command, args) {
  var id = card.dataset.id, entry = recs.get(id);
  if (!entry || card._busy[key]) return Promise.resolve(false);
  var device = entry.item.device;
  card._busy[key] = true; recUpdateCard(card, entry);
  return recPost(id, command, args).then(function (res) {
    res = res || {};
    var cur = recs.get(id);
    if (cur && res.state && typeof res.state === 'object') { cur.item.state = res.state; cur.at = Date.now(); }
    if (res.ok === false) { notify(res.error || 'The recorder could not do that.', 'error'); return false; }
    notify(recToast(command, args, device, res.state));
    return true;
  }, function (err) { notify(err.message || 'Request failed', 'error'); return false; }).then(function (ok) {
    delete card._busy[key];
    var cur = recs.get(id);
    if (cur && recCards.get(id) === card) recUpdateCard(card, cur);
    return ok;
  });
}
function recCommitName(card) {
  var entry = recs.get(card.dataset.id), input = card._r.liveName;
  if (!entry || !card._nameDirty) return;
  var value = input.value.replace(/\s+/g, ' ').trim(), current = recState(entry.item).meeting.name || '';
  if (!value || value === current) { card._nameDirty = false; input.value = current; return; }
  recSend(card, 'set_name', 'set_name', {name: value}).then(function (ok) {
    card._nameDirty = false;
    var cur = recs.get(card.dataset.id);
    if (!ok && cur) input.value = recState(cur.item).meeting.name || '';
  });
}
function recOnClick(event) {
  var btn = event.target.closest('[data-act]');
  if (!btn || btn.disabled) return;
  var card = btn.closest('.rec-card'), entry = card && recs.get(card.dataset.id);
  if (!entry) return;
  var s = recState(entry.item), act = btn.dataset.act, device = entry.item.device || 'this computer';
  if (act === 'recordings') { recPanelOpen(card.dataset.id, btn); return; }
  if (act === 'start') {
    var name = card._r.startName.value.replace(/\s+/g, ' ').trim();
    recSend(card, 'start', 'start', name ? {name: name} : {}).then(function (ok) { if (ok) card._r.startName.value = ''; });
  } else if (act === 'stop') {
    confirmDialog({title: 'Stop recording on ' + device + '?', lead: 'The recording ends and is uploaded for transcription.', confirmLabel: 'Stop recording'}).then(function (ok) {
      if (ok) recSend(card, 'stop', 'stop', {});
    });
  } else if (act === 'mute') {
    var track = btn.dataset.track, muted = !!(s.tracks[track] && s.tracks[track].muted), cmd = muted ? 'unmute' : 'mute';
    recSend(card, cmd + track, cmd, {track: track});
  } else if (act === 'refresh') { recSend(card, 'refresh_devices', 'refresh_devices', {}); }
  else if (act === 'retry') { recSend(card, 'retry_uploads', 'retry_uploads', {}); }
  else if (act === 'update') { recSend(card, btn.dataset.cmd, btn.dataset.cmd, {}); }
  else if (act === 'accept_call') {
    var nm = s.call.prompt && s.call.prompt.name;
    recSend(card, 'accept_call_prompt', 'accept_call_prompt', nm ? {name: nm} : {});
  } else if (act === 'dismiss_call') { recSend(card, 'dismiss_call_prompt', 'dismiss_call_prompt', {}); }
  else if (act === 'stop_suggested') { recSend(card, 'stop_suggested', 'stop_suggested', {}); }
  else if (act === 'keep') { recSend(card, 'keep_recording', 'keep_recording', {}); }
}
function recOnKey(event) {
  var t = event.target, card = t.closest && t.closest('.rec-card');
  if (!card) return;
  if (t.matches('[data-r="startName"]') && event.key === 'Enter') { event.preventDefault(); card._r.actBtns.start.click(); }
  else if (t.matches('[data-r="liveName"]')) {
    if (event.key === 'Enter') { event.preventDefault(); t.blur(); }
    else if (event.key === 'Escape') { card._nameDirty = false; var e = recs.get(card.dataset.id); t.value = e ? recState(e.item).meeting.name || '' : ''; t.blur(); }
  }
}

/* ---- Recordings panel: every recording saved on one recorder, with its upload status ---- */
var REC_TONE_CLASS = {ok: 'done', info: 'running', warn: 'warn', error: 'error', muted: 'none'};
var REC_FILTERS = {
  uploaded: ['uploaded_ready', 'uploaded_transcribing', 'uploaded_queued', 'uploaded_error'],
  moving: ['uploading', 'waiting'],
  missing: ['not_on_server', 'in_trash', 'partial'],
  failed: ['failed', 'invalid']
};
var REC_OFFLINE_TEXT = 'Recorder went offline. Close this panel or wait for it to reconnect.';
var REC_LOCKED_TEXT = 'Remote control is turned off on this computer.';
var recPanel = null, recPanelUi = null;

function recToneClass(tone) { return REC_TONE_CLASS[tone] || 'none'; }
function recInFlight(row) { return !!row && (row.status === 'uploading' || row.status === 'waiting'); }
/* "42 recordings · 2 not on server · 1 failed": only the groups that need a look are listed. */
function recRowsSummary(summary, rowCount) {
  var by = (summary && summary.by_status) || {}, total = summary && summary.total != null ? Number(summary.total) : Number(rowCount) || 0;
  function n(k) { return Number(by[k]) || 0; }
  var parts = [plural(total, 'recording', 'recordings')];
  var missing = n('not_on_server') + n('partial'), trash = n('in_trash'), failed = n('failed') + n('invalid'), moving = n('uploading') + n('waiting');
  if (missing) parts.push(missing + ' not on server');
  if (trash) parts.push(trash + ' in server trash');
  if (failed) parts.push(failed + ' failed');
  if (moving) parts.push(moving + ' uploading');
  return parts.join(' · ');
}
function recRowText(row) {
  return [row.name, row.started ? fmtDate(row.started) : '', row.started ? fmtDate(row.started, true) : '', row.label].join(' ').toLowerCase();
}
function recFilterRows(rows, query, filter) {
  var words = String(query || '').toLowerCase().split(/\s+/).filter(Boolean), group = REC_FILTERS[filter];
  return (rows || []).filter(function (row) {
    if (group && group.indexOf(row.status) < 0) return false;
    if (!words.length) return true;
    var text = recRowText(row);
    return words.every(function (w) { return text.indexOf(w) >= 0; });
  });
}
function recCanReupload(row) {
  if (row.active || row.status === 'recording') return {ok: false, why: 'This recording is still in progress.'};
  if (row.status === 'uploading') return {ok: false, why: 'This recording is uploading right now.'};
  if (row.valid === false) return {ok: false, why: row.reason ? 'Cannot upload: ' + row.reason : 'This recording is not valid, so it cannot be uploaded.'};
  return {ok: true, why: ''};
}
function recCanDelete(row) {
  if (row.active || row.status === 'recording') return {ok: false, why: 'Stop the recording before deleting it.'};
  if (row.status === 'uploading') return {ok: false, why: 'Wait for the upload to finish before deleting it.'};
  return {ok: true, why: ''};
}
function recDeleteNeedsWarning(rows) {
  return (rows || []).some(function (r) { return r.server_has_copy === false; });
}
function recDeleteWarning(trashName) {
  return 'The server has no copy. This permanently removes the only copy (it goes to this computer\'s ' + (trashName || 'Recycle Bin') + ').';
}
function recMetaText(row) {
  var parts = [row.started ? fmtDate(row.started, true) : 'Unknown date'];
  if (row.duration_sec != null && !isNaN(Number(row.duration_sec))) parts.push(fmtDuration(row.duration_sec));
  parts.push(fmtBytes(row.size_bytes));
  return parts.join(' · ');
}
/* Options for confirmDialog() when deleting `rows` from the recorder. */
function recDeleteDialog(rows, trashName, device) {
  var risky = recDeleteNeedsWarning(rows), bare = rows.filter(function (r) { return r.server_has_copy === false; });
  var items = bare.concat(rows.filter(function (r) { return r.server_has_copy !== false; })).map(function (r) {
    return {name: r.name, meta: r.server_has_copy === false ? 'Not on server' : (r.started ? fmtDate(r.started, true) : 'Unknown date') + ' · ' + fmtBytes(r.size_bytes)};
  });
  return {
    title: 'Delete from this computer?',
    lead: risky
      ? 'Removing ' + plural(rows.length, 'recording', 'recordings') + ' from ' + (device || 'this computer') + '. ' + bare.length + (bare.length === 1 ? ' of them is' : ' of them are') + ' not on the server.'
      : 'They stay on the server. This only frees space on this computer (it goes to this computer\'s ' + (trashName || 'Recycle Bin') + ').',
    warning: risky ? recDeleteWarning(trashName) : '',
    items: items,
    itemsLabel: 'Recordings to delete',
    note: risky ? 'To get one back, restore it from the ' + (trashName || 'Recycle Bin') + ' on that computer.' : '',
    confirmLabel: 'Delete',
    danger: true
  };
}
function recSafeMeetingUrl(url) {
  url = typeof url === 'string' ? url : '';
  return url.indexOf('/sessions/') === 0 && /^[A-Za-z0-9._%~-]+$/.test(url.slice(10)) && url.indexOf('..') < 0 ? url : null;
}
/* One problem out of an HTTP answer: null when it worked. `offline` = the recorder is gone. */
function recProblem(status, data) {
  data = data || {};
  if (status === 404 || status === 409) return {offline: true, text: REC_OFFLINE_TEXT};
  if (status === 504) return {text: 'The recorder did not answer in time. Try again.'};
  if (status === 429) return {text: 'The recorder is busy with earlier commands. Try again in a moment.'};
  if (status < 200 || status >= 300) return {text: typeof data.detail === 'string' && data.detail ? data.detail : 'Could not reach the recorder.'};
  if (data.ok === false) {
    var locked = data.code === 'remote_control_disabled';
    return {text: locked ? REC_LOCKED_TEXT : (data.error || 'The recorder could not do that.'), locked: locked};
  }
  return null;
}
/* Toast / notice text for a finished reupload or delete; `names` maps session id -> name. */
function recActionMessage(kind, res, names, device) {
  res = res || {};
  var results = Array.isArray(res.results) ? res.results : [], refused = results.filter(function (r) { return !r.ok; });
  var okCount = results.length - refused.length, del = kind === 'delete', parts = [];
  var done = Number(del ? res.deleted : res.queued);
  if (isNaN(done)) done = okCount;
  if (del) {
    if (done > 0) parts.push('Deleted ' + plural(done, 'recording', 'recordings') + ' from ' + device + '.');
  } else {
    var already = Number(res.already_queued) || 0;
    if (done > 0) parts.push(plural(done, 'recording', 'recordings') + ' queued for upload on ' + device + '.');
    else if (already > 0) parts.push(plural(already, 'recording is', 'recordings are') + ' already in the upload queue on ' + device + '.');
  }
  if (refused.length) {
    var first = refused[0], why = String(first.error || 'The recorder refused.');
    parts.push('Could not ' + (del ? 'delete ' : 're-upload ') + ((names && names[first.session_id]) || 'a recording') + ': ' + why.replace(/\s+$/, '') + (/[.!?]$/.test(why) ? '' : '.')
      + (refused.length > 1 ? ' (' + (refused.length - 1) + ' more could not be ' + (del ? 'deleted' : 're-uploaded') + '.)' : ''));
  }
  if (!parts.length) parts.push(del ? 'Nothing was deleted.' : 'Nothing was queued.');
  return {text: parts.join(' '), error: refused.length > 0 && done <= 0, refused: refused.length};
}

function recPanelUiInit() {
  var g = function (id) { return document.getElementById(id); };
  recPanelUi = {
    dlg: g('rec-panel'), title: g('rp-title'), summary: g('rp-summary'), close: g('rp-close'), search: g('rp-search'), filter: g('rp-filter'),
    refresh: g('rp-refresh'), all: g('rp-all'), shown: g('rp-shown'), alert: g('rp-alert'), alertIc: g('rp-alert-ic'), alertText: g('rp-alert-text'),
    notice: g('rp-notice'), noticeIc: g('rp-notice-ic'), noticeText: g('rp-notice-text'), note: g('rp-note'), body: g('rp-body'), list: g('rp-list'),
    state: g('rp-state'), stateTitle: g('rp-state-title'), stateText: g('rp-state-text'), retry: g('rp-retry'), skel: g('rp-skel'),
    bulk: g('rp-bulk'), selCount: g('rp-selcount'), bulkRe: g('rp-bulk-reupload'), bulkDel: g('rp-bulk-delete'), bulkClear: g('rp-bulk-clear')
  };
  return recPanelUi;
}

var REC_ROW_HTML =
  '<label class="rp-check"><input type="checkbox" data-r="check"></label>'
  + '<div class="rp-main"><p class="rp-name" data-r="name"></p><p class="rp-meta" data-r="meta"></p><p class="rp-detail" data-r="detail" hidden></p></div>'
  + '<div class="rp-status"><span class="badge" data-r="badge">' + dot() + '<span class="badge-text" data-r="badgeText"></span></span></div>'
  + '<div class="rp-actions">'
  + '<a class="btn ghost sm" data-rp="open" hidden>' + icon('open', 14) + '<span class="rp-short">Open</span><span class="rp-long">Open on server</span></a>'
  + '<button type="button" class="btn secondary sm" data-rp="reupload">' + icon('refresh', 14) + '<span>Re-upload</span></button>'
  + '<button type="button" class="btn danger sm" data-rp="delete">' + icon('trash', 14) + '<span class="rp-short">Delete</span><span class="rp-long">Delete from this computer</span></button></div>';

function recRowMake() {
  var li = document.createElement('li');
  li.className = 'rp-row';
  li.innerHTML = REC_ROW_HTML;
  var r = {};
  li.querySelectorAll('[data-r]').forEach(function (el) { r[el.dataset.r] = el; });
  li.querySelectorAll('[data-rp]').forEach(function (el) { r[el.dataset.rp] = el; });
  li._r = r;
  return li;
}
function recRowUpdate(li, row, P) {
  var r = li._r, id = row.session_id, sel = P.sel.has(id), idle = P.offline || P.busy;
  li.dataset.id = id;
  li.classList.toggle('is-selected', sel);
  r.check.checked = sel;
  r.check.setAttribute('aria-label', 'Select ' + row.name);
  recSet(r.name, row.name); r.name.title = row.name;
  recSet(r.meta, recMetaText(row));
  r.badge.className = 'badge ' + recToneClass(row.tone);
  recSet(r.badgeText, row.label);
  r.detail.hidden = !row.detail;
  recSet(r.detail, row.detail || '');
  var ru = recCanReupload(row), de = recCanDelete(row);
  r.reupload.disabled = idle || !ru.ok;
  r.reupload.setAttribute('aria-label', 'Re-upload ' + row.name);
  r.reupload.title = ru.ok ? '' : ru.why;
  if (ru.ok) r.reupload.removeAttribute('title');
  r.delete.disabled = idle || !de.ok;
  r.delete.setAttribute('aria-label', 'Delete ' + row.name + ' from this computer');
  r.delete.title = de.ok ? 'Move this recording to the ' + P.trash + ' on this computer' : de.why;
  var url = recSafeMeetingUrl(row.meeting_url);
  r.open.hidden = !url;
  if (url) { r.open.setAttribute('href', url); r.open.setAttribute('aria-label', 'Open ' + row.name + ' on the server'); }
  else r.open.removeAttribute('href');
}

function recPanelSetBanner(el, icEl, textEl, text, icName) {
  el.hidden = !text;
  if (text) { recSetIcon(icEl, icName, 16); recSet(textEl, text); }
}
function recPanelRender() {
  var P = recPanel, ui = recPanelUi;
  if (!P || !ui) return;
  var hasRows = P.rows.length > 0, visible = recFilterRows(P.rows, P.q, P.f);
  recSet(ui.title, 'Recordings on ' + P.device);
  recSet(ui.summary, P.data ? recRowsSummary(P.data.summary, P.rows.length) : (P.loaded ? '' : 'Loading recordings...'));
  ui.body.setAttribute('aria-busy', !P.loaded && !P.error && !P.offline ? 'true' : 'false');
  ui.body.classList.toggle('is-offline', P.offline);
  ui.refresh.classList.toggle('is-busy', P.inflight);
  ui.refresh.setAttribute('aria-busy', P.inflight ? 'true' : 'false');
  ui.search.disabled = !hasRows; ui.filter.disabled = !hasRows;

  // alerts: offline (kept list dimmed) or a refresh problem while a list is showing
  var alertText = hasRows ? (P.offline ? REC_OFFLINE_TEXT : (P.error || '')) : '';
  recPanelSetBanner(ui.alert, ui.alertIc, ui.alertText, alertText, 'alert');
  ui.alert.className = 'banner rp-banner ' + (P.offline ? 'warn' : 'err');
  var truncated = P.data && P.data.truncated;
  ui.note.hidden = !truncated;
  recSet(ui.note, truncated ? 'Showing the newest ' + P.rows.length + ' of ' + (P.data.total || P.rows.length) + ' recordings.' : '');
  if (P.noticeText) { ui.notice.hidden = false; ui.notice.className = 'banner rp-banner ' + (P.noticeKind === 'err' ? 'err' : 'ok'); recSetIcon(ui.noticeIc, P.noticeKind === 'err' ? 'alert' : 'check', 16); recSet(ui.noticeText, P.noticeText); }
  else ui.notice.hidden = true;

  // state area: loading, empty, error, offline, no match
  var stateTitle = '', stateText = '', showRetry = false;
  if (!P.loaded && !P.error && !P.offline) { stateTitle = 'Loading recordings...'; }
  else if (!hasRows && P.error) { stateTitle = 'Could not load recordings'; stateText = P.error; showRetry = true; }
  else if (!hasRows && P.offline) { stateTitle = 'Recorder went offline'; stateText = REC_OFFLINE_TEXT; }
  else if (!hasRows) { stateTitle = 'No saved recordings on this computer.'; stateText = 'Recordings show up here after a meeting is recorded on this computer, and stay until they are deleted from it.'; }
  else if (!visible.length) { stateTitle = 'No recordings match.'; stateText = 'Try a different search or choose All statuses.'; }
  ui.state.hidden = !stateTitle;
  ui.skel.hidden = P.loaded || !!P.error || P.offline;
  recSet(ui.stateTitle, stateTitle); recSet(ui.stateText, stateText);
  ui.stateText.hidden = !stateText;
  ui.retry.hidden = !showRetry;

  // list (rows are reused by id, so focus and scroll survive a refresh)
  var keep = {};
  visible.forEach(function (row) { keep[row.session_id] = true; });
  P.els.forEach(function (li, id) { if (!keep[id]) { li.remove(); P.els.delete(id); } });
  visible.forEach(function (row, i) {
    var li = P.els.get(row.session_id);
    if (!li) { li = recRowMake(); P.els.set(row.session_id, li); }
    if (ui.list.children[i] !== li) ui.list.insertBefore(li, ui.list.children[i] || null);
    recRowUpdate(li, row, P);
  });
  ui.list.hidden = !visible.length;

  // select-all-visible and the count of what the filter hides
  var nSel = visible.filter(function (r) { return P.sel.has(r.session_id); }).length;
  ui.all.checked = visible.length > 0 && nSel === visible.length;
  ui.all.indeterminate = nSel > 0 && nSel < visible.length;
  ui.all.disabled = !visible.length || P.offline || P.busy;
  recSet(ui.shown, hasRows && visible.length !== P.rows.length ? 'Showing ' + visible.length + ' of ' + P.rows.length : '');

  // bulk bar
  var chosen = P.rows.filter(function (r) { return P.sel.has(r.session_id); });
  var canRe = chosen.filter(function (r) { return recCanReupload(r).ok; }).length, canDel = chosen.filter(function (r) { return recCanDelete(r).ok; }).length;
  ui.bulk.hidden = !chosen.length;
  recSet(ui.selCount, chosen.length + ' selected');
  ui.bulkRe.disabled = P.offline || P.busy || !canRe;
  ui.bulkDel.disabled = P.offline || P.busy || !canDel;
  ui.bulkRe.title = canRe && canRe < chosen.length ? (chosen.length - canRe) + ' cannot be re-uploaded right now and are skipped.' : '';
  ui.bulkDel.title = canDel && canDel < chosen.length ? (chosen.length - canDel) + ' cannot be deleted right now and are skipped.' : '';
  if (!ui.bulkRe.title) ui.bulkRe.removeAttribute('title');
  if (!ui.bulkDel.title) ui.bulkDel.removeAttribute('title');
}
function recPanelNotice(text, kind) {
  var P = recPanel;
  if (!P) return;
  clearTimeout(P.noticeTimer);
  P.noticeText = text; P.noticeKind = kind;
  P.noticeTimer = setTimeout(function () { if (recPanel === P) { P.noticeText = ''; recPanelRender(); } }, 12000);
  recPanelRender();
}

function recApi(P, path, body) {
  var opts = {credentials: 'same-origin'};
  if (body) { opts.method = 'POST'; opts.headers = {'Content-Type': 'application/json'}; opts.body = JSON.stringify(body); }
  return fetch('/v1/recorders/' + encodeURIComponent(P.id) + path, opts).then(function (r) {
    if (r.status === 401 || r.status === 403) { window.location = '/login'; throw new Error('Signed out'); }
    return r.json().catch(function () { return {}; }).then(function (data) { return {status: r.status, data: data || {}}; });
  });
}
function recPanelLoad() {
  var P = recPanel;
  if (!P) return Promise.resolve();
  if (P.inflight) { P.again = true; return Promise.resolve(); }
  P.inflight = true; clearTimeout(P.timer); clearTimeout(P.reload);
  recPanelRender();
  var asked = P.id;
  return recApi(P, '/recordings').then(function (res) {
    if (recPanel !== P) return;
    if (asked !== P.id) { P.again = true; return; } // the panel was rebound to a restarted recorder: ask again
    var prob = recProblem(res.status, res.data);
    if (!prob) {
      P.data = res.data; P.rows = Array.isArray(res.data.recordings) ? res.data.recordings : [];
      P.error = null; P.offline = false; P.loaded = true;
      if (res.data.device) P.device = res.data.device;
      if (res.data.trash_name) P.trash = res.data.trash_name;
      var ids = {};
      P.rows.forEach(function (r) { ids[r.session_id] = true; });
      Array.from(P.sel).forEach(function (id) { if (!ids[id]) P.sel.delete(id); });
    } else if (prob.offline) { P.offline = true; P.offlineBy = 'http'; P.loaded = true; }
    else {
      P.error = prob.text; P.loaded = true;
      if (prob.locked) { P.data = null; P.rows = []; P.sel.clear(); }
    }
  }, function () {
    if (recPanel === P) { P.error = 'Could not reach the server. Check the connection and try again.'; P.loaded = true; }
  }).then(function () {
    if (recPanel !== P) return;
    P.inflight = false;
    recPanelRender();
    if (P.again) { P.again = false; return recPanelLoad(); }
    // while something is uploading, keep the list fresh; stop as soon as nothing is in flight
    if (!P.offline && !P.error && P.rows.some(recInFlight)) P.timer = setTimeout(recPanelLoad, 4000);
  });
}
function recPanelRun(P, kind, rows, skipped) {
  var names = {}, ids = rows.map(function (r) { names[r.session_id] = r.name; return r.session_id; });
  P.busy = true; recPanelRender();
  var asked = P.id;
  return recApi(P, '/recordings/' + kind, {session_ids: ids}).then(function (res) {
    if (recPanel !== P) return;
    var prob = recProblem(res.status, res.data);
    if (prob) {
      if (prob.offline && asked === P.id) { P.offline = true; P.offlineBy = 'http'; }
      recPanelNotice(prob.text, 'err'); notify(prob.text, 'error');
      return;
    }
    var msg = recActionMessage(kind, res.data, names, P.device);
    var text = msg.text + (skipped ? ' ' + plural(skipped, 'selected recording was', 'selected recordings were') + ' skipped because ' + (skipped === 1 ? 'it' : 'they') + ' cannot be ' + (kind === 'delete' ? 'deleted' : 're-uploaded') + ' right now.' : '');
    recPanelNotice(text, msg.refused ? 'err' : 'ok'); notify(text, msg.error ? 'error' : undefined);
    (res.data.results || []).forEach(function (r) { if (r.ok) P.sel.delete(r.session_id); });
    if (kind === 'delete') recPanelLoad(); else P.reload = setTimeout(recPanelLoad, 1000);
  }, function (err) {
    if (recPanel === P && !(err && err.message === 'Signed out')) { recPanelNotice('Request failed. Check the connection and try again.', 'err'); }
  }).then(function () {
    if (recPanel !== P) return;
    P.busy = false; recPanelRender();
  });
}
function recPanelReupload(rows) {
  var P = recPanel;
  if (!P || P.busy || P.offline) return;
  var ok = rows.filter(function (r) { return recCanReupload(r).ok; }), skipped = rows.length - ok.length;
  if (!ok.length) { recPanelNotice('None of the selected recordings can be re-uploaded right now.', 'err'); return; }
  var go = ok.length > 1 ? confirmDialog({
    title: 'Re-upload ' + plural(ok.length, 'recording', 'recordings') + '?',
    lead: 'They go back in the upload queue on ' + P.device + ' and are sent to the server again.',
    items: ok.map(function (r) { return {name: r.name, meta: r.label}; }), itemsLabel: 'Recordings to re-upload',
    confirmLabel: 'Re-upload', danger: false
  }) : Promise.resolve(true);
  go.then(function (yes) { if (yes && recPanel === P && !P.busy) recPanelRun(P, 'reupload', ok, skipped); });
}
function recPanelDelete(rows) {
  var P = recPanel;
  if (!P || P.busy || P.offline) return;
  var ok = rows.filter(function (r) { return recCanDelete(r).ok; }), skipped = rows.length - ok.length;
  if (!ok.length) { recPanelNotice('None of the selected recordings can be deleted right now.', 'err'); return; }
  confirmDialog(recDeleteDialog(ok, P.trash, P.device)).then(function (yes) {
    if (yes && recPanel === P && !P.busy) recPanelRun(P, 'delete', ok, skipped);
  });
}
function recPanelRowById(id) {
  var P = recPanel, hit = null;
  if (P) P.rows.forEach(function (r) { if (r.session_id === id) hit = r; });
  return hit;
}
function recPanelOpen(id, launcher) {
  var ui = recPanelUi || recPanelUiInit(), entry = recs.get(id);
  if (!ui.dlg || !ui.dlg.showModal) return;
  recPanel = {
    id: id, device: (entry && entry.item.device) || 'this computer', trash: (entry && entry.item.platform === 'macos') ? 'Trash' : 'Recycle Bin',
    launcher: launcher || null, last: (entry && entry.item) || null, rows: [], data: null, sel: new Set(), q: '', f: 'all', loaded: false, error: null, offline: false, offlineBy: '',
    inflight: false, again: false, busy: false, timer: null, reload: null, noticeTimer: null, noticeText: '', noticeKind: '', els: new Map(), downOnBackdrop: false
  };
  ui.list.textContent = ''; ui.search.value = ''; ui.filter.value = 'all';
  recPanelRender();
  ui.dlg.showModal();
  ui.close.focus();
  recPanelLoad();
}
function recPanelClosed() {
  var P = recPanel;
  if (!P) return;
  clearTimeout(P.timer); clearTimeout(P.reload); clearTimeout(P.noticeTimer);
  recPanel = null;
  var l = P.launcher;
  if (l && document.body.contains(l)) l.focus();
}
/* A restarted client gets a NEW instance id, so the panel follows the machine, not the id. The presence frames carry
   no machine id; the stable identity is the device name plus the OS family. */
function recSameMachine(prev, item) {
  if (!prev || !item) return false;
  var a = String(prev.device || '').trim().toLowerCase(), b = String(item.device || '').trim().toLowerCase();
  if (!a || a !== b) return false;
  var pa = String(prev.platform || ''), pb = String(item.platform || '');
  return !pa || !pb || pa === pb;
}
/* The instance id `prev` (its last presence item) came back as, or null. Only an unambiguous match counts: exactly one
   other live recorder on the same machine, and it connected no earlier than `prev` did (a restart, not a twin that was
   already there). Two same-named computers, or none, mean "do not guess". */
function recFindRestarted(prev, items) {
  if (!prev) return null;
  var same = (items || []).filter(function (it) { return it && it.instance_id && it.instance_id !== prev.instance_id && recSameMachine(prev, it); });
  if (same.length !== 1) return null;
  return Number(same[0].connected_at || 0) >= Number(prev.connected_at || 0) ? same[0].instance_id : null;
}
/* Called after every recorder frame: the open panel follows its recorder going away and coming back. */
function recPanelOnFrame() {
  var P = recPanel;
  if (!P) return;
  var entry = recs.get(P.id);
  if (entry) { P.last = entry.item; if (entry.item.device) P.device = entry.item.device; }
  if (!entry) {
    var next = recFindRestarted(P.last, Array.from(recs.values()).map(function (e) { return e.item; }));
    if (next) {
      // Same computer, new instance: rebind, drop the offline state, and load its list.
      P.id = next; P.last = recs.get(next).item; P.offline = false; P.offlineBy = ''; P.error = null; P.sel.clear();
      P.device = P.last.device || P.device;
      clearTimeout(P.timer);
      recPanelRender(); recPanelLoad();
      return;
    }
    if (!P.offline || P.offlineBy !== 'ws') { P.offline = true; P.offlineBy = 'ws'; clearTimeout(P.timer); recPanelRender(); }
  } else if (P.offline && P.offlineBy === 'ws') {
    P.offline = false; P.offlineBy = '';
    recPanelRender(); recPanelLoad();
  }
}
function recPanelInit() {
  var ui = recPanelUiInit();
  if (!ui.dlg) return;
  ui.close.addEventListener('click', function () { ui.dlg.close(); });
  ui.dlg.addEventListener('close', recPanelClosed);
  ui.dlg.addEventListener('mousedown', function (e) { if (recPanel) recPanel.downOnBackdrop = e.target === ui.dlg; });
  ui.dlg.addEventListener('click', function (e) { if (e.target === ui.dlg && recPanel && recPanel.downOnBackdrop) ui.dlg.close(); });
  ui.refresh.addEventListener('click', function () { if (recPanel && !recPanel.inflight) { recPanel.noticeText = ''; recPanelLoad(); } });
  ui.retry.addEventListener('click', function () { recPanelLoad(); });
  ui.search.addEventListener('input', function () { if (recPanel) { recPanel.q = ui.search.value; recPanelRender(); } });
  ui.filter.addEventListener('change', function () { if (recPanel) { recPanel.f = ui.filter.value; recPanelRender(); } });
  ui.all.addEventListener('change', function () {
    var P = recPanel;
    if (!P) return;
    recFilterRows(P.rows, P.q, P.f).forEach(function (r) { if (ui.all.checked) P.sel.add(r.session_id); else P.sel.delete(r.session_id); });
    recPanelRender();
  });
  ui.list.addEventListener('change', function (e) {
    var P = recPanel, li = e.target.closest && e.target.closest('.rp-row');
    if (!P || !li || !e.target.matches('[data-r="check"]')) return;
    if (e.target.checked) P.sel.add(li.dataset.id); else P.sel.delete(li.dataset.id);
    recPanelRender();
  });
  ui.list.addEventListener('click', function (e) {
    var b = e.target.closest('[data-rp]'), li = b && b.closest('.rp-row');
    if (!b || !li || b.disabled) return;
    var row = recPanelRowById(li.dataset.id);
    if (!row) return;
    if (b.dataset.rp === 'reupload') recPanelReupload([row]);
    else if (b.dataset.rp === 'delete') recPanelDelete([row]);
  });
  function chosen() { var P = recPanel; return P ? P.rows.filter(function (r) { return P.sel.has(r.session_id); }) : []; }
  ui.bulkRe.addEventListener('click', function () { recPanelReupload(chosen()); });
  ui.bulkDel.addEventListener('click', function () { recPanelDelete(chosen()); });
  ui.bulkClear.addEventListener('click', function () { if (recPanel) { recPanel.sel.clear(); recPanelRender(); } });
}

function recInit() {
  var grid = document.getElementById('rec-grid');
  recPanelInit();
  grid.addEventListener('click', recOnClick);
  grid.addEventListener('keydown', recOnKey);
  grid.addEventListener('input', function (event) {
    if (event.target.matches('[data-r="liveName"]')) event.target.closest('.rec-card')._nameDirty = true;
  });
  grid.addEventListener('focusout', function (event) {
    if (event.target.matches('[data-r="liveName"]')) recCommitName(event.target.closest('.rec-card'));
  });
  // The clock and countdowns tick locally between frames (no polling).
  setInterval(function () { if (recs.size) recRender(); }, 1000);
  // Keep telling the server this page is (not) visible; a quiet or hidden page lets recorders stop metering.
  document.addEventListener('visibilitychange', recSendWatch);
  setInterval(recSendWatch, 10000);
  fetch('/v1/recorders', {credentials: 'same-origin'}).then(function (r) {
    if (r.status === 401 || r.status === 403) { window.location = '/login'; throw new Error('Signed out'); }
    if (!r.ok) throw new Error('Unable to load');
    return r.json();
  }).then(function (data) {
    if (!recReady) { recApply({type: 'snapshot', items: data.items || []}); }
  }).catch(function () {});
  recConnect();
}
if (typeof document !== 'undefined' && document.getElementById && document.getElementById('rec-grid')) recInit();
"""


def render_recorders_page(*, token_configured: bool, appearance: str = "system") -> str:
    """Live recorders: one card per running Meeting Notes app, with remote control.

    Rendered client-side. First paint comes from ``GET /v1/recorders``; after that the page
    holds a websocket to ``/v1/recorders/events`` (frames ``snapshot`` / ``upsert`` / ``remove``
    / ``ping``) and patches cards in place, keyed by ``instance_id``. Buttons POST
    ``/v1/recorders/{instance_id}/commands`` (the protocol lives in ``meeting_notes/remote.py``).
    """
    body = (
        f"""
<div class="page recorders-page">
<header class="page-head">
  <h1>Recorders</h1>
  <span class="count" id="rec-count" aria-live="polite"></span>
  <div class="head-tools"><span class="rec-conn" id="rec-conn" role="status" hidden>{_icon("refresh")}<span>Reconnecting...</span></span></div>
</header>
<p class="rec-loading" id="rec-loading">Loading recorders...</p>
<div class="rec-grid" id="rec-grid" aria-busy="true" hidden></div>
<div class="empty-teach rec-empty" id="rec-empty" hidden>
  <h2>No recorders are running</h2>
  <p>Open Meeting Notes on a computer and it appears here.</p>
  <p class="help">A computer that is asleep, offline or has the app closed is not listed. Once it is running you can see what it is recording and control it from this page.</p>
</div>
</div>
<dialog class="dialog rec-panel" id="rec-panel" aria-labelledby="rp-title">
  <div class="rp-head">
    <div class="rp-titles">
      <h2 class="rp-title" id="rp-title">Recordings</h2>
      <p class="rp-summary" id="rp-summary" role="status" aria-live="polite"></p>
    </div>
    <button type="button" class="btn ghost icon-only" id="rp-close" aria-label="Close recordings">{_icon("x")}</button>
  </div>
  <div class="rp-tools">
    <label class="rp-search"><span class="sr-only">Search recordings</span><input type="text" id="rp-search" autocomplete="off" spellcheck="false" placeholder="Search by name or date"></label>
    <label class="rp-filter"><span class="sr-only">Filter by status</span><select id="rp-filter">
      <option value="all">All statuses</option>
      <option value="uploaded">Uploaded</option>
      <option value="moving">Uploading or waiting</option>
      <option value="missing">Not on server</option>
      <option value="failed">Failed or invalid</option>
    </select></label>
    <button type="button" class="btn secondary" id="rp-refresh">{_icon("refresh")}<span>Refresh</span></button>
  </div>
  <div class="rp-pick"><label><input type="checkbox" id="rp-all"><span>Select all visible</span></label><span class="rp-shown" id="rp-shown"></span></div>
  <div class="rp-banners">
    <div class="banner rp-banner" id="rp-alert" role="status" hidden><span id="rp-alert-ic"></span><span id="rp-alert-text"></span></div>
    <div class="banner rp-banner" id="rp-notice" role="status" hidden><span id="rp-notice-ic"></span><span id="rp-notice-text"></span></div>
    <p class="rp-note" id="rp-note" hidden></p>
  </div>
  <div class="rp-body" id="rp-body" aria-busy="true">
    <ul class="rp-list" id="rp-list" role="list" hidden></ul>
    <div class="rp-skel" id="rp-skel" aria-hidden="true"><i></i><i></i><i></i><i></i></div>
    <div class="rp-state" id="rp-state" hidden>
      <p class="rp-state-title" id="rp-state-title"></p>
      <p class="rp-state-text" id="rp-state-text" hidden></p>
      <button type="button" class="btn secondary" id="rp-retry" hidden>Retry</button>
    </div>
  </div>
  <div class="rp-bulk" id="rp-bulk" hidden>
    <span class="rp-selcount" id="rp-selcount"></span>
    <button type="button" class="btn secondary" id="rp-bulk-reupload">{_icon("refresh")}<span>Re-upload</span></button>
    <button type="button" class="btn danger" id="rp-bulk-delete">{_icon("trash")}<span>Delete from this computer</span></button>
    <button type="button" class="btn ghost" id="rp-bulk-clear">Clear</button>
  </div>
</dialog>
{_CONFIRM_DIALOG_HTML}
<script>
"""
        + _JS_HELPERS
        + _RECORDERS_JS
        + """
</script>
"""
    )
    return _shell(
        "Recorders",
        body,
        token_configured=token_configured,
        active="recorders",
        main_class="recorders-page-main",
        appearance=appearance,
    )


def render_meeting_notes_page(*, token_configured: bool, appearance: str = "system") -> str:
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
<footer class="pager"><button id="notes-more" class="btn secondary" style="display:none">Load more</button></footer>

<div class="overlay" id="notes-overlay" role="dialog" aria-modal="true" aria-label="Meeting notes">
  <div class="overlay-inner">
    <div class="overlay-head"><button class="btn secondary" id="notes-close">Back</button><div class="title"><div class="help" id="notes-meta"></div><h1 id="notes-title">Meeting notes</h1></div><button class="btn secondary" id="notes-retry">Regenerate notes</button></div>
    <div id="notes-state" class="help" role="status"></div>
    <section class="card notes-hero"><div class="notes-toolbar"><div style="flex:1"><h2 id="notes-hero-title">Meeting summary</h2><div class="help" id="notes-hero-meta" style="margin:0"></div></div><button class="btn secondary" id="notes-download">Download .md</button></div></section>
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
  function sectionMarkup(section, empty){var title=section[0], vals=values(section[1]);if(!vals.length)return '<section class="notes-section notes-empty"><h2>'+text(title)+'</h2><p>Nothing recorded yet.</p></section>';var prose=title==='Summary'||title==='Meeting notes',body=title==='Action items'?actionMarkup(section[1]):(prose?mdBlock(vals.join('\\n\\n')):(vals.length===1?'<p>'+mdInline(vals[0])+'</p>':'<ul>'+vals.map(function(v){return '<li>'+mdInline(v)+'</li>';}).join('')+'</ul>'));return '<section class="notes-section"><h2>'+text(title)+'</h2>'+body+'</section>';}
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
  document.getElementById('notes-summary').innerHTML=mdBlock(n.summary||n.overview||'No summary was generated.');
  var narrative=n.polished_meeting_notes||n.polished_notes||n.meeting_notes||n.narrative||n.notes;document.getElementById('notes-narrative').innerHTML=narrative?mdBlock(narrative):'<p>No detailed meeting notes were generated.</p>';
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
    return _shell(
        "Notes", body, token_configured=token_configured, active="meeting-notes", appearance=appearance
    )


def render_install_page(
    server_address: str, *, token_configured: bool, appearance: str = "system"
) -> str:
    """Human-readable Windows and macOS installation and first-run guide."""
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
  <div class="copy-row"><button type="button" class="btn secondary" data-copy-target="cmd-oneline">{copy_icon}<span>Copy command</span></button></div>
  <ol>
    <li><a class="btn primary" href="/install/client-agent.ps1" download>Download installer</a></li>
    <li>Open PowerShell normally. Administrator mode is not required.</li>
    <li>Run the downloaded script:</li>
  </ol>
  <pre class="command" id="cmd-file" tabindex="0">powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$env:USERPROFILE\Downloads\Install-MeetingNotes.ps1"</pre>
  <div class="copy-row"><button type="button" class="btn secondary" data-copy-target="cmd-file">{copy_icon}<span>Copy command</span></button></div>
  <p class="help">If your browser renamed the file, use its actual filename. Re-running
  the installer upgrades the application and preserves your existing server token,
  recording folder, and client settings.</p>
</section>
<section id="macos">
  <h2>macOS</h2>
  <p>For Apple silicon Macs running macOS 13 or newer. Open <strong>Terminal</strong> (no administrator password is needed) and run:</p>
  <pre class="command" id="cmd-mac" tabindex="0">curl -fsSL {address}/install/mac.sh | bash</pre>
  <div class="copy-row"><button type="button" class="btn secondary" data-copy-target="cmd-mac">{copy_icon}<span>Copy command</span></button></div>
  <p class="help">The script verifies the download against this server, installs
  <strong>Meeting Notes</strong> into <code>~/Applications</code>, keeps your existing settings and
  recordings, points the app at <strong>{address}</strong>, and opens it. Run it again any time to update.
  No BlackHole or other audio driver is needed: system audio is captured with macOS itself.</p>
  <p><strong>First run on a Mac</strong></p>
  <ol>
    <li>When macOS asks, allow <strong>Microphone</strong> access.</li>
    <li>Start a recording once. macOS will ask for <strong>Screen &amp; System Audio Recording</strong>: open
    <em>System Settings &rarr; Privacy &amp; Security &rarr; Screen &amp; System Audio Recording</em>, switch
    <strong>Meeting Notes</strong> on, then quit and reopen the app. Only audio is captured, never the screen.</li>
    <li>Open <strong>Settings</strong> in the app and enter the same server token used to sign in here.</li>
    <li>In <em>Privacy &amp; Security &rarr; Microphone</em> and <em>Screen &amp; System Audio Recording</em>, confirm Meeting Notes is switched on if a track stays silent.</li>
  </ol>
  <p class="help">To remove it, drag <code>~/Applications/Meeting Notes.app</code> to the Trash.
  Recordings in <code>~/Meeting Notes</code> and settings in <code>~/.meeting-notes</code> are not touched.</p>
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
    return _shell(
        "Install", body, token_configured=token_configured, active="install", appearance=appearance
    )


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

# The in-app updater starts this script with the app folder as its working
# directory, and Windows refuses to rename a folder any process (this one
# included) is "in". Step out of it before anything else.
$neutralDir = [IO.Path]::GetTempPath()
Set-Location -LiteralPath $neutralDir
[Environment]::CurrentDirectory = $neutralDir

__RECORDINGS_GUARD__
$recordingsDir = Assert-RecordingsAreSafe
$stoppedApp = $false

function Restart-PreviousApp {
    # The update failed after the running app was closed: bring it back so the
    # window never just disappears.
    $oldExe = Join-Path $installDir "MeetingNotes.exe"
    if ($stoppedApp -and (Test-Path -LiteralPath $oldExe)) {
        try { Start-Process -FilePath $oldExe -WorkingDirectory $installDir | Out-Null } catch { }
    }
}

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
                $script:stoppedApp = $true
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
        # A just-stopped process or an antivirus scan can hold a handle for a
        # moment; retry briefly before giving up.
        $renamed = $false
        for ($attempt = 1; $attempt -le 10 -and -not $renamed; $attempt++) {
            try {
                Rename-Item -LiteralPath $installDir -NewName (Split-Path -Leaf $previous)
                $renamed = $true
            } catch {
                Start-Sleep -Milliseconds 500
            }
        }
        if (-not $renamed) {
            Restart-PreviousApp
            throw "Meeting Notes could not be updated because a file in $installDir is in use. Close any program using it and run the installer again. The installed version was left unchanged."
        }
    }
    try {
        Move-Item -LiteralPath $staging -Destination $installDir
    } catch {
        if (Test-Path -LiteralPath $previous) { Rename-Item -LiteralPath $previous -NewName (Split-Path -Leaf $installDir) }
        Restart-PreviousApp
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


def render_mac_installer(server_address: str) -> str:
    """The configured macOS installer script (``curl ... /install/mac.sh | bash``)."""
    from meeting_notes.server import mac_installer

    return mac_installer.render_mac_installer(server_address)


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

# Windows cannot delete a folder a process is "in"; step out of the app folder.
$neutralDir = [IO.Path]::GetTempPath()
Set-Location -LiteralPath $neutralDir
[Environment]::CurrentDirectory = $neutralDir

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
def render_sessions_page(*, token_configured: bool, appearance: str = "system") -> str:
    return render_transcriptions_page(token_configured=token_configured, appearance=appearance)


def render_session_detail_page(
    session_id: str, *, token_configured: bool, appearance: str = "system"
) -> str:
    return render_transcriptions_page(
        token_configured=token_configured, initial_session_id=session_id, appearance=appearance
    )


# -- settings ---------------------------------------------------------------


# Settings sections that act immediately through the API (not through the form's
# Save button), so they live outside the <form>: AI access keys and client logs.
_SETTINGS_IMMEDIATE_HTML = r"""
<div class="settings-sheet immediate">
  <section class="sect" aria-labelledby="settings-notion-heading">
    <h2 id="settings-notion-heading">Notion</h2>
    <div class="sect-body">
    <p class="help">Copy finished meeting notes into Notion. Create an internal integration at
    notion.so/profile/integrations, then paste its token here (or set NOTION_TOKEN on the server). Connecting,
    testing and removing the token take effect immediately; they do not wait for Save settings.</p>
    <p class="notion-status" id="notion-status" role="status">Loading...</p>
    <div id="notion-token-area">
      <label class="field">
        <span class="name">Integration token</span>
        <input type="password" id="notion-token" autocomplete="off" spellcheck="false" placeholder="Paste the integration token">
      </label>
      <div class="inline-actions notion-actions">
        <button type="button" class="btn secondary" id="notion-connect">Connect</button>
        <button type="button" class="btn secondary" id="notion-test">Test connection</button>
        <button type="button" class="btn danger" id="notion-remove" hidden>Remove</button>
      </div>
    </div>
    <p class="error-text" id="notion-error" role="alert" hidden></p>
    <p class="help">For each parent page you set on a note type (under Meeting notes AI): open the page in Notion,
    choose the ••• menu, then Connections, and add the integration. Otherwise Notion reports the page as not
    found. Monthly pages are created under the parent as "&lt;Month&gt;-&lt;YYYY&gt; &lt;note type name&gt;", with the
    newest meeting at the top.</p>
    </div>
  </section>
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
        <div class="copy-row"><button type="button" class="btn secondary" data-copy-target="reveal-key">__ICON_COPY__<span>Copy key</span></button></div>
        <div class="field-name">Add it to Claude Code</div>
        <pre class="command" id="reveal-cmd" tabindex="0"></pre>
        <div class="copy-row"><button type="button" class="btn secondary" data-copy-target="reveal-cmd">__ICON_COPY__<span>Copy command</span></button></div>
        <div class="field-name">For other agents (no key needed to read these)</div>
        <ul class="reveal-links">
          <li><a href="/api/v1/manifest" target="_blank" rel="noopener">/api/v1/manifest</a> machine-readable description</li>
          <li><a href="/llms.txt" target="_blank" rel="noopener">/llms.txt</a> short guide for language models</li>
          <li><a href="/api-docs.md" target="_blank" rel="noopener">/api-docs.md</a> full reference</li>
        </ul>
        <button type="button" class="btn secondary" id="key-reveal-done">I have saved the key</button>
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
        <p><button type="submit" class="btn secondary" id="key-create">__ICON_KEY__<span>Create key</span></button></p>
      </form>
    </div>
    </div>
  </section>
  <section class="sect" aria-labelledby="settings-recorders-heading">
    <h2 id="settings-recorders-heading">Recorders</h2>
    <div class="sect-body">
    <p class="help">Running recorders appear on the Recorders page while they are open, where you can see what they
    are doing and control them.</p>
    <p><a class="btn secondary" href="/recorders">__ICON_RADIO__<span>Open Recorders</span></a></p>
    </div>
  </section>
  <section class="sect" aria-labelledby="settings-logs-heading">
    <h2 id="settings-logs-heading">Client logs</h2>
    <div class="sect-body">
    <p class="help">Diagnostic bundles sent from the Windows app (Logs, then Send to server). The app redacts the
    sign-in token before sending. The newest 20 bundles per computer are kept.</p>
    <div id="logs-box" aria-live="polite"><p class="help" role="status">Loading logs...</p></div>
    <div class="inline-actions"><button type="button" class="btn secondary" id="logs-refresh">__ICON_REFRESH__<span>Refresh</span></button></div>
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
    return '<p class="error-text" role="alert">' + escapeHtml(message) + '</p><p><button type="button" class="btn secondary" ' + retryAttr + '>Try again</button></p>';
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
      var status = revoked ? badge('none', 'Revoked') : badge('done', 'Active');
      var action = revoked ? '' : '<button type="button" class="btn danger sm row-btn" data-revoke="' + escapeHtml(k.id) + '" data-name="' + escapeHtml(k.name) + '">Revoke<span class="sr-only"> ' + escapeHtml(k.name) + '</span></button>';
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
        '<td class="l-act"><a class="btn secondary sm row-btn" href="' + escapeHtml(item.url) + '" download>' + icon('download', 16) + '<span>Download<span class="sr-only"> log bundle from ' + escapeHtml(item.device) + ', ' + escapeHtml(when) + '</span></span></a></td></tr>';
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

  // ---- Notion ----
  var nStatus = el('notion-status'), nError = el('notion-error'), nToken = el('notion-token');
  var nBtns = ['notion-connect', 'notion-test', 'notion-remove'].map(el);
  function nFail(message) { nError.textContent = message; nError.hidden = false; }
  function nBusy(on) { nBtns.forEach(function (b) { b.disabled = on; }); }
  function renderNotion(d) {
    nError.hidden = true;
    var env = d.source === 'env';
    if (!d.connected) nStatus.textContent = 'Not connected';
    else if (env) nStatus.textContent = 'Using NOTION_TOKEN from the server environment' + (d.workspace_name ? ' (' + d.workspace_name + ')' : '');
    else nStatus.textContent = 'Connected as ' + (d.bot_name || 'your integration') + (d.workspace_name ? ' (' + d.workspace_name + ')' : '');
    nToken.disabled = env; nToken.value = '';
    el('notion-connect').hidden = env;
    nToken.parentNode.hidden = env;
    el('notion-remove').hidden = env || !d.connected;
    el('notion-test').hidden = !d.connected;
    nBusy(false);
  }
  function loadNotion() {
    return api('GET', '/v1/notion').then(renderNotion).catch(function (e) { nStatus.textContent = 'Could not load Notion status.'; nFail(e.message); });
  }
  el('notion-connect').addEventListener('click', function () {
    var token = nToken.value.trim();
    if (!token) { nFail('Paste the integration token first.'); nToken.focus(); return; }
    nError.hidden = true; nBusy(true);
    api('PUT', '/v1/notion/token', {token: token})
      .then(function (d) { renderNotion(d); notify('Notion connected.'); })
      .catch(function (e) { nBusy(false); nFail(e.message); });
  });
  el('notion-test').addEventListener('click', function () {
    nError.hidden = true; nBusy(true);
    api('POST', '/v1/notion/test').then(function (d) {
      nBusy(false);
      if (d.ok) notify('Connected as ' + (d.name || 'your integration') + (d.workspace ? ' (' + d.workspace + ')' : '') + '.');
      else nFail(d.error || 'Notion rejected the connection.');
    }).catch(function (e) { nBusy(false); nFail(e.message); });
  });
  el('notion-remove').addEventListener('click', function () {
    if (!confirm('Remove the saved Notion token?\n\nMeeting notes stop being copied to Notion until you connect again. Pages already created stay in Notion.')) return;
    nError.hidden = true; nBusy(true);
    api('DELETE', '/v1/notion/token').then(function (d) { renderNotion(d); notify('Notion token removed.'); })
      .catch(function (e) { nBusy(false); nFail(e.message); });
  });
  nToken.addEventListener('input', function () { nError.hidden = true; });
  loadNotion();

  loadKeys();
  loadLogs();
})();
"""


_NOTE_STYLES_JS = r"""
(function () {
  var list = document.getElementById("style-list");
  var json = document.getElementById("note-templates-json");
  var def = document.getElementById("default-template-id");
  var form = list.closest("form");
  var addBtn = document.getElementById("style-add");
  var tpl = document.getElementById("style-tpl");
  var errBox = document.getElementById("style-error");
  function userStyles() { return Array.prototype.slice.call(list.querySelectorAll(".style:not([data-standard])")); }
  function allStyles() { return Array.prototype.slice.call(list.querySelectorAll(".style")); }
  function nameOf(el) { return (el.querySelector(".style-name-input").value || "").trim(); }
  function refresh() {
    var current = def.value;
    def.innerHTML = "";
    allStyles().forEach(function (el) {
      var o = document.createElement("option");
      o.value = el.dataset.id;
      o.textContent = nameOf(el) || "Untitled note type";
      def.appendChild(o);
      el.querySelector(".style-title").textContent = nameOf(el) || "Untitled note type";
    });
    if (Array.prototype.some.call(def.options, function (o) { return o.value === current; })) def.value = current;
    else def.value = "standard";
    allStyles().forEach(function (el) { el.querySelector(".style-default").hidden = el.dataset.id !== def.value; });
  }
  var parentInfo = {};
  function renderDest(el) {
    var nIn = el.querySelector(".style-notion-input"), sum = el.querySelector(".style-sum"), link = el.querySelector(".style-notion-link");
    if (!nIn || !sum) return;
    var v = nIn.value.trim(), saved = (nIn.dataset.saved || "").trim(), info = parentInfo[el.dataset.id];
    var known = v && v === saved && info;
    sum.textContent = !v ? "· Not saved to Notion" : (known && info.title ? "· Notion: " + info.title : "· Notion page set");
    link.textContent = "";
    if (!v) { link.textContent = "Not saved to Notion"; return; }
    if (v !== saved) { link.textContent = "Save settings to check this page."; return; }
    var a = document.createElement("a");
    a.href = info ? info.url : "https://www.notion.so/" + v.replace(/-/g, "");
    a.target = "_blank"; a.rel = "noopener";
    a.textContent = (info && info.title) || "Open Notion page";
    link.appendChild(document.createTextNode("Saves to "));
    link.appendChild(a);
  }
  function loadParents() {
    fetch("/v1/notion/parents", {credentials: "same-origin"}).then(function (r) { return r.ok ? r.json() : {}; })
      .then(function (d) { parentInfo = (d && d.items) || {}; allStyles().forEach(renderDest); }).catch(function () {});
  }
  function randomId() {
    var a = new Uint8Array(6), out = "t";
    (window.crypto || window.msCrypto).getRandomValues(a);
    for (var i = 0; i < a.length; i++) out += ("0" + a[i].toString(16)).slice(-2);
    return out;
  }
  function wire(el) {
    var nameInput = el.querySelector(".style-name-input"), resetBtn = el.querySelector(".style-name-reset");
    function syncReset() { if (resetBtn) resetBtn.hidden = nameInput.value.trim() === resetBtn.dataset["default"]; }
    nameInput.addEventListener("input", function () { this.removeAttribute("aria-invalid"); syncReset(); refresh(); });
    if (resetBtn) resetBtn.addEventListener("click", function () { nameInput.value = resetBtn.dataset["default"]; nameInput.removeAttribute("aria-invalid"); syncReset(); refresh(); });
    var del = el.querySelector(".style-delete");
    if (del) del.addEventListener("click", function () { el.remove(); refresh(); errBox.hidden = true; });
    var nIn = el.querySelector(".style-notion-input");
    var nBtn = el.querySelector(".style-notion-backfill"), nMsg = el.querySelector(".style-notion-msg");
    if (nIn) nIn.addEventListener("input", function () { renderDest(el); });
    renderDest(el);
    if (nBtn) nBtn.addEventListener("click", function () {
      var id = el.dataset.id;
      function say(m) { nMsg.textContent = m; }
      if ((nIn.value || "").trim() !== (nIn.dataset.saved || "").trim()) { say("Save settings first."); return; }
      if (!nIn.value.trim()) { say("Set a Notion parent page and save settings first."); return; }
      nBtn.disabled = true; say("Checking...");
      function readErr(r) { return r.json().catch(function () { return {}; }).then(function (d) { throw new Error((d && d.detail) || ("Something went wrong (HTTP " + r.status + ").")); }); }
      fetch("/v1/notion/backfill?template=" + encodeURIComponent(id), {credentials: "same-origin"})
        .then(function (r) { return r.ok ? r.json() : readErr(r); })
        .then(function (d) {
          if (!d.count) { say("Nothing to copy: every meeting with notes of this note type is already in Notion."); return null; }
          var styleName = (d.template && d.template.name) || nameOf(el) || "this note type";
          if (!confirm("Copy " + d.count + " existing meeting" + (d.count === 1 ? "" : "s") + " with the " + styleName + " note type to Notion?\n\nThey are added to the monthly pages in order, newest at the top. This runs in the background.")) { say(""); return null; }
          return fetch("/v1/notion/backfill", {method: "POST", credentials: "same-origin", headers: {"Content-Type": "application/json"}, body: JSON.stringify({template: id})})
            .then(function (r) { return r.ok ? r.json() : readErr(r); })
            .then(function (q) { say("Queued " + q.queued + " meeting" + (q.queued === 1 ? "" : "s") + ". They will appear in Notion over the next few minutes."); });
        })
        .catch(function (e) { say(e.message); })
        .finally(function () { nBtn.disabled = false; });
    });
  }
  allStyles().forEach(wire);
  def.addEventListener("change", refresh);
  addBtn.addEventListener("click", function () {
    var node = tpl.content.firstElementChild.cloneNode(true);
    node.dataset.id = randomId();
    node.querySelector(".style-prompt-input").value = document.querySelector('textarea[name="ai_workflow"]').value;
    list.appendChild(node);
    wire(node);
    node.open = true;
    refresh();
    var input = node.querySelector(".style-name-input");
    input.focus();
    if (node.scrollIntoView) node.scrollIntoView({block: "nearest"});
  });
  function fail(el, field, message) {
    el.open = true;
    field.setAttribute("aria-invalid", "true");
    errBox.textContent = message;
    errBox.hidden = false;
    field.focus();
  }
  form.addEventListener("submit", function (event) {
    var parents = {}, hidden = document.getElementById("notion-parents-json");
    allStyles().forEach(function (st) { var f = st.querySelector(".style-notion-input"); parents[st.dataset.id] = f ? f.value.trim() : ""; });
    if (hidden) hidden.value = JSON.stringify(parents);
    errBox.hidden = true;
    var seen = {}, out = [];
    var styles = allStyles();
    for (var i = 0; i < styles.length; i++) {
      var el = styles[i], id = el.dataset.id;
      var nameField = el.querySelector(".style-name-input"), promptField = el.querySelector(".style-prompt-input");
      var name = nameOf(el);
      if (!name) { event.preventDefault(); return fail(el, nameField, "Give every note type a name."); }
      if (seen[name.toLowerCase()]) { event.preventDefault(); return fail(el, nameField, "Note type names must be unique: " + name); }
      seen[name.toLowerCase()] = 1;
      if (el.hasAttribute("data-standard")) continue;
      if (!promptField.value.trim()) { event.preventDefault(); return fail(el, promptField, "The summary instructions for " + (name || "this note type") + " can't be empty."); }
      out.push({id: id, name: name, prompt: promptField.value});
    }
    if (!document.querySelector('textarea[name="ai_workflow"]').value.trim()) {
      event.preventDefault();
      return fail(list.querySelector("[data-standard]"), document.querySelector('textarea[name="ai_workflow"]'), "The Standard summary instructions can't be empty.");
    }
    json.value = JSON.stringify(out);
  });
  refresh();
  loadParents();
})();
"""


def _note_style_item(template: dict, *, standard: bool, builtin: bool, default_id: str, notion_parent: str = "") -> str:
    tid = html.escape(template["id"])
    name = html.escape(template["name"])
    prompt = html.escape(template["prompt"])
    attrs = f' data-id="{tid}"'
    if standard:
        attrs += " data-standard"
    if builtin:
        attrs += " data-builtin"
    prompt_name = ' name="ai_workflow"' if standard else ""
    name_attr = ' name="standard_name"' if standard else ""
    default_name = settings_mod.BUILTIN_TEMPLATES.get(template["id"], ("", ""))[0]
    default_hidden = "" if template["id"] == default_id else " hidden"
    badge = '<span class="style-badge">Built-in</span>' if builtin else ""
    footer = (
        '<a class="btn secondary sm" href="/v1/bridge/workflow.md" download>Download prompt</a>'
        if standard else (
            "" if builtin else
            f'<button type="button" class="btn danger sm style-delete">{_icon("trash", 14)}<span>Delete note type</span></button>'
        )
    )
    reset_hidden = "" if template["name"] != default_name else " hidden"
    name_help = (
        f'<span class="help" style="margin:0"><button type="button" class="btn secondary sm style-name-reset"'
        f' data-default="{html.escape(default_name, quote=True)}"{reset_hidden}>Reset name</button></span>'
        if builtin else ""
    )
    notion_value = html.escape(notion_parent or "", quote=True)
    if notion_parent:
        notion_sum = "· Notion page set"
        notion_link = (f'Saves to <a href="https://www.notion.so/{html.escape(notion_parent.replace("-", ""), quote=True)}"'
                       f' target="_blank" rel="noopener">Open Notion page</a>')
    else:
        notion_sum = "· Not saved to Notion"
        notion_link = "Not saved to Notion"
    return f"""<details class="style"{attrs}>
  <summary><span class="style-title">{name}</span>{badge}<span class="style-badge style-default"{default_hidden}>Default</span><span class="style-sum">{notion_sum}</span></summary>
  <div class="style-body">
    <label class="field"><span class="name">Name</span>
      <input type="text" class="style-name-input"{name_attr} maxlength="{settings_mod.MAX_TEMPLATE_NAME_CHARS}" value="{name}" autocomplete="off" placeholder="e.g. Customer call">
      {name_help}</label>
    <div class="style-part">
    <label class="field"><span class="name">Summary instructions</span>
      <span class="help" style="margin:0">What the AI writes for this type of meeting.</span>
      <textarea{prompt_name} class="style-prompt-input" rows="16">{prompt}</textarea></label>
    </div>
    <div class="style-notion style-part">
      <label class="field"><span class="name">Save to Notion</span>
        <span class="help" style="margin:0">Notes of this type go into month pages under this Notion page.</span>
        <input type="text" class="style-notion-input" value="{notion_value}" data-saved="{notion_value}" autocomplete="off" spellcheck="false" placeholder="Paste a Notion page link or id">
        <span class="help" style="margin:0">Leave empty to keep this note type out of Notion. Share the page with the integration first (page ••• menu, then Connections).</span></label>
      <p class="style-notion-link" role="status">{notion_link}</p>
      <div class="inline-actions"><button type="button" class="btn secondary sm style-notion-backfill">Copy existing notes</button><span class="help style-notion-msg" role="status"></span></div>
    </div>
    <div class="inline-actions">{footer}</div>
  </div>
</details>"""


def _note_styles_html(settings) -> str:
    default_id = settings.default_template()["id"]
    items = [
        _note_style_item(t, standard=t["id"] == settings_mod.STANDARD_TEMPLATE_ID, builtin=t["builtin"], default_id=default_id,
            notion_parent=(getattr(settings, "notion_parents", None) or {}).get(t["id"], ""))
        for t in settings.all_templates()
    ]
    blank = _note_style_item(
        {"id": "new", "name": "", "prompt": ""}, standard=False, builtin=False, default_id=default_id
    )
    options = "".join(
        f'<option value="{html.escape(t["id"])}"{" selected" if t["id"] == default_id else ""}>{html.escape(t["name"])}</option>'
        for t in settings.all_templates()
    )
    items_html = "".join(items)
    stored = html.escape(json.dumps(settings.note_templates, ensure_ascii=False), quote=True)
    notion_auto_checked = " checked" if getattr(settings, "notion_auto_copy", False) else ""
    return f"""
    <label class="checkbox" style="margin-top:16px">
      <input type="checkbox" name="notion_auto_copy" value="on"{notion_auto_checked}>
      <span>Copy notes to Notion automatically</span>
    </label>
    <p class="help">When notes finish, copy them to their note type's Notion page. Re-generated notes update the
    existing copy. Note types with no Notion parent page are never copied.</p>
    <input type="hidden" name="notion_parents" id="notion-parents-json" value="">
    <label class="field" style="margin-top:16px">
      <span class="name">Default note type</span>
      <select name="default_template_id" id="default-template-id">{options}</select>
    </label>
    <p class="help">Used when notes are built automatically, by the Generate button on the
    meetings list, and whenever a meeting is generated without picking a note type. You can choose a
    different note type for any single meeting from the meeting view.</p>

    <div class="styles" id="note-styles">
      <div class="styles-head">
        <div><h3 class="styles-title">Note types</h3>
        <p class="help" style="margin:2px 0 0">A note type sets how the summary is written and where it's saved in Notion. Note types do not rename the saved meeting. Changes apply when you save.</p></div>
        <button type="button" class="btn secondary sm" id="style-add">{_icon("sparkles", 14)}<span>Add note type</span></button>
      </div>
      <input type="hidden" name="note_templates" id="note-templates-json" value="{stored}">
      <div class="banner err" id="style-error" role="alert" hidden></div>
      <div id="style-list">{items_html}</div>
      <template id="style-tpl">{blank}</template>
    </div>"""


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
        .replace("__ICON_RADIO__", _icon("radio"))
        .replace("__ICON_REFRESH__", _icon("refresh"))
    )
    note_styles_html = _note_styles_html(settings)
    immediate_js = _SETTINGS_IMMEDIATE_JS.replace(
        "__SERVER_ADDRESS_JSON__", json.dumps(settings.server_address.strip().rstrip("/")).replace("</", "<\\/")
    )

    body = f"""
<div class="page-head"><h1>Settings</h1></div>
{message_html}
{error_html}
<div class="settings-layout">
  <nav class="settings-nav" aria-label="Settings sections"><a href="#settings-appearance-heading">Appearance</a><a href="#settings-install-heading">Installation</a><a href="#settings-transcription-heading">Transcription</a><a href="#settings-ai-heading">Meeting notes AI</a><a href="#settings-retention-heading">Audio retention</a><a href="#settings-index-heading">Search index</a><a href="#settings-notion-heading">Notion</a><a href="#settings-agents-heading">AI access</a><a href="#settings-recorders-heading">Recorders</a><a href="#settings-logs-heading">Client logs</a></nav>
  <div class="settings-main">
  <form method="post" action="/settings" class="settings-sheet">
  <section class="sect" aria-labelledby="settings-appearance-heading">
    <h2 id="settings-appearance-heading">Appearance</h2>
    <div class="sect-body">
    {_appearance_control("appearance", _normalize_appearance(settings.appearance))}
    <p class="help">System follows your device's light or dark setting. The choice applies immediately and is
    saved for this server, so every browser shows the same theme.</p>
    </div>
  </section>

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
        <div class="inline-actions"><button type="button" class="btn secondary" id="codex-connect">Connect ChatGPT</button><button type="button" class="btn danger" id="codex-disconnect" style="display:none">Disconnect</button></div>
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
        <div class="inline-actions"><button type="button" class="btn secondary" id="claude-connect">Connect Claude</button><button type="button" class="btn danger" id="claude-disconnect" style="display:none">Disconnect</button></div>
      </div>
      <div id="claude-login-panel" style="display:none">
        <p>Open <a id="claude-login-url" href="#" target="_blank" rel="noopener">the Claude sign-in link</a>,
        approve access there, then paste the code shown back here.</p>
        <label class="field">
          <span class="name">Authorization code</span>
          <input type="text" id="claude-login-code-input" autocomplete="off">
        </label>
        <p><button type="button" class="btn secondary" id="claude-code-submit">Submit code</button></p>
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
      <div class="inline-actions"><button type="button" class="btn secondary" id="ollama-model-refresh">Load available models</button>
      <span class="help" id="ollama-model-status" role="status" style="margin:0"></span></div>
      <p class="help" style="margin-top:12px">The Ollama service must be reachable from the server or bridge container.</p>
    </div>

    {note_styles_html}
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
    <div class="inline-actions"><button type="button" class="btn secondary" id="reindex-btn">Rebuild index now</button>
    <span id="reindex-status" class="help" role="status" style="margin:0"></span></div>
    </div>
  </section>
  <div class="save-bar">
    <button type="submit" class="btn primary">Save settings</button><span class="help">Changes take effect after saving.</span>
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
{_NOTE_STYLES_JS}
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
    return _shell(
        "Settings",
        body,
        token_configured=token_configured,
        active="settings",
        main_class="settings-page",
        appearance=settings.appearance,
    )
