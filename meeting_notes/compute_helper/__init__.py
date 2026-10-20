"""The GPU helper runtime.

Runs inside the helper pack's own Python (see docs/gpu-helper-design.md). It polls the Meeting Notes server for
transcription work, runs faster-whisper and pyannote on the local NVIDIA GPU, and posts the result back. It must
never import PySide6 or anything from ``meeting_notes.client`` / ``meeting_notes.server``, and the heavy
libraries (torch, faster-whisper, pyannote) are imported lazily so the module imports without them.
"""

RUNTIME_VERSION = "1"
