"""One stylesheet for the whole app, applied on the QApplication.

The client is a studio recording console: graphite chrome, with card stock
wherever a document lives (the live preview, the settings and history sheets).
Grease-pencil red is the only accent -- the record transport, focus rings and
selection. The same world is used by the server web UI.

The stylesheet lives here rather than on the main window because a stylesheet
set on a single widget does not reach dialogs: they are top-level windows of
their own. Styling only the main window is what leaves a dialog with its native
light background while inheriting light-on-light label colours -- invisible text.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

# -- palette -------------------------------------------------------------------
GRAPHITE = "#1d1f22"         # console chrome
PANEL = "#26292d"            # grouped areas on the chrome
PANEL_RAISED = "#2f3338"     # buttons and menus on the chrome
WELL = "#17191b"             # inputs sunk into a panel
RULE = "#3d4247"             # hairlines on graphite
INK = "#ece6d6"              # text on graphite (warm, from the card stock)
MUTED = "#a6a396"            # secondary text on graphite
DISABLED = "#77756d"

CARD = "#f1ead9"             # card stock: documents
CARD_FIELD = "#f8f3e6"
KRAFT = "#d9c8a5"            # spines, rules, secondary panels
KRAFT_DEEP = "#b9a578"
CARD_INK = "#24211b"
CARD_MUTED = "#5f5747"

RED = "#c8372d"              # grease pencil: the only accent
RED_HOT = "#d9493e"
RED_DEEP = "#a52c23"

# Kept for callers that imported the earlier names.
BACKGROUND = GRAPHITE
SURFACE = PANEL
BORDER = RULE
TEXT = INK

SANS = '"Barlow", "Segoe UI", sans-serif'
CONDENSED = '"Barlow Condensed", "Segoe UI Semibold", "Segoe UI", sans-serif'

FONT_FILES = (
    "Barlow-Regular.ttf",
    "Barlow-Medium.ttf",
    "Barlow-SemiBold.ttf",
    "BarlowCondensed-Medium.ttf",
    "BarlowCondensed-SemiBold.ttf",
)


def load_fonts() -> list:
    """Register the bundled Barlow files with Qt; never raises.

    Needs a QGuiApplication to exist. If a file is missing (a packaging slip) or
    Qt refuses it, the stylesheet's font stacks fall back to Segoe UI.
    """
    families: set = set()
    try:
        from PySide6.QtGui import QFontDatabase

        folder = Path(__file__).resolve().parent / "fonts"
        for name in FONT_FILES:
            font_id = QFontDatabase.addApplicationFont(str(folder / name))
            if font_id >= 0:
                families.update(QFontDatabase.applicationFontFamilies(font_id))
    except Exception:  # noqa: BLE001 - fonts are cosmetic
        return []
    return sorted(families)


def _check_mark_url() -> str:
    """A small tick image for checked boxes (Qt stylesheets can only use files)."""
    try:
        from PySide6.QtCore import QPointF, Qt
        from PySide6.QtGui import QColor, QImage, QPainter, QPen

        path = Path(tempfile.gettempdir()) / "meeting-notes-ui-check-v1.png"
        if not path.exists():
            image = QImage(36, 36, QImage.Format_ARGB32)
            image.fill(Qt.transparent)
            painter = QPainter(image)
            painter.setRenderHint(QPainter.Antialiasing, True)
            pen = QPen(QColor("#ffffff"), 4.2)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            painter.setPen(pen)
            painter.drawPolyline([QPointF(8, 19), QPointF(15, 26), QPointF(28, 10)])
            painter.end()
            image.save(str(path))
        if path.exists():
            return path.as_posix()
    except Exception:  # noqa: BLE001 - a filled box still reads as checked
        pass
    return ""


_CHECK = _check_mark_url()
_CHECK_IMAGE = f"image: url({_CHECK});" if _CHECK else ""

APP_STYLE = f"""
QWidget#root, QDialog {{ background: {GRAPHITE}; }}
QLabel, QCheckBox, QPushButton, QToolButton, QLineEdit, QPlainTextEdit, QListWidget, QMenu {{
    font-family: {SANS}; font-size: 14px;
}}
QToolTip {{
    background: {CARD}; color: {CARD_INK}; border: 1px solid {KRAFT_DEEP};
    padding: 4px 6px; font-family: {SANS}; font-size: 13px;
}}
QLabel {{ color: {INK}; background: transparent; }}
QLabel#subtle {{ color: {MUTED}; font-size: 12px; }}
QLabel#brand {{ font-family: {CONDENSED}; font-size: 22px; font-weight: 600; color: {INK}; }}
QLabel#version {{ color: {MUTED}; font-size: 12px; }}
QLabel#legend {{
    font-family: {CONDENSED}; font-size: 13px; font-weight: 600; color: {MUTED};
}}
QLabel#clock {{
    font-family: {CONDENSED}; font-size: 46px; font-weight: 600; color: #8a877c;
}}
QLabel#clock[live="true"] {{ color: {INK}; }}
QLabel#devices {{ color: {MUTED}; font-size: 13px; }}
QLabel#status {{ color: {MUTED}; font-size: 13px; }}
QLabel#historyHeading, QLabel#sheetTitle {{
    font-family: {CONDENSED}; font-size: 22px; font-weight: 600; color: {INK};
}}
QLabel#historyHeading {{ color: {CARD_INK}; font-size: 20px; }}

QFrame#topbar {{ background: {GRAPHITE}; border: none; border-bottom: 1px solid {RULE}; }}
QFrame#transport {{
    background: {PANEL}; border: 1px solid {RULE}; border-radius: 6px;
}}
QFrame#transport QLabel {{ background: transparent; }}

/* --- buttons (console) --------------------------------------------------- */
QPushButton {{
    background: {PANEL_RAISED}; color: {INK}; border: 1px solid {RULE};
    border-radius: 4px; padding: 7px 14px; font-weight: 500;
}}
QPushButton:hover {{ background: #3a4046; }}
QPushButton:pressed {{ background: #2a2e32; }}
QPushButton:focus {{ border: 2px solid {RED}; padding: 6px 13px; }}
QPushButton:disabled {{ color: {DISABLED}; background: {PANEL}; border-color: {RULE}; }}
QPushButton:default {{ background: {RED}; border-color: {RED_HOT}; color: #ffffff; font-weight: 600; }}
QPushButton:default:hover {{ background: {RED_HOT}; }}
QPushButton:default:focus {{ border: 2px solid {CARD}; padding: 6px 13px; }}

QPushButton#tool, QToolButton#more {{
    background: transparent; border: 1px solid transparent; color: {INK};
}}
QPushButton#tool:hover, QToolButton#more:hover {{ background: {PANEL_RAISED}; border-color: {RULE}; }}
QPushButton#tool:pressed, QToolButton#more:pressed {{ background: {PANEL}; }}
QPushButton#tool:disabled {{ color: {DISABLED}; background: transparent; border-color: transparent; }}
QToolButton#more {{ border-radius: 4px; padding: 6px 8px; }}
QToolButton#more:focus, QToolButton#more:open {{ border: 2px solid {RED}; padding: 5px 7px; }}
QToolButton#more:open {{ border-color: {RULE}; background: {PANEL_RAISED}; padding: 6px 8px; }}
QToolButton#more::menu-indicator {{ image: none; width: 0; }}

QFrame#updateBar {{ background: {KRAFT}; border: 1px solid {KRAFT_DEEP}; border-radius: 5px; }}
QLabel#updateNote {{ color: {CARD_INK}; font-weight: 500; }}
QPushButton#update {{
    background: {GRAPHITE}; color: {INK}; border: 1px solid {GRAPHITE}; font-weight: 600;
}}
QPushButton#update:hover {{ background: #33373c; }}
QPushButton#update:disabled {{ background: #6d685c; color: #cfc8b6; border-color: #6d685c; }}
QPushButton#update:focus {{ border: 2px solid {RED}; padding: 6px 13px; }}

/* Record transport: outlined red while idle, solid red while recording. */
QPushButton#record, QPushButton#recording {{
    font-size: 17px; font-weight: 600; padding: 9px 22px; border-radius: 5px;
    border: 2px solid {RED}; min-height: 28px;
}}
QPushButton#record {{ background: {WELL}; color: {INK}; }}
QPushButton#record:hover {{ background: #35201f; color: #ffffff; border-color: {RED_HOT}; }}
QPushButton#record:pressed {{ background: {RED_DEEP}; color: #ffffff; }}
QPushButton#record:focus {{ border: 2px solid {CARD}; padding: 9px 22px; background: {WELL}; }}
QPushButton#record:disabled {{ background: {PANEL}; color: {DISABLED}; border-color: #5a3532; }}
QPushButton#recording {{ background: {RED}; border-color: {RED_HOT}; color: #ffffff; }}
QPushButton#recording:hover {{ background: {RED_HOT}; }}
QPushButton#recording:pressed {{ background: {RED_DEEP}; }}
QPushButton#recording:focus {{ border: 2px solid {CARD}; padding: 9px 22px; background: {RED}; }}
QPushButton#recording:disabled {{ background: {RED_DEEP}; color: #f0d6d2; border-color: {RED_DEEP}; }}

/* Lane mute keys, one per track. */
QPushButton#mute_mic, QPushButton#mute_system {{
    background: {PANEL}; color: {INK}; border: 1px solid {RULE};
    padding: 5px 10px; min-width: 78px; font-size: 13px;
}}
QPushButton#mute_mic:hover, QPushButton#mute_system:hover {{ background: {PANEL_RAISED}; }}
QPushButton#mute_mic:focus, QPushButton#mute_system:focus {{ border: 2px solid {RED}; padding: 4px 9px; }}
QPushButton#mute_mic:disabled, QPushButton#mute_system:disabled {{
    color: #8f8c81; background: transparent; border: 1px dashed #5b6167;
}}
QPushButton#mute_mic:checked, QPushButton#mute_system:checked {{
    background: {CARD}; color: {CARD_INK}; border-color: {KRAFT}; font-weight: 600;
}}

/* --- fields -------------------------------------------------------------- */
QLineEdit {{
    background: {WELL}; color: {INK}; border: 1px solid {RULE}; border-radius: 4px;
    padding: 8px 10px; selection-background-color: {RED}; selection-color: #ffffff;
    placeholder-text-color: {DISABLED}; lineedit-password-character: 42;
}}
QLineEdit:focus {{ border: 2px solid {RED}; padding: 7px 9px; }}
QLineEdit:disabled {{ color: {DISABLED}; }}

QPlainTextEdit {{
    background: {WELL}; color: {INK}; border: 1px solid {RULE}; border-radius: 4px;
    padding: 8px; selection-background-color: {RED}; selection-color: #ffffff;
    placeholder-text-color: {DISABLED};
}}
QPlainTextEdit:focus {{ border: 2px solid {RED}; padding: 7px; }}

/* The live preview reads as a track-sheet card. */
QPlainTextEdit#preview {{
    background: {CARD}; color: {CARD_INK}; border: 1px solid {KRAFT_DEEP}; border-radius: 3px;
    padding: 10px 14px; font-size: 15px; placeholder-text-color: {CARD_MUTED};
}}
QPlainTextEdit#preview:focus {{ border: 2px solid {RED}; padding: 9px 13px; }}

QCheckBox {{ color: {INK}; background: transparent; spacing: 10px; }}
QCheckBox::indicator {{
    width: 16px; height: 16px; border: 1px solid {MUTED}; border-radius: 3px; background: {WELL};
}}
QCheckBox::indicator:hover {{ border-color: {INK}; }}
QCheckBox::indicator:checked {{ background: {RED}; border-color: {RED}; {_CHECK_IMAGE} }}
QCheckBox:focus {{ color: #ffffff; }}
QCheckBox::indicator:focus {{ border: 2px solid {RED_HOT}; }}
QCheckBox::indicator:checked:focus {{ border: 2px solid {CARD}; }}

/* --- menus, scrollbars, splitter ---------------------------------------- */
QMenu {{
    background: {PANEL_RAISED}; color: {INK}; border: 1px solid {RULE}; padding: 4px;
}}
QMenu::item {{ padding: 8px 26px 8px 14px; border-radius: 3px; }}
QMenu::item:selected {{ background: {RED}; color: #ffffff; }}
QMenu::item:disabled {{ color: {DISABLED}; }}
QMenu::separator {{ height: 1px; background: {RULE}; margin: 4px 6px; }}

QScrollBar:vertical {{ background: transparent; width: 12px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {KRAFT_DEEP}; border-radius: 4px; min-height: 28px; margin: 2px 2px; }}
QScrollBar::handle:vertical:hover {{ background: {KRAFT}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QScrollBar:horizontal {{ background: transparent; height: 12px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: {KRAFT_DEEP}; border-radius: 4px; min-width: 28px; margin: 2px 2px; }}
QSplitter::handle {{ background: {KRAFT}; width: 1px; }}

/* --- sheets: settings and history are card stock under a graphite strip --- */
QFrame#sheetHead {{ background: {GRAPHITE}; border: none; border-bottom: 2px solid {RED}; }}
QLabel#sheetSub {{ color: {MUTED}; font-size: 12px; }}
QFrame#sheetBody {{ background: {CARD}; border: none; }}
QFrame#sheetBody QLabel {{ color: {CARD_INK}; }}
QFrame#sheetBody QLabel#subtle, QFrame#sheetBody QLabel#status {{ color: {CARD_MUTED}; }}
QFrame#sheetBody QLabel#legend {{ color: {CARD_MUTED}; }}
QFrame#sheetBody QCheckBox {{ color: {CARD_INK}; }}
QFrame#sheetBody QCheckBox::indicator {{ background: {CARD_FIELD}; border: 1px solid {CARD_MUTED}; }}
QFrame#sheetBody QCheckBox::indicator:hover {{ border-color: {CARD_INK}; }}
QFrame#sheetBody QCheckBox::indicator:checked {{ background: {RED}; border-color: {RED}; {_CHECK_IMAGE} }}
QFrame#sheetBody QCheckBox::indicator:focus {{ border: 2px solid {RED}; }}
QFrame#sheetBody QCheckBox::indicator:checked:focus {{ border: 2px solid {CARD_INK}; }}
QFrame#sheetBody QCheckBox:focus {{ color: {CARD_INK}; }}
QFrame#sheetBody QLineEdit {{
    background: {CARD_FIELD}; color: {CARD_INK}; border: 1px solid {KRAFT_DEEP};
    placeholder-text-color: {CARD_MUTED};
}}
QFrame#sheetBody QLineEdit:focus {{ border: 2px solid {RED}; padding: 7px 9px; }}
QFrame#sheetBody QPlainTextEdit {{
    background: {CARD_FIELD}; color: {CARD_INK}; border: 1px solid {KRAFT_DEEP};
    padding: 10px 14px; font-size: 15px; placeholder-text-color: {CARD_MUTED};
}}
QFrame#sheetBody QPlainTextEdit:focus {{ border: 2px solid {RED}; padding: 9px 13px; }}
QFrame#sheetBody QPushButton {{
    background: {KRAFT}; color: {CARD_INK}; border: 1px solid {KRAFT_DEEP};
}}
QFrame#sheetBody QPushButton:hover {{ background: #e6d8b8; }}
QFrame#sheetBody QPushButton:pressed {{ background: {KRAFT_DEEP}; }}
QFrame#sheetBody QPushButton:focus {{ border: 2px solid {RED}; padding: 6px 13px; }}
QFrame#sheetBody QPushButton:disabled {{
    background: transparent; color: #8d8470; border: 1px solid {KRAFT};
}}
QFrame#sheetBody QPushButton:default {{
    background: {RED}; color: #ffffff; border: 1px solid {RED_DEEP}; font-weight: 600;
}}
QFrame#sheetBody QPushButton:default:hover {{ background: {RED_HOT}; }}
QFrame#sheetBody QPushButton:default:focus {{ border: 2px solid {CARD_INK}; padding: 6px 13px; }}
QFrame#sheetBody QPushButton#danger {{
    background: transparent; color: {RED_DEEP}; border: 1px solid {RED_DEEP};
}}
QFrame#sheetBody QPushButton#danger:hover {{ background: {RED}; color: #ffffff; }}
QFrame#sheetBody QPushButton#danger:disabled {{
    color: #8d8470; border-color: {KRAFT}; background: transparent;
}}
QFrame#sheetBody QListWidget {{
    background: {CARD_FIELD}; color: {CARD_INK}; border: 1px solid {KRAFT_DEEP};
    border-radius: 3px; padding: 0; outline: 0;
}}
QFrame#sheetBody QListWidget:focus {{ border: 2px solid {RED}; }}
QFrame#sheetBody QListWidget::item {{ padding: 0; border-bottom: 1px solid {KRAFT}; }}
QFrame#sheetBody QListWidget::item:selected {{ background: {RED}; color: #ffffff; }}
QFrame#sheetBody QListWidget::item:hover:!selected {{ background: {KRAFT}; }}

/* --- alert strips under the header --------------------------------------- */
QFrame#alertBar {{
    background: {RED_DEEP}; border: 1px solid {RED_HOT}; border-radius: 5px;
}}
QFrame#alertBar QLabel {{ color: #ffffff; font-weight: 500; background: transparent; }}
QFrame#alertBar QPushButton {{
    background: {CARD}; color: {CARD_INK}; border: 1px solid {CARD}; font-weight: 600;
}}
QFrame#alertBar QPushButton:hover {{ background: {KRAFT}; }}
QFrame#alertBar QPushButton:focus {{ border: 2px solid {GRAPHITE}; padding: 6px 13px; }}
QFrame#warnBar {{
    background: {KRAFT}; border: 1px solid {KRAFT_DEEP}; border-radius: 5px;
}}
QFrame#warnBar QLabel {{ color: {CARD_INK}; font-weight: 500; background: transparent; }}
QFrame#warnBar QPushButton {{
    background: {GRAPHITE}; color: {INK}; border: 1px solid {GRAPHITE}; font-weight: 600;
}}
QFrame#warnBar QPushButton:hover {{ background: #33373c; }}
QFrame#warnBar QPushButton:focus {{ border: 2px solid {RED}; padding: 6px 13px; }}

/* --- logs and connection results ------------------------------------------ */
QPlainTextEdit#logView {{
    font-family: "Cascadia Mono", "Consolas", "Courier New", monospace; font-size: 12px;
}}
QFrame#sheetBody QPlainTextEdit#logView {{
    font-family: "Cascadia Mono", "Consolas", "Courier New", monospace; font-size: 12px;
    padding: 8px 10px;
}}
QFrame#sheetBody QListWidget#sources::item {{ padding: 11px 14px; font-weight: 500; }}
QFrame#sheetBody QListWidget#sources {{ font-size: 14px; }}
QLabel#connResult {{ color: {CARD_INK}; font-weight: 500; }}
QLabel#connResult[state="error"] {{ color: {RED_DEEP}; font-weight: 600; }}
QFrame#sheetBody QLabel#connResult {{ color: {CARD_INK}; }}
QFrame#sheetBody QLabel#connResult[state="error"] {{ color: {RED_DEEP}; }}

/* --- meeting prompt: a small console card -------------------------------- */
QFrame#promptCard {{
    background: {GRAPHITE}; border: 1px solid {RULE}; border-radius: 8px;
}}
QLabel#promptTitle {{ font-size: 16px; font-weight: 600; color: {INK}; }}
QFrame#promptCard QLabel {{ background: transparent; }}
QFrame#promptCard QPushButton#record {{
    background: {RED}; color: #ffffff; border: 1px solid {RED_HOT}; border-radius: 4px;
    font-size: 14px; font-weight: 600; padding: 8px 18px; min-height: 0;
}}
QFrame#promptCard QPushButton#record:hover {{ background: {RED_HOT}; }}
QFrame#promptCard QPushButton#record:pressed {{ background: {RED_DEEP}; }}
QFrame#promptCard QPushButton#record:focus {{ border: 2px solid {CARD}; padding: 7px 17px; background: {RED}; }}
"""


def make_sheet(dialog, title: str, subtitle: str = ""):
    """Give a dialog the sheet look and return the layout for its card-stock body.

    A graphite header strip carries the title in condensed caps (with a grease
    pencil rule under it); everything the dialog holds sits on card stock.
    """
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout

    dialog.setObjectName("sheet")
    install_dark_titlebar(dialog)
    outer = QVBoxLayout(dialog)
    outer.setContentsMargins(0, 0, 0, 0)
    outer.setSpacing(0)

    head = QFrame()
    head.setObjectName("sheetHead")
    head_layout = QHBoxLayout(head)
    head_layout.setContentsMargins(24, 14, 24, 12)
    label = QLabel(title.upper())
    label.setObjectName("sheetTitle")
    font = label.font()
    font.setLetterSpacing(QFont.AbsoluteSpacing, 1.2)
    label.setFont(font)
    head_layout.addWidget(label)
    if subtitle:
        sub = QLabel(subtitle)
        sub.setObjectName("sheetSub")
        head_layout.addWidget(sub, 1)
    else:
        head_layout.addStretch(1)
    outer.addWidget(head)

    body = QFrame()
    body.setObjectName("sheetBody")
    outer.addWidget(body, 1)
    body_layout = QVBoxLayout(body)
    body_layout.setContentsMargins(24, 20, 24, 20)
    body_layout.setSpacing(14)
    return body_layout


# -- dark native title bar (Windows only; silently a no-op elsewhere) -------------

def apply_dark_titlebar(widget) -> bool:
    """Ask DWM for a dark title bar on this top-level window."""
    import sys

    if sys.platform != "win32":
        return False
    try:
        import ctypes

        hwnd = int(widget.winId())
        value = ctypes.c_int(1)
        dwm = ctypes.windll.dwmapi
        # 20 is DWMWA_USE_IMMERSIVE_DARK_MODE; 19 is the pre-20H1 spelling.
        for attribute in (20, 19):
            if dwm.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                return True
    except Exception:  # noqa: BLE001 - cosmetic only
        pass
    return False


def install_dark_titlebar(widget) -> None:
    """Apply the dark title bar whenever ``widget`` is shown (its handle exists then)."""
    import sys

    if sys.platform != "win32":
        return
    try:
        from PySide6.QtCore import QEvent, QObject

        class Filter(QObject):
            def eventFilter(self, obj, event):  # noqa: N802 - Qt naming
                if event.type() == QEvent.Show:
                    apply_dark_titlebar(obj)
                return False

        filt = Filter(widget)
        widget.installEventFilter(filt)
        widget._dark_titlebar_filter = filt
    except Exception:  # noqa: BLE001
        pass
