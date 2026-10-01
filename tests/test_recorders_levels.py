"""Server side of the idle level preview: the watch lease, capability gating and ``levels`` frames.

Real app under uvicorn (see ``test_recorders_server``); recorders and pages are websocket clients. A
recorder only ever hears ``watch`` if it advertised ``idle_levels`` in its hello, and only while a page
that says it is visible keeps renewing its own lease.
"""

from __future__ import annotations

import json
import time

import pytest
from websockets.exceptions import ConnectionClosed

from meeting_notes import remote
from tests.test_recorders_server import hello_frame, start, wait_until

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

CAPS = [remote.CAP_IDLE_LEVELS]


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture()
def env(tmp_path, monkeypatch):
    server, e = start(tmp_path, monkeypatch)
    e.hub.command_timeout = 1.0
    e.clock = Clock()
    e.hub.clock = e.clock
    e.hub.stale_after = 10**6      # the fake clock must not make recorders look dead
    yield e
    server.stop()


def recv_json(ws, timeout=2.0):
    return json.loads(ws.recv(timeout=timeout))


def next_of(ws, kind, timeout=3.0, skip=("ping",)):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            frame = recv_json(ws, timeout=max(0.05, end - time.monotonic()))
        except TimeoutError:
            break
        if frame.get("type") == kind:
            return frame
        assert frame.get("type") in skip or frame.get("type") in ("upsert", "snapshot", "remove", "levels", "watch"), frame
    raise AssertionError(f"no {kind} frame")


def no_frame(ws, kind="watch", wait=0.4):
    end = time.monotonic() + wait
    while time.monotonic() < end:
        try:
            frame = recv_json(ws, timeout=max(0.05, end - time.monotonic()))
        except TimeoutError:
            return
        assert frame.get("type") != kind, frame


def page_beat(page, visible=True):
    page.send(json.dumps({"type": "watch", "visible": visible}))


def watch_value(ws):
    return next_of(ws, "watch")["levels"]


# -- the lease ---------------------------------------------------------------------------------------


def test_recorder_is_asked_for_levels_only_while_a_page_is_looking(env):
    rec = env.recorder(caps=CAPS)
    wait_until(lambda: env.hub.list_items())
    no_frame(rec)                                   # nobody on the Recorders page yet
    page = env.events()
    assert watch_value(rec) is True                 # a page subscribed: levels wanted
    page.close()
    assert watch_value(rec) is False                # the last page left


def test_a_page_that_hides_its_tab_stops_the_stream_and_resumes_when_shown(env):
    rec = env.recorder(caps=CAPS)
    page = env.events()
    assert watch_value(rec) is True
    page_beat(page, visible=False)
    assert watch_value(rec) is False
    page_beat(page, visible=True)
    assert watch_value(rec) is True
    page.close()


def test_two_pages_keep_it_on_until_the_last_one_goes(env):
    rec = env.recorder(caps=CAPS)
    first, second = env.events(), env.events()
    assert watch_value(rec) is True
    first.close()
    wait_until(lambda: len(env.hub._subs) == 1)
    no_frame(rec)                                   # one viewer is still looking
    second.close()
    assert watch_value(rec) is False


def test_the_viewer_lease_times_out_and_live_ones_are_renewed(env):
    rec = env.recorder(caps=CAPS)
    page = env.events()
    assert watch_value(rec) is True
    # Renewed on the sweep once WATCH_REFRESH has passed (the recorder's own lease is longer).
    env.clock.now += remote.WATCH_REFRESH + 1
    page_beat(page)                                  # the page is alive and keeps beating
    assert watch_value(rec) is True                  # (the beat itself renews the recorder's lease)
    env.clock.now += remote.WATCH_REFRESH + 1
    env.hub.sweep()
    assert watch_value(rec) is True
    # The page goes silent (tab frozen, network gone) without closing: it expires, levels stop.
    env.clock.now += remote.VIEWER_TTL + 1
    env.hub.sweep()
    assert watch_value(rec) is False
    page.close()


def test_a_recorder_that_connects_while_a_page_is_open_is_asked_at_once(env):
    page = env.events()
    wait_until(lambda: len(env.hub._subs) == 1)
    rec = env.recorder(caps=CAPS)
    assert watch_value(rec) is True
    page.close()


# -- capability gating ------------------------------------------------------------------------------------


def test_a_recorder_without_the_capability_is_never_sent_a_watch(env):
    old = env.recorder()                             # no caps: every recorder before 0.7.7
    page = env.events()
    wait_until(lambda: len(env.hub._subs) == 1)
    page_beat(page, visible=False)
    page_beat(page, visible=True)
    env.clock.now += remote.WATCH_REFRESH + 1
    env.hub.sweep()
    no_frame(old, wait=0.5)
    page.close()
    no_frame(old, wait=0.3)
    assert old.protocol.state.name == "OPEN"
    assert all(not r.watching for r in env.hub._items.values())


def test_unknown_capabilities_are_ignored(env):
    rec = env.recorder(caps=["teleport", 7, None, remote.CAP_IDLE_LEVELS, remote.CAP_IDLE_LEVELS])
    wait_until(lambda: env.hub.list_items())
    assert env.hub.get(rec.instance_id).caps == (remote.CAP_IDLE_LEVELS,)
    junk = env.recorder(caps="idle_levels")          # not a list
    assert env.hub.get(junk.instance_id).caps == ()


# -- levels frames -----------------------------------------------------------------------------------------


def test_levels_frames_update_the_state_and_reach_pages_compactly(env):
    rec = env.recorder(caps=CAPS)
    page = env.events()
    next_of(page, "snapshot")
    assert watch_value(rec) is True
    rec.send(json.dumps({"type": "levels", "mic": 0.25, "system": 0.5}))
    frame = next_of(page, "levels")
    assert frame == {"type": "levels", "instance_id": rec.instance_id, "tracks": {"mic": 0.25, "system": 0.5}}
    assert len(json.dumps(frame)) < 120              # tiny
    tracks = env.hub.get(rec.instance_id).state["tracks"]
    assert tracks["mic"]["level"] == 0.25 and tracks["system"]["peak"] == 0.5
    page.close()


def test_levels_are_clamped_and_a_missing_track_is_left_alone(env):
    rec = env.recorder(caps=CAPS)
    page = env.events()
    watch_value(rec)
    rec.send(json.dumps({"type": "levels", "mic": 7, "system": "loud", "junk": 1}))
    frame = next_of(page, "levels")
    assert frame["tracks"] == {"mic": 1.0, "system": 0.0}
    env.clock.now += 1
    rec.send(json.dumps({"type": "levels", "system": 0.4}))
    assert next_of(page, "levels")["tracks"] == {"system": 0.4}
    assert env.hub.get(rec.instance_id).state["tracks"]["mic"]["level"] == 1.0
    page.close()


def test_levels_are_throttled_on_the_server_and_ignored_while_recording(env):
    rec = env.recorder(caps=CAPS)
    page = env.events()
    watch_value(rec)
    env.clock.now += 1
    rec.send(json.dumps({"type": "levels", "mic": 0.1}))
    next_of(page, "levels")
    rec.send(json.dumps({"type": "levels", "mic": 0.9}))     # same instant: dropped
    no_frame(page, "levels", wait=0.4)
    assert env.hub.get(rec.instance_id).state["tracks"]["mic"]["level"] == 0.1
    # While recording, levels travel in the state snapshot, so a late idle frame is ignored.
    rec.send(json.dumps({"type": "state", "state": {"status": "recording"}}))
    wait_until(lambda: env.hub.get(rec.instance_id).state["status"] == "recording")
    env.clock.now += 1
    rec.send(json.dumps({"type": "levels", "mic": 0.7}))
    no_frame(page, "levels", wait=0.4)
    assert env.hub.get(rec.instance_id).state["tracks"]["mic"]["level"] == 0.0   # the snapshot's, not 0.7
    page.close()


def test_a_flood_of_levels_frames_does_not_get_a_recorder_dropped(env):
    rec = env.recorder(caps=CAPS)
    for i in range(remote.MAX_MESSAGE_BYTES // 100):      # far more than MAX_JUNK_FRAMES
        rec.send(json.dumps({"type": "levels", "mic": (i % 10) / 10}))
    time.sleep(0.2)
    assert env.hub.get(rec.instance_id) is not None
    rec.send(json.dumps({"type": "state", "state": {"status": "idle"}}))
    time.sleep(0.1)
    assert env.hub.get(rec.instance_id) is not None


def test_stored_idle_levels_are_cleared_when_the_last_page_leaves(env):
    rec = env.recorder(caps=CAPS)
    page = env.events()
    watch_value(rec)
    rec.send(json.dumps({"type": "levels", "mic": 0.8, "system": 0.6}))
    wait_until(lambda: env.hub.get(rec.instance_id).state["tracks"]["mic"]["level"] == 0.8)
    page.close()
    assert watch_value(rec) is False
    tracks = env.hub.get(rec.instance_id).state["tracks"]
    assert tracks["mic"]["level"] == 0.0 and tracks["system"]["level"] == 0.0   # the next page starts quiet


def test_preview_field_in_the_state_is_normalized(env):
    rec = env.recorder(caps=CAPS, state={"status": "idle", "preview": {"supported": True, "active": "yes", "tracks": ["mic", "x"]}})
    item = wait_until(lambda: env.hub.list_items())[0]
    assert item["state"]["preview"] == {"supported": True, "active": False, "tracks": ["mic"]}
    old = env.recorder(state={"status": "idle"})
    items = {i["instance_id"]: i for i in env.hub.list_items()}
    assert items[old.instance_id]["state"]["preview"] == {"supported": False, "active": False, "tracks": ["mic", "system"]}


def test_a_closed_recorder_socket_does_not_break_the_watch_loop(env):
    rec = env.recorder(caps=CAPS)
    other = env.recorder(caps=CAPS)
    page = env.events()
    watch_value(other)
    rec.close()
    wait_until(lambda: len(env.hub.list_items()) == 1)
    env.clock.now += remote.WATCH_REFRESH + 1
    env.hub.sweep()
    assert watch_value(other) is True
    page.close()
