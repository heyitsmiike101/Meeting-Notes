"""The notes scratchpad: a "Your notes" pane for free text and links during a meeting.

Behaviour follows Life Dash's notes: the text is stored as Markdown exactly as the editor writes it, it saves itself on
every pause in typing, and bare http(s) links are recognised with the same rule as Life Dash and the server's meeting
page (``meeting_notes/scratchpad.py``). The pane is a native port of Life Dash's notes editor: a formatting top bar, a
"/" menu and a WYSIWYG surface (``notes_editor.py``); Markdown is what is stored, never what is shown
(``notes_doc.py`` loads and serializes it). Links are coloured and open with Ctrl+click (Cmd+click on macOS), since a
plain click has to place the caret.

One :class:`ScratchpadModel` owns the text (a shared ``QTextDocument``) and the saving; any number of
:class:`ScratchpadView` widgets show it: one in the main window's notes view and one in the
:class:`ScratchpadPopout` that opens beside the compact bar. Saving goes through ``RecordingController.save_scratchpad``
(the session folder while recording, else a draft the next recording takes over).

The popout is an ordinary resizable window that remembers its size between runs in the config's ``ui_state``
(``notes_popout_size``).

Co-editing: the Recorders page can type into the same notes at the same time (``set_notes``, merged line by line in
``meeting_notes/notes_merge.py``). The text goes to the server on a shorter pause than the save (``LIVE_DELAY_MS``),
and a merge from the page is loaded with every view's caret kept on its line (``apply_text``).
"""

from __future__ import annotations

import logging
from typing import Callable

from PySide6.QtCore import QEvent, QObject, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QTextCursor, QTextDocument
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QToolButton, QVBoxLayout, QWidget

from meeting_notes import config as config_mod
from meeting_notes import notes_merge
from meeting_notes.client.ui import notes_doc, theme
from meeting_notes.client.ui.notes_editor import OPEN_HINT, LinkCard, NotesEditor, NotesToolbar

log = logging.getLogger("meeting_notes.client.ui.scratchpad")

# Life Dash autosaves on a pause in typing; this is that pause.
SAVE_DELAY_MS = 800
LIVE_DELAY_MS = 250  # co-editing: the pause before the text goes to the server (the Recorders page sees it)
POPOUT_SIZE = (340, 420)  # the compact-view window's size until the user resizes it
POPOUT_MIN_SIZE = (260, 220)
POPOUT_GAP = 8
SIZE_SAVE_DELAY_MS = 400  # a resize is written once the drag has settled

TITLE = "Your notes"
PLACEHOLDER = (
    "Links and notes for this meeting. They are saved as you type, sent with the recording "
    "and used when the meeting notes are written."
)
SAVED = "Saved"
SAVING = "Saving..."
NOT_SAVED = "Not saved"
LOCKED = "Finishing the recording..."


# -- remembered sizes ---------------------------------------------------------------------------------------


def remembered_popout_size(state=None) -> tuple:
    """The popout window's size from the saved UI state (``notes_popout_size``), else the default."""
    try:
        state = config_mod.ui_state() if state is None else state
        raw = (state or {}).get("notes_popout_size")
        width, height = int(raw[0]), int(raw[1])
    except Exception:  # noqa: BLE001 - missing, malformed or unreadable: the default
        return POPOUT_SIZE
    return (max(POPOUT_MIN_SIZE[0], width), max(POPOUT_MIN_SIZE[1], height))


def remember(**changes) -> None:
    """Write a size into the saved UI state; remembering the layout must never break the window."""
    try:
        config_mod.update_ui_state(**changes)
    except Exception:  # noqa: BLE001
        log.debug("could not save the notes layout", exc_info=True)


class ScratchpadModel(QObject):
    """The scratchpad's text and its saving; views share :attr:`document`."""

    status_changed = Signal(str)
    locked_changed = Signal(bool)
    edited = Signal()           # typed here, after LIVE_DELAY_MS: send the text to the server
    reloading = Signal()        # a merged text is about to replace the document (views note their caret)
    reloaded = Signal(object)   # ...and has: a line map old block index -> new (views put their caret back)

    def __init__(self, saver: Callable[[str], bool], parent=None):
        super().__init__(parent)
        self._saver = saver
        self.document = QTextDocument(self)
        self.document.setDocumentMargin(0)
        self._loading = False
        self._dirty = False
        self._locked = False
        self._status = ""
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(SAVE_DELAY_MS)
        self._timer.timeout.connect(self.flush)
        self._live_timer = QTimer(self)
        self._live_timer.setSingleShot(True)
        self._live_timer.setInterval(LIVE_DELAY_MS)
        self._live_timer.timeout.connect(self.edited.emit)
        self.document.contentsChanged.connect(self._on_changed)
        theme.manager().changed.connect(lambda _name: self._restyle())

    # -- state -------------------------------------------------------------------

    @property
    def status(self) -> str:
        return self._status

    @property
    def locked(self) -> bool:
        return self._locked

    def text(self) -> str:
        """The notes as Markdown (the document walked back into the text that is stored and sent)."""
        return notes_doc.serialize(self.document)

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
            notes_doc.load(self.document, text or "")
            self.document.clearUndoRedoStacks()
        finally:
            self._loading = False
        self._dirty = False
        self._set_status(SAVED if text else "")

    def apply_text(self, text: str) -> bool:
        """Replace the text with one merged from the Recorders page and save it as if typed here.

        Every view keeps its caret on its line (``reloading``/``reloaded``): the person typing here carries on where
        they were while the other person's lines appear around them.
        """
        self._timer.stop()
        self._live_timer.stop()
        old = self._block_texts()
        self.reloading.emit()
        self.set_text(text)
        self.reloaded.emit(notes_merge.line_map(old, self._block_texts()))
        self._dirty = True  # saved (and sent) even when empty: an emptied pad is an edit too
        return self.flush()

    def _block_texts(self) -> list:
        block, out = self.document.firstBlock(), []
        while block.isValid():
            out.append(block.text())
            block = block.next()
        return out

    def _restyle(self) -> None:
        """Links and inline code carry their colours in the text; a theme change recolours them (not an edit)."""
        self._loading = True
        try:
            notes_doc.restyle_theme(self.document)
        finally:
            self._loading = False

    def clear(self) -> None:
        """A new meeting starts with an empty scratchpad (the old text went with its recording)."""
        self._timer.stop()
        self._live_timer.stop()
        self.set_text("")

    def _on_changed(self) -> None:
        if self._loading:
            return
        self._dirty = True
        self._set_status(SAVING)
        self._timer.start()
        self._live_timer.start()

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


class ScratchpadView(QFrame):
    """"Your notes": a title, the save status, the formatting bar and the editor. ``closable`` adds a close button."""

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

        self.editor = NotesEditor()
        self.editor.setDocument(model.document)
        self.editor.ensurePolished()
        model.document.setDefaultFont(self.editor.font())
        self.editor.setAccessibleName(TITLE)
        self.editor.setPlaceholderText(PLACEHOLDER)
        self.toolbar = NotesToolbar(self.editor)
        self.link_card = LinkCard()
        self.editor.attach_link_card(self.link_card)
        layout.addWidget(self.toolbar)
        layout.addWidget(self.link_card)
        layout.addWidget(self.editor, 1)
        self.hint = QLabel(f"Links are kept in the notes. {OPEN_HINT} a link.")
        self.hint.setObjectName("subtle")
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)

        model.status_changed.connect(self._on_status)
        model.locked_changed.connect(self._on_locked)
        model.reloading.connect(self._note_place)
        model.reloaded.connect(self._restore_place)
        self._place = None
        self._on_status(model.status)
        self._on_locked(model.locked)
        # Leaving the editor saves at once rather than after the pause.
        self.editor.installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 - Qt naming
        if obj is self.editor and event.type() == QEvent.FocusOut:
            self.model.flush()
        return super().eventFilter(obj, event)

    def _on_status(self, status: str) -> None:
        self.status_label.setText(status)

    def _note_place(self) -> None:
        """Before a merge reloads the notes: where the caret is (line and column) and how far it is scrolled."""
        cursor = self.editor.textCursor()
        doc = self.model.document
        anchor = doc.findBlock(cursor.anchor())
        self._place = (
            cursor.blockNumber(), cursor.positionInBlock(),
            anchor.blockNumber(), cursor.anchor() - anchor.position(),
            self.editor.verticalScrollBar().value(),
        )

    def _restore_place(self, where) -> None:
        """After the reload: the caret (and selection) back on the same lines, the scroll where it was."""
        place, self._place = self._place, None
        if place is None:
            return
        line, column, anchor_line, anchor_column, scroll = place
        doc = self.model.document

        def position(block_number: int, col: int) -> int:
            block = doc.findBlockByNumber(where(block_number))
            if not block.isValid():
                block = doc.lastBlock()
            return block.position() + max(0, min(col, block.length() - 1))

        cursor = QTextCursor(doc)
        cursor.setPosition(position(anchor_line, anchor_column))
        cursor.setPosition(position(line, column), QTextCursor.KeepAnchor)
        self.editor.setTextCursor(cursor)
        self.editor.verticalScrollBar().setValue(scroll)

    def _on_locked(self, locked: bool) -> None:
        self.editor.setReadOnly(locked)
        self.toolbar.setEnabled(not locked)
        if locked:
            self.link_card.setVisible(False)

    def focus_editor(self) -> None:
        self.editor.setFocus(Qt.OtherFocusReason)
        self.editor.moveCursor(QTextCursor.End)


class ScratchpadPopout(QWidget):
    """The scratchpad as its own small always-on-top window, docked to the left of the compact bar.

    It opens at the size it had last time (``notes_popout_size`` in the saved UI state) and remembers a resize once
    the drag has settled.
    """

    hidden_by_user = Signal()

    def __init__(self, model: ScratchpadModel, parent=None):
        super().__init__(parent)
        self.setObjectName("root")
        self.setWindowTitle(f"{TITLE} - Meeting Notes")
        self.setWindowFlags(Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setMinimumSize(*POPOUT_MIN_SIZE)
        self._saved_size = remembered_popout_size()
        self.resize(*self._saved_size)
        self._size_timer = QTimer(self)
        self._size_timer.setSingleShot(True)
        self._size_timer.setInterval(SIZE_SAVE_DELAY_MS)
        self._size_timer.timeout.connect(self._remember_size)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.view = ScratchpadView(model, closable=True)
        self.view.close_requested.connect(self._hide_by_user)
        layout.addWidget(self.view)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        if (self.width(), self.height()) != self._saved_size:
            self._size_timer.start()

    def _remember_size(self) -> None:
        size = (self.width(), self.height())
        if size == self._saved_size:
            return
        self._saved_size = size
        remember(notes_popout_size=list(size))

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

