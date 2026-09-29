"""The Logs window: every diagnostic the client keeps, in one place.

Left, the sources (client log, startup errors, audio devices, upload queue,
configuration, About). Right, a read-only monospace viewer, with the actions
that turn a bad day into something you can hand over: Copy, Open logs folder,
Save all as .zip and Send to server. Everything shown or exported is redacted.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QObject, QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices, QFont, QFontDatabase
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from meeting_notes import config as config_mod
from meeting_notes.client import logs as logs_mod
from meeting_notes.client import logsetup
from meeting_notes.client.ui.theme import make_sheet

log = logging.getLogger("meeting_notes.client.ui.logs")


class _Bridge(QObject):
    done = Signal(object)


def _mono_font() -> QFont:
    families = set(QFontDatabase.families())
    for name in ("Cascadia Mono", "Consolas", "Courier New"):
        if name in families:
            font = QFont(name)
            break
    else:
        font = QFontDatabase.systemFont(QFontDatabase.FixedFont)
    font.setPixelSize(12)
    font.setStyleHint(QFont.Monospace)
    return font


class LogsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Logs")
        self.setMinimumSize(900, 560)
        self._bridges: list = []
        self._server = config_mod.server_settings()

        layout = make_sheet(self, "Logs")
        row = QHBoxLayout()
        row.setSpacing(14)

        self.sources = QListWidget()
        self.sources.setObjectName("sources")
        self.sources.setAccessibleName("Log sources")
        self.sources.setFixedWidth(210)
        self._keys = []
        for key, title, _name in logs_mod.SOURCES:
            item = QListWidgetItem(title)
            item.setData(Qt.UserRole, key)
            self.sources.addItem(item)
            self._keys.append(key)
        self.sources.currentRowChanged.connect(self._show_current)
        row.addWidget(self.sources)

        right = QVBoxLayout()
        right.setSpacing(10)
        self.title_label = QLabel("")
        self.title_label.setObjectName("historyHeading")
        right.addWidget(self.title_label)
        self.viewer = QPlainTextEdit()
        self.viewer.setObjectName("logView")
        self.viewer.setAccessibleName("Log contents")
        self.viewer.setReadOnly(True)
        self.viewer.setLineWrapMode(QPlainTextEdit.NoWrap)
        mono = _mono_font()
        self.viewer.setFont(mono)
        # The app stylesheet sets the UI face on every widget, so the monospace
        # face has to be asserted at widget level as well.
        self.viewer.setStyleSheet(f'font-family: "{mono.family()}"; font-size: 12px;')
        right.addWidget(self.viewer, 1)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh)
        self.copy_button = QPushButton("Copy")
        self.copy_button.clicked.connect(self._copy)
        self.folder_button = QPushButton("Open logs folder")
        self.folder_button.clicked.connect(self._open_folder)
        self.zip_button = QPushButton("Save all as .zip...")
        self.zip_button.clicked.connect(self._save_zip)
        self.send_button = QPushButton("Send to server")
        self.send_button.clicked.connect(self._send)
        for button in (
            self.refresh_button,
            self.copy_button,
            self.folder_button,
            self.zip_button,
            self.send_button,
        ):
            button.setAutoDefault(False)
            actions.addWidget(button)
        actions.addStretch(1)
        right.addLayout(actions)
        row.addLayout(right, 1)
        layout.addLayout(row, 1)

        self.status_label = QLabel("")
        self.status_label.setObjectName("status")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        url = (self._server.get("url") or "").strip()
        if not url:
            self.send_button.setEnabled(False)
            self.send_button.setToolTip("Configure a server URL in Settings to send logs")
            self.status_label.setText("Send to server is off until a server URL is set in Settings.")

        self.sources.setCurrentRow(0)

    # -- content -------------------------------------------------------------

    def source_keys(self) -> list:
        return list(self._keys)

    def select(self, key: str) -> None:
        if key in self._keys:
            self.sources.setCurrentRow(self._keys.index(key))

    def _current_key(self) -> str:
        row = self.sources.currentRow()
        return self._keys[row] if 0 <= row < len(self._keys) else self._keys[0]

    def _show_current(self, _row: int = 0) -> None:
        key = self._current_key()
        title = next(t for k, t, _n in logs_mod.SOURCES if k == key)
        self.title_label.setText(title)
        self.viewer.setPlainText(logs_mod.source_text(key))
        if key == logs_mod.CLIENT_LOG:
            # Newest entries are at the bottom of the file: land there.
            cursor = self.viewer.textCursor()
            cursor.movePosition(cursor.MoveOperation.End)
            self.viewer.setTextCursor(cursor)

    def refresh(self) -> None:
        self._show_current()
        self.status_label.setText("Refreshed.")

    # -- actions ---------------------------------------------------------------

    def _copy(self) -> None:
        QApplication.clipboard().setText(self.viewer.toPlainText())
        self.status_label.setText("Copied to the clipboard.")

    def _open_folder(self) -> None:
        folder = logsetup.log_dir()
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _save_zip(self) -> None:
        suggested = str(Path.home() / logs_mod.default_zip_name())
        chosen, _ = QFileDialog.getSaveFileName(
            self, "Save all logs", suggested, "Zip archive (*.zip)"
        )
        if not chosen:
            return
        try:
            logs_mod.build_zip(Path(chosen))
        except OSError as exc:
            self.status_label.setText(f"Could not save the zip: {exc}")
            return
        self.status_label.setText(f"Saved {chosen}")
        log.info("saved log bundle to %s", chosen)

    def _send(self) -> None:
        url = (self._server.get("url") or "").strip()
        token = self._server.get("token") or ""
        name = logs_mod.default_zip_name()
        self.send_button.setEnabled(False)
        self.status_label.setText("Sending logs...")

        def work():
            return logs_mod.send_zip(url, token, logs_mod.build_zip(), name)

        self._run(work, self._sent)

    def _sent(self, result) -> None:
        self.send_button.setEnabled(bool((self._server.get("url") or "").strip()))
        if isinstance(result, Exception):
            self.status_label.setText(f"Could not send: {result}")
            return
        self.status_label.setText(result.message)

    def _run(self, work: Callable[[], object], done: Callable[[object], None]) -> None:
        bridge = _Bridge()
        self._bridges.append(bridge)

        def deliver(result: object) -> None:
            if bridge in self._bridges:
                self._bridges.remove(bridge)
            done(result)

        bridge.done.connect(deliver)

        def runner() -> None:
            try:
                result = work()
            except Exception as exc:  # noqa: BLE001
                result = exc
            bridge.done.emit(result)

        threading.Thread(target=runner, daemon=True, name="logs-send").start()
