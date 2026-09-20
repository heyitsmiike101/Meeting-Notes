"""Whole-pipeline test: record two tracks, then transcribe and merge them.

This is the test that would catch the pieces drifting apart -- each module has
its own unit tests, but only this one proves that a session written by the
recorder can actually be read back, aligned and rendered by the transcriber.
No audio hardware and no real speech model are involved.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from meeting_notes import cli
from meeting_notes.audio.session import RecordingSession
from meeting_notes.transcribe.protocol import Segment, register
from tests.fakes import RELEASE, FakeSource

RATE = 1000


@pytest.fixture(autouse=True)
def release_stalled_threads():
    RELEASE.clear()
    yield
    RELEASE.set()
    time.sleep(0.05)


class ScriptedTranscriber:
    """Returns canned segments per track, so merge/render logic is what's tested."""

    def __init__(self, script=None, **kwargs):
        self.script = script or {
            "mic": [Segment(0.05, 0.15, "Morning, can everyone hear me?", "mic")],
            "system": [
                Segment(0.20, 0.30, "Loud and clear.", "system"),
                Segment(0.35, 0.45, "Let's start with the roadmap.", "system"),
            ],
        }

    def transcribe(self, wav_path: Path, track: str):
        return self.script.get(track, [])


@pytest.fixture
def recorded_session(tmp_path):
    session = RecordingSession(
        session_dir=tmp_path,
        sources={
            "mic": FakeSource(name="Fake Mic", samplerate=RATE),
            "system": FakeSource(name="Fake Loopback", samplerate=RATE),
        },
        block_seconds=0.05,
        progress_interval=0.01,
    )
    session.start()
    time.sleep(0.6)
    session.request_stop()
    session.finalize()
    return tmp_path


def test_record_then_transcribe_produces_labeled_transcript(recorded_session, capsys):
    register("scripted", ScriptedTranscriber)
    rc = cli.main(["transcribe", str(recorded_session), "--backend", "scripted"])
    assert rc == 0

    markdown = (recorded_session / "transcript.md").read_text()
    payload = json.loads((recorded_session / "transcript.json").read_text())

    # Both speakers must be attributed, which is the entire point of recording
    # two tracks instead of one mixed stream.
    assert "You" in markdown
    assert "Them" in markdown
    assert "can everyone hear me" in markdown
    assert "roadmap" in markdown

    segments = payload["segments"]
    assert [s["label"] for s in segments] == ["You", "Them", "Them"]
    # Chronological order on the shared timeline, not per-track order.
    assert segments == sorted(segments, key=lambda s: s["start"])


def test_transcribe_rejects_a_directory_with_no_session(tmp_path, capsys):
    assert cli.main(["transcribe", str(tmp_path), "--backend", "null"]) == 2
    assert "No session.json" in capsys.readouterr().err


def test_repair_rebuilds_wavs_from_raw_after_a_kill(tmp_path):
    """Simulates the process being killed mid-meeting: .raw exists, no .wav."""
    from meeting_notes.wav_io import RawTrackWriter

    meta = {"tracks": {"mic": {"samplerate": RATE, "label": "You"}}}
    (tmp_path / "session.json").write_text(json.dumps(meta))
    writer = RawTrackWriter(tmp_path / "mic.raw")
    writer.write_silence(RATE * 3)
    writer.close()

    assert cli.main(["repair", str(tmp_path)]) == 0
    import wave

    with wave.open(str(tmp_path / "mic.wav")) as fh:
        assert fh.getnframes() == RATE * 3
        assert fh.getframerate() == RATE


def test_devices_command_runs_without_audio_backend(capsys):
    """soundcard is absent here; the command must still report, not explode."""
    assert cli.main(["devices"]) == 0
    out = capsys.readouterr().out
    assert "Microphones" in out and "System audio sources" in out
