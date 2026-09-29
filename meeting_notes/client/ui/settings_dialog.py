"""Settings: where recordings are saved, and which server transcribes them."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from meeting_notes import config as config_mod
from meeting_notes.client import authcheck, logsetup, paths
from meeting_notes.client.ui.icons import icon_size, make_icon
from meeting_notes.client.ui.theme import make_sheet

log = logging.getLogger("meeting_notes.client.ui.settings")


class _Bridge(QObject):
    done = Signal(object)


def _section(text: str, first: bool = False) -> QLabel:
    label = QLabel(text.upper())
    label.setObjectName("legend")
    label.setContentsMargins(0, 0 if first else 10, 0, 0)
    return label


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(560)
        self._config = config_mod.load_config()
        # Injectable so tests never touch the network.
        self.checker = authcheck.check_connection
        self._bridges: list = []
        self._save_anyway = False
        self._checking = False
        self.save_button = None

        layout = make_sheet(self, "Settings")

        def new_form() -> QFormLayout:
            form = QFormLayout()
            form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            form.setHorizontalSpacing(18)
            form.setVerticalSpacing(10)
            form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
            return form

        # One form for every section, so the label column and the checkbox
        # column line up all the way down the sheet.
        form = new_form()
        form.addRow(_section("Recordings", first=True))

        # -- save folder (where the recordings and transcripts land) ----------
        self.save_dir_edit = QLineEdit(str(config_mod.save_dir(self._config)))
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._pick_folder)
        row = QHBoxLayout()
        row.addWidget(self.save_dir_edit, 1)
        row.addWidget(browse)
        form.addRow("Save recordings to", row)
        self.folder_error = QLabel("")
        self.folder_error.setObjectName("connResult")
        self.folder_error.setProperty("state", "error")
        self.folder_error.setWordWrap(True)
        self.folder_error.setMinimumWidth(380)
        self.folder_error.setVisible(False)
        form.addRow("", self.folder_error)
        self.save_dir_edit.textChanged.connect(lambda _t: self._validate_folder())

        # -- transcription server --------------------------------------------
        form.addRow(_section("Server"))
        server = config_mod.server_settings(self._config)
        self.url_edit = QLineEdit(server.get("url", ""))
        self.url_edit.setPlaceholderText("http://192.168.1.50:8000")
        form.addRow("Server URL", self.url_edit)

        self.token_edit = QLineEdit(server.get("token", ""))
        self.token_edit.setEchoMode(QLineEdit.Password)
        self.token_edit.setPlaceholderText("shared token (optional on a trusted LAN)")
        token_row = QHBoxLayout()
        token_row.addWidget(self.token_edit, 1)
        self.test_button = QPushButton("Test connection")
        self.test_button.clicked.connect(self.test_connection)
        token_row.addWidget(self.test_button)
        form.addRow("Server token", token_row)
        self.result_label = QLabel("")
        self.result_label.setObjectName("connResult")
        self.result_label.setWordWrap(True)
        self.result_label.setAccessibleName("Connection test result")
        self.result_icon = QLabel()
        self.result_icon.setFixedSize(18, 18)
        self.result_box = QWidget()
        result_row = QHBoxLayout(self.result_box)
        result_row.setContentsMargins(0, 0, 0, 0)
        result_row.setSpacing(8)
        result_row.addWidget(self.result_icon, 0, Qt.AlignTop)
        result_row.addWidget(self.result_label, 1)
        self.result_box.setVisible(False)
        form.addRow("", self.result_box)
        self.url_edit.textChanged.connect(self._clear_result)
        self.token_edit.textChanged.connect(self._clear_result)

        self.live_check = QCheckBox("Show the server's live preview while recording")
        self.live_check.setChecked(bool(server.get("live_preview", True)))
        form.addRow("", self.live_check)

        self.upload_check = QCheckBox("Upload finished recordings for transcription")
        self.upload_check.setChecked(bool(server.get("auto_upload", True)))
        form.addRow("", self.upload_check)

        # -- client updates ---------------------------------------------------
        form.addRow(_section("Updates"))
        self.update_check = QCheckBox("Check the server for client updates")
        self.update_check.setChecked(bool(server.get("check_updates", True)))
        form.addRow("", self.update_check)

        self.auto_update_check = QCheckBox("Install client updates automatically when idle")
        self.auto_update_check.setChecked(bool(server.get("auto_update", False)))
        form.addRow("", self.auto_update_check)

        form.addRow(_section("Meeting detection"))
        detection = config_mod.meeting_detection_settings(self._config)
        self._detection = detection
        self.detect_check = QCheckBox("Offer to record when a Teams, Zoom or Google Meet call starts")
        self.detect_check.setChecked(bool(detection["enabled"]))
        form.addRow("", self.detect_check)

        self.auto_stop_check = QCheckBox("Stop prompted recordings automatically when the call ends")
        self.auto_stop_check.setChecked(bool(detection["auto_stop"]))
        form.addRow("", self.auto_stop_check)
        layout.addLayout(form)

        note = QLabel(
            "Transcription runs on the server only. The live preview is approximate "
            "and disposable; the transcript you keep is produced by the server from "
            "the complete recording after the meeting. A dropped connection can never "
            "lose the local audio."
        )
        note.setWordWrap(True)
        note.setObjectName("subtle")

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self.save_button = buttons.button(QDialogButtonBox.Save)
        buttons.accepted.connect(self._save_clicked)
        buttons.rejected.connect(self.reject)

        layout.addWidget(note)
        layout.addStretch(1)
        layout.addWidget(buttons)

    def _pick_folder(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Choose where recordings are saved", self.save_dir_edit.text()
        )
        if chosen:
            self.save_dir_edit.setText(chosen)

    # -- folder safety ---------------------------------------------------------

    def _validate_folder(self) -> bool:
        """Reject a recordings folder an installer would wipe. Inline, no modal."""
        text = self.save_dir_edit.text().strip()
        error = paths.validate_save_dir(text) if text else None
        self.folder_error.setText(error or "")
        self.folder_error.setVisible(bool(error))
        return error is None

    # -- connection test ---------------------------------------------------------

    def _clear_result(self, *_args) -> None:
        self._save_anyway = False
        if self.save_button is not None:
            self.save_button.setText("Save")
        if not self._checking:
            self.result_box.setVisible(False)

    def _show_result(self, result, suffix: str = "") -> None:
        ok = result.status == authcheck.OK
        self.result_label.setProperty("state", "ok" if ok else "error")
        self.result_label.style().unpolish(self.result_label)
        self.result_label.style().polish(self.result_label)
        self.result_label.setText(result.message() + suffix)
        glyph, colour = ("check", "#24211b") if ok else ("alert", "#a52c23")
        self.result_icon.setPixmap(make_icon(glyph, colour, colour, 18).pixmap(18, 18))
        self.result_box.setVisible(True)

    def _start_check(self, done) -> None:
        url = self.url_edit.text().strip().rstrip("/")
        token = self.token_edit.text().strip()
        self._checking = True
        self.test_button.setEnabled(False)
        self.result_label.setProperty("state", "ok")
        self.result_label.setText("Checking...")
        self.result_icon.clear()
        self.result_box.setVisible(True)
        bridge = _Bridge()
        self._bridges.append(bridge)

        def deliver(result: object) -> None:
            if bridge in self._bridges:
                self._bridges.remove(bridge)
            self._checking = False
            self.test_button.setEnabled(True)
            done(result)

        bridge.done.connect(deliver)

        def runner() -> None:
            try:
                result = self.checker(url, token)
            except Exception as exc:  # noqa: BLE001
                result = authcheck.CheckResult(authcheck.ERROR, url, str(exc))
            bridge.done.emit(result)

        threading.Thread(target=runner, daemon=True, name="settings-check").start()

    def test_connection(self) -> None:
        """Run the check in the background and show the outcome under the token."""
        self._start_check(self._test_finished)

    def _test_finished(self, result) -> None:
        self._show_result(result)

    def _save_clicked(self) -> None:
        if not self._validate_folder():
            return
        url = self.url_edit.text().strip()
        if not url or self._save_anyway or self._checking:
            self.accept()
            return
        self.save_button.setEnabled(False)
        self._start_check(self._save_checked)

    def _save_checked(self, result) -> None:
        self.save_button.setEnabled(True)
        if result.status == authcheck.REJECTED:
            # Inline rather than a modal: say it, and let a second press keep it.
            self._show_result(result, ". Press Save again to keep these settings anyway.")
            self._save_anyway = True
            self.save_button.setText("Save anyway")
            return
        self._show_result(result)
        self.accept()

    def accept(self) -> None:  # noqa: D102
        if not self._validate_folder():
            return
        data = dict(self._config)
        data["save_dir"] = self.save_dir_edit.text().strip() or str(config_mod.DEFAULT_SAVE_DIR)
        data["server"] = {
            "url": self.url_edit.text().strip().rstrip("/"),
            "token": self.token_edit.text().strip(),
            "live_preview": self.live_check.isChecked(),
            "auto_upload": self.upload_check.isChecked(),
            "check_updates": self.update_check.isChecked(),
            "auto_update": self.auto_update_check.isChecked(),
        }
        data["meeting_detection"] = {
            "enabled": self.detect_check.isChecked(),
            "auto_stop": self.auto_stop_check.isChecked(),
            "end_grace_sec": self._detection["end_grace_sec"],
        }
        config_mod.save_config(data)
        logsetup.register_secret(data["server"]["token"])
        log.info("settings saved; changed: %s", ", ".join(_changed_keys(self._config, data)) or "nothing")
        # Created now rather than at record time: a bad path should fail here,
        # in a dialog, not thirty seconds into a meeting.
        try:
            Path(data["save_dir"]).expanduser().mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        super().accept()


def _changed_keys(old: dict, new: dict) -> list:
    """Dotted names of settings that differ (never their values: some are secrets)."""
    changed = []
    for key in sorted(set(old) | set(new)):
        a, b = old.get(key), new.get(key)
        if isinstance(a, dict) or isinstance(b, dict):
            a, b = a or {}, b or {}
            for sub in sorted(set(a) | set(b)):
                if a.get(sub) != b.get(sub):
                    changed.append(f"{key}.{sub}")
        elif a != b:
            changed.append(key)
    return changed
