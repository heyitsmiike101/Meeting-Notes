"""GPU helper runtime: poll / lease / heartbeat / complete against a fake server and a fake engine.

No GPU, models, torch or faster-whisper are needed.
"""

from __future__ import annotations

import io
import json
import threading
import time

import httpx

from meeting_notes.compute_helper import gpu as gpu_mod
from meeting_notes.compute_helper.ipc import Ipc
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
