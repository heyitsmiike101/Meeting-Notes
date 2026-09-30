"""macOS install + update path: server routes, the mac.sh script, the in-app updater."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from meeting_notes import __version__
from meeting_notes.client import update
from meeting_notes.server import mac_installer, web
from meeting_notes.server.app import create_app

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated:DeprecationWarning"
)


def _app(tmp_path):
    return create_app(transcriber_factory=None, data_root=str(tmp_path / "data"))


# -- the script -----------------------------------------------------------------------------------


def test_installer_script_verifies_swaps_safely_and_keeps_recordings():
    script = web.render_mac_installer("http://meeting.lan")
    assert script.startswith("#!/bin/bash\n")
    assert "\r" not in script
    assert "SERVER='http://meeting.lan'" in script or "SERVER=http://meeting.lan" in script
    assert 'MANIFEST_URL="$SERVER/install/client-manifest-macos.json"' in script
    # integrity: same size + sha256 as the manifest, same host as the manifest
    assert "shasum -a 256" in script and "stat -f%z" in script
    assert "must be hosted by the same server" in script
    # safe swap: stage, move the old aside, restore on failure, never delete in place
    assert '$APP.new-$$' in script and '$APP.old-$$' in script
    assert "The previous version was restored" in script
    # per-user, no privileges, ad-hoc app only
    assert '$HOME/Applications' in script
    assert "sudo " not in script.replace("not with sudo", "")  # comments say "no sudo,"; never runs it
    assert "lan.meeting.notes" in script
    # recordings + config safety
    assert "guard_recordings" in script and "Your recordings folder is inside the app" in script
    assert "$HOME/Meeting Notes" in script
    assert "$HOME/.meeting-notes" in script and "config.json" in script
    assert "merge-config.js" in script and "cfg.server.url = url" in script and "cfg.server.token" in script
    # only stops our own installed copy
    assert 'pgrep -f "$APP/Contents/MacOS/"' in script
    assert script.rstrip().endswith('main "$@"')


def test_installer_never_embeds_a_token_and_quotes_the_address():
    script = web.render_mac_installer("http://meeting.lan:8000/")
    assert "Bearer" not in script and "token=" not in script.lower()
    assert "SERVER=http://meeting.lan:8000\n" in script  # trailing slash trimmed
    hostile = web.render_mac_installer("http://x/';rm -rf ~;'")
    assert "SERVER='http://x/'\"'\"';rm -rf ~;'\"'\"''" in hostile  # shlex-quoted, cannot break out


@pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None, reason="needs a POSIX bash")
def test_installer_script_is_valid_bash(tmp_path):
    path = tmp_path / "mac.sh"
    path.write_text(web.render_mac_installer("http://meeting.lan"), encoding="utf-8", newline="\n")
    result = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(sys.platform != "darwin", reason="exercises the real macOS tools")
def test_installer_refuses_a_recordings_folder_inside_the_app(tmp_path):
    home = tmp_path / "home"
    (home / ".meeting-notes").mkdir(parents=True)
    app = home / "Applications" / "Meeting Notes.app"
    (home / ".meeting-notes" / "config.json").write_text(
        '{"save_dir": "%s"}' % (app / "Contents" / "rec"), encoding="utf-8"
    )
    script = tmp_path / "mac.sh"
    script.write_text(web.render_mac_installer("http://127.0.0.1:9"), encoding="utf-8", newline="\n")
    result = subprocess.run(
        ["/bin/bash", str(script)], capture_output=True, text=True, env={"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
    )
    assert result.returncode != 0
    assert "recordings folder is inside the app" in result.stderr
    assert "Nothing was changed" in result.stderr


# -- server routes ------------------------------------------------------------------------------------


def test_mac_manifest_package_and_script_are_public_with_a_token(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_NOTES_TOKEN", "s3cret")
    app = _app(tmp_path)
    package = Path(app.state.store.root) / "client" / "MeetingNotes-macOS.zip"
    package.parent.mkdir(parents=True)
    payload = b"test macos package"
    package.write_bytes(payload)
    client = TestClient(app)

    manifest = client.get("/install/client-manifest-macos.json")
    assert manifest.status_code == 200
    body = manifest.json()
    assert body["url"] == "http://testserver/install/MeetingNotes-macOS.zip"
    assert body["sha256"] == hashlib.sha256(payload).hexdigest()
    assert body["size"] == len(payload)
    assert body["version"] == __version__
    script = client.get("/install/mac.sh")  # no Authorization header: curl | bash has no cookie
    assert script.status_code == 200
    assert script.text.startswith("#!/bin/bash")
    assert body["installer"]["url"] == "http://testserver/install/mac.sh"
    assert body["installer"]["size"] == len(script.content)
    assert body["installer"]["sha256"] == hashlib.sha256(script.content).hexdigest()
    zipped = client.get("/install/MeetingNotes-macOS.zip")
    assert zipped.status_code == 200 and zipped.content == payload


def test_mac_manifest_and_package_404_when_not_uploaded(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_NOTES_TOKEN", "s3cret")
    client = TestClient(_app(tmp_path))
    assert client.get("/install/client-manifest-macos.json").status_code == 404
    assert client.get("/install/MeetingNotes-macOS.zip").status_code == 404
    assert client.get("/install/mac.sh").status_code == 200  # the script itself always renders


def test_windows_manifest_is_untouched_by_the_mac_package(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = _app(tmp_path)
    client_dir = Path(app.state.store.root) / "client"
    client_dir.mkdir(parents=True)
    (client_dir / "MeetingNotes-macOS.zip").write_bytes(b"mac")
    client = TestClient(app)
    assert client.get("/install/client-manifest.json").status_code == 404  # no Windows zip
    (client_dir / "MeetingNotes-Windows.zip").write_bytes(b"win")
    body = client.get("/install/client-manifest.json").json()
    assert body["url"].endswith("/install/MeetingNotes-Windows.zip")
    assert body["installer"]["url"].endswith("/install/client-agent.ps1")


def test_install_page_has_the_mac_one_liner_and_permission_steps(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    text = TestClient(_app(tmp_path)).get("/install").text
    assert "curl -fsSL http://testserver/install/mac.sh | bash" in text
    assert "Screen &amp; System Audio Recording" in text
    assert "Microphone" in text
    assert "BlackHole" in text  # ... to say it is not needed
    # the Windows instructions are still there
    assert "client-agent.ps1" in text


# -- the in-app updater --------------------------------------------------------------------------------


def test_updater_picks_the_platform_manifest():
    assert update.manifest_path("win32") == "/install/client-manifest.json"
    assert update.manifest_path("darwin") == "/install/client-manifest-macos.json"
    assert update.manifest_url("http://meeting.lan", "darwin") == "http://meeting.lan/install/client-manifest-macos.json"
    assert update.manifest_url("http://meeting.lan", "linux").endswith("/install/client-manifest.json")


def test_check_on_a_mac_reads_the_mac_manifest_and_prefers_the_shell_installer(monkeypatch):
    seen = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "version": "9.9.9",
                "url": "/install/MeetingNotes-macOS.zip",
                "size": 5,
                "sha256": "a" * 64,
                "installer": {"url": "/install/mac.sh", "size": 7, "sha256": "b" * 64},
            }

    monkeypatch.setattr(update.sys, "platform", "darwin")
    monkeypatch.setattr(update.httpx, "get", lambda url, **kw: seen.update(url=url) or Response())
    manifest = update.ClientUpdater("http://meeting.lan", "tok", current_version="0.1.0").check()
    assert seen["url"] == "http://meeting.lan/install/client-manifest-macos.json"
    assert manifest.download_url == "http://meeting.lan/install/mac.sh"
    assert (manifest.size, manifest.sha256) == (7, "b" * 64)


def test_downloaded_installer_on_a_mac_gets_a_sh_suffix(monkeypatch):
    monkeypatch.setattr(update.sys, "platform", "darwin")
    manifest = update.UpdateManifest("1.0.0", "http://meeting.lan/install/download", 1, "c" * 64)
    path = update.ClientUpdater._temporary_path(manifest)
    try:
        assert path.suffix == ".sh"
    finally:
        path.unlink()


def test_apply_runs_the_mac_installer_with_bash_detached(monkeypatch, tmp_path):
    seen = {}

    def fake_popen(cmd, **kw):
        seen.update(cmd=cmd, **kw)
        return object()

    monkeypatch.setattr(update.sys, "platform", "darwin")
    monkeypatch.setattr(update.subprocess, "Popen", fake_popen)
    installer = tmp_path / "mac.sh"
    installer.write_text("#!/bin/bash\n", encoding="utf-8")
    update.ClientUpdater.apply(installer)
    assert seen["cmd"] == ["/bin/bash", str(installer)]
    assert seen["start_new_session"] is True  # survives the app it is about to quit
    assert seen["cwd"] == tempfile.gettempdir()


def test_apply_rejects_a_shell_installer_off_macos_and_a_ps1_on_macos(monkeypatch, tmp_path):
    sh = tmp_path / "mac.sh"
    sh.write_text("x", encoding="utf-8")
    ps1 = tmp_path / "x.ps1"
    ps1.write_text("x", encoding="utf-8")
    monkeypatch.setattr(update.subprocess, "Popen", lambda *a, **k: pytest.fail("must not launch"))
    monkeypatch.setattr(update.sys, "platform", "win32")
    with pytest.raises(update.UpdateError, match="macOS"):
        update.ClientUpdater.apply(sh)
    monkeypatch.setattr(update.sys, "platform", "darwin")
    monkeypatch.setattr(update.os, "name", "posix")
    with pytest.raises(update.UpdateError, match="Windows only"):
        update.ClientUpdater.apply(ps1)


def test_mac_installer_constants_agree_with_the_build_script():
    build = (Path(__file__).resolve().parents[1] / "tools" / "build_macos.sh").read_text(encoding="utf-8")
    assert mac_installer.BUNDLE_ID in build
    assert mac_installer.APP_NAME.replace(".app", "") in build
    assert mac_installer.PACKAGE_NAME in build
