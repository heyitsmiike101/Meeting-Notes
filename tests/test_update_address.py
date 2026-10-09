"""Updates follow the address the client used, not the saved ``server_address``;
update installers log to update.log."""

from __future__ import annotations

import hashlib
import logging
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from meeting_notes.client import update
from meeting_notes.server import settings as settings_mod
from meeting_notes.server import web
from meeting_notes.server.app import (
    UPDATE_INSTALLER_MAC_PATH,
    UPDATE_INSTALLER_PS1_PATH,
    create_app,
)

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated:DeprecationWarning"
)

SAVED = "http://notes.example.lan"
VIA_IP = "http://192.0.2.10:8000"


def _setup(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = create_app(transcriber_factory=None, data_root=str(tmp_path / "data"))
    root = Path(app.state.store.root)
    (root / "client").mkdir(parents=True)
    (root / "client" / "MeetingNotes-Windows.zip").write_bytes(b"win")
    (root / "client" / "MeetingNotes-macOS.zip").write_bytes(b"mac")
    settings_mod.save_settings(root, settings_mod.Settings(model="base.en", server_address=SAVED))
    return app


def test_manifests_point_at_update_endpoints_with_request_address(tmp_path, monkeypatch):
    client = TestClient(_setup(tmp_path, monkeypatch), base_url=VIA_IP)
    for path, update_path, render in (
        ("/install/client-manifest.json", UPDATE_INSTALLER_PS1_PATH, web.render_client_installer),
        ("/install/client-manifest-macos.json", UPDATE_INSTALLER_MAC_PATH, web.render_mac_installer),
    ):
        body = client.get(path).json()
        assert body["url"].startswith(VIA_IP + "/")
        assert body["installer"]["url"] == VIA_IP + update_path
        expected = render(VIA_IP).encode("utf-8")
        assert body["installer"]["sha256"] == hashlib.sha256(expected).hexdigest()
        assert body["installer"]["size"] == len(expected)
        served = client.get(update_path)
        assert served.status_code == 200
        assert served.content == expected  # manifest hash == endpoint bytes
        assert "notes.example.lan" not in served.text
        assert VIA_IP in served.text


def test_browser_installers_still_use_saved_server_address(tmp_path, monkeypatch):
    client = TestClient(_setup(tmp_path, monkeypatch), base_url=VIA_IP)
    ps1 = client.get("/install/client-agent.ps1").text
    sh = client.get("/install/mac.sh").text
    assert SAVED in ps1 and SAVED in sh
    assert VIA_IP not in ps1 and VIA_IP not in sh


def test_update_endpoint_logs_address_and_client(tmp_path, monkeypatch, caplog):
    client = TestClient(_setup(tmp_path, monkeypatch), base_url=VIA_IP)
    with caplog.at_level(logging.INFO, logger="meeting_notes.server.app"):
        client.get(UPDATE_INSTALLER_PS1_PATH, headers={"X-Meeting-Notes-Client": "0.7.12"})
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "update installer" in text and VIA_IP in text and "0.7.12" in text


def test_real_updater_flow_gets_installer_for_its_own_address(tmp_path, monkeypatch):
    """check() -> download() through the real updater (transport stubbed onto the app):
    the downloaded installer bakes in the address used, not server_address."""
    tc = TestClient(_setup(tmp_path, monkeypatch), base_url=VIA_IP)

    def fake_get(url, **kw):
        return tc.get(url.replace(VIA_IP, ""), headers=kw.get("headers"))

    class _Stream:
        def __init__(self, resp):
            self.resp = resp

        def __enter__(self):
            return self.resp

        def __exit__(self, *a):
            return False

    def fake_stream(method, url, **kw):
        resp = tc.get(url.replace(VIA_IP, ""), headers=kw.get("headers"))
        resp.iter_bytes = lambda n=None: iter([resp.content])
        return _Stream(resp)

    monkeypatch.setattr(update.httpx, "get", fake_get)
    monkeypatch.setattr(update.httpx, "stream", fake_stream)
    updater = update.ClientUpdater(VIA_IP, "", current_version="0.0.1")
    manifest = updater.check()
    assert manifest is not None and manifest.download_url == VIA_IP + UPDATE_INSTALLER_PS1_PATH
    path = updater.download(manifest, tmp_path / "Install.ps1")
    text = path.read_text(encoding="utf-8")
    assert VIA_IP in text and "notes.example.lan" not in text


def test_windows_installer_template_logs_to_update_log():
    script = web.render_client_installer(VIA_IP)
    assert "update.log" in script
    assert "function Write-Log" in script and "Initialize-Log" in script
    assert "262144" in script and "-Tail 200" in script
    assert "trap {" in script and "installer finished: FAILED" in script
    assert "installer finished: OK" in script
    assert "MEETING_NOTES_UPDATE" in script
    # console output for interactive installs is unchanged
    assert 'Write-Host "`n==> $message" -ForegroundColor Cyan' in script
    assert VIA_IP in script


def test_windows_installer_parses_in_powershell(tmp_path):
    exe = shutil.which("pwsh") or shutil.which("powershell")
    if exe is None:
        pytest.skip("no PowerShell available")
    script = tmp_path / "install.ps1"
    script.write_text(web.render_client_installer(VIA_IP), encoding="utf-8")
    probe = (
        "$e=$null;$t=$null;"
        f"[void][System.Management.Automation.Language.Parser]::ParseFile('{script}',[ref]$t,[ref]$e);"
        "if($e.Count){$e|%{$_.Message};exit 1}"
    )
    out = subprocess.run([exe, "-NoProfile", "-Command", probe], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stdout + out.stderr


def test_mac_installer_always_logs_and_parses(tmp_path):
    script = web.render_mac_installer(VIA_IP)
    # logline appends regardless of MEETING_NOTES_UPDATE; only terminal output is conditional
    assert '>>"$UPDATE_LOG"' in script
    assert 'logline() { printf' in script
    assert 'if [ -z "$UPDATING" ]' in script  # interactive runs keep terminal output
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("no bash")
    path = tmp_path / "mac.sh"
    path.write_bytes(script.encode("utf-8"))
    out = subprocess.run([bash, "-n", str(path)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr


def test_client_logs_failure_reasons(tmp_path, monkeypatch, caplog):
    def boom(*a, **k):
        raise httpx.ConnectError("name resolution failed")

    monkeypatch.setattr(update.httpx, "get", boom)
    monkeypatch.setattr(update.httpx, "stream", boom)
    updater = update.ClientUpdater("http://meeting.example", "", current_version="0.0.1")
    with caplog.at_level(logging.INFO, logger="meeting_notes.client.update"):
        with pytest.raises(update.UpdateError):
            updater.check()
        manifest = update.UpdateManifest("9.9.9", "http://meeting.example/x.ps1", 3, "0" * 64)
        with pytest.raises(update.UpdateError):
            updater.download(manifest, tmp_path / "x.ps1")
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "update check failed" in text and "name resolution failed" in text
    assert "update download failed" in text and "http://meeting.example/x.ps1" in text


def test_windows_apply_sets_update_env(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(update.os, "name", "nt")
    monkeypatch.setattr(update.subprocess, "Popen", lambda cmd, **kw: seen.update(kw=kw, cmd=cmd))
    script = tmp_path / "i.ps1"
    script.write_text("x")
    update.ClientUpdater.apply(script)
    assert seen["kw"]["env"]["MEETING_NOTES_UPDATE"] == "1"
