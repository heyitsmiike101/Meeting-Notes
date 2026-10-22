"""The "record this meeting?" prompt shown when a call is detected."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from meeting_notes.client.ui import theme
from meeting_notes.client.ui.icons import icon_size, make_icon

AUTO_DISMISS_MS = 60_000
STRIP_WIDTH = 720  # the cards are wide and short: they sit along the top edge
_MARGIN = 6          # plus the card's own shadow gutter, below
_SHADOW = 14


def prompt_screen():
    """The screen the person is using: the one under the mouse, else the primary one.

    Always using the primary screen put prompts on a laptop's own display while it sat closed or ignored
    beside a docked monitor, so a countdown could run out unseen.
    """
    try:
        screen = QGuiApplication.screenAt(QCursor.pos())
    except Exception:  # noqa: BLE001
        screen = None
    return screen or QApplication.primaryScreen()


def place_top_center(widget) -> None:
    """Top centre of the screen in use. Bottom right is where Teams, Zoom and Windows show their own
    notifications, so a card there covered the meeting's "join" or "admit" toasts."""
    screen = prompt_screen()
    if screen is None:
        return
    area = screen.availableGeometry()
    widget.adjustSize()
    widget.move(area.left() + (area.width() - widget.width()) // 2, area.top() + _MARGIN - 1)


class MeetingPrompt(QDialog):
    """Wide, short, always-on-top, non-modal strip at the top centre of the screen."""

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
        self.setMinimumWidth(STRIP_WIDTH)
        self._finished = False

        # A frameless, translucent window holding one toast-like card in the
        # active theme; the transparent gutter around it is where the shadow falls.
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setStyleSheet("QDialog { background: transparent; }")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(_SHADOW, _SHADOW - 4, _SHADOW, _SHADOW + 4)
        card = QFrame()
        card.setObjectName("promptCard")
        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(22)
        shadow.setOffset(0, 5)
        shadow.setColor(QColor(theme.tokens()["shadow"]))
        card.setGraphicsEffect(shadow)
        outer.addWidget(card)

        layout = QHBoxLayout(card)  # one wide, short strip
        layout.setContentsMargins(16, 10, 12, 10)
        layout.setSpacing(12)
        text = QVBoxLayout()
        text.setSpacing(2)
        layout.addLayout(text, 1)
        head = QHBoxLayout()
        head.setSpacing(8)
        tally = QLabel()
        accent = theme.tokens()["accent_text"]
        tally.setPixmap(make_icon("mic", accent, accent, 18).pixmap(18, 18))
        tally.setFixedSize(18, 18)
        head.addWidget(tally, 0, Qt.AlignVCenter)
        self.title_label = QLabel(f"{label} call detected")
        self.title_label.setObjectName("promptTitle")
        head.addWidget(self.title_label, 1)
        text.addLayout(head)
        self.question_label = QLabel("Record this meeting?")
        self.question_label.setObjectName("subtle")
        text.addWidget(self.question_label)
        self.name_edit = QLineEdit(suggested_name)
        self.name_edit.setAccessibleName("Meeting name")
        self.name_edit.setMinimumWidth(240)
        layout.addWidget(self.name_edit, 1)

        row = QHBoxLayout()
        self.later_button = QPushButton("Not now")
        self.later_button.clicked.connect(self._on_not_now)
        self.record_button = QPushButton("Record")
        self.record_button.setObjectName("record")
        self.record_button.setDefault(True)
        self.record_button.clicked.connect(self._on_record)
        self.record_button.setIcon(make_icon("mic", "#ffffff", "#ffffff", 16))
        self.record_button.setIconSize(icon_size(16))
        row.addWidget(self.later_button)
        row.addWidget(self.record_button)
        layout.addLayout(row)
        self.name_edit.returnPressed.connect(self._on_record)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._on_not_now)
        self._timer.start(timeout_ms)

    # -- placement -----------------------------------------------------------

    def place_top_center(self) -> None:
        place_top_center(self)

    def show_prompt(self) -> None:
        self.place_top_center()
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


AUTO_STOP_COUNTDOWN_SEC = 60
AUTO_RECORD_CARD_MS = 20_000


def clock_text(when: datetime) -> str:
    """A wall-clock time the way people say it: ``3:00 PM`` (no leading zero, any platform)."""
    hour = when.hour % 12 or 12
    return f"{hour}:{when.minute:02d} {'AM' if when.hour < 12 else 'PM'}"


def auto_end_detail(
    mode: str, deadline: Optional[datetime] = None, silence_sec: int = 30, end_on_close: bool = False
) -> str:
    """The card's line saying how an auto-recorded call will end.

    ``end_on_close``: the recording also stops when the meeting closes (the "Stop when the meeting closes"
    setting); ``mode`` is then the extra Also-end rule ("call" alone means just the close).
    """
    if mode == "hour" and deadline is not None:
        rule, alone = f"on the hour ({clock_text(deadline)})", f"Stops at {clock_text(deadline)}"
    elif mode == "silence":
        rule = f"after {silence_sec} seconds of silence"
        alone = f"Stops after {silence_sec} seconds of silence"
    elif mode == "bye":
        rule = alone = "after goodbyes and 20 seconds of silence"
        alone = "Stops " + alone
    else:
        rule = alone = ""
    if end_on_close or mode == "call":
        return "Stops when the meeting closes" + (f" or {rule}" if rule else "")
    return alone or "Stop it yourself when the meeting is over"


class AutoRecordCard(QDialog):
    """"Recording this call" notice, shown when auto record starts a recording.

    Bottom-right like the other cards, never takes focus, and goes away by itself.
    Emits ``disable_requested`` (Disable auto end) or ``dismissed`` (OK, Escape,
    closing it, or the timeout); ``close_silently`` removes it without either.
    """

    disable_requested = Signal()
    dismissed = Signal()

    def __init__(
        self,
        label: str,
        meeting_name: str,
        mode: str,
        deadline: Optional[datetime] = None,
        parent=None,
        timeout_ms: int = AUTO_RECORD_CARD_MS,
        end_on_close: bool = False,
    ):
        super().__init__(parent)
        self.setWindowFlags(Qt.Tool | Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint)
        self.setWindowTitle("Recording a call")
        self.setModal(False)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setMinimumWidth(STRIP_WIDTH)
        self._finished = False
        self.mode = mode

        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setStyleSheet("QDialog { background: transparent; }")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(_SHADOW, _SHADOW - 4, _SHADOW, _SHADOW + 4)
        card = QFrame()
        card.setObjectName("promptCard")
        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(22)
        shadow.setOffset(0, 5)
        shadow.setColor(QColor(theme.tokens()["shadow"]))
        card.setGraphicsEffect(shadow)
        outer.addWidget(card)

        layout = QHBoxLayout(card)  # one wide, short strip
        layout.setContentsMargins(16, 10, 12, 10)
        layout.setSpacing(12)
        text = QVBoxLayout()
        text.setSpacing(2)
        layout.addLayout(text, 1)
        head = QHBoxLayout()
        head.setSpacing(8)
        tally = QLabel()
        accent = theme.tokens()["accent_text"]
        tally.setPixmap(make_icon("mic", accent, accent, 18).pixmap(18, 18))
        tally.setFixedSize(18, 18)
        head.addWidget(tally, 0, Qt.AlignVCenter)
        self.title_label = QLabel(f"Recording {label} call")
        self.title_label.setObjectName("promptTitle")
        head.addWidget(self.title_label, 1)
        text.addLayout(head)
        self.name_label = QLabel(meeting_name)
        self.name_label.setObjectName("subtle")
        self.detail_label = QLabel(auto_end_detail(mode, deadline, end_on_close=end_on_close))
        self.detail_label.setObjectName("subtle")
        # Both on one line keeps the strip short: "Mike/Tara 1:1 · Stops when the meeting closes".
        details = QHBoxLayout()
        details.setSpacing(6)
        details.addWidget(self.name_label)
        dot = QLabel("·")
        dot.setObjectName("subtle")
        details.addWidget(dot)
        details.addWidget(self.detail_label)
        details.addStretch(1)
        text.addLayout(details)

        row = QHBoxLayout()
        self.disable_button = None
        if mode != "manual" or end_on_close:
            self.disable_button = QPushButton("Disable auto end")
            self.disable_button.setAutoDefault(False)  # only OK looks like the primary action
            self.disable_button.clicked.connect(self._on_disable)
            row.addWidget(self.disable_button)
        self.ok_button = QPushButton("OK")
        self.ok_button.setObjectName("record")
        self.ok_button.setDefault(True)
        self.ok_button.clicked.connect(self._on_ok)
        row.addWidget(self.ok_button)
        layout.addLayout(row)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._on_ok)
        self._timer.start(timeout_ms)

    def show_prompt(self) -> None:
        place_top_center(self)
        self.show()
        self.raise_()

    def _finish(self) -> bool:
        if self._finished:
            return False
        self._finished = True
        self._timer.stop()
        return True

    def _on_disable(self) -> None:
        if self._finish():
            self.hide()
            self.disable_requested.emit()
            self.deleteLater()

    def _on_ok(self) -> None:
        if self._finish():
            self.hide()
            self.dismissed.emit()
            self.deleteLater()

    def close_silently(self) -> None:
        self._finish()
        self.hide()
        self.deleteLater()

    def closeEvent(self, event):  # noqa: N802 - Qt naming
        if not self._finished:
            self._on_ok()
        super().closeEvent(event)

    def keyPressEvent(self, event):  # noqa: N802 - Qt naming
        if event.key() == Qt.Key_Escape:
            self._on_ok()
            return
        super().keyPressEvent(event)


class CallEndingPrompt(QDialog):
    """"Call seems to have ended -- stopping in N s" banner (bottom-right).

    Emits exactly one of ``keep_requested`` (Keep recording / Escape / close),
    ``stop_requested`` (Stop now) or ``expired`` (countdown reached zero).
    """

    keep_requested = Signal()
    stop_requested = Signal()
    expired = Signal()

    def __init__(
        self,
        seconds: int = AUTO_STOP_COUNTDOWN_SEC,
        parent=None,
        autostart: bool = True,
        title: str = "Call seems to have ended",
    ):
        super().__init__(parent)
        self.setWindowFlags(Qt.Tool | Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint)
        self.setWindowTitle("Call ended")
        self.setModal(False)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setMinimumWidth(STRIP_WIDTH)
        self._finished = False
        self.remaining = int(seconds)

        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setStyleSheet("QDialog { background: transparent; }")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(_SHADOW, _SHADOW - 4, _SHADOW, _SHADOW + 4)
        card = QFrame()
        card.setObjectName("promptCard")
        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(22)
        shadow.setOffset(0, 5)
        shadow.setColor(QColor(theme.tokens()["shadow"]))
        card.setGraphicsEffect(shadow)
        outer.addWidget(card)

        layout = QHBoxLayout(card)  # one wide, short strip
        layout.setContentsMargins(16, 10, 12, 10)
        layout.setSpacing(12)
        text = QVBoxLayout()
        text.setSpacing(2)
        layout.addLayout(text, 1)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("promptTitle")
        text.addWidget(self.title_label)
        self.countdown_label = QLabel()
        self.countdown_label.setObjectName("subtle")
        text.addWidget(self.countdown_label)
        row = QHBoxLayout()
        self.stop_button = QPushButton("Stop now")
        self.stop_button.clicked.connect(self._on_stop)
        self.keep_button = QPushButton("Keep recording")
        self.keep_button.setObjectName("record")
        self.keep_button.setDefault(True)
        self.keep_button.clicked.connect(self._on_keep)
        row.addWidget(self.stop_button)
        row.addWidget(self.keep_button)
        layout.addLayout(row)
        self._update_text()

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.tick)
        if autostart:
            self._timer.start()

    def _update_text(self) -> None:
        self.countdown_label.setText(f"Stopping in {self.remaining} s unless you keep recording.")

    def tick(self) -> None:
        """One second of countdown (public so tests can drive it)."""
        if self._finished:
            return
        self.remaining -= 1
        if self.remaining <= 0:
            self.remaining = 0
            self._update_text()
            if self._finish():
                self.hide()
                self.expired.emit()
                self.deleteLater()
            return
        self._update_text()

    def show_prompt(self) -> None:
        place_top_center(self)
        self.show()
        self.raise_()

    def _finish(self) -> bool:
        if self._finished:
            return False
        self._finished = True
        self._timer.stop()
        return True

    def _on_stop(self) -> None:
        if self._finish():
            self.hide()
            self.stop_requested.emit()
            self.deleteLater()

    def _on_keep(self) -> None:
        if self._finish():
            self.hide()
            self.keep_requested.emit()
            self.deleteLater()

    def close_silently(self) -> None:
        self._finish()
        self.hide()
        self.deleteLater()

    def closeEvent(self, event):  # noqa: N802 - Qt naming
        if not self._finished:
            self._on_keep()
        super().closeEvent(event)

    def keyPressEvent(self, event):  # noqa: N802 - Qt naming
        if event.key() == Qt.Key_Escape:
            self._on_keep()
            return
        super().keyPressEvent(event)


class StopSuggestionPrompt(QDialog):
    """"Meeting seems to have ended -- stop recording?" card (bottom-right).

    A suggestion only: unlike ``CallEndingPrompt`` there is no countdown, and
    nothing ever stops the recording except the person pressing **Stop recording**.
    Emits exactly one of ``stop_requested`` or ``keep_requested`` (Keep recording,
    Escape, or closing the card); ``close_silently`` removes it without either
    (audio came back, the call restarted, the recording ended).
    """

    stop_requested = Signal()
    keep_requested = Signal()

    def __init__(self, title: str = "Meeting seems to have ended", detail: str = "Stop recording?", parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Tool | Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint)
        self.setWindowTitle("Stop recording?")
        self.setModal(False)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setMinimumWidth(STRIP_WIDTH)
        self._finished = False

        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setStyleSheet("QDialog { background: transparent; }")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(_SHADOW, _SHADOW - 4, _SHADOW, _SHADOW + 4)
        card = QFrame()
        card.setObjectName("promptCard")
        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(22)
        shadow.setOffset(0, 5)
        shadow.setColor(QColor(theme.tokens()["shadow"]))
        card.setGraphicsEffect(shadow)
        outer.addWidget(card)

        layout = QHBoxLayout(card)  # one wide, short strip
        layout.setContentsMargins(16, 10, 12, 10)
        layout.setSpacing(12)
        text = QVBoxLayout()
        text.setSpacing(2)
        layout.addLayout(text, 1)
        head = QHBoxLayout()
        head.setSpacing(8)
        tally = QLabel()
        accent = theme.tokens()["accent_text"]
        tally.setPixmap(make_icon("info", accent, accent, 18).pixmap(18, 18))
        tally.setFixedSize(18, 18)
        head.addWidget(tally, 0, Qt.AlignVCenter)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("promptTitle")
        head.addWidget(self.title_label, 1)
        text.addLayout(head)
        self.detail_label = QLabel(detail)
        self.detail_label.setObjectName("subtle")
        self.detail_label.setWordWrap(True)
        text.addWidget(self.detail_label)
        row = QHBoxLayout()
        self.keep_button = QPushButton("Keep recording")
        self.keep_button.setAutoDefault(False)  # only Stop looks like the primary action
        self.keep_button.clicked.connect(self._on_keep)
        self.stop_button = QPushButton("Stop recording")
        self.stop_button.setObjectName("record")
        self.stop_button.setDefault(True)
        self.stop_button.clicked.connect(self._on_stop)
        row.addWidget(self.keep_button)
        row.addWidget(self.stop_button)
        layout.addLayout(row)

    def show_prompt(self) -> None:
        place_top_center(self)
        self.show()
        self.raise_()

    def _finish(self) -> bool:
        if self._finished:
            return False
        self._finished = True
        return True

    def _on_stop(self) -> None:
        if self._finish():
            self.hide()
            self.stop_requested.emit()
            self.deleteLater()

    def _on_keep(self) -> None:
        if self._finish():
            self.hide()
            self.keep_requested.emit()
            self.deleteLater()

    def close_silently(self) -> None:
        self._finish()
        self.hide()
        self.deleteLater()

    def closeEvent(self, event):  # noqa: N802 - Qt naming
        if not self._finished:
            self._on_keep()
        super().closeEvent(event)

    def keyPressEvent(self, event):  # noqa: N802 - Qt naming
        if event.key() == Qt.Key_Escape:
            self._on_keep()
            return
        super().keyPressEvent(event)


class AutoEndedNotice(QDialog):
    """"Recording stopped automatically" card (bottom-right), shown after an auto end stops a recording.

    It stays until dismissed (Dismiss, Escape or closing it) so a stop that happened while the person was
    away is still explained when they come back. **Open** brings the main window forward.
    """

    open_requested = Signal()
    dismissed = Signal()

    def __init__(self, detail: str, title: str = "Recording stopped automatically", parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Tool | Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint)
        self.setWindowTitle(title)
        self.setModal(False)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setMinimumWidth(STRIP_WIDTH)
        self._finished = False

        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setStyleSheet("QDialog { background: transparent; }")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(_SHADOW, _SHADOW - 4, _SHADOW, _SHADOW + 4)
        card = QFrame()
        card.setObjectName("promptCard")
        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(22)
        shadow.setOffset(0, 5)
        shadow.setColor(QColor(theme.tokens()["shadow"]))
        card.setGraphicsEffect(shadow)
        outer.addWidget(card)

        layout = QHBoxLayout(card)  # one wide, short strip
        layout.setContentsMargins(16, 10, 12, 10)
        layout.setSpacing(12)
        text = QVBoxLayout()
        text.setSpacing(2)
        layout.addLayout(text, 1)
        head = QHBoxLayout()
        head.setSpacing(8)
        tally = QLabel()
        accent = theme.tokens()["accent_text"]
        tally.setPixmap(make_icon("info", accent, accent, 18).pixmap(18, 18))
        tally.setFixedSize(18, 18)
        head.addWidget(tally, 0, Qt.AlignVCenter)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("promptTitle")
        head.addWidget(self.title_label, 1)
        text.addLayout(head)
        self.detail_label = QLabel(detail)
        self.detail_label.setObjectName("subtle")
        self.when_label = QLabel(f"Stopped at {datetime.now().strftime('%I:%M %p').lstrip('0')}")
        self.when_label.setObjectName("subtle")
        details = QHBoxLayout()  # one line: "The meeting closed. · Stopped at 2:31 PM"
        details.setSpacing(6)
        details.addWidget(self.detail_label)
        dot = QLabel("·")
        dot.setObjectName("subtle")
        details.addWidget(dot)
        details.addWidget(self.when_label)
        details.addStretch(1)
        text.addLayout(details)
        row = QHBoxLayout()
        self.open_button = QPushButton("Open Meeting Notes")
        self.open_button.setAutoDefault(False)
        self.open_button.clicked.connect(self._on_open)
        self.dismiss_button = QPushButton("Dismiss")
        self.dismiss_button.setObjectName("record")
        self.dismiss_button.setDefault(True)
        self.dismiss_button.clicked.connect(self._on_dismiss)
        row.addWidget(self.open_button)
        row.addWidget(self.dismiss_button)
        layout.addLayout(row)

    def show_prompt(self) -> None:
        place_top_center(self)
        self.show()
        self.raise_()

    def _finish(self) -> bool:
        if self._finished:
            return False
        self._finished = True
        return True

    def _on_open(self) -> None:
        if self._finish():
            self.hide()
            self.open_requested.emit()
            self.deleteLater()

    def _on_dismiss(self) -> None:
        if self._finish():
            self.hide()
            self.dismissed.emit()
            self.deleteLater()

    def close_silently(self) -> None:
        self._finish()
        self.hide()
        self.deleteLater()

    def closeEvent(self, event):  # noqa: N802 - Qt naming
        if not self._finished:
            self._on_dismiss()
        super().closeEvent(event)

    def keyPressEvent(self, event):  # noqa: N802 - Qt naming
        if event.key() == Qt.Key_Escape:
            self._on_dismiss()
            return
        super().keyPressEvent(event)
