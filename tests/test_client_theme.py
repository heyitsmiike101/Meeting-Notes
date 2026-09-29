"""The Windows client's theme: Light / Dark / System, tokens, fonts, live switching."""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6", reason="PySide6 not installed")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from meeting_notes import config as config_mod  # noqa: E402
from meeting_notes.client.ui import theme  # noqa: E402


@pytest.fixture()
def qt_app():
    app = QApplication.instance() or QApplication([])
    theme.load_fonts()
    yield app
    theme.apply_appearance("light", app)  # never leak a dark theme into other tests


class FakeHints:
    def __init__(self, scheme):
        self._scheme = scheme

    def colorScheme(self):  # noqa: N802 - Qt naming
        return self._scheme


# -- config ----------------------------------------------------------------------

def test_appearance_defaults_to_system_and_validates():
    assert config_mod.appearance_setting({}) == "system"
    assert config_mod.appearance_setting({"appearance": "dark"}) == "dark"
    assert config_mod.appearance_setting({"appearance": " Light "}) == "light"
    assert config_mod.appearance_setting({"appearance": "solarized"}) == "system"
    assert config_mod.appearance_setting({"appearance": None}) == "system"
    assert theme.normalize_appearance("nonsense") == "system"


def test_appearance_reads_the_config_file(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_NOTES_CONFIG", str(tmp_path / "config.json"))
    assert config_mod.appearance_setting() == "system"
    config_mod.save_config({"appearance": "dark"})
    assert config_mod.appearance_setting() == "dark"


# -- system mode -----------------------------------------------------------------

def test_system_scheme_follows_the_qt_color_scheme(qt_app):
    assert theme.detect_system_scheme(FakeHints(Qt.ColorScheme.Dark)) == "dark"
    assert theme.detect_system_scheme(FakeHints(Qt.ColorScheme.Light)) == "light"


def test_system_scheme_falls_back_to_the_registry_when_qt_is_unknown(qt_app, monkeypatch):
    monkeypatch.setattr(theme, "_registry_scheme", lambda: "dark")
    assert theme.detect_system_scheme(FakeHints(Qt.ColorScheme.Unknown)) == "dark"
    monkeypatch.setattr(theme, "_registry_scheme", lambda: None)
    assert theme.detect_system_scheme(FakeHints(Qt.ColorScheme.Unknown)) == "light"


def test_resolve_theme_explicit_settings_ignore_the_system():
    assert theme.resolve_theme("light", "dark") == "light"
    assert theme.resolve_theme("dark", "light") == "dark"
    assert theme.resolve_theme("system", "dark") == "dark"
    assert theme.resolve_theme("system", "light") == "light"


def test_system_mode_follows_the_os_and_reacts_to_changes(qt_app, monkeypatch):
    system = {"scheme": "dark"}
    monkeypatch.setattr(theme, "detect_system_scheme", lambda hints=None: system["scheme"])
    seen = []
    theme.manager().changed.connect(seen.append)
    try:
        theme.apply_appearance("light", qt_app)
        assert theme.apply_appearance("system", qt_app) == "dark"
        system["scheme"] = "light"
        theme._on_system_scheme_changed(Qt.ColorScheme.Light)  # what colorSchemeChanged triggers
        assert theme.active_name() == "light"
        assert seen[-1] == "light"
        # An explicit choice is not moved by the OS.
        theme.apply_appearance("dark", qt_app)
        system["scheme"] = "light"
        theme._on_system_scheme_changed(Qt.ColorScheme.Light)
        assert theme.active_name() == "dark"
    finally:
        theme.manager().changed.disconnect(seen.append)


# -- stylesheet and tokens -----------------------------------------------------------

def test_stylesheet_is_built_for_both_themes_from_the_token_table():
    light, dark = theme.build_stylesheet("light"), theme.build_stylesheet("dark")
    assert light != dark
    for name, sheet in (("light", light), ("dark", dark)):
        tokens = theme.TOKENS[name]
        assert "$" not in sheet, "an unsubstituted token"
        assert tokens["accent"] in sheet and tokens["bg"] in sheet and tokens["text"] in sheet
        assert "Inter" in sheet
    assert theme.TOKENS["light"]["panel"] not in dark
    assert theme.APP_STYLE == light  # the module default is the light theme


def test_the_old_studio_look_is_gone_from_the_stylesheet():
    for sheet in (theme.build_stylesheet("light"), theme.build_stylesheet("dark")):
        for word in ("Barlow", "CONDENSED", "sheetHead", "KRAFT", "transport", "legend"):
            assert word not in sheet


def test_both_themes_define_the_same_tokens():
    assert set(theme.LIGHT) == set(theme.DARK)


def _luminance(hex_colour: str) -> float:
    h = hex_colour.lstrip("#")
    channels = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    channels = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def _contrast(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


@pytest.mark.parametrize("name", ["light", "dark"])
def test_text_colours_meet_contrast_on_their_surfaces(name):
    t = theme.TOKENS[name]
    pairs = [
        ("text", "bg"), ("text", "panel"), ("text2", "bg"), ("muted", "bg"), ("muted", "panel"),
        ("accent_text", "bg"), ("accent_text", "panel"), ("on_accent", "accent"),
        ("on_accent", "accent_hover"), ("on_accent", "accent_press"), ("on_accent", "danger"),
        ("on_accent", "danger_hover"), ("danger_text", "danger_soft"), ("text", "danger_soft"),
        ("text", "warn_soft"), ("text", "info_soft"), ("ok_text", "bg"), ("text", "accent_soft"),
    ]
    for fg, bg in pairs:
        assert _contrast(t[fg], t[bg]) >= 4.5, (name, fg, bg, _contrast(t[fg], t[bg]))


# -- applying ---------------------------------------------------------------------------

def test_apply_appearance_restyles_the_app_and_notifies(qt_app):
    seen = []
    theme.manager().changed.connect(seen.append)
    try:
        theme.apply_appearance("light", qt_app)
        assert qt_app.styleSheet() == theme.build_stylesheet("light")
        seen.clear()
        assert theme.apply_appearance("dark", qt_app) == "dark"
        assert qt_app.styleSheet() == theme.build_stylesheet("dark")
        assert theme.is_dark() and seen == ["dark"]
        assert qt_app.palette().window().color().name() == theme.TOKENS["dark"]["bg"]
        theme.apply_appearance("dark", qt_app)
        assert seen == ["dark"]  # no change, no signal
    finally:
        theme.manager().changed.disconnect(seen.append)


def test_icons_follow_the_active_theme(qt_app):
    from meeting_notes.client.ui.icons import make_icon

    def colours():
        image = make_icon("history", size=24).pixmap(24, 24).toImage()
        return {
            image.pixelColor(x, y).name()
            for x in range(24)
            for y in range(24)
            if image.pixelColor(x, y).alpha() > 250
        }

    theme.apply_appearance("light", qt_app)
    assert theme.TOKENS["light"]["icon"] in colours()
    theme.apply_appearance("dark", qt_app)
    assert theme.TOKENS["dark"]["icon"] in colours()


def test_fonts_register_and_fall_back_silently(qt_app, monkeypatch):
    from PySide6.QtGui import QFontDatabase

    assert "Inter" in theme.load_fonts()
    assert {"Regular", "Medium", "SemiBold", "Bold"} <= set(QFontDatabase.styles("Inter"))
    monkeypatch.setattr(theme, "FONT_FILES", ("does-not-exist.ttf",))
    assert theme.load_fonts() == []  # a missing file never raises
    font = theme.ui_font(13, tabular=True)
    assert font.pixelSize() == 13


def test_settings_dialog_saves_and_applies_the_appearance_live(qt_app, tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_NOTES_CONFIG", str(tmp_path / "config.json"))
    config_mod.save_config({"save_dir": str(tmp_path / "rec")})
    from meeting_notes.client.ui.settings_dialog import SettingsDialog

    dialog = SettingsDialog()
    assert dialog.appearance_combo.currentData() == "system"
    assert [dialog.appearance_combo.itemText(i) for i in range(3)] == ["System", "Light", "Dark"]
    dialog.appearance_combo.setCurrentIndex(dialog.appearance_combo.findData("dark"))
    dialog.accept()
    assert config_mod.load_config()["appearance"] == "dark"
    assert theme.active_name() == "dark"
    assert qt_app.styleSheet() == theme.build_stylesheet("dark")

    again = SettingsDialog()
    assert again.appearance_combo.currentData() == "dark"
    again.appearance_combo.setCurrentIndex(again.appearance_combo.findData("light"))
    again.accept()
    assert theme.active_name() == "light"


def test_main_window_and_prompt_render_in_both_themes(qt_app, tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_NOTES_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setenv("MEETING_NOTES_NO_DETECT", "1")
    from meeting_notes.audio import devices as devices_mod

    def deny(kind, requested=None, samplerate=None):
        raise devices_mod.DeviceNotFound("none")

    monkeypatch.setattr(devices_mod, "resolve_source", deny)
    from meeting_notes.client.ui.main_window import MainWindow
    from meeting_notes.client.ui.meeting_prompt import MeetingPrompt

    window = MainWindow()
    window._timer.stop()
    try:
        for name in ("dark", "light"):
            theme.apply_appearance(name, qt_app)
            window.resize(900, 640)
            image = window.grab().toImage()
            assert image.pixelColor(5, 300).name() == theme.TOKENS[name]["bg"]
            prompt = MeetingPrompt("Zoom", "Standup")
            assert prompt.grab().width() > 0
            prompt.close_silently()
    finally:
        window.controller.stop_uploader()
        window.close()
