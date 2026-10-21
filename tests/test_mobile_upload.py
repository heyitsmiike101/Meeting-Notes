"""Mobile recorder: upload protocol, state machine and preparation (``meeting_notes/server/mobile_upload.py``).

The media fixtures are generated with ffmpeg at test time (a sine WebM/Opus file and a fragmented MP4/AAC file) and
cut into byte chunks the way MediaRecorder blobs arrive. Real browser captures are a later fixture set.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import wave
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from meeting_notes import timing as timing_mod
from meeting_notes.server import mobile_common as mc
from meeting_notes.server import mobile_upload as mu
from meeting_notes.server import store as store_mod


def _find_ffmpeg():
    found = shutil.which("ffmpeg")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA")
    if local:
        for p in Path(local, "Microsoft", "WinGet", "Packages").glob("**/ffmpeg.exe"):
            os.environ["PATH"] = str(p.parent) + os.pathsep + os.environ.get("PATH", "")
            return str(p)
    return None


FFMPEG = _find_ffmpeg()
pytestmark = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is required")

SECONDS = 6


def _make(path: Path, *args: str) -> bytes:
    subprocess.run(
        [FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={SECONDS}", *args, str(path)],
        check=True,
    )
    return path.read_bytes()


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    d = tmp_path_factory.mktemp("media")
    return {
        "audio/webm": _make(d / "a.webm", "-c:a", "libopus", "-b:a", "32k"),
        "audio/mp4": _make(
            d / "a.mp4", "-c:a", "aac", "-b:a", "32k", "-movflags", "frag_keyframe+empty_moov+default_base_moof"
        ),
    }


def _split(data: bytes, parts: int = 4):
    size = -(-len(data) // parts)
    return [data[i : i + size] for i in range(0, len(data), size)]


class FakeJobs:
    def __init__(self, store):
        self.store = store
        self.calls = []
        self.fail = 0

    def ensure_job(self, sid, settings=None):
        if self.fail:
            self.fail -= 1
            raise RuntimeError("queue unavailable")
        self.calls.append(sid)
        return self.store.create_job(sid, {})


class Env:
    def __init__(self, tmp_path):
        self.store = store_mod.Store(str(tmp_path / "data"))
        self.jobs = FakeJobs(self.store)
        self.install()

    def install(self):
        self.app = FastAPI()
        self.mobile = mu.install_mobile_upload(self.app, store=self.store, job_queue=self.jobs, data_root=None)
        self.client = TestClient(self.app)
        self.origin = (mc.allowed_origins(self.store.root) or ["http://testserver"])[0]
        self.client.headers["Origin"] = self.origin
        return self

    # helpers -----------------------------------------------------------------------------------------------
    def create(self, sid="m1", **kw):
        body = {"session_id": sid, "name": "Standup", "note_type": "general", "device": "iPhone", "started_wall": 1000.0, "live": False}
        body.update(kw)
        return self.client.post("/v1/mobile/sessions", json=body)

    def put(self, sid, run, seq, data, mime="audio/webm"):
        return self.client.put(f"/v1/mobile/sessions/{sid}/runs/{run}/chunks/{seq}", content=data, headers={"Content-Type": mime})

    def upload_run(self, sid, run, data, mime="audio/webm", parts=4):
        chunks = _split(data, parts)
        for i, c in enumerate(chunks):
            assert self.put(sid, run, i, c, mime).status_code == 204
        return len(chunks)

    def finish(self, sid, runs, **kw):
        return self.client.post(f"/v1/mobile/sessions/{sid}/finish", json={"runs": runs, "name": kw.get("name", "Standup"), "note_type": "general"})

    def wait(self, sid, states=(mc.FINALIZED, mc.FAILED), timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            body = self.client.get(f"/v1/mobile/sessions/{sid}").json()
            if body["state"] in states:
                return body
            time.sleep(0.1)
        raise AssertionError(f"timed out in {body}")

    def wav_seconds(self, sid):
        with wave.open(str(self.store.track_wav_path(sid, "system"))) as w:
            assert w.getframerate() == 16000 and w.getnchannels() == 1
            return w.getnframes() / 16000


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.mobile.stop()


def _run(index, count, started_ms=0, mime="audio/webm", state="complete"):
    return {"index": index, "mime": mime, "state": state, "chunk_count": count, "started_ms": started_ms}


# -- create / read --------------------------------------------------------------------------------------------


def test_create_idempotent_and_conflict(env):
    r = env.create()
    assert r.status_code == 201 and r.json()["state"] == "open" and r.json()["runs"] == {}
    st = mc.read_state(env.store, "m1")
    assert st["state"] == "open" and st["bytes"] == 0 and st["platform"] == "iOS"
    assert env.create().status_code == 200
    env.store.write_session_meta("old", {"created": 1, "tracks": {}})
    assert env.create("old").status_code == 409
    assert env.create("bad/id").status_code in (400, 404)
    meta = env.store.read_session_meta("m1")
    assert meta["upload"]["state"] == "uploading"


def test_max_open_sessions(env, monkeypatch):
    monkeypatch.setattr(mu, "MAX_OPEN_SESSIONS", 2)
    assert env.create("a").status_code == 201
    assert env.create("b").status_code == 201
    assert env.create("c").status_code == 507
    assert env.create("a").status_code == 200  # an existing one is still readable
    assert env.client.delete("/v1/mobile/sessions/a").status_code == 204
    assert env.create("c").status_code == 201


def test_unknown_session(env):
    assert env.client.get("/v1/mobile/sessions/nope").status_code == 404
    assert env.put("nope", 0, 0, b"x").status_code == 404


# -- chunks ---------------------------------------------------------------------------------------------------


def test_chunk_put_idempotent_conflict_and_ranges(env):
    env.create()
    assert env.put("m1", 0, 0, b"aaaa").status_code == 204
    assert env.put("m1", 0, 0, b"aaaa").status_code == 204
    assert env.put("m1", 0, 0, b"bbbb").status_code == 409
    for seq in (1, 2, 4):
        assert env.put("m1", 0, seq, b"x" * seq).status_code == 204
    assert env.put("m1", 2, 0, b"z", "audio/mp4").status_code == 204
    body = env.client.get("/v1/mobile/sessions/m1").json()
    assert body["runs"] == {"0": [[0, 2], [4, 4]], "2": [[0, 0]]}
    assert body["bytes"] == 4 + 1 + 2 + 4 + 1
    assert env.store.read_session_meta("m1")["upload"]["bytes_received"] == body["bytes"]
    # the run keeps its first audio type
    assert env.put("m1", 0, 9, b"q", "audio/mp4").status_code == 409
    # parameters are fine
    assert env.put("m1", 0, 10, b"q", "audio/webm;codecs=opus").status_code == 204


def test_chunk_put_validation(env):
    env.create()
    assert env.put("m1", 0, 0, b"x", "text/plain").status_code == 415
    assert env.put("m1", 200, 0, b"x").status_code == 400
    assert env.put("m1", 0, mu.MAX_CHUNKS_PER_RUN, b"x").status_code == 400
    assert env.put("m1", 0, 0, b"").status_code == 400


def test_chunk_quotas_enforced_while_streaming(env, monkeypatch):
    env.create()
    monkeypatch.setattr(mu, "MAX_CHUNK_BYTES", 100)
    assert env.put("m1", 0, 0, b"x" * 101).status_code == 413
    assert env.client.get("/v1/mobile/sessions/m1").json()["bytes"] == 0
    assert not list((mc.mobile_dir(env.store, "m1") / "runs" / "0").glob(".tmp-*"))
    # the quota counts the bytes actually read, not the header
    monkeypatch.setattr(mu, "MAX_STAGING_BYTES", 150)
    assert env.put("m1", 0, 0, b"x" * 100).status_code == 204
    assert env.put("m1", 0, 1, b"y" * 51).status_code == 507
    assert env.put("m1", 0, 1, b"y" * 50).status_code == 204
    env.create("m2")
    assert env.put("m2", 0, 0, b"z").status_code == 507  # staging is global across sessions


def test_not_open_is_409(env, media):
    env.create()
    n = env.upload_run("m1", 0, media["audio/webm"])
    assert env.finish("m1", [_run(0, n)]).status_code == 202
    assert env.put("m1", 0, 99, b"late").status_code == 409
    env.wait("m1")


# -- auth / origin --------------------------------------------------------------------------------------------


def test_origin_required_for_cookie_auth(env, monkeypatch):
    monkeypatch.setenv("MEETING_NOTES_TOKEN", "sekret")
    c = TestClient(env.app)
    c.cookies.set("meeting_notes_token", "sekret")
    body = {"session_id": "o1", "name": "x", "started_wall": 1.0}
    assert c.post("/v1/mobile/sessions", json=body).status_code == 403
    assert c.post("/v1/mobile/sessions", json=body, headers={"Origin": "https://evil.example"}).status_code == 403
    assert c.post("/v1/mobile/sessions", json=body, headers={"Origin": env.origin}).status_code == 201
    assert c.put("/v1/mobile/sessions/o1/runs/0/chunks/0", content=b"x", headers={"Content-Type": "audio/webm"}).status_code == 403
    assert c.delete("/v1/mobile/sessions/o1").status_code == 403
    assert c.get("/v1/mobile/sessions/o1").status_code == 200  # reads need no Origin
    bearer = TestClient(env.app)
    bearer.headers["Authorization"] = "Bearer sekret"
    assert bearer.post("/v1/mobile/sessions", json={**body, "session_id": "o2"}).status_code == 201
    assert TestClient(env.app).get("/v1/mobile/sessions/o1").status_code == 401


# -- finish / preparation ----------------------------------------------------------------------------------


def test_finish_missing_chunks(env):
    env.create()
    env.put("m1", 0, 0, b"a")
    env.put("m1", 0, 2, b"c")
    r = env.finish("m1", [_run(0, 3)])
    assert r.status_code == 409 and r.json()["missing"] == [[0, 1]]
    assert mc.read_state(env.store, "m1")["state"] == "open"
    assert env.finish("m1", []).status_code == 400


@pytest.mark.parametrize("mime", ["audio/webm", "audio/mp4"])
def test_finish_builds_track_with_gap(env, media, mime):
    env.create(platform="iOS")
    n0 = env.upload_run("m1", 0, media[mime], mime)
    n1 = env.upload_run("m1", 1, media[mime], mime, parts=3)
    r = env.finish("m1", [_run(0, n0, 5000, mime), _run(1, n1, 15000, mime)])
    assert r.status_code == 202 and r.json()["state"] in ("preparing", "finalized")
    done = env.wait("m1")
    assert done["state"] == "finalized", done
    assert done["job_id"] and env.jobs.calls == ["m1"]
    # 6 s + (10 s start delta - 6 s) gap + 6 s
    assert env.wav_seconds("m1") == pytest.approx(16.0, abs=0.4)
    meta = env.store.read_session_meta("m1")
    assert meta["source"] == "mobile" and meta["in_person"] is True and meta["platform"] == "iOS"
    assert meta["name"] == "Standup" and meta["device"] == "iPhone" and meta["note_type"] == "general"
    assert meta["started_wall"] == 1000.0
    assert meta["tracks"]["system"]["source"] == "mobile"
    assert meta["upload"]["state"] == "complete" and meta["upload"]["percent"] == 100.0
    assert [r["index"] for r in meta["mobile"]["runs"]] == [0, 1]
    assert meta["mobile"]["lost_runs"] == []
    assert len(meta["mobile"]["gaps"]) == 1 and meta["mobile"]["gaps"][0]["seconds"] == pytest.approx(4.0, abs=0.4)
    # timing log loads and maps frames to time (gap included)
    clock = timing_mod.load_timing_log(env.store.track_timing_path("m1", "system"))
    assert clock.samplerate == 16000 and len(clock.segments) == 1
    assert clock.total_frames == pytest.approx(16.0 * 16000, abs=0.4 * 16000)
    assert clock.monotonic_at_seconds(12.0) - clock.monotonic_at_seconds(0.0) == pytest.approx(12.0, abs=0.5)
    assert len(clock.gaps) == 1 and clock.in_gap(8.0) and not clock.in_gap(2.0) and not clock.in_gap(14.0)
    # staging is cleaned up and the session is no longer unfinished
    assert not (mc.mobile_dir(env.store, "m1") / "runs").exists()
    assert env.mobile.staging_bytes() == 0
    assert env.store.read_job(done["job_id"]) is not None


def test_finish_idempotent(env, media):
    env.create()
    n = env.upload_run("m1", 0, media["audio/webm"])
    assert env.finish("m1", [_run(0, n)]).status_code == 202
    assert env.finish("m1", [_run(0, n)]).status_code == 202
    env.wait("m1")
    assert env.finish("m1", [_run(0, n)]).json()["state"] == "finalized"
    assert env.jobs.calls == ["m1"]


def test_overlap_becomes_no_gap_and_long_gap_is_capped(env, media):
    env.create("ov")
    n0 = env.upload_run("ov", 0, media["audio/webm"])
    n1 = env.upload_run("ov", 1, media["audio/webm"])
    env.finish("ov", [_run(0, n0, 0), _run(1, n1, 3000)])  # starts before the previous run ended
    assert env.wait("ov")["state"] == "finalized"
    assert env.wav_seconds("ov") == pytest.approx(12.0, abs=0.4)
    assert env.store.read_session_meta("ov")["mobile"]["gaps"] == []

    env.create("cap")
    n0 = env.upload_run("cap", 0, media["audio/webm"])
    n1 = env.upload_run("cap", 1, media["audio/webm"])
    env.finish("cap", [_run(0, n0, 0), _run(1, n1, 3 * 3600 * 1000)])
    assert env.wait("cap")["state"] == "finalized"
    assert env.wav_seconds("cap") == pytest.approx(12.0 + 10.0, abs=0.5)
    gap = env.store.read_session_meta("cap")["mobile"]["gaps"][0]
    assert gap["capped"] is True and gap["padded_seconds"] == pytest.approx(10.0)


def test_lost_run_is_recorded(env, media):
    env.create()
    n0 = env.upload_run("m1", 0, media["audio/webm"])
    assert env.put("m1", 1, 0, b"this is not audio at all" * 20).status_code == 204
    env.finish("m1", [_run(0, n0, 0), _run(1, 1, 10000, state="lost-tail")])
    assert env.wait("m1")["state"] == "finalized"
    meta = env.store.read_session_meta("m1")
    assert meta["mobile"]["lost_runs"] == [1]
    assert env.wav_seconds("m1") == pytest.approx(6.0, abs=0.4)


def test_truncated_interrupted_run_keeps_what_decodes(env, media):
    env.create()
    data = media["audio/webm"]
    assert env.put("m1", 0, 0, data[: len(data) * 2 // 3]).status_code == 204
    env.finish("m1", [_run(0, 1, 0, state="interrupted")])
    done = env.wait("m1")
    assert done["state"] == "finalized", done
    assert 1.0 < env.wav_seconds("m1") < 6.5


def test_undecodable_complete_run_fails_then_retry(env, media):
    env.create()
    env.put("m1", 0, 0, b"garbage" * 50)
    env.finish("m1", [_run(0, 1)])
    done = env.wait("m1")
    assert done["state"] == "failed" and done["error"]
    assert (mc.mobile_dir(env.store, "m1") / "runs" / "0" / "0.part").exists()  # chunks kept
    assert env.put("m1", 0, 1, b"x").status_code == 409
    assert env.client.post("/v1/mobile/sessions/m1/retry").json()["state"] == "preparing"
    assert env.wait("m1")["state"] == "failed"
    assert env.client.delete("/v1/mobile/sessions/m1").status_code == 204
    assert env.client.get("/v1/mobile/sessions/m1").status_code == 404
    assert not env.store.session_exists("m1")


def test_job_failure_then_retry_succeeds(env, media):
    env.jobs.fail = 1
    env.create()
    n = env.upload_run("m1", 0, media["audio/webm"])
    env.finish("m1", [_run(0, n)])
    assert env.wait("m1")["state"] == "failed"
    assert env.client.post("/v1/mobile/sessions/m1/retry").status_code == 202
    done = env.wait("m1")
    assert done["state"] == "finalized" and done["job_id"]
    assert env.client.post("/v1/mobile/sessions/m1/retry").json()["state"] == "finalized"


def test_retry_only_for_failed(env):
    env.create()
    assert env.client.post("/v1/mobile/sessions/m1/retry").status_code == 409


def test_delete_rules(env, media):
    env.create()
    env.put("m1", 0, 0, b"abc")
    assert env.client.delete("/v1/mobile/sessions/m1").status_code == 204
    assert not env.store.session_exists("m1") and not env.store.media_session_dir("m1").exists()
    assert env.mobile.staging_bytes() == 0
    # preparing / finalized sessions cannot be deleted
    env.create("p")
    n = env.upload_run("p", 0, media["audio/webm"])
    env.mobile.stop()  # park the worker so the session stays in `preparing`
    env.finish("p", [_run(0, n)])
    assert env.client.delete("/v1/mobile/sessions/p").status_code == 409
    env.install()
    assert env.wait("p")["state"] == "finalized"
    assert env.client.delete("/v1/mobile/sessions/p").status_code == 409


def test_restart_recovers_preparing(env, media):
    env.create()
    n = env.upload_run("m1", 0, media["audio/webm"])
    env.mobile.stop()
    assert env.finish("m1", [_run(0, n)]).status_code == 202
    assert mc.read_state(env.store, "m1")["state"] == "preparing"
    assert not env.jobs.calls
    env.install()  # a new process finds the session mid-preparation
    assert env.wait("m1")["state"] == "finalized"
    assert env.jobs.calls == ["m1"]


def test_finish_fences_live(env, media):
    closed = []
    mc.register_live_closer(closed.append)
    try:
        env.create()
        n = env.upload_run("m1", 0, media["audio/webm"])
        env.finish("m1", [_run(0, n)])
        assert "m1" in closed
        assert mc.live_writes_allowed(env.store, "m1") is False
    finally:
        mc._live_closers.clear()
    env.wait("m1")


# -- retention ------------------------------------------------------------------------------------------------


def test_sweep_abandoned(env):
    env.create("old")
    env.create("fresh")
    env.put("old", 0, 0, b"x")
    st = mc.read_state(env.store, "old")
    st["updated"] = time.time() - 15 * 24 * 3600
    mc.write_state(env.store, "old", st)
    assert env.mobile.sweep_abandoned(time.time()) == ["old"]
    assert not env.store.session_exists("old") and not env.store.media_session_dir("old").exists()
    assert mc.read_state(env.store, "fresh") is not None
    assert env.mobile.sweep_abandoned(time.time() + 13 * 24 * 3600) == []
    assert env.mobile.sweep_abandoned(time.time() + 15 * 24 * 3600) == ["fresh"]


# -- review fixes ------------------------------------------------------------------------------------------


def test_complete_run_that_will_not_decode_is_lost_not_fatal(env, media):
    env.create()
    n0 = env.upload_run("m1", 0, media["audio/webm"])
    assert env.put("m1", 1, 0, b"garbage" * 50).status_code == 204
    env.finish("m1", [_run(0, n0, 0), _run(1, 1, 10000)])  # the phone said run 1 was complete
    done = env.wait("m1")
    assert done["state"] == "finalized", done
    assert env.store.read_session_meta("m1")["mobile"]["lost_runs"] == [1]


def test_lost_first_run_keeps_wall_clock_times(env, media):
    from meeting_notes import timing

    env.create()
    assert env.put("m1", 0, 0, b"not audio" * 30).status_code == 204
    n1 = env.upload_run("m1", 1, media["audio/webm"])
    env.finish("m1", [_run(0, 1, 0, state="interrupted"), _run(1, n1, 30000)])
    assert env.wait("m1")["state"] == "finalized"
    meta = env.store.read_session_meta("m1")
    assert meta["mobile"]["lost_runs"] == [0]
    assert meta["mobile"]["runs"][0]["start_seconds"] == pytest.approx(30.0, abs=0.01)
    assert env.wav_seconds("m1") == pytest.approx(36.0, abs=0.5)  # 30 s of silence, then the run
    entries = [json.loads(line) for line in env.store.track_timing_path("m1", "system").read_text().splitlines()]
    assert entries[0]["event"] == "open" and entries[1]["event"] == "gap"
    timing.load_timing_log(env.store.track_timing_path("m1", "system"))


def test_live_raw_audio_is_removed_after_publish(env, media):
    env.create()
    raw = env.store.track_raw_path("m1", "system")
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(b"\0" * 3200)  # what a live transcript left behind
    n0 = env.upload_run("m1", 0, media["audio/webm"])
    env.finish("m1", [_run(0, n0, 0)])
    assert env.wait("m1")["state"] == "finalized"
    assert not raw.exists() and env.store.track_wav_path("m1", "system").exists()


def test_a_wrong_bearer_does_not_skip_the_origin_check(env, monkeypatch):
    monkeypatch.setenv("MEETING_NOTES_TOKEN", "sekret")
    c = TestClient(env.app)
    c.cookies.set("meeting_notes_token", "sekret")
    body = {"session_id": "o3", "name": "x", "started_wall": 1.0}
    assert c.post("/v1/mobile/sessions", json=body, headers={"Authorization": "Bearer "}).status_code in (401, 403)
    assert c.post("/v1/mobile/sessions", json=body, headers={"Authorization": "Bearer nope"}).status_code == 403


def test_runs_up_to_199_are_accepted():
    from meeting_notes.server import mobile_upload as mu

    assert mu.MAX_RUNS == 200
