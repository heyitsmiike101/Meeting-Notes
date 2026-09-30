"""Settings: where recordings are saved, and which server transcribes them."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QSizePolicy,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from meeting_notes import config as config_mod
from meeting_notes.client import authcheck, logsetup, paths, retention
from meeting_notes.client.queue import SessionQueue
from meeting_notes.client.recordings import format_size
from meeting_notes.client.ui.icons import icon_size, make_icon
from meeting_notes.client.ui import theme
from meeting_notes.client.ui.theme import make_sheet

log = logging.getLogger("meeting_notes.client.ui.settings")


class _Bridge(QObject):
    done = Signal(object)


def _section(text: str, first: bool = False) -> QLabel:
    label = QLabel(text)
    label.setObjectName("section")
    label.setContentsMargins(0, 0 if first else 12, 0, 2)
    return label


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(640)
        self._config = config_mod.load_config()
        # Injectable so tests never touch the network.
        self.checker = authcheck.check_connection
        self._bridges: list = []
        self._save_anyway = False
        self._checking = False
        self.save_button = None
        # Injectable so tests never touch the disk scan, the network or the
        # Recycle Bin.
        self.folder_stats = retention.folder_stats
        self.cleanup_planner = retention.plan_with_server
        self.cleanup_executor = retention.execute_plan
        self._cleanup_busy = False

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

        # -- local recordings: optional clean-up of old, safely uploaded copies ---
        form.addRow(_section("Local recordings"))
        self.retention_combo = QComboBox()
        self.retention_combo.setAccessibleName("Keep recordings on this computer")
        for days, text in ((0, "Forever"), (7, "7 days"), (30, "30 days"), (90, "90 days")):
            self.retention_combo.addItem(text, days)
        self.retention_combo.setCurrentIndex(
            max(0, self.retention_combo.findData(config_mod.local_retention_days(self._config)))
        )
        form.addRow("Keep recordings on this computer", self.retention_combo)
        retention_note = QLabel(
            f"Only after the server has the finished transcript. Removed recordings go to the "
            f"{retention.trash_name()}; anything still waiting to upload is kept."
        )
        retention_note.setObjectName("subtle")
        retention_note.setWordWrap(True)
        # Wrapped labels in a QFormLayout are laid out at a height computed
        # for the wrong width and get clipped; give this one a definite width
        # and the height that width actually needs.
        retention_note.setFixedWidth(340)
        retention_note.setFixedHeight(
            retention_note.heightForWidth(340) + retention_note.fontMetrics().descent()
        )
        form.addRow("", retention_note)
        self.local_stats_label = QLabel("Checking the folder...")
        self.local_stats_label.setObjectName("subtle")
        self.cleanup_button = QPushButton("Clean up now")
        self.cleanup_button.clicked.connect(self._cleanup_clicked)
        stats_row = QHBoxLayout()
        stats_row.addWidget(self.local_stats_label, 1)
        stats_row.addWidget(self.cleanup_button)
        form.addRow("In this folder", stats_row)
        self.cleanup_result = QLabel("")
        self.cleanup_result.setObjectName("connResult")
        self.cleanup_result.setWordWrap(True)
        self.cleanup_result.setVisible(False)
        form.addRow("", self.cleanup_result)
        self.retention_combo.currentIndexChanged.connect(lambda _i: self._sync_cleanup_button())
        self._sync_cleanup_button()
        self.save_dir_edit.editingFinished.connect(self._refresh_local_stats)
        self._refresh_local_stats()

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
        result_row.addWidget(self.result_icon, 0, Qt.AlignVCenter)
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

        self.remote_check = QCheckBox("Allow control from the server")
        self.remote_check.setChecked(config_mod.remote_control_allowed(self._config))
        form.addRow("", self.remote_check)
        remote_note = QLabel(
            "Lets the Recorders page on your server start and stop recordings, mute and more "
            "while this app is open. The app always shows a notice when it does."
        )
        remote_note.setObjectName("subtle")
        remote_note.setWordWrap(True)
        # Same fixed-width trick as the retention note: wrapped labels in a form get clipped otherwise.
        remote_note.setFixedWidth(350)
        remote_note.setFixedHeight(remote_note.heightForWidth(350) + remote_note.fontMetrics().descent())
        form.addRow("", remote_note)

        # -- client updates ---------------------------------------------------
        form.addRow(_section("Updates"))
        self.update_check = QCheckBox("Check the server for client updates")
        self.update_check.setChecked(bool(server.get("check_updates", True)))
        form.addRow("", self.update_check)


        form.addRow(_section("Meeting detection"))
        detection = config_mod.meeting_detection_settings(self._config)
        self._detection = detection
        self.detect_check = QCheckBox("Offer to record Teams, Zoom and Google Meet calls")
        self.detect_check.setChecked(bool(detection["enabled"]))
        form.addRow("", self.detect_check)

        self.auto_stop_check = QCheckBox("Stop prompted recordings when the call ends")
        self.auto_stop_check.setChecked(bool(detection["auto_stop"]))
        form.addRow("", self.auto_stop_check)

        self.suggest_stop_check = QCheckBox("Suggest stopping when a meeting seems over")
        self.suggest_stop_check.setChecked(bool(detection["suggest_stop"]))
        self.suggest_stop_check.setToolTip(
            "Asks whether to stop when the call ends or when there has been no audio for five minutes. "
            "It never stops a recording by itself."
        )
        form.addRow("", self.suggest_stop_check)

        # -- appearance: System / Light / Dark, applied as soon as it is saved --
        form.addRow(_section("Appearance"))
        self.appearance_combo = QComboBox()
        self.appearance_combo.setAccessibleName("Appearance")
        for value, text in (("system", "System"), ("light", "Light"), ("dark", "Dark")):
            self.appearance_combo.addItem(text, value)
        self._appearance = config_mod.appearance_setting(self._config)
        self.appearance_combo.setCurrentIndex(max(0, self.appearance_combo.findData(self._appearance)))
        form.addRow("Theme", self.appearance_combo)
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
            self._refresh_local_stats()

    # -- local recordings clean-up -----------------------------------------------

    def _run_bg(self, work, done) -> None:
        """Run ``work`` on a thread and hand its result (or exception) to ``done``."""
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

        threading.Thread(target=runner, daemon=True, name="settings-bg").start()

    def _folder(self) -> Path:
        text = self.save_dir_edit.text().strip()
        return Path(text).expanduser() if text else config_mod.save_dir({})

    def _active_dir(self):
        controller = getattr(self.parent(), "controller", None)
        if controller is not None and getattr(controller, "state", "idle") != "idle":
            return getattr(controller, "session_dir", None)
        return None

    def _sync_cleanup_button(self) -> None:
        chosen = bool(self.retention_combo.currentData())
        self.cleanup_button.setEnabled(not self._cleanup_busy and chosen)
        self.cleanup_button.setToolTip(
            "Apply the keep period now, after a check with the server"
            if chosen
            else "Choose a keep period first; Forever never removes anything"
        )

    def _refresh_local_stats(self) -> None:
        folder = self._folder()
        self.local_stats_label.setText("Checking the folder...")

        def done(result) -> None:
            if isinstance(result, Exception):
                self.local_stats_label.setText("Could not read the folder")
                return
            count = result["count"]
            self.local_stats_label.setText(
                f"{count} recording{'s' if count != 1 else ''}, {format_size(result['bytes'])}"
            )

        self._run_bg(lambda: self.folder_stats(folder), done)

    def _show_cleanup(self, text: str, state: str = "ok") -> None:
        self.cleanup_result.setProperty("state", state)
        self.cleanup_result.style().unpolish(self.cleanup_result)
        self.cleanup_result.style().polish(self.cleanup_result)
        self.cleanup_result.setText(text)
        self.cleanup_result.setVisible(True)

    def _cleanup_clicked(self) -> None:
        days = int(self.retention_combo.currentData() or 0)
        if not days or self._cleanup_busy:
            return
        url = self.url_edit.text().strip().rstrip("/")
        token = self.token_edit.text().strip()
        if not url:
            self._show_cleanup(
                "Set the server URL first: recordings are only removed once the server has them.", "error"
            )
            return
        folder = self._folder()
        active = self._active_dir()
        self._cleanup_busy = True
        self._sync_cleanup_button()
        self._show_cleanup("Checking with the server...", "ok")

        def done(plan) -> None:
            self._cleanup_busy = False
            self._sync_cleanup_button()
            if isinstance(plan, Exception):
                self._show_cleanup(f"Could not check the recordings: {plan}", "error")
                return
            doomed = [d for d in plan if d.delete]
            if not doomed:
                kept = retention.summarize_kept(plan)
                self._show_cleanup(
                    "Nothing to clean up right now." + (f" Kept: {kept}." if kept else ""), "ok"
                )
                return
            size = sum(d.size_bytes for d in doomed)
            if not self._confirm_cleanup(len(doomed), size, days):
                self._show_cleanup("Clean up cancelled. Nothing was removed.", "ok")
                return
            self._execute_cleanup(plan, folder, active)

        self._run_bg(lambda: self.cleanup_planner(folder, days, url, token, active_dir=active), done)

    def _confirm_cleanup(self, count: int, size: int, days: int) -> bool:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("Clean up recordings")
        box.setText(
            f"Move {count} recording{'s' if count != 1 else ''} ({format_size(size)}) to the {retention.trash_name()}?"
        )
        box.setInformativeText(
            f"Each is older than {days} days and the server has its finished transcript. "
            "Anything still uploading is kept."
        )
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.setDefaultButton(QMessageBox.No)
        return box.exec() == QMessageBox.Yes

    def _execute_cleanup(self, plan, folder: Path, active) -> None:
        self._cleanup_busy = True
        self._sync_cleanup_button()
        self._show_cleanup("Removing recordings...", "ok")
        queue = SessionQueue.for_save_dir(folder)

        def done(report) -> None:
            self._cleanup_busy = False
            self._sync_cleanup_button()
            if isinstance(report, Exception):
                self._show_cleanup(f"Clean up failed: {report}", "error")
            else:
                text = report.summary() + "."
                if report.failed:
                    text += f" {len(report.failed)} could not be removed."
                self._show_cleanup(text, "error" if report.failed else "ok")
            self._refresh_local_stats()

        self._run_bg(lambda: self.cleanup_executor(plan, queue=queue, active_dir=active), done)

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
        tokens = theme.tokens()
        glyph, colour = ("check-circle", tokens["ok_text"]) if ok else ("alert-circle", tokens["danger_text"])
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
        }
        data["meeting_detection"] = {
            "enabled": self.detect_check.isChecked(),
            "auto_stop": self.auto_stop_check.isChecked(),
            "suggest_stop": self.suggest_stop_check.isChecked(),
            "end_grace_sec": self._detection["end_grace_sec"],
        }
        data["remote_control_allowed"] = self.remote_check.isChecked()
        data["appearance"] = self.appearance_combo.currentData() or "system"
        data["local_retention_days"] = int(self.retention_combo.currentData() or 0)
        config_mod.save_config(data)
        # Live: restyle the whole app now, without a restart.
        try:
            theme.apply_appearance(data["appearance"])
        except Exception:  # noqa: BLE001 - a theme failure must never lose the save
            log.exception("could not apply the appearance setting")
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
