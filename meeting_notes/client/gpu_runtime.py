"""The GPU helper's runtime on this computer: where it lives, what graphics card is here, and the installer.

The runtime (Python, CUDA libraries, models, helper code) is a "helper pack" the server prepares and this app
downloads from the server only (``docs/gpu-helper-design.md``). Everything lives under the user's profile
(``%LOCALAPPDATA%\\MeetingNotes-GPU``, outside the app folder so client updates keep it): nothing here needs
administrator rights.

Layout::

    MeetingNotes-GPU/
      current/            the installed runtime (venv/, python/, models/, helper-code.zip, pack.json)
      previous/           only while an update is swapping in
      staging/
        files/            the pack as downloaded (kept between attempts so a download resumes)
        build/            what becomes ``current`` once its self-test passes

:class:`GpuRuntimeInstaller` runs register (if no key) -> manifest -> download (Range resume, SHA-256) -> extract
Python -> ``uv venv`` + ``uv pip install --no-index`` -> self-test -> swap into ``current``, and reports each step.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx
from PySide6.QtCore import QObject, Signal

from meeting_notes import config as config_mod
from meeting_notes.client.api import ServerClient, ServerUnavailable

log = logging.getLogger("meeting_notes.client.gpu_runtime")

UNSUPPORTED_TEXT = "GPU helper is available on Windows PCs with an NVIDIA graphics card."
NO_GPU_TEXT = "No NVIDIA graphics card found"
STEPS = ("Registering", "Downloading", "Unpacking", "Installing", "Checking", "Finishing")
_CHUNK = 1 << 20
_PROGRESS_GAP = 0.15  # seconds between progress signals


class InstallError(Exception):
    """The install failed; the message is written for the person looking at Settings."""


class InstallCancelled(Exception):
    """The person pressed Cancel."""


class PackGone(Exception):
    """The server no longer has the pack version the manifest listed (it refreshed its pack mid-install)."""


def supported(platform: Optional[str] = None) -> bool:
    return (sys.platform if platform is None else platform) == "win32"


# -- where things are -------------------------------------------------------------------------------------------


def runtime_root() -> Path:
    """``%LOCALAPPDATA%\\MeetingNotes-GPU`` (``MEETING_NOTES_GPU_RUNTIME`` overrides it, for tests).

    Deliberately a sibling of the app's ``%LOCALAPPDATA%\\MeetingNotes`` folder, not inside it: a client update
    replaces that whole folder, and the GPU runtime (several GB) must survive updates.
    """
    override = os.environ.get("MEETING_NOTES_GPU_RUNTIME")
    if override:
        return Path(override)
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / "AppData" / "Local"
    return base / "MeetingNotes-GPU"


def current_dir(root: Optional[Path] = None) -> Path:
    return (root or runtime_root()) / "current"


def venv_python(base: Path) -> Path:
    """The helper venv's interpreter inside ``base`` (``current`` or the build folder)."""
    if sys.platform == "win32":
        return base / "venv" / "Scripts" / "python.exe"
    return base / "venv" / "bin" / "python"


def helper_code(base: Path) -> Path:
    return base / "helper-code.zip"


def models_dir(base: Path) -> Path:
    return base / "models"


def installed_info(root: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """``pack.json`` of the installed runtime, or None when none is installed (or it is damaged)."""
    base = current_dir(root)
    try:
        info = json.loads((base / "pack.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(info, dict) or not venv_python(base).exists() or not helper_code(base).exists():
        return None
    return info


def is_installed(root: Optional[Path] = None) -> bool:
    return installed_info(root) is not None


def runtime_text(root: Optional[Path] = None, platform: Optional[str] = None) -> str:
    """The runtime line of Settings."""
    if not supported(platform):
        return "Not available on this computer"
    info = installed_info(root)
    if info is None:
        return "Not installed"
    version = str(info.get("pack_version") or "")
    return f"Installed (pack {version[:12]})" if version else "Installed"


def runtime_env(base: Path) -> Dict[str, str]:
    """The environment for the runtime process: helper code on ``PYTHONPATH``, nothing else of ours."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(helper_code(base))
    env["PYTHONUTF8"] = "1"
    env.pop("PYTHONHOME", None)
    return env


# -- the graphics card ------------------------------------------------------------------------------------------


def _hidden_window_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def detect_gpu(timeout: float = 5.0, run: Optional[Callable[..., Any]] = None) -> Optional[Dict[str, Any]]:
    """``{name, vram_mb, driver}`` of the first NVIDIA card from ``nvidia-smi``, or None (no card, no driver, slow)."""
    run = run or subprocess.run
    if run is subprocess.run and shutil.which("nvidia-smi") is None:
        return None
    try:
        done = run(
            ["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=timeout, creationflags=_hidden_window_flags(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(done, "returncode", 1) != 0:
        return None
    for line in str(getattr(done, "stdout", "") or "").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 3 and parts[0]:
            try:
                vram = int(float(parts[1]))
            except ValueError:
                vram = 0
            return {"name": parts[0], "vram_mb": vram, "driver": parts[2]}
    return None


def gpu_text(info: Optional[Dict[str, Any]], platform: Optional[str] = None) -> str:
    """``NVIDIA GeForce GTX 1650 · 4 GB · driver 566.36``, or that no card was found."""
    if not supported(platform):
        return UNSUPPORTED_TEXT
    if not info:
        return NO_GPU_TEXT
    parts = [str(info.get("name") or "NVIDIA graphics card")]
    vram = int(info.get("vram_mb") or 0)
    if vram > 0:
        parts.append(f"{max(1, round(vram / 1024))} GB")
    if info.get("driver"):
        parts.append(f"driver {info['driver']}")
    return " · ".join(parts)


_gpu_cache: Dict[str, Any] = {"at": 0.0, "info": None, "done": False}


def cached_gpu(refresh: bool = False, max_age: float = 60.0) -> Tuple[Optional[Dict[str, Any]], bool]:
    """``(info, known)``: the last detection and whether one has run. Runs ``nvidia-smi`` (slow) only when
    ``refresh`` is set or the cache is older than ``max_age``, so call it from a worker thread."""
    if refresh or not _gpu_cache["done"] or time.monotonic() - _gpu_cache["at"] > max_age:
        _gpu_cache.update(info=detect_gpu(), at=time.monotonic(), done=True)
    return _gpu_cache["info"], _gpu_cache["done"]


def cached_gpu_text() -> str:
    """The graphics-card line from the cache only (never starts a process)."""
    if not supported():
        return UNSUPPORTED_TEXT
    return gpu_text(_gpu_cache["info"]) if _gpu_cache["done"] else "Checking..."


# -- the installer ----------------------------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(_CHUNK)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _safe_rel(path: str) -> Path:
    """A manifest path as a relative path that stays inside its folder."""
    rel = Path(path.replace("\\", "/"))
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        raise InstallError(f"The server sent an unsafe file path: {path}")
    return rel


def _rmtree(path: Path) -> None:
    """Remove a folder, retrying briefly (antivirus and indexers hold files open on Windows)."""
    for attempt in range(5):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            if attempt == 4:
                raise
            time.sleep(0.4)


def default_runner(cmd: List[str], env: Dict[str, str], cancel: threading.Event, timeout: float = 1800.0):
    """Run ``cmd`` and return ``(returncode, output)``; kills it when ``cancel`` is set or ``timeout`` passes."""
    proc = subprocess.Popen(
        cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
        errors="replace", creationflags=_hidden_window_flags(),
    )
    out: List[str] = []
    reader = threading.Thread(target=lambda: out.extend(proc.stdout), daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    while proc.poll() is None:
        if cancel.is_set() or time.monotonic() > deadline:
            proc.kill()
            break
        time.sleep(0.2)
    proc.wait()
    reader.join(2)
    if cancel.is_set():
        raise InstallCancelled()
    return proc.returncode, "".join(out)


class GpuRuntimeInstaller(QObject):
    """Installs or updates the runtime on a worker thread (:meth:`start`), or inline (:meth:`run`, for tests).

    Signals (delivered on the thread that owns this object): ``progress(step, done_bytes, total_bytes)`` and
    ``finished(ok, message)``. Cancel with :meth:`cancel`; nothing already installed is touched until the
    new runtime has passed its self-test.
    """

    progress = Signal(str, object, object)
    finished = Signal(bool, str)

    def __init__(
        self,
        server_url: str,
        token: str,
        *,
        root: Optional[Path] = None,
        transport: Optional[httpx.BaseTransport] = None,
        runner: Optional[Callable[..., Tuple[int, str]]] = None,
        device_name: Optional[str] = None,
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)
        self.server_url = (server_url or "").strip().rstrip("/")
        self.token = token or ""
        self.root = Path(root) if root is not None else runtime_root()
        self._transport = transport
        self._runner = runner or default_runner
        self._device = device_name
        self._cancel = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_emit = 0.0
        self._total = 0
        self._done_before = 0
        # Called (on the install thread) just before ``current`` is replaced, so the owner can stop the running runtime.
        self.before_swap: Optional[Callable[[], None]] = None

    # -- control ---------------------------------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._cancel.clear()
        self._thread = threading.Thread(target=self.run, daemon=True, name="gpu-runtime-install")
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()

    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def join(self, timeout: Optional[float] = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    # -- the run ---------------------------------------------------------------------------------------------

    def run(self) -> bool:
        """Do the whole install; emits ``finished`` and returns whether it worked. Never raises."""
        try:
            version = self._install()
        except InstallCancelled:
            log.info("GPU runtime install cancelled")
            self.finished.emit(False, "Cancelled. Anything already installed was left as it was.")
            return False
        except InstallError as exc:
            log.warning("GPU runtime install failed: %s", exc)
            self.finished.emit(False, str(exc))
            return False
        except ServerUnavailable as exc:
            log.warning("GPU runtime install: server unreachable: %s", exc)
            self.finished.emit(False, "Could not reach the server. Check the connection and try again.")
            return False
        except Exception as exc:  # noqa: BLE001
            log.exception("GPU runtime install crashed")
            self.finished.emit(False, f"Install failed: {type(exc).__name__}: {exc}")
            return False
        self.finished.emit(True, f"GPU support installed (pack {version[:12]})")
        return True

    def _check_cancel(self) -> None:
        if self._cancel.is_set():
            raise InstallCancelled()

    def _emit(self, step: str, done: int = 0, total: int = 0, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_emit < _PROGRESS_GAP:
            return
        self._last_emit = now
        self.progress.emit(step, int(done), int(total))

    def _client(self) -> ServerClient:
        if not self.server_url:
            raise InstallError("Set the server address in Settings first.")
        client = ServerClient(self.server_url, self.token or None, timeout=30.0)
        if self._transport is not None:
            client._client.close()
            client._client = httpx.Client(
                base_url=client.base_url, timeout=30.0, headers=client._client.headers, transport=self._transport
            )
        return client

    def _install(self) -> str:
        if not supported():
            raise InstallError(UNSUPPORTED_TEXT)
        self._check_cancel()
        staging = self.root / "staging"
        files_dir = staging / "files"
        build = staging / "build"
        with self._client() as client:
            self._emit("Registering", force=True)
            self._register(client)
            for attempt in (1, 2):
                self._check_cancel()
                manifest = self._manifest(client)
                version = str(manifest["pack_version"])
                files = self._file_list(manifest)
                self._prepare_staging(staging, files_dir, version)
                try:
                    self._download(client, files, files_dir, version)
                    break
                except PackGone:
                    # The server built a newer pack while we were downloading: start over from a fresh manifest, once.
                    if attempt == 2:
                        raise InstallError(
                            "The server's GPU pack changed again while downloading. Try again in a minute."
                        ) from None
                    log.info("GPU runtime install: pack %s is gone, restarting from a fresh manifest", version)
        self._check_cancel()
        if build.exists():
            _rmtree(build)
        build.mkdir(parents=True)
        self._emit("Unpacking", force=True)
        python_exe = self._extract_python(files, files_dir, build)
        self._check_cancel()
        self._emit("Installing", force=True)
        self._make_venv(files, files_dir, build, python_exe, manifest)
        self._place_payload(files, files_dir, build)
        (build / "pack.json").write_text(
            json.dumps({"pack_version": version, "created": manifest.get("created")}), encoding="utf-8"
        )
        self._check_cancel()
        self._emit("Checking", force=True)
        self._self_test(build)
        self._check_cancel()
        self._emit("Finishing", force=True)
        if self.before_swap is not None:
            self.before_swap()
        self._check_cancel()
        self._swap(staging, build)
        self._save_version(version)
        return version

    # -- registration ----------------------------------------------------------------------------------------

    def _register(self, client: ServerClient) -> None:
        settings = config_mod.gpu_helper_settings()
        if settings["key"] and settings["helper_id"]:
            return
        instance_id = settings.get("instance_id") or uuid.uuid4().hex
        import socket

        name = self._device or socket.gethostname() or "Windows PC"
        try:
            reply = client.register_compute_helper(name, instance_id)
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code in (401, 403):
                raise InstallError("The server refused the password. Check it on the Server page.") from exc
            if code == 404:
                raise InstallError("This server does not support GPU helpers. Update the server first.") from exc
            raise InstallError(f"The server could not register this computer (HTTP {code}).") from exc
        key, helper_id = reply.get("key"), reply.get("helper_id")
        if not isinstance(key, str) or not key or not isinstance(helper_id, str) or not helper_id:
            raise InstallError("The server's answer to registering this computer was not understood.")
        self._update_config(helper_id=helper_id, key=key, instance_id=instance_id)

    @staticmethod
    def _update_config(**fields: Any) -> None:
        data = config_mod.load_config()
        group = dict(data.get("gpu_helper") if isinstance(data.get("gpu_helper"), dict) else {})
        for name, default in config_mod.GPU_HELPER_DEFAULTS.items():
            group.setdefault(name, default)
        group.update(fields)
        data["gpu_helper"] = group
        config_mod.save_config(data)

    def _save_version(self, version: str) -> None:
        self._update_config(installed_pack_version=version)

    # -- manifest and download -------------------------------------------------------------------------------

    def _manifest(self, client: ServerClient) -> Dict[str, Any]:
        try:
            manifest = client.compute_pack_manifest()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise InstallError(
                    "The server has no GPU helper pack yet. Prepare it in the server's Settings, then try again."
                ) from exc
            raise InstallError(f"The server could not send the pack list (HTTP {exc.response.status_code}).") from exc
        if not isinstance(manifest, dict) or not manifest.get("pack_version") or not isinstance(manifest.get("files"), list):
            raise InstallError("The server's pack list was not understood.")
        return manifest

    @staticmethod
    def _file_list(manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
        out = []
        for item in manifest["files"]:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                raise InstallError("The server's pack list was not understood.")
            _safe_rel(item["path"])
            try:
                size = int(item["size"])
            except (KeyError, TypeError, ValueError):
                raise InstallError("The server's pack list was not understood.") from None
            digest = str(item.get("sha256") or "").lower()
            if len(digest) != 64:
                raise InstallError("The server's pack list has a file without a checksum.")
            out.append({"path": item["path"], "size": size, "sha256": digest, "kind": str(item.get("kind") or "")})
        return out

    def _prepare_staging(self, staging: Path, files_dir: Path, version: str) -> None:
        """Keep partial downloads of the same pack; start clean when the pack changed."""
        marker = staging / "pack-version"
        try:
            same = marker.read_text(encoding="utf-8").strip() == version
        except OSError:
            same = False
        if not same and staging.exists():
            _rmtree(staging)
        files_dir.mkdir(parents=True, exist_ok=True)
        marker.write_text(version, encoding="utf-8")

    def _download(self, client: ServerClient, files: List[Dict[str, Any]], files_dir: Path, version: str) -> None:
        total = sum(f["size"] for f in files)
        self._total = total
        done_before = 0
        for item in files:
            self._check_cancel()
            dest = files_dir / _safe_rel(item["path"])
            dest.parent.mkdir(parents=True, exist_ok=True)
            self._fetch_one(client, item, dest, done_before, total, version)
            done_before += item["size"]
        self._emit("Downloading", total, total, force=True)

    def _fetch_one(
        self, client: ServerClient, item: Dict[str, Any], dest: Path, done_before: int, total: int, version: str
    ) -> None:
        size, want = item["size"], item["sha256"]
        have = dest.stat().st_size if dest.exists() else 0
        if have == size and _sha256_file(dest) == want:
            self._emit("Downloading", done_before + size, total)
            return
        if have > size:
            dest.unlink()
            have = 0
        try:
            self._stream_into(client, item["path"], dest, have, size, done_before, total, version)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 416 and have:  # the partial file is not a prefix of this one
                dest.unlink(missing_ok=True)
                self._stream_into(client, item["path"], dest, 0, size, done_before, total, version)
            else:
                raise InstallError(
                    f"The server could not send {item['path']} (HTTP {exc.response.status_code})."
                ) from exc
        except httpx.TransportError as exc:
            raise ServerUnavailable(str(exc)) from exc
        if dest.stat().st_size != size or _sha256_file(dest) != want:
            dest.unlink(missing_ok=True)
            raise InstallError(f"{item['path']} did not download correctly (checksum mismatch). Try again.")

    def _stream_into(
        self, client: ServerClient, path: str, dest: Path, start: int, size: int, done_before: int, total: int,
        version: str,
    ) -> None:
        with client.compute_pack_file(path, start, version) as resp:
            if resp.status_code == 410:
                resp.read()
                raise PackGone(version)
            if resp.status_code == 416:
                resp.read()
                raise httpx.HTTPStatusError("range not satisfiable", request=resp.request, response=resp)
            if resp.status_code >= 400:
                resp.read()
                raise httpx.HTTPStatusError("download failed", request=resp.request, response=resp)
            if start > 0 and resp.status_code == 206:
                mode, got = "ab", start
            else:  # 200: the server sent the whole file (or this was not a resume)
                mode, got = "wb", 0
            with open(dest, mode) as out:
                for chunk in resp.iter_bytes(_CHUNK):
                    self._check_cancel()
                    out.write(chunk)
                    got += len(chunk)
                    self._emit("Downloading", done_before + min(got, size), total)

    # -- building the runtime --------------------------------------------------------------------------------

    @staticmethod
    def _find(files: List[Dict[str, Any]], kind: str, test: Callable[[str], bool]) -> Optional[Dict[str, Any]]:
        for item in files:
            if item["kind"] == kind or (not item["kind"] and test(item["path"].lower())):
                return item
        for item in files:
            if test(item["path"].lower()):
                return item
        return None

    def _extract_python(self, files: List[Dict[str, Any]], files_dir: Path, build: Path) -> Path:
        archive = self._find(files, "python", lambda p: p.endswith((".tar.gz", ".tgz")))
        if archive is None:
            raise InstallError("The pack has no Python.")
        target = build / "python"
        target.mkdir(parents=True, exist_ok=True)
        try:
            with tarfile.open(files_dir / _safe_rel(archive["path"]), "r:gz") as tar:
                if hasattr(tarfile, "data_filter"):
                    tar.extractall(target, filter="data")
                else:  # older Python: refuse anything that would land outside the folder
                    root = target.resolve()
                    for member in tar.getmembers():
                        if not (target / member.name).resolve().is_relative_to(root):
                            raise InstallError("The pack's Python archive is not safe to unpack.")
                    tar.extractall(target)
        except (tarfile.TarError, OSError) as exc:
            raise InstallError(f"Could not unpack Python: {exc}") from exc
        name = "python.exe" if sys.platform == "win32" else "python3"
        for candidate in sorted(target.rglob(name), key=lambda p: len(p.parts)):
            if candidate.is_file():
                return candidate
        raise InstallError("The pack's Python archive has no python executable.")

    def _requirements_file(self, manifest: Dict[str, Any], build: Path) -> Path:
        req = manifest.get("requirements")
        if isinstance(req, list):
            text = "\n".join(str(line) for line in req)
        elif isinstance(req, str):
            text = req
        else:
            raise InstallError("The pack list has no requirements.")
        path = build / "requirements.txt"
        path.write_text(text.rstrip() + "\n", encoding="utf-8")
        return path

    def _run(self, cmd: List[str], what: str, env: Optional[Dict[str, str]] = None) -> str:
        log.info("gpu runtime: %s", what)
        try:
            code, output = self._runner(cmd, env if env is not None else dict(os.environ), self._cancel)
        except OSError as exc:
            raise InstallError(f"Could not run {what}: {exc}") from exc
        if code != 0:
            tail = " ".join(str(output).strip().splitlines()[-3:])
            raise InstallError(f"{what.capitalize()} failed: {tail[:400] or 'exit code ' + str(code)}")
        return str(output)

    def _make_venv(
        self, files: List[Dict[str, Any]], files_dir: Path, build: Path, python_exe: Path, manifest: Dict[str, Any]
    ) -> None:
        uv_item = self._find(files, "uv", lambda p: p.rsplit("/", 1)[-1].startswith("uv") and p.endswith(".exe"))
        if uv_item is None:
            raise InstallError("The pack has no uv.")
        uv = str(files_dir / _safe_rel(uv_item["path"]))
        venv = build / "venv"
        env = dict(os.environ)
        env.update(UV_PYTHON_DOWNLOADS="never", UV_CACHE_DIR=str(self.root / "staging" / "uv-cache"), UV_NO_CONFIG="1")
        self._run([uv, "venv", "--python", str(python_exe), str(venv)], "creating the Python environment", env)
        self._check_cancel()
        wheels = files_dir / "wheels"
        requirements = self._requirements_file(manifest, build)
        self._run(
            [uv, "pip", "install", "--python", str(venv_python(build)), "--no-index", "--find-links", str(wheels),
             "-r", str(requirements)],
            "installing the GPU libraries", env,
        )

    def _place_payload(self, files: List[Dict[str, Any]], files_dir: Path, build: Path) -> None:
        code = self._find(files, "code", lambda p: p.endswith("helper-code.zip"))
        if code is None:
            raise InstallError("The pack has no helper code.")
        shutil.copy2(files_dir / _safe_rel(code["path"]), helper_code(build))
        source = files_dir / "models"
        target = models_dir(build)
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            target.mkdir()

    def _self_test(self, build: Path) -> None:
        cmd = [str(venv_python(build)), "-m", "meeting_notes.compute_helper", "self-test", "--models", str(models_dir(build))]
        output = self._run(cmd, "the GPU self-test", runtime_env(build))
        result = None
        for line in reversed(output.strip().splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    result = json.loads(line)
                except ValueError:
                    result = None
                break
        if not isinstance(result, dict):
            raise InstallError("The GPU self-test gave no result. Try again, or check that the graphics driver is installed.")
        if result.get("ok") is not True:
            why = result.get("error") or result.get("message") or "the graphics card could not be used"
            raise InstallError(f"The GPU self-test failed: {why}")

    # -- swapping it in --------------------------------------------------------------------------------------

    def _swap(self, staging: Path, build: Path) -> None:
        """Move ``build`` to ``current``; the old ``current`` waits as ``previous`` until the new one starts."""
        current, previous = current_dir(self.root), self.root / "previous"
        if previous.exists():
            _rmtree(previous)
        had_old = current.exists()
        if had_old:
            os.replace(current, previous)
        try:
            os.replace(build, current)
            self._relocate_venv(build, current)
            code, output = self._runner(
                [str(venv_python(current)), "-I", "-c", "import sys; print(sys.version_info[0])"],
                runtime_env(current), threading.Event(),
            )
            if code != 0:
                raise InstallError(f"The new runtime does not start: {str(output).strip()[:300]}")
        except Exception:
            if current.exists():
                _rmtree(current)
            if had_old:
                os.replace(previous, current)
            raise
        if had_old:
            _rmtree(previous)
        try:
            _rmtree(staging)
        except OSError:
            log.debug("could not remove the staging folder", exc_info=True)

    @staticmethod
    def _relocate_venv(old: Path, new: Path) -> None:
        """A venv remembers where its Python is; point ``pyvenv.cfg`` at the folder it was moved to."""
        cfg = new / "venv" / "pyvenv.cfg"
        try:
            text = cfg.read_text(encoding="utf-8")
        except OSError:
            return
        cfg.write_text(text.replace(str(old), str(new)).replace(str(old).replace("\\", "/"), str(new).replace("\\", "/")),
                       encoding="utf-8")


def remove_runtime(root: Optional[Path] = None) -> None:
    """Delete the whole runtime folder (``MeetingNotes-GPU``) and forget the installed pack version. The caller stops the
    helper first (a running runtime keeps its files open)."""
    root = root or runtime_root()
    if root.exists():
        _rmtree(root)
    data = config_mod.load_config()
    group = data.get("gpu_helper")
    if isinstance(group, dict) and group.get("installed_pack_version"):
        group["installed_pack_version"] = None
        data["gpu_helper"] = group
        config_mod.save_config(data)


def pack_status(server_url: str, token: str, *, transport: Optional[httpx.BaseTransport] = None) -> Optional[str]:
    """The pack version the server offers now, or None (no pack, or the server cannot be reached)."""
    if not (server_url or "").strip():
        return None
    try:
        client = ServerClient(server_url, token or None, timeout=8.0)
        if transport is not None:
            client._client.close()
            client._client = httpx.Client(
                base_url=client.base_url, timeout=8.0, headers=client._client.headers, transport=transport
            )
        with client:
            version = client.compute_pack_manifest().get("pack_version")
    except Exception:  # noqa: BLE001 - a nicety for the button label
        return None
    return str(version) if version else None
