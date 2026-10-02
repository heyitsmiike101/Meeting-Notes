# COMPAT FIXTURE - do not edit. Verbatim copy of meeting_notes/client/queue.py from the 0.7.10 client
# (release/0.7.10), with only the meeting_notes.* imports rewritten to be package-relative.
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
import inspect
import logging
import os
import re
import threading
import time
import wave
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import httpx
import numpy as np

from . import wire
from . import version_gate
from .api import ServerClient, ServerUnavailable, UPLOAD_TIMEOUT
from .resample import Downsampler

# Read the source WAV this many frames at a time, so converting a multi-hour
# recording to 16 kHz PCM never has to hold more than one chunk of it (at
# either sample rate) in memory -- the same reasoning as the chunked upload
# in api.py.
_CONVERT_CHUNK_FRAMES = 1 << 16

# ServerClient's plain-float default (10s for everything) is sized for a
# quick health probe, not for streaming a multi-hundred-MB upload body --
# a slow-but-alive LAN link could easily take longer than 10s to write one
# chunk, which would abort a perfectly good upload. Give connect/pool their
# own short budget (a dead server should still be noticed quickly) and give
# read/write the room a large body actually needs.
# Kept as a compatibility alias for callers/tests that imported the old
# private name; the timeout now also serves imported-recording uploads.
_UPLOAD_TIMEOUT = UPLOAD_TIMEOUT


log = logging.getLogger("meeting_notes.client.queue")


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

    # How long write_state keeps retrying the atomic rename when Windows
    # refuses it (see below) before falling back to an in-place rewrite.
    REPLACE_RETRY_SECONDS = 3.0

    def write_state(self, entry_id: str, state: Dict[str, Any]) -> None:
        path = self._state_path(entry_id)
        tmp = path.with_suffix(".json.tmp")
        text = json.dumps(state, indent=2)
        tmp.write_text(text, encoding="utf-8")
        # On Windows the rename fails with "Access is denied" whenever another
        # handle has the destination open at that instant: the UI reading
        # progress, an indexer, or endpoint security scanning the file. Seen
        # on a real machine, where it failed every upload and then killed the
        # upload worker. Retry briefly, then fall back to rewriting in place
        # (readers already tolerate a torn file by skipping it for a tick).
        deadline = time.monotonic() + self.REPLACE_RETRY_SECONDS
        delay = 0.02
        while True:
            try:
                tmp.replace(path)
                return
            except PermissionError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(delay)
                delay = min(delay * 2, 0.25)
        try:
            path.write_text(text, encoding="utf-8")
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

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
                # Tracks the server has already acknowledged on some earlier
                # attempt -- a retry skips these instead of re-sending a
                # multi-hundred-MB track the server already has.
                "uploaded_tracks": [],
                # Lifecycle fields are intentionally durable.  They let the
                # desktop UI show "pending", byte upload progress and server
                # transcription progress after a restart, rather than only a
                # count of opaque queue entries.
                "upload_state": "pending",
                "upload_percent": 0.0,
                "upload_bytes": 0,
                "upload_total": 0,
                "transcription_state": "pending",
                "transcription_percent": 0.0,
                "job_id": None,
                # True once POST /finalize succeeded (job_id is then durable):
                # the upload is over and only the transcript is awaited.
                "finalized": False,
            },
        )
        return entry_id

    def requeue(self, session_dir: Path) -> str:
        """Put a saved recording back on the queue so every track is sent again.

        Unlike ``enqueue`` (which keeps an existing entry's history so a retry
        skips tracks the server already has), this is for a server that has
        lost the meeting: the recorded ``uploaded_tracks`` acks are stale, so
        they are cleared and the entry is made due immediately. Returns the
        entry id. There is only ever one state file per folder, so asking twice
        (or for a folder that is already queued) never duplicates it. An entry
        an uploader is working on right now is left untouched.
        """
        session_dir = Path(session_dir).resolve()
        entry_id = self.entry_id(session_dir)
        if self.read_state(entry_id) is None:
            self.enqueue(session_dir)
            return entry_id
        if self._claim_path(entry_id).exists():
            log.info("requeue: %s is being uploaded right now; left as is", entry_id)
            return entry_id
        state = self.read_state(entry_id) or {}
        state.update(
            session_dir=str(session_dir),
            attempts=0,
            last_error=None,
            status="pending",
            next_attempt_at=None,
            uploaded_tracks=[],
            upload_state="pending",
            upload_percent=0.0,
            upload_bytes=0,
            upload_total=0,
            transcription_state="pending",
            transcription_percent=0.0,
            job_id=None,
            finalized=False,
        )
        self.write_state(entry_id, state)
        return entry_id

    def is_queued(self, session_dir: Path) -> bool:
        return self.read_state(self.entry_id(Path(session_dir))) is not None

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
        self.release(entry_id)

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

    # A claim is an O_EXCL-created ``<id>.claim`` file beside the entry. Two
    # processes can legitimately drain the same queue at once -- the app's
    # background uploader and `meeting-notes upload` in a terminal -- and
    # without this they both pick the same entry and upload it twice (seen on
    # a real run). O_EXCL is the one cross-platform atomic "create if absent",
    # and a claim older than CLAIM_STALE_SECONDS is treated as abandoned so a
    # crashed uploader can't wedge an entry forever.
    CLAIM_STALE_SECONDS = 6 * 3600

    def _claim_path(self, entry_id: str) -> Path:
        return self.queue_dir / f"{entry_id}.claim"

    def claim(self, entry_id: str) -> bool:
        """Take exclusive ownership of an entry. False if someone else has it."""
        path = self._claim_path(entry_id)
        try:
            if path.exists() and (time.time() - path.stat().st_mtime) > self.CLAIM_STALE_SECONDS:
                path.unlink(missing_ok=True)
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        except OSError:
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"pid": os.getpid(), "at": time.time()}))
        return True

    def release(self, entry_id: str) -> None:
        self._claim_path(entry_id).unlink(missing_ok=True)

    def reset_failed(self) -> int:
        """Give every terminally-failed entry a fresh set of attempts.

        Called when the server settings change. Found on a real run: a wrong
        token 403'd eight times, the entry went "failed", and after the token
        was corrected in Settings that meeting still never uploaded -- nothing
        ever looks at a failed entry again. A settings change is exactly the
        moment the reason for the failure may have gone away, so it is the
        moment to try again. Returns how many entries were reset.
        """
        count = 0
        for entry in self.pending():
            if entry.get("status") != "failed":
                continue
            state = self.read_state(entry["id"])
            if state is None:
                continue
            state["status"] = "pending"
            state["attempts"] = 0
            state["next_attempt_at"] = None
            self.write_state(entry["id"], state)
            count += 1
        return count

    def retry_all_now(self) -> int:
        """Make every waiting entry due immediately, failed or merely backed off.

        Called when the server settings change. ``reset_failed`` alone was not
        enough: an upload rejected for a wrong token is deliberately held
        "pending" at the 5-minute backoff (so it never exhausts its attempts),
        which meant that after the token was fixed only whichever entry
        happened to be due uploaded, and the rest sat for up to five minutes
        looking stuck. Returns how many entries were woken.
        """
        count = self.reset_failed()
        if count:
            log.info("queue: reset %d failed entries", count)
        for entry in self.pending():
            if not entry.get("next_attempt_at"):
                continue
            state = self.read_state(entry["id"])
            if state is None:
                continue
            state["next_attempt_at"] = None
            self.write_state(entry["id"], state)
            count += 1
        log.info("queue: retry_all_now woke %d entries", count)
        return count

    def mark_track_uploaded(self, entry_id: str, track: str) -> None:
        """Record that ``track`` has been fully sent and acknowledged.

        Read-modify-write like every other state change here, so a crash or
        another failed track right after this one still leaves the record of
        *this* track's success on disk -- a retry only has to redo whatever
        didn't make it, not start the whole session over.
        """
        state = self.read_state(entry_id)
        if state is None:
            return  # entry already gone (e.g. raced with mark_done) -- nothing to record
        uploaded = list(state.get("uploaded_tracks") or [])
        if track not in uploaded:
            uploaded.append(track)
        state["uploaded_tracks"] = uploaded
        self.write_state(entry_id, state)

    def mark_finalized(self, entry_id: str, job_id: str) -> Optional[Dict[str, Any]]:
        """Persist that finalize succeeded, so it is never repeated for this entry."""
        return self.update_progress(
            entry_id,
            finalized=True,
            job_id=job_id,
            last_error=None,
            upload_state="complete",
            upload_percent=100.0,
        )

    def clear_finalized(self, entry_id: str) -> Optional[Dict[str, Any]]:
        """Forget a finalize the server no longer honours (job/session 404)."""
        return self.update_progress(entry_id, finalized=False, job_id=None)

    def update_progress(self, entry_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
        """Persist upload/transcription lifecycle fields and return new state."""
        state = self.read_state(entry_id)
        if state is None:
            return None
        state.update(fields)
        state["progress_updated_at"] = time.time()
        self.write_state(entry_id, state)
        return state


def _is_auth_error(exc: BaseException) -> bool:
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) in (401, 403)


def _is_client_too_old(exc: BaseException) -> bool:
    """HTTP 426: the server no longer accepts this client version."""
    return version_gate.is_too_old_error(exc)


def _is_not_found(exc: BaseException) -> bool:
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) == 404


class TranscriptionFailed(RuntimeError):
    """The server accepted the upload but its transcription job errored.

    This is a *transcription* problem, not an upload problem: the audio is on
    the server and finalized, so it must never send the entry back through
    upload/finalize.
    """


class _JobGone(Exception):
    """The server no longer knows the job (or session): finalize is needed again."""


class _WorkerStopping(Exception):
    """The worker is shutting down mid-wait; not a failure, just resume later."""


class _StillTranscribing(Exception):
    """The wait budget for this pass ran out; poll again on the next pass."""


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


def _expected_pcm16_frames(wav_path: Path, target_rate: int = wire.STREAM_SAMPLE_RATE) -> int:
    """How many 16 kHz frames converting ``wav_path`` should produce.

    Computed from the WAV header alone (framerate + frame count) -- cheap
    enough to call on every attempt, unlike actually running the resampler,
    which is the whole point: it lets a retry tell a complete ``.pcm16`` left
    behind by a previous attempt from a torn or stale one without redoing the
    conversion just to find out. Mirrors ``Downsampler``'s own integer-ratio
    test so the two agree on the (overwhelmingly common) exact case; a
    device with a non-integer ratio to 16 kHz (e.g. 44.1 kHz) gets a rounded
    estimate instead, which is what the interpolating decimation path in
    resample.py actually converges to.
    """
    with wave.open(str(wav_path), "rb") as src:
        rate = src.getframerate()
        source_frames = src.getnframes()
    if rate <= 0 or target_rate <= 0:
        return 0
    ratio = rate / target_rate
    ratio_rounded = round(ratio)
    if ratio_rounded > 0 and abs(ratio - ratio_rounded) < 1e-9:
        return source_frames // ratio_rounded
    return round(source_frames * target_rate / rate)


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


def _write_transcript(session_dir: Path, transcript: dict) -> None:
    """Write the server's transcript response into transcript.md/.json.

    The server's job endpoint (``meeting_notes.server.jobs``) already renders
    both forms with the exact same ``meeting_notes.transcribe.merge``
    functions the server uses for its final pass, and hands them back as
    ready-to-write strings under ``markdown``/``json``.  The client only
    persists those server-produced results; it never loads a speech model or
    performs transcription locally.
    """
    markdown = transcript.get("markdown")
    json_text = transcript.get("json")
    if markdown is None or json_text is None:
        # A response from a server that doesn't match this shape must not
        # crash the upload worker -- write what's usable, note what isn't.
        markdown = markdown if markdown is not None else "# Meeting transcript\n\n_(no transcript text returned by the server)_\n"
        json_text = json_text if json_text is not None else json.dumps({"segments": []})
    (session_dir / "transcript.md").write_text(markdown, encoding="utf-8")
    (session_dir / "transcript.json").write_text(json_text, encoding="utf-8")


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
        *,
        poll_interval: float = 2.0,
        initial_backoff: float = 5.0,
        max_backoff: float = 300.0,
        max_attempts: int = 8,
        max_poll_delay: float = 30.0,
        poll_budget: float = 120.0,
        client_factory: Optional[Callable[[], ServerClient]] = None,
        on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        """Create a durable uploader.

        ``on_progress`` receives a copy of the queue state after each
        throttled update. It is informational only; queue JSON remains the
        source of truth and callback failures are ignored.
        """
        self.queue = queue
        self.base_url = base_url
        self.token = token
        self.poll_interval = poll_interval
        self.initial_backoff = initial_backoff
        self.max_backoff = max_backoff
        self.max_attempts = max_attempts
        # Waiting for the server's transcription job is not an upload failure
        # and has no failure timeout: the job is polled with a growing delay
        # (poll_interval .. max_poll_delay).  One pass polls for at most
        # ``poll_budget`` seconds, then yields so other queue entries are not
        # starved and resumes on a later pass.
        self.max_poll_delay = max_poll_delay
        self.poll_budget = poll_budget
        # The generous read/write timeout is specifically for this worker's
        # own uploads; anything else that wants a plain ServerClient (health
        # checks, doctor probes) still gets the short float default.
        self._client_factory = client_factory or (
            lambda: ServerClient(base_url, token, timeout=_UPLOAD_TIMEOUT)
        )
        self.on_progress = on_progress

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
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - the worker must outlive any one bad pass
                # A crash here used to end the thread, so nothing uploaded
                # again until the app was restarted.
                log.exception("upload pass failed; retrying on the next poll")
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
        if not self.queue.claim(entry_id):
            return  # another uploader (the app, or a terminal) has this one

        try:
            session_dir = Path(entry["session_dir"])
            log.info(
                "upload attempt %d for %s (%s)", int(entry.get("attempts", 0)) + 1, entry_id, session_dir
            )
            self._upload_session(entry_id, session_dir, entry)
        except _WorkerStopping:
            log.info("upload worker stopping; %s resumes on the next start", entry_id)
            self.queue.release(entry_id)
            return
        except _StillTranscribing:
            # Uploaded and finalized; the server is just busy.  Not an
            # error: keep the entry, check again after a pause.
            self.queue.release(entry_id)
            self.queue.update_progress(entry_id, next_attempt_at=time.time() + self.max_poll_delay)
            return
        except TranscriptionFailed as exc:
            # The audio is on the server; its transcription job errored.
            # Surface it -- and stop: re-uploading cannot fix that.
            self.queue.release(entry_id)
            message = f"Transcription failed on the server: {exc}"
            log.warning("transcription failed for %s: %s", entry_id, exc)
            self.queue.mark_attempt_failed(entry_id, message, next_attempt_at=None, terminal=True)
            self._report_progress(
                entry_id, upload_state="complete", transcription_state="error", last_error=message
            )
            return
        except Exception as exc:  # noqa: BLE001 - one bad entry must not sink the worker
            self.queue.release(entry_id)
            finalized = bool((self.queue.read_state(entry_id) or {}).get("finalized"))
            attempts = int(entry.get("attempts", 0)) + 1
            backoff = min(self.initial_backoff * (2 ** (attempts - 1)), self.max_backoff)
            # After a successful finalize the upload is over: never go
            # terminal (or re-upload) over what is only a transcript fetch.
            terminal = attempts >= self.max_attempts and not finalized
            if _is_auth_error(exc) or _is_client_too_old(exc):
                # A 401/403 (or a 426 "update the client") is a configuration
                # problem, not a flaky network:
                # burning through the attempt budget and going terminal just
                # guarantees the session is stranded once the token IS fixed.
                # Hold it at the slow backoff instead; retry_all_now() /
                # a settings change is what should wake it up.
                terminal = False
                backoff = self.max_backoff
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            log.warning(
                "upload failed for %s: %s: %s (http=%s, attempt %d, %s)",
                entry_id, type(exc).__name__, exc, status_code, attempts,
                "giving up" if terminal else f"retry in {backoff:.0f}s",
            )
            self.queue.mark_attempt_failed(
                entry_id,
                f"{type(exc).__name__}: {exc}",
                next_attempt_at=time.time() + backoff,
                terminal=terminal,
            )
            if finalized:
                self._report_progress(
                    entry_id, upload_state="complete", last_error=f"{type(exc).__name__}: {exc}"
                )
            else:
                self._report_progress(
                    entry_id,
                    upload_state="error" if terminal else "pending",
                    transcription_state="error" if terminal else "pending",
                    last_error=f"{type(exc).__name__}: {exc}",
                )
            if terminal:
                # No future attempt is coming to clean these up itself -- a
                # half-uploaded track's leftover .pcm16 would otherwise sit
                # next to a session marked failed forever.
                raw_session_dir = entry.get("session_dir")
                if raw_session_dir:
                    self._cleanup_leftover_pcm16(Path(raw_session_dir))
            return
        log.info("upload complete for %s", entry_id)
        self.queue.mark_done(entry_id)

    def _report_progress(self, entry_id: str, **fields: Any) -> None:
        """Persist lifecycle progress and notify an optional UI observer."""
        state = self.queue.update_progress(entry_id, **fields)
        if state is not None and self.on_progress is not None:
            try:
                self.on_progress(dict(state))
            except Exception:
                # Progress is diagnostic/UI-only and must never affect upload.
                pass

    def _cleanup_leftover_pcm16(self, session_dir: Path) -> None:
        if not session_dir.exists():
            return
        try:
            for pcm_path in session_dir.glob("*.pcm16"):
                pcm_path.unlink(missing_ok=True)
        except OSError:
            pass  # best-effort tidy-up; a missing/unreadable dir is not worth failing over

    # -- the actual upload ----------------------------------------------------

    def _upload_session(self, entry_id: str, session_dir: Path, entry: Dict[str, Any]) -> None:
        if not session_dir.exists():
            raise FileNotFoundError(f"session directory is gone: {session_dir}")
        meta_path = session_dir / "session.json"
        if not meta_path.exists():
            raise FileNotFoundError(f"session.json missing in {session_dir}")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        session_id = session_dir.name
        # Tracks a previous attempt already got an ack for -- skip them
        # entirely rather than re-converting and re-uploading a track the
        # server already has just because a *different* track failed.
        uploaded_tracks = set(entry.get("uploaded_tracks") or [])

        # Compute the aggregate wire size from WAV headers before conversion.
        # This is O(number of tracks), not O(audio length), and allows the
        # progress callback to report a meaningful whole-session percentage.
        track_totals: Dict[str, int] = {}
        for track, info in sorted((meta.get("tracks") or {}).items()):
            wav_name = info.get("wav")
            wav_path = session_dir / wav_name if wav_name else None
            if wav_path is None or not wav_path.exists():
                continue
            track_totals[track] = _expected_pcm16_frames(wav_path) * 2
        completed_bytes = sum(track_totals.get(track, 0) for track in uploaded_tracks)
        total_bytes = sum(track_totals.values())
        client = self._client_factory()
        last_server_report_at = [0.0]
        last_server_percent = [-1.0]

        def report(**fields: Any) -> None:
            self._report_progress(entry_id, **fields)
            # The server's session page has its own pipeline state.  Status
            # publication is best-effort: an offline server must never turn a
            # successful local upload into a failed queue attempt.
            payload = {
                "name": meta.get("name") or session_id,
                "device": meta.get("device") or "",
            }
            # The pipeline endpoint intentionally accepts only upload fields;
            # transcription progress is derived from the finalized job on the
            # server.  Local queue state still retains both phases for the
            # desktop UI.
            if "upload_state" not in fields:
                return
            payload.update(
                state=fields.get("upload_state"),
                percent=fields.get("upload_percent", 0.0),
                bytes_received=fields.get("upload_bytes", 0),
                bytes_total=fields.get("upload_total", 0),
            )
            now = time.monotonic()
            percent = fields.get("upload_percent")
            if percent is None:
                percent = fields.get("transcription_percent")
            try:
                percent = float(percent) if percent is not None else None
            except (TypeError, ValueError):
                percent = None
            state_change = fields.get("upload_state") in {"pending", "complete", "error"} or fields.get(
                "transcription_state"
            ) in {"complete", "error"}
            meaningful = (
                state_change
                or now - last_server_report_at[0] >= 2.0
                or (percent is not None and abs(percent - last_server_percent[0]) >= 1.0)
            )
            if not meaningful:
                return
            last_server_report_at[0] = now
            if percent is not None:
                last_server_percent[0] = percent
            try:
                client.report_upload_status(session_id, payload)
            except Exception:
                pass

        if entry.get("finalized") and entry.get("job_id"):
            # Finalize already succeeded on an earlier pass (or before a
            # restart): the upload is over.  Only wait for the transcript.
            log.info("resuming transcription wait for %s (job %s); not re-uploading", entry_id, entry["job_id"])
            try:
                self._await_transcript(client, entry_id, session_dir, meta, entry["job_id"], report)
            finally:
                client.close()
            return

        try:
            report(
                # ``pending`` creates the server-side session row before the
                # first large track body arrives.  It is required for fresh
                # recorder sessions; subsequent updates use uploading.
                upload_state="pending",
                upload_percent=0.0,
                upload_bytes=completed_bytes,
                upload_total=total_bytes,
                transcription_state="pending",
            )
            for track, info in sorted((meta.get("tracks") or {}).items()):
                if track in uploaded_tracks:
                    continue
                wav_name = info.get("wav")
                if not wav_name:
                    continue
                wav_path = session_dir / wav_name
                if not wav_path.exists():
                    continue
                pcm_path = wav_path.with_suffix(".pcm16")
                expected_frames = _expected_pcm16_frames(wav_path)
                if pcm_path.exists() and pcm_path.stat().st_size == expected_frames * 2:
                    # A previous attempt already produced this exact
                    # conversion and just never got it acknowledged (the
                    # upload itself failed, or a different track did) --
                    # reuse it instead of re-reading and re-filtering the
                    # whole WAV again.
                    frames = expected_frames
                else:
                    frames = _wav_to_pcm16(wav_path, pcm_path)

                last_report_at = [0.0]

                def on_chunk(sent: int, _track_total: int, *, track_name=track) -> None:
                    nonlocal completed_bytes
                    now = time.monotonic()
                    aggregate = completed_bytes + sent
                    # Persist at most five times a second, plus the final
                    # chunk. Chunk callbacks remain exact and bounded-memory;
                    # only queue JSON writes are throttled.
                    if (
                        sent < _track_total
                        and now - last_report_at[0] < 0.2
                    ):
                        return
                    last_report_at[0] = now
                    report(
                        upload_state="uploading",
                        upload_percent=round(aggregate * 100.0 / total_bytes, 2) if total_bytes else 0.0,
                        upload_bytes=aggregate,
                        upload_total=total_bytes,
                        upload_track=track_name,
                    )

                self._upload_track_with_progress(
                    client, session_id, track, pcm_path, frames, on_chunk
                )
                # Only cleaned up once the upload actually succeeded -- a
                # failed attempt leaves it behind so the retry doesn't have
                # to redo a conversion whose result was never sent anywhere.
                pcm_path.unlink(missing_ok=True)
                uploaded_tracks.add(track)
                completed_bytes += pcm_path.stat().st_size if pcm_path.exists() else track_totals.get(track, frames * 2)
                self.queue.mark_track_uploaded(entry_id, track)
                report(
                    upload_state="uploading",
                    upload_percent=round(completed_bytes * 100.0 / total_bytes, 2) if total_bytes else 100.0,
                    upload_bytes=completed_bytes,
                    upload_total=total_bytes,
                    upload_track=track,
                )

            job_id = self._finalize(client, entry_id, session_dir, meta, session_id, report, total_bytes)
        except Exception as exc:
            report(
                upload_state="error",
                transcription_state="error",
                last_error=f"{type(exc).__name__}: {exc}",
            )
            client.close()
            raise
        try:
            # From here the upload is DONE.  Nothing below may mark it failed
            # or trigger another finalize (except the one 404 case).
            self._await_transcript(client, entry_id, session_dir, meta, job_id, report)
        finally:
            client.close()

    def _finalize(self, client, entry_id, session_dir, meta, session_id, report, total_bytes) -> str:
        """POST /finalize once and persist the job id (and that it happened)."""
        timing = _collect_timing(session_dir)
        job_id = client.finalize(session_id, meta, timing)
        self.queue.mark_finalized(entry_id, job_id)
        log.info("finalize ok for %s: job %s", entry_id, job_id)
        report(
            upload_state="complete",
            upload_percent=100.0,
            upload_bytes=total_bytes,
            upload_total=total_bytes,
            transcription_state="pending",
            transcription_percent=0.0,
            job_id=job_id,
        )
        return job_id

    def _await_transcript(self, client, entry_id, session_dir, meta, job_id, report) -> None:
        """Wait for the job, write the transcript.  Never re-uploads.

        Raises TranscriptionFailed for a server-side job error,
        _StillTranscribing when this pass's wait budget is spent, and
        _WorkerStopping on shutdown.  If the server says the job is gone
        (404) the entry is finalized again exactly once.
        """
        refinalized = False
        while True:
            try:
                transcript = self._poll_job(
                    client, job_id, entry_id=entry_id, progress_callback=report
                )
            except _JobGone:
                if refinalized:
                    raise RuntimeError(f"server has no job {job_id} even after re-finalizing")
                refinalized = True
                log.warning("job %s for %s is gone on the server (404); finalizing once more", job_id, entry_id)
                self.queue.clear_finalized(entry_id)
                job_id = self._finalize(
                    client, entry_id, session_dir, meta, session_dir.name, report, 0
                )
                continue
            _write_transcript(session_dir, transcript)
            return

    @staticmethod
    def _upload_track_with_progress(client, session_id, track, pcm_path, frames, callback):
        """Call old injected test clients and new progress-aware clients."""
        method = client.upload_track
        try:
            supports_progress = "progress_callback" in inspect.signature(method).parameters
        except (TypeError, ValueError):
            supports_progress = True
        if supports_progress:
            return method(
                session_id, track, pcm_path, frames, progress_callback=callback
            )
        return method(session_id, track, pcm_path, frames)

    def _poll_job(
        self,
        client: ServerClient,
        job_id: str,
        *,
        entry_id: Optional[str] = None,
        progress_callback: Optional[Callable[..., None]] = None,
    ) -> Dict[str, Any]:
        """Poll a finalized job until it is done, with growing delays.

        There is deliberately no failure timeout: a job may sit behind a
        multi-hour one on the server's single worker.  Transient errors
        (unreachable, 5xx) just keep polling.  Auth errors propagate (the
        entry is already finalized, so the retry only resumes polling).  A
        404 means the job is gone (_JobGone); a job in state "error" is a
        TranscriptionFailed.  After ``poll_budget`` seconds this pass gives
        up politely (_StillTranscribing) so other entries get a turn.
        """
        delay = max(self.poll_interval, 0.0)
        started = time.monotonic()
        while True:
            info = None
            try:
                info = client.job(job_id)
            except Exception as exc:  # noqa: BLE001
                if _is_not_found(exc):
                    raise _JobGone() from exc
                if _is_auth_error(exc) or _is_client_too_old(exc):
                    raise
                if not isinstance(exc, (ServerUnavailable, httpx.HTTPError)):
                    raise
                log.info("job %s poll failed (%s); will keep waiting", job_id, exc)
            if info is not None:
                state = info.get("state")
                if entry_id is not None:
                    raw_percent = info.get("transcription_percent")
                    if raw_percent is None:
                        raw_percent = float(info.get("progress") or 0.0) * 100.0
                    fields = dict(
                        transcription_state=(
                            "complete" if state == wire.JobState.DONE
                            else "error" if state == wire.JobState.ERROR
                            else "transcribing" if state == wire.JobState.RUNNING
                            else "queued"
                        ),
                        transcription_percent=round(float(raw_percent), 2),
                        job_id=job_id,
                    )
                    if progress_callback is not None:
                        progress_callback(**fields)
                    else:
                        self._report_progress(entry_id, **fields)
                if state == wire.JobState.DONE:
                    return client.transcript(job_id)
                if state == wire.JobState.ERROR:
                    raise TranscriptionFailed(info.get("error") or f"job {job_id} failed")
            if self._stop_event.is_set():
                raise _WorkerStopping()
            if time.monotonic() - started >= self.poll_budget:
                raise _StillTranscribing()
            if self._stop_event.wait(delay):
                raise _WorkerStopping()
            delay = min(max(delay * 1.5, self.poll_interval), self.max_poll_delay)
