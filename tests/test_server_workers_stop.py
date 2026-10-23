"""Every background thread a server app starts is stopped by its shutdown (and by the tests' leak guard)."""

from __future__ import annotations

import threading
import time

from fastapi.testclient import TestClient

from meeting_notes.server import app as app_mod
from meeting_notes.server.app import create_app


def _new_threads(before):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        alive = [t for t in threading.enumerate() if t not in before and t.is_alive()]
        if not alive:
            return []
        time.sleep(0.05)
    return [t.name for t in alive]


def test_stop_workers_ends_every_background_thread(tmp_path):
    before = set(threading.enumerate())
    app = create_app(data_root=str(tmp_path / "data"), enable_mcp=False)
    assert len(set(threading.enumerate()) - before) >= 5  # jobs, offers, compute, retention, notion, mobile...

    app.state.stop_workers()
    app.state.stop_workers()  # a second call (the lifespan after the leak guard) does nothing

    assert _new_threads(before) == []
    assert all(stop is not app.state.stop_workers for stop in app_mod._UNSTOPPED.values())


def test_the_lifespan_shutdown_stops_them_including_the_mobile_worker(tmp_path):
    before = set(threading.enumerate())
    with TestClient(create_app(data_root=str(tmp_path / "data"), enable_mcp=False)) as client:
        assert client.get("/health").status_code == 200
    assert _new_threads(before) == []
