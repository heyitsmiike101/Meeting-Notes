"""The "record this meeting?" prompt shown when a call is detected."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

AUTO_DISMISS_MS = 60_000
_MARGIN = 16


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
        self.setMinimumWidth(360)
        self.setStyleSheet("QDialog { border: 1px solid #30363d; border-radius: 8px; }")
        self._finished = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        self.title_label = QLabel(f"{label} call detected")
        self.title_label.setStyleSheet("font-weight: 600; font-size: 14px;")
        layout.addWidget(self.title_label)
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
