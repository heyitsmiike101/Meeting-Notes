"""Runs the GPU helper runtime as a child process and reports what it is doing.

The runtime (``python -m meeting_notes.compute_helper run``, from the pack :mod:`gpu_runtime` installed) polls the
server for transcription work. This supervisor starts it when the helper is on and installed, reads its JSON status
lines, restarts it when it crashes (5 s, 30 s, then 2 min apart), pauses it while this app records and resumes it
after, and stops it on Quit or when the helper is turned off.

The runtime never sees the main server password: it gets the helper key through a key file in the settings folder.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal

from meeting_notes import config as config_mod
from meeting_notes.client import gpu_runtime
from meeting_notes.compute_helper.ipc import write_control

log = logging.getLogger("meeting_notes.client.gpu_helper")

BACKOFF_SECONDS = (5, 30, 120)
STABLE_AFTER_SECONDS = 60  # a run this long counts as healthy: the next crash starts the backoff over
STOP_WAIT_MS = 4000
KEY_FILE_NAME = "gpu-helper.key"

# Supervisor states (the "state" of a status dict).
OFF, NOT_INSTALLED, STARTING, IDLE, BUSY, PAUSED, OFFLINE, ERROR, UPDATE_NEEDED = (
    "off", "not_installed", "starting", "idle", "busy", "paused", "offline", "error", "update needed",
)
FATAL_EXIT_CODE = 2  # the runtime: bad config or refused key (revoked); restarting cannot help

# What Settings, the tray and the server's Recorders page say, word for word.
TEXT_OFF = "Off"
TEXT_NOT_INSTALLED = "GPU support is not installed"
TEXT_STARTING = "Starting"
TEXT_IDLE = "Idle"
TEXT_PAUSED = "Paused"
TEXT_PAUSED_RECORDING = "Paused while recording"
TEXT_OFFLINE = "Offline"
TEXT_UPDATE_NEEDED = "Update GPU support"

_current: Dict[str, Any] = {"text": TEXT_OFF}


def current_status_text() -> str:
    """The latest status line of the running supervisor (``Off`` when there is none), for Settings."""
    return str(_current["text"])


def key_file_path() -> Path:
    return config_mod.config_path().parent / KEY_FILE_NAME


def write_key_file(key: str) -> Path:
    """Put the helper key where the runtime reads it (only this user can read the file)."""
    path = key_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.read_text(encoding="utf-8").strip() == key:
            return path
    except OSError:
        pass
    path.write_text(key, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def remove_key_file() -> None:
    try:
        key_file_path().unlink()
    except OSError:
        pass


def status_text(state: str, job: Optional[str] = None, progress: Optional[float] = None,
                error: Optional[str] = None, paused_for_recording: bool = False) -> str:
    """One line for a state: ``Working on M-0142 · 42%``, ``Paused while recording``, ``Idle``, ``Offline``."""
    if state == BUSY:
        text = f"Working on {job}" if job else "Working"
        if isinstance(progress, (int, float)) and 0 <= progress <= 1:
            text += f" · {int(round(progress * 100))}%"
        return text
    if state == PAUSED:
        return TEXT_PAUSED_RECORDING if paused_for_recording else TEXT_PAUSED
    if state == IDLE:
        return TEXT_IDLE
    if state == OFFLINE:
        return TEXT_OFFLINE
    if state == UPDATE_NEEDED:
        return TEXT_UPDATE_NEEDED
    if state == STARTING:
        return TEXT_STARTING
    if state == NOT_INSTALLED:
        return TEXT_NOT_INSTALLED
    if state == ERROR:
        return f"Error: {error}" if error else "Error"
    return TEXT_OFF


class GpuHelperSupervisor(QObject):
    """Owns the runtime process. ``status_changed(dict)`` carries ``{state, text, job, progress, error}``."""

    status_changed = Signal(dict)

    def __init__(
        self,
        parent: Optional[QObject] = None,
        *,
        root: Optional[Path] = None,
        process_factory: Optional[Callable[[], Any]] = None,
        server_url: Optional[Callable[[], str]] = None,
        backoff=BACKOFF_SECONDS,
    ):
        super().__init__(parent)
        self.root = Path(root) if root is not None else gpu_runtime.runtime_root()
        self._factory = process_factory or QProcess
        self._server_url = server_url or (lambda: str(config_mod.server_settings().get("url") or ""))
        self._backoff = tuple(backoff)
        self._proc: Any = None
        self._wanted = False
        self._buffer = b""
        self._started_at = 0.0
        self._crashes = 0
        self._paused_user = False
        self._paused_recording = False
        self._sent_paused = False
        self.restart_delay_ms: Optional[int] = None  # set while a restart is scheduled
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._on_restart_timer)
        self.status: Dict[str, Any] = self._make_status(OFF)
        _current["text"] = self.status["text"]

    # -- status ----------------------------------------------------------------------------------------------

    def _make_status(self, state: str, job=None, progress=None, error=None) -> Dict[str, Any]:
        return {
            "state": state, "job": job, "progress": progress, "error": error,
            "text": status_text(state, job, progress, error, self._paused_recording and self._effective_paused()),
        }

    def _set_status(self, state: str, job=None, progress=None, error=None) -> None:
        status = self._make_status(state, job, progress, error)
        if status == self.status:
            return
        self.status = status
        _current["text"] = status["text"]
        self.status_changed.emit(dict(status))

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.state() != QProcess.NotRunning

    @property
    def wanted(self) -> bool:
        return self._wanted

    @property
    def paused(self) -> bool:
        return self._effective_paused()

    def _effective_paused(self) -> bool:
        return self._paused_user or self._paused_recording

    # -- start / stop ----------------------------------------------------------------------------------------

    def start(self) -> bool:
        """Start (or keep) the runtime. Returns False, with a status saying why, when it cannot run."""
        self._wanted = True
        if self.running:
            return True
        return self._spawn()

    def _spawn(self) -> bool:
        self._timer.stop()
        self.restart_delay_ms = None
        settings = config_mod.gpu_helper_settings()
        if not gpu_runtime.supported():
            self._wanted = False
            self._set_status(OFF)
            return False
        if not gpu_runtime.is_installed(self.root):
            self._wanted = False
            self._set_status(NOT_INSTALLED)
            return False
        url = self._server_url().strip().rstrip("/")
        if not settings["key"] or not url:
            self._wanted = False
            self._set_status(ERROR, error="Set the server address and install GPU support again")
            return False
        base = gpu_runtime.current_dir(self.root)
        try:
            key_file = write_key_file(settings["key"])
        except OSError as exc:
            self._wanted = False
            self._set_status(ERROR, error=f"Could not save the helper key: {exc.strerror or exc}")
            return False
        proc = self._factory()
        env = QProcessEnvironment.systemEnvironment()
        for name, value in gpu_runtime.runtime_env(base).items():
            env.insert(name, value)
        proc.setProcessEnvironment(env)
        proc.setProgram(str(gpu_runtime.venv_python(base)))
        proc.setArguments([
            "-m", "meeting_notes.compute_helper", "run", "--server", url, "--key-file", str(key_file),
            "--models", str(gpu_runtime.models_dir(base)),
            "--pack-version", str(settings["installed_pack_version"] or (gpu_runtime.installed_info(self.root) or {}).get("pack_version") or ""),
            "--work-dir", str(self.root / "work"),
            # Commands go through this file, never stdin (see compute_helper/ipc.py for why).
            "--control-file", str(self._control_path()),
            "--parent-pid", str(os.getpid()),
        ])
        proc.readyReadStandardOutput.connect(self._on_stdout)
        proc.readyReadStandardError.connect(self._on_stderr)
        proc.finished.connect(self._on_finished)
        proc.errorOccurred.connect(self._on_error)
        self._proc = proc
        self._buffer = b""
        self._sent_paused = self._effective_paused()
        self._write_control(paused=self._sent_paused)
        self._started_at = time.monotonic()
        self._set_status(STARTING)
        proc.start()
        log.info("GPU helper runtime started")
        return True

    def stop(self) -> None:
        """Ask the runtime to stop, and kill it if it does not."""
        self._wanted = False
        self._timer.stop()
        self.restart_delay_ms = None
        proc, self._proc = self._proc, None
        if proc is not None and proc.state() != QProcess.NotRunning:
            self._write_control(paused=self._sent_paused, stop=True)
            if not proc.waitForFinished(STOP_WAIT_MS):
                proc.kill()
                proc.waitForFinished(2000)
        self._set_status(OFF)

    # -- commands --------------------------------------------------------------------------------------------

    def _control_path(self) -> Path:
        return self.root / "control.json"

    def _write_control(self, *, paused: bool, stop: bool = False) -> None:
        try:
            write_control(self._control_path(), paused=paused, stop=stop)
        except OSError:
            log.debug("could not write the GPU helper control file", exc_info=True)

    def _sync_pause(self) -> None:
        """Tell the runtime about the effective pause, once per change."""
        want = self._effective_paused()
        if self.running and want != self._sent_paused:
            self._write_control(paused=want)
            self._sent_paused = want
        if self.running or self._wanted:
            current = self.status["state"]
            if want and current in (IDLE, BUSY, STARTING, PAUSED):
                self._set_status(PAUSED, self.status.get("job"), self.status.get("progress"))
            elif not want and current == PAUSED:
                self._set_status(IDLE)

    def set_user_paused(self, paused: bool) -> None:
        """The tray's Pause / Resume GPU helper."""
        self._paused_user = bool(paused)
        self._sync_pause()

    def set_recording(self, recording: bool) -> None:
        """Pause while this client records, resume after."""
        self._paused_recording = bool(recording)
        self._sync_pause()

    # -- the process -----------------------------------------------------------------------------------------

    def _on_stdout(self) -> None:
        proc = self._proc
        if proc is None:
            return
        self._buffer += bytes(proc.readAllStandardOutput())
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            self._handle_line(line.decode("utf-8", "replace").strip())

    def _on_stderr(self) -> None:
        proc = self._proc
        if proc is None:
            return
        text = bytes(proc.readAllStandardError()).decode("utf-8", "replace").strip()
        if text:
            log.info("gpu helper: %s", text[-500:])

    def _handle_line(self, line: str) -> None:
        if not line.startswith("{"):
            if line:
                log.info("gpu helper: %s", line[:300])
            return
        try:
            event = json.loads(line)
        except ValueError:
            return
        if not isinstance(event, dict) or event.get("event") != "status":
            return
        state = str(event.get("state") or "")
        board = event.get("board") if isinstance(event.get("board"), str) and event.get("board") else None
        job = board or (event.get("job") if isinstance(event.get("job"), str) else None)
        progress = event.get("progress") if isinstance(event.get("progress"), (int, float)) else None
        error = event.get("error") if isinstance(event.get("error"), str) and event.get("error") else None
        if state not in (IDLE, BUSY, PAUSED, OFFLINE, ERROR, UPDATE_NEEDED, "installing"):
            return
        if state in ("installing", "working"):
            state = BUSY
        # The runtime and this app agree on pausing; if the runtime comes up unpaused, push our state now.
        if self._effective_paused() and state in (IDLE, BUSY) and not self._sent_paused:
            self._write_control(paused=True)
            self._sent_paused = True
        if self._effective_paused() and state == IDLE:
            state = PAUSED
        self._set_status(state, job, progress, error)

    def _on_error(self, _error=None) -> None:
        proc = self._proc
        if proc is not None and proc.state() == QProcess.NotRunning:
            self._on_finished(-1)

    def _on_finished(self, exit_code=0, _status=None) -> None:
        proc, self._proc = self._proc, None
        if proc is None or not self._wanted:
            return
        uptime = time.monotonic() - self._started_at
        if exit_code == FATAL_EXIT_CODE:
            msg = self.status.get("error") or "The server refused this helper. Install GPU support again."
            log.error("GPU helper runtime exited with a fatal error: %s", msg)
            self._wanted = False
            self._set_status(ERROR, error=msg)
            return
        if uptime >= STABLE_AFTER_SECONDS:
            self._crashes = 0
        delay = self._backoff[min(self._crashes, len(self._backoff) - 1)]
        self._crashes += 1
        log.warning("GPU helper runtime exited (code %s) after %.0fs; restarting in %ss", exit_code, uptime, delay)
        self.restart_delay_ms = int(delay * 1000)
        self._set_status(OFFLINE)
        self._timer.start(self.restart_delay_ms)

    def _on_restart_timer(self) -> None:
        if self._wanted:
            self._spawn()
