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

The two lanes are laid out like a studio track sheet: a numbered legend column
(LANE 1 / YOU, LANE 2 / THEM) beside a ruled trace area.
"""

from __future__ import annotations

from collections import deque
from typing import Deque, Dict

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

# Meter colours come from the console palette: cream card stock for you, kraft
# for them. Red is reserved for clipping and for a track that has no signal.
TRACK_COLORS = {
    "mic": QColor("#efe6cc"),      # card stock: you
    "system": QColor("#c9a96b"),   # kraft: them
}
TRACK_LEGENDS = {"mic": ("YOU", "MICROPHONE"), "system": ("THEM", "SYSTEM AUDIO")}
TRACK_LABELS = {"mic": "You (microphone)", "system": "Them (system audio)"}

BACKGROUND = QColor("#1d1f22")
LANE = QColor("#26292d")
LANE_EDGE = QColor("#3d4247")
GRID = QColor("#33373c")
TEXT = QColor("#a6a396")
INK = QColor("#ece6d6")
CLIP = QColor("#d9493e")
RED = QColor("#c8372d")
CARD = QColor("#f1ead9")
CARD_INK = QColor("#24211b")

GUTTER = 96          # lane legend column
LANE_GAP = 6


def _condensed(pixel_size: int, weight=QFont.DemiBold) -> QFont:
    font = QFont("Barlow Condensed")
    font.setPixelSize(pixel_size)
    font.setWeight(weight)
    font.setLetterSpacing(QFont.AbsoluteSpacing, 0.8)
    return font


class WaveformWidget(QWidget):
    """Scrolling peak meter: two labelled lanes, like a track sheet."""

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
        self.setMinimumHeight(132)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setAutoFillBackground(False)
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
        painter.fillRect(self.rect(), BACKGROUND)

        if not self.tracks:
            painter.end()
            return

        count = len(self.tracks)
        lane_height = (self.height() - LANE_GAP * (count - 1)) / count
        for index, track in enumerate(self.tracks):
            top = index * (lane_height + LANE_GAP)
            self._paint_lane(painter, index, track, top, lane_height)

        painter.end()

    def _paint_lane(self, painter: QPainter, index: int, track: str, top: float, height: float) -> None:
        width = float(self.width())
        lane = QRectF(0.5, top + 0.5, width - 1, height - 1)
        active = self._active.get(track, True)
        muted = self._muted.get(track, False)
        dimmed = muted or not active

        painter.setPen(QPen(LANE_EDGE, 1))
        painter.setBrush(LANE)
        painter.drawRoundedRect(lane, 4, 4)

        # -- legend gutter: lane number, name, source ----------------------------
        painter.setPen(QPen(LANE_EDGE, 1))
        painter.drawLine(int(GUTTER), int(top + 6), int(GUTTER), int(top + height - 6))
        name, source = TRACK_LEGENDS.get(track, (track.upper(), ""))
        colour = QColor(TRACK_COLORS.get(track, QColor("#94a3b8")))
        legend_colour = QColor(TEXT if dimmed else INK)

        # A short lane (window at its minimum with a banner showing) drops the
        # source caption and tucks the name under the lane number.
        compact = height < 92
        painter.setFont(_condensed(12, QFont.Medium))
        painter.setPen(TEXT)
        painter.drawText(
            QRectF(14, top + 6, GUTTER - 20, 14), Qt.AlignLeft | Qt.AlignVCenter, f"LANE {index + 1}"
        )
        painter.setFont(_condensed(20 if compact else 24))
        painter.setPen(legend_colour)
        if compact:
            name_rect = QRectF(14, top + height - 32, GUTTER - 20, 28)
        else:
            name_rect = QRectF(14, top + height / 2 - 17, GUTTER - 20, 34)
        painter.drawText(name_rect, Qt.AlignLeft | Qt.AlignVCenter, name)
        if not compact:
            painter.setFont(_condensed(11, QFont.Medium))
            painter.setPen(TEXT)
            painter.drawText(
                QRectF(14, top + height - 22, GUTTER - 16, 14), Qt.AlignLeft | Qt.AlignVCenter, source
            )

        # -- trace ----------------------------------------------------------------
        area_left = GUTTER + 8.0
        area_width = max(1.0, width - area_left - 10)
        centre = top + height / 2
        reach = max(4.0, height / 2 - 12)

        painter.setPen(QPen(GRID, 1))
        for fraction in (0.5, 1.0):
            for sign in (-1, 1):
                y = centre + sign * reach * fraction
                painter.drawLine(int(area_left), int(y), int(area_left + area_width), int(y))
        painter.setPen(QPen(QColor("#5a5e63"), 1))
        painter.drawLine(int(area_left), int(centre), int(area_left + area_width), int(centre))

        if dimmed:
            colour.setAlpha(70)

        levels = self._levels[track]
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
            fill.setAlpha(min(fill.alpha(), 90 if self._recording else 45))
            painter.fillPath(path, fill)
            painter.setPen(QPen(colour, 1.4))
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(path)

        # Recording tick: a grease-pencil mark at the write head.
        if self._recording and not dimmed:
            painter.setPen(QPen(RED, 2))
            x = area_left + area_width
            painter.drawLine(int(x), int(top + 8), int(x), int(top + height - 8))

        # -- readout / state chip -------------------------------------------------
        current = levels[-1] if levels else 0.0
        chip_text, chip_fill, chip_ink = None, None, None
        if muted:
            chip_text, chip_fill, chip_ink = "MUTED", CARD, CARD_INK
        elif not active:
            chip_text, chip_fill, chip_ink = "NO SIGNAL", RED, QColor("#ffffff")
        elif current >= 0.99:
            # Clipping is worth shouting about: it is unrecoverable distortion
            # in the archive copy, not just a display artefact.
            chip_text, chip_fill, chip_ink = "CLIP", CLIP, QColor("#ffffff")

        if chip_text:
            painter.setFont(_condensed(13))
            metrics = painter.fontMetrics()
            chip_w = metrics.horizontalAdvance(chip_text) + 16
            chip = QRectF(width - 14 - chip_w, top + 8, chip_w, 20)
            painter.setPen(Qt.NoPen)
            painter.setBrush(chip_fill)
            painter.drawRoundedRect(chip, 3, 3)
            painter.setPen(chip_ink)
            painter.drawText(chip, Qt.AlignCenter, chip_text)

        # Level readout lives in the legend column so it never sits on the trace.
        painter.setFont(_condensed(13))
        painter.setPen(TEXT)
        painter.drawText(
            QRectF(GUTTER - 52, top + 6, 44, 14), Qt.AlignRight | Qt.AlignVCenter, f"{int(current * 100)}%"
        )
