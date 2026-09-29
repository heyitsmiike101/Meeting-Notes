"""The big elapsed-time readout.

Digits are drawn one per fixed cell so the timecode never jitters as the seconds
tick over, whatever the font's own figure widths are (tabular figures without
depending on an OpenType feature).
"""

from __future__ import annotations

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QFontMetrics, QPainter
from PySide6.QtWidgets import QLabel, QSizePolicy


class TimecodeLabel(QLabel):
    def __init__(self, text: str = "00:00:00", parent=None):
        super().__init__(text, parent)
        self.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        self.setAccessibleName("Elapsed recording time")

    def _cells(self, metrics: QFontMetrics):
        digit = max(metrics.horizontalAdvance(str(d)) for d in range(10))
        colon = max(metrics.horizontalAdvance(":") - 2, 6)
        return digit, colon

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        metrics = QFontMetrics(self.font())
        digit, colon = self._cells(metrics)
        return QSize(digit * 6 + colon * 2 + 4, metrics.height() + 2)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        return self.sizeHint()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        painter.setFont(self.font())
        painter.setPen(self.palette().color(self.foregroundRole()))
        metrics = painter.fontMetrics()
        digit, colon = self._cells(metrics)
        x = 0
        for char in self.text():
            width = colon if char == ":" else digit
            painter.drawText(
                QRect(x, 0, width, self.height()), Qt.AlignCenter | Qt.AlignVCenter, char
            )
            x += width
        painter.end()
