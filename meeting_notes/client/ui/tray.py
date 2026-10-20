"""The notification-area icon shown while the GPU helper is on.

Menu: a status line (not clickable), Open Meeting Notes, Pause / Resume GPU helper, Quit. Closing the main window
hides it here instead of quitting, so the helper keeps working.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

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
        self.open_action = QAction("Open Meeting Notes", self.menu)
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
