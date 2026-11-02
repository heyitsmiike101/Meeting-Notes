"""A one-line, left-aligned label that scrolls on hover: the recorder's device names beside their audio lanes.

A name that fits is shown as it is. One that does not is cut with "..." until the mouse is over it; then it scrolls to
its end, pauses, scrolls back and repeats until the mouse leaves. The whole text is also the tooltip.
"""

from __future__ import annotations

from PySide6.QtCore import QRect, Qt, QTimer
from PySide6.QtGui import QPainter, QPalette
from PySide6.QtWidgets import QLabel, QSizePolicy

TICK_MS = 16
STEP_PX = 1
PAUSE_TICKS = 45  # at each end, about three quarters of a second


class MarqueeLabel(QLabel):
    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self.setWordWrap(False)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)  # a long name never widens the row
        self.setAttribute(Qt.WA_Hover, True)
        self._offset = 0
        self._direction = 1
        self._pause = 0
        self._hovered = False
        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._step)

    @property
    def offset(self) -> int:
        return self._offset

    def overflow(self) -> int:
        """How many pixels of the text do not fit (0 when it fits)."""
        return max(0, self.fontMetrics().horizontalAdvance(self.text()) - self.contentsRect().width())

    def setText(self, text: str) -> None:  # noqa: N802 - Qt naming
        if text == self.text():
            return
        super().setText(text)
        self.setToolTip(text)
        self._restart()

    def enterEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._hovered = True
        self._restart()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._hovered = False
        self._restart()
        super().leaveEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self._restart()

    def _restart(self) -> None:
        self._offset, self._direction, self._pause = 0, 1, PAUSE_TICKS
        if self._hovered and self.overflow():
            self._timer.start()
        else:
            self._timer.stop()
        self.update()

    def _step(self) -> None:
        over = self.overflow()
        if not over:
            self._restart()
            return
        if self._pause:
            self._pause -= 1
            return
        self._offset += self._direction * STEP_PX
        if self._offset >= over or self._offset <= 0:
            self._offset = max(0, min(over, self._offset))
            self._direction = -self._direction
            self._pause = PAUSE_TICKS
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        rect = self.contentsRect()
        painter.setClipRect(rect)
        painter.setFont(self.font())
        painter.setPen(self.palette().color(QPalette.WindowText))
        text = self.text()
        scrolling = self._timer.isActive()
        if not scrolling:
            text = self.fontMetrics().elidedText(text, Qt.ElideRight, rect.width())
        width = max(rect.width(), self.fontMetrics().horizontalAdvance(text) + 2)
        target = QRect(rect.left() - (self._offset if scrolling else 0), rect.top(), width, rect.height())
        painter.drawText(target, Qt.AlignLeft | Qt.AlignVCenter, text)
        painter.end()
