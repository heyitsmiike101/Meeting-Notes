"""Entry point for the desktop recorder: `meeting-notes-ui`."""

from __future__ import annotations

import logging
import sys
import traceback
from pathlib import Path


def main(argv=None) -> int:
    args = list(argv if argv is not None else sys.argv)
    smoke_test = "--smoke-test" in args
    if smoke_test:
        args.remove("--smoke-test")
        # Exercise the bundled Qt platform plugin without displaying or
        # recording anything. Release CI uses this to reject broken packages.
        import os

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    if not smoke_test:
        _start_logging()

    from PySide6.QtWidgets import QApplication

    from meeting_notes.client.ui.main_window import MainWindow
    from meeting_notes import config as config_mod
    from meeting_notes.client.ui import theme

    # Reuse an existing application when embedded by a test/launcher. The
    # packaged executable still creates exactly one, while this avoids a
    # libshiboken singleton crash in integration tests that already own Qt.
    app = QApplication.instance() or QApplication(args)
    app.setApplicationName("Meeting Notes")
    # Bundled Inter, registered before any window exists. If loading fails the
    # stylesheet's font stack falls back to Segoe UI.
    theme.load_fonts()
    # Light / Dark / System from the client config; applied on the application
    # so dialogs inherit it too, and re-applied when the OS app mode flips.
    theme.apply_appearance(config_mod.appearance_setting(), app)
    if smoke_test:
        # Importing MainWindow above exercises all client-side imports. A real
        # widget also forces QtWidgets and the platform plugin to initialize.
        # Probe both SoundCard enumeration paths too. Empty device lists are
        # valid on CI/build hosts; an import or backend failure is not and
        # should fail the release before it reaches an end user.
        from meeting_notes.audio import soundcard_source

        soundcard = soundcard_source.import_soundcard()
        # Hosted build workers need not have audio hardware. Importing the
        # backend is the packaging invariant; endpoint failures are an OS
        # condition and are captured by the runtime diagnostic log.
        probes = [lambda: soundcard.all_microphones(), lambda: soundcard.all_speakers()]
        if sys.platform != "darwin":  # macOS has no loopback; asking only logs a warning
            probes.insert(1, lambda: soundcard.all_microphones(include_loopback=True))
        for probe_call in probes:
            try:
                probe_call()
            except Exception:
                pass
        if sys.platform == "darwin":
            # The PyObjC bindings behind system audio, call detection and the
            # Trash must be inside the bundle: a missing framework package
            # would otherwise only surface mid-meeting.
            import AppKit  # noqa: F401
            import AVFoundation  # noqa: F401  (microphone permission status)
            import CoreMedia  # noqa: F401
            import Foundation  # noqa: F401
            import Quartz  # noqa: F401
            import ScreenCaptureKit  # noqa: F401

            from meeting_notes.audio import screencapture_source
            from meeting_notes.client import meeting_detect_mac

            available, why = screencapture_source.available()
            if not available:
                raise RuntimeError(f"ScreenCaptureKit bindings unusable: {why}")
            screencapture_source._handler_class()
            meeting_detect_mac.list_audio_processes()
        from PySide6.QtWidgets import QWidget

        probe = QWidget()
        probe.setWindowTitle("Meeting Notes package self-test")
        probe.close()
        return 0

    window = MainWindow()
    _install_quit_cleanup(app, window)
    _install_power_hooks(window)
    window.show()
    return app.exec()


def _install_power_hooks(window) -> None:
    """macOS: tell the controller about sleep and wake (a lid closed mid-recording). No-op elsewhere."""
    controller = getattr(window, "controller", None)
    if controller is None:
        return
    try:
        from meeting_notes.client import power_mac

        power_mac.install(controller.on_system_sleep, controller.on_system_wake)
    except Exception:  # noqa: BLE001
        logging.getLogger("meeting_notes.client.app").debug("power hooks unavailable", exc_info=True)


def _install_quit_cleanup(app, window) -> None:
    """Make quitting (Cmd+Q, Dock Quit, last window closed, interpreter exit) always release audio capture.

    The window's close path already stops a recording and the idle meter; this is the backstop for every
    other way out. On macOS an unreleased ScreenCaptureKit stream keeps the purple screen-recording
    indicator on, so each exit also stops all streams, and a watchdog force-exits if the process
    lingers (a thread wedged in a native call would otherwise keep the app, and the indicator, alive).
    """
    import atexit
    import logging
    import os
    import threading

    log = logging.getLogger("meeting_notes.client.app")
    state = {"done": False}

    def cleanup() -> None:
        if state["done"]:
            return
        state["done"] = True
        controller = getattr(window, "controller", None)
        try:
            if controller is not None:
                controller.shutdown()
            else:
                from meeting_notes.audio import screencapture_source

                screencapture_source.stop_all_streams()
        except Exception:  # noqa: BLE001
            log.debug("quit cleanup failed", exc_info=True)

    def on_about_to_quit() -> None:
        cleanup()
        if sys.platform == "darwin":
            timer = threading.Timer(8.0, lambda: os._exit(0))
            timer.daemon = True
            timer.start()

    app.aboutToQuit.connect(on_about_to_quit)
    atexit.register(cleanup)


def _start_logging() -> None:
    """Rotating client log + exception hooks, then one line saying who started.

    The token is deliberately not logged (and the log's filter would mask it).
    """
    import logging
    import platform

    from meeting_notes import __version__
    from meeting_notes import config as config_mod
    from meeting_notes.client import logsetup

    path = logsetup.setup_logging()
    log = logging.getLogger("meeting_notes.client.app")
    try:
        server = config_mod.server_settings()
        log.info(
            "startup: v%s on %s %s, python %s, exe=%s, save_dir=%s, server=%s, log=%s",
            __version__,
            platform.system(),
            platform.release(),
            platform.python_version(),
            sys.executable,
            config_mod.save_dir(),
            server.get("url") or "(not configured)",
            path,
        )
    except Exception:  # noqa: BLE001 - logging must never stop startup
        pass
    try:
        from meeting_notes.client import logs as logs_mod

        problem = logs_mod.update_log_problem()
        if problem:
            log.warning("the last self-update did not finish cleanly (update.log): %s", problem)
    except Exception:  # noqa: BLE001
        pass


def _report_startup_error(exc: Exception, *, show_dialog: bool = True) -> Path:
    """Persist a packaged-client startup failure where a standard user can read it."""
    log_dir = Path.home() / ".meeting-notes"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "client-startup-error.log"
    try:
        import logging

        logging.getLogger("meeting_notes.client.app").critical(
            "startup failed", exc_info=(type(exc), exc, exc.__traceback__)
        )
    except Exception:  # noqa: BLE001
        pass
    log_path.write_text(
        "Meeting Notes could not start.\n\n" + "".join(traceback.format_exception(exc)),
        encoding="utf-8",
    )
    if not show_dialog:
        return log_path
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox

        app = QApplication.instance() or QApplication(sys.argv)
        QMessageBox.critical(
            None,
            "Meeting Notes could not start",
            f"The startup error was written to:\n{log_path}",
        )
    except Exception:
        # If Qt itself is the missing dependency, the readable log is still
        # available and PyInstaller/console builds can report the exit code.
        pass
    return log_path


def run(argv=None) -> int:
    """Run the client and make windowed-build startup failures discoverable."""
    try:
        return main(argv)
    except Exception as exc:  # a windowed executable otherwise fails invisibly
        _report_startup_error(exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
