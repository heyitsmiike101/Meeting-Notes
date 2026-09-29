"""Live level display for the two tracks.

This is the only thing on screen that answers the question people actually have
while recording: "is it working?" A recorder that silently captures silence --
wrong device, muted mic, output switched to something else -- looks identical to
a working one until the meeting is over and it is too late. Two independent
lanes make the common failure obvious at a glance, because a dead track is a
flat line next to a live one.

It draws peak levels over time, not a true waveform: at 30fps we get one value
per frame, and what matters here is "is audio arriving and how loud", not
sample-accurate shape.
"""

from __future__ import annotations

from collections import deque
from typing import Deque, Dict, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

TRACK_COLORS = {
    "mic": QColor("#4ade80"),      # green: you
    "system": QColor("#60a5fa"),   # blue: them
}
TRACK_LABELS = {"mic": "You (microphone)", "system": "Them (system audio)"}

BACKGROUND = QColor("#0f1419")
GRID = QColor("#1e2630")
TEXT = QColor("#8b98a5")
CLIP = QColor("#f87171")


class WaveformWidget(QWidget):
    """Scrolling peak meter with one lane per track."""

    def __init__(self, tracks=("mic", "system"), history: int = 400, parent=None):
        super().__init__(parent)
        self.tracks = list(tracks)
        self.history = history
        self._levels: Dict[str, Deque[float]] = {
            t: deque([0.0] * history, maxlen=history) for t in self.tracks
        }
        self._active: Dict[str, bool] = {t: True for t in self.tracks}
        self._recording = False
        self.setMinimumHeight(150)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setAutoFillBackground(False)

    # -- state ---------------------------------------------------------------

    def push(self, levels: Dict[str, float]) -> None:
        """Append one sample per track. Missing tracks repeat silence."""
        for track in self.tracks:
            value = float(levels.get(track, 0.0) or 0.0)
            self._levels[track].append(max(0.0, min(1.0, value)))
        self.update()

    def set_track_active(self, track: str, active: bool) -> None:
        """A degraded or failed track is drawn dimmed, so it reads as a problem."""
        if track in self._active:
            self._active[track] = active
            self.update()

    def set_recording(self, recording: bool) -> None:
        self._recording = recording
        self.update()

    def clear(self) -> None:
        for buf in self._levels.values():
            buf.clear()
            buf.extend([0.0] * self.history)
        self.update()

    def peak(self, track: str) -> float:
        buf = self._levels.get(track)
        return max(buf) if buf else 0.0

    # -- painting ------------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.fillRect(self.rect(), BACKGROUND)

        if not self.tracks:
            painter.end()
            return

        lane_height = self.height() / len(self.tracks)
        for index, track in enumerate(self.tracks):
            top = index * lane_height
            self._paint_lane(painter, track, top, lane_height)

        painter.end()

    def _paint_lane(self, painter: QPainter, track: str, top: float, height: float) -> None:
        centre = top + height / 2
        colour = QColor(TRACK_COLORS.get(track, QColor("#94a3b8")))
        if not self._active.get(track, True):
            colour.setAlpha(70)

        painter.setPen(QPen(GRID, 1))
        painter.drawLine(0, int(centre), self.width(), int(centre))

        levels = self._levels[track]
        count = len(levels)
        if count and self.width() > 0:
            step = self.width() / count
            # Filled envelope rather than discrete bars: at meeting sample rates
            # bars alias into a moire pattern as the window resizes.
            path = QPainterPath()
            path.moveTo(0, centre)
            for i, level in enumerate(levels):
                path.lineTo(i * step, centre - level * (height / 2 - 12))
            for i in range(count - 1, -1, -1):
                path.lineTo(i * step, centre + levels[i] * (height / 2 - 12))
            path.closeSubpath()

            fill = QColor(colour)
            fill.setAlpha(60 if self._recording else 35)
            painter.fillPath(path, fill)
            painter.setPen(QPen(colour, 1.4))
            painter.drawPath(path)

        painter.setPen(QPen(TEXT, 1))
        font = painter.font()
        font.setPointSize(8)
        painter.setFont(font)
        label = TRACK_LABELS.get(track, track)
        if not self._active.get(track, True):
            label += "  (no signal)"
        painter.drawText(8, int(top + 14), label)

        current = levels[-1] if levels else 0.0
        if current >= 0.99:
            # Clipping is worth shouting about: it is unrecoverable distortion
            # in the archive copy, not just a display artefact.
            painter.setPen(QPen(CLIP, 1))
            painter.drawText(self.width() - 46, int(top + 14), "CLIP")
        else:
            painter.drawText(self.width() - 46, int(top + 14), f"{int(current * 100):3d}%")
