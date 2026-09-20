"""Proves VAD does not corrupt the timestamps the two-track merge depends on.

This is the load-bearing assumption of the whole transcription design. Voice
activity detection concatenates only the speech parts of a track and decodes
that compressed audio -- each of our tracks is mostly silence, so this is a
large speed win and the main reason Whisper never sees dead air to hallucinate
over. But if the resulting timestamps were in COMPRESSED time, every segment
after the first silence would be placed too early on the shared timeline and
the [You]/[Them] interleaving would be quietly, unfixably wrong.

faster-whisper restores them via SpeechTimestampsMap, which adds back the
silence elided before each chunk. These tests verify that empirically rather
than trusting the docstring, and they need no Whisper model and no network:
Silero VAD ships inside faster-whisper as a bundled ONNX file.
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

faster_whisper = pytest.importorskip("faster_whisper", reason="faster-whisper not installed")

from faster_whisper.audio import decode_audio  # noqa: E402
from faster_whisper.transcribe import SpeechTimestampsMap  # noqa: E402
from faster_whisper.vad import VadOptions, collect_chunks, get_speech_timestamps  # noqa: E402

from meeting_notes.wav_io import RawTrackWriter, wrap_raw_as_wav  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "jfk.wav"
SR = 16000


def test_restoration_adds_back_elided_silence():
    """The core arithmetic, independent of any audio.

    Two speech chunks with 10s of silence before the first and 5s between them.
    A segment decoded at compressed t=0 really happened at t=10.
    """
    chunks = [
        {"start": 10 * SR, "end": 20 * SR},  # 10s of speech, after 10s of silence
        {"start": 25 * SR, "end": 30 * SR},  # 5s of speech, after 5s more silence
    ]
    mapping = SpeechTimestampsMap(chunks, SR)

    assert mapping.get_original_time(0.0) == pytest.approx(10.0, abs=0.01)
    assert mapping.get_original_time(5.0) == pytest.approx(15.0, abs=0.01)
    # Compressed t=10 is the start of chunk two: 10s of speech consumed, so the
    # original time is 25s, not 20s -- the 5s gap has to reappear here.
    assert mapping.get_original_time(11.0) == pytest.approx(26.0, abs=0.01)


def _build_track(lead_silence: float, tail_silence: float, tmp_path: Path) -> tuple:
    """A 48kHz track shaped like a real one: silence, speech, silence."""
    speech16 = decode_audio(str(FIXTURE), sampling_rate=SR)
    speech48 = np.repeat(speech16, 3)  # crude 16k->48k; adequate as a test signal
    track = np.concatenate(
        [
            np.zeros(int(lead_silence * 48000), dtype=np.float32),
            speech48,
            np.zeros(int(tail_silence * 48000), dtype=np.float32),
        ]
    )
    # Write it through OUR writer, so this also proves the format we produce is
    # what faster-whisper can consume.
    raw = tmp_path / "mic.raw"
    writer = RawTrackWriter(raw)
    writer.write_float(track.reshape(-1, 1))
    writer.close()
    wav = tmp_path / "mic.wav"
    wrap_raw_as_wav(raw, wav, 48000)
    return wav, lead_silence + len(speech48) / 48000


@pytest.mark.skipif(not FIXTURE.exists(), reason="speech fixture missing")
def test_our_48khz_wav_decodes_and_resamples(tmp_path):
    """Our recorder writes 48kHz; Whisper wants 16kHz. faster-whisper resamples
    internally via PyAV, so no pre-conversion step is needed anywhere."""
    wav, _ = _build_track(2.0, 2.0, tmp_path)
    with wave.open(str(wav)) as fh:
        assert fh.getframerate() == 48000 and fh.getnchannels() == 1
        original_seconds = fh.getnframes() / 48000

    audio = decode_audio(str(wav), sampling_rate=SR)
    assert audio.dtype == np.float32
    assert len(audio) / SR == pytest.approx(original_seconds, abs=0.05)


@pytest.mark.skipif(not FIXTURE.exists(), reason="speech fixture missing")
def test_real_vad_restores_original_timestamps(tmp_path):
    """End to end with real speech and real VAD: a segment decoded at
    compressed t=0 must map back to where the speech actually is."""
    lead = 10.0
    wav, speech_ends_at = _build_track(lead, 10.0, tmp_path)
    audio = decode_audio(str(wav), sampling_rate=SR)

    chunks = get_speech_timestamps(audio, VadOptions())
    assert chunks, "VAD should find the planted speech"

    # VAD located the speech where we actually put it.
    assert chunks[0]["start"] / SR == pytest.approx(lead, abs=0.5)
    assert chunks[-1]["end"] / SR == pytest.approx(speech_ends_at, abs=0.5)

    compressed = np.concatenate(collect_chunks(audio, chunks)[0])
    # The whole point: far less audio actually reaches the model.
    assert len(compressed) < len(audio) * 0.6

    mapping = SpeechTimestampsMap(chunks, SR)
    # Without restoration this segment would be reported at 0.0s and land ten
    # seconds early on the shared timeline.
    assert mapping.get_original_time(0.0) == pytest.approx(lead, abs=0.5)
    assert mapping.get_original_time(0.0) > 1.0
