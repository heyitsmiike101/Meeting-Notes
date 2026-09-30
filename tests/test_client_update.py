"""Hermetic tests for the client update channel (no network or installer)."""

from __future__ import annotations

import hashlib

import pytest

from meeting_notes.client import update


def test_semver_comparison_handles_prereleases_and_build_metadata():
    assert update.is_newer_version("v1.2.3", "1.2.2")
    assert update.is_newer_version("1.2.3", "1.2.3-rc.2")
    assert not update.is_newer_version("1.2.3+build.9", "1.2.3+build.10")
    assert update.is_newer_version("1.2.3-rc.10", "1.2.3-rc.2")
    assert not update.is_newer_version("01.2.3", "1.2.2")


def test_manifest_requires_size_and_sha256_and_resolves_server_path():
    data = {
        "version": "1.0.1",
        "url": "/install/client-agent.ps1",
        "size": 12,
        "sha256": "a" * 64,
    }
    manifest = update.UpdateManifest.from_json(data, "http://meeting.lan:8000")
    assert manifest.download_url == "http://meeting.lan:8000/install/client-agent.ps1"
    assert manifest.size == 12
    with pytest.raises(update.UpdateError):
        update.UpdateManifest.from_json({**data, "sha256": "bad"}, "http://meeting.lan")
    with pytest.raises(update.UpdateError, match="configured server"):
        update.UpdateManifest.from_json(
            {**data, "url": "https://attacker.example/update.ps1"}, "http://meeting.lan"
        )


def test_check_uses_authenticated_manifest_and_returns_only_newer(monkeypatch):
    seen = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "version": "0.4.0",
                "url": "/install/client-agent.ps1",
                "size": 1,
                "sha256": "b" * 64,
            }

    def fake_get(url, **kwargs):
        seen.update(url=url, **kwargs)
        return Response()

    monkeypatch.setattr(update.httpx, "get", fake_get)
    monkeypatch.setattr(update.sys, "platform", "win32")  # the Windows manifest; macOS has its own test
    manifest = update.ClientUpdater("http://meeting.lan", "secret", current_version="0.3.0").check()
    assert manifest is not None and manifest.version == "0.4.0"
    assert seen["url"].endswith("/install/client-manifest.json")
    assert seen["headers"] == {
        "Authorization": "Bearer secret",
        "Accept-Encoding": "identity",
    }


def test_manifest_prefers_runnable_installer_over_zip():
    manifest = update.UpdateManifest.from_json(
        {
            "version": "0.3.0",
            "url": "/install/MeetingNotes-Windows.zip",
            "size": 999,
            "sha256": "b" * 64,
            "installer": {
                "url": "/install/client-agent.ps1",
                "size": 12,
                "sha256": "a" * 64,
            },
        },
        "http://meeting.lan",
    )
    assert manifest.download_url == "http://meeting.lan/install/client-agent.ps1"
    assert manifest.size == 12
    assert manifest.sha256 == "a" * 64


def test_download_rejects_wrong_size_or_digest_without_leaving_partial_file(monkeypatch, tmp_path):
    payload = b"verified installer"

    class StreamResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def raise_for_status(self):
            return None

        def iter_bytes(self, chunk_size):
            yield payload[:5]
            yield payload[5:]

    monkeypatch.setattr(update.httpx, "stream", lambda *args, **kwargs: StreamResponse())
    good = update.UpdateManifest(
        "0.4.0", "http://meeting.lan/install/client-agent.ps1", len(payload), hashlib.sha256(payload).hexdigest()
    )
    path = update.ClientUpdater("http://meeting.lan").download(good, tmp_path / "agent.ps1")
    assert path.read_bytes() == payload

    bad = update.UpdateManifest(good.version, good.download_url, good.size + 1, good.sha256)
    with pytest.raises(update.UpdateError, match="manifest verification"):
        update.ClientUpdater("http://meeting.lan").download(bad, tmp_path / "bad.ps1")
    assert not (tmp_path / "bad.ps1").exists()


def test_apply_launches_installer_outside_the_app_folder(monkeypatch, tmp_path):
    import tempfile
    from meeting_notes.client import update as update_mod

    seen = {}
    monkeypatch.setattr(update_mod.os, "name", "nt")
    monkeypatch.setattr(update_mod.subprocess, "Popen", lambda cmd, **kw: seen.update(kw) or object())
    installer = tmp_path / "installer.ps1"
    installer.write_text("# test", encoding="utf-8")
    update_mod.ClientUpdater.apply(installer)
    assert seen["cwd"] == tempfile.gettempdir()
