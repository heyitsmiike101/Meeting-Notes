"""The GPU runtime installer (``client/gpu_runtime.py``): register, download with resume, verify, build, swap.

A fake server (``httpx.MockTransport``) serves a tiny helper pack and a fake runner stands in for ``uv`` and the
runtime's Python, so nothing is downloaded or installed for real.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6", reason="PySide6 not installed")

from PySide6.QtWidgets import QApplication  # noqa: E402

from meeting_notes import config as config_mod  # noqa: E402
from meeting_notes.client import gpu_runtime as gr  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def env(tmp_path, monkeypatch, qt_app):
    monkeypatch.setenv("MEETING_NOTES_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setattr(gr, "supported", lambda platform=None: (platform or "win32") == "win32")
    return tmp_path


def _tar_gz() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"fake python"
        info = tarfile.TarInfo("python/python.exe")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


WHEEL = os.urandom(5000)
CONTENT = {
    "uv.exe": (b"fake uv", "uv"),
    "python/cpython-3.12.tar.gz": (_tar_gz(), "python"),
    "wheels/torch-1.whl": (WHEEL, "wheel"),
    "helper-code.zip": (b"PK fake zip", "code"),
    "models/whisper/tiny/model.bin": (b"model bytes", "model"),
}


class FakeServer:
    def __init__(self, *, tamper=None, manifest_status=200, key="mnh_secret"):
        self.tamper = tamper  # a path whose served bytes are wrong
        self.manifest_status = manifest_status
        self.key = key
        self.requests = []
        self.ranges = []

    def manifest(self):
        files = [
            {"path": p, "size": len(b), "sha256": hashlib.sha256(b).hexdigest(), "kind": k}
            for p, (b, k) in CONTENT.items()
        ]
        return {
            "pack_version": "pack-abcdef123456", "created": "2026-10-15", "python": "3.12", "files": files,
            "requirements": "torch==2.5.0\nfaster-whisper==1.0.0", "models": {"whisper": "tiny", "diarization": None},
            "total_size": sum(len(b) for b, _k in CONTENT.values()),
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append((request.method, path))
        if request.method == "POST" and path == "/v1/compute/helpers":
            body = json.loads(request.content)
            assert body["name"] and body["instance_id"]
            return httpx.Response(201, json={"helper_id": "h-1", "key": self.key})
        if path == "/v1/compute/pack/manifest":
            if self.manifest_status != 200:
                return httpx.Response(self.manifest_status, json={"error": "no pack"})
            return httpx.Response(200, json=self.manifest())
        if path.startswith("/v1/compute/pack/files/"):
            name = path[len("/v1/compute/pack/files/"):]
            data = CONTENT[name][0]
            if name == self.tamper:
                data = b"X" * len(data)
            rng = request.headers.get("range")
            if rng:
                self.ranges.append((name, rng))
                start = int(rng.split("=")[1].rstrip("-"))
                return httpx.Response(206, content=data[start:])
            return httpx.Response(200, content=data)
        return httpx.Response(404)


class FakeRunner:
    """Stands in for ``uv`` and the runtime's Python. ``fail`` is a substring of a command that should fail."""

    def __init__(self, fail=None):
        self.fail = fail
        self.calls = []

    def __call__(self, cmd, env, cancel):
        self.calls.append((list(cmd), dict(env)))
        text = " ".join(cmd)
        if self.fail and self.fail in text:
            return 1, "boom: it broke"
        if cmd[1:2] == ["venv"]:
            venv = Path(cmd[-1])
            py = venv / "Scripts" / "python.exe" if os.name == "nt" else venv / "bin" / "python"
            py.parent.mkdir(parents=True)
            py.write_text("py")
            (venv / "pyvenv.cfg").write_text(f"home = {Path(cmd[3]).parent}\n")
            return 0, ""
        if "self-test" in cmd:
            return 0, 'noise\n{"ok": true, "gpu": "fake"}\n'
        return 0, "ok"


def _installer(env, server, runner, **kw):
    inst = gr.GpuRuntimeInstaller(
        "http://server.test", "main-token", root=env / "rt", transport=httpx.MockTransport(server),
        runner=runner, device_name="TEST-PC", **kw,
    )
    inst.events = []
    inst.done = []
    inst.progress.connect(lambda step, d, t: inst.events.append((step, d, t)))
    inst.finished.connect(lambda ok, msg: inst.done.append((ok, msg)))
    return inst


def test_install_registers_downloads_builds_and_swaps_in(env):
    server, runner = FakeServer(), FakeRunner()
    inst = _installer(env, server, runner)
    assert inst.run() is True
    assert inst.done == [(True, "GPU support installed (pack pack-abcdef1)")]
    root = env / "rt"
    cur = root / "current"
    assert gr.installed_info(root)["pack_version"] == "pack-abcdef123456"
    assert gr.venv_python(cur).exists() and (cur / "helper-code.zip").read_bytes() == b"PK fake zip"
    assert (cur / "models" / "whisper" / "tiny" / "model.bin").read_bytes() == b"model bytes"
    assert not (root / "staging").exists() and not (root / "previous").exists()
    # registered once with the main token's client, key and pack version saved
    group = config_mod.gpu_helper_settings()
    assert group["key"] == "mnh_secret" and group["helper_id"] == "h-1"
    assert group["installed_pack_version"] == "pack-abcdef123456" and group["instance_id"]
    assert [m for m, p in server.requests if p == "/v1/compute/helpers"] == ["POST"]
    # the build steps, in order: venv, offline pip install, self-test with helper code on PYTHONPATH, start check
    kinds = []
    for cmd, env_vars in runner.calls:
        text = " ".join(cmd)
        if cmd[1:2] == ["venv"]:
            kinds.append("venv")
        elif "install" in cmd:
            kinds.append("pip")
            assert "--no-index" in cmd and "--find-links" in cmd and "-r" in cmd
            assert Path(cmd[cmd.index("-r") + 1]).name == "requirements.txt"
        elif "self-test" in cmd:
            kinds.append("self-test")
            assert "meeting_notes.compute_helper" in text and "--models" in cmd
            assert env_vars["PYTHONPATH"].endswith("helper-code.zip")
        else:
            kinds.append("check")
    assert kinds == ["venv", "pip", "self-test", "check"]
    # the venv's pyvenv.cfg follows the move into current/
    cfg = (cur / "venv" / "pyvenv.cfg").read_text()
    assert str(cur) in cfg and "staging" not in cfg
    steps = [e[0] for e in inst.events]
    assert steps[0] == "Registering" and "Downloading" in steps and steps[-1] == "Finishing"
    assert inst.events[steps.index("Downloading")][2] > 0


def test_a_helper_that_already_has_a_key_does_not_register_again(env):
    config_mod.save_config({"gpu_helper": {"key": "mnh_old", "helper_id": "h-0"}})
    server = FakeServer()
    assert _installer(env, server, FakeRunner()).run() is True
    assert not any(p == "/v1/compute/helpers" for _m, p in server.requests)
    assert config_mod.gpu_helper_settings()["key"] == "mnh_old"


def test_a_checksum_mismatch_fails_removes_the_file_and_installs_nothing(env):
    server = FakeServer(tamper="wheels/torch-1.whl")
    inst = _installer(env, server, FakeRunner())
    assert inst.run() is False
    ok, message = inst.done[0]
    assert ok is False and "checksum" in message and "wheels/torch-1.whl" in message
    assert not (env / "rt" / "staging" / "files" / "wheels" / "torch-1.whl").exists()
    assert not (env / "rt" / "current").exists()
    assert config_mod.gpu_helper_settings()["installed_pack_version"] is None


def test_a_partial_download_resumes_with_a_range_request(env):
    root = env / "rt"
    partial = root / "staging" / "files" / "wheels"
    partial.mkdir(parents=True)
    (root / "staging" / "pack-version").write_text("pack-abcdef123456")
    (partial / "torch-1.whl").write_bytes(WHEEL[:1234])
    server = FakeServer()
    inst = _installer(env, server, FakeRunner())
    assert inst.run() is True
    assert server.ranges == [("wheels/torch-1.whl", "bytes=1234-")]
    assert (root / "current").exists()


def test_a_server_that_ignores_range_restarts_the_file(env):
    class NoRange(FakeServer):
        def __call__(self, request):
            if "range" in request.headers:
                request = httpx.Request(request.method, request.url, headers=[
                    (k, v) for k, v in request.headers.items() if k != "range"])
            return super().__call__(request)

    root = env / "rt"
    (root / "staging" / "files" / "wheels").mkdir(parents=True)
    (root / "staging" / "pack-version").write_text("pack-abcdef123456")
    (root / "staging" / "files" / "wheels" / "torch-1.whl").write_bytes(b"garbage-prefix")
    inst = _installer(env, NoRange(), FakeRunner())
    assert inst.run() is True


def test_a_new_pack_discards_the_old_partial_download(env):
    root = env / "rt"
    (root / "staging" / "files").mkdir(parents=True)
    (root / "staging" / "pack-version").write_text("an-older-pack")
    (root / "staging" / "files" / "stale.bin").write_bytes(b"x")
    assert _installer(env, FakeServer(), FakeRunner()).run() is True
    assert not (root / "staging").exists()


def _old_install(root: Path) -> None:
    cur = root / "current"
    gr.venv_python(cur).parent.mkdir(parents=True)
    gr.venv_python(cur).write_text("old python")
    (cur / "helper-code.zip").write_bytes(b"old")
    (cur / "pack.json").write_text(json.dumps({"pack_version": "old-pack"}))
    (cur / "marker.txt").write_text("old runtime")


def test_the_old_runtime_is_kept_when_the_new_one_fails_its_self_test(env):
    _old_install(env / "rt")
    inst = _installer(env, FakeServer(), FakeRunner(fail="self-test"))
    assert inst.run() is False
    assert "self-test failed" in inst.done[0][1].lower()
    assert (env / "rt" / "current" / "marker.txt").read_text() == "old runtime"
    assert gr.installed_info(env / "rt")["pack_version"] == "old-pack"


def test_a_self_test_that_reports_not_ok_fails_the_install(env):
    class Runner(FakeRunner):
        def __call__(self, cmd, env, cancel):
            if "self-test" in cmd:
                return 0, '{"ok": false, "error": "CUDA not available"}'
            return super().__call__(cmd, env, cancel)

    inst = _installer(env, FakeServer(), Runner())
    assert inst.run() is False and "CUDA not available" in inst.done[0][1]
    assert not (env / "rt" / "current").exists()


def test_a_self_test_without_a_json_result_fails_even_on_exit_zero(env):
    for text in ("all good, no json here", "{not json at all", '{"ok": "yes"}', '{"gpu": "fake"}', ""):
        class Runner(FakeRunner):
            def __call__(self, cmd, env, cancel, _text=text):
                if "self-test" in cmd:
                    return 0, _text
                return super().__call__(cmd, env, cancel)

        inst = _installer(env, FakeServer(), Runner())
        assert inst.run() is False, text
        assert "self-test" in inst.done[-1][1].lower(), text
        assert not (env / "rt" / "current").exists()


class RefreshingServer(FakeServer):
    """The pack moves on after the first file: files of ``pack-abcdef123456`` then answer 410 (or the new bytes)."""

    def __init__(self, gone_times=1, **kw):
        super().__init__(**kw)
        self.gone_times = gone_times
        self.manifests = 0
        self.versions = []

    def manifest(self):
        m = super().manifest()
        m["pack_version"] = f"pack-{self.manifests:012d}"
        return m

    def __call__(self, request):
        path = request.url.path
        if path == "/v1/compute/pack/manifest":
            self.manifests += 1
        if path.startswith("/v1/compute/pack/files/"):
            self.versions.append((path, request.url.params.get("version")))
            if self.gone_times > 0 and self.manifests <= self.gone_times:
                return httpx.Response(410, json={"detail": "pack version gone"})
        return super().__call__(request)


def test_every_file_request_names_the_manifests_pack_version(env):
    server = RefreshingServer(gone_times=0)
    inst = _installer(env, server, FakeRunner())
    assert inst.run() is True
    assert len(server.versions) == len(CONTENT)
    assert {v for _p, v in server.versions} == {"pack-000000000001"}


def test_a_pack_that_moves_mid_install_restarts_from_a_fresh_manifest_once(env):
    server = RefreshingServer(gone_times=1)
    inst = _installer(env, server, FakeRunner())
    assert inst.run() is True, inst.done
    assert server.manifests == 2
    assert gr.installed_info(env / "rt")["pack_version"] == "pack-000000000002"
    assert [v for _p, v in server.versions][-1] == "pack-000000000002"


def test_a_pack_that_keeps_moving_fails_with_a_clear_message(env):
    server = RefreshingServer(gone_times=5)
    inst = _installer(env, server, FakeRunner())
    assert inst.run() is False
    assert server.manifests == 2
    assert "changed again" in inst.done[0][1]
    assert not (env / "rt" / "current").exists()


def test_the_runtime_is_stopped_just_before_the_swap(env):
    seen = []
    inst = _installer(env, FakeServer(), FakeRunner())
    inst.before_swap = lambda: seen.append((env / "rt" / "current").exists())
    assert inst.run() is True and seen == [False]


def test_the_old_runtime_is_replaced_and_removed_after_a_good_update(env):
    _old_install(env / "rt")
    assert _installer(env, FakeServer(), FakeRunner()).run() is True
    root = env / "rt"
    assert not (root / "current" / "marker.txt").exists()
    assert gr.installed_info(root)["pack_version"] == "pack-abcdef123456"
    assert not (root / "previous").exists()


def test_the_old_runtime_comes_back_when_the_new_one_does_not_start_after_the_swap(env):
    _old_install(env / "rt")

    class Runner(FakeRunner):
        def __call__(self, cmd, env, cancel):
            if "-I" in cmd:
                return 1, "cannot start"
            return super().__call__(cmd, env, cancel)

    inst = _installer(env, FakeServer(), Runner())
    assert inst.run() is False
    assert (env / "rt" / "current" / "marker.txt").read_text() == "old runtime"
    assert not (env / "rt" / "previous").exists()


def test_a_pip_failure_names_the_step_and_the_reason(env):
    inst = _installer(env, FakeServer(), FakeRunner(fail="install"))
    assert inst.run() is False
    assert "installing the gpu libraries failed" in inst.done[0][1].lower() and "boom" in inst.done[0][1]


def test_cancel_stops_before_anything_is_installed(env):
    server = FakeServer()
    inst = _installer(env, server, FakeRunner())
    inst.cancel()
    assert inst.run() is False
    assert inst.done[0][0] is False and "Cancelled" in inst.done[0][1]
    assert not (env / "rt" / "current").exists()


def test_cancel_during_the_download(env):
    inst = _installer(env, FakeServer(), FakeRunner())
    inst.progress.connect(lambda step, d, t: inst.cancel() if step == "Downloading" else None)
    assert inst.run() is False and "Cancelled" in inst.done[0][1]
    assert not (env / "rt" / "current").exists()


def test_a_server_without_a_pack_says_so(env):
    inst = _installer(env, FakeServer(manifest_status=404), FakeRunner())
    assert inst.run() is False
    assert "no GPU helper pack" in inst.done[0][1]


def test_an_unsafe_path_from_the_server_is_refused(env):
    class Evil(FakeServer):
        def manifest(self):
            m = super().manifest()
            m["files"][0]["path"] = "../escape.exe"
            return m

    inst = _installer(env, Evil(), FakeRunner())
    assert inst.run() is False and "unsafe" in inst.done[0][1]


def test_install_needs_windows(env, monkeypatch):
    monkeypatch.setattr(gr, "supported", lambda platform=None: False)
    inst = _installer(env, FakeServer(), FakeRunner())
    assert inst.run() is False and inst.done[0][1] == gr.UNSUPPORTED_TEXT


def test_remove_deletes_the_folder_and_forgets_the_version(env):
    _old_install(env / "rt")
    config_mod.save_config({"gpu_helper": {"key": "k", "installed_pack_version": "old-pack"}})
    gr.remove_runtime(env / "rt")
    assert not (env / "rt").exists() and gr.installed_info(env / "rt") is None
    group = config_mod.gpu_helper_settings()
    assert group["installed_pack_version"] is None and group["key"] == "k"


def test_runtime_text_and_pack_status(env):
    assert gr.runtime_text(env / "rt") == "Not installed"
    _old_install(env / "rt")
    assert gr.runtime_text(env / "rt").startswith("Installed")
    assert gr.runtime_text(env / "rt", platform="darwin") == "Not available on this computer"
    assert gr.pack_status("http://s", "t", transport=httpx.MockTransport(FakeServer())) == "pack-abcdef123456"
    assert gr.pack_status("http://s", "t", transport=httpx.MockTransport(FakeServer(manifest_status=404))) is None
    assert gr.pack_status("", "t") is None


# -- the graphics card ---------------------------------------------------------------------------------------


def test_detect_gpu_reads_nvidia_smi_output():
    out = SimpleNamespace(returncode=0, stdout="NVIDIA GeForce GTX 1650, 4096, 566.36\n")
    info = gr.detect_gpu(run=lambda *a, **k: out)
    assert info == {"name": "NVIDIA GeForce GTX 1650", "vram_mb": 4096, "driver": "566.36"}
    assert gr.gpu_text(info, platform="win32") == "NVIDIA GeForce GTX 1650 · 4 GB · driver 566.36"


def test_detect_gpu_without_a_card_or_driver():
    assert gr.detect_gpu(run=lambda *a, **k: SimpleNamespace(returncode=9, stdout="")) is None
    assert gr.detect_gpu(run=lambda *a, **k: SimpleNamespace(returncode=0, stdout="\n")) is None

    def boom(*a, **k):
        raise FileNotFoundError("nvidia-smi")

    assert gr.detect_gpu(run=boom) is None
    assert gr.gpu_text(None, platform="win32") == "No NVIDIA graphics card found"
    assert gr.gpu_text(None, platform="darwin") == gr.UNSUPPORTED_TEXT


def test_a_file_held_by_antivirus_is_retried_not_fatal(monkeypatch, tmp_path):
    from meeting_notes.client import gpu_runtime as gr

    calls = []

    def flaky_fetch(self, client, item, dest, done_before, total, version):
        calls.append(item["path"])
        if len(calls) < 3:
            raise PermissionError(32, "The process cannot access the file because it is being used by another process")

    monkeypatch.setattr(gr.GpuRuntimeInstaller, "_fetch_one", flaky_fetch)
    monkeypatch.setattr(gr.time, "sleep", lambda s: None)
    inst = gr.GpuRuntimeInstaller("http://x", "t", root=tmp_path)
    inst._download(None, [{"path": "wheels/av.whl", "size": 1, "sha256": "x"}], tmp_path / "files", "v1")
    assert calls == ["wheels/av.whl"] * 3


def test_a_file_that_stays_busy_still_fails_eventually(monkeypatch, tmp_path):
    import pytest
    from meeting_notes.client import gpu_runtime as gr

    def busy(self, *a, **k):
        raise PermissionError(32, "busy")

    clock = iter(range(0, 1000, 5))
    monkeypatch.setattr(gr.GpuRuntimeInstaller, "_fetch_one", busy)
    monkeypatch.setattr(gr.time, "sleep", lambda s: None)
    monkeypatch.setattr(gr.time, "monotonic", lambda: float(next(clock)))
    inst = gr.GpuRuntimeInstaller("http://x", "t", root=tmp_path)
    with pytest.raises(PermissionError):
        inst._download(None, [{"path": "wheels/av.whl", "size": 1, "sha256": "x"}], tmp_path / "files", "v1")
