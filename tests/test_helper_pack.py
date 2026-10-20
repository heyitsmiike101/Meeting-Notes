"""Helper pack builder and API, fully offline (fake downloader / pip / model fetcher)."""

from __future__ import annotations

import hashlib
import io
import json
import sys
import types
import zipfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from meeting_notes.server import helper_pack as hp
from meeting_notes.server import helper_pack_api


def _uv_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("uv-x86_64-pc-windows-msvc/uv.exe", b"UVEXE")
    return buf.getvalue()


PY_BYTES = b"fake python tarball"


class Fakes:
    def __init__(self, tmp_path: Path, *, token="tok", bad_hash=None, diar_error=None, pip_fail=False):
        self.token = token
        self.bad_hash = bad_hash
        self.diar_error = diar_error
        self.pip_fail = pip_fail
        self.model = "small.en"
        self.fetched = []
        self.uv = _uv_zip()
        self.req = tmp_path / "req.txt"
        self.req.write_text("--extra-index-url https://example.invalid/whl\n# c\ntorch==1.0\nnumpy==2.0\n")
        self.code = tmp_path / "pkg"
        (self.code / "client" / "ui").mkdir(parents=True)
        (self.code / "server" / "static").mkdir(parents=True)
        (self.code / "compute_helper").mkdir()
        (self.code / "a.py").write_text("x = 1\n")
        (self.code / "client" / "ui" / "w.py").write_text("ui\n")
        (self.code / "client" / "c.py").write_text("c\n")
        (self.code / "server" / "static" / "app.css").write_text("css")
        (self.code / "compute_helper" / "clip.wav").write_bytes(b"RIFF")

    def downloader(self, url, dest, progress):
        data = self.uv if url == hp.UV_URL else PY_BYTES
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        progress(len(data), len(data))

    def pip(self, args, dest, tick):
        if self.pip_fail:
            raise hp.PackError("pip download failed: boom")
        (dest / "torch-1.0-cp312-cp312-win_amd64.whl").write_bytes(b"w" * 10)
        tick()

    def fetch(self, repo, dest, token, patterns):
        self.fetched.append((repo, token))
        if "pyannote" in repo and self.diar_error:
            raise self.diar_error
        (dest / "model.bin").write_bytes(b"m" * 5)
        (dest / ".cache").mkdir(exist_ok=True)
        (dest / ".cache" / "junk").write_text("x")


@pytest.fixture
def patched(monkeypatch):
    monkeypatch.setattr(hp, "UV_SHA256", hashlib.sha256(_uv_zip()).hexdigest())
    monkeypatch.setattr(hp, "PYTHON_SHA256", hashlib.sha256(PY_BYTES).hexdigest())


def make(tmp_path, fakes, **kw):
    settings = types.SimpleNamespace(model=fakes.model, diarization_model="pyannote/speaker-diarization-community-1")
    return hp.HelperPackBuilder(
        tmp_path / "data",
        settings_provider=lambda: settings,
        downloader=fakes.downloader,
        pip_runner=fakes.pip,
        model_fetcher=fakes.fetch,
        token_provider=lambda: fakes.token,
        requirements_path=fakes.req,
        code_root=fakes.code,
        app_version="9.9.9",
        **kw,
    ), settings


def test_build_manifest_contents(tmp_path, patched):
    f = Fakes(tmp_path)
    b, _ = make(tmp_path, f)
    version = b.build()
    m = b.manifest()
    assert m["pack_version"] == version == b.current_version()
    assert m["python"] == hp.PYTHON_VERSION
    assert m["requirements"] == ["torch==1.0", "numpy==2.0"]
    assert m["models"] == {"whisper": "small.en", "diarization": "pyannote/speaker-diarization-community-1"}
    by_path = {x["path"]: x for x in m["files"]}
    assert by_path["uv.exe"]["kind"] == "uv" and by_path["uv.exe"]["size"] == 5
    assert by_path[hp.PYTHON_PACK_PATH]["kind"] == "python"
    assert by_path["wheels/torch-1.0-cp312-cp312-win_amd64.whl"]["kind"] == "wheel"
    assert by_path["helper-code.zip"]["kind"] == "code"
    assert by_path["models/whisper/small.en/model.bin"]["kind"] == "model"
    assert "requirements.txt" in by_path
    assert not any(".cache" in p for p in by_path)
    assert m["total_size"] == sum(x["size"] for x in m["files"])
    for x in m["files"]:
        assert x["sha256"] == hashlib.sha256((b.current_dir() / x["path"]).read_bytes()).hexdigest()
    assert ("Systran/faster-whisper-small.en", "tok") in f.fetched
    req = (b.current_dir() / "requirements.txt").read_text()
    assert "extra-index" not in req and "torch==1.0" in req


def test_helper_code_zip_excludes_ui_and_static(tmp_path, patched):
    f = Fakes(tmp_path)
    b, _ = make(tmp_path, f)
    b.build()
    with zipfile.ZipFile(b.current_dir() / "helper-code.zip") as zf:
        names = set(zf.namelist())
    assert "meeting_notes/a.py" in names
    assert "meeting_notes/client/c.py" in names
    assert "meeting_notes/compute_helper/clip.wav" in names
    assert "meeting_notes/client/ui/w.py" not in names
    assert not any("static" in n for n in names)


def test_missing_token_omits_diarization(tmp_path, patched):
    f = Fakes(tmp_path, token=None)
    b, _ = make(tmp_path, f)
    b.build()
    m = b.manifest()
    assert m["models"]["diarization"] is None
    assert "HUGGINGFACE_TOKEN" in m["note"]
    assert not any(p["path"].startswith("models/diarization") for p in m["files"])
    assert all("pyannote" not in repo for repo, _ in f.fetched)


def test_auth_error_omits_diarization(tmp_path, patched):
    class GatedRepoError(Exception):
        pass

    f = Fakes(tmp_path, diar_error=GatedRepoError("403 gated"))
    b, _ = make(tmp_path, f)
    b.build()
    m = b.manifest()
    assert m["models"]["diarization"] is None and m["note"]
    assert not (b.current_dir() / "models" / "diarization").exists()


def test_other_diarization_error_fails_build(tmp_path, patched):
    f = Fakes(tmp_path, diar_error=RuntimeError("disk exploded"))
    b, _ = make(tmp_path, f)
    with pytest.raises(RuntimeError):
        b.build()
    assert b.current_version() is None


def test_hash_mismatch_aborts(tmp_path, monkeypatch):
    monkeypatch.setattr(hp, "UV_SHA256", "0" * 64)
    monkeypatch.setattr(hp, "PYTHON_SHA256", hashlib.sha256(PY_BYTES).hexdigest())
    f = Fakes(tmp_path)
    b, _ = make(tmp_path, f)
    with pytest.raises(hp.PackError, match="SHA-256"):
        b.build()
    assert b.current_version() is None
    assert not [p for p in b.root.iterdir() if p.is_dir()]  # temp dir cleaned up


def test_failed_rebuild_keeps_previous_pack(tmp_path, patched):
    f = Fakes(tmp_path)
    b, settings = make(tmp_path, f)
    v1 = b.build()
    settings.model = "base.en"
    assert b.is_out_of_date()
    f.pip_fail = True
    with pytest.raises(hp.PackError):
        b.build()
    assert b.current_version() == v1
    assert b.manifest()["pack_version"] == v1
    assert (b.root / v1 / "manifest.json").is_file()
    assert not [p for p in b.root.iterdir() if p.name.startswith(".build-")]


def test_rebuild_swaps_and_keeps_one_previous(tmp_path, patched):
    f = Fakes(tmp_path)
    b, settings = make(tmp_path, f)
    v1 = b.build()
    settings.model = "base.en"
    v2 = b.build()
    assert v2 != v1 and b.current_version() == v2
    assert (b.root / v1).is_dir()  # previous kept
    settings.model = "tiny.en"
    v3 = b.build()
    assert not (b.root / v1).exists() and (b.root / v2).is_dir() and b.current_version() == v3


def test_background_start_status(tmp_path, patched):
    f = Fakes(tmp_path)
    b, _ = make(tmp_path, f)
    assert b.status()["state"] == "idle" and not b.status()["has_pack"]
    assert b.start() is True
    b.wait(10)
    st = b.status()
    assert st["state"] == "ready" and st["pack_version"] and st["out_of_date"] is False and st["error"] is None


def test_background_error_status(tmp_path, patched):
    f = Fakes(tmp_path, pip_fail=True)
    b, _ = make(tmp_path, f)
    b.start()
    b.wait(10)
    st = b.status()
    assert st["state"] == "error" and "boom" in st["error"]


def test_no_model_configured(tmp_path, patched):
    f = Fakes(tmp_path)
    f.model = ""
    b, _ = make(tmp_path, f)
    with pytest.raises(hp.PackError, match="no Whisper model"):
        b.build()


def test_pack_version_changes_with_inputs(tmp_path, patched):
    f = Fakes(tmp_path)
    b, settings = make(tmp_path, f)
    v = b.planned_version()
    f.req.write_text("torch==1.1\n")
    assert b.planned_version() != v
    f.req.write_text("torch==1.0\nnumpy==2.0\n")
    assert b.planned_version() == v
    f.token = None
    assert b.planned_version() != v


# -- API -----------------------------------------------------------------------------------------------------


@pytest.fixture
def api(tmp_path, patched, monkeypatch):
    fake_compute = types.ModuleType("meeting_notes.server.compute")
    fake_compute.calls = 0

    def require_helper_key_or_token(request):
        fake_compute.calls += 1
        if request.headers.get("authorization") != "Bearer ok":
            from fastapi import HTTPException

            raise HTTPException(status_code=401, detail="nope")

    fake_compute.require_helper_key_or_token = require_helper_key_or_token
    monkeypatch.setitem(sys.modules, "meeting_notes.server.compute", fake_compute)
    import meeting_notes.server as server_pkg

    monkeypatch.setattr(server_pkg, "compute", fake_compute, raising=False)
    monkeypatch.setenv("MEETING_NOTES_TOKEN", "ok")
    f = Fakes(tmp_path)
    b, _ = make(tmp_path, f)
    app = FastAPI()
    app.include_router(helper_pack_api.make_router(tmp_path / "data", b))
    return TestClient(app), b, {"Authorization": "Bearer ok"}


def test_api_no_pack_and_auth(api):
    c, b, h = api
    assert c.get("/v1/compute/pack/manifest").status_code == 401
    r = c.get("/v1/compute/pack/manifest", headers=h)
    assert r.status_code == 404 and r.json() == {"error": "no pack"}
    assert c.get("/v1/compute/pack/status").status_code == 401
    assert c.get("/v1/compute/pack/status", headers=h).json()["state"] == "idle"


def test_api_prepare_manifest_and_download(api):
    c, b, h = api
    r = c.post("/v1/compute/pack/prepare", headers=h)
    assert r.status_code == 202 and r.json()["started"] is True
    b.wait(10)
    m = c.get("/v1/compute/pack/manifest", headers=h).json()
    assert m["models"]["whisper"] == "small.en"
    r = c.get("/v1/compute/pack/files/uv.exe", headers=h)
    assert r.status_code == 200 and r.content == b"UVEXE"
    assert r.headers["accept-ranges"] == "bytes"


def test_api_range(api):
    c, b, h = api
    b.build()
    path = "models/whisper/small.en/model.bin"  # b"mmmmm"
    r = c.get(f"/v1/compute/pack/files/{path}", headers={**h, "Range": "bytes=1-3"})
    assert r.status_code == 206 and r.content == b"mmm"
    assert r.headers["content-range"] == "bytes 1-3/5"
    r = c.get(f"/v1/compute/pack/files/{path}", headers={**h, "Range": "bytes=3-"})
    assert r.status_code == 206 and r.content == b"mm"
    r = c.get(f"/v1/compute/pack/files/{path}", headers={**h, "Range": "bytes=-2"})
    assert r.status_code == 206 and r.content == b"mm" and r.headers["content-range"] == "bytes 3-4/5"
    r = c.get(f"/v1/compute/pack/files/{path}", headers={**h, "Range": "bytes=9-"})
    assert r.status_code == 416
    r = c.get(f"/v1/compute/pack/files/{path}", headers={**h, "Range": "garbage"})
    assert r.status_code == 416


def test_api_traversal_and_unlisted(api, tmp_path):
    c, b, h = api
    b.build()
    (b.root / "secret.txt").write_text("s")
    for bad in ["../secret.txt", "..%2Fsecret.txt", "%2e%2e/secret.txt", "manifest.json", "nope.bin",
                "models/../manifest.json", "C:/Windows/win.ini"]:
        assert c.get(f"/v1/compute/pack/files/{bad}", headers=h).status_code == 404, bad
    assert b.file_path("../secret.txt") is None


def test_api_version_serves_the_previous_pack_and_410_when_it_is_gone(api):
    c, b, h = api
    v1 = b.build()
    (b.root / v1 / "uv.exe").write_bytes(b"OLD-UV")
    settings = b.settings_provider()
    settings.model = "base.en"
    v2 = b.build()
    assert v2 != v1
    cur = c.get("/v1/compute/pack/files/uv.exe", headers=h)
    assert cur.status_code == 200 and cur.content == b"UVEXE"
    old = c.get("/v1/compute/pack/files/uv.exe", params={"version": v1}, headers=h)
    assert old.status_code == 200 and old.content == b"OLD-UV"
    ranged = c.get("/v1/compute/pack/files/uv.exe", params={"version": v1}, headers={**h, "Range": "bytes=4-"})
    assert ranged.status_code == 206 and ranged.content == b"UV"
    # a file the old manifest does not list, and traversal, stay 404 with a version
    assert c.get("/v1/compute/pack/files/manifest.json", params={"version": v1}, headers=h).status_code == 404
    assert c.get("/v1/compute/pack/files/..%2Fsecret.txt", params={"version": v1}, headers=h).status_code == 404
    # a third build prunes v1
    settings.model = "tiny.en"
    b.build()
    gone = c.get("/v1/compute/pack/files/uv.exe", params={"version": v1}, headers=h)
    assert gone.status_code == 410 and "no longer available" in gone.json()["detail"]
    assert c.get("/v1/compute/pack/files/uv.exe", params={"version": "../x"}, headers=h).status_code == 410
    assert c.get("/v1/compute/pack/files/uv.exe", params={"version": "nope"}, headers=h).status_code == 410


def test_parse_range():
    assert helper_pack_api.parse_range("bytes=0-0", 5) == (0, 0)
    assert helper_pack_api.parse_range("bytes=2-100", 5) == (2, 4)
    assert helper_pack_api.parse_range("bytes=5-", 5) is None
    with pytest.raises(ValueError):
        helper_pack_api.parse_range("bytes=-", 5)
    with pytest.raises(ValueError):
        helper_pack_api.parse_range("items=1-2", 5)
