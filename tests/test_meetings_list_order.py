"""Meetings list: newest first and stable across pages (server), grouped by local day (browser helpers),
the bulk Combine button's explanation, and the Recordings panel following a restarted recorder."""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from meeting_notes.server import splitmerge, splitmerge_ui, web
from meeting_notes.server import store as store_mod

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


# -- server order ------------------------------------------------------------


def _store(tmp_path):
    return store_mod.Store(str(tmp_path / "data"))


def _order(store, per_page=50):
    out, page = [], 1
    while True:
        data = store.list_sessions(page=page, per_page=per_page)
        out.extend(item["session_id"] for item in data["items"])
        if len(out) >= data["total"] or not data["items"]:
            return out
        page += 1


def test_list_is_newest_first_by_start_time_not_id_or_arrival(tmp_path):
    store = _store(tmp_path)
    # ids deliberately unrelated to time; one is a "late upload" of an old recording, one a split part
    for sid, started in (("zz-old", 1_700_000_000.0), ("aa-new", 1_790_000_000.0), ("mm-mid", 1_780_000_000.0),
                         ("m-0001-part-2", 1_785_000_000.0)):
        store.write_session_meta(sid, {"name": sid, "started_wall": started, "created": "2001-01-01T00:00:00"})
    assert _order(store) == ["aa-new", "m-0001-part-2", "mm-mid", "zz-old"]


def test_started_wall_wins_over_a_stale_created_string_and_iso_created_is_the_fallback(tmp_path):
    store = _store(tmp_path)
    store.write_session_meta("a", {"name": "a", "started_wall": 1_790_000_000.0, "created": "1999-01-01T00:00:00"})
    store.write_session_meta("b", {"name": "b", "created": 1_780_000_000.0})  # numeric created, no started_wall
    store.write_session_meta("c", {"name": "c", "created": "2020-05-01T10:00:00"})  # ISO only
    assert _order(store) == ["a", "b", "c"]


def test_equal_start_times_have_one_total_order_so_pages_never_overlap_or_skip(tmp_path):
    store = _store(tmp_path)
    ids = [f"s{n:03d}" for n in range(37)]
    for sid in ids:  # every meeting starts at the same second (e.g. a bulk import)
        store.write_session_meta(sid, {"name": sid, "started_wall": 1_790_000_000.0})
    store.write_session_meta("zlast", {"name": "zlast", "started_wall": 1_700_000_000.0})
    seen = _order(store, per_page=5)
    assert len(seen) == len(set(seen)) == 38
    assert seen == sorted(ids, reverse=True) + ["zlast"]


def test_search_and_state_filters_keep_the_same_order(tmp_path):
    store = _store(tmp_path)
    store.write_session_meta("b", {"name": "Budget old", "started_wall": 1_700_000_000.0})
    store.write_session_meta("a", {"name": "Budget new", "started_wall": 1_790_000_000.0})
    store.write_session_meta("c", {"name": "Other", "started_wall": 1_795_000_000.0})
    data = store.list_sessions(q="Budget")
    assert [i["session_id"] for i in data["items"]] == ["a", "b"]


# -- browser helpers (pure) --------------------------------------------------

HARNESS = r"""
const vm = require('vm'), fs = require('fs');
const ctx = {window: {MN_ICONS: {}}, console};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), ctx);
process.stdout.write(JSON.stringify(vm.runInContext(fs.readFileSync(process.argv[3], 'utf8'), ctx)));
"""

LIST_CHECKS = r"""
(function () {
  // "now" is Wed 30 Sep 2026 09:00 local; epochs are built from LOCAL calendar fields so the test is zone-proof
  var now = new Date(2026, 8, 30, 9, 0, 0);
  function at(y, m, d, h, mi) { return new Date(y, m - 1, d, h, mi || 0, 0).getTime() / 1000; }
  var rows = [
    {session_id: 'r-old-year', created: at(2025, 12, 31, 23, 59)},
    {session_id: 'r-today-early', created: at(2026, 9, 30, 0, 1)},
    {session_id: 'r-yday-late', created: at(2026, 9, 29, 23, 59)},
    {session_id: 'r-yday-early', created: at(2026, 9, 29, 0, 0)},
    {session_id: 'r-mon', created: at(2026, 9, 28, 15, 7)},
    {session_id: 'r-tie-b', created: at(2026, 9, 28, 8, 0)},
    {session_id: 'r-tie-a', created: at(2026, 9, 28, 8, 0)},
    {session_id: 'r-iso', created: '2026-09-27T10:00:00'},
    {session_id: 'r-ms', created: at(2026, 9, 27, 18, 0) * 1000},
    {session_id: 'r-none', created: null},
    {session_id: 'r-junk', created: 'not a date'}
  ];
  var sorted = sortMeetings(rows);
  var groups = groupMeetingsByDay(sorted, now, 'en-US');
  // a later page overlapping an earlier one is merged by the caller; grouping itself must give one header per day
  var shuffled = groupMeetingsByDay(sortMeetings(rows.slice().reverse()), now, 'en-US');
  return {
    order: sorted.map(function (r) { return r.session_id; }),
    groups: groups.map(function (g) { return [g.label, g.rows.map(function (r) { return r.session_id; })]; }),
    sameWhenShuffled: JSON.stringify(groups) === JSON.stringify(shuffled),
    times: [meetingTime({created: 5}), meetingTime({created: '5'}), meetingTime({created: 1.7e12}), meetingTime({}), meetingTime(null)],
    labels: [dayLabel(at(2026, 9, 30, 8), now, 'en-US'), dayLabel(at(2026, 9, 29, 8), now, 'en-US'),
             dayLabel(at(2026, 9, 28, 8), now, 'en-US'), dayLabel(at(2025, 9, 28, 8), now, 'en-US'),
             dayLabel(NaN, now, 'en-US')]
  };
})()
"""


def _run_node(tmp_path, source, checks, tz):
    script = tmp_path / "page.js"
    script.write_text(source, encoding="utf-8")
    chk = tmp_path / "checks.js"
    chk.write_text(checks, encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    env = dict(os.environ, TZ=tz)
    done = subprocess.run([NODE, str(harness), str(script), str(chk)], capture_output=True, text=True,
                          encoding="utf-8", timeout=60, env=env)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@needs_node
@pytest.mark.parametrize("tz", ["UTC", "America/New_York", "Pacific/Auckland"])
def test_sort_and_day_grouping_helpers(tmp_path, tz):
    out = _run_node(tmp_path, web._JS_HELPERS, LIST_CHECKS, tz)
    assert out["order"] == [
        "r-today-early", "r-yday-late", "r-yday-early", "r-mon",
        "r-tie-b", "r-tie-a",  # same start time: larger id first, like the server's ORDER BY ... session_id DESC
        "r-ms", "r-iso",       # the 27th: 18:00 (millisecond epoch) before 10:00 (ISO text)
        "r-old-year",
        "r-none", "r-junk",    # unknown dates sort last
    ]
    labels = [g[0] for g in out["groups"]]
    assert labels == ["Today", "Yesterday", "Monday, Sep 28", "Sunday, Sep 27", "Wednesday, Dec 31, 2025", "Unknown date"]
    by_label = dict((g[0], g[1]) for g in out["groups"])
    assert by_label["Yesterday"] == ["r-yday-late", "r-yday-early"]  # 23:59 and 00:00 are the same local day
    assert by_label["Monday, Sep 28"] == ["r-mon", "r-tie-b", "r-tie-a"]
    assert sorted(by_label["Unknown date"]) == ["r-junk", "r-none"]
    assert out["sameWhenShuffled"] is True
    assert out["times"][0] == 5 and out["times"][1] == 5 and out["times"][2] == 1.7e9
    assert out["times"][3] is None and out["times"][4] is None  # NaN serialises as null
    assert out["labels"] == ["Today", "Yesterday", "Monday, Sep 28", "Sunday, Sep 28, 2025", "Unknown date"]


def test_meetings_page_renders_day_groups_in_one_list():
    page = web.render_transcriptions_page(token_configured=True)
    assert 'id="rows"' in page and page.count('id="rows"') == 1
    for needle in ("function renderMeetingList()", "groupMeetingsByDay(sortMeetings(listState.items)", "dayHeadMarkup",
                   'class="day-head"', "day-title", "if (ticket !== listState.seq) return true;"):
        assert needle in page, needle
    # rows are reconciled, never prepended in arbitrary order
    assert "afterbegin" not in page
    css = web.stylesheet_text()
    assert ".day-head" in css and ".day-title" in css
    assert ".day-head { padding:24px 4px 6px; }" in css  # phone


# -- bulk Combine ------------------------------------------------------------


def test_bulk_combine_limit_matches_the_server_and_explains_itself():
    js = splitmerge_ui.JS
    assert f"CB_MAX = {splitmerge.COMBINE_MAX};" in js
    for needle in ("function combineBlocker(ids)", "Select two or more meetings to combine", "has not finished transcribing",
                   "Audio was deleted for", "btn.title = why ||", "Cannot combine: "):
        assert needle in js, needle
    # the existing dialog is reused: chronological order and clear-selection-then-refresh live in openCombine/cbSubmit
    assert "openCombine(selectedIds())" in js
    assert "box.checked = false" in js and "loadRows(true)" in js


COMBINE_CHECKS = r"""
(function () {
  var rowInfo = {
    ok1: {session_id: 'ok1', name: 'One', has_audio: true, pipeline: {transcription: {state: 'complete'}}},
    ok2: {session_id: 'ok2', name: 'Two', has_audio: true, pipeline: {transcription: {state: 'complete'}}},
    busy: {session_id: 'busy', name: 'Busy', has_audio: true, pipeline: {transcription: {state: 'running', percent: 10}}},
    noaudio: {session_id: 'noaudio', name: 'Gone', has_audio: false, pipeline: {transcription: {state: 'complete'}}}
  };
  var many = []; for (var i = 0; i < 21; i++) { many.push('ok1'); }
  return [combineBlocker(['ok1']), combineBlocker(['ok1', 'ok2']), combineBlocker(['ok1', 'busy']),
          combineBlocker(['ok1', 'noaudio']), combineBlocker(many), combineBlocker(['ok1', 'unloaded'])];
})()
"""


@needs_node
def test_combine_blocker_reasons(tmp_path):
    # combineBlocker reads the page globals rowInfo / processingState, so load the helpers and the function alone
    block = splitmerge_ui.JS[splitmerge_ui.JS.index("/* Why the current selection"):]
    block = block[: block.index("(function () {")]
    source = web._JS_HELPERS + "\nvar CB_MAX = 20; var rowInfo = {};\n" + block
    checks = COMBINE_CHECKS.replace("var rowInfo = {", "rowInfo = {")
    out = _run_node(tmp_path, source, checks, "UTC")
    assert out[0] == "Select two or more meetings to combine"
    assert out[1] == ""
    assert out[2] == "Busy has not finished transcribing"
    assert out[3] == "Audio was deleted for Gone; combining needs audio"
    assert out[4] == "At most 20 meetings can be combined at once"
    assert out[5] == ""  # a row that is not loaded yet is left for the dialog to check


# -- Recordings panel follows a restarted recorder ---------------------------

REBIND_CHECKS = r"""
(function () {
  function it(id, device, platform, at) { return {instance_id: id, device: device, platform: platform, connected_at: at}; }
  var prev = it('old', 'DESKTOP-ABC', 'windows', 100);
  return {
    one: recFindRestarted(prev, [it('new', 'desktop-abc ', 'windows', 200)]),
    twoMatches: recFindRestarted(prev, [it('n1', 'DESKTOP-ABC', 'windows', 200), it('n2', 'DESKTOP-ABC', 'windows', 300)]),
    otherMachine: recFindRestarted(prev, [it('x', 'LAPTOP', 'windows', 200)]),
    otherOs: recFindRestarted(prev, [it('x', 'DESKTOP-ABC', 'macos', 200)]),
    olderTwin: recFindRestarted(prev, [it('twin', 'DESKTOP-ABC', 'windows', 50)]),
    twinPlusRestart: recFindRestarted(prev, [it('twin', 'DESKTOP-ABC', 'windows', 50), it('new', 'DESKTOP-ABC', 'windows', 200)]),
    sameId: recFindRestarted(prev, [it('old', 'DESKTOP-ABC', 'windows', 100)]),
    blankDevice: recFindRestarted(it('o', '', 'windows', 1), [it('n', '', 'windows', 2)]),
    unknownPlatform: recFindRestarted(it('o', 'PC', '', 1), [it('n', 'pc', 'macos', 2)]),
    noPrev: recFindRestarted(null, [it('n', 'PC', 'windows', 2)]),
    junk: recFindRestarted(prev, [null, {}, it('', 'DESKTOP-ABC', 'windows', 5)]),
    empty: recFindRestarted(prev, [])
  };
})()
"""


@needs_node
def test_recorder_restart_matching_helper(tmp_path):
    out = _run_node(tmp_path, web._JS_HELPERS + web._RECORDERS_JS, REBIND_CHECKS, "UTC")
    assert out["one"] == "new"                      # device compared case-insensitively, trimmed
    assert out["twoMatches"] is None                # ambiguous: do not guess
    assert out["otherMachine"] is None
    assert out["otherOs"] is None
    assert out["olderTwin"] is None                 # was already connected before the old instance: a twin, not a restart
    assert out["twinPlusRestart"] is None           # an older same-named computer is live too: any pick would be a guess
    assert out["sameId"] is None
    assert out["blankDevice"] is None               # no device name, no identity
    assert out["unknownPlatform"] == "n"            # a missing OS family does not block the match
    assert out["noPrev"] is None
    assert out["junk"] is None
    assert out["empty"] is None


def test_recordings_panel_rebinds_and_ignores_answers_for_the_old_id():
    js = web._RECORDERS_JS
    assert "function recFindRestarted(prev, items)" in js
    assert "P.id = next;" in js and "recPanelLoad();" in js
    assert "var asked = P.id;" in js and "if (asked !== P.id) { P.again = true; return; }" in js
    assert "prob.offline && asked === P.id" in js
