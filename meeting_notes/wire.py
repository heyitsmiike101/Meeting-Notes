"""The contract between the recorder client and the transcription server.

Both sides import this module, so the protocol is defined exactly once and its
framing is unit-testable without a network.

Design notes worth knowing before changing anything here:

* **Everything on the wire is 16 kHz mono int16.** That is Whisper's native
  input, so sending it costs nothing in transcription quality, needs no audio
  codec on the client, and is only 32 KB/s per track -- trivial on a LAN. The
  full-quality 48 kHz recording never leaves the machine that made it; it stays
  local as the archive copy.
* **The local recording is always the source of truth.** Streaming exists to
  give a live preview while the meeting runs, not to be the record. A dropped
  connection can therefore never lose audio: the client keeps recording, and the
  complete file is uploaded afterwards for the authoritative pass.
* **Two passes, deliberately.** A CPU-only server cannot run a large model
  against two live streams and keep up with realtime, so the live pass uses a
  fast model and is explicitly disposable; the final pass runs on the complete
  upload and can use whatever model you like.
"""

from __future__ import annotations

import struct
from dataclasses import asdict, dataclass, field
from typing import List, Optional

PROTOCOL_VERSION = 1

STREAM_SAMPLE_RATE = 16000
STREAM_DTYPE = "<i2"  # little-endian int16
BYTES_PER_FRAME = 2
TRACKS = ("mic", "system")

# Binary audio frame header: track index, then the absolute frame offset this
# payload starts at. The offset is what makes the stream idempotent -- after a
# reconnect the client resends from wherever the server last acknowledged, and
# the server can place any payload without relying on arrival order.
_FRAME_HEADER = struct.Struct("<BQ")
FRAME_HEADER_SIZE = _FRAME_HEADER.size


class ProtocolError(ValueError):
    """Raised when a peer sends something that does not fit the contract."""


def track_index(track: str) -> int:
    try:
        return TRACKS.index(track)
    except ValueError as exc:
        raise ProtocolError(f"unknown track {track!r}; expected one of {TRACKS}") from exc


def encode_audio_frame(track: str, frame_offset: int, pcm: bytes) -> bytes:
    """Pack one chunk of int16 PCM for a websocket binary frame."""
    if frame_offset < 0:
        raise ProtocolError("frame_offset must not be negative")
    if len(pcm) % BYTES_PER_FRAME:
        raise ProtocolError("pcm payload must contain whole int16 samples")
    return _FRAME_HEADER.pack(track_index(track), frame_offset) + pcm


def decode_audio_frame(payload: bytes) -> tuple:
    """Unpack a binary frame into (track, frame_offset, pcm_bytes)."""
    if len(payload) < FRAME_HEADER_SIZE:
        raise ProtocolError("binary frame is shorter than its header")
    index, offset = _FRAME_HEADER.unpack_from(payload, 0)
    if index >= len(TRACKS):
        raise ProtocolError(f"track index {index} out of range")
    pcm = payload[FRAME_HEADER_SIZE:]
    if len(pcm) % BYTES_PER_FRAME:
        raise ProtocolError("pcm payload must contain whole int16 samples")
    return TRACKS[index], offset, pcm


# -- JSON control messages ---------------------------------------------------


@dataclass
class Hello:
    """First frame on a stream: identifies the session being recorded."""

    session_id: str
    name: str = ""
    tracks: List[str] = field(default_factory=lambda: list(TRACKS))
    sample_rate: int = STREAM_SAMPLE_RATE
    protocol: int = PROTOCOL_VERSION
    started_wall: float = 0.0
    type: str = "hello"


@dataclass
class Ack:
    """Server confirms the highest contiguous frame offset it has stored."""

    track: str
    frames: int
    type: str = "ack"


@dataclass
class Partial:
    """A live preview segment. Approximate by design; superseded by the final pass."""

    track: str
    start: float
    end: float
    text: str
    type: str = "partial"


@dataclass
class ServerError:
    detail: str
    type: str = "error"


def to_json(message) -> dict:
    return asdict(message)


# -- HTTP surface ------------------------------------------------------------

HEALTH = "/health"
STREAM = "/v1/stream"


def track_upload_path(session_id: str, track: str) -> str:
    return f"/v1/sessions/{session_id}/tracks/{track}"


def finalize_path(session_id: str) -> str:
    return f"/v1/sessions/{session_id}/finalize"


def job_path(job_id: str) -> str:
    return f"/v1/jobs/{job_id}"


def job_transcript_path(job_id: str) -> str:
    return f"/v1/jobs/{job_id}/transcript"


@dataclass
class JobState:
    """Lifecycle of a final-pass transcription job."""

    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"


def auth_headers(token: Optional[str]) -> dict:
    return {"Authorization": f"Bearer {token}"} if token else {}
