"""The macOS manifest announces the version of the app actually in the published zip."""

from __future__ import annotations

import plistlib
import zipfile

import pytest
from fastapi.testclient import TestClient

from meeting_notes import __version__
from meeting_notes.server import client_packages
from meeting_notes.server.app import create_app


def make_mac_zip(path, version="0.7.14", junk=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Meeting Notes.app/Contents/Info.plist",
                    plistlib.dumps({"CFBundleShortVersionString": version, "CFBundleName": "Meeting Notes"}))
        if junk:
            zf.writestr("Meeting Notes.app/Contents/._Info.plist", b"\x00\x05\x16\x07 resource fork")
            zf.writestr("Meeting Notes.app/Contents/Frameworks/X.framework/Resources/Info.plist",
                        plistlib.dumps({"CFBundleShortVersionString": "9.9.9"}))


def test_reads_the_app_version_from_the_zip(tmp_path):
    z = tmp_path / "client" / "MeetingNotes-macOS.zip"
    make_mac_zip(z, "0.7.14")
    assert client_packages.mac_package_version(z) == "0.7.14"
    assert client_packages.published_version("macos", tmp_path) == "0.7.14"
    assert client_packages.published_version("windows", tmp_path) == __version__
    make_mac_zip(z, "0.7.15", junk=False)  # replaced: the cache follows the file
    assert client_packages.mac_package_version(z) == "0.7.15"


def test_falls_back_to_the_server_version(tmp_path):
    assert client_packages.published_version("macos", tmp_path) == __version__  # no package
    bad = tmp_path / "client" / "MeetingNotes-macOS.zip"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"not a zip")
    assert client_packages.published_version("macos", tmp_path) == __version__


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_NOTES_TOKEN", "pkg-secret")
    app = create_app(data_root=str(tmp_path / "data"), enable_mcp=False)
    (tmp_path / "data" / "client").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "client" / "MeetingNotes-Windows.zip").write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    with TestClient(app) as c:
        yield c, tmp_path / "data" / "client"


def test_mac_manifest_reports_the_packaged_version_windows_the_server_version(client):
    c, folder = client
    make_mac_zip(folder / "MeetingNotes-macOS.zip", "0.7.14")
    assert c.get("/install/client-manifest-macos.json").json()["version"] == "0.7.14"
    assert c.get("/install/client-manifest.json").json()["version"] == __version__
    make_mac_zip(folder / "MeetingNotes-macOS.zip", __version__)
    assert c.get("/install/client-manifest-macos.json").json()["version"] == __version__
