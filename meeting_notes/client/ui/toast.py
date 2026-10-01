"""A brief, bottom-centred notice that overlays the window (docs/design.md: Toast).

Inverts the theme (dark pill on light, light pill on dark), fades in and out over
150 ms, replaces any earlier toast, and never takes focus or mouse input.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPropertyAnimation, Qt, QTimer
from PySide6.QtWidgets import QGraphicsOpacityEffect, QLabel

from meeting_notes.client.ui import theme

SHOW_MS = 4000
FADE_MS = 150
_BOTTOM_GAP = 28


class Toast(QLabel):
    def __init__(self, parent, show_ms: int = SHOW_MS):
        super().__init__(parent)
        self.setObjectName("toast")
        self.setAlignment(Qt.AlignCenter)
        self.setFocusPolicy(Qt.NoFocus)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAccessibleName("Notice")
        self._effect = QGraphicsOpacityEffect(self)
        self._effect.setOpacity(0.0)
        self.setGraphicsEffect(self._effect)
        self._fade = QPropertyAnimation(self._effect, b"opacity", self)
        self._fade.setDuration(FADE_MS)
        self._fade.finished.connect(self._after_fade)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(show_ms)
        self._hide_timer.timeout.connect(lambda: self._fade_to(0.0))
        self._restyle()
        theme.manager().changed.connect(self._restyle)
        parent.installEventFilter(self)
        self.hide()

    def show_message(self, text: str) -> None:
        """Show ``text``, replacing whatever the previous toast said."""
        self._hide_timer.stop()
        self.setText(text)
        self.adjustSize()
        self._place()
        self.raise_()
        self.show()
        self._fade_to(1.0)
        self._hide_timer.start()

    def _fade_to(self, target: float) -> None:
        self._fade.stop()
        self._fade.setStartValue(self._effect.opacity())
        self._fade.setEndValue(target)
        self._fade.start()

    def _after_fade(self) -> None:
        if self._effect.opacity() <= 0.0:
            self.hide()

    def _restyle(self, *_args) -> None:
        tokens = theme.tokens()
        self.setStyleSheet(
            f"QLabel#toast {{ background: {tokens['toast_bg']}; color: {tokens['toast_text']};"
            " border: none; border-radius: 8px; padding: 9px 16px; font-size: 13px; }"
        )
        if self.text():
            self.adjustSize()
            self._place()

    def _place(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        self.move(max(0, (parent.width() - self.width()) // 2), max(0, parent.height() - self.height() - _BOTTOM_GAP))

    def eventFilter(self, obj, event):  # noqa: N802 - Qt naming
        if obj is self.parentWidget() and event.type() == QEvent.Resize and self.text():
            self._place()
        return False
