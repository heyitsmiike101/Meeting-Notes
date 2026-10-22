"""Upload protocol, state machine and preparation of the mobile recorder (``docs/mobile-recorder-design.md``).

A phone records in *runs* (one ``MediaRecorder`` start..stop) made of *chunks* (``dataavailable`` blobs). The page
PUTs each chunk, then ``finish`` freezes a manifest; a single background worker joins each run's chunks, decodes them
with ffmpeg, lays the runs on one 16 kHz mono ``system`` track (silence over the gaps), writes the timing log and
session meta and queues the normal transcription job.

On disk (per session, under the media volume)::

    <media>/<id>/mobile/state.json            state record (mobile_common)
    <media>/<id>/mobile/runs/<n>/<seq>.part   a received chunk
    <media>/<id>/mobile/runs/<n>/<seq>.sha    its sha256 (hex), written before the rename of the chunk
    <media>/<id>/mobile/work/                 preparation scratch (joined runs, decoded PCM)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from .. import timing as timing_mod
from .. import wav_io
from ..wire import STREAM_SAMPLE_RATE
from . import auth
from . import mobile_common as mc
from . import store as store_mod

logger = logging.getLogger(__name__)

# -- limits (design "Limits") ----------------------------------------------------------------------------------


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


MAX_CHUNK_BYTES = 8 * 1024 * 1024
MAX_RUNS = 200  # run index 0..199 (every iPhone screen lock starts a new run; the page stops at 190)
MAX_CHUNKS_PER_RUN = 400  # seq 0..399; a 5-minute run at a 5 s timeslice is ~60
MAX_RUN_SECONDS = 4 * 3600  # per-run audio cap at decode (-t); runs are normally 5 min, but the page stops rotating at its run cap
MAX_OPEN_SESSIONS = _env_int("MEETING_NOTES_MOBILE_MAX_OPEN", 5)
MAX_STAGING_BYTES = _env_int("MEETING_NOTES_MOBILE_MAX_STAGING_BYTES", 2 * 1024**3)
MAX_SESSION_BYTES = _env_int("MEETING_NOTES_MAX_UPLOAD_BYTES", 8 * 1024**3)
ABANDONED_AFTER_SECONDS = 14 * 24 * 3600
SWEEP_INTERVAL_SECONDS = 3600
GAP_CAP_SECONDS = 600  # a longer gap becomes GAP_CAPPED_SECONDS of silence
GAP_CAPPED_SECONDS = 10
GAP_LOG_MIN_SECONDS = 2.0  # shorter gaps are rotation noise: silence is still inserted, but not reported
DECODE_TIMEOUT_SECONDS = 1800  # a long run (see MAX_RUN_SECONDS) decodes in well under this
ALLOWED_MIMES = ("audio/webm", "audio/mp4", "video/webm", "video/mp4")
_RUN_STATES = ("recording", "complete", "interrupted", "lost-tail")
_PARTIAL_RUN_STATES = ("interrupted", "lost-tail", "recording")
_MAX_NAME = 200
_UNFINISHED = (mc.OPEN, mc.PREPARING, mc.FAILED)


def _now() -> float:
    return time.time()


def _mime_base(value: str) -> str:
    return (value or "").split(";", 1)[0].strip().lower()


def _ext_for(mime: str) -> str:
    return "mp4" if mime.endswith("mp4") else "webm"


def _platform_of(*texts: str) -> str:
    blob = " ".join(t for t in texts if t).lower()
    if any(k in blob for k in ("iphone", "ipad", "ipod", "ios")):
        return "iOS"
    if "android" in blob or "pixel" in blob or "galaxy" in blob:
        return "Android"
    return ""


class MobileUpload:
    """Holds the registry of unfinished sessions and the preparation worker."""

    def __init__(self, *, store, job_queue, data_root=None):
        self.store = store
        self.job_queue = job_queue
        self.data_root = data_root
        self._reg_lock = threading.Lock()
        self._unfinished: set = set()
        self._queue: "queue.Queue[Optional[str]]" = queue.Queue()
        self._queued: set = set()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_sweep = 0.0
        self.router = APIRouter()
        self._routes()

    # -- registry / state helpers ------------------------------------------------------------------------------

    def _scan(self) -> None:
        found = set()
        base = Path(self.store.media_sessions_dir)
        try:
            names = [p.name for p in base.iterdir() if p.is_dir()]
        except OSError:
            names = []
        for name in names:
            if not store_mod.is_safe_id(name):
                continue
            st = mc.read_state(self.store, name)
            if st and st.get("state") in _UNFINISHED:
                found.add(name)
        with self._reg_lock:
            self._unfinished = found

    def _track(self, sid: str, state: str) -> None:
        with self._reg_lock:
            if state in _UNFINISHED:
                self._unfinished.add(sid)
            else:
                self._unfinished.discard(sid)

    def _unfinished_ids(self) -> List[str]:
        with self._reg_lock:
            return sorted(self._unfinished)

    def staging_bytes(self, exclude: Optional[str] = None) -> int:
        total = 0
        for sid in self._unfinished_ids():
            if sid == exclude:
                continue
            st = mc.read_state(self.store, sid)
            if st:
                total += int(st.get("bytes") or 0)
        return total

    def _save(self, sid: str, st: dict) -> None:
        st["updated"] = _now()
        mc.write_state(self.store, sid, st)
        self._track(sid, st.get("state"))

    def _load(self, sid: str) -> dict:
        st = mc.read_state(self.store, sid)
        if st is None:
            raise HTTPException(status_code=404, detail="unknown mobile session")
        return st

    def _runs_dir(self, sid: str) -> Path:
        return mc.mobile_dir(self.store, sid) / "runs"

    def _chunk_path(self, sid: str, run: int, seq: int) -> Path:
        return self._runs_dir(sid) / str(run) / f"{seq}.part"

    def _received(self, sid: str) -> Dict[int, List[int]]:
        out: Dict[int, List[int]] = {}
        base = self._runs_dir(sid)
        if not base.is_dir():
            return out
        for rd in base.iterdir():
            if not rd.is_dir() or not rd.name.isdigit():
                continue
            seqs = sorted(int(p.stem) for p in rd.glob("*.part") if p.stem.isdigit())
            if seqs:
                out[int(rd.name)] = seqs
        return out

    @staticmethod
    def _ranges(seqs: List[int]) -> List[List[int]]:
        ranges: List[List[int]] = []
        for s in seqs:
            if ranges and s == ranges[-1][1] + 1:
                ranges[-1][1] = s
            else:
                ranges.append([s, s])
        return ranges

    def _update_upload_meta(self, sid: str, st: dict, state: str = "uploading") -> None:
        received = int(st.get("bytes") or 0)

        def upd(meta: dict) -> dict:
            if not meta:
                meta = {"created": st.get("created") or _now(), "tracks": {}}
            meta["upload"] = {"state": state, "percent": 0.0, "bytes_received": received, "bytes_total": 0}
            return meta

        try:
            self.store.update_session_meta(sid, upd)
        except Exception:  # noqa: BLE001 - progress display only
            logger.exception("mobile upload progress meta failed for %s", sid)

    # -- validation --------------------------------------------------------------------------------------------

    @staticmethod
    def _check_id(sid: str) -> None:
        if not store_mod.is_safe_id(sid):
            raise HTTPException(status_code=400, detail=f"invalid session_id: {sid!r}")

    @staticmethod
    def _check_run_seq(run: int, seq: Optional[int] = None) -> None:
        if not 0 <= run < MAX_RUNS:
            raise HTTPException(status_code=400, detail=f"run must be 0..{MAX_RUNS - 1}")
        if seq is not None and not 0 <= seq < MAX_CHUNKS_PER_RUN:
            raise HTTPException(status_code=400, detail=f"seq must be 0..{MAX_CHUNKS_PER_RUN - 1}")

    # -- routes ------------------------------------------------------------------------------------------------

    def _public(self, sid: str, st: dict) -> dict:
        recv = self._received(sid)
        out = {
            "state": st["state"],
            "runs": {str(n): self._ranges(seqs) for n, seqs in sorted(recv.items())},
            "bytes": int(st.get("bytes") or 0),
        }
        if st.get("job_id"):
            out["job_id"] = st["job_id"]
        if st.get("error"):
            out["error"] = st["error"]
        return out

    def _routes(self) -> None:
        r = self.router
        dep = [Depends(auth.require_token)]

        @r.post("/v1/mobile/sessions", dependencies=dep)
        async def create(request: Request):
            mc.require_same_origin(request, self.store.root)
            try:
                body = await request.json()
            except (json.JSONDecodeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail="invalid JSON body") from exc
            if not isinstance(body, dict):
                raise HTTPException(status_code=400, detail="request body must be an object")
            sid = str(body.get("session_id") or "")
            self._check_id(sid)
            name = body.get("name") or ""
            device = body.get("device") or ""
            note_type = body.get("note_type") or ""
            for label, value in (("name", name), ("device", device), ("note_type", note_type)):
                if not isinstance(value, str) or len(value.strip()) > _MAX_NAME:
                    raise HTTPException(status_code=400, detail=f"{label} must be a string of {_MAX_NAME} characters or fewer")
            started_wall = body.get("started_wall")
            if started_wall is None:
                started_wall = _now()
            if isinstance(started_wall, bool) or not isinstance(started_wall, (int, float)):
                raise HTTPException(status_code=400, detail="started_wall must be a number")
            platform = str(body.get("platform") or "") or _platform_of(device, request.headers.get("user-agent", ""))

            with mc.session_lock(sid):
                existing = mc.read_state(self.store, sid)
                if existing is not None:
                    return JSONResponse(self._public(sid, existing), status_code=200)
                if self.store.session_exists(sid) or self.store.media_session_dir(sid).exists():
                    raise HTTPException(status_code=409, detail="a session with this id already exists")
                if len(self._unfinished_ids()) >= MAX_OPEN_SESSIONS:
                    raise HTTPException(status_code=507, detail="too many unfinished phone recordings on the server")
                st = {
                    "session_id": sid,
                    "state": mc.OPEN,
                    "created": _now(),
                    "name": name.strip(),
                    "people_count": store_mod.valid_people_count(body.get("people_count")),
                    "note_type": note_type.strip(),
                    "device": device.strip(),
                    "platform": platform,
                    "started_wall": float(started_wall),
                    "live": bool(body.get("live")),
                    "bytes": 0,
                    "runs": {},
                }
                self.store.ensure_media_session_dir(sid)
                self._save(sid, st)
                self._update_upload_meta_new(sid, st)
            return JSONResponse(self._public(sid, st), status_code=201)

        @r.put("/v1/mobile/sessions/{sid}/runs/{run}/chunks/{seq}", dependencies=dep)
        async def put_chunk(sid: str, run: int, seq: int, request: Request):
            mc.require_same_origin(request, self.store.root)
            self._check_id(sid)
            self._check_run_seq(run, seq)
            mime = _mime_base(request.headers.get("content-type", ""))
            if mime not in ALLOWED_MIMES:
                raise HTTPException(status_code=415, detail="unsupported audio type")
            st = self._load(sid)
            if st["state"] != mc.OPEN:
                raise HTTPException(status_code=409, detail=f"session is {st['state']}, not open")
            declared = request.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > MAX_CHUNK_BYTES:
                raise HTTPException(status_code=413, detail="chunk too large")

            others = self.staging_bytes(exclude=sid)
            base_bytes = int(st.get("bytes") or 0)
            run_dir = self._runs_dir(sid) / str(run)
            run_dir.mkdir(parents=True, exist_ok=True)
            tmp = run_dir / f".tmp-{uuid.uuid4().hex}"
            digest = hashlib.sha256()
            size = 0
            try:
                with open(tmp, "wb") as fh:
                    async for part in request.stream():
                        if not part:
                            continue
                        size += len(part)
                        if size > MAX_CHUNK_BYTES:
                            raise HTTPException(status_code=413, detail="chunk too large")
                        if base_bytes + size > MAX_SESSION_BYTES:
                            raise HTTPException(status_code=413, detail="recording exceeds the upload size limit")
                        if others + base_bytes + size > MAX_STAGING_BYTES:
                            raise HTTPException(status_code=507, detail="server staging space is full")
                        digest.update(part)
                        fh.write(part)
                if size == 0:
                    raise HTTPException(status_code=400, detail="empty chunk")
                hexd = digest.hexdigest()
                dest = self._chunk_path(sid, run, seq)
                with mc.session_lock(sid):
                    st = self._load(sid)
                    if st["state"] != mc.OPEN:
                        raise HTTPException(status_code=409, detail=f"session is {st['state']}, not open")
                    run_rec = st["runs"].get(str(run))
                    if run_rec and run_rec.get("mime") and run_rec["mime"] != mime:
                        raise HTTPException(status_code=409, detail="a run keeps one audio type")
                    if dest.exists():
                        prev = self._chunk_hash(dest)
                        if prev == hexd:
                            return Response(status_code=204)
                        raise HTTPException(status_code=409, detail="a different chunk is already stored here")
                    cur = int(st.get("bytes") or 0)
                    if cur + size > MAX_SESSION_BYTES:
                        raise HTTPException(status_code=413, detail="recording exceeds the upload size limit")
                    if self.staging_bytes(exclude=sid) + cur + size > MAX_STAGING_BYTES:
                        raise HTTPException(status_code=507, detail="server staging space is full")
                    dest.with_suffix(".sha").write_text(hexd, encoding="ascii")
                    os.replace(tmp, dest)
                    st["runs"].setdefault(str(run), {})["mime"] = mime
                    st["bytes"] = cur + size
                    self._save(sid, st)
                    self._update_upload_meta(sid, st)
                return Response(status_code=204)
            finally:
                try:
                    tmp.unlink()
                except OSError:
                    pass

        @r.get("/v1/mobile/sessions/{sid}", dependencies=dep)
        async def get_session(sid: str):
            self._check_id(sid)
            return self._public(sid, self._load(sid))

        @r.post("/v1/mobile/sessions/{sid}/finish", dependencies=dep)
        async def finish(sid: str, request: Request):
            mc.require_same_origin(request, self.store.root)
            self._check_id(sid)
            try:
                body = await request.json()
            except (json.JSONDecodeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail="invalid JSON body") from exc
            if not isinstance(body, dict):
                raise HTTPException(status_code=400, detail="request body must be an object")
            with mc.session_lock(sid):
                st = self._load(sid)
                if st["state"] in (mc.PREPARING, mc.FINALIZED):
                    return JSONResponse({"state": st["state"]}, status_code=202)
                if st["state"] == mc.FAILED:
                    raise HTTPException(status_code=409, detail="preparation failed; use retry")
                manifest = self._parse_manifest(body, st)
                recv = self._received(sid)
                missing = []
                for run in manifest:
                    have = set(recv.get(run["index"], []))
                    missing += [[run["index"], s] for s in range(run["chunk_count"]) if s not in have]
                if missing:
                    return JSONResponse({"detail": "chunks missing", "missing": missing}, status_code=409)
                for run in manifest:
                    rec = st["runs"].get(str(run["index"]), {})
                    if rec.get("mime"):
                        run["mime"] = rec["mime"]
                st["manifest"] = manifest
                if isinstance(body.get("name"), str) and body["name"].strip():
                    st["name"] = body["name"].strip()[:_MAX_NAME]
                if "people_count" in body:  # absent = keep; null/invalid = automatic
                    st["people_count"] = store_mod.valid_people_count(body.get("people_count"))
                if isinstance(body.get("note_type"), str) and body["note_type"].strip():
                    st["note_type"] = body["note_type"].strip()[:_MAX_NAME]
                st["state"] = mc.PREPARING
                st.pop("error", None)
                self._save(sid, st)
                mc.fence_live(sid)
                self._enqueue(sid)
            return JSONResponse({"state": mc.PREPARING}, status_code=202)

        @r.post("/v1/mobile/sessions/{sid}/retry", dependencies=dep)
        async def retry(sid: str, request: Request):
            mc.require_same_origin(request, self.store.root)
            self._check_id(sid)
            with mc.session_lock(sid):
                st = self._load(sid)
                if st["state"] in (mc.PREPARING, mc.FINALIZED):
                    return JSONResponse({"state": st["state"]}, status_code=202)
                if st["state"] != mc.FAILED or not st.get("manifest"):
                    raise HTTPException(status_code=409, detail="only a failed preparation can be retried")
                st["state"] = mc.PREPARING
                st.pop("error", None)
                self._save(sid, st)
                mc.fence_live(sid)
                self._enqueue(sid)
            return JSONResponse({"state": mc.PREPARING}, status_code=202)

        @r.delete("/v1/mobile/sessions/{sid}", dependencies=dep)
        async def delete(sid: str, request: Request):
            mc.require_same_origin(request, self.store.root)
            self._check_id(sid)
            with mc.session_lock(sid):
                st = self._load(sid)
                if st["state"] not in (mc.OPEN, mc.FAILED):
                    raise HTTPException(status_code=409, detail=f"session is {st['state']}")
                self._remove_session(sid)
            return Response(status_code=204)

    def _update_upload_meta_new(self, sid: str, st: dict) -> None:
        def upd(meta: dict) -> dict:
            meta = dict(meta or {})
            meta.setdefault("created", st["created"])
            meta.setdefault("tracks", {})
            for key in ("name", "device"):
                if st.get(key):
                    meta[key] = st[key]
            if st.get("people_count"):
                meta["people_count"] = st["people_count"]
            meta["source"] = mc.SOURCE
            meta["upload"] = {"state": "uploading", "percent": 0.0, "bytes_received": 0, "bytes_total": 0}
            return meta

        self.store.update_session_meta(sid, upd)

    @staticmethod
    def _chunk_hash(path: Path) -> str:
        sidecar = path.with_suffix(".sha")
        try:
            return sidecar.read_text(encoding="ascii").strip()
        except OSError:
            h = hashlib.sha256()
            with open(path, "rb") as fh:
                for block in iter(lambda: fh.read(1 << 20), b""):
                    h.update(block)
            return h.hexdigest()

    @staticmethod
    def _parse_manifest(body: dict, st: dict) -> List[dict]:
        runs = body.get("runs")
        if not isinstance(runs, list) or not runs:
            raise HTTPException(status_code=400, detail="manifest needs at least one run")
        if len(runs) > MAX_RUNS:
            raise HTTPException(status_code=400, detail=f"at most {MAX_RUNS} runs")
        out, seen = [], set()
        for item in runs:
            if not isinstance(item, dict):
                raise HTTPException(status_code=400, detail="invalid run entry")
            try:
                index = int(item["index"])
                count = int(item["chunk_count"])
                started_ms = float(item.get("started_ms") or 0)
            except (KeyError, TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail="invalid run entry") from exc
            if not 0 <= index < MAX_RUNS or index in seen:
                raise HTTPException(status_code=400, detail="invalid or repeated run index")
            if not 0 <= count <= MAX_CHUNKS_PER_RUN:
                raise HTTPException(status_code=400, detail="invalid chunk_count")
            state = str(item.get("state") or "complete")
            if state not in _RUN_STATES:
                raise HTTPException(status_code=400, detail="invalid run state")
            seen.add(index)
            out.append(
                {
                    "index": index,
                    "mime": _mime_base(str(item.get("mime") or "")),
                    "state": state,
                    "chunk_count": count,
                    "started_ms": started_ms,
                }
            )
        out.sort(key=lambda r: r["index"])
        return out

    def _remove_session(self, sid: str) -> None:
        """Delete a never-finalized session: the staging dir, and the session dirs when nothing else lives there."""
        shutil.rmtree(mc.mobile_dir(self.store, sid), ignore_errors=True)
        media = Path(self.store.media_session_dir(sid))
        # Live preview may have written a raw track for it; a deleted phone recording takes that with it.
        try:
            self.store.delete_session(sid)
        except Exception:  # noqa: BLE001
            shutil.rmtree(media, ignore_errors=True)
            shutil.rmtree(self.store.session_dir(sid), ignore_errors=True)
        self._track(sid, mc.FINALIZED)

    # -- worker ------------------------------------------------------------------------------------------------

    def _enqueue(self, sid: str) -> None:
        with self._reg_lock:
            if sid in self._queued:
                return
            self._queued.add(sid)
        self._queue.put(sid)

    def start(self) -> None:
        self._scan()
        for sid in self._unfinished_ids():
            st = mc.read_state(self.store, sid)
            if st and st["state"] == mc.PREPARING:
                self._enqueue(sid)
        self._thread = threading.Thread(target=self._run, name="mobile-prepare", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._queue.put(None)
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                sid = self._queue.get(timeout=min(60, SWEEP_INTERVAL_SECONDS))
            except queue.Empty:
                sid = ""
            if sid is None:
                break
            if sid:
                with self._reg_lock:
                    self._queued.discard(sid)
                try:
                    self.prepare(sid)
                except Exception:  # noqa: BLE001
                    logger.exception("mobile preparation crashed for %s", sid)
            if time.time() - self._last_sweep >= SWEEP_INTERVAL_SECONDS:
                self._last_sweep = time.time()
                try:
                    self.sweep_abandoned(time.time())
                except Exception:  # noqa: BLE001
                    logger.exception("mobile sweep failed")

    def sweep_abandoned(self, now: float) -> List[str]:
        """Delete unfinished sessions untouched for 14 days. Returns the removed ids."""
        removed = []
        for sid in self._unfinished_ids():
            with mc.session_lock(sid):
                st = mc.read_state(self.store, sid)
                if not st or st["state"] not in _UNFINISHED:
                    continue
                last = float(st.get("updated") or st.get("created") or now)
                if now - last < ABANDONED_AFTER_SECONDS or st["state"] == mc.PREPARING:
                    continue
                self._remove_session(sid)
                removed.append(sid)
        return removed

    # -- preparation -------------------------------------------------------------------------------------------

    def prepare(self, sid: str) -> None:
        with mc.session_lock(sid):
            st = mc.read_state(self.store, sid)
            if not st or st["state"] != mc.PREPARING or not st.get("manifest"):
                return
            manifest = st["manifest"]
        work = mc.mobile_dir(self.store, sid) / "work"
        try:
            result = self._build(sid, st, manifest, work)
            # Publishing writes a WAV of ~115 MB per hour: never under the session lock, which the request handlers
            # take on the event loop. Nothing else changes a PREPARING session (DELETE is refused, finish/retry only
            # read it), so the state is re-checked afterwards instead.
            job_id = self._publish(sid, self._load(sid), result)
            with mc.session_lock(sid):
                st = self._load(sid)
                if st.get("state") != mc.PREPARING:
                    logger.warning("mobile %s left preparing during publish (%s)", sid, st.get("state"))
                st["job_id"] = job_id
                st["state"] = mc.FINALIZED
                st.pop("error", None)
                self._save(sid, st)
            shutil.rmtree(work, ignore_errors=True)
            shutil.rmtree(self._runs_dir(sid), ignore_errors=True)
        except Exception as exc:  # noqa: BLE001
            logger.exception("mobile preparation failed for %s", sid)
            msg = (str(exc) or exc.__class__.__name__)[:300]
            try:
                with mc.session_lock(sid):
                    st = self._load(sid)
                    st["state"] = mc.FAILED
                    st["error"] = msg
                    self._save(sid, st)
            except Exception:  # noqa: BLE001
                logger.exception("could not record the failure of %s", sid)
            shutil.rmtree(work, ignore_errors=True)

    @staticmethod
    def _ffmpeg() -> str:
        found = shutil.which("ffmpeg")
        if not found:
            raise RuntimeError("ffmpeg is required to prepare phone recordings")
        return found

    def _decode(self, joined: Path, pcm: Path, *, tolerant: bool) -> int:
        """ffmpeg ``joined`` to raw 16 kHz mono s16le at ``pcm``. Returns frames decoded."""
        cmd = [self._ffmpeg(), "-nostdin", "-v", "error"]
        if tolerant:
            cmd += ["-err_detect", "ignore_err"]
        cmd += [
            "-i", str(joined), "-t", str(MAX_RUN_SECONDS), "-vn", "-ac", "1", "-ar", str(STREAM_SAMPLE_RATE),
            "-c:a", "pcm_s16le", "-f", "s16le", "-y", str(pcm),
        ]  # fmt: skip
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=DECODE_TIMEOUT_SECONDS, check=False)
        except subprocess.TimeoutExpired as exc:
            raise ValueError("decoding timed out") from exc
        frames = pcm.stat().st_size // 2 if pcm.exists() else 0
        if proc.returncode != 0 and not (tolerant and frames > 0):
            tail = (proc.stderr or b"").decode("utf-8", "replace").strip()[-300:]
            raise ValueError(f"could not decode: {tail}" if tail else "could not decode")
        return frames

    def _build(self, sid: str, st: dict, manifest: List[dict], work: Path) -> dict:
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True, exist_ok=True)
        rate = STREAM_SAMPLE_RATE
        decoded = []  # (run, pcm_path, frames)
        lost: List[int] = []
        for run in manifest:
            idx = run["index"]
            mime = run.get("mime") or st["runs"].get(str(idx), {}).get("mime") or "audio/webm"
            joined = work / f"run-{idx}.{_ext_for(mime)}"
            with open(joined, "wb") as out:
                for seq in range(run["chunk_count"]):
                    src = self._chunk_path(sid, idx, seq)
                    if not src.exists():
                        raise ValueError(f"chunk {idx}/{seq} is missing")
                    with open(src, "rb") as fh:
                        shutil.copyfileobj(fh, out, 1 << 20)
            pcm = work / f"run-{idx}.pcm"
            partial = run["state"] in _PARTIAL_RUN_STATES
            frames = 0
            try:
                frames = self._decode(joined, pcm, tolerant=partial)
            except (ValueError, OSError) as exc:
                if not partial:
                    # A run the phone called complete but that will not decode cleanly: keep what decodes rather
                    # than failing the whole meeting for good (the manifest is frozen, so Retry would fail the same way).
                    logger.warning("mobile %s run %s did not decode cleanly (%s); decoding what it can", sid, idx, exc)
                    try:
                        frames = self._decode(joined, pcm, tolerant=True)
                    except (ValueError, OSError):
                        frames = 0
            try:
                joined.unlink()
            except OSError:
                pass
            if frames <= 0:
                lost.append(idx)
                continue
            decoded.append((run, pcm, frames))
        if not decoded:
            raise ValueError("no usable audio in the recording")

        raw_path = work / "system.raw"
        run_info, gaps = [], []
        log: List[dict] = []
        frames_total = 0
        t_clock = 0.0  # seconds of (real) meeting time at the current write position
        # The meeting starts at the first run the phone recorded, decoded or not: when it was lost, the first
        # decoded run is placed after a leading gap so wall-clock times stay right.
        first_ms = min(float(r["started_ms"]) for r in manifest)
        with open(raw_path, "wb") as out:
            for n, (run, pcm, frames) in enumerate(decoded):
                dur = frames / rate
                pad = 0
                lead = n == 0 and run["started_ms"] > first_ms
                if n or lead:
                    if n:
                        prev_run, _, prev_frames = decoded[n - 1]
                        delta = (run["started_ms"] - prev_run["started_ms"]) / 1000.0 - prev_frames / rate
                    else:
                        delta = (run["started_ms"] - first_ms) / 1000.0
                    gap = max(0.0, delta)
                    if lead:
                        log.append(
                            {"event": "open", "segment": 0, "frames": 0, "t": 0.0, "wall": st["started_wall"],
                             "samplerate": rate, "channels": 1, "device": st.get("device") or "phone"}
                        )  # fmt: skip
                    capped = gap > GAP_CAP_SECONDS
                    silence = GAP_CAPPED_SECONDS if capped else gap
                    pad = int(round(silence * rate))
                    if pad:
                        out.write(b"\0" * (pad * 2))
                    if gap >= GAP_LOG_MIN_SECONDS:
                        gaps.append(
                            {"before_run": run["index"], "seconds": round(gap, 3), "padded_seconds": round(pad / rate, 3), "capped": capped}
                        )
                        log.append(
                            {"event": "gap", "segment": 0, "frames_before": frames_total, "frames_padded": pad,
                             "seconds_lost": round(gap, 3), "reason": "phone-gap", "t": round(t_clock, 6)}
                        )  # fmt: skip
                    frames_total += pad
                    t_clock += gap
                start_frame = frames_total
                if n == 0 and not lead:
                    log.append(
                        {"event": "open", "segment": 0, "frames": 0, "t": 0.0, "wall": st["started_wall"],
                         "samplerate": rate, "channels": 1, "device": st.get("device") or "phone"}
                    )  # fmt: skip
                else:
                    log.append({"event": "progress", "segment": 0, "frames": start_frame, "t": round(t_clock, 6)})
                with open(pcm, "rb") as fh:
                    shutil.copyfileobj(fh, out, 1 << 20)
                frames_total += frames
                t_clock += dur
                log.append({"event": "progress", "segment": 0, "frames": frames_total, "t": round(t_clock, 6)})
                run_info.append(
                    {"index": run["index"], "mime": run.get("mime") or "", "state": run["state"],
                     "chunk_count": run["chunk_count"], "started_ms": run["started_ms"],
                     "start_seconds": round(start_frame / rate, 3), "duration_seconds": round(dur, 3)}
                )  # fmt: skip
                try:
                    pcm.unlink()
                except OSError:
                    pass
        log.append({"event": "close", "segment": 0, "frames": frames_total, "t": round(t_clock, 6)})
        return {"raw": raw_path, "frames": frames_total, "runs": run_info, "gaps": gaps, "lost": lost, "log": log}

    def _publish(self, sid: str, st: dict, res: dict) -> str:
        store = self.store
        wav_path = store.track_wav_path(sid, mc.TRACK)
        tmp_wav = wav_path.with_name(wav_path.name + ".tmp")
        wav_io.wrap_raw_as_wav(res["raw"], tmp_wav, STREAM_SAMPLE_RATE)
        timing_path = store.track_timing_path(sid, mc.TRACK)
        timing_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_log = timing_path.with_name(timing_path.name + ".tmp")
        with open(tmp_log, "w", encoding="utf-8") as fh:
            for entry in res["log"]:
                fh.write(json.dumps(entry, separators=(",", ":")) + "\n")
        os.replace(tmp_log, timing_path)
        os.replace(tmp_wav, wav_path)
        # A live transcript wrote its (best-effort) audio to system.raw; the final WAV replaces it.
        for leftover in (store.track_raw_path(sid, mc.TRACK), store._ranges_path(sid, mc.TRACK)):
            try:
                leftover.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                logger.warning("could not remove %s", leftover, exc_info=True)

        received = int(st.get("bytes") or 0)
        mobile = {"runs": res["runs"], "gaps": res["gaps"], "lost_runs": res["lost"]}

        def upd(meta: dict) -> dict:
            meta = dict(meta or {})
            meta.setdefault("created", st.get("created") or _now())
            if st.get("name") and not meta.get("name_updated_at"):
                meta["name"] = st["name"]
            for key in ("device", "platform", "note_type"):
                if st.get(key):
                    meta[key] = st[key]
            if st.get("people_count") and not meta.get("people_count_updated_at"):
                meta["people_count"] = st["people_count"]
            meta["source"] = mc.SOURCE
            meta["in_person"] = True
            meta["started_wall"] = st.get("started_wall")
            tracks = dict(meta.get("tracks") or {})
            tracks[mc.TRACK] = dict(tracks.get(mc.TRACK) or {}) | {"source": mc.SOURCE}
            meta["tracks"] = tracks
            meta["mobile"] = mobile
            meta["upload"] = {"state": "complete", "percent": 100.0, "bytes_received": received, "bytes_total": received}
            return meta

        store.update_session_meta(sid, upd)
        return self.job_queue.ensure_job(sid)


def install_mobile_upload(app: FastAPI, *, store, job_queue, data_root=None) -> MobileUpload:
    """Register the ``/v1/mobile/sessions`` routes on ``app``, start the preparation worker and re-queue any
    session left in ``preparing`` by a restart. Returns the handle (``.stop()`` ends the worker)."""
    mobile = MobileUpload(store=store, job_queue=job_queue, data_root=data_root)
    app.include_router(mobile.router)
    mobile.start()
    return mobile
