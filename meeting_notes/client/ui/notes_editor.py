"""The notes editor: a native Qt port of Life Dash's notes editor, for the recorder's "Your notes" pane.

Life Dash edits a rendered DOM in the browser (``app/static/notes.js``); here the rendered surface is a
``QTextDocument`` (see :mod:`meeting_notes.client.ui.notes_doc`, which also holds the Markdown loader and
serializer). The same things are here, under the same names where they translate: a formatting top bar
(:class:`NotesToolbar`), a "/" menu (:class:`SlashMenu`), a link card (:class:`LinkCard`), a key engine with Notion
semantics (``enterInItem``, ``backspaceKey``, ``indentLi`` ...), inline autoformat (``inlineAutoformat``) and the
shortcuts (``editorShortcut``). Markdown is what is stored, never what is shown.

Qt differs from the browser in a few places, each noted where it happens: the document is one shared
``QTextDocument`` (undo is Qt's own, not a hand-made history), lists are ``QTextList`` objects kept in step by
:func:`notes_doc.relink_lists`, and a to-do's checkbox is drawn and toggled by Qt itself.
"""

from __future__ import annotations

import re
import sys
from typing import Callable, Dict, List, Optional, Tuple

from PySide6.QtCore import QEvent, QPoint, QRect, QRectF, QSize, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QPainter, QTextBlock, QTextBlockFormat, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLayout, QLineEdit, QPushButton, QScrollArea, QTextEdit, QToolButton, QVBoxLayout,
    QWidget,
)

from meeting_notes.client.ui import icons, notes_doc as nd, theme

OPEN_HINT = "Cmd+click to open" if sys.platform == "darwin" else "Ctrl+click to open"
CARET_HINT = "Type / for commands"
LINK_ERROR = "Enter a web address that starts with http:// or https://"
_URL_OK = re.compile(r"^https?://[^\s/]\S*$", re.IGNORECASE)

# Lifedash notes.js: BLOCKS. Photo, Attachment and Link to... are left out: the scratchpad has no uploads.
BLOCKS = [
    ("text", "Text", "Plain paragraph"),
    ("h1", "Heading 1", "Big"),
    ("h2", "Heading 2", "Smaller"),
    ("h3", "Heading 3", "Smaller still"),
    ("bullet", "Bulleted list", "• item"),
    ("number", "Numbered list", "1. item"),
    ("task", "To-do list", "☐ task"),
    ("quote", "Quote", "Indented"),
    ("codeblock", "Code block", "Fenced"),
    ("table", "Table", "3 x 3, Tab moves"),
    ("divider", "Divider", "Horizontal rule"),
]

# Lifedash notes.js: INLINE_RULES. A pair fires on its CLOSING marker; the content may not start or end with a
# space, and an opening "_" or "*" may not touch a word character (snake_case_name stays text).
INLINE_RULES = [
    ("bold", 2, re.compile(r"(^|[^*])\*\*([^\s*](?:[^*]*[^\s*])?)\*\*$")),
    ("strike", 2, re.compile(r"(^|[^~])~~([^\s~](?:[^~]*[^\s~])?)~~$")),
    ("code", 1, re.compile(r"(^|[^`])`([^`\s](?:[^`]*[^`\s])?)`$")),
    ("italic", 1, re.compile(r"(^|[^*\w])\*([^\s*](?:[^*]*[^\s*])?)\*$")),
    ("italic", 1, re.compile(r"(^|[^A-Za-z0-9_])_([^\s_](?:[^_]*[^\s_])?)_$")),
]

# Lifedash notes.js: TURN_INTO_CODES (Digit0 paragraph, 1/2/3 headings, 4 to-do, 5 bullet, 6 numbered, 8 code block).
# Qt reports the shifted symbol on most layouts, so both the digit and its shifted key are listed.
TURN_INTO_KEYS = {
    Qt.Key_0: "text", Qt.Key_ParenRight: "text",
    Qt.Key_1: "h1", Qt.Key_Exclam: "h1",
    Qt.Key_2: "h2", Qt.Key_At: "h2",
    Qt.Key_3: "h3", Qt.Key_NumberSign: "h3",
    Qt.Key_4: "task", Qt.Key_Dollar: "task",
    Qt.Key_5: "bullet", Qt.Key_Percent: "bullet",
    Qt.Key_6: "number", Qt.Key_AsciiCircum: "number",
    Qt.Key_8: "codeblock", Qt.Key_Asterisk: "codeblock",
}
_KIND_OF_COMMAND = {
    "text": "paragraph", "h1": "h1", "h2": "h2", "h3": "h3", "bullet": "bullet", "number": "number", "task": "task",
    "codeblock": "code",
}
_STATE_KEYS = ("bold", "italic", "strike", "code", "h1", "h2", "h3", "bullet", "number", "task", "quote", "codeblock")


def _repolish(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


# =============================================================================== layout

class FlowLayout(QLayout):
    """The standard Qt flow layout: items run left to right and wrap (the 340px pane cannot hold the whole toolbar)."""

    def __init__(self, parent=None, hspacing: int = 4, vspacing: int = 2):
        super().__init__(parent)
        self._items: List = []
        self._h, self._v = hspacing, vspacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item) -> None:  # noqa: N802 - Qt naming
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):  # noqa: N802 - Qt naming
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):  # noqa: N802 - Qt naming
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):  # noqa: N802 - Qt naming
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt naming
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt naming
        return self._arrange(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802 - Qt naming
        super().setGeometry(rect)
        self._arrange(rect, apply=True)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802 - Qt naming
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(), margins.top() + margins.bottom())

    def _arrange(self, rect: QRect, apply: bool) -> int:
        """Lay the rows out. A row breaks at a group separator when it can (Life Dash's flex-wrap splits anywhere,
        but a group of buttons torn in two reads as two groups), and only mid-group when a whole group is too wide."""
        margins = self.contentsMargins()
        area = rect.adjusted(margins.left(), margins.top(), -margins.right(), -margins.bottom())
        rows: List[List] = [[]]
        width = 0
        for item in self._items:
            w = item.sizeHint().width()
            if rows[-1] and width + w > area.width():
                row = rows[-1]
                last_sep = max((n for n, it in enumerate(row) if isinstance(it.widget(), _Separator)), default=-1)
                if last_sep > 0:
                    carried = row[last_sep + 1:]
                    del row[last_sep:]  # the separator ends the row; the group after it starts the next
                    rows.append(carried)
                    width = sum(it.sizeHint().width() + self._h for it in carried)
                else:
                    rows.append([])
                    width = 0
            rows[-1].append(item)
            width += w + self._h
        y = area.y()
        for row in rows:
            x, line = area.x(), 0
            for item in row:
                size = item.sizeHint()
                if apply:
                    item.setGeometry(QRect(QPoint(x, y), size))
                x += size.width() + self._h
                line = max(line, size.height())
            y += line + self._v
        return y - self._v - rect.y() + margins.bottom()


class _Separator(QWidget):
    """Lifedash ``.note-toolbar-sep``: a hairline between groups of buttons."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(11, 32)

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.fillRect(QRect(5, 8, 1, 16), QColor(theme.tokens()["border"]))
        painter.end()


# =============================================================================== toolbar

class NotesToolbar(QFrame):
    """Lifedash's formatting toolbar (note_fields.html ``toolbar``) minus Img/Attach, same order, same tooltips.

    Buttons never take focus (Lifedash ``tabindex="-1"``) and are lit by the editor's ``state_changed``."""

    # (key, text or icon name, tooltip, mark)
    LAYOUT = [
        ("bold", "B", "Bold (Ctrl+B)", "bold"),
        ("italic", "I", "Italic (Ctrl+I)", "italic"),
        ("strike", "S", "Strikethrough", "strike"),
        None,
        ("h1", "H1", "Heading 1", ""),
        ("h2", "H2", "Heading 2", ""),
        ("h3", "H3", "Heading 3", ""),
        None,
        ("text", "icon:note-paragraph", "Paragraph", ""),
        ("bullet", "icon:note-bullets", "Bulleted list", ""),
        ("number", "1.", "Numbered list", ""),
        ("task", "icon:note-task", "To-do list", ""),
        None,
        ("quote", "icon:note-quote", "Quote", ""),
        ("codeblock", "icon:note-codeblock", "Code block", ""),
        ("code", "a", "Inline code", "code"),
        ("link", "Link", "Link", ""),
    ]
    # Life Dash's "type / for blocks" line is off in its note editor (note_editor_modal.html passes hint=False); the
    # editor's own "Type / for commands" caret hint says it instead.

    def __init__(self, editor: "NotesEditor", parent=None):
        super().__init__(parent)
        self.setObjectName("noteToolbar")
        self.editor = editor
        self.buttons: Dict[str, QToolButton] = {}
        flow = FlowLayout(self, hspacing=4, vspacing=2)
        for entry in self.LAYOUT:
            if entry is None:
                flow.addWidget(_Separator(self))
                continue
            key, face, tip, mark = entry
            button = QToolButton(self)
            button.setObjectName("noteTool")
            button.setFocusPolicy(Qt.NoFocus)
            button.setToolTip(tip)
            button.setAccessibleName(tip.split(" (")[0])
            button.setCursor(Qt.PointingHandCursor)
            button.setProperty("on", False)
            if mark:
                button.setProperty("mark", mark)
            if face.startswith("icon:"):
                button.setProperty("icon_name", face[5:])
                button.setIcon(icons.make_icon(face[5:], size=15))
                button.setIconSize(QSize(15, 15))
            else:
                button.setText(face)
            button.clicked.connect(lambda _c=False, k=key: self.editor.run_command(k))
            flow.addWidget(button)
            self.buttons[key] = button
        editor.state_changed.connect(self.set_state)
        theme.manager().changed.connect(self._on_theme)

    def _on_theme(self, _name: str = "") -> None:
        self.refresh_icons()

    def refresh_icons(self) -> None:
        for button in self.buttons.values():
            name = button.property("icon_name")
            if name:
                button.setIcon(icons.make_icon(name, size=15))

    def set_state(self, state: dict) -> None:
        """Light the buttons whose format is on at the caret (Lifedash notes.js: syncToolbar)."""
        for key, button in self.buttons.items():
            on = bool(state.get(key))
            if bool(button.property("on")) != on:
                button.setProperty("on", on)
                _repolish(button)


# =============================================================================== slash menu

class _SlashItem(QPushButton):
    """One row of the "/" menu: the label on the left, a dim hint on the right."""

    def __init__(self, key: str, label: str, hint: str, parent=None):
        super().__init__(label, parent)
        self.key = key
        self.label = label
        self.setObjectName("noteSlashItem")
        self.setFocusPolicy(Qt.NoFocus)
        self.setCursor(Qt.PointingHandCursor)
        self.setProperty("active", False)
        self.hint = QLabel(hint, self)
        self.hint.setObjectName("noteSlashHint")
        self.hint.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.hint.adjustSize()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        base = super().sizeHint()
        return QSize(base.width() + self.hint.sizeHint().width() + 16, base.height())

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self.hint.adjustSize()
        self.hint.move(self.width() - self.hint.width() - 9, (self.height() - self.hint.height()) // 2)


class SlashMenu(QFrame):
    """Lifedash's "/" block menu: a floating list at the caret. It never takes focus, so typing carries on in the
    editor (which forwards Up/Down/Enter/Tab/Escape here)."""

    chosen = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus)
        self.setObjectName("noteSlash")
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFocusPolicy(Qt.NoFocus)
        self.setMinimumWidth(240)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollArea(self)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setFocusPolicy(Qt.NoFocus)
        self.scroll.setStyleSheet("QScrollArea { background: transparent; border: none; } "
                                  "QScrollArea > QWidget > QWidget { background: transparent; }")
        self.body = QWidget()
        column = QVBoxLayout(self.body)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        self.items: List[_SlashItem] = []
        for key, label, hint in BLOCKS:
            item = _SlashItem(key, label, hint, self.body)
            item.clicked.connect(lambda _c=False, k=key: self.chosen.emit(k))
            column.addWidget(item)
            self.items.append(item)
        self.scroll.setWidget(self.body)
        outer.addWidget(self.scroll)

    # Lifedash notes.js: filterMenu
    def set_filter(self, query: str) -> int:
        """Show the items whose label or key contains ``query``; the first is active. Returns how many are shown."""
        lower = query.lower()
        shown = 0
        for item in self.items:
            hit = not lower or lower in item.label.lower() or lower in item.key
            item.setVisible(hit)
            self._set_active(item, hit and shown == 0)
            shown += 1 if hit else 0
        self._fit()
        return shown

    def _fit(self) -> None:
        rows = [i for i in self.items if not i.isHidden()]
        height = sum(i.sizeHint().height() for i in rows) + 10
        self.setFixedHeight(min(280, max(height, 36)))

    @staticmethod
    def _set_active(item: _SlashItem, active: bool) -> None:
        if bool(item.property("active")) != active:
            item.setProperty("active", active)
            _repolish(item)

    def visible_keys(self) -> List[str]:
        return [i.key for i in self.items if not i.isHidden()]

    def active_key(self) -> Optional[str]:
        for item in self.items:
            if not item.isHidden() and item.property("active"):
                return item.key
        return None

    def move_active(self, delta: int) -> None:
        rows = [i for i in self.items if not i.isHidden()]
        if not rows:
            return
        current = next((n for n, i in enumerate(rows) if i.property("active")), 0)
        target = (current + delta) % len(rows)
        for n, item in enumerate(rows):
            self._set_active(item, n == target)
        self.scroll.ensureWidgetVisible(rows[target], 0, 4)

    def show_at(self, global_pos: QPoint) -> None:
        """Place the menu with its top-left at ``global_pos``, kept inside the screen."""
        screen = self.screen()
        area = screen.availableGeometry() if screen is not None else QRect(0, 0, 1920, 1040)
        self.adjustSize()
        width = max(self.width(), self.minimumWidth())
        x = max(area.left(), min(global_pos.x(), area.right() + 1 - width))
        y = global_pos.y()
        if y + self.height() > area.bottom() + 1:
            y = max(area.top(), global_pos.y() - self.height() - 24)
        self.move(x, y)
        if not self.isVisible():
            self.show()


# =============================================================================== link card

class LinkCard(QFrame):
    """Lifedash notes.js: openLinkCard. A small card under the toolbar, not a modal prompt: one field, Add and Cancel."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("noteLinkCard")
        self.editor: Optional["NotesEditor"] = None
        self._context: Dict = {}
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)
        row = QHBoxLayout()
        row.setSpacing(6)
        self.url = QLineEdit()
        self.url.setPlaceholderText("https://")
        self.url.setAccessibleName("Link address")
        self.add_button = QPushButton("Add")
        self.add_button.setDefault(True)
        self.cancel_button = QPushButton("Cancel")
        row.addWidget(self.url, 1)
        row.addWidget(self.add_button)
        row.addWidget(self.cancel_button)
        column.addLayout(row)
        self.error = QLabel("")
        self.error.setObjectName("connResult")
        self.error.setProperty("state", "error")
        self.error.setWordWrap(True)
        self.error.setVisible(False)
        column.addWidget(self.error)
        self.add_button.clicked.connect(lambda _c=False: self.add())
        self.cancel_button.clicked.connect(lambda _c=False: self.dismiss())
        self.url.returnPressed.connect(self.add)
        self.setVisible(False)

    def toggle(self) -> None:
        """The Link button toggles the card (Lifedash routes a second click through the same close)."""
        if self.isVisible():
            self.dismiss()
        else:
            self.open_card()

    def open_card(self) -> None:
        if self.editor is None:
            return
        self._context = self.editor.link_context()
        self.url.setText(self._context.get("href", ""))
        self.error.setVisible(False)
        self.setVisible(True)
        self.url.setFocus(Qt.OtherFocusReason)
        self.url.selectAll()

    def add(self) -> None:
        if self.editor is None:
            return
        url = self.url.text().strip()
        if url and not _URL_OK.match(url):
            self.error.setText(LINK_ERROR)
            self.error.setVisible(True)
            return
        self.setVisible(False)
        self.editor.setFocus(Qt.OtherFocusReason)
        self.editor.commit_link(self._context, url)

    def dismiss(self) -> None:
        self.setVisible(False)
        if self.editor is not None:
            self.editor.setFocus(Qt.OtherFocusReason)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.key() == Qt.Key_Escape:
            self.dismiss()
            event.accept()
            return
        super().keyPressEvent(event)


# =============================================================================== the editor

class NotesEditor(QTextEdit):
    """The WYSIWYG notes surface. Plain-text paste, its own Enter/Backspace/Tab engine, inline autoformat, the "/" menu."""

    state_changed = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("scratchpad")
        self.setAcceptRichText(False)
        self.setTabChangesFocus(False)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self._link_card: Optional[LinkCard] = None
        self._slash = SlashMenu(self)
        self._slash.chosen.connect(self._choose_slash)
        self._slash_dismissed = -1
        self.cursorPositionChanged.connect(self._emit_state)
        self.currentCharFormatChanged.connect(lambda _f: self._emit_state())
        self.selectionChanged.connect(self._emit_state)
        self._commands: Dict[str, Callable[[], bool]] = {
            "bold": lambda: self._toggle_inline("bold"),
            "italic": lambda: self._toggle_inline("italic"),
            "strike": lambda: self._toggle_inline("strike"),
            "code": lambda: self._toggle_inline("code"),
            "h1": lambda: self._apply_block("h1"), "h2": lambda: self._apply_block("h2"),
            "h3": lambda: self._apply_block("h3"),
            "text": lambda: self._apply_block("paragraph", toggle=False),
            "bullet": lambda: self._apply_block("bullet"), "number": lambda: self._apply_block("number"),
            "task": lambda: self._apply_block("task"),
            "quote": self._toggle_quote, "codeblock": lambda: self._apply_block("code"),
            "table": self._insert_table, "divider": self._insert_divider, "link": self._toggle_link_card,
            "indent": lambda: self._indent(False), "outdent": lambda: self._indent(True),
        }

    # -- wiring --------------------------------------------------------------------

    def attach_link_card(self, card: LinkCard) -> None:
        self._link_card = card
        card.editor = self

    def setReadOnly(self, ro: bool) -> None:  # noqa: N802 - Qt naming
        super().setReadOnly(ro)
        if ro:
            self._close_slash()

    # -- commands (Lifedash notes.js: CMD, runFormatCommand) ----------------------------

    def run_command(self, key: str) -> bool:
        """The one dispatcher the toolbar, the "/" menu and the shortcuts all use."""
        fn = self._commands.get(key)
        if fn is None or self.isReadOnly():
            return False
        done = bool(fn())
        if key not in ("bold", "italic", "strike", "code"):  # an inline toggle IS the typing format; keep it
            self._sync_typing_format()
        self._emit_state()
        return done

    def _selected_blocks(self) -> List[QTextBlock]:
        """The blocks the selection (or the caret) touches, outside tables."""
        cur = self.textCursor()
        doc = self.document()
        start, end = cur.selectionStart(), cur.selectionEnd()
        first, last = doc.findBlock(start), doc.findBlock(end)
        if end > start and last.position() == end:
            last = last.previous()
        out: List[QTextBlock] = []
        block = first
        while block.isValid():
            if QTextCursor(block).currentTable() is None:
                out.append(block)
            if block.blockNumber() >= last.blockNumber():
                break
            block = block.next()
        return out

    def _sync_typing_format(self) -> None:
        """An empty block types in its own character format (a heading's size, a code line's face)."""
        cur = self.textCursor()
        if not cur.hasSelection() and not cur.block().text():
            self.setCurrentCharFormat(QTextCharFormat(cur.block().charFormat()))

    def _toggle_inline(self, kind: str) -> bool:
        cur = self.textCursor()
        block = cur.block()
        if nd.code_of(block):
            return False  # a code block is literal text
        if cur.hasSelection():
            start, end = cur.selectionStart(), cur.selectionEnd()
            on = not nd.range_has(self.document(), start, end, kind)
            cur.beginEditBlock()
            nd.format_range(self.document(), start, end, kind, on)
            cur.endEditBlock()
            return True
        fmt = QTextCharFormat(self.currentCharFormat())
        semibold = nd.semibold_block(block)
        on = not nd.char_has(fmt, kind, semibold)
        nd.inline_editor(kind, on, nd.heading_of(block), semibold)(fmt)
        self.setCurrentCharFormat(fmt)
        return True

    def _apply_block(self, kind: str, toggle: bool = True) -> bool:
        """Lifedash notes.js: toggleBlock, toggleTask. Applies to every selected block; again turns it off."""
        blocks = [b for b in self._selected_blocks() if not nd.is_rule(b)]
        if not blocks:
            return False
        doc = self.document()
        cur = self.textCursor()
        cur.beginEditBlock()
        try:
            target = kind
            if toggle and all(nd.block_kind(b) == kind for b in blocks):
                target = "bullet" if kind == "task" else "paragraph"  # Lifedash toggleTask: off leaves a plain bullet
            if target == "code":
                fence = nd.next_id(doc, nd.BLOCK_CODE)
                for block in blocks:
                    nd.set_block_kind(block, "code", code_id=fence)
                    self._split_hard_breaks(block)
            else:
                for block in blocks:
                    nd.set_block_kind(block, target)
            nd.relink_lists(doc)
        finally:
            cur.endEditBlock()
        return True

    def _split_hard_breaks(self, block: QTextBlock) -> None:
        """A code block is one line per block: Shift+Enter breaks in a paragraph become lines."""
        doc = self.document()
        text = block.text()
        cursor = QTextCursor(doc)
        for at in [i for i, ch in enumerate(text) if ch == nd.LINE_SEP][::-1]:
            cursor.setPosition(block.position() + at)
            cursor.setPosition(block.position() + at + 1, QTextCursor.KeepAnchor)
            cursor.removeSelectedText()
            cursor.insertBlock()

    def _toggle_quote(self) -> bool:
        """Lifedash notes.js: toggleQuote. One shared quote container, never one quote per block."""
        blocks = [b for b in self._selected_blocks() if not nd.is_rule(b)]
        if not blocks:
            return False
        doc = self.document()
        cur = self.textCursor()
        cur.beginEditBlock()
        try:
            if all(nd.quote_of(b) for b in blocks):
                for block in blocks:
                    nd.set_quote(block, 0)
            else:
                existing = next((nd.quote_of(b) for b in blocks if nd.quote_of(b)), 0)
                qid = existing or nd.next_id(doc, nd.BLOCK_QUOTE)
                for block in blocks:
                    nd.set_quote(block, qid)
            nd.relink_lists(doc)
        finally:
            cur.endEditBlock()
        return True

    def _insert_divider(self) -> bool:
        """Lifedash notes.js: insertDivider. A divider, then an empty paragraph to keep writing in."""
        cur = self.textCursor()
        if cur.currentTable() is not None:
            return False
        cur.beginEditBlock()
        try:
            if cur.hasSelection():
                cur.removeSelectedText()
            block = cur.block()
            blank = not block.text() and nd.block_kind(block) == "paragraph"
            if blank:
                nd.set_block_kind(block, "rule", quote=0)
            else:
                cur.movePosition(QTextCursor.EndOfBlock)
                cur.insertBlock(nd.block_format("rule"), nd.char_format())
            cur.movePosition(QTextCursor.EndOfBlock)
            cur.insertBlock(nd.block_format("paragraph"), nd.char_format())
        finally:
            cur.endEditBlock()
        self.setTextCursor(cur)
        return True

    def _insert_table(self) -> bool:
        """Lifedash notes.js: insertTable. A 3 x 3 table (header row + two), the caret in its first cell."""
        cur = self.textCursor()
        if cur.currentTable() is not None:
            return False
        doc = self.document()
        cur.beginEditBlock()
        try:
            if cur.hasSelection():
                cur.removeSelectedText()
            block = cur.block()
            prev = block.previous()
            if not block.text() and nd.block_kind(block) == "paragraph" and prev.isValid() \
                    and QTextCursor(prev).currentTable() is None:
                cur.setPosition(prev.position() + prev.length() - 1)
                cur.setPosition(block.position() + block.length() - 1, QTextCursor.KeepAnchor)
                cur.removeSelectedText()
            else:
                cur.movePosition(QTextCursor.EndOfBlock)
            noop = lambda _c, _t, _s: None  # noqa: E731
            table = nd.build_table(cur, ["", "", ""], [["", "", ""], ["", "", ""]], ["", "", ""], noop)
            after = doc.findBlock(table.lastPosition() + 1)
            if after.isValid() and QTextCursor(after).currentTable() is None and not after.text():
                nd.set_block_kind(after, "paragraph", quote=0)
            nd.relink_lists(doc)
            target = table.cellAt(0, 0).firstCursorPosition()
        finally:
            cur.endEditBlock()
        self.setTextCursor(target)
        return True

    def _indent(self, out: bool) -> bool:
        """Lifedash notes.js: indentBlock / outdentBlock (the Tab / Shift+Tab pair, as commands)."""
        blocks = [b for b in self._selected_blocks() if nd.item_level(b) >= 0]
        if not blocks:
            return False
        cur = self.textCursor()
        cur.beginEditBlock()
        try:
            moved = False
            covered = set()
            for block in blocks:
                if block.blockNumber() in covered:
                    continue  # already moved with its parent
                covered.update(b.blockNumber() for b in nd._subtree(block))
                moved = (nd.outdent_item(block) if out else nd.indent_item(block)) or moved
        finally:
            cur.endEditBlock()
        return moved

    # -- links (Lifedash notes.js: openLinkCard) ---------------------------------------

    def _toggle_link_card(self) -> bool:
        if self._link_card is None:
            return False
        self._link_card.toggle()
        return True

    def link_context(self) -> Dict:
        """Where the link card's Add will land: the selection, the link under the caret, or just the caret."""
        cur = self.textCursor()
        doc = self.document()
        if cur.hasSelection():
            start, end = cur.selectionStart(), cur.selectionEnd()
            found = nd.anchor_range(doc, start)
            href = found[2] if found and found[0] <= start and end <= found[1] else ""
            return {"start": start, "end": end, "href": href}
        found = nd.anchor_range(doc, cur.position())
        if found:
            return {"start": found[0], "end": found[1], "href": found[2]}
        return {"start": cur.position(), "end": cur.position(), "href": ""}

    def commit_link(self, context: Dict, url: str) -> None:
        """Add (or, with an empty ``url``, remove) the link the card was opened for."""
        if self.isReadOnly():
            return
        doc = self.document()
        start, end = context.get("start", 0), context.get("end", 0)
        cur = self.textCursor()
        cur.beginEditBlock()
        try:
            if start < end:
                nd.set_link(doc, start, end, url)
                cur.setPosition(end)
            elif url:
                cur.setPosition(start)
                fmt = QTextCharFormat(self.currentCharFormat())
                nd.apply_link(fmt, url)
                cur.insertText(url, fmt)
        finally:
            cur.endEditBlock()
        self.setTextCursor(cur)
        plain = QTextCharFormat(self.currentCharFormat())
        nd.clear_link(plain)
        self.setCurrentCharFormat(plain)  # what is typed next is not part of the link

    def link_at(self, pos: QPoint) -> Optional[str]:
        """The address of the link under ``pos`` (viewport coordinates), or None."""
        href = self.anchorAt(pos)
        return href or None

    @staticmethod
    def _open_modifier(event) -> bool:
        return bool(event.modifiers() & (Qt.ControlModifier | Qt.MetaModifier))

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        url = self.link_at(event.position().toPoint())
        if url:
            self.viewport().setCursor(Qt.PointingHandCursor if self._open_modifier(event) else Qt.IBeamCursor)
            self.viewport().setToolTip(f"{url}\n{OPEN_HINT}")
        else:
            self.viewport().setCursor(Qt.IBeamCursor)
            self.viewport().setToolTip("")
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._close_slash()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.LeftButton and self._open_modifier(event):
            url = self.link_at(event.position().toPoint())
            if url and not self.textCursor().hasSelection():
                QDesktopServices.openUrl(QUrl(url))
                event.accept()
                return
        super().mouseReleaseEvent(event)

    def focusOutEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._close_slash()
        super().focusOutEvent(event)

    # -- paste ---------------------------------------------------------------------

    def insertFromMimeData(self, source) -> None:  # noqa: N802 - Qt naming
        """Paste as plain text (Lifedash sends a paste through the renderer; the scratchpad keeps it simple), and
        link any bare URL that arrives."""
        if not source.hasText():
            return
        text = source.text().replace("\r\n", "\n").replace("\r", "\n")
        cur = self.textCursor()
        block = cur.block()
        if nd.heading_of(block) or cur.currentTable() is not None:
            text = text.replace("\n", " ")
        cur.beginEditBlock()
        start = min(cur.position(), cur.anchor())
        cur.insertText(text)
        self._autolink_range(start, cur.position())
        cur.endEditBlock()
        self.setTextCursor(cur)
        self.ensureCursorVisible()

    def _autolink_range(self, start: int, end: int) -> None:
        doc = self.document()
        block = doc.findBlock(start)
        while block.isValid() and block.position() <= end:
            nd.autolink_block(block)
            block = block.next()

    # -- keys (Lifedash notes.js: the key engine) ---------------------------------------

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self.isReadOnly():
            super().keyPressEvent(event)
            return
        if self._slash.isVisible() and self._slash_key(event):
            return
        if self._shortcut(event):
            event.accept()
            return
        key, mods = event.key(), event.modifiers()
        bare = not (mods & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier))
        if self._text_key(event) and nd.is_rule(self.textCursor().block()):
            nd.set_block_kind(self.textCursor().block(), "paragraph")  # typing on a divider writes a paragraph there
        if bare and key in (Qt.Key_Return, Qt.Key_Enter):
            if mods & Qt.ShiftModifier:
                self._soft_break()
            else:
                self._enter()
            self._finish_edit()
            event.accept()
            return
        if bare and key == Qt.Key_Backspace and not (mods & Qt.ShiftModifier):
            if self._clear_everything() or self._backspace():
                self._finish_edit()
                event.accept()
                return
        if bare and key == Qt.Key_Delete:
            if self._clear_everything() or self._delete():
                self._finish_edit()
                event.accept()
                return
        if bare and key in (Qt.Key_Tab, Qt.Key_Backtab):
            self._tab(back=key == Qt.Key_Backtab or bool(mods & Qt.ShiftModifier))
            event.accept()
            return
        super().keyPressEvent(event)
        text = event.text()
        if len(text) == 1 and text in "*_`~":
            self._inline_autoformat()
        elif text == " ":
            self._after_space()
        self._slash_update()

    @staticmethod
    def _text_key(event) -> bool:
        text = event.text()
        return bool(text) and text.isprintable() and not (event.modifiers() & (Qt.ControlModifier | Qt.MetaModifier))

    def _finish_edit(self) -> None:
        self._sync_typing_format()
        self.ensureCursorVisible()
        self._slash_update()
        self._emit_state()

    # Lifedash notes.js: editorShortcut + TURN_INTO_CODES
    def _shortcut(self, event) -> bool:
        mods = event.modifiers()
        if not (mods & Qt.ControlModifier) or (mods & Qt.AltModifier):
            return False
        key = event.key()
        if not (mods & Qt.ShiftModifier):
            name = {Qt.Key_B: "bold", Qt.Key_I: "italic", Qt.Key_E: "code", Qt.Key_K: "link"}.get(key)
            if name:
                self.run_command(name)
                return True
            return False
        if key == Qt.Key_X:
            self.run_command("strike")
            return True
        name = TURN_INTO_KEYS.get(key)
        if name:
            self.turn_into(name)
            return True
        return False

    def turn_into(self, name: str) -> bool:
        """Lifedash notes.js: turnInto. Sets the kind (it does not toggle, unlike the toolbar button)."""
        kind = _KIND_OF_COMMAND.get(name)
        if kind is None:
            return False
        done = self._apply_block(kind, toggle=False)
        self._sync_typing_format()
        self._emit_state()
        return done

    # -- Enter / Shift+Enter ---------------------------------------------------------

    def _enter(self) -> None:
        """Lifedash notes.js: splitAtCaret, enterInItem. A heading's tail is a paragraph, an item's tail an item
        (a to-do's an unchecked to-do), Enter on an empty item leaves the list, in code it adds a line."""
        cur = self.textCursor()
        if cur.currentTable() is not None:
            return  # a cell is one line; Tab moves
        cur.beginEditBlock()
        try:
            if cur.hasSelection():
                cur.removeSelectedText()
            block = cur.block()
            nd.autolink_block(block)
            kind = nd.block_kind(block)
            if kind == "rule":
                cur.movePosition(QTextCursor.EndOfBlock)
                cur.insertBlock(nd.block_format("paragraph"), nd.char_format())
            elif kind == "code":
                cur.insertBlock()
            elif kind in ("h1", "h2", "h3"):
                self._enter_heading(cur, block)
            elif kind in ("bullet", "number", "task"):
                self._enter_item(cur, block)
            else:
                self._enter_paragraph(cur, block)
        finally:
            cur.endEditBlock()
        self.setTextCursor(cur)

    @staticmethod
    def _enter_heading(cur: QTextCursor, block: QTextBlock) -> None:
        at_start = cur.positionInBlock() == 0 and bool(block.text())
        cur.insertBlock()
        if at_start:
            nd.set_block_kind(cur.block().previous(), "paragraph")  # the heading is pushed down, intact
        else:
            nd.set_block_kind(cur.block(), "paragraph")  # a heading's tail is a paragraph, never another heading

    def _enter_item(self, cur: QTextCursor, block: QTextBlock) -> None:
        if not block.text():  # an empty item means "I'm done": one level out, else out of the list
            if nd.item_level(block) > 0:
                nd.outdent_item(block)
            else:
                nd.item_to_paragraph(block)
            return
        cur.insertBlock()
        new = cur.block()
        if nd.is_task(new):
            fmt = new.blockFormat()
            fmt.setMarker(QTextBlockFormat.MarkerType.Unchecked)
            cur.setBlockFormat(fmt)

    @staticmethod
    def _enter_paragraph(cur: QTextCursor, block: QTextBlock) -> None:
        quote = nd.quote_of(block)
        nxt = block.next()
        if quote and not block.text() and not (nxt.isValid() and nd.quote_of(nxt) == quote):
            nd.set_quote(block, 0)  # Lifedash exitEmptyQuote: Enter on the last, empty line leaves the quote
            return
        cur.insertBlock()

    def _soft_break(self) -> None:
        """Lifedash notes.js: softBreak. A hard break inside the block (not in a heading, code or a table cell)."""
        cur = self.textCursor()
        block = cur.block()
        if cur.currentTable() is not None or nd.code_of(block) or nd.heading_of(block) or nd.is_rule(block):
            return
        cur.beginEditBlock()
        cur.insertText(nd.LINE_SEP)
        cur.endEditBlock()
        self.setTextCursor(cur)

    # -- Backspace / Delete ------------------------------------------------------------

    def _clear_everything(self) -> bool:
        """Select-all + Delete leaves one empty paragraph (Lifedash notes.js: resetEditor)."""
        cur = self.textCursor()
        doc = self.document()
        if not cur.hasSelection() or cur.selectionStart() != 0 or cur.selectionEnd() < doc.characterCount() - 1:
            return False
        if doc.blockCount() == 1 and not doc.begin().text():
            return False
        if doc.rootFrame().childFrames():  # a table: remove it with the rest by starting the document over
            nd.load(doc, "")
            self.setTextCursor(QTextCursor(doc))
            return True
        cur.beginEditBlock()
        cur.removeSelectedText()
        nd.set_block_kind(cur.block(), "paragraph", quote=0)
        cur.setBlockCharFormat(nd.char_format())
        cur.endEditBlock()
        self.setTextCursor(cur)
        return True

    def _backspace(self) -> bool:
        """Lifedash notes.js: backspaceKey. One rung of the ladder per press at the start of a block: to-do ->
        bullet -> paragraph, heading -> paragraph, quote -> unquoted, code line -> paragraph; a paragraph joins the
        one above, or removes a divider right before it."""
        cur = self.textCursor()
        if cur.hasSelection() or cur.positionInBlock() != 0:
            return False
        block = cur.block()
        if cur.currentTable() is not None:
            return True  # never merge cells
        kind = nd.block_kind(block)
        cur.beginEditBlock()
        try:
            return self._backspace_at_start(cur, block, kind)
        finally:
            cur.endEditBlock()

    def _backspace_at_start(self, cur: QTextCursor, block: QTextBlock, kind: str) -> bool:
        if kind == "rule":
            self._remove_block(block, to_previous=True)
            return True
        if kind == "task":
            nd.set_block_kind(block, "bullet")  # the checkbox goes first; the bullet stays
            nd.relink_lists(self.document())
            return True
        if kind in ("bullet", "number"):
            if not block.text():
                prev = self._prev_item(block)
                if prev is not None or nd.item_level(block) > 0:
                    self._remove_block(block, to_previous=True)  # an empty sub-bullet is deleted, not shuffled out
                    return True
            if nd.item_level(block) > 0:
                nd.outdent_item(block)
            else:
                nd.item_to_paragraph(block)
            return True
        if kind in ("h1", "h2", "h3"):
            nd.set_block_kind(block, "paragraph")
            return True
        if kind == "code":
            return self._backspace_code(cur, block)
        quote = nd.quote_of(block)
        prev = block.previous()
        if quote and not (prev.isValid() and nd.quote_of(prev) == quote):
            nd.set_quote(block, 0)  # Lifedash unwrapQuote: the quote's first line comes out of it
            return True
        if not prev.isValid():
            return True  # the very first block: nothing above to join
        if QTextCursor(prev).currentTable() is not None:
            return True
        if nd.is_rule(prev):
            self._remove_block(prev)
            return True
        if nd.block_kind(prev) in ("h1", "h2", "h3", "code"):
            # the joined text takes the look of the block it joins
            nd.restyle_block(block, (0, False), (nd.heading_of(prev), bool(nd.code_of(prev))))
        return False  # the default join

    def _backspace_code(self, cur: QTextCursor, block: QTextBlock) -> bool:
        fence = nd.code_of(block)
        prev, nxt = block.previous(), block.next()
        first = not (prev.isValid() and nd.code_of(prev) == fence)
        last = not (nxt.isValid() and nd.code_of(nxt) == fence)
        if first:  # Lifedash: the start of the block turns the whole block into text
            lines = [block]
            while nxt.isValid() and nd.code_of(nxt) == fence:
                lines.append(nxt)
                nxt = nxt.next()
            texts = [b.text() for b in lines]
            cur.setPosition(lines[0].position())
            cur.setPosition(lines[-1].position() + lines[-1].length() - 1, QTextCursor.KeepAnchor)
            cur.removeSelectedText()
            first_block = cur.block()
            nd.set_block_kind(first_block, "paragraph")
            cur.insertText(nd.LINE_SEP.join(texts), nd.char_format())
            cur.setPosition(first_block.position())
            self.setTextCursor(cur)
            return True
        if not block.text() and last:
            nd.set_block_kind(block, "paragraph")  # an empty last line steps out of the fence
            return True
        return False

    def _prev_item(self, block: QTextBlock) -> Optional[QTextBlock]:
        prev = block.previous()
        return prev if prev.isValid() and nd.item_level(prev) >= 0 else None

    def _remove_block(self, block: QTextBlock, to_previous: bool = False) -> None:
        """Delete a whole block (its separator included); the caret lands at the start of the next one (or, with
        ``to_previous``, at the end of the one above), else wherever there is a block left."""
        doc = self.document()
        cur = QTextCursor(doc)
        nxt, prev = block.next(), block.previous()
        use_next = nxt.isValid() and QTextCursor(nxt).currentTable() is None
        use_prev = prev.isValid() and QTextCursor(prev).currentTable() is None
        if use_next and not (to_previous and use_prev):
            cur.setPosition(block.position())
            cur.setPosition(nxt.position(), QTextCursor.KeepAnchor)
            cur.removeSelectedText()
            self.setTextCursor(QTextCursor(doc.findBlock(block.position())))
        elif use_prev:
            cur.setPosition(prev.position() + prev.length() - 1)
            cur.setPosition(block.position() + block.length() - 1, QTextCursor.KeepAnchor)
            cur.removeSelectedText()
            end = QTextCursor(doc)
            end.setPosition(min(prev.position() + prev.length() - 1, doc.characterCount() - 1))
            self.setTextCursor(end)
        else:
            nd.set_block_kind(block, "paragraph")

    def _delete(self) -> bool:
        """Lifedash notes.js: deleteKey. At the end of a block the next block's text joins this one; a divider
        right after it simply goes."""
        cur = self.textCursor()
        if cur.hasSelection() or not cur.atBlockEnd():
            return False
        block = cur.block()
        if cur.currentTable() is not None:
            return True
        nxt = block.next()
        if not nxt.isValid():
            return True
        if QTextCursor(nxt).currentTable() is not None:
            return True
        cur.beginEditBlock()
        try:
            if nd.is_rule(nxt):
                end = QTextCursor(self.document())
                end.setPosition(block.position() + block.length() - 1)
                end.setPosition(nxt.position() + nxt.length() - 1, QTextCursor.KeepAnchor)
                end.removeSelectedText()
                return True
            if nd.block_kind(block) in ("h1", "h2", "h3", "code") or nd.code_of(nxt):
                nd.restyle_block(nxt, (nd.heading_of(nxt), bool(nd.code_of(nxt))),
                                 (nd.heading_of(block), bool(nd.code_of(block))))
            return False
        finally:
            cur.endEditBlock()

    # -- Tab -----------------------------------------------------------------------------

    def _tab(self, back: bool) -> None:
        """Tab / Shift+Tab: indent / outdent in a list, move between cells in a table, else move focus like any
        field (Lifedash notes.js: tabKey)."""
        cur = self.textCursor()
        if cur.currentTable() is not None:
            self._table_move(back)
            return
        if any(nd.item_level(b) >= 0 for b in self._selected_blocks()):
            self._indent(out=back)
            self._emit_state()
            return
        self.focusNextPrevChild(not back)

    def _table_move(self, back: bool) -> None:
        cur = self.textCursor()
        table = cur.currentTable()
        cell = table.cellAt(cur)
        row, col = cell.row(), cell.column()
        if back:
            col -= 1
            if col < 0:
                row, col = row - 1, table.columns() - 1
            if row < 0:
                return
        else:
            col += 1
            if col >= table.columns():
                row, col = row + 1, 0
            if row >= table.rows():
                table.appendRows(1)  # Tab out of the last cell adds a row
        self.setTextCursor(table.cellAt(row, col).lastCursorPosition())

    # -- inline autoformat (Lifedash notes.js: inlineAutoformat) --------------------------

    def _inline_autoformat(self) -> bool:
        """`**b**`, `*i*`/`_i_`, `` `c` ``, `~~s~~` become formatting when the closing marker is typed (never in
        code, inline code or a link; snake_case stays text). One undo step reverts it."""
        cur = self.textCursor()
        if cur.hasSelection():
            return False
        block = cur.block()
        if nd.code_of(block) or nd.is_rule(block):
            return False
        pib = cur.positionInBlock()
        before = block.text()[:pib]
        probe = cur.charFormat()
        if not before or before[-1] not in "*_`~" or probe.isAnchor() or probe.boolProperty(nd.CHAR_CODE):
            return False
        semibold = nd.semibold_block(block)
        for kind, width, rx in INLINE_RULES:
            found = rx.search(before)
            if not found or nd.char_has(probe, kind, semibold):
                continue
            content = found.group(2)
            start = pib - len(content) - width * 2
            base = block.position()
            doc = self.document()
            cur.beginEditBlock()
            edit = QTextCursor(doc)
            edit.setPosition(base + pib - width)
            edit.setPosition(base + pib, QTextCursor.KeepAnchor)
            edit.removeSelectedText()
            edit.setPosition(base + start)
            edit.setPosition(base + start + width, QTextCursor.KeepAnchor)
            edit.removeSelectedText()
            nd.format_range(doc, base + start, base + start + len(content), kind, True)
            caret = self.textCursor()
            caret.setPosition(base + start + len(content))
            self.setTextCursor(caret)
            cur.endEditBlock()
            fmt = QTextCharFormat(self.currentCharFormat())
            nd.inline_editor(kind, False, nd.heading_of(block), semibold)(fmt)
            self.setCurrentCharFormat(fmt)  # what is typed next is outside the span
            return True
        return False

    def _after_space(self) -> None:
        """A space ends a word: link a bare URL before it, and keep the space itself out of a link."""
        cur = self.textCursor()
        block = cur.block()
        cur.beginEditBlock()
        nd.autolink_block(block)
        before = QTextCursor(self.document())
        before.setPosition(cur.position())
        probe = before.charFormat()
        if probe.isAnchor():
            end = nd.anchor_range(self.document(), cur.position())
            if end is not None and end[1] == cur.position():
                nd.set_link(self.document(), cur.position() - 1, cur.position(), "")
        cur.endEditBlock()
        plain = QTextCharFormat(self.currentCharFormat())
        if plain.isAnchor():
            nd.clear_link(plain)
            self.setCurrentCharFormat(plain)

    # -- the "/" menu (Lifedash notes.js: slashInfo, slashUpdate, chooseBlock) ---------------

    def _slash_info(self) -> Optional[Tuple[int, int, str]]:
        """(position of the "/", caret position, query) when the caret is typing a "/" command, else None.
        The "/" has to start the block or follow whitespace, so "3/4" and a URL's own slashes never open it."""
        cur = self.textCursor()
        if cur.hasSelection() or cur.currentTable() is not None:
            return None
        block = cur.block()
        if nd.code_of(block) or nd.is_rule(block):
            return None  # literal in code
        pib = cur.positionInBlock()
        text = block.text()[:pib]
        at = text.rfind("/")
        if at == -1 or (at > 0 and not text[at - 1].isspace()):
            return None
        return block.position() + at, block.position() + pib, text[at + 1:]

    def _slash_update(self) -> None:
        info = self._slash_info()
        if info is None:
            self._slash_dismissed = -1
            self._close_slash()
            return
        if info[0] == self._slash_dismissed:
            return
        if self._slash.set_filter(info[2]) == 0:
            self._close_slash()  # nothing matches: an unrelated "/" mid-sentence never pins the menu open
            return
        rect = self.cursorRect()
        self._slash.show_at(self.viewport().mapToGlobal(rect.bottomLeft() + QPoint(0, 2)))

    def _close_slash(self) -> None:
        if self._slash.isVisible():
            self._slash.hide()

    def _slash_key(self, event) -> bool:
        key = event.key()
        if key == Qt.Key_Down:
            self._slash.move_active(1)
        elif key == Qt.Key_Up:
            self._slash.move_active(-1)
        elif key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Tab):
            choice = self._slash.active_key()
            if choice is None:
                return False
            self._choose_slash(choice)
        elif key == Qt.Key_Escape:
            info = self._slash_info()
            self._slash_dismissed = info[0] if info else -1
            self._close_slash()
        else:
            return False
        event.accept()
        return True

    def _choose_slash(self, key: str) -> None:
        """Lifedash notes.js: chooseBlock. Delete the "/query" and nothing else, then run the same command a toolbar
        click runs on the current block."""
        info = self._slash_info()
        self._close_slash()
        cur = self.textCursor()
        cur.beginEditBlock()
        if info is not None:
            edit = QTextCursor(self.document())
            edit.setPosition(info[0])
            edit.setPosition(info[1], QTextCursor.KeepAnchor)
            edit.removeSelectedText()
        self.setFocus(Qt.OtherFocusReason)
        self.run_command(key)
        cur.endEditBlock()

    # -- state (Lifedash notes.js: syncToolbar) -------------------------------------------

    def state(self) -> Dict:
        """Which toolbar keys are on at the caret, plus ``block``: the current block's kind."""
        cur = self.textCursor()
        block = cur.block()
        fmt = self.currentCharFormat()
        semibold = nd.semibold_block(block)
        state = {k: False for k in _STATE_KEYS}
        for kind in ("bold", "italic", "strike", "code"):
            state[kind] = nd.char_has(fmt, kind, semibold)
        blocks = self._selected_blocks()
        kinds = [nd.block_kind(b) for b in blocks]
        for key, kind in (("h1", "h1"), ("h2", "h2"), ("h3", "h3"), ("bullet", "bullet"), ("number", "number"),
                          ("task", "task"), ("codeblock", "code")):
            state[key] = bool(kinds) and all(k == kind for k in kinds)
        state["quote"] = bool(blocks) and all(nd.quote_of(b) for b in blocks)
        state["block"] = nd.block_kind(block)
        return state

    def _emit_state(self) -> None:
        self.state_changed.emit(self.state())

    # -- painting -------------------------------------------------------------------------

    def _visible_blocks(self):
        layout = self.document().documentLayout()
        dx, dy = self.horizontalScrollBar().value(), self.verticalScrollBar().value()
        height = self.viewport().height()
        block = self.cursorForPosition(QPoint(0, 0)).block()
        while block.isValid():
            rect = layout.blockBoundingRect(block).translated(-dx, -dy)
            if rect.top() > height:
                break
            if block.isVisible():
                yield block, rect
            block = block.next()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self.viewport())
        self._paint_decorations(painter)
        painter.end()
        super().paintEvent(event)
        self._paint_caret_hint()

    def _paint_decorations(self, painter: QPainter) -> None:
        """Quote bars, code panels and divider hairlines, drawn under the text."""
        tokens = theme.tokens()
        width = self.viewport().width()
        painter.setRenderHint(QPainter.Antialiasing, True)
        run: Dict[str, Optional[List]] = {"quote": None, "code": None}

        def close(kind: str) -> None:
            span = run[kind]
            run[kind] = None
            if not span:
                return
            top, bottom = span[1] - 3, span[2] + 3
            if kind == "quote":
                painter.fillRect(QRectF(2, top, 3, bottom - top), QColor(tokens["accent"]))
            else:
                painter.setPen(QColor(tokens["border"]))
                painter.setBrush(QColor(tokens["panel"]))
                painter.drawRoundedRect(QRectF(0.5, top, width - 1, bottom - top), 5, 5)

        for block, rect in self._visible_blocks():
            for kind, ident in (("quote", nd.quote_of(block)), ("code", nd.code_of(block))):
                span = run[kind]
                if span is not None and span[0] != ident:
                    close(kind)
                    span = None
                if ident:
                    if span is None:
                        run[kind] = [ident, rect.top(), rect.bottom()]
                    else:
                        span[2] = rect.bottom()
            if nd.is_rule(block):
                painter.fillRect(QRectF(0, round((rect.top() + rect.bottom()) / 2), width, 1), QColor(tokens["border"]))
        close("quote")
        close("code")

    def _paint_caret_hint(self) -> None:
        """Lifedash ``is-caret-empty``: "Type / for commands" at the caret on an empty plain line (the whole-document
        empty state shows the placeholder instead)."""
        if not self.hasFocus() or self.isReadOnly() or self.document().isEmpty():
            return
        cur = self.textCursor()
        block = cur.block()
        if cur.hasSelection() or block.text() or cur.currentTable() is not None:
            return
        if nd.block_kind(block) != "paragraph" or nd.quote_of(block):
            return
        rect = self.cursorRect()
        painter = QPainter(self.viewport())
        painter.setFont(self.font())
        painter.setPen(QColor(theme.tokens()["muted"]))
        painter.drawText(QRect(rect.left() + 4, rect.top(), max(40, self.viewport().width() - rect.left() - 4), rect.height()),
                         int(Qt.AlignVCenter | Qt.AlignLeft), CARET_HINT)
        painter.end()

    def changeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().changeEvent(event)
        if event.type() == QEvent.PaletteChange:
            self.viewport().update()


__all__ = ["BLOCKS", "FlowLayout", "LinkCard", "NotesEditor", "NotesToolbar", "OPEN_HINT", "SlashMenu"]
