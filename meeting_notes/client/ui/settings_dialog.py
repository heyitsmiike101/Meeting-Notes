"""Settings: where recordings are saved, and which server transcribes them."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from meeting_notes import config as config_mod

MODELS = ["base.en", "small.en", "large-v3-turbo"]


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(520)
        self._config = config_mod.load_config()

        form = QFormLayout()

        # -- save folder (where the recordings and transcripts land) ----------
        self.save_dir_edit = QLineEdit(str(config_mod.save_dir(self._config)))
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._pick_folder)
        row = QHBoxLayout()
        row.addWidget(self.save_dir_edit, 1)
        row.addWidget(browse)
        form.addRow("Save recordings to", row)

        # -- transcription server --------------------------------------------
        server = config_mod.server_settings(self._config)
        self.url_edit = QLineEdit(server.get("url", ""))
        self.url_edit.setPlaceholderText("http://192.168.1.50:8000")
        form.addRow("Server URL", self.url_edit)

        self.token_edit = QLineEdit(server.get("token", ""))
        self.token_edit.setEchoMode(QLineEdit.Password)
        self.token_edit.setPlaceholderText("shared token (optional on a trusted LAN)")
        form.addRow("Server token", self.token_edit)

        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.addItems(MODELS)
        current_model = (self._config.get("transcribe") or {}).get("model", "base.en")
        self.model_combo.setCurrentText(current_model)
        form.addRow("Model", self.model_combo)

        self.live_check = QCheckBox("Show a live preview transcript while recording")
        self.live_check.setChecked(bool(server.get("live_preview", True)))
        form.addRow("", self.live_check)

        self.upload_check = QCheckBox("Upload finished recordings for transcription")
        self.upload_check.setChecked(bool(server.get("auto_upload", True)))
        form.addRow("", self.upload_check)

        note = QLabel(
            "The live preview is approximate and disposable. The transcript you keep "
            "is produced from the complete recording after the meeting, so a dropped "
            "connection can never lose audio."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #8b98a5; font-size: 11px;")

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(note)
        layout.addStretch(1)
        layout.addWidget(buttons)

    def _pick_folder(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Choose where recordings are saved", self.save_dir_edit.text()
        )
        if chosen:
            self.save_dir_edit.setText(chosen)

    def accept(self) -> None:  # noqa: D102
        data = dict(self._config)
        data["save_dir"] = self.save_dir_edit.text().strip() or str(config_mod.DEFAULT_SAVE_DIR)
        data["server"] = {
            "url": self.url_edit.text().strip().rstrip("/"),
            "token": self.token_edit.text().strip(),
            "live_preview": self.live_check.isChecked(),
            "auto_upload": self.upload_check.isChecked(),
        }
        transcribe = dict(data.get("transcribe") or {})
        transcribe["model"] = self.model_combo.currentText().strip() or "base.en"
        data["transcribe"] = transcribe
        config_mod.save_config(data)
        # Created now rather than at record time: a bad path should fail here,
        # in a dialog, not thirty seconds into a meeting.
        try:
            Path(data["save_dir"]).expanduser().mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        super().accept()
