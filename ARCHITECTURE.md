# Architecture

Two pieces, split so the machine in the meeting does as little work as possible.

```
  Laptop in the meeting                    Server on the LAN (Docker)
 ┌───────────────────────────┐            ┌────────────────────────────┐
 │  meeting-notes-ui  (Qt)   │            │  FastAPI + faster-whisper  │
 │                           │            │                            │
 │  mic ──┐                  │  16kHz     │   live pass  ──► partials  │
 │        ├─► recorder ──────┼──ws────────┼─► (fast model, disposable) │
 │  sys ──┘      │           │  PCM       │                            │
 │               ▼           │            │                            │
 │        48kHz WAV on disk  │  upload    │   final pass ──► transcript│
 │        (the real artifact)├──http──────┼─► (any model, authoritative)│
 │               │           │            │                            │
 │        upload queue ◄─────┘            └────────────────────────────┘
 │        (retries forever)  │
 └───────────────────────────┘
```

## Why it is split this way

**The client stays light.** It needs `soundcard`, `numpy`, PySide6 and an HTTP
client. No Whisper, no model files, no ML stack on the laptop. Installing on a
second machine costs nothing but the UI.

**One machine does the compute.** Models are downloaded and cached once, on the
server, instead of on every laptop.

## The two passes

A CPU-only server cannot run a large model against two live streams and keep up
with realtime, so there are deliberately two different jobs:

| | Live pass | Final pass |
|---|---|---|
| Input | streamed 16kHz PCM | the complete recording, uploaded |
| Model | fast (`base.en`) | whatever you configure |
| Purpose | see it working, read along | the transcript you keep |
| If it fails | nothing is lost | retried until it succeeds |

The live preview is explicitly disposable. That is what makes a dropped wifi
connection a non-event: the laptop keeps recording to disk regardless, and the
authoritative transcript is produced afterwards from that complete local file.

## Why everything on the wire is 16kHz mono int16

It is Whisper's native input, so sending it costs nothing in transcription
quality — the model resamples to 16kHz internally anyway. It is 32 KB/s per
track, which is nothing on a LAN. And it needs no audio codec on the client, so
there is no FLAC or Opus dependency to install or to go wrong.

The full-quality 48kHz recording never leaves the machine that made it. It stays
local as the archive copy.

## Resumable streaming

Every binary frame carries its absolute frame offset, and the server
acknowledges the highest contiguous offset it has stored per track. After a
reconnect the client resumes from that offset. Duplicate or out-of-order frames
are therefore idempotent: writing the same offset twice is a no-op rather than
corruption. This is why a brief network drop does not even disturb the live
view, let alone the recording.

The protocol lives in one file, `meeting_notes/wire.py`, imported by both sides
so it cannot drift.

## The offline queue

Finished recordings are queued unconditionally, even when the server is
reachable at that moment. A background worker uploads, waits for the job, and
writes `transcript.md` / `transcript.json` back into the session directory. If
the server is down the entry simply stays pending and is retried, including
across restarts of the app.

This is the mechanism behind "record anyway": there is no state in which
starting a meeting depends on the server being up.

The worker runs for as long as the app is open, not just after a recording.
Tying it to "just finished recording" would mean a backlog only cleared if you
happened to record again — so a meeting captured on a plane would sit unqueued
until the next meeting, which is the wrong dependency. Opening the app is
enough.

The queue lives in `<save folder>/.upload-queue`, deliberately beside the
recordings rather than among them: its JSON state files should not appear in
the folder the user browses for their meetings.

## Layout

```
meeting_notes/
  wire.py              protocol shared by both sides
  audio/               capture (client side); screencapture_source.py = macOS
                       system audio (ScreenCaptureKit)
  wav_io.py timing.py  recording format and clock alignment (shared)
  transcribe/          Whisper backend and transcript merge (server side)
  client/
    controller.py      bridges recorder, stream and queue
    resample.py        48kHz -> 16kHz with anti-aliasing
    streamer.py        websocket with reconnect and resume
    queue.py           offline upload queue
    api.py             HTTP client
    meeting_detect.py  call detection: shared logic + Windows probes
    meeting_detect_mac.py  macOS probes (CoreAudio, NSWorkspace, CGWindowList)
    update.py          verified self-update (PowerShell on Windows, bash on macOS)
    ui/                Qt window, waveform, settings
  server/
    app.py store.py jobs.py live.py auth.py
    index.py           SQLite index behind the web UI / JSON API
    settings.py        persisted server settings (model, retention, ...)
    retention.py        the audio-retention sweep and its worker thread
    web.py             HTML rendering for the browser UI
    agent/             agent access: per-agent API keys, REST /api/v1, MCP at /mcp
    client_logs.py     diagnostic zips uploaded by the client (/v1/client-logs)
    mac_installer.py   the /install/mac.sh script (curl | bash macOS installer)
    compat.py          client release window, version header, recorder registry, 426
    splitmerge.py      split / combine meetings, suggestions, AI topic-shift queue
    splitmerge_api.py  its routes (web token only); splitmerge_ui.py its dialogs
docker/                Dockerfile, compose
```

## The web UI, the session index, and settings

The server also serves the product's primary management UI from the same
FastAPI app and process as the recorder client's API. A persistent sidebar
links to a home dashboard, meetings, and settings. Home combines
currently connected live sessions with recent history; meetings
uses a searchable table and opens each recording in a full-screen transcript
and audio-player overlay. Re-transcription, audio-only deletion, and complete
session deletion all call the JSON API. A fixed Install button downloads a
Windows agent installer preconfigured with the server address stored in
settings. There is no new port, template dependency, or frontend build step:
``server/web.py`` emits the HTML/CSS and small vanilla-JS clients directly,
escaping all server-rendered values with ``html.escape``.

**Why JSON-first, not server-rendered pages.** A deployment of this server is
expected to accumulate sessions for as long as it runs -- months or years of
meetings, not a handful for a demo. Rendering the session list by walking
``sessions/*/session.json`` on every page load gets slower with every meeting
ever recorded, forever. So the actual listing/search/pagination logic lives
in one place -- ``GET /v1/sessions`` -- and both the web UI and any future
client (the Qt app, eventually, per its own history view) go through it. The
HTML pages don't re-implement that logic; they're shells that call it.

Live websocket connections publish a small, process-local status snapshot at
``GET /v1/live``. It contains only connection metadata and recent partial
transcription text; recordings and durable results continue to use the normal
session store and index. The generated ``/install/client-agent.ps1`` script
downloads the release artifact, writes the agent's local server URL, creates a
shortcut, and launches it. It deliberately does not embed the shared API token.

**Multiple clients.** Each recorder creates a globally distinct session ID
from its timestamp, host name, and random suffix. The server accepts concurrent
websockets and isolates storage and preview buffers by ``(session_id, track)``;
it rejects a second live connection that presents an already-active ID rather
than allowing two streams to write the same files. Live model inference is
serialized deliberately on the CPU while network ingestion continues, and
completed recordings enter a FIFO final-transcription queue. Thus multiple
meetings can record safely at once even when their previews or final results
must wait briefly for the single configured model.

**The index (``server/index.py``).** Backing ``/v1/sessions`` is a small
SQLite database at ``<data_root>/index.sqlite`` (WAL mode, for concurrent
readers/writers across the request threadpool, the job worker, and the
retention worker) with two tables: ``sessions`` (one row per session --
name, created, duration, audio presence/size, and the latest job's
id/state/progress/error) and ``transcript_text`` (full-text search over
transcripts, via SQLite's FTS5 extension when available, falling back to a
plain table searched with ``LIKE`` when it isn't). This is a read
optimization, not a second source of truth: every column is derived from
files already on disk, so ``Store.reindex()`` can always rebuild it from
scratch. That happens automatically once, at startup, if the index file is
missing (a fresh data root, or an upgrade from before the index existed), and
is reachable on demand via ``POST /v1/reindex`` (also a button on the
settings page) for an operator who wants to force a rebuild -- e.g. after
restoring the data volume from a backup that didn't include it. Every write
that changes a session's state (a transcript saved, a job's progress ticking,
audio deleted by retention) updates the index incrementally and in place, so
reads never have to fall back to a directory walk.

**Settings (``server/settings.py``).** Persisted at
``<data_root>/settings.json``: the public server address, transcription model
and beam size, optional diarization controls, the note templates (``ai_workflow`` is the
Standard template's prompt; ``note_templates`` holds the other built-ins and user note types, and
each review records the ``template_id``/``template_name`` it was queued with), and the audio
retention policy
(below). ``MEETING_NOTES_MODEL`` (and the
other ``MEETING_NOTES_*`` env vars) remain the *bootstrap* defaults for a
fresh install -- what ``settings.json`` is seeded from the first time it's
read with no file present -- but once an operator saves settings through the
settings page or ``PUT /v1/settings``, the file wins from then on, even
across a restart where the env var reasserts its original value. The
transcriber factory (`app.py`) reads the model and beam size from settings at
the moment it's called, not once at server start, so a change takes effect on
the very next job with no restart; the one exception is the live preview's
cached model instance (see ``live.py``'s own module docstring for why it
caches at all), which is why saving settings also calls
``live_preview.reset_transcriber()``.

## Optional remote-speaker diarization

When enabled, the job queue runs pyannote Community-1 over the mixed system
track and assigns its time ranges to the existing Whisper segments by greatest
overlap. Stable display labels (``Them 1``, ``Them 2``, ...) are applied before
the microphone and system transcripts are merged. The backend is lazy and the
dependency is an optional extra, so the normal install does not import or
install PyTorch. Docker includes it only when ``INSTALL_DIARIZATION=true`` was
set at build time, while runtime activation, model name, speaker limits, and
Hugging Face token are separate settings.

## Audio retention

Meeting *transcripts* are kept forever; the raw *audio* behind them can be
deleted automatically once it's no longer needed, per
``audio_retention_days`` in settings: ``-1`` keeps it forever (the default),
``0`` deletes it as soon as the transcript finishes, and any other N deletes
it N days after the session was recorded. A companion setting,
``delete_audio_only_after_success`` (on by default), makes sure a job that
failed never has its only copy of the source audio deleted out from under
it -- so a failed transcription can always be retried.

``server/retention.py`` implements this as a pure rule
(``should_delete_audio``, easy to unit test against plain dicts) plus a
sweep (``apply_retention``) and a daemon thread (``RetentionWorker``, started
and stopped the same way as ``JobQueue``) that runs the sweep on a timer --
every ``retention_check_interval_minutes``, and once shortly after startup so
a session that aged out while the server was down doesn't wait up to a full
interval to be noticed. The one case the periodic sweep is too slow for is
0-day retention ("as soon as the transcript is done" means *now*, not
"within the next hour"), so ``JobQueue`` also checks the policy immediately
for a session right after its job finishes successfully. Retention only ever
removes audio (``.wav``/``.raw``/the ranges sidecar); ``session.json`` and
every transcript are untouched, so a session with its audio deleted still
shows up in the session list, still has a transcript, and just can't be
re-transcribed anymore (no audio left to re-transcribe from).

## Recently deleted (soft delete)

``DELETE /v1/sessions/{id}``, the web delete route and bulk delete never remove a meeting; ``Store.trash_session``
*moves* everything that belongs to it, keyed by session id:

    <data>/trash/sessions/<id>/session/     the session dir (session.json, timing logs)
    <data>/trash/sessions/<id>/jobs/        its job + transcript files
    <data>/trash/sessions/<id>/reviews/     its meeting-notes reviews
    <data>/trash/sessions/<id>/trash.json   record: name, created, duration, deleted_at, deleted_via, sizes
    <media>/trash/media/<id>/               its audio

Moving (rather than a ``deleted`` flag in ``session.json``) means the index, job worker, review claim queue, retention
sweep, search, Home, live and the agent API/MCP all see a trashed meeting exactly as they would a removed one, with no
per-consumer filter to forget; jobs and reviews stay associated because they travel in the same folder. ``restore_session``
moves the pieces back and re-indexes them (409 if the id is live again). ``purge_trashed`` is the old full removal.
Meetings are purged after ``TRASH_RETENTION_DAYS`` (30) by ``retention.purge_trash``, called from the ``RetentionWorker``
sweep. A recorder that re-sends an id sitting in trash (stream, pipeline PUT, track upload, finalize) gets it restored
first by ``_restore_trashed`` in ``app.py``; an id that was permanently deleted is created fresh as before. A job still
queued/running when its meeting is trashed is marked errored (audio is kept, so it can be retranscribed after a restore).
Trash routes (``/v1/trash*``) use ``auth.require_token`` only, never agent keys.

## Split and combine meetings

``server/splitmerge.py`` (logic), ``splitmerge_api.py`` (routes, installed by one ``install_split_merge`` call in
``create_app``) and ``splitmerge_ui.py`` + ``static/splitmerge.css`` (dialogs) add two operations that work purely on
files the server already has; nothing is retranscribed. Both need a finished transcript (newest non-superseded job
``done``), no live stream, and a completed upload, and both send the original(s) to Recently deleted with
``deleted_via`` = ``split``/``combine`` and the derived ids recorded in ``trash.json`` (``derived_kind``,
``derived_ids``).

**Timeline.** Transcript times are session-relative (0 = the earliest instant any track started), but a track WAV starts at
that track's own first frame and drifts against wall time. ``TrackAudio`` maps a session time to a WAV frame through the
track's timing log (``FrameClock``: session time -> monotonic -> clock frame -> WAV frame, so the clock rate may differ from
the WAV rate). Tracks without a timing log are treated as starting at session 0.

**Suggestions** (``GET .../split-suggestions``; each ``{time_sec, reason, confidence, label, kind}``, sorted by time):
(a) silence on every track for at least 120 s: candidate windows are the gaps between transcript speech segments, and
when audio exists an RMS scan of the WAVs (0.5 s windows, quiet below 0.004, cached per file) finds runs where all tracks
are quiet. A run confirmed by audio is ``high``; a transcript gap with audio that is not quiet is ``low`` (music or
noise); with the audio deleted it is ``medium``. The point is the middle of the silence. (b) audio lost for at least
15 s, from ``in_gap`` segments and timing-log gaps (a silence that mostly overlaps one is reported as the loss);
``high`` when 60 s or more on every track, ``low`` for a late-attach at the start (point = its end). (c) AI topic shifts
(below). Suggestions within 60 s of each other collapse to the strongest, and any within 10 s of either end are dropped.

**Split** (``POST .../split {points, names?, regenerate_notes?}``): points must be strictly inside the meeting and leave
every part at least 10 s. Part ids are ``<id>_part1..N`` (a ``-sK`` prefix, or a uuid, if those are taken or unsafe). Per
track, boundary frames are computed once per cut, so adjacent parts never lose or repeat a sample; each part gets a WAV
slice (same rate, mono, 16-bit), a timing log with the original monotonic times kept (so the tracks stay aligned and
gaps are carried over), ``session.json`` (``started_wall`` = original + offset, ``split`` provenance), and a transcript job
written directly as ``done``. Segments go to the part holding their midpoint, shifted by the part's start and clamped. A
track that has no audio in a part is omitted there. Parts are WAV-only: the ``.raw`` copies and range sidecars exist for
resumable streaming, which is over. A meeting whose audio was deleted can still be split into transcript-only parts.
``POST /v1/sessions/{orig}/unsplit`` restores the original and permanently removes its parts (they are derived copies);
it is refused if the original has already left Recently deleted.

**Combine** (``POST /v1/sessions/combine {ids, name?, regenerate_notes?}``, 2 to 20 meetings): ordered by start time. Each
track is the parts concatenated on one timeline; every meeting contributes exactly its duration (a track that started late
or is missing is padded with silence) and the real gap between one recording's end and the next's start is filled with
silence, **capped at 600 s**: a longer gap inserts exactly 600 s and ``combine.gaps[].capped`` records the real length. Gaps
under 1 s are not filled and an overlap adds nothing (``combine.parts[].overlap``). Each inserted stretch becomes an
``in_gap`` segment, so the transcript says audio was lost there, and a ``gap`` event in the new synthetic timing log
(``t`` = seconds since the start), which also carries the parts' own gaps. All meetings must have audio
(``audio was deleted for X; combining needs audio``, 409) and the same WAV format per track (no resampling; 422). The
combined ``session.json`` uses the first meeting's name (unless given), device and track metadata, with
``combined_from`` and a ``combine`` block (parts, offsets, gaps, differing devices). ``POST .../uncombine`` restores every
original and removes the combined meeting, refusing if any original is gone from Recently deleted.

**Continuation hint** (``GET .../continuations``): the same-device meeting (``Index.sessions_between``) that ended within
five minutes before this one started, or started within five minutes after it ended.

**Optional AI topic shifts.** ``POST .../split-suggestions/ai`` (409 when the AI provider is ``disabled``) queues a job in
``<data>/split_ai/`` (``SplitAiQueue``), deliberately separate from the notes review queue. The bridge's claim request says
``kinds=notes,split_suggestions``; only then does ``/v1/bridge/review/claim`` return a job with ``"kind":
"split_suggestions"`` (served before notes, since someone is waiting), so an older bridge never sees one. The bridge
downloads a timestamped transcript (``/v1/bridge/split-suggestions/{id}/transcript``, lines like ``[123.4s] You: text``) and
the workflow, runs its provider with ``bridge/split_prompt.md`` and ``split_suggestions.schema.json`` and posts
``{"suggestions": [{"time_sec", "title"}]}`` to ``/complete`` (or ``{"error"}`` to ``/failure``). A result is tied to the
transcript job it was made from, so a retranscribe makes it stale. The split dialog polls ``GET .../split-suggestions``
for the ``ai`` status and shows topic changes as markers.

## Web auth

The browser pages are protected by the same ``MEETING_NOTES_TOKEN`` bearer
token as the recorder client's API (see "Security" below), via a cookie
instead of a header: ``POST /login`` verifies a token against
``auth.token_is_valid`` and sets an HttpOnly, ``SameSite=Lax`` cookie, which
the browser then attaches automatically both to page navigations and to the
same-origin ``fetch()`` calls the pages make against ``/v1/...`` (so
``auth.require_token`` -- the JSON API's own dependency -- accepts either the
usual ``Authorization: Bearer`` header or that cookie; the recorder client
only ever sends the header, so nothing changes for it). An HTML route with
neither lands on ``/login`` rather than a bare 401 body. When no token is
configured at all, the pages are open, matching the API, and show a subtle
banner saying so instead of silently pretending to be secured.

## Notion export

``server/notion.py`` (service, jobs, ordering), ``notion_api.py`` (HTTP client) and ``notion_blocks.py``
(Markdown/notes to blocks) copy finished notes into Notion. Store listeners (``review_completed``,
``session_renamed``) only enqueue a job file under ``<data>/notion/jobs``; one worker thread runs them
(``resume_interrupted`` re-queues survivors after a restart; transient failures back off exponentially up to
5 attempts, anything else fails at once with a readable reason). State (month page ids, each meeting's toggle
block id and start time, per-meeting status, and a cache of parent page titles) is ``<data>/notion/state.json``; the token is ``<data>/notion/token``
or ``NOTION_TOKEN``, never part of settings. The client pins ``Notion-Version: 2026-03-11``, spaces requests
~3/s and honours ``Retry-After`` on 429. Newest-first relies on that version's ``position`` object on "append block
children": a meeting is inserted ``after_block`` the next-newer tracked toggle, or at ``start`` if it is the newest.
A neighbour deleted in Notion is dropped from tracking and the next one is used. Content respects the API
limits (100 blocks and two nesting levels per request, 2000 chars per rich-text item); deeper levels are appended
to the created block afterwards. The review-completed listener always re-exports a meeting that already has a Notion entry (``notion_auto_copy`` only gates first copies); a note type without a parent page enqueues a ``remove`` job instead. Regenerating in the same note type updates the toggle in place (new children are
appended, then the old ones deleted); a different note type/month inserts the new toggle first and then deletes the
old. If a state file is lost, month pages are found again by exact title, but older meetings are no longer tracked
for ordering until they are re-sent.

## Agent access

``server/agent/`` gives AI agents read (and optionally write, never delete)
access to meetings, notes, transcripts, action items and decisions. Keys are
minted in Settings → AI access (``/v1/agent-keys``, behind the normal web
token), stored hashed in ``agent_keys.json``, and are a separate credential
from ``MEETING_NOTES_TOKEN`` in both directions. The same service layer backs
the REST routes (``/api/v1``) and the MCP server (``/mcp``, Streamable HTTP);
``create_app`` installs both via ``install_agent_access`` and runs the MCP
session manager inside its lifespan. Uploaded client diagnostics
(``/v1/client-logs``) live in ``server/client_logs.py``.

## Client compatibility

The server promises to keep working with the current recorder **and the five
releases before it** (`SUPPORTED_CLIENT_WINDOW` in `server/compat.py`). Old
recorders are installed on laptops we cannot reach, so the wire protocol and the
HTTP endpoints they use are a compatibility surface: change them additively.

* **Enforced by tests.** `tests/compat/clients/vX_Y_Z/` holds a frozen copy of
  each release's network code (wire, api, queue, streamer, update, logs).
  `test_compat_contract.py` runs every one of them against the current server,
  over real HTTP and websockets, through a full recording lifecycle (see
  `tests/compat/README.md`). A release cannot ship without its fixture:
  `test_compat_release_checklist.py` fails if `__version__` is not in
  `compat.RELEASES` or a supported release has no fixture.
* **Version reporting.** Recorders send `X-Meeting-Notes-Client: <version>;
  <platform>`. It is parsed leniently; releases up to 0.7.3 send nothing, and a
  missing or unparseable header is a legacy client that is always accepted. The
  server stamps `client: {version, platform}` into a session's metadata on
  upload. Which recorders are connected right now (and their versions) comes
  from the live, in-memory registry in `server/recorders.py` (`GET /v1/recorders`).
* **The floor.** `min_client_version` (oldest release in the window) is
  published in `/health` and `/install/client-manifest.json`. It is a new
  optional manifest field; every released `UpdateManifest.from_json` ignores
  unknown fields (tested).
* **Refusal (HTTP 426) is narrow by design.** Only a recorder that reports a
  version older than the window is refused, and only when it tries to *start* a
  new upload: `POST /v1/uploads`, or a pipeline PUT / track upload / live stream
  for a session the server does not have. The body is
  `{"detail": "...update the recorder...", "min_client_version": "..."}`. Finalize,
  job polling, history, client logs and any session already on the server are
  never refused, so a recording that reached the server is always finished, and
  a refused recording stays on the recorder's disk and uploads after the update.
  The live stream is refused with websocket close code 4400 (already "permanent,
  stop retrying" in every released streamer).

## Recorder presence and remote control

`meeting_notes/remote.py` (shared by both sides, like `wire.py`) defines the protocol; the server half is
`server/recorders.py`, the recorder half `client/control_channel.py` plus `MainWindow` (recordings commands: `client/remote_recordings.py`).

* **Channel.** Each running recorder opens one websocket to `/v1/recorders/connect` (Bearer = the upload token,
  `X-Meeting-Notes-Client` header) and sends `hello` {protocol, instance_id (new per app launch), device, platform
  (0.7.6+ Macs send `macOS 26.6` from `platform.mac_ver()`; the server shows an older `Darwin 25.0.0` as `macOS 26` via
  `remote.friendly_platform`), version, state}. The server answers `welcome`. After that the recorder sends a full `state` snapshot on every change
  (at most 4/s), else every 1 s while recording and every 5 s idle, which doubles as the heartbeat. Snapshots are
  normalized by `remote.sanitize_state` (status, meeting, per-track device/muted/level/peak/degraded/connected,
  banners, update, upload queue, call prompt, stop suggestion, `control.allowed`); unknown keys are dropped and sizes
  capped. The recorder never blocks on it: `publish` is a dict assignment, reconnects back off 0.5 s to 30 s, a server
  without the endpoint (HTTP 403/404 on the handshake) is retried only every 30 minutes, a rejected token every minute.
* **Registry.** `RecorderHub` holds the live recorders in memory only, keyed by instance id. An entry exists exactly
  while its socket is open; it is also dropped after 30 s without a frame (close 4408), and a second connection with the
  same instance id replaces the first (4409). There is no persistence and no history: this is presence, not a client
  manager (it replaced the old `clients.json` / `GET /v1/clients` list).
* **Web.** `GET /v1/recorders` lists them; `WS /v1/recorders/events` (web login, same-origin only) pushes `snapshot`,
  `upsert` and `remove` so the Recorders page is live without polling. Pages and feed are web-auth only; agent API keys
  and the agent API have no recorder access.
* **Commands.** `POST /v1/recorders/{instance_id}/commands` with `{command, args}`. Whitelist (`remote.COMMANDS`,
  enforced by the server and again by the recorder): `start {name?}`, `stop`, `mute|unmute {track}`, `refresh_devices`,
  `accept_call_prompt {name?}`, `dismiss_call_prompt`, `keep_recording`, `stop_suggested`, `retry_uploads`,
  `check_update`, `install_update` (idle only), `set_name {name}`. The server forwards `{type: command, command_id,
  command, args}` and waits up to 5 s for the recorder's `ack` `{ok, code, error, state}`; the HTTP reply is 200 with the
  ack (a refusal is `ok: false` with a `code` such as `remote_control_disabled`, `already_recording`, `not_recording`,
  `recording_in_progress`, `no_prompt`), 404 not connected, 400 invalid, 504 no answer. On the recorder the command
  runs on the Qt thread through the same handlers as the buttons, is logged (`source=server`), shows a short notice, and
  is refused when "Allow control from the server" is off in its Settings.
* **Idle level preview (0.7.7).** Before recording the window shows live mic and system input greyed as "Preview", and
  the Recorders card shows it too. `client/idle_meter.py` is the meter: one daemon thread per track opens the same
  `AudioSource` a recording would and keeps the latest `block_peak` (the recording's own level function); it writes
  nothing. `RecordingController.set_idle_wanted` runs it only while allowed (setting `show_audio_levels`, default on),
  idle, and someone looks (window visible and not minimized, or a web viewer watching). `controller.start()` stops the
  meter first (bounded join, a wedged lane is abandoned) and a `_starting` flag stops the device watcher restarting
  it, so no two readers share a device and the recording has no gap. Device scans retarget it by device name.
  **macOS meters the microphone only**: system audio is ScreenCaptureKit, whose permission prompt and screen-recording
  indicator should not appear just for a level; the system bar shows a dash (tooltip) until recording.
  **Watch protocol** (`remote.py`): the recorder advertises `caps: ["idle_levels"]` in its hello; the page sends
  `{type: watch, visible}` on the events socket (open, visibility change, every 10 s; valid 30 s); the hub sends each
  capable recorder `{type: watch, levels: bool}` on change and renews every ~8 s; the recorder treats it as a 25 s
  lease, so a dead server or page never leaves the mic open. While watched and idle it sends `{type: levels, mic,
  system}` at most every 0.2 s (unchanged values once a second); the hub ignores faster frames and anything while not
  idle, stores the level, and forwards a compact `levels` event to pages, which repaint only the bars. State carries
  `preview {supported, active, tracks}`; an older recorder has none, so its card says "Levels show while recording".
  Recorders without the cap are never sent a `watch`; unknown frames are ignored by old clients and servers.
* **Recordings (0.7.6).** Three more whitelisted commands let the server see and manage what sits in a recorder's save
  folder: `list_recordings {offset?}`, `reupload {session_ids}` (the recorder's own `SessionQueue.requeue` + wake, the
  path of its Re-upload window) and `delete_local {session_ids}` (Recycle Bin / Trash through `client/retention.py`;
  refuses the active recording and anything an uploader holds, matches ids only against folders that exist in the save
  folder, drops the queue entry). Their ack carries an extra `result` (normalized by `remote.sanitize_result`). Such an
  ack may be up to `MAX_ACK_FRAME_BYTES` (512 KB result + 16 KB; every other frame is still 16 KB and a big non-ack
  frame is junk), a command frame may be up to 64 KB (at most 100 ids per command), and the server waits
  `TIMEOUT_FACTOR` times the usual command timeout for them. A listing is paged by size (`next_offset`; at most 1000
  recordings). The recorder does the scanning / deleting on a worker thread, so the window and the heartbeat never stall.
* **Status join.** `server/recorder_recordings.py`: `GET /v1/recorders/{id}/recordings` sends `list_recordings` (paging),
  looks every session id up in the live index (`Index.get_many`) and the trash, and `recording_status.combine` turns the
  recorder's queue state plus the server's knowledge into one status (shared with the client): recording, uploading N%,
  failed, waiting, uploaded (transcript ready / transcribing / queued / failed), partial, in trash, not on server, invalid,
  unknown. What this computer is doing right now (uploading, failed, waiting) wins over what the server says. `POST
  /v1/recorders/{id}/recordings/reupload|delete` chunk up to 1000 ids into commands of 100 and merge the per-id results.
  `POST /v1/recordings/status` (client token) gives a recorder the server's side for its own ids so its Re-upload window
  can show the same statuses. All are web/server-token only; agent keys are refused.

## Security

The upload endpoints require a bearer token shared between client and server
(`MEETING_NOTES_TOKEN`). If it is unset the server allows everything and says so
loudly at startup — convenient on a trusted home LAN, but it is an open audio
endpoint, so it should be a deliberate choice rather than a default nobody
noticed.

## macOS system audio (ScreenCaptureKit)

Windows loops back an output device through WASAPI; macOS has no such device, so
`audio/screencapture_source.py` asks ScreenCaptureKit (macOS 13+) for the
display's audio (`capturesAudio`, `excludesCurrentProcessAudio`, 48 kHz stereo,
a 2x2 pixel 1 fps video stream that is ignored) and presents it through the same
`AudioSource` / `Reader` protocol as every other source, so the recorder, timing
log and WAV writer have no macOS special cases. Layers, only the last of which
imports PyObjC (lazily, behind platform checks):

- `extract_pcm` / `pcm_bytes_to_array` turn a `CMSampleBuffer` into
  `(frames, channels)` float32 (interleaved or planar, float or int). CoreMedia
  is passed in, so tests use a fake.
- `BlockQueue` gives the recorder its blocking `read(n)` while keeping frame
  position equal to wall-clock time: ScreenCaptureKit may deliver nothing while
  the Mac is silent, so a read that is more than a short slack behind the wall
  clock is filled with silence, and later-arriving frames are dropped to repay
  exactly that "debt". A stream error is raised to the recorder, whose existing
  watchdog reopens the source and pads the gap.
- `ScreenCaptureKitSource` is the `AudioSource`; `ObjcStream` is the PyObjC
  bridge (`SCShareableContent` -> `SCContentFilter` -> `SCStream` with an
  `SCStreamOutput` handler).

Device selection (`audio/devices.py`) lists ScreenCaptureKit first on macOS 13+
and keeps BlackHole-style drivers as a fallback: if Screen & System Audio
Recording has not been granted and a driver is installed, the driver is the
default so recording still works. Only `resolve_source(..., interactive=True)`
(recording start) may trigger the one-time macOS permission prompt; device
probing never does. Call detection and the updater have macOS branches described
in the README.

Server side, `/install/client-manifest-macos.json`, `/install/MeetingNotes-macOS.zip`
and `/install/mac.sh` mirror the Windows endpoints (public, token never embedded);
the manifest carries the server's `__version__`, and `client/update.py` selects
the manifest by `sys.platform` and runs the verified `.sh` with `/bin/bash`.

### Device hot-plug on macOS

`client/device_watch.py` polls `RecordingController._scan_devices` every 3 s (there is no
`WM_DEVICECHANGE` equivalent wired up on macOS, so polling is the whole mechanism). The mic
is a CoreAudio enumeration through `soundcard` (fresh on every call, so a headset that is
switched on later shows up). "System" is not a device: it means "ScreenCaptureKit is usable
right now" (macOS 13+, PyObjC bindings, Screen & System Audio Recording allowed), falling
back to an installed BlackHole-style driver. The scan never prompts;
`devices.prompt_system_permission_once()` is called only when a recording starts. When
ScreenCaptureKit becomes usable mid-recording the watcher attaches it through
`RecordingSession.attach_source`, the same late-attach gap path as on Windows; a dead SCK
stream is replaced by re-resolving its name.
