"""Authored line icons for the recorder console.

Every icon is drawn here with QPainter on one 24x24 grid, in one stroke (1.75
units, round caps and joins), so the set reads as one family. Nothing depends
on an image plugin or on files that Nuitka might forget to ship. Glyph
characters and emoji are deliberately not used.
"""

from __future__ import annotations

import math
from typing import Callable, Dict

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPainterPath, QPen, QPixmap

GRID = 24.0
STROKE = 1.75


def _poly(painter: QPainter, points, close: bool = False) -> None:
    path = QPainterPath(QPointF(*points[0]))
    for x, y in points[1:]:
        path.lineTo(x, y)
    if close:
        path.closeSubpath()
    painter.drawPath(path)


def _folder(p: QPainter) -> None:
    _poly(p, [(3, 7), (9.5, 7), (11.5, 9.5), (21, 9.5), (21, 19), (3, 19)], close=True)


def _upload(p: QPainter) -> None:
    _poly(p, [(12, 16), (12, 4.5)])
    _poly(p, [(7.5, 9), (12, 4.5), (16.5, 9)])
    _poly(p, [(4, 15), (4, 19.5), (20, 19.5), (20, 15)])


def _download(p: QPainter) -> None:
    _poly(p, [(12, 4), (12, 15.5)])
    _poly(p, [(7.5, 11), (12, 15.5), (16.5, 11)])
    _poly(p, [(4, 15), (4, 19.5), (20, 19.5), (20, 15)])


def _history(p: QPainter) -> None:
    p.drawEllipse(QPointF(12, 12), 8.5, 8.5)
    _poly(p, [(12, 7), (12, 12), (15.5, 14)])


def _settings(p: QPainter) -> None:
    for y, knob in ((6.5, 9.0), (12.0, 15.5), (17.5, 8.0)):
        _poly(p, [(4, y), (knob - 3, y)])
        _poly(p, [(knob + 3, y), (20, y)])
        p.drawEllipse(QPointF(knob, y), 2.4, 2.4)


def _more(p: QPainter) -> None:
    p.setPen(Qt.NoPen)  # solid dots: the caller already set the brush
    for x in (5.5, 12.0, 18.5):
        p.drawEllipse(QPointF(x, 12), 1.9, 1.9)


def _refresh(p: QPainter) -> None:
    rect = QRectF(5, 5, 14, 14)
    path = QPainterPath()
    path.arcMoveTo(rect, 50)
    path.arcTo(rect, 50, 270)
    p.drawPath(path)
    # Arrow head at the arc's end (320 degrees), pointing along the travel.
    end = path.currentPosition()
    theta = math.radians(320)
    heading = math.atan2(-math.cos(theta), -math.sin(theta))
    for spread in (math.radians(40), -math.radians(40)):
        back = heading + math.pi + spread
        p.drawLine(end, QPointF(end.x() + 4.4 * math.cos(back), end.y() + 4.4 * math.sin(back)))


def _log(p: QPainter) -> None:
    _poly(p, [(6, 3.5), (14.5, 3.5), (19, 8), (19, 20.5), (6, 20.5)], close=True)
    _poly(p, [(14.5, 3.5), (14.5, 8), (19, 8)])
    _poly(p, [(9.5, 12.5), (15.5, 12.5)])
    _poly(p, [(9.5, 16), (15.5, 16)])


def _record(p: QPainter) -> None:
    p.setPen(Qt.NoPen)
    p.drawEllipse(QPointF(12, 12), 6.5, 6.5)


def _stop(p: QPainter) -> None:
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(QRectF(6, 6, 12, 12), 1.8, 1.8)


def _check(p: QPainter) -> None:
    _poly(p, [(5, 12.5), (10, 17.5), (19, 7)])


def _alert(p: QPainter) -> None:
    _poly(p, [(12, 3.5), (21.5, 20), (2.5, 20)], close=True)
    _poly(p, [(12, 9.5), (12, 14.2)])
    _poly(p, [(12, 17.2), (12, 17.3)])


def _logs(p: QPainter) -> None:
    _poly(p, [(4, 5.5), (20, 5.5)])
    _poly(p, [(4, 10.5), (20, 10.5)])
    _poly(p, [(4, 15.5), (14, 15.5)])
    _poly(p, [(4, 20.5), (11, 20.5)])


def _chevron(p: QPainter) -> None:
    _poly(p, [(7, 9.5), (12, 14.5), (17, 9.5)])


# Icons whose shapes are solid fills rather than strokes.
_FILLED = {"more", "record", "stop"}

_DRAWERS: Dict[str, Callable[[QPainter], None]] = {
    "folder": _folder,
    "upload": _upload,
    "download": _download,
    "history": _history,
    "settings": _settings,
    "more": _more,
    "refresh": _refresh,
    "log": _log,
    "record": _record,
    "stop": _stop,
    "chevron": _chevron,
    "check": _check,
    "alert": _alert,
    "logs": _logs,
}


def _render(name: str, colour: QColor, size: int, ratio: float) -> QPixmap:
    px = int(round(size * ratio))
    image = QImage(px, px, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.scale(px / GRID, px / GRID)
        pen = QPen(colour, STROKE)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(colour if name in _FILLED else Qt.NoBrush)
        _DRAWERS[name](painter)
    finally:
        painter.end()
    pixmap = QPixmap.fromImage(image)
    pixmap.setDevicePixelRatio(ratio)
    return pixmap


def make_icon(name: str, colour: str = "#ece6d6", disabled: str = "#6f6d66", size: int = 18) -> QIcon:
    """A crisp QIcon (Normal and Disabled) at 1x and 2x for the named glyph."""
    icon = QIcon()
    if name not in _DRAWERS:
        return icon
    for mode, hex_colour in ((QIcon.Normal, colour), (QIcon.Disabled, disabled)):
        for ratio in (1.0, 2.0):
            icon.addPixmap(_render(name, QColor(hex_colour), size, ratio), mode)
    return icon


def icon_size(size: int = 18) -> QSize:
    return QSize(size, size)


__all__ = ["make_icon", "icon_size"]
