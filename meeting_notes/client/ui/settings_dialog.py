"""Settings: where recordings are saved, and which server transcribes them."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QSizePolicy,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from meeting_notes import __version__
from meeting_notes import config as config_mod
from meeting_notes.client import authcheck, logsetup, paths, retention
from meeting_notes.client.queue import SessionQueue
from meeting_notes.client.recordings import format_size
from meeting_notes.client.ui.icons import icon_size, make_icon
from meeting_notes.client.ui.logs_dialog import LogsPanel
from meeting_notes.client.ui import theme
from meeting_notes.client.ui.theme import make_sheet

log = logging.getLogger("meeting_notes.client.ui.settings")


# Widths for result labels whose text changes (see _fit_wrapped); they sit in the field column.
CLEANUP_RESULT_WIDTH = 380
RESULT_LABEL_WIDTH = 354  # beside the 18px status icon and its 8px gap
_QWIDGETSIZE_MAX = 16777215


class _Bridge(QObject):
    done = Signal(object)


def _fit_wrapped(label: QLabel, width: int) -> None:
    """Size a word-wrapped label to the height its current text needs at ``width``.

    QFormLayout lays wrapped labels out at a height computed for the wrong width, which
    clips the last line; labels whose text changes must be re-measured after each change.
    """
    label.ensurePolished()  # a label on a page that is not showing yet must be measured with its styled font
    # heightForWidth() never reports less than the label's current fixed height, so release the old height
    # first; otherwise every re-fit would grow the label by the descent.
    label.setMinimumHeight(0)
    label.setMaximumHeight(_QWIDGETSIZE_MAX)
    label.setFixedWidth(width)
    label.setFixedHeight(label.heightForWidth(width) + label.fontMetrics().descent())


def _section(text: str, first: bool = False) -> QLabel:
    label = QLabel(text)
    label.setObjectName("section")
    label.setContentsMargins(0, 0 if first else 12, 0, 2)
    return label


# (key, sidebar title). The order is the order of the sidebar.
PAGES = (
    ("general", "General"),
    ("audio", "Audio"),
    ("recordings", "Recordings"),
    ("server", "Server"),
    ("remote", "Remote control"),
    ("logs", "Logs"),
    ("about", "About"),
)
DEFAULT_PAGE = "general"
NOTE_WIDTH = 400  # wrapped notes in the field column (see _fit_wrapped)


def _note(text: str) -> QLabel:
    """A muted, word-wrapped note that is not clipped inside a form."""
    label = QLabel(text)
    label.setObjectName("subtle")
    label.setWordWrap(True)
    _fit_wrapped(label, NOTE_WIDTH)
    return label


class SettingsDialog(QDialog):
    """Settings, as a sidebar of pages (General, Audio, Recordings, Server, Remote control, Logs, About).

    Every widget keeps its attribute name whichever page it sits on; Save and Cancel act on all pages.
    """

    def __init__(
        self,
        parent=None,
        page: Optional[str] = None,
        *,
        focus_password: bool = False,
        first_run: bool = False,
        note_types: Optional[List[Dict[str, str]]] = None,
        server_default_note_type: str = "",
    ):
        """``focus_password`` opens the Server page with the cursor in the password field;
        ``first_run`` also spells out where the password comes from (the first-start prompt).
        ``note_types`` is the server's note types as ``[{id, name}]`` (the window fetched them; empty when it
        could not) and ``server_default_note_type`` the id the server uses by default."""
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumSize(720, 520)
        self.resize(820, 580)
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
        self._page_keys = [key for key, _title in PAGES]
        self._page_widgets: dict = {}

        layout = make_sheet(self, "Settings")

        body = QHBoxLayout()
        body.setSpacing(18)
        self.nav = QListWidget()
        self.nav.setObjectName("settingsNav")
        self.nav.setAccessibleName("Settings pages")
        self.nav.setFixedWidth(170)
        for key, title in PAGES:
            item = QListWidgetItem(title)
            item.setData(Qt.UserRole, key)
            self.nav.addItem(item)
        body.addWidget(self.nav)
        self.pages = QStackedWidget()
        body.addWidget(self.pages, 1)
        layout.addLayout(body, 1)

        def new_form() -> QFormLayout:
            form = QFormLayout()
            form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            form.setHorizontalSpacing(18)
            form.setVerticalSpacing(10)
            form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
            return form

        def new_page(key: str):
            """A scrollable page with a heading; returns the form to fill."""
            title = dict(PAGES)[key]
            content = QWidget()
            content.setObjectName("settingsPage")
            column = QVBoxLayout(content)
            column.setContentsMargins(0, 0, 14, 0)
            column.setSpacing(12)
            heading = QLabel(title)
            heading.setObjectName("heading")
            column.addWidget(heading)
            form = new_form()
            column.addLayout(form)
            column.addStretch(1)
            scroll = QScrollArea()
            scroll.setObjectName("settingsScroll")
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.NoFrame)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            scroll.setWidget(content)
            self.pages.addWidget(scroll)
            self._page_widgets[key] = scroll
            return form

        # ---- General: appearance, meeting detection -------------------------------
        form = new_page("general")
        form.addRow(_section("Appearance", first=True))
        # System / Light / Dark, applied as soon as it is saved.
        self.appearance_combo = QComboBox()
        self.appearance_combo.setAccessibleName("Appearance")
        for value, text in (("system", "System"), ("light", "Light"), ("dark", "Dark")):
            self.appearance_combo.addItem(text, value)
        self._appearance = config_mod.appearance_setting(self._config)
        self.appearance_combo.setCurrentIndex(max(0, self.appearance_combo.findData(self._appearance)))
        form.addRow("Theme", self.appearance_combo)

        form.addRow(_section("Meeting detection"))
        detection = config_mod.meeting_detection_settings(self._config)
        self._detection = detection
        self.detect_check = QCheckBox("Offer to record Teams, Zoom and Google Meet calls")
        self.detect_check.setChecked(bool(detection["enabled"]))
        form.addRow("", self.detect_check)

        # Auto record: only meaningful while detection is on; its "Auto end" choice only shows while it is on.
        self.auto_record_check = QCheckBox("Start recording automatically when a call starts")
        self.auto_record_check.setChecked(bool(detection["auto_record"]))
        form.addRow("", self.auto_record_check)
        self.auto_end_combo = QComboBox()
        self.auto_end_combo.setAccessibleName("Auto end")
        for value, text in (
            ("call", "When the call ends"),
            ("hour", "On the hour"),
            ("silence", "After 30 seconds of silence"),
            ("manual", "Manual only"),
        ):
            self.auto_end_combo.addItem(text, value)
        self.auto_end_combo.setCurrentIndex(max(0, self.auto_end_combo.findData(detection["auto_end"])))
        form.addRow("Auto end", self.auto_end_combo)
        self.auto_end_note = _note(
            "When the call ends stops once the call is over and its audio has gone quiet. On the hour stops at the "
            "end of the hour the call is in (a call joined in the last 10 minutes before the hour runs to the "
            "next one). You can turn auto end off for any recording."
        )
        form.addRow("", self.auto_end_note)
        self._meeting_form = form

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
        self.detect_check.toggled.connect(lambda _on: self._sync_auto_record())
        self.auto_record_check.toggled.connect(lambda _on: self._sync_auto_record())
        self._sync_auto_record()

        form.addRow(_section("Notes"))
        self.default_note_type_combo = QComboBox()
        self.default_note_type_combo.setAccessibleName("Default note type")
        self._fill_note_types(note_types or [], server_default_note_type, config_mod.default_note_type_setting(self._config))
        form.addRow("Default note type", self.default_note_type_combo)
        if not note_types:
            form.addRow("", _note("Connect to the server to choose a note type."))
        form.addRow("", _note(
            "Meetings recorded here get notes of this type automatically, saved where that note type sends them "
            "(for example its Notion page). You can pick another type for a single meeting next to the meeting name."
        ))

        # ---- Audio: live input levels before recording (nothing is recorded) ------
        form = new_page("audio")
        self.levels_check = QCheckBox("Show audio levels before recording")
        self.levels_check.setChecked(config_mod.idle_levels_enabled(self._config))
        form.addRow("", self.levels_check)
        form.addRow("", _note(
            "Shows live input from your microphone and speakers while the window is open, greyed out "
            "until you record. Nothing is saved. Turn it off to keep your microphone closed until you press "
            "Start recording."
        ))
        form.addRow("", _note(
            "The main window shows which microphone and speakers are in use; its menu has Refresh audio devices to re-scan them."
        ))

        # ---- Recordings: save folder, local clean-up ---------------------------------
        form = new_page("recordings")
        form.addRow(_section("Save folder", first=True))
        self.save_dir_edit = QLineEdit(str(config_mod.save_dir(self._config)))
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._pick_folder)
        row = QHBoxLayout()
        row.setSpacing(8)
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

        form.addRow(_section("Local recordings"))
        self.retention_combo = QComboBox()
        self.retention_combo.setAccessibleName("Keep recordings on this computer")
        for days, text in ((0, "Forever"), (7, "7 days"), (30, "30 days"), (90, "90 days")):
            self.retention_combo.addItem(text, days)
        self.retention_combo.setCurrentIndex(
            max(0, self.retention_combo.findData(config_mod.local_retention_days(self._config)))
        )
        form.addRow("Keep recordings", self.retention_combo)
        form.addRow("", _note(
            f"Only after the server has the finished transcript. Removed recordings go to the "
            f"{retention.trash_name()}; anything still waiting to upload is kept."
        ))
        self.local_stats_label = QLabel("Checking the folder...")
        self.local_stats_label.setObjectName("subtle")
        self.cleanup_button = QPushButton("Clean up now")
        self.cleanup_button.clicked.connect(self._cleanup_clicked)
        stats_row = QHBoxLayout()
        stats_row.setSpacing(8)
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

        # ---- Server: transcription server, uploads, updates ------------------------------
        form = new_page("server")
        server = config_mod.server_settings(self._config)
        self.url_edit = QLineEdit(server.get("url", ""))
        self.url_edit.setPlaceholderText("http://192.168.1.50:8000")
        form.addRow("Server URL", self.url_edit)

        self.token_edit = QLineEdit(server.get("token", ""))
        self.token_edit.setEchoMode(QLineEdit.Password)
        self.token_edit.setPlaceholderText("Server password (leave empty if the server has none)")
        self.token_edit.setAccessibleName("Server password")
        token_row = QHBoxLayout()
        token_row.setSpacing(8)
        token_row.addWidget(self.token_edit, 1)
        self.test_button = QPushButton("Test connection")
        self.test_button.clicked.connect(self.test_connection)
        token_row.addWidget(self.test_button)
        form.addRow("Server password", token_row)
        server_url = (server.get("url") or "").strip()
        if first_run and server_url:
            hint = f"Paste the same password you use to sign in at {server_url}."
        else:
            hint = "The same password you use to sign in on the server's web page."
        self.password_hint = _note(hint)
        form.addRow("", self.password_hint)
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

        form.addRow(_section("Uploads"))
        self.live_check = QCheckBox("Show the server's live preview while recording")
        self.live_check.setChecked(bool(server.get("live_preview", True)))
        form.addRow("", self.live_check)

        self.upload_check = QCheckBox("Upload finished recordings for transcription")
        self.upload_check.setChecked(bool(server.get("auto_upload", True)))
        form.addRow("", self.upload_check)

        form.addRow(_section("Updates"))
        self.update_check = QCheckBox("Check the server for client updates")
        self.update_check.setChecked(bool(server.get("check_updates", True)))
        form.addRow("", self.update_check)

        form.addRow("", _note(
            "Transcription runs on the server only. The live preview is approximate "
            "and disposable; the transcript you keep is produced by the server from "
            "the complete recording after the meeting. A dropped connection can never "
            "lose the local audio."
        ))

        # ---- Remote control ----------------------------------------------------------
        form = new_page("remote")
        self.remote_check = QCheckBox("Allow control from the server")
        self.remote_check.setChecked(config_mod.remote_control_allowed(self._config))
        form.addRow("", self.remote_check)
        form.addRow("", _note(
            "Lets the Recorders page on your server start and stop recordings, mute and more "
            "while this app is open. The app always shows a notice when it does."
        ))

        # ---- Logs: the former Logs window ------------------------------------------------
        logs_page = QWidget()
        logs_column = QVBoxLayout(logs_page)
        logs_column.setContentsMargins(0, 0, 0, 0)
        logs_column.setSpacing(12)
        logs_heading = QLabel("Logs")
        logs_heading.setObjectName("heading")
        logs_column.addWidget(logs_heading)
        self.logs_panel = LogsPanel()
        logs_column.addWidget(self.logs_panel, 1)
        self.pages.addWidget(logs_page)
        self._page_widgets["logs"] = logs_page

        # ---- About ---------------------------------------------------------------------
        form = new_page("about")
        self.about_labels: dict = {}
        for key, label, value in (
            ("version", "Version", __version__),
            ("save_dir", "Recordings folder", str(config_mod.save_dir(self._config))),
            ("config", "Settings file", str(config_mod.config_path())),
            ("logs", "Logs folder", str(logsetup.log_dir())),
        ):
            text = QLabel(value)
            text.setWordWrap(True)
            text.setTextInteractionFlags(Qt.TextSelectableByMouse)
            _fit_wrapped(text, NOTE_WIDTH)
            self.about_labels[key] = text
            form.addRow(label, text)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self.save_button = buttons.button(QDialogButtonBox.Save)
        buttons.accepted.connect(self._save_clicked)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.nav.currentRowChanged.connect(self._on_nav_row)
        if focus_password:
            page = "server"
        self.show_page(page if page in self._page_keys else self._remembered_page())
        if focus_password:
            self.token_edit.setFocus()
        else:
            self.nav.setFocus()

    # -- pages -------------------------------------------------------------------------

    def page_keys(self) -> list:
        return list(self._page_keys)

    def current_page(self) -> str:
        row = self.nav.currentRow()
        return self._page_keys[row] if 0 <= row < len(self._page_keys) else DEFAULT_PAGE

    def show_page(self, key: str) -> None:
        if key in self._page_keys:
            self.nav.setCurrentRow(self._page_keys.index(key))

    def _on_nav_row(self, row: int) -> None:
        if not 0 <= row < len(self._page_keys):
            return
        key = self._page_keys[row]
        self.pages.setCurrentWidget(self._page_widgets[key])
        self._refit_results()
        if key == "logs":
            self.logs_panel.reload_server()

    def _refit_results(self) -> None:
        """Re-measure the result labels whose text changed while their page was not showing."""
        if self.cleanup_result.text():
            _fit_wrapped(self.cleanup_result, CLEANUP_RESULT_WIDTH)
        if self.result_label.text():
            _fit_wrapped(self.result_label, RESULT_LABEL_WIDTH)

    def _remembered_page(self) -> str:
        key = self._config.get("settings_page")
        return key if key in self._page_keys else DEFAULT_PAGE

    def _remember_page(self) -> None:
        """Reopen on the page last used (Save writes it with the other settings; Cancel writes just this)."""
        key = self.current_page()
        if self._config.get("settings_page", DEFAULT_PAGE) == key:
            return
        try:
            data = config_mod.load_config()
            data["settings_page"] = key
            config_mod.save_config(data)
        except Exception:  # noqa: BLE001 - cosmetic
            log.debug("could not remember the settings page", exc_info=True)

    def reject(self) -> None:  # noqa: D102
        self._remember_page()
        super().reject()

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
        # Shown before it is measured: a label measures taller while visible than while hidden.
        self.cleanup_result.setVisible(True)
        _fit_wrapped(self.cleanup_result, CLEANUP_RESULT_WIDTH)

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

    def _validate_or_show(self) -> bool:
        """Validate on Save, and bring the page with the problem to the front."""
        if self._validate_folder():
            return True
        self.show_page("recordings")
        return False

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
        self.result_box.setVisible(True)
        _fit_wrapped(self.result_label, RESULT_LABEL_WIDTH)
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
        _fit_wrapped(self.result_label, RESULT_LABEL_WIDTH)
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
        if not self._validate_or_show():
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

    def _fill_note_types(self, types: List[Dict[str, str]], server_default: str, saved: str) -> None:
        """First "Server default" (with the type's name when known), then each known type; a saved id the
        list lacks stays as an item so saving the dialog does not silently drop it."""
        combo = self.default_note_type_combo
        names = {t["id"]: t["name"] for t in types}
        server_name = names.get(server_default)
        combo.addItem(f"Server default ({server_name})" if server_name else "Server default", "")
        for item in types:
            combo.addItem(item["name"], item["id"])
        if saved and saved not in names:
            combo.addItem(f"{saved} (not found on the server)" if types else saved, saved)
        combo.setCurrentIndex(max(0, combo.findData(saved)))

    def _sync_auto_record(self) -> None:
        """Auto record needs detection; its Auto end row shows only while it is on, and the call-end
        auto-stop checkbox (for prompted recordings) hides while no prompt will appear."""
        self.auto_record_check.setEnabled(self.detect_check.isChecked())
        auto = self.auto_record_check.isEnabled() and self.auto_record_check.isChecked()
        form = self._meeting_form
        form.setRowVisible(self.auto_end_combo, auto)
        form.setRowVisible(self.auto_end_note, auto)
        form.setRowVisible(self.auto_stop_check, not auto)

    def accept(self) -> None:  # noqa: D102
        if not self._validate_or_show():
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
            "auto_record": self.auto_record_check.isChecked(),
            "auto_end": self.auto_end_combo.currentData() or config_mod.DEFAULT_AUTO_END,
            "end_grace_sec": self._detection["end_grace_sec"],
        }
        note_type = self.default_note_type_combo.currentData() or ""
        if note_type:
            data["default_note_type"] = note_type
        else:
            data.pop("default_note_type", None)  # "Server default"
        data["remote_control_allowed"] = self.remote_check.isChecked()
        data["show_audio_levels"] = self.levels_check.isChecked()
        data["appearance"] = self.appearance_combo.currentData() or "system"
        data["local_retention_days"] = int(self.retention_combo.currentData() or 0)
        data["settings_page"] = self.current_page()
        if data["server"]["token"]:
            data["password_prompted"] = True  # saved once: never open Settings on its own for this again
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
