"""The macOS "Meeting Notes needs a few permissions" panel that sits over the main window.

It is a child of the window body, not a dialog: the topbar stays visible around it, the card area
behind it is dimmed, and Settings can still be opened. The window feeds it the current
:class:`~meeting_notes.client.permissions.Permission` list; it only draws and emits signals.
"""

from __future__ import annotations

from typing import List

from PySide6.QtCore import QEvent, QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from meeting_notes.client import permissions
from meeting_notes.client.ui import theme

TITLE = "Meeting Notes needs a few permissions"
SCRIM_ALPHA = 215  # of 255: the window behind stays visible but quiet
CARD_MAX_WIDTH = 640

_TONE = {permissions.GRANTED: "ok", permissions.NOT_GRANTED: "warn"}


def steps_text(steps: List[str]) -> str:
    return "\n".join(f"{n}. {text}" for n, text in enumerate(steps, 1))


class PermissionsOverlay(QWidget):
    open_settings = Signal(str)  # a System Settings deep link
    allow_microphone = Signal()
    quit_and_reopen = Signal()
    check_again = Signal()
    not_now = Signal()

    def __init__(self, parent=None, anchor=None):
        """``anchor`` (a widget inside ``parent``): the panel covers from the anchor's top edge down,
        so anything above it (the header, the alert strips) stays visible and clickable."""
        super().__init__(parent)
        self._anchor = anchor
        self.setObjectName("permOverlay")
        self.setAttribute(Qt.WA_StyledBackground, False)
        self._bundled = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 16, 20, 16)
        row = QHBoxLayout()
        row.addStretch(1)
        self.card = QFrame()
        self.card.setObjectName("permCard")
        self.card.setMaximumWidth(CARD_MAX_WIDTH)
        row.addWidget(self.card, 100)
        row.addStretch(1)
        outer.addLayout(row, 1)  # the card takes the whole panel height; its list scrolls when short

        column = QVBoxLayout(self.card)
        column.setContentsMargins(20, 18, 20, 16)
        column.setSpacing(10)
        self.title_label = QLabel(TITLE)
        self.title_label.setObjectName("permTitle")
        self.title_label.setWordWrap(True)
        column.addWidget(self.title_label)
        self.intro_label = QLabel("")
        self.intro_label.setObjectName("permNote")
        self.intro_label.setWordWrap(True)
        column.addWidget(self.intro_label)

        self.scroll = QScrollArea()
        self.scroll.setObjectName("permScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list_widget = QWidget()
        self.list_widget.setObjectName("permList")
        self.list_layout = QVBoxLayout(self.list_widget)
        self.list_layout.setContentsMargins(0, 0, 6, 0)
        self.list_layout.setSpacing(8)
        self.scroll.setWidget(self.list_widget)
        column.addWidget(self.scroll, 1)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        footer.addStretch(1)
        self.not_now_button = QPushButton("Not now")
        self.not_now_button.setToolTip("Hide this for now; a Fix button stays above the recorder")
        self.not_now_button.clicked.connect(self.not_now)
        self.check_button = QPushButton("Check again")
        self.check_button.setDefault(True)
        self.check_button.clicked.connect(self.check_again)
        footer.addWidget(self.not_now_button)
        footer.addWidget(self.check_button)
        column.addLayout(footer)

        self.rows: dict = {}  # permission key -> row widgets (for tests and the screenshots)
        if parent is not None:
            parent.installEventFilter(self)
        if anchor is not None:
            anchor.installEventFilter(self)
        self.setVisible(False)

    # -- placement -------------------------------------------------------------------

    def reposition(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        top = 0
        if self._anchor is not None:
            top = max(0, self._anchor.mapTo(parent, QPoint(0, 0)).y())
        self.setGeometry(QRect(0, top, parent.width(), max(0, parent.height() - top)))

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 - Qt naming
        if event.type() in (QEvent.Resize, QEvent.Move, QEvent.LayoutRequest, QEvent.Show):
            self.reposition()
        return False

    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self.reposition()
        self.raise_()
        super().showEvent(event)

    # -- content ---------------------------------------------------------------------

    def set_items(self, items: List["permissions.Permission"], *, bundled: bool = False) -> None:
        self._bundled = bundled
        self._clear_rows()
        for item in items:
            self.list_layout.addWidget(self._build_row(item))
        self.list_layout.addStretch(1)
        screen_missing = any(p.key == permissions.SCREEN and p.needed for p in items)
        mic_ok = any(p.key == permissions.MICROPHONE and p.status == permissions.GRANTED for p in items)
        text = "Meeting Notes only records while these are switched on in macOS."
        if screen_missing and mic_ok:
            text = (
                "You can still record your microphone without Screen & System Audio Recording, "
                "but the other people on a call will not be captured."
            )
        elif screen_missing:
            text += " Without Screen & System Audio Recording, only your microphone is recorded."
        self.intro_label.setText(text)

    def _clear_rows(self) -> None:
        self.rows.clear()
        while self.list_layout.count():
            item = self.list_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def _build_row(self, item) -> QFrame:
        frame = QFrame()
        frame.setObjectName("permRow")
        frame.setProperty("needed", "true" if item.needed else "false")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(14, 10, 14, 12)
        layout.setSpacing(6)
        head = QHBoxLayout()
        title = QLabel(item.title)
        title.setObjectName("permName")
        head.addWidget(title, 1)
        badge = QLabel(item.status_text)
        badge.setObjectName("recBadge")
        badge.setProperty("tone", _TONE.get(item.status, "muted"))
        head.addWidget(badge, 0, Qt.AlignVCenter)
        layout.addLayout(head)
        parts = {"frame": frame, "title": title, "badge": badge, "steps": None, "note": None,
                 "open": None, "allow": None, "restart": None}
        if item.status != permissions.GRANTED:
            note = QLabel(item.note)
            note.setObjectName("permNote")
            note.setWordWrap(True)
            layout.addWidget(note)
            steps = QLabel(steps_text(item.steps))
            steps.setObjectName("permStep")
            steps.setWordWrap(True)
            layout.addWidget(steps)
            buttons = QHBoxLayout()
            buttons.setSpacing(8)
            open_button = QPushButton("Open System Settings")
            open_button.clicked.connect(lambda _c=False, url=item.settings_url: self.open_settings.emit(url))
            buttons.addWidget(open_button)
            parts.update(note=note, steps=steps, open=open_button)
            if item.can_request:
                allow = QPushButton("Allow microphone")
                allow.clicked.connect(self.allow_microphone)
                buttons.addWidget(allow)
                parts["allow"] = allow
            if item.needs_restart and self._bundled:
                restart = QPushButton("Quit and reopen")
                restart.setToolTip("Needed after switching the permission on")
                restart.clicked.connect(self.quit_and_reopen)
                buttons.addWidget(restart)
                parts["restart"] = restart
            buttons.addStretch(1)
            layout.addLayout(buttons)
        self.rows[item.key] = parts
        return frame

    # -- drawing ---------------------------------------------------------------------

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        colour = QColor(theme.tokens()["bg"])
        colour.setAlpha(SCRIM_ALPHA)
        painter.fillRect(self.rect(), colour)
        painter.end()

    def mousePressEvent(self, event) -> None:  # noqa: N802 - the scrim swallows clicks on what it covers
        event.accept()
