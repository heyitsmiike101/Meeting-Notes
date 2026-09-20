"""Entry point for the desktop recorder: `meeting-notes-ui`."""

from __future__ import annotations

import sys


def main(argv=None) -> int:
    from PySide6.QtWidgets import QApplication

    from meeting_notes.client.ui.main_window import MainWindow
    from meeting_notes.client.ui.theme import APP_STYLE

    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("Meeting Notes")
    # Applied on the application so dialogs inherit it too.
    app.setStyleSheet(APP_STYLE)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
