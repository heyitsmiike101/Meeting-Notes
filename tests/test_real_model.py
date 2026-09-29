"""Opt-in test that runs a REAL Whisper model against real speech.

Excluded from the default suite because it downloads a model (~75MB for
tiny.en) on first run. Enable with:

    MEETING_NOTES_SLOW_TESTS=1 pytest tests/test_real_model.py -v

This cannot run in the development container used to build this project --
huggingface.co is blocked there by egress policy -- so it exists to be run on a
real machine, and is the check that actually confirms audio becomes text.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        os.environ.get("MEETING_NOTES_SLOW_TESTS") != "1",
        reason="set MEETING_NOTES_SLOW_TESTS=1 to run (downloads a Whisper model)",
    ),
]

FIXTURE = Path(__file__).parent / "fixtures" / "jfk.wav"


@pytest.mark.skipif(not FIXTURE.exists(), reason="speech fixture missing")
def test_tiny_model_transcribes_known_speech():
    pytest.importorskip("faster_whisper")
    from meeting_notes.transcribe.faster_whisper_backend import FasterWhisperTranscriber

    progress = []
    transcriber = FasterWhisperTranscriber(
        model_size="tiny.en",
        local_files_only=False,  # allow the one-time download
        on_progress=lambda track, frac, speech: progress.append(frac),
    )
    segments = transcriber.transcribe(FIXTURE, "mic")

    assert segments, "a real model should produce segments from real speech"
    text = " ".join(s.text for s in segments).lower()
    # The clip is JFK's "ask not what your country can do for you".
    assert "ask not" in text, f"unexpected transcript: {text!r}"
    assert all(s.track == "mic" for s in segments)
    assert segments[0].start >= 0.0 and segments[-1].end <= 12.0
    assert progress and progress[-1] == 1.0


@pytest.mark.skipif(not FIXTURE.exists(), reason="speech fixture missing")
def test_timestamps_stay_in_original_time_with_leading_silence(tmp_path):
    """The two-track merge's core assumption, against a real model this time."""
    pytest.importorskip("faster_whisper")
    import numpy as np
    from faster_whisper.audio import decode_audio

    from meeting_notes.transcribe.faster_whisper_backend import FasterWhisperTranscriber
    from meeting_notes.wav_io import RawTrackWriter, wrap_raw_as_wav

    lead = 15.0
    speech = np.repeat(decode_audio(str(FIXTURE), sampling_rate=16000), 3)
    track = np.concatenate([np.zeros(int(lead * 48000), dtype=np.float32), speech])
    raw = tmp_path / "mic.raw"
    writer = RawTrackWriter(raw)
    writer.write_float(track.reshape(-1, 1))
    writer.close()
    wav = tmp_path / "mic.wav"
    wrap_raw_as_wav(raw, wav, 48000)

    transcriber = FasterWhisperTranscriber(model_size="tiny.en", local_files_only=False)
    segments = transcriber.transcribe(wav, "mic")

    assert segments
    # Speech starts 15s in. If VAD returned compressed time this would be ~0.
    assert segments[0].start > lead - 2.0, (
        f"first segment at {segments[0].start:.1f}s but speech starts at {lead}s -- "
        "VAD timestamps were not restored to original time"
    )
