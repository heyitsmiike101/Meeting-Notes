"""The notes document: Markdown <-> a rich ``QTextDocument`` for the recorder's "Your notes" editor.

This is the Qt half of a port of Life Dash's notes editor. Life Dash edits a rendered DOM (``notes.js``) and walks it
back into Markdown with ``notes_serialize.js``; here the rendered surface is a ``QTextDocument`` and :func:`load` and
:func:`serialize` are that pair. Markdown is what is stored; the editor never shows it. Qt's own
``toMarkdown()`` is not used: it drops bold and italic, merges quote lines and writes ``- - -``.

The three parts (the loader, the editor's commands in ``notes_editor.py`` and the serializer) share one CLOSED
VOCABULARY, exactly as Life Dash's ``markdown.render`` / ``notes.js`` / ``notes_serialize.js`` do: anything outside it
cannot be created by the editor, so nothing disappears on the next autosave.

Block vocabulary (each is a ``QTextBlock`` unless noted)
    paragraph      plain block; an *empty* one between blocks is a deliberate blank line (``\\`` in the Markdown)
    heading 1-3    ``setHeadingLevel`` plus a larger weight-600 font (``#``, ``##``, ``###``; 4-6 load as 3)
    list items     a ``QTextList`` per run: ``ListDisc`` bullets, ``ListDecimal`` numbers, to-dos are bullets whose
                   block ``marker`` is ``Unchecked``/``Checked``; nesting is the list's ``indent`` (level + 1)
    quote          blocks with the int property :data:`BLOCK_QUOTE` (a quote id: equal ids = ONE quote container)
    code block     one block per line with :data:`BLOCK_CODE` (a fence id) and :data:`BLOCK_LANG`
    divider        an empty block with :data:`BLOCK_RULE`
    table          a ``QTextTable`` (first row = header); cells hold inline formatting only
Inline vocabulary: bold (weight >= 600), italic, strike-out, inline code (:data:`CHAR_CODE`), links (anchors).

The block rules and inline rules are the server's own (``meeting_notes.markdown``, itself a port of Life Dash's
``markdown.py``), reused rather than copied, so the editor reads a note the way the meeting page renders it.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import Qt
from PySide6.QtGui import (
    QColor, QTextBlock, QTextBlockFormat, QTextCharFormat, QTextCursor, QTextDocument, QTextFormat, QTextLength,
    QTextListFormat, QTextTable, QTextTableFormat,
)

from meeting_notes import markdown as md
from meeting_notes import scratchpad as sp
from meeting_notes.client.ui import theme

# -- the vocabulary's names ------------------------------------------------------------
BLOCK_QUOTE = QTextFormat.UserProperty + 1   # int quote id; equal ids on consecutive blocks = one quote
BLOCK_CODE = QTextFormat.UserProperty + 2    # int fence id; equal ids on consecutive blocks = one code block
BLOCK_LANG = QTextFormat.UserProperty + 3    # the fence's language ("" = none)
BLOCK_RULE = QTextFormat.UserProperty + 4    # True on the empty block that is a divider
CHAR_CODE = QTextFormat.UserProperty + 11    # True on inline code
TABLE_ALIGNS = QTextFormat.UserProperty + 20  # "left,center,,right": a column alignment per column ("" = none)

LINE_SEP = " "        # Shift+Enter: a hard break inside a block
BREAK = "\x01"             # notes_serialize.js's BREAK marker: a break not yet turned into a backslash or a space
BLANK_LINE = "\\"          # a line that is exactly one backslash: an empty line typed on purpose
HEADING_PX = {1: 22, 2: 18, 3: 16}
CODE_PX = 13
BOLD, SEMI = 700, 600
QUOTE_INDENT = 14
CODE_INDENT = 8
GAP = 4                    # space between paragraphs (Qt collapses neighbouring margins)

# Lifedash notes_serialize.js: BLOCK_TRIGGER (text render() would read back as a block gets a leading backslash)
BLOCK_TRIGGER = re.compile(r"^(?:#{1,6}\s|[-*+]\s|\d+[.)]\s|>|```|(?:-{3,}|\*{3,}|_{3,})\s*$)")
# Lifedash notes_serialize.js: INLINE_ESCAPE (characters that would read back as formatting); "]" is added inside
# a link label, whose pattern ends at the first one. "_" is not escaped: the renderer has no underscore emphasis,
# so snake_case is stored as typed.
_INLINE_ESCAPE = re.compile(r"([\\`*~\[])")
_LABEL_ESCAPE = re.compile(r"([\\`*~\[\]])")
_TAG = re.compile(r"<(/?)(strong|em|s|code|a)\b([^>]*)>")
_MARK = {"strike": "~~", "bold": "**", "italic": "*"}
_ORDER = ("strike", "bold", "italic")


@dataclass(frozen=True)
class Style:
    """The inline formatting of one run of text."""

    bold: bool = False
    italic: bool = False
    strike: bool = False
    code: bool = False
    href: str = ""


PLAIN = Style()


# =============================================================================== formats

def mono_families() -> List[str]:
    """The theme's monospace stack as a list of family names."""
    names = [part.strip().strip('"') for part in theme.MONO.split(",")]
    return [n for n in names if n and n != "monospace"] + ["monospace"]


def _set_mono(fmt: QTextCharFormat, on: bool, px: int = 0) -> None:
    if on:
        fmt.setFontFamilies(mono_families())
        fmt.setFontFixedPitch(True)
        if px:
            fmt.setProperty(QTextFormat.FontPixelSize, px)
    else:
        for prop in (QTextFormat.FontFamilies, QTextFormat.FontFamily, QTextFormat.FontFixedPitch):
            fmt.clearProperty(prop)


def apply_link(fmt: QTextCharFormat, href: str) -> None:
    """Make ``fmt`` a link to ``href``: anchor, accent colour, underline."""
    fmt.setAnchor(True)
    fmt.setAnchorHref(href)
    fmt.setForeground(QColor(theme.tokens()["accent_text"]))
    fmt.setFontUnderline(True)


def clear_link(fmt: QTextCharFormat) -> None:
    fmt.setAnchor(False)
    fmt.setAnchorHref("")
    for prop in (QTextFormat.ForegroundBrush, QTextFormat.TextUnderlineStyle, QTextFormat.FontUnderline):
        fmt.clearProperty(prop)


def apply_inline_code(fmt: QTextCharFormat, on: bool, heading: int = 0) -> None:
    """Inline code: a monospace run with a quiet background (Life Dash's ``<code>``)."""
    if on:
        fmt.setProperty(CHAR_CODE, True)
        _set_mono(fmt, True, 0 if heading else CODE_PX)
        fmt.setBackground(QColor(theme.tokens()["panel_hover"]))
    else:
        fmt.clearProperty(CHAR_CODE)
        _set_mono(fmt, False)
        fmt.clearProperty(QTextFormat.BackgroundBrush)
        if not heading:
            fmt.clearProperty(QTextFormat.FontPixelSize)


def char_format(style: Style = PLAIN, heading: int = 0, semibold: bool = False, code_block: bool = False) -> QTextCharFormat:
    """The character format for a run: ``heading``/``semibold`` give the block's own weight-600 base (a heading or a
    table header), which is presentation, not Bold; ``code_block`` is a line of a fenced block."""
    fmt = QTextCharFormat()
    if heading:
        fmt.setProperty(QTextFormat.FontPixelSize, HEADING_PX[heading])
    if style.bold:
        fmt.setFontWeight(BOLD)
    elif heading or semibold:
        fmt.setFontWeight(SEMI)
    if style.italic:
        fmt.setFontItalic(True)
    if style.strike:
        fmt.setFontStrikeOut(True)
    if code_block:
        _set_mono(fmt, True, CODE_PX)
    elif style.code:
        apply_inline_code(fmt, True, heading)
    if style.href:
        apply_link(fmt, style.href)
    return fmt


def block_format(kind: str = "paragraph", *, quote: int = 0, code: int = 0, lang: str = "") -> QTextBlockFormat:
    """The block format of ``kind``: paragraph, h1-h3, item, code, rule."""
    fmt = QTextBlockFormat()
    top = bottom = GAP
    if kind in ("h1", "h2", "h3"):
        fmt.setHeadingLevel(int(kind[1]))
        top = 10
    elif kind == "item":
        top = bottom = 1
    elif kind == "code":
        top = bottom = 0
        fmt.setNonBreakableLines(True)
        fmt.setLeftMargin(CODE_INDENT)
        fmt.setProperty(BLOCK_CODE, code)
        fmt.setProperty(BLOCK_LANG, lang)
    elif kind == "rule":
        top = bottom = 8
        fmt.setProperty(BLOCK_RULE, True)
    fmt.setTopMargin(top)
    fmt.setBottomMargin(bottom)
    if quote:
        fmt.setProperty(BLOCK_QUOTE, quote)
        fmt.setLeftMargin(fmt.leftMargin() + QUOTE_INDENT)
    return fmt


def _block_char_format(kind: str) -> QTextCharFormat:
    """The format the next typed character takes in an empty block of ``kind``."""
    if kind in ("h1", "h2", "h3"):
        return char_format(heading=int(kind[1]))
    return char_format(code_block=(kind == "code"))


# =============================================================================== block facts

def heading_of(block: QTextBlock) -> int:
    return block.blockFormat().headingLevel()


def quote_of(block: QTextBlock) -> int:
    return block.blockFormat().intProperty(BLOCK_QUOTE)


def code_of(block: QTextBlock) -> int:
    return block.blockFormat().intProperty(BLOCK_CODE)


def is_rule(block: QTextBlock) -> bool:
    return block.blockFormat().boolProperty(BLOCK_RULE)


def item_level(block: QTextBlock) -> int:
    """Nesting level of a list item (0 = top), or -1 when ``block`` is not an item."""
    lst = block.textList()
    return -1 if lst is None else max(0, lst.format().indent() - 1)


def _list_kind(block: QTextBlock) -> str:
    """``"ul"`` or ``"ol"`` from the item's list style."""
    style = block.textList().format().style()
    return "ol" if style in (QTextListFormat.ListDecimal, QTextListFormat.ListLowerAlpha,
                             QTextListFormat.ListUpperAlpha, QTextListFormat.ListLowerRoman,
                             QTextListFormat.ListUpperRoman) else "ul"


def is_task(block: QTextBlock) -> bool:
    return block.textList() is not None and block.blockFormat().marker() != QTextBlockFormat.MarkerType.NoMarker


def block_kind(block: QTextBlock) -> str:
    """rule, code, h1-h3, task, bullet, number or paragraph (a quote is a property on top of any of these)."""
    if is_rule(block):
        return "rule"
    if code_of(block):
        return "code"
    level = heading_of(block)
    if level:
        return f"h{min(level, 3)}"
    if block.textList() is not None:
        if is_task(block):
            return "task"
        return "number" if _list_kind(block) == "ol" else "bullet"
    return "paragraph"


def next_id(document: QTextDocument, prop: int) -> int:
    """A fresh quote or fence id: one more than the largest in use."""
    best = 0
    block = document.begin()
    while block.isValid():
        best = max(best, block.blockFormat().intProperty(prop))
        block = block.next()
    return best + 1


def is_empty_document(document: QTextDocument) -> bool:
    return document.blockCount() == 1 and not document.begin().text() and document.begin().blockFormat().headingLevel() == 0 \
        and document.begin().textList() is None and not is_rule(document.begin())


# =============================================================================== restyling blocks

def _is_bold(weight: int, semibold_base: bool) -> bool:
    return weight >= BOLD or (weight >= SEMI and not semibold_base)


def restyle_block(block: QTextBlock, old: Tuple[int, bool], new: Tuple[int, bool]) -> None:
    """Rewrite the character formats of ``block``'s text when its kind changes. ``old``/``new`` are ``(heading, code)``:
    the font size and the weight-600 base follow the heading, the monospace face follows the code block."""
    old_head, old_code = old
    new_head, new_code = new
    if (old_head, old_code) == (new_head, new_code):
        return
    doc = block.document()
    spans = []
    for it in block:
        frag = it.fragment()
        if frag.isValid():
            spans.append((frag.position(), frag.length(), QTextCharFormat(frag.charFormat())))

    def convert(fmt: QTextCharFormat) -> QTextCharFormat:
        bold = _is_bold(fmt.fontWeight(), bool(old_head))
        inline_code = fmt.boolProperty(CHAR_CODE)
        fmt.clearProperty(QTextFormat.FontWeight)
        if bold:
            fmt.setFontWeight(BOLD)
        elif new_head:
            fmt.setFontWeight(SEMI)
        fmt.clearProperty(QTextFormat.FontPixelSize)
        if new_head:
            fmt.setProperty(QTextFormat.FontPixelSize, HEADING_PX[new_head])
        if new_code:
            _set_mono(fmt, True, CODE_PX)
        elif old_code and not inline_code:
            _set_mono(fmt, False)
        elif inline_code and not new_head:
            fmt.setProperty(QTextFormat.FontPixelSize, CODE_PX)
        return fmt

    cursor = QTextCursor(doc)
    for pos, length, fmt in spans:
        cursor.setPosition(pos)
        cursor.setPosition(pos + length, QTextCursor.KeepAnchor)
        cursor.setCharFormat(convert(fmt))
    cursor = QTextCursor(block)
    cursor.setBlockCharFormat(convert(QTextCharFormat(block.charFormat())))


# =============================================================================== lists

_LIST_STYLE = {"ul": QTextListFormat.ListDisc, "ol": QTextListFormat.ListDecimal}


class ListTracker:
    """Gives consecutive list items of one level and kind ONE ``QTextList``, so Qt numbers them 1..n and the
    serializer sees one list. Used by the loader and by :func:`relink_lists` (Lifedash notes.js has no equivalent: a
    ``<ul>`` is one element there by construction)."""

    def __init__(self) -> None:
        self.entries: Dict[int, Tuple[object, str, int]] = {}
        self.claimed: set = set()

    def reset(self) -> None:
        self.entries.clear()

    def place(self, block: QTextBlock, level: int, kind: str, quote: int) -> None:
        for deeper in [k for k in self.entries if k > level]:
            del self.entries[deeper]
        current = block.textList()
        entry = self.entries.get(level)
        if entry is not None and entry[1] == kind and entry[2] == quote:
            wanted = entry[0]
            if current is None or current.objectIndex() != wanted.objectIndex():
                wanted.add(block)
            return
        lst = None
        if current is not None and current.objectIndex() not in self.claimed \
                and current.format().indent() == level + 1 and _list_kind(block) == kind:
            lst = current
        if lst is None:
            fmt = QTextListFormat()
            fmt.setStyle(_LIST_STYLE[kind])
            fmt.setIndent(level + 1)
            lst = QTextCursor(block).createList(fmt)
        self.entries[level] = (lst, kind, quote)
        self.claimed.add(lst.objectIndex())


def relink_lists(document: QTextDocument) -> None:
    """Merge adjacent same-level, same-kind items into one list and split lists a paragraph has cut in two."""
    tracker = ListTracker()
    block = document.begin()
    while block.isValid():
        level = item_level(block)
        if level < 0:
            tracker.reset()
        else:
            tracker.place(block, level, _list_kind(block), quote_of(block))
        block = block.next()


def make_item(block: QTextBlock, kind: str, level: int, checked: Optional[bool] = None) -> None:
    """Turn ``block`` into a list item of ``kind`` (bullet, number, task) at ``level`` in a list of its own; run
    :func:`relink_lists` afterwards to join it to its neighbours."""
    bf = block.blockFormat()
    keep_quote = bf.intProperty(BLOCK_QUOTE)
    new = block_format("item", quote=keep_quote)
    if kind == "task":
        new.setMarker(QTextBlockFormat.MarkerType.Checked if checked else QTextBlockFormat.MarkerType.Unchecked)
    cursor = QTextCursor(block)
    cursor.setBlockFormat(new)
    fmt = QTextListFormat()
    fmt.setStyle(_LIST_STYLE["ol" if kind == "number" else "ul"])
    fmt.setIndent(level + 1)
    cursor.createList(fmt)


def set_block_kind(block: QTextBlock, kind: str, *, quote: Optional[int] = None, code_id: int = 0, lang: str = "",
                   level: Optional[int] = None) -> None:
    """Change ``block`` to ``kind`` (paragraph, h1-h3, bullet, number, task, code, rule) keeping its text and its
    inline formatting. ``quote`` overrides the quote id (0 = unquote). A list item keeps its level unless ``level``
    says otherwise. Callers wrap this in an edit block and run :func:`relink_lists` afterwards."""
    bf = block.blockFormat()
    old = (heading_of(block), bool(code_of(block)))
    keep_level = max(0, item_level(block)) if level is None else level
    checked = bf.marker() == QTextBlockFormat.MarkerType.Checked
    quote_id = bf.intProperty(BLOCK_QUOTE) if quote is None else quote
    head = int(kind[1]) if kind in ("h1", "h2", "h3") else 0
    restyle_block(block, old, (head, kind == "code"))
    cursor = QTextCursor(block)
    if kind in ("bullet", "number", "task"):
        was_task = is_task(block)
        make_item_with_quote(block, kind, keep_level, quote_id, checked and was_task)
        return
    fmt = block_format("paragraph" if kind == "paragraph" else kind, quote=quote_id, code=code_id, lang=lang)
    cursor.setBlockFormat(fmt)
    cursor.setBlockCharFormat(_block_char_format(kind))


def make_item_with_quote(block: QTextBlock, kind: str, level: int, quote_id: int, checked: bool = False) -> None:
    cursor = QTextCursor(block)
    cursor.setBlockFormat(block_format("item", quote=quote_id))
    cursor.setBlockCharFormat(char_format())
    make_item(block, kind, level, checked)


# =============================================================================== structure edits

def _subtree(block: QTextBlock) -> List[QTextBlock]:
    """The items nested under ``block`` (the following items deeper than it)."""
    level = item_level(block)
    out: List[QTextBlock] = []
    nxt = block.next()
    while nxt.isValid() and item_level(nxt) > level:
        out.append(nxt)
        nxt = nxt.next()
    return out


def _relevel(block: QTextBlock, level: int) -> None:
    make_item(block, block_kind(block), level, block.blockFormat().marker() == QTextBlockFormat.MarkerType.Checked)


def can_indent(block: QTextBlock) -> bool:
    """An item can go one level in when an item of its own level sits above it under the same parent."""
    level = item_level(block)
    if level < 0:
        return False
    prev = block.previous()
    while prev.isValid() and item_level(prev) > level:
        prev = prev.previous()
    return prev.isValid() and item_level(prev) == level and quote_of(prev) == quote_of(block)


def indent_item(block: QTextBlock) -> bool:
    """Lifedash notes.js: indentLi. One level in, under the previous sibling, taking its sub-items along."""
    if not can_indent(block):
        return False
    moving = [block] + _subtree(block)
    levels = [item_level(b) for b in moving]
    for b, level in zip(moving, levels):
        _relevel(b, level + 1)
    relink_lists(block.document())
    return True


def outdent_item(block: QTextBlock) -> bool:
    """Lifedash notes.js: outdentLi. One level out; the items after it at its old level become its sub-items."""
    level = item_level(block)
    if level < 1:
        return False
    moving = [block] + _subtree(block)
    levels = [item_level(b) for b in moving]
    for b, old in zip(moving, levels):
        _relevel(b, old - 1)
    relink_lists(block.document())
    return True


def item_to_paragraph(block: QTextBlock) -> None:
    """Lifedash notes.js: liToParagraph. The item becomes a plain paragraph in place; its sub-items move out a level."""
    under = _subtree(block)
    levels = [item_level(b) for b in under]
    set_block_kind(block, "paragraph")
    for b, old in zip(under, levels):
        _relevel(b, max(0, old - 1))
    relink_lists(block.document())


def set_quote(block: QTextBlock, quote_id: int) -> None:
    """Put ``block`` in quote ``quote_id`` (0 takes it out), keeping everything else about it."""
    fmt = block.blockFormat()
    left = fmt.leftMargin() - (QUOTE_INDENT if fmt.intProperty(BLOCK_QUOTE) else 0)
    if quote_id:
        fmt.setProperty(BLOCK_QUOTE, quote_id)
        left += QUOTE_INDENT
    else:
        fmt.clearProperty(BLOCK_QUOTE)
    fmt.setLeftMargin(left)
    QTextCursor(block).setBlockFormat(fmt)


# ------------------------------------------------------------------ inline formatting

def char_has(fmt: QTextCharFormat, kind: str, semibold: bool = False) -> bool:
    """Whether the inline format ``kind`` (bold, italic, strike, code) is on in ``fmt``."""
    if kind == "bold":
        return _is_bold(fmt.fontWeight(), semibold)
    if kind == "italic":
        return fmt.fontItalic()
    if kind == "strike":
        return fmt.fontStrikeOut()
    return fmt.boolProperty(CHAR_CODE)


def inline_editor(kind: str, on: bool, heading: int = 0, semibold: bool = False):
    """A function that switches the inline format ``kind`` on or off in a character format."""

    def edit(fmt: QTextCharFormat) -> None:
        if kind == "bold":
            fmt.clearProperty(QTextFormat.FontWeight)
            if on:
                fmt.setFontWeight(BOLD)
            elif heading or semibold:
                fmt.setFontWeight(SEMI)
        elif kind == "italic":
            if on:
                fmt.setFontItalic(True)
            else:
                fmt.clearProperty(QTextFormat.FontItalic)
        elif kind == "strike":
            if on:
                fmt.setFontStrikeOut(True)
            else:
                fmt.clearProperty(QTextFormat.FontStrikeOut)
        else:
            apply_inline_code(fmt, on, heading)

    return edit


def semibold_block(block: QTextBlock) -> bool:
    """Whether the block's weight 600 is its own base (a heading, a table header) rather than Bold."""
    if heading_of(block):
        return True
    table = QTextCursor(block).currentTable()
    return table is not None and table.cellAt(QTextCursor(block)).row() == 0


def _pieces(document: QTextDocument, start: int, end: int):
    """(position, length, format, block) of every text fragment inside [start, end), outside code lines."""
    out = []
    block = document.findBlock(start)
    while block.isValid() and block.position() < end:
        if not code_of(block):
            for it in block:
                frag = it.fragment()
                if not frag.isValid():
                    continue
                lo, hi = max(start, frag.position()), min(end, frag.position() + frag.length())
                if lo < hi:
                    out.append((lo, hi - lo, QTextCharFormat(frag.charFormat()), block))
        block = block.next()
    return out


def range_has(document: QTextDocument, start: int, end: int, kind: str) -> bool:
    """True when every character of [start, end) outside code lines has the inline format ``kind``."""
    pieces = _pieces(document, start, end)
    return bool(pieces) and all(char_has(fmt, kind, semibold_block(block)) for _p, _l, fmt, block in pieces)


def format_range(document: QTextDocument, start: int, end: int, kind: str, on: bool) -> None:
    """Switch the inline format ``kind`` on or off over [start, end)."""
    cursor = QTextCursor(document)
    for pos, length, fmt, block in _pieces(document, start, end):
        inline_editor(kind, on, heading_of(block), semibold_block(block))(fmt)
        cursor.setPosition(pos)
        cursor.setPosition(pos + length, QTextCursor.KeepAnchor)
        cursor.setCharFormat(fmt)


def anchor_range(document: QTextDocument, pos: int) -> Optional[Tuple[int, int, str]]:
    """``(start, end, href)`` of the link the caret at ``pos`` touches, or None."""
    block = document.findBlock(pos)
    if not block.isValid():
        return None
    spans = []
    for it in block:
        frag = it.fragment()
        if frag.isValid() and frag.charFormat().isAnchor():
            spans.append((frag.position(), frag.position() + frag.length(), frag.charFormat().anchorHref()))
    for lo, hi, href in spans:
        if lo <= pos <= hi:
            start, end = lo, hi
            for lo2, hi2, href2 in spans:  # a link split into runs by formatting is still one link
                if href2 == href and hi2 == start:
                    start = lo2
                if href2 == href and lo2 == end:
                    end = hi2
            return start, end, href
    return None


def set_link(document: QTextDocument, start: int, end: int, href: str) -> None:
    """Make [start, end) a link to ``href`` (an empty ``href`` removes the link)."""
    cursor = QTextCursor(document)
    for pos, length, fmt, _block in _pieces(document, start, end):
        clear_link(fmt)
        if href:
            apply_link(fmt, href)
        cursor.setPosition(pos)
        cursor.setPosition(pos + length, QTextCursor.KeepAnchor)
        cursor.setCharFormat(fmt)


# =============================================================================== loading

def _inline_runs(text: str) -> List[Tuple[str, Style]]:
    """The inline Markdown of ``text`` as (text, style) runs, by the server's own ``markdown._inline``."""
    out = md._inline(text)
    runs: List[Tuple[str, Style]] = []
    counts = {"strong": 0, "em": 0, "s": 0, "code": 0}
    hrefs: List[str] = []

    def push(segment: str) -> None:
        if not segment:
            return
        plain = html.unescape(segment).replace(md._HARD_MARK, LINE_SEP)
        style = Style(bold=counts["strong"] > 0, italic=counts["em"] > 0, strike=counts["s"] > 0,
                      code=counts["code"] > 0, href=hrefs[-1] if hrefs else "")
        runs.append((plain, style))

    pos = 0
    for match in _TAG.finditer(out):
        push(out[pos:match.start()])
        pos = match.end()
        closing, name, attrs = match.group(1) == "/", match.group(2), match.group(3)
        if name == "a":
            if closing:
                if hrefs:
                    hrefs.pop()
            else:
                found = re.search(r'href="([^"]*)"', attrs)
                hrefs.append(html.unescape(found.group(1)) if found else "")
        else:
            counts[name] = max(0, counts[name] + (-1 if closing else 1))
    push(out[pos:])
    return runs


def _parse(lines: List[str], ids: Dict[str, int], allow_table: bool = True) -> List[dict]:
    """Port of ``markdown._blocks``: the block records of ``lines`` (no HTML), in the same order and by the same rules."""
    out: List[dict] = []
    para: List[str] = []
    stack: List[Tuple[str, int]] = []
    in_code = False
    code_lang = ""
    code_buf: List[str] = []
    last_item: Dict = {}

    def flush_para() -> None:
        if para:
            out.append({"t": "para", "text": md._join_lines(para)})
            para.clear()

    def close_all() -> None:
        flush_para()
        stack.clear()

    def open_list(tag: str, indent: int) -> int:
        while stack and stack[-1][1] > indent:
            stack.pop()
        if stack and stack[-1][1] == indent:
            if stack[-1][0] == tag:
                return len(stack) - 1
            stack.pop()
        stack.append((tag, indent))
        return len(stack) - 1

    def emit_item(text: str, kind: str, checked: bool, level: int) -> None:
        rec = {"t": "item", "kind": kind, "checked": checked, "level": level, "lines": [text]}
        out.append(rec)
        last_item.clear()
        last_item.update(rec=rec, idx=len(out) - 1)

    def continues_item(raw: str) -> bool:
        if not last_item or last_item["idx"] != len(out) - 1 or in_code:
            return False
        if not md._is_hard(last_item["rec"]["lines"][-1]):
            return False
        line = raw.strip()
        if not line:
            return False
        if line == "\\" or md._ESCAPED_LINE_START.match(line):
            return True
        return not (
            md._BULLET.match(line) or md._ORDERED.match(line) or md._HEADING.match(line) or md._QUOTE.match(line)
            or md._RULE.match(line) or md._FENCE.match(line) or md._is_table_row(line)
        )

    skip_to = 0
    for index, raw in enumerate(lines):
        if index < skip_to:
            continue
        if continues_item(raw):
            line = raw.strip()
            if line != "\\" and md._ESCAPED_LINE_START.match(line):
                line = line[1:]
            last_item["rec"]["lines"].append(line)
            continue
        if md._ESCAPED_LINE_START.match(raw) and not in_code:
            stack.clear()
            para.append(raw[1:].strip())
            continue
        fence = md._FENCE.match(raw)
        if fence:
            if in_code:
                out.append({"t": "code", "lang": code_lang, "lines": list(code_buf)})
                code_buf.clear()
                in_code = False
            else:
                close_all()
                in_code = True
                code_lang = fence.group(1)
            continue
        if in_code:
            code_buf.append(raw)
            continue
        if raw.strip() == "\\" and para and md._is_hard(para[-1]):
            para.append("\\")
            continue
        if raw.strip() == "\\":
            close_all()
            out.append({"t": "blank"})
            continue
        if not raw.strip():
            close_all()
            continue
        if md._RULE.match(raw):
            close_all()
            out.append({"t": "rule"})
            continue
        if allow_table and md._is_table_row(raw) and index + 1 < len(lines):
            header = md._split_row(raw)
            aligns = md._alignments(lines[index + 1], len(header))
            if aligns is not None and len(header) <= md._MAX_TABLE_COLS:
                rows: List[List[str]] = []
                cursor = index + 2
                while (cursor < len(lines) and lines[cursor].strip() and md._is_table_row(lines[cursor])
                       and len(rows) < md._MAX_TABLE_ROWS):
                    rows.append(md._split_row(lines[cursor]))
                    cursor += 1
                if len(rows) < md._MAX_TABLE_ROWS:
                    close_all()
                    out.append({"t": "table", "header": header, "aligns": aligns, "rows": rows})
                    skip_to = cursor
                    continue
        heading = md._HEADING.match(raw)
        if heading:
            close_all()
            out.append({"t": "head", "level": min(len(heading.group(1)), 3), "text": heading.group(2)})
            continue
        if md._QUOTE.match(raw):
            close_all()
            inner: List[str] = []
            cursor = index
            while cursor < len(lines):
                quoted = md._QUOTE.match(lines[cursor])
                if not quoted:
                    break
                inner.append(quoted.group(1))
                cursor += 1
            ids["quote"] += 1
            qid = ids["quote"]
            for rec in _parse(inner, ids, allow_table=False):
                rec.setdefault("quote", qid)
                out.append(rec)
            skip_to = cursor
            continue
        task = md._TASK.match(raw)
        if task:
            flush_para()
            emit_item(task.group(3), "task", task.group(2).lower() == "x", open_list("ul", len(task.group(1))))
            continue
        bullet = md._BULLET.match(raw)
        if bullet:
            flush_para()
            emit_item(bullet.group(2), "bullet", False, open_list("ul", len(bullet.group(1))))
            continue
        ordered = md._ORDERED.match(raw)
        if ordered:
            flush_para()
            emit_item(ordered.group(3), "number", False, open_list("ol", len(ordered.group(1))))
            continue
        if stack:
            stack.clear()
        para.append(raw.strip())
    if in_code:
        out.append({"t": "code", "lang": "", "lines": list(code_buf)})
    close_all()
    return out


class _Writer:
    """Appends block records to a document through one cursor. ``fresh`` = the cursor sits in an empty block no
    record has used yet (the first one, or the one Qt leaves after a table)."""

    def __init__(self, document: QTextDocument) -> None:
        self.document = document
        self.cursor = QTextCursor(document)
        self.fresh = True
        self.tracker = ListTracker()
        self.ids: Dict[str, int] = {"quote": 0, "code": 0}

    def begin(self, fmt: QTextBlockFormat, cfmt: QTextCharFormat) -> QTextBlock:
        if self.fresh:
            self.cursor.setBlockFormat(fmt)
            self.cursor.setBlockCharFormat(cfmt)
            self.fresh = False
        else:
            self.cursor.insertBlock(fmt, cfmt)
        return self.cursor.block()

    def runs(self, text: str, heading: int = 0, semibold: bool = False, code_block: bool = False) -> None:
        for piece, style in _inline_runs(text):
            self.cursor.insertText(piece, char_format(style, heading, semibold, code_block))

    def write(self, rec: dict) -> None:
        kind = rec["t"]
        quote = rec.get("quote", 0)
        if kind != "item":
            self.tracker.reset()
        if kind == "para":
            self.begin(block_format("paragraph", quote=quote), char_format())
            self.runs(rec["text"])
        elif kind == "blank":
            self.begin(block_format("paragraph", quote=quote), char_format())
        elif kind == "head":
            name = f"h{rec['level']}"
            self.begin(block_format(name, quote=quote), _block_char_format(name))
            self.runs(rec["text"], heading=rec["level"])
        elif kind == "rule":
            self.begin(block_format("rule", quote=quote), char_format())
        elif kind == "code":
            self.ids["code"] += 1
            cid = self.ids["code"]
            for line in rec["lines"] or [""]:
                self.begin(block_format("code", quote=quote, code=cid, lang=rec["lang"]), _block_char_format("code"))
                if line:
                    self.cursor.insertText(line, char_format(code_block=True))
        elif kind == "item":
            fmt = block_format("item", quote=quote)
            if rec["kind"] == "task":
                fmt.setMarker(QTextBlockFormat.MarkerType.Checked if rec["checked"] else QTextBlockFormat.MarkerType.Unchecked)
            block = self.begin(fmt, char_format())
            self.runs(md._join_lines(rec["lines"]))
            self.tracker.place(block, rec["level"], "ol" if rec["kind"] == "number" else "ul", quote)
        elif kind == "table":
            self.table(rec)

    def table(self, rec: dict) -> None:
        build_table(self.cursor, rec["header"], rec["rows"], rec["aligns"], self.runs_into)
        self.cursor.movePosition(QTextCursor.End)
        self.fresh = True

    def runs_into(self, cursor: QTextCursor, text: str, semibold: bool) -> None:
        for piece, style in _inline_runs(text):
            cursor.insertText(piece, char_format(style, semibold=semibold))


def table_format(columns: int, aligns: List[str]) -> QTextTableFormat:
    fmt = QTextTableFormat()
    tokens = theme.tokens()
    fmt.setBorder(1)
    fmt.setBorderStyle(QTextTableFormat.BorderStyle_Solid)
    fmt.setBorderBrush(QColor(tokens["border_strong"]))
    fmt.setCellPadding(5)
    fmt.setCellSpacing(0)
    fmt.setHeaderRowCount(1)
    fmt.setWidth(QTextLength(QTextLength.PercentageLength, 100))
    fmt.setColumnWidthConstraints([QTextLength(QTextLength.PercentageLength, 100.0 / max(1, columns))] * columns)
    fmt.setProperty(TABLE_ALIGNS, ",".join(aligns))
    return fmt


def build_table(cursor: QTextCursor, header: List[str], rows: List[List[str]], aligns: List[str], fill) -> QTextTable:
    """Insert a table at ``cursor``: ``fill(cell_cursor, text, semibold)`` writes each cell's inline text."""
    width = len(header)
    aligns = (list(aligns) + [""] * width)[:width]
    table = cursor.insertTable(len(rows) + 1, width, table_format(width, aligns))
    for r in range(len(rows) + 1):
        cells = header if r == 0 else (rows[r - 1] + [""] * width)[:width]
        for c in range(width):
            cell_cursor = table.cellAt(r, c).firstCursorPosition()
            bf = QTextBlockFormat()
            bf.setAlignment({"center": Qt.AlignHCenter, "right": Qt.AlignRight}.get(aligns[c], Qt.AlignLeft))
            cell_cursor.setBlockFormat(bf)
            if cells[c]:
                fill(cell_cursor, cells[c], r == 0)
            elif r == 0:
                cell_cursor.setBlockCharFormat(char_format(semibold=True))
    return table


def load(document: QTextDocument, markdown: str) -> None:
    """Build the rich document from Markdown (replacing whatever it held)."""
    text = (markdown or "").replace("\r\n", "\n").replace("\r", "\n")
    undo = document.isUndoRedoEnabled()
    document.setUndoRedoEnabled(False)
    document.clear()
    writer = _Writer(document)
    for rec in _parse(text.split("\n"), writer.ids):
        writer.write(rec)
    if writer.fresh:  # the empty block Qt leaves after a table (or the only block): make it a plain paragraph
        writer.cursor.setBlockFormat(block_format("paragraph"))
        writer.cursor.setBlockCharFormat(char_format())
    document.setUndoRedoEnabled(undo)
    document.setModified(False)


# =============================================================================== serializing

def _trim_breaks(text: str) -> str:
    return re.sub(r"^[\s\x01]+|[\s\x01]+$", "", text)


def _escape_lead(line: str) -> str:
    return "\\" + line if BLOCK_TRIGGER.match(line) else line


def _hard_breaks(text: str) -> str:
    """Lifedash notes_serialize.js: hardBreaks. Lines joined by a backslash and a newline."""
    return "\\\n".join(_escape_lead(line) for line in _trim_breaks(text).split(BREAK))


def _soft_breaks(text: str) -> str:
    """Lifedash notes_serialize.js: softBreaks. A heading or table cell has one line: a break is a space."""
    return _trim_breaks(text).replace(BREAK, " ")


def _runs_of(block: QTextBlock, semibold: bool) -> List[Tuple[str, Style]]:
    runs: List[Tuple[str, Style]] = []
    for it in block:
        frag = it.fragment()
        if not frag.isValid():
            continue
        fmt = frag.charFormat()
        style = Style(
            bold=_is_bold(fmt.fontWeight(), semibold), italic=fmt.fontItalic(), strike=fmt.fontStrikeOut(),
            code=fmt.boolProperty(CHAR_CODE), href=fmt.anchorHref() if fmt.isAnchor() else "",
        )
        text = frag.text()
        if runs and runs[-1][1] == style:
            runs[-1] = (runs[-1][0] + text, style)
        else:
            runs.append((text, style))
    return runs


def _plain_text(text: str, label: bool) -> str:
    """Lifedash notes_serialize.js: escapeText. A non-breaking space is not the writer's; a break becomes the marker."""
    text = text.replace(" ", " ").replace("​", "").replace(BREAK, "")
    text = (_LABEL_ESCAPE if label else _INLINE_ESCAPE).sub(r"\\\1", text)
    return text.replace(LINE_SEP, BREAK)


def _inter(*styles: Style) -> Style:
    """The emphasis every one of ``styles`` shares (what can stay around a space moved to a run's edge)."""
    return Style(bold=all(s.bold for s in styles), italic=all(s.italic for s in styles),
                 strike=all(s.strike for s in styles), href=styles[0].href)


def _split_edges(runs: List[Tuple[str, Style]]) -> List[Tuple[str, Style]]:
    """Emphasis must not start or end on a space: a space at the edge of a marked run steps outside the markers that
    open or close there (and stays inside the ones that carry on across it)."""
    out: List[Tuple[str, Style]] = []
    empty = Style()
    for i, (text, style) in enumerate(runs):
        prev = runs[i - 1][1] if i > 0 else empty
        nxt = runs[i + 1][1] if i + 1 < len(runs) else empty
        core = text.strip()
        if (style.code and "`" not in text) or not (style.bold or style.italic or style.strike) or core == text:
            out.append((text, style))
        elif not core:
            out.append((text, _inter(style, prev, nxt)))
        else:
            lead = text[: len(text) - len(text.lstrip())]
            trail = text[len(text.rstrip()):]
            if lead:
                out.append((lead, _inter(style, prev)))
            out.append((core, style))
            if trail:
                out.append((trail, _inter(style, nxt)))
    merged: List[Tuple[str, Style]] = []
    for text, style in out:
        if merged and merged[-1][1] == style:
            merged[-1] = (merged[-1][0] + text, style)
        else:
            merged.append((text, style))
    return merged


class _Emitter:
    """Writes runs of text with their emphasis markers. The open markers are a stack: a marker that is still wanted
    stays open across runs; one that is not wanted closes (and the wanted ones above it reopen)."""

    def __init__(self, label: bool = False) -> None:
        self.out: List[str] = []
        self.stack: List[str] = []
        self.label = label

    def move(self, wanted: List[str]) -> None:
        keep = 0
        while keep < len(self.stack) and self.stack[keep] in wanted:
            keep += 1
        self.out.extend(_MARK[m] for m in reversed(self.stack[keep:]))
        reopen = [m for m in self.stack[keep:] if m in wanted] + [m for m in wanted if m not in self.stack]
        self.out.extend(_MARK[m] for m in reopen)
        self.stack = self.stack[:keep] + reopen

    def text(self, text: str, style: Style) -> None:
        self.move([m for m in _ORDER if getattr(style, m)])
        if style.code and "`" not in text:
            self.out.append("`" + text.replace(LINE_SEP, " ") + "`")
        else:
            self.out.append(_plain_text(text, self.label))

    def finish(self) -> str:
        self.move([])
        return "".join(self.out)


def _emit_runs(runs: List[Tuple[str, Style]], label: bool = False) -> str:
    emitter = _Emitter(label)
    for text, style in _split_edges(runs):
        emitter.text(text, style)
    return emitter.finish()


def _bare_ok(text: str) -> bool:
    spans = sp.link_spans(text)
    return len(spans) == 1 and spans[0][0] == 0 and spans[0][1] == len(text)


def inline_markdown(block: QTextBlock, semibold: bool = False) -> str:
    """The inline Markdown of a block (breaks still the BREAK marker). An anchor whose text is its address is written
    bare, so a bare URL round-trips unchanged (Lifedash notes_serialize.js: the ``label === href`` rule). Emphasis
    that covers a whole link stays open around it ("**a [b](u) c**"), as the renderer nests it."""
    runs = _split_edges(_runs_of(block, semibold))
    emitter = _Emitter()
    i = 0
    while i < len(runs):
        href = runs[i][1].href
        j = i
        while j < len(runs) and runs[j][1].href == href:
            j += 1
        group = runs[i:j]
        i = j
        if not href:
            for text, style in group:
                emitter.text(text, style)
            continue
        common = [m for m in _ORDER if all(getattr(s, m) for _t, s in group)]
        emitter.move(common)
        inner = [(t, Style(**{**s.__dict__, **{m: False for m in common}})) for t, s in group]
        joined = "".join(t for t, _s in inner)
        if all(not (s.bold or s.italic or s.strike or s.code) for _t, s in inner) and joined == href and _bare_ok(href):
            emitter.out.append(href)
            continue
        label = _emit_runs(inner, label=True)
        target = href.replace(" ", "%20").replace(")", "%29")
        emitter.out.append(label if not _trim_breaks(label) else f"[{label}]({target})")
    return emitter.finish()


@dataclass
class _Item:
    block: Optional[QTextBlock] = None
    table: Optional[QTextTable] = None


def _items(document: QTextDocument) -> List[_Item]:
    items: List[_Item] = []
    seen = set()
    block = document.begin()
    while block.isValid():
        table = QTextCursor(block).currentTable()
        if table is None:
            items.append(_Item(block=block))
        elif table.firstPosition() not in seen:
            seen.add(table.firstPosition())
            items.append(_Item(table=table))
        block = block.next()
    return items


def _cell_text(table: QTextTable, row: int, col: int) -> str:
    cell = table.cellAt(row, col)
    doc = table.document()
    block = doc.findBlock(cell.firstPosition())
    parts = []
    while block.isValid() and block.position() <= cell.lastPosition():
        parts.append(inline_markdown(block, semibold=(row == 0)))
        block = block.next()
    text = _soft_breaks(" ".join(p for p in parts if p))
    return re.sub(r"\s*\n\s*", " ", text).replace("|", "\\|").strip()


def _align_cell(table: QTextTable) -> List[str]:
    raw = table.format().stringProperty(TABLE_ALIGNS).split(",") if table.format().hasProperty(TABLE_ALIGNS) else []
    return (raw + [""] * table.columns())[: table.columns()]


_DELIM = {"": "---", "left": ":---", "center": ":---:", "right": "---:"}


def _table_markdown(table: QTextTable) -> str:
    """Lifedash notes_serialize.js: serializeTable; the inverse of ``markdown._table_html``."""
    cols = table.columns()
    if not table.rows() or not cols:
        return ""
    aligns = _align_cell(table)
    lines = ["| " + " | ".join(_cell_text(table, 0, c) for c in range(cols)) + " |",
             "| " + " | ".join(_DELIM.get(aligns[c], "---") for c in range(cols)) + " |"]
    for r in range(1, table.rows()):
        lines.append("| " + " | ".join(_cell_text(table, r, c) for c in range(cols)) + " |")
    return "\n".join(lines)


def _list_markdown(blocks: List[QTextBlock]) -> str:
    """Lifedash notes_serialize.js: serializeList. Two spaces per level; numbers renumbered 1..n per list; an
    item with no text and nothing under it is dropped (the empty row left by Enter to get out of a list)."""
    lines: List[str] = []
    counters: Dict[int, int] = {}
    owners: Dict[int, int] = {}
    for i, block in enumerate(blocks):
        level = item_level(block)
        text = _trim_breaks(inline_markdown(block))
        nested = i + 1 < len(blocks) and item_level(blocks[i + 1]) > level
        if not text and not nested:
            continue
        for deeper in [k for k in counters if k > level]:
            del counters[deeper]
            owners.pop(deeper, None)
        owner = block.textList().objectIndex()
        if owners.get(level) != owner:
            owners[level] = owner
            counters[level] = 0
        counters[level] += 1
        if is_task(block):
            marker = "- [x] " if block.blockFormat().marker() == QTextBlockFormat.MarkerType.Checked else "- [ ] "
        else:
            marker = f"{counters[level]}. " if _list_kind(block) == "ol" else "- "
        pad = "  " * level
        parts = [_escape_lead(line) for line in text.split(BREAK)]
        for n, line in enumerate(parts):
            body = line + ("" if n == len(parts) - 1 else "\\")
            lines.append(pad + marker + body if n == 0 else pad + "  " + body)
    return "\n".join(lines)


def _quote_prefix(text: str) -> str:
    """Lifedash notes_serialize.js: quotePrefixAll. A blank line inside the quote is a bare ``>``."""
    return "\n".join("> " + line if line else ">" for line in text.split("\n"))


def _structural_blank(items: List[_Item], i: int, in_quote: bool, nothing_before: bool) -> bool:
    """Qt keeps an empty block in front of a table that opens the document and between two tables; neither is a
    blank line the writer typed."""
    block = items[i].block
    if in_quote or block.text().strip() or block.textList() is not None or quote_of(block) or code_of(block) or is_rule(block):
        return False
    after = i + 1 < len(items) and items[i + 1].table is not None
    before = i > 0 and items[i - 1].table is not None
    return after and (nothing_before or before)


def _blocks_markdown(items: List[_Item], in_quote: bool, document_first: bool = False) -> str:
    """Lifedash notes_serialize.js: serializeBlocks. Top-level blocks joined by one blank line, a deliberate empty
    paragraph as a backslash line, trailing blanks dropped."""
    out: List[str] = []
    n = len(items)
    i = 0
    while i < n:
        item = items[i]
        if item.table is not None:
            text = _table_markdown(item.table)
            if text:
                out.append(text)
            i += 1
            continue
        block = item.block
        if _structural_blank(items, i, in_quote, not out):
            i += 1
            continue
        qid = 0 if in_quote else quote_of(block)
        if qid:
            j = i
            while j < n and items[j].block is not None and quote_of(items[j].block) == qid:
                j += 1
            inner = _blocks_markdown(items[i:j], True)
            out.append(_quote_prefix(inner) if inner else BLANK_LINE)
            i = j
            continue
        if is_rule(block):
            out.append("---")
            i += 1
            continue
        cid = code_of(block)
        if cid:
            j = i
            while j < n and items[j].block is not None and code_of(items[j].block) == cid \
                    and (in_quote or not quote_of(items[j].block)):
                j += 1
            lang = block.blockFormat().stringProperty(BLOCK_LANG)
            body = "\n".join(items[k].block.text().replace(LINE_SEP, "\n") for k in range(i, j))
            out.append(f"```{lang}\n{body}\n```")
            i = j
            continue
        if block.textList() is not None:
            j = i + 1
            first_list = block.textList().objectIndex()
            while j < n and items[j].block is not None and items[j].block.textList() is not None \
                    and (in_quote or not quote_of(items[j].block)):
                if item_level(items[j].block) == 0 and item_level(block) == 0 \
                        and items[j].block.textList().objectIndex() != first_list \
                        and item_level(items[j - 1].block) == 0:
                    break
                if item_level(items[j].block) == 0:
                    first_list = items[j].block.textList().objectIndex()
                j += 1
            text = _list_markdown([items[k].block for k in range(i, j)])
            if text:
                out.append(text)
            i = j
            continue
        level = heading_of(block)
        if level:
            body = _soft_breaks(inline_markdown(block, semibold=True))
            out.append("#" * min(level, 3) + " " + body if body else BLANK_LINE)
            i += 1
            continue
        text = _hard_breaks(inline_markdown(block))
        out.append(text if text else BLANK_LINE)
        i += 1
    while out and out[-1] == BLANK_LINE:
        out.pop()
    return "\n\n".join(out)


def serialize(document: QTextDocument) -> str:
    """Walk the document back into Markdown (the port of ``notes_serialize.js``)."""
    return _blocks_markdown(_items(document), False).replace(BREAK, " ").strip("\n")


# =============================================================================== links and theme

def autolink_block(block: QTextBlock) -> bool:
    """Turn bare http(s) URLs in ``block`` that are not links yet into links (the same rule as the server's page).
    Skips code blocks and inline code. True when anything changed."""
    if code_of(block) or is_rule(block):
        return False
    spans = sp.link_spans(block.text())
    if not spans:
        return False
    base = block.position()
    taken = []
    for it in block:
        frag = it.fragment()
        if frag.isValid() and (frag.charFormat().isAnchor() or frag.charFormat().boolProperty(CHAR_CODE)):
            taken.append((frag.position(), frag.position() + frag.length()))
    cursor = QTextCursor(block.document())
    changed = False
    for start, end, url in spans:
        lo, hi = base + start, base + end
        if any(a < hi and lo < b for a, b in taken):
            continue
        link = QTextCharFormat()
        apply_link(link, url)
        cursor.setPosition(lo)
        cursor.setPosition(hi, QTextCursor.KeepAnchor)
        cursor.mergeCharFormat(link)
        changed = True
    return changed


def restyle_theme(document: QTextDocument) -> None:
    """Recolour links and inline code after a theme change (their colours are in the character formats)."""
    tokens = theme.tokens()
    cursor = QTextCursor(document)
    block = document.begin()
    while block.isValid():
        for it in block:
            frag = it.fragment()
            if not frag.isValid():
                continue
            fmt = frag.charFormat()
            if fmt.isAnchor() or fmt.boolProperty(CHAR_CODE):
                new = QTextCharFormat(fmt)
                if fmt.isAnchor():
                    new.setForeground(QColor(tokens["accent_text"]))
                if fmt.boolProperty(CHAR_CODE):
                    new.setBackground(QColor(tokens["panel_hover"]))
                cursor.setPosition(frag.position())
                cursor.setPosition(frag.position() + frag.length(), QTextCursor.KeepAnchor)
                cursor.setCharFormat(new)
        block = block.next()


__all__ = [
    "BLOCK_CODE", "BLOCK_LANG", "BLOCK_QUOTE", "BLOCK_RULE", "CHAR_CODE", "LINE_SEP", "Style", "autolink_block",
    "block_format", "block_kind", "char_format", "load", "relink_lists", "restyle_theme", "serialize", "set_block_kind",
]
