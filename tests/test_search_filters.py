"""The search query language (title: attendee: notes: transcript:) -- parser, index, API."""

from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from meeting_notes import wire
from meeting_notes.server import board as board_mod
from meeting_notes.server import index as index_mod
from meeting_notes.server import search_query as sq
from meeting_notes.server import store as store_mod
from meeting_notes.server.app import create_app

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated:DeprecationWarning"
)

TOKEN = "search-secret"
H = {"Authorization": f"Bearer {TOKEN}"}


# -- parser -------------------------------------------------------------------


def _t(q):
    return [(t.field, t.text) for t in sq.parse(q).terms]


def test_parse_words_and_phrases():
    assert _t("budget  review") == [(None, "budget"), (None, "review")]
    assert _t('"budget review" q3') == [(None, "budget review"), (None, "q3")]
    assert _t("") == [] and _t(None) == [] and _t("   ") == []


def test_parse_filters_and_aliases_case_insensitive():
    assert _t("title:tara") == [("title", "tara")]
    assert _t("TITLE:Tara Attendees:bob PERSON:al") == [("title", "Tara"), ("attendee", "bob"), ("attendee", "al")]
    assert _t('attendee:"Tara Swart" notes:budget transcript:"go live"') == [
        ("attendee", "Tara Swart"), ("notes", "budget"), ("transcript", "go live"),
    ]


def test_parse_unknown_field_is_plain_text():
    assert _t("foo:bar") == [(None, "foo:bar")]
    assert _t("http://x.io title:a") == [(None, "http://x.io"), ("title", "a")]
    assert _t("M-0142") == [(None, "M-0142")]


def test_parse_empty_filter_values_ignored():
    assert _t("title:") == []
    assert _t('title:"" notes:  x') == [(None, "x")]
    assert _t('attendee:"   "') == []


def test_parse_unterminated_quote_and_find_text():
    assert _t('title:"tara sw') == [("title", "tara sw")]
    assert _t('"open phrase') == [(None, "open phrase")]
    assert sq.parse("title:tara notes:budget").find_text == "budget"
    assert sq.parse("title:tara attendee:bob").find_text == ""
    assert sq.parse("hello title:x").find_text == "hello"
    assert sq.parse("title:x").has_filters and not sq.parse("x").has_filters


def test_notes_helpers():
    payload = {
        "title": "T", "summary": "S", "meeting_notes": "N", "participants": ["Ann", {"name": "Bob"}, 3],
        "key_points": ["k"], "action_items": [{"action": "do it", "owner": "Cy", "context": "c"}, "plain"],
        "risks": [{"text": "r"}],
    }
    assert sq.attendee_names(payload) == ["Ann", "Bob"]
    body = sq.notes_body(payload)
    for word in ("S", "N", "k", "do it", "Cy", "plain", "r"):
        assert word in body.split("\n")
    assert sq.notes_title(payload) == "T"
    assert sq.notes_body(None) == "" and sq.attendee_names("x") == []


# -- index --------------------------------------------------------------------


def _store(tmp_path):
    return store_mod.Store(str(tmp_path / "data"))


def _meeting(store, sid, name, transcript, notes=None, created="2026-09-2"):
    store.write_session_meta(sid, {"name": name, "created": created + str(len(sid) % 9)})
    job = store.create_job(sid)
    store.update_job(job, state=wire.JobState.DONE, progress=1.0)
    store.write_transcript(job, "# md", json.dumps({"segments": [{"text": transcript}]}))
    if notes is not None:
        review = store.create_review(sid)
        store.claim_next_review()
        store.complete_review(review["review_id"], notes)
        return review["review_id"]
    return None


def _ids(store, q, **kw):
    return sorted(i["session_id"] for i in store.list_sessions(q=q, per_page=200, **kw)["items"])


def _populate(store):
    # Tara is mentioned everywhere; only a, b are titled with her; only a, c have her as an attendee.
    _meeting(store, "a", "Tara weekly", "we talked to tara about pricing", {
        "title": "Weekly sync", "summary": "Pricing discussion", "participants": ["Tara Swart", "Mike"],
        "action_items": [{"action": "send quote", "owner": "Mike"}]})
    _meeting(store, "bb", "Sync with TARA", "tara said hello", {
        "title": "Intro", "summary": "Hello world", "participants": ["Jon Smit"]})
    _meeting(store, "ccc", "Quarterly review", "mention of tara and the budget", {
        "title": "Review", "summary": "Budget review", "meeting_notes": "tara raised a risk",
        "participants": [{"name": "Tara Swart"}]})
    _meeting(store, "dddd", "Standup", "nothing relevant here", None)


def test_title_attendee_notes_transcript_filters(tmp_path):
    store = _store(tmp_path)
    _populate(store)
    assert _ids(store, "tara") == ["a", "bb", "ccc"]  # bare: name, transcript, notes, attendees
    assert _ids(store, "title:tara") == ["a", "bb"]
    assert _ids(store, "TITLE:tara") == ["a", "bb"]
    assert _ids(store, "attendee:tara") == ["a", "ccc"]
    assert _ids(store, 'attendee:"Tara Swart"') == ["a", "ccc"]
    assert _ids(store, "person:swar") == ["a", "ccc"]  # substring
    assert _ids(store, "notes:budget") == ["ccc"]
    assert _ids(store, "notes:quote") == ["a"]  # action item
    assert _ids(store, "notes:mike") == ["a"]  # owner
    assert _ids(store, "notes:tara") == ["ccc"]
    assert _ids(store, "transcript:tara") == ["a", "bb", "ccc"]
    assert _ids(store, "transcript:budget") == ["ccc"]
    assert _ids(store, "title:weekly") == ["a"]
    assert _ids(store, "title:intro") == ["bb"]  # the notes title counts


def test_terms_and_together(tmp_path):
    store = _store(tmp_path)
    _populate(store)
    assert _ids(store, "title:tara attendee:swart") == ["a"]
    assert _ids(store, "title:tara notes:hello") == ["bb"]
    assert _ids(store, "tara budget") == ["ccc"]  # each bare term somewhere
    assert _ids(store, "tara pricing") == ["a"]
    assert _ids(store, "title:tara transcript:budget") == []
    assert _ids(store, '"tara swart"') == ["a", "ccc"]  # phrase in attendees


def test_unknown_and_empty_filters(tmp_path):
    store = _store(tmp_path)
    _populate(store)
    assert _ids(store, "foo:bar") == []
    assert _ids(store, "title:") == _ids(store, "") == ["a", "bb", "ccc", "dddd"]
    assert _ids(store, 'title:"" tara') == ["a", "bb", "ccc"]


def test_board_id_and_special_characters(tmp_path):
    store = _store(tmp_path)
    _populate(store)
    digits = board_mod.board_digits("bb")
    assert "bb" in _ids(store, f"M-{digits}")
    assert _ids(store, f"m{digits} title:tara") == ["bb"]
    assert _ids(store, "title:%") == []  # LIKE wildcards are literal
    assert _ids(store, "title:_") == []
    assert _ids(store, 'notes:"c++" OR (x') == []  # FTS syntax never blows up


def test_pagination_and_total_with_filters(tmp_path):
    store = _store(tmp_path)
    for n in range(7):
        _meeting(store, f"s{n}", f"Tara {n}", "x", {"title": "T", "participants": ["Tara"]},
                 created=f"2026-09-0{n}")
    _meeting(store, "other", "Other", "x", None)
    r1 = store.list_sessions(q="title:tara attendee:tara", page=1, per_page=3)
    r3 = store.list_sessions(q="title:tara attendee:tara", page=3, per_page=3)
    assert r1["total"] == 7 and len(r1["items"]) == 3 and len(r3["items"]) == 1


def test_notes_follow_edits_regeneration_and_deletion(tmp_path):
    store = _store(tmp_path)
    rid = _meeting(store, "a", "Meeting", "t", {
        "title": "Old title", "summary": "alpha", "participants": ["Ann Lee"]})
    assert _ids(store, "notes:alpha") == ["a"] and _ids(store, "attendee:ann") == ["a"]
    # participant rename rewrites the notes and the attendees
    store.set_review_participants(rid, [{"name": "Anna Lee", "was": "Ann Lee"}])
    assert _ids(store, "attendee:anna") == ["a"]
    store.set_review_participants(rid, [{"name": "Zed", "was": "Anna Lee"}])
    assert _ids(store, "attendee:anna") == [] and _ids(store, "attendee:zed") == ["a"]
    # the user renames the notes title
    store.rename_review(rid, "Brand new title")
    assert _ids(store, "title:brand") == ["a"] and _ids(store, "title:old") == []
    # regeneration clears the notes until they finish again
    store.retry_review(rid)
    assert _ids(store, "notes:alpha") == [] and _ids(store, "attendee:zed") == []
    store.claim_next_review()
    store.complete_review(rid, {"title": "X", "summary": "beta", "participants": ["Yan"]})
    assert _ids(store, "notes:beta") == ["a"] and _ids(store, "notes:alpha") == []
    assert _ids(store, "attendee:yan") == ["a"]
    # a failed regeneration also leaves nothing to search
    store.retry_review(rid)
    store.claim_next_review()
    store.fail_review(rid, "boom")
    assert _ids(store, "notes:beta") == []
    # delete / trash drop everything
    store.delete_session("a")
    assert _ids(store, "beta") == []
    with store.index._lock:
        assert store.index._conn.execute("SELECT COUNT(*) FROM notes_meta").fetchone()[0] == 0
        assert store.index._conn.execute("SELECT COUNT(*) FROM notes_text").fetchone()[0] == 0


def test_only_newest_review_counts_and_trash_restore(tmp_path):
    store = _store(tmp_path)
    old = _meeting(store, "a", "Meeting", "t", {"title": "T", "summary": "firstgen", "participants": ["Ann"]})
    new = store.create_review("a", force=True)
    store.claim_next_review()
    store.complete_review(new["review_id"], {"title": "T2", "summary": "secondgen", "participants": ["Bo"]})
    assert _ids(store, "notes:secondgen") == ["a"] and _ids(store, "notes:firstgen") == []
    store.trash_session("a")
    assert _ids(store, "secondgen") == []
    store.restore_session("a")
    assert _ids(store, "notes:secondgen") == ["a"] and _ids(store, "attendee:bo") == ["a"]
    assert old != new["review_id"]


def test_old_index_without_notes_tables_is_rebuilt(tmp_path):
    store = _store(tmp_path)
    _populate(store)
    store.index.close()
    db = tmp_path / "data" / "index.sqlite"
    conn = sqlite3.connect(str(db))
    conn.execute("DROP TABLE notes_meta")
    conn.execute("DROP TABLE notes_text")
    conn.commit()
    conn.close()
    again = _store(tmp_path)  # startup recreates the tables and refills them from the review files
    assert _ids(again, "notes:budget") == ["ccc"]
    assert _ids(again, "attendee:tara") == ["a", "ccc"]
    again.reindex()
    assert _ids(again, "title:intro") == ["bb"]


def test_like_fallback_without_fts(tmp_path, monkeypatch):
    real = sqlite3.connect

    class NoFts:
        def __init__(self, conn):
            self._c = conn
            self.row_factory = None

        def execute(self, sql, *a):
            if "fts5" in sql.lower():
                raise sqlite3.OperationalError("no such module: fts5")
            return self._c.execute(sql, *a)

        def __getattr__(self, name):
            return getattr(self._c, name)

        def __setattr__(self, name, value):
            if name == "row_factory":
                self._c.row_factory = value
            object.__setattr__(self, name, value)

    monkeypatch.setattr(index_mod.sqlite3, "connect", lambda *a, **k: NoFts(real(*a, **k)))
    store = _store(tmp_path)
    assert store.index.fts_enabled is False
    _populate(store)
    assert _ids(store, "notes:budget") == ["ccc"]
    assert _ids(store, "transcript:pricing") == ["a"]
    assert _ids(store, "tara budget") == ["ccc"]


# -- API ----------------------------------------------------------------------


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_NOTES_TOKEN", TOKEN)
    app = create_app(data_root=str(tmp_path / "data"))
    _populate(app.state.store)
    return TestClient(app), app


def test_sessions_api_filters(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/v1/sessions", params={"q": "title:tara"}, headers=H).json()
    assert sorted(i["session_id"] for i in r["items"]) == ["a", "bb"] and r["total"] == 2
    r = client.get("/v1/sessions", params={"q": 'attendee:"tara swart" notes:budget'}, headers=H).json()
    assert [i["session_id"] for i in r["items"]] == ["ccc"]


def _agent_headers(app):
    return {"Authorization": "Bearer " + app.state.agent_keys.create("reader")["key"]}


def test_agent_api_search_uses_filters(tmp_path, monkeypatch):
    client, app = _client(tmp_path, monkeypatch)
    ah = _agent_headers(app)
    r = client.get("/api/v1/search", params={"q": "title:tara"}, headers=ah)
    assert r.status_code == 200, r.text
    data = r.json()
    assert sorted(i["id"] for i in data["items"]) == ["a", "bb"]
    r = client.get("/api/v1/search", params={"q": "attendee:swart"}, headers=ah).json()
    assert sorted(i["id"] for i in r["items"]) == ["a", "ccc"]
    assert all(m["field"] == "participants" for i in r["items"] for m in i["matches"])
    r = client.get("/api/v1/search", params={"q": "notes:budget"}, headers=ah).json()
    assert [i["id"] for i in r["items"]] == ["ccc"]
    assert client.get("/api/v1/search", params={"q": "title:"}, headers=ah).status_code == 400
    r = client.get("/api/v1/meetings", params={"q": "title:tara"}, headers=ah).json()
    assert sorted(i["id"] for i in r["items"]) == ["a", "bb"]


# -- page ----------------------------------------------------------------------

import re
import shutil
import subprocess

from meeting_notes.server import web

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

QUERIES = [
    "", "   ", "budget  review", '"budget review" q3', "title:tara", "TITLE:Tara Attendees:bob PERSON:al",
    'attendee:"Tara Swart" notes:budget transcript:"go live"', "foo:bar", "http://x.io title:a", "M-0142",
    "title:", 'title:"" notes:  x', 'attendee:"   "', 'title:"tara sw', '"open phrase', "constructor:x",
    "notes:a transcript:b", "transcript:a transcript:b", 'x"y z"w', "title:a:b",
]


def _node(tmp_path, source):
    script = tmp_path / "t.js"
    script.write_text(source, encoding="utf-8")
    out = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@needs_node
def test_js_parser_matches_python_parser(tmp_path):
    helpers = web._JS_HELPERS_SRC.split("var ICONS")[0]
    got = _node(tmp_path, helpers + f"\nprocess.stdout.write(JSON.stringify({json.dumps(QUERIES)}.map(function(q){{return parseSearchQuery(q);}})));")
    for q, parsed in zip(QUERIES, got):
        py = sq.parse(q)
        assert [(t["field"], t["text"]) for t in parsed["terms"]] == [(t.field, t.text) for t in py.terms], q
        assert parsed["find"] == py.find_text, q


@needs_node
def test_js_find_view_and_ranges(tmp_path):
    helpers = web._JS_HELPERS_SRC.split("var ICONS")[0]
    got = _node(tmp_path, helpers + """
process.stdout.write(JSON.stringify({
  v1: parseSearchQuery('transcript:go').view, v2: parseSearchQuery('notes:a transcript:b').view,
  v3: parseSearchQuery('title:x').view, v4: parseSearchQuery('hello').view, v5: parseSearchQuery('notes:a').view,
  r: findRanges('Tara met tara; TARA?', 'tara'), r2: findRanges('a.b a+b', 'a+b'), r3: findRanges('x', ''),
  r4: findRanges('(x) [y]', '(x)')
}));""")
    assert got["v1"] == "transcript" and got["v5"] == "notes"
    assert got["v2"] == got["v3"] == got["v4"] == ""
    assert got["r"] == [[0, 4], [9, 13], [15, 19]]
    assert got["r2"] == [[4, 7]] and got["r3"] == [] and got["r4"] == [[0, 3]]


def test_sidebar_search_has_help_and_filters():
    page = web.render_transcriptions_page(token_configured=True)
    assert 'id="search-help-btn"' in page and 'id="search-help"' in page
    for key in ("title:", "attendee:", "notes:", "transcript:"):
        assert f'data-insert="{key}"' in page
    assert "title:tara" in page
    assert 'data-short="Search or M-0142"' in page
    assert 'id="search-help-btn"' in web.render_install_page("http://x", token_configured=True)  # every page's sidebar


def test_meetings_page_has_in_meeting_find():
    page = web.render_transcriptions_page(token_configured=True)
    for needle in (
        'id="doc-find"', 'id="find-bar"', 'id="find-input"', 'id="find-count"', 'id="find-prev"', 'id="find-next"',
        "function findRun(", "function findReapply(", "function findStep(", "createTreeWalker", "document.createElement('mark')",
        "surroundContents", "findReapply();}\n}", "pendingFind", "?find=", "e.shiftKey?-1:1", "'Escape'",
        "dialog[open]", "Nothing matches", "q-chip",
    ):
        assert needle in page, needle
    # matches are marked by splitting text nodes only: never by writing the query or the text back as HTML
    find_js = page[page.index("function findRun("): page.index("function findReapply(")]
    assert "innerHTML" not in find_js and "insertAdjacentHTML" not in find_js
    assert "Ctrl+F" in page
    # opened from a deep link
    deep = web.render_transcriptions_page(token_configured=True, initial_session_id="abc")
    assert "get('find')" in deep and "pendingFind = uf" in deep


@needs_node
def test_meetings_page_script_is_valid_javascript(tmp_path):
    page = web.render_transcriptions_page(token_configured=True, initial_session_id="abc")
    scripts = re.findall(r"<script>(.*?)</script>", page, re.S)
    assert scripts
    for i, src in enumerate(scripts):
        f = tmp_path / f"s{i}.js"
        f.write_text(src, encoding="utf-8")
        out = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr
