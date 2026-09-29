"""Search and inspect the server's session history from the recorder app."""

from __future__ import annotations

import datetime as dt
import threading
from typing import Callable, Optional

from PySide6.QtCore import QObject, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QPainter, QPen
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
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from meeting_notes import config as config_mod
from meeting_notes.client.api import ServerClient
from meeting_notes.client.ui.theme import make_sheet


class _WorkerBridge(QObject):
    done = Signal(object)


class _SpineDelegate(QStyledItemDelegate):
    """One meeting as a tape-box spine: name over a condensed date/state line.

    The item text stays two lines, name then "date · state" (that is what
    tests and accessibility read); this only decides how it is painted.
    """

    ROW_HEIGHT = 58

    def sizeHint(self, option, index):  # noqa: N802 - Qt naming
        return QSize(option.rect.width(), self.ROW_HEIGHT)

    def paint(self, painter, option, index):  # noqa: N802 - Qt naming
        painter.save()
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        selected = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)
        rect = option.rect
        if selected:
            painter.fillRect(rect, QColor("#c8372d"))
        elif hovered:
            painter.fillRect(rect, QColor("#d9c8a5"))
        painter.setPen(QPen(QColor("#d9c8a5") if not selected else QColor("#a52c23"), 1))
        painter.drawLine(rect.left(), rect.bottom(), rect.right(), rect.bottom())

        title, _, detail = str(index.data(Qt.DisplayRole) or "").partition("\n")
        ink = QColor("#ffffff") if selected else QColor("#24211b")
        muted = QColor("#f6dedb") if selected else QColor("#5f5747")

        title_font = QFont(option.font)
        title_font.setPixelSize(15)
        title_font.setWeight(QFont.DemiBold)
        painter.setFont(title_font)
        painter.setPen(ink)
        text_rect = rect.adjusted(14, 9, -12, 0)
        metrics = painter.fontMetrics()
        painter.drawText(
            QRect(text_rect.left(), text_rect.top(), text_rect.width(), 20),
            Qt.AlignLeft | Qt.AlignVCenter,
            metrics.elidedText(title, Qt.ElideRight, text_rect.width()),
        )
        detail_font = QFont("Barlow Condensed")
        detail_font.setPixelSize(13)
        detail_font.setWeight(QFont.Medium)
        detail_font.setLetterSpacing(QFont.AbsoluteSpacing, 0.6)
        painter.setFont(detail_font)
        painter.setPen(muted)
        painter.drawText(
            QRect(text_rect.left(), text_rect.top() + 24, text_rect.width(), 18),
            Qt.AlignLeft | Qt.AlignVCenter,
            detail.upper(),
        )
        painter.restore()


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

        layout = make_sheet(self, "Meeting history")
        layout.setSpacing(12)
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
        self.sessions.setItemDelegate(_SpineDelegate(self.sessions))
        self.sessions.setMouseTracking(True)
        self.sessions.setAccessibleName("Meetings")
        self.sessions.currentItemChanged.connect(self._selection_changed)
        splitter.addWidget(self.sessions)

        detail = QWidget()
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(14, 0, 0, 0)
        detail_layout.setSpacing(10)
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
        self.status_label.setObjectName("status")
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
