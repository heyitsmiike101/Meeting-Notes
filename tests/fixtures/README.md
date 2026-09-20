`jfk.wav` is ~11s of John F. Kennedy's 1961 inaugural address: a work of the US
federal government and therefore public domain. It is the same clip whisper.cpp
ships as its standard sample.

It exists so `test_vad_alignment.py` can run real voice activity detection
against real speech. That test needs no Whisper model and no network -- Silero
VAD ships inside faster-whisper as a 1.2MB bundled ONNX file.
