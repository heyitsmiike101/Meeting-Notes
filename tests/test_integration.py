"""End-to-end: record, queue, upload, transcript -- through the real controller.

Every module in this project had its own passing tests while the application
did not work at all, because nothing exercised the seams between them. Three
integration bugs shipped that way: the upload worker was never started, the UI
called ``.get()`` on a dataclass, and the queue was handed the recordings
folder instead of its own directory. Each test below maps to one of those, or
to a promise the architecture makes ("a dropped server cannot lose a meeting").

No audio hardware, no Whisper model, no external network: fake audio sources
drive the real recorder, and a real server runs in-process with a stub
transcriber.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from meeting_notes import config as config_mod
from meeting_notes import wire
from meeting_notes.client import controller as controller_mod
from meeting_notes.client.controller import IDLE, RecordingController
from meeting_notes.client.queue import SessionQueue
from meeting_notes.server.app import create_app
from meeting_notes.transcribe.protocol import Segment
from tests.fakes import RELEASE, FakeSource

RATE = 48000


class StubTranscriber:
    def __init__(self, **_kwargs):
        pass

    def transcribe(self, wav_path: Path, track: str):
        text = "hello from the microphone" if track == "mic" else "hello from the far end"
        return [Segment(start=0.0, end=0.4, text=text, track=track)]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LiveServer:
    def __init__(self, data_root: Path):
        self.port = _free_port()
        app = create_app(transcriber_factory=StubTranscriber, data_root=str(data_root))
        self.server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning")
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> str:
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert self.server.started, "server did not start"
        return self.url

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)


@pytest.fixture(autouse=True)
def release_stalled_threads():
    RELEASE.clear()
    yield
    RELEASE.set()
    time.sleep(0.05)


@pytest.fixture()
def server(tmp_path):
    live = LiveServer(tmp_path / "server-data")
    live.start()
    yield live
    live.stop()


def configure(tmp_path, monkeypatch, *, server_url: str = "", live_preview: bool = False) -> Path:
    """Isolated config pointing at a temp save folder, like a fresh install."""
    monkeypatch.setenv("MEETING_NOTES_CONFIG", str(tmp_path / "config.json"))
    save_dir = tmp_path / "Meeting Notes"
    config_mod.save_config(
        {
            "save_dir": str(save_dir),
            "server": {
                "url": server_url,
                "token": "",
                "live_preview": live_preview,
                "auto_upload": True,
            },
        }
    )
    return save_dir


def record_briefly(controller: RecordingController, seconds: float = 1.2, name: str = "standup"):
    sources = {
        "mic": FakeSource(name="Fake Mic", samplerate=RATE),
        "system": FakeSource(name="Fake Loopback", samplerate=RATE),
    }
    session_dir = controller.start(name, sources=sources)
    assert session_dir is not None, f"recording did not start: {controller.error}"
    time.sleep(seconds)
    controller.stop()
    return session_dir


def drain(controller: RecordingController, timeout: float = 30.0) -> bool:
    """Run the uploader until the queue empties. Returns True if it emptied."""
    assert controller.start_uploader(), "uploader refused to start"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if controller.queue_status()["pending"] == 0:
            return True
        time.sleep(0.2)
    return False


# -- defect 1: nothing ever ran the uploader ---------------------------------


def test_recording_is_uploaded_and_transcript_comes_back(tmp_path, monkeypatch, server):
    """The whole promise in one test: record, and a transcript appears.

    Before the fix nothing constructed an UploadWorker outside the tests, so
    a session queued and sat there forever and no transcript ever arrived.
    """
    configure(tmp_path, monkeypatch, server_url=server.url)
    controller = RecordingController()
    session_dir = record_briefly(controller)

    assert (session_dir / "mic.wav").exists()
    assert (session_dir / "system.wav").exists()
    assert controller.queue_status()["pending"] == 1

    try:
        assert drain(controller), "queue never emptied"
    finally:
        controller.stop_uploader()

    transcript = session_dir / "transcript.md"
    assert transcript.exists(), "no transcript was written back into the session"
    text = transcript.read_text(encoding="utf-8")
    assert "hello from the microphone" in text
    assert "hello from the far end" in text
    # Both speakers attributed -- the point of recording two tracks.
    assert "You" in text and "Them" in text
    assert json.loads((session_dir / "transcript.json").read_text())["segments"]


# -- defect 3: the queue was handed the recordings folder --------------------


def test_queue_state_lives_beside_recordings_not_among_them(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch, server_url="http://127.0.0.1:1")
    controller = RecordingController()
    session_dir = record_briefly(controller)

    save_dir = config_mod.save_dir()
    queue_dir = SessionQueue.for_save_dir(save_dir).queue_dir
    assert queue_dir.parent == save_dir and queue_dir != save_dir
    assert list(queue_dir.glob("*.json")), "the session was not queued"
    # The recordings folder must contain session folders only -- no queue state.
    assert not list(save_dir.glob("*.json")), "queue state leaked into the recordings folder"
    assert session_dir.parent == save_dir


# -- defect 2: a dataclass reached code expecting a mapping ------------------


def test_live_partial_is_normalised_and_renders_in_the_ui(tmp_path, monkeypatch):
    """The streamer hands over a wire.Partial; the UI reads mappings.

    This is the exact callback path, so it fails the way the real app failed:
    AttributeError inside the Qt timer tick on the first live partial.
    """
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from meeting_notes.client.ui.main_window import MainWindow

    configure(tmp_path, monkeypatch)
    QApplication.instance() or QApplication([])

    window = MainWindow()
    window._timer.stop()
    # Exactly what LiveStreamer._handle passes to on_partial.
    window.controller._record_partial(
        wire.Partial(track="system", start=1.0, end=2.0, text="can you hear me")
    )
    window._drain_partials()  # would raise AttributeError before the fix

    assert "can you hear me" in window.preview.toPlainText()
    assert "Them" in window.preview.toPlainText()
    stored = window.controller.partials()[0]
    assert isinstance(stored, dict) and stored["track"] == "system"


# -- the offline promise -----------------------------------------------------


def test_meeting_survives_a_server_that_is_down_and_uploads_later(tmp_path, monkeypatch):
    """Record with nothing listening, then bring the server up.

    The local recording is the source of truth, so an unreachable server must
    cost nothing but time.
    """
    dead_port = _free_port()
    configure(tmp_path, monkeypatch, server_url=f"http://127.0.0.1:{dead_port}")

    controller = RecordingController()
    session_dir = record_briefly(controller, name="offline")
    assert (session_dir / "mic.wav").exists(), "recording must not depend on the server"
    assert controller.queue_status()["pending"] == 1

    # One pass against the dead server leaves the entry queued, not lost.
    controller.start_uploader()
    time.sleep(1.0)
    controller.stop_uploader()
    assert controller.queue_status()["pending"] == 1
    assert not (session_dir / "transcript.md").exists()

    # Now the server exists on that same port, as if it came back.
    live = LiveServer(tmp_path / "server-data")
    live.port = dead_port
    live.server = uvicorn.Server(
        uvicorn.Config(
            create_app(transcriber_factory=StubTranscriber, data_root=str(tmp_path / "sd2")),
            host="127.0.0.1",
            port=dead_port,
            log_level="warning",
        )
    )
    live.thread = threading.Thread(target=live.server.run, daemon=True)
    live.start()
    try:
        controller._uploader = None  # allow a fresh worker after the earlier stop
        assert drain(controller, timeout=40), "backlog did not upload once the server returned"
    finally:
        controller.stop_uploader()
        live.stop()

    assert (session_dir / "transcript.md").exists(), "the queued meeting never transcribed"


def test_uploader_drains_a_backlog_that_existed_before_it_started(tmp_path, monkeypatch, server):
    """'Runs whenever the app is open': a backlog must clear on launch,
    without needing another recording to trigger it."""
    configure(tmp_path, monkeypatch, server_url=server.url)
    controller = RecordingController()
    session_dir = record_briefly(controller, name="yesterday")
    assert controller.queue_status()["pending"] == 1

    # A brand new controller, as if the app had been closed and reopened.
    fresh = RecordingController()
    assert fresh.state == IDLE
    try:
        assert drain(fresh), "a pre-existing backlog was not drained on start"
    finally:
        fresh.stop_uploader()
    assert (session_dir / "transcript.md").exists()


def test_opening_the_window_starts_the_uploader(tmp_path, monkeypatch, server):
    """The actual shipped bug: UploadWorker existed and worked, but no
    application code ever constructed one -- only tests did. Asserting that
    start_uploader() works is not enough; something must call it."""
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from meeting_notes.client.ui.main_window import MainWindow

    configure(tmp_path, monkeypatch, server_url=server.url)
    QApplication.instance() or QApplication([])

    window = MainWindow()
    window._timer.stop()
    try:
        assert window.controller._uploader is not None, "the window never started the uploader"
    finally:
        window.controller.stop_uploader()
    assert window.controller._uploader is None


def test_window_shows_pending_uploads_in_the_status_line(tmp_path, monkeypatch):
    """A queued upload the user cannot see is indistinguishable from a lost one."""
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from meeting_notes.client.ui.main_window import MainWindow

    configure(tmp_path, monkeypatch, server_url="http://127.0.0.1:1")
    QApplication.instance() or QApplication([])

    controller = RecordingController()
    record_briefly(controller, name="pending-one")

    window = MainWindow()
    window._timer.stop()
    window.controller.stop_uploader()
    window._update_status()
    assert "pending" in window.status_label.text().lower()


class _FakeMonotonic:
    """Stands in for the ``time`` module: ``monotonic()`` only moves when the test says so."""

    def __init__(self):
        self.now = 1000.0

    def advance(self, seconds):
        self.now += seconds

    def monotonic(self):
        return self.now

    def __getattr__(self, name):
        return getattr(time, name)


def test_queue_status_is_cached_briefly_to_avoid_reglobbing_every_ui_tick(tmp_path, monkeypatch):
    """The status line is polled by a 33ms Qt timer. Without a cache,
    controller.queue_status() -> SessionQueue.pending() re-globs the queue
    directory and re-parses every entry's JSON on every single tick, forever
    -- 30x/sec even when nothing has changed."""
    configure(tmp_path, monkeypatch, server_url="http://127.0.0.1:1")
    # The cache is time-based, so drive it with a fake monotonic clock: with the real clock, a slow disk or a
    # loaded machine between the two calls could outlast the 1 s TTL and make "still cached" fail.
    clock = _FakeMonotonic()
    monkeypatch.setattr(controller_mod, "time", clock)
    controller = RecordingController()

    assert controller.queue_status() == {"pending": 0, "failed": 0, "last_error": ""}

    # Queue a session directly on disk, bypassing the controller -- like a
    # second process, or a retry, touching the same queue directory. Nothing
    # in SessionQueue itself caches, so a live re-read would see this
    # immediately; the point of this test is that the controller's own cache
    # holds the stale answer for a little while instead.
    session_dir = config_mod.save_dir() / "sideloaded"
    session_dir.mkdir(parents=True)
    SessionQueue.for_save_dir(config_mod.save_dir()).enqueue(session_dir)

    clock.advance(controller_mod._QUEUE_STATUS_CACHE_SECONDS / 2)
    assert controller.queue_status() == {"pending": 0, "failed": 0, "last_error": ""}  # still the cached answer

    clock.advance(controller_mod._QUEUE_STATUS_CACHE_SECONDS)  # past the cache's TTL
    assert controller.queue_status() == {"pending": 1, "failed": 0, "last_error": ""}
