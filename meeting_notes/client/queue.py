"""Uploading completed recordings when the server was (or becomes) reachable.

This is the other half of "the local recording is always the source of
truth" (see ``meeting_notes.wire``): the live preview in ``streamer.py`` may
lose audio without consequence, but the authoritative transcript comes from
uploading the *complete* local recording, and that upload must eventually
happen even if the server was down, unreachable, or the laptop went to sleep
mid-upload. So nothing here is allowed to assume it runs to completion in one
try. State lives entirely on disk as one small JSON file per pending session
-- never in memory only -- specifically so a crash or restart mid-upload
loses nothing but time: the next ``UploadWorker`` just picks the entry back
up from whatever attempt count and error it left behind.

Deliberately NOT copied here: the audio itself. ``enqueue`` only records
*where* the session lives; the session directory the recorder already wrote
stays the one and only copy until it has been durably uploaded.
"""

from __future__ import annotations

import json
import re
import threading
import time
import wave
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import numpy as np

from meeting_notes import wire
from meeting_notes.client.api import ServerClient
from meeting_notes.client.resample import Downsampler
from meeting_notes.transcribe import merge as merge_mod

# Read the source WAV this many frames at a time, so converting a multi-hour
# recording to 16 kHz PCM never has to hold more than one chunk of it (at
# either sample rate) in memory -- the same reasoning as the chunked upload
# in api.py.
_CONVERT_CHUNK_FRAMES = 1 << 16


def default_queue_dir(save_dir: Path) -> Path:
    """Where the queue lives by default: a hidden sibling of the recordings folder."""
    return Path(save_dir) / ".upload-queue"


class SessionQueue:
    """A directory of small JSON state files, one per session waiting to upload.

    Every method here reads and writes straight through to disk -- there is
    no cached index -- so two ``SessionQueue`` instances over the same
    directory (e.g. one created before a restart, one after) always agree,
    and nothing is lost if the process dies between calls.
    """

    def __init__(self, queue_dir: Path):
        self.queue_dir = Path(queue_dir)
        self.queue_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def for_save_dir(cls, save_dir: Path) -> "SessionQueue":
        return cls(default_queue_dir(Path(save_dir)))

    # -- naming -----------------------------------------------------------

    def entry_id(self, session_dir: Path) -> str:
        # The session directory's own name is already a unique, filesystem-
        # safe identifier (see create_session_dir in audio/session.py), so
        # reuse it rather than inventing a second id -- that also makes
        # enqueue() naturally idempotent for the same session.
        name = Path(session_dir).name
        return re.sub(r"[^A-Za-z0-9._-]+", "_", name) or "session"

    def _state_path(self, entry_id: str) -> Path:
        return self.queue_dir / f"{entry_id}.json"

    # -- reading / writing state -------------------------------------------

    def read_state(self, entry_id: str) -> Optional[Dict[str, Any]]:
        path = self._state_path(entry_id)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError):
            # A state file torn by a mid-write crash should be skipped, not
            # mistaken for "nothing pending" or crash the caller.
            return None

    def write_state(self, entry_id: str, state: Dict[str, Any]) -> None:
        path = self._state_path(entry_id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(path)  # atomic on every platform we run on

    # -- public API ---------------------------------------------------------

    def enqueue(self, session_dir: Path) -> str:
        """Record ``session_dir`` as needing upload. Never copies audio."""
        session_dir = Path(session_dir).resolve()
        entry_id = self.entry_id(session_dir)
        if self.read_state(entry_id) is not None:
            return entry_id  # already queued -- keep its existing attempt history
        self.write_state(
            entry_id,
            {
                "session_dir": str(session_dir),
                "attempts": 0,
                "last_error": None,
                "status": "pending",
                "enqueued_at": time.time(),
                "next_attempt_at": None,
            },
        )
        return entry_id

    def pending(self) -> List[Dict[str, Any]]:
        """Every entry currently on disk, in enqueue order, done or not."""
        entries = []
        for path in sorted(self.queue_dir.glob("*.json")):
            if path.suffix == ".tmp":
                continue
            state = self.read_state(path.stem)
            if state is None:
                continue
            state = dict(state)
            state["id"] = path.stem
            entries.append(state)
        entries.sort(key=lambda e: e.get("enqueued_at") or 0)
        return entries

    def mark_done(self, entry_id: str) -> None:
        self._state_path(entry_id).unlink(missing_ok=True)

    def mark_attempt_failed(
        self,
        entry_id: str,
        error: str,
        *,
        next_attempt_at: Optional[float] = None,
        terminal: bool = False,
    ) -> None:
        """Record one failed attempt without dropping the entry.

        ``terminal`` marks it as no longer worth auto-retrying (attempts
        exhausted); the entry still stays on disk with its reason rather than
        vanishing, since a person may want to know a session never made it up.
        """
        state = self.read_state(entry_id) or {"session_dir": "", "enqueued_at": time.time()}
        state["attempts"] = int(state.get("attempts", 0)) + 1
        state["last_error"] = error
        state["last_attempt_at"] = time.time()
        state["next_attempt_at"] = next_attempt_at
        state["status"] = "failed" if terminal else "pending"
        self.write_state(entry_id, state)


def _wav_to_pcm16(wav_path: Path, out_path: Path) -> int:
    """Convert a local (typically 48 kHz) mono WAV to 16 kHz mono int16 PCM.

    Streams through the source in chunks via ``Downsampler`` rather than
    loading the whole recording into memory -- the same "never hold a
    multi-hour file whole" rule the upload itself follows.
    """
    with wave.open(str(wav_path), "rb") as src:
        rate = src.getframerate()
        channels = src.getnchannels()
        if channels != 1:
            raise ValueError(f"{wav_path}: expected a mono track WAV, got {channels} channels")
        downsampler = Downsampler(rate, wire.STREAM_SAMPLE_RATE)
        total_frames = 0
        with open(out_path, "wb") as dst:
            while True:
                raw = src.readframes(_CONVERT_CHUNK_FRAMES)
                if not raw:
                    break
                block = np.frombuffer(raw, dtype="<i2")
                out = downsampler.process(block)
                data = np.ascontiguousarray(out, dtype="<i2").tobytes()
                dst.write(data)
                total_frames += len(data) // wire.BYTES_PER_FRAME
    return total_frames


def _collect_timing(session_dir: Path) -> Dict[str, list]:
    """Parse every ``*.timing.jsonl`` sidecar into {track: [entry, ...]}."""
    timing: Dict[str, list] = {}
    for path in sorted(session_dir.glob("*.timing.jsonl")):
        track = path.name[: -len(".timing.jsonl")]
        entries = []
        # errors="replace"/skip-on-parse-error for the same reason
        # meeting_notes.timing.load_timing_log does: a killed process can
        # leave one torn final line, and everything before it is still good.
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        timing[track] = entries
    return timing


def _normalize_segments(raw_segments: Optional[list], labels: Dict[str, str]) -> List[dict]:
    """Coerce whatever the server's transcript JSON contains into the shape
    ``meeting_notes.transcribe.merge``'s renderers expect, defensively -- the
    server is a separate component and a missing/odd field here must not
    crash the upload worker."""
    out = []
    for seg in raw_segments or []:
        track = seg.get("track", "")
        out.append(
            {
                "start": float(seg.get("start", 0.0) or 0.0),
                "end": float(seg.get("end", 0.0) or 0.0),
                "track": track,
                "label": seg.get("label") or labels.get(track, track),
                "text": seg.get("text", ""),
                "in_gap": bool(seg.get("in_gap", False)),
            }
        )
    out.sort(key=lambda d: (d["start"], d["track"]))
    return out


def _write_transcript(session_dir: Path, transcript: dict, meta: dict) -> None:
    """Render the server's transcript response into transcript.md/.json,
    in the same format ``meeting-notes transcribe`` produces locally so a
    session looks the same whether it was transcribed on-device or via the
    server's final pass.
    """
    labels = {
        track: info.get("label", track) for track, info in (meta.get("tracks") or {}).items()
    }
    segments = _normalize_segments(transcript.get("segments"), labels)
    session_meta = transcript.get("session") or meta
    (session_dir / "transcript.md").write_text(
        merge_mod.render_markdown(segments, session_meta), encoding="utf-8"
    )
    (session_dir / "transcript.json").write_text(
        merge_mod.render_json(segments, session_meta), encoding="utf-8"
    )


class UploadWorker:
    """Background thread that drains a ``SessionQueue`` against the server.

    Every entry is independent: one session stuck retrying (server down,
    disk gone, whatever) never blocks any other entry, and a bug processing
    one is caught and turned into a recorded failure rather than taking the
    whole thread down -- there is no meeting-recording work left for this
    thread to protect once capture has finished, but the *next* session's
    upload still needs it alive.
    """

    def __init__(
        self,
        queue: SessionQueue,
        base_url: str,
        token: Optional[str] = None,
        settings: Optional[Dict[str, Any]] = None,
        *,
        poll_interval: float = 2.0,
        initial_backoff: float = 5.0,
        max_backoff: float = 300.0,
        max_attempts: int = 8,
        client_factory: Optional[Callable[[], ServerClient]] = None,
    ):
        self.queue = queue
        self.base_url = base_url
        self.token = token
        self.settings = settings or {}
        self.poll_interval = poll_interval
        self.initial_backoff = initial_backoff
        self.max_backoff = max_backoff
        self.max_attempts = max_attempts
        self._client_factory = client_factory or (lambda: ServerClient(base_url, token))

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="upload-worker", daemon=True)
        self._thread.start()

    def stop(self, join_timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=join_timeout)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self.run_once()
            if self._stop_event.wait(self.poll_interval):
                return

    # -- one pass over the queue ---------------------------------------------

    def run_once(self) -> None:
        """Attempt every entry that is due right now. Safe with zero entries."""
        for entry in self.queue.pending():
            if self._stop_event.is_set():
                return
            self._process_entry(entry)

    def _process_entry(self, entry: Dict[str, Any]) -> None:
        entry_id = entry["id"]
        if entry.get("status") == "failed":
            return  # exhausted its retries; needs a person, not another attempt
        next_at = entry.get("next_attempt_at")
        if next_at and time.time() < next_at:
            return  # still inside this entry's own backoff window

        try:
            session_dir = Path(entry["session_dir"])
            self._upload_session(session_dir)
        except Exception as exc:  # noqa: BLE001 - one bad entry must not sink the worker
            attempts = int(entry.get("attempts", 0)) + 1
            backoff = min(self.initial_backoff * (2 ** (attempts - 1)), self.max_backoff)
            self.queue.mark_attempt_failed(
                entry_id,
                f"{type(exc).__name__}: {exc}",
                next_attempt_at=time.time() + backoff,
                terminal=attempts >= self.max_attempts,
            )
            return
        self.queue.mark_done(entry_id)

    # -- the actual upload ----------------------------------------------------

    def _upload_session(self, session_dir: Path) -> None:
        if not session_dir.exists():
            raise FileNotFoundError(f"session directory is gone: {session_dir}")
        meta_path = session_dir / "session.json"
        if not meta_path.exists():
            raise FileNotFoundError(f"session.json missing in {session_dir}")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        session_id = session_dir.name

        client = self._client_factory()
        try:
            for track, info in sorted((meta.get("tracks") or {}).items()):
                wav_name = info.get("wav")
                if not wav_name:
                    continue
                wav_path = session_dir / wav_name
                if not wav_path.exists():
                    continue
                pcm_path = wav_path.with_suffix(".pcm16")
                frames = _wav_to_pcm16(wav_path, pcm_path)
                client.upload_track(session_id, track, pcm_path, frames)
                # Only cleaned up once the upload actually succeeded -- a
                # failed attempt leaves it behind so the retry doesn't have
                # to redo a conversion whose result was never sent anywhere.
                pcm_path.unlink(missing_ok=True)

            timing = _collect_timing(session_dir)
            job_id = client.finalize(session_id, meta, timing, self.settings)
            transcript = self._poll_job(client, job_id)
            _write_transcript(session_dir, transcript, meta)
        finally:
            client.close()

    def _poll_job(self, client: ServerClient, job_id: str) -> Dict[str, Any]:
        while True:
            info = client.job(job_id)
            state = info.get("state")
            if state == wire.JobState.DONE:
                return client.transcript(job_id)
            if state == wire.JobState.ERROR:
                raise RuntimeError(info.get("detail") or f"job {job_id} failed")
            if self._stop_event.is_set():
                raise RuntimeError("upload worker stopping while a job was still running")
            time.sleep(self.poll_interval)
