"""The notes document: Markdown <-> a rich QTextDocument (the port of Life Dash's render / notes_serialize pair).

Headless (Qt's offscreen platform). The oracle is the server's own renderer: whatever the editor stores must render
the way the text it was loaded from rendered.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6", reason="PySide6 not installed")

from PySide6.QtGui import QTextBlockFormat, QTextCursor, QTextDocument  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from meeting_notes import markdown as md  # noqa: E402
from meeting_notes.client.ui import notes_doc as nd  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    return QApplication.instance() or QApplication([])


def _load(text: str) -> QTextDocument:
    doc = QTextDocument()
    nd.load(doc, text)
    return doc


def _round(text: str) -> str:
    return nd.serialize(_load(text))


def _blocks(doc: QTextDocument):
    block = doc.begin()
    while block.isValid():
        yield block
        block = block.next()


CORPUS = r"""# Agenda

## Sub heading

### Third level

Plain **bold** and *italic* and ~~strike~~ and `code` and ***both***.

See https://x.io/a_b now, and [label](https://y.io/p) too, or **[bold link](https://z.io)**.

- one
- two
  - nested a
  - nested b
    - deep
- three

1. first
2. second
   - mixed
3. third

- [ ] todo
- [x] done
  - [ ] sub task

> quoted line
> same paragraph
>
> second paragraph
> - a list in a quote

```python
def f():
    return 1
```

---

| a | b | c |
|---|:---:|---:|
| 1 | **x** | `y` |
| 2 | z | w |

line one\
line two\
line three

\# not a heading

snake_case and 3*4*5 and a [note] here

\

after a blank line
"""


def test_the_corpus_renders_the_same_after_a_round_trip():
    """The oracle: the server's renderer cannot tell the stored text from the one that was loaded."""
    assert md.render(_round(CORPUS)) == md.render(CORPUS)


def test_the_round_trip_is_stable():
    once = _round(CORPUS)
    assert _round(once) == once


@pytest.mark.parametrize("text", [
    "# One", "## Two", "### Three", "plain text", "**bold** and *italic* and ~~strike~~", "`code`", "***both***",
    "- a\n- b", "1. a\n2. b", "- [ ] a\n- [x] b", "> a\n>\n> b", "```\nx\n```", "```python\nx = 1\ny = 2\n```", "---",
    "| a | b |\n| --- | --- |\n| 1 | 2 |", "a\\\nb", "x\n\n\\\n\ny",
])
def test_each_construct_round_trips_to_itself(text):
    assert _round(text) == text


def test_empty_document_is_the_empty_string():
    assert _round("") == ""
    assert nd.serialize(QTextDocument()) == ""
    assert _round("\n\n   \n") == ""


def test_h4_to_h6_load_as_heading_3():
    assert _round("#### deep\n\n###### deeper") == "### deep\n\n### deeper"


def test_headings_use_larger_semibold_text_and_read_back_without_bold():
    doc = _load("## Sub **strong**")
    block = doc.begin()
    assert block.blockFormat().headingLevel() == 2
    weights = [it.fragment().charFormat().fontWeight() for it in block]
    assert weights == [600, 700]  # the heading's own weight is presentation; only the second run is Bold
    assert nd.serialize(doc) == "## Sub **strong**"


def test_lists_are_real_lists_with_levels_and_numbers():
    doc = _load("- a\n  - b\n- c\n\n1. x\n2. y")
    blocks = list(_blocks(doc))
    levels = [nd.item_level(b) for b in blocks]
    assert levels == [0, 1, 0, 0, 0]
    assert blocks[0].textList().format().style() == blocks[0].textList().format().style().__class__.ListDisc
    assert nd.block_kind(blocks[3]) == "number" and blocks[3].textList().itemNumber(blocks[4]) == 1
    assert nd.block_kind(blocks[0]) == "bullet"


def test_numbers_are_renumbered_on_serialize():
    assert _round("5. a\n9. b\n2. c") == "1. a\n2. b\n3. c"


def test_task_items_carry_a_checkbox_marker():
    doc = _load("- [ ] open\n- [x] done\n- plain")
    kinds = [nd.block_kind(b) for b in _blocks(doc)]
    assert kinds == ["task", "task", "bullet"]
    markers = [b.blockFormat().marker() for b in _blocks(doc)]
    assert markers[0] == QTextBlockFormat.MarkerType.Unchecked and markers[1] == QTextBlockFormat.MarkerType.Checked
    assert nd.serialize(doc) == "- [ ] open\n- [x] done\n- plain"


def test_a_quote_is_one_container_with_paragraphs_in_it():
    doc = _load("> a\n>\n> b\n\noutside")
    blocks = list(_blocks(doc))
    ids = [nd.quote_of(b) for b in blocks]
    assert ids[0] and ids[0] == ids[1] and ids[2] == 0
    assert blocks[0].blockFormat().leftMargin() == nd.QUOTE_INDENT
    assert nd.serialize(doc) == "> a\n>\n> b\n\noutside"


def test_two_quotes_stay_two_quotes():
    text = "> a\n\n> b"
    assert _round(text) == text
    ids = [nd.quote_of(b) for b in _blocks(_load(text))]
    assert ids[0] != ids[1]


def test_a_hard_break_inside_a_quote_stays_inside():
    assert _round("> a\\\n> b") == "> a\\\n> b"
    doc = _load("> a\\\n> b")
    assert doc.blockCount() == 1 and nd.LINE_SEP in doc.begin().text()


def test_code_block_keeps_language_and_lines():
    doc = _load("```python\na\n\nb\n```")
    blocks = list(_blocks(doc))
    assert [b.text() for b in blocks] == ["a", "", "b"]
    assert all(nd.code_of(b) for b in blocks) and len({nd.code_of(b) for b in blocks}) == 1
    assert blocks[0].blockFormat().stringProperty(nd.BLOCK_LANG) == "python"
    assert blocks[0].blockFormat().nonBreakableLines()
    assert blocks[0].charFormat().fontFixedPitch()
    assert nd.serialize(doc) == "```python\na\n\nb\n```"


def test_two_code_blocks_stay_two_blocks():
    text = "```\na\n```\n\n```js\nb\n```"
    assert _round(text) == text


def test_code_is_literal_text_nothing_inside_is_formatting():
    text = "```\n**not bold** `x` # y\n- z\n```"
    assert _round(text) == text


def test_divider_variants_load_as_one_rule():
    for source in ("---", "***", "___", "-----"):
        doc = _load(f"a\n\n{source}\n\nb")
        assert [nd.is_rule(b) for b in _blocks(doc)] == [False, True, False]
        assert nd.serialize(doc) == "a\n\n---\n\nb"


def test_table_is_a_qtexttable_with_a_bold_header_and_alignment():
    doc = _load("| a | b |\n|:--|--:|\n| **1** | 2 |")
    cursor = QTextCursor(doc.begin().next())
    table = cursor.currentTable() or QTextCursor(doc.begin()).currentTable()
    assert table is not None and (table.rows(), table.columns()) == (2, 2)
    header = table.cellAt(0, 0).firstCursorPosition().block()
    assert header.begin().fragment().charFormat().fontWeight() == 600
    assert nd.serialize(doc) == "| a | b |\n| :--- | ---: |\n| **1** | 2 |"


def test_table_cells_escape_pipes_and_short_rows_are_padded():
    doc = _load("| a | b |\n|---|---|\n| x \\| y |")
    assert nd.serialize(doc) == "| a | b |\n| --- | --- |\n| x \\| y |  |"


def test_text_around_a_table_survives():
    text = "before\n\n| a |\n| --- |\n| 1 |\n\nafter"
    assert _round(text) == text
    assert _round("| a |\n| --- |\n| 1 |") == "| a |\n| --- |\n| 1 |"
    two = "| a |\n| --- |\n| 1 |\n\n| b |\n| --- |\n| 2 |"
    assert _round(two) == two


def test_hard_breaks_are_trailing_backslashes():
    doc = _load("a\\\nb")
    assert doc.blockCount() == 1 and doc.begin().text() == f"a{nd.LINE_SEP}b"
    assert nd.serialize(doc) == "a\\\nb"
    assert _round("- a\\\n  b\n- c") == "- a\\\n  b\n- c"


def test_a_break_in_a_heading_or_table_cell_is_a_space():
    doc = QTextDocument()
    nd.load(doc, "## ab")
    cursor = QTextCursor(doc)
    cursor.setPosition(1)
    cursor.insertText(nd.LINE_SEP)
    assert nd.serialize(doc) == "## a b"
    table = _load("| ab |\n| --- |\n| c |")
    cell = QTextCursor(table.begin().next()).currentTable().cellAt(0, 0).firstCursorPosition()
    cell.setPosition(cell.position() + 1)
    cell.insertText(nd.LINE_SEP)
    assert nd.serialize(table).splitlines()[0] == "| a b |"


def test_bare_urls_become_links_and_come_back_bare():
    text = "see https://x.io/a_b, and (https://x.io/p_(q))."
    doc = _load(text)
    hrefs = [it.fragment().charFormat().anchorHref() for it in doc.begin() if it.fragment().charFormat().isAnchor()]
    assert hrefs == ["https://x.io/a_b", "https://x.io/p_(q)"]
    assert nd.serialize(doc) == text


def test_an_explicit_link_keeps_its_label():
    assert _round("[the **deck**](https://x.io/d)") == "[the **deck**](https://x.io/d)"
    assert _round("[https://x.io](https://x.io)") == "https://x.io"  # text == href: written bare, renders the same


def test_a_javascript_link_stays_text():
    doc = _load("[x](javascript:alert(1))")  # keep the document alive while its fragments are read
    assert "javascript" in nd.serialize(doc)
    assert not any(it.fragment().charFormat().isAnchor() for it in doc.begin())


def test_literal_markup_characters_are_escaped_and_survive():
    for text in ("snake_case_name", "3*4*5", "a * b", "~tilde~", "back`tick", "a [note] here", "C:\\dir\\file", "x_y_z and *"):
        doc = QTextDocument()
        cursor = QTextCursor(doc)
        cursor.insertText(text)
        stored = nd.serialize(doc)
        again = _load(stored)
        assert again.toPlainText() == text, (text, stored)
        assert nd.serialize(again) == stored


def test_snake_case_is_stored_as_typed():
    doc = QTextDocument()
    QTextCursor(doc).insertText("use snake_case here")
    assert nd.serialize(doc) == "use snake_case here"


def test_three_stars_are_escaped_not_italic():
    doc = QTextDocument()
    QTextCursor(doc).insertText("3*4*5")
    assert nd.serialize(doc) == "3\\*4\\*5"
    assert _load("3\\*4\\*5").toPlainText() == "3*4*5"


@pytest.mark.parametrize("text", ["# not a heading", "- not a list", "1. not a list", "> not a quote", "```", "---", "* x", "+ x"])
def test_a_paragraph_that_would_read_as_a_block_gets_a_backslash(text):
    doc = QTextDocument()
    QTextCursor(doc).insertText(text)
    stored = nd.serialize(doc)
    assert stored.startswith("\\")
    reloaded = _load(stored)
    assert [nd.block_kind(b) for b in _blocks(reloaded)] == ["paragraph"]
    assert reloaded.toPlainText() == text


def test_an_escaped_line_start_round_trips_as_text():
    doc = _load("\\# not a heading\n\n\\- nor this")
    assert [b.text() for b in _blocks(doc)] == ["# not a heading", "- nor this"]
    assert nd.serialize(doc) == "\\# not a heading\n\n\\- nor this"


def test_blank_paragraphs_between_blocks_are_kept_and_trailing_ones_dropped():
    doc = _load("a\n\n\\\n\nb")
    assert [b.text() for b in _blocks(doc)] == ["a", "", "b"]
    assert nd.serialize(doc) == "a\n\n\\\n\nb"
    doc2 = _load("a")
    cursor = QTextCursor(doc2)
    cursor.movePosition(QTextCursor.End)
    cursor.insertBlock()
    cursor.insertBlock()
    assert nd.serialize(doc2) == "a"


def test_empty_list_items_are_not_stored():
    doc = _load("- a\n- b")
    cursor = QTextCursor(doc)
    cursor.movePosition(QTextCursor.End)
    cursor.insertBlock()
    assert nd.serialize(doc) == "- a\n- b"


def test_non_breaking_spaces_become_spaces():
    doc = QTextDocument()
    QTextCursor(doc).insertText("a\u00a0b")
    assert nd.serialize(doc) == "a b"


def test_inline_formatting_markers_hug_the_text():
    doc = QTextDocument()
    cursor = QTextCursor(doc)
    from PySide6.QtGui import QTextCharFormat

    bold = QTextCharFormat()
    bold.setFontWeight(700)
    cursor.insertText("a", QTextCharFormat())
    cursor.insertText(" two words ", bold)
    cursor.insertText("b", QTextCharFormat())
    assert nd.serialize(doc) == "a **two words** b"


def test_nested_and_adjacent_emphasis_reads_back_the_same():
    for text in ("**a *b* c**", "*a **b** c*", "**bold** *it*", "~~**x**~~", "**`code`**"):
        assert md.render(_round(text)) == md.render(text), text


def test_markdown_inside_a_list_inside_a_quote_survives():
    text = "> - a\n>   - b\n> - c"
    assert md.render(_round(text)) == md.render(text)


def test_loading_replaces_the_previous_content():
    doc = _load("old\n\n- list")
    nd.load(doc, "new")
    assert nd.serialize(doc) == "new" and doc.blockCount() == 1


def test_crlf_and_trailing_blank_lines_are_normalised():
    assert _round("a\r\n\r\nb\r\n\r\n") == "a\n\nb"


def test_indent_and_outdent_move_an_item_with_its_children():
    doc = _load("- a\n- b\n  - c\n- d")
    blocks = list(_blocks(doc))
    assert nd.indent_item(blocks[1])
    assert nd.serialize(doc) == "- a\n  - b\n    - c\n- d"
    assert not nd.indent_item(list(_blocks(doc))[0])  # a first item has nothing to go under
    assert nd.outdent_item(list(_blocks(doc))[1])
    assert nd.serialize(doc) == "- a\n- b\n  - c\n- d"


def test_a_paragraph_between_numbers_restarts_the_list_like_the_page_does():
    doc = _load("1. a\n2. b\n3. c")
    blocks = list(_blocks(doc))
    nd.item_to_paragraph(blocks[1])
    out = nd.serialize(doc)
    assert out == "1. a\n\nb\n\n1. c"
    assert md.render(out).count("<ol>") == 2


def test_set_block_kind_changes_kind_and_keeps_inline_formatting():
    doc = _load("hello **world**")
    block = doc.begin()
    nd.set_block_kind(block, "h2")
    assert nd.serialize(doc) == "## hello **world**"
    nd.set_block_kind(block, "paragraph")
    assert nd.serialize(doc) == "hello **world**"
    assert all(it.fragment().charFormat().fontWeight() in (400, 700) for it in block)
    nd.set_block_kind(block, "bullet")
    assert nd.serialize(doc) == "- hello **world**"
