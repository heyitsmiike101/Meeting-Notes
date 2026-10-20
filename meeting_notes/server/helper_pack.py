"""Builds the GPU helper pack: everything a Windows helper PC needs, prepared once on the server.

The server downloads from the internet (uv, a standalone CPython, wheels, models); helpers then download only
from the server (see ``helper_pack_api``). The pack lives in ``<data>/helper-pack/<pack_version>/`` with a
``current`` pointer file next to the version folders. A new pack is built in a temp folder and renamed into
place only when complete, and the pointer is switched last, so the previous pack stays served until the new
one is whole (and stays on disk afterwards as the one-back fallback).

Everything that touches the network or runs a subprocess is injected (``downloader``, ``pip_runner``,
``model_fetcher``) so the tests run offline with fakes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

logger = logging.getLogger("meeting_notes.server.helper_pack")

# -- pinned downloads --------------------------------------------------------------------------------------------
# Bump together with the SHA-256 (published next to each release asset). A mismatch aborts the build.
UV_VERSION = "0.13.3"
UV_URL = f"https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/uv-x86_64-pc-windows-msvc.zip"
UV_SHA256 = "bb701bd35b586c45966654ea2703c9bd116a6a4fc0a8ca755bd644c2c41c5ebe"

PYTHON_VERSION = "3.12.15"
PYTHON_BUILD_TAG = "20261013"
PYTHON_FILENAME = f"cpython-{PYTHON_VERSION}+{PYTHON_BUILD_TAG}-x86_64-pc-windows-msvc-install_only.tar.gz"
PYTHON_URL = (
    "https://github.com/astral-sh/python-build-standalone/releases/download/"
    f"{PYTHON_BUILD_TAG}/{PYTHON_FILENAME.replace('+', '%2B')}"
)
PYTHON_SHA256 = "bbc902d4d187ac4914915bf538196a064b49109a33a104965b5164ced5b80a1e"
PYTHON_PACK_PATH = "python/" + PYTHON_FILENAME.replace("+", "-")

REQUIREMENTS_PATH = Path(__file__).resolve().parent.parent / "compute_helper" / "requirements-windows-cuda.txt"
DEFAULT_DIARIZATION_MODEL = "pyannote/speaker-diarization-community-1"

# Package folders left out of helper-code.zip (the runtime is headless and never serves the web UI).
_CODE_EXCLUDE_DIRS = (("client", "ui"), ("server", "static"))
# Files kept from the whisper snapshot (the CTranslate2 model files faster-whisper loads).
_WHISPER_PATTERNS = ["config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*"]

STATE_IDLE = "idle"
STATE_PREPARING = "preparing"
STATE_READY = "ready"
STATE_ERROR = "error"

Downloader = Callable[[str, Path, Callable[[int, int], None]], None]
PipRunner = Callable[[List[str], Path, Callable[[], None]], None]
ModelFetcher = Callable[[str, Path, Optional[str], Optional[List[str]]], None]


class PackError(Exception):
    """A pack build step failed in a way worth showing the operator."""


# -- default (networked) implementations -------------------------------------------------------------------------


def default_downloader(url: str, dest: Path, progress: Callable[[int, int], None]) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "meeting-notes-helper-pack"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            progress(done, total)


def default_pip_runner(args: List[str], dest: Path, tick: Callable[[], None]) -> None:
    """Run ``python -m pip <args>`` with the server's own interpreter, calling ``tick`` about once a second."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "pip", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    tail: List[str] = []

    def drain() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            tail.append(line.rstrip())
            del tail[:-15]

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    while proc.poll() is None:
        tick()
        time.sleep(1.0)
    reader.join(timeout=5)
    if proc.returncode != 0:
        raise PackError("pip download failed: " + " | ".join(tail[-6:]))


def default_model_fetcher(repo_id: str, dest: Path, token: Optional[str], patterns: Optional[List[str]]) -> None:
    from huggingface_hub import snapshot_download

    snapshot_download(repo_id=repo_id, local_dir=str(dest), token=token or None, allow_patterns=patterns)


def default_token() -> Optional[str]:
    return os.environ.get("HUGGINGFACE_TOKEN") or os.environ.get("HF_TOKEN") or None


def whisper_repo(model: str) -> str:
    """The Hugging Face repo for a faster-whisper model name (same mapping faster-whisper itself uses)."""
    try:
        from faster_whisper.utils import _MODELS

        repo = _MODELS.get(model)
        if repo:
            return repo
    except Exception:  # noqa: BLE001 - fall back to treating the name as a repo id
        pass
    if "/" in model:
        return model
    raise PackError(f"unknown Whisper model {model!r}; cannot find its download")


def is_auth_error(exc: BaseException) -> bool:
    """Gated/unauthorized/not-found-without-token errors from huggingface_hub (matched by name, no hard import)."""
    for cls in type(exc).__mro__:
        if cls.__name__ in ("GatedRepoError", "RepositoryNotFoundError", "HfHubHTTPError", "LocalTokenNotFoundError"):
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if cls.__name__ != "HfHubHTTPError" or status in (401, 403, 404):
                return True
    text = str(exc)
    return "401" in text or "403" in text or "gated" in text.lower()


# -- helpers ------------------------------------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _dir_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def requirement_pins(path: Path) -> List[str]:
    """The package pins from a requirements file (option lines, comments and blanks dropped)."""
    pins = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line and not line.startswith("-"):
            pins.append(line)
    return pins


def _file_kind(rel: str) -> str:
    if rel == "uv.exe":
        return "uv"
    if rel.startswith("python/"):
        return "python"
    if rel.startswith("wheels/"):
        return "wheel"
    if rel == "helper-code.zip":
        return "code"
    if rel.startswith("models/"):
        return "model"
    return "other"


# -- the builder --------------------------------------------------------------------------------------------------



def _helper_code_fingerprint() -> str:
    """Hash of the Python code helpers run (the runtime and the transcription backends), so a server whose
    helper code changed reports its pack as out of date even within one app version."""
    import meeting_notes

    root = Path(meeting_notes.__file__).resolve().parent
    digest = hashlib.sha256()
    for sub in ("compute_helper", "transcribe"):
        for path in sorted((root / sub).rglob("*.py")):
            digest.update(str(path.relative_to(root)).encode("utf-8"))
            digest.update(path.read_bytes())
    return digest.hexdigest()[:16]

class HelperPackBuilder:
    def __init__(
        self,
        data_root: Path,
        *,
        settings_provider: Callable[[], object],
        downloader: Optional[Downloader] = None,
        pip_runner: Optional[PipRunner] = None,
        model_fetcher: Optional[ModelFetcher] = None,
        token_provider: Callable[[], Optional[str]] = default_token,
        requirements_path: Optional[Path] = None,
        code_root: Optional[Path] = None,
        app_version: Optional[str] = None,
    ) -> None:
        self.root = Path(data_root) / "helper-pack"
        self.settings_provider = settings_provider
        self.downloader = downloader or default_downloader
        self.pip_runner = pip_runner or default_pip_runner
        self.model_fetcher = model_fetcher or default_model_fetcher
        self.token_provider = token_provider
        self.requirements_path = Path(requirements_path or REQUIREMENTS_PATH)
        self.code_root = Path(code_root) if code_root else Path(__file__).resolve().parent.parent
        if app_version is None:
            from .. import __version__ as app_version
        self.app_version = app_version
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._status: Dict[str, object] = {
            "state": STATE_IDLE, "step": "", "bytes_done": 0, "bytes_total": 0, "error": None, "note": None,
        }
        self._base = 0  # bytes finished in earlier steps

    # -- reading the current pack --

    def current_version(self) -> Optional[str]:
        try:
            value = (self.root / "current").read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return value if value and (self.root / value / "manifest.json").is_file() else None

    def current_dir(self) -> Optional[Path]:
        version = self.current_version()
        return self.root / version if version else None

    def manifest(self) -> Optional[dict]:
        pack = self.current_dir()
        if pack is None:
            return None
        try:
            return json.loads((pack / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def version_dir(self, version: str) -> Optional[Path]:
        """The folder of a pack version that is still on disk (the current and previous ones), or None."""
        if not version or version != Path(version).name or version.startswith("."):
            return None
        pack = self.root / version
        return pack if (pack / "manifest.json").is_file() else None

    def file_path(self, rel: str, version: Optional[str] = None) -> Optional[Path]:
        """Path of a file listed in a pack's manifest (the current pack unless ``version`` is given), or None
        (unlisted, traversal, missing, or that version is gone)."""
        pack = self.version_dir(version) if version else self.current_dir()
        if pack is None:
            return None
        try:
            manifest = json.loads((pack / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not any(f.get("path") == rel for f in manifest.get("files", [])):
            return None
        target = (pack / rel).resolve()
        try:
            target.relative_to(pack.resolve())
        except ValueError:
            return None
        return target if target.is_file() else None

    # -- versioning --

    def _plan(self) -> dict:
        s = self.settings_provider()
        whisper = (getattr(s, "model", "") or "").strip()
        diar = (getattr(s, "diarization_model", "") or DEFAULT_DIARIZATION_MODEL).strip()
        return {
            "pins": requirement_pins(self.requirements_path),
            "whisper": whisper,
            "diarization": diar,
            "has_token": bool(self.token_provider()),
            "app_version": self.app_version,
            "code": _helper_code_fingerprint(),
            "uv": [UV_VERSION, UV_SHA256],
            "python": [PYTHON_FILENAME, PYTHON_SHA256],
        }

    def planned_version(self) -> str:
        blob = json.dumps(self._plan(), sort_keys=True).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()[:12]

    def is_out_of_date(self) -> bool:
        cur = self.current_version()
        return cur is not None and cur != self.planned_version()

    # -- status --

    def status(self) -> dict:
        with self._lock:
            out = dict(self._status)
        out["pack_version"] = self.current_version()
        try:
            out["out_of_date"] = self.is_out_of_date()
        except Exception:  # noqa: BLE001 - status must never raise
            out["out_of_date"] = False
        manifest = self.manifest()
        out["total_size"] = manifest.get("total_size") if manifest else None
        out["has_pack"] = manifest is not None
        if out["state"] == STATE_IDLE and manifest is not None:
            out["state"] = STATE_READY
        return out

    def _set(self, **kw) -> None:
        with self._lock:
            self._status.update(kw)

    def _progress(self, done: int, total: int) -> None:
        self._set(bytes_done=self._base + done, bytes_total=0 if not total else self._base + total)

    # -- running --

    def start(self) -> bool:
        """Begin a build in the background. False if one is already running."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._status.update(state=STATE_PREPARING, step="starting", bytes_done=0, bytes_total=0,
                                error=None, note=None)
            self._base = 0
            self._thread = threading.Thread(target=self._run, name="helper-pack", daemon=True)
            self._thread.start()
            return True

    def wait(self, timeout: Optional[float] = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _run(self) -> None:
        try:
            self.build()
        except Exception as exc:  # noqa: BLE001 - surfaced through status
            logger.exception("helper pack build failed")
            self._set(state=STATE_ERROR, error=str(exc) or type(exc).__name__)
        else:
            self._set(state=STATE_READY, step="done")

    def build(self) -> str:
        """Build synchronously; returns the new pack version. Leaves the previous pack untouched on failure."""
        plan = self._plan()
        if not plan["whisper"]:
            raise PackError("no Whisper model is configured on the server")
        if not plan["pins"]:
            raise PackError("requirements file has no pins")
        version = self.planned_version()
        self.root.mkdir(parents=True, exist_ok=True)
        final = self.root / version
        if (final / "manifest.json").is_file():
            self._write_current(version)
            return version
        tmp = Path(tempfile.mkdtemp(prefix=f".build-{version}-", dir=self.root))
        try:
            manifest = self._build_into(tmp, plan, version)
            (tmp / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            if final.exists():
                shutil.rmtree(final, ignore_errors=True)
            os.replace(tmp, final)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        previous = self.current_version()
        self._write_current(version)
        self._prune(keep={version, previous})
        return version

    def _write_current(self, version: str) -> None:
        tmp = self.root / "current.tmp"
        tmp.write_text(version, encoding="utf-8")
        os.replace(tmp, self.root / "current")

    def _prune(self, keep: set) -> None:
        for child in self.root.iterdir():
            if child.is_dir() and child.name not in keep and not child.name.startswith(".build-"):
                shutil.rmtree(child, ignore_errors=True)
            elif child.is_dir() and child.name.startswith(".build-"):
                shutil.rmtree(child, ignore_errors=True)  # leftovers of a crashed build

    def _download_verified(self, url: str, dest: Path, sha256: str, what: str) -> None:
        self.downloader(url, dest, self._progress)
        got = sha256_file(dest)
        if got != sha256:
            dest.unlink(missing_ok=True)
            raise PackError(f"{what} failed its SHA-256 check (expected {sha256[:12]}..., got {got[:12]}...)")
        self._base = int(self._status["bytes_done"])

    def _build_into(self, pack: Path, plan: dict, version: str) -> dict:
        # uv
        self._set(step="Downloading uv")
        zpath = pack / "uv.zip"
        self._download_verified(UV_URL, zpath, UV_SHA256, "uv download")
        with zipfile.ZipFile(zpath) as zf:
            names = [n for n in zf.namelist() if n.split("/")[-1].lower() == "uv.exe"]
            if not names:
                raise PackError("uv download did not contain uv.exe")
            with zf.open(names[0]) as src, open(pack / "uv.exe", "wb") as dst:
                shutil.copyfileobj(src, dst)
        zpath.unlink()

        # python
        self._set(step="Downloading Python")
        self._download_verified(PYTHON_URL, pack / PYTHON_PACK_PATH, PYTHON_SHA256, "Python download")

        # wheels
        self._set(step="Downloading Python packages")
        wheels = pack / "wheels"
        wheels.mkdir()
        base = self._base

        def tick() -> None:
            self._set(bytes_done=base + _dir_size(wheels))

        self.pip_runner(
            ["download", "--only-binary=:all:", "--platform", "win_amd64", "--python-version", "3.12",
             "--implementation", "cp", "--no-input", "--disable-pip-version-check",
             "--dest", str(wheels), "-r", str(self.requirements_path)],
            wheels, tick,
        )
        if not any(wheels.iterdir()):
            raise PackError("pip download produced no wheels")
        self._base = base + _dir_size(wheels)
        self._set(bytes_done=self._base)
        # requirements.txt for the offline install on the helper: pins only (index options would fight --no-index)
        (pack / "requirements.txt").write_text("\n".join(plan["pins"]) + "\n", encoding="utf-8")

        # helper code
        self._set(step="Packaging helper code")
        self._zip_code(pack / "helper-code.zip")

        # models
        self._set(step=f"Downloading Whisper model {plan['whisper']}")
        wdir = pack / "models" / "whisper" / plan["whisper"]
        wdir.mkdir(parents=True)
        self.model_fetcher(whisper_repo(plan["whisper"]), wdir, self.token_provider(), _WHISPER_PATTERNS)
        if not (wdir / "model.bin").is_file():
            raise PackError(f"Whisper model {plan['whisper']!r} download is missing model.bin")
        self._base += _dir_size(wdir)
        self._set(bytes_done=self._base)

        diar_name: Optional[str] = None
        note: Optional[str] = None
        token = self.token_provider()
        if not token:
            note = "Speaker labels are unavailable on helpers: the server has no HUGGINGFACE_TOKEN."
        else:
            self._set(step="Downloading diarization model")
            ddir = pack / "models" / "diarization"
            ddir.mkdir(parents=True)
            try:
                self.model_fetcher(plan["diarization"], ddir, token, None)
                diar_name = plan["diarization"]
            except Exception as exc:  # noqa: BLE001
                if not is_auth_error(exc):
                    raise
                shutil.rmtree(ddir, ignore_errors=True)
                note = (f"Speaker labels are unavailable on helpers: Hugging Face refused access to "
                        f"{plan['diarization']} (accept its terms and check the token).")
        self._set(note=note)

        # manifest
        self._set(step="Hashing files")
        for cache in list(pack.rglob(".cache")):  # huggingface local_dir bookkeeping
            shutil.rmtree(cache, ignore_errors=True)
        files = []
        total = 0
        for p in sorted(pack.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(pack).as_posix()
            if rel == "manifest.json":
                continue
            size = p.stat().st_size
            total += size
            files.append({"path": rel, "size": size, "sha256": sha256_file(p), "kind": _file_kind(rel)})
        return {
            "pack_version": version,
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "python": PYTHON_VERSION,
            "requirements": plan["pins"],
            "models": {"whisper": plan["whisper"], "diarization": diar_name},
            "note": note,
            "files": files,
            "total_size": total,
        }

    def _zip_code(self, dest: Path) -> None:
        pkg = self.code_root
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in sorted(pkg.rglob("*")):
                if not p.is_file():
                    continue
                rel = p.relative_to(pkg)
                parts = rel.parts
                if "__pycache__" in parts or any(parts[: len(ex)] == ex for ex in _CODE_EXCLUDE_DIRS):
                    continue
                if p.suffix == ".pyc" or rel.name == "requirements-windows-cuda.txt":
                    continue
                if p.suffix != ".py" and parts[0] not in ("compute_helper", "bridge"):
                    continue  # package data other than the helper's own and the packaged prompts is not needed
                info = zipfile.ZipInfo("meeting_notes/" + rel.as_posix(), date_time=(2020, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                zf.writestr(info, p.read_bytes())
