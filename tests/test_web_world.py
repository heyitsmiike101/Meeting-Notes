"""The web UI's design system: self-hosted Inter, board numbers, the session
timeline, the sidebar chrome (no eyebrow labels) and the light/dark/system themes."""

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
    resp = client.get("/static/fonts/inter-latin-wght-normal.woff2")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "font/woff2"
    assert "immutable" in resp.headers["cache-control"]
    assert resp.content[:4] == b"wOF2"


def test_every_font_the_stylesheet_names_exists_and_unknown_files_404(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    urls = set(re.findall(r"url\((/static/fonts/[^)]+\.woff2)\)", stylesheet_text()))
    assert len(urls) >= 2
    assert not any("barlow" in url for url in urls)
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


def test_sidebar_layout_and_no_page_uses_eyebrows():
    for name, page in _pages().items():
        assert 'class="eyebrow"' not in page and ".eyebrow" not in page, name
        assert '<main id="main"' in page, name
        assert "alert(" not in page, f"{name}: errors are shown inline, not with alert()"
        if name == "login":
            assert '<aside class="sidebar">' not in page
            continue
        assert '<aside class="sidebar">' in page, name
        assert '<nav class="tabbar"' in page, name
        assert 'name="appearance-quick"' in page, name
    meetings = _pages()["meetings"]
    assert '<nav class="primary" aria-label="Primary">' in meetings
    assert meetings.index('href="/meetings"') < meetings.index('href="/settings"')
    assert 'aria-current="page"' in meetings
    # Search sits at the top of the sidebar, above the navigation.
    assert re.search(r'<aside class="sidebar">.*id="q".*<nav class="primary"', meetings, re.S)
    # ... and works from every page: the form submits to the Meetings list.
    assert 'action="/meetings" method="get"' in _pages()["home"]


def test_browser_surfaces_are_themed():
    css = stylesheet_text()
    for needle in (
        "::selection", "caret-color:var(--accent)", "accent-color:var(--accent)", ":focus-visible",
        "scrollbar-color", "text-underline-offset", "font-variant-numeric:tabular-nums",
        "prefers-reduced-motion",
    ):
        assert needle in css, needle
    assert "border-left:3px" not in css and "linear-gradient(135deg,#" not in css
    # No leftovers of the previous themed look.
    for gone in ("Barlow", "--kraft", "--graphite", "--red", "board-cell", ".spine", ".lane-no"):
        assert gone not in css, gone


def test_meeting_row_has_status_badges_open_link_and_states():
    page = render_transcriptions_page(token_configured=True)
    for needle in (
        "function meetingRow(row)", 'class="mrow', "badge('done','Notes ready')", "Not created",
        'class="row-open mrow-title"', "function emptyRows()", 'id="list-error"', 'class="mrow skel"',
        'class="sub-id"', "function dot()",
    ):
        assert needle in page, needle
    # The stable meeting number is a quiet secondary id, not a column.
    assert 'class="board-cell"' not in page and "board-chip" not in page


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
    assert ".strip-draw" in css and "@keyframes fade-in" in css
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


def test_meeting_rows_are_compact_links_and_keyboard_order_is_link_first():
    page = render_transcriptions_page(token_configured=True)
    # The row is a list item whose title is a real link; the row itself is not a tab stop.
    assert "'<li class=\"mrow'" in page and 'tabindex="0" data-id' not in page
    assert '<a class="row-open mrow-title" href="/sessions/' in page
    assert 'class="sub-id"' in page and 'class="sub-audio"' in page
    assert 'data-short="Search or M-0142"' in page and "function setPlaceholder()" in page
    # Checkboxes appear on hover, focus or when anything is selected, and stay keyboard reachable.
    css = stylesheet_text()
    assert ".row-select { opacity:0" in css and ".mlist.has-sel .row-select" in css


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


# -- appearance: light, dark and system themes --------------------------------------------------


def test_both_theme_token_blocks_and_the_system_query_are_present():
    css = stylesheet_text()
    assert re.search(r":root \{[^}]*color-scheme:light", css, re.S)
    assert re.search(r':root\[data-theme="dark"\] \{[^}]*color-scheme:dark', css, re.S)
    system = re.search(
        r'@media \(prefers-color-scheme: dark\) \{\s*:root\[data-theme="system"\], :root:not\(\[data-theme\]\) \{(.*?)\n  \}\n\}',
        css,
        re.S,
    )
    assert system, "dark tokens must apply to data-theme=system under prefers-color-scheme"
    explicit = re.search(r':root\[data-theme="dark"\] \{(.*?)\n\}', css, re.S).group(1)

    def names(block):
        return sorted(re.findall(r"(--[a-z0-9-]+):", block))

    assert names(system.group(1)) == names(explicit)
    light = re.search(r"^:root \{(.*?)\n\}", css, re.S | re.M).group(1)
    for name in ("--bg", "--text", "--accent", "--border", "--success-dot", "--danger-dot", "--warning-fg"):
        assert name + ":" in light and name + ":" in explicit, name


@pytest.mark.parametrize("value", ["system", "light", "dark"])
def test_data_theme_is_rendered_server_side_for_every_page(value):
    pages = {
        "login": render_login_page(appearance=value),
        "home": render_home_page(token_configured=True, appearance=value),
        "meetings": render_transcriptions_page(token_configured=True, appearance=value),
        "settings": render_settings_page(Settings(appearance=value), token_configured=True),
        "install": render_install_page("http://meeting.lan", token_configured=True, appearance=value),
    }
    for name, page in pages.items():
        assert f'<html lang="en" data-theme="{value}">' in page, name
        assert '<meta name="color-scheme" content="light dark">' in page, name
        if value == "system":
            assert page.count('<meta name="theme-color"') == 2 and "prefers-color-scheme: dark" in page, name
        else:
            assert page.count('<meta name="theme-color"') == 1, name
    # Unknown values fall back to system rather than breaking the page.
    assert 'data-theme="system"' in render_login_page(appearance="neon")


def test_settings_page_has_an_appearance_control_with_the_saved_choice_checked():
    page = render_settings_page(Settings(appearance="dark"), token_configured=True)
    assert 'id="settings-appearance-heading"' in page and 'href="#settings-appearance-heading"' in page
    assert re.search(r'name="appearance" value="dark" data-appearance checked', page)
    assert 'name="appearance" value="light" data-appearance checked' not in page
    assert "/v1/appearance" in page


def test_appearance_round_trips_through_the_form_the_small_endpoint_and_the_api(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    assert client.get("/v1/settings").json()["appearance"] == "system"
    assert 'data-theme="system"' in client.get("/meetings").text

    # The theme switch's own endpoint changes only the theme.
    resp = client.put("/v1/appearance", json={"appearance": "dark"})
    assert resp.status_code == 200 and resp.json() == {"appearance": "dark"}
    assert client.get("/v1/settings").json()["appearance"] == "dark"
    for path in ("/", "/meetings", "/settings", "/install"):
        assert 'data-theme="dark"' in client.get(path).text, path
    assert client.put("/v1/appearance", json={"appearance": "neon"}).status_code == 400
    assert client.get("/v1/settings").json()["appearance"] == "dark"

    # A full settings save from an older client (no appearance field) keeps the theme.
    body = {k: v for k, v in client.get("/v1/settings").json().items() if k not in ("appearance", "model_choices")}
    body["model"] = "base.en"
    assert client.put("/v1/settings", json=body).status_code == 200
    assert client.get("/v1/settings").json()["appearance"] == "dark"
    assert client.put("/v1/settings", json={**body, "appearance": "light"}).json()["appearance"] == "light"

    # The settings form posts the chosen theme with everything else.
    form = {
        "model": "base.en", "beam_size": "5", "audio_retention_days": "-1",
        "retention_check_interval_minutes": "60", "ai_provider": "disabled",
        "diarization_min_speakers": "1", "diarization_max_speakers": "8", "appearance": "dark",
    }
    saved = client.post("/settings", data=form)
    assert saved.status_code == 200 and 'data-theme="dark"' in saved.text
    assert client.get("/v1/settings").json()["appearance"] == "dark"
    bad = client.post("/settings", data={**form, "appearance": "neon"})
    assert "appearance must be system, light, or dark" in bad.text
    # A stale form without the field must not reset it either.
    form.pop("appearance")
    assert client.post("/settings", data=form).status_code == 200
    assert client.get("/v1/settings").json()["appearance"] == "dark"


def test_appearance_endpoint_requires_the_token(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, token="secret-token")
    assert client.put("/v1/appearance", json={"appearance": "dark"}).status_code in (401, 403)
