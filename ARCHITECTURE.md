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
  audio/               capture (client side)
  wav_io.py timing.py  recording format and clock alignment (shared)
  transcribe/          Whisper backend and transcript merge (server side)
  client/
    controller.py      bridges recorder, stream and queue
    resample.py        48kHz -> 16kHz with anti-aliasing
    streamer.py        websocket with reconnect and resume
    queue.py           offline upload queue
    api.py             HTTP client
    ui/                Qt window, waveform, settings
  server/
    app.py store.py jobs.py live.py auth.py
    index.py           SQLite index behind the web UI / JSON API
    settings.py        persisted server settings (model, retention, ...)
    retention.py        the audio-retention sweep and its worker thread
    web.py             HTML rendering for the browser UI
docker/                Dockerfile, compose
```

## The web UI, the session index, and settings

The server also serves the product's primary management UI from the same
FastAPI app and process as the recorder client's API. A persistent sidebar
links to a home dashboard, saved transcriptions, and settings. Home combines
currently connected live sessions with recent history; saved transcriptions
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
and beam size, optional diarization controls, and the audio retention policy
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

## Security

The upload endpoints require a bearer token shared between client and server
(`MEETING_NOTES_TOKEN`). If it is unset the server allows everything and says so
loudly at startup — convenient on a trusted home LAN, but it is an open audio
endpoint, so it should be a deliberate choice rather than a default nobody
noticed.
