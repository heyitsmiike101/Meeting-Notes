"""The recorder window: start, stop, and proof that it is working."""

from __future__ import annotations

import logging
import os
import re
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from PySide6.QtCore import QEvent, QObject, QProcess, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QFileDialog,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from meeting_notes import config as config_mod
from meeting_notes import __version__, remote
from meeting_notes.client.control_channel import ControlChannel, refusal_code
from meeting_notes.client.controller import (
    IDLE,
    RECORDING,
    STOPPING,
    RecordingController,
    missing_device_text,
)
from meeting_notes.client import authcheck, meeting_detect, paths, permissions, remote_recordings, retention, version_gate
from meeting_notes.client.update import ClientUpdater, UpdateManifest
from meeting_notes.client.ui.meeting_prompt import (
    AUTO_STOP_COUNTDOWN_SEC,
    CallEndingPrompt,
    MeetingPrompt,
    StopSuggestionPrompt,
)
from meeting_notes.client.ui.permissions_overlay import PermissionsOverlay
from meeting_notes.client.ui.settings_dialog import SettingsDialog
from meeting_notes.client.ui.upload_dialog import UploadDialog, UploadRequest
from meeting_notes.client.ui.history_dialog import HistoryDialog
from meeting_notes.client.ui.reupload_dialog import ReuploadDialog
from meeting_notes.client.ui import devicechange, theme
from meeting_notes.client.ui.theme import install_titlebar
from meeting_notes.client.ui.toast import Toast
from meeting_notes.client.ui.icons import icon_size, make_icon
from meeting_notes.client.ui.waveform import LANE_GAP, WaveformWidget
from meeting_notes.client.idle_meter import idle_meter_wanted



log = logging.getLogger("meeting_notes.client.ui")

# A system-audio peak (0..1 float) below this counts as silence (about -46 dBFS).
SYSTEM_SILENCE_PEAK = 0.005
# Both tracks silent (same threshold) this long during any recording -> suggest
# stopping. Covers meetings that are not recognised as calls (in person, an
# unrecognised app).
SILENCE_SUGGEST_SEC = 5 * 60

AUTH_RECHECK_MS = 5 * 60 * 1000  # how often an idle client re-verifies its password
# After a check that could not reach the server, look again sooner (a Local Network permission just
# granted, a network still coming up) before settling back to AUTH_RECHECK_MS.
AUTH_RETRY_MS = (10 * 1000, 30 * 1000)
# First-start prompt for the server password opens this long after the window shows.
FIRST_RUN_PROMPT_MS = 300
# If macOS never answers the microphone request, carry on with the rest of first run after this long.
FIRST_RUN_MIC_TIMEOUT_MS = 120_000
# Local-recording clean-up (only does anything when a keep period is chosen in
# Settings): first look shortly after start-up, then every six hours.
RETENTION_STARTUP_DELAY_MS = 2 * 60 * 1000
RETENTION_INTERVAL_MS = 6 * 60 * 60 * 1000
# How often a running client asks the server whether a newer client exists.
UPDATE_RECHECK_MS = 6 * 60 * 60 * 1000

UNSUPPORTED_TEXT = "This version is no longer supported by the server \u2014 update to keep uploading"

PASSWORD_NEEDED_TEXT = (
    "Enter the server password to connect. Meetings record but won't upload until it's set."
)
PASSWORD_REJECTED_TEXT = "The server rejected your password."

_AUTH_TEXT = re.compile(
    r"(?i)\b40[13]\b|unauthori[sz]ed|forbidden|check the (?:token|password)|rejected the (?:token|password)"
)
_UNREACHABLE_TEXT = re.compile(
    r"(?i)ServerUnavailable|refused|10061|timed out|unreachable|getaddrinfo|no route|connect"
)


def _is_auth_text(text) -> bool:
    return bool(text) and bool(_AUTH_TEXT.search(str(text)))


def _is_unreachable_text(text) -> bool:
    return bool(text) and bool(_UNREACHABLE_TEXT.search(str(text)))


def _meetings_waiting(count: int) -> str:
    return f"{count} meeting is" if count == 1 else f"{count} meetings are"


# How long a one-off message (a result, a notice) owns the status line while idle before the live
# status ("Ready. Server: ...") takes over again. Progress messages ("Uploading...") wait for their
# result instead, so they hold much longer.
STATUS_HOLD_SEC = 20.0
PROGRESS_HOLD_SEC = 600.0


def _short_upload_error(error: str) -> str:
    """One readable clause from an upload error, for the status line.

    The raw text is an exception repr with a URL in it; the person just
    needs to know it's the token, or that the server is down.
    """
    text = str(error)
    if "401" in text or "403" in text:
        return "server rejected the password (check Settings)"
    if "ServerUnavailable" in text or "10061" in text or "refused" in text:
        return "server unreachable"
    if (
        "PermissionError" in text
        or "WinError 5" in text
        or "Access is denied" in text
        or ".json.tmp" in text
        or "WinError 32" in text
    ):
        # A file the upload queue keeps was briefly locked (antivirus, sync client, a second
        # window). It is retried on its own; the raw path dump would only alarm.
        return "couldn't update the upload queue file; retrying"
    first = text.splitlines()[0] if text else ""
    return first[:60] + ("..." if len(first) > 60 else "")


def _upload_failure_text(error) -> str:
    """Why an upload failed, in a short sentence: the server's own reason when it gave one."""
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    if status in (404, 405):
        return "this server is too old to take it (update the server to 0.7.8 or newer)"
    if status is not None:
        try:
            detail = response.json().get("detail")
        except Exception:  # noqa: BLE001
            detail = None
        if isinstance(detail, str) and detail.strip():
            return detail.strip()[:160]
    return _short_upload_error(error)


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


class _RemoteBridge(QObject):
    """Carries a server command from the control channel's thread to the GUI thread."""

    command = Signal(str, str, object)
    notice = Signal(str)  # a toast from a worker thread (recordings commands)
    unauthorized = Signal()  # the control channel was refused for its password (4401 / 401)


# How long the state snapshot's "peak" looks back (seconds), and how often it is published.
REMOTE_PEAK_WINDOW_SEC = 1.0
REMOTE_PUBLISH_MS = 250

# Toast wording per command (see execute_remote_command).
_TOAST = {
    "start": "Recording started from the server",
    "stop": "Stopped from the server",
    "refresh_devices": "Devices refreshed from the server",
    "accept_call_prompt": "Recording started from the server",
    "dismiss_call_prompt": "Call prompt dismissed from the server",
    "keep_recording": "Keeping the recording, as asked from the server",
    "stop_suggested": "Stopped from the server",
    "retry_uploads": "Retrying uploads from the server",
    "install_update": "Update started from the server",
    "set_name": "Meeting renamed from the server",
}


class MainWindow(QWidget):
    def __init__(self, controller: RecordingController = None, *, remote_channel_factory=None):
        """``remote_channel_factory(on_command)`` returns the control channel (tests inject a fake)."""
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

        self.update_button = QPushButton("Update now")
        self.update_button.setObjectName("update")
        self.update_button.setToolTip("Download, verify and install the newer client from the configured server")
        self.update_button.clicked.connect(self._request_update)
        self.update_button.setVisible(False)
        self.upload_button = QPushButton("Upload")
        self.upload_button.setToolTip(
            "Send an audio recording to the server for transcription, or add a transcript you already have"
        )
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
        # No server password saved: a red strip from the first moment, with no network needed.
        (self.password_bar, self.password_label, self.password_button) = self._make_strip(
            "alertBar", "alert-circle", "danger_text", "Enter password", self._open_password_settings
        )
        self.password_label.setText(PASSWORD_NEEDED_TEXT)
        # macOS permissions still missing and the panel dismissed with "Not now".
        (self.perm_bar, self.perm_label, self.perm_button) = self._make_strip(
            "warnBar", "alert", "warn_icon", "Fix", self._show_permissions
        )
        # Audio-device banners come first: "you are not being recorded" is the
        # most urgent thing this window can say. The red one stays until the
        # device is back; the green "connected at ..." one fades after a while.
        (self.device_bar, self.device_label, _unused) = self._make_strip(
            "deviceAlert", "alert-circle", "on_accent", "", None
        )
        _unused.setVisible(False)
        (self.device_ok_bar, self.device_ok_label, _unused2) = self._make_strip(
            "okBar", "check-circle", "ok_text", "", None
        )
        _unused2.setVisible(False)
        self._device_ok_effect = QGraphicsOpacityEffect(self.device_ok_bar)
        self._device_ok_effect.setOpacity(1.0)
        self.device_ok_bar.setGraphicsEffect(self._device_ok_effect)
        for strip in (
            self.device_bar, self.device_ok_bar, self.password_bar, self.alert_bar, self.perm_bar,
            self.folder_bar, self.warn_bar,
        ):
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
        self.update_note = QLabel("Update available")
        self.update_note.setObjectName("updateNote")
        update_row.addWidget(self.update_note, 1)
        # "What's new" only exists when the server's manifest carries notes.
        self.whats_new_link = QLabel("")
        self.whats_new_link.setObjectName("updateLink")
        self.whats_new_link.setTextFormat(Qt.RichText)
        self.whats_new_link.setOpenExternalLinks(False)
        self.whats_new_link.linkActivated.connect(self._show_whats_new)
        self.whats_new_link.setVisible(False)
        update_row.addWidget(self.whats_new_link)
        update_row.addWidget(self.update_button)
        self.update_bar.setVisible(False)
        layout.addWidget(self.update_bar)

        # The server refused this client version (HTTP 426 or a manifest
        # minimum): a red strip with its own Update button, above everything.
        (self.unsupported_bar, self.unsupported_label, self.unsupported_button) = self._make_strip(
            "alertBar", "alert-circle", "danger_text", "Update now", self._request_update
        )
        self.unsupported_label.setText(UNSUPPORTED_TEXT)
        self.unsupported_button.setDefault(True)  # the one primary action on this strip
        self.unsupported_bar.setVisible(False)
        layout.insertWidget(0, self.unsupported_bar)

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
        # macOS permissions panel: covers the recorder card and everything below, leaving the
        # header and the strips above it usable.
        self.perm_overlay = PermissionsOverlay(body, anchor=card)
        self.perm_overlay.open_settings.connect(self._open_system_settings)
        self.perm_overlay.allow_microphone.connect(self._allow_microphone)
        self.perm_overlay.quit_and_reopen.connect(self._quit_and_reopen)
        self.perm_overlay.check_again.connect(self._permissions_check_again)
        self.perm_overlay.not_now.connect(self._permissions_not_now)

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
        # Each mute button fills exactly the height of its meter lane: the same
        # even split and the same gap the waveform paints its lanes with.
        mute_controls = QVBoxLayout()
        mute_controls.setContentsMargins(0, 0, 0, 0)
        mute_controls.setSpacing(LANE_GAP)
        for button in (self.mute_mic_button, self.mute_system_button):
            button.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Ignored)
            mute_controls.addWidget(button, 1)
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
        self._say_until = 0.0

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
        # Live input levels before recording (greyed "Preview"); the setting is read here and again after
        # Settings closes, never per tick.
        self._idle_levels_enabled = config_mod.idle_levels_enabled()
        self._idle_error_logged = False
        kinds = getattr(self.controller, "idle_kinds", ("mic", "system"))
        for lane in ("mic", "system"):
            if lane not in kinds:
                self.waveform.set_track_unavailable(
                    lane,
                    "System audio is not previewed on macOS (it needs screen-recording access). "
                    "It shows while recording.",
                )
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
        # Meeting detection: a slow poll of cheap OS probes. Only created on
        # Windows and macOS; elsewhere the feature is inert.
        self._auto_session = False
        self._auto_stop_note = ""
        self._prompt: Optional[MeetingPrompt] = None
        # End-of-call auto-stop needs several signals (see _check_end_pending).
        self._end_pending = False          # detector says the call ended; waiting for silence
        self._end_wait_logged = False
        self._auto_stop_kept = False       # user chose "Keep recording" for this recording
        self._end_prompt: Optional[CallEndingPrompt] = None
        # "Meeting seems over -- stop?" suggestion (any recording, never automatic).
        self._suggest_prompt: Optional[StopSuggestionPrompt] = None
        self._suggest_kind = ""            # "call-end" | "silence"
        self._suggest_kept = False         # Keep recording chosen for the current call
        self._silence_armed = True         # re-armed once audio resumes
        self._audio_last_active = time.monotonic()
        self._system_last_active = time.monotonic()
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
        self._auth_retry_index = 0
        self._auth_retry_timer = QTimer(self)
        self._auth_retry_timer.setSingleShot(True)
        self._auth_retry_timer.timeout.connect(lambda: self._start_auth_check("retry after unreachable"))
        # macOS permissions (injectable for tests; the probes never prompt).
        self._perm_enabled = sys.platform == "darwin"
        self._perm_probe = permissions.snapshot
        self._is_bundled = lambda: permissions.bundle_path() is not None
        self._perm_items: list = []
        self._perm_dismissed: frozenset = frozenset()
        self._net_error = ""  # the last server-contact failure text (macOS Local Network evidence)
        self._perm_bridge = _AsyncBridge(self)
        self._perm_bridge.done.connect(lambda _granted: self._refresh_permissions())
        # First-run permission prompts (macOS): mic, then screen recording, then the panel, then the password
        # Settings dialog. ``_perm_first_run_hold`` keeps the panel down until the prompts are done.
        self._first_run_bridge = _AsyncBridge(self)
        self._first_run_bridge.done.connect(lambda _granted: self._first_run_after_mic())
        self._perm_panel_open = False
        self._first_run_gate = False  # the password dialog waits for the panel to close
        self._first_run_pending = self._perm_first_run_due()
        self._perm_first_run_hold = self._first_run_pending
        self._alert_was_visible = False
        self._moving_recordings = False
        self._auth_timer = QTimer(self)
        self._auth_timer.timeout.connect(self._periodic_auth_check)
        self._auth_timer.start(AUTH_RECHECK_MS)
        self._retention_running = False
        self._retention_timer = QTimer(self)
        self._retention_timer.timeout.connect(self._run_retention)
        self._retention_timer.start(RETENTION_INTERVAL_MS)
        self._update_timer = QTimer(self)
        self._update_timer.timeout.connect(lambda: self._check_for_update(force=True))
        self._update_timer.start(UPDATE_RECHECK_MS)
        self._gate_check_started = False
        QTimer.singleShot(RETENTION_STARTUP_DELAY_MS, self._run_retention)
        self._refresh_devices()
        # Devices are then re-scanned in the background for as long as the app is
        # open (a headset switched on later just appears), and a Windows
        # device-change notification triggers an immediate re-scan. Tests set
        # MEETING_NOTES_NO_DEVICE_WATCH so no window polls real hardware.
        self._device_filter = None
        if not os.environ.get("MEETING_NOTES_NO_DEVICE_WATCH") and hasattr(self.controller, "start_device_watch"):
            try:
                self.controller.start_device_watch()
                self._device_filter = devicechange.install(
                    QApplication.instance(), self.controller.wake_device_watch
                )
            except Exception:  # noqa: BLE001 - automatic pickup is a convenience
                log.exception("could not start the device watcher")
        # Started with the window: a meeting recorded while the server was
        # down must upload next time the app opens, without needing another
        # recording to trigger it.
        self.controller.start_uploader()
        self._setup_remote(remote_channel_factory)
        self._update_status()
        # Checking is asynchronous and only happens when a server is
        # configured. This keeps startup responsive and makes a server outage
        # indistinguishable from an ordinary offline recording session.
        QTimer.singleShot(0, self._check_for_update)
        QTimer.singleShot(0, lambda: self._start_auth_check("startup"))
        self._refresh_folder_strip()
        self._refresh_permissions()
        QTimer.singleShot(FIRST_RUN_PROMPT_MS, self._first_run_begin)

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
        self._stop_remote()
        try:
            self.controller.stop_idle_meter()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.controller.stop_device_watch()
        except Exception:  # noqa: BLE001
            pass
        self._close_prompt()
        recording = self.controller.state == RECORDING
        if recording:
            self.record_button.setEnabled(False)
            self.record_button.setText("Finishing...")
            self._set_record_look("finishing")
            self._say("Finishing the recording before closing...", hold=PROGRESS_HOLD_SEC)

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
        # Any other outcome (odd HTTP status) tells us nothing about the password.
        # The failure text is kept: "No route to host" (errno 65) on a LAN address is how macOS
        # says Local Network access has not been allowed for this app.
        self._net_error = result.detail if result.status == authcheck.UNREACHABLE else ""
        if result.status == authcheck.UNREACHABLE:
            self._schedule_auth_retry()
        else:
            self._auth_retry_index = 0
            self._auth_retry_timer.stop()
        self._refresh_alerts()
        self._refresh_permissions()

    def _schedule_auth_retry(self) -> None:
        """Look again soon after an unreachable check: AUTH_RETRY_MS steps, then the 5 minute cadence."""
        if self._pending_close or self._auth_retry_index >= len(AUTH_RETRY_MS):
            return
        delay = AUTH_RETRY_MS[self._auth_retry_index]
        self._auth_retry_index += 1
        self._auth_retry_timer.start(delay)

    # -- server password: first-run prompt ------------------------------------------------

    def _open_password_settings(self) -> None:
        self._open_settings("server", focus_password=True)

    def _perm_first_run_due(self) -> bool:
        """macOS, prompts allowed, and the permissions were never asked for (no ``permissions_prompted`` flag)."""
        if not self._perm_enabled or os.environ.get("MEETING_NOTES_NO_PERMISSION_PROMPT"):
            return False
        try:
            return not config_mod.load_config().get("permissions_prompted")
        except Exception:  # noqa: BLE001
            return False

    def _first_run_begin(self) -> None:
        """A moment after the window shows: ask macOS for what is still undetermined, then the password."""
        if self._pending_close or self._teardown_done:
            return
        self._first_run_gate = True
        if not (self._first_run_pending and self._perm_first_run_due()):
            self._first_run_pending = False
            self._perm_first_run_hold = False
            self._first_run_release_password()
            return
        cfg = config_mod.load_config()
        cfg["permissions_prompted"] = True  # before prompting: a crash or quit must not loop
        try:
            config_mod.save_config(cfg)
        except OSError:
            log.exception("could not remember the first-run permission prompts")
        log.info("first start: asking macOS for permissions")
        state = permissions.microphone_state()
        if state == permissions._AV_NOT_DETERMINED and permissions.request_microphone(
            lambda granted: self._first_run_bridge.done.emit(granted)
        ):
            # The answer arrives through _first_run_bridge; the timer is a safety net (the step runs once).
            QTimer.singleShot(FIRST_RUN_MIC_TIMEOUT_MS, self._first_run_after_mic)
            return
        self._first_run_after_mic()

    def _first_run_after_mic(self) -> None:
        """The microphone is answered (or was not askable): now Screen & System Audio Recording, one prompt at a time."""
        if not self._first_run_pending:
            return
        if self._pending_close or self._teardown_done:
            return
        from meeting_notes.audio import devices as devices_mod

        try:
            if permissions.screen_granted() is False and not devices_mod.permission_requested():
                devices_mod.mark_permission_requested()  # a later recording must not prompt a second time
                permissions.request_screen()
        except Exception:  # noqa: BLE001
            log.exception("could not request screen recording access")
        self._first_run_pending = False
        self._perm_first_run_hold = False
        self._refresh_permissions()
        self._first_run_release_password()

    def _first_run_release_password(self) -> None:
        """Open the first-run password dialog once nothing else is in the way (no prompts running, panel closed)."""
        if not self._first_run_gate or self._first_run_pending or self._perm_panel_open:
            return
        self._first_run_gate = False
        QTimer.singleShot(0, self._maybe_first_run_password)

    def _maybe_first_run_password(self) -> None:
        """A server is configured but no password was ever saved: open Settings on it, once."""
        if os.environ.get("MEETING_NOTES_NO_FIRST_RUN_PROMPT") or self._pending_close or self._teardown_done:
            return
        cfg = config_mod.load_config()
        server = config_mod.server_settings(cfg)
        if not (server.get("url") or "").strip() or server.get("token") or cfg.get("password_prompted"):
            return
        cfg["password_prompted"] = True  # before opening: a cancelled prompt must not come back every launch
        try:
            config_mod.save_config(cfg)
        except OSError:
            log.exception("could not remember the first-run password prompt")
        log.info("first start without a server password: opening Settings")
        self._open_settings("server", focus_password=True, first_run=True)

    def _refresh_alerts(self) -> None:
        """Show or clear the red password strips and the amber unreachable strip."""
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
        # A server with no password saved: refused for it is "enter it", not "it was rejected".
        server = config_mod.server_settings()
        no_password = bool((server.get("url") or "").strip()) and not server.get("token")
        password_needed = no_password and self._auth_state != "ok"
        if password_needed:
            rejected = False
        self.password_bar.setVisible(password_needed)
        # A real 401/403 from the uploader while the last check said "ok" means the
        # token changed on the server since: verify right away instead of in 5 min.
        if queue_rejected and self._auth_state == "ok" and not self._auth_check_running:
            self._start_auth_check("upload was rejected")

        if rejected:
            text = PASSWORD_REJECTED_TEXT
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
            log.warning("password rejected by the server; %d meetings waiting", waiting)
            QApplication.alert(self)
        if not rejected and self._alert_was_visible:
            log.info("password alert cleared")
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

        # The server refuses this client version (426 / manifest minimum).
        refused = version_gate.too_old() is not None
        self.unsupported_bar.setVisible(refused)
        if refused and self._update_manifest is None and not self._gate_check_started:
            self._gate_check_started = True
            self._check_for_update(force=True)

    # -- macOS permissions -----------------------------------------------------------------

    def _perm_context(self) -> Tuple[str, str]:
        """The server URL and the latest sign that it could not be reached (for Local Network)."""
        url = (config_mod.server_settings().get("url") or "").strip()
        error = self._net_error
        if not error:
            try:
                last = (self.controller.queue_status() or {}).get("last_error", "")
            except Exception:  # noqa: BLE001
                last = ""
            if permissions.is_no_route(last):
                error = str(last)
        return url, error

    def _refresh_permissions(self) -> None:
        """Re-read every permission and show the panel, the compact strip, or neither."""
        if not self._perm_enabled:
            return
        url, error = self._perm_context()
        try:
            items = list(self._perm_probe(url, error))
        except Exception:  # noqa: BLE001 - a probe must never break the window
            log.exception("could not read the macOS permissions")
            items = []
        self._perm_items = items
        missing = [p for p in items if p.needed]
        if not missing:
            self._perm_dismissed = frozenset()
            self._perm_panel_open = False
            self.perm_overlay.setVisible(False)
            self.perm_bar.setVisible(False)
            self._first_run_release_password()
            return
        keys = frozenset(p.key for p in missing)
        if self._perm_first_run_hold:  # the system prompts come first; the panel follows with fresh statuses
            self._perm_panel_open = False
            self.perm_overlay.setVisible(False)
            self.perm_bar.setVisible(False)
            return
        self.perm_overlay.set_items(items, bundled=self._is_bundled())
        names = ", ".join(p.title for p in missing)
        text = f"Permissions needed: {names}."
        if text != self.perm_label.text():
            self.perm_label.setText(text)
            log.warning("permissions missing: %s", ", ".join(sorted(keys)))
        if keys <= self._perm_dismissed:
            self._perm_panel_open = False
            self.perm_overlay.setVisible(False)
            self.perm_bar.setVisible(True)
        else:
            self._perm_panel_open = True
            self.perm_bar.setVisible(False)
            self.perm_overlay.setVisible(True)
            self.perm_overlay.raise_()
        self._first_run_release_password()

    def _show_permissions(self) -> None:
        """The strip's Fix button: bring the panel back."""
        self._perm_dismissed = frozenset()
        self._refresh_permissions()

    def _permissions_not_now(self) -> None:
        self._perm_dismissed = frozenset(p.key for p in self._perm_items if p.needed)
        self._refresh_permissions()

    def _permissions_check_again(self) -> None:
        """Re-read the permissions, the audio devices and the server; the panel closes by itself when all is well."""
        self._refresh_permissions()
        self._refresh_devices()
        self._auth_retry_index = 0
        self._start_auth_check("permissions re-check")

    def _permissions_after_failed_start(self) -> None:
        if not self._perm_enabled:
            return
        self._perm_dismissed = frozenset()
        self._refresh_permissions()

    def _open_system_settings(self, url: str) -> None:
        if url:
            QDesktopServices.openUrl(QUrl(url))

    def _allow_microphone(self) -> None:
        """Show macOS's own Microphone prompt (only possible while the answer is still "not asked")."""
        permissions.request_microphone(lambda granted: self._perm_bridge.done.emit(granted))
        self._refresh_permissions()

    def _quit_and_reopen(self) -> None:
        """Relaunch the bundled app: macOS applies Screen & System Audio Recording only after a restart."""
        path = permissions.bundle_path()
        if path is None:
            return
        if self.controller.state != IDLE:
            self._say("Stop recording before restarting Meeting Notes.")
            return
        log.info("restarting to apply a macOS permission")
        # A detached shell waits for this process to exit, then opens a fresh copy of the app.
        QProcess.startDetached("/bin/sh", ["-c", 'sleep 3; /usr/bin/open -n "$0"', str(path)])
        self.close()

    def changeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().changeEvent(event)
        if event.type() == QEvent.ActivationChange and self.isActiveWindow():
            self._on_window_activated()

    def _on_window_activated(self) -> None:
        """Back from System Settings (or any other app): re-read what the person just changed."""
        if self._pending_close or self._teardown_done:
            return
        self._refresh_permissions()
        if self._auth_state == "unreachable" and not self._auth_check_running:
            self._start_auth_check("window activated")

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
            self._say("Stop recording before moving your recordings.")
            return
        source = config_mod.save_dir()
        destination = Path(config_mod.DEFAULT_SAVE_DIR)
        self._moving_recordings = True
        self.move_button.setEnabled(False)
        self._say(f"Moving recordings to {destination}...", hold=PROGRESS_HOLD_SEC)
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
            self._say(f"Could not move recordings: {result}. Nothing was deleted.")
            return
        self.folder_bar.setVisible(False)
        self._say(f"Moved {result} files to {config_mod.DEFAULT_SAVE_DIR}.")

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
        self._reset_end_state()
        self._auto_stop_kept = False
        self._system_last_active = time.monotonic()
        self._audio_last_active = time.monotonic()
        self._silence_armed = True
        self._suggest_kept = False
        self.waveform.clear()
        self.waveform.set_preview(False)
        self.preview.clear()
        self._seen_partials = 0
        session_dir = self.controller.start(self.name_edit.text().strip())
        if session_dir is None:
            self._say(f"Could not start: {self.controller.error}")
            self._permissions_after_failed_start()
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
        self._reset_end_state()
        # controller.stop() joins the supervisor thread, the recorder threads
        # and the live streamer -- several seconds combined -- so it runs off
        # the GUI thread; see the comment by self._timer in __init__. The
        # button stays disabled/"Finishing..." (set here, synchronously, so it
        # actually paints before the join starts) until _on_stop_finished
        # fires.
        self.record_button.setEnabled(False)
        self.record_button.setText("Finishing...")
        self._set_record_look("finishing")
        self._say("Finishing up...", hold=PROGRESS_HOLD_SEC)
        self._run_async(self.controller.stop, self._on_stop_finished)

    def _on_stop_finished(self, meta) -> None:
        if isinstance(meta, Exception):
            meta = None  # controller.stop() never raises; guards _run_async's own contract
        self._apply_stopped_ui(meta)

    def _apply_stopped_ui(self, meta) -> None:
        self._say_until = 0.0  # "Finishing up..." is done, whatever the outcome
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
            self._say(
                f"Saved {_hms(meta.get('duration_sec') or 0)} to {where}. "
                "Queued for transcription."
            )
            if note:
                self._say(f"{note} {self.status_label.text()}")

    def _open_settings(self, page=None, *, focus_password: bool = False, first_run: bool = False) -> None:
        # The signal that triggers this passes ``checked`` (a bool); only a page name selects a page.
        extra = {}
        if focus_password:
            extra["focus_password"] = True
        if first_run:
            extra["first_run"] = True
        dialog = (
            SettingsDialog(self, page=page, **extra) if isinstance(page, str) else SettingsDialog(self, **extra)
        )
        if dialog.exec():
            self._apply_meeting_settings()
            self._idle_levels_enabled = config_mod.idle_levels_enabled()
            self._publish_remote_state()  # the "allow control" choice shows on the server at once
            self._refresh_devices()
            # restart_uploader() can block for up to UploadWorker's stop()
            # join_timeout (5s) if an upload is in flight -- same freeze risk
            # as controller.stop(), so it gets the same async treatment. The
            # settings button is disabled meanwhile so a second click can't
            # start an overlapping restart.
            self.settings_button.setEnabled(False)
            self._say("Applying settings...", hold=PROGRESS_HOLD_SEC)
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
            active_dir=self._recording_active_dir(),
        )
        dialog.exec()
        result = dialog.result
        if result is not None and result.total:
            self._say(result.summary() + ".")
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
        self._say_until = 0.0  # "Applying settings..." is done
        self.settings_button.setEnabled(True)
        self._update_status()
        self._auth_state = "unknown"
        self._auth_retry_index = 0
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
        """Upload an audio recording, a transcript file or pasted text, off the GUI thread."""
        server = config_mod.server_settings()
        if not server.get("url"):
            self._say("Cannot upload: configure a server in Settings first.")
            return
        dialog = UploadDialog(self, start_dir=str(config_mod.save_dir()))
        if not dialog.exec() or dialog.request is None:
            return
        self._start_upload(dialog.request, server)

    def _start_upload(self, request: UploadRequest, server: dict) -> None:
        self.upload_button.setEnabled(False)
        self._uploading_recording = True
        if request.kind == "audio":
            self._say(f"Uploading {request.label}...", hold=PROGRESS_HOLD_SEC)
        else:
            self._say(f"Adding the transcript {request.label}...", hold=PROGRESS_HOLD_SEC)

        def work():
            from meeting_notes.client.api import ServerClient, UPLOAD_TIMEOUT

            with ServerClient(server["url"], server.get("token") or None, timeout=UPLOAD_TIMEOUT) as client:
                if request.kind == "audio":
                    return client.upload_recording(request.path, name=request.name)
                return client.upload_transcript(
                    request.text,
                    name=request.name,
                    started_at=request.started_at,
                    source=request.source,
                    filename=request.filename,
                )

        self._run_async(work, lambda result: self._on_recording_uploaded(result, request))

    def _on_recording_uploaded(self, result, request) -> None:
        self._uploading_recording = False
        self.upload_button.setEnabled(True)
        if isinstance(request, str):  # older callers passed just the file name
            request = UploadRequest(kind="audio", path=Path(request))
        label = request.label
        if isinstance(result, Exception):
            self._say(f"Could not upload {label}: {_upload_failure_text(result)}")
            return
        if request.kind == "transcript":
            notes = " Notes are being generated." if isinstance(result, dict) and result.get("notes") else ""
            self._say(f"Added the transcript {label}; it is in your meetings on the server.{notes}")
            return
        job_id = result.get("job_id") if isinstance(result, dict) else None
        suffix = f" (job {job_id})" if job_id else ""
        self._say(f"Uploaded {label}; server transcription queued{suffix}.")

    # -- meeting detection ----------------------------------------------------

    def _create_meeting_detector(self):
        if sys.platform not in ("win32", "darwin") or os.environ.get("MEETING_NOTES_NO_DETECT"):
            return None
        if sys.platform == "darwin":
            from meeting_notes.client import meeting_detect_mac

            return meeting_detect.MeetingDetector(
                read_usage=meeting_detect_mac.read_mic_usage,
                read_titles=meeting_detect_mac.list_window_titles,
                own_executable="",  # the mac probe already excludes our own pid
                end_grace_sec=self._detect_settings["end_grace_sec"],
            )
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
            now = time.monotonic()
            for event in self._detector.poll(now):
                self._handle_meeting_event(event)
            self._check_end_pending(now)
        except Exception:  # noqa: BLE001 - detection is best-effort
            pass

    def _handle_meeting_event(self, event) -> None:
        log.info("meeting detection: %s", event)
        if isinstance(event, meeting_detect.MeetingStarted):
            if self.controller.state == RECORDING:
                # A new call inside a running recording: suggestions may come back
                # for it, and one about the previous call no longer applies.
                if self._suggest_kept or self._suggest_prompt is not None or self._end_pending:
                    log.info("meeting detection: new call during a recording; re-arming stop suggestions")
                self._suggest_kept = False
                if self._end_prompt is None:
                    self._end_pending = False
                self._dismiss_suggestion("a call started")
            if not self._detect_settings["enabled"]:
                return
            if self.controller.state != IDLE or self._prompt is not None:
                return
            self._show_prompt(event.label, event.suggested_name)
        elif isinstance(event, meeting_detect.MeetingEnded):
            self._close_prompt()
            if self._auto_stop_eligible():
                # Mic released + no call window is not enough on its own: the
                # system-audio track must also have been quiet (see below).
                self._end_pending = True
                self._end_wait_logged = False
                log.info("meeting detection: call-end signals from detector; checking system audio before auto-stop")
                self._check_end_pending(time.monotonic())
            elif self._suggest_eligible():
                self._end_pending = True
                self._end_wait_logged = False
                log.info("meeting detection: call-end signals from detector; checking system audio before suggesting a stop")
                self._check_end_pending(time.monotonic())
            elif self._auto_session:
                log.info("meeting detection: call ended but auto-stop is off/unavailable; recording continues")

    # -- end-of-call auto-stop --------------------------------------------------

    def _auto_stop_eligible(self) -> bool:
        return bool(
            self._auto_session
            and self._detect_settings["auto_stop"]
            and not self._auto_stop_kept
            and self.controller.state == RECORDING
            and not self._pending_close
        )

    def _suggest_eligible(self) -> bool:
        return bool(
            self._detect_settings.get("suggest_stop", True)
            and not self._suggest_kept
            and self.controller.state == RECORDING
            and not self._pending_close
        )

    def _note_levels(self, now: float, levels) -> None:
        """Track when each side last carried sound (for the stop suggestions)."""
        self._note_system_level(now, levels.get("system"))
        for track, peak in levels.items():
            try:
                muted = bool(self.controller.source_muted(track))
            except Exception:  # noqa: BLE001
                muted = False
            # Same rule as the system check: a muted track proves nothing.
            if muted or peak is None or float(peak) >= SYSTEM_SILENCE_PEAK:
                self._audio_last_active = now

    def _note_system_level(self, now: float, peak) -> None:
        """Track when the system-audio track last carried sound."""
        try:
            muted = bool(self.controller.source_muted("system"))
        except Exception:  # noqa: BLE001
            muted = False
        # A muted track (or a track we cannot read) proves nothing: never
        # count it as silence.
        if muted or peak is None or float(peak) >= SYSTEM_SILENCE_PEAK:
            self._system_last_active = now

    def _reset_end_state(self) -> None:
        self._end_pending = False
        self._end_wait_logged = False
        prompt, self._end_prompt = self._end_prompt, None
        if prompt is not None:
            prompt.close_silently()
        self._dismiss_suggestion("recording state reset", log_it=False)

    # -- "meeting seems over -- stop recording?" suggestions ----------------------

    def _show_suggestion(self, kind: str, title: str) -> None:
        prompt = StopSuggestionPrompt(title, "Stop recording?")
        prompt.stop_requested.connect(self._on_suggest_stop)
        prompt.keep_requested.connect(self._on_suggest_keep)
        self._suggest_prompt = prompt
        self._suggest_kind = kind
        log.info("stop suggestion shown (%s): %s", kind, title)
        prompt.show_prompt()
        QApplication.alert(self)

    def _dismiss_suggestion(self, why: str, *, log_it: bool = True) -> None:
        prompt, self._suggest_prompt = self._suggest_prompt, None
        if prompt is not None:
            if log_it:
                log.info("stop suggestion dismissed automatically (%s): %s", self._suggest_kind, why)
            prompt.close_silently()

    def _on_suggest_stop(self) -> None:
        log.info("stop suggestion (%s): user chose Stop recording", self._suggest_kind)
        self._suggest_prompt = None
        self._end_pending = False
        if self.controller.state == RECORDING and self.record_button.isEnabled() and not self._pending_close:
            self._auto_session = False
            self._stop()

    def _on_suggest_keep(self) -> None:
        log.info("stop suggestion (%s): user chose Keep recording", self._suggest_kind)
        self._suggest_prompt = None
        self._end_pending = False
        if self._suggest_kind == "call-end":
            # Quiet for this call. (A silence suggestion has its own re-arm: it
            # only comes back after audio resumes and stops again.)
            self._suggest_kept = True

    def _check_silence(self, now: float) -> None:
        """Both sides silent for SILENCE_SUGGEST_SEC -> suggest stopping, once per
        silence: it re-arms when audio comes back."""
        if self.controller.state != RECORDING:
            return
        quiet = now - self._audio_last_active
        if quiet < SILENCE_SUGGEST_SEC:
            self._silence_armed = True
            if self._suggest_prompt is not None and self._suggest_kind == "silence":
                self._dismiss_suggestion("audio resumed")
            return
        if (
            not self._silence_armed
            or self._suggest_prompt is not None
            or self._end_prompt is not None
            or not self._detect_settings.get("suggest_stop", True)
            or self._pending_close
        ):
            return
        self._silence_armed = False
        self._show_suggestion("silence", f"No audio for {SILENCE_SUGGEST_SEC // 60} minutes")

    def _check_end_pending(self, now: float) -> None:
        if not self._end_pending:
            return
        if not self._auto_stop_eligible():
            if self._suggest_eligible():
                self._check_end_suggestion(now)
                return
            self._reset_end_state()
            return
        quiet = now - self._system_last_active
        grace = float(self._detect_settings["end_grace_sec"])
        if self._end_prompt is None:
            if quiet >= grace:
                log.info("meeting detection: call ended and system audio silent for %.0fs; showing stop countdown", quiet)
                prompt = CallEndingPrompt(AUTO_STOP_COUNTDOWN_SEC)
                prompt.keep_requested.connect(self._on_end_keep)
                prompt.stop_requested.connect(self._on_end_stop_now)
                prompt.expired.connect(self._on_end_expired)
                self._end_prompt = prompt
                prompt.show_prompt()
                QApplication.alert(self)
            elif not self._end_wait_logged:
                self._end_wait_logged = True
                log.info("meeting detection: call ended but system audio still active; not stopping (waiting for %.0fs of silence)", grace)
        elif quiet < grace:
            log.info("meeting detection: system audio resumed; cancelling stop countdown")
            self._end_wait_logged = True
            prompt, self._end_prompt = self._end_prompt, None
            prompt.close_silently()

    def _check_end_suggestion(self, now: float) -> None:
        """Call over + system audio quiet for the grace -> suggest (never force) a stop."""
        quiet = now - self._system_last_active
        grace = float(self._detect_settings["end_grace_sec"])
        if self._suggest_prompt is None:
            if quiet >= grace:
                log.info("meeting detection: call ended and system audio silent for %.0fs; suggesting a stop", quiet)
                self._show_suggestion("call-end", "Meeting seems to have ended")
            elif not self._end_wait_logged:
                self._end_wait_logged = True
                log.info("meeting detection: call ended but system audio still active; not suggesting a stop (waiting for %.0fs of silence)", grace)
        elif self._suggest_kind == "call-end" and quiet < grace:
            self._end_wait_logged = True
            self._dismiss_suggestion("system audio resumed")

    def _on_end_keep(self) -> None:
        log.info("meeting detection: user chose Keep recording; auto-stop disabled for this recording")
        self._end_prompt = None
        self._end_pending = False
        self._auto_stop_kept = True
        self._suggest_kept = True

    def _on_end_stop_now(self) -> None:
        log.info("meeting detection: user chose Stop now")
        self._end_prompt = None
        self._auto_stop_now("Call ended — recording stopped and queued.")

    def _on_end_expired(self) -> None:
        log.info("meeting detection: stop countdown finished")
        self._end_prompt = None
        self._auto_stop_now("Call ended — recording stopped and queued.")

    def _auto_stop_now(self, note: str) -> None:
        if not (self._auto_session and self.controller.state == RECORDING
                and not self._pending_close and self.record_button.isEnabled()):
            self._reset_end_state()
            return
        self._auto_stop_note = note
        log.info("meeting detection: stopping the recording")
        self._stop()
        self._say(note)

    def _show_prompt(self, label: str, name: str) -> None:
        prompt = MeetingPrompt(label, name)
        prompt.record_requested.connect(self._on_prompt_record)
        prompt.dismissed.connect(self._on_prompt_dismissed)
        self._prompt = prompt
        self._prompt_info = (label, name)
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

    def _sync_devices(self) -> None:
        """Cheap, every tick: device labels, the device banners, the mute buttons.

        The device watcher thread only publishes state; everything that touches a
        widget happens here on the GUI thread.
        """
        try:
            labels = self.controller.device_labels()
            banners = self.controller.device_banners()
        except Exception:  # noqa: BLE001 - never let a tick fail
            return
        if labels:
            text = f"You: {labels.get('mic', '?')}\nThem: {labels.get('system', '?')}"
            if text != self.devices_label.text():
                self.devices_label.setText(text)
        errors = [b["text"] for b in banners if b["level"] == "error"]
        oks = [b for b in banners if b["level"] == "ok"]
        error_text = "\n".join(errors)
        if error_text != self.device_label.text():
            self.device_label.setText(error_text)
        self.device_bar.setVisible(bool(errors))
        ok_text = "\n".join(str(b["text"]) for b in oks)
        if ok_text != self.device_ok_label.text():
            self.device_ok_label.setText(ok_text)
        self.device_ok_bar.setVisible(bool(oks))
        if oks:
            remaining = min(float(b["remaining"] or 0) for b in oks)
            self._device_ok_effect.setOpacity(max(0.0, min(1.0, remaining / 2.0)))
        if self.controller.state == RECORDING:
            recorders = getattr(self.controller.session, "recorders", {})
            for track, button in (("mic", self.mute_mic_button), ("system", self.mute_system_button)):
                want = track in recorders
                if button.isEnabled() != want:
                    button.setEnabled(want)

    def _open_logs(self) -> None:
        """Logs live on a page of Settings; this opens Settings on it."""
        self._open_settings("logs")

    def _tick(self) -> None:
        if self.controller.state == RECORDING:
            levels = self.controller.levels()
            self.waveform.push(levels)
            now = time.monotonic()
            self._note_remote_levels(now, levels)
            self._note_levels(now, levels)
            self._check_silence(now)
            for track, degraded in self.controller.degraded().items():
                self.waveform.set_track_active(track, not degraded)
            self.clock.setText(_hms(self.controller.elapsed))
            self._drain_partials()
        else:
            self._tick_idle_levels()
        self._sync_devices()
        self._update_status()
        self._refresh_alerts()

    def _window_visible(self) -> bool:
        """On screen: shown and not minimized (a hidden window has no use for a live meter)."""
        return self.isVisible() and not self.isMinimized()

    def _tick_idle_levels(self) -> None:
        """Idle: show live input as a greyed preview while it is useful (window on screen, or a web viewer
        watching this recorder), and let go of the devices otherwise. Never raises into the Qt loop."""
        controller = self.controller
        if not hasattr(controller, "set_idle_wanted"):
            return
        try:
            channel = getattr(self, "_remote", None)
            watched = bool(channel is not None and getattr(channel, "watched", False))
            idle = self._record_state == "idle" and controller.state == IDLE and not self._pending_close
            wanted = idle_meter_wanted(
                enabled=self._idle_levels_enabled,
                idle=idle,
                window_visible=self._window_visible(),
                watched=watched,
            )
            controller.set_idle_wanted(wanted)
            active = bool(wanted and controller.idle_meter_active)
            levels = controller.idle_levels() if active else {}
            self.waveform.set_preview(active)
            if active:
                self.waveform.push(levels)
            if channel is not None:
                channel.publish_levels(levels if active else None)
        except Exception:  # noqa: BLE001 - a preview must never break the window
            if not self._idle_error_logged:
                self._idle_error_logged = True
                log.exception("idle level preview failed")

    def _drain_partials(self) -> None:
        partials = self.controller.partials()
        for item in partials[self._seen_partials :]:
            label = "You" if item.get("track") == "mic" else "Them"
            self.preview.appendPlainText(f"{label}: {item.get('text', '')}")
        self._seen_partials = len(partials)

    def _queue_note(self) -> str:
        q = self.controller.queue_status()
        bits = []
        uploading = int(q.get("pending") or 0)
        try:
            uploading = max(0, uploading - int(self.controller.queue_awaiting_transcript()))
        except Exception:  # noqa: BLE001
            pass
        if uploading:
            bits.append(f"{uploading} upload{'s' if uploading != 1 else ''} pending")
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
            transcription_state = progress.get("transcription_state")
            if upload_state == "complete":
                # Uploaded and finalized: waiting on the server is not an error.
                if transcription_state == "transcribing":
                    bits.append(
                        f"Uploaded · transcribing {progress.get('transcription_percent', 0):.0f}%"
                    )
                elif transcription_state in ("queued", "pending"):
                    bits.append("Uploaded · transcribing (queued)")
            else:
                if upload_state == "uploading":
                    bits.append(f"uploading {progress.get('upload_percent', 0):.0f}%")
                elif upload_state == "pending" and not uploading:
                    bits.append("upload pending")
                if transcription_state == "transcribing":
                    bits.append(
                        f"transcribing {progress.get('transcription_percent', 0):.0f}%"
                    )
        return "  |  " + ", ".join(bits) if bits else ""

    def _say(self, text: str, hold: float = STATUS_HOLD_SEC) -> None:
        """Show a one-off message on the status line.

        One-off messages are transient: while idle, once ``hold`` seconds have passed the line goes
        back to the live status (see ``_update_status``), so an old result never lingers.
        """
        self._say_until = time.monotonic() + hold
        self.status_label.setText(text)

    def _live_idle_status(self) -> str:
        server = config_mod.server_settings()
        where = server.get("url") or "not configured"
        return f"Ready. Server: {where}{self._queue_note()}"

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
        elif self.controller.state == IDLE:
            expired = time.monotonic() >= self._say_until
            if expired or not self.status_label.text():
                text = self._live_idle_status()
                if text != self.status_label.text():
                    self.status_label.setText(text)

    # -- client updates ------------------------------------------------------

    def _check_for_update(self, force: bool = False) -> None:
        """Check the configured server without ever blocking the Qt thread."""
        if self._update_check_started and not force:
            return
        server = config_mod.server_settings()
        url = (server.get("url") or "").strip()
        # ``force`` (a periodic re-check, or the server just refused this
        # client version) also works when the routine check is switched off:
        # a client the server no longer accepts has to be able to find its update.
        if not url or (not server.get("check_updates", True) and not force):
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
            if version_gate.too_old() is not None:
                self._say("No newer client was found on the server.")
            return
        self._update_manifest = result
        log.info("update available: v%s", result.version)
        # Never installed by itself: the bar and its button are the only way in.
        self.update_note.setText(f"Update available: {result.version}")
        self.update_button.setText("Update now")
        self.update_button.setVisible(True)
        self.update_bar.setVisible(True)
        if result.notes or result.notes_url:
            self.whats_new_link.setText(
                f'<a href="whatsnew" style="color: {theme.tokens()["accent_text"]};">What\u2019s new</a>'
            )
            self.whats_new_link.setVisible(True)
        else:
            self.whats_new_link.setVisible(False)

    def _show_whats_new(self, _link: str = "") -> None:
        manifest = self._update_manifest
        if manifest is None:
            return
        if manifest.notes_url:
            QDesktopServices.openUrl(QUrl(manifest.notes_url))
        elif manifest.notes:
            QMessageBox.information(self, f"What\u2019s new in {manifest.version}", manifest.notes)

    def _request_update(self) -> None:
        """The Update now button. Nothing else ever starts an update."""
        if self._update_installing:
            return
        if self._update_manifest is None:
            # The server refused this version but no manifest is known yet.
            self._say("Looking for the update on the server...", hold=PROGRESS_HOLD_SEC)
            self._check_for_update(force=True)
            return
        if self.controller.state == RECORDING:
            QMessageBox.information(
                self,
                "Recording in progress",
                "The update is ready, but it will not interrupt your active recording. "
                "Stop recording before installing it.",
            )
            return
        self._begin_update()

    def _begin_update(self) -> None:
        manifest = self._update_manifest
        updater = self._update_updater
        if manifest is None or updater is None:
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
        self._say("Downloading and verifying the client update...", hold=PROGRESS_HOLD_SEC)
        self._run_async(lambda: updater.download(manifest), self._on_update_downloaded)

    def _on_update_downloaded(self, result) -> None:
        log.info("update download finished: %s", result)
        if isinstance(result, Exception):
            self._update_installing = False
            self.update_button.setEnabled(True)
            self._say(f"Client update failed: {result}")
            return
        # Do not launch an installer that could close the process while a
        # meeting began during the download. Keep the verified file and offer
        # it again once recording has finished.
        if self.controller.state == RECORDING:
            self._verified_update_path = Path(result)
            self._update_installing = False
            self.update_button.setEnabled(True)
            self.update_button.setToolTip("Verified update ready; stop recording to install it")
            self._say("Update verified and ready; it will wait until recording stops.")
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
            self._say(f"Client update failed: {result}")
            return
        self.update_button.setText("Update installer launched")
        self._say(
            "The verified update installer was launched. Your recordings and settings were preserved."
        )

    # -- live presence and remote control (server Recorders page) ---------------------

    def _setup_remote(self, factory) -> None:
        self._remote: Any = None
        self._remote_bridge = _RemoteBridge(self)
        self._remote_bridge.command.connect(self._on_remote_command)
        self._remote_bridge.notice.connect(self._on_remote_notice)
        self._remote_bridge.unauthorized.connect(self._on_remote_unauthorized)
        self._remote_recordings_lock = threading.Lock()  # one listing / delete at a time
        self._remote_peaks: Dict[str, deque] = {"mic": deque(), "system": deque()}
        self._prompt_info: Tuple[str, str] = ("", "")
        self._remote_state_error_logged = False
        self._remote_quiet = False
        self._toast = Toast(self)
        if factory is None:
            if os.environ.get("MEETING_NOTES_NO_REMOTE"):
                return
            factory = self._default_remote_channel
        try:
            self._remote = factory(self._remote_command_from_thread)
            try:
                self._remote.on_unauthorized = self._remote_bridge.unauthorized.emit
            except Exception:  # noqa: BLE001 - a stand-in channel without the hook is fine
                pass
            self._remote.start()
        except Exception:  # noqa: BLE001 - presence is a convenience; never stop the app starting
            log.exception("could not start the remote control channel")
            self._remote = None
            return
        self._remote_timer = QTimer(self)
        self._remote_timer.timeout.connect(self._publish_remote_state)
        self._remote_timer.start(REMOTE_PUBLISH_MS)
        self._publish_remote_state()

    @staticmethod
    def _remote_server_config() -> Tuple[str, str]:
        server = config_mod.server_settings()
        return (server.get("url") or "", server.get("token") or "")

    def _default_remote_channel(self, on_command) -> ControlChannel:
        # url/token are re-read on every connect attempt, so Settings changes apply without a restart.
        return ControlChannel(self._remote_server_config, on_command)

    def _stop_remote(self) -> None:
        channel, self._remote = getattr(self, "_remote", None), None
        timer = getattr(self, "_remote_timer", None)
        if timer is not None:
            timer.stop()
        if channel is not None:
            try:
                channel.stop(join_timeout=0.3)
            except Exception:  # noqa: BLE001
                pass

    def _remote_command_from_thread(self, command_id: str, name: str, args) -> None:
        """Called on the channel thread: hop to the GUI thread."""
        self._remote_bridge.command.emit(command_id, name, args)

    def _on_remote_unauthorized(self) -> None:
        """The control channel was refused (close code 4401 / HTTP 401): show it now, not in 5 minutes."""
        log.info("control channel refused for its password")
        self._auth_state = "rejected"
        self._refresh_alerts()

    def _on_remote_notice(self, text: str) -> None:
        self._toast.show_message(text)
        self._refresh_alerts()

    def _on_remote_command(self, command_id: str, name: str, args) -> None:
        if name in remote.RECORDING_COMMANDS:
            self._on_recordings_command(command_id, name, args)
            return
        ok, code, error = self.execute_remote_command(name, args)
        channel = self._remote
        if channel is None:
            return
        snapshot = self._safe_remote_state()
        try:
            channel.send_ack(command_id, ok, code, error, snapshot)
            channel.publish(snapshot)
        except Exception:  # noqa: BLE001
            log.exception("could not answer a remote command")

    # -- recordings commands (list / re-upload / delete the saved recordings) ---------------

    def _recording_active_dir(self) -> Optional[Path]:
        """The folder being recorded (or still being finalized), if any: never re-queued or deleted."""
        controller = self.controller
        if controller.state == IDLE or not getattr(controller, "session_dir", None):
            return None
        return Path(controller.session_dir)

    def _on_recordings_command(self, command_id: str, name: str, args) -> None:
        """``list_recordings`` / ``reupload`` / ``delete_local`` from the server (GUI thread).

        The slow ones (listing a big save folder, moving folders to the Recycle Bin) run on a worker
        thread and answer the command themselves; ``reupload`` only writes small queue files, so it
        stays here and reuses the Re-upload dialog's own path (``controller.reupload_recordings``).
        """
        channel = self._remote

        def answer(ok, code=None, error=None, result=None) -> None:
            if channel is None:
                return
            try:
                channel.send_ack(command_id, ok, code, error, None, result=result)
            except Exception:  # noqa: BLE001
                log.exception("could not answer a remote command")

        try:
            name, args = remote.clean_command(name, args)
        except ValueError as exc:
            answer(False, refusal_code(exc), str(exc))
            return
        if not config_mod.remote_control_allowed():
            log.info("remote command: %s (source=server) -> refused(remote_control_disabled)", name)
            answer(False, "remote_control_disabled", "Remote control is turned off in this app's Settings.")
            return
        try:
            save_dir = Path(config_mod.save_dir())
            queue = self.controller.session_queue()
        except Exception as exc:  # noqa: BLE001
            log.exception("remote command %s: no save folder", name)
            answer(False, "no_save_folder", f"Could not open the save folder: {exc}")
            return
        active = self._recording_active_dir()
        notice = self._remote_bridge.notice

        if name == "reupload":
            try:
                result = remote_recordings.reupload(
                    save_dir, queue, args["session_ids"], self.controller.reupload_recordings, active
                )
            except Exception as exc:  # noqa: BLE001
                log.exception("remote command reupload failed")
                answer(False, "failed", f"{type(exc).__name__}: {exc}")
                return
            count = result["queued"] + result["already_queued"]
            log.info("remote command: reupload (source=server) -> %d queued, %d refused",
                     count, len(result["results"]) - count)
            self._toast.show_message(
                f"Re-upload asked from the server: {count} recording{'s' if count != 1 else ''} queued"
            )
            self._refresh_alerts()
            answer(True, None, None, result)
            return

        def work() -> None:
            with self._remote_recordings_lock:
                try:
                    if name == "list_recordings":
                        result = remote_recordings.list_recordings(save_dir, queue, active, args.get("offset", 0))
                        log.info("remote command: list_recordings (source=server) -> %d of %d",
                                 len(result["recordings"]), result["total"])
                    else:
                        result = remote_recordings.delete_local(save_dir, queue, args["session_ids"], active)
                        log.info("remote command: delete_local (source=server) -> %d deleted, %d refused",
                                 result["deleted"], len(result["results"]) - result["deleted"])
                        if result["deleted"]:
                            n = result["deleted"]
                            notice.emit(f"Deleted {n} recording{'s' if n != 1 else ''} on the server's request "
                                        f"(moved to the {retention.trash_name()})")
                except Exception as exc:  # noqa: BLE001
                    log.exception("remote command %s failed", name)
                    answer(False, "failed", f"{type(exc).__name__}: {exc}")
                    return
            answer(True, None, None, result)

        threading.Thread(target=work, name=f"remote-{name}", daemon=True).start()

    def _note_remote_levels(self, now: float, levels) -> None:
        """Rolling window of recent levels, so the snapshot can report a ~1 s peak."""
        for track, history in self._remote_peaks.items():
            value = levels.get(track)
            if value is not None:
                history.append((now, float(value)))
            while history and now - history[0][0] > REMOTE_PEAK_WINDOW_SEC:
                history.popleft()

    def _publish_remote_state(self) -> None:
        channel = self._remote
        if channel is None:
            return
        snapshot = self._safe_remote_state()
        if snapshot is not None:
            channel.publish(snapshot)

    def _safe_remote_state(self) -> Optional[dict]:
        try:
            return self.build_remote_state()
        except Exception:  # noqa: BLE001 - presence must never hurt the window
            if not self._remote_state_error_logged:
                self._remote_state_error_logged = True
                log.exception("could not build the remote state snapshot")
            return None

    def _remote_status(self) -> str:
        if self._record_state == "finishing" or self.controller.state == STOPPING:
            return "finishing"
        return "recording" if self.controller.state == RECORDING else "idle"

    def build_remote_state(self) -> dict:
        """What this window shows, in the shape ``remote.sanitize_state`` documents (GUI thread)."""
        controller = self.controller
        status = self._remote_status()
        active = status != "idle"
        session_dir = controller.session_dir if active else None
        labels = controller.device_labels() or {}
        banners = controller.device_banners() if active else []
        levels = controller.levels() if active else {}
        preview_active = False
        if not active and self._idle_levels_enabled:
            try:
                preview_active = bool(controller.idle_meter_active)
                levels = controller.idle_levels() if preview_active else {}
            except Exception:  # noqa: BLE001
                preview_active, levels = False, {}
        degraded = controller.degraded() if active else {}
        now = time.monotonic()
        out_banners: List[dict] = []
        track_bad = set()
        for b in banners:
            track = b.get("track")
            if b.get("level") == "error":
                track_bad.add(track)
                if b.get("text") == missing_device_text(track):
                    banner_id = "no_mic" if track == "mic" else "no_system"
                else:
                    banner_id = "device_lost"
                out_banners.append({"id": banner_id, "level": "error", "text": b.get("text", "")})
            elif b.get("level") == "ok":
                out_banners.append({"id": "device_back", "level": "ok", "text": b.get("text", "")})
        for widget, label, banner_id, level in (
            (self.alert_bar, self.alert_label, "token_rejected", "error"),
            (self.warn_bar, self.warn_label, "server_unreachable", "warn"),
            (self.folder_bar, self.folder_label, "recordings_in_app_folder", "warn"),
            (self.update_bar, self.update_note, "update_available", "info"),
            (self.unsupported_bar, self.unsupported_label, "unsupported_version", "error"),
        ):
            if not widget.isHidden():
                out_banners.append({"id": banner_id, "level": level, "text": label.text()})

        tracks = {}
        for track in remote.TRACKS:
            label = str(labels.get(track) or "")
            lost = label.endswith(" (lost)")
            device = label[: -len(" (lost)")] if lost else label
            absent = not label or label == "not connected" or label.startswith("unavailable")
            current = float(levels.get(track) or 0.0)
            history = [v for t, v in self._remote_peaks[track] if now - t <= REMOTE_PEAK_WINDOW_SEC]
            try:
                muted = bool(controller.source_muted(track)) if active else False
            except Exception:  # noqa: BLE001
                muted = False
            tracks[track] = {
                "device": None if absent else device,
                "connected": not (absent or lost or track in track_bad),
                "muted": muted,
                "level": current,
                "peak": max([current] + history) if active else (current if preview_active else 0.0),
                "degraded": bool(degraded.get(track)),
            }

        try:
            queue = controller.queue_status() or {}
            awaiting = int(controller.queue_awaiting_transcript())
            progress = controller.queue_progress() or {}
        except Exception:  # noqa: BLE001
            queue, awaiting, progress = {}, 0, {}
        if progress.get("upload_state") not in (None, "complete"):
            up_state, percent = progress.get("upload_state"), progress.get("upload_percent")
        else:
            up_state, percent = progress.get("transcription_state"), progress.get("transcription_percent")

        suggestion = None
        if self._suggest_prompt is not None:
            suggestion = {
                "kind": self._suggest_kind,
                "title": self._suggest_prompt.title_label.text(),
                "seconds_left": None,
            }
        elif self._end_prompt is not None:
            suggestion = {
                "kind": "countdown",
                "title": self._end_prompt.title_label.text(),
                "seconds_left": self._end_prompt.remaining,
            }
        manifest = self._update_manifest
        return {
            "status": status,
            "meeting": {
                "name": self.name_edit.text().strip(),
                "session_id": Path(session_dir).name if session_dir else None,
                "elapsed_sec": controller.elapsed if active else None,
            },
            "tracks": tracks,
            "banners": out_banners,
            "update": {
                "available": manifest is not None,
                "version": manifest.version if manifest is not None else None,
                "installing": self._update_installing,
            },
            "uploads": {
                "pending": queue.get("pending", 0),
                "failed": queue.get("failed", 0),
                "awaiting_transcript": awaiting,
                "current_percent": percent if progress else None,
                "state": up_state if progress else None,
            },
            "call": {
                "prompt": (
                    {"label": self._prompt_info[0], "name": self._prompt_info[1]}
                    if self._prompt is not None
                    else None
                ),
                "active_app": None,  # the detector only reports a call when it starts
            },
            "suggestion": suggestion,
            "control": {"allowed": config_mod.remote_control_allowed()},
            "stream": controller.stream_state(),
            "preview": {
                "supported": bool(self._idle_levels_enabled),
                "active": preview_active,
                "tracks": list(getattr(controller, "idle_kinds", remote.TRACKS)),
            },
        }

    def execute_remote_command(self, name: str, args=None) -> Tuple[bool, Optional[str], Optional[str]]:
        """Run a server command through the same paths as the buttons (GUI thread).

        Returns ``(ok, code, error)``; ``code`` is one of ``remote.ERROR_CODES``.
        """
        self._remote_quiet = False  # set by a command that succeeded without changing anything
        try:
            name, args = remote.clean_command(name, args)
        except ValueError as exc:
            result = (False, refusal_code(exc), str(exc))
        else:
            if not config_mod.remote_control_allowed():
                result = (
                    False,
                    "remote_control_disabled",
                    "Remote control is turned off in this app's Settings.",
                )
            else:
                try:
                    result = self._dispatch_remote(name, args)
                except Exception as exc:  # noqa: BLE001 - a command must never break the window
                    log.exception("remote command %s failed", name)
                    result = (False, "failed", f"{type(exc).__name__}: {exc}")
        ok, code, error = result
        log.info("remote command: %s (source=server) -> %s", name, "ok" if ok else f"refused({code})")
        if args and args.get("name"):
            log.debug("remote command %s: name=%r", name, args["name"][:60])
        if ok and not self._remote_quiet:
            text = self._remote_toast_text(name, args)
            if text:
                self._toast.show_message(text)
        return ok, code, error

    @staticmethod
    def _remote_toast_text(name: str, args: dict) -> Optional[str]:
        if name in ("mute", "unmute"):
            who = "you" if args["track"] == "mic" else "them"
            return f"{'Muted' if name == 'mute' else 'Unmuted'} {who} from the server"
        return _TOAST.get(name)

    def _dispatch_remote(self, name: str, args: dict) -> Tuple[bool, Optional[str], Optional[str]]:
        controller = self.controller
        state = controller.state
        finishing = self._record_state == "finishing" or state == STOPPING
        ok = (True, None, None)

        if name == "start":
            if state == RECORDING:
                return False, "already_recording", "Already recording."
            if finishing or self._pending_close or self._update_installing:
                return False, "busy", "The app is busy finishing something; try again in a moment."
            if "name" in args:
                self.name_edit.setText(args["name"])
            self._auto_session = False
            self._start()
            return self._started_result()
        if name == "stop":
            if state != RECORDING or not self.record_button.isEnabled():
                return False, "not_recording", "Not recording."
            self._auto_session = False
            self._stop()
            return ok
        if name in ("mute", "unmute"):
            if state != RECORDING:
                return False, "not_recording", "Not recording."
            track, want = args["track"], name == "mute"
            if track not in getattr(controller.session, "recorders", {}):
                return False, "no_such_track", "That audio source is not connected."
            button = self.mute_mic_button if track == "mic" else self.mute_system_button
            if button.isChecked() != want:
                button.setChecked(want)  # toggled -> _toggle_source_mute, the button's own path
            else:
                self._remote_quiet = True  # already in that state: nothing to announce
            if bool(controller.source_muted(track)) != want:
                return False, "failed", "Could not change the mute."
            return ok
        if name == "refresh_devices":
            self._refresh_devices()
            wake = getattr(controller, "wake_device_watch", None)
            if wake is not None:
                wake()
            return ok
        if name in ("accept_call_prompt", "dismiss_call_prompt"):
            prompt = self._prompt
            if prompt is None:
                return False, "no_prompt", "There is no call prompt to answer."
            if name == "dismiss_call_prompt":
                prompt.later_button.click()
                return ok
            if state != IDLE:
                return False, "already_recording", "Already recording."
            if "name" in args:
                prompt.name_edit.setText(args["name"])
            prompt.record_button.click()
            return self._started_result()
        if name in ("keep_recording", "stop_suggested"):
            prompt = self._suggest_prompt or self._end_prompt
            if prompt is None:
                return False, "no_suggestion", "There is no stop suggestion to answer."
            if name == "keep_recording":
                prompt.keep_button.click()
                return ok
            prompt.stop_button.click()
            if self._record_state != "finishing":
                return False, "failed", "Could not stop the recording."
            return ok
        if name == "retry_uploads":
            try:
                controller.session_queue().retry_all_now()
                controller.start_uploader()
            except Exception as exc:  # noqa: BLE001
                return False, "failed", f"Could not retry the uploads: {exc}"
            return ok
        if name == "check_update":
            self._check_for_update(force=True)
            return ok
        if name == "install_update":
            if state != IDLE or finishing:
                return False, "recording_in_progress", "An update can't be installed while recording."
            if self._update_installing:
                return False, "busy", "An update is already being installed."
            if self._update_manifest is None or self._update_updater is None:
                return False, "no_update", "No update is available."
            self._begin_update()  # not _request_update: that one pops a modal box while recording
            return ok
        if name == "set_name":
            if finishing or state not in (IDLE, RECORDING):
                return False, "busy", "The app is busy finishing something; try again in a moment."
            self.name_edit.setText(args["name"])
            if state == RECORDING:
                controller.set_recording_name(args["name"])  # what stop() saves as the meeting name
            return ok
        return False, "unknown_command", "unknown command"  # unreachable: clean_command whitelists

    def _started_result(self) -> Tuple[bool, Optional[str], Optional[str]]:
        if self.controller.state == RECORDING:
            return True, None, None
        return False, "failed", str(self.controller.error or "Could not start the recording.")
