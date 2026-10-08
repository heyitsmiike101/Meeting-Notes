# COMPAT FIXTURE - do not edit. Verbatim copy of meeting_notes/client/update.py from the 0.7.12 client
# (release/0.7.12), with only the meeting_notes.* imports rewritten to be package-relative.
"""Safe, server-hosted updates for the desktop client.

The update channel is deliberately small and boring: the configured server
publishes a JSON manifest, and the client downloads the exact artifact named by
that manifest.  An artifact is accepted only when both its byte count and
SHA-256 digest match.  The caller decides when to launch the installer; in
particular, the UI never launches it while a recording is active.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Tuple
from urllib.parse import urljoin, urlparse

import httpx

from . import __version__
from . import wire
from . import identity, version_gate


class UpdateError(RuntimeError):
    """The update manifest or downloaded artifact was not usable."""


@dataclass(frozen=True, order=True)
class SemanticVersion:
    """A small SemVer 2.0 comparator (build metadata does not affect order)."""

    major: int
    minor: int
    patch: int
    prerelease: Tuple[object, ...] = ()

    def __str__(self) -> str:
        suffix = ""
        if self.prerelease:
            suffix = "-" + ".".join(str(part) for part in self.prerelease)
        return f"{self.major}.{self.minor}.{self.patch}{suffix}"


_VERSION_RE = re.compile(
    r"^\s*[vV]?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?\s*$"
)


def parse_version(value: str) -> SemanticVersion:
    """Parse a SemVer string, accepting the common leading ``v`` prefix."""
    if not isinstance(value, str):
        raise ValueError("version must be a string")
    match = _VERSION_RE.match(value)
    if not match:
        raise ValueError(f"invalid semantic version: {value!r}")
    identifiers = []
    for identifier in (match.group(4) or "").split("."):
        if not identifier:
            continue
        if identifier.isdigit():
            # SemVer forbids numeric identifiers with leading zeroes.
            if len(identifier) > 1 and identifier.startswith("0"):
                raise ValueError(f"invalid semantic version: {value!r}")
            identifiers.append(int(identifier))
        else:
            identifiers.append(identifier)
    return SemanticVersion(int(match.group(1)), int(match.group(2)), int(match.group(3)), tuple(identifiers))


def is_newer_version(candidate: str, current: str = __version__) -> bool:
    """Return whether *candidate* is newer than *current*.

    Invalid remote versions are rejected rather than accidentally treating a
    malformed manifest as an update.  The current local version is expected to
    come from package metadata and is allowed to raise if it is broken.
    """
    try:
        return _version_is_newer(parse_version(candidate), parse_version(current))
    except ValueError:
        return False


def _semver_key(version: SemanticVersion):
    # ``order=True`` cannot compare an int prerelease identifier with a string.
    # SemVer orders numeric identifiers before non-numeric identifiers and a
    # release before every prerelease.
    if not version.prerelease:
        pre = (1,)
    else:
        parts = []
        for item in version.prerelease:
            parts.append((0, item) if isinstance(item, int) else (1, item))
        pre = (0, tuple(parts))
    return version.major, version.minor, version.patch, pre


def _version_is_newer(candidate: SemanticVersion, current: SemanticVersion) -> bool:
    return _semver_key(candidate) > _semver_key(current)


@dataclass(frozen=True)
class UpdateManifest:
    version: str
    download_url: str
    size: int
    sha256: str
    # Optional extras a server may publish alongside the installer.
    notes: str = ""
    notes_url: str = ""
    min_client_version: str = ""

    @classmethod
    def from_json(cls, data: Dict[str, Any], base_url: str) -> "UpdateManifest":
        if not isinstance(data, dict):
            raise UpdateError("update manifest is not an object")
        version = data.get("version") or data.get("client_version")
        if not isinstance(version, str):
            raise UpdateError("update manifest has no version")

        # Accept both the compact shape and the nested package/artifact shape;
        # this keeps the client compatible with server releases that add a
        # package descriptor without making the UI know server details.
        artifact: Dict[str, Any] = {}
        # Prefer a directly runnable installer when a manifest also publishes
        # a package descriptor; package-only manifests still work for callers
        # that want a verified download.
        for key in ("installer", "artifact", "package", "asset"):
            value = data.get(key)
            if isinstance(value, dict):
                artifact = value
                break
            if isinstance(value, str) and not artifact:
                artifact = {"url": value}
        url = (
            artifact.get("url")
            or artifact.get("download_url")
            or data.get("download_url")
            or data.get("url")
        )
        if not isinstance(url, str) or not url.strip():
            raise UpdateError("update manifest has no download URL")
        raw_size = artifact.get("size", data.get("size"))
        digest = artifact.get("sha256", data.get("sha256"))
        try:
            size = int(raw_size)
        except (TypeError, ValueError) as exc:
            raise UpdateError("update manifest has no valid size") from exc
        if size < 0:
            raise UpdateError("update manifest size cannot be negative")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            raise UpdateError("update manifest has no valid SHA-256")
        notes = data.get("notes")
        notes = notes.strip()[:4000] if isinstance(notes, str) else ""
        notes_url = ""
        raw_notes_url = data.get("notes_url")
        if isinstance(raw_notes_url, str) and raw_notes_url.strip():
            try:
                notes_url = _resolve_url(base_url, raw_notes_url.strip())
            except UpdateError:
                notes_url = ""  # a link to somewhere else is simply not shown
        min_version = data.get("min_client_version")
        if not (isinstance(min_version, str) and _valid_version(min_version)):
            min_version = ""
        return cls(
            version,
            _resolve_url(base_url, url),
            size,
            digest.lower(),
            notes=notes,
            notes_url=notes_url,
            min_client_version=min_version,
        )


def _valid_version(value: str) -> bool:
    try:
        parse_version(value)
    except ValueError:
        return False
    return True


def _resolve_url(base_url: str, target: str) -> str:
    parsed = urlparse(target)
    if parsed.scheme and parsed.scheme not in {"http", "https"}:
        raise UpdateError("update URL must use HTTP or HTTPS")
    if parsed.scheme:
        base = urlparse(base_url)
        # Never send the configured server token to an arbitrary URL supplied
        # by a remote manifest. Updates are intentionally server-hosted.
        if (parsed.scheme, parsed.netloc.lower()) != (base.scheme, base.netloc.lower()):
            raise UpdateError("update URL must use the configured server")
    return target if parsed.scheme else urljoin(base_url.rstrip("/") + "/", target.lstrip("/"))


MANIFEST_PATH_WINDOWS = "/install/client-manifest.json"
MANIFEST_PATH_MACOS = "/install/client-manifest-macos.json"


def manifest_path(platform: Optional[str] = None) -> str:
    """The server manifest path for a platform (default: this machine's)."""
    platform = sys.platform if platform is None else platform
    return MANIFEST_PATH_MACOS if platform == "darwin" else MANIFEST_PATH_WINDOWS


def manifest_url(base_url: str, platform: Optional[str] = None) -> str:
    return _resolve_url(base_url, manifest_path(platform))


class ClientUpdater:
    """Fetch and verify updates from one configured meeting-notes server."""

    def __init__(
        self,
        base_url: str,
        token: Optional[str] = None,
        *,
        current_version: str = __version__,
        timeout: float = 10.0,
    ):
        if not base_url or not base_url.strip():
            raise ValueError("server URL is required")
        self.base_url = base_url.rstrip("/")
        self.token = token or ""
        self.current_version = current_version
        self.timeout = timeout

    def _headers(self) -> Dict[str, str]:
        headers = dict(wire.auth_headers(self.token))
        headers.update(identity.client_headers())
        # Hash the published artifact bytes, not a transparent gzip transfer
        # encoding that an HTTP client may decode before yielding chunks.
        headers["Accept-Encoding"] = "identity"
        return headers

    def check(self) -> Optional[UpdateManifest]:
        """Return a newer manifest, or ``None`` when the client is current."""
        try:
            response = httpx.get(
                manifest_url(self.base_url), headers=self._headers(), timeout=self.timeout
            )
            version_gate.inspect_response(response)
            response.raise_for_status()
            manifest = UpdateManifest.from_json(response.json(), self.base_url)
            candidate = parse_version(manifest.version)
            current = parse_version(self.current_version)
        except (httpx.HTTPError, ValueError, TypeError, UpdateError) as exc:
            raise UpdateError(f"could not check for client updates: {exc}") from exc
        # A manifest whose minimum supported version is above ours means the
        # server will not keep taking uploads from this client.
        if manifest.min_client_version and _version_is_newer(
            parse_version(manifest.min_client_version), current
        ):
            version_gate.note_too_old(
                version_gate.MANIFEST, manifest.min_client_version, "the update manifest requires a newer client"
            )
        else:
            version_gate.clear(version_gate.MANIFEST)
        return manifest if _version_is_newer(candidate, current) else None

    def download(self, manifest: UpdateManifest, destination: Optional[Path] = None) -> Path:
        """Download *manifest* and verify its exact size and SHA-256 digest."""
        path = Path(destination) if destination is not None else self._temporary_path(manifest)
        path.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        total = 0
        try:
            with httpx.stream(
                "GET", manifest.download_url, headers=self._headers(), timeout=self.timeout
            ) as response:
                version_gate.inspect_response(response)
                response.raise_for_status()
                with path.open("wb") as output:
                    for chunk in response.iter_bytes(1 << 20):
                        output.write(chunk)
                        digest.update(chunk)
                        total += len(chunk)
        except (httpx.HTTPError, OSError) as exc:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            raise UpdateError(f"could not download client update: {exc}") from exc
        if total != manifest.size or digest.hexdigest().lower() != manifest.sha256.lower():
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            raise UpdateError(
                "downloaded client update failed manifest verification "
                f"(size {total}/{manifest.size}, SHA-256 {digest.hexdigest()})"
            )
        return path

    @staticmethod
    def _temporary_path(manifest: UpdateManifest) -> Path:
        suffix = Path(urlparse(manifest.download_url).path).suffix or (".sh" if sys.platform == "darwin" else ".ps1")
        fd, name = tempfile.mkstemp(prefix="meeting-notes-update-", suffix=suffix)
        os.close(fd)
        return Path(name)

    def download_and_apply(self, manifest: UpdateManifest) -> Path:
        """Verify and launch a server-hosted per-user installer (PowerShell / bash)."""
        path = self.download(manifest)
        self.apply(path)
        return path

    @staticmethod
    def apply(path: Path) -> None:
        """Launch an already verified installer without requiring elevation."""
        path = Path(path)
        suffix = path.suffix.lower()
        if suffix == ".sh":
            if sys.platform != "darwin":
                raise UpdateError("the macOS installer can only be run on macOS")
            try:
                # The script quits this app, swaps the bundle and relaunches it,
                # so it must outlive us: its own session, no inherited pipes.
                # MEETING_NOTES_UPDATE=1 makes the script log to ~/.meeting-notes/logs/update.log.
                subprocess.Popen(
                    ["/bin/bash", str(path)],
                    env={**os.environ, "MEETING_NOTES_UPDATE": "1"},
                    close_fds=True,
                    cwd=tempfile.gettempdir(),
                    start_new_session=True,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except OSError as exc:
                raise UpdateError(f"could not launch the verified client installer: {exc}") from exc
            return
        if suffix != ".ps1":
            raise UpdateError("verified update is not a PowerShell or shell installer")
        if os.name != "nt":
            raise UpdateError("PowerShell client updates are supported on Windows only")
        try:
            # Never start the installer inside the app folder (the shortcut's
            # working directory): Windows won't let it rename a folder a
            # process is sitting in, and the update would fail.
            subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(path)],
                close_fds=True,
                cwd=tempfile.gettempdir(),
            )
        except OSError as exc:
            raise UpdateError(f"could not launch the verified client installer: {exc}") from exc
