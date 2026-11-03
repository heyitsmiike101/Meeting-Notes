"""Co-editing "Your notes" from the Recorders page: the merge (notes_merge.py and its JavaScript twin), ``set_notes``
(remote.py), the server's capability gate (server/recorders.py), the recorder's handling (client/ui/main_window.py) and the page's wiring (recorders.py)."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from meeting_notes import notes_merge, remote
from tests.test_recorders_server import answer_once, env  # noqa: F401 - fixtures
from tests.test_remote_client import _recording, _run, qt_app, window  # noqa: F401 - fixtures

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# -- protocol ------------------------------------------------------------------------------------------------


def test_set_notes_args_are_cleaned_like_the_notes():
    assert remote.clean_command("set_notes", {"text": "a\r\nb\n\n", "base": "  ", "force": True}) == (
        "set_notes", {"text": "a\nb", "base": "", "force": True})
    assert remote.clean_command("set_notes", {"text": "", "base": "x", "force": False}) == (
        "set_notes", {"text": "", "base": "x"})
    for bad in ({"text": 1, "base": ""}, {"base": ""}, {"text": "", "base": "", "force": "yes"}, {"text": "", "base": None}):
        with pytest.raises(ValueError):
            remote.clean_command("set_notes", bad)
    assert remote.CAP_NOTES_EDIT in remote.CAPS and "notes_changed" in remote.ERROR_CODES
    assert remote.MAX_SET_NOTES_BYTES >= 2 * remote.MAX_NOTES_FRAME_BYTES


# -- server --------------------------------------------------------------------------------------------------


def test_set_notes_reaches_a_recorder_that_can_edit(env):  # noqa: F811
    ws = env.recorder(caps=list(remote.CAPS))
    t = answer_once(ws, lambda f: {"ok": True, "code": None, "error": None, "state": {"status": "idle"}})
    big = "x" * 150_000  # bigger than an ordinary command frame
    with env.web() as http:
        r = http.post(remote.command_path(ws.instance_id), json={"command": "set_notes", "args": {"text": big, "base": "old"}})
    t.join(5)
    assert r.status_code == 200 and r.json()["ok"] is True
    assert t.seen["command"] == "set_notes" and t.seen["args"] == {"text": big, "base": "old"}
    ws.close()


def test_set_notes_is_never_sent_to_an_older_recorder(env):  # noqa: F811
    ws = env.recorder(caps=["notes"])
    with env.web() as http:
        r = http.post(remote.command_path(ws.instance_id), json={"command": "set_notes", "args": {"text": "x", "base": ""}})
    assert r.status_code == 400 and "too old to edit its notes" in r.json()["detail"]
    ws.close()


# -- recorder --------------------------------------------------------------------------------------------------


def _pad(window):
    from meeting_notes import scratchpad

    return scratchpad.clean(window.scratchpad.text()) or ""


def test_the_recorder_takes_the_pages_text_and_saves_it(window, tmp_path):  # noqa: F811
    _recording(window, tmp_path)
    saved = []
    window.scratchpad._saver = lambda text: saved.append(text) or True
    window.scratchpad.set_text("first")
    assert _run(window, "set_notes", {"text": "first\n\n- from the page", "base": "first"}) == (True, None, None)
    assert _pad(window) == "first\n\n- from the page" and saved[-1] == "first\n\n- from the page"
    assert window._toast.isHidden()  # the page saves as you type: never a toast
    # emptying the notes is an edit too (saved, so the server hears "no notes")
    assert _run(window, "set_notes", {"text": "", "base": _pad(window)}) == (True, None, None)
    assert _pad(window) == "" and saved[-1] == ""


def test_typing_on_both_sides_at_once_is_merged(window, tmp_path):  # noqa: F811
    _recording(window, tmp_path)
    window.scratchpad._saver = lambda text: True
    base = "Agenda\n\nBudget\n\nHiring"
    # typed here since the page last saw the notes: a line under Agenda
    window.scratchpad.set_text("Agenda\n\nRecorder line\n\nBudget\n\nHiring")
    # the page meanwhile added a line at the end
    ok, code, _error = _run(window, "set_notes", {"text": base + "\n\nPage line", "base": base})
    assert (ok, code) == (True, None)
    assert _pad(window) == "Agenda\n\nRecorder line\n\nBudget\n\nHiring\n\nPage line"
    # ``force`` (an older page's "Keep mine") still replaces the notes outright
    assert _run(window, "set_notes", {"text": "from the page", "base": "anything", "force": True})[0] is True
    assert _pad(window) == "from the page"


def test_a_merge_keeps_the_caret_on_its_line(window, tmp_path):  # noqa: F811
    _recording(window, tmp_path)
    window.scratchpad._saver = lambda text: True
    window.scratchpad.set_text("one\n\ntwo\n\nthree")
    editor = window.scratchpad_pane.editor
    cursor = editor.textCursor()
    cursor.setPosition(window.scratchpad.document.findBlockByNumber(1).position() + 2)  # "tw|o" (a block per paragraph)
    editor.setTextCursor(cursor)
    assert editor.textCursor().block().text() == "two"
    # the page inserts a paragraph above: the caret stays inside "two", at the same column
    assert _run(window, "set_notes", {"text": "zero\n\none\n\ntwo\n\nthree", "base": "one\n\ntwo\n\nthree"})[0] is True
    after = editor.textCursor()
    assert after.block().text() == "two" and after.positionInBlock() == 2
    assert after.position() != after.block().document().characterCount()  # not thrown to the end


def test_the_notes_cannot_change_while_the_recording_finishes(window, tmp_path):  # noqa: F811
    _recording(window, tmp_path)
    window.scratchpad.set_text("kept")
    window.scratchpad.set_locked(True)
    assert _run(window, "set_notes", {"text": "late", "base": "kept"})[:2] == (False, "busy")
    assert _pad(window) == "kept"


def test_typing_here_goes_to_the_server_on_a_short_pause(window, qt_app):  # noqa: F811
    from meeting_notes.client.ui import scratchpad as ui_scratchpad

    sent = []
    window._push_live_notes = lambda text=None: sent.append(window.scratchpad.text())
    window.scratchpad.edited.disconnect()
    window.scratchpad.edited.connect(lambda: window._push_live_notes())
    assert ui_scratchpad.LIVE_DELAY_MS < ui_scratchpad.SAVE_DELAY_MS
    window.scratchpad.document.setPlainText("typed")
    window.scratchpad._live_timer.timeout.emit()
    assert sent == ["typed"]


# -- the merge ---------------------------------------------------------------------------------------------------

MERGE_CASES = [
    # (base, mine, theirs, expected)
    ("a\nb\nc", "a\nb\nc", "a\nb\nc", "a\nb\nc"),
    ("a\nb\nc", "a\nB\nc", "a\nb\nc", "a\nB\nc"),                 # only mine changed
    ("a\nb\nc", "a\nb\nc", "a\nb\nC", "a\nb\nC"),                 # only theirs changed
    ("a\nb\nc", "A\nb\nc", "a\nb\nC", "A\nb\nC"),                 # different lines: both kept
    ("a\nb\nc", "a\nM\nc", "a\nT\nc", "a\nM\nc"),                 # the same line: mine wins it, never the whole text
    ("a\nc", "a\nm\nc", "a\nt\nc", "a\nt\nm\nc"),                 # both inserted at one spot: both, theirs first
    ("a\nb", "a\nb\nmine end", "top\na\nb", "top\na\nb\nmine end"),
    ("a\nb\nc\nd", "a\nB\nc\nd", "a\nb\nC\nd", "a\nB\nC\nd"),     # neighbouring lines are independent edits
    ("x\ny\nz", "x\nz", "x\ny\nz\nw", "x\nz\nw"),                 # a deletion and an append
    ("", "mine", "", "mine"),
    ("", "", "theirs", "theirs"),
    ("", "m", "t", "t\nm"),
    ("- a\n- b", "- a\n- b\n- c", "- a\n- B", "- a\n- B\n- c"),
    ("\n\n", "\nm\n", "\n\nt", "\nm\nt"),                         # blank lines are lines too
]


@pytest.mark.parametrize("base,mine,theirs,expected", MERGE_CASES)
def test_merge3(base, mine, theirs, expected):
    assert notes_merge.merge3(base, mine, theirs) == expected


def test_line_map_follows_a_line_through_a_merge():
    where = notes_merge.line_map(["one", "", "two", "", "three"], ["zero", "", "one", "", "two", "", "three"])
    assert [where(i) for i in range(5)] == [2, 3, 4, 5, 6]
    gone = notes_merge.line_map(["a", "b", "c"], ["a", "c"])
    assert gone(0) == 0 and gone(2) == 1 and gone(1) in (0, 1)
    assert notes_merge.line_map(["a"], [""])(5) == 0


NODE = shutil.which("node")
MERGE_HARNESS = r"""
const vm = require('vm'), fs = require('fs');
const ctx = {window: {MN_ICONS: {}}, console};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), ctx);
const cases = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
ctx.cases = cases;
const out = vm.runInContext(`({
  merged: cases.merge.map(function (c) { return recNotesMerge(c[0], c[1], c[2]); }),
  carets: cases.caret.map(function (c) { return recCaretAfter(c[0], c[1], c[2]); })
})`, ctx);
process.stdout.write(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_page_merges_exactly_like_the_recorder(tmp_path):
    import random

    from meeting_notes.server import web

    rng = random.Random(7)
    words = ["a", "b", "c", "", "- item", "## Head", "d", "e"]

    def edit(lines):
        lines = list(lines)
        for _ in range(rng.randint(1, 3)):
            op = rng.choice("ird")
            at = rng.randint(0, len(lines))
            if op == "i":
                lines.insert(at, rng.choice(words) + str(rng.randint(0, 3)))
            elif lines and op == "r":
                lines[min(at, len(lines) - 1)] = rng.choice(words) + "!"
            elif lines:
                del lines[min(at, len(lines) - 1)]
        return lines

    cases = [list(c[:3]) for c in MERGE_CASES]
    for _ in range(300):
        base = [rng.choice(words) for _ in range(rng.randint(0, 8))]
        cases.append(["\n".join(base), "\n".join(edit(base)), "\n".join(edit(base))])
    carets = [["hello world", "hello brave world", 8], ["hello world", "hello brave world", 2],
              ["abc", "abXc", 3], ["abc", "", 2]]
    script = tmp_path / "page.js"
    script.write_text(web._JS_HELPERS + web._RECORDERS_JS, encoding="utf-8")
    data = tmp_path / "cases.json"
    data.write_text(json.dumps({"merge": cases, "caret": carets}), encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(MERGE_HARNESS, encoding="utf-8")
    done = subprocess.run([NODE, str(harness), str(script), str(data)], capture_output=True, text=True,
                          encoding="utf-8", timeout=60)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    mismatches = [(c, m) for c, m in zip(cases, out["merged"]) if notes_merge.merge3(*c) != m]
    assert not mismatches, mismatches[:3]
    assert out["carets"] == [14, 2, 4, 0]


# -- page ------------------------------------------------------------------------------------------------------


def test_the_page_coedits_only_on_a_recorder_that_can():
    from meeting_notes.server import web

    js = web._RECORDERS_JS
    assert "recCaps(it).indexOf('notes_edit') >= 0" in js
    assert "recPost(card.dataset.id, 'set_notes', {text: text, base: e.base})" in js
    assert "'/v1/recorders/' + encodeURIComponent(id) + '/notes'" in js  # the full text, never the list's preview
    assert 'data-r="notesText"' in js and "REC_NOTES_SAVE_MS = 300" in js
    # the recorder's typing is merged into the box while editing; no conflict strip any more
    assert "recNotesPull(card)" in js and "recNotesMerge(sent, r.notesText.value, theirs)" in js
    assert "Use theirs" not in js and "notesConflict" not in js
    # the text box's value is set from the server's text, never parsed as markup
    assert "box.value = text" in js and "notesText.innerHTML" not in js
    assert json.dumps(remote.CAP_NOTES_EDIT) == '"notes_edit"'
