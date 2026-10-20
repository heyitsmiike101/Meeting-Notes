"""Installs and updates GPU support in the background whenever the GPU helper is on.

The main window owns one :class:`GpuAutoInstaller`. :meth:`check` (start-up, hourly, after Settings are saved, when the
helper reports "not installed" / "update needed", after a recording ends) decides off the GUI thread whether the
runtime is missing, out of date against the server's pack, or has no helper key, and starts a
:class:`gpu_runtime.GpuRuntimeInstaller` when it should. The install belongs to the window, not to the Settings
dialog, so closing Settings never stops it. A failed install is retried after 5 minutes, 30 minutes, then every 2 hours.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, Qt, QThread, Signal

from meeting_notes import config as config_mod
from meeting_notes.client import gpu_runtime

log = logging.getLogger("meeting_notes.client.gpu_auto")

FAILURE_BACKOFF_SECONDS = (5 * 60, 30 * 60, 2 * 60 * 60)
KEY_RESET_GAP_SECONDS = 10 * 60  # a key the server keeps refusing is not re-registered in a loop


class _StopBridge(QObject):
    """Lets the install thread ask the GUI thread to stop the runtime and wait for it (blocking queued connection)."""

    request = Signal()


class GpuAutoInstaller(QObject):
    """``progress(step, done, total)``, ``finished(ok, message)`` and ``busy_changed(bool)`` follow an install."""

    progress = Signal(str, object, object)
    finished = Signal(bool, str)
    busy_changed = Signal(bool)
    _decided = Signal(object)  # worker thread -> GUI thread

    def __init__(
        self,
        parent: Optional[QObject] = None,
        *,
        supervisor: Any = None,
        on_changed: Optional[Callable[[], None]] = None,
        installer_factory: Optional[Callable[..., Any]] = None,
        pack_status: Optional[Callable[[str, str], Optional[str]]] = None,
        register: Optional[Callable[[str, str], None]] = None,
        clock: Callable[[], float] = time.monotonic,
        backoff=FAILURE_BACKOFF_SECONDS,
    ):
        super().__init__(parent)
        self._supervisor = supervisor
        self._on_changed = on_changed
        self._factory = installer_factory
        self._pack_status = pack_status
        self._register = register
        self._clock = clock
        self._backoff = tuple(backoff)
        self._installer: Any = None
        self._busy = False
        self._checking = False
        self._recording = False
        self._deferred = False  # a check was skipped while recording
        self._failures = 0
        self._blocked_until = 0.0
        self._cancelled = False  # Cancel pauses automatic retries until a settings change or restart
        self._last_key_reset: Optional[float] = None
        self._runtime_stopped = False
        self._verb = "Installing"
        self._last_message = ""
        self._last_ok = True
        self._stop_bridge = _StopBridge(self)
        self._stop_bridge.request.connect(self._stop_now, Qt.BlockingQueuedConnection)
        self._decided.connect(self._on_decided)

    # -- state -------------------------------------------------------------------------------------------------

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def last_message(self) -> str:
        return self._last_message

    @property
    def last_ok(self) -> bool:
        return self._last_ok

    def _set_busy(self, busy: bool) -> None:
        if busy != self._busy:
            self._busy = busy
            self.busy_changed.emit(busy)

    def set_recording(self, recording: bool) -> None:
        """No new automatic install starts while this app records; one skipped for that runs when recording ends."""
        recording = bool(recording)
        was, self._recording = self._recording, recording
        if was and not recording and self._deferred:
            self._deferred = False
            self.check()

    def settings_changed(self) -> None:
        """Settings were saved: forget failures and a Cancel, then look again (switching the helper on installs now)."""
        self._failures = 0
        self._blocked_until = 0.0
        self._cancelled = False
        self.check()

    def cancel(self) -> None:
        """Stop the running install and pause automatic retries until the next settings change or restart."""
        self._cancelled = True
        if self._installer is not None:
            self._installer.cancel()

    # -- deciding ----------------------------------------------------------------------------------------------

    def check(self, force: bool = False) -> bool:
        """Look whether GPU support needs installing, updating or a new key, and do it in the background.

        ``force`` (Install now, a rejected key) ignores a recording in progress, the failure backoff and a Cancel.
        Returns whether a look was started."""
        if os.environ.get("MEETING_NOTES_NO_GPU_HELPER"):
            return False
        if self._busy or self._checking:
            return False
        if not config_mod.gpu_helper_settings()["enabled"] or not gpu_runtime.supported():
            return False
        server = config_mod.server_settings()
        url = str(server.get("url") or "").strip().rstrip("/")
        token = str(server.get("token") or "").strip()
        if not url:
            return False
        if not force:
            if self._cancelled:
                return False
            if self._recording:
                self._deferred = True
                return False
            if self._clock() < self._blocked_until:
                return False
        else:
            self._cancelled = False
        has_key = bool(config_mod.gpu_helper_settings()["key"])
        self._checking = True
        threading.Thread(
            target=self._decide, args=(url, token, has_key), daemon=True, name="gpu-auto-check"
        ).start()
        return True

    def _decide(self, url: str, token: str, has_key: bool) -> None:
        """Worker thread: what, if anything, to do. Never raises."""
        try:
            info = gpu_runtime.installed_info()
            if info is None:
                result: Any = ("install", url, token, False)
            else:
                pack = self._pack_status or gpu_runtime.pack_status
                server_pack = pack(url, token)
                if server_pack and server_pack != info.get("pack_version"):
                    result = ("install", url, token, True)
                elif not has_key:
                    register = self._register or gpu_runtime.register_helper
                    try:
                        register(url, token)
                        result = ("registered", True, "This PC was registered with the server again.")
                    except Exception as exc:  # noqa: BLE001
                        log.warning("GPU helper re-registration failed: %s", exc)
                        result = ("registered", False, _failure_text(exc))
                else:
                    result = None  # up to date, or the server has no pack ready yet: look again later
        except Exception:  # noqa: BLE001
            log.exception("GPU support check failed")
            result = None
        self._decided.emit(result)

    def _on_decided(self, result) -> None:
        self._checking = False
        if result is None:
            return
        if result[0] == "registered":
            _, ok, message = result
            self._remember(ok, message)
            if ok:
                self._reset_failures()
                self.finished.emit(True, message)
                self._notify_changed()
            else:
                self._note_failure()
                self.finished.emit(False, message)
            return
        _, url, token, updating = result
        self._start_install(url, token, updating)

    # -- installing --------------------------------------------------------------------------------------------

    def _start_install(self, url: str, token: str, updating: bool) -> None:
        factory = self._factory or (lambda u, t, parent=None: gpu_runtime.GpuRuntimeInstaller(u, t, parent=parent))
        installer = factory(url, token, parent=self)
        installer.progress.connect(self._on_progress)
        installer.finished.connect(self._on_finished)
        installer.before_swap = self._stop_runtime_for_swap
        self._installer = installer
        self._runtime_stopped = False
        self._verb = "Updating" if updating else "Installing"
        self._last_message = f"{self._verb} GPU support..."
        self._last_ok = True
        self._show_in_supervisor(f"{self._verb} GPU support...")
        self._set_busy(True)
        installer.start()

    def _stop_runtime_for_swap(self) -> None:
        """Called on the install thread right before it replaces ``current``: stop the running runtime (its files are
        open) and wait for it to exit. The supervisor lives on the GUI thread, so hop over and block until done."""
        if self._supervisor is None:
            return
        self._runtime_stopped = True
        if QThread.currentThread() is self.thread():
            self._supervisor.stop()
        else:
            self._stop_bridge.request.emit()  # BlockingQueuedConnection: returns once stop() has finished

    def _stop_now(self) -> None:
        if self._supervisor is not None:
            self._supervisor.stop()

    def _on_progress(self, step: str, done, total) -> None:
        if total:
            text = f"{self._verb} GPU support: {int(float(done) / float(total) * 100)}%"
        else:
            text = f"{self._verb} GPU support..."
        self._last_message = text
        self._show_in_supervisor(text)
        self.progress.emit(step, done, total)

    def _on_finished(self, ok: bool, message: str) -> None:
        installer, self._installer = self._installer, None
        self._show_in_supervisor(None)
        self._remember(ok, message)
        if ok:
            self._reset_failures()
        elif not self._cancelled:
            self._note_failure()
        self._set_busy(False)
        self.finished.emit(ok, message)
        # After a good install, or a failed one that had already stopped the runtime: start it again if enabled.
        if ok or self._runtime_stopped:
            self._notify_changed()
        self._runtime_stopped = False
        if installer is not None:
            installer.deleteLater()

    def _remember(self, ok: bool, message: str) -> None:
        self._last_ok, self._last_message = ok, message

    def _reset_failures(self) -> None:
        self._failures = 0
        self._blocked_until = 0.0

    def _note_failure(self) -> None:
        delay = self._backoff[min(self._failures, len(self._backoff) - 1)]
        self._failures += 1
        self._blocked_until = self._clock() + delay
        log.info("GPU support install failed; next automatic try in %ss", delay)

    def _show_in_supervisor(self, text: Optional[str]) -> None:
        setter = getattr(self._supervisor, "set_installing", None)
        if callable(setter):
            setter(text)

    def _notify_changed(self) -> None:
        if callable(self._on_changed):
            self._on_changed()

    # -- a key the server refused ----------------------------------------------------------------------------

    def on_key_rejected(self) -> bool:
        """The runtime says the server refused its key (revoked): forget the key and register again."""
        if self._busy:
            return False
        now = self._clock()
        if self._last_key_reset is not None and now - self._last_key_reset < KEY_RESET_GAP_SECONDS:
            self._remember(False, "The server keeps refusing this PC's helper key. Check the server's GPU helpers list.")
            return False
        self._last_key_reset = now
        from meeting_notes.client import gpu_helper

        gpu_runtime.GpuRuntimeInstaller._update_config(helper_id=None, key=None)
        gpu_helper.remove_key_file()
        return self.check(force=True)


def _failure_text(exc: Exception) -> str:
    text = str(exc).strip()
    return text or f"{type(exc).__name__}"
