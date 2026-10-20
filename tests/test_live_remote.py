"""Live transcription on a GPU helper: the /v1/compute/live socket, LiveRemote and LivePreview's remote hook."""

from __future__ import annotations

import concurrent.futures as cf
import json
import threading
import time
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from meeting_notes import wire
from meeting_notes.server import compute_api
from meeting_notes.server import live as live_mod
from meeting_notes.server import live_remote as lr
from meeting_notes.server.compute import HelperRegistry
from meeting_notes.transcribe.protocol import Segment

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated:DeprecationWarning"
)

PCM = b"\x01\x00" * 16000  # one second of int16 mono at 16 kHz


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class Rig:
    def __init__(self, tmp_path, **settings):
        self.settings = types.SimpleNamespace(
            compute_offload_enabled=True, live_on_helper=True, gpu_live_model="small.en", **settings
        )
        self.clock = FakeClock()
        self.remote = lr.LiveRemote(lambda: self.settings, clock=self.clock)
        self.registry = HelperRegistry(tmp_path)
        self.app = FastAPI()
        self.app.include_router(compute_api.router)
        self.app.state.compute_registry = self.registry
        self.app.state.live_remote = self.remote
        self.client = TestClient(self.app)

    def key(self, name="PC", instance="inst-1"):
        made = self.registry.create_helper(name, instance)
        return made["helper_id"], made["key"]

    def connect(self, key):
        return self.client.websocket_connect("/v1/compute/live", headers={"Authorization": f"Bearer {key}"})

    def wait(self, predicate, seconds=3.0):
        end = time.time() + seconds
        while time.time() < end:
            if predicate():
                return
            time.sleep(0.01)
        raise AssertionError("condition not reached")


def ok_reply(frame):
    return [{"type": "result", "id": frame["id"], "segments": [{"start": 0.1, "end": 0.9, "text": "hello there"}], "device": "cuda"}]


class FakeHelper:
    """The helper side: connects, says hello, and answers each utterance with ``behavior(frame)`` (a list of replies)."""

    def __init__(self, rig: Rig, key: str, behavior=ok_reply, models=("small.en",), delay=0.0):
        self.rig, self.key, self.behavior, self.delay = rig, key, behavior, delay
        self.models = list(models)
        self.frames: list = []
        self._cm = None
        self.ws = None
        self._thread = None

    def __enter__(self):
        self._cm = self.rig.connect(self.key)
        self.ws = self._cm.__enter__()
        self.ws.send_text(json.dumps({"type": "hello", "models": self.models, "gpu": {"name": "RTX"}, "pack_version": "p1"}))
        self.rig.wait(lambda: self.rig.remote.enabled())
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def _serve(self):
        try:
            while True:
                frame = json.loads(self.ws.receive_text())
                if frame.get("type") != "utterance":
                    continue
                self.frames.append(frame)
                if self.delay:
                    time.sleep(self.delay)
                for reply in self.behavior(frame) or []:
                    self.ws.send_text(json.dumps(reply))
        except Exception:  # noqa: BLE001 - socket closed
            return

    def __exit__(self, *exc):
        try:
            self._cm.__exit__(*exc)
        except Exception:  # noqa: BLE001
            pass
        return False


def call(remote, **kw):
    return remote.transcribe(PCM, 16000, kw.pop("track", "mic"), kw.pop("model", "small.en"), **kw)


def in_thread(fn, *args, **kw):
    ex = cf.ThreadPoolExecutor(max_workers=1)
    fut = ex.submit(fn, *args, **kw)
    ex.shutdown(wait=False)
    return fut


# -- the socket ----------------------------------------------------------------


def test_transcribes_on_the_helper_and_reports_presence(tmp_path):
    rig = Rig(tmp_path)
    helper_id, key = rig.key()
    with FakeHelper(rig, key) as h:
        segments = call(rig.remote, timeout=3)
        assert [(s.start, s.end, s.text, s.track) for s in segments] == [(0.1, 0.9, "hello there", "mic")]
        frame = h.frames[0]
        assert frame["model"] == "small.en" and frame["track"] == "mic" and frame["sample_rate"] == 16000
        assert frame["deadline"] > time.time() and frame["budget_ms"] == 3000 and len(frame["pcm_b64"]) > 100
        live = {r["helper_id"]: r for r in rig.registry.list_helpers()}[helper_id]["live"]
        assert live["connected"] and live["ready"] and live["served"] == 1 and live["last_error"] is None
        assert live["avg_ms"] is not None
    rig.wait(lambda: not rig.remote.enabled())
    assert rig.registry.list_helpers()[0]["live"]["connected"] is False


def test_socket_refused_before_accept_without_a_valid_key(tmp_path):
    rig = Rig(tmp_path)
    helper_id, key = rig.key()
    with pytest.raises(WebSocketDisconnect) as e:
        with rig.client.websocket_connect("/v1/compute/live"):
            pass
    assert e.value.code == 4401
    with pytest.raises(WebSocketDisconnect) as e:
        with rig.client.websocket_connect("/v1/compute/live", headers={"Authorization": "Bearer mnh_nope"}):
            pass
    assert e.value.code == 4403
    with pytest.raises(WebSocketDisconnect) as e:  # the key in the URL is not accepted
        with rig.client.websocket_connect(f"/v1/compute/live?key={key}"):
            pass
    assert e.value.code == 4401
    rig.registry.revoke_helper(helper_id)
    with pytest.raises(WebSocketDisconnect) as e:
        with rig.connect(key):
            pass
    assert e.value.code == 4401
    assert not rig.remote.enabled()


def test_bad_hello_is_closed(tmp_path):
    rig = Rig(tmp_path)
    _, key = rig.key()
    with rig.connect(key) as ws:
        ws.send_text(json.dumps({"type": "hello", "models": "small.en"}))
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_text()
    assert e.value.code == 4400


def test_oversized_frame_closes_the_socket(tmp_path):
    rig = Rig(tmp_path)
    _, key = rig.key()
    with FakeHelper(rig, key) as h:
        h.ws.send_text("x" * (lr.MAX_FRAME_BYTES + 1))
        rig.wait(lambda: not rig.remote.enabled())


def test_revoke_closes_the_live_socket_and_fails_pending(tmp_path):
    rig = Rig(tmp_path)
    helper_id, key = rig.key()
    with FakeHelper(rig, key, behavior=lambda f: None) as h:
        fut = in_thread(call, rig.remote, timeout=5)
        rig.wait(lambda: h.frames)
        started = time.time()
        rig.registry.revoke_helper(helper_id)
        assert fut.result(timeout=3) is None and time.time() - started < 2
        assert not rig.remote.enabled()


def test_replacing_a_key_closes_the_old_live_socket(tmp_path):
    rig = Rig(tmp_path)
    _, key = rig.key("PC", "inst-1")
    with FakeHelper(rig, key):
        rig.key("PC", "inst-1")  # same instance id: the old key is revoked
        rig.wait(lambda: not rig.remote.enabled())


def test_a_connection_drop_fails_pending_at_once(tmp_path):
    rig = Rig(tmp_path)
    _, key = rig.key()
    helper = FakeHelper(rig, key, behavior=lambda f: None)
    with helper:
        fut = in_thread(call, rig.remote, timeout=5)
        rig.wait(lambda: helper.frames)
        started = time.time()
    assert fut.result(timeout=3) is None and time.time() - started < 2


# -- timeouts, errors, validation ----------------------------------------------


def test_a_slow_helper_times_out_and_its_late_result_is_ignored(tmp_path):
    rig = Rig(tmp_path)
    helper_id, key = rig.key()
    with FakeHelper(rig, key, delay=0.5) as h:
        assert call(rig.remote, timeout=0.2) is None
        live = rig.remote.stats(helper_id)
        assert live["served"] == 0 and live["last_error"] == "timed out"
        rig.wait(lambda: h.frames)
        time.sleep(0.6)  # the late result arrives meanwhile; nothing is waiting for it
        assert rig.remote.stats(helper_id)["served"] == 0
        h.delay = 0
        assert call(rig.remote, timeout=3)[0].text == "hello there"
        assert rig.remote.stats(helper_id)["served"] == 1


def test_unknown_ids_are_ignored(tmp_path):
    rig = Rig(tmp_path)
    _, key = rig.key()

    def noisy(frame):
        return [
            {"type": "result", "id": "nope", "segments": []},
            {"type": "error", "id": "nope", "error": "x"},
            {"type": "mystery"},
            *ok_reply(frame),
        ]

    with FakeHelper(rig, key, behavior=noisy):
        assert call(rig.remote, timeout=3)[0].text == "hello there"


def test_a_result_from_another_connection_is_ignored(tmp_path):
    rig = Rig(tmp_path)
    _, key_a = rig.key("A", "a")
    _, key_b = rig.key("B", "b")
    with FakeHelper(rig, key_a, behavior=lambda f: None) as a:
        fut = in_thread(call, rig.remote, timeout=0.8)
        rig.wait(lambda: a.frames)
        with FakeHelper(rig, key_b) as b:
            b.ws.send_text(json.dumps({"type": "result", "id": a.frames[0]["id"], "segments": [{"start": 0, "end": 1, "text": "spoof"}]}))
            assert fut.result(timeout=3) is None


def test_busy_and_expired_do_not_count_toward_the_cooldown(tmp_path):
    rig = Rig(tmp_path)
    helper_id, key = rig.key()
    with FakeHelper(rig, key, behavior=lambda f: [{"type": "error", "id": f["id"], "error": "busy"}]) as h:
        for _ in range(5):
            assert call(rig.remote, timeout=2) is None
        assert len(h.frames) == 5  # still being asked
        assert rig.remote.stats(helper_id)["last_error"] == "busy" and rig.remote.stats(helper_id)["ready"]


def test_three_failures_put_a_helper_in_cooldown_for_a_minute(tmp_path):
    rig = Rig(tmp_path)
    helper_id, key = rig.key()
    with FakeHelper(rig, key, behavior=lambda f: [{"type": "error", "id": f["id"], "error": "boom"}]) as h:
        for _ in range(3):
            assert call(rig.remote, timeout=2) is None
        assert rig.remote.stats(helper_id)["ready"] is False
        started = time.time()
        assert call(rig.remote, timeout=2) is None and time.time() - started < 0.5
        assert len(h.frames) == 3  # the fourth never reached it
        rig.clock.now += lr.COOLDOWN_SECONDS + 1
        assert rig.remote.stats(helper_id)["ready"] is True
        h.behavior = ok_reply
        assert call(rig.remote, timeout=2)[0].text == "hello there"
        assert len(h.frames) == 4


def test_round_robin_across_helpers(tmp_path):
    rig = Rig(tmp_path)
    _, key_a = rig.key("A", "a")
    _, key_b = rig.key("B", "b")
    with FakeHelper(rig, key_a) as a, FakeHelper(rig, key_b) as b:
        for _ in range(4):
            assert call(rig.remote, timeout=3)
        assert len(a.frames) == 2 and len(b.frames) == 2


def test_a_cooling_helper_is_skipped_for_the_other(tmp_path):
    rig = Rig(tmp_path)
    _, key_a = rig.key("A", "a")
    _, key_b = rig.key("B", "b")
    bad = lambda f: [{"type": "error", "id": f["id"], "error": "boom"}]  # noqa: E731
    with FakeHelper(rig, key_a, behavior=bad) as a, FakeHelper(rig, key_b) as b:
        results = [call(rig.remote, timeout=3) for _ in range(12)]
        assert len(a.frames) == 3  # cooled down after three failures
        assert sum(r is not None for r in results) == len(b.frames) == 9


def test_at_most_eight_requests_pending_per_connection(tmp_path):
    rig = Rig(tmp_path)
    _, key = rig.key()
    with FakeHelper(rig, key, behavior=lambda f: None) as h:
        futs = [in_thread(call, rig.remote, timeout=2) for _ in range(lr.MAX_PENDING)]
        rig.wait(lambda: len(h.frames) == lr.MAX_PENDING)
        started = time.time()
        assert call(rig.remote, timeout=2) is None and time.time() - started < 0.5
        assert len(h.frames) == lr.MAX_PENDING
        assert [f.result(timeout=5) for f in futs] == [None] * lr.MAX_PENDING


def test_a_model_the_helper_lacks_is_not_requested_from_it(tmp_path):
    rig = Rig(tmp_path)
    _, key = rig.key()
    with FakeHelper(rig, key, models=("tiny.en",)) as h:
        assert call(rig.remote, model="small.en", timeout=1) is None
        assert h.frames == []
        assert call(rig.remote, model="tiny.en", timeout=3)


@pytest.mark.parametrize(
    "segments,ok",
    [
        ([{"start": 0.0, "end": 1.0, "text": "fine"}], True),
        ([], True),
        ([{"start": 0.0, "end": 2.0, "text": "within the one second slack"}], True),  # utterance is 1.0 s
        ([{"start": 0.0, "end": 2.5, "text": "too long"}], True),  # end trimmed to 2.0 (seen live with large models)
        ([{"start": -0.1, "end": 0.5, "text": "negative"}], False),
        ([{"start": 0.5, "end": 0.2, "text": "backwards"}], False),
        ([{"start": float("nan"), "end": 0.5, "text": "nan"}], False),
        ([{"start": 0.5, "end": 0.9, "text": "a"}, {"start": 0.1, "end": 0.2, "text": "unsorted"}], False),
        ([{"start": 0.0, "end": 0.5, "text": "x" * 4001}], False),
        ([{"start": 0.0, "end": 0.5, "text": 7}], False),
        (["nope"], False),
        ([{"start": 0.0, "end": 0.5, "text": "t"}] * 201, False),
        ("segments", False),
    ],
)
def test_result_validation(segments, ok):
    if ok:
        out = lr.LiveRemote._validate(segments, 1.0, "mic")
        assert len(out) == len(segments) and all(isinstance(s, Segment) and s.track == "mic" for s in out)
        assert all(s.end <= 2.0 for s in out)
    else:
        with pytest.raises(ValueError):
            lr.LiveRemote._validate(segments, 1.0, "mic")


def test_an_invalid_result_over_the_socket_falls_back(tmp_path):
    rig = Rig(tmp_path)
    helper_id, key = rig.key()
    bad = lambda f: [{"type": "result", "id": f["id"], "segments": [{"start": 0, "end": 1, "text": 7}]}]  # noqa: E731
    with FakeHelper(rig, key, behavior=bad):
        assert call(rig.remote, timeout=2) is None
        assert rig.remote.stats(helper_id)["last_error"].startswith("invalid result")


def test_segments_past_the_utterance_are_dropped_not_the_whole_result():
    # large-v3-turbo sometimes predicts a timestamp past the audio it was given; keep the good segments.
    out = lr.LiveRemote._validate(
        [{"start": 0.0, "end": 1.2, "text": "kept"}, {"start": 1.5, "end": 29.0, "text": "hallucinated"}], 1.0, "mic")
    assert [s.text for s in out] == ["kept"] and out[0].end == 1.2


# -- settings gating ---------------------------------------------------------------


def test_enabled_and_model_follow_the_settings(tmp_path):
    rig = Rig(tmp_path)
    assert not rig.remote.enabled()  # no connection
    _, key = rig.key()
    with FakeHelper(rig, key):
        assert rig.remote.enabled() and rig.remote.model() == "small.en"
        rig.settings.live_on_helper = False
        assert not rig.remote.enabled()
        rig.settings.live_on_helper, rig.settings.compute_offload_enabled = True, False
        assert not rig.remote.enabled()
        rig.settings.compute_offload_enabled, rig.settings.gpu_live_model = True, "large-v3-turbo"
        assert rig.remote.enabled() and rig.remote.model() == "large-v3-turbo"
        rig.settings.gpu_live_model = ""
        assert rig.remote.model() == "small.en"
        # missing attributes behave as the defaults (on)
        rig.remote.settings_provider = lambda: types.SimpleNamespace()
        assert rig.remote.enabled() and rig.remote.model() == "small.en"


# -- LivePreview ------------------------------------------------------------------


class FakeRemote:
    def __init__(self, result=None, enabled=True, block: threading.Event = None):
        self.result, self._enabled, self.block = result, enabled, block
        self.calls = []
        self.waiting = threading.Event()

    def enabled(self):
        return self._enabled

    def model(self):
        return "small.en"

    def transcribe(self, pcm, sample_rate, track, model, timeout=2.5):
        self.calls.append((len(pcm), sample_rate, track, model))
        self.waiting.set()
        if self.block is not None:
            self.block.wait(5)
        return self.result


class LocalTranscriber:
    def __init__(self):
        self.calls = 0

    def transcribe(self, wav_path, track):
        self.calls += 1
        return [Segment(start=0.0, end=1.0, text="local text", track=track)]


def make_preview(monkeypatch, remote, seconds=3):
    local = LocalTranscriber()
    preview = live_mod.LivePreview(lambda: local, remote=remote)
    pcm = b"\x01\x00" * int(16000 * seconds)
    monkeypatch.setattr(live_mod._TrackBuffer, "take_mature_chunks", lambda self, m: [(32000, 32000 + len(pcm) // 2, pcm)])
    preview.feed("s1", "mic", pcm)
    return preview, local


def test_poll_uses_the_helper_result_and_applies_the_filters(monkeypatch):
    remote = FakeRemote(
        [
            Segment(0.5, 1.5, "  from the gpu  ", "mic"),
            Segment(1.0, 1.2, ".", "mic"),  # hallucinated punctuation
            Segment(9.0, 9.5, "past the end of the audio", "mic"),
        ]
    )
    preview, local = make_preview(monkeypatch, remote)
    partials = preview.poll("s1", "mic")
    assert local.calls == 0 and remote.calls and remote.calls[0][2:] == ("mic", "small.en")
    assert [(p.track, p.start, p.end, p.text) for p in partials] == [("mic", 2.5, 3.5, "from the gpu")]


def test_poll_falls_back_to_the_local_model_when_the_helper_gives_nothing(monkeypatch):
    preview, local = make_preview(monkeypatch, FakeRemote(None))
    partials = preview.poll("s1", "mic")
    assert local.calls == 1 and [p.text for p in partials] == ["local text"]


def test_poll_stays_local_when_remote_is_disabled_or_absent(monkeypatch):
    remote = FakeRemote([Segment(0, 1, "gpu", "mic")], enabled=False)
    preview, local = make_preview(monkeypatch, remote)
    assert [p.text for p in preview.poll("s1", "mic")] == ["local text"] and remote.calls == []
    preview2, local2 = make_preview(monkeypatch, None)
    assert [p.text for p in preview2.poll("s1", "mic")] == ["local text"]


def test_poll_survives_a_remote_that_raises(monkeypatch):
    class Boom(FakeRemote):
        def transcribe(self, *a, **k):
            raise RuntimeError("loop closed")

    preview, local = make_preview(monkeypatch, Boom())
    assert [p.text for p in preview.poll("s1", "mic")] == ["local text"]


def test_the_model_lock_is_free_during_the_remote_wait(monkeypatch):
    release = threading.Event()
    remote = FakeRemote([Segment(0, 1, "gpu", "mic")], block=release)
    preview, _ = make_preview(monkeypatch, remote)
    fut = in_thread(preview.poll, "s1", "mic")
    assert remote.waiting.wait(3)
    assert preview._lock.acquire(timeout=1), "the model lock must not be held while waiting on a helper"
    preview._lock.release()
    release.set()
    assert [p.text for p in fut.result(timeout=3)] == ["gpu"]


def test_only_one_remote_request_per_session_track_in_flight(monkeypatch):
    release = threading.Event()
    remote = FakeRemote([Segment(0, 1, "gpu", "mic")], block=release)
    preview, _ = make_preview(monkeypatch, remote)
    first = in_thread(preview._transcribe_remote, "s1", "mic", PCM)
    assert remote.waiting.wait(3)
    assert preview._transcribe_remote("s1", "mic", PCM) is None  # a second one for the same track goes local
    assert len(remote.calls) == 1
    other = in_thread(preview._transcribe_remote, "s1", "system", PCM)  # another track is independent
    rig_end = time.time() + 3
    while len(remote.calls) < 2 and time.time() < rig_end:
        time.sleep(0.01)
    assert len(remote.calls) == 2
    release.set()
    assert first.result(timeout=3) and other.result(timeout=3)
    assert preview._remote_inflight == set()


def test_live_preview_with_a_real_remote_end_to_end(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    _, key = rig.key()
    local = LocalTranscriber()
    preview = live_mod.LivePreview(lambda: local, remote=rig.remote)
    pcm = b"\x01\x00" * 48000
    monkeypatch.setattr(live_mod._TrackBuffer, "take_mature_chunks", lambda self, m: [(0, 48000, pcm)])
    preview.feed("s1", "mic", pcm)
    with FakeHelper(rig, key):
        partials = preview.poll("s1", "mic")
    assert [p.text for p in partials] == ["hello there"] and local.calls == 0
    assert isinstance(partials[0], wire.Partial)
