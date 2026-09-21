"""Tests for the transcription server (meeting_notes.server.*).

No Whisper model is ever loaded here -- huggingface.co is unreachable from
this environment (and shouldn't be needed anyway: unit tests have no business
downloading gigabytes of weights). Every test that needs a "transcriber" uses
``StubTranscriber`` below, which returns canned ``Segment`` objects for a
given wav path/track and never touches the network.

``fastapi.testclient.TestClient`` drives the whole app -- HTTP and websocket
-- in-process, so these tests need no real network or subprocess either.
"""

from __future__ import annotations

import json
import time
import threading
from pathlib import Path
from typing import Dict, List

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from meeting_notes import wire
from meeting_notes.server.app import create_app
from meeting_notes.transcribe.protocol import Segment

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated:DeprecationWarning"
)


class StubTranscriber:
    """A ``Transcriber`` that returns pre-baked segments instead of running
    any model. ``segments_by_track`` maps track name -> list[Segment]."""

    def __init__(self, segments_by_track: Dict[str, List[Segment]]):
        self.segments_by_track = segments_by_track
        self.calls: list = []

    def transcribe(self, wav_path: Path, track: str) -> List[Segment]:
        self.calls.append((track, Path(wav_path)))
        return list(self.segments_by_track.get(track, ()))


class GatedTranscriber(StubTranscriber):
    """A StubTranscriber that blocks until released.

    Lets a test assert things about a job that is genuinely still running,
    instead of racing the worker and hoping it has not finished yet.
    """

    def __init__(self, segments_by_track: Dict[str, List[Segment]]):
        super().__init__(segments_by_track)
        self.entered = threading.Event()
        self.release = threading.Event()

    def transcribe(self, wav_path: Path, track: str) -> List[Segment]:
        self.entered.set()
        self.release.wait(timeout=10)
        return super().transcribe(wav_path, track)


def make_app(tmp_path, *, transcriber_factory=None) -> "FastAPI":  # noqa: F821
    return create_app(transcriber_factory=transcriber_factory, data_root=str(tmp_path / "data"))


def silence_pcm(seconds: float, sample_rate: int = wire.STREAM_SAMPLE_RATE) -> bytes:
    return b"\x00\x00" * int(seconds * sample_rate)


def wait_for_job_state(client: TestClient, job_id: str, state: str, *, timeout: float = 5.0) -> dict:
    """Poll the job status route until it reaches ``state`` (or ERROR, or a
    timeout) -- the worker runs on a background thread, so completion isn't
    synchronous with the finalize call."""
    deadline = time.monotonic() + timeout
    last = {}
    while time.monotonic() < deadline:
        resp = client.get(wire.job_path(job_id))
        assert resp.status_code == 200
        last = resp.json()
        if last["state"] in (state, wire.JobState.ERROR):
            return last
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} never reached {state!r}, last seen: {last}")


# -- health -------------------------------------------------------------------


def test_health_requires_no_auth_and_reports_protocol_version(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_NOTES_TOKEN", "secret-token")
    app = make_app(tmp_path)
    client = TestClient(app)

    resp = client.get(wire.HEALTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["protocol"] == wire.PROTOCOL_VERSION
    assert "model" in body and "device" in body and "live_enabled" in body


# -- auth -----------------------------------------------------------------


def test_protected_route_rejects_missing_or_wrong_token_and_accepts_right_one(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_NOTES_TOKEN", "s3cret")
    app = make_app(tmp_path)
    client = TestClient(app)
    protected = wire.job_path("does-not-exist")

    resp = client.get(protected)
    assert resp.status_code == 401  # no credentials at all

    resp = client.get(protected, headers={"Authorization": "Bearer wrong-token"})
    assert resp.status_code == 403  # credentials presented, but wrong

    resp = client.get(protected, headers={"Authorization": "Bearer s3cret"})
    # Auth passed; the 404 here is about the (nonexistent) job, not auth.
    assert resp.status_code == 404


def test_unset_token_allows_all_requests(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = make_app(tmp_path)
    client = TestClient(app)

    # No Authorization header at all -- with no token configured this must
    # still reach the handler (proven by getting the route's own 404, not an
    # auth failure).
    resp = client.get(wire.job_path("does-not-exist"))
    assert resp.status_code == 404


# -- websocket streaming ----------------------------------------------------


def test_websocket_hello_and_frames_store_audio_byte_for_byte(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = make_app(tmp_path)
    client = TestClient(app)

    session_id = "sess-basic"
    mic_pcm = bytes(range(256)) * 8  # 2048 bytes = 1024 frames, deterministic content
    hello = wire.Hello(session_id=session_id, tracks=["mic"])

    with client.websocket_connect(wire.STREAM) as ws:
        ws.send_json(wire.to_json(hello))
        ws.send_bytes(wire.encode_audio_frame("mic", 0, mic_pcm))
        ack = ws.receive_json()
        assert ack == {"type": "ack", "track": "mic", "frames": len(mic_pcm) // wire.BYTES_PER_FRAME}

    store = app.state.store
    stored = store.track_raw_path(session_id, "mic").read_bytes()
    assert stored == mic_pcm  # byte-for-byte, not just same length


def test_live_sessions_api_tracks_connected_recorder(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = make_app(tmp_path)
    client = TestClient(app)

    hello = wire.Hello(
        session_id="live-one", name="Weekly sync", device="LAPTOP-1", tracks=["mic"]
    )
    with client.websocket_connect(wire.STREAM) as ws:
        ws.send_json(wire.to_json(hello))
        body = client.get("/v1/live").json()
        assert body["total"] == 1
        assert body["items"][0]["name"] == "Weekly sync"
        assert body["items"][0]["device"] == "LAPTOP-1"

    assert client.get("/v1/live").json()["total"] == 0


def test_two_clients_stream_concurrently_without_crossing_audio(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = make_app(tmp_path)
    client = TestClient(app)
    pcm_a = b"\x11\x22" * 128
    pcm_b = b"\x33\x44" * 96

    with client.websocket_connect(wire.STREAM) as first:
        first.send_json(
            wire.to_json(wire.Hello(session_id="client-a", device="LAPTOP-A", tracks=["mic"]))
        )
        with client.websocket_connect(wire.STREAM) as second:
            second.send_json(
                wire.to_json(
                    wire.Hello(session_id="client-b", device="LAPTOP-B", tracks=["mic"])
                )
            )

            live = client.get("/v1/live").json()
            assert live["total"] == 2
            assert {item["device"] for item in live["items"]} == {"LAPTOP-A", "LAPTOP-B"}

            first.send_bytes(wire.encode_audio_frame("mic", 0, pcm_a))
            second.send_bytes(wire.encode_audio_frame("mic", 0, pcm_b))
            assert first.receive_json()["frames"] == 128
            assert second.receive_json()["frames"] == 96

        assert client.get("/v1/live").json()["total"] == 1

    assert client.get("/v1/live").json()["total"] == 0
    assert app.state.store.track_raw_path("client-a", "mic").read_bytes() == pcm_a
    assert app.state.store.track_raw_path("client-b", "mic").read_bytes() == pcm_b


def test_duplicate_active_session_id_is_rejected(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = make_app(tmp_path)
    client = TestClient(app)
    hello = wire.to_json(wire.Hello(session_id="same-session", tracks=["mic"]))

    with client.websocket_connect(wire.STREAM) as first:
        first.send_json(hello)
        with client.websocket_connect(wire.STREAM) as duplicate:
            duplicate.send_json(hello)
            error = duplicate.receive_json()
            assert error["type"] == "error"
            assert "already streaming" in error["detail"]
            assert duplicate.receive()["code"] == 4409

        first.send_bytes(wire.encode_audio_frame("mic", 0, b"\x01\x02"))
        assert first.receive_json()["frames"] == 1


def test_out_of_order_and_duplicate_frames_reassemble_correctly(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = make_app(tmp_path)
    client = TestClient(app)

    session_id = "sess-ooo"
    frame_len = 100  # frames per chunk
    # Each chunk is internally uniform (all bytes == its index) so the
    # reassembled file can be checked exactly, chunk by chunk.
    chunks = [bytes([i]) * (frame_len * wire.BYTES_PER_FRAME) for i in range(3)]
    expected = b"".join(chunks)

    with client.websocket_connect(wire.STREAM) as ws:
        ws.send_json(wire.to_json(wire.Hello(session_id=session_id, tracks=["mic"])))

        # Deliberately shuffled arrival order...
        ws.send_bytes(wire.encode_audio_frame("mic", 1 * frame_len, chunks[1]))
        ws.send_bytes(wire.encode_audio_frame("mic", 0 * frame_len, chunks[0]))
        ws.send_bytes(wire.encode_audio_frame("mic", 2 * frame_len, chunks[2]))
        # ...plus a duplicate resend of an already-written offset, exactly
        # what a reconnecting client does when it resends everything since
        # its last ack just in case some of it didn't land.
        ws.send_bytes(wire.encode_audio_frame("mic", 0 * frame_len, chunks[0]))

        # Only two of those four writes actually advance the contiguous
        # prefix (chunk 1 alone at offset 100 doesn't; the duplicate of
        # chunk 0 doesn't either), so exactly two acks are produced, in this
        # order, and both name the correct contiguous frame count.
        first_ack = ws.receive_json()
        second_ack = ws.receive_json()

    assert first_ack == {"type": "ack", "track": "mic", "frames": 2 * frame_len}
    assert second_ack == {"type": "ack", "track": "mic", "frames": 3 * frame_len}

    store = app.state.store
    stored = store.track_raw_path(session_id, "mic").read_bytes()
    assert stored == expected
    assert store.contiguous_frames(session_id, "mic") == 3 * frame_len


def test_protocol_version_mismatch_is_rejected_with_clear_reason(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = make_app(tmp_path)
    client = TestClient(app)

    bad_hello = wire.to_json(wire.Hello(session_id="sess-badver"))
    bad_hello["protocol"] = wire.PROTOCOL_VERSION + 1

    with client.websocket_connect(wire.STREAM) as ws:
        ws.send_json(bad_hello)
        error = ws.receive_json()
        assert error["type"] == "error"
        assert "protocol" in error["detail"].lower()
        closed = ws.receive()
        assert closed["type"] == "websocket.close"
        assert closed["code"] == 4400
        assert "protocol" in closed["reason"].lower()


def test_malformed_binary_frame_does_not_kill_the_connection(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = make_app(tmp_path)
    client = TestClient(app)

    with client.websocket_connect(wire.STREAM) as ws:
        ws.send_json(wire.to_json(wire.Hello(session_id="sess-malformed", tracks=["mic"])))

        ws.send_bytes(b"\x00")  # far too short to even contain the header
        error = ws.receive_json()
        assert error["type"] == "error"

        # The connection is still alive and usable afterwards.
        good_pcm = b"\x01\x02" * 10
        ws.send_bytes(wire.encode_audio_frame("mic", 0, good_pcm))
        ack = ws.receive_json()
        assert ack == {"type": "ack", "track": "mic", "frames": 10}


# -- upload + finalize + job -------------------------------------------------


def _upload_and_finalize(client: TestClient, session_id: str, tracks_pcm: Dict[str, bytes], settings=None):
    for track, pcm in tracks_pcm.items():
        resp = client.post(
            wire.track_upload_path(session_id, track),
            content=pcm,
            headers={"Content-Type": "application/octet-stream", "X-Frames": str(len(pcm) // 2)},
        )
        assert resp.status_code == 200, resp.text

    now = time.monotonic()
    # Both tracks share the same start_monotonic (t=1000.0) so their segment
    # times land on one shared timeline exactly as far apart as the segments
    # themselves say -- see meeting_notes.timing / merge_tracks.
    timing = {}
    for track, pcm in tracks_pcm.items():
        frames = len(pcm) // 2
        timing[track] = [
            {
                "event": "open",
                "segment": 0,
                "frames": 0,
                "t": 1000.0,
                "wall": 0.0,
                "samplerate": wire.STREAM_SAMPLE_RATE,
                "channels": 1,
                "device": track,
            },
            {
                "event": "close",
                "segment": 0,
                "frames": frames,
                "t": 1000.0 + frames / wire.STREAM_SAMPLE_RATE,
            },
        ]

    body = {
        "meta": {"created": "2026-09-20", "tracks": {t: {} for t in tracks_pcm}},
        "timing": timing,
        "settings": settings or {},
    }
    resp = client.post(wire.finalize_path(session_id), json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()["job_id"]


def test_finalize_enqueues_job_that_completes_with_both_tracks_interleaved(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)

    segments_by_track = {
        "mic": [Segment(start=0.0, end=1.0, text="Hello there", track="mic")],
        "system": [Segment(start=0.5, end=1.5, text="Hi, good to see you", track="system")],
    }
    app = make_app(tmp_path, transcriber_factory=lambda **_kw: StubTranscriber(segments_by_track))
    client = TestClient(app)

    session_id = "sess-final"
    job_id = _upload_and_finalize(
        client,
        session_id,
        {"mic": silence_pcm(2.0), "system": silence_pcm(2.0)},
    )

    job = wait_for_job_state(client, job_id, wire.JobState.DONE)
    assert job["state"] == wire.JobState.DONE
    assert job["error"] is None
    assert job["session_id"] == session_id

    resp = client.get(wire.job_transcript_path(job_id))
    assert resp.status_code == 200
    transcript = resp.json()

    assert "Hello there" in transcript["markdown"]
    assert "Hi, good to see you" in transcript["markdown"]
    assert "You" in transcript["markdown"]
    assert "Them" in transcript["markdown"]
    # mic (You) starts at t=0.0, system (Them) at t=0.5 -- You's block has to
    # come first in the rendered markdown for the interleaving to be right.
    assert transcript["markdown"].index("You") < transcript["markdown"].index("Them")

    payload = json.loads(transcript["json"])
    labels = [seg["label"] for seg in payload["segments"]]
    assert labels == ["You", "Them"]
    assert payload["segments"][0]["text"] == "Hello there"
    assert payload["segments"][1]["text"] == "Hi, good to see you"


def test_job_polling_moves_queued_to_done_and_transcript_404s_before_done(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)

    segments_by_track = {"mic": [Segment(start=0.0, end=0.5, text="hi", track="mic")]}
    stub = GatedTranscriber(segments_by_track)
    app = make_app(tmp_path, transcriber_factory=lambda **_kw: stub)
    client = TestClient(app)

    session_id = "sess-poll"
    job_id = _upload_and_finalize(client, session_id, {"mic": silence_pcm(1.0)})

    # Hold the worker inside transcribe(), so the job is provably unfinished
    # while we assert. Asserting straight after enqueue and hoping the worker
    # has not run yet is a race: with a stub this fast it usually HAS run.
    assert stub.entered.wait(timeout=10), "worker never started the job"

    resp = client.get(wire.job_transcript_path(job_id))
    assert resp.status_code == 404

    first = client.get(wire.job_path(job_id)).json()
    assert first["state"] in (wire.JobState.QUEUED, wire.JobState.RUNNING)

    stub.release.set()

    job = wait_for_job_state(client, job_id, wire.JobState.DONE)
    assert job["state"] == wire.JobState.DONE

    resp = client.get(wire.job_transcript_path(job_id))
    assert resp.status_code == 200


def test_unknown_job_id_404s(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = make_app(tmp_path)
    client = TestClient(app)
    assert client.get(wire.job_path("nonexistent")).status_code == 404
    assert client.get(wire.job_transcript_path("nonexistent")).status_code == 404


# -- path traversal -----------------------------------------------------------


def test_path_traversal_session_id_rejected_over_websocket(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = make_app(tmp_path)
    client = TestClient(app)

    with client.websocket_connect(wire.STREAM) as ws:
        ws.send_json(wire.to_json(wire.Hello(session_id="../../etc")))
        error = ws.receive_json()
        assert error["type"] == "error"
        assert "session_id" in error["detail"]
        closed = ws.receive()
        assert closed["type"] == "websocket.close"
        assert closed["code"] == 4400

    # Nothing was created outside (or even inside, safely) the data root.
    data_root = app.state.store.root
    for path in data_root.rglob("*"):
        assert ".." not in path.parts


def test_path_traversal_session_id_rejected_over_http(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = make_app(tmp_path)
    client = TestClient(app)

    resp = client.post(
        wire.track_upload_path("../../etc", "mic"),
        content=b"\x00\x00",
        headers={"Content-Type": "application/octet-stream"},
    )
    # Either the router itself never matches a traversal-shaped path segment,
    # or our own validation catches it -- either way it must not be a 200,
    # and it must not have written anything outside the data root.
    assert resp.status_code != 200

    data_root = app.state.store.root
    for path in data_root.rglob("*"):
        assert ".." not in path.parts


def test_store_rejects_unsafe_ids_directly():
    from meeting_notes.server.store import is_safe_id

    assert not is_safe_id("../../etc")
    assert not is_safe_id("..")
    assert not is_safe_id("a/b")
    assert not is_safe_id("a\\b")
    assert not is_safe_id("")
    assert is_safe_id("sess-1234_abc.def")


# -- live preview (unit-level; no VAD/model needed to reach these gates) ----


def test_live_preview_disabled_when_no_transcriber_configured():
    from meeting_notes.server.live import LivePreview

    live = LivePreview(transcriber_factory=None)
    assert not live.enabled
    live.feed("sess", "mic", b"\x00\x00" * 1000)  # must not raise
    assert live.poll("sess", "mic") == []


def test_live_preview_waits_for_enough_new_audio_before_running_vad():
    from meeting_notes.server.live import LivePreview

    calls = []

    def factory(**_kw):
        calls.append(1)
        raise AssertionError("should never be constructed before enough audio has arrived")

    live = LivePreview(transcriber_factory=factory, interval=8.0)
    # Well under 8 seconds of 16kHz audio -> must not even attempt a VAD pass
    # (which is what would first try to build the transcriber).
    live.feed("sess", "mic", silence_pcm(1.0))
    assert live.poll("sess", "mic") == []
    assert not calls


def test_live_preview_drops_hallucinated_partials(monkeypatch):
    """Seen on a real run: VAD handed the previewer a 0.4 s decay tail left
    over after the previous commit, and Whisper answered with seven "."
    segments whose timestamps marched 29 s past the end of that fragment.
    Neither punctuation-only text nor a segment starting beyond the chunk's
    real length may ever become a Partial."""
    from meeting_notes.server import live as live_mod

    chunk_frames = int(0.4 * wire.STREAM_SAMPLE_RATE)
    stub = StubTranscriber(
        {
            "system": [
                Segment(start=0.0, end=5.0, text=".", track="system"),
                Segment(start=5.0, end=10.0, text="...", track="system"),
                # Real words, but placed 29 s into a 0.4 s chunk: impossible.
                Segment(start=29.0, end=30.0, text="thank you", track="system"),
                # The one legitimate segment.
                Segment(start=0.05, end=0.35, text="Yes.", track="system"),
            ]
        }
    )
    live = live_mod.LivePreview(transcriber_factory=lambda **_kw: stub, interval=0.0, maturity=0.0)
    # Bypass VAD: hand poll() one mature chunk of the fragment's real length.
    pcm = b"\x01\x00" * chunk_frames
    monkeypatch.setattr(
        live_mod._TrackBuffer, "take_mature_chunks", lambda self, maturity: [(160000, 160000 + chunk_frames, pcm)]
    )
    live.feed("sess", "system", pcm)

    partials = live.poll("sess", "system")

    assert [p.text for p in partials] == ["Yes."]
    assert partials[0].start == pytest.approx(10.0 + 0.05)


def test_collapse_repeats_squashes_a_runaway_word_loop():
    from meeting_notes.transcribe.protocol import collapse_repeats

    assert collapse_repeats("Test " * 250) == "Test Test Test"
    assert collapse_repeats("Test, test. TEST test test") == "Test, test. TEST"
    assert collapse_repeats("one two three") == "one two three"
    assert collapse_repeats("no no no no way") == "no no no way"
    assert collapse_repeats("a b a b a b") == "a b a b a b"  # not a single-word run
    assert collapse_repeats("") == ""


def test_is_real_text_rejects_punctuation_only():
    from meeting_notes.transcribe.protocol import is_real_text

    assert not is_real_text(".")
    assert not is_real_text("...")
    assert not is_real_text("")
    assert not is_real_text(" - ")
    assert is_real_text("Yes.")
    assert is_real_text("42")


def test_live_preview_skips_ahead_over_a_hole_in_the_stream(tmp_path, monkeypatch):
    """Seen on a real run: after a 40 s server outage the client reconnected
    and resumed streaming, but its resend buffer had not covered the whole
    outage, so the server's copy had a hole. The previewer was fed only from
    the contiguous prefix, which stopped at the hole -- and the live preview
    was dead for the rest of the meeting. Audio arriving after a hole must
    still reach the previewer, placed at its real position."""
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = make_app(tmp_path, transcriber_factory=lambda **_kw: StubTranscriber({}))
    live = app.state.live_preview
    client = TestClient(app)
    rate = wire.STREAM_SAMPLE_RATE

    with client.websocket_connect(wire.STREAM) as ws:
        ws.send_json(wire.to_json(wire.Hello(session_id="sess-hole", tracks=["mic"])))
        ws.send_bytes(wire.encode_audio_frame("mic", 0, silence_pcm(1.0)))
        assert ws.receive_json()["frames"] == rate
        # 40 s of nothing, then the stream resumes.
        ws.send_bytes(wire.encode_audio_frame("mic", 41 * rate, silence_pcm(1.0)))
        # No ack follows a non-contiguous frame, so use a text message the
        # server always answers as a barrier to know it has been processed.
        ws.send_text("sync")
        assert ws.receive_json()["type"] == "error"
        buf = live._buffers[("sess-hole", "mic")]
        assert buf._committed_frame == 41 * rate
        assert buf._total_frames == 42 * rate
        assert len(buf._buffer) == rate * wire.BYTES_PER_FRAME  # only post-hole audio held

    # A fresh connection must not re-preview what the store already had.
    with client.websocket_connect(wire.STREAM) as ws:
        ws.send_json(wire.to_json(wire.Hello(session_id="sess-hole", tracks=["mic"])))
        ws.send_bytes(wire.encode_audio_frame("mic", 0, silence_pcm(0.5)))  # a resend of old audio
        ws.send_text("sync")
        while ws.receive_json()["type"] != "error":  # a fresh connection re-acks the prefix first
            pass
        assert ("sess-hole", "mic") not in live._buffers


def test_job_wraps_raw_audio_when_no_wav_exists(tmp_path, monkeypatch):
    """Seen on a real run: a session that was streamed but never finalized
    has only .raw files. Re-running transcription on it enqueued a job that
    found no WAV and reported DONE with nothing -- and retention then
    deleted the audio. The job must read the raw PCM when that is all there
    is."""
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    stub = StubTranscriber({"mic": [Segment(start=0.0, end=1.0, text="from raw", track="mic")]})
    app = make_app(tmp_path, transcriber_factory=lambda **_kw: stub)
    store = app.state.store
    client = TestClient(app)

    with client.websocket_connect(wire.STREAM) as ws:
        ws.send_json(wire.to_json(wire.Hello(session_id="raw-only", tracks=["mic"])))
        ws.send_bytes(wire.encode_audio_frame("mic", 0, silence_pcm(1.0)))
        ws.receive_json()
    assert store.track_raw_path("raw-only", "mic").exists()
    assert not store.track_wav_path("raw-only", "mic").exists()

    job_id = app.state.job_queue.enqueue("raw-only", {})
    wait_for_job_state(client, job_id, wire.JobState.DONE)

    assert [c[0] for c in stub.calls] == ["mic"]
    assert store.track_wav_path("raw-only", "mic").exists()
    assert "from raw" in client.get(wire.job_transcript_path(job_id)).json()["markdown"]


# -- websocket disconnect handling (Fix C) -----------------------------------


def test_websocket_disconnect_right_after_a_frame_does_not_crash_the_server(tmp_path, monkeypatch, caplog):
    """A client that drops the connection between sending a frame and reading
    the server's ack/partial used to blow up: only the inbound `receive()`
    was guarded, so any of the sends after it (ack, error, partial) could
    raise WebSocketDisconnect or Starlette's own RuntimeError straight out of
    the handler -- a stack trace on every ordinary wifi drop. This exercises
    it end-to-end through TestClient; test_send_failure_* below pins the
    exact exception-handling behaviour deterministically."""
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = make_app(tmp_path)
    client = TestClient(app)

    with client.websocket_connect(wire.STREAM) as ws:
        ws.send_json(wire.to_json(wire.Hello(session_id="sess-drop", tracks=["mic"])))
        ws.send_bytes(wire.encode_audio_frame("mic", 0, b"\x01\x02" * 10))
        # Disconnect immediately, without ever reading the ack the server is
        # about to send back.
        ws.close()

    assert "Traceback" not in caplog.text

    # The rest of the server must be completely unaffected -- nothing about
    # that dropped connection should have wedged any shared state (the job
    # queue thread, the store, etc). A later, unrelated request proves it.
    resp = client.get(wire.HEALTH)
    assert resp.status_code == 200


@pytest.mark.parametrize(
    "failure",
    [
        WebSocketDisconnect(code=1006),
        RuntimeError('Cannot call "send" once a close message has been sent.'),
    ],
    ids=["WebSocketDisconnect", "RuntimeError-close-message"],
)
def test_send_failure_after_a_frame_is_swallowed_not_raised(tmp_path, monkeypatch, failure):
    """Deterministically reproduces the race TestClient's in-memory transport
    can't: the client vanishes between the inbound frame and our ack, so the
    send that would carry that ack fails. Drives the ASGI app directly, with
    a `send` that fails on cue, so the exact exception (and only that one) is
    under test rather than real socket timing.
    """
    import asyncio
    import json as _json

    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = create_app(data_root=str(tmp_path / "data"))

    hello_text = _json.dumps(wire.to_json(wire.Hello(session_id="sess-race", tracks=["mic"])))
    frame = wire.encode_audio_frame("mic", 0, b"\x01\x02" * 10)

    incoming = [
        {"type": "websocket.connect"},
        {"type": "websocket.receive", "text": hello_text},
        {"type": "websocket.receive", "bytes": frame},
    ]
    sent: list = []

    async def receive():
        return incoming.pop(0) if incoming else {"type": "websocket.disconnect", "code": 1006}

    async def send(message):
        sent.append(message)
        # The ack for the frame above is the second outbound message (the
        # first is "websocket.accept") -- fail exactly that one, as a client
        # that vanished right after sending the frame would.
        if len(sent) == 2:
            raise failure

    scope = {
        "type": "websocket",
        "asgi": {"version": "3.0", "spec_version": "2.1"},
        "path": wire.STREAM,
        "raw_path": wire.STREAM.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
    }

    # The whole point: this must complete without raising `failure` back out.
    asyncio.run(app(scope, receive, send))

    # ...and cleanup must still have run, not been skipped by the exception.
    assert not app.state.live_preview._buffers


def test_send_failure_unrelated_to_a_disconnect_is_not_swallowed(tmp_path, monkeypatch):
    """The RuntimeError guard is deliberately narrow -- it must not turn into
    a blanket except that hides a genuine bug behind a clean disconnect."""
    import asyncio
    import json as _json

    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = create_app(data_root=str(tmp_path / "data"))

    hello_text = _json.dumps(wire.to_json(wire.Hello(session_id="sess-bug", tracks=["mic"])))
    frame = wire.encode_audio_frame("mic", 0, b"\x01\x02" * 10)
    incoming = [
        {"type": "websocket.connect"},
        {"type": "websocket.receive", "text": hello_text},
        {"type": "websocket.receive", "bytes": frame},
    ]
    sent: list = []

    async def receive():
        return incoming.pop(0) if incoming else {"type": "websocket.disconnect", "code": 1006}

    async def send(message):
        sent.append(message)
        if len(sent) == 2:
            raise RuntimeError("boom -- unrelated to any disconnect")

    scope = {
        "type": "websocket",
        "asgi": {"version": "3.0", "spec_version": "2.1"},
        "path": wire.STREAM,
        "raw_path": wire.STREAM.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
    }

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(app(scope, receive, send))


# -- finalize: malformed request bodies (Fix D) ------------------------------


def test_finalize_rejects_malformed_json_body_with_400(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = make_app(tmp_path)
    client = TestClient(app)

    resp = client.post(
        wire.finalize_path("sess-bad-json"),
        content=b"{not valid json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert "json" in resp.json()["detail"].lower()


@pytest.mark.parametrize("body", [[1, 2, 3], "just a string", 42])
def test_finalize_rejects_a_non_dict_body_with_400(tmp_path, monkeypatch, body):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = make_app(tmp_path)
    client = TestClient(app)

    resp = client.post(wire.finalize_path("sess-bad-body"), json=body)
    assert resp.status_code == 400
    assert "object" in resp.json()["detail"].lower()


@pytest.mark.parametrize("field", ["meta", "timing", "settings"])
def test_finalize_rejects_a_non_dict_meta_timing_or_settings_with_400(tmp_path, monkeypatch, field):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = make_app(tmp_path)
    client = TestClient(app)

    body = {"meta": {}, "timing": {}, "settings": {}, field: ["not", "a", "dict"]}

    resp = client.post(wire.finalize_path("sess-bad-field"), json=body)
    assert resp.status_code == 400
    assert field in resp.json()["detail"]


def test_finalize_rejects_client_transcription_overrides(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    client = TestClient(make_app(tmp_path))
    resp = client.post(
        wire.finalize_path("sess-client-override"),
        json={
            "meta": {},
            "timing": {},
            "settings": {"transcriber": {"model_size": "tiny.en"}},
        },
    )
    assert resp.status_code == 400
    assert "controlled by the server" in resp.json()["detail"]
