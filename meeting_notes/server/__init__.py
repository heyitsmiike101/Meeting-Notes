"""The transcription server: a disposable live preview plus an authoritative
final pass, for a recorder client that streams audio over the LAN.

See ``meeting_notes.wire`` for the protocol both sides speak, and
``meeting_notes.server.app`` for the FastAPI application.
"""

from __future__ import annotations
