"""One stylesheet for the whole app, built from a token table per theme.

The client is a calm, familiar Windows productivity app: neutral surfaces, one
restrained blue accent (primary action, focus, selection, links), subtle 1px
borders and Inter. It follows the same direction contract as the server web UI
(``.impeccable/surfaces/meeting-notes-server-web-py.md``).

Theme is a user setting -- System, Light or Dark (``appearance`` in the client
config). ``TOKENS`` below is the single source of truth for both themes: the
stylesheet, the application palette, the custom-painted meters, the icons and the
history list all read colours from it, so nothing else hard-codes a hex value.
Red exists only as the semantic recording / destructive / error state.

The stylesheet lives on the QApplication rather than on the main window because
a stylesheet set on a single widget does not reach dialogs: they are top-level
windows of their own.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from string import Template
from typing import Callable, Dict, Optional

APPEARANCES = ("system", "light", "dark")
DEFAULT_APPEARANCE = "system"

# -- tokens --------------------------------------------------------------------
LIGHT: Dict[str, str] = {
    "bg": "#ffffff",              # page
    "panel": "#f8f8f9",           # cards and meter lanes
    "panel_hover": "#f0f0f2",     # hover on quiet controls and rows
    "raised": "#ffffff",          # menus, popovers, the meeting prompt
    "border": "#e4e4e7",          # 1px hairlines
    "border_strong": "#d4d4d8",   # inputs and secondary buttons
    "text": "#18181b",
    "text2": "#52525b",
    "muted": "#6b6b76",           # secondary text (>= 4.5:1 on bg and panel)
    "disabled": "#a1a1aa",
    "accent": "#3b5bdb",          # the one accent: fills
    "accent_hover": "#3350c4",
    "accent_press": "#2c45ab",
    "accent_text": "#3b5bdb",     # accent as text or an icon (>= 4.5:1 on bg)
    "accent_soft": "#edf0fc",     # selected rows
    "on_accent": "#ffffff",
    "danger": "#d92d20",          # recording / destructive fills
    "danger_hover": "#b42318",
    "danger_press": "#912018",
    "danger_text": "#b42318",
    "danger_soft": "#fef3f2",
    "danger_border": "#fda29b",
    "warn_soft": "#fffaeb",
    "warn_border": "#fec84b",
    "warn_text": "#93370d",
    "warn_icon": "#b54708",
    "info_soft": "#eff3fe",
    "info_border": "#c3cffa",
    "info_text": "#2b3f9e",
    "ok_text": "#067647",
    "ok_soft": "#ecfdf3",
    "ok_border": "#a6e4c0",
    "icon": "#52525b",
    "icon_disabled": "#a1a1aa",
    "meter_you": "#3b5bdb",
    "meter_them": "#8a8a96",
    "shadow": "#40000000",
    "toast_bg": "#171a1f",        # the toast inverts the theme
    "toast_text": "#f4f5f7",
}

DARK: Dict[str, str] = {
    "bg": "#111113",
    "panel": "#18181b",
    "panel_hover": "#222226",
    "raised": "#1f1f23",
    "border": "#2a2a30",
    "border_strong": "#3b3b43",
    "text": "#ececef",
    "text2": "#b4b4bd",
    "muted": "#8e8e99",
    "disabled": "#5b5b64",
    "accent": "#3f5ce0",
    "accent_hover": "#4a68ea",
    "accent_press": "#3550cc",
    "accent_text": "#7d97ff",
    "accent_soft": "#1b2140",
    "on_accent": "#ffffff",
    "danger": "#d92d20",
    "danger_hover": "#c0261a",
    "danger_press": "#a01f15",
    "danger_text": "#f87171",
    "danger_soft": "#2a1516",
    "danger_border": "#5c2528",
    "warn_soft": "#2a2010",
    "warn_border": "#5a4318",
    "warn_text": "#f5c26b",
    "warn_icon": "#f5b544",
    "info_soft": "#171d38",
    "info_border": "#2c3868",
    "info_text": "#a9bbff",
    "ok_text": "#4ade80",
    "ok_soft": "#10231a",
    "ok_border": "#1f5a3a",
    "icon": "#b4b4bd",
    "icon_disabled": "#5b5b64",
    "meter_you": "#6b88fb",
    "meter_them": "#7b7b87",
    "shadow": "#99000000",
    "toast_bg": "#e8e9ec",
    "toast_text": "#171a1f",
}

TOKENS: Dict[str, Dict[str, str]] = {"light": LIGHT, "dark": DARK}

FONT_FILES = (
    "Inter-Regular.ttf",
    "Inter-Medium.ttf",
    "Inter-SemiBold.ttf",
    "Inter-Bold.ttf",
)
FONT_FAMILY = "Inter"
SANS = '"Inter", "Segoe UI Variable Text", "Segoe UI", "SF Pro Text", ".AppleSystemUIFont", "Helvetica Neue", sans-serif'
MONO = '"Cascadia Mono", "Consolas", "SF Mono", "Menlo", "Courier New", monospace'

_families: set = set()


def load_fonts() -> list:
    """Register the bundled Inter files with Qt; never raises.

    Needs a QGuiApplication to exist. If a file is missing (a packaging slip) or
    Qt refuses it, the stylesheet's font stack falls back to Segoe UI silently.
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
    _families.update(families)
    return sorted(families)


def ui_font(pixel_size: int = 13, weight=None, tabular: bool = False):
    """A QFont in the UI face for custom-painted widgets (falls back to Segoe UI)."""
    from PySide6.QtGui import QFont, QFontDatabase

    families = set(QFontDatabase.families())
    for name in (FONT_FAMILY, "Segoe UI Variable Text", "Segoe UI", "SF Pro Text", "Helvetica Neue"):
        if name in families:
            font = QFont(name)
            break
    else:
        font = QFont(FONT_FAMILY)
    font.setPixelSize(pixel_size)
    font.setWeight(weight if weight is not None else QFont.Normal)
    if tabular:
        enable_tabular(font)
    return font


def enable_tabular(font) -> bool:
    """Turn on Inter's ``tnum`` feature (Qt 6.7+); a no-op where unsupported."""
    try:
        from PySide6.QtGui import QFont

        font.setFeature(QFont.Tag("tnum"), 1)
        return True
    except Exception:  # noqa: BLE001 - digits merely stay proportional
        return False


# -- which theme is active -------------------------------------------------------

def normalize_appearance(value) -> str:
    """Return a valid appearance setting, defaulting to ``system``."""
    text = str(value or "").strip().lower()
    return text if text in APPEARANCES else DEFAULT_APPEARANCE


def _registry_scheme() -> Optional[str]:
    """Windows app mode from the registry (the fallback when Qt cannot say)."""
    if sys.platform != "win32":
        return None
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        )
        try:
            value, _kind = winreg.QueryValueEx(key, "AppsUseLightTheme")
        finally:
            winreg.CloseKey(key)
        return "light" if int(value) else "dark"
    except Exception:  # noqa: BLE001
        return None


def detect_system_scheme(hints=None) -> str:
    """``"light"`` or ``"dark"`` per the OS app mode.

    Qt 6.5+ reports it through ``QStyleHints.colorScheme()``; when that is
    Unknown (or unavailable) the Windows registry value is used. ``hints`` lets
    tests pass a fake style-hints object.
    """
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QGuiApplication

        if hints is None:
            hints = QGuiApplication.styleHints() if QGuiApplication.instance() else None
        if hints is not None:
            scheme = hints.colorScheme()
            if scheme == Qt.ColorScheme.Dark:
                return "dark"
            if scheme == Qt.ColorScheme.Light:
                return "light"
    except Exception:  # noqa: BLE001
        pass
    return _registry_scheme() or "light"


def resolve_theme(appearance, system_scheme: Optional[str] = None) -> str:
    """Map an appearance setting to a concrete ``light`` / ``dark`` theme."""
    appearance = normalize_appearance(appearance)
    if appearance in ("light", "dark"):
        return appearance
    return system_scheme if system_scheme in TOKENS else detect_system_scheme()


_state = {"appearance": DEFAULT_APPEARANCE, "theme": "light"}


def active_name() -> str:
    return _state["theme"]


def tokens(name: Optional[str] = None) -> Dict[str, str]:
    """The token table of the active theme (or of ``name``)."""
    return TOKENS[name or _state["theme"]]


def is_dark() -> bool:
    return _state["theme"] == "dark"


# -- stylesheet ------------------------------------------------------------------

def _asset_png(name: str, colour: str, draw: Callable) -> str:
    """A small image file for a stylesheet (Qt stylesheets can only use files)."""
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QColor, QImage, QPainter, QPen

        path = Path(tempfile.gettempdir()) / f"meeting-notes-ui-{name}-{colour.lstrip('#')}.png"
        if not path.exists():
            image = QImage(36, 36, QImage.Format_ARGB32)
            image.fill(Qt.transparent)
            painter = QPainter(image)
            painter.setRenderHint(QPainter.Antialiasing, True)
            pen = QPen(QColor(colour), 4.2)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            painter.setPen(pen)
            draw(painter)
            painter.end()
            image.save(str(path))
        if path.exists():
            return path.as_posix()
    except Exception:  # noqa: BLE001 - a filled box still reads as checked
        pass
    return ""


def _draw_tick(painter) -> None:
    from PySide6.QtCore import QPointF

    painter.drawPolyline([QPointF(8, 19), QPointF(15, 26), QPointF(28, 10)])


def _draw_chevron(painter) -> None:
    from PySide6.QtCore import QPointF

    painter.drawPolyline([QPointF(8, 13), QPointF(18, 23), QPointF(28, 13)])


_TEMPLATE = Template(
    """
QWidget { font-family: $sans; font-size: 13px; }
QWidget#root, QDialog { background: $bg; }
QToolTip {
    background: $raised; color: $text; border: 1px solid $border_strong;
    padding: 4px 8px; font-family: $sans; font-size: 12px;
}
QLabel { color: $text; background: transparent; }
QLabel#brand { font-size: 15px; font-weight: 600; color: $text; }
QLabel#version, QLabel#subtle { color: $muted; font-size: 12px; }
QLabel#devices { color: $muted; font-size: 12px; }
QLabel#status { color: $muted; font-size: 12px; }
QLabel#section { font-size: 12px; font-weight: 600; color: $text2; }
QLabel#heading, QLabel#historyHeading { font-size: 15px; font-weight: 600; color: $text; }
QLabel#clock { font-size: 40px; font-weight: 400; color: $muted; }
QLabel#clock[live="true"] { color: $text; }

QFrame#topbar { background: $bg; border: none; border-bottom: 1px solid $border; }
QFrame#recordCard { background: $panel; border: 1px solid $border; border-radius: 10px; }
QFrame#recordCard QLabel { background: transparent; }

/* --- buttons: secondary by default, accent for the primary action ---------- */
QPushButton {
    background: $bg; color: $text; border: 1px solid $border_strong;
    border-radius: 6px; padding: 6px 12px; font-weight: 500;
}
QPushButton:hover { background: $panel_hover; }
QPushButton:pressed { background: $border; }
QPushButton:focus { border: 2px solid $accent; padding: 5px 11px; }
QPushButton:disabled { color: $disabled; background: $panel; border-color: $border; }
QPushButton:default { background: $accent; border-color: $accent; color: $on_accent; font-weight: 600; }
QPushButton:default:hover { background: $accent_hover; border-color: $accent_hover; }
QPushButton:default:pressed { background: $accent_press; border-color: $accent_press; }
QPushButton:default:focus { border: 2px solid $accent_text; padding: 5px 11px; }
QPushButton:default:disabled { background: $panel_hover; border-color: $border; color: $disabled; }

/* Quiet ghost buttons in the header. */
QPushButton#tool, QToolButton#more {
    background: transparent; border: 1px solid transparent; color: $text2;
}
QPushButton#tool:hover, QToolButton#more:hover { background: $panel_hover; color: $text; }
QPushButton#tool:pressed, QToolButton#more:pressed { background: $border; }
QPushButton#tool:disabled { color: $disabled; background: transparent; border-color: transparent; }
QToolButton#more { border-radius: 6px; padding: 6px 8px; }
QToolButton#more:focus { border: 2px solid $accent; padding: 5px 7px; }
QToolButton#more:open { background: $panel_hover; border: 1px solid transparent; padding: 6px 8px; }
QToolButton#more::menu-indicator { image: none; width: 0; }

/* The primary action: Start recording (accent), Stop recording (destructive red). */
QPushButton#record, QPushButton#recording {
    font-size: 14px; font-weight: 600; padding: 8px 18px; border-radius: 6px;
    border: 1px solid $accent; min-height: 22px; color: $on_accent;
}
QPushButton#record { background: $accent; }
QPushButton#record:hover { background: $accent_hover; border-color: $accent_hover; }
QPushButton#record:pressed { background: $accent_press; border-color: $accent_press; }
QPushButton#record:focus { border: 2px solid $accent_text; padding: 7px 17px; }
QPushButton#record:disabled { background: $panel_hover; color: $disabled; border-color: $border; }
QPushButton#recording { background: $danger; border-color: $danger; }
QPushButton#recording:hover { background: $danger_hover; border-color: $danger_hover; }
QPushButton#recording:pressed { background: $danger_press; border-color: $danger_press; }
QPushButton#recording:focus { border: 2px solid $danger_text; padding: 7px 17px; }
QPushButton#recording:disabled { background: $panel_hover; color: $disabled; border-color: $border; }

/* Per-track mute toggles. */
QPushButton#mute_mic, QPushButton#mute_system {
    background: $bg; color: $text; border: 1px solid $border_strong;
    padding: 4px 10px; font-size: 12px; font-weight: 500;
    border-radius: 8px; /* matches the meter lane it sits beside */
}
QPushButton#mute_mic:hover, QPushButton#mute_system:hover { background: $panel_hover; }
QPushButton#mute_mic:focus, QPushButton#mute_system:focus { border: 2px solid $accent; padding: 3px 9px; }
QPushButton#mute_mic:disabled, QPushButton#mute_system:disabled {
    color: $disabled; background: transparent; border: 1px solid $border;
}
QPushButton#mute_mic:checked, QPushButton#mute_system:checked {
    background: $panel_hover; color: $text; border-color: $border_strong; font-weight: 600;
}

QPushButton#danger, QPushButton#deleteButton { background: $bg; color: $danger_text; border: 1px solid $danger_border; }
QPushButton#danger:hover, QPushButton#deleteButton:hover { background: $danger_soft; }
QPushButton#danger:disabled, QPushButton#deleteButton:disabled { color: $disabled; border-color: $border; background: $panel; }

/* --- fields --------------------------------------------------------------- */
QLineEdit, QComboBox, QDateTimeEdit {
    background: $bg; color: $text; border: 1px solid $border_strong; border-radius: 6px;
    padding: 7px 10px; selection-background-color: $accent; selection-color: $on_accent;
    placeholder-text-color: $muted; lineedit-password-character: 42;
}
QLineEdit:hover, QComboBox:hover, QDateTimeEdit:hover { border-color: $muted; }
QLineEdit:focus, QComboBox:focus, QComboBox:on, QDateTimeEdit:focus { border: 2px solid $accent; padding: 6px 9px; }
QLineEdit:disabled { color: $disabled; background: $panel; }
QComboBox { min-width: 120px; }
QComboBox::drop-down, QDateTimeEdit::drop-down { border: none; width: 26px; }
QComboBox::down-arrow, QDateTimeEdit::down-arrow { image: url($chevron); width: 12px; height: 12px; }
QComboBox QAbstractItemView {
    background: $raised; color: $text; border: 1px solid $border_strong; border-radius: 6px;
    padding: 4px; outline: 0; selection-background-color: $panel_hover; selection-color: $text;
}
QComboBox QAbstractItemView::item { padding: 6px 10px; min-height: 20px; border-radius: 4px; }

QPlainTextEdit {
    background: $bg; color: $text; border: 1px solid $border_strong; border-radius: 8px;
    padding: 10px 12px; selection-background-color: $accent; selection-color: $on_accent;
    placeholder-text-color: $muted;
}
QPlainTextEdit:focus { border: 2px solid $accent; padding: 9px 11px; }
QPlainTextEdit#preview { border-color: $border; font-size: 14px; }
QPlainTextEdit#preview:focus { border: 2px solid $accent; padding: 9px 11px; }

QCheckBox { color: $text; background: transparent; spacing: 10px; }
QCheckBox::indicator {
    width: 16px; height: 16px; border: 1px solid $muted; border-radius: 4px; background: $bg;
}
QCheckBox::indicator:hover { border-color: $text2; }
QCheckBox::indicator:checked { background: $accent; border-color: $accent; $check_image }
QCheckBox::indicator:focus { border: 2px solid $accent; }
QCheckBox::indicator:checked:focus { border: 2px solid $accent_text; }
QCheckBox:disabled { color: $disabled; }

QRadioButton { color: $text; background: transparent; spacing: 8px; }
QRadioButton::indicator { width: 16px; height: 16px; border: 1px solid $muted; border-radius: 9px; background: $bg; }
QRadioButton::indicator:hover { border-color: $text2; }
QRadioButton::indicator:checked {
    border-color: $accent;
    background: qradialgradient(cx: 0.5, cy: 0.5, radius: 0.5, fx: 0.5, fy: 0.5,
                                stop: 0 $accent, stop: 0.42 $accent, stop: 0.5 $bg, stop: 1 $bg);
}
QRadioButton::indicator:focus { border-color: $accent; }
QRadioButton:disabled { color: $disabled; }

/* --- menus, scrollbars, splitter, lists ------------------------------------- */
QMenu {
    background: $raised; color: $text; border: 1px solid $border_strong; border-radius: 8px;
    padding: 4px;
}
QMenu::item { padding: 7px 24px 7px 10px; border-radius: 4px; }
QMenu::item:selected { background: $panel_hover; color: $text; }
QMenu::item:disabled { color: $disabled; }
QMenu::separator { height: 1px; background: $border; margin: 4px 6px; }
QMenu::icon { padding-left: 6px; }

QScrollBar:vertical { background: transparent; width: 12px; margin: 0; }
QScrollBar::handle:vertical { background: $border_strong; border-radius: 4px; min-height: 28px; margin: 2px 2px; }
QScrollBar::handle:vertical:hover { background: $muted; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QScrollBar:horizontal { background: transparent; height: 12px; margin: 0; }
QScrollBar::handle:horizontal { background: $border_strong; border-radius: 4px; min-width: 28px; margin: 2px 2px; }
QScrollBar::handle:horizontal:hover { background: $muted; }
QSplitter::handle { background: $border; width: 1px; }

QListWidget {
    background: $bg; color: $text; border: 1px solid $border; border-radius: 8px;
    padding: 4px; outline: 0;
}
QListWidget:focus { border: 1px solid $accent; }
QListWidget::item { padding: 8px 10px; border-radius: 6px; }
QListWidget::item:selected { background: $accent_soft; color: $text; }
QListWidget::item:hover:!selected { background: $panel_hover; }
QListWidget#sources::item { padding: 8px 12px; font-weight: 500; }
QListWidget#settingsNav { background: transparent; border: none; border-right: 1px solid $border; border-radius: 0; padding: 0 10px 0 0; }
QListWidget#settingsNav::item { padding: 9px 12px; font-weight: 500; }
QScrollArea#settingsScroll, QWidget#settingsPage { background: transparent; border: none; }

/* --- banners: inline alert strips under the header -------------------------- */
QFrame#alertBar { background: $danger_soft; border: 1px solid $danger_border; border-radius: 8px; }
QFrame#alertBar QLabel { color: $text; font-weight: 500; background: transparent; }
QFrame#alertBar QLabel#deleteWarning { color: $danger_text; font-weight: 600; }
QFrame#alertBar QPushButton { border-color: $danger_border; }
QFrame#warnBar { background: $warn_soft; border: 1px solid $warn_border; border-radius: 8px; }
QFrame#warnBar QLabel { color: $text; font-weight: 500; background: transparent; }
QFrame#warnBar QPushButton { border-color: $warn_border; }
QFrame#deviceAlert { background: $danger; border: 1px solid $danger_press; border-radius: 8px; }
QFrame#deviceAlert QLabel { color: $on_accent; font-weight: 600; background: transparent; }
QFrame#okBar { background: $ok_soft; border: 1px solid $ok_border; border-radius: 8px; }
QFrame#okBar QLabel { color: $text; font-weight: 500; background: transparent; }
QFrame#updateBar, QFrame#infoBar {
    background: $info_soft; border: 1px solid $info_border; border-radius: 8px;
}
QFrame#updateBar QLabel, QFrame#infoBar QLabel { color: $text; font-weight: 500; background: transparent; }
QFrame#updateBar QPushButton, QFrame#infoBar QPushButton { border-color: $info_border; }
QFrame#updateBar QPushButton#update {
    background: $accent; color: $on_accent; border-color: $accent; font-weight: 600;
}
QFrame#updateBar QPushButton#update:hover { background: $accent_hover; border-color: $accent_hover; }
QFrame#updateBar QPushButton#update:disabled { background: $panel_hover; color: $disabled; border-color: $border; }

/* --- logs and connection results -------------------------------------------- */
QPlainTextEdit#logView { font-family: $mono; font-size: 12px; padding: 8px 10px; }
QLabel#connResult { color: $text2; font-weight: 500; }
QLabel#connResult[state="ok"] { color: $ok_text; }
QLabel#connResult[state="error"] { color: $danger_text; font-weight: 600; }

/* --- re-upload dialog: one bordered row per saved recording ------------------ */
QFrame#recRow { background: $bg; border: 1px solid $border; border-radius: 8px; }
QFrame#recRow[invalid="true"] { background: $danger_soft; border: 1px solid $danger_border; }
QFrame#recRow QLabel { background: transparent; }
QCheckBox#recName, QLabel#recName { font-weight: 600; }
QLabel#recBadge {
    color: $accent_text; background: $accent_soft; border-radius: 9px;
    padding: 2px 9px; font-size: 11px; font-weight: 600;
}
QLabel#recBadge[tone="ok"] { color: $ok_text; background: $ok_soft; }
QLabel#recBadge[tone="info"] { color: $info_text; background: $info_soft; }
QLabel#recBadge[tone="warn"] { color: $warn_text; background: $warn_soft; }
QLabel#recBadge[tone="error"] { color: $danger_text; background: $danger_soft; }
QLabel#recBadge[tone="muted"] { color: $muted; background: $panel_hover; }
QScrollArea#recScroll, QWidget#recList { background: transparent; border: none; }

/* --- meeting prompt: a toast-like card --------------------------------------- */
QFrame#promptCard { background: $raised; border: 1px solid $border_strong; border-radius: 12px; }
QLabel#promptTitle { font-size: 14px; font-weight: 600; color: $text; }
QFrame#promptCard QLabel { background: transparent; }
QFrame#promptCard QPushButton#record {
    font-size: 13px; padding: 6px 16px; min-height: 0; border-radius: 6px;
}
QFrame#promptCard QPushButton#record:focus { padding: 5px 15px; }
"""
)


def build_stylesheet(name: str = "light") -> str:
    """The application stylesheet for one theme, from its token table."""
    t = dict(TOKENS[name])
    check = _asset_png("check", "#ffffff", _draw_tick)
    t["check_image"] = f"image: url({check});" if check else ""
    t["chevron"] = _asset_png("chevron", t["text2"], _draw_chevron)
    t["sans"] = SANS
    t["mono"] = MONO
    return _TEMPLATE.safe_substitute(t)


def build_palette(name: str = "light"):
    """A QPalette in the theme's colours, for widgets a stylesheet does not reach."""
    from PySide6.QtGui import QColor, QPalette

    t = TOKENS[name]
    palette = QPalette()
    roles = {
        QPalette.Window: t["bg"],
        QPalette.WindowText: t["text"],
        QPalette.Base: t["bg"],
        QPalette.AlternateBase: t["panel"],
        QPalette.Text: t["text"],
        QPalette.Button: t["panel"],
        QPalette.ButtonText: t["text"],
        QPalette.ToolTipBase: t["raised"],
        QPalette.ToolTipText: t["text"],
        QPalette.PlaceholderText: t["muted"],
        QPalette.Highlight: t["accent"],
        QPalette.HighlightedText: t["on_accent"],
        QPalette.Link: t["accent_text"],
        QPalette.BrightText: t["on_accent"],
    }
    for role, colour in roles.items():
        palette.setColor(role, QColor(colour))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        palette.setColor(QPalette.Disabled, role, QColor(t["disabled"]))
    return palette


# Default stylesheet (light), kept as a module constant for callers and tests
# that set it directly; the app itself goes through ``apply_appearance``.
APP_STYLE = build_stylesheet("light")


# -- applying a theme -----------------------------------------------------------

_manager = None


def manager():
    """The process-wide notifier; its ``changed(str)`` signal fires on a theme switch."""
    global _manager
    if _manager is None:
        from PySide6.QtCore import QObject, Signal

        class ThemeManager(QObject):
            changed = Signal(str)

        _manager = ThemeManager()
        try:
            from PySide6.QtGui import QGuiApplication

            app = QGuiApplication.instance()
            if app is not None:
                app.styleHints().colorSchemeChanged.connect(_on_system_scheme_changed)
        except Exception:  # noqa: BLE001 - live following is a nicety
            pass
    return _manager


def _on_system_scheme_changed(*_args) -> None:
    if _state["appearance"] == "system":
        apply_appearance("system")


def apply_appearance(appearance=None, app=None, system_scheme: Optional[str] = None) -> str:
    """Resolve ``appearance`` and restyle the application; returns the theme name.

    Safe to call repeatedly (a Settings save, or the OS flipping app mode while
    the setting is System). Emits ``manager().changed`` when the theme changes.
    """
    from PySide6.QtWidgets import QApplication

    if appearance is None:
        from meeting_notes import config as config_mod

        appearance = config_mod.appearance_setting()
    appearance = normalize_appearance(appearance)
    name = resolve_theme(appearance, system_scheme)
    previous = _state["theme"]
    _state["appearance"], _state["theme"] = appearance, name
    app = app or QApplication.instance()
    notifier = manager() if app is not None else None
    if app is not None:
        app.setStyleSheet(build_stylesheet(name))
        app.setPalette(build_palette(name))
        refresh_titlebars()
    if notifier is not None and name != previous:
        notifier.changed.emit(name)
    return name


# -- dialogs -----------------------------------------------------------------------

def make_sheet(dialog, title: str = "", subtitle: str = ""):
    """Give a dialog the standard neutral look and return its content layout.

    No custom title strip: the window title (set by the caller) is the title,
    and content sits directly on the dialog surface.
    """
    from PySide6.QtWidgets import QVBoxLayout

    dialog.setObjectName("sheet")
    install_titlebar(dialog)
    outer = QVBoxLayout(dialog)
    outer.setContentsMargins(24, 20, 24, 20)
    outer.setSpacing(14)
    return outer


# -- native title bar (Windows only; silently a no-op elsewhere) ---------------------

def apply_titlebar(widget, dark: Optional[bool] = None) -> bool:
    """Ask DWM for a dark or light title bar on this top-level window."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        hwnd = int(widget.winId())
        value = ctypes.c_int(1 if (is_dark() if dark is None else dark) else 0)
        dwm = ctypes.windll.dwmapi
        # 20 is DWMWA_USE_IMMERSIVE_DARK_MODE; 19 is the pre-20H1 spelling.
        for attribute in (20, 19):
            if dwm.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                return True
    except Exception:  # noqa: BLE001 - cosmetic only
        pass
    return False


def install_titlebar(widget) -> None:
    """Match the title bar to the active theme whenever ``widget`` is shown."""
    if sys.platform != "win32":
        return
    try:
        from PySide6.QtCore import QEvent, QObject

        class Filter(QObject):
            def eventFilter(self, obj, event):  # noqa: N802 - Qt naming
                if event.type() == QEvent.Show:
                    apply_titlebar(obj)
                return False

        filt = Filter(widget)
        widget.installEventFilter(filt)
        widget._titlebar_filter = filt
    except Exception:  # noqa: BLE001
        pass


def refresh_titlebars() -> None:
    """Re-apply the active theme to every visible top-level window."""
    if sys.platform != "win32":
        return
    try:
        from PySide6.QtWidgets import QApplication

        for widget in QApplication.topLevelWidgets():
            if widget.isVisible():
                apply_titlebar(widget)
    except Exception:  # noqa: BLE001
        pass


# Historical names.
apply_dark_titlebar = apply_titlebar
install_dark_titlebar = install_titlebar
