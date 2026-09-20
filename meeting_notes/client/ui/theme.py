"""One stylesheet for the whole app, applied on the QApplication.

It lives here rather than on the main window because a stylesheet set on a
single widget does not reach dialogs: they are top-level windows of their own.
Styling only the main window is what leaves a dialog with its native light
background while inheriting light-on-light label colours -- invisible text.
"""

from __future__ import annotations

BACKGROUND = "#0f1419"
SURFACE = "#161b22"
BORDER = "#263140"
TEXT = "#e6edf3"
MUTED = "#8b98a5"

APP_STYLE = f"""
QWidget#root, QDialog {{ background: {BACKGROUND}; }}
QLabel {{ color: {TEXT}; background: transparent; }}
QLabel#subtle {{ color: {MUTED}; font-size: 11px; }}
QLabel#clock {{ color: {TEXT}; font-size: 26px; font-weight: 600; }}
QCheckBox {{ color: {TEXT}; background: transparent; spacing: 8px; }}
QLineEdit, QPlainTextEdit, QComboBox {{
    background: {SURFACE}; color: {TEXT}; border: 1px solid {BORDER};
    border-radius: 6px; padding: 6px; selection-background-color: #1f6feb;
}}
QComboBox QAbstractItemView {{
    background: {SURFACE}; color: {TEXT}; selection-background-color: #1f6feb;
    border: 1px solid {BORDER};
}}
QPushButton {{
    background: #21262d; color: {TEXT}; border: 1px solid #30363d;
    border-radius: 6px; padding: 8px 16px;
}}
QPushButton:hover {{ background: #30363d; }}
QPushButton:disabled {{ color: {MUTED}; background: #1b1f24; }}
QPushButton#record {{ background: #238636; border-color: #2ea043; font-weight: 600; }}
QPushButton#record:hover {{ background: #2ea043; }}
QPushButton#recording {{ background: #b62324; border-color: #da3633; font-weight: 600; }}
QScrollBar:vertical {{ background: {BACKGROUND}; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: #30363d; border-radius: 5px; min-height: 24px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
"""
