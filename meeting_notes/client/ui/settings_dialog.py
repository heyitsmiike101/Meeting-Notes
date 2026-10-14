"""Settings: where recordings are saved, and which server transcribes them."""

from __future__ import annotations

import logging
import sys  # noqa: F401 - tests patch sd.sys.platform
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

from meeting_notes import config as config_mod
from meeting_notes.client import authcheck, paths, retention
from meeting_notes.client import settings_schema as schema_mod
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


# (key, sidebar title). The order is the order of the sidebar; the pages, their titles and everything on them come
# from ``settings_schema.build_schema`` (the server's Recorders page renders the same schema).
PAGES = tuple((key, title) for key, title, _local_only in schema_mod.PAGE_TITLES)
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
        device_lister=None,
    ):
        """``focus_password`` opens the Server page with the cursor in the password field;
        ``first_run`` also spells out where the password comes from (the first-start prompt).
        ``note_types`` is the server's note types as ``[{id, name}]`` (the window fetched them; empty when it
        could not) and ``server_default_note_type`` the id the server uses by default. ``device_lister()`` returns
        ``{"mic": [names], "system": [names]}`` for the Audio page (it runs on a worker thread); the default lists
        the real devices, and lists nothing under tests that disable device scanning."""
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumSize(720, 520)
        self.resize(820, 580)
        self._config = config_mod.load_config()
        self._device_lister = device_lister
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
        server = config_mod.server_settings(self._config)
        # Every page, section, label, help text and choice below comes from this schema; the server's Recorders page
        # draws the same one (see settings_schema.py). ``schema_rows`` records which widget shows which item.
        self._schema = schema_mod.build_schema(
            note_types_known=bool(note_types), first_run=first_run, server_url=(server.get("url") or "").strip()
        )
        self._items = schema_mod.items_by_id(self._schema)
        self.schema_rows: list = []

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
        self._row("general", form, "section.appearance", first=True)
        # System / Light / Dark, applied as soon as it is saved.
        self.appearance_combo = QComboBox()
        self.appearance_combo.setAccessibleName(self._aria("appearance"))
        for value, text in self._items["appearance"]["choices"]:
            self.appearance_combo.addItem(text, value)
        self._appearance = config_mod.appearance_setting(self._config)
        self.appearance_combo.setCurrentIndex(max(0, self.appearance_combo.findData(self._appearance)))
        self._row("general", form, "appearance", self.appearance_combo)

        self._row("general", form, "section.detection")
        detection = config_mod.meeting_detection_settings(self._config)
        self._detection = detection
        self.detect_check = self._checkbox("meeting_detection.enabled", detection["enabled"])
        self._row("general", form, "meeting_detection.enabled", self.detect_check)

        # Auto record: only meaningful while detection is on; its "Also end" choice only shows while it is on.
        self.auto_record_check = self._checkbox("meeting_detection.auto_record", detection["auto_record"])
        self._row("general", form, "meeting_detection.auto_record", self.auto_record_check)
        self.auto_end_combo = QComboBox()
        self.auto_end_combo.setAccessibleName(self._aria("meeting_detection.auto_end"))
        for value, text in self._items["meeting_detection.auto_end"]["choices"]:
            self.auto_end_combo.addItem(text, value)
        self.auto_end_combo.setCurrentIndex(max(0, self.auto_end_combo.findData(detection["auto_end"])))
        self._row("general", form, "meeting_detection.auto_end", self.auto_end_combo)
        self.auto_end_note = self._note_label("note.auto_end")
        self._row("general", form, "note.auto_end", self.auto_end_note)
        self.auto_end_bye_note = self._note_label("note.auto_end_bye", "")
        self._row("general", form, "note.auto_end_bye", self.auto_end_bye_note)
        self._meeting_form = form

        self.auto_stop_check = self._checkbox("meeting_detection.auto_stop", detection["auto_stop"])
        self._row("general", form, "meeting_detection.auto_stop", self.auto_stop_check)

        self.suggest_stop_check = self._checkbox("meeting_detection.suggest_stop", detection["suggest_stop"])
        self._row("general", form, "meeting_detection.suggest_stop", self.suggest_stop_check)
        self.detect_check.toggled.connect(lambda _on: self._sync_auto_record())
        self.auto_record_check.toggled.connect(lambda _on: self._sync_auto_record())
        self.auto_end_combo.currentIndexChanged.connect(lambda _i: self._sync_auto_record())

        self._row("general", form, "section.notes")
        self.default_note_type_combo = QComboBox()
        self.default_note_type_combo.setAccessibleName(self._aria("default_note_type"))
        self._fill_note_types(note_types or [], server_default_note_type, config_mod.default_note_type_setting(self._config))
        self._row("general", form, "default_note_type", self.default_note_type_combo)
        if not note_types:
            self._row("general", form, "note.note_type_offline", self._note_label("note.note_type_offline"))
        self._row("general", form, "note.note_type", self._note_label("note.note_type"))

        # ---- Audio: live input levels before recording (nothing is recorded) ------
        form = new_page("audio")
        self.levels_check = self._checkbox("show_audio_levels", config_mod.idle_levels_enabled(self._config))
        self._row("audio", form, "show_audio_levels", self.levels_check)
        self._row("audio", form, "note.levels", self._note_label("note.levels"))
        self._row("audio", form, "section.devices")
        self.mic_combo = QComboBox()
        self.mic_combo.setAccessibleName(self._aria("audio_devices.mic"))
        self.system_combo = QComboBox()
        self.system_combo.setAccessibleName(self._aria("audio_devices.system"))
        chosen = config_mod.audio_device_settings(self._config)
        for kind, combo in (("mic", self.mic_combo), ("system", self.system_combo)):
            self._fill_device_combo(combo, kind, [], chosen[kind])
        self.system_combo.setToolTip(self._items["audio_devices.system"]["help"])
        for combo in (self.mic_combo, self.system_combo):
            # Long device names must not widen the page (the notes below would be clipped).
            combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(24)
        self._row("audio", form, "audio_devices.mic", self.mic_combo)
        self._row("audio", form, "audio_devices.system", self.system_combo)
        self._mac_system_note = None
        if "note.devices_mac" in self._items:
            self._mac_system_note = self._note_label("note.devices_mac")
            self._row("audio", form, "note.devices_mac", self._mac_system_note)
        self._row("audio", form, "note.devices_auto", self._note_label("note.devices_auto"))
        self._row("audio", form, "note.devices_window", self._note_label("note.devices_window"))

        # ---- Recordings: save folder, local clean-up ---------------------------------
        form = new_page("recordings")
        self._row("recordings", form, "section.save_folder", first=True)
        self.save_dir_edit = QLineEdit(str(config_mod.save_dir(self._config)))
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._pick_folder)
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(self.save_dir_edit, 1)
        row.addWidget(browse)
        self._row("recordings", form, "save_dir", row)
        self.folder_error = QLabel("")
        self.folder_error.setObjectName("connResult")
        self.folder_error.setProperty("state", "error")
        self.folder_error.setWordWrap(True)
        self.folder_error.setMinimumWidth(380)
        self.folder_error.setVisible(False)
        self._helper_row(form, self.folder_error)
        self.save_dir_edit.textChanged.connect(lambda _t: self._validate_folder())

        self._row("recordings", form, "section.local")
        self.retention_combo = QComboBox()
        self.retention_combo.setAccessibleName(self._aria("local_retention_days"))
        for days, text in self._items["local_retention_days"]["choices"]:
            self.retention_combo.addItem(text, days)
        self.retention_combo.setCurrentIndex(
            max(0, self.retention_combo.findData(config_mod.local_retention_days(self._config)))
        )
        self._row("recordings", form, "local_retention_days", self.retention_combo)
        self._row("recordings", form, "note.retention", self._note_label("note.retention"))
        self.delete_after_upload_check = self._checkbox(
            "delete_after_upload", config_mod.delete_after_upload(self._config)
        )
        self._row("recordings", form, "delete_after_upload", self.delete_after_upload_check)
        self.local_stats_label = QLabel("Checking the folder...")
        self.local_stats_label.setObjectName("subtle")
        self.cleanup_button = QPushButton("Clean up now")
        self.cleanup_button.clicked.connect(self._cleanup_clicked)
        stats_row = QHBoxLayout()
        stats_row.setSpacing(8)
        stats_row.addWidget(self.local_stats_label, 1)
        stats_row.addWidget(self.cleanup_button)
        self._row("recordings", form, "recordings.stats", stats_row)
        self.cleanup_result = QLabel("")
        self.cleanup_result.setObjectName("connResult")
        self.cleanup_result.setWordWrap(True)
        self.cleanup_result.setVisible(False)
        self._helper_row(form, self.cleanup_result)
        self.retention_combo.currentIndexChanged.connect(lambda _i: self._sync_cleanup_button())
        self._sync_cleanup_button()
        self.save_dir_edit.editingFinished.connect(self._refresh_local_stats)
        self._refresh_local_stats()

        # ---- Server: transcription server, uploads, updates ------------------------------
        form = new_page("server")
        self.url_edit = QLineEdit(server.get("url", ""))
        self.url_edit.setPlaceholderText(self._items["server.url"]["placeholder"])
        self._row("server", form, "server.url", self.url_edit)

        self.token_edit = QLineEdit(server.get("token", ""))
        self.token_edit.setEchoMode(QLineEdit.Password)
        self.token_edit.setPlaceholderText(self._items["server.token"]["placeholder"])
        self.token_edit.setAccessibleName(self._aria("server.token"))
        token_row = QHBoxLayout()
        token_row.setSpacing(8)
        token_row.addWidget(self.token_edit, 1)
        self.test_button = QPushButton("Test connection")
        self.test_button.clicked.connect(self.test_connection)
        token_row.addWidget(self.test_button)
        self._row("server", form, "server.token", token_row)
        self.password_hint = self._note_label("note.password_hint")
        self._row("server", form, "note.password_hint", self.password_hint)
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
        self._helper_row(form, self.result_box)
        self.url_edit.textChanged.connect(self._clear_result)
        self.token_edit.textChanged.connect(self._clear_result)

        self._row("server", form, "section.uploads")
        self.live_check = self._checkbox("server.live_preview", server.get("live_preview", True))
        self._row("server", form, "server.live_preview", self.live_check)
        self.live_check.toggled.connect(lambda _on: self._sync_auto_record())

        self.upload_check = self._checkbox("server.auto_upload", server.get("auto_upload", True))
        self._row("server", form, "server.auto_upload", self.upload_check)

        self._row("server", form, "section.updates")
        self.update_check = self._checkbox("server.check_updates", server.get("check_updates", True))
        self._row("server", form, "server.check_updates", self.update_check)

        self._row("server", form, "note.transcription", self._note_label("note.transcription"))

        # ---- Remote control ----------------------------------------------------------
        form = new_page("remote")
        self.remote_check = self._checkbox("remote_control_allowed", config_mod.remote_control_allowed(self._config))
        self._row("remote", form, "remote_control_allowed", self.remote_check)
        self._row("remote", form, "note.remote", self._note_label("note.remote"))

        # ---- Logs: the former Logs window ------------------------------------------------
        logs_page = QWidget()
        logs_column = QVBoxLayout(logs_page)
        logs_column.setContentsMargins(0, 0, 0, 0)
        logs_column.setSpacing(12)
        logs_heading = QLabel(dict(PAGES)["logs"])
        logs_heading.setObjectName("heading")
        logs_column.addWidget(logs_heading)
        self.logs_panel = LogsPanel()
        logs_column.addWidget(self.logs_panel, 1)
        self.pages.addWidget(logs_page)
        self._page_widgets["logs"] = logs_page

        # ---- About ---------------------------------------------------------------------
        form = new_page("about")
        self.about_labels: dict = {}
        values = schema_mod.read_only_values(self._config)
        for key in ("version", "save_dir", "config", "logs"):
            text = QLabel(values[f"about.{key}"])
            text.setWordWrap(True)
            text.setTextInteractionFlags(Qt.TextSelectableByMouse)
            _fit_wrapped(text, NOTE_WIDTH)
            self.about_labels[key] = text
            self._row("about", form, f"about.{key}", text)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self.save_button = buttons.button(QDialogButtonBox.Save)
        buttons.accepted.connect(self._save_clicked)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.nav.currentRowChanged.connect(self._on_nav_row)
        self._sync_auto_record()
        self._load_devices()
        if focus_password:
            page = "server"
        self.show_page(page if page in self._page_keys else self._remembered_page())
        if focus_password:
            self.token_edit.setFocus()
        else:
            self.nav.setFocus()

    # -- schema-driven rows --------------------------------------------------------------

    def _aria(self, key: str) -> str:
        item = self._items[key]
        return item.get("aria") or item["label"]

    def _checkbox(self, key: str, checked) -> QCheckBox:
        item = self._items[key]
        box = QCheckBox(item["label"])
        box.setChecked(bool(checked))
        if item.get("help"):
            box.setToolTip(item["help"])
        return box

    def _note_label(self, item_id: str, text: Optional[str] = None) -> QLabel:
        return _note(self._items[item_id]["text"] if text is None else text)

    def _row(self, page: str, form: QFormLayout, item_id: str, widget=None, *, first: bool = False) -> None:
        """Add one schema item to ``form``: a section heading (no widget), a note or checkbox on a blank label, or
        a field with its label. Recorded in ``schema_rows`` so the parity test can check it."""
        item = self._items[item_id]
        if item["type"] == "section":
            widget = _section(item["title"], first=first)
            form.addRow(widget)
        elif item["type"] == "note" or item["kind"] == "bool":
            form.addRow("", widget)
        else:
            form.addRow(item["label"], widget)
        self.schema_rows.append((page, item_id, form, widget))

    def _helper_row(self, form: QFormLayout, widget) -> None:
        """A row that belongs to the dialog only (an inline error or result), not to the schema."""
        form.addRow("", widget)
        self.schema_rows.append((None, None, form, widget))

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
            self.local_stats_label.setText(schema_mod.stats_text(result["count"], result["bytes"]))

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
        for value, label in schema_mod.note_type_options(self._items["default_note_type"], types, server_default, saved):
            combo.addItem(label, value)
        combo.setCurrentIndex(max(0, combo.findData(saved)))

    # -- audio devices ---------------------------------------------------------------

    def _fill_device_combo(self, combo: QComboBox, kind: str, names: List[str], saved: str) -> None:
        """Automatic first, then the connected devices; a saved device that is not connected stays, marked."""
        spec = self._items[f"audio_devices.{kind}"]
        combo.blockSignals(True)
        combo.clear()
        for value, label in schema_mod.device_options(spec, names, saved):
            combo.addItem(label, value)
        combo.setCurrentIndex(max(0, combo.findData(saved)))
        combo.blockSignals(False)

    def _default_device_lister(self) -> Dict[str, List[str]]:
        return schema_mod.list_devices()

    def _load_devices(self) -> None:
        """List the connected devices off the GUI thread, then fill both combos."""
        lister = self._device_lister or self._default_device_lister

        def done(result) -> None:
            if isinstance(result, Exception):
                log.warning("could not list audio devices for Settings: %s", result)
                return
            self.set_device_names(result)

        self._run_bg(lister, done)

    def set_device_names(self, found: Dict[str, List[str]]) -> None:
        """Fill the device combos from ``{"mic": [...], "system": [...]}``, keeping what is currently picked."""
        for kind, combo in (("mic", self.mic_combo), ("system", self.system_combo)):
            names = [str(n) for n in (found or {}).get(kind) or []]
            self._fill_device_combo(combo, kind, names, combo.currentData() or "")
        if self._mac_system_note is not None:
            self.system_combo.setEnabled(self.system_combo.count() > 1)

    def _sync_auto_record(self) -> None:
        """Auto record needs detection; its Also end row shows only while it is on; the
        Stop when the meeting closes checkbox is always shown. The rules are the schema's own
        (``enabled_if`` / ``show_if``), the same ones the server's page applies."""
        values = self.collect_values()
        items = self._items
        self.auto_record_check.setEnabled(schema_mod.is_enabled(items["meeting_detection.auto_record"], values))
        form = self._meeting_form
        form.setRowVisible(self.auto_end_combo, schema_mod.is_shown(items["meeting_detection.auto_end"], values))
        form.setRowVisible(self.auto_end_note, schema_mod.is_shown(items["note.auto_end"], values))
        bye = items["note.auto_end_bye"]
        text = schema_mod.note_text(bye, values) if schema_mod.is_shown(bye, values) else ""
        if text:
            self.auto_end_bye_note.setText(text)
            _fit_wrapped(self.auto_end_bye_note, NOTE_WIDTH)
        form.setRowVisible(self.auto_end_bye_note, bool(text))
        form.setRowVisible(self.auto_stop_check, True)

    def collect_values(self) -> Dict[str, object]:
        """What the widgets say now, as flat ``{dotted key: value}`` (the sign-in token is not one: it is a secret)."""
        return {
            "appearance": self.appearance_combo.currentData() or "system",
            "meeting_detection.enabled": self.detect_check.isChecked(),
            "meeting_detection.auto_record": self.auto_record_check.isChecked(),
            "meeting_detection.auto_end": self.auto_end_combo.currentData() or config_mod.DEFAULT_AUTO_END,
            "meeting_detection.auto_stop": self.auto_stop_check.isChecked(),
            "meeting_detection.suggest_stop": self.suggest_stop_check.isChecked(),
            "default_note_type": self.default_note_type_combo.currentData() or "",
            "show_audio_levels": self.levels_check.isChecked(),
            "audio_devices.mic": self.mic_combo.currentData() or "",
            "audio_devices.system": self.system_combo.currentData() or "",
            "save_dir": self.save_dir_edit.text().strip(),
            "local_retention_days": int(self.retention_combo.currentData() or 0),
            "delete_after_upload": self.delete_after_upload_check.isChecked(),
            "server.url": self.url_edit.text().strip().rstrip("/"),
            "server.live_preview": self.live_check.isChecked(),
            "server.auto_upload": self.upload_check.isChecked(),
            "server.check_updates": self.update_check.isChecked(),
            "remote_control_allowed": self.remote_check.isChecked(),
        }

    def accept(self) -> None:  # noqa: D102
        if not self._validate_or_show():
            return
        # The same merge and follow-up the server's remote Save runs (settings_schema.merge_values / after_save).
        data = schema_mod.merge_values(self._config, self.collect_values(), token=self.token_edit.text().strip())
        data["settings_page"] = self.current_page()
        config_mod.save_config(data)
        schema_mod.after_save(self._config, data)
        super().accept()


_changed_keys = schema_mod.changed_keys
