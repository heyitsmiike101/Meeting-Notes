"""Voice profiles web UI: nav flag, /people pages, settings checkbox, meeting-page overlay script."""

from __future__ import annotations

import dataclasses
import re
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from meeting_notes.server import settings as settings_mod
from meeting_notes.server import web
from meeting_notes.server.app import create_app

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated:DeprecationWarning"
)
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")

FORM = {
    "model": "small.en", "beam_size": "5", "audio_retention_days": "-1", "retention_check_interval_minutes": "60",
    "ai_provider": "disabled", "appearance": "system", "compute_claim_timeout_seconds": "30",
    "diarization_without_helper": "run_here",
}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = create_app(transcriber_factory=lambda **kw: None, data_root=str(tmp_path / "data"))
    with TestClient(app) as c:
        c.root = tmp_path / "data"
        yield c


def set_people(client, on: bool, diarization: bool = True):
    current = settings_mod.load_settings(client.root)
    settings_mod.save_settings(
        client.root, dataclasses.replace(current, voice_profiles_enabled=on, diarization_enabled=diarization))


def navs(page: str):
    sidebar = page[page.index('<nav class="primary"'): page.index("</nav>", page.index('<nav class="primary"'))]
    tabbar = page[page.index('<nav class="tabbar"'): page.index("</nav>", page.index('<nav class="tabbar"'))]
    return sidebar, tabbar


# -- nav ------------------------------------------------------------------------------------------------------


def test_nav_has_no_people_item_while_off(client):
    for path in ("/", "/meetings", "/recorders", "/settings"):
        for nav in navs(client.get(path).text):
            assert "/people" not in nav and "People" not in nav, path
    for nav in navs(web.render_home_page(token_configured=True)):  # a bare render (no request) agrees
        assert "/people" not in nav


def test_nav_shows_people_between_meetings_and_recorders_in_sidebar_and_tabbar(client):
    set_people(client, True)
    for path in ("/", "/meetings", "/recorders", "/settings", "/people"):
        sidebar, tabbar = navs(client.get(path).text)
        for nav in (sidebar, tabbar):
            assert '<a href="/people"' in nav and "<span>People</span>" in nav, path
            assert nav.index('href="/meetings"') < nav.index('href="/people"') < nav.index('href="/recorders"')
        assert tabbar.count("<a href=") == 5


def test_nav_needs_diarization_too(client):
    set_people(client, True, diarization=False)
    assert all("/people" not in nav for nav in navs(client.get("/").text))


def test_people_nav_icon_exists_in_both_icon_tables():
    assert "users" in web._ICON_PATHS
    assert '"users"' in (web._STATIC_DIR / "icons.js").read_text(encoding="utf-8")


def test_settings_save_updates_the_nav_in_the_same_response(client):
    r = client.post("/settings", data={**FORM, "diarization_enabled": "on", "voice_profiles_enabled": "on"})
    assert r.status_code == 200
    assert all('href="/people"' in nav for nav in navs(r.text))
    r = client.post("/settings", data={**FORM, "diarization_enabled": "on", "voice_profiles_enabled": "0"})
    assert all("/people" not in nav for nav in navs(r.text))


# -- routes ---------------------------------------------------------------------------------------------------


def test_people_pages_are_404_while_off_and_200_when_on(client):
    assert client.get("/people").status_code == 404
    assert client.get("/people/p_abc123").status_code == 404
    set_people(client, True)
    r = client.get("/people")
    assert r.status_code == 200 and "<h1>People</h1>" in r.text and 'id="people-rows"' in r.text
    r = client.get("/people/p_abc123")
    assert r.status_code == 200 and 'var PERSON_ID = "p_abc123";' in r.text
    assert client.get("/people/bad%20id").status_code == 404


def test_people_list_page_structure():
    page = web.render_people_page(token_configured=True, appearance="dark")
    assert '<html lang="en" data-theme="dark">' in page
    for label in ("Most talking", "Most recent", "Alphabetical"):
        assert label in page
    assert "mn-people-sort" in page and "try { var savedSort = localStorage" in page  # remembered, in try/catch
    assert "/v1/people?sort=" in page
    assert "People appear after a meeting" in page  # empty state
    assert "Sounds like " in page  # hint for unnamed people
    assert "<style>" not in page


def test_person_page_structure_and_actions():
    page = web.render_person_page("p_x", token_configured=True)
    for text in ("Voice samples", "Meetings", "What they said", "Merge with...", "Delete profile", "Not them",
                 "Move to...", "Unlink"):
        assert text in page
    for needle in ("'PATCH'", "/v1/people/appearances", "'/merge'", "'DELETE'", "new Audio(s.url)",
                   "'#t=' + t", "escapeHtml(q.text)"):
        assert needle in page, needle
    assert 'id="confirm-dialog"' in page and 'id="pick-dialog"' in page


def test_person_id_is_escaped_in_the_script():
    page = web.render_person_page('x"</script><b>', token_configured=True)
    assert "</script><b>" not in page
    assert "\\u003c/script>" in page


# -- settings -------------------------------------------------------------------------------------------------


def test_settings_checkbox_and_roundtrip(client):
    page = client.get("/settings").text
    assert 'name="voice_profiles_enabled" id="voice-profiles"' in page
    assert "Recognise the same person across meetings by voice; name them on the People page." in page
    assert 'id="voice-profiles-delete"' not in page  # no delete button until it is on
    client.post("/settings", data={**FORM, "diarization_enabled": "on", "voice_profiles_enabled": "on"})
    assert settings_mod.load_settings(client.root).voice_profiles_enabled is True
    page = client.get("/settings").text
    assert re.search(r'id="voice-profiles" value="on" checked', page)
    assert 'id="voice-profiles-delete"' in page and "Delete all voice profiles" in page
    assert '"/v1/people", {method: "DELETE"' in page
    assert "only work while Speaker labels is on" not in page


def test_settings_notes_that_diarization_is_needed(client):
    set_people(client, True, diarization=False)
    page = client.get("/settings").text
    assert "only work while Speaker labels is on" in page
    assert 'id="voice-profiles-delete"' not in page


# -- meeting page ---------------------------------------------------------------------------------------------


def test_meeting_page_script_shows_display_names_links_suggestions_and_seeks(client):
    page = client.get("/sessions/s1").text
    assert "seg.display||seg.label" in page
    assert "personId:seg.display?seg.person_id:null" in page
    assert "'<a href=\"/people/' + encodeURIComponent(o.personId)" in page
    assert 'id="people-suggestions"' in page and "data.people_suggestions" in page
    assert "sounds like" in page and 'data-ps="confirm"' in page and 'data-ps="reject"' in page
    assert "/v1/people/appearances" in page
    assert "pendingSeek" in page and "#t=" in page  # /sessions/{id}#t=SECONDS scrolls to that moment


@needs_node
def test_speaker_row_links_the_display_name_only_when_there_is_a_person():
    src = web._JS_HELPERS_SRC

    def between(a, b):
        i = src.index(a)
        return src[i: src.index(b, i)]

    script = (
        between("function escapeHtml", "\nfunction localInputValue")
        + between("function initials", "/* Live preview")
        + between("function fmtDuration", "/* Meetings list helpers")
        + """
var a = speakerRow({mic: false, label: 'Sarah <b>', personId: 'p_1', start: 5, text: 'hi'});
var b = speakerRow({mic: false, label: 'Them 2', start: 5, text: 'hi'});
if (a.indexOf('<a href="/people/p_1">Sarah &lt;b&gt;</a>') < 0) throw new Error('no link: ' + a);
if (b.indexOf('/people/') >= 0) throw new Error('unexpected link: ' + b);
"""
    )
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


@needs_node
@pytest.mark.parametrize("render", [
    lambda: web.render_people_page(token_configured=True),
    lambda: web.render_person_page("p_x", token_configured=True),
])
def test_people_page_scripts_parse(render, tmp_path):
    scripts = re.findall(r"<script>(.*?)</script>", render(), flags=re.S)
    assert scripts
    for i, text in enumerate(scripts):
        path = tmp_path / f"s{i}.js"
        path.write_text(text, encoding="utf-8")
        result = subprocess.run([NODE, "--check", str(path)], capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
