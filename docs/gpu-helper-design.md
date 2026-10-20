# GPU helper and GPU server: design contract

Developer contract for the GPU helper (a Windows PC with an NVIDIA GPU that does transcription work for the
server) and the GPU server Docker option. User-facing docs are in `docs/gpu-helper.md`. Everything here is off
by default; with it off, behavior is unchanged.

## Terms

- **Helper**: a recording client on a Windows PC with an NVIDIA GPU that has the GPU helper turned on. It runs a
  separate **helper runtime** process (Python + CUDA libraries + models) supervised by the client.
- **Helper pack**: the files a helper needs (Python, wheels, models, helper code), prepared by the server and
  downloaded only from the server.
- **Offer / lease / attempt**: a transcription job offered to helpers; a helper's exclusive, time-limited claim
  on it; each claim is a new attempt with its own id and secret.

## Server settings (`server/settings.py`)

| key | type | default | meaning |
|---|---|---|---|
| `compute_offload_enabled` | bool | `false` | offer transcription jobs to connected helpers |
| `compute_claim_timeout_seconds` | int 5..300 | `30` | how long an offer waits with no usable helper online before running locally |
| `diarization_without_helper` | `"run_here"` / `"skip"` | `"run_here"` | speaker labels when the job runs on the server and `diarization_enabled` is on |

`diarization_enabled` keeps its meaning ("produce speaker labels"). A helper diarizes when the job's captured
settings say labels are on and the pack contains the pyannote pipeline.

## Job pipeline (`server/jobs.py`)

`_process` is split into three functions; behavior of the local path is unchanged:

1. `_prepare(job)`: read meta, list tracks, wrap raw-only tracks into WAVs, compute weights. Returns a
   `Prepared(session_id, tracks, wav_paths, weights, meta, settings, is_new_meeting)`.
2. Transcription, either `_transcribe_local(prepared, job_id)` (today's per-track loop) or a helper result.
3. `_finalize(job_id, prepared, track_segments)`: merge, render, write transcript, DONE, auto notes,
   immediate retention. Shared by both paths.

Diarization errors (factory or `diarize()` raising) are caught in the local path: the track keeps unlabeled
"Them", a warning is logged, and `job["warnings"]` gets `"speaker labels failed: <error>"`.

### Choosing where a job runs

When the worker thread takes a job:

- New jobs go through a dispatcher thread (`meeting-notes-offers`), never the local worker, so a long local run
  never holds back work a helper could take. If `compute_offload_enabled` and `HelperRegistry.any_online()` (a
  helper polled within 45 s, idle **or busy**, not paused,
  not busy, runtime ready), the worker runs `_prepare`, captures the job's effective settings into
  `job["compute"]["settings"]` (whisper model, beam size, hotwords if any, diarization on/off, model, min/max
  speakers), sets `job["compute"] = {"status": "offered", "offered_at": t, "offer_expires": t + timeout, ...}`,
  keeps `state = running`, progress 0, and **moves on** without waiting.
- Otherwise it runs locally as today.

`job["compute"]["status"]` values: `offered` -> `leased` -> (`done` | `local`). `local` means the server
took it back; the reason is in `job["compute"]["fallback_reason"]` (`no claim`, `heartbeat lost`,
`over budget`, `helper failed: ...`, `restart`).

### Sweeper

A `ComputeSweeper` thread (every 2 s) and every compute request check:

- `offered` with no usable helper online for `compute_claim_timeout_seconds` -> `local` (`no claim`); offered for
  more than `OFFER_MAX_WAIT_SECONDS` (15 min) -> `local` (`waited too long for a helper`). Offers are claimed
  oldest first, so several meetings at once queue for one helper. Either way the job goes to the local queue
  with `force_local=True`.
- `leased` with `last_heartbeat` older than 60 s -> revoke, `local` (`heartbeat lost`).
- `leased` past `lease_deadline` -> revoke, `local` (`over budget`). Budget = 120 s + 0.25 x total audio seconds.

The local worker, when taking a job whose `compute.status` is `local`, runs `_transcribe_local` (respecting
`diarization_without_helper`) and `_finalize`. A job is never offered twice.

### Atomic transitions (`server/store.py`)

`Store.transition_job(job_id, expect: dict, **fields) -> Optional[dict]` updates a job only if every
key/value in `expect` matches (dotted keys allowed for `compute.*`), under the store's job lock; returns the
new record or `None`. All compute transitions use it: claim (`offered` -> `leased` with a new attempt),
heartbeat (attempt match), complete (`leased` + attempt -> `done`), release/expiry (`leased`|`offered` ->
`local`). The lease secret is stored only as SHA-256.

### Restart and deletion

- `resume_interrupted`: jobs with `compute.status` in (`offered`, `leased`) become `local` (`restart`), are
  re-queued, and their attempt is revoked.
- A helper request for a job whose session is trashed or deleted gets 410; the job is failed "meeting deleted"
  as today.
- `ensure_job` treats `offered`/`leased` jobs as active (no duplicate jobs).

## Helper registry and keys (`server/compute.py`)

- Durable helpers in `<data>/compute/helpers.json`: `{helper_id, name, created, key_hash, key_prefix,
  revoked}`. Keys are `mnh_` + `token_urlsafe(24)`, stored as SHA-256 (same scheme as agent keys).
- In-memory presence per helper: `last_seen, version, runtime_version, gpu {name, vram_mb, driver},
  state (idle|busy|paused|installing|error), current_job, progress, last_error, pack_version`.
- Helper keys authorize **only** `/v1/compute/*` helper endpoints below. The main server token never goes to the
  runtime process.

## HTTP API

All bodies JSON. Helper endpoints use `Authorization: Bearer mnh_...`.

### Registration (main server token, from the recording client)

- `POST /v1/compute/helpers` `{name, instance_id}` -> `201 {helper_id, key}` (key returned once). Same
  `instance_id` again replaces the old key (old one revoked).
- `GET /v1/compute/helpers` (web/main token) -> list with presence.
- `DELETE /v1/compute/helpers/{helper_id}` (web/main token) -> revoke.

### Helper pack (helper key or main token)

- `GET /v1/compute/pack/manifest` -> `200 {pack_version, created, python, files: [{path, size, sha256, kind}],
  requirements, models: {whisper: <name>, diarization: <name>|null}, total_size}` or `404 {"error": "no pack"}`.
- `GET /v1/compute/pack/files/{path}` -> file bytes; supports `Range` for resume.

### Work (helper key)

- `POST /v1/compute/poll` `{version, runtime_version, pack_version, gpu, state, paused}` -> presence update.
  Returns `204` (no work) or `200` with a lease:
  ```json
  {"job_id": "...", "attempt": "a1b2...", "lease": "<secret>", "session_id": "...", "board": "M-0142",
   "tracks": [{"name": "mic", "seconds": 1312.5, "url": "/v1/compute/jobs/<job>/audio/mic"}],
   "whisper": {"model": "small.en", "beam_size": 5, "hotwords": null, "language": null},
   "diarization": {"enabled": true, "model": "pyannote/speaker-diarization-community-1",
                   "min_speakers": 1, "max_speakers": 8},
   "heartbeat_seconds": 15, "deadline_seconds": 448}
  ```
  A helper only gets work when `state` is idle, not paused, and its `pack_version` equals the server's (else 204
  with header `X-Pack-Outdated: 1`).
- `GET /v1/compute/jobs/{job_id}/audio/{track}` with headers `X-Attempt`, `X-Lease` -> 16 kHz mono int16 WAV.
  `409` if the attempt is no longer active.
- `POST /v1/compute/jobs/{job_id}/heartbeat` `{attempt, lease, progress (0..1), step}` -> `200` or `409`
  (revoked; helper must stop and discard).
- `POST /v1/compute/jobs/{job_id}/complete` `{attempt, lease, device, timings, tracks: {"mic": [{start, end,
  text}], "system": [...]}, speaker_turns: [{start, end, speaker}] | null}` -> `200` / `409` / `422`
  (validation). A repeated completion of the already accepted attempt returns `200` again.
- `POST /v1/compute/jobs/{job_id}/release` `{attempt, lease, error}` -> `200`; job goes `local`.

### Result validation

Track names must equal the offered tracks; times finite, `0 <= start <= end <= track_seconds + 2`; segments
sorted by start; at most 20 000 segments per track; text <= 4 000 chars per segment; body <= 20 MB;
`speaker_turns` only when diarization was requested. Valid results become `Segment(start, end, text, track)`;
speaker turns go through `assign_speakers` on the server, then `_finalize`.

### Progress shown to users

Job progress follows heartbeats. The job record keeps `compute.helper_name` and `compute.device`; the meeting
page and Transcriptions list show "Transcribing on <helper> (GPU) 42%" and, after a fallback, "Transcribed on
this server (GPU helper: <reason>)".

## Helper pack (`server/helper_pack.py`)

Prepared on demand from Settings (or `POST /v1/compute/pack/prepare`, main token; status at
`GET /v1/compute/pack/status`). The server downloads from the internet once; helpers download only from the
server. Output in `<data>/helper-pack/<pack_version>/` plus `current` pointer:

- `uv.exe` (pinned version, verified SHA-256 from a constant in the code);
- standalone CPython 3.12 for Windows x86_64 (python-build-standalone, pinned, verified SHA-256);
- `wheels/`: `pip download --only-binary=:all: --platform win_amd64 --python-version 3.12` of
  `meeting_notes/compute_helper/requirements-windows-cuda.txt` (exact pins; torch from the PyTorch CUDA index);
- `helper-code.zip`: the pure-Python `meeting_notes` package files the runtime needs, from the installed
  package;
- `models/whisper/<name>/`: the server's configured faster-whisper model files;
- `models/diarization/`: the pyannote pipeline snapshot, fetched with the server's own `HUGGINGFACE_TOKEN`
  (omitted, and diarization unavailable on helpers, when the server has no token);
- `manifest.json` (see API). `pack_version` = hash of pins + model names + meeting_notes version.

Preparation runs in a background thread, reports progress, and keeps the previous pack until the new one is
complete. It must be re-run when the Whisper model or the pins change; Settings shows "Out of date".

## Helper runtime (`meeting_notes/compute_helper/`)

Pure Python, runs inside the helper pack's Python with `helper-code.zip` on `sys.path`:

- `python -m meeting_notes.compute_helper run --server <url> --key-file <path> --models <dir>` loops: poll every
  5 s; on a lease, download audio, transcribe each track with faster-whisper on CUDA (`compute_type`
  `float16`, falling back to `int8_float32` on unsupported GPUs), diarize the system track with pyannote on
  CUDA when asked, heartbeat every 15 s from a separate thread (also during model loading), post complete or
  release. Models load from the pack's `models/` with `local_files_only`.
- Talks to the supervising client with stdout JSON lines out and a control file in (`--control-file`,
  `{"paused", "stop"}`, checked twice a second; `--parent-pid` stops it when the client exits). Never stdin: on
  Windows a thread blocked reading an inherited stdin pipe stalls faster-whisper. Outputs `{"event": "status", state, job,
  progress, gpu, error}`; accepts `{"cmd": "pause"}`, `{"cmd": "resume"}`, `{"cmd": "stop"}`.
- `python -m meeting_notes.compute_helper self-test --models <dir>`: verifies torch CUDA and CTranslate2 CUDA
  by transcribing a bundled 3-second clip on the GPU; prints a JSON result.
- `python -m meeting_notes.compute_helper gpu-info`: GPU name, VRAM, driver (via `nvidia-smi`).

## Recording client (Windows)

Config (`~/.meeting-notes/config.json`), new group `gpu_helper`: `enabled` (false), `start_at_login` (true),
`helper_id`, `key` (secret), `installed_pack_version`.

- Settings page **GPU helper** (`settings_schema.py` + `settings_dialog.py`, schema-synced; remote settings show
  the switch and status but not Install/Remove): switch, GPU detected/not detected (from `nvidia-smi`), runtime
  status and Install / Update / Remove with progress, Start at login, status (idle, working on M-0142 42%,
  paused while recording, offline, error). Non-Windows: "GPU helper is available on Windows PCs with an NVIDIA
  graphics card."
- Installer (`client/gpu_runtime.py`): register (if no key) -> manifest -> download to
  `%LOCALAPPDATA%\MeetingNotes-GPU\staging\` (resume with Range, verify SHA-256) -> extract Python ->
  `uv venv` + `uv pip install --no-index --find-links wheels -r requirements.txt` -> copy models and code ->
  `self-test` -> swap into `MeetingNotes-GPU\current\` (previous kept until success) -> record pack version.
- Supervisor (`client/gpu_helper.py`): starts the runtime with `QProcess` when enabled and installed, restarts
  it on crash with backoff (5 s, 30 s, 2 min), sends `pause` while this client records and `resume` after,
  `stop` on Quit or disable.
- Tray (`client/ui/tray.py`): shown while the helper is enabled. Menu: status line, Open Meeting Notes, Pause /
  Resume GPU helper, Quit. Closing the window hides it to the tray while the helper is enabled; Quit stops
  recording (with the existing confirmation), the helper, and the app.
- Start at login (`client/autostart.py`): `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\MeetingNotes` =
  `"<abs path to MeetingNotes.exe>" --tray`, set when the helper is enabled and Start at login is on, removed
  otherwise and by the uninstaller. `--tray` starts hidden in the tray.
- Single instance (`client/single_instance.py`): `QLocalServer` named per user; a second launch sends
  "activate" and exits (with `--tray` it just exits).
- UI matches the web UI (tokens from `client/ui/theme.py`, same wording as the server's GPU helpers block).

## Web UI

- Settings > Transcription > **GPU helpers**: Use GPU helpers (switch), wait for a helper (seconds), speaker
  labels (on/off, model, min/max speakers — the previously hidden block), speaker labels without a helper
  (run here / skip), helper pack (status, size, version, Prepare / Refresh, progress, "Out of date"), connected
  helpers table (name, GPU, status, last seen, Revoke).
- Recorders page: a GPU line on each recorder that is a helper (GPU name, idle / working on M-0142 42% /
  paused / offline).
- Meeting page and Transcriptions list: where the transcript ran, fallback reason.

## GPU server Docker option

- `docker/Dockerfile`: build arg `INSTALL_GPU=false`; when true, installs `nvidia-cublas-cu12` and
  `nvidia-cudnn-cu12` (9.x) wheels and sets `LD_LIBRARY_PATH` to their library dirs; diarization's torch
  already brings CUDA.
- `docker/docker-compose.yml`: `MEETING_NOTES_DEVICE=${MEETING_NOTES_DEVICE:-cpu}` (was hard-coded).
- `docker/docker-compose.gpu.yml` (override): GPU device reservation (`driver: nvidia, count: all,
  capabilities: [gpu]`), `INSTALL_GPU: "true"`, `MEETING_NOTES_DEVICE=cuda`,
  `MEETING_NOTES_DIARIZATION_DEVICE=cuda`. Used as `docker compose -f docker-compose.yml -f
  docker-compose.gpu.yml up -d`.
- Startup check: when a device is `cuda` but CUDA is not usable, log a clear warning and fall back to CPU for
  that backend (`/health` reports `transcription_device` and `diarization_device` actually in use).
