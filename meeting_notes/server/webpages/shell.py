"""The page shell shared by every page: icons, navigation, appearance, banners and the ``_shell`` wrapper."""

from __future__ import annotations

import contextvars
import hashlib
import html
from pathlib import Path
from typing import Optional

from meeting_notes import __version__
from meeting_notes.server import ai_health, updates as updates_mod


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
    "chev-up": '<path d="m18 15-6-6-6 6"/>',
    "chev-down": '<path d="m6 9 6 6 6-6"/>',
    "laptop": '<path d="M18 5a2 2 0 0 1 2 2v8.526a2 2 0 0 0 .212.897l1.068 2.127a1 1 0 0 1-.9 1.45H3.62a1 1 0 0 1-.9-1.45l1.068-2.127A2 2 0 0 0 4 15.526V7a2 2 0 0 1 2-2z"/><path d="M20.054 15.987H3.946"/>',
    "radio": '<path d="M4.9 19.1C1 15.2 1 8.8 4.9 4.9"/><path d="M7.8 16.2c-2.3-2.3-2.3-6.1 0-8.5"/><circle cx="12" cy="12" r="2"/><path d="M16.2 7.8c2.3 2.3 2.3 6.1 0 8.5"/><path d="M19.1 4.9C23 8.8 23 15.1 19.1 19"/>',
    "mic-off": '<path d="M12 19v3"/><path d="M15 9.34V5a3 3 0 0 0-5.68-1.33"/><path d="M16.95 16.95A7 7 0 0 1 5 12v-2"/><path d="M18.89 13.23A7 7 0 0 0 19 12v-2"/><path d="m2 2 20 20"/><path d="M9 9v3a3 3 0 0 0 5.12 2.12"/>',
    "speaker-off": '<path d="M11 4.702a.705.705 0 0 0-1.203-.498L6.413 7.587A1.4 1.4 0 0 1 5.416 8H3a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h2.416a1.4 1.4 0 0 1 .997.413l3.383 3.384A.705.705 0 0 0 11 19.298z"/><path d="m22 9-6 6"/><path d="m16 9 6 6"/>',
    "info": '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>',
    "users": '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
    "queue": '<path d="m12.83 2.18a2 2 0 0 0-1.66 0L2.6 6.08a1 1 0 0 0 0 1.83l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.9a1 1 0 0 0 0-1.83Z"/><path d="m22 17.65-9.17 4.16a2 2 0 0 1-1.66 0L2 17.65"/><path d="m22 12.65-9.17 4.16a2 2 0 0 1-1.66 0L2 12.65"/>',
    "play": '<polygon points="6 3 20 12 6 21 6 3"/>',
    "pause": '<rect x="14" y="4" width="4" height="16" rx="1"/><rect x="6" y="4" width="4" height="16" rx="1"/>',
    # The desktop client's own glyphs (client/ui/icons.py, same geometry); the Recorders page draws them at 1.6 stroke.
    "c-folder": '<path d="M3 7h6.5l2 2.5H21V19H3z"/>',
    "c-upload": '<path d="M12 16V4.5"/><path d="m7.5 9 4.5-4.5L16.5 9"/><path d="M4 15v4.5h16V15"/>',
    "c-history": '<circle cx="12" cy="12" r="8.5"/><path d="M12 7v5l3.5 2"/>',
    "c-settings": '<path d="M4 6.5h2M12 6.5h8M4 12h8.5M18.5 12H20M4 17.5h1M11 17.5h9"/><circle cx="9" cy="6.5" r="2.4"/><circle cx="15.5" cy="12" r="2.4"/><circle cx="8" cy="17.5" r="2.4"/>',
    "c-more": '<circle cx="5.5" cy="12" r="1.9" fill="currentColor" stroke="none"/><circle cx="12" cy="12" r="1.9" fill="currentColor" stroke="none"/><circle cx="18.5" cy="12" r="1.9" fill="currentColor" stroke="none"/>',
    "c-refresh": '<path d="M16.5 6.64A7 7 0 1 0 17.36 16.5"/><path d="m13.03 17.26 4.33-.76v4.4"/>',
    "c-logs": '<path d="M4 5.5h16M4 10.5h16M4 15.5h10M4 20.5h7"/>',
    "c-mic": '<rect x="9" y="3.5" width="6" height="11" rx="3"/><path d="M5.5 12a6.5 5.5 0 0 0 13 0"/><path d="M12 17.5V21M8.5 21h7"/>',
    # the client's notes-view lane icons (icons.py _mic_off, _speaker, _speaker_off)
    "c-mic-off": '<rect x="9" y="3.5" width="6" height="11" rx="3"/><path d="M5.5 12a6.5 5.5 0 0 0 13 0"/><path d="M12 17.5V21M8.5 21h7"/><path d="m4.5 4.5 15 15"/>',
    "c-speaker": '<path d="M4 9.5h4.5L13 5.5v13l-4.5-4H4z"/><path d="M16.57 8.94a4 4 0 0 1 0 6.12M19.5 6.64a7 7 0 0 1 0 10.72"/>',
    "c-speaker-off": '<path d="M4 9.5h4.5L13 5.5v13l-4.5-4H4z"/><path d="M16.57 8.94a4 4 0 0 1 0 6.12M19.5 6.64a7 7 0 0 1 0 10.72"/><path d="m4.5 4.5 15 15"/>',
    "c-stop": '<rect x="6" y="6" width="12" height="12" rx="1.8" fill="currentColor" stroke="none"/>',
    "c-alert": '<path d="M12 3.5 21.5 20h-19z"/><path d="M12 9.5v4.7M12 17.2v.1"/>',
    "c-alert-circle": '<circle cx="12" cy="12" r="9"/><path d="M12 7.5v5M12 16v.1"/>',
    "c-info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v5.5M12 7.7v.1"/>',
    "c-check-circle": '<circle cx="12" cy="12" r="9"/><path d="m8 12.3 3 3 5.2-6"/>',
}


def _icon(name: str, size: int = 16) -> str:
    return (
        f'<svg class="ic" viewBox="0 0 24 24" width="{size}" height="{size}" '
        f'aria-hidden="true" focusable="false">{_ICON_PATHS[name]}</svg>'
    )


# -- shared shell -------------------------------------------------------

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


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
    ("/", "Home", "home", "home"),
    ("/meetings", "Meetings", "transcriptions", "list"),
    ("/people", "People", "people", "users"),
    ("/queue", "Queue", "queue", "queue"),
    ("/recorders", "Recorders", "recorders", "radio"),
    ("/settings", "Settings", "settings", "sliders"),
)
# Whether the People nav item (voice profiles) is shown. A request-scoped flag so every page render agrees:
# ``PeopleNavMiddleware`` in app.py sets it per request from the settings; the settings page passes its own value.
_people_nav: contextvars.ContextVar = contextvars.ContextVar("mn_people_nav", default=False)


def set_people_nav(on: bool) -> None:
    _people_nav.set(bool(on))


def _nav_items(people_nav: Optional[bool] = None) -> tuple:
    show = _people_nav.get() if people_nav is None else people_nav
    return tuple(item for item in _NAV if show or item[2] != "people")


_ACTIVE_ALIASES = {"sessions": "transcriptions", "install": "settings"}

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
  var helpBtn = document.getElementById('search-help-btn'), help = document.getElementById('search-help');
  function setHelp(open) {
    if (!help || !helpBtn) return;
    help.hidden = !open; helpBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
  }
  if (helpBtn && help) {
    helpBtn.addEventListener('click', function () { setHelp(help.hidden); });
    help.addEventListener('click', function (event) {
      var key = event.target.closest && event.target.closest('[data-insert]');
      if (!key) return;
      var box = document.getElementById('q'), v = box.value;
      box.value = (v && !/\s$/.test(v) ? v + ' ' : v) + key.getAttribute('data-insert');
      box.focus(); box.setSelectionRange(box.value.length, box.value.length);
      box.dispatchEvent(new Event('input', {bubbles: true}));
    });
    document.addEventListener('click', function (event) {
      if (!help.hidden && !event.target.closest('.side-search')) setHelp(false);
    });
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && !help.hidden) { setHelp(false); helpBtn.focus(); }
    });
  }
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


def _update_footer_link() -> str:
    """Small accent link under the version when a newer ``main`` exists (updates.py)."""
    status = updates_mod.current_status()
    if not status:
        return ""
    return (
        f'<a class="app-update" href="{updates_mod.CHANGELOG_URL}" target="_blank" rel="noopener noreferrer">'
        f'Update available · v{html.escape(str(status["latest"]))}</a>'
    )


def _update_banner() -> str:
    """Calm notice at the top of Settings (the only place a phone's top bar can reach it)."""
    status = updates_mod.current_status()
    if not status:
        return ""
    return (
        f'<div class="banner update" role="status">{_icon("info")}<span>A newer version, '
        f'v{html.escape(str(status["latest"]))}, is available on GitHub; this server runs '
        f'v{html.escape(str(status["current"]))}. Nothing is updated automatically. '
        f'<a href="{updates_mod.CHANGELOG_URL}" target="_blank" rel="noopener noreferrer">See what changed</a>.'
        "</span></div>"
    )


def _ai_login_banner() -> str:
    """Every page, while the AI provider's login no longer works: notes are on hold until it is reconnected."""
    info = ai_health.active_banner()
    if not info:
        return ""
    return (
        f'<div class="banner warn ai-login-banner" role="alert">{_icon("alert")}<span>'
        f'The {html.escape(info["name"])} login for meeting notes has expired, so notes are on hold: meetings wait '
        "in the queue and get their notes once it is reconnected. "
        '<a href="/settings#notes">Reconnect in Settings</a>.</span></div>'
    )


def _shell(
    title: str,
    body: str,
    *,
    token_configured: bool,
    active: str = "",
    main_class: str = "",
    nav: bool = True,
    appearance: str = "system",
    people_nav: Optional[bool] = None,
) -> str:
    active = _ACTIVE_ALIASES.get(active, active)
    appearance = _normalize_appearance(appearance)
    nav_items = _nav_items(people_nav)
    sidebar = ""
    tabbar = ""
    if nav:
        current = ' aria-current="page"'
        links = "".join(
            f'<a href="{href}"{current if key == active else ""}>'
            f"{_icon(icon)}<span>{label}</span></a>"
            for href, label, key, icon in nav_items
        )
        tab_links = "".join(
            f'<a href="{href}"{current if key == active else ""}>'
            f"{_icon(icon, 20)}<span>{label}</span></a>"
            for href, label, key, icon in nav_items
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
           placeholder="Search or M-0142" title="Search names, transcripts, notes, attendees, or a meeting number like M-0142. Use title: attendee: notes: transcript: to narrow it."
           data-short="Search or M-0142" data-full="Search meetings">
    <kbd aria-hidden="true">/</kbd>
    <button type="button" class="search-help-btn" id="search-help-btn" aria-label="Search filters" aria-expanded="false" aria-controls="search-help" title="Search filters">?</button>
    <div class="search-help" id="search-help" role="group" aria-label="Search filters" hidden>
      <p class="sh-title">Narrow the search</p>
      <ul class="sh-list">
        <li><button type="button" class="sh-key" data-insert="title:">title:</button><span>the meeting name</span></li>
        <li><button type="button" class="sh-key" data-insert="attendee:">attendee:</button><span>people on the notes</span></li>
        <li><button type="button" class="sh-key" data-insert="notes:">notes:</button><span>the written notes</span></li>
        <li><button type="button" class="sh-key" data-insert="transcript:">transcript:</button><span>what was said</span></li>
      </ul>
      <p class="sh-eg">Example: <code>title:tess notes:budget</code><br>A phrase: <code>attendee:"Tess Stone"</code><br>Every term has to match. Plain words search everywhere.</p>
    </div>
  </form>
  <nav class="primary" aria-label="Primary">{links}</nav>
  <div class="side-foot">
    {_appearance_control("appearance-quick", appearance, compact=True)}
    <span class="app-version" aria-label="Meeting Notes version">v{html.escape(__version__)}</span>
    {_update_footer_link()}
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

    banner += _ai_login_banner()

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
