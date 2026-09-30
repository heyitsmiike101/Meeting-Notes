"""Saved recordings on this computer: what the server has of each, re-upload, and delete local copies."""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from PySide6.QtCore import QObject, Qt, Signal
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
from meeting_notes import recording_status
from meeting_notes.client import recordings, remote_recordings, retention
from meeting_notes.client.queue import SessionQueue
from meeting_notes.client.recordings import RecordingInfo, ReuploadResult
from meeting_notes.client.ui.icons import make_icon
from meeting_notes.client.ui import theme
from meeting_notes.client.ui.theme import make_sheet

log = logging.getLogger("meeting_notes.client.ui.reupload")

# How long one answer from the server is trusted before the dialog asks again.
STATUS_TTL_SEC = 30.0
# Pill text is cut here (the tooltip keeps the full text) so a long failure reason can not squeeze the name.
_PILL_MAX_CHARS = 40
_LOADING = {"status": "unknown", "label": "Checking server...", "tone": "muted", "detail": None,
            "server_has_copy": False}
NOTE_UNREACHABLE = "Could not reach the server; showing what this computer knows."
NOTE_TOO_OLD = "This server is too old to report status."
NOTE_NO_SERVER = "No server is set up; showing what this computer knows."

StatusProvider = Callable[[List[str]], Dict[str, Dict[str, Any]]]


def _when(value: float) -> str:
    try:
        d = dt.datetime.fromtimestamp(float(value))
    except (TypeError, ValueError, OSError, OverflowError):
        return "Unknown date"
    return f"{d:%b} {d.day}, {d:%Y}, {d.hour % 12 or 12}:{d:%M} {'AM' if d.hour < 12 else 'PM'}"


def _plural(n: int, word: str = "recording") -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def default_status_provider(ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Ask the configured server what it has of these recordings (runs on a worker thread)."""
    server = config_mod.server_settings()
    url = server.get("url")
    if not url:
        raise RuntimeError("no server configured")
    from meeting_notes.client.api import ServerClient

    client = ServerClient(url, server.get("token") or None, timeout=8.0)
    try:
        return client.recordings_status(ids)
    finally:
        client.close()


class _StatusBridge(QObject):
    """Hands a worker thread's answer to the GUI thread (a queued signal, like main_window's _AsyncBridge)."""

    done = Signal(object)


class DeleteConfirmDialog(QDialog):
    """"Delete from this computer?": the names, where they go, and a red warning when no server copy exists."""

    def __init__(self, names: List[str], warning: Optional[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Delete from this computer")
        self.setMinimumWidth(460)
        self.names = list(names)
        self.warning = warning
        layout = make_sheet(self, "Delete from this computer")
        layout.setSpacing(12)
        title = QLabel(f"Delete {_plural(len(names))} from this computer?")
        title.setObjectName("heading")
        title.setWordWrap(True)
        layout.addWidget(title)

        shown = names[:5]
        self.names_label = QLabel("\n".join(shown))
        self.names_label.setObjectName("recName")
        self.names_label.setWordWrap(True)
        layout.addWidget(self.names_label)
        self.more_label: Optional[QLabel] = None
        if len(names) > len(shown):
            self.more_label = QLabel(f"and {len(names) - len(shown)} more")
            self.more_label.setObjectName("subtle")
            layout.addWidget(self.more_label)

        trash = retention.trash_name()
        where = QLabel(f"They go to this computer's {trash}, so you can still put them back from there.")
        where.setObjectName("subtle")
        where.setWordWrap(True)
        layout.addWidget(where)

        self.warning_box: Optional[QFrame] = None
        self.calm_label: Optional[QLabel] = None
        if warning:
            self.warning_box = QFrame()
            self.warning_box.setObjectName("alertBar")
            box = QHBoxLayout(self.warning_box)
            box.setContentsMargins(12, 10, 12, 10)
            box.setSpacing(8)
            icon = QLabel()
            icon.setFixedSize(18, 18)
            colour = theme.tokens()["danger_text"]
            icon.setPixmap(make_icon("alert-circle", colour, colour, 18).pixmap(18, 18))
            self.warning_label = QLabel(warning)
            self.warning_label.setObjectName("deleteWarning")
            self.warning_label.setWordWrap(True)
            box.addWidget(icon, 0, Qt.AlignTop)
            box.addWidget(self.warning_label, 1)
            layout.addWidget(self.warning_box)
        else:
            self.calm_label = QLabel("They stay on the server; this only frees space on this computer.")
            self.calm_label.setWordWrap(True)
            layout.addWidget(self.calm_label)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setDefault(True)
        self.cancel_button.clicked.connect(self.reject)
        self.delete_button = QPushButton("Delete")
        self.delete_button.setObjectName("danger")
        self.delete_button.setAutoDefault(False)
        self.delete_button.clicked.connect(self.accept)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.delete_button)
        layout.addLayout(buttons)
        self.cancel_button.setFocus()


def confirm_delete(names: List[str], warning: Optional[str], parent=None) -> bool:
    """The real confirm box (the dialog's default ``confirm``)."""
    return DeleteConfirmDialog(names, warning, parent).exec() == QDialog.Accepted


class _RecordingRow(QFrame):
    """One recording: checkbox + name, a muted detail line, and a status pill."""

    def __init__(self, info: RecordingInfo, local: Dict[str, Any], on_toggle: Callable[[], None]):
        super().__init__()
        self.info = info
        self.local = local
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
        # The recording in progress can not be picked (local["valid"] is False for it).
        self.check.setEnabled(bool(local.get("valid")))
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
        if info.error and not local.get("active"):
            self.error_label = QLabel(info.error)
            self.error_label.setObjectName("connResult")
            self.error_label.setProperty("state", "error")
            self.error_label.setWordWrap(True)
            self.error_label.setContentsMargins(28, 0, 0, 0)
            column.addWidget(self.error_label)
        row.addLayout(column, 1)

        self.badge = QLabel("")
        self.badge.setObjectName("recBadge")
        row.addWidget(self.badge, 0, Qt.AlignTop)
        self.status: Dict[str, Any] = dict(_LOADING)
        self.set_status(self.status)

    def set_status(self, status: Dict[str, Any]) -> None:
        """Show ``status`` (a ``recording_status.combine`` result) in the pill."""
        self.status = status
        label = str(status["label"])
        shown = label if len(label) <= _PILL_MAX_CHARS else label[: _PILL_MAX_CHARS - 1].rstrip() + "…"
        tip = status.get("detail") or ""
        if shown != label:
            tip = f"{label}\n{tip}" if tip else label
        self.badge.setText(shown)
        self.badge.setToolTip(tip)
        self.badge.setProperty("tone", status["tone"])
        self.badge.setProperty("status", status["status"])
        self.badge.style().unpolish(self.badge)
        self.badge.style().polish(self.badge)

    @property
    def checked(self) -> bool:
        return self.check.isChecked()

    def set_checked(self, value: bool) -> None:
        if self.check.isEnabled():
            self.check.setChecked(value)


class ReuploadDialog(QDialog):
    """List the recordings in the save folder, show what the server has of each, and re-send or delete them.

    ``submit`` receives the chosen folders and returns a ``ReuploadResult``; the main window passes the
    controller's method so the uploader is woken. ``status_provider(ids)`` returns ``{id: server_state}``
    (see ``recording_status``); it runs on a worker thread unless ``async_status`` is False (tests).
    ``confirm(names, warning)`` asks before deleting; ``remover`` moves a folder to the Recycle Bin / Trash.
    """

    def __init__(
        self,
        parent=None,
        *,
        save_dir: Optional[Path] = None,
        queue: Optional[SessionQueue] = None,
        submit: Optional[Callable[[List[Path]], ReuploadResult]] = None,
        server_configured: Optional[bool] = None,
        active_dir: Optional[Path] = None,
        status_provider: Optional[StatusProvider] = None,
        async_status: bool = True,
        clock: Callable[[], float] = time.monotonic,
        confirm: Optional[Callable[[List[str], Optional[str]], bool]] = None,
        remover: Callable[[Path], str] = retention.move_to_recycle_bin,
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
        self._active_dir = Path(active_dir) if active_dir is not None else None
        self._provider: Optional[StatusProvider] = status_provider or (
            default_status_provider if server_configured else None
        )
        self._async = async_status
        self._clock = clock
        self._confirm = confirm or (lambda names, warning: confirm_delete(names, warning, self))
        self._remover = remover
        self._extra: List[Path] = []
        self._rows: List[_RecordingRow] = []
        self.result: Optional[ReuploadResult] = None
        self._bulk = False
        # Server answers: id -> (state or None when the server did not know, fetched_at). Lives as long as
        # the dialog so re-rendering after a re-upload / delete / browse does not ask again for everything.
        self._cache: Dict[str, Tuple[Optional[Dict[str, Any]], float]] = {}
        self._inflight: set = set()
        self._status_note = ""
        self._failed_at: Optional[float] = None  # a failed ask is not repeated until the TTL passes
        self._closed = False
        self._bridge = _StatusBridge(self)
        self._bridge.done.connect(self._on_status_done)

        layout = make_sheet(self, "Re-upload a saved recording")
        layout.setSpacing(12)

        intro = QLabel(
            "Recordings are kept on this computer, and each one shows whether the server has a copy. "
            "Choose recordings to send to the server again (each is re-sent in full and transcribed again), "
            "or delete local copies to free space."
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
        self.refresh_status_button = QPushButton("Refresh status")
        self.refresh_status_button.setObjectName("refreshStatus")
        self.refresh_status_button.setToolTip("Ask the server again what it has of each recording")
        self.refresh_status_button.setAutoDefault(False)
        self.refresh_status_button.clicked.connect(self.refresh_status)
        header.addWidget(self.select_all)
        header.addStretch(1)
        header.addWidget(self.count_label)
        header.addWidget(self.refresh_status_button)
        layout.addLayout(header)

        self.note_label = QLabel("")
        self.note_label.setObjectName("subtle")
        self.note_label.setWordWrap(True)
        self.note_label.setVisible(False)
        layout.addWidget(self.note_label)

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

        # Inline result line: an error (red) or the queued / deleted confirmation (green).
        self.result_box = QWidget()
        result_row = QHBoxLayout(self.result_box)
        result_row.setContentsMargins(0, 0, 0, 0)
        result_row.setSpacing(8)
        self.result_icon = QLabel()
        self.result_icon.setFixedSize(18, 18)
        self.result_label = QLabel("")
        self.result_label.setObjectName("connResult")
        self.result_label.setWordWrap(True)
        result_row.addWidget(self.result_icon, 0, Qt.AlignTop)
        result_row.addWidget(self.result_label, 1)
        self.result_box.setVisible(False)
        layout.addWidget(self.result_box)

        buttons = QHBoxLayout()
        self.close_button = QPushButton("Close")
        self.close_button.setAutoDefault(False)
        self.close_button.clicked.connect(self.reject)
        self.delete_button = QPushButton("Delete from this computer...")
        self.delete_button.setObjectName("deleteButton")
        self.delete_button.setToolTip("Move the chosen recordings to this computer's "
                                      f"{retention.trash_name()}")
        self.delete_button.setAutoDefault(False)
        self.delete_button.clicked.connect(self._delete)
        self.reupload_button = QPushButton("Re-upload")
        self.reupload_button.setDefault(True)
        self.reupload_button.clicked.connect(self._reupload)
        buttons.addWidget(self.delete_button)
        buttons.addStretch(1)
        buttons.addWidget(self.close_button)
        buttons.addWidget(self.reupload_button)
        layout.addLayout(buttons)

        self.refresh()

    def done(self, result: int) -> None:  # noqa: D102 - QDialog hook: stop applying late answers
        self._closed = True
        super().done(result)

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
            local = remote_recordings.describe(info, self.queue, self._active_dir)
            row = _RecordingRow(info, local, self._update_selection)
            self._rows.append(row)
            self.list_layout.addWidget(row)
            if info.path in checked:
                row.set_checked(True)
        self.list_layout.addStretch(1)
        valid = [r for r in self._rows if r.check.isEnabled()]
        self.empty_label.setVisible(not self._rows)
        if not self._rows:
            self.empty_label.setText(
                f"No saved recordings found in {self.save_dir}.\n"
                "Use Browse for a folder to pick one from somewhere else."
            )
        self.select_all.setEnabled(bool(valid))
        self._apply_status()
        self._update_selection()
        self._fetch_missing()

    # -- server status --------------------------------------------------------

    def _fresh_state(self, sid: str) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """(known, state) from the cache; known is False when missing or older than the TTL."""
        hit = self._cache.get(sid)
        if hit is None or self._clock() - hit[1] >= STATUS_TTL_SEC:
            return False, None
        return True, hit[0]

    def _apply_status(self) -> None:
        """Paint every pill from the local row plus whatever the cache knows (in place, no re-render)."""
        for row in self._rows:
            sid = row.info.path.name
            known, state = self._fresh_state(sid)
            combined = recording_status.combine(row.local, state)
            waiting = self._provider is not None and self._failed_at is None and not known
            if combined["status"] == "unknown" and waiting:
                combined = dict(_LOADING)
            row.set_status(combined)
        self.note_label.setText(self._status_note)
        self.note_label.setVisible(bool(self._status_note))

    def _fetch_missing(self) -> None:
        if self._provider is None:
            self._status_note = NOTE_NO_SERVER
            self._apply_status()
            return
        if self._failed_at is not None and self._clock() - self._failed_at < STATUS_TTL_SEC:
            return
        self._failed_at = None
        ids: List[str] = []
        for row in self._rows:
            sid = row.info.path.name
            if row.local.get("active") or sid in self._inflight or sid in ids:
                continue
            if not self._fresh_state(sid)[0]:
                ids.append(sid)
        if not ids:
            return
        self._inflight.update(ids)
        provider = self._provider

        def work() -> Tuple[List[str], Optional[Dict[str, Dict[str, Any]]], Optional[BaseException]]:
            try:
                return ids, provider(list(ids)), None
            except Exception as exc:  # noqa: BLE001 - any failure means "status unknown", never a crash
                return ids, None, exc

        if not self._async:
            self._on_status_done(work())
            return

        def run() -> None:
            outcome = work()
            try:
                self._bridge.done.emit(outcome)
            except RuntimeError:  # the dialog was deleted while we were asking
                pass

        threading.Thread(target=run, name="reupload-status", daemon=True).start()

    def _on_status_done(self, outcome) -> None:
        ids, states, error = outcome
        self._inflight.difference_update(ids)
        if self._closed:
            return
        now = self._clock()
        if error is not None:
            log.info("re-upload dialog: server status unavailable: %s", error)
            self._failed_at = now
            code = getattr(getattr(error, "response", None), "status_code", None)
            if isinstance(error, RuntimeError) and str(error) == "no server configured":
                self._status_note = NOTE_NO_SERVER
            elif code == 404:
                self._status_note = NOTE_TOO_OLD
            else:
                self._status_note = NOTE_UNREACHABLE
        else:
            self._failed_at = None
            self._status_note = ""
            for sid in ids:
                state = (states or {}).get(sid)
                self._cache[sid] = (state if isinstance(state, dict) else None, now)
        self._apply_status()

    def invalidate_status(self, ids: Optional[List[str]] = None) -> None:
        """Forget cached answers (all of them when ``ids`` is None) so the next fetch asks again."""
        if ids is None:
            self._cache.clear()
        else:
            for sid in ids:
                self._cache.pop(sid, None)
        self._failed_at = None
        self._status_note = ""

    def refresh_status(self) -> None:
        """The "Refresh status" button: drop every cached answer and ask the server again."""
        self.invalidate_status()
        self._apply_status()
        self._fetch_missing()

    # -- selection ------------------------------------------------------------

    def selected(self) -> List[RecordingInfo]:
        return [r.info for r in self._rows if r.checked]

    def rows(self) -> List[_RecordingRow]:
        return list(self._rows)

    def _update_selection(self) -> None:
        count = len(self.selected())
        valid = [r for r in self._rows if r.check.isEnabled()]
        self.count_label.setText(f"{count} selected" if valid else "")
        self.reupload_button.setText(f"Re-upload {count}" if count else "Re-upload")
        self.reupload_button.setEnabled(count > 0)
        self.delete_button.setEnabled(count > 0)
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

    # -- actions --------------------------------------------------------------

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
        self.invalidate_status([i.path.name for i in chosen])
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

    def _delete(self) -> None:
        chosen_rows = [r for r in self._rows if r.checked]
        if not chosen_rows:
            return
        names = [r.info.name for r in chosen_rows]
        # No server copy, or the server could not be asked (or has not answered yet): the local
        # folder may be the only copy, so the confirm says so.
        risky = any(not r.status.get("server_has_copy") for r in chosen_rows)
        trash = retention.trash_name()
        warning = recording_status.delete_warning(trash) if risky else None
        if not self._confirm(names, warning):
            return
        paths = [r.info.path for r in chosen_rows]
        try:
            outcome = remote_recordings.delete_folders(paths, self.queue, self._active_dir, self._remover)
        except Exception as exc:  # noqa: BLE001 - keep the dialog usable
            log.exception("delete failed")
            self._show_result(f"Could not delete the recordings: {exc}", ok=False)
            return
        deleted = int(outcome["deleted"])
        log.info("re-upload dialog: moved %d to the %s", deleted, trash)
        gone = {paths[i] for i, r in enumerate(outcome["results"]) if r["ok"]}
        self._extra = [p for p in self._extra if p not in gone]
        shown = {r.info.path.name: r.info.name for r in chosen_rows}
        problems = "; ".join(
            f"{shown.get(r['session_id'], r['session_id'])}: {r['error']}"
            for r in outcome["results"] if not r["ok"]
        )
        self.refresh()
        if deleted:
            text = f"Moved {_plural(deleted)} to the {trash}."
            if problems:
                text += f" Not deleted: {problems}"
            self._show_result(text, ok=True)
        else:
            self._show_result(f"Nothing was deleted. {problems}", ok=False)
