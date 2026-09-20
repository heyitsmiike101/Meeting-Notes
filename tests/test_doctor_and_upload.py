"""Tests for server-awareness in ``doctor.py`` and the headless ``upload`` CLI.

Hardware-free and model-free, same as ``tests/test_client_transport.py``: the
only "network" involved is a REAL server (either the full
``meeting_notes.server.app.create_app`` with a stub transcriber, or a tiny
purpose-built FastAPI app for a case the real one can't easily be coaxed
into) run in-process in a background uvicorn thread on localhost -- no real
Whisper model can run here (huggingface.co is blocked), and
``ServerClient``/``UploadWorker`` use real sockets, so there is no ASGI
transport shortcut available to them.

Config isolation is via ``MEETING_NOTES_CONFIG`` (see
``meeting_notes/config.py``), pointed at a per-test temp file, so nothing
here can ever touch a real ``~/.meeting-notes/config.json``.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import wave
from pathlib import Path
from typing import Optional

import numpy as np
import pytest
import uvicorn
from fastapi import FastAPI

from meeting_notes import config as config_mod
from meeting_notes import doctor, wire
from meeting_notes.cli import build_parser
from meeting_notes.client.queue import SessionQueue
from meeting_notes.server.app import create_app
from meeting_notes.transcribe.protocol import Segment


# =============================================================================
# shared test infrastructure
# =============================================================================


class _StubTranscriber:
    """Stands in for faster-whisper: one canned segment per track, no model
    download -- huggingface.co is blocked here, so nothing real could run."""

    def transcribe(self, wav_path, track):
        return [Segment(start=0.0, end=0.4, text=f"hello from {track}", track=track)]


def _stub_transcriber_factory(**_kwargs):
    return _StubTranscriber()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _LiveServer:
    """A real uvicorn server on localhost, for tests that need actual sockets
    (``ServerClient``/``UploadWorker`` make real HTTP calls, not ASGI-transport
    calls)."""

    def __init__(self, app: FastAPI):
        self.port = _free_port()
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> str:
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        if not self.server.started:
            raise RuntimeError("server did not start in time")
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)


@pytest.fixture(scope="module")
def stub_server(tmp_path_factory):
    """A healthy, un-authenticated server -- the "everything works" case."""
    data_root = tmp_path_factory.mktemp("server-data")
    app = create_app(transcriber_factory=_stub_transcriber_factory, data_root=str(data_root))
    live = _LiveServer(app)
    base_url = live.start()
    yield base_url
    live.stop()


@pytest.fixture()
def dead_port_url() -> str:
    """A URL nothing is listening on: bound then immediately released, so the
    port number is valid but any connection to it is refused."""
    port = _free_port()
    return f"http://127.0.0.1:{port}"


@pytest.fixture()
def mismatched_protocol_server():
    """A minimal hand-rolled app that answers /health with the WRONG protocol
    version -- the real server always reports its own true
    ``wire.PROTOCOL_VERSION``, so there is no way to provoke a mismatch from
    it short of monkeypatching the module both sides read from (which would
    "fix" the client's expectation right along with the server's answer and
    never produce a mismatch at all). A tiny standalone app sidesteps that."""
    app = FastAPI()

    @app.get(wire.HEALTH)
    async def health():
        return {
            "status": "ok",
            "protocol": wire.PROTOCOL_VERSION + 1,
            "model": "some-model",
            "device": "cpu",
        }

    live = _LiveServer(app)
    base_url = live.start()
    yield base_url
    live.stop()


def _make_session_dir(tmp_path: Path, name: str = "session") -> Path:
    """A minimal but realistic two-track session directory, matching what
    RecordingSession.finalize() produces (see meeting_notes/audio/session.py) --
    including timing sidecars, since merge_tracks (used server-side by
    jobs.py) drops any track with no timing log. Same shape as the helper of
    the same name in tests/test_client_transport.py."""
    session_dir = tmp_path / name
    session_dir.mkdir()
    rate = 48000
    tracks = {}
    for track, freq in (("mic", 440.0), ("system", 220.0)):
        wav_path = session_dir / f"{track}.wav"
        t = np.arange(int(rate * 0.2)) / rate
        samples = (0.3 * np.sin(2 * np.pi * freq * t) * 32767).astype("<i2")
        with wave.open(str(wav_path), "wb") as fh:
            fh.setnchannels(1)
            fh.setsampwidth(2)
            fh.setframerate(rate)
            fh.writeframes(samples.tobytes())
        duration = len(samples) / rate
        timing_path = session_dir / f"{track}.timing.jsonl"
        timing_path.write_text(
            json.dumps(
                {
                    "event": "open",
                    "segment": 0,
                    "frames": 0,
                    "t": 0.0,
                    "wall": 0.0,
                    "samplerate": rate,
                    "channels": 1,
                    "device": "test",
                }
            )
            + "\n"
            + json.dumps({"event": "close", "segment": 0, "frames": len(samples), "t": duration})
            + "\n",
            encoding="utf-8",
        )
        tracks[track] = {
            "wav": wav_path.name,
            "samplerate": rate,
            "channels": 1,
            "label": "You" if track == "mic" else "Them",
            "frames": len(samples),
            "duration_sec": round(duration, 3),
        }
    meta = {
        "version": 1,
        "created": "2026-01-01T00:00:00",
        "duration_sec": 0.2,
        "tracks": tracks,
    }
    (session_dir / "session.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return session_dir


def _write_config(config_path: Path, save_dir: Path, server: Optional[dict] = None) -> None:
    data = {"save_dir": str(save_dir)}
    if server is not None:
        data["server"] = server
    config_mod.save_config(data, path=config_path)


@pytest.fixture()
def isolated_config(tmp_path, monkeypatch):
    """Point MEETING_NOTES_CONFIG at a private temp file for this test, so
    nothing here can ever read or write a real ~/.meeting-notes/config.json."""
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("MEETING_NOTES_CONFIG", str(config_path))
    save_dir = tmp_path / "recordings"
    save_dir.mkdir()
    return config_path, save_dir


# =============================================================================
# doctor.py -- _check_server
# =============================================================================


class TestCheckServer:
    def test_no_server_configured_is_ok_not_a_failure(self, isolated_config):
        config_path, save_dir = isolated_config
        _write_config(config_path, save_dir, server=None)

        check = doctor._check_server()
        assert check.ok is True
        assert "locally" in check.detail
        assert "meeting-notes transcribe" in check.detail

    def test_configured_but_unreachable_fails_with_url_and_fix(self, isolated_config, dead_port_url):
        config_path, save_dir = isolated_config
        _write_config(config_path, save_dir, server={"url": dead_port_url, "token": ""})

        check = doctor._check_server()
        assert check.ok is False
        assert dead_port_url in check.detail
        assert check.fix  # actionable, not a bare error dump
        assert "reachable" in check.fix or "running" in check.fix

    def test_protocol_mismatch_fails_and_names_both_versions(
        self, isolated_config, mismatched_protocol_server
    ):
        config_path, save_dir = isolated_config
        _write_config(config_path, save_dir, server={"url": mismatched_protocol_server, "token": ""})

        check = doctor._check_server()
        assert check.ok is False
        assert str(wire.PROTOCOL_VERSION) in check.detail
        assert str(wire.PROTOCOL_VERSION + 1) in check.detail
        assert "release" in check.detail or "version" in check.detail

    def test_auth_rejected_fails_and_points_at_token_config(
        self, isolated_config, tmp_path_factory, monkeypatch
    ):
        # A server with an actual token configured, reached with the WRONG
        # token -- require_token (server/auth.py) answers 403 for "wrong",
        # distinct from 401 for "missing", but doctor should treat either as
        # "rejected".
        monkeypatch.setenv("MEETING_NOTES_TOKEN", "right-token")
        data_root = tmp_path_factory.mktemp("server-data-auth")
        app = create_app(transcriber_factory=_stub_transcriber_factory, data_root=str(data_root))
        live = _LiveServer(app)
        base_url = live.start()
        try:
            config_path, save_dir = isolated_config
            _write_config(
                config_path, save_dir, server={"url": base_url, "token": "wrong-token"}
            )

            check = doctor._check_server()
            assert check.ok is False
            assert "credentials" in check.detail or "token" in check.detail.lower()
            assert check.fix
            assert "Settings" in check.fix or "MEETING_NOTES_TOKEN" in check.fix
        finally:
            live.stop()

    def test_reachable_and_healthy_reports_model_and_device(self, isolated_config, stub_server):
        config_path, save_dir = isolated_config
        _write_config(config_path, save_dir, server={"url": stub_server, "token": ""})

        check = doctor._check_server()
        assert check.ok is True
        assert stub_server in check.detail
        # /health with no MEETING_NOTES_MODEL set reports "none"/"cpu" -- see
        # server/app.py's health() -- still worth asserting they're surfaced.
        assert "model=" in check.detail
        assert "device=" in check.detail

    def test_never_raises_on_a_corrupt_config(self, isolated_config):
        config_path, save_dir = isolated_config
        config_path.write_text("{not valid json", encoding="utf-8")

        check = doctor._check_server()  # must not raise
        assert isinstance(check, doctor.Check)


# =============================================================================
# doctor.py -- _check_upload_queue
# =============================================================================


class TestCheckUploadQueue:
    def test_missing_queue_directory_is_ok_and_never_raises(self, isolated_config):
        # SessionQueue.for_save_dir creates the directory on demand, so this
        # also covers "queue dir does not exist yet".
        config_path, save_dir = isolated_config
        _write_config(config_path, save_dir)

        check = doctor._check_upload_queue()
        assert check.ok is True
        assert "empty" in check.detail

    def test_pending_entries_are_ok(self, isolated_config, tmp_path):
        config_path, save_dir = isolated_config
        _write_config(config_path, save_dir)

        session_dir = _make_session_dir(tmp_path, "pending-session")
        queue = SessionQueue.for_save_dir(save_dir)
        queue.enqueue(session_dir)

        check = doctor._check_upload_queue()
        assert check.ok is True
        assert "1" in check.detail

    def test_failed_entry_is_a_warning_naming_the_error(self, isolated_config, tmp_path):
        config_path, save_dir = isolated_config
        _write_config(config_path, save_dir)

        session_dir = _make_session_dir(tmp_path, "failed-session")
        queue = SessionQueue.for_save_dir(save_dir)
        entry_id = queue.enqueue(session_dir)
        queue.mark_attempt_failed(entry_id, "connection refused", terminal=True)

        check = doctor._check_upload_queue()
        assert check.ok is False
        assert check.fix  # WARN, not FAIL, per format_report's marker rule
        assert "connection refused" in check.detail


# =============================================================================
# doctor.py -- run_doctor never raises, and includes the new checks
# =============================================================================


class TestRunDoctorIncludesServerChecks:
    def test_run_doctor_appends_server_and_queue_checks(self, isolated_config):
        config_path, save_dir = isolated_config
        _write_config(config_path, save_dir)

        checks = doctor.run_doctor()
        names = [c.name for c in checks]
        assert "transcription server" in names
        assert "upload queue" in names


# =============================================================================
# cli.py -- `meeting-notes upload`
# =============================================================================


def _run_cli(argv: list) -> "tuple[int]":
    args = build_parser().parse_args(argv)
    return args.func(args)


class TestUploadCli:
    def test_list_on_empty_queue(self, isolated_config, capsys):
        config_path, save_dir = isolated_config
        _write_config(config_path, save_dir)

        code = _run_cli(["upload", "--list"])
        out = capsys.readouterr().out
        assert code == 0
        assert "empty" in out

    def test_list_on_populated_queue_does_not_upload(self, isolated_config, tmp_path, capsys):
        config_path, save_dir = isolated_config
        _write_config(config_path, save_dir, server={"url": "http://127.0.0.1:1", "token": ""})

        session_dir = _make_session_dir(tmp_path, "listed-session")
        queue = SessionQueue.for_save_dir(save_dir)
        queue.enqueue(session_dir)

        code = _run_cli(["upload", "--list"])
        out = capsys.readouterr().out
        assert code == 0
        assert "listed-session" in out
        assert "pending" in out
        # Nothing was actually attempted: entry is untouched.
        entries = queue.pending()
        assert len(entries) == 1
        assert entries[0]["attempts"] == 0

    def test_no_server_configured_exits_2(self, isolated_config, tmp_path, capsys):
        config_path, save_dir = isolated_config
        _write_config(config_path, save_dir, server=None)

        session_dir = _make_session_dir(tmp_path, "orphan-session")
        queue = SessionQueue.for_save_dir(save_dir)
        queue.enqueue(session_dir)

        code = _run_cli(["upload"])
        err = capsys.readouterr().err
        assert code == 2
        assert "No server" in err

    def test_successful_drain_writes_transcript_and_empties_queue(
        self, isolated_config, tmp_path, stub_server, capsys
    ):
        config_path, save_dir = isolated_config
        _write_config(config_path, save_dir, server={"url": stub_server, "token": ""})

        session_dir = _make_session_dir(tmp_path, "good-session")
        queue = SessionQueue.for_save_dir(save_dir)
        queue.enqueue(session_dir)

        code = _run_cli(["upload"])
        out = capsys.readouterr().out
        assert code == 0
        assert (session_dir / "transcript.md").exists()
        assert (session_dir / "transcript.json").exists()
        md = (session_dir / "transcript.md").read_text(encoding="utf-8")
        assert "hello from mic" in md
        assert queue.pending() == []
        assert "empty" in out.lower()

    def test_drain_against_dead_server_leaves_entry_pending_and_exits_nonzero(
        self, isolated_config, tmp_path, dead_port_url, capsys
    ):
        config_path, save_dir = isolated_config
        _write_config(config_path, save_dir, server={"url": dead_port_url, "token": ""})

        session_dir = _make_session_dir(tmp_path, "flaky-session")
        queue = SessionQueue.for_save_dir(save_dir)
        queue.enqueue(session_dir)

        code = _run_cli(["upload"])
        out = capsys.readouterr().out
        assert code == 1
        entries = queue.pending()
        assert len(entries) == 1
        assert entries[0]["status"] == "pending"  # not yet exhausted its retries
        assert entries[0]["attempts"] >= 1
        assert "flaky-session" in out

    def test_once_runs_a_single_pass(self, isolated_config, tmp_path, dead_port_url, capsys):
        config_path, save_dir = isolated_config
        _write_config(config_path, save_dir, server={"url": dead_port_url, "token": ""})

        session_dir = _make_session_dir(tmp_path, "once-session")
        queue = SessionQueue.for_save_dir(save_dir)
        queue.enqueue(session_dir)

        code = _run_cli(["upload", "--once"])
        assert code == 1
        entries = queue.pending()
        assert len(entries) == 1
        assert entries[0]["attempts"] == 1  # exactly one pass, no retry loop
