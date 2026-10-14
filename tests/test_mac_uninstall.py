"""macOS uninstall path: the /install/uninstall-mac.sh script, its route and the Install page."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from meeting_notes.server import mac_installer, web
from meeting_notes.server.app import create_app

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated:DeprecationWarning"
)

BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="needs bash")
BUNDLE_ID = mac_installer.BUNDLE_ID


def _app(tmp_path):
    return create_app(transcriber_factory=None, data_root=str(tmp_path / "data"))


# -- the script text ----------------------------------------------------------------------------------


def test_uninstaller_text_is_safe_and_complete():
    script = web.render_mac_uninstaller()
    assert script.startswith("#!/bin/bash\n")
    assert "\r" not in script
    assert "set -euo pipefail" in script
    assert BUNDLE_ID in script and "Meeting Notes.app" in script
    assert "--keep-settings" in script
    assert """osascript -e 'quit app "Meeting Notes"'""" in script
    assert 'pkill -f "$app/Contents/MacOS/"' in script
    assert "tccutil reset Microphone" in script and "tccutil reset ScreenCapture" in script
    assert "Meeting Notes was uninstalled for this Mac user." in script
    assert script.rstrip().endswith('main "$@"')
    # no token, no server address, no sudo
    assert "Bearer" not in script and "http://" not in script and "token=" not in script.lower()
    assert "sudo " not in script.replace("not with sudo", "")
    # the only recursive delete is the guarded helper, never on HOME, a recordings variable or a glob
    rm_lines = [line.strip() for line in script.splitlines() if "rm -r" in line and not line.strip().startswith("#")]
    assert rm_lines == ['if rm -rf -- "$1" 2>/dev/null; then'], rm_lines
    assert not re.search(r"rm\s+-\S*\s+(--\s+)?[\"']?\$\{?HOME", script)
    assert 'remove_path "$HOME"' not in script and "remove_path \"$rec\"" not in script
    assert "remove_path \"$default_rec\"" not in script and "remove_path \"$dir\"" not in script
    # only these are ever removed
    assert re.findall(r"remove_path \"[^\"]*\"", script) == [
        'remove_path "$app"',
        'remove_path "$leftover"',
        'remove_path "$CONFIG_DIR"',
    ]
    assert "guard_recordings" in script and "Nothing was changed" in script


@needs_bash
def test_uninstaller_is_valid_bash(tmp_path):
    path = tmp_path / "uninstall-mac.sh"
    path.write_text(web.render_mac_uninstaller(), encoding="utf-8", newline="\n")
    result = subprocess.run([BASH, "-n", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


# -- routes + page ------------------------------------------------------------------------------------


def test_uninstaller_route_is_public_and_holds_no_token(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_NOTES_TOKEN", "s3cret-token-value")
    client = TestClient(_app(tmp_path))
    resp = client.get("/install/uninstall-mac.sh", follow_redirects=False)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/x-shellscript")
    assert "s3cret-token-value" not in resp.text
    assert resp.text == web.render_mac_uninstaller()


def _install_page(tmp_path, monkeypatch, with_mac_zip: bool) -> str:
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = _app(tmp_path)
    if with_mac_zip:
        package = Path(app.state.store.root) / "client" / "MeetingNotes-macOS.zip"
        package.parent.mkdir(parents=True)
        package.write_bytes(b"zip")
    resp = TestClient(app).get("/install")
    assert resp.status_code == 200
    return resp.text


def test_install_page_has_windows_and_macos_sections_and_mac_download(tmp_path, monkeypatch):
    page = _install_page(tmp_path, monkeypatch, with_mac_zip=True)
    assert '<section id="windows">' in page and "<h2>Windows</h2>" in page
    assert '<section id="macos">' in page and "<h2>macOS</h2>" in page
    assert 'href="#windows"' in page and 'href="#macos"' in page  # jump links
    assert page.index('id="windows"') < page.index('id="macos"')
    # the Windows content sits under the Windows heading, the Mac content under macOS
    windows_part, mac_part = page.split('<section id="macos">')
    assert "client-agent.ps1" in windows_part and "uninstall-client.ps1" in windows_part
    assert "mac.sh" not in windows_part
    assert "client-agent.ps1" not in mac_part
    for sub in ("Install", "Download only", "Update", "Uninstall", "Troubleshooting"):
        assert f">{sub}</h3>" in windows_part, sub
        assert f">{sub}</h3>" in mac_part, sub
    # Mac direct download, enabled
    assert 'href="/install/MeetingNotes-macOS.zip" download>Download the Mac app (.zip)</a>' in mac_part
    assert "right-click" in mac_part and "com.apple.quarantine" in mac_part
    # Mac uninstall
    assert "curl -fsSL http://testserver/install/uninstall-mac.sh | bash" in mac_part
    assert 'href="/install/uninstall-mac.sh"' in mac_part
    assert "--keep-settings" in mac_part
    # the install one-liner is unchanged
    assert "curl -fsSL http://testserver/install/mac.sh | bash" in mac_part


def test_install_page_disables_the_mac_download_when_no_package(tmp_path, monkeypatch):
    page = _install_page(tmp_path, monkeypatch, with_mac_zip=False)
    assert 'href="/install/MeetingNotes-macOS.zip"' not in page
    assert '<button type="button" class="btn primary" disabled>Download the Mac app (.zip)</button>' in page
    assert 'id="mac-package-missing"' in page
    assert 'id="macos"' in page  # the client still links to /install#macos


# -- running it against a fake HOME -------------------------------------------------------------------


def _posix(path: Path) -> str:
    """The path as Git Bash on Windows spells it (/c/...), unchanged elsewhere."""
    text = path.as_posix()
    if re.match(r"^[A-Za-z]:/", text):
        text = "/" + text[0].lower() + text[2:]
    return text


def _plist(bundle_id: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0">\n<dict>\n'
        f"\t<key>CFBundleIdentifier</key>\n\t<string>{bundle_id}</string>\n</dict>\n</plist>\n"
    )


class _Sandbox:
    def __init__(self, tmp_path: Path):
        self.root = tmp_path
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.shims = tmp_path / "shims"
        self.shims.mkdir()
        self.calls = tmp_path / "calls.log"
        self.calls.write_text("", encoding="utf-8")
        calls = _posix(self.calls)
        self._shim("uname", 'echo Darwin')
        self._shim("id", 'echo 501')
        self._shim("pgrep", "exit 1")
        self._shim("sleep", "exit 0")
        for tool in ("osascript", "pkill", "tccutil", "xattr"):
            self._shim(tool, f'echo "{tool} $*" >> "{calls}"')

    def _shim(self, name: str, body: str) -> None:
        path = self.shims / name
        path.write_text(f"#!/bin/bash\n{body}\n", encoding="utf-8", newline="\n")
        path.chmod(0o755)

    @property
    def app(self) -> Path:
        return self.home / "Applications" / "Meeting Notes.app"

    @property
    def config_dir(self) -> Path:
        return self.home / ".meeting-notes"

    def make_app(self, bundle_id: str = BUNDLE_ID) -> Path:
        (self.app / "Contents" / "MacOS").mkdir(parents=True)
        (self.app / "Contents" / "Info.plist").write_text(_plist(bundle_id), encoding="utf-8")
        (self.app / "Contents" / "MacOS" / "MeetingNotes").write_text("x", encoding="utf-8")
        return self.app

    def make_config(self, save_dir: Path | None) -> None:
        (self.config_dir / "logs").mkdir(parents=True)
        (self.config_dir / "logs" / "client.log").write_text("log", encoding="utf-8")
        text = '{"server": {"url": "http://meeting.lan", "token": "t"}'
        if save_dir is not None:
            text += ', "save_dir": "%s"' % _posix(save_dir)
        (self.config_dir / "config.json").write_text(text + "}\n", encoding="utf-8")

    def make_recordings(self, folder: Path) -> Path:
        (folder / "2026-01-01_standup").mkdir(parents=True)
        wav = folder / "2026-01-01_standup" / "mic.wav"
        wav.write_bytes(b"RIFF")
        return wav

    def run(self, *args: str) -> subprocess.CompletedProcess:
        script = self.root / "uninstall-mac.sh"
        script.write_text(web.render_mac_uninstaller(), encoding="utf-8", newline="\n")
        import os

        env = dict(os.environ)
        env["HOME"] = _posix(self.home)
        env["PATH"] = str(self.shims) + os.pathsep + env["PATH"]
        return subprocess.run([BASH, _posix(script), *args], capture_output=True, text=True, env=env, timeout=60)


@pytest.fixture
def box(tmp_path):
    return _Sandbox(tmp_path)


@needs_bash
def test_run_removes_app_and_settings_and_keeps_recordings(box):
    box.make_app()
    rec = box.home / "Meeting Notes"
    wav = box.make_recordings(rec)
    box.make_config(None)

    result = box.run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert not box.app.exists()
    assert not box.config_dir.exists()
    assert wav.is_file()
    assert box.home.is_dir()
    assert "Your recordings were not touched" in result.stdout
    assert "Meeting Notes was uninstalled for this Mac user." in result.stdout
    calls = box.calls.read_text(encoding="utf-8")
    assert f"tccutil reset Microphone {BUNDLE_ID}" in calls
    assert f"tccutil reset ScreenCapture {BUNDLE_ID}" in calls

    # safe to run twice
    again = box.run()
    assert again.returncode == 0, again.stdout + again.stderr
    assert "Meeting Notes was uninstalled for this Mac user." in again.stdout
    assert wav.is_file()


@needs_bash
def test_run_keeps_a_custom_save_folder_and_honours_keep_settings(box):
    box.make_app()
    rec = box.root / "elsewhere" / "My Recordings"
    wav = box.make_recordings(rec)
    box.make_config(rec)

    result = box.run("--keep-settings")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not box.app.exists()
    assert (box.config_dir / "config.json").is_file()
    assert (box.config_dir / "logs" / "client.log").is_file()
    assert wav.is_file()
    assert "Kept" in result.stdout and "--keep-settings" in result.stdout
    assert str(rec.name) in result.stdout


@needs_bash
def test_run_quits_a_running_app_then_kills_it_if_it_stays(box):
    box.make_app()
    box._shim("pgrep", "echo 123")
    result = box.run()
    assert result.returncode == 0, result.stdout + result.stderr
    calls = box.calls.read_text(encoding="utf-8")
    assert 'osascript -e quit app "Meeting Notes"' in calls
    assert "pkill -f" in calls and "Meeting Notes.app/Contents/MacOS/" in calls
    assert not box.app.exists()


@needs_bash
def test_run_refuses_when_recordings_are_inside_the_settings_folder(box):
    box.make_app()
    rec = box.config_dir / "recordings"
    wav = box.make_recordings(rec)
    box.make_config(rec)

    result = box.run()
    assert result.returncode != 0
    assert "Nothing was changed" in result.stderr
    assert box.app.exists() and wav.is_file() and (box.config_dir / "config.json").is_file()

    # with --keep-settings nothing the script removes holds the recordings
    ok = box.run("--keep-settings")
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert not box.app.exists() and wav.is_file()


@needs_bash
def test_run_refuses_when_recordings_are_inside_the_app(box):
    app = box.make_app()
    rec = app / "Contents" / "rec"
    wav = box.make_recordings(rec)
    box.make_config(rec)

    result = box.run()
    assert result.returncode != 0
    assert "Nothing was changed" in result.stderr
    assert app.exists() and wav.is_file() and (box.config_dir / "config.json").is_file()


@needs_bash
def test_run_refuses_stray_wav_files_inside_the_app(box):
    app = box.make_app()
    (app / "Contents" / "oops.wav").write_bytes(b"RIFF")
    box.make_config(None)
    result = box.run()
    assert result.returncode != 0
    assert "Recordings were found inside" in result.stderr
    assert (app / "Contents" / "oops.wav").is_file()


@needs_bash
def test_run_leaves_a_different_app_with_the_same_name_alone(box):
    app = box.make_app(bundle_id="com.example.other")
    result = box.run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert app.exists()
    assert "is not Meeting Notes" in result.stderr


@needs_bash
def test_run_reads_the_bundle_id_with_plutil_when_present(box):
    app = box.make_app()
    (app / "Contents" / "Info.plist").write_text("binary-plist-placeholder", encoding="utf-8")
    box._shim("plutil", f'[ "$1" = "-extract" ] && [ "$2" = "CFBundleIdentifier" ] && echo {BUNDLE_ID} || exit 1')
    result = box.run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert not app.exists()


@needs_bash
def test_run_with_nothing_installed_is_a_clean_noop(box):
    result = box.run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "nothing to remove" in result.stdout
    assert "Meeting Notes was uninstalled for this Mac user." in result.stdout


@needs_bash
def test_run_rejects_unknown_options_without_changing_anything(box):
    box.make_app()
    result = box.run("--wipe-everything")
    assert result.returncode != 0
    assert box.app.exists()
