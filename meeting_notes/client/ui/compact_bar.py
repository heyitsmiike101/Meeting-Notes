"""Compact view: a slim always-on-top bar docked to the right edge of the screen.

The main window hides while it is up. It shows that the recording is still running (a dot and the elapsed time),
that audio is being picked up (two level meters), lets you mute either side, opens the notes scratchpad beside it,
and expands back to the window.

The bar owns no recording state: the window pushes it in with :meth:`CompactBar.update_state` on its own tick and
handles the ``mute_requested`` / ``expand_requested`` / ``notes_requested`` signals with its existing handlers.
"""

from __future__ import annotations

import logging
from typing import Dict, Optional

from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QLabel, QToolButton, QVBoxLayout, QHBoxLayout, QWidget

from meeting_notes.client.ui import theme
from meeting_notes.client.ui.icons import icon_size, make_icon

log = logging.getLogger("meeting_notes.client.compact_bar")

BAR_WIDTH = 64
CORNER_RADIUS = 12
DEFAULT_Y_FRACTION = 0.5
METER_HEIGHT = 72
METER_WIDTH = 10
BUTTON_SIZE = 26

STATE_IDLE = "idle"
STATE_RECORDING = "recording"
STATE_FINISHING = "finishing"


def format_elapsed(seconds: float) -> str:
    """``mm:ss`` under an hour, ``h:mm`` from one hour on (the bar is only 64 px wide)."""
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        return f"{seconds // 3600}:{(seconds % 3600) // 60:02d}"
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def clamp_fraction(value) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return DEFAULT_Y_FRACTION
    if value != value:  # NaN
        return DEFAULT_Y_FRACTION
    return max(0.0, min(1.0, value))


class _Dot(QWidget):
    """The status dot: red (pulsing while recording), amber while finishing, grey when idle."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(12, 12)
        self._state = STATE_IDLE
        self._phase = 1.0
        self._tick = 0
        self._timer = QTimer(self)
        self._timer.setInterval(120)
        self._timer.timeout.connect(self._pulse)

    def set_state(self, state: str) -> None:
        if state == self._state:
            return
        self._state = state
        self._phase = 1.0
        self._tick = 0
        if state == STATE_RECORDING and self.isVisible():
            self._timer.start()
        else:
            self._timer.stop()
        self.update()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        if self._state == STATE_RECORDING:
            self._timer.start()

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().hideEvent(event)
        self._timer.stop()

    def _pulse(self) -> None:
        self._tick = (self._tick + 1) % 16
        self._phase = 0.55 + 0.45 * abs(8 - self._tick) / 8.0
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        tokens = theme.tokens()
        if self._state == STATE_RECORDING:
            colour = QColor(tokens["danger"])
            colour.setAlphaF(self._phase)
        elif self._state == STATE_FINISHING:
            colour = QColor(tokens["warn_icon"])
        else:
            colour = QColor(tokens["disabled"])
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        painter.setBrush(colour)
        painter.drawEllipse(QRectF(1.5, 1.5, 9, 9))
        painter.end()


class _Meter(QWidget):
    """A small vertical level bar: level 0..1 fills from the bottom; greyed when the track is muted."""

    def __init__(self, track: str, parent=None):
        super().__init__(parent)
        self.track = track
        self.setFixedSize(METER_WIDTH, METER_HEIGHT)
        self._level = 0.0
        self._muted = False
        self._live = False

    @property
    def level(self) -> float:
        return self._level

    def set_level(self, level: float, muted: bool = False, live: bool = True) -> None:
        try:
            level = float(level or 0.0)
        except (TypeError, ValueError):
            level = 0.0
        level = max(0.0, min(1.0, level))
        if (level, muted, live) == (self._level, self._muted, self._live):
            return
        self._level, self._muted, self._live = level, bool(muted), bool(live)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        tokens = theme.tokens()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(self.rect())
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(tokens["panel_hover"]))
        painter.drawRoundedRect(rect, 3, 3)
        if self._muted or not self._live:
            painter.end()
            return
        key = "meter_you" if self.track == "mic" else "meter_them"
        painter.setBrush(QColor(tokens[key]))
        # A little headroom curve so quiet speech is still visible.
        shown = self._level ** 0.6
        height = max(0.0, rect.height() * shown)
        if self._level > 0.005:
            height = max(height, 3.0)
            painter.drawRoundedRect(QRectF(0, rect.height() - height, rect.width(), height), 3, 3)
        painter.end()


class CompactBar(QWidget):
    """The bar. Signals only; the window decides what a mute or an expand means."""

    expand_requested = Signal()
    notes_requested = Signal()  # show or hide the notes scratchpad beside the bar
    mute_requested = Signal(str, bool)  # track ("mic" | "system"), want muted
    position_changed = Signal(float)  # y fraction 0..1, after a drag finished

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("compactBar")
        self.setWindowTitle("Meeting Notes")
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setFixedWidth(BAR_WIDTH)
        self.setAccessibleName("Meeting Notes compact bar")

        self._screen = None
        self._y_fraction = DEFAULT_Y_FRACTION
        self._state = STATE_IDLE
        self._drag_offset: Optional[int] = None
        self._muted: Dict[str, bool] = {"mic": False, "system": False}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(5, 10, 5, 8)
        outer.setSpacing(8)

        self.dot = _Dot()
        self.time_label = QLabel("Idle")
        self.time_label.setObjectName("compactTime")
        self.time_label.setAlignment(Qt.AlignCenter)
        self.time_label.setAccessibleName("Recording status")
        status = QVBoxLayout()
        status.setSpacing(3)
        status.addWidget(self.dot, 0, Qt.AlignHCenter)
        status.addWidget(self.time_label)
        outer.addLayout(status)

        self.mic_meter = _Meter("mic")
        self.system_meter = _Meter("system")
        self.mic_meter.setAccessibleName("Microphone level")
        self.system_meter.setAccessibleName("Meeting audio level")
        meters = QHBoxLayout()
        meters.setSpacing(10)
        meters.setContentsMargins(0, 0, 0, 0)
        meters.addStretch(1)
        meters.addWidget(self.mic_meter)
        meters.addWidget(self.system_meter)
        meters.addStretch(1)
        outer.addLayout(meters)

        self.mute_mic_button = self._make_button("mic")
        self.mute_system_button = self._make_button("system")
        self.mute_mic_button.toggled.connect(lambda checked: self._on_toggled("mic", checked))
        self.mute_system_button.toggled.connect(lambda checked: self._on_toggled("system", checked))
        mutes = QHBoxLayout()
        mutes.setSpacing(2)
        mutes.setContentsMargins(0, 0, 0, 0)
        mutes.addWidget(self.mute_mic_button)
        mutes.addWidget(self.mute_system_button)
        outer.addLayout(mutes)

        self.notes_button = QToolButton()
        self.notes_button.setObjectName("compactExpand")  # same quiet look as Expand
        self.notes_button.setCheckable(True)
        self.notes_button.setToolTip("Your notes")
        self.notes_button.setAccessibleName("Show your notes")
        self.notes_button.setFocusPolicy(Qt.StrongFocus)
        self.notes_button.setCursor(Qt.PointingHandCursor)
        self.notes_button.setIconSize(icon_size(16))
        self.notes_button.setFixedSize(40, BUTTON_SIZE)
        self.notes_button.clicked.connect(lambda _c=False: self.notes_requested.emit())
        outer.addWidget(self.notes_button, 0, Qt.AlignHCenter)

        self.expand_button = QToolButton()
        self.expand_button.setObjectName("compactExpand")
        self.expand_button.setToolTip("Expand to the full window")
        self.expand_button.setAccessibleName("Expand Meeting Notes")
        self.expand_button.setFocusPolicy(Qt.StrongFocus)
        self.expand_button.setCursor(Qt.PointingHandCursor)
        self.expand_button.setIconSize(icon_size(16))
        self.expand_button.setFixedSize(40, BUTTON_SIZE)
        self.expand_button.clicked.connect(lambda _c=False: self.expand_requested.emit())
        outer.addWidget(self.expand_button, 0, Qt.AlignHCenter)

        self._refresh_mute_buttons()
        self.apply_theme()
        theme.manager().changed.connect(lambda _name: self.apply_theme())
        self._connect_screens()

    # -- building ----------------------------------------------------------------

    def _make_button(self, track: str) -> QToolButton:
        button = QToolButton()
        button.setObjectName("compactMute")
        button.setCheckable(True)
        button.setEnabled(False)
        button.setFocusPolicy(Qt.StrongFocus)
        button.setCursor(Qt.PointingHandCursor)
        button.setIconSize(icon_size(16))
        button.setFixedSize(BUTTON_SIZE, BUTTON_SIZE)
        button.setProperty("track", track)
        return button

    def apply_theme(self) -> None:
        t = theme.tokens()
        self.setStyleSheet(
            f"""
            QLabel#compactTime {{ color: {t['text']}; font-size: 12px; font-weight: 600; background: transparent; }}
            QToolButton#compactMute, QToolButton#compactExpand {{
                background: transparent; border: 1px solid transparent; border-radius: 6px;
            }}
            QToolButton#compactMute:hover, QToolButton#compactExpand:hover {{ background: {t['panel_hover']}; }}
            QToolButton#compactMute:pressed, QToolButton#compactExpand:pressed {{ background: {t['border']}; }}
            QToolButton#compactMute:focus, QToolButton#compactExpand:focus {{ border: 2px solid {t['accent']}; }}
            QToolButton#compactExpand:checked {{ background: {t['accent_soft']}; }}
            QToolButton#compactMute:checked {{
                background: {t['danger_soft']}; border: 1px solid {t['danger_border']};
            }}
            QToolButton#compactMute:disabled {{ background: transparent; border: 1px solid transparent; }}
            """
        )
        self.expand_button.setIcon(make_icon("expand", size=16))
        self.notes_button.setIcon(make_icon("notes", size=16))
        self._refresh_mute_buttons()
        self.update()

    def _refresh_mute_buttons(self) -> None:
        t = theme.tokens()
        for track, button, on, off, who in (
            ("mic", self.mute_mic_button, "mic-off", "mic", "microphone"),
            ("system", self.mute_system_button, "speaker-off", "speaker", "meeting audio"),
        ):
            muted = self._muted[track]
            glyph = on if muted else off
            colour = t["danger_text"] if muted else None
            button.setIcon(make_icon(glyph, colour, size=16))
            action = "Unmute" if muted else "Mute"
            button.setAccessibleName(f"{action} {who}")
            button.setToolTip(f"{action} {who}")

    # -- state pushed by the window --------------------------------------------

    def set_notes_open(self, shown: bool) -> None:
        """Mark the notes button as on while the scratchpad window is showing."""
        self.notes_button.setChecked(bool(shown))
        self.notes_button.setAccessibleName("Hide your notes" if shown else "Show your notes")

    @property
    def status_text(self) -> str:
        return self.time_label.text()

    @property
    def state(self) -> str:
        return self._state

    def update_state(
        self,
        state: str,
        elapsed: float = 0.0,
        levels: Optional[Dict[str, float]] = None,
        muted: Optional[Dict[str, bool]] = None,
        mute_enabled: Optional[Dict[str, bool]] = None,
    ) -> None:
        self._state = state
        self.dot.set_state(state)
        if state == STATE_RECORDING:
            text = format_elapsed(elapsed)
        elif state == STATE_FINISHING:
            text = "Finishing"
        else:
            text = "Idle"
        if self.time_label.text() != text:
            self.time_label.setText(text)
        muted = muted or {}
        levels = levels or {}
        live = state != STATE_FINISHING
        for track, meter in (("mic", self.mic_meter), ("system", self.system_meter)):
            meter.set_level(levels.get(track, 0.0), bool(muted.get(track)), live)
        changed = False
        for track, button in (("mic", self.mute_mic_button), ("system", self.mute_system_button)):
            want_muted = bool(muted.get(track, False))
            if self._muted[track] != want_muted:
                self._muted[track] = want_muted
                changed = True
            if button.isChecked() != want_muted:
                button.blockSignals(True)
                button.setChecked(want_muted)
                button.blockSignals(False)
            enabled = bool((mute_enabled or {}).get(track, False)) and state == STATE_RECORDING
            if button.isEnabled() != enabled:
                button.setEnabled(enabled)
        if changed:
            self._refresh_mute_buttons()

    def _on_toggled(self, track: str, checked: bool) -> None:
        self.mute_requested.emit(track, bool(checked))

    # -- docking -----------------------------------------------------------------

    def _connect_screens(self) -> None:
        app = QGuiApplication.instance()
        if app is None:
            return
        app.screenRemoved.connect(self._on_screens_changed)
        app.screenAdded.connect(self._on_screen_added)
        app.primaryScreenChanged.connect(self._on_screens_changed)
        for screen in app.screens():
            self._watch(screen)

    def _watch(self, screen) -> None:
        try:
            screen.availableGeometryChanged.connect(self._on_screens_changed)
            screen.geometryChanged.connect(self._on_screens_changed)
        except Exception:  # noqa: BLE001 - a screen going away mid-connect
            log.debug("could not watch a screen", exc_info=True)

    def _on_screen_added(self, screen) -> None:
        self._watch(screen)
        self._on_screens_changed()

    def _on_screens_changed(self, *_args) -> None:
        """A monitor was added, removed or resized: re-dock (to the primary screen if ours is gone)."""
        screens = QGuiApplication.screens()
        if self._screen is None or not any(self._screen is s for s in screens):
            self._screen = QGuiApplication.primaryScreen()
        if self.isVisible():
            self.reposition()

    def _available_geometry(self) -> QRect:
        """The docking area: the available geometry (taskbar excluded) of the screen we are on."""
        screen = self._screen or QGuiApplication.primaryScreen()
        return screen.availableGeometry() if screen is not None else QRect(0, 0, 1920, 1040)

    @property
    def y_fraction(self) -> float:
        return self._y_fraction

    def set_y_fraction(self, value) -> None:
        self._y_fraction = clamp_fraction(value)

    def dock(self, screen=None, y_fraction=None) -> None:
        """Dock to the right edge of ``screen`` (default: where we were, else the primary screen)."""
        if screen is not None:
            self._screen = screen
        elif self._screen is None:
            self._screen = QGuiApplication.primaryScreen()
        if y_fraction is not None:
            self._y_fraction = clamp_fraction(y_fraction)
        self.reposition()

    def reposition(self) -> None:
        self.adjustSize()
        area = self._available_geometry()
        height = self.height()
        span = max(0, area.height() - height)
        y = area.top() + int(round(self._y_fraction * span))
        self.move(area.right() - self.width() + 1, y)

    def _clamped_y(self, y: int) -> int:
        area = self._available_geometry()
        return max(area.top(), min(int(y), area.bottom() + 1 - self.height()))

    def _fraction_for_y(self, y: int) -> float:
        area = self._available_geometry()
        span = area.height() - self.height()
        if span <= 0:
            return DEFAULT_Y_FRACTION
        return clamp_fraction((self._clamped_y(y) - area.top()) / span)

    def move_vertically_to(self, y: int) -> None:
        """Move along the right edge, clamped inside the screen; x stays docked."""
        area = self._available_geometry()
        y = self._clamped_y(y)
        self._y_fraction = self._fraction_for_y(y)
        self.move(area.right() - self.width() + 1, y)

    # -- painting and mouse ------------------------------------------------------

    def sizeHint(self) -> QSize:  # noqa: D401 - Qt naming
        hint = super().sizeHint()
        return QSize(BAR_WIDTH, hint.height())

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        t = theme.tokens()
        radius = CORNER_RADIUS
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, 0.5, -0.5)
        path = QPainterPath()
        # Rounded on the left, square against the screen edge on the right.
        path.moveTo(rect.right(), rect.top())
        path.lineTo(rect.left() + radius, rect.top())
        path.quadTo(QPointF(rect.left(), rect.top()), QPointF(rect.left(), rect.top() + radius))
        path.lineTo(rect.left(), rect.bottom() - radius)
        path.quadTo(QPointF(rect.left(), rect.bottom()), QPointF(rect.left() + radius, rect.bottom()))
        path.lineTo(rect.right(), rect.bottom())
        path.closeSubpath()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(QPen(QColor(t["border_strong"]), 1))
        painter.setBrush(QColor(t["bg"]))
        painter.drawPath(path)
        painter.end()

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.LeftButton:
            self._drag_offset = int(event.globalPosition().y()) - self.y()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.move_vertically_to(int(event.globalPosition().y()) - self._drag_offset)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._drag_offset is not None and event.button() == Qt.LeftButton:
            self._drag_offset = None
            self.position_changed.emit(self._y_fraction)
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.LeftButton:
            self._drag_offset = None
            self.expand_requested.emit()
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.key() == Qt.Key_Escape:
            self.expand_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)
