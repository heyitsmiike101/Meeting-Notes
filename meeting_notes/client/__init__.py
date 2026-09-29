"""The laptop-side half of the recorder/server protocol in ``meeting_notes.wire``.

Everything under this package is a *client* of the LAN transcription server:
it streams a disposable live preview while recording continues locally, and
separately uploads the complete recording afterwards for the authoritative
transcript. See ``meeting_notes.wire`` for why that split exists -- the short
version is that the local recording is always the source of truth, and
nothing in this package may be able to put that at risk.
"""

from __future__ import annotations
