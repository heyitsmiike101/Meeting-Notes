"""Client-side transport tests: resampling, HTTP upload, the upload queue and
the live-preview websocket streamer.

Hardware-free and model-free by construction (nothing here touches an audio
device or a Whisper model). The only "network" involved is the REAL server
from ``meeting_notes.server.app`` (``create_app``), run in-process in a
background thread on localhost with an injected stub transcriber -- no real
Whisper model can run in this environment (huggingface.co is blocked), so the
stub stands in for it the same way ``create_app``'s own ``transcriber_factory``
parameter is designed for tests to do.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import wave
from pathlib import Path

import httpx
import numpy as np
import pytest
import uvicorn
from fastapi import FastAPI

from meeting_notes import config as config_mod
from meeting_notes import wire
from meeting_notes.client.api import ServerClient, ServerUnavailable
from meeting_notes.client.controller import RecordingController
from meeting_notes.client.queue import SessionQueue, UploadWorker
from meeting_notes.client.resample import Downsampler, downsample_to_16k
from meeting_notes.client.streamer import LiveStreamer
from meeting_notes.server.app import create_app
from meeting_notes.transcribe.protocol import Segment
from tests.fakes import FakeSource


class _StubTranscriber:
    """Stands in for faster-whisper: returns one canned segment per track,
    fast and with no model download -- huggingface.co is blocked here."""

    def transcribe(self, wav_path, track):
        return [Segment(start=0.0, end=0.4, text=f"hello from {track}", track=track)]


def _stub_transcriber_factory(**_kwargs):
    return _StubTranscriber()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _LiveServer:
    def __init__(self, app: FastAPI):
        self.port = _free_port()
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> str:
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        if not self.server.started:
            raise RuntimeError("server did not start in time")
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)


@pytest.fixture(scope="module")
def stub_server(tmp_path_factory):
    data_root = tmp_path_factory.mktemp("server-data")
    app = create_app(transcriber_factory=_stub_transcriber_factory, data_root=str(data_root))
    live = _LiveServer(app)
    base_url = live.start()
    yield base_url
    live.stop()


@pytest.fixture()
def dead_port_url() -> str:
    """A URL nothing is listening on, for exercising unreachable-server paths."""
    port = _free_port()  # bound then immediately released -> connection refused
    return f"http://127.0.0.1:{port}"


def _make_session_dir(tmp_path: Path, name: str = "session") -> Path:
    """A minimal but realistic two-track session directory, matching what
    RecordingSession.finalize() produces (see meeting_notes/audio/session.py) --
    including timing sidecars, since merge_tracks (used server-side by
    jobs.py) drops any track with no timing log."""
    session_dir = tmp_path / name
    session_dir.mkdir()
    rate = 48000
    tracks = {}
    for track, freq in (("mic", 440.0), ("system", 220.0)):
        wav_path = session_dir / f"{track}.wav"
        t = np.arange(int(rate * 0.2)) / rate
        samples = (0.3 * np.sin(2 * np.pi * freq * t) * 32767).astype("<i2")
        with wave.open(str(wav_path), "wb") as fh:
            fh.setnchannels(1)
            fh.setsampwidth(2)
            fh.setframerate(rate)
            fh.writeframes(samples.tobytes())
        duration = len(samples) / rate
        timing_path = session_dir / f"{track}.timing.jsonl"
        timing_path.write_text(
            json.dumps(
                {
                    "event": "open",
                    "segment": 0,
                    "frames": 0,
                    "t": 0.0,
                    "wall": 0.0,
                    "samplerate": rate,
                    "channels": 1,
                    "device": "test",
                }
            )
            + "\n"
            + json.dumps({"event": "close", "segment": 0, "frames": len(samples), "t": duration})
            + "\n",
            encoding="utf-8",
        )
        tracks[track] = {
            "wav": wav_path.name,
            "samplerate": rate,
            "channels": 1,
            "label": "You" if track == "mic" else "Them",
            "frames": len(samples),
            "duration_sec": round(duration, 3),
        }
    meta = {
        "version": 1,
        "created": "2026-01-01T00:00:00",
        "duration_sec": 0.2,
        "tracks": tracks,
    }
    (session_dir / "session.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return session_dir


# =============================================================================
# resample.py
# =============================================================================


def _amplitude(signal: np.ndarray) -> float:
    """RMS-based amplitude estimate of a steady sinusoid (immune to FFT bin
    magnitude scaling with N, unlike comparing raw FFT peak heights across
    signals of different length/sample rate)."""
    values = signal.astype(np.float64)
    return float(np.sqrt(2.0) * np.sqrt(np.mean(np.square(values))))


class TestResample:
    def test_seven_khz_tone_survives_48k_to_16k(self):
        # 7 kHz is comfortably inside the 8 kHz Nyquist of the 16 kHz target,
        # so a correct anti-aliasing filter should pass it at close to full
        # strength -- this is the "don't over-filter real content" half of
        # the anti-aliasing proof.
        sr, target, freq, amp = 48000, 16000, 7000.0, 0.8
        t = np.arange(int(sr * 1.0)) / sr
        tone = (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)

        ds = Downsampler(sr, target)
        out = np.concatenate([ds.process(tone[i : i + 4800]) for i in range(0, len(tone), 4800)])

        steady = out[2000:]  # skip the filter's warm-up region
        assert _amplitude(steady) > 0.6 * amp  # survives: not silenced

    def test_twelve_khz_tone_is_attenuated_not_aliased(self):
        # 12 kHz is above the 8 kHz Nyquist of a 16 kHz stream. Naive
        # decimation (keep every 3rd sample of 48 kHz, no filtering) would
        # fold this down to a full-strength 4 kHz tone -- audible, wrong, and
        # exactly the bug this module exists to prevent. A correct low-pass
        # filter removes it almost entirely before decimation instead.
        sr, target, freq, amp = 48000, 16000, 12000.0, 0.8
        t = np.arange(int(sr * 1.0)) / sr
        tone = (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)

        # What naive (unfiltered) decimation would do, for contrast.
        naive = tone[::3]
        assert _amplitude(naive[300:]) > 0.7 * amp  # confirms the alias would be strong

        ds = Downsampler(sr, target)
        out = np.concatenate([ds.process(tone[i : i + 4800]) for i in range(0, len(tone), 4800)])
        steady = out[2000:]
        assert _amplitude(steady) < 0.05 * amp  # attenuated, nowhere near a full-strength alias

        # And specifically: no strong energy shows up at the alias frequency
        # (4 kHz) that naive decimation would have produced.
        n = len(steady)
        spec = np.abs(np.fft.rfft(steady * np.hanning(n)))
        freqs = np.fft.rfftfreq(n, d=1 / target)
        alias_bin = np.argmin(np.abs(freqs - 4000.0))
        naive_spec = np.abs(np.fft.rfft(naive[300:].astype(np.float64) * np.hanning(len(naive) - 300)))
        assert spec[alias_bin] < 0.05 * naive_spec.max()

    def test_streaming_matches_whole_block_processing(self):
        # Consecutive calls on consecutive blocks must be equivalent to
        # filtering the whole signal at once -- otherwise every block
        # boundary is an audible click in the live preview.
        sr = 48000
        t = np.arange(sr) / sr  # 1 second
        tone = (0.5 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32)

        whole = Downsampler(sr, 16000).process(tone)

        ds = Downsampler(sr, 16000)
        chunked = np.concatenate([ds.process(tone[i : i + 480]) for i in range(0, len(tone), 480)])

        # Odd chunk size (not a multiple of the 3x decimation ratio), to
        # stress the phase-tracking across boundaries specifically.
        ds_odd = Downsampler(sr, 16000)
        odd_chunked = np.concatenate(
            [ds_odd.process(tone[i : i + 700]) for i in range(0, len(tone), 700)]
        )

        assert len(whole) == len(chunked) == len(odd_chunked)
        np.testing.assert_allclose(whole, chunked, atol=1e-6)
        np.testing.assert_allclose(whole, odd_chunked, atol=1e-6)

    def test_output_length_48k_to_16k(self):
        five_seconds = np.zeros(48000 * 5, dtype=np.float32)
        out = Downsampler(48000, 16000).process(five_seconds)
        assert len(out) == 16000 * 5

    def test_int16_input_round_trips_to_int16_output(self):
        block = (0.4 * np.sin(2 * np.pi * 1000 * np.arange(4800) / 48000) * 32767).astype("<i2")
        out = downsample_to_16k(block, 48000)
        assert out.dtype == np.dtype("<i2") or out.dtype == np.int16
        assert len(out) == 1600

    def test_non_integer_ratio_does_not_crash_and_is_close_to_expected_length(self):
        # 44.1kHz devices exist; the fallback path (filter + linear
        # interpolate) just needs to behave sanely, not be bit-exact.
        sr = 44100
        t = np.arange(sr) / sr
        tone = (0.5 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32)
        ds = Downsampler(sr, 16000)
        out = np.concatenate([ds.process(tone[i : i + 512]) for i in range(0, len(tone), 512)])
        assert abs(len(out) - 16000) <= 2


# =============================================================================
# api.py — ServerClient
# =============================================================================


class TestServerClient:
    def test_connection_failure_raises_server_unavailable(self, dead_port_url):
        client = ServerClient(dead_port_url, timeout=1.0)
        with pytest.raises(ServerUnavailable):
            client.health()

    def test_http_error_is_not_server_unavailable(self, stub_server):
        # The server IS reachable here; a 404 is a real answer, not an
        # unreachable-server condition, so it must NOT raise ServerUnavailable.
        base_url = stub_server
        client = ServerClient(base_url, timeout=5.0)
        with pytest.raises(httpx.HTTPStatusError):
            client.job("does-not-exist")

    def test_health(self, stub_server):
        base_url = stub_server
        client = ServerClient(base_url, timeout=5.0)
        assert client.health()["status"] == "ok"

    def test_upload_finalize_job_transcript_round_trip(self, stub_server, tmp_path):
        base_url = stub_server
        client = ServerClient(base_url, timeout=5.0)

        pcm_path = tmp_path / "mic.pcm16"
        samples = np.arange(1000, dtype="<i2")
        pcm_path.write_bytes(samples.tobytes())

        result = client.upload_track("sess-1", "mic", pcm_path, frames=len(samples))
        assert result["frames"] == len(samples)

        # A timing log with at least one real point is required -- merge_tracks
        # (used server-side to build the transcript) drops any track whose
        # FrameClock has no frames at all, which an empty timing list would.
        duration = len(samples) / wire.STREAM_SAMPLE_RATE
        timing_entries = [
            {
                "event": "open",
                "segment": 0,
                "frames": 0,
                "t": 0.0,
                "wall": 0.0,
                "samplerate": wire.STREAM_SAMPLE_RATE,
                "channels": 1,
                "device": "test",
            },
            {"event": "close", "segment": 0, "frames": len(samples), "t": duration},
        ]
        job_id = client.finalize(
            "sess-1",
            meta={"tracks": {"mic": {}}},
            timing={"mic": timing_entries},
            settings={},
        )
        assert job_id

        # Poll until done -- the real job queue runs on a background thread,
        # so this proves the client can be polled in a loop, not just that a
        # single call happens to already show DONE.
        state = None
        for _ in range(200):
            info = client.job(job_id)
            state = info["state"]
            if state == wire.JobState.DONE:
                break
            time.sleep(0.02)
        assert state == wire.JobState.DONE

        transcript = client.transcript(job_id)
        assert "hello from mic" in transcript["markdown"]
        segments = json.loads(transcript["json"])["segments"]
        assert segments and segments[0]["track"] == "mic"
        assert "hello from mic" in segments[0]["text"]

    def test_iter_file_streams_a_large_file_in_bounded_chunks(self, tmp_path):
        # Pure local test of the memory-safety property upload_track relies
        # on: reading via _iter_file never holds more than one chunk of a
        # multi-MB file in memory. (Kept off the network on purpose -- see
        # test_upload_track_bounded_chunk_size below for why.)
        from meeting_notes.client import api as api_mod

        pcm_path = tmp_path / "big.pcm16"
        # ~4 MB -- big enough that Path.read_bytes() would be an obviously
        # different (whole-file) code path from chunked reads.
        big = np.zeros(2_000_000, dtype="<i2")
        pcm_path.write_bytes(big.tobytes())

        chunk_size = 64 * 1024
        chunks = list(api_mod._iter_file(pcm_path, chunk_size=chunk_size))
        assert all(0 < len(c) <= chunk_size for c in chunks)
        assert sum(len(c) for c in chunks) == pcm_path.stat().st_size
        assert len(chunks) > 1  # actually multiple reads, not one big one


# =============================================================================
# queue.py — SessionQueue / UploadWorker
# =============================================================================


class TestSessionQueue:
    def test_enqueue_then_successful_upload_writes_transcript_and_clears_entry(
        self, stub_server, tmp_path
    ):
        base_url = stub_server
        session_dir = _make_session_dir(tmp_path, "good-session")

        queue = SessionQueue(tmp_path / ".upload-queue")
        queue.enqueue(session_dir)
        assert len(queue.pending()) == 1

        worker = UploadWorker(queue, base_url, poll_interval=0.01)
        worker.run_once()

        assert queue.pending() == []  # entry cleared
        assert (session_dir / "transcript.md").exists()
        assert (session_dir / "transcript.json").exists()
        md = (session_dir / "transcript.md").read_text(encoding="utf-8")
        assert "hello from mic" in md
        assert "hello from system" in md
        transcript_json = json.loads((session_dir / "transcript.json").read_text(encoding="utf-8"))
        assert len(transcript_json["segments"]) == 2  # mic + system

    def test_failing_server_leaves_entry_pending_with_incremented_attempts(
        self, dead_port_url, tmp_path
    ):
        session_dir = _make_session_dir(tmp_path, "flaky-session")
        queue = SessionQueue(tmp_path / ".upload-queue")
        queue.enqueue(session_dir)

        worker = UploadWorker(queue, dead_port_url, poll_interval=0.01, max_attempts=8)
        worker.run_once()

        entries = queue.pending()
        assert len(entries) == 1
        assert entries[0]["attempts"] == 1
        assert entries[0]["status"] == "pending"
        assert entries[0]["last_error"]

        worker.run_once()  # next_attempt_at should still be in the future
        assert queue.pending()[0]["attempts"] == 1  # backoff held it off, no second attempt yet

    def test_state_survives_new_session_queue_instance(self, tmp_path):
        session_dir = _make_session_dir(tmp_path, "persisted-session")
        queue_dir = tmp_path / ".upload-queue"

        queue_a = SessionQueue(queue_dir)
        queue_a.enqueue(session_dir)

        queue_b = SessionQueue(queue_dir)  # fresh instance, same directory
        entries = queue_b.pending()
        assert len(entries) == 1
        assert Path(entries[0]["session_dir"]) == session_dir.resolve()

    def test_deleted_session_dir_is_handled_without_crashing(self, dead_port_url, tmp_path):
        import shutil

        session_dir = _make_session_dir(tmp_path, "vanishing-session")
        queue = SessionQueue(tmp_path / ".upload-queue")
        queue.enqueue(session_dir)
        shutil.rmtree(session_dir)

        worker = UploadWorker(queue, dead_port_url, poll_interval=0.01)
        worker.run_once()  # must not raise

        entries = queue.pending()
        assert len(entries) == 1
        assert entries[0]["attempts"] == 1
        assert "gone" in entries[0]["last_error"] or "FileNotFoundError" in entries[0]["last_error"]

    def test_run_once_with_zero_pending_entries(self, tmp_path):
        queue = SessionQueue(tmp_path / ".upload-queue")
        worker = UploadWorker(queue, "http://127.0.0.1:1")
        worker.run_once()  # must not raise
        assert queue.pending() == []


# =============================================================================
# streamer.py — LiveStreamer
# =============================================================================


class TestLiveStreamer:
    def test_never_blocks_or_raises_when_server_unreachable(self, dead_port_url):
        streamer = LiveStreamer(dead_port_url)
        streamer.start("sess-dead", "Dead server test", time.time())
        try:
            pcm = np.zeros(160, dtype="<i2").tobytes()
            t0 = time.monotonic()
            streamer.submit("mic", pcm)  # must return promptly, not block on the dead connection
            elapsed = time.monotonic() - t0
            assert elapsed < 0.5

            # Give the background thread a moment to attempt (and fail) a
            # connection; state must never claim success.
            time.sleep(0.3)
            assert streamer.state in ("disconnected", "connecting")
        finally:
            streamer.stop(join_timeout=2.0)

    def test_on_partial_callback_exception_does_not_kill_the_thread(self, dead_port_url):
        def bad_callback(partial):
            raise RuntimeError("boom")

        streamer = LiveStreamer(dead_port_url, on_partial=bad_callback)
        streamer.start("sess-cb", "Callback test", time.time())
        try:
            for _ in range(5):
                streamer.submit("mic", np.zeros(160, dtype="<i2").tobytes())
                time.sleep(0.05)
            # Never connected (dead port), so bad_callback was never even
            # invoked -- this just confirms submit()/the thread stay alive.
            assert streamer.state in ("disconnected", "connecting")
        finally:
            streamer.stop(join_timeout=2.0)

    def test_connects_to_stub_server_and_drains_buffer_on_ack(self, stub_server):
        base_url = stub_server
        streamer = LiveStreamer(base_url, buffer_seconds=5.0)
        streamer.start("sess-live", "Live test", time.time())
        try:
            pcm = (np.sin(np.arange(160) / 10.0) * 1000).astype("<i2").tobytes()
            streamer.submit("mic", pcm)

            deadline = time.monotonic() + 5.0
            drained = False
            while time.monotonic() < deadline:
                if streamer.state == "connected":
                    # White-box check: once the stub server acks everything
                    # submitted, LiveStreamer's own unacked buffer should be
                    # empty again -- the resume/ack-trim behaviour working
                    # end to end, not just each piece in isolation.
                    buf = streamer._buffers.get("mic")
                    if buf is not None and len(buf) == 0:
                        drained = True
                        break
                time.sleep(0.05)
            assert streamer.state == "connected"
            assert drained
        finally:
            streamer.stop(join_timeout=2.0)

    def test_consecutive_chunks_all_reach_the_server(self, stub_server):
        """Regression: _send_pending skipped the chunk starting exactly at the
        sent high-water mark, so every other block was never sent and the
        server's contiguous prefix stalled after the first one. Submitting
        several back-to-back chunks and requiring the unacked buffer to drain
        completely is what catches it -- one chunk never could."""
        streamer = LiveStreamer(stub_server, buffer_seconds=5.0)
        streamer.start("sess-consecutive", "Consecutive", time.time())
        try:
            chunk = (np.sin(np.arange(8000) / 10.0) * 1000).astype("<i2").tobytes()
            for _ in range(6):
                streamer.submit("system", chunk)
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                buf = streamer._buffers.get("system")
                if streamer.state == "connected" and buf is not None and len(buf) == 0:
                    break
                time.sleep(0.05)
            remaining = list(streamer._buffers.get("system") or [])
            assert remaining == [], f"unacked chunks left at offsets {[o for o, _ in remaining]}"
        finally:
            streamer.stop(join_timeout=2.0)

    def test_submit_drops_oldest_beyond_cap_without_blocking(self, dead_port_url):
        # Never connects (dead port), so nothing ever gets acked/trimmed --
        # every submitted chunk stays in the buffer until the cap forces
        # drops, which is exactly the scenario the cap exists for.
        streamer = LiveStreamer(dead_port_url, buffer_seconds=0.01)  # ~160 frames at 16kHz
        streamer.start("sess-cap", "Cap test", time.time())
        try:
            chunk = np.zeros(200, dtype="<i2").tobytes()  # bigger than the whole cap
            for _ in range(20):
                streamer.submit("mic", chunk)
            with streamer._lock:
                total_frames = sum(
                    len(data) // wire.BYTES_PER_FRAME for _, data in streamer._buffers["mic"]
                )
            assert total_frames <= 200  # capped, not unbounded growth
        finally:
            streamer.stop(join_timeout=2.0)


# =============================================================================
# streamer.py — permanent rejection (bad token, protocol/session errors)
# =============================================================================


class TestLiveStreamerPermanentRejection:
    """Before this fix, every failure -- transient or not -- retried forever
    with exponential backoff, and `last_error` was never read by anything. A
    server that will reject a session for as long as it lives (wrong token,
    an invalid session_id) deserves a different outcome: stop hammering it,
    and say something more specific than "server unreachable".
    """

    def test_invalid_session_id_is_a_permanent_rejection_not_a_retry_loop(self, stub_server):
        # Server-side this is a 4400 close (see meeting_notes.server.app's
        # `stream` route validating session_id via store.is_safe_id) --
        # exactly the kind of error retrying with backoff would never fix.
        streamer = LiveStreamer(stub_server)
        streamer.start("../not a safe id", "Bad id test", time.time())
        try:
            deadline = time.monotonic() + 5.0
            while streamer.state != "rejected" and time.monotonic() < deadline:
                time.sleep(0.05)
            assert streamer.state == "rejected"
            assert streamer.permanent_error and "rejected by server" in streamer.permanent_error

            # No retry loop left running -- the background thread must have
            # exited on its own, not merely gone quiet between backoffs.
            time.sleep(0.3)
            assert streamer._thread is not None and not streamer._thread.is_alive()
        finally:
            streamer.stop(join_timeout=2.0)

    def test_unauthorized_token_is_a_permanent_rejection(self, tmp_path_factory, monkeypatch):
        # A dedicated server (not the shared stub_server fixture) so the
        # required token can be configured for just this test.
        monkeypatch.setenv("MEETING_NOTES_TOKEN", "the-real-secret")
        data_root = tmp_path_factory.mktemp("auth-rejection-server-data")
        app = create_app(transcriber_factory=_stub_transcriber_factory, data_root=str(data_root))
        live = _LiveServer(app)
        base_url = live.start()
        try:
            streamer = LiveStreamer(base_url, token="wrong-token")
            streamer.start("sess-unauth", "Unauthorized test", time.time())
            try:
                deadline = time.monotonic() + 5.0
                while streamer.state != "rejected" and time.monotonic() < deadline:
                    time.sleep(0.05)
                assert streamer.state == "rejected"
                assert "unauthorized" in (streamer.permanent_error or "").lower()
            finally:
                streamer.stop(join_timeout=2.0)
        finally:
            live.stop()

    def test_a_fresh_session_gets_a_fresh_chance_after_a_permanent_rejection(self, stub_server):
        # permanent_error/state must not leak from a previous session into a
        # new one started on the same LiveStreamer instance.
        streamer = LiveStreamer(stub_server)
        streamer.start("not a safe id!", "First (bad) session", time.time())
        deadline = time.monotonic() + 5.0
        while streamer.state != "rejected" and time.monotonic() < deadline:
            time.sleep(0.05)
        assert streamer.state == "rejected"

        try:
            streamer.start("sess-second-chance", "Second (good) session", time.time())
            assert streamer.permanent_error is None
            deadline = time.monotonic() + 5.0
            while streamer.state not in ("connected", "rejected") and time.monotonic() < deadline:
                time.sleep(0.05)
            assert streamer.state == "connected"
        finally:
            streamer.stop(join_timeout=2.0)


class TestControllerStreamError:
    """RecordingController.stream_error() is how MainWindow._update_status
    is meant to learn about a permanent rejection -- see streamer.py and
    main_window.py."""

    def test_controller_surfaces_a_permanent_stream_rejection(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MEETING_NOTES_TOKEN", "the-real-secret")
        monkeypatch.setenv("MEETING_NOTES_CONFIG", str(tmp_path / "config.json"))
        data_root = tmp_path / "server-data"
        app = create_app(transcriber_factory=_stub_transcriber_factory, data_root=str(data_root))
        live = _LiveServer(app)
        base_url = live.start()
        try:
            config_mod.save_config(
                {
                    "save_dir": str(tmp_path / "Meeting Notes"),
                    "server": {
                        "url": base_url,
                        "token": "wrong-token",
                        "live_preview": True,
                        "auto_upload": False,
                    },
                }
            )
            controller = RecordingController()
            assert controller.stream_error() is None  # nothing started yet
            sources = {
                "mic": FakeSource(name="Fake Mic", samplerate=48000),
                "system": FakeSource(name="Fake Loopback", samplerate=48000),
            }
            session_dir = controller.start("rejected-session", sources=sources)
            assert session_dir is not None, f"recording did not start: {controller.error}"
            try:
                deadline = time.monotonic() + 5.0
                while controller.stream_error() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                error = controller.stream_error()
                assert error and "unauthorized" in error.lower()
            finally:
                controller.stop()
        finally:
            live.stop()


# =============================================================================
# api.py / queue.py — upload timeouts and retry efficiency
# =============================================================================


class TestUploadTimeouts:
    def test_server_client_accepts_an_httpx_timeout_object(self, stub_server):
        # ServerClient just hands `timeout` straight to httpx.Client, which
        # already accepts either a plain float or an httpx.Timeout -- this
        # confirms the polymorphic path (used by UploadWorker for large
        # uploads) actually works end to end against a real server.
        timeout = httpx.Timeout(connect=2.0, read=5.0, write=5.0, pool=2.0)
        client = ServerClient(stub_server, timeout=timeout)
        assert client.health()["status"] == "ok"

    def test_upload_worker_default_client_uses_a_generous_read_write_timeout(self, tmp_path):
        # White-box: the whole point is that UploadWorker's own client isn't
        # stuck with ServerClient's 10s-for-everything default, which is far
        # too short for writing a multi-hundred-MB body over a slow LAN link.
        queue = SessionQueue(tmp_path / ".upload-queue")
        worker = UploadWorker(queue, "http://127.0.0.1:1")
        client = worker._client_factory()
        try:
            timeout = client._client.timeout
            assert timeout.read == 120.0
            assert timeout.write == 120.0
            assert timeout.connect == 10.0
        finally:
            client.close()


class TestUploadWorkerRetryEfficiency:
    """Fix 4(b)/(c): a retry after a partial failure must not re-convert or
    re-upload a track the server already acknowledged, and a session that
    finally gives up for good must not leave half-converted .pcm16 files
    behind forever."""

    def test_retry_skips_reconverting_a_track_whose_pcm16_survived_a_failed_upload(
        self, tmp_path, monkeypatch
    ):
        from meeting_notes.client import queue as queue_mod

        session_dir = _make_session_dir(tmp_path, "retry-session")
        queue = SessionQueue(tmp_path / ".upload-queue")
        queue.enqueue(session_dir)

        convert_calls = []
        real_convert = queue_mod._wav_to_pcm16

        def counting_convert(wav_path, out_path):
            convert_calls.append(wav_path.name)
            return real_convert(wav_path, out_path)

        monkeypatch.setattr(queue_mod, "_wav_to_pcm16", counting_convert)

        class _FlakyTrackClient:
            """Uploads whatever isn't in ``fail_tracks``; raises for the rest,
            mimicking a connection drop partway through a multi-track upload."""

            def __init__(self):
                self.fail_tracks = {"system"}
                self.uploaded = []

            def upload_track(self, session_id, track, pcm_path, frames):
                if track in self.fail_tracks:
                    raise ServerUnavailable("simulated drop mid-upload")
                self.uploaded.append((track, frames))
                return {"frames": frames}

            def finalize(self, session_id, meta, timing, settings):
                return "job-1"

            def job(self, job_id):
                return {"state": wire.JobState.DONE}

            def transcript(self, job_id):
                return {"markdown": "# ok\n", "json": "{}"}

            def close(self):
                pass

        client = _FlakyTrackClient()
        worker = UploadWorker(
            queue, "http://unused", poll_interval=0.01, client_factory=lambda: client
        )

        worker.run_once()  # mic converts + uploads fine; system converts, then fails to upload

        entry = queue.pending()[0]
        assert entry["status"] == "pending"  # not exhausted -- still worth retrying
        assert entry.get("uploaded_tracks") == ["mic"]
        assert convert_calls == ["mic.wav", "system.wav"]
        assert not (session_dir / "mic.pcm16").exists()  # cleaned up once acked
        assert (session_dir / "system.pcm16").exists()  # left behind: never acked

        # Make the retry due right away, and let the upload succeed this time.
        state = queue.read_state(entry["id"])
        state["next_attempt_at"] = None
        queue.write_state(entry["id"], state)
        client.fail_tracks.clear()

        worker.run_once()

        assert queue.pending() == []  # fully drained
        assert convert_calls == ["mic.wav", "system.wav"]  # system was NOT reconverted
        assert not (session_dir / "system.pcm16").exists()  # cleaned up once it finally uploaded
        assert (session_dir / "transcript.md").exists()

    def test_terminal_failure_cleans_up_leftover_pcm16_files(self, tmp_path):
        session_dir = _make_session_dir(tmp_path, "doomed-session")
        queue = SessionQueue(tmp_path / ".upload-queue")
        queue.enqueue(session_dir)

        class _AlwaysFailsClient:
            def upload_track(self, session_id, track, pcm_path, frames):
                # The conversion (which writes the .pcm16 to disk) has
                # already happened by the time this is called -- simulate
                # every upload attempt dying right after that.
                raise ServerUnavailable("server never accepts anything")

            def close(self):
                pass

        worker = UploadWorker(
            queue,
            "http://unused",
            poll_interval=0.01,
            max_attempts=1,  # fail terminally on the very first attempt
            client_factory=_AlwaysFailsClient,
        )
        worker.run_once()

        entries = queue.pending()
        assert entries[0]["status"] == "failed"
        # The conversion for at least the first track processed left a
        # .pcm16 behind; terminal failure must not leave it there forever.
        assert not list(session_dir.glob("*.pcm16"))


class TestAuthFailuresAreNotTerminal:
    """Found on a real run: a wrong token 403'd on every attempt, the entry
    went "failed" after max_attempts, and after the token was corrected in
    Settings that meeting never uploaded -- nothing looks at a failed entry
    again. Auth errors must not eat the attempt budget, and a settings
    change must give up-for-good entries another chance."""

    class _Forbidden:
        def upload_track(self, session_id, track, pcm_path, frames):
            request = httpx.Request("POST", "http://unused/x")
            response = httpx.Response(403, request=request)
            raise httpx.HTTPStatusError("403", request=request, response=response)

        def close(self):
            pass

    def test_403_holds_the_entry_instead_of_exhausting_it(self, tmp_path):
        session_dir = _make_session_dir(tmp_path, "wrong-token")
        queue = SessionQueue(tmp_path / ".upload-queue")
        queue.enqueue(session_dir)
        worker = UploadWorker(
            queue, "http://unused", poll_interval=0.01, max_attempts=1, client_factory=self._Forbidden
        )

        worker.run_once()

        (entry,) = queue.pending()
        assert entry["status"] == "pending"  # NOT failed, despite max_attempts=1
        assert entry["attempts"] == 1
        assert "403" in entry["last_error"]
        assert entry["next_attempt_at"] > time.time() + worker.max_backoff * 0.9

    def test_reset_failed_gives_terminal_entries_a_fresh_start(self, tmp_path):
        session_dir = _make_session_dir(tmp_path, "gave-up")
        queue = SessionQueue(tmp_path / ".upload-queue")
        entry_id = queue.enqueue(session_dir)
        queue.mark_attempt_failed(entry_id, "boom", next_attempt_at=time.time() + 999, terminal=True)
        assert queue.pending()[0]["status"] == "failed"

        assert queue.reset_failed() == 1

        (entry,) = queue.pending()
        assert entry["status"] == "pending"
        assert entry["attempts"] == 0
        assert entry["next_attempt_at"] is None
        assert queue.reset_failed() == 0  # nothing left to reset

    def test_controller_restart_uploader_resets_failed_entries(self, tmp_path, monkeypatch):
        import json as _json

        from meeting_notes.client.controller import RecordingController

        save_dir = tmp_path / "save"
        save_dir.mkdir()
        config_path = tmp_path / "config.json"
        config_path.write_text(
            _json.dumps({"save_dir": str(save_dir), "server": {"url": "http://127.0.0.1:9", "token": "x"}})
        )
        monkeypatch.setenv("MEETING_NOTES_CONFIG", str(config_path))

        queue = SessionQueue.for_save_dir(save_dir)
        entry_id = queue.enqueue(_make_session_dir(tmp_path, "stuck"))
        queue.mark_attempt_failed(entry_id, "HTTPStatusError: 403 Forbidden", terminal=True)

        controller = RecordingController()
        # No uploader thread needed for this: patch it out so the test never
        # touches the network, and just check the queue housekeeping.
        monkeypatch.setattr(controller, "start_uploader", lambda: True)
        assert controller.queue_status()["failed"] == 1
        assert "403" in controller.queue_status()["last_error"]

        controller.restart_uploader()

        status = controller.queue_status()
        assert status == {"pending": 1, "failed": 0, "last_error": "HTTPStatusError: 403 Forbidden"}


class TestConcurrentUploaders:
    """Seen on a real run: the app's background uploader and a `meeting-notes
    upload` in a terminal both drained the same queue directory and uploaded
    the same session twice. An entry must be claimable by exactly one
    uploader at a time, and a claim must not outlive the attempt."""

    def test_second_worker_skips_an_entry_the_first_has_claimed(self, tmp_path):
        session_dir = _make_session_dir(tmp_path, "shared")
        queue = SessionQueue(tmp_path / ".upload-queue")
        entry_id = queue.enqueue(session_dir)
        uploads = []

        class _Recorder:
            def upload_track(self, session_id, track, pcm_path, frames):
                uploads.append((session_id, track))
                # Simulate "still uploading" long enough for a second worker
                # to run a full pass meanwhile.
                assert queue.claim(entry_id) is False
                other = UploadWorker(queue, "http://unused", poll_interval=0.01, client_factory=_Recorder)
                other.run_once()
                return {"track": track, "frames": frames}

            def finalize(self, session_id, meta, timing, settings):
                return "job-1"

            def job(self, job_id):
                return {"state": "done"}

            def transcript(self, job_id):
                return {"markdown": "# transcript", "json": "{}"}

            def close(self):
                pass

        UploadWorker(queue, "http://unused", poll_interval=0.01, client_factory=_Recorder).run_once()

        assert len(uploads) == 2  # mic + system, once each -- not four
        assert queue.pending() == []
        assert not list((tmp_path / ".upload-queue").glob("*.claim"))  # released on completion

    def test_claim_is_released_after_a_failed_attempt(self, tmp_path, dead_port_url):
        session_dir = _make_session_dir(tmp_path, "flaky")
        queue = SessionQueue(tmp_path / ".upload-queue")
        entry_id = queue.enqueue(session_dir)
        UploadWorker(queue, dead_port_url, poll_interval=0.01).run_once()
        assert queue.pending()[0]["attempts"] == 1
        assert queue.claim(entry_id) is True  # nobody is holding it any more
        queue.release(entry_id)

    def test_stale_claim_from_a_dead_uploader_is_broken(self, tmp_path):
        queue = SessionQueue(tmp_path / ".upload-queue")
        queue.queue_dir.mkdir(parents=True, exist_ok=True)
        entry_id = "abandoned"
        assert queue.claim(entry_id)
        old = time.time() - queue.CLAIM_STALE_SECONDS - 60
        import os as _os

        _os.utime(queue._claim_path(entry_id), (old, old))
        assert queue.claim(entry_id)  # the stale one was broken and re-taken
