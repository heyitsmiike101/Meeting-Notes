"""The web UI's visual world: self-hosted fonts, board numbers, the session
strip and the console-bar chrome (no sidebar, no eyebrow labels)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from meeting_notes.server import board
from meeting_notes.server.app import create_app
from meeting_notes.server.settings import Settings
from meeting_notes.server.web import (
    render_home_page,
    render_install_page,
    render_login_page,
    render_settings_page,
    stylesheet_text,
    render_transcriptions_page,
)

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated:DeprecationWarning"
)

REPO = Path(__file__).resolve().parents[1]


def _client(tmp_path, monkeypatch, token=None):
    if token:
        monkeypatch.setenv("MEETING_NOTES_TOKEN", token)
    else:
        monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    return TestClient(create_app(data_root=str(tmp_path / "data")))


# -- fonts -----------------------------------------------------------------


def test_font_is_served_as_woff2_without_authentication(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, token="secret-token")
    # An unauthenticated API call is refused, but fonts (needed by the sign-in page) are public.
    assert client.get("/v1/sessions").status_code in (401, 403)
    resp = client.get("/static/fonts/barlow-latin-400.woff2")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "font/woff2"
    assert "immutable" in resp.headers["cache-control"]
    assert resp.content[:4] == b"wOF2"


def test_every_font_the_stylesheet_names_exists_and_unknown_files_404(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    urls = set(re.findall(r"url\((/static/fonts/[^)]+\.woff2)\)", stylesheet_text()))
    assert len(urls) >= 7
    for url in urls:
        assert client.get(url).status_code == 200, url
    assert client.get("/static/fonts/nope.woff2").status_code == 404
    assert client.get("/static/fonts/..%2Fweb.py").status_code == 404
    assert client.get("/static/fonts/web.py").status_code == 404


def test_fonts_are_packaged_and_no_external_font_hosts_are_referenced():
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert "server/static/fonts/*" in pyproject
    for page in (render_login_page(), render_home_page(token_configured=True), stylesheet_text()):
        assert "fonts.googleapis" not in page and "fonts.gstatic" not in page
    assert "font-display:swap" in stylesheet_text()
    assert "server/static/app.css" in pyproject and "server/static/icons.js" in pyproject


# -- board numbers -----------------------------------------------------------


def test_board_number_is_stable_and_well_formed():
    assert board.board_number("abc") == board.board_number("abc")
    assert re.fullmatch(r"M-\d{4}", board.board_number("sess-2026-09-28-kickoff"))
    # Pinned so a change to the algorithm (which would relabel every meeting) is deliberate.
    assert board.board_number("a") == "M-2220"
    assert board.board_number("") == "M-" + f"{0x811C9DC5 % 10000:04d}"
    assert board.board_number("a") != board.board_number("b")


def test_board_query_parsing():
    assert board.parse_board_query("M-0142") == "0142"
    assert board.parse_board_query(" m0142 ") == "0142"
    assert board.parse_board_query("M-01") == "01"
    assert board.parse_board_query("budget") is None
    assert board.parse_board_query("M-12345") is None
    assert board.parse_board_query(None) is None


def test_session_list_and_detail_carry_board_number_and_search_matches_it(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    store = client.app.state.store
    for sid in ("alpha-one", "beta-two", "gamma-three"):
        store.write_session_meta(sid, {"name": f"Meeting {sid}", "created": "2026-09-20"})
        store._index_upsert_session(sid)
    items = client.get("/v1/sessions").json()["items"]
    assert {i["session_id"]: i["board"] for i in items} == {
        sid: board.board_number(sid) for sid in ("alpha-one", "beta-two", "gamma-three")
    }
    target = board.board_number("beta-two")
    found = client.get("/v1/sessions", params={"q": target}).json()["items"]
    assert "beta-two" in [i["session_id"] for i in found]
    assert client.get("/v1/sessions/beta-two").json()["board"] == target
    # A prefix such as "M-" plus two digits matches too, lower case included.
    prefix = "m-" + target[2:4]
    assert "beta-two" in [
        i["session_id"] for i in client.get("/v1/sessions", params={"q": prefix}).json()["items"]
    ]


# -- chrome and the session strip ---------------------------------------------


def _pages():
    return {
        "login": render_login_page(error=True),
        "home": render_home_page(token_configured=True),
        "meetings": render_transcriptions_page(token_configured=True),
        "settings": render_settings_page(Settings(), token_configured=True),
        "install": render_install_page("http://meeting.lan", token_configured=True),
    }


def test_console_bar_replaces_the_sidebar_and_no_page_uses_eyebrows():
    for name, page in _pages().items():
        assert 'class="eyebrow"' not in page and ".eyebrow" not in page, name
        assert "sidebar" not in page, name
        assert '<header class="console">' in page, name
        assert '<main id="main"' in page, name
        assert "alert(" not in page, f"{name}: errors are shown inline, not with alert()"
    meetings = _pages()["meetings"]
    assert '<nav class="primary" aria-label="Primary">' in meetings
    assert meetings.index('href="/meetings"') < meetings.index('href="/settings"')
    assert 'aria-current="page"' in meetings
    # Search lives in the console bar and is the widest control.
    assert re.search(r'<header class="console">.*id="q".*</header>', meetings, re.S)


def test_browser_surfaces_are_themed():
    css = stylesheet_text()
    for needle in (
        "::selection", "caret-color:var(--red)", "accent-color:var(--red)", ":focus-visible",
        "scrollbar-color", "text-underline-offset", "font-variant-numeric:tabular-nums",
        "prefers-reduced-motion",
    ):
        assert needle in css, needle
    assert "border-left:3px" not in css and "linear-gradient(135deg,#" not in css


def test_shelf_row_has_board_length_bar_ticks_and_open_action():
    page = render_transcriptions_page(token_configured=True)
    for needle in (
        'class="board-cell"', "function lengthBar(row)", 'class="lenbar"', "function scaleBars()",
        "tick('done')", "Notes ready", "Not created", 'class="row-open"', "function emptyRows()",
        'id="list-error"', 'class="skel"',
    ):
        assert needle in page, needle


def test_session_strip_markup_and_script_are_present_and_use_real_segment_fields():
    page = render_transcriptions_page(token_configured=True)
    css = stylesheet_text()
    assert 'id="session-strip"' in page and 'id="strip-wrap"' in page
    for needle in (
        "function renderStrip(data)", "function jumpToSegment(index)", "function validSpan(s)",
        "s.start", "s.end", "s.track", "meta.duration_sec", "in_gap", "data-seg",
        "wrap.hidden=true",  # degrades: hidden when no segment carries timing
        "strip-draw", "function onStripKey(e)",
    ):
        assert needle in page, needle
    # Clicking a block switches to the transcript and highlights that segment.
    assert "setDetailView('transcript')" in page and "classList.add('hit')" in page
    assert 'id="seg-\'+i+\'"' in page
    assert "@keyframes strip-draw" in css
    assert re.search(r"@media \(prefers-reduced-motion: reduce\)", css)


def test_meeting_sheet_keeps_every_control_and_adds_copy():
    page = render_transcriptions_page(token_configured=True)
    for element_id in (
        "close-overlay", "edit-meeting-name", "queue-review", "show-transcript", "notes-download",
        "notes-copy", "edit-summary-name", "notes-retry", "retranscribe", "delete-audio",
        "delete-entry", "audio-players", "transcription-checklist", "review-status",
    ):
        assert f'id="{element_id}"' in page, element_id
    assert "function trapFocus(event, root)" in page
    assert 'role="dialog" aria-modal="true"' in page


# -- round 2: static assets, dates, shelf rows, exact counts ------------------------


def test_stylesheet_and_icons_are_cached_static_files_linked_from_every_page(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, token="secret-token")
    for page in _pages().values():
        assert re.search(r'<link rel="stylesheet" href="/static/app\.css\?v=[0-9a-f]{10}">', page)
        assert "<style>" not in page
    css = client.get("/static/app.css")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
    assert "immutable" in css.headers["cache-control"]
    icons = client.get("/static/icons.js")
    assert icons.status_code == 200 and icons.headers["content-type"].startswith("text/javascript")
    assert client.get("/static/web.py").status_code == 404


def test_icons_js_matches_the_python_icon_table():
    from meeting_notes.server import web

    text = (REPO / "meeting_notes" / "server" / "static" / "icons.js").read_text(encoding="utf-8")
    payload = json.loads(text[text.index("{"): text.rindex("}") + 1])
    assert payload == web._ICON_PATHS


def test_one_compact_date_formatter_everywhere():
    for page in (render_home_page(token_configured=True), render_transcriptions_page(token_configured=True)):
        assert "function fmtDate(value, relative)" in page
        assert "toLocaleString" not in page
        assert '"Today"' in page and '"Yesterday"' in page
        assert "opts.year" in page  # year only when it is not the current year


def test_shelf_rows_are_compact_and_keyboard_order_skips_the_row():
    page = render_transcriptions_page(token_configured=True)
    assert "return '<tr aria-label=\"Open '" in page  # no tabindex on the row itself
    assert 'class="sub-board"' in page and 'class="row-open"' in page
    assert 'data-short="Search or M-0142"' in page and "function setPlaceholder()" in page
    css = stylesheet_text()
    assert ".lenbar .len-time { flex:none; width:7.2ch" in css


def test_axis_labels_are_compact_and_never_overlap():
    page = render_transcriptions_page(token_configured=True)
    assert "function fmtAxis(t)" in page and "function pruneAxis()" in page
    assert "plotW/72" in page


def test_notes_status_is_not_repeated_under_the_title():
    page = render_transcriptions_page(token_configured=True)
    assert "Ready to share" not in page


def test_home_shows_an_exact_audio_count(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    store = client.app.state.store
    for sid in ("aa", "bb"):
        store.write_session_meta(sid, {"name": sid, "created": "2026-09-20"})
        store._index_upsert_session(sid)
    assert client.get("/v1/sessions").json()["audio_total"] == 0
    assert "audio_total" in render_home_page(token_configured=True)
    assert "+'" not in render_home_page(token_configured=True).split("audio-count')")[1][:120]


def test_settings_scrollspy_activates_first_section_at_top():
    page = render_settings_page(Settings(), token_configured=True)
    assert "window.scrollY < 8" in page and "window.innerHeight * 0.3" in page
    assert 'class="settings-page"' in page
