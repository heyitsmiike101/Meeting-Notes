"""The "Your notes" editor: a native port of Life Dash's notes editor (key engine, autoformat, toolbar, "/" menu, links).

Headless (Qt's offscreen platform); keystrokes go through ``QTest`` so the real ``keyPressEvent`` runs.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6", reason="PySide6 not installed")

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtGui import QDesktopServices, QTextBlockFormat, QTextCursor  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from meeting_notes.client.ui import notes_doc as nd  # noqa: E402
from meeting_notes.client.ui.notes_editor import BLOCKS, OPEN_HINT  # noqa: E402
from meeting_notes.client.ui.scratchpad import ScratchpadModel, ScratchpadPopout, ScratchpadView  # noqa: E402
from meeting_notes.client.ui.theme import APP_STYLE  # noqa: E402

CTRL, SHIFT = Qt.ControlModifier, Qt.ShiftModifier


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(APP_STYLE)
    return app


@pytest.fixture
def view(qt_app):
    model = ScratchpadModel(lambda text: True)
    pane = ScratchpadView(model, closable=False)
    pane.resize(340, 520)
    pane.show()
    qt_app.processEvents()
    pane.editor.setFocus()
    yield pane
    pane.close()


def _type(view, text):
    QTest.keyClicks(view.editor, text)


def _key(view, key, mods=Qt.NoModifier):
    QTest.keyClick(view.editor, key, mods)


def _md(view) -> str:
    return view.model.text()


def _blocks(view):
    block = view.model.document.begin()
    while block.isValid():
        yield block
        block = block.next()


def _table(view):
    for block in _blocks(view):
        table = QTextCursor(block).currentTable()
        if table is not None:
            return table
    return None


def _caret_block(view):
    return view.editor.textCursor().block()


def _load(view, text, caret_at_end=True):
    view.model.set_text(text)
    if caret_at_end:
        view.editor.moveCursor(QTextCursor.End)
    view.editor.setFocus()


# -- Enter ----------------------------------------------------------------------------------------------------


def test_enter_splits_a_paragraph(view):
    _type(view, "hello world")
    for _ in range(5):
        _key(view, Qt.Key_Left)
    _key(view, Qt.Key_Return)
    assert _md(view) == "hello\n\nworld"  # the space at the edge is trimmed, as in Life Dash


def test_enter_after_a_heading_gives_a_paragraph(view):
    _load(view, "## Title")
    _key(view, Qt.Key_Return)
    _type(view, "body")
    assert _md(view) == "## Title\n\nbody"
    assert nd.block_kind(_caret_block(view)) == "paragraph"


def test_enter_at_the_start_of_a_heading_pushes_it_down(view):
    _load(view, "## Title", caret_at_end=False)
    view.editor.moveCursor(QTextCursor.Start)
    _key(view, Qt.Key_Return)
    assert nd.block_kind(_caret_block(view)) == "h2" and _caret_block(view).text() == "Title"
    assert [b.text() for b in _blocks(view)] == ["", "Title"]


def test_enter_in_a_list_makes_a_new_item_and_a_todo_stays_a_todo(view):
    _load(view, "- [x] one")
    _key(view, Qt.Key_Return)
    _type(view, "two")
    assert _md(view) == "- [x] one\n- [ ] two"  # the new to-do starts unchecked
    _load(view, "1. a")
    _key(view, Qt.Key_Return)
    _type(view, "b")
    assert _md(view) == "1. a\n2. b"


def test_enter_on_an_empty_item_leaves_the_list(view):
    _load(view, "- a\n- b")
    _key(view, Qt.Key_Return)
    _key(view, Qt.Key_Return)
    assert nd.block_kind(_caret_block(view)) == "paragraph"
    _type(view, "after")
    assert _md(view) == "- a\n- b\n\nafter"


def test_enter_on_an_empty_nested_item_goes_one_level_out(view):
    _load(view, "- a\n  - b")
    _key(view, Qt.Key_Return)
    assert nd.item_level(_caret_block(view)) == 1
    _key(view, Qt.Key_Return)
    assert nd.item_level(_caret_block(view)) == 0
    _type(view, "c")
    assert _md(view) == "- a\n  - b\n- c"


def test_enter_in_a_code_block_adds_a_line_inside_it(view):
    _load(view, "```python\nx = 1\n```")
    _key(view, Qt.Key_Return)
    _type(view, "y = 2")
    assert _md(view) == "```python\nx = 1\ny = 2\n```"
    assert len({nd.code_of(b) for b in _blocks(view)}) == 1


def test_enter_after_a_divider_makes_a_paragraph(view):
    _load(view, "a\n\n---\n\nb")
    cursor = view.editor.textCursor()
    cursor.setPosition(view.model.document.findBlockByNumber(1).position())
    view.editor.setTextCursor(cursor)
    _key(view, Qt.Key_Return)
    assert nd.block_kind(_caret_block(view)) == "paragraph" and _caret_block(view).previous().text() == ""
    assert nd.is_rule(view.model.document.findBlockByNumber(1))


def test_enter_on_the_last_empty_quote_line_leaves_the_quote(view):
    _load(view, "> a")
    _key(view, Qt.Key_Return)
    assert nd.quote_of(_caret_block(view))
    _key(view, Qt.Key_Return)
    assert not nd.quote_of(_caret_block(view))
    _type(view, "out")
    assert _md(view) == "> a\n\nout"


def test_shift_enter_is_a_hard_break_stored_as_a_backslash(view):
    _type(view, "one")
    _key(view, Qt.Key_Return, SHIFT)
    _type(view, "two")
    assert view.model.document.blockCount() == 1
    assert _md(view) == "one\\\ntwo"
    _load(view, "- item")
    _key(view, Qt.Key_Return, SHIFT)
    _type(view, "more")
    assert _md(view) == "- item\\\n  more"
    _load(view, "## head")
    _key(view, Qt.Key_Return, SHIFT)
    _type(view, "x")
    assert _md(view) == "## headx"  # a heading has no break


# -- Backspace / Delete -----------------------------------------------------------------------------------------


def test_backspace_at_the_start_turns_a_heading_into_a_paragraph(view):
    _load(view, "## Title", caret_at_end=False)
    view.editor.moveCursor(QTextCursor.Start)
    _key(view, Qt.Key_Backspace)
    assert _md(view) == "Title" and nd.block_kind(_caret_block(view)) == "paragraph"


def test_backspace_climbs_the_ladder_todo_bullet_paragraph(view):
    _load(view, "- [ ] item", caret_at_end=False)
    view.editor.moveCursor(QTextCursor.Start)
    _key(view, Qt.Key_Backspace)
    assert _md(view) == "- item"
    _key(view, Qt.Key_Backspace)
    assert _md(view) == "item"


def test_backspace_on_an_empty_item_deletes_it_and_lands_on_the_item_above(view):
    _load(view, "- a\n- b")
    _key(view, Qt.Key_Return)
    _key(view, Qt.Key_Backspace)
    assert _md(view) == "- a\n- b" and _caret_block(view).text() == "b"
    assert view.editor.textCursor().atBlockEnd()


def test_backspace_at_the_start_of_a_nested_item_goes_one_level_out(view):
    _load(view, "- a\n  - b", caret_at_end=False)
    cursor = view.editor.textCursor()
    cursor.setPosition(view.model.document.findBlockByNumber(1).position())
    view.editor.setTextCursor(cursor)
    _key(view, Qt.Key_Backspace)
    assert _md(view) == "- a\n- b"


def test_backspace_takes_the_first_quote_line_out_of_the_quote(view):
    _load(view, "> a\n>\n> b", caret_at_end=False)
    view.editor.moveCursor(QTextCursor.Start)
    _key(view, Qt.Key_Backspace)
    assert _md(view) == "a\n\n> b"


def test_backspace_in_a_code_block(view):
    _load(view, "```\na\n```")
    _key(view, Qt.Key_Return)  # an empty last line
    _key(view, Qt.Key_Backspace)
    assert nd.block_kind(_caret_block(view)) == "paragraph" and _md(view) == "```\na\n```"  # trailing blank dropped
    _load(view, "```\nx\ny\n```", caret_at_end=False)
    view.editor.moveCursor(QTextCursor.Start)
    _key(view, Qt.Key_Backspace)  # the start of the block turns it all into text
    assert _md(view) == "x\\\ny"


def test_backspace_right_after_a_divider_deletes_it(view):
    _load(view, "a\n\n---\n\nb")
    cursor = view.editor.textCursor()
    cursor.setPosition(view.model.document.findBlockByNumber(2).position())
    view.editor.setTextCursor(cursor)
    _key(view, Qt.Key_Backspace)
    assert _md(view) == "a\n\nb"


def test_backspace_joins_a_paragraph_onto_the_one_above(view):
    _load(view, "ab\n\ncd", caret_at_end=False)
    cursor = view.editor.textCursor()
    cursor.setPosition(view.model.document.findBlockByNumber(1).position())
    view.editor.setTextCursor(cursor)
    _key(view, Qt.Key_Backspace)
    assert _md(view) == "abcd"


def test_delete_at_the_end_joins_and_removes_a_following_divider(view):
    _load(view, "ab\n\ncd", caret_at_end=False)
    view.editor.moveCursor(QTextCursor.Start)
    view.editor.moveCursor(QTextCursor.EndOfBlock)
    _key(view, Qt.Key_Delete)
    assert _md(view) == "abcd"
    _load(view, "a\n\n---\n\nb", caret_at_end=False)
    view.editor.moveCursor(QTextCursor.Start)
    view.editor.moveCursor(QTextCursor.EndOfBlock)
    _key(view, Qt.Key_Delete)
    assert _md(view) == "a\n\nb"


def test_select_all_and_delete_leaves_one_empty_paragraph(view):
    _load(view, "# T\n\n- a\n- b\n\n> q\n\n```\nc\n```")
    _key(view, Qt.Key_A, CTRL)
    _key(view, Qt.Key_Delete)
    assert _md(view) == "" and view.model.document.blockCount() == 1
    assert nd.block_kind(_caret_block(view)) == "paragraph" and not nd.quote_of(_caret_block(view))
    _type(view, "fresh")
    assert _md(view) == "fresh"


def test_select_all_and_delete_clears_a_table_too(view):
    _load(view, "a\n\n| x |\n| --- |\n| 1 |")
    _key(view, Qt.Key_A, CTRL)
    _key(view, Qt.Key_Backspace)
    assert _md(view) == "" and view.model.document.blockCount() == 1


# -- Tab ----------------------------------------------------------------------------------------------------------


def test_tab_and_shift_tab_indent_and_outdent_a_list_item(view):
    _load(view, "- a\n- b\n- c", caret_at_end=False)
    cursor = view.editor.textCursor()
    cursor.setPosition(view.model.document.findBlockByNumber(1).position())
    view.editor.setTextCursor(cursor)
    _key(view, Qt.Key_Tab)
    assert _md(view) == "- a\n  - b\n- c"
    _key(view, Qt.Key_Tab)  # nothing above it to go under: stays
    assert _md(view) == "- a\n  - b\n- c"
    _key(view, Qt.Key_Backtab, SHIFT)
    assert _md(view) == "- a\n- b\n- c"


def test_tab_outside_a_list_moves_focus_like_before(view, monkeypatch):
    moves = []
    monkeypatch.setattr(view.editor, "focusNextPrevChild", lambda forward: moves.append(forward) or True)
    _type(view, "text")
    _key(view, Qt.Key_Tab)
    _key(view, Qt.Key_Backtab, SHIFT)
    assert moves == [True, False] and _md(view) == "text"  # no tab character typed


def test_tab_moves_between_table_cells_and_adds_a_row_at_the_end(view):
    _load(view, "| a | b |\n| --- | --- |\n| c | d |", caret_at_end=False)
    table = _table(view)
    view.editor.setTextCursor(table.cellAt(0, 0).firstCursorPosition())
    _key(view, Qt.Key_Tab)
    assert view.editor.textCursor().block().text() == "b"
    _key(view, Qt.Key_Tab)
    assert view.editor.textCursor().block().text() == "c"
    _key(view, Qt.Key_Backtab, SHIFT)
    assert view.editor.textCursor().block().text() == "b"
    view.editor.setTextCursor(table.cellAt(1, 1).firstCursorPosition())
    _key(view, Qt.Key_Tab)
    assert table.rows() == 3
    _type(view, "new")
    assert _md(view).endswith("| new |  |")


# -- inline autoformat ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("typed, expected", [
    ("**bold**", "**bold**"), ("*it*", "*it*"), ("_it_", "*it*"), ("`code`", "`code`"), ("~~gone~~", "~~gone~~"),
    ("a **b** c", "a **b** c"),
])
def test_closing_a_marker_formats_the_span(view, typed, expected):
    _type(view, typed)
    assert _md(view) == expected
    assert view.editor.toPlainText() == typed.replace("**", "").replace("~~", "").replace("`", "").replace("*", "").replace("_", "")


def test_the_converted_span_is_really_formatted_and_typing_continues_outside_it(view):
    _type(view, "**bold**")
    _type(view, " after")
    block = view.model.document.begin()
    parts = [(it.fragment().text(), it.fragment().charFormat().fontWeight()) for it in block]
    assert parts == [("bold", 700), (" after", 400)]


def test_snake_case_and_stars_in_prose_stay_text(view):
    _type(view, "use snake_case_name and 3*4*5 and a * b")
    assert view.editor.toPlainText() == "use snake_case_name and 3*4*5 and a * b"
    assert all(it.fragment().charFormat().fontWeight() == 400 and not it.fragment().charFormat().fontItalic()
               for it in view.model.document.begin())


def test_autoformat_is_one_undo_step(view):
    _type(view, "**bold**")
    assert view.editor.toPlainText() == "bold"
    view.editor.undo()
    assert view.editor.toPlainText() == "**bold**" and _md(view).startswith("\\*\\*bold")


def test_autoformat_does_not_fire_in_code_blocks_or_inline_code_or_links(view):
    _load(view, "```\nx\n```")
    _type(view, " **no**")
    assert view.editor.toPlainText().endswith("**no**")
    _load(view, "[a](https://x.io)")
    _type(view, "**q**")
    assert view.editor.toPlainText().endswith("**q**")  # typed at the end of a link: still the link's text


# -- commands and the toolbar --------------------------------------------------------------------------------------------


def test_toolbar_has_lifedash_buttons_in_order_with_its_tooltips(view):
    tips = [(k, b.toolTip()) for k, b in view.toolbar.buttons.items()]
    assert tips == [
        ("bold", "Bold (Ctrl+B)"), ("italic", "Italic (Ctrl+I)"), ("strike", "Strikethrough"), ("h1", "Heading 1"),
        ("h2", "Heading 2"), ("h3", "Heading 3"), ("text", "Paragraph"), ("bullet", "Bulleted list"),
        ("number", "Numbered list"), ("task", "To-do list"), ("quote", "Quote"), ("codeblock", "Code block"),
        ("code", "Inline code"), ("link", "Link"),
    ]
    assert not hasattr(view.toolbar, "hint")  # Life Dash's note editor has no "type / for blocks" line (hint=False)
    assert all(b.focusPolicy() == Qt.NoFocus for b in view.toolbar.buttons.values())
    assert view.toolbar.buttons["bold"].objectName() == "noteTool"


def test_toolbar_wraps_inside_the_narrow_pane(view):
    one_row = view.toolbar.layout().sizeHint().height()
    assert view.toolbar.height() > one_row  # 340px cannot hold the whole bar on one line


def test_block_buttons_toggle(view):
    _type(view, "text")
    view.toolbar.buttons["h1"].click()
    assert _md(view) == "# text"
    view.toolbar.buttons["h1"].click()
    assert _md(view) == "text"
    view.toolbar.buttons["bullet"].click()
    assert _md(view) == "- text"
    view.toolbar.buttons["number"].click()
    assert _md(view) == "1. text"
    view.toolbar.buttons["number"].click()
    assert _md(view) == "text"
    view.toolbar.buttons["task"].click()
    assert _md(view) == "- [ ] text"
    view.toolbar.buttons["task"].click()  # Lifedash toggleTask: off leaves a bullet
    assert _md(view) == "- text"
    view.toolbar.buttons["text"].click()
    assert _md(view) == "text"
    view.toolbar.buttons["quote"].click()
    assert _md(view) == "> text"
    view.toolbar.buttons["quote"].click()
    assert _md(view) == "text"
    view.toolbar.buttons["codeblock"].click()
    assert _md(view) == "```\ntext\n```"
    view.toolbar.buttons["codeblock"].click()
    assert _md(view) == "text"


def test_block_commands_apply_to_every_selected_block(view):
    _load(view, "a\n\nb\n\nc")
    _key(view, Qt.Key_A, CTRL)
    view.editor.run_command("bullet")
    assert _md(view) == "- a\n- b\n- c"
    view.editor.run_command("quote")
    assert _md(view) == "> - a\n> - b\n> - c"
    assert len({nd.quote_of(b) for b in _blocks(view)}) == 1  # one quote container, not one per line
    view.editor.run_command("quote")
    view.editor.run_command("bullet")
    assert _md(view) == "a\n\nb\n\nc"


def test_inline_buttons_format_the_selection_and_toggle_off(view):
    _type(view, "make bold")
    _key(view, Qt.Key_A, CTRL)
    view.toolbar.buttons["bold"].click()
    assert _md(view) == "**make bold**"
    view.toolbar.buttons["italic"].click()
    assert _md(view) == "***make bold***"
    view.toolbar.buttons["strike"].click()
    view.toolbar.buttons["code"].click()
    assert "~~" in _md(view) and "`" in _md(view)
    view.toolbar.buttons["code"].click()
    view.toolbar.buttons["strike"].click()
    view.toolbar.buttons["italic"].click()
    view.toolbar.buttons["bold"].click()
    assert _md(view) == "make bold"


def test_an_inline_command_with_no_selection_sets_the_typing_format(view):
    _key(view, Qt.Key_B, CTRL)
    _type(view, "bold")
    _key(view, Qt.Key_B, CTRL)
    _type(view, " plain")
    assert _md(view) == "**bold** plain"


def test_toolbar_lights_the_buttons_that_are_on_at_the_caret(view):
    on = lambda key: bool(view.toolbar.buttons[key].property("on"))  # noqa: E731
    _load(view, "# **big** plain")
    view.editor.moveCursor(QTextCursor.Start)
    view.editor.moveCursor(QTextCursor.Right)
    assert on("h1") and on("bold") and not on("h2") and not on("bullet")
    view.editor.moveCursor(QTextCursor.End)
    assert on("h1") and not on("bold")
    _load(view, "- [ ] todo\n\n> quote\n\n```\ncode\n```\n\n1. n")
    view.editor.moveCursor(QTextCursor.Start)
    assert on("task") and not on("bullet")
    view.editor.moveCursor(QTextCursor.NextBlock)
    assert on("quote") and not on("task")
    view.editor.moveCursor(QTextCursor.NextBlock)
    assert on("codeblock") and not on("quote")
    view.editor.moveCursor(QTextCursor.End)
    assert on("number") and not on("codeblock")


def test_state_changed_carries_the_block_kind(view):
    seen = []
    view.editor.state_changed.connect(seen.append)
    view.editor.run_command("h2")
    assert seen and seen[-1]["block"] == "h2" and seen[-1]["h2"] is True


def test_table_and_divider_commands(view):
    _type(view, "intro")
    view.editor.run_command("divider")
    assert _md(view) == "intro\n\n---"
    assert nd.block_kind(_caret_block(view)) == "paragraph"  # a paragraph after it to keep writing in
    _type(view, "next")
    view.editor.run_command("table")
    table = view.editor.textCursor().currentTable()
    assert table is not None and (table.rows(), table.columns()) == (3, 3)
    assert view.editor.textCursor().block().text() == ""
    assert _md(view).endswith("|  |  |  |\n| --- | --- | --- |\n|  |  |  |\n|  |  |  |")
    _type(view, "h")
    _key(view, Qt.Key_Tab)
    _type(view, "i")
    assert "| h | i |" in _md(view)
    assert view.editor.run_command("table") is False  # no table in a table


def test_divider_in_an_empty_paragraph_replaces_it(view):
    view.editor.run_command("divider")
    kinds = [nd.block_kind(b) for b in _blocks(view)]
    assert kinds == ["rule", "paragraph"]


def test_typing_on_a_divider_writes_a_paragraph_there(view):
    _load(view, "a\n\n---\n\nb", caret_at_end=False)
    cursor = view.editor.textCursor()
    cursor.setPosition(view.model.document.findBlockByNumber(1).position())
    view.editor.setTextCursor(cursor)
    _type(view, "x")
    assert _md(view) == "a\n\nx\n\nb"


def test_indent_and_outdent_commands(view):
    _load(view, "- a\n- b")
    assert view.editor.run_command("indent")
    assert _md(view) == "- a\n  - b"
    assert view.editor.run_command("outdent")
    assert _md(view) == "- a\n- b"
    assert not view.editor.run_command("outdent")


def test_headings_use_the_larger_sizes(view):
    _type(view, "x")
    view.editor.run_command("h1")
    block = view.model.document.begin()
    assert block.begin().fragment().charFormat().property(nd.QTextFormat.FontPixelSize) == 22
    view.editor.run_command("h2")
    assert block.begin().fragment().charFormat().property(nd.QTextFormat.FontPixelSize) == 18
    view.editor.run_command("text")
    assert block.begin().fragment().charFormat().property(nd.QTextFormat.FontPixelSize) is None


# -- shortcuts ---------------------------------------------------------------------------------------------------------


def test_shortcuts(view):
    _type(view, "x")
    _key(view, Qt.Key_A, CTRL)
    _key(view, Qt.Key_B, CTRL)
    assert _md(view) == "**x**"
    _key(view, Qt.Key_B, CTRL)
    _key(view, Qt.Key_I, CTRL)
    assert _md(view) == "*x*"
    _key(view, Qt.Key_I, CTRL)
    _key(view, Qt.Key_E, CTRL)
    assert _md(view) == "`x`"
    _key(view, Qt.Key_E, CTRL)
    _key(view, Qt.Key_X, CTRL | SHIFT)
    assert _md(view) == "~~x~~"
    _key(view, Qt.Key_X, CTRL | SHIFT)
    assert _md(view) == "x"


@pytest.mark.parametrize("key, expected", [
    (Qt.Key_1, "# x"), (Qt.Key_2, "## x"), (Qt.Key_3, "### x"), (Qt.Key_4, "- [ ] x"), (Qt.Key_5, "- x"),
    (Qt.Key_6, "1. x"), (Qt.Key_8, "```\nx\n```"), (Qt.Key_0, "x"),
])
def test_turn_into_shortcuts(view, key, expected):
    _type(view, "x")
    view.editor.run_command("h3")
    _key(view, key, CTRL | SHIFT)
    assert _md(view) == expected
    if key != Qt.Key_0:
        _key(view, key, CTRL | SHIFT)  # turn-into sets the kind, it does not toggle
        assert _md(view) == expected


def test_shifted_symbol_keys_work_too(view):
    _type(view, "x")
    _key(view, Qt.Key_Exclam, CTRL | SHIFT)
    assert _md(view) == "# x"


# -- the "/" menu ---------------------------------------------------------------------------------------------------------


def test_slash_menu_lists_the_blocks_in_order(view):
    _type(view, "/")
    menu = view.editor._slash
    assert menu.isVisible() and menu.objectName() == "noteSlash"
    assert menu.visible_keys() == [b[0] for b in BLOCKS]
    labels = [(i.label, i.hint.text()) for i in menu.items]
    assert labels == [
        ("Text", "Plain paragraph"), ("Heading 1", "Big"), ("Heading 2", "Smaller"), ("Heading 3", "Smaller still"),
        ("Bulleted list", "• item"), ("Numbered list", "1. item"), ("To-do list", "☐ task"), ("Quote", "Indented"),
        ("Code block", "Fenced"), ("Table", "3 x 3, Tab moves"), ("Divider", "Horizontal rule"),
    ]
    assert menu.minimumWidth() == 240 and menu.height() <= 280
    assert menu.items[0].objectName() == "noteSlashItem" and menu.items[0].property("active") is True


def test_slash_opens_at_line_start_and_after_a_space_but_not_mid_word(view):
    menu = view.editor._slash
    _type(view, "/")
    assert menu.isVisible()
    _key(view, Qt.Key_Escape)
    assert not menu.isVisible()
    _key(view, Qt.Key_Backspace)
    _type(view, "word")
    _type(view, "/")
    assert not menu.isVisible()  # "word/" is not a command
    _type(view, " /")
    assert menu.isVisible()
    _key(view, Qt.Key_Backspace)
    assert not menu.isVisible()


def test_slash_does_not_open_in_a_code_block_or_a_url(view):
    _load(view, "```\nx\n```")
    _type(view, " /")
    assert not view.editor._slash.isVisible()
    _load(view, "")
    _type(view, "see https://x.io/a")
    assert not view.editor._slash.isVisible()
    _type(view, "3/4")
    assert not view.editor._slash.isVisible()


def test_slash_filters_and_closes_when_nothing_matches(view):
    menu = view.editor._slash
    _type(view, "/head")
    assert menu.visible_keys() == ["h1", "h2", "h3"] and menu.active_key() == "h1"
    _type(view, "ing 2")
    assert menu.visible_keys() == ["h2"]
    _key(view, Qt.Key_Backspace)
    _key(view, Qt.Key_Backspace)
    assert menu.isVisible()
    _type(view, "zzz")
    assert not menu.isVisible()  # nothing matches: an unrelated "/" never pins the menu open
    _load(view, "")
    _type(view, "/CODE")
    assert menu.visible_keys() == ["codeblock"]  # case-insensitive, matches the label or the key
    _key(view, Qt.Key_Escape)


def test_slash_arrow_keys_move_the_highlight_and_enter_chooses(view):
    menu = view.editor._slash
    _type(view, "/")
    _key(view, Qt.Key_Down)
    _key(view, Qt.Key_Down)
    assert menu.active_key() == "h2"
    _key(view, Qt.Key_Up)
    assert menu.active_key() == "h1"
    _key(view, Qt.Key_Return)
    assert not menu.isVisible()
    _type(view, "Title")
    assert _md(view) == "# Title"  # the "/" is gone and the line is a heading


def test_slash_choice_removes_only_the_query_and_applies_to_the_block(view):
    _type(view, "keep this /quo")
    _key(view, Qt.Key_Return)
    assert _md(view) == "> keep this"
    _load(view, "")
    _type(view, "/to")
    _key(view, Qt.Key_Tab)  # Tab chooses too
    _type(view, "task")
    assert _md(view) == "- [ ] task"


def test_slash_escape_closes_and_stays_closed_for_that_slash(view):
    menu = view.editor._slash
    _type(view, "/hea")
    _key(view, Qt.Key_Escape)
    assert not menu.isVisible()
    _type(view, "d")
    assert not menu.isVisible() and view.editor.toPlainText() == "/head"


def test_slash_menu_items_can_be_clicked(view):
    menu = view.editor._slash
    _type(view, "/div")
    next(i for i in menu.items if i.key == "divider").click()
    assert [nd.block_kind(b) for b in _blocks(view)] == ["rule", "paragraph"]


def test_slash_table_and_divider_replace_the_empty_line(view):
    _type(view, "/tab")
    _key(view, Qt.Key_Return)
    assert view.editor.textCursor().currentTable() is not None
    assert _md(view).startswith("|  |  |  |")


def test_slash_choice_is_one_undo_step(view):
    _type(view, "hello")
    _key(view, Qt.Key_Return)
    _type(view, "/h1")
    _key(view, Qt.Key_Return)
    view.editor.undo()
    assert view.editor.toPlainText() == "hello\n/h1" and _md(view) == "hello\n\n/h1"


# -- the caret hint ------------------------------------------------------------------------------------------------------


def test_caret_hint_paints_on_an_empty_plain_line_without_error(view):
    _load(view, "text")
    _key(view, Qt.Key_Return)
    view.editor.grab()  # paintEvent draws "Type / for commands" here
    assert not view.model.document.isEmpty() and nd.block_kind(_caret_block(view)) == "paragraph"
    view.editor.run_command("h1")
    view.editor.grab()  # not on a heading
    view.editor.setPlaceholderText("placeholder")


# -- links ---------------------------------------------------------------------------------------------------------------


def _card(view):
    return view.link_card


def test_ctrl_k_opens_the_link_card_and_enter_links_the_selection(view):
    _type(view, "read the deck")
    cursor = view.editor.textCursor()
    cursor.setPosition(9)
    cursor.setPosition(13, QTextCursor.KeepAnchor)
    view.editor.setTextCursor(cursor)
    _key(view, Qt.Key_K, CTRL)
    card = _card(view)
    assert card.isVisible() and card.objectName() == "noteLinkCard" and card.url.placeholderText() == "https://"
    assert card.add_button.text() == "Add" and card.cancel_button.text() == "Cancel"
    card.url.setText("https://x.io/deck")
    QTest.keyClick(card.url, Qt.Key_Return)
    assert not card.isVisible()
    assert _md(view) == "read the [deck](https://x.io/deck)"
    assert view.editor.hasFocus()


def test_link_card_with_no_selection_inserts_the_address_as_its_text(view):
    _type(view, "see ")
    view.toolbar.buttons["link"].click()
    _card(view).url.setText("https://x.io/a")
    _card(view).add_button.click()
    assert _md(view) == "see https://x.io/a"
    _type(view, " more")
    assert _md(view) == "see https://x.io/a more"  # what follows is not part of the link
    anchors = [it.fragment().text() for it in view.model.document.begin() if it.fragment().charFormat().isAnchor()]
    assert anchors == ["https://x.io/a"]


def test_link_card_rejects_anything_but_http_and_https(view):
    _type(view, "x")
    _key(view, Qt.Key_A, CTRL)
    _key(view, Qt.Key_K, CTRL)
    card = _card(view)
    for bad in ("ftp://files.example.com", "example.com", "javascript:alert(1)", "https://"):
        card.url.setText(bad)
        card.add_button.click()
        assert card.isVisible() and card.error.isVisibleTo(card)
        assert card.error.text() == "Enter a web address that starts with http:// or https://"
        assert card.error.objectName() == "connResult" and card.error.property("state") == "error"
    assert _md(view) == "x"
    card.url.setText("http://ok.example")
    card.add_button.click()
    assert _md(view) == "[x](http://ok.example)"


def test_link_card_edits_an_existing_link_and_an_empty_address_removes_it(view):
    _load(view, "go [here](https://old.io) now", caret_at_end=False)
    cursor = view.editor.textCursor()
    cursor.setPosition(5)
    view.editor.setTextCursor(cursor)
    _key(view, Qt.Key_K, CTRL)
    card = _card(view)
    assert card.url.text() == "https://old.io"  # prefilled
    card.url.setText("https://new.io")
    card.add_button.click()
    assert _md(view) == "go [here](https://new.io) now"
    _key(view, Qt.Key_K, CTRL)
    card.url.setText("")
    card.add_button.click()
    assert _md(view) == "go here now"


def test_link_card_cancel_and_escape_leave_the_text_alone(view):
    _type(view, "abc")
    view.toolbar.buttons["link"].click()
    assert _card(view).isVisible()
    view.toolbar.buttons["link"].click()  # the button toggles it
    assert not _card(view).isVisible()
    _key(view, Qt.Key_K, CTRL)
    _card(view).cancel_button.click()
    assert not _card(view).isVisible() and _md(view) == "abc"
    _key(view, Qt.Key_K, CTRL)
    QTest.keyClick(_card(view), Qt.Key_Escape)
    assert not _card(view).isVisible() and view.editor.hasFocus()


def test_typed_and_pasted_urls_become_links(view):
    _type(view, "see https://x.io/a_b now")
    anchors = [it.fragment().text() for it in view.model.document.begin() if it.fragment().charFormat().isAnchor()]
    assert anchors == ["https://x.io/a_b"]
    assert _md(view) == "see https://x.io/a_b now"
    from PySide6.QtCore import QMimeData

    mime = QMimeData()
    mime.setText("and https://y.io/p, ok")
    view.editor.insertFromMimeData(mime)
    assert view.editor.link_at(view.editor.cursorRect(_cursor_at(view, view.editor.toPlainText().index("y.io") + 1)).center()) \
        == "https://y.io/p"
    assert _md(view) == "see https://x.io/a_b nowand https://y.io/p, ok"


def _cursor_at(view, pos):
    cursor = view.editor.textCursor()
    cursor.setPosition(pos)
    return cursor


def test_paste_is_plain_text(view):
    from PySide6.QtCore import QMimeData

    mime = QMimeData()
    mime.setHtml("<b>bold</b> <i>x</i>")
    mime.setText("bold x")
    view.editor.insertFromMimeData(mime)
    assert _md(view) == "bold x"
    assert not view.editor.acceptRichText()


def test_links_are_coloured_and_hovering_one_shows_the_open_hint(view):
    _load(view, "open https://x.io/a now", caret_at_end=False)
    QTest.qWait(10)
    pos = view.editor.cursorRect(_cursor_at(view, 9)).center()
    assert view.editor.link_at(pos) == "https://x.io/a"
    QTest.mouseMove(view.editor.viewport(), pos)
    assert view.editor.viewport().toolTip() == f"https://x.io/a\n{OPEN_HINT}"
    assert view.editor.link_at(view.editor.cursorRect(_cursor_at(view, 2)).center()) is None
    fmt = _cursor_at(view, 9).charFormat()
    assert fmt.isAnchor() and fmt.fontUnderline()
    from meeting_notes.client.ui import theme

    assert fmt.foreground().color().name() == theme.tokens()["accent_text"]


def test_ctrl_click_opens_a_link_and_a_plain_click_places_the_caret(view, monkeypatch):
    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", staticmethod(lambda url: opened.append(url.toString()) or True))
    _load(view, "open https://x.io/a now", caret_at_end=False)
    QTest.qWait(10)
    pos = view.editor.cursorRect(_cursor_at(view, 9)).center()
    QTest.mouseClick(view.editor.viewport(), Qt.LeftButton, Qt.NoModifier, pos)
    assert opened == [] and view.editor.textCursor().position() > 0
    QTest.mouseClick(view.editor.viewport(), Qt.LeftButton, CTRL, pos)
    assert opened == ["https://x.io/a"]


def test_a_todo_checkbox_toggles_on_click(view):
    _load(view, "- [ ] one\n- [x] two", caret_at_end=False)
    QTest.qWait(10)
    doc = view.model.document
    rect = view.editor.cursorRect(_cursor_at(view, doc.findBlockByNumber(0).position()))
    QTest.mouseClick(view.editor.viewport(), Qt.LeftButton, Qt.NoModifier, QPoint(rect.left() - 6, rect.center().y()))
    assert doc.findBlockByNumber(0).blockFormat().marker() == QTextBlockFormat.MarkerType.Checked
    assert _md(view) == "- [x] one\n- [x] two"


# -- the pane, the lock and the pop-out ---------------------------------------------------------------------------------------


def test_the_lock_makes_the_editor_read_only_and_disables_the_toolbar(view):
    view.model.set_locked(True)
    assert view.editor.isReadOnly() and not view.toolbar.isEnabled()
    _type(view, "nope")
    assert _md(view) == ""
    assert view.editor.run_command("bold") is False
    view.model.set_locked(False)
    assert not view.editor.isReadOnly() and view.toolbar.isEnabled()
    _type(view, "yes")
    assert _md(view) == "yes"


def test_the_pane_and_the_popout_share_one_document_and_each_has_its_own_toolbar(view):
    popout = ScratchpadPopout(view.model)
    popout.show()
    other = popout.view
    assert other.editor.document() is view.editor.document() is view.model.document
    assert other.toolbar is not view.toolbar and other.editor is not view.editor
    other.editor.setFocus()
    QTest.keyClicks(other.editor, "typed in the popout")
    assert view.editor.toPlainText() == "typed in the popout"
    other.editor.selectAll()
    other.toolbar.buttons["bold"].click()
    assert _md(view) == "**typed in the popout**"
    other.toolbar.buttons["h2"].click()
    assert _md(view) == "## **typed in the popout**"
    popout.close()


def test_loading_text_does_not_save_but_editing_does(view):
    saved = []
    view.model._saver = lambda text: saved.append(text) or True
    view.model.set_text("# loaded")
    assert not view.model._dirty and saved == []
    _type(view, " more")
    assert view.model._dirty and view.model.status == "Saving..."
    assert view.model.flush() is True and saved == ["# loaded more"]


def test_the_notes_survive_a_save_and_reload_cycle(view):
    _type(view, "Plan")
    view.editor.run_command("h1")
    _key(view, Qt.Key_Return)
    _type(view, "one")
    view.editor.run_command("bullet")
    _key(view, Qt.Key_Return)
    _type(view, "two **bold**")
    stored = _md(view)
    assert stored == "# Plan\n\n- one\n- two **bold**"
    view.model.set_text(stored)
    assert _md(view) == stored
