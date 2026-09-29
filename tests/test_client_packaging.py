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
