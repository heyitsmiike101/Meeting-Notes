"""The notes scratchpad: a "Your notes" pane for free text and links during a meeting.

Behaviour follows Life Dash's notes: the text is kept exactly as typed (Markdown), it saves itself on every pause
in typing, and bare http(s) links are recognised with the same rule as Life Dash and the server's meeting page
(``meeting_notes/scratchpad.py``). In the editor links are coloured and open with Ctrl+click (Cmd+click on macOS),
since a plain click has to place the caret. A Preview button swaps the editor for the text rendered the way the
server's meeting page renders it (``meeting_notes/markdown.py``, Life Dash's rules); there a plain click opens a link.

One :class:`ScratchpadModel` owns the text (a shared ``QTextDocument``) and the saving; any number of
:class:`ScratchpadView` widgets show it: one in the main window's side pane and one in the
:class:`ScratchpadPopout` that opens beside the compact bar. Saving goes through ``RecordingController.save_scratchpad``
(the session folder while recording, else a draft the next recording takes over).
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Callable, Optional

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QColor, QDesktopServices, QGuiApplication, QSyntaxHighlighter, QTextCharFormat, QTextCursor, QTextDocument,
)
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPlainTextDocumentLayout, QPlainTextEdit, QStackedLayout, QTextBrowser, QToolButton,
    QVBoxLayout, QWidget,
)

from meeting_notes import markdown as markdown_mod
from meeting_notes import scratchpad as scratchpad_mod
from meeting_notes.client.ui import theme

log = logging.getLogger("meeting_notes.client.ui.scratchpad")

# Life Dash autosaves on a pause in typing; this is that pause.
SAVE_DELAY_MS = 800
PANE_WIDTH = 340
POPOUT_SIZE = (340, 420)
POPOUT_GAP = 8

TITLE = "Your notes"
PLACEHOLDER = (
    "Links and notes for this meeting. They are saved as you type, sent with the recording "
    "and used when the meeting notes are written."
)
OPEN_HINT = "Cmd+click to open" if sys.platform == "darwin" else "Ctrl+click to open"
PREVIEW_HINT = "Rendered as the meeting page shows it. Click a link to open it."
# A preview that is showing follows edits made in the other view (the pane and the pop-out share one document).
PREVIEW_DELAY_MS = 300
# Qt's rich text has no form controls: task-list boxes become the characters the web page's boxes look like.
_TASK_DONE = "☑ "  # ballot box with check
_TASK_OPEN = "☐ "  # ballot box
_TASK_INPUT = re.compile(r"<input\b[^>]*>\s*")
SAVED = "Saved"
SAVING = "Saving..."
NOT_SAVED = "Not saved"
LOCKED = "Finishing the recording..."


class _LinkHighlighter(QSyntaxHighlighter):
    """Colours and underlines every link, block by block (a link never spans lines)."""

    def highlightBlock(self, text: str) -> None:  # noqa: N802 - Qt naming
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(theme.tokens()["accent_text"]))
        fmt.setFontUnderline(True)
        for start, end, _url in scratchpad_mod.link_spans(text):
            self.setFormat(start, end - start, fmt)


class ScratchpadModel(QObject):
    """The scratchpad's text and its saving; views share :attr:`document`."""

    status_changed = Signal(str)
    locked_changed = Signal(bool)

    def __init__(self, saver: Callable[[str], bool], parent=None):
        super().__init__(parent)
        self._saver = saver
        self.document = QTextDocument(self)
        self.document.setDocumentLayout(QPlainTextDocumentLayout(self.document))
        self.document.setDocumentMargin(0)
        self._highlighter = _LinkHighlighter(self.document)
        self._loading = False
        self._dirty = False
        self._locked = False
        self._status = ""
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(SAVE_DELAY_MS)
        self._timer.timeout.connect(self.flush)
        self.document.contentsChanged.connect(self._on_changed)
        theme.manager().changed.connect(lambda _name: self._highlighter.rehighlight())

    # -- state -------------------------------------------------------------------

    @property
    def status(self) -> str:
        return self._status

    @property
    def locked(self) -> bool:
        return self._locked

    def text(self) -> str:
        return self.document.toPlainText()

    def has_text(self) -> bool:
        return bool(self.text().strip())

    def _set_status(self, status: str) -> None:
        if status != self._status:
            self._status = status
            self.status_changed.emit(status)

    def set_locked(self, locked: bool) -> None:
        """Read-only while a recording is finishing: the text being sent can no longer change."""
        locked = bool(locked)
        if locked == self._locked:
            return
        self._locked = locked
        self.locked_changed.emit(locked)
        self._set_status(LOCKED if locked else (SAVED if self.has_text() else ""))

    # -- editing and saving ------------------------------------------------------

    def set_text(self, text: str) -> None:
        """Replace the text without saving it (e.g. the draft loaded at launch)."""
        self._loading = True
        try:
            self.document.setPlainText(text or "")
            self.document.clearUndoRedoStacks()
        finally:
            self._loading = False
        self._dirty = False
        self._set_status(SAVED if text else "")

    def clear(self) -> None:
        """A new meeting starts with an empty scratchpad (the old text went with its recording)."""
        self._timer.stop()
        self.set_text("")

    def _on_changed(self) -> None:
        if self._loading:
            return
        self._dirty = True
        self._set_status(SAVING)
        self._timer.start()

    def save_now(self) -> bool:
        """Save the text even when it has not changed since the last save."""
        self._dirty = bool(self.text()) or self._dirty
        return self.flush()

    def flush(self) -> bool:
        """Save now if anything changed since the last save. True when the text on disk is current."""
        self._timer.stop()
        if not self._dirty:
            return True
        try:
            ok = bool(self._saver(self.text()))
        except Exception:  # noqa: BLE001 - saving notes must never break the recorder
            log.exception("could not save the notes scratchpad")
            ok = False
        self._dirty = not ok
        self._set_status(SAVED if ok else NOT_SAVED)
        return ok


class _NotesEdit(QPlainTextEdit):
    """The editor: plain text, links open with Ctrl/Cmd+click."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("scratchpad")
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.setTabChangesFocus(True)

    def link_at(self, pos: QPoint) -> Optional[str]:
        cursor = self.cursorForPosition(pos)
        block = cursor.block()
        # cursorForPosition snaps to the nearest gap; only count a hit on the glyphs themselves.
        rect = self.cursorRect(cursor)
        if abs(rect.center().y() - pos.y()) > rect.height():
            return None
        index = cursor.positionInBlock()
        text = block.text()
        url = scratchpad_mod.link_at(text, index)
        if url is None and index > 0:
            url = scratchpad_mod.link_at(text, index - 1)
        return url

    @staticmethod
    def _open_modifier(event) -> bool:
        mods = event.modifiers()
        return bool(mods & (Qt.ControlModifier | Qt.MetaModifier))

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        url = self.link_at(event.position().toPoint())
        if url:
            self.viewport().setCursor(Qt.PointingHandCursor if self._open_modifier(event) else Qt.IBeamCursor)
            self.viewport().setToolTip(f"{url}\n{OPEN_HINT}")
        else:
            self.viewport().setCursor(Qt.IBeamCursor)
            self.viewport().setToolTip("")
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.LeftButton and self._open_modifier(event):
            url = self.link_at(event.position().toPoint())
            if url and not self.textCursor().hasSelection():
                QDesktopServices.openUrl(QUrl(url))
                event.accept()
                return
        super().mouseReleaseEvent(event)


def preview_html(text: str) -> str:
    """The scratchpad rendered for a ``QTextBrowser``: the server's HTML, with task boxes Qt can show."""

    def box(match) -> str:
        return _TASK_DONE if "checked" in match.group(0) else _TASK_OPEN

    return _TASK_INPUT.sub(box, markdown_mod.render(text))


class ScratchpadView(QFrame):
    """"Your notes": a title, the save status, an Edit/Preview switch and the editor. ``closable`` adds a close button."""

    close_requested = Signal()

    def __init__(self, model: ScratchpadModel, *, closable: bool = True, parent=None):
        super().__init__(parent)
        self.setObjectName("scratchpadPane")
        self.model = model
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 14)
        layout.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(8)
        self.title = QLabel(TITLE)
        self.title.setObjectName("section")
        head.addWidget(self.title)
        head.addStretch(1)
        self.status_label = QLabel("")
        self.status_label.setObjectName("subtle")
        self.status_label.setAccessibleName("Notes save status")
        head.addWidget(self.status_label)
        self.preview_button = QToolButton()
        self.preview_button.setObjectName("more")
        self.preview_button.setCheckable(True)
        self.preview_button.setText("Preview")
        self.preview_button.setToolTip("Show the notes as the meeting page will show them")
        self.preview_button.setAccessibleName("Preview your notes")
        self.preview_button.setCursor(Qt.PointingHandCursor)
        self.preview_button.setFocusPolicy(Qt.StrongFocus)
        self.preview_button.toggled.connect(self.set_previewing)
        head.addWidget(self.preview_button)
        self.close_button = QToolButton()
        self.close_button.setObjectName("more")
        self.close_button.setText("×")
        self.close_button.setToolTip("Hide your notes")
        self.close_button.setAccessibleName("Hide your notes")
        self.close_button.setCursor(Qt.PointingHandCursor)
        self.close_button.setFocusPolicy(Qt.StrongFocus)
        self.close_button.clicked.connect(lambda _c=False: self.close_requested.emit())
        self.close_button.setVisible(closable)
        head.addWidget(self.close_button)
        layout.addLayout(head)

        self.editor = _NotesEdit()
        self.editor.setDocument(model.document)
        self.editor.setAccessibleName(TITLE)
        self.editor.setPlaceholderText(PLACEHOLDER)
        self.preview = QTextBrowser()
        self.preview.setObjectName("scratchpadPreview")
        self.preview.setAccessibleName(f"{TITLE}, preview")
        self.preview.setOpenExternalLinks(True)  # the renderer only ever emits http(s) links
        self.preview.setOpenLinks(True)
        self.pages = QStackedLayout()
        self.pages.addWidget(self.editor)
        self.pages.addWidget(self.preview)
        layout.addLayout(self.pages, 1)
        self.hint = QLabel(f"Links are kept in the notes. {OPEN_HINT} a link.")
        self.hint.setObjectName("subtle")
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)

        model.status_changed.connect(self._on_status)
        model.locked_changed.connect(self._on_locked)
        self._on_status(model.status)
        self._on_locked(model.locked)
        # Leaving the editor saves at once rather than after the pause.
        self.editor.installEventFilter(self)
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(PREVIEW_DELAY_MS)
        self._preview_timer.timeout.connect(self._render_preview)
        model.document.contentsChanged.connect(self._preview_stale)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 - Qt naming
        if obj is self.editor and event.type() == QEvent.FocusOut:
            self.model.flush()
        return super().eventFilter(obj, event)

    @property
    def previewing(self) -> bool:
        return self.pages.currentWidget() is self.preview

    def set_previewing(self, on: bool) -> None:
        """Show the rendered notes (``on``) or the editor; switching to the preview saves first."""
        if on:
            self.model.flush()
            self._render_preview()
        self.pages.setCurrentWidget(self.preview if on else self.editor)
        self.hint.setText(PREVIEW_HINT if on else f"Links are kept in the notes. {OPEN_HINT} a link.")
        if self.preview_button.isChecked() != on:
            self.preview_button.setChecked(on)
        self.preview_button.setText("Edit" if on else "Preview")
        self.preview_button.setToolTip("Back to editing" if on else "Show the notes as the meeting page will show them")
        if not on:
            self.focus_editor()

    def _preview_stale(self) -> None:
        if self.previewing:
            self._preview_timer.start()

    def _render_preview(self) -> None:
        self.preview.setHtml(preview_html(self.model.text()))

    def _on_status(self, status: str) -> None:
        self.status_label.setText(status)

    def _on_locked(self, locked: bool) -> None:
        self.editor.setReadOnly(locked)

    def focus_editor(self) -> None:
        if self.previewing:
            self.set_previewing(False)
            return
        self.editor.setFocus(Qt.OtherFocusReason)
        self.editor.moveCursor(QTextCursor.End)


class ScratchpadPopout(QWidget):
    """The scratchpad as its own small always-on-top window, docked to the left of the compact bar."""

    hidden_by_user = Signal()

    def __init__(self, model: ScratchpadModel, parent=None):
        super().__init__(parent)
        self.setObjectName("root")
        self.setWindowTitle(f"{TITLE} - Meeting Notes")
        self.setWindowFlags(Qt.Tool | Qt.WindowStaysOnTopHint)
        self.resize(*POPOUT_SIZE)
        self.setMinimumSize(260, 220)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.view = ScratchpadView(model, closable=True)
        self.view.close_requested.connect(self._hide_by_user)
        layout.addWidget(self.view)

    def _hide_by_user(self) -> None:
        self.hide()
        self.hidden_by_user.emit()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self.view.model.flush()
        super().closeEvent(event)
        self.hidden_by_user.emit()

    def dock_beside(self, anchor: QRect, screen=None) -> None:
        """Place the window just left of ``anchor`` (the compact bar), vertically centred on it, inside the screen."""
        screen = screen or QGuiApplication.screenAt(anchor.center()) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry() if screen is not None else QRect(0, 0, 1920, 1040)
        frame = self.frameGeometry()
        width, height = max(frame.width(), self.width()), max(frame.height(), self.height())
        x = anchor.left() - POPOUT_GAP - width
        y = anchor.center().y() - height // 2
        x = max(area.left(), min(x, area.right() + 1 - width))
        y = max(area.top(), min(y, area.bottom() + 1 - height))
        self.move(x, y)

