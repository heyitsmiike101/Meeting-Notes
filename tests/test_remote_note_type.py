"""The remote-control side of note types and auto end: protocol (``remote.py``) and the Recorders page.

The window's own handling is in ``test_note_type_client.py``; the server rule in ``test_note_type_tagging.py``.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from meeting_notes import remote
from meeting_notes.server import web
from meeting_notes.server.web import render_recorders_page, stylesheet_text

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


# -- the protocol ---------------------------------------------------------------------


def test_caps_advertise_note_type_and_auto_end():
    assert {"idle_levels", "note_type", "auto_end"} <= set(remote.CAPS)
    assert remote.clean_caps(["note_type", "auto_end", "bogus", "note_type", 3]) == ("note_type", "auto_end")
    assert len(remote.CAPS) <= remote.MAX_CAPS


def test_set_note_type_is_a_command_with_a_required_id():
    assert remote.clean_command("set_note_type", {"note_type": "quick"}) == ("set_note_type", {"note_type": "quick"})
    assert remote.clean_command("set_note_type", {"note_type": " t0123abc-9_X "})[1] == {"note_type": "t0123abc-9_X"}
    assert remote.clean_command("set_note_type", {"note_type": "a" * 64})[1] == {"note_type": "a" * 64}


@pytest.mark.parametrize(
    "args",
    [None, {}, {"note_type": ""}, {"note_type": "  "}, {"note_type": None}, {"note_type": 5}, {"note_type": ["quick"]},
     {"note_type": "has space"}, {"note_type": "../etc"}, {"note_type": "a" * 65}, {"note_type": "tëst"},
     {"note_type": "q\nuick"}, {"note_type": "quick", "name": "x"}],
)
def test_set_note_type_refuses_bad_arguments(args):
    with pytest.raises(ValueError):
        remote.clean_command("set_note_type", args)


def test_disable_auto_end_takes_no_arguments():
    assert remote.clean_command("disable_auto_end", None) == ("disable_auto_end", {})
    assert remote.clean_command("disable_auto_end", {}) == ("disable_auto_end", {})
    with pytest.raises(ValueError):
        remote.clean_command("disable_auto_end", {"force": True})


def test_the_no_auto_end_refusal_code_exists():
    assert "no_auto_end" in remote.ERROR_CODES


def test_the_server_note_type_ids_fit_the_protocols_id_rule():
    from meeting_notes.server import settings as settings_mod

    for tid in list(settings_mod.BUILTIN_TEMPLATES) + ["t0123456789ab", "a" * 40, "x-y_z"]:
        assert settings_mod._TEMPLATE_ID_RE.match(tid) and remote.valid_note_type(tid)


def test_the_snapshot_carries_note_type_and_auto_end():
    state = remote.sanitize_state({"note_type": "quick", "auto_end": {"mode": "hour", "label": "Auto end at 3:00 PM"}})
    assert state["note_type"] == "quick"
    assert state["auto_end"] == {"mode": "hour", "label": "Auto end at 3:00 PM"}
    assert remote.sanitize_state(state) == state  # stable under a second pass
    silence = remote.sanitize_state({"auto_end": {"mode": "silence", "label": "Auto end after 30 seconds of silence"}})
    assert silence["auto_end"]["mode"] == "silence"


def test_the_snapshot_defaults_and_drops_junk():
    idle = remote.sanitize_state({})
    assert idle["note_type"] is None and idle["auto_end"] == {"mode": None, "label": None}
    assert remote.sanitize_state(None)["auto_end"] == {"mode": None, "label": None}
    for junk in ("quick plus junk", "", "  ", 7, ["quick"], {"id": "quick"}, "x" * 65, "../x"):
        assert remote.sanitize_state({"note_type": junk})["note_type"] is None
    for bad in ({"mode": "manual", "label": "x"}, {"mode": "other"}, {"mode": 3}, "hour", ["hour"], None,
                {"label": "orphan"}):
        assert remote.sanitize_state({"auto_end": bad})["auto_end"] == {"mode": None, "label": None}
    long = remote.sanitize_state({"auto_end": {"mode": "hour", "label": "z" * 500, "extra": 1}})["auto_end"]
    assert set(long) == {"mode", "label"} and len(long["label"]) <= 80
    assert remote.sanitize_state({"auto_end": {"mode": "hour"}})["auto_end"] == {"mode": "hour", "label": None}


# -- the Recorders page ----------------------------------------------------------------


def _card_html():
    js = web._RECORDERS_JS
    return js[js.index("var REC_CARD_HTML"): js.index("function recMakeCard")]


def test_the_card_has_the_note_type_select_between_the_name_and_the_record_button():
    html = _card_html()
    order = ['data-r="name"', 'data-r="type"', 'data-act="record"', 'data-r="autoEnd"', 'data-act="disable_auto_end"']
    positions = [html.index(needle) for needle in order]
    assert positions == sorted(positions), order
    assert '<select class="cw-type" data-r="type" aria-label="Note type"' in html and "hidden" in html.split("data-r=\"type\"")[1].split(">")[0]
    assert ">Disable auto end</button>" in html


def test_the_page_offers_the_select_only_for_a_recorder_with_the_cap_and_two_or_more_types():
    js = web._RECORDERS_JS
    assert "caps.indexOf('note_type') >= 0 && types.length >= 2" in js
    assert "caps.indexOf('auto_end') >= 0" in js
    assert "/v1/note-templates" in js and js.count("fetch('/v1/note-templates'") == 1  # fetched once, then reused
    assert "recLoadTypes();" in js
    assert "function recCaps(item)" in js and "item.caps" in js


def test_the_page_sends_set_note_type_and_disable_auto_end():
    js = web._RECORDERS_JS
    assert "recSend(card, 'set_note_type', 'set_note_type', {note_type: sel.value})" in js
    assert "recSend(card, 'disable_auto_end', 'disable_auto_end', {})" in js
    sent = set(re.findall(r"recSend\(card, [^,]+, '([a-z_]+)'", js))
    assert {"set_note_type", "disable_auto_end"} <= sent <= set(remote.COMMANDS)
    assert "'Note type changed on ' + d + '.'" in js and "'Auto end turned off on ' + d + '.'" in js


def test_the_page_follows_the_recorder_without_clobbering_an_open_select():
    js = web._RECORDERS_JS
    assert "document.activeElement !== r.type && !card._busy.set_note_type" in js
    assert "s.noteType" in js and "noteType: typeof s.note_type === 'string'" in js
    assert "autoEnd: s.auto_end" in js and "ae.label" in js
    # option text goes in through the DOM, never through markup
    assert "o.textContent = t.name" in js and "o.value = t.id" in js
    rhs = re.findall(r"\.innerHTML\s*=\s*([^;]+);", js)
    assert all(re.fullmatch(r"REC_CARD_HTML|REC_ROW_HTML|icon\([^)]*\)", e.strip()) for e in rhs)


def test_the_select_and_strip_are_styled_with_the_clients_tokens():
    css = stylesheet_text()
    block = css[css.index(".cw-type {"): css.index(".cw-rec {")]
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", block) and "box-shadow" not in block
    assert "var(--cl-border-strong)" in block and "height:40px" in block
    assert ".cw-type { height:44px; }" in css and ".cw-type { width:100%; max-width:none; }" in css  # phone


@needs_node
def test_the_page_script_parses_and_the_helpers_work_in_node(tmp_path):
    script = tmp_path / "page.js"
    script.write_text(web._JS_HELPERS + web._RECORDERS_JS, encoding="utf-8")
    checks = tmp_path / "checks.js"
    checks.write_text(
        """(function () {
  recTypes = [{id: 'standard', name: 'Standard'}, {id: 'quick', name: 'Quick notes'}];
  return {
    caps: [recCaps({caps: ['note_type']}), recCaps({}), recCaps(null), recCaps({caps: 'x'})],
    has: [recHasType(recTypes, 'quick'), recHasType(recTypes, 'nope'), recHasType(recTypes, null), recHasType(null, 'x')],
    state: [recState({state: {note_type: 'quick', auto_end: {mode: 'hour', label: 'Auto end at 3:00 PM'}}}),
            recState({state: {note_type: 5, auto_end: 'x'}}), recState(null)],
    toasts: [recToast('set_note_type', {}, 'PC'), recToast('disable_auto_end', {}, 'PC')]
  };
})()""",
        encoding="utf-8",
    )
    harness = tmp_path / "harness.js"
    harness.write_text(
        "const vm = require('vm'), fs = require('fs');\n"
        "const ctx = {window: {MN_ICONS: {}}, console};\nvm.createContext(ctx);\n"
        "vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), ctx);\n"
        "process.stdout.write(JSON.stringify(vm.runInContext(fs.readFileSync(process.argv[3], 'utf8'), ctx)));\n",
        encoding="utf-8",
    )
    done = subprocess.run([NODE, str(harness), str(script), str(checks)], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out["caps"] == [["note_type"], [], [], []]
    assert out["has"] == [True, False, False, False]
    assert out["state"][0]["noteType"] == "quick" and out["state"][0]["autoEnd"]["label"] == "Auto end at 3:00 PM"
    assert out["state"][1]["noteType"] is None and out["state"][1]["autoEnd"] is None
    assert out["state"][2]["noteType"] is None and out["state"][2]["autoEnd"] is None
    assert out["toasts"] == ["Note type changed on PC.", "Auto end turned off on PC."]


def test_the_rendered_page_still_ships_the_new_script():
    page = render_recorders_page(token_configured=True)
    assert "set_note_type" in page and "disable_auto_end" in page and "/v1/note-templates" in page
