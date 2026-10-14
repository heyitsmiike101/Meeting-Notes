"""The "Both sides" playback track: mixing, cache, deletion, endpoint, page."""

from __future__ import annotations

import os
import wave
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from meeting_notes import wire
from meeting_notes.server import mixdown
from meeting_notes.server.app import create_app
from meeting_notes.server.store import Store

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated:DeprecationWarning"
)


def tone(seconds, rate, freq, amp=0.4, channels=1):
    t = np.arange(int(seconds * rate)) / rate
    mono = amp * np.sin(2 * np.pi * freq * t)
    data = np.repeat(mono[:, None], channels, axis=1) if channels > 1 else mono[:, None]
    return np.rint(data * 32767).astype("<i2")


def write_wav(path: Path, samples: np.ndarray, rate: int) -> Path:
    with wave.open(str(path), "wb") as fh:
        fh.setnchannels(samples.shape[1])
        fh.setsampwidth(2)
        fh.setframerate(rate)
        fh.writeframes(samples.tobytes())
    return path


def read_wav(path: Path):
    with wave.open(str(path), "rb") as fh:
        raw = fh.readframes(fh.getnframes())
        return np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0, fh.getframerate(), fh.getnchannels()


def dominant_freq(samples: np.ndarray, rate: int) -> float:
    spectrum = np.abs(np.fft.rfft(samples))
    return float(np.argmax(spectrum) * rate / len(samples))


# -- mixing ----------------------------------------------------------------------


def test_two_tones_at_the_same_rate_sum_with_half_gain(tmp_path):
    a = write_wav(tmp_path / "mic.wav", tone(2, 16000, 440), 16000)
    b = write_wav(tmp_path / "system.wav", tone(2, 16000, 1000), 16000)
    frames = mixdown.mix_wavs([a, b], tmp_path / "out.wav")
    out, rate, channels = read_wav(tmp_path / "out.wav")
    assert (rate, channels, frames, len(out)) == (16000, 1, 32000, 32000)
    expected = 0.5 * (tone(2, 16000, 440)[:, 0] + tone(2, 16000, 1000)[:, 0]).astype(np.float64) / 32768.0
    assert np.max(np.abs(out - expected)) < 2e-4
    # both tones are audible in the result
    spectrum = np.abs(np.fft.rfft(out))
    freqs = np.fft.rfftfreq(len(out), 1 / 16000)
    peaks = sorted(freqs[np.argsort(spectrum)[-2:]])
    assert [round(p) for p in peaks] == [440, 1000]


def test_different_rates_channels_and_lengths(tmp_path):
    mic = write_wav(tmp_path / "mic.wav", tone(3, 16000, 500), 16000)  # 3 s mono 16 kHz
    system = write_wav(tmp_path / "system.wav", tone(5, 48000, 1500, channels=2), 48000)  # 5 s stereo 48 kHz
    frames = mixdown.mix_wavs([mic, system], tmp_path / "out.wav")
    out, rate, channels = read_wav(tmp_path / "out.wav")
    assert (rate, channels) == (16000, 1)
    assert len(out) == frames == 5 * 16000  # the longer track decides the length
    # aligned: during the first 3 s both tones, afterwards only the 1500 Hz one at half gain
    both = out[: 3 * 16000]
    only_system = out[3 * 16000 + 400 :]
    spectrum = np.abs(np.fft.rfft(both))
    freqs = np.fft.rfftfreq(len(both), 1 / 16000)
    assert sorted(round(f) for f in freqs[np.argsort(spectrum)[-2:]]) == [500, 1500]
    assert round(dominant_freq(only_system, 16000)) == 1500
    assert np.max(np.abs(only_system)) == pytest.approx(0.5 * 0.4, abs=0.02)
    # a stretch where the mic is silent-by-absence is not louder than where it plays
    assert np.max(np.abs(both)) <= 1.0


def test_output_rate_is_the_lower_common_rate(tmp_path):
    a = write_wav(tmp_path / "a.wav", tone(1, 8000, 300), 8000)
    b = write_wav(tmp_path / "b.wav", tone(1, 44100, 300), 44100)
    mixdown.mix_wavs([a, b], tmp_path / "out.wav")
    out, rate, _ = read_wav(tmp_path / "out.wav")
    assert rate == 8000 and len(out) == 8000


def test_full_scale_inputs_never_clip(tmp_path):
    loud = np.full((1600, 1), 32767, dtype="<i2")
    a = write_wav(tmp_path / "a.wav", loud, 16000)
    b = write_wav(tmp_path / "b.wav", loud, 16000)
    mixdown.mix_wavs([a, b], tmp_path / "out.wav")
    out, _, _ = read_wav(tmp_path / "out.wav")
    assert out.max() <= 32767 / 32768 and out.min() > 0.99


def test_block_processing_matches_one_shot_for_long_input(tmp_path):
    mic = write_wav(tmp_path / "mic.wav", tone(7, 16000, 440), 16000)
    system = write_wav(tmp_path / "system.wav", tone(9, 44100, 1200, channels=2), 44100)
    mixdown.mix_wavs([mic, system], tmp_path / "one.wav", block_seconds=60)
    mixdown.mix_wavs([mic, system], tmp_path / "blocks.wav", block_seconds=0.37)
    one, _, _ = read_wav(tmp_path / "one.wav")
    blocks, _, _ = read_wav(tmp_path / "blocks.wav")
    assert len(one) == len(blocks) == 9 * 16000
    # block edges must not introduce clicks: identical to the one-shot result
    assert np.max(np.abs(one - blocks)) < 2.0 / 32768


def test_unreadable_source_is_a_mix_error(tmp_path):
    bad = tmp_path / "mic.wav"
    bad.write_bytes(b"not a wav")
    good = write_wav(tmp_path / "system.wav", tone(1, 16000, 300), 16000)
    with pytest.raises(mixdown.MixError):
        mixdown.mix_wavs([bad, good], tmp_path / "out.wav")


def test_cache_is_reused_and_rebuilt_when_a_source_is_newer(tmp_path):
    a = write_wav(tmp_path / "mic.wav", tone(1, 16000, 440), 16000)
    b = write_wav(tmp_path / "system.wav", tone(1, 16000, 880), 16000)
    cache = tmp_path / mixdown.CACHE_NAME
    assert mixdown.ensure_mixed([a, b], cache) == cache
    first = cache.stat().st_mtime_ns
    mixdown.ensure_mixed([a, b], cache)
    assert cache.stat().st_mtime_ns == first  # served from cache
    # replace a source with a longer, newer file
    write_wav(a, tone(2, 16000, 440), 16000)
    later = cache.stat().st_mtime + 5
    os.utime(a, (later, later))
    mixdown.ensure_mixed([a, b], cache)
    out, _, _ = read_wav(cache)
    assert len(out) == 32000
    assert not list(tmp_path.glob("*.tmp"))


# -- the endpoint ----------------------------------------------------------------


@pytest.fixture
def served(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = create_app(transcriber_factory=None, data_root=str(tmp_path / "data"))
    store = Store(tmp_path / "data")
    return TestClient(app), store


def put_session(store: Store, session_id: str, **tracks):
    store.ensure_session_dir(session_id)
    media = store.ensure_media_session_dir(session_id)
    store.session_meta_path(session_id).write_text(
        '{"created": "2026-09-20", "name": "m", "tracks": {%s}}' % ", ".join(f'"{t}": {{}}' for t in tracks)
    )
    for track, (samples, rate) in tracks.items():
        write_wav(media / f"{track}.wav", samples, rate)
    return media


def test_mixed_endpoint_serves_a_wav_with_both_sides(served):
    client, store = served
    put_session(store, "s1", mic=(tone(2, 16000, 440), 16000), system=(tone(3, 48000, 1000, channels=2), 48000))
    resp = client.get("/sessions/s1/audio/mixed")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("audio/wav")
    assert resp.headers.get("accept-ranges") == "bytes"
    assert resp.content.startswith(b"RIFF")
    cache = store.mixed_cache_path("s1")
    out, rate, channels = read_wav(cache)
    assert (rate, channels, len(out)) == (16000, 1, 3 * 16000)
    # per-track routes still work and "mixed" is not read as a track
    assert client.get("/sessions/s1/audio/mic").status_code == 200
    assert client.get("/sessions/s1/audio/bogus").status_code == 400


def test_mixed_endpoint_supports_range_requests(served):
    client, store = served
    put_session(store, "s2", mic=(tone(2, 16000, 440), 16000), system=(tone(2, 16000, 880), 16000))
    full = client.get("/sessions/s2/audio/mixed")
    resp = client.get("/sessions/s2/audio/mixed", headers={"Range": "bytes=100-199"})
    assert resp.status_code == 206
    assert resp.headers["content-range"] == f"bytes 100-199/{len(full.content)}"
    assert resp.content == full.content[100:200]


def test_mixed_endpoint_with_one_track_serves_that_track(served):
    client, store = served
    media = put_session(store, "s3", system=(tone(1, 16000, 880), 16000))
    resp = client.get("/sessions/s3/audio/mixed")
    assert resp.status_code == 200
    assert resp.content == (media / "system.wav").read_bytes()
    assert not store.mixed_cache_path("s3").exists()


def test_mixed_endpoint_404s_without_audio_and_rejects_bad_ids(served):
    client, store = served
    store.ensure_session_dir("s4")
    assert client.get("/sessions/s4/audio/mixed").status_code == 404
    assert client.get("/sessions/does-not-exist/audio/mixed").status_code == 404
    assert client.get("/sessions/..%2Fx/audio/mixed").status_code in (400, 404)


def test_mixed_endpoint_requires_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_NOTES_TOKEN", "secret-token")
    app = create_app(transcriber_factory=None, data_root=str(tmp_path / "data"))
    store = Store(tmp_path / "data")
    put_session(store, "s5", mic=(tone(1, 16000, 440), 16000), system=(tone(1, 16000, 880), 16000))
    client = TestClient(app)
    resp = client.get("/sessions/s5/audio/mixed", follow_redirects=False)
    assert resp.status_code in (302, 303, 307, 401)
    assert b"RIFF" not in resp.content
    ok = client.get("/sessions/s5/audio/mixed", headers={"Authorization": "Bearer secret-token"})
    assert ok.status_code == 200 and ok.content.startswith(b"RIFF")


def test_cache_is_not_counted_as_audio_or_a_track_and_goes_with_the_audio(served):
    client, store = served
    media = put_session(store, "s6", mic=(tone(1, 16000, 440), 16000), system=(tone(1, 16000, 880), 16000))
    store._index_upsert_session("s6")
    before = store.session_index_row("s6")
    assert client.get("/sessions/s6/audio/mixed").status_code == 200
    assert store.mixed_cache_path("s6").exists()
    store._index_upsert_session("s6")
    after = store.session_index_row("s6")
    assert after["has_audio"] == before["has_audio"] and after["audio_bytes"] == before["audio_bytes"]
    assert sorted(p.name for p in media.glob("*.wav")) == ["mic.wav", "system.wav"]
    assert set(store.read_session_meta("s6")["tracks"]) == {"mic", "system"}

    freed = store.delete_session_audio("s6")
    assert freed > 0
    assert {p.name for p in media.iterdir()} <= {"session.json"}
    assert client.get("/sessions/s6/audio/mixed").status_code == 404
    assert not store.session_index_row("s6")["has_audio"]


def test_cache_with_stale_temp_files_is_removed_with_the_audio(served):
    client, store = served
    media = put_session(store, "s7", mic=(tone(1, 16000, 440), 16000), system=(tone(1, 16000, 880), 16000))
    (media / "mixed.mix.deadbeef.tmp").write_bytes(b"partial")
    store.delete_session_audio("s7")
    assert {p.name for p in media.iterdir()} <= {"session.json"}


def test_trashing_a_meeting_takes_the_cache_with_it(served):
    client, store = served
    media = put_session(store, "s8", mic=(tone(1, 16000, 440), 16000), system=(tone(1, 16000, 880), 16000))
    assert client.get("/sessions/s8/audio/mixed").status_code == 200
    store.trash_session("s8")
    assert not store.mixed_cache_path("s8").exists()
    assert client.get("/sessions/s8/audio/mixed").status_code == 404


# -- the page ----------------------------------------------------------------------


def test_meeting_page_script_puts_both_sides_before_the_track_players(served):
    client, _store = served
    page = client.get("/meetings").text
    both = page.index("Both sides")
    mic = page.index("You · microphone")
    assert both < mic
    assert "/audio/mixed" in page
    assert "data.has_audio && tracks.mic && tracks.system" in page  # only when there are two sides to mix
    css = client.get("/static/app.css").text if client.get("/static/app.css").status_code == 200 else ""
    assert not css or ".audio-both" in css
