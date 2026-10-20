"""Shared setup for the GPU helper compute tests: an app with a fake clock, a helper client, and job waiting."""

from __future__ import annotations

import dataclasses
import json
import time

from fastapi.testclient import TestClient

from meeting_notes import wire
from meeting_notes.server import settings as settings_mod
from meeting_notes.server.app import create_app
from meeting_notes.transcribe.protocol import Segment
from tests.test_server import _upload_and_finalize, silence_pcm


class FakeTranscriber:
    """Local transcription stub; counts calls so tests can prove a job ran here (or did not)."""

    def __init__(self):
        self.calls = []

    def transcribe(self, wav_path, track):
        self.calls.append(track)
        return [Segment(start=0.0, end=1.0, text=f"local {track}", track=track)]


class Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Rig:
    """One app + test client + fake clock + a registered helper key."""

    def __init__(self, tmp_path, monkeypatch, *, offload=True, transcriber=None, diarizer_factory=None, **settings):
        monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
        self.transcriber = transcriber or FakeTranscriber()
        self.app = create_app(
            transcriber_factory=lambda **_kw: self.transcriber,
            diarizer_factory=diarizer_factory,
            data_root=str(tmp_path / "data"),
        )
        self.client = TestClient(self.app)
        self.store = self.app.state.store
        self.registry = self.app.state.compute_registry
        self.queue = self.app.state.job_queue
        self.clock = Clock()
        self.registry.clock = self.clock
        # Tests drive expiry by hand with registry.sweep(); keep the background sweeper out of the way.
        self.registry.pack_version_provider = None
        self.configure(compute_offload_enabled=offload, **{"model": "small.en", **settings})
        self.helper_headers = self.register("Test PC", "inst-1")

    def configure(self, **fields) -> None:
        current = settings_mod.load_settings(self.store.root)
        settings_mod.save_settings(self.store.root, dataclasses.replace(current, **fields))

    def register(self, name: str, instance_id: str) -> dict:
        resp = self.client.post("/v1/compute/helpers", json={"name": name, "instance_id": instance_id})
        assert resp.status_code == 201, resp.text
        return {"Authorization": f"Bearer {resp.json()['key']}"}

    def poll(self, headers=None, **info):
        body = {"version": "1", "runtime_version": "1", "pack_version": "p1", "state": "idle", "paused": False,
                "gpu": {"name": "RTX 4070", "vram_mb": 12000, "driver": "555"}}
        body.update(info)
        return self.client.post("/v1/compute/poll", json=body, headers=headers or self.helper_headers)

    def new_job(self, session_id="s1", seconds=10.0, tracks=("mic", "system"), settings=None) -> str:
        return _upload_and_finalize(self.client, session_id, {t: silence_pcm(seconds) for t in tracks}, settings)

    def offered_job(self, session_id="s1", **kw) -> str:
        """Upload a session and wait until its job is offered to helpers (a helper must have polled idle first)."""
        assert self.poll().status_code == 204
        job_id = self.new_job(session_id, **kw)
        self.wait_compute(job_id, "offered")
        return job_id

    def claim(self, headers=None) -> dict:
        resp = self.poll(headers)
        assert resp.status_code == 200, resp.text
        return resp.json()

    def lease_headers(self, lease: dict) -> dict:
        return {**self.helper_headers, "X-Attempt": lease["attempt"], "X-Lease": lease["lease"]}

    def body(self, lease: dict, **extra) -> dict:
        return {"attempt": lease["attempt"], "lease": lease["lease"], **extra}

    def good_result(self, lease: dict, **extra) -> dict:
        return self.body(
            lease,
            device="cuda",
            timings={"mic": 1.2},
            tracks={t["name"]: [{"start": 0.0, "end": 1.0, "text": f"gpu {t['name']}"}] for t in lease["tracks"]},
            speaker_turns=None,
            **extra,
        )

    def job(self, job_id: str) -> dict:
        return self.store.read_job(job_id)

    def wait_compute(self, job_id: str, status: str, timeout: float = 5.0) -> dict:
        return self._wait(job_id, lambda j: (j.get("compute") or {}).get("status") == status, f"compute {status}", timeout)

    def wait_state(self, job_id: str, state: str = wire.JobState.DONE, timeout: float = 5.0) -> dict:
        return self._wait(job_id, lambda j: j.get("state") in (state, wire.JobState.ERROR), state, timeout)

    def _wait(self, job_id, predicate, label, timeout) -> dict:
        deadline = time.monotonic() + timeout
        job = {}
        while time.monotonic() < deadline:
            job = self.store.read_job(job_id) or {}
            if predicate(job):
                return job
            time.sleep(0.02)
        raise AssertionError(f"job {job_id} never reached {label}; last seen: {job}")

    def transcript(self, job_id: str) -> dict:
        data = self.client.get(wire.job_transcript_path(job_id)).json()
        return json.loads(data["json"])
