"""The recorder window: start, stop, and proof that it is working."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable, List

from PySide6.QtCore import QObject, Qt, QTimer, Signal
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
        self._refresh_devices()
        # Started with the window: a meeting recorded while the server was
        # down must upload next time the app opens, without needing another
        # recording to trigger it.
        self.controller.start_uploader()
        self._update_status()

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
        recording = self.controller.state == RECORDING
        if recording:
            self.record_button.setEnabled(False)
            self.record_button.setText("Finishing...")
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
        # controller.stop() joins the supervisor thread, the recorder threads
        # and the live streamer -- several seconds combined -- so it runs off
        # the GUI thread; see the comment by self._timer in __init__. The
        # button stays disabled/"Finishing..." (set here, synchronously, so it
        # actually paints before the join starts) until _on_stop_finished
        # fires.
        self.record_button.setEnabled(False)
        self.record_button.setText("Finishing...")
        self.status_label.setText("Finishing up...")
        self._run_async(self.controller.stop, self._on_stop_finished)

    def _on_stop_finished(self, meta) -> None:
        if isinstance(meta, Exception):
            meta = None  # controller.stop() never raises; guards _run_async's own contract
        self._apply_stopped_ui(meta)

    def _apply_stopped_ui(self, meta) -> None:
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
            # restart_uploader() can block for up to UploadWorker's stop()
            # join_timeout (5s) if an upload is in flight -- same freeze risk
            # as controller.stop(), so it gets the same async treatment. The
            # settings button is disabled meanwhile so a second click can't
            # start an overlapping restart.
            self.settings_button.setEnabled(False)
            self.status_label.setText("Applying settings...")
            self._run_async(self.controller.restart_uploader, self._on_uploader_restarted)

    def _on_uploader_restarted(self, result) -> None:
        self.settings_button.setEnabled(True)
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

    def _queue_note(self) -> str:
        q = self.controller.queue_status()
        bits = []
        if q.get("pending"):
            bits.append(f"{q['pending']} upload{'s' if q['pending'] != 1 else ''} pending")
        if q.get("failed"):
            bits.append(f"{q['failed']} failed")
        if bits and q.get("last_error"):
            bits.append(_short_upload_error(q["last_error"]))
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
