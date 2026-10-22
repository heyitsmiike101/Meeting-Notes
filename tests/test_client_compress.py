"""Compressing finished recordings to Opus (meeting_notes.client.compress)."""

import json
import wave

import numpy as np
import pytest

from meeting_notes.client import compress

pytestmark = pytest.mark.skipif(not compress.available(), reason="PyAV with libopus is required")


def make_wav(path, seconds=3.0, rate=48000):
    t = np.arange(int(seconds * rate)) / rate
    rng = np.random.default_rng(1)
    sig = 0.3 * np.sin(2 * np.pi * 440 * t) + 0.02 * rng.standard_normal(t.shape[0])
    pcm = (np.clip(sig, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(rate)
        fh.writeframes(pcm.tobytes())
    return seconds


def make_session(tmp_path, tracks=("mic", "system"), seconds=3.0):
    d = tmp_path / "s1"
    d.mkdir()
    (d / "session.json").write_text(json.dumps({"tracks": {t: {"wav": f"{t}.wav"} for t in tracks}}))
    for t in tracks:
        make_wav(d / f"{t}.wav", seconds)
    return d


def test_compress_track_replaces_wav_with_opus(tmp_path):
    wav = tmp_path / "mic.wav"
    seconds = make_wav(wav, 4.0)
    wav_size = wav.stat().st_size
    out = compress.compress_track(wav)
    assert out == tmp_path / "mic.opus"
    assert out.is_file() and not wav.exists()
    assert not list(tmp_path.glob("*.tmp*"))
    assert out.stat().st_size < wav_size / 20
    assert abs(compress.audio_duration(out) - seconds) < 0.1


def test_decode_to_pcm16_matches_duration(tmp_path):
    wav = tmp_path / "mic.wav"
    make_wav(wav, 3.0)
    out = compress.compress_track(wav)
    frames = compress.decode_to_pcm16(out, tmp_path / "mic.pcm16")
    assert abs(frames - 3 * 16000) < 800
    assert (tmp_path / "mic.pcm16").stat().st_size == frames * 2


def test_failure_keeps_wav(tmp_path, monkeypatch):
    wav = tmp_path / "mic.wav"
    make_wav(wav, 2.0)

    def boom(*a, **k):
        raise RuntimeError("encoder exploded")

    monkeypatch.setattr(compress, "_encode", boom)
    with pytest.raises(RuntimeError):
        compress.compress_track(wav)
    assert wav.is_file()
    assert [p.name for p in tmp_path.iterdir()] == ["mic.wav"]


def test_bad_duration_keeps_wav(tmp_path, monkeypatch):
    wav = tmp_path / "mic.wav"
    make_wav(wav, 3.0)
    monkeypatch.setattr(compress, "decode_to_pcm16", lambda src, dst: 16000)
    with pytest.raises(ValueError):
        compress.compress_track(wav)
    assert wav.is_file()
    assert [p.name for p in tmp_path.iterdir()] == ["mic.wav"]


def test_compress_session_and_idempotent(tmp_path):
    d = make_session(tmp_path)
    result = compress.compress_session(d)
    assert sorted(result["compressed"]) == ["mic", "system"] and not result["errors"]
    assert sorted(p.name for p in d.iterdir()) == ["mic.opus", "session.json", "system.opus"]
    again = compress.compress_session(d)
    assert again["compressed"] == [] and not again["errors"]


def test_session_with_raw_is_skipped(tmp_path):
    d = make_session(tmp_path)
    (d / "mic.raw").write_bytes(b"\0\0")
    result = compress.compress_session(d)
    assert result["skipped"] and result["compressed"] == []
    assert (d / "mic.wav").is_file()


def test_session_without_json_is_skipped(tmp_path):
    d = make_session(tmp_path)
    (d / "session.json").unlink()
    assert compress.compress_session(d)["compressed"] == []
    assert (d / "mic.wav").is_file()


def test_unavailable_is_a_noop(tmp_path, monkeypatch):
    d = make_session(tmp_path)
    monkeypatch.setattr(compress, "available", lambda: False)
    result = compress.compress_session(d)
    assert result["compressed"] == [] and result["skipped"]
    assert (d / "mic.wav").is_file()


def test_track_failure_is_reported_not_raised(tmp_path, monkeypatch):
    d = make_session(tmp_path)
    monkeypatch.setattr(compress, "_encode", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    result = compress.compress_session(d)
    assert set(result["errors"]) == {"mic", "system"}
    assert (d / "mic.wav").is_file()


def test_track_audio_path_prefers_opus(tmp_path):
    d = make_session(tmp_path, tracks=("mic",))
    assert compress.track_audio_path(d, "mic", {"wav": "mic.wav"}).name == "mic.wav"
    compress.compress_session(d)
    assert compress.track_audio_path(d, "mic", {"wav": "mic.wav"}).name == "mic.opus"
    assert compress.track_audio_path(d, "system") is None


def test_compress_folder_skips_active_and_queued(tmp_path):
    from meeting_notes.client.queue import SessionQueue

    root = tmp_path / "rec"
    root.mkdir()
    dirs = {}
    for name in ("done", "active", "queued"):
        d = root / name
        d.mkdir()
        (d / "session.json").write_text(json.dumps({"tracks": {"mic": {"wav": "mic.wav"}}}))
        make_wav(d / "mic.wav", 1.0)
        dirs[name] = d
    SessionQueue.for_save_dir(root).enqueue(dirs["queued"])
    result = compress.compress_folder(root, active_dir=dirs["active"])
    assert list(result) == ["done"]
    assert (dirs["done"] / "mic.opus").is_file()
    assert (dirs["active"] / "mic.wav").is_file() and (dirs["queued"] / "mic.wav").is_file()
