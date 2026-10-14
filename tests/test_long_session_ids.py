"""Long meeting names: the session id a 0.7.16-or-older recorder sends can be well over 128 characters."""

from __future__ import annotations

import time

import httpx
import pytest

from meeting_notes import wire
from meeting_notes.server import store as store_mod

LONG = ("2026-10-14_13-08-49_Meeting-join-All-Covered-Leadership-Lunch-n-Learn-Spotlight-Operations-with-Bob-"
        "Deschamps_MFUENTES-ACW11_1a2b3c4d")


def test_real_long_id_is_accepted():
    assert len(LONG) > 128
    assert store_mod.is_safe_id(LONG)
    assert not store_mod.is_safe_id("a" * (store_mod.MAX_ID_LENGTH + 1))
    assert not store_mod.is_safe_id("../x")


def test_close_reason_fits_a_close_frame():
    reason = wire.close_reason("invalid session_id: " + repr("x" * 300))
    assert len(reason.encode("utf-8")) <= 120 and reason.endswith("...")
    assert wire.close_reason("unauthorized") == "unauthorized"
    assert len(wire.close_reason("é" * 200).encode("utf-8")) <= 120  # cut on a character boundary


def test_live_stream_and_upload_work_with_a_long_meeting_name(tmp_path, monkeypatch):
    """The real client streamer and the pipeline PUT against a real socket, with the id from the log."""
    import sys
    sys.path.insert(0, "tests/compat")
    import compat_support as support
    from meeting_notes.client import streamer as st
    from meeting_notes.server.app import create_app

    monkeypatch.setenv("MEETING_NOTES_TOKEN", "long-id-token")
    app = create_app(data_root=str(tmp_path / "data"), enable_mcp=False, transcriber_factory=None)
    live = support.LiveServer(app)
    base = live.start()
    s = st.LiveStreamer(base, "long-id-token", buffer_seconds=5.0)
    try:
        s.start(LONG, "Long name", time.time())
        tone = support.pcm_tone(0.5)
        for _ in range(4):
            s.submit("mic", tone)
            s.submit("system", tone)
        deadline = time.time() + 8
        while time.time() < deadline and s.state != "connected":
            time.sleep(0.05)
        assert s.state == "connected", (s.state, s.last_error, s.permanent_error)
        r = httpx.get(base + "/v1/live", headers={"Authorization": "Bearer long-id-token"})
        assert LONG in {i["session_id"] for i in r.json()["items"]}
    finally:
        s.stop(join_timeout=3.0)
        live.stop()


def test_an_invalid_id_gets_a_clean_permanent_rejection(tmp_path, monkeypatch):
    """A refused stream must reach the recorder as a rejection (not a protocol error it retries forever)."""
    import sys
    sys.path.insert(0, "tests/compat")
    import compat_support as support
    from meeting_notes.client import streamer as st
    from meeting_notes.server.app import create_app

    monkeypatch.setenv("MEETING_NOTES_TOKEN", "long-id-token")
    app = create_app(data_root=str(tmp_path / "data"), enable_mcp=False, transcriber_factory=None)
    live = support.LiveServer(app)
    base = live.start()
    s = st.LiveStreamer(base, "long-id-token")
    try:
        s.start("x" * 250, "Too long", time.time())
        deadline = time.time() + 8
        while time.time() < deadline and s.state != "rejected":
            time.sleep(0.05)
        assert s.state == "rejected", (s.state, s.last_error)
    finally:
        s.stop(join_timeout=2.0)
        live.stop()
