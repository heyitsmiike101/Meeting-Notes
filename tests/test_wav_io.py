"""Hardware-free tests for meeting_notes.wav_io.

These exercise pure sample-format math and crash-safe file handling only --
no real audio device is ever touched.
"""

from __future__ import annotations

import wave

import numpy as np
import pytest

from meeting_notes.wav_io import (
    RawTrackWriter,
    SAMPLE_WIDTH,
    downmix_mono,
    finalize_session,
    float_to_int16,
    read_wav_mono,
    wrap_raw_as_wav,
)


# --------------------------------------------------------------------------
# float_to_int16
# --------------------------------------------------------------------------


def test_float_to_int16_plus_one_does_not_wrap_negative():
    # The classic bug: (1.0 * 32768).astype(int16) == -32768. Assert we land
    # solidly positive near full scale instead.
    out = float_to_int16(np.array([1.0], dtype=np.float32))
    assert out[0] > 0
    assert out[0] == 32767


def test_float_to_int16_minus_one_is_negative_full_scale():
    out = float_to_int16(np.array([-1.0], dtype=np.float32))
    assert out[0] == -32767


def test_float_to_int16_clamps_beyond_range():
    out = float_to_int16(np.array([2.0, -2.0, 5.5, -100.0], dtype=np.float32))
    assert list(out) == [32767, -32767, 32767, -32767]


def test_float_to_int16_rounds_not_truncates():
    # 3.7 / 32767 * 32767 == 3.7 -> should round to 4, not truncate to 3.
    x = 3.7 / 32767.0
    out = float_to_int16(np.array([x], dtype=np.float32))
    assert out[0] == 4


def test_float_to_int16_handles_nan_and_inf():
    out = float_to_int16(np.array([float("nan"), float("inf"), float("-inf")], dtype=np.float32))
    assert np.all(np.isfinite(out))
    assert out[0] == 0  # nan -> 0
    assert out[1] == 32767  # +inf -> clipped to +1.0 -> full scale
    assert out[2] == -32767  # -inf -> clipped to -1.0


# --------------------------------------------------------------------------
# downmix_mono
# --------------------------------------------------------------------------


def test_downmix_mono_averages_not_sums():
    # Two identical full-scale channels must stay at full scale (average),
    # not clip to 2.0 (sum). If this summed, a real stereo signal panned
    # center would distort every time it downmixed.
    block = np.ones((10, 2), dtype=np.float32)
    out = downmix_mono(block)
    assert out.shape == (10,)
    assert np.allclose(out, 1.0)


def test_downmix_mono_averages_differing_channels():
    block = np.array([[1.0, -1.0], [0.5, 0.5]], dtype=np.float32)
    out = downmix_mono(block)
    assert np.allclose(out, [0.0, 0.5])


def test_downmix_mono_passthrough_for_1d():
    block = np.array([0.1, 0.2, -0.3], dtype=np.float32)
    out = downmix_mono(block)
    assert np.allclose(out, block)


def test_downmix_mono_passthrough_for_single_column():
    block = np.array([[0.1], [0.2], [-0.3]], dtype=np.float32)
    out = downmix_mono(block)
    assert out.shape == (3,)
    assert np.allclose(out, [0.1, 0.2, -0.3])


# --------------------------------------------------------------------------
# RawTrackWriter + wrap_raw_as_wav round trip
# --------------------------------------------------------------------------


def test_raw_writer_wrap_round_trip(tmp_path):
    raw_path = tmp_path / "mic.raw"
    wav_path = tmp_path / "mic.wav"
    samplerate = 16000

    known = np.array([0, 100, -100, 32767, -32767, 1234], dtype=np.int16)
    writer = RawTrackWriter(raw_path)
    writer.write_int16(known)
    writer.close()

    frames = wrap_raw_as_wav(raw_path, wav_path, samplerate)
    assert frames == len(known)

    data, rate = read_wav_mono(wav_path)
    assert rate == samplerate
    assert len(data) == len(known)
    # read_wav_mono divides back by 32767.0, same scale float_to_int16 used.
    expected = known.astype(np.float32) / 32767.0
    assert np.allclose(data, expected, atol=1e-4)


def test_raw_writer_tracks_frame_count_across_reopen(tmp_path):
    raw_path = tmp_path / "mic.raw"
    writer = RawTrackWriter(raw_path)
    writer.write_int16(np.array([1, 2, 3], dtype=np.int16))
    writer.close()

    # Reopening (as would happen after a resumed/crashed process) must pick
    # up the existing byte count rather than starting frames back at 0.
    writer2 = RawTrackWriter(raw_path)
    assert writer2.frames == 3
    writer2.close()


def test_write_silence_appends_expected_zero_frames(tmp_path):
    raw_path = tmp_path / "mic.raw"
    writer = RawTrackWriter(raw_path)
    writer.write_int16(np.array([5, 5, 5], dtype=np.int16))
    written = writer.write_silence(1000)
    writer.close()

    assert written == 1000
    assert writer.frames == 1003

    raw_bytes = raw_path.read_bytes()
    assert len(raw_bytes) == 1003 * SAMPLE_WIDTH
    tail = np.frombuffer(raw_bytes, dtype="<i2")[3:]
    assert np.all(tail == 0)


def test_write_silence_zero_or_negative_is_noop(tmp_path):
    writer = RawTrackWriter(tmp_path / "mic.raw")
    assert writer.write_silence(0) == 0
    assert writer.write_silence(-5) == 0
    assert writer.frames == 0
    writer.close()


# --------------------------------------------------------------------------
# Crash recovery
# --------------------------------------------------------------------------


def test_finalize_session_recovers_unclean_raw(tmp_path):
    # Simulate a process that was killed mid-meeting: a .raw file exists
    # (written directly, bypassing RawTrackWriter/close) with no matching
    # .wav and no clean shutdown ever happened.
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    samplerate = 48000
    numframes = 48000 * 3  # 3 seconds
    samples = np.zeros(numframes, dtype=np.int16)
    samples[::2] = 1000  # a bit of signal so it's not pure silence
    (session_dir / "mic.raw").write_bytes(samples.tobytes())

    import json

    (session_dir / "session.json").write_text(
        json.dumps({"tracks": {"mic": {"samplerate": samplerate}}})
    )

    results = finalize_session(session_dir)
    assert "mic" in results
    wav_path = session_dir / "mic.wav"
    assert wav_path.exists()

    with wave.open(str(wav_path), "rb") as fh:
        assert fh.getnframes() == numframes
        assert fh.getframerate() == samplerate
        duration = fh.getnframes() / fh.getframerate()
    assert duration == pytest.approx(3.0)
    assert results["mic"]["rate_guessed"] is False


def test_finalize_session_guesses_rate_when_missing_from_metadata(tmp_path):
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    (session_dir / "system.raw").write_bytes(np.zeros(100, dtype=np.int16).tobytes())
    # No session.json at all -> falls back to 48000 and records the guess.
    results = finalize_session(session_dir)
    assert results["system"]["samplerate"] == 48000
    assert results["system"]["rate_guessed"] is True


def test_wrap_raw_as_wav_drops_torn_trailing_sample(tmp_path):
    # A kill mid-write can leave a final, incomplete 2-byte sample: just one
    # byte written. That trailing byte must be dropped entirely, not treated
    # as the low byte of a bogus extra sample that shifts everything after it
    # (there is nothing after it here, but the dropped byte must not resurface
    # as a corrupt sample either).
    raw_path = tmp_path / "mic.raw"
    wav_path = tmp_path / "mic.wav"

    complete = np.array([10, 20, 30, 40], dtype=np.int16)
    torn_byte = bytes([0xAB])  # half of a would-be 5th sample
    raw_path.write_bytes(complete.tobytes() + torn_byte)

    frames = wrap_raw_as_wav(raw_path, wav_path, 8000)
    assert frames == len(complete)

    data, rate = read_wav_mono(wav_path)
    assert len(data) == len(complete)
    assert np.allclose(data, complete.astype(np.float32) / 32767.0, atol=1e-4)
