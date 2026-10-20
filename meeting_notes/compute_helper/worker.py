"""The helper's poll loop: claim a lease, download audio, transcribe / diarize on the GPU, post the result.

HTTP contract: docs/gpu-helper-design.md ("Work"). Mirrors ``meeting_notes.bridge`` (httpx client, poll loop,
logging) with two differences: a heartbeat thread keeps the lease alive for the whole job (including model
loading), and the job can be aborted from three places (revoked lease, ``stop`` command, engine error).
"""

from __future__ import annotations

import inspect
import logging
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import httpx

from .. import __version__
from . import RUNTIME_VERSION
from .ipc import Ipc

log = logging.getLogger("meeting_notes.compute_helper")

POLL_PATH = "/v1/compute/poll"
HEARTBEAT_PATH = "/v1/compute/jobs/{job_id}/heartbeat"
COMPLETE_PATH = "/v1/compute/jobs/{job_id}/complete"
RELEASE_PATH = "/v1/compute/jobs/{job_id}/release"
DIARIZE_TRACK = "system"  # only the mixed far-end track is diarized
DIARIZATION_SHARE = 0.5  # of the overall progress, when diarizing (on a GPU it takes about as long as transcribing)
PROGRESS_BEAT_SECONDS = 2.0  # progress changes reach the server this often, between the lease heartbeats
STOPPED_ERROR = "helper stopped"
IDLE_UNLOAD_SECONDS = 600.0  # free VRAM between meetings (the GPU may be shared)


def _takes_progress(fn: Any) -> bool:
    """Whether ``fn`` takes a ``progress`` keyword (test engines and older diarizers do not)."""
    try:
        return "progress" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


class JobAborted(BaseException):
    """Unwinds a job. A BaseException on purpose: faster-whisper's progress hook swallows ``Exception``."""


class FatalError(RuntimeError):
    """Configuration the helper cannot recover from (rejected key, unusable server). Exit non-zero."""


class RequestFailed(RuntimeError):
    """The server could not be reached, or answered with an error, after the allowed retries."""


@dataclass
class WorkerConfig:
    server: str
    key: str
    pack_version: str = ""
    poll_seconds: float = 5.0
    timeout: float = 30.0
    retries: int = 4
    retry_base: float = 1.0  # first backoff step; doubles, capped at 30 s
    work_dir: Optional[str] = None


class _Job:
    def __init__(self, lease: Dict[str, Any]):
        self.lease = lease
        self.id = str(lease["job_id"])
        self.attempt = str(lease["attempt"])
        self.secret = str(lease["lease"])
        self.board = lease.get("board")
        self.abort = threading.Event()
        self.revoked = False  # the server no longer wants this attempt: discard, do not release
        self.progress = 0.0
        self.step = "starting"
        self.stage = "transcribing"  # "transcribing" or "speakers"
        self.transcribe_progress = 0.0
        self.speakers_progress: Optional[float] = None  # None: this job does not identify speakers

    def progress_fields(self) -> Dict[str, Any]:
        return {"progress": round(self.progress, 4), "step": self.step, "stage": self.stage,
                "transcribe_progress": round(self.transcribe_progress, 4),
                "speakers_progress": None if self.speakers_progress is None else round(self.speakers_progress, 4)}

    def body(self, **extra: Any) -> Dict[str, Any]:
        return {"attempt": self.attempt, "lease": self.secret, **extra}


class Worker:
    def __init__(self, config: WorkerConfig, engine: Any, ipc: Ipc, *, gpu: Optional[dict] = None,
                 client: Optional[httpx.Client] = None, live: Optional[Any] = None):
        self.config = config
        self.engine = engine
        self.ipc = ipc
        self.gpu = gpu
        self.client = client or httpx.Client(
            base_url=config.server.rstrip("/"), timeout=config.timeout, follow_redirects=False)
        self._headers = {"Authorization": f"Bearer {config.key}"}
        self._stop = threading.Event()
        self._paused = threading.Event()  # tray / user pause: no batch claims, live connection closed
        self._recording = threading.Event()  # this PC is recording: no batch claims, live keeps serving
        self.live = live  # a LiveClient (or None); its status rides on every status event
        self._job: Optional[_Job] = None

    def _gpu_status(self) -> Optional[dict]:
        """The GPU facts for status events, plus the Whisper compute type once a model is loaded."""
        compute_type = getattr(self.engine, "compute_type", None)
        if self.gpu is None or not compute_type:
            return self.gpu
        return {**self.gpu, "compute_type": compute_type}

    def _batch_held(self) -> bool:
        return self._paused.is_set() or self._recording.is_set()

    # -- supervisor commands -------------------------------------------------

    def command(self, cmd: str) -> None:
        if cmd == "pause":
            self._paused.set()
            if self.live is not None:
                self.live.set_paused(True)
        elif cmd == "resume":
            self._paused.clear()
            if self.live is not None:
                self.live.set_paused(False)
        elif cmd == "record":
            self._recording.set()
        elif cmd == "unrecord":
            self._recording.clear()
        elif cmd == "stop":
            self._stop.set()
            if self.live is not None:
                self.live.set_paused(True)
            job = self._job
            if job is not None:
                job.abort.set()

    def stop(self) -> None:
        self.command("stop")

    # -- HTTP ----------------------------------------------------------------

    def _request(self, method: str, path: str, *, retries: Optional[int] = None, **kwargs: Any) -> httpx.Response:
        """One request with backoff on connection errors / timeouts. Never logs the key."""
        attempts = (self.config.retries if retries is None else retries) + 1
        headers = {**self._headers, **kwargs.pop("headers", {})}
        last: Optional[Exception] = None
        for n in range(attempts):
            try:
                return self.client.request(method, path, headers=headers, **kwargs)
            except httpx.TransportError as exc:
                last = exc
                if n + 1 < attempts:
                    time.sleep(min(self.config.retry_base * (2 ** n), 30.0))
        raise RequestFailed(f"server request failed: {last}")

    def _poll(self) -> Optional[Dict[str, Any]]:
        """A lease, or None. Raises FatalError / RequestFailed. Sets ``self._outdated``."""
        paused = self._batch_held()
        response = self._request("POST", POLL_PATH, retries=0, json={
            "version": __version__, "runtime_version": RUNTIME_VERSION,
            "pack_version": self.config.pack_version, "gpu": self.gpu,
            "state": "paused" if paused else "idle", "paused": paused,
        })
        if response.status_code in (401, 403):
            raise FatalError("the server rejected this helper's key (revoked or wrong server)")
        self._outdated = response.headers.get("X-Pack-Outdated") == "1"
        if response.status_code == 204:
            return None
        if response.status_code != 200:
            raise RequestFailed(f"poll answered {response.status_code}")
        try:
            lease = response.json()
            if not isinstance(lease, dict):
                raise ValueError("not an object")
            for field in ("job_id", "attempt", "lease", "tracks"):
                lease[field]
        except (ValueError, KeyError) as exc:
            raise RequestFailed(f"malformed lease from the server: {exc}") from exc
        return lease

    # -- main loop -----------------------------------------------------------

    def run(self) -> int:
        self._outdated = False
        self.ipc.status("starting", gpu=self._gpu_status())
        if self.live is not None:
            self.live.set_paused(self._paused.is_set())
            self.live.start()
        code = 1
        try:
            code = self._run_loop()
        finally:
            if self.live is not None:
                self.live.stop()
            if code == 0:
                self.ipc.status("stopped", gpu=self._gpu_status())
            self.client.close()
        return code

    def _run_loop(self) -> int:
        failures = 0
        while not self._stop.is_set():
            try:
                lease = self._poll()
            except FatalError as exc:
                log.error("%s", exc)
                self.ipc.status("error", gpu=self._gpu_status(), error=str(exc))
                return 2
            except Exception as exc:  # noqa: BLE001 - transient; never crash the loop
                failures += 1
                log.warning("poll failed: %s", exc)
                self.ipc.status("offline", gpu=self._gpu_status(), error=str(exc)[:300])
                self._stop.wait(min(self.config.poll_seconds * (2 ** min(failures, 4)), 60.0))
                continue
            failures = 0
            if lease is None:
                if self._outdated:
                    self.ipc.status("update needed", gpu=self._gpu_status())
                else:
                    unload = getattr(self.engine, "unload_if_idle", None)
                    if unload is not None:
                        try:
                            unload(IDLE_UNLOAD_SECONDS)
                        except Exception:  # noqa: BLE001
                            log.debug("idle unload failed", exc_info=True)
                    self.ipc.status("paused" if self._batch_held() else "idle", gpu=self._gpu_status())
                self._stop.wait(self.config.poll_seconds)
                continue
            if self._batch_held() or self._outdated:
                # Work arrived anyway (race with pause): hand it straight back.
                self._release_lease(_Job(lease), "helper paused")
                self._stop.wait(self.config.poll_seconds)
                continue
            self._process(lease)
        return 0

    # -- one job -------------------------------------------------------------

    def _process(self, lease: Dict[str, Any]) -> None:
        job = _Job(lease)
        self._job = job
        if self._stop.is_set():
            job.abort.set()
        heartbeat_stop = threading.Event()
        beat = threading.Thread(
            target=self._heartbeat_loop, args=(job, heartbeat_stop, float(lease.get("heartbeat_seconds") or 15)),
            name="compute-helper-heartbeat", daemon=True)
        beat.start()
        work: Optional[Path] = None
        try:
            if self.config.work_dir:
                Path(self.config.work_dir).mkdir(parents=True, exist_ok=True)
            work = Path(tempfile.mkdtemp(prefix="meeting-notes-helper-", dir=self.config.work_dir))
            self._set_progress(job, 0.0, "starting")
            payload = self._run_job(job, work)
            self._complete(job, payload)
        except JobAborted:
            if job.revoked:
                log.info("job %s: lease revoked, discarding", job.id)
            else:
                self._release_lease(job, STOPPED_ERROR)
        except Exception as exc:  # noqa: BLE001 - any engine / download failure goes back to the server
            log.exception("job %s failed", job.id)
            self._release_lease(job, f"{type(exc).__name__}: {exc}"[:500])
        finally:
            heartbeat_stop.set()
            beat.join(timeout=5)
            if work is not None:
                shutil.rmtree(work, ignore_errors=True)
            self._job = None
            if not self._stop.is_set():
                self.ipc.status("paused" if self._batch_held() else "idle", gpu=self._gpu_status())

    def _check(self, job: _Job) -> None:
        if job.abort.is_set() or self._stop.is_set():
            raise JobAborted()

    def _set_progress(self, job: _Job, progress: float, step: str) -> None:
        job.progress = max(0.0, min(1.0, progress))
        job.step = step
        self.ipc.status("working", job=job.id, board=job.board, progress=round(job.progress, 2), gpu=self._gpu_status())

    def _run_job(self, job: _Job, work: Path) -> Dict[str, Any]:
        lease = job.lease
        tracks: List[Dict[str, Any]] = list(lease["tracks"])
        whisper = lease.get("whisper") or {}
        diarization = lease.get("diarization") or {}
        started = time.monotonic()
        timings: Dict[str, float] = {}

        self._set_progress(job, 0.0, "downloading")
        paths: Dict[str, Path] = {}
        for track in tracks:
            self._check(job)
            paths[track["name"]] = self._download(job, track, work)
        timings["download"] = round(time.monotonic() - started, 2)

        diarize = bool(
            diarization.get("enabled") and self.engine.can_diarize()
            and any(t["name"] == DIARIZE_TRACK for t in tracks))
        share = DIARIZATION_SHARE if diarize else 0.0
        job.speakers_progress = 0.0 if diarize else None
        total = sum(max(float(t.get("seconds") or 0.0), 0.001) for t in tracks)

        result: Dict[str, List[Dict[str, float]]] = {}
        done = 0.0
        began = time.monotonic()
        for track in tracks:
            name = track["name"]
            seconds = max(float(track.get("seconds") or 0.0), 0.001)
            self._check(job)
            job.transcribe_progress = done / total
            self._set_progress(job, done / total * (1 - share), f"transcribing {name}")

            def on_progress(fraction: float, _done=done, _seconds=seconds, _name=name) -> None:
                self._check(job)
                job.transcribe_progress = (_done + fraction * _seconds) / total
                self._set_progress(job, job.transcribe_progress * (1 - share), f"transcribing {_name}")

            result[name] = self.engine.transcribe(paths[name], name, whisper, on_progress)
            self._check(job)
            done += seconds
        timings["transcribe"] = round(time.monotonic() - began, 2)
        job.transcribe_progress = 1.0

        speaker_turns = None
        if diarize:
            began = time.monotonic()
            job.stage = "speakers"
            self._set_progress(job, 1 - share, "diarizing")

            def on_speakers(fraction: float) -> None:
                self._check(job)
                job.speakers_progress = min(max(float(fraction), 0.0), 1.0)
                self._set_progress(job, 1 - share + job.speakers_progress * share, "diarizing")

            if _takes_progress(self.engine.diarize):
                speaker_turns = self.engine.diarize(paths[DIARIZE_TRACK], diarization, progress=on_speakers)
            else:
                speaker_turns = self.engine.diarize(paths[DIARIZE_TRACK], diarization)
            self._check(job)
            job.speakers_progress = 1.0
            timings["diarize"] = round(time.monotonic() - began, 2)
        timings["total"] = round(time.monotonic() - started, 2)
        self._set_progress(job, 1.0, "uploading")
        return {"device": getattr(self.engine, "device_label", "cuda"), "timings": timings,
                "tracks": result, "speaker_turns": speaker_turns}

    def _download(self, job: _Job, track: Dict[str, Any], work: Path) -> Path:
        target = work / f"{track['name']}.wav"
        headers = {"X-Attempt": job.attempt, "X-Lease": job.secret}
        attempts = self.config.retries + 1
        for n in range(attempts):
            try:
                with self.client.stream("GET", track["url"], headers={**self._headers, **headers}) as response:
                    if response.status_code in (409, 410):
                        job.revoked = True
                        job.abort.set()
                        raise JobAborted()
                    if response.status_code != 200:
                        raise RuntimeError(f"audio download for {track['name']!r} answered {response.status_code}")
                    with target.open("wb") as handle:
                        for chunk in response.iter_bytes():
                            self._check(job)
                            handle.write(chunk)
                return target
            except httpx.TransportError as exc:
                log.warning("audio download for %s failed (%s), retrying", track["name"], exc)
                if n + 1 >= attempts:
                    raise RuntimeError(f"audio download failed: {exc}") from exc
                time.sleep(min(self.config.retry_base * (2 ** n), 30.0))
        raise RuntimeError("unreachable")  # pragma: no cover

    # -- lease upkeep --------------------------------------------------------

    def _heartbeat_loop(self, job: _Job, done: threading.Event, interval: float) -> None:
        """Renew the lease every ``interval`` seconds, and send progress sooner (every PROGRESS_BEAT_SECONDS) while
        it is moving, so the meeting's progress bars move smoothly instead of in 15-second jumps."""
        interval = max(interval, 0.01)
        tick = min(interval, PROGRESS_BEAT_SECONDS)
        last_sent: Optional[Dict[str, Any]] = None
        last_time = time.monotonic()
        while not done.wait(tick):
            fields = job.progress_fields()
            if fields == last_sent and time.monotonic() - last_time < interval - 1e-3:
                continue
            last_sent, last_time = fields, time.monotonic()
            try:
                response = self._request(
                    "POST", HEARTBEAT_PATH.format(job_id=job.id), retries=0, json=job.body(**fields))
            except Exception as exc:  # noqa: BLE001 - a missed beat is survivable; the next may land
                log.warning("heartbeat failed: %s", exc)
                continue
            if response.status_code in (409, 410):
                log.info("job %s: heartbeat refused (%s), aborting", job.id, response.status_code)
                job.revoked = True
                job.abort.set()
                return

    def _complete(self, job: _Job, payload: Dict[str, Any]) -> None:
        try:
            response = self._request("POST", COMPLETE_PATH.format(job_id=job.id), json=job.body(**payload))
        except RequestFailed as exc:
            log.error("job %s: could not deliver the result: %s", job.id, exc)
            self._release_lease(job, f"result not delivered: {exc}"[:500])
            return
        if response.status_code == 200:
            log.info("job %s: complete", job.id)
        elif response.status_code in (409, 410):
            log.info("job %s: result no longer wanted (%s)", job.id, response.status_code)
        else:
            detail = response.text[:300]
            log.error("job %s: server answered %s to the result: %s", job.id, response.status_code, detail)
            self._release_lease(job, f"result rejected ({response.status_code}): {detail}")

    def _release_lease(self, job: _Job, error: str) -> None:
        try:
            self._request("POST", RELEASE_PATH.format(job_id=job.id), retries=2, json=job.body(error=error))
        except Exception as exc:  # noqa: BLE001 - the server's sweeper reclaims it anyway
            log.warning("job %s: release failed: %s", job.id, exc)
