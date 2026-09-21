"""The optional Codex bridge worker.

The bridge deliberately runs outside the web process.  It claims one review
job from the server, downloads the transcript and workflow instructions into a
private temporary directory, then asks the locally authenticated Codex CLI to
write structured meeting notes.  The transcript is never put in argv or in an
environment variable, and the server bearer token is removed from the child
environment.

The HTTP contract is intentionally small and stable:

* ``GET /v1/bridge/review/claim?worker_id=...`` (204 means no work)
* ``POST /v1/bridge/review/{id}/complete`` with the validated notes object
* ``POST /v1/bridge/review/{id}/failure`` with ``{"error": ...}``

The claim response may provide ``transcript_url`` and ``workflow_url`` (or
inline ``transcript``/``workflow`` for small test deployments).  URLs are
resolved relative to the configured server URL and always fetched with the
same bearer authentication.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urljoin

import httpx

from .review_contract import (
    ReviewValidationError,
    output_schema as _output_schema,
    validate_notes as _validate_notes,
)


CLAIM_PATH = "/v1/bridge/review/claim"
COMPLETE_PATH = "/v1/bridge/review/{job_id}/complete"
FAILURE_PATH = "/v1/bridge/review/{job_id}/failure"


class BridgeError(RuntimeError):
    """A recoverable bridge or server error."""


@dataclass(frozen=True)
class BridgeConfig:
    server_url: str
    token: Optional[str] = None
    codex_command: str = "codex"
    poll_seconds: float = 10.0
    timeout: float = 30.0
    worker_id: str = ""

    @property
    def resolved_worker_id(self) -> str:
        return self.worker_id or f"codex-bridge-{uuid.uuid4().hex[:12]}"


def _resource_text(name: str) -> str:
    return resources.files("meeting_notes").joinpath("bridge", name).read_text(encoding="utf-8")


def output_schema() -> Dict[str, Any]:
    return _output_schema()


def bridge_prompt() -> str:
    return _resource_text("prompt.md")


def _headers(token: Optional[str]) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


def _validate_string(value: Any, name: str, *, allow_empty: bool = True) -> None:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise BridgeError(f"notes.{name} must be a string")


def validate_notes(value: Any) -> Dict[str, Any]:
    """Validate Codex output while preserving the bridge's public error type."""
    try:
        return _validate_notes(value)
    except ReviewValidationError as exc:
        raise BridgeError(str(exc)) from exc


def _safe_child_env() -> Dict[str, str]:
    """Retain Codex's normal ChatGPT login, but remove application secrets."""
    blocked = {
        "MEETING_NOTES_TOKEN", "OPENAI_API_KEY", "OPENAI_ADMIN_KEY",
        "MEETING_NOTES_TRANSCRIPT", "TRANSCRIPT", "TRANSCRIPT_TEXT",
    }
    return {key: value for key, value in os.environ.items() if key.upper() not in blocked}


class BridgeWorker:
    def __init__(self, config: BridgeConfig):
        self.config = config
        self.base_url = config.server_url.rstrip("/") + "/"
        self.client = httpx.Client(timeout=config.timeout, follow_redirects=False)

    def close(self) -> None:
        self.client.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}))
        headers.update(_headers(self.config.token))
        try:
            response = self.client.request(method, urljoin(self.base_url, path.lstrip("/")), headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise BridgeError(f"server request failed: {exc}") from exc
        return response

    def claim(self) -> Optional[Dict[str, Any]]:
        response = self._request("GET", CLAIM_PATH, params={"worker_id": self.config.resolved_worker_id})
        if response.status_code == 204 or not response.content:
            return None
        if response.status_code == 409:
            return None
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not payload.get("id"):
            raise BridgeError("claim response must contain an id")
        return payload

    def _download(self, job: Dict[str, Any], key: str, temp_dir: Path) -> Path:
        inline = job.get(key)
        suffix = ".md" if key == "workflow" else ".txt"
        path = temp_dir / f"{key}{suffix}"
        if inline is not None:
            path.write_text(str(inline), encoding="utf-8")
            return path
        url = job.get(f"{key}_url") or job.get(f"{key}_path")
        if not url:
            raise BridgeError(f"claim response has no {key}_url")
        response = self._request("GET", str(url))
        response.raise_for_status()
        path.write_bytes(response.content)
        return path

    def _run_codex(self, transcript: Path, workflow: Path, output: Path, schema: Path) -> None:
        prompt = bridge_prompt() + (
            "\n\nThe server-provided workflow is at: " + str(workflow) +
            "\nThe server-provided transcript is at: " + str(transcript) +
            "\nWrite only the JSON object required by the schema."
        )
        command = [
            self.config.codex_command, "exec", "--ephemeral", "--sandbox", "read-only",
            "--skip-git-repo-check", "--output-schema", str(schema), "-o", str(output), prompt,
        ]
        completed = subprocess.run(command, cwd=str(transcript.parent), env=_safe_child_env(), check=False)
        if completed.returncode:
            raise BridgeError(f"codex exited with status {completed.returncode}")
        if not output.exists():
            raise BridgeError("codex did not produce an output file")

    def process(self, job: Dict[str, Any]) -> Dict[str, Any]:
        job_id = str(job["id"])
        try:
            with tempfile.TemporaryDirectory(prefix="meeting-notes-bridge-") as raw_dir:
                temp_dir = Path(raw_dir)
                transcript = self._download(job, "transcript", temp_dir)
                workflow = self._download(job, "workflow", temp_dir)
                schema = temp_dir / "schema.json"
                schema.write_text(json.dumps(output_schema()), encoding="utf-8")
                output = temp_dir / "notes.json"
                self._run_codex(transcript, workflow, output, schema)
                notes = validate_notes(json.loads(output.read_text(encoding="utf-8")))
            response = self._request("POST", COMPLETE_PATH.format(job_id=job_id), json={"notes": notes})
            response.raise_for_status()
            return notes
        except Exception as exc:
            try:
                self._request("POST", FAILURE_PATH.format(job_id=job_id), json={"error": str(exc)[:2000]})
            except Exception:
                pass
            raise

    def run(self, *, once: bool = False) -> None:
        try:
            while True:
                job = self.claim()
                if job is not None:
                    try:
                        self.process(job)
                    except Exception:
                        if once:
                            raise
                    if once:
                        return
                elif once:
                    return
                if not once:
                    time.sleep(max(0.1, self.config.poll_seconds))
        finally:
            self.close()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Process queued Meeting Notes reviews with Codex")
    parser.add_argument("--server", default=os.environ.get("MEETING_NOTES_SERVER", "http://127.0.0.1:8000"))
    parser.add_argument("--token", default=os.environ.get("MEETING_NOTES_TOKEN"))
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--poll-seconds", type=float, default=10.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    worker = BridgeWorker(BridgeConfig(args.server, args.token, args.codex, args.poll_seconds))
    try:
        worker.run(once=args.once)
    except (BridgeError, httpx.HTTPError) as exc:
        print(f"meeting-notes bridge: {exc}", file=os.sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
