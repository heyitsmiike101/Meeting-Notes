"""The collapsed recorder: a thin bar across the top of the window, under the header.

Collapsing the recorder (the header's recorder toggle) puts this bar in place of the recording card, the meters and
the live preview. It keeps what a running meeting needs at a glance: the status dot, the elapsed time, two small level
meters and Start/Stop. The header's recorder toggle, always visible just above it, expands the recorder again. With
the notes pane open the bar sits above the notes; with them closed the window shrinks to the header plus this bar.

Like the compact bar, the strip owns no recording state: the window pushes it in with :meth:`RecorderStrip.update_state`
on its own tick, mirrors its record button with :meth:`RecorderStrip.mirror_record_button`, and handles
``record_clicked`` with its existing record button.
"""

from __future__ import annotations

from typing import Dict, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QWidget

from meeting_notes.client.ui import theme
from meeting_notes.client.ui.compact_bar import STATE_FINISHING, STATE_IDLE, STATE_RECORDING, _Dot, _Meter
from meeting_notes.client.ui.icons import icon_size

STRIP_METER_SIZE = (6, 22)
ALERT_TEXT_WIDTH = 380  # an audio-device alert is a sentence; the bar shows its start, the tooltip all of it

_STATUS_TEXT = {STATE_IDLE: "Ready", STATE_RECORDING: "Recording", STATE_FINISHING: "Finishing"}


class RecorderStrip(QFrame):
    """The bar. Signals only; the window's own record button does the work."""

    record_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("recorderStrip")
        self._state = STATE_IDLE
        self._alert = ""
        outer = QHBoxLayout(self)
        outer.setContentsMargins(20, 0, 20, 0)
        outer.setSpacing(0)
        inner = QWidget()
        inner.setMaximumWidth(1560)  # the header's readable width, so the two line up on a wide screen
        row = QHBoxLayout(inner)
        row.setContentsMargins(0, 8, 0, 8)
        row.setSpacing(10)
        outer.addStretch(1)
        outer.addWidget(inner, 100)
        outer.addStretch(1)

        self.dot = _Dot()
        row.addWidget(self.dot, 0, Qt.AlignVCenter)
        self.clock = QLabel("00:00:00")
        self.clock.setObjectName("stripClock")
        self.clock.setAccessibleName("Elapsed recording time")
        clock_font = self.clock.font()
        theme.enable_tabular(clock_font)
        self.clock.setFont(clock_font)
        row.addWidget(self.clock, 0, Qt.AlignVCenter)
        self.status = QLabel(_STATUS_TEXT[STATE_IDLE])
        self.status.setObjectName("stripStatus")
        row.addWidget(self.status, 0, Qt.AlignVCenter)
        row.addSpacing(4)
        self.meters: Dict[str, _Meter] = {}
        for track, tip in (("mic", "You"), ("system", "Them")):
            meter = _Meter(track)
            meter.setFixedSize(*STRIP_METER_SIZE)
            meter.setToolTip(f"{tip}: input level")
            self.meters[track] = meter
            row.addWidget(meter, 0, Qt.AlignVCenter)
        row.addStretch(1)

        self.record_button = QPushButton("Start recording")
        self.record_button.setObjectName("record")
        self.record_button.setIconSize(icon_size(16))
        self.record_button.setCursor(Qt.PointingHandCursor)
        self.record_button.clicked.connect(self.record_clicked)
        row.addWidget(self.record_button, 0, Qt.AlignVCenter)
        self.apply_theme()

    @property
    def state(self) -> str:
        return self._state

    def status_text(self) -> str:
        return _STATUS_TEXT[self._state]

    def apply_theme(self) -> None:
        self.dot.update()
        for meter in self.meters.values():
            meter.update()

    def update_state(
        self,
        state: str,
        clock_text: str,
        levels: Optional[Dict[str, float]] = None,
        muted: Optional[Dict[str, bool]] = None,
    ) -> None:
        """The window's tick: the dot, clock, status word and meters (live only while recording)."""
        state = state if state in _STATUS_TEXT else STATE_IDLE
        self._state = state
        self.dot.set_state(state)
        if self.clock.text() != clock_text:
            self.clock.setText(clock_text)
        live = "true" if state != STATE_IDLE else "false"
        if self.clock.property("live") != live:
            self.clock.setProperty("live", live)
            self.clock.style().unpolish(self.clock)
            self.clock.style().polish(self.clock)
        self._show_status()
        levels = levels or {}
        muted = muted or {}
        for track, meter in self.meters.items():
            meter.set_level(levels.get(track, 0.0), bool(muted.get(track)), state == STATE_RECORDING)

    def set_alert(self, text: str) -> None:
        """An audio-device alert from the hidden recorder (e.g. a microphone gone): shown in red in place of the status."""
        text = " ".join((text or "").split())
        if text == self._alert:
            return
        self._alert = text
        self.status.setProperty("alert", "true" if text else "false")
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)
        self._show_status()

    def alert_text(self) -> str:
        return self._alert

    def _show_status(self) -> None:
        if self._alert:
            shown = QFontMetrics(self.status.font()).elidedText(self._alert, Qt.ElideRight, ALERT_TEXT_WIDTH)
            self.status.setToolTip(self._alert)
        else:
            shown = _STATUS_TEXT[self._state]
            self.status.setToolTip("")
        if self.status.text() != shown:
            self.status.setText(shown)

    def mirror_record_button(self, button: QPushButton) -> None:
        """Copy the window's record button: wording, icon, look (Start accent, Stop red) and enabled state."""
        mine = self.record_button
        if mine.text() != button.text():
            mine.setText(button.text())
        mine.setIcon(button.icon())
        mine.setEnabled(button.isEnabled())
        mine.setToolTip(button.toolTip())
        if mine.objectName() != button.objectName():
            mine.setObjectName(button.objectName())
            mine.style().unpolish(mine)
            mine.style().polish(mine)
