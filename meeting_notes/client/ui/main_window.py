"""The recorder window: start, stop, and proof that it is working."""

from __future__ import annotations

import logging
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QFileDialog,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from meeting_notes import config as config_mod
from meeting_notes import __version__
from meeting_notes.client.controller import IDLE, RECORDING, RecordingController
from meeting_notes.client import authcheck, meeting_detect, paths, retention
from meeting_notes.client.update import ClientUpdater, UpdateManifest
from meeting_notes.client.ui.meeting_prompt import MeetingPrompt
from meeting_notes.client.ui.settings_dialog import SettingsDialog
from meeting_notes.client.ui.history_dialog import HistoryDialog
from meeting_notes.client.ui.reupload_dialog import ReuploadDialog
from meeting_notes.client.ui.logs_dialog import LogsDialog
from meeting_notes.client.ui import theme
from meeting_notes.client.ui.theme import install_titlebar
from meeting_notes.client.ui.icons import icon_size, make_icon
from meeting_notes.client.ui.waveform import WaveformWidget



log = logging.getLogger("meeting_notes.client.ui")

AUTH_RECHECK_MS = 5 * 60 * 1000  # how often an idle client re-verifies its token
# Local-recording clean-up (only does anything when a keep period is chosen in
# Settings): first look shortly after start-up, then every six hours.
RETENTION_STARTUP_DELAY_MS = 2 * 60 * 1000
RETENTION_INTERVAL_MS = 6 * 60 * 60 * 1000

_AUTH_TEXT = re.compile(r"(?i)\b40[13]\b|unauthori[sz]ed|forbidden|check the token|rejected the token")
_UNREACHABLE_TEXT = re.compile(
    r"(?i)ServerUnavailable|refused|10061|timed out|unreachable|getaddrinfo|no route|connect"
)


def _is_auth_text(text) -> bool:
    return bool(text) and bool(_AUTH_TEXT.search(str(text)))


def _is_unreachable_text(text) -> bool:
    return bool(text) and bool(_UNREACHABLE_TEXT.search(str(text)))


def _meetings_waiting(count: int) -> str:
    return f"{count} meeting is" if count == 1 else f"{count} meetings are"


def _short_upload_error(error: str) -> str:
    """One readable clause from an upload error, for the status line.

    The raw text is an exception repr with a URL in it; the person just
    needs to know it's the token, or that the server is down.
    """
    text = str(error)
    if "401" in text or "403" in text:
        return "server rejected the token (check Settings)"
    if "ServerUnavailable" in text or "10061" in text or "refused" in text:
        return "server unreachable"
    first = text.splitlines()[0] if text else ""
    return first[:60] + ("..." if len(first) > 60 else "")


def _hms(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


class _AsyncBridge(QObject):
    """Marshals one background-thread result onto the Qt event loop.

    ``Signal`` has to be a class attribute of a ``QObject`` subclass, which
    is the only reason this class exists -- the thread-safety itself comes
    for free from Qt: a signal emitted from any thread and connected with the
    default (queued) connection type runs its slot on the receiver's own
    thread, so ``done`` firing from a plain ``threading.Thread`` still lands
    ``on_done`` safely back on the GUI thread.
    """

    done = Signal(object)


class MainWindow(QWidget):
    def __init__(self, controller: RecordingController = None):
        super().__init__()
        self.controller = controller or RecordingController()
        self.setObjectName("root")
        self.setWindowTitle("Meeting Notes")
        self.setMinimumSize(720, 560)
        install_titlebar(self)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # -- header -----------------------------------------------------------
        # Hierarchy: the app name on the left; Upload, History and Settings are
        # quiet ghost buttons; the rarely used device/diagnostic actions live in
        # "More".
        topbar = QFrame()
        topbar.setObjectName("topbar")
        topbar_row = QHBoxLayout(topbar)
        topbar_row.setContentsMargins(20, 0, 20, 0)
        topbar_row.setSpacing(0)
        topbar_inner = QWidget()
        topbar_inner.setMaximumWidth(1560)
        header = QHBoxLayout(topbar_inner)
        header.setContentsMargins(0, 10, 0, 10)
        header.setSpacing(4)
        topbar_row.addStretch(1)
        topbar_row.addWidget(topbar_inner, 100)
        topbar_row.addStretch(1)
        title = QLabel("Meeting Notes")
        title.setObjectName("brand")
        header.addWidget(title)
        header.addSpacing(6)
        self.version_label = QLabel(f"v{__version__}")
        self.version_label.setObjectName("version")
        header.addWidget(self.version_label, 0, Qt.AlignVCenter)
        header.addStretch(1)

        self.update_button = QPushButton("Update available")
        self.update_button.setObjectName("update")
        self.update_button.setToolTip("Download and install the newer client from the configured server")
        self.update_button.clicked.connect(self._request_update)
        self.update_button.setVisible(False)
        self.upload_button = QPushButton("Upload recording")
        self.upload_button.setToolTip("Send an existing audio file to the server for transcription")
        self.upload_button.clicked.connect(self._open_recording_upload)
        self.history_button = QPushButton("History")
        self.history_button.clicked.connect(self._open_history)
        self.settings_button = QPushButton("Settings")
        self.settings_button.clicked.connect(self._open_settings)
        self._header_icons = (
            (self.upload_button, "upload"),
            (self.history_button, "history"),
            (self.settings_button, "settings"),
        )
        for button, _glyph in self._header_icons:
            button.setObjectName("tool")
            button.setIconSize(icon_size())
            button.setCursor(Qt.PointingHandCursor)

        # The secondary actions are QActions in a compact menu. The old
        # attribute names still point at them so callers keep working.
        self.folder_button = QAction("Open recordings folder", self)
        self.folder_button.triggered.connect(self._open_folder)
        self.reupload_action = QAction("Re-upload a saved recording...", self)
        self.reupload_action.setToolTip(
            "Send recordings kept on this computer to the server again, for example after a meeting was deleted there"
        )
        self.reupload_action.triggered.connect(self._open_reupload)
        self.refresh_audio_button = QAction("Refresh audio devices", self)
        self.refresh_audio_button.setToolTip("Re-scan microphones and speakers")
        self.refresh_audio_button.triggered.connect(self._refresh_devices)
        # ``audio_log_button`` is the historical name; it now opens the Logs window,
        # which lists the audio diagnostic among the other sources.
        self.audio_log_button = QAction("Logs...", self)
        self.audio_log_button.setToolTip(
            "Client log, audio devices, upload queue and configuration; save or send them"
        )
        self.audio_log_button.triggered.connect(self._open_logs)
        self._menu_icons = (
            (self.folder_button, "folder"),
            (self.reupload_action, "upload"),
            (self.refresh_audio_button, "refresh"),
            (self.audio_log_button, "logs"),
        )
        self.more_menu = QMenu(self)
        self.more_menu.addAction(self.folder_button)
        self.more_menu.addAction(self.reupload_action)
        self.more_menu.addAction(self.refresh_audio_button)
        self.more_menu.addAction(self.audio_log_button)
        self.more_button = QToolButton()
        self.more_button.setObjectName("more")
        self.more_button.setIconSize(icon_size(20))
        self.more_button.setToolTip("More: recordings folder, re-upload, audio devices, logs")
        self.more_button.setAccessibleName("More actions")
        self.more_button.setMenu(self.more_menu)
        self.more_button.setPopupMode(QToolButton.InstantPopup)
        self.more_button.setFocusPolicy(Qt.StrongFocus)
        self.more_button.setCursor(Qt.PointingHandCursor)

        header.addWidget(self.upload_button)
        header.addWidget(self.history_button)
        header.addWidget(self.settings_button)
        header.addWidget(self.more_button)
        outer.addWidget(topbar)

        # The body is a centred column: on a 1920 or ultrawide screen the content
        # keeps a readable width instead of stretching edge to edge.
        body = QWidget()
        body.setObjectName("root")
        body_row = QHBoxLayout(body)
        body_row.setContentsMargins(20, 16, 20, 14)
        body_row.setSpacing(0)
        content = QWidget()
        content.setMaximumWidth(1560)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        body_row.addStretch(1)
        body_row.addWidget(content, 100)
        body_row.addStretch(1)
        outer.addWidget(body, 1)

        # Inline banners directly under the header: a rejected token is an error
        # (red); an unreachable server or a recordings folder that an update could
        # wipe is a warning (amber).
        self._strip_icons: list = []
        (self.alert_bar, self.alert_label, self.alert_button) = self._make_strip(
            "alertBar", "alert-circle", "danger_text", "Fix in Settings", self._open_settings
        )
        (self.folder_bar, self.folder_label, self.move_button) = self._make_strip(
            "warnBar", "alert", "warn_icon", "Move recordings", self._move_recordings
        )
        (self.warn_bar, self.warn_label, self.warn_button) = self._make_strip(
            "warnBar", "alert", "warn_icon", "", None
        )
        self.warn_button.setVisible(False)
        for strip in (self.alert_bar, self.folder_bar, self.warn_bar):
            strip.setVisible(False)
            layout.addWidget(strip)

        # A newer client is announced in its own info banner, so it is prominent
        # without crowding the header at the minimum window width.
        self.update_bar = QFrame()
        self.update_bar.setObjectName("updateBar")
        update_row = QHBoxLayout(self.update_bar)
        update_row.setContentsMargins(14, 8, 8, 8)
        update_row.setSpacing(10)
        update_icon = QLabel()
        update_icon.setFixedSize(20, 20)
        self._strip_icons.append((update_icon, "info", "accent_text"))
        update_row.addWidget(update_icon, 0, Qt.AlignVCenter)
        self.update_note = QLabel("A newer Meeting Notes is ready on your server.")
        self.update_note.setObjectName("updateNote")
        update_row.addWidget(self.update_note, 1)
        update_row.addWidget(self.update_button)
        self.update_bar.setVisible(False)
        layout.addWidget(self.update_bar)

        # -- recording card: clock, devices, name, start/stop --------------------
        card = QFrame()
        card.setObjectName("recordCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(20, 14, 20, 18)
        card_layout.setSpacing(12)

        status = QHBoxLayout()
        status.setSpacing(14)
        self.clock = QLabel("00:00:00")
        self.clock.setObjectName("clock")
        self.clock.setAccessibleName("Elapsed recording time")
        # Inter's tabular figures keep the digits from jittering as seconds tick.
        clock_font = self.clock.font()
        theme.enable_tabular(clock_font)
        self.clock.setFont(clock_font)
        status.addWidget(self.clock, 0, Qt.AlignVCenter)
        status.addStretch(1)
        self.devices_label = QLabel("")
        self.devices_label.setObjectName("devices")
        self.devices_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        status.addWidget(self.devices_label)
        card_layout.addLayout(status)

        controls = QHBoxLayout()
        controls.setSpacing(12)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Meeting name (optional)")
        self.name_edit.setAccessibleName("Meeting name")
        self.name_edit.setMinimumHeight(40)
        controls.addWidget(self.name_edit, 1)
        self.record_button = QPushButton("Start recording")
        self.record_button.setObjectName("record")
        self.record_button.setMinimumWidth(170)
        self.record_button.setMinimumHeight(40)
        self.record_button.setIconSize(icon_size(18))
        self.record_button.setCursor(Qt.PointingHandCursor)
        self.record_button.clicked.connect(self._toggle)
        controls.addWidget(self.record_button)
        card_layout.addLayout(controls)
        layout.addWidget(card)

        # Muting consumes audio normally and writes aligned silence for only
        # the selected source.  The other recorder and the live preview remain
        # connected, so a user can mute one side of a call without stopping
        # the meeting.
        self.mute_mic_button = QPushButton("Mute you")
        self.mute_mic_button.setObjectName("mute_mic")
        self.mute_mic_button.setAccessibleName("Mute your microphone")
        self.mute_mic_button.setCheckable(True)
        self.mute_mic_button.setEnabled(False)
        self.mute_mic_button.toggled.connect(
            lambda checked: self._toggle_source_mute("mic", checked)
        )
        self.mute_system_button = QPushButton("Mute them")
        self.mute_system_button.setObjectName("mute_system")
        self.mute_system_button.setAccessibleName("Mute system audio")
        self.mute_system_button.setCheckable(True)
        self.mute_system_button.setEnabled(False)
        self.mute_system_button.toggled.connect(
            lambda checked: self._toggle_source_mute("system", checked)
        )

        # -- level meters -----------------------------------------------------
        # Keep each mute control on the same horizontal band as the meter row it
        # affects, so a recording source and its control read together.
        waveform_controls = QHBoxLayout()
        # No inset: the rows and the mute column share the recording card's left
        # and right edges exactly.
        waveform_controls.setContentsMargins(0, 0, 0, 0)
        waveform_controls.setSpacing(10)
        self.mute_mic_button.setFixedWidth(108)
        self.mute_system_button.setFixedWidth(108)
        self.waveform = WaveformWidget()
        waveform_controls.addWidget(self.waveform, 1)
        mute_controls = QVBoxLayout()
        mute_controls.setContentsMargins(0, 0, 0, 0)
        mute_controls.setSpacing(0)
        mute_controls.addStretch(1)
        mute_controls.addWidget(self.mute_mic_button)
        mute_controls.addStretch(2)
        mute_controls.addWidget(self.mute_system_button)
        mute_controls.addStretch(1)
        waveform_controls.addLayout(mute_controls)
        track_bed = QWidget()
        track_bed.setLayout(waveform_controls)
        track_bed.setMaximumHeight(300)
        layout.addWidget(track_bed, 3)

        # -- live preview: a plain panel ------------------------------------------
        preview_label = QLabel("Live preview")
        preview_label.setObjectName("section")
        layout.addWidget(preview_label)
        self.preview = QPlainTextEdit()
        self.preview.setObjectName("preview")
        self.preview.setAccessibleName("Live preview transcript")
        self.preview.setReadOnly(True)
        self.preview.setMinimumHeight(64)
        self.preview.setPlaceholderText(
            "A rough live transcript appears here while recording. The transcript you "
            "keep is made from the full recording after the meeting."
        )
        layout.addWidget(self.preview, 2)

        # -- footer -------------------------------------------------------------
        self.status_label = QLabel("")
        self.status_label.setObjectName("status")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self._record_state = "idle"
        self._set_record_look("idle")
        self._apply_theme_icons()
        theme.manager().changed.connect(lambda _name: self._apply_theme_icons())

        self._seen_partials = 0
        # A polled timer for the cheap, frequent stuff: the capture and network
        # threads simply publish state (levels, elapsed time, partials), and the
        # UI samples it here. Nothing they do can block or crash the event loop.
        #
        # The exception is teardown -- controller.stop(), UploadWorker.stop() --
        # which joins background threads for real seconds (recorder/streamer/
        # upload-worker shutdown), long enough to freeze the window if run
        # directly on this thread. Those go through _run_async instead: a
        # plain thread does the joining, and an _AsyncBridge signal delivers
        # the result back here once it's done.
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(33)
        self._pending_close = False
        self._teardown_done = False
        self._async_bridges: List[_AsyncBridge] = []  # kept alive until each fires once
        self._update_manifest: Optional[UpdateManifest] = None
        self._update_updater: Optional[ClientUpdater] = None
        self._update_check_started = False
        self._update_installing = False
        self._verified_update_path: Optional[Path] = None
        self._uploading_recording = False
        # Meeting detection: a slow poll of cheap Windows probes. Only created
        # on Windows; elsewhere the feature is inert.
        self._auto_session = False
        self._auto_stop_note = ""
        self._prompt: Optional[MeetingPrompt] = None
        self._detect_settings = config_mod.meeting_detection_settings()
        self._detector = self._create_meeting_detector()
        self._detect_timer = QTimer(self)
        self._detect_timer.timeout.connect(self._poll_meeting)
        if self._detector is not None:
            self._detect_timer.start(2000)
        # Token / connectivity state. The checker is injectable for tests.
        self._auth_checker = authcheck.check_connection
        self._auth_state = "unknown"  # unknown | ok | rejected | unreachable
        self._auth_check_running = False
        self._auth_checked_at = 0.0
        self._alert_was_visible = False
        self._moving_recordings = False
        self._auth_timer = QTimer(self)
        self._auth_timer.timeout.connect(self._periodic_auth_check)
        self._auth_timer.start(AUTH_RECHECK_MS)
        self._retention_running = False
        self._retention_timer = QTimer(self)
        self._retention_timer.timeout.connect(self._run_retention)
        self._retention_timer.start(RETENTION_INTERVAL_MS)
        QTimer.singleShot(RETENTION_STARTUP_DELAY_MS, self._run_retention)
        self._refresh_devices()
        # Started with the window: a meeting recorded while the server was
        # down must upload next time the app opens, without needing another
        # recording to trigger it.
        self.controller.start_uploader()
        self._update_status()
        # Checking is asynchronous and only happens when a server is
        # configured. This keeps startup responsive and makes a server outage
        # indistinguishable from an ordinary offline recording session.
        QTimer.singleShot(0, self._check_for_update)
        QTimer.singleShot(0, lambda: self._start_auth_check("startup"))
        self._refresh_folder_strip()

    def closeEvent(self, event):  # noqa: N802 - Qt naming
        if self._teardown_done:
            # Second time through (see _on_close_ready below): the blocking
            # work is done, so let Qt actually close the window now.
            super().closeEvent(event)
            return
        if self._pending_close:
            # A previous close request is still finishing (stop() and/or
            # stop_uploader() joining their threads) -- let it, rather than
            # starting a second one on top of it.
            event.ignore()
            return
        self._pending_close = True
        self._detect_timer.stop()
        self._close_prompt()
        recording = self.controller.state == RECORDING
        if recording:
            self.record_button.setEnabled(False)
            self.record_button.setText("Finishing...")
            self._set_record_look("finishing")
            self.status_label.setText("Finishing the recording before closing...")

        def work():
            # Both calls can block for real seconds (thread joins) -- see the
            # comment by self._timer in __init__ -- so this whole function
            # runs off the GUI thread via _run_async, not directly here.
            meta = self.controller.stop() if recording else None
            self.controller.stop_uploader()
            return meta

        self._run_async(work, self._on_close_ready)
        event.ignore()  # accepted on the next close(), once teardown is done

    def _on_close_ready(self, meta) -> None:
        self._pending_close = False
        self._teardown_done = True
        if isinstance(meta, Exception):
            # controller.stop()/stop_uploader() are themselves written to
            # never raise; this only guards _run_async's own contract.
            meta = None
        if meta is not None:
            self._apply_stopped_ui(meta)
        self.close()  # re-enters closeEvent, which now takes the "done" branch above

    def _make_strip(self, name: str, glyph: str, colour_key: str, button_text: str, on_click):
        strip = QFrame()
        strip.setObjectName(name)
        row = QHBoxLayout(strip)
        row.setContentsMargins(14, 8, 8, 8)
        row.setSpacing(10)
        icon_label = QLabel()
        icon_label.setFixedSize(20, 20)
        self._strip_icons.append((icon_label, glyph, colour_key))
        row.addWidget(icon_label, 0, Qt.AlignVCenter)
        label = QLabel("")
        label.setWordWrap(True)
        row.addWidget(label, 1)
        button = QPushButton(button_text)
        if on_click is not None:
            button.clicked.connect(on_click)
        row.addWidget(button)
        return strip, label, button

    def _apply_theme_icons(self) -> None:
        """(Re)draw every icon in the active theme's colours."""
        for button, glyph in self._header_icons:
            button.setIcon(make_icon(glyph))
        for action, glyph in self._menu_icons:
            action.setIcon(make_icon(glyph))
        self.more_button.setIcon(make_icon("more"))
        tokens = theme.tokens()
        for label, glyph, key in self._strip_icons:
            label.setPixmap(make_icon(glyph, tokens[key], tokens[key], 20).pixmap(20, 20))
        self._set_record_look(self._record_state)
        theme.refresh_titlebars()

    # -- token / connection alerts ---------------------------------------------

    def _start_auth_check(self, reason: str = "") -> None:
        """Probe the server with the configured token, off the GUI thread."""
        server = config_mod.server_settings()
        url = (server.get("url") or "").strip()
        if not url:
            self._auth_state = "unknown"
            return
        if self._auth_check_running:
            return
        self._auth_check_running = True
        token = server.get("token") or ""
        checker = self._auth_checker
        log.info("auth check (%s)", reason or "requested")
        self._run_async(lambda: checker(url, token), self._on_auth_checked)

    def _periodic_auth_check(self) -> None:
        if self.controller.state == IDLE and not self._pending_close:
            self._start_auth_check("periodic")

    def _on_auth_checked(self, result) -> None:
        import time as _time

        self._auth_check_running = False
        self._auth_checked_at = _time.monotonic()
        if isinstance(result, Exception):
            return
        if result.status == authcheck.OK:
            self._auth_state = "ok"
        elif result.status == authcheck.REJECTED:
            self._auth_state = "rejected"
        elif result.status == authcheck.UNREACHABLE:
            self._auth_state = "unreachable"
        elif result.status == authcheck.NO_SERVER:
            self._auth_state = "unknown"
        # Any other outcome (odd HTTP status) tells us nothing about the token.
        self._refresh_alerts()

    def _refresh_alerts(self) -> None:
        """Show or clear the red token strip and the kraft unreachable strip."""
        try:
            queue = self.controller.queue_status() or {}
        except Exception:  # noqa: BLE001
            queue = {}
        waiting = int(queue.get("pending", 0) or 0) + int(queue.get("failed", 0) or 0)
        last_error = queue.get("last_error", "")
        stream_error = None
        if self.controller.state == RECORDING:
            try:
                stream_error = self.controller.stream_error()
            except Exception:  # noqa: BLE001
                stream_error = None

        queue_rejected = _is_auth_text(last_error)
        stream_rejected = _is_auth_text(stream_error)
        rejected = self._auth_state == "rejected" or (
            self._auth_state != "ok" and (queue_rejected or stream_rejected)
        )
        # A real 401/403 from the uploader while the last check said "ok" means the
        # token changed on the server since: verify right away instead of in 5 min.
        if queue_rejected and self._auth_state == "ok" and not self._auth_check_running:
            self._start_auth_check("upload was rejected")

        if rejected:
            text = "The server rejected your token."
            if waiting:
                text += f" {_meetings_waiting(waiting).capitalize()} waiting to upload."
            else:
                text += " Uploads and the live preview stay off until it is fixed."
        else:
            text = ""
        if text != self.alert_label.text():
            self.alert_label.setText(text)
        self.alert_bar.setVisible(rejected)
        if rejected and not self._alert_was_visible:
            log.warning("token rejected by the server; %d meetings waiting", waiting)
            QApplication.alert(self)
        if not rejected and self._alert_was_visible:
            log.info("token alert cleared")
        self._alert_was_visible = rejected

        unreachable = (
            not rejected
            and waiting > 0
            and self._auth_state != "ok"
            and (self._auth_state == "unreachable" or _is_unreachable_text(last_error))
        )
        warn_text = (
            f"Can't reach the server. {_meetings_waiting(waiting).capitalize()} waiting and will upload when it's back."
            if unreachable
            else ""
        )
        if warn_text != self.warn_label.text():
            self.warn_label.setText(warn_text)
        self.warn_bar.setVisible(unreachable)

    # -- recordings folder inside the app folder ---------------------------------

    def _refresh_folder_strip(self) -> None:
        folder = paths.inside_app_folder(config_mod.save_dir())
        if folder is None or self._moving_recordings:
            self.folder_bar.setVisible(False)
            return
        target = config_mod.DEFAULT_SAVE_DIR
        self.folder_label.setText(
            f"Your recordings folder is inside the app folder ({folder}), which an update "
            f"replaces, and could delete your meetings. Move them to {target}?"
        )
        self.folder_bar.setVisible(True)
        log.warning("save folder %s is inside the app folder %s", config_mod.save_dir(), folder)

    def _move_recordings(self) -> None:
        if self.controller.state != IDLE:
            self.status_label.setText("Stop recording before moving your recordings.")
            return
        source = config_mod.save_dir()
        destination = Path(config_mod.DEFAULT_SAVE_DIR)
        self._moving_recordings = True
        self.move_button.setEnabled(False)
        self.status_label.setText(f"Moving recordings to {destination}...")
        log.info("moving recordings from %s to %s", source, destination)

        def work():
            self.controller.stop_uploader()
            count = paths.move_recordings(source, destination)
            cfg = config_mod.load_config()
            cfg["save_dir"] = str(destination)
            config_mod.save_config(cfg)
            self.controller.restart_uploader()
            return count

        self._run_async(work, self._on_recordings_moved)

    def _on_recordings_moved(self, result) -> None:
        self._moving_recordings = False
        self.move_button.setEnabled(True)
        if isinstance(result, Exception):
            log.error("moving recordings failed: %s", result)
            self.status_label.setText(f"Could not move recordings: {result}. Nothing was deleted.")
            return
        self.folder_bar.setVisible(False)
        self.status_label.setText(f"Moved {result} files to {config_mod.DEFAULT_SAVE_DIR}.")

    def _set_record_look(self, state: str) -> None:
        """Accent "Start recording" while idle; destructive red while recording.

        Qt does not re-evaluate #id selectors when objectName changes, so the
        button is repolished; the icon and the clock's lit state follow.
        """
        self._record_state = state
        recording = state in ("recording", "finishing")
        tokens = theme.tokens()
        self.record_button.setObjectName("recording" if recording else "record")
        if state == "recording":
            self.record_button.setIcon(make_icon("stop", "#ffffff", tokens["icon_disabled"], 18))
        elif state == "finishing":
            self.record_button.setIcon(make_icon("stop", tokens["icon_disabled"], tokens["icon_disabled"], 18))
        else:
            self.record_button.setIcon(make_icon("mic", "#ffffff", tokens["icon_disabled"], 18))
        self.clock.setProperty("live", "true" if recording else "false")
        self._restyle(self.record_button)
        self._restyle(self.clock)

    @staticmethod
    def _restyle(widget) -> None:
        """Qt does not re-evaluate #id selectors when objectName changes."""
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    def _run_async(self, work: Callable[[], object], on_done: Callable[[object], None]) -> None:
        """Run ``work`` on a plain background thread; deliver its result (or
        exception) to ``on_done`` back on the GUI thread.

        Exists for controller/uploader calls that join background threads --
        controller.stop(), controller.stop_uploader(), controller.restart_uploader()
        -- and so can block for several real seconds. Calling them directly
        from a slot freezes the window ("Not Responding") for that long; this
        keeps the join off the GUI thread while still landing the follow-up
        UI update safely on it, via Qt's automatic queued connection for a
        signal emitted from another thread.
        """
        bridge = _AsyncBridge()
        self._async_bridges.append(bridge)  # keep it alive until it fires

        def _deliver(result: object) -> None:
            self._async_bridges.remove(bridge)
            on_done(result)

        bridge.done.connect(_deliver)

        def runner() -> None:
            try:
                result = work()
            except Exception as exc:  # noqa: BLE001 - deliver the failure, don't drop it silently
                result = exc
            bridge.done.emit(result)

        threading.Thread(target=runner, daemon=True, name="ui-async").start()

    # -- actions --------------------------------------------------------------

    def _toggle(self) -> None:
        # Any manual start/stop makes the recording the user's own: it is
        # never stopped automatically when a call ends.
        self._auto_session = False
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
        self._set_record_look("recording")
        for lane in ("mic", "system"):
            self.waveform.set_track_muted(lane, False)
            self.waveform.set_track_active(lane, True)
        for track, button in (("mic", self.mute_mic_button), ("system", self.mute_system_button)):
            button.blockSignals(True)
            button.setChecked(False)
            button.blockSignals(False)
            button.setText("Mute you" if track == "mic" else "Mute them")
            button.setAccessibleName("Mute your microphone" if track == "mic" else "Mute system audio")
            button.setEnabled(bool(getattr(self.controller.session, "recorders", {}).get(track)))

    def _stop(self) -> None:
        # controller.stop() joins the supervisor thread, the recorder threads
        # and the live streamer -- several seconds combined -- so it runs off
        # the GUI thread; see the comment by self._timer in __init__. The
        # button stays disabled/"Finishing..." (set here, synchronously, so it
        # actually paints before the join starts) until _on_stop_finished
        # fires.
        self.record_button.setEnabled(False)
        self.record_button.setText("Finishing...")
        self._set_record_look("finishing")
        self.status_label.setText("Finishing up...")
        self._run_async(self.controller.stop, self._on_stop_finished)

    def _on_stop_finished(self, meta) -> None:
        if isinstance(meta, Exception):
            meta = None  # controller.stop() never raises; guards _run_async's own contract
        self._apply_stopped_ui(meta)

    def _apply_stopped_ui(self, meta) -> None:
        self._auto_session = False
        note, self._auto_stop_note = self._auto_stop_note, ""
        self.waveform.set_recording(False)
        self.record_button.setEnabled(True)
        self.record_button.setText("Start recording")
        self._set_record_look("idle")
        for lane in ("mic", "system"):
            self.waveform.set_track_muted(lane, False)
            self.waveform.set_track_active(lane, True)
        for track, button in (("mic", self.mute_mic_button), ("system", self.mute_system_button)):
            button.blockSignals(True)
            button.setChecked(False)
            button.blockSignals(False)
            button.setText("Mute you" if track == "mic" else "Mute them")
            button.setAccessibleName("Mute your microphone" if track == "mic" else "Mute system audio")
            button.setEnabled(False)
        if meta:
            where = self.controller.session_dir
            self.status_label.setText(
                f"Saved {_hms(meta.get('duration_sec') or 0)} to {where}. "
                "Queued for transcription."
            )
            if note:
                self.status_label.setText(f"{note} {self.status_label.text()}")
        self._maybe_auto_update()

    def _open_settings(self) -> None:
        if SettingsDialog(self).exec():
            self._apply_meeting_settings()
            self._refresh_devices()
            # restart_uploader() can block for up to UploadWorker's stop()
            # join_timeout (5s) if an upload is in flight -- same freeze risk
            # as controller.stop(), so it gets the same async treatment. The
            # settings button is disabled meanwhile so a second click can't
            # start an overlapping restart.
            self.settings_button.setEnabled(False)
            self.status_label.setText("Applying settings...")
            self._run_async(self.controller.restart_uploader, self._on_uploader_restarted)

    def _open_history(self) -> None:
        HistoryDialog(self).exec()

    def _open_reupload(self) -> None:
        """Choose saved recordings and put them back on the upload queue."""
        dialog = ReuploadDialog(
            self,
            save_dir=config_mod.save_dir(),
            queue=self.controller.session_queue(),
            submit=self.controller.reupload_recordings,
            server_configured=bool(config_mod.server_settings().get("url")),
        )
        dialog.exec()
        result = dialog.result
        if result is not None and result.total:
            self.status_label.setText(result.summary() + ".")
            self._refresh_alerts()

    # -- local recording clean-up ----------------------------------------------

    def _run_retention(self) -> None:
        """Apply the "keep recordings for N days" policy, only while idle.

        Never runs while recording or stopping, while a manual upload or a
        recordings move is in progress, or when no keep period is chosen.
        Everything slow (server checks, deleting folders) is off the GUI thread.
        """
        days = config_mod.local_retention_days()
        if (
            not days
            or self._retention_running
            or self._pending_close
            or self._uploading_recording
            or self._moving_recordings
            or self.controller.state != IDLE
        ):
            return
        server = config_mod.server_settings()
        if not server.get("url"):
            return
        self._retention_running = True
        save_dir = config_mod.save_dir()
        url, token = server["url"], server.get("token") or None
        self._run_async(
            lambda: retention.run_with_server(save_dir, days, url, token),
            self._on_retention_done,
        )

    def _on_retention_done(self, result) -> None:
        self._retention_running = False
        if isinstance(result, Exception):
            log.warning("local recording clean-up failed: %s: %s", type(result).__name__, result)

    def _on_uploader_restarted(self, result) -> None:
        self.settings_button.setEnabled(True)
        self._update_status()
        self._auth_state = "unknown"
        self._start_auth_check("settings saved")
        self._refresh_folder_strip()
        # Settings may have added or changed the configured server.
        self._check_for_update(force=True)

    def _open_folder(self) -> None:
        target = self.controller.session_dir or config_mod.save_dir()
        Path(target).expanduser().mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def _toggle_source_mute(self, track: str, muted: bool) -> None:
        """Apply one mute toggle without touching the other recorder."""
        if not self.controller.set_source_muted(track, muted):
            button = self.mute_mic_button if track == "mic" else self.mute_system_button
            button.blockSignals(True)
            button.setChecked(False)
            button.blockSignals(False)
            return
        button = self.mute_mic_button if track == "mic" else self.mute_system_button
        if track == "mic":
            self.waveform.set_track_muted("mic", muted)
            button.setText("Unmute you" if muted else "Mute you")
            button.setAccessibleName("Unmute your microphone" if muted else "Mute your microphone")
        else:
            self.waveform.set_track_muted("system", muted)
            button.setText("Unmute them" if muted else "Mute them")
            button.setAccessibleName("Unmute system audio" if muted else "Mute system audio")

    def _open_recording_upload(self) -> None:
        """Choose an existing recording and upload it off the GUI thread."""
        server = config_mod.server_settings()
        if not server.get("url"):
            self.status_label.setText("Cannot upload: configure a server in Settings first.")
            return
        path_text, _ = QFileDialog.getOpenFileName(
            self,
            "Choose a recording",
            str(config_mod.save_dir()),
            "Audio recordings (*.wav *.mp3 *.m4a *.mp4 *.flac *.ogg *.oga *.opus *.aac *.webm);;All files (*)",
        )
        if not path_text:
            return
        path = Path(path_text)
        self.upload_button.setEnabled(False)
        self._uploading_recording = True
        self.status_label.setText(f"Uploading {path.name}...")

        def work():
            from meeting_notes.client.api import ServerClient, UPLOAD_TIMEOUT

            with ServerClient(server["url"], server.get("token") or None, timeout=UPLOAD_TIMEOUT) as client:
                return client.upload_recording(path)

        self._run_async(work, lambda result: self._on_recording_uploaded(result, path.name))

    def _on_recording_uploaded(self, result, filename: str) -> None:
        self._uploading_recording = False
        self.upload_button.setEnabled(True)
        if isinstance(result, Exception):
            self.status_label.setText(f"Could not upload {filename}: {_short_upload_error(result)}")
            return
        job_id = result.get("job_id") if isinstance(result, dict) else None
        suffix = f" (job {job_id})" if job_id else ""
        self.status_label.setText(f"Uploaded {filename}; server transcription queued{suffix}.")

    # -- meeting detection ----------------------------------------------------

    def _create_meeting_detector(self):
        if sys.platform != "win32" or os.environ.get("MEETING_NOTES_NO_DETECT"):
            return None
        return meeting_detect.MeetingDetector(
            own_executable=sys.executable,
            end_grace_sec=self._detect_settings["end_grace_sec"],
        )

    def _apply_meeting_settings(self) -> None:
        self._detect_settings = config_mod.meeting_detection_settings()
        if self._detector is not None:
            self._detector.end_grace_sec = float(self._detect_settings["end_grace_sec"])
        if not self._detect_settings["enabled"]:
            self._close_prompt()

    def _poll_meeting(self) -> None:
        """Timer slot: nothing here may ever raise into the Qt event loop."""
        if self._detector is None:
            return
        try:
            for event in self._detector.poll(time.monotonic()):
                self._handle_meeting_event(event)
        except Exception:  # noqa: BLE001 - detection is best-effort
            pass

    def _handle_meeting_event(self, event) -> None:
        log.info("meeting detection: %s", event)
        if isinstance(event, meeting_detect.MeetingStarted):
            if not self._detect_settings["enabled"]:
                return
            if self.controller.state != IDLE or self._prompt is not None:
                return
            self._show_prompt(event.label, event.suggested_name)
        elif isinstance(event, meeting_detect.MeetingEnded):
            self._close_prompt()
            if (
                self._auto_session
                and self._detect_settings["auto_stop"]
                and self.controller.state == RECORDING
                and not self._pending_close
                and self.record_button.isEnabled()
            ):
                self._auto_stop_note = "Call ended — recording stopped and queued."
                log.info("meeting detection: call ended, auto-stopping the recording")
                self._stop()
                self.status_label.setText(self._auto_stop_note)

    def _show_prompt(self, label: str, name: str) -> None:
        prompt = MeetingPrompt(label, name)
        prompt.record_requested.connect(self._on_prompt_record)
        prompt.dismissed.connect(self._on_prompt_dismissed)
        self._prompt = prompt
        log.info("meeting detection: prompt shown for %s call %r", label, name)
        prompt.show_prompt()
        QApplication.alert(self)

    def _close_prompt(self) -> None:
        prompt, self._prompt = self._prompt, None
        if prompt is not None:
            prompt.close_silently()

    def _on_prompt_dismissed(self) -> None:
        # The detector announces each call once, so "Not now" needs no extra
        # bookkeeping: there is no second prompt until this call ends.
        log.info("meeting detection: prompt dismissed")
        self._prompt = None

    def _on_prompt_record(self, name: str) -> None:
        log.info("meeting detection: prompt accepted (%r)", name)
        self._prompt = None
        if self.controller.state != IDLE:
            return
        self.name_edit.setText(name)
        self._start()
        self._auto_session = self.controller.state == RECORDING

    # -- polling --------------------------------------------------------------

    def _refresh_devices(self) -> None:
        found = self.controller.probe_devices()
        mic = found.get("mic", "?")
        system = found.get("system", "?")
        self.devices_label.setText(f"You: {mic}\nThem: {system}")

    def _open_logs(self) -> None:
        LogsDialog(self).exec()

    def _tick(self) -> None:
        if self.controller.state == RECORDING:
            levels = self.controller.levels()
            self.waveform.push(levels)
            for track, degraded in self.controller.degraded().items():
                self.waveform.set_track_active(track, not degraded)
            self.clock.setText(_hms(self.controller.elapsed))
            self._drain_partials()
        self._update_status()
        self._refresh_alerts()

    def _drain_partials(self) -> None:
        partials = self.controller.partials()
        for item in partials[self._seen_partials :]:
            label = "You" if item.get("track") == "mic" else "Them"
            self.preview.appendPlainText(f"{label}: {item.get('text', '')}")
        self._seen_partials = len(partials)

    def _queue_note(self) -> str:
        q = self.controller.queue_status()
        bits = []
        if q.get("pending"):
            bits.append(f"{q['pending']} upload{'s' if q['pending'] != 1 else ''} pending")
        if q.get("failed"):
            bits.append(f"{q['failed']} failed")
        if bits and q.get("last_error"):
            bits.append(_short_upload_error(q["last_error"]))
        try:
            progress = self.controller.queue_progress()
        except Exception:
            progress = {}
        if progress:
            upload_state = progress.get("upload_state")
            if upload_state == "uploading":
                bits.append(f"uploading {progress.get('upload_percent', 0):.0f}%")
            elif upload_state == "pending" and not q.get("pending"):
                bits.append("upload pending")
            transcription_state = progress.get("transcription_state")
            if transcription_state == "transcribing":
                bits.append(
                    f"transcribing {progress.get('transcription_percent', 0):.0f}%"
                )
        return "  |  " + ", ".join(bits) if bits else ""

    def _update_status(self) -> None:
        if self.controller.state == RECORDING and self.controller.error:
            self.status_label.setText(
                f"Recording, but: {self.controller.error}{self._queue_note()}"
            )
            return
        if self.controller.state == RECORDING:
            stream = self.controller.stream_state()
            stream_error = self.controller.stream_error()
            if stream_error:
                # A permanent rejection (bad token, protocol mismatch) is a
                # different situation from "can't reach the server right
                # now" and says so specifically, rather than the generic
                # "unreachable" text -- that one goes away on its own once
                # the network comes back; this one won't until Settings
                # changes.
                note = stream_error
            else:
                note = {
                    "connected": "live preview connected",
                    "connecting": "connecting to server...",
                    "disconnected": "server unreachable; recording locally and will upload later",
                    "off": "live preview off",
                }.get(stream, stream)
            self.status_label.setText(f"Recording. {note}{self._queue_note()}")
        elif self.controller.state == IDLE and not self.status_label.text():
            server = config_mod.server_settings()
            where = server.get("url") or "not configured"
            self.status_label.setText(f"Ready. Server: {where}{self._queue_note()}")

    # -- client updates ------------------------------------------------------

    def _check_for_update(self, force: bool = False) -> None:
        """Check the configured server without ever blocking the Qt thread."""
        if self._update_check_started and not force:
            return
        server = config_mod.server_settings()
        url = (server.get("url") or "").strip()
        if not url or not server.get("check_updates", True):
            return
        self._update_check_started = True
        updater = ClientUpdater(url, server.get("token") or "")
        self._update_updater = updater
        self._run_async(updater.check, self._on_update_checked)

    def _on_update_checked(self, result) -> None:
        if isinstance(result, Exception):
            # Update checks are best-effort. A server being offline must never
            # turn into a warning that distracts from recording locally.
            return
        if result is None:
            return
        self._update_manifest = result
        log.info("update available: v%s", result.version)
        self.update_button.setText(f"Update to v{result.version}")
        self.update_button.setVisible(True)
        self.update_bar.setVisible(True)
        self._maybe_auto_update()

    def _maybe_auto_update(self) -> None:
        """Apply an opted-in update only after recording has become idle."""
        if self._update_manifest is None or self._update_installing:
            return
        if self.controller.state == RECORDING:
            self.update_button.setToolTip("Stop recording before installing this update")
            return
        server = config_mod.server_settings()
        if server.get("auto_update", False):
            self._begin_update(confirm=False)

    def _request_update(self) -> None:
        if self._update_manifest is None or self._update_installing:
            return
        if self.controller.state == RECORDING:
            QMessageBox.information(
                self,
                "Recording in progress",
                "The update is ready, but it will not interrupt your active recording. "
                "Stop recording before installing it.",
            )
            return
        self._begin_update(confirm=True)

    def _begin_update(self, *, confirm: bool) -> None:
        manifest = self._update_manifest
        updater = self._update_updater
        if manifest is None or updater is None:
            return
        if confirm:
            answer = QMessageBox.question(
                self,
                "Install client update",
                f"Download and install Meeting Notes v{manifest.version} from the configured server?\n\n"
                "Your recordings and server settings will be preserved.",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if answer != QMessageBox.Yes:
                return
        log.info("update: starting v%s", manifest.version)
        self._update_installing = True
        self.update_button.setEnabled(False)
        if self._verified_update_path is not None:
            # The artifact was downloaded while a recording was active. The
            # launch itself is tiny and happens only after this idle-state
            # check, so a recording can never be interrupted by an update.
            path = self._verified_update_path
            self._verified_update_path = None
            self._run_async(lambda: updater.apply(path), self._on_update_applied)
            return
        self.status_label.setText("Downloading and verifying the client update...")
        self._run_async(lambda: updater.download(manifest), self._on_update_downloaded)

    def _on_update_downloaded(self, result) -> None:
        log.info("update download finished: %s", result)
        if isinstance(result, Exception):
            self._update_installing = False
            self.update_button.setEnabled(True)
            self.status_label.setText(f"Client update failed: {result}")
            return
        # Do not launch an installer that could close the process while a
        # meeting began during the download. Keep the verified file and offer
        # it again once recording has finished.
        if self.controller.state == RECORDING:
            self._verified_update_path = Path(result)
            self._update_installing = False
            self.update_button.setEnabled(True)
            self.update_button.setToolTip("Verified update ready; stop recording to install it")
            self.status_label.setText("Update verified and ready; it will wait until recording stops.")
            return
        updater = self._update_updater
        if updater is None:
            self._update_installing = False
            self.update_button.setEnabled(True)
            return
        self._run_async(lambda: updater.apply(Path(result)), self._on_update_applied)

    def _on_update_applied(self, result) -> None:
        log.info("update apply finished: %s", result)
        self._update_installing = False
        if isinstance(result, Exception):
            self.update_button.setEnabled(True)
            self.status_label.setText(f"Client update failed: {result}")
            return
        self.update_button.setText("Update installer launched")
        self.status_label.setText(
            "The verified update installer was launched. Your recordings and settings were preserved."
        )
