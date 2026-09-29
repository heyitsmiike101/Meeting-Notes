"""Pick saved recordings on this computer and send them to the server again."""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import Callable, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from meeting_notes import config as config_mod
from meeting_notes.client import recordings
from meeting_notes.client.queue import SessionQueue
from meeting_notes.client.recordings import RecordingInfo, ReuploadResult
from meeting_notes.client.ui.icons import make_icon
from meeting_notes.client.ui import theme
from meeting_notes.client.ui.theme import make_sheet

log = logging.getLogger("meeting_notes.client.ui.reupload")


def _when(value: float) -> str:
    try:
        d = dt.datetime.fromtimestamp(float(value))
    except (TypeError, ValueError, OSError, OverflowError):
        return "Unknown date"
    return f"{d:%b} {d.day}, {d:%Y}, {d.hour % 12 or 12}:{d:%M} {'AM' if d.hour < 12 else 'PM'}"


class _RecordingRow(QFrame):
    """One recording: checkbox + name, a muted detail line, and a Queued pill."""

    def __init__(self, info: RecordingInfo, on_toggle: Callable[[], None]):
        super().__init__()
        self.info = info
        self.setObjectName("recRow")
        self.setProperty("invalid", "true" if not info.valid else "false")
        row = QHBoxLayout(self)
        row.setContentsMargins(14, 10, 14, 10)
        row.setSpacing(10)
        column = QVBoxLayout()
        column.setSpacing(2)

        self.check = QCheckBox(info.name)
        self.check.setObjectName("recName")
        self.check.setAccessibleName(f"Select {info.name}")
        self.check.setEnabled(info.valid)
        self.check.toggled.connect(lambda _c: on_toggle())
        column.addWidget(self.check)

        bits = [_when(info.started), recordings.format_duration(info.duration_sec),
                recordings.format_size(info.size_bytes)]
        if info.name != info.path.name:
            bits.append(info.path.name)
        self.detail = QLabel("  ·  ".join(bits))
        self.detail.setObjectName("subtle")
        self.detail.setContentsMargins(28, 0, 0, 0)
        self.detail.setTextInteractionFlags(Qt.NoTextInteraction)
        column.addWidget(self.detail)

        self.error_label: Optional[QLabel] = None
        if info.error:
            self.error_label = QLabel(info.error)
            self.error_label.setObjectName("connResult")
            self.error_label.setProperty("state", "error")
            self.error_label.setWordWrap(True)
            self.error_label.setContentsMargins(28, 0, 0, 0)
            column.addWidget(self.error_label)
        row.addLayout(column, 1)

        self.badge: Optional[QLabel] = None
        if info.queued and info.valid:
            self.badge = QLabel("Queued")
            self.badge.setObjectName("recBadge")
            self.badge.setToolTip("Already waiting on the upload queue; re-uploading resets it to send everything again")
            row.addWidget(self.badge, 0, Qt.AlignTop)

    @property
    def checked(self) -> bool:
        return self.check.isChecked()

    def set_checked(self, value: bool) -> None:
        if self.check.isEnabled():
            self.check.setChecked(value)


class ReuploadDialog(QDialog):
    """List the recordings in the save folder and queue the chosen ones again.

    ``submit`` receives the chosen folders and returns a ``ReuploadResult``;
    the main window passes the controller's method so the uploader is woken.
    """

    def __init__(
        self,
        parent=None,
        *,
        save_dir: Optional[Path] = None,
        queue: Optional[SessionQueue] = None,
        submit: Optional[Callable[[List[Path]], ReuploadResult]] = None,
        server_configured: Optional[bool] = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Re-upload a saved recording")
        self.setMinimumSize(720, 540)
        self.save_dir = Path(save_dir) if save_dir is not None else config_mod.save_dir()
        self.queue = queue or SessionQueue.for_save_dir(self.save_dir)
        self._submit = submit or (lambda folders: recordings.reupload(self.queue, folders))
        if server_configured is None:
            server_configured = bool(config_mod.server_settings().get("url"))
        self._server_configured = server_configured
        self._extra: List[Path] = []
        self._rows: List[_RecordingRow] = []
        self.result: Optional[ReuploadResult] = None
        self._bulk = False

        layout = make_sheet(self, "Re-upload a saved recording")
        layout.setSpacing(12)

        intro = QLabel(
            "Recordings are kept on this computer. Choose the ones to send to the server again; "
            "each is re-sent in full and transcribed again."
        )
        intro.setObjectName("subtle")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        folder_row = QHBoxLayout()
        self.folder_label = QLabel()
        self.folder_label.setObjectName("subtle")
        self.folder_label.setWordWrap(True)
        self.folder_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.browse_button = QPushButton("Browse for a folder...")
        self.browse_button.setToolTip("Add a recording folder (or a folder of recordings) from somewhere else")
        self.browse_button.setAutoDefault(False)
        self.browse_button.clicked.connect(self._browse)
        folder_row.addWidget(self.folder_label, 1)
        folder_row.addWidget(self.browse_button)
        layout.addLayout(folder_row)

        header = QHBoxLayout()
        self.select_all = QCheckBox("Select all")
        self.select_all.setAccessibleName("Select all recordings")
        self.select_all.toggled.connect(self._select_all_toggled)
        self.count_label = QLabel("")
        self.count_label.setObjectName("subtle")
        header.addWidget(self.select_all)
        header.addStretch(1)
        header.addWidget(self.count_label)
        layout.addLayout(header)

        self.scroll = QScrollArea()
        self.scroll.setObjectName("recScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.viewport().setAutoFillBackground(False)
        self.list_host = QWidget()
        self.list_host.setObjectName("recList")
        self.list_layout = QVBoxLayout(self.list_host)
        self.list_layout.setContentsMargins(0, 0, 6, 0)
        self.list_layout.setSpacing(8)
        self.scroll.setWidget(self.list_host)
        layout.addWidget(self.scroll, 1)

        self.empty_label = QLabel("")
        self.empty_label.setObjectName("subtle")
        self.empty_label.setWordWrap(True)
        self.empty_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.empty_label)

        # Inline result line: an error (red) or the queued confirmation (green).
        self.result_box = QWidget()
        result_row = QHBoxLayout(self.result_box)
        result_row.setContentsMargins(0, 0, 0, 0)
        result_row.setSpacing(8)
        self.result_icon = QLabel()
        self.result_icon.setFixedSize(18, 18)
        self.result_label = QLabel("")
        self.result_label.setObjectName("connResult")
        self.result_label.setWordWrap(True)
        result_row.addWidget(self.result_icon, 0, Qt.AlignVCenter)
        result_row.addWidget(self.result_label, 1)
        self.result_box.setVisible(False)
        layout.addWidget(self.result_box)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.close_button = QPushButton("Close")
        self.close_button.setAutoDefault(False)
        self.close_button.clicked.connect(self.reject)
        self.reupload_button = QPushButton("Re-upload")
        self.reupload_button.setDefault(True)
        self.reupload_button.clicked.connect(self._reupload)
        buttons.addWidget(self.close_button)
        buttons.addWidget(self.reupload_button)
        layout.addLayout(buttons)

        self.refresh()

    # -- listing --------------------------------------------------------------

    def _all_infos(self) -> List[RecordingInfo]:
        infos = recordings.scan_save_folder(self.save_dir, self.queue)
        seen = {i.path.resolve() for i in infos}
        for extra in self._extra:
            try:
                key = extra.resolve()
            except OSError:
                key = extra
            if key not in seen:
                seen.add(key)
                infos.append(recordings.inspect_recording(extra, self.queue))
        infos.sort(key=lambda r: r.started, reverse=True)
        return infos

    def refresh(self, keep_checked: bool = False) -> None:
        checked = {r.info.path for r in self._rows if r.checked} if keep_checked else set()
        while self.list_layout.count():
            item = self.list_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._rows = []
        self.folder_label.setText(f"Save folder: {self.save_dir}")
        for info in self._all_infos():
            row = _RecordingRow(info, self._update_selection)
            self._rows.append(row)
            self.list_layout.addWidget(row)
            if info.path in checked:
                row.set_checked(True)
        self.list_layout.addStretch(1)
        valid = [r for r in self._rows if r.info.valid]
        self.empty_label.setVisible(not self._rows)
        if not self._rows:
            self.empty_label.setText(
                f"No saved recordings found in {self.save_dir}.\n"
                "Use Browse for a folder to pick one from somewhere else."
            )
        self.select_all.setEnabled(bool(valid))
        self._update_selection()

    # -- selection ------------------------------------------------------------

    def selected(self) -> List[RecordingInfo]:
        return [r.info for r in self._rows if r.checked]

    def rows(self) -> List[_RecordingRow]:
        return list(self._rows)

    def _update_selection(self) -> None:
        count = len(self.selected())
        valid = [r for r in self._rows if r.info.valid]
        self.count_label.setText(f"{count} selected" if valid else "")
        self.reupload_button.setText(f"Re-upload {count}" if count else "Re-upload")
        self.reupload_button.setEnabled(count > 0)
        self._bulk = True
        try:
            self.select_all.setChecked(bool(valid) and count == len(valid))
        finally:
            self._bulk = False

    def _select_all_toggled(self, on: bool) -> None:
        if self._bulk:
            return
        for row in self._rows:
            row.set_checked(on)

    # -- browse ---------------------------------------------------------------

    def _browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Choose a recording folder", str(self.save_dir)
        )
        if chosen:
            self.add_folder(Path(chosen))

    def add_folder(self, folder: Path) -> None:
        """Add a recording folder, or every recording inside a folder of them."""
        folder = Path(folder)
        if (folder / "session.json").exists() or recordings.looks_like_session(folder):
            found = [folder]
        else:
            found = recordings.session_folders(folder)
        if not found:
            self._show_result(f"No recordings found in {folder}.", ok=False)
            return
        self._hide_result()
        known = {r.info.path.resolve() for r in self._rows}
        for path in found:
            if path.resolve() not in known and path not in self._extra:
                self._extra.append(path)
        self.refresh(keep_checked=True)
        wanted = {p.resolve() for p in found}
        for row in self._rows:
            if row.info.path.resolve() in wanted:
                row.set_checked(True)

    # -- result line ----------------------------------------------------------

    def _show_result(self, text: str, ok: bool) -> None:
        tokens = theme.tokens()
        self.result_label.setProperty("state", "ok" if ok else "error")
        self.result_label.style().unpolish(self.result_label)
        self.result_label.style().polish(self.result_label)
        self.result_label.setText(text)
        glyph, colour = ("check-circle", tokens["ok_text"]) if ok else ("alert-circle", tokens["danger_text"])
        self.result_icon.setPixmap(make_icon(glyph, colour, colour, 18).pixmap(18, 18))
        self.result_box.setVisible(True)

    def _hide_result(self) -> None:
        self.result_box.setVisible(False)

    # -- action ---------------------------------------------------------------

    def _reupload(self) -> None:
        chosen = self.selected()
        if not chosen:
            return
        if not self._server_configured:
            self._show_result("Set the server URL in Settings first, then try again.", ok=False)
            return
        try:
            result = self._submit([i.path for i in chosen])
        except Exception as exc:  # noqa: BLE001 - keep the dialog usable
            log.exception("re-upload failed")
            self._show_result(f"Could not queue the recordings: {exc}", ok=False)
            return
        self.result = result
        log.info("re-upload dialog: %s", result.summary())
        self.refresh()
        problems = "; ".join(f"{p.name}: {why}" for p, why in result.rejected.items())
        if result.total:
            text = result.summary() + "."
            if problems:
                text += f" Skipped {problems}."
            self._show_result(text, ok=True)
            self.close_button.setText("Done")
        else:
            self._show_result(f"Nothing was queued. {problems}", ok=False)
