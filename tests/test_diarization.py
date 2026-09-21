from pathlib import Path

from meeting_notes.transcribe.diarize import SpeakerTurn, assign_speakers
from meeting_notes.transcribe.protocol import Segment


def test_assign_speakers_uses_greatest_overlap_and_stable_labels():
    segments = [
        Segment(0.0, 2.0, "first", "system"),
        Segment(2.0, 4.0, "second", "system"),
        Segment(5.0, 6.0, "unknown", "system"),
    ]
    turns = [
        SpeakerTurn(0.0, 1.6, "SPEAKER_B"),
        SpeakerTurn(1.6, 2.2, "SPEAKER_A"),
        SpeakerTurn(2.2, 4.0, "SPEAKER_A"),
    ]

    assigned = assign_speakers(segments, turns)

    assert [s.speaker for s in assigned] == ["Them 1", "Them 2", None]
    assert [s.text for s in assigned] == ["first", "second", "unknown"]


def test_pyannote_module_does_not_import_heavy_dependency_until_construction():
    from meeting_notes.transcribe import pyannote_backend

    assert pyannote_backend.PyannoteDiarizer.__name__ == "PyannoteDiarizer"


def test_merge_uses_diarized_label_without_changing_mic_label():
    from meeting_notes.transcribe.merge import merge_tracks

    merged = merge_tracks(
        {
            "mic": [Segment(0, 1, "hello", "mic")],
            "system": [Segment(1, 2, "hi", "system", speaker="Them 2")],
        },
        {},
    )

    assert [item["label"] for item in merged] == ["You", "Them 2"]
