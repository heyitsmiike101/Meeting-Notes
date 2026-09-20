"""The recorder window: start, stop, and proof that it is working."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from meeting_notes import config as config_mod
from meeting_notes.client.controller import IDLE, RECORDING, RecordingController
from meeting_notes.client.ui.settings_dialog import SettingsDialog
from meeting_notes.client.ui.theme import APP_STYLE
from meeting_notes.client.ui.waveform import WaveformWidget



def _hms(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


class MainWindow(QWidget):
    def __init__(self, controller: RecordingController = None):
        super().__init__()
        self.controller = controller or RecordingController()
        self.setObjectName("root")
        self.setWindowTitle("Meeting Notes")
        self.setMinimumSize(720, 560)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        # -- header -----------------------------------------------------------
        header = QHBoxLayout()
        title = QLabel("Meeting Notes")
        title.setFont(QFont(self.font().family(), 15, QFont.DemiBold))
        header.addWidget(title)
        header.addStretch(1)
        self.folder_button = QPushButton("Open folder")
        self.folder_button.clicked.connect(self._open_folder)
        self.settings_button = QPushButton("Settings")
        self.settings_button.clicked.connect(self._open_settings)
        header.addWidget(self.folder_button)
        header.addWidget(self.settings_button)
        layout.addLayout(header)

        # -- waveform ---------------------------------------------------------
        self.waveform = WaveformWidget()
        layout.addWidget(self.waveform, 3)

        # -- clock + device status -------------------------------------------
        status = QHBoxLayout()
        self.clock = QLabel("00:00:00")
        self.clock.setObjectName("clock")
        status.addWidget(self.clock)
        status.addStretch(1)
        self.devices_label = QLabel("")
        self.devices_label.setObjectName("subtle")
        self.devices_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        status.addWidget(self.devices_label)
        layout.addLayout(status)

        # -- controls ---------------------------------------------------------
        controls = QHBoxLayout()
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Meeting name (optional)")
        controls.addWidget(self.name_edit, 1)
        self.record_button = QPushButton("Start recording")
        self.record_button.setObjectName("record")
        self.record_button.setMinimumWidth(170)
        self.record_button.clicked.connect(self._toggle)
        controls.addWidget(self.record_button)
        layout.addLayout(controls)

        # -- live preview ------------------------------------------------------
        preview_label = QLabel("Live preview")
        preview_label.setObjectName("subtle")
        layout.addWidget(preview_label)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setPlaceholderText(
            "A rough live transcript appears here while recording. The transcript you "
            "keep is made from the full recording after the meeting."
        )
        layout.addWidget(self.preview, 2)

        # -- footer -------------------------------------------------------------
        self.status_label = QLabel("")
        self.status_label.setObjectName("subtle")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self._seen_partials = 0
        # A single polled timer instead of cross-thread signals: the capture and
        # network threads simply publish state, and the UI samples it. Nothing
        # they do can block or crash the event loop.
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(33)
        self._refresh_devices()
        self._update_status()

    @staticmethod
    def _restyle(widget) -> None:
        """Qt does not re-evaluate #id selectors when objectName changes."""
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    # -- actions --------------------------------------------------------------

    def _toggle(self) -> None:
        if self.controller.state == IDLE:
            self._start()
        elif self.controller.state == RECORDING:
            self._stop()

    def _start(self) -> None:
        self.waveform.clear()
        self.preview.clear()
        self._seen_partials = 0
        session_dir = self.controller.start(self.name_edit.text().strip())
        if session_dir is None:
            self.status_label.setText(f"Could not start: {self.controller.error}")
            return
        self.waveform.set_recording(True)
        self.record_button.setText("Stop recording")
        self.record_button.setObjectName("recording")
        self._restyle(self.record_button)

    def _stop(self) -> None:
        self.record_button.setEnabled(False)
        self.record_button.setText("Finishing...")
        meta = self.controller.stop()
        self.waveform.set_recording(False)
        self.record_button.setEnabled(True)
        self.record_button.setText("Start recording")
        self.record_button.setObjectName("record")
        self._restyle(self.record_button)
        if meta:
            where = self.controller.session_dir
            self.status_label.setText(
                f"Saved {_hms(meta.get('duration_sec') or 0)} to {where}. "
                "Queued for transcription."
            )

    def _open_settings(self) -> None:
        if SettingsDialog(self).exec():
            self._refresh_devices()
            self._update_status()

    def _open_folder(self) -> None:
        target = self.controller.session_dir or config_mod.save_dir()
        Path(target).expanduser().mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    # -- polling --------------------------------------------------------------

    def _refresh_devices(self) -> None:
        found = self.controller.probe_devices()
        mic = found.get("mic", "?")
        system = found.get("system", "?")
        self.devices_label.setText(f"You: {mic}\nThem: {system}")

    def _tick(self) -> None:
        if self.controller.state == RECORDING:
            levels = self.controller.levels()
            self.waveform.push(levels)
            for track, degraded in self.controller.degraded().items():
                self.waveform.set_track_active(track, not degraded)
            self.clock.setText(_hms(self.controller.elapsed))
            self._drain_partials()
        self._update_status()

    def _drain_partials(self) -> None:
        partials = self.controller.partials()
        for item in partials[self._seen_partials :]:
            label = "You" if item.get("track") == "mic" else "Them"
            self.preview.appendPlainText(f"{label}: {item.get('text', '')}")
        self._seen_partials = len(partials)

    def _update_status(self) -> None:
        if self.controller.state == RECORDING and self.controller.error:
            self.status_label.setText(f"Recording, but: {self.controller.error}")
            return
        if self.controller.state == RECORDING:
            stream = self.controller.stream_state()
            note = {
                "connected": "live preview connected",
                "connecting": "connecting to server...",
                "disconnected": "server unreachable; recording locally and will upload later",
                "off": "live preview off",
            }.get(stream, stream)
            self.status_label.setText(f"Recording. {note}")
        elif self.controller.state == IDLE and not self.status_label.text():
            server = config_mod.server_settings()
            where = server.get("url") or "not configured"
            self.status_label.setText(f"Ready. Server: {where}")
