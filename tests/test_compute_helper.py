"""GPU helper runtime: poll / lease / heartbeat / complete against a fake server and a fake engine.

No GPU, models, torch or faster-whisper are needed.
"""

from __future__ import annotations

import io
import json
import threading
import time

import httpx
import pytest

from meeting_notes.compute_helper import gpu as gpu_mod
from meeting_notes.compute_helper.ipc import Ipc
from meeting_notes.compute_helper import worker as worker_mod
from meeting_notes.compute_helper.worker import Worker, WorkerConfig

KEY = "mnh_secret-test-key"


def _lease(**overrides):
    lease = {
        "job_id": "j1", "attempt": "a1", "lease": "L1", "session_id": "s1", "board": "M-0142",
        "tracks": [{"name": "mic", "seconds": 10.0, "url": "/v1/compute/jobs/j1/audio/mic"},
                   {"name": "system", "seconds": 30.0, "url": "/v1/compute/jobs/j1/audio/system"}],
        "whisper": {"model": "small.en", "beam_size": 5, "hotwords": None, "language": None},
        "diarization": {"enabled": True, "model": "x", "min_speakers": 1, "max_speakers": 8},
        "heartbeat_seconds": 0.02, "deadline_seconds": 100,
    }
    lease.update(overrides)
    return lease


class FakeServer:
    def __init__(self, leases=None, *, outdated=False, heartbeat_status=200, complete_status=200,
                 audio_status=200):
        self.leases = list(leases or [])
        self.outdated = outdated
        self.heartbeat_status = heartbeat_status
        self.complete_status = complete_status
        self.audio_status = audio_status
        self.calls = []  # (method, path, json body or None)
        self.polls = []
        self.audio_headers = []
        self.auth = set()
        self.lock = threading.Lock()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        with self.lock:
            self.calls.append((request.method, request.url.path, body))
            self.auth.add(request.headers.get("Authorization"))
        path = request.url.path
        if path == "/v1/compute/poll":
            self.polls.append(body)
            if self.leases and not self.outdated and not body["paused"]:
                return httpx.Response(200, json=self.leases.pop(0))
            return httpx.Response(204, headers={"X-Pack-Outdated": "1"} if self.outdated else {})
        if "/audio/" in path:
            self.audio_headers.append((request.headers.get("X-Attempt"), request.headers.get("X-Lease")))
            return httpx.Response(self.audio_status, content=b"RIFF....WAVE")
        if path.endswith("/heartbeat"):
            return httpx.Response(self.heartbeat_status, json={})
        if path.endswith("/complete"):
            return httpx.Response(self.complete_status, json={"error": "bad"} if self.complete_status != 200 else {})
        if path.endswith("/release"):
            return httpx.Response(200, json={})
        return httpx.Response(404)

    def posted(self, suffix):
        return [b for m, p, b in self.calls if m == "POST" and p.endswith(suffix)]


class FakeEngine:
    device_label = "Fake GPU"

    def __init__(self, *, diarize=True, delay=0.0, fail=None, gate=None):
        self._diarize = diarize
        self.delay = delay
        self.fail = fail
        self.gate = gate  # Event the engine waits on, to hold a job open
        self.started = threading.Event()
        self.transcribed = []

    def can_diarize(self):
        return self._diarize

    def transcribe(self, wav, track, whisper, progress=None):
        self.started.set()
        if self.fail:
            raise RuntimeError(self.fail)
        if self.gate is not None:
            for _ in range(500):
                if self.gate.wait(0.01):
                    break
                if progress:
                    progress(0.1)  # lets the worker's abort checks fire
        elif self.delay:
            time.sleep(self.delay)
        assert wav.exists() and wav.read_bytes().startswith(b"RIFF")
        self.transcribed.append((track, whisper["model"]))
        if progress:
            progress(0.5)
            progress(1.0)
        return [{"start": 0.0, "end": 1.0, "text": f"hello from {track}"}]

    def diarize(self, wav, diarization):
        return [{"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00"}]


class Harness:
    def __init__(self, server, engine=None, **config):
        self.server = server
        self.engine = engine or FakeEngine()
        self.out = io.StringIO()
        self.ipc = Ipc(out=self.out, inp=io.StringIO(""))
        cfg = WorkerConfig(server="http://helper.test", key=KEY, pack_version="p1", poll_seconds=0.01,
                           retry_base=0.0, **config)
        client = httpx.Client(base_url="http://helper.test", transport=httpx.MockTransport(server))
        self.worker = Worker(cfg, self.engine, self.ipc, gpu={"name": "Fake GPU", "vram_mb": 8192, "driver": "1"},
                             client=client)
        self.result = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        self.result = self.worker.run()

    def start(self):
        self.thread.start()
        return self

    def wait_for(self, predicate, timeout=5.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(0.005)
        return False

    def stop(self):
        self.worker.stop()
        self.thread.join(timeout=5)
        assert not self.thread.is_alive()

    def events(self):
        return [json.loads(line) for line in self.out.getvalue().splitlines()]

    def states(self):
        return [e["state"] for e in self.events()]


def test_claim_download_heartbeat_complete():
    server = FakeServer([_lease()])
    h = Harness(server, FakeEngine(delay=0.15)).start()
    assert h.wait_for(lambda: server.posted("/complete"))
    h.stop()

    assert server.auth == {f"Bearer {KEY}"}
    assert h.result == 0
    # audio fetched with the attempt + lease headers
    assert server.audio_headers == [("a1", "L1")] * 2
    # heartbeats ran during the job
    beats = server.posted("/heartbeat")
    assert beats and beats[0]["attempt"] == "a1" and beats[0]["lease"] == "L1"
    assert 0.0 <= beats[0]["progress"] <= 1.0 and beats[0]["step"]
    done = server.posted("/complete")[0]
    assert done["attempt"] == "a1" and done["lease"] == "L1"
    assert done["device"] == "Fake GPU"
    assert done["tracks"] == {"mic": [{"start": 0.0, "end": 1.0, "text": "hello from mic"}],
                              "system": [{"start": 0.0, "end": 1.0, "text": "hello from system"}]}
    assert done["speaker_turns"] == [{"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00"}]
    assert {"download", "transcribe", "diarize", "total"} <= set(done["timings"])
    assert h.engine.transcribed == [("mic", "small.en"), ("system", "small.en")]
    # poll reports the pack version and gpu
    assert server.polls[0]["pack_version"] == "p1" and server.polls[0]["gpu"]["name"] == "Fake GPU"
    # status events: working with progress, then back to idle; the key is never emitted
    events = h.events()
    working = [e for e in events if e["state"] == "working"]
    assert working and working[0]["job"] == "j1" and working[0]["board"] == "M-0142"
    assert max(e["progress"] for e in working) == 1.0
    assert events[-1]["state"] == "stopped"
    assert KEY not in h.out.getvalue()


class StagedEngine(FakeEngine):
    """Diarization that reports progress and takes a moment, so heartbeats see the speakers stage."""

    def diarize(self, wav, diarization, progress=None):
        progress(0.5)
        time.sleep(0.15)
        progress(1.0)
        return super().diarize(wav, diarization)


def test_heartbeats_carry_per_stage_progress():
    server = FakeServer([_lease()])
    h = Harness(server, StagedEngine(delay=0.1)).start()
    assert h.wait_for(lambda: server.posted("/complete"))
    h.stop()
    beats = server.posted("/heartbeat")
    transcribing = [b for b in beats if b["stage"] == "transcribing"]
    speakers = [b for b in beats if b["stage"] == "speakers"]
    assert transcribing and speakers
    assert all(b["speakers_progress"] == 0.0 for b in transcribing)
    assert all(b["transcribe_progress"] == 1.0 and b["speakers_progress"] >= 0.5 for b in speakers)
    assert all(b["progress"] >= 1 - worker_mod.DIARIZATION_SHARE for b in speakers)


def test_progress_beats_only_when_progress_moves(monkeypatch):
    """Between lease heartbeats, a beat goes out only when the progress changed."""
    monkeypatch.setattr(worker_mod, "PROGRESS_BEAT_SECONDS", 0.01)
    gate = threading.Event()
    server = FakeServer([_lease(heartbeat_seconds=5)])
    h = Harness(server, FakeEngine(diarize=False, gate=gate)).start()
    assert h.wait_for(lambda: server.posted("/heartbeat"))
    time.sleep(0.3)  # the engine keeps reporting the same 10%: no new beats
    count = len(server.posted("/heartbeat"))
    assert count <= 3
    gate.set()
    assert h.wait_for(lambda: server.posted("/complete"))
    h.stop()
    beats = server.posted("/heartbeat")
    assert all(b["speakers_progress"] is None for b in beats)


def test_no_diarization_when_unavailable_or_off():
    for engine, lease in ((FakeEngine(diarize=False), _lease()),
                          (FakeEngine(), _lease(diarization={"enabled": False}))):
        server = FakeServer([lease])
        h = Harness(server, engine).start()
        assert h.wait_for(lambda: server.posted("/complete"))
        h.stop()
        assert server.posted("/complete")[0]["speaker_turns"] is None


def test_heartbeat_409_aborts_and_discards():
    server = FakeServer([_lease()], heartbeat_status=409)
    gate = threading.Event()
    h = Harness(server, FakeEngine(gate=gate)).start()
    assert h.engine.started.wait(5)
    assert h.wait_for(lambda: server.posted("/heartbeat"))
    # the job unwinds on its own: no result, no release (the server already took it back)
    assert h.wait_for(lambda: h.worker._job is None)
    h.stop()
    assert not server.posted("/complete")
    assert not server.posted("/release")


def test_complete_409_is_discarded_quietly():
    server = FakeServer([_lease()], complete_status=409)
    h = Harness(server).start()
    assert h.wait_for(lambda: server.posted("/complete"))
    assert h.wait_for(lambda: h.worker._job is None)
    h.stop()
    assert not server.posted("/release")


def test_complete_422_releases_the_lease():
    server = FakeServer([_lease()], complete_status=422)
    h = Harness(server).start()
    assert h.wait_for(lambda: server.posted("/release"))
    h.stop()
    assert "rejected" in server.posted("/release")[0]["error"]


def test_audio_409_discards():
    server = FakeServer([_lease()], audio_status=409)
    h = Harness(server).start()
    assert h.wait_for(lambda: server.audio_headers)
    assert h.wait_for(lambda: h.worker._job is None)
    h.stop()
    assert not server.posted("/release") and not server.posted("/complete")


def test_engine_error_releases_and_loop_continues():
    server = FakeServer([_lease(), _lease(job_id="j2", attempt="a2", lease="L2")])
    engine = FakeEngine(fail="CUDA out of memory")
    h = Harness(server, engine).start()
    assert h.wait_for(lambda: len(server.posted("/release")) == 2)
    h.stop()
    first = server.posted("/release")[0]
    assert first["attempt"] == "a1" and first["lease"] == "L1" and "CUDA out of memory" in first["error"]
    assert not server.posted("/complete")


def test_no_work_stays_idle():
    server = FakeServer([])
    h = Harness(server).start()
    assert h.wait_for(lambda: len(server.polls) >= 3)
    h.stop()
    assert "idle" in h.states() and "working" not in h.states()
    assert not server.posted("/heartbeat")


def test_pack_outdated_emits_update_needed_and_takes_no_work():
    server = FakeServer([_lease()], outdated=True)
    h = Harness(server).start()
    assert h.wait_for(lambda: "update needed" in h.states())
    assert h.wait_for(lambda: len(server.polls) >= 2)
    h.stop()
    assert not server.audio_headers and not server.posted("/complete")
    assert "working" not in h.states()


def test_pause_resume_through_ipc():
    server = FakeServer([_lease()])
    h = Harness(server)
    h.worker.command("pause")
    h.start()
    assert h.wait_for(lambda: len(server.polls) >= 2)
    assert all(p["paused"] and p["state"] == "paused" for p in server.polls)
    assert not server.audio_headers and "paused" in h.states()
    h.worker.command("resume")
    assert h.wait_for(lambda: server.posted("/complete"))
    h.stop()
    assert any(not p["paused"] for p in server.polls)


def test_pause_does_not_interrupt_current_job():
    server = FakeServer([_lease()])
    gate = threading.Event()
    h = Harness(server, FakeEngine(gate=gate)).start()
    assert h.engine.started.wait(5)
    h.worker.command("pause")
    gate.set()
    assert h.wait_for(lambda: server.posted("/complete"))
    h.stop()
    assert not server.posted("/release")


def test_stop_releases_current_lease_and_exits():
    server = FakeServer([_lease()])
    gate = threading.Event()  # never set: the job would run for ever
    h = Harness(server, FakeEngine(gate=gate)).start()
    assert h.engine.started.wait(5)
    h.stop()
    assert h.result == 0
    releases = server.posted("/release")
    assert len(releases) == 1 and releases[0]["error"] == "helper stopped"
    assert not server.posted("/complete")


def test_ipc_reader_parses_commands_and_eof_means_stop():
    seen = []
    ipc = Ipc(out=io.StringIO(), inp=io.StringIO('{"cmd":"pause"}\nnot json\n{"cmd":"bogus"}\n{"cmd":"resume"}\n'))
    ipc.start_reader(seen.append).join(timeout=5)
    assert seen == ["pause", "resume", "stop"]


def test_rejected_key_is_fatal():
    class Rejecting(FakeServer):
        def __call__(self, request):
            return httpx.Response(401, json={"error": "nope"})

    h = Harness(Rejecting()).start()
    h.thread.join(timeout=5)
    assert h.result == 2
    last = h.events()[-1]
    assert last["state"] == "error" and "rejected" in last["error"]


def test_connection_errors_never_crash_and_report_offline():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise httpx.ConnectError("down")

    h = Harness(FakeServer(), None)
    h.worker.client = httpx.Client(base_url="http://helper.test", transport=httpx.MockTransport(handler))
    h.start()
    assert h.wait_for(lambda: "offline" in h.states())
    assert h.thread.is_alive()
    h.stop()
    assert calls["n"] >= 1


def test_parse_smi_and_missing_tool(monkeypatch):
    assert gpu_mod.parse_smi("NVIDIA GeForce RTX 4070, 12282, 560.94\n") == {
        "name": "NVIDIA GeForce RTX 4070", "vram_mb": 12282, "driver": "560.94"}
    assert gpu_mod.parse_smi("") is None
    monkeypatch.setattr(gpu_mod, "_find_smi", lambda: None)
    assert gpu_mod.query_gpu() is None



def test_missing_work_dir_is_created(tmp_path):
    # Seen on a real PC: a work dir that didn't exist yet crashed the runtime before the job's error handling.
    work = tmp_path / "not" / "there" / "yet"
    server = FakeServer([_lease()])
    h = Harness(server, work_dir=str(work)).start()
    assert h.wait_for(lambda: server.posted("/complete"))
    h.stop()
    assert work.is_dir() and not server.posted("/release")


def test_a_setup_failure_releases_instead_of_crashing(tmp_path, monkeypatch):
    from meeting_notes.compute_helper import worker as worker_mod

    def failing(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(worker_mod.tempfile, "mkdtemp", failing)
    server = FakeServer([_lease(), _lease(job_id="j2", attempt="a2", lease="L2")])
    h = Harness(server, work_dir=str(tmp_path)).start()
    assert h.wait_for(lambda: len(server.posted("/release")) == 2)
    h.stop()
    assert "disk full" in server.posted("/release")[0]["error"]


def test_control_file_drives_pause_resume_and_stop(tmp_path):
    from meeting_notes.compute_helper.ipc import start_control_watcher, write_control

    path = tmp_path / "control.json"
    seen = []
    thread = start_control_watcher(path, seen.append, interval=0.01)
    write_control(path, paused=True)
    deadline = time.monotonic() + 2
    while "pause" not in seen and time.monotonic() < deadline:
        time.sleep(0.01)
    write_control(path, paused=False)
    while "resume" not in seen and time.monotonic() < deadline:
        time.sleep(0.01)
    write_control(path, paused=False, stop=True)
    thread.join(timeout=2)
    assert seen == ["pause", "resume", "stop"]


def test_runtime_stops_when_its_parent_is_gone(tmp_path):
    from meeting_notes.compute_helper.ipc import start_control_watcher

    seen = []
    start_control_watcher(None, seen.append, parent_pid=12345, interval=0.01, alive=lambda pid: False).join(2)
    assert seen == ["stop"]


def test_process_alive_sees_this_process():
    import os

    from meeting_notes.compute_helper.ipc import process_alive

    assert process_alive(os.getpid())


# -- live transcription client ---------------------------------------------------------------------------

import asyncio  # noqa: E402
import base64  # noqa: E402

import numpy as np  # noqa: E402

from meeting_notes.compute_helper.live import LiveClient, decode_pcm, live_url  # noqa: E402
from meeting_notes.compute_helper.ipc import read_control  # noqa: E402


class FakeLiveEngine:
    def __init__(self, *, models=("small.en",), delay=0.0, fail=None):
        self._models = list(models)
        self.delay = delay
        self.fail = fail
        self.calls = []
        self.unloads = 0
        self.gate = None

    live_device_label = "cuda:Fake (float16)"

    def live_models(self):
        return list(self._models)

    def live_transcribe(self, model, audio, track=""):
        self.calls.append((model, track, audio.dtype, audio.size))
        if self.gate is not None:
            self.gate.wait(5)
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise RuntimeError(self.fail)
        return [{"start": 0.0, "end": 0.5, "text": f"heard {audio.size}"}]

    def live_unload(self):
        self.unloads += 1


class LiveServer:
    """A WebSocket server on localhost that records what the helper sends and pushes utterances."""

    def __init__(self):
        self.messages = []  # frames from the helper
        self.headers = []
        self.connections = 0
        self.sockets = []
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.port = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        assert self.ready.wait(5)

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._serve())

    async def _serve(self):
        from websockets.asyncio.server import serve

        async def handler(ws):
            self.connections += 1
            self.headers.append(ws.request.headers.get("Authorization"))
            self.sockets.append(ws)
            try:
                async for raw in ws:
                    self.messages.append(json.loads(raw))
            finally:
                if ws in self.sockets:
                    self.sockets.remove(ws)

        async with serve(handler, "127.0.0.1", 0) as server:
            self.port = server.sockets[0].getsockname()[1]
            self.stopper = asyncio.Event()
            self.ready.set()
            await self.stopper.wait()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def send(self, payload):
        fut = asyncio.run_coroutine_threadsafe(self.sockets[0].send(json.dumps(payload)), self.loop)
        fut.result(5)

    def drop(self):
        fut = asyncio.run_coroutine_threadsafe(self.sockets[0].close(), self.loop)
        fut.result(5)

    def of_type(self, kind):
        return [m for m in self.messages if m.get("type") == kind]

    def close(self):
        self.loop.call_soon_threadsafe(self.stopper.set)
        self.thread.join(timeout=5)


def _utterance(rid="u1", *, samples=1600, deadline=None, model="small.en"):
    pcm = (np.arange(samples, dtype=np.int16) % 100).tobytes()
    msg = {"type": "utterance", "id": rid, "model": model, "track": "mic", "sample_rate": 16000,
           "pcm_b64": base64.b64encode(pcm).decode()}
    if deadline is not None:
        msg["deadline"] = deadline
    return msg


def _wait(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


@pytest.fixture
def live_server():
    server = LiveServer()
    yield server
    server.close()


def _live(server, engine=None, **kw):
    statuses = []
    client = LiveClient(server.url, KEY, engine or FakeLiveEngine(), pack_version="p9",
                        gpu={"name": "Fake GPU"}, on_status=statuses.append, backoff=(0.05, 0.05), **kw)
    client.statuses = statuses
    return client


def test_live_url_follows_the_server_scheme():
    assert live_url("http://srv:8080/") == "ws://srv:8080/v1/compute/live"
    assert live_url("https://srv.example") == "wss://srv.example/v1/compute/live"


def test_decode_pcm_gives_float32_and_resamples():
    pcm = np.array([0, 16384, -16384, 32767], dtype="<i2").tobytes()
    audio = decode_pcm(base64.b64encode(pcm).decode())
    assert audio.dtype == np.float32 and audio[1] == 0.5 and audio[2] == -0.5
    assert decode_pcm(base64.b64encode(np.zeros(8000, dtype="<i2").tobytes()).decode(), 8000).size == 16000


def test_live_hello_then_utterance_gets_a_result(live_server):
    engine = FakeLiveEngine(models=("small.en", "base.en"))
    client = _live(live_server, engine)
    client.start()
    try:
        assert _wait(lambda: live_server.of_type("hello"))
        hello = live_server.of_type("hello")[0]
        assert hello == {"type": "hello", "models": ["small.en", "base.en"], "gpu": {"name": "Fake GPU"},
                         "pack_version": "p9"}
        assert live_server.headers == [f"Bearer {KEY}"]
        assert _wait(lambda: client.status()["connected"])
        live_server.send(_utterance("u1", samples=3200, deadline=time.time() + 30))
        assert _wait(lambda: live_server.of_type("result"))
        result = live_server.of_type("result")[0]
        assert result["id"] == "u1" and result["device"] == "cuda:Fake (float16)"
        assert result["segments"] == [{"start": 0.0, "end": 0.5, "text": "heard 3200"}]
        assert engine.calls == [("small.en", "mic", np.dtype("float32"), 3200)]
        status = client.status()
        assert status["served"] == 1 and status["avg_ms"] is not None and status["error"] is None
    finally:
        client.stop()


def test_live_drops_expired_requests_without_decoding(live_server):
    engine = FakeLiveEngine()
    client = _live(live_server, engine)
    client.start()
    try:
        assert _wait(lambda: client.status()["connected"])
        live_server.send(_utterance("old", deadline=time.time() - 1))
        assert _wait(lambda: live_server.of_type("error"))
        assert live_server.of_type("error")[0] == {"type": "error", "id": "old", "error": "expired"}
        assert engine.calls == [] and client.status()["served"] == 0
    finally:
        client.stop()


def test_live_a_queued_request_that_expires_while_waiting_is_dropped(live_server):
    engine = FakeLiveEngine()
    engine.gate = threading.Event()
    client = _live(live_server, engine)
    client.start()
    try:
        assert _wait(lambda: client.status()["connected"])
        live_server.send(_utterance("a", deadline=time.time() + 30))
        assert _wait(lambda: engine.calls)
        live_server.send(_utterance("b", deadline=time.time() + 0.2))  # waits in the queue
        time.sleep(0.4)
        engine.gate.set()
        assert _wait(lambda: len(live_server.of_type("result")) == 1 and live_server.of_type("error"))
        assert live_server.of_type("result")[0]["id"] == "a"
        assert live_server.of_type("error")[0] == {"type": "error", "id": "b", "error": "expired"}
        assert len(engine.calls) == 1
    finally:
        engine.gate.set()
        client.stop()


def test_live_replies_busy_when_one_is_already_waiting(live_server):
    engine = FakeLiveEngine()
    engine.gate = threading.Event()
    client = _live(live_server, engine)
    client.start()
    try:
        assert _wait(lambda: client.status()["connected"])
        live_server.send(_utterance("a"))
        assert _wait(lambda: engine.calls)  # a is decoding
        live_server.send(_utterance("b"))  # queued
        live_server.send(_utterance("c"))  # no room
        assert _wait(lambda: live_server.of_type("error"))
        assert live_server.of_type("error")[0] == {"type": "error", "id": "c", "error": "busy"}
        engine.gate.set()
        assert _wait(lambda: len(live_server.of_type("result")) == 2)
        assert [r["id"] for r in live_server.of_type("result")] == ["a", "b"]
    finally:
        engine.gate.set()
        client.stop()


def test_live_reconnects_after_the_server_drops_it(live_server):
    client = _live(live_server)
    client.start()
    try:
        assert _wait(lambda: client.status()["connected"])
        live_server.drop()
        assert _wait(lambda: live_server.connections == 2 and len(live_server.of_type("hello")) == 2)
        assert _wait(lambda: client.status()["connected"])
        live_server.send(_utterance("again"))
        assert _wait(lambda: live_server.of_type("result"))
    finally:
        client.stop()


def test_live_keeps_retrying_while_the_server_is_unreachable():
    client = LiveClient("http://127.0.0.1:1", KEY, FakeLiveEngine(), backoff=(0.05,))
    client.start()
    try:
        time.sleep(0.4)
        assert client.status() == {"connected": False, "served": 0, "avg_ms": None, "error": None}
        assert client._thread.is_alive()
    finally:
        client.stop()


def test_live_other_errors_are_reported_and_live_continues(live_server):
    engine = FakeLiveEngine(fail="boom")
    client = _live(live_server, engine)
    client.start()
    try:
        assert _wait(lambda: client.status()["connected"])
        live_server.send(_utterance("a"))
        assert _wait(lambda: live_server.of_type("error"))
        assert "boom" in live_server.of_type("error")[0]["error"]
        engine.fail = None
        live_server.send(_utterance("b"))
        assert _wait(lambda: live_server.of_type("result"))
        assert client.status()["error"] is None
    finally:
        client.stop()


def test_live_a_model_missing_from_the_pack_is_an_error(live_server):
    client = _live(live_server)
    client.start()
    try:
        assert _wait(lambda: client.status()["connected"])
        live_server.send(_utterance("a", model="large-v3"))
        assert _wait(lambda: live_server.of_type("error"))
        assert "not in the helper pack" in live_server.of_type("error")[0]["error"]
    finally:
        client.stop()


def test_cuda_oom_disables_live_until_restart(live_server):
    engine = FakeLiveEngine(fail="CUDA failed with error out of memory")
    client = _live(live_server, engine)
    client.start()
    try:
        assert _wait(lambda: client.status()["connected"])
        live_server.send(_utterance("a"))
        assert _wait(lambda: live_server.of_type("error"))
        assert live_server.of_type("error")[0]["error"] == "CUDA out of memory"
        assert _wait(lambda: not client.status()["connected"] and client.status()["error"])
        assert "out of memory" in client.status()["error"]
        time.sleep(0.4)  # well past the backoff: it must not come back
        assert live_server.connections == 1 and not client.status()["connected"]
        assert _wait(lambda: engine.unloads >= 1)
    finally:
        client.stop()


def test_pausing_closes_the_live_connection_and_resume_reopens_it(live_server):
    engine = FakeLiveEngine()
    client = _live(live_server, engine)
    client.start()
    try:
        assert _wait(lambda: client.status()["connected"])
        client.set_paused(True)
        assert _wait(lambda: not client.status()["connected"] and not live_server.sockets)
        time.sleep(0.3)
        assert live_server.connections == 1
        assert _wait(lambda: engine.unloads >= 1)
        client.set_paused(False)
        assert _wait(lambda: client.status()["connected"] and live_server.connections == 2)
    finally:
        client.stop()


def test_worker_paused_closes_live_but_recording_keeps_it(live_server):
    server = FakeServer([_lease()])
    engine = FakeLiveEngine()
    live = _live(live_server, engine)
    h = Harness(server)
    h.worker.live = live
    h.worker.command("record")
    h.start()
    try:
        assert _wait(lambda: live.status()["connected"])
        assert h.wait_for(lambda: len(server.polls) >= 2)
        assert all(p["paused"] for p in server.polls) and not server.audio_headers  # batch held
        live_server.send(_utterance("u"))
        assert _wait(lambda: live_server.of_type("result"))  # live still served
        assert "paused" in h.states()
        h.worker.command("pause")
        assert _wait(lambda: not live.status()["connected"])
        h.worker.command("unrecord")
        h.worker.command("resume")
        assert _wait(lambda: live.status()["connected"])
        assert h.wait_for(lambda: server.posted("/complete"))
    finally:
        h.stop()
    assert not live._thread.is_alive()


def test_status_events_carry_the_live_part():
    out = io.StringIO()
    ipc = Ipc(out=out, inp=io.StringIO(""))
    ipc.status("idle")
    ipc.set_live({"connected": True, "served": 2, "avg_ms": 350, "error": None})
    ipc.status("working", job="j", progress=0.5)
    events = [json.loads(line) for line in out.getvalue().splitlines()]
    assert events[0]["live"] == {"connected": False, "served": 0, "avg_ms": None, "error": None}
    assert events[1]["state"] == "idle" and events[1]["live"]["connected"] is True  # re-sent on change
    assert events[2]["live"] == {"connected": True, "served": 2, "avg_ms": 350, "error": None}


def test_control_file_reports_recording_apart_from_paused(tmp_path):
    from meeting_notes.compute_helper.ipc import start_control_watcher, write_control

    path = tmp_path / "control.json"
    seen = []
    thread = start_control_watcher(path, seen.append, interval=0.01)
    deadline = time.monotonic() + 2

    def until(cmd):
        while cmd not in seen and time.monotonic() < deadline:
            time.sleep(0.01)

    write_control(path, paused=False, recording=True)
    until("record")
    write_control(path, paused=True, recording=True)
    until("pause")
    write_control(path, paused=False, recording=False)
    until("unrecord")
    until("resume")
    write_control(path, paused=False, recording=False, stop=True)
    thread.join(timeout=2)
    assert seen[0] == "record" and seen[-1] == "stop"
    assert sorted(seen[1:-1]) == ["pause", "resume", "unrecord"]


def test_an_old_control_file_with_only_paused_still_works(tmp_path):
    from meeting_notes.compute_helper.ipc import start_control_watcher

    path = tmp_path / "control.json"
    path.write_text('{"paused": true, "stop": false}')
    seen = []
    thread = start_control_watcher(path, seen.append, interval=0.01)
    deadline = time.monotonic() + 2
    while not seen and time.monotonic() < deadline:
        time.sleep(0.01)
    path.write_text('{"paused": true, "stop": true}')
    thread.join(timeout=2)
    assert seen == ["pause", "stop"] and read_control(path)["paused"] is True


def test_engine_keeps_live_and_batch_instances_apart(tmp_path):
    from meeting_notes.compute_helper import engine as engine_mod

    (tmp_path / "whisper" / "small.en").mkdir(parents=True)
    eng = engine_mod.Engine(tmp_path)
    assert eng.live_models() == ["small.en"]
    eng._whisper["small.en"] = object()
    eng._live_whisper["small.en"] = object()
    assert eng._whisper["small.en"] is not eng._live_whisper["small.en"]
    eng.last_used -= 10_000
    assert eng.unload_if_idle(600.0) is True
    assert eng._whisper == {} and list(eng._live_whisper) == ["small.en"]  # idle unload is batch only
    eng.live_unload()
    assert eng._live_whisper == {}


def test_live_deadline_uses_the_servers_budget_not_its_clock():
    import types

    from meeting_notes.compute_helper.live import LiveClient

    me = types.SimpleNamespace(_clock=lambda: 1_000_000.0)
    # The server's clock is 10 minutes behind: its absolute deadline is long past here, but 2 s of budget remain.
    message = {"deadline": 999_400.0, "_local_deadline": 1_000_002.0}
    assert LiveClient._expired(me, message) is False
    assert LiveClient._expired(me, {"deadline": 999_400.0}) is True  # old servers: absolute deadline only


def test_a_control_read_that_fails_keeps_the_last_state(tmp_path, monkeypatch):
    """Windows refuses a read while the client replaces the file: that must not look like 'all off'."""
    from meeting_notes.compute_helper import ipc

    reads = iter([{"paused": True, "recording": True}, None, None, {"paused": True, "recording": True},
                  {"paused": True, "recording": True, "stop": True}])
    monkeypatch.setattr(ipc, "_read_control_for_watch", lambda _p: next(reads))
    seen = []
    thread = ipc.start_control_watcher(tmp_path / "control.json", seen.append, interval=0.001)
    thread.join(timeout=2)
    assert seen == ["pause", "record", "stop"]  # no spurious resume / unrecord in between


def test_control_write_retries_while_the_file_is_busy(tmp_path, monkeypatch):
    from meeting_notes.compute_helper import ipc

    real, calls = ipc.os.replace, []

    def flaky(src, dst):
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError(5, "Access is denied")
        return real(src, dst)

    monkeypatch.setattr(ipc.os, "replace", flaky)
    ipc.write_control(tmp_path / "control.json", paused=True)
    assert len(calls) == 3 and ipc.read_control(tmp_path / "control.json")["paused"] is True
