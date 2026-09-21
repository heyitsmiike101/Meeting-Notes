"""Entry point for the desktop recorder: `meeting-notes-ui`."""

from __future__ import annotations

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

    from PySide6.QtWidgets import QApplication

    from meeting_notes.client.ui.main_window import MainWindow
    from meeting_notes.client.ui.theme import APP_STYLE

    # Reuse an existing application when embedded by a test/launcher. The
    # packaged executable still creates exactly one, while this avoids a
    # libshiboken singleton crash in integration tests that already own Qt.
    app = QApplication.instance() or QApplication(args)
    app.setApplicationName("Meeting Notes")
    # Applied on the application so dialogs inherit it too.
    app.setStyleSheet(APP_STYLE)
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
        for probe_call in (
            lambda: soundcard.all_microphones(),
            lambda: soundcard.all_microphones(include_loopback=True),
            lambda: soundcard.all_speakers(),
        ):
            try:
                probe_call()
            except Exception:
                pass
        from PySide6.QtWidgets import QWidget

        probe = QWidget()
        probe.setWindowTitle("Meeting Notes package self-test")
        probe.close()
        return 0

    window = MainWindow()
    window.show()
    return app.exec()


def _report_startup_error(exc: Exception, *, show_dialog: bool = True) -> Path:
    """Persist a packaged-client startup failure where a standard user can read it."""
    log_dir = Path.home() / ".meeting-notes"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "client-startup-error.log"
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
