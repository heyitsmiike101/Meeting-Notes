"""The "record this meeting?" prompt shown when a call is detected."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from meeting_notes.client.ui import theme
from meeting_notes.client.ui.icons import icon_size, make_icon

AUTO_DISMISS_MS = 60_000
_MARGIN = 6          # plus the card's own shadow gutter, below
_SHADOW = 14


class MeetingPrompt(QDialog):
    """Small always-on-top, non-modal card in the bottom-right of the screen."""

    record_requested = Signal(str)
    dismissed = Signal()

    def __init__(self, label: str, suggested_name: str, parent=None, timeout_ms: int = AUTO_DISMISS_MS):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.Tool | Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint
        )
        self.setWindowTitle("Meeting detected")
        self.setModal(False)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setMinimumWidth(388)
        self._finished = False

        # A frameless, translucent window holding one toast-like card in the
        # active theme; the transparent gutter around it is where the shadow falls.
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setStyleSheet("QDialog { background: transparent; }")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(_SHADOW, _SHADOW - 4, _SHADOW, _SHADOW + 4)
        card = QFrame()
        card.setObjectName("promptCard")
        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(22)
        shadow.setOffset(0, 5)
        shadow.setColor(QColor(theme.tokens()["shadow"]))
        card.setGraphicsEffect(shadow)
        outer.addWidget(card)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)
        head = QHBoxLayout()
        head.setSpacing(8)
        tally = QLabel()
        accent = theme.tokens()["accent_text"]
        tally.setPixmap(make_icon("mic", accent, accent, 18).pixmap(18, 18))
        tally.setFixedSize(18, 18)
        head.addWidget(tally, 0, Qt.AlignVCenter)
        self.title_label = QLabel(f"{label} call detected")
        self.title_label.setObjectName("promptTitle")
        head.addWidget(self.title_label, 1)
        layout.addLayout(head)
        self.question_label = QLabel("Record this meeting?")
        self.question_label.setObjectName("subtle")
        layout.addWidget(self.question_label)
        self.name_edit = QLineEdit(suggested_name)
        self.name_edit.setAccessibleName("Meeting name")
        layout.addWidget(self.name_edit)

        row = QHBoxLayout()
        row.addStretch(1)
        self.later_button = QPushButton("Not now")
        self.later_button.clicked.connect(self._on_not_now)
        self.record_button = QPushButton("Record")
        self.record_button.setObjectName("record")
        self.record_button.setDefault(True)
        self.record_button.clicked.connect(self._on_record)
        self.record_button.setIcon(make_icon("mic", "#ffffff", "#ffffff", 16))
        self.record_button.setIconSize(icon_size(16))
        row.addWidget(self.later_button)
        row.addWidget(self.record_button)
        layout.addLayout(row)
        self.name_edit.returnPressed.connect(self._on_record)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._on_not_now)
        self._timer.start(timeout_ms)

    # -- placement -----------------------------------------------------------

    def place_bottom_right(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        self.adjustSize()
        self.move(
            area.right() - self.width() - _MARGIN + 1,
            area.bottom() - self.height() - _MARGIN + 1,
        )

    def show_prompt(self) -> None:
        self.place_bottom_right()
        self.show()
        self.raise_()

    # -- outcomes ------------------------------------------------------------

    def _finish(self) -> bool:
        if self._finished:
            return False
        self._finished = True
        self._timer.stop()
        return True

    def _on_record(self) -> None:
        if self._finish():
            name = self.name_edit.text().strip()
            self.hide()
            self.record_requested.emit(name)
            self.deleteLater()

    def _on_not_now(self) -> None:
        if self._finish():
            self.hide()
            self.dismissed.emit()
            self.deleteLater()

    def close_silently(self) -> None:
        """Remove the prompt without emitting either outcome (call ended)."""
        self._finish()
        self.hide()
        self.deleteLater()

    def closeEvent(self, event):  # noqa: N802 - Qt naming
        if not self._finished:
            self._on_not_now()
        super().closeEvent(event)

    def keyPressEvent(self, event):  # noqa: N802 - Qt naming
        if event.key() == Qt.Key_Escape:
            self._on_not_now()
            return
        super().keyPressEvent(event)
