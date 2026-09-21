"""Search and inspect the server's session history from the recorder app."""

from __future__ import annotations

import datetime as dt
import threading
from typing import Callable, Optional

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from meeting_notes import config as config_mod
from meeting_notes.client.api import ServerClient


class _WorkerBridge(QObject):
    done = Signal(object)


def _created_text(value) -> str:
    try:
        return dt.datetime.fromtimestamp(float(value)).astimezone().strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return "Unknown date"


class HistoryDialog(QDialog):
    """Desktop view over ``GET /v1/sessions`` and its detail endpoint.

    Network work always happens on a short-lived background thread. The
    recorder window therefore stays responsive if the LAN server is asleep.
    """

    def __init__(self, parent=None, client: Optional[ServerClient] = None):
        super().__init__(parent)
        self.setWindowTitle("Meeting history")
        self.setMinimumSize(860, 560)
        self._client = client
        self._owns_client = client is None
        self._bridges: list[_WorkerBridge] = []
        self._current_session_id: Optional[str] = None

        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search names and transcripts")
        self.search_edit.returnPressed.connect(self.refresh)
        self.refresh_button = QPushButton("Search")
        self.refresh_button.clicked.connect(self.refresh)
        top.addWidget(self.search_edit, 1)
        top.addWidget(self.refresh_button)
        layout.addLayout(top)

        splitter = QSplitter(Qt.Horizontal)
        self.sessions = QListWidget()
        self.sessions.setMinimumWidth(280)
        self.sessions.currentItemChanged.connect(self._selection_changed)
        splitter.addWidget(self.sessions)

        detail = QWidget()
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(10, 0, 0, 0)
        self.heading = QLabel("Select a meeting")
        self.heading.setObjectName("historyHeading")
        self.transcript = QPlainTextEdit()
        self.transcript.setReadOnly(True)
        self.transcript.setPlaceholderText("The final transcript will appear here.")
        detail_layout.addWidget(self.heading)
        detail_layout.addWidget(self.transcript, 1)

        actions = QHBoxLayout()
        self.open_web_button = QPushButton("Open in browser")
        self.open_web_button.clicked.connect(self._open_web)
        self.retranscribe_button = QPushButton("Retranscribe")
        self.retranscribe_button.clicked.connect(self._retranscribe)
        self.delete_audio_button = QPushButton("Delete audio")
        self.delete_audio_button.clicked.connect(self._delete_audio)
        self.delete_button = QPushButton("Delete meeting")
        self.delete_button.setObjectName("danger")
        self.delete_button.clicked.connect(self._delete_session)
        for button in (
            self.open_web_button,
            self.retranscribe_button,
            self.delete_audio_button,
            self.delete_button,
        ):
            button.setEnabled(False)
            actions.addWidget(button)
        actions.addStretch(1)
        detail_layout.addLayout(actions)
        splitter.addWidget(detail)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)

        self.status_label = QLabel("")
        self.status_label.setObjectName("subtle")
        layout.addWidget(self.status_label)

        if self._client is None:
            server = config_mod.server_settings()
            if server.get("url"):
                self._client = ServerClient(server["url"], server.get("token"), timeout=10.0)
            else:
                self.status_label.setText("Configure a server URL in Settings to view history.")
                self.refresh_button.setEnabled(False)
        if self._client is not None:
            self.refresh()

    def closeEvent(self, event):  # noqa: N802 - Qt naming
        if self._owns_client and self._client is not None:
            self._client.close()
        super().closeEvent(event)

    def _run(self, work: Callable[[], object], done: Callable[[object], None]) -> None:
        bridge = _WorkerBridge()
        self._bridges.append(bridge)

        def deliver(result: object) -> None:
            if bridge in self._bridges:
                self._bridges.remove(bridge)
            done(result)

        bridge.done.connect(deliver)

        def runner() -> None:
            try:
                result = work()
            except Exception as exc:  # noqa: BLE001 - rendered in the dialog
                result = exc
            bridge.done.emit(result)

        threading.Thread(target=runner, daemon=True, name="history-request").start()

    def refresh(self) -> None:
        if self._client is None:
            return
        self.refresh_button.setEnabled(False)
        self.status_label.setText("Loading meetings...")
        query = self.search_edit.text().strip() or None
        self._run(lambda: self._client.list_sessions(q=query, per_page=200), self._loaded)

    def _loaded(self, result: object) -> None:
        self.refresh_button.setEnabled(True)
        if isinstance(result, Exception):
            self.status_label.setText(f"Could not load history: {result}")
            return
        self.sessions.clear()
        items = result.get("items", [])
        for session in items:
            state = session.get("latest_state") or "not transcribed"
            title = session.get("name") or session.get("session_id")
            item = QListWidgetItem(f"{title}\n{_created_text(session.get('created'))} · {state}")
            item.setData(Qt.UserRole, session.get("session_id"))
            self.sessions.addItem(item)
        total = int(result.get("total", len(items)))
        self.status_label.setText(f"{total} meeting{'s' if total != 1 else ''}")
        if items:
            self.sessions.setCurrentRow(0)
        else:
            self._clear_detail("No meetings found")

    def _selection_changed(self, current, _previous) -> None:
        if current is None or self._client is None:
            return
        session_id = current.data(Qt.UserRole)
        self._current_session_id = session_id
        self.heading.setText("Loading transcript...")
        self.transcript.clear()
        self._set_actions_enabled(False)
        self._run(lambda: self._client.session_detail(session_id), self._detail_loaded)

    def _detail_loaded(self, result: object) -> None:
        if isinstance(result, Exception):
            self.heading.setText("Could not load meeting")
            self.transcript.setPlainText(str(result))
            return
        meta = result.get("meta") or {}
        title = meta.get("name") or result.get("session_id") or "Meeting"
        jobs = result.get("jobs") or []
        state = jobs[0].get("state") if jobs else "not transcribed"
        self.heading.setText(f"{title} · {state}")
        self.transcript.setPlainText(result.get("markdown") or "No final transcript yet.")
        self._set_actions_enabled(True, has_audio=bool(result.get("has_audio")))

    def _clear_detail(self, heading: str) -> None:
        self._current_session_id = None
        self.heading.setText(heading)
        self.transcript.clear()
        self._set_actions_enabled(False)

    def _set_actions_enabled(self, enabled: bool, *, has_audio: bool = False) -> None:
        self.open_web_button.setEnabled(enabled)
        self.delete_button.setEnabled(enabled)
        self.retranscribe_button.setEnabled(enabled and has_audio)
        self.delete_audio_button.setEnabled(enabled and has_audio)

    def _open_web(self) -> None:
        if not self._current_session_id or self._client is None:
            return
        QDesktopServices.openUrl(
            QUrl(f"{self._client.base_url}/sessions/{self._current_session_id}")
        )

    def _confirm(self, title: str, text: str) -> bool:
        return QMessageBox.question(self, title, text) == QMessageBox.Yes

    def _mutate(self, label: str, call: Callable[[], object]) -> None:
        self.status_label.setText(label)

        def finished(result: object) -> None:
            if isinstance(result, Exception):
                self.status_label.setText(f"Action failed: {result}")
            else:
                self.refresh()

        self._run(call, finished)

    def _retranscribe(self) -> None:
        session_id = self._current_session_id
        if session_id and self._client:
            self._mutate("Queued for retranscription...", lambda: self._client.retranscribe_session(session_id))

    def _delete_audio(self) -> None:
        session_id = self._current_session_id
        if not session_id or not self._client:
            return
        if self._confirm("Delete audio?", "The transcript will remain, but the audio cannot be recovered."):
            self._mutate("Deleting audio...", lambda: self._client.delete_session_audio(session_id))

    def _delete_session(self) -> None:
        session_id = self._current_session_id
        if not session_id or not self._client:
            return
        if self._confirm("Delete meeting?", "This permanently deletes the meeting, audio, and transcript."):
            self._mutate("Deleting meeting...", lambda: self._client.delete_session(session_id))
