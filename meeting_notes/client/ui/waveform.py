"""Live level display for the two tracks.

This is the only thing on screen that answers the question people actually have
while recording: "is it working?" A recorder that silently captures silence --
wrong device, muted mic, output switched to something else -- looks identical to
a working one until the meeting is over and it is too late. Two independent
rows make the common failure obvious at a glance, because a dead track is a
flat line next to a live one.

It draws peak levels over time, not a true waveform: at 30fps we get one value
per frame, and what matters here is "is audio arriving and how loud", not
sample-accurate shape.

Each row is a quiet panel: a plain sentence-case label ("You . Microphone"), a
small state badge when something needs saying (Muted, No signal, Clipping), and
the scrolling level trace. All colours come from the active theme's tokens.
"""

from __future__ import annotations

from collections import deque
from typing import Deque, Dict

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from meeting_notes.client.ui import theme

# (who, what is being captured) per track.
TRACK_LEGENDS = {"mic": ("You", "Microphone"), "system": ("Them", "System audio")}
TRACK_LABELS = {"mic": "You (microphone)", "system": "Them (system audio)"}
TRACK_TOKENS = {"mic": "meter_you", "system": "meter_them"}

LANE_GAP = 8
PAD_X = 14
HEADER = 26          # label row height inside a lane


class WaveformWidget(QWidget):
    """Scrolling peak meter: two labelled rows, one per track."""

    def __init__(self, tracks=("mic", "system"), history: int = 400, parent=None):
        super().__init__(parent)
        self.tracks = list(tracks)
        self.history = history
        self._levels: Dict[str, Deque[float]] = {
            t: deque([0.0] * history, maxlen=history) for t in self.tracks
        }
        self._active: Dict[str, bool] = {t: True for t in self.tracks}
        self._muted: Dict[str, bool] = {t: False for t in self.tracks}
        self._recording = False
        self.setMinimumHeight(124)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setAutoFillBackground(False)
        theme.manager().changed.connect(lambda _name: self.update())
        self.setAccessibleName("Level meters for you and them")

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

    def set_track_muted(self, track: str, muted: bool) -> None:
        """A muted lane is dimmed and tagged, so silence is never a surprise."""
        if track in self._muted:
            self._muted[track] = muted
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
        painter.setRenderHint(QPainter.TextAntialiasing, True)

        if not self.tracks:
            painter.end()
            return

        count = len(self.tracks)
        lane_height = (self.height() - LANE_GAP * (count - 1)) / count
        for index, track in enumerate(self.tracks):
            top = index * (lane_height + LANE_GAP)
            self._paint_lane(painter, track, top, lane_height)

        painter.end()

    def _paint_lane(self, painter: QPainter, track: str, top: float, height: float) -> None:
        t = theme.tokens()
        width = float(self.width())
        lane = QRectF(0.5, top + 0.5, width - 1, height - 1)
        active = self._active.get(track, True)
        muted = self._muted.get(track, False)
        dimmed = muted or not active
        levels = self._levels[track]
        current = levels[-1] if levels else 0.0

        painter.setPen(QPen(QColor(t["border"]), 1))
        painter.setBrush(QColor(t["panel"]))
        painter.drawRoundedRect(lane, 8, 8)

        # -- label row: "You . Microphone" -------------------------------------
        who, what = TRACK_LEGENDS.get(track, (track.capitalize(), ""))
        label_rect = QRectF(PAD_X, top + 4, width - 2 * PAD_X, HEADER - 4)
        painter.setFont(theme.ui_font(13, QFont.DemiBold))
        painter.setPen(QColor(t["muted"] if dimmed else t["text"]))
        painter.drawText(label_rect, Qt.AlignLeft | Qt.AlignVCenter, who)
        who_width = painter.fontMetrics().horizontalAdvance(who)
        painter.setFont(theme.ui_font(13, QFont.Normal))
        painter.setPen(QColor(t["muted"]))
        painter.drawText(
            QRectF(PAD_X + who_width, label_rect.top(), width, label_rect.height()),
            Qt.AlignLeft | Qt.AlignVCenter,
            f" · {what}" if what else "",
        )

        # -- readout and state badge, right-aligned ------------------------------
        right = width - PAD_X
        painter.setFont(theme.ui_font(12, QFont.Normal, tabular=True))
        painter.setPen(QColor(t["muted"]))
        readout = f"{int(current * 100)}%"
        readout_w = painter.fontMetrics().horizontalAdvance("100%")
        painter.drawText(
            QRectF(right - readout_w, label_rect.top(), readout_w, label_rect.height()),
            Qt.AlignRight | Qt.AlignVCenter,
            readout,
        )
        right -= readout_w + 10

        badge = None
        if muted:
            badge = ("Muted", t["panel_hover"], t["border_strong"], t["text2"])
        elif not active:
            badge = ("No signal", t["danger_soft"], t["danger_border"], t["danger_text"])
        elif current >= 0.99:
            # Clipping is unrecoverable distortion in the archive copy, not just
            # a display artefact, so it is worth a badge.
            badge = ("Clipping", t["danger_soft"], t["danger_border"], t["danger_text"])
        if badge:
            text, fill, edge, ink = badge
            painter.setFont(theme.ui_font(11, QFont.Medium))
            badge_w = painter.fontMetrics().horizontalAdvance(text) + 16
            rect = QRectF(right - badge_w, label_rect.top() + 2, badge_w, label_rect.height() - 4)
            painter.setPen(QPen(QColor(edge), 1))
            painter.setBrush(QColor(fill))
            painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), rect.height() / 2, rect.height() / 2)
            painter.setPen(QColor(ink))
            painter.drawText(rect, Qt.AlignCenter, text)

        # -- trace ----------------------------------------------------------------
        area_left = PAD_X + 0.0
        area_width = max(1.0, width - 2 * PAD_X)
        area_top = top + HEADER + 2
        area_height = max(6.0, height - HEADER - 2 - 10)
        centre = area_top + area_height / 2
        reach = max(2.0, area_height / 2 - 1)

        painter.setPen(QPen(QColor(t["border"]), 1))
        painter.drawLine(int(area_left), int(centre), int(area_left + area_width), int(centre))

        colour = QColor(t[TRACK_TOKENS.get(track, "meter_them")])
        if dimmed:
            colour.setAlpha(80)

        count = len(levels)
        if count:
            step = area_width / count
            # Filled envelope rather than discrete bars: at meeting sample rates
            # bars alias into a moire pattern as the window resizes.
            path = QPainterPath()
            path.moveTo(area_left, centre)
            for i, level in enumerate(levels):
                path.lineTo(area_left + i * step, centre - level * reach)
            for i in range(count - 1, -1, -1):
                path.lineTo(area_left + i * step, centre + levels[i] * reach)
            path.closeSubpath()

            fill = QColor(colour)
            fill.setAlpha(min(fill.alpha(), 70 if self._recording else 40))
            painter.fillPath(path, fill)
            painter.setPen(QPen(colour, 1.3))
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(path)
