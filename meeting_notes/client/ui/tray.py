"""The notification-area icons.

``GpuTray`` (Windows) is shown while the GPU helper is on. Menu: a status line (not clickable), Show Meeting Notes,
Pause / Resume GPU helper, Quit. Closing the main window hides it here instead of quitting, so the helper keeps
working.

``RecorderTray`` (macOS) is the recorder's menu-bar icon, up for as long as the app runs, beside the window (Mac
apps keep a status item in the menu bar; without one the recorder was either its window or nothing). Menu: the
recording status, Start / Stop recording, Mute you / Mute them, Show Meeting Notes, Quit Meeting Notes.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, Optional

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from meeting_notes.client.ui import theme
from meeting_notes.client.ui.compact_bar import format_elapsed
from meeting_notes.client.ui.icons import make_icon

TITLE = "Meeting Notes"


def app_icon() -> QIcon:
    """The app icon: the window's own, else the bundled .ico, else the mic glyph in the theme's colours."""
    app = QApplication.instance()
    icon = app.windowIcon() if app is not None else QIcon()
    if not icon.isNull():
        return icon
    here = Path(__file__).resolve()
    for base in (here.parents[3], Path.cwd(), Path(__file__).resolve().parent):
        candidate = base / "assets" / "meeting-notes.ico"
        if candidate.exists():
            icon = QIcon(str(candidate))
            if not icon.isNull():
                return icon
    return make_icon("mic", size=32)


class GpuTray(QObject):
    """A ``QSystemTrayIcon`` with the helper's menu. ``available()`` is False where there is no tray."""

    open_requested = Signal()
    pause_toggled = Signal(bool)  # True: pause
    quit_requested = Signal()

    def __init__(self, parent: Optional[QObject] = None, *, icon: Optional[QIcon] = None):
        super().__init__(parent)
        self._paused = False
        self.icon = QSystemTrayIcon(icon or app_icon(), self)
        self.menu = QMenu()
        self.status_action = QAction("Off", self.menu)
        self.status_action.setEnabled(False)
        self.open_action = QAction("Show Meeting Notes", self.menu)
        self.pause_action = QAction("Pause GPU helper", self.menu)
        self.quit_action = QAction("Quit", self.menu)
        self.menu.addAction(self.status_action)
        self.menu.addSeparator()
        self.menu.addAction(self.open_action)
        self.menu.addAction(self.pause_action)
        self.menu.addSeparator()
        self.menu.addAction(self.quit_action)
        self.open_action.triggered.connect(lambda _c=False: self.open_requested.emit())
        self.pause_action.triggered.connect(self._on_pause)
        self.quit_action.triggered.connect(lambda _c=False: self.quit_requested.emit())
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._on_activated)
        self.icon.setToolTip(TITLE)

    @staticmethod
    def available() -> bool:
        return QSystemTrayIcon.isSystemTrayAvailable()

    @property
    def visible(self) -> bool:
        return self.icon.isVisible()

    def show(self) -> None:
        self.icon.show()

    def hide(self) -> None:
        self.icon.hide()

    def set_status(self, text: str) -> None:
        self.status_action.setText(f"GPU helper: {text}")
        self.icon.setToolTip(f"{TITLE} \u00b7 GPU helper: {text}")

    def set_paused(self, paused: bool) -> None:
        self._paused = bool(paused)
        self.pause_action.setText("Resume GPU helper" if self._paused else "Pause GPU helper")

    @property
    def paused(self) -> bool:
        return self._paused

    def _on_pause(self, _checked=False) -> None:
        self.set_paused(not self._paused)
        self.pause_toggled.emit(self._paused)

    def _on_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.open_requested.emit()


def recorder_tray_wanted(platform: Optional[str] = None) -> bool:
    """The recorder's own menu-bar icon is a Mac thing (Windows has the GPU helper's tray, when that is on)."""
    return (platform or sys.platform) == "darwin" and QSystemTrayIcon.isSystemTrayAvailable()


def menu_bar_icon(recording: bool) -> QIcon:
    """The mic glyph as a template image (macOS tints it for a light or dark menu bar); red while recording."""
    if recording:
        return make_icon("record", colour=theme.tokens()["danger"], size=18)
    icon = make_icon("mic", colour="#000000", size=18)
    icon.setIsMask(True)
    return icon


class RecorderTray(QObject):
    """The Mac menu-bar icon and its menu. The window does the work: these only emit what was picked."""

    open_requested = Signal()
    record_requested = Signal()  # Start or Stop, whichever the menu said
    mute_toggled = Signal(str, bool)  # track, muted
    quit_requested = Signal()

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._recording = False
        self.icon = QSystemTrayIcon(menu_bar_icon(False), self)
        self.menu = QMenu()
        self.status_action = QAction("Idle", self.menu)
        self.status_action.setEnabled(False)
        self.record_action = QAction("Start recording", self.menu)
        self.mute_actions: Dict[str, QAction] = {
            "mic": QAction("Mute you", self.menu),
            "system": QAction("Mute them", self.menu),
        }
        self.open_action = QAction("Show Meeting Notes", self.menu)
        self.quit_action = QAction("Quit Meeting Notes", self.menu)
        self.menu.addAction(self.status_action)
        self.menu.addSeparator()
        self.menu.addAction(self.record_action)
        for track, action in self.mute_actions.items():
            action.setCheckable(True)
            action.setEnabled(False)
            action.toggled.connect(lambda muted, t=track: self.mute_toggled.emit(t, bool(muted)))
            self.menu.addAction(action)
        self.menu.addSeparator()
        self.menu.addAction(self.open_action)
        self.menu.addSeparator()
        self.menu.addAction(self.quit_action)
        self.record_action.triggered.connect(lambda _c=False: self.record_requested.emit())
        self.open_action.triggered.connect(lambda _c=False: self.open_requested.emit())
        self.quit_action.triggered.connect(lambda _c=False: self.quit_requested.emit())
        self.icon.setContextMenu(self.menu)  # a click on a Mac status item opens its menu
        self.icon.setToolTip(TITLE)

    @property
    def visible(self) -> bool:
        return self.icon.isVisible()

    def show(self) -> None:
        self.icon.show()

    def hide(self) -> None:
        self.icon.hide()

    def update_state(self, state: str, elapsed: float = 0.0, muted: Optional[Dict[str, bool]] = None,
                     mute_enabled: Optional[Dict[str, bool]] = None) -> None:
        """Mirror the window: ``state`` is "idle", "recording" or "finishing" (the compact bar's states)."""
        muted, mute_enabled = muted or {}, mute_enabled or {}
        recording = state == "recording"
        if state == "recording":
            status, record, can_record = f"Recording {format_elapsed(elapsed)}", "Stop recording", True
        elif state == "finishing":
            status, record, can_record = "Finishing", "Finishing...", False
        else:
            status, record, can_record = "Idle", "Start recording", True
        if self.status_action.text() != status:
            self.status_action.setText(status)
            self.icon.setToolTip(f"{TITLE} \u00b7 {status}")
        if self.record_action.text() != record:
            self.record_action.setText(record)
        self.record_action.setEnabled(can_record)
        for track, action in self.mute_actions.items():
            want = bool(muted.get(track))
            if action.isChecked() != want:
                action.blockSignals(True)  # the window already is in this state: nothing to send back
                action.setChecked(want)
                action.blockSignals(False)
            action.setEnabled(recording and bool(mute_enabled.get(track)))
        if recording != self._recording:
            self._recording = recording
            self.icon.setIcon(menu_bar_icon(recording))
