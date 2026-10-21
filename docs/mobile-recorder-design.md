# Mobile recorder: design contract

Developer contract for recording in-person meetings from a phone's web browser. User-facing docs go in
`README.md` and `docs/reference.md` when it ships. Server-only: no desktop client release is needed.
Revised after a Codex review (2026-10-20); the review's answers to the open questions are adopted below.

## Goals and limits

- A page, `/record/`, for **iPhone (Safari) and Android (Chrome) only**. On a laptop or desktop it says to use the
  desktop recorder (which also captures the other side of online calls) and offers no recorder.
- Recordings up to about **an hour**; two hours must still work.
- **Live transcript** is an optional switch, **off by default**. When on, the meeting shows live on the web
  Meetings page like a desktop recording.
- One microphone in the room: one track, **speakers identified on it** ("Speaker 1", "Speaker 2").
- Reached over `https://meeting.fowlapps.com` (browsers allow the microphone only on HTTPS). Away from home the
  user is on a VPN; with no connection the page still records and uploads later.

### What can and cannot be recovered (stated honestly in the UI)

A web page cannot promise that nothing is ever lost. The unit that survives is a **chunk whose IndexedDB write has
committed**:

| what happened | what survives |
|---|---|
| Stop pressed, or a recorder run rotated normally | everything (the run is complete) |
| Interrupted (screen locked, call, mic lost) and the browser delivered the final blob | everything up to the interruption |
| Tab or phone killed mid-run | every committed chunk of earlier runs; the current run's committed chunks are tried, but an unfinished run may not decode (at most one run, about 5 minutes, at risk) |
| Storage denied or full | the page shows an error at once and does not pretend to save |

The page only says **"Saved on this phone"** for chunks whose IndexedDB transaction completed. Restarting after an
interruption is attempted automatically when the page is visible again; if the browser needs a tap to grant the
microphone again, the page shows **Resume recording**. iPhone cannot record with the screen locked or another app
in front; the page keeps the screen awake (Screen Wake Lock, which the system may refuse or release) and says so.

## Terms

- **Mobile session**: one meeting recorded on the phone; a normal server session id (existing id rules).
- **Run**: one `MediaRecorder` from `start()` to `stop()`. The page **rotates the recorder every 5 minutes**
  (stop, start a new one on the same stream) and after every interruption, so each completed run is a
  self-contained file and a lost run costs at most ~5 minutes.
- **Chunk**: one `dataavailable` blob of a run (`timeslice` 5 s; browsers may deliver later or larger). Only the
  chunks of one completed run, joined in order, are expected to form a playable file.

## Recording on the phone

- `getUserMedia({audio: {echoCancellation: false, noiseSuppression: false, autoGainControl: true, channelCount: 1}})`.
- `MediaRecorder` with the first supported type of `audio/webm;codecs=opus`, `audio/mp4;codecs=mp4a.40.2`,
  `audio/mp4`; the run records the **actual `recorder.mimeType`**. `audioBitsPerSecond: 32000` (may be ignored).
- Interruption signals: `visibilitychange` to hidden, the track's `ended`/`mute`, `MediaRecorder` `error` or an
  unrequested `stop`. The run is closed (final blob saved if delivered) and marked `interrupted`.
- IndexedDB `mn-record`, versioned schema: `sessions` (id, name, note type, device label, state, live,
  runs [{index, mime, state: recording|complete|interrupted|lost-tail, started_ms, chunk_count}]) and `chunks`
  (key `[session, run, seq]`, blob, committed, uploaded). A chunk is queued for upload only after its write
  committed. `navigator.storage.persist()` is requested; `QuotaExceededError` stops the recording with a clear
  message rather than silently dropping audio. Free space is shown before starting when the browser reports it.
- After the server reports the meeting **finalized**, that session's data is deleted from the phone.
- On open, sessions not finalized are listed: **Upload** (send what is missing, then finish) or **Delete from this
  phone** (confirm dialog; also deletes the server's partial upload).

## Upload protocol

Same-origin `fetch` with the sign-in cookie. **Every cookie-authenticated write (POST/PUT/DELETE) requires an
exact `Origin` match** with the configured server address (`server_address`), and the bearer-token path for
desktop clients is unchanged. This check is added to the new routes now, and is worth extending to all
cookie-authenticated writes later.

| route | body | answer |
|---|---|---|
| `POST /v1/mobile/sessions` | `{session_id, name, note_type, device, started_wall, live}` | `201`; same id again = `200` with current state |
| `PUT /v1/mobile/sessions/{id}/runs/{n}/chunks/{seq}` | raw blob, `Content-Type` = run mime | `204`; identical bytes again = `204`; different bytes = `409`; session not `open` = `409` |
| `GET /v1/mobile/sessions/{id}` | | `{state, runs: {n: [[from, to], ...] received seq ranges}, job_id?, error?}` |
| `POST /v1/mobile/sessions/{id}/finish` | manifest `{runs: [{index, mime, state, chunk_count, started_ms}], name, note_type}` | `202 {state}`; missing chunks = `409 {missing}`; repeat = current state |
| `DELETE /v1/mobile/sessions/{id}` | | only while `open` or `failed`; `409` while `preparing` |

### Server state machine (persisted, `<media>/<id>/mobile/state.json`, atomic writes, one lock per session)

`open → preparing → finalized` or `→ failed` (with `error`; **Retry** from the page re-runs preparation).

- Chunk write: stream the request to a temp file (enforcing per-chunk and aggregate quotas **while** reading, not
  from `Content-Length`), hash it, then atomically rename to `runs/<n>/<seq>.part` if absent; if present, compare
  hashes (`204`/`409`). The aggregate byte count is updated under the session lock.
- `finish`: under the lock, freeze the manifest (immutable once accepted), check every listed chunk exists, move to
  `preparing`, enqueue preparation. Further PUTs are `409`.
- Preparation runs on a **bounded worker** (one decode at a time server-wide) and is restartable: a server restart
  finds `preparing` sessions and runs them again from the frozen manifest.
- Chunks are deleted only after the WAV, meta, timing log and job record are all written (`finalized`).

### Preparation (decoding and placing in time)

1. Per run: reject gaps in its sequence; join its chunks; probe and decode with ffmpeg
   (`-ac 1 -ar 16000 -c:a pcm_s16le`), output capped by duration (run length limit 15 min) and size. A run marked
   `interrupted`/`lost-tail` that fails to decode is decoded as far as ffmpeg gets (`-err_detect ignore_err`) and,
   if nothing usable comes out, recorded as lost in `meta.mobile.lost_runs`; the meeting still finishes.
2. **Durations come from decoded frame counts**, never from phone clocks. Phone `started_ms` is used only for the
   gap *between* runs, with sanity rules: negative or overlapping gaps become 0, and gaps are capped at 10 minutes
   (a longer gap becomes 10 s of silence and is listed in `meta.mobile.gaps`).
3. Concatenate runs with silence for gaps into the `system` track WAV (16 kHz mono). Write a timing log in the
   format `timing.py` expects: an `open` event with the meeting start, frame-offset entries at each run start,
   and a `gap` event per gap, so `merge_tracks` places text in wall time.
4. Meta: `name`, `device`, `platform` (`iOS`/`Android`), `source: "mobile"`, `in_person: true`, `started_wall`,
   `note_type`, `tracks.system.source = "mobile"`, `mobile: {runs, gaps, lost_runs}`.
5. Stop any live writes for this session (see Live), then `job_queue.ensure_job`. From here it is a normal meeting.

### Limits (all configurable; defaults)

8 MB per chunk; 15 minutes of audio per run; 40 runs; the existing `MEETING_NOTES_MAX_UPLOAD_BYTES` per session;
**5 unfinished mobile sessions** at once and **2 GB** of staging in total (`507` when full); unfinished sessions
untouched for **14 days** are deleted by the retention sweep; one preparation at a time; live stream frames
rate-limited to real time ×2 and offsets bounded.

## Speaker labels for in-person meetings

- Diarization already runs on the `system` track. For `in_person` sessions the labels are "Speaker 1",
  "Speaker 2", ...: the prefix comes from the session, in **both** the local path (`jobs._transcribe_local`) and
  the helper path (`finish_remote`), and the track's base label is "Speaker".
- Policy (new setting `in_person_diarization`, default `wait_then_cpu`): wait up to **N minutes** (setting, default
  see open question 1) for a GPU helper; if none takes it, transcribe here and run speaker labels on the CPU on a
  **separate low-priority worker** (one at a time) so a long CPU run never holds up other meetings. Notes wait for
  the labels; the meeting shows "Identifying speakers" meanwhile. With the setting `off`, the transcript says
  speaker labels are unavailable rather than promising them.
- A one-line context for in-person meetings is added to the review prompt (like the names glossary, without
  editing stored prompts): "This was an in-person meeting recorded on one phone; 'Speaker N' labels are voices told
  apart by software, not names, and may split or merge people."

## Live transcript (optional, off by default)

- Uses the existing `/v1/stream` websocket and live preview, so it shows on the Meetings page via `/v1/live`.
- Audio: the recorder's `MediaStream` → `AudioContext` (whatever rate the device gives, typically 44.1/48 kHz) →
  `AudioWorkletNode` that **explicitly resamples** to 16 kHz mono PCM16 and posts 20 ms frames → websocket, in
  the existing binary frame format, track `system`.
- **Auth: a short-lived (60 s), single-use, session-bound stream ticket** from `POST /v1/mobile/sessions/{id}/
  stream-ticket` (cookie + Origin check), sent as a websocket subprotocol value; `/v1/stream` also requires the
  exact `Origin`. A reconnect asks for a new ticket. Desktop token/header auth is unchanged; the shared token and
  the cookie are never extended to the socket.
- **Connection generations**: each ticket carries a generation; a newer connection for the session replaces the
  older one (the server closes the old socket) instead of the current "duplicate session" rejection, for ticketed
  mobile sessions only. When `finish` is accepted the live session is closed and further stream writes for it are
  refused, so a late socket cannot touch the final audio.
- The streamed audio is **not** a recording and is not kept as a fallback (it can have holes); the final `system`
  WAV always comes from the chunks.
- If the socket drops, recording continues; the page reconnects with backoff and shows "Live transcript paused"
  while it is down. Switching live on or off during a recording is allowed.

## Installing on the phone (PWA)

- `GET /record/manifest.webmanifest` (`start_url` and `scope` `/record/`, `display: standalone`, theme colours from
  the existing tokens, 192/512 PNG icons from the app icon).
- A versioned service worker scoped to `/record/` caches **only the recorder shell** (HTML, CSS, JS, fonts) so the
  page opens with no connection **after it has been opened online once**. It never caches API responses, auth
  redirects or anything sent with the cookie. A new version shows "Update available, reload" when not recording.
- "Add to Home Screen" instructions shown once per device (iOS has no install prompt).
- The sign-in cookie gets `Secure` when the configured `server_address` is `https://` (trusted configuration, not a
  request header).

## The page

Mobile layout from the existing tokens and components (no desktop counterpart, so the client-matches-web rule does
not apply):

- Top: meeting name, note type picker (same list and default as the desktop recorder), **Live transcript** switch.
- Center: one large **Record** button; while recording: elapsed time, a level meter, "Saved on this phone · 12 of
  14 pieces uploaded", **Stop**. No pause (Stop and a new recording is clearer).
- Short warnings: "Keep this page open. Locking the screen pauses the recording." (iPhone); "Not connected:
  recording on this phone, it uploads when you're back online"; "Microphone blocked: allow it in Settings > Safari >
  Microphone"; "Storage full: recording stopped".
- Below: recordings not yet finished on this phone (Upload / Delete), and live text when the switch is on.
- Device label "iPhone" / "Android phone" by default, editable once, kept in localStorage.
- Desktop browsers: a short page linking to the desktop recorder install.

## Tests (release gates in bold)

- Server: chunk PUT idempotency/conflict/quotas enforced while streaming; concurrent PUT + finish + DELETE; state
  machine and restart during `preparing`; finish with missing chunks; gap rules (negative, overlap, cap); decoded
  durations; timing log consumed by `merge_tracks`; limits (`507`, `413`, age sweep); Origin check on every cookie
  write; stream tickets (expiry, single use, wrong session, wrong Origin); connection generations and the
  post-finish write fence; "Speaker N" labels in both job paths; in-person diarization policy and its separate
  worker; prompt context line; manifest and service worker types/scope/no-cache of API.
- **Real fixtures from iPhone Safari and Android Chrome** (complete runs, an interrupted run, a killed-tab run)
  under `tests/fixtures/mobile/`, decoded by the server.
- Page (node harness): upload queue order/backoff, resume from status ranges, run rotation and interruption
  bookkeeping, "saved" only after commit, quota error, desktop gate.
- **Manual on real phones**: a one-hour recording; lock the screen mid-way; airplane mode then reconnect; kill the
  tab and reopen; live on/off; Add to Home Screen, then record with no connection.

## Open question for Mike

1. How long should an in-person meeting wait for a GPU helper before falling back to speaker labels on the
   server's CPU (about 1.5 h per hour of audio on triwizard)? Codex suggests ~5 minutes; a phone meeting recorded
   away from home may finish while the desktop with the GPU is off.
