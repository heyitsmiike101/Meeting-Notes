from pathlib import Path
import struct


ROOT = Path(__file__).resolve().parents[1]


def test_windows_icon_is_a_multi_size_ico():
    icon = ROOT / "assets" / "meeting-notes.ico"
    data = icon.read_bytes()
    reserved, kind, count = struct.unpack_from("<HHH", data)
    assert (reserved, kind) == (0, 1)
    assert count >= 4
    entries = list(struct.iter_unpack("<BBBBHHII", data[6:6 + 16 * count]))
    sizes = [entry[0] or 256 for entry in entries]
    assert {16, 32, 48, 256}.issubset(sizes)
    for width, height, colors, reserved, planes, bit_count, image_size, offset in entries:
        assert (height or 256) == (width or 256)
        assert (colors, reserved, planes, bit_count) == (0, 0, 1, 32)
        assert image_size > 0
        assert offset + image_size <= len(data)


def test_windows_builds_embed_the_release_icon():
    for workflow in (ROOT / ".github" / "workflows" / "ci.yml", ROOT / ".github" / "workflows" / "release.yml"):
        assert "--windows-icon-from-ico=assets/meeting-notes.ico" in workflow.read_text(encoding="utf-8")


def test_client_fonts_ship_with_the_package_and_the_nuitka_builds():
    fonts = ROOT / "meeting_notes" / "client" / "ui" / "fonts"
    for name in (
        "Inter-Regular.ttf",
        "Inter-Medium.ttf",
        "Inter-SemiBold.ttf",
        "Inter-Bold.ttf",
        "LICENSE.txt",
    ):
        assert (fonts / name).is_file(), name
    assert not list(fonts.glob("Barlow*")), "the old Barlow files must be gone"
    assert "client/ui/fonts/*" in (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for workflow in (ROOT / ".github" / "workflows" / "ci.yml", ROOT / ".github" / "workflows" / "release.yml"):
        assert "--include-package-data=meeting_notes" in workflow.read_text(encoding="utf-8")


def test_client_registers_the_bundled_fonts():
    import os

    import pytest

    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from meeting_notes.client.ui.theme import load_fonts

    app = QApplication.instance() or QApplication([])
    assert app is not None
    families = load_fonts()
    assert "Inter" in families
    from PySide6.QtGui import QFontDatabase

    styles = set(QFontDatabase.styles("Inter"))
    assert {"Regular", "Medium", "SemiBold", "Bold"} <= styles
