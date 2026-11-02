"""Editing "Your notes" from the Recorders page: ``set_notes`` (remote.py), the server's capability gate
(server/recorders.py), the recorder's handling (client/ui/main_window.py) and the page's wiring (recorders.py)."""

from __future__ import annotations

import json

import pytest

from meeting_notes import remote
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
    # the next save starts from what the page last sent, even if the editor wrote it differently
    window.scratchpad.set_text("first\n\nas the editor wrote it")
    window._server_notes = ("as the page sent it", _pad(window))
    assert _run(window, "set_notes", {"text": "second", "base": "as the page sent it"})[0] is True
    assert _pad(window) == "second"
    # emptying the notes is an edit too (saved, so the server hears "no notes")
    assert _run(window, "set_notes", {"text": "", "base": "second"}) == (True, None, None)
    assert _pad(window) == "" and saved[-1] == ""


def test_typing_on_the_recorder_meanwhile_is_never_overwritten_unless_forced(window, tmp_path):  # noqa: F811
    _recording(window, tmp_path)
    window.scratchpad._saver = lambda text: True
    window.scratchpad.set_text("typed here since")
    ok, code, _error = _run(window, "set_notes", {"text": "from the page", "base": "what the page started from"})
    assert (ok, code) == (False, "notes_changed") and _pad(window) == "typed here since"
    assert _run(window, "set_notes", {"text": "from the page", "base": "what the page started from", "force": True})[0] is True
    assert _pad(window) == "from the page"


def test_the_notes_cannot_change_while_the_recording_finishes(window, tmp_path):  # noqa: F811
    _recording(window, tmp_path)
    window.scratchpad.set_text("kept")
    window.scratchpad.set_locked(True)
    assert _run(window, "set_notes", {"text": "late", "base": "kept"})[:2] == (False, "busy")
    assert _pad(window) == "kept"


# -- page ------------------------------------------------------------------------------------------------------


def test_the_page_edits_the_notes_only_on_a_recorder_that_can():
    from meeting_notes.server import web

    js = web._RECORDERS_JS
    assert "recCaps(it).indexOf('notes_edit') >= 0" in js
    assert "recPost(card.dataset.id, 'set_notes', args)" in js and "{text: text, base: e.base}" in js
    assert "res.code === 'notes_changed'" in js and "Use theirs" in js and "Keep mine" in js
    assert "'/v1/recorders/' + encodeURIComponent(id) + '/notes'" in js  # the full text, never the list's preview
    assert 'data-r="notesText"' in js and "REC_NOTES_SAVE_MS = 800" in js
    # the text box's value is set from the server's text, never parsed as markup
    assert "r.notesText.value = text" in js and "notesText.innerHTML" not in js
    assert json.dumps(remote.CAP_NOTES_EDIT) == '"notes_edit"'
