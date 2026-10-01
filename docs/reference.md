# Reference

Detailed material that does not fit in the [README](../README.md): per-platform
notes, the web UI in depth, the command-line tools, transcription models, the
optional diarization feature, build details and limitations. For how the pieces
talk to each other see [architecture.md](architecture.md).

Meeting Notes records a meeting as **two separate audio tracks** (your
microphone and your system audio) and transcribes them into one transcript with
exact "You" / "Them" labels:

```
**[00:04:12] You:**  Can we push the launch to the 30th?
**[00:04:19] Them:** That works, I'll update the tracker.
```

Runs on Windows and macOS. On Windows it needs **no driver and no admin**; on
macOS 13+ the packaged app captures system audio through ScreenCaptureKit, also
with **no driver and no admin** (one permission click instead).

## Meeting detection (Windows and macOS clients)

The Windows client can notice when a Teams, Zoom or Google Meet (Chrome, Brave,
Edge) call starts and ask whether to record it. It reads Windows' per-app
microphone-use record (current user, no admin) every couple of seconds, so it
only works while the client is running; the window can be minimized.

- When a call has held the microphone for a few seconds, a small always-on-top
  card appears in the bottom-right corner: "Teams call detected", an editable
  meeting name (taken from the call window's title, or e.g. "Zoom call 2:30 PM"),
  and **Record** / **Not now**. It goes away by itself after a minute.
- **Record** starts a normal recording with that name. Manual Start/Stop still
  works exactly as before.
- When the call ends (after a 20-second grace so brief drops do not count), a
  recording that was started from the prompt is stopped and queued automatically.
  A recording you started by hand is never stopped automatically.
- **Stop suggestions (every recording, never automatic).** When the same
  end-of-call evidence is met (mic released, no call window, system audio quiet for
  the grace) during a recording that has no auto-stop countdown -- one you started
  by hand, or a prompted one with auto-stop off -- a small always-on-top card asks
  "Meeting seems to have ended -- stop recording?" with **Stop recording** and
  **Keep recording**. Nothing is stopped unless you press Stop. Keep suppresses
  further suggestions for that call (a new call in the same recording brings them
  back), and the card goes away by itself if the audio or the call comes back.
- **Silence fallback** for meetings that are not recognised as calls (in person, an
  unknown app): if both tracks stay quiet for 5 minutes during any recording, the
  same card asks "No audio for 5 minutes -- stop recording?". It does not repeat
  until audio has resumed and gone quiet again.
- Settings has checkboxes for the prompt, the auto-stop, and "Suggest stopping when
  a meeting seems over" (`meeting_detection` in `config.json`: `enabled`,
  `auto_stop`, `suggest_stop`; `end_grace_sec` is clamped to 5-300). Each
  suggestion and your choice are written to the client log.

## Client updates (Windows client)

Updates are never installed by themselves. With "Check the server for client
updates" on (the default), the client asks the server for a newer version at start
and every few hours; when one exists a persistent bar reads "Update available:
0.7.4" with an **Update now** button (and a **What's new** link when the server's
manifest carries `notes` or `notes_url`). Clicking it downloads, verifies (size and
SHA-256) and runs the installer; while a recording is running it refuses and waits
until you stop. The old "install automatically when idle" setting is gone, and
`auto_update` in an old `config.json` is ignored.

Every request to the server (uploads, the token check, the updater and the live
stream's handshake) carries `X-Meeting-Notes-Client: <version>; <platform>`. If the
server answers HTTP 426 (`{"detail": ..., "min_client_version": "x.y.z"}`), or the
manifest's `min_client_version` is newer than the client, a red banner says "This
version is no longer supported by the server -- update to keep uploading" with the
same Update now button. Queued recordings wait and upload after the update.

**On macOS** the same prompt and end-of-call rules apply, with macOS probes
(`client/meeting_detect_mac.py`): CoreAudio's per-process "is running input"
flag says which app holds the microphone (macOS 14.2+; older versions fall back
to "a mic is live" attributed to the running meeting apps), Meeting Notes' own
capture is ignored, and window titles come from `CGWindowListCopyWindowInfo`.
Titles need the Screen & System Audio Recording permission; without it the
prompt still appears, named after the app ("Zoom call 2:30 PM"). A call ends when
the mic is released, no call window remains, and the system audio has been
silent for the grace period, then the usual 60-second stop countdown runs.

## Two pieces

The laptop in the meeting does as little as possible; a box on your LAN does the
compute.

- **Recorder** (`meeting-notes-ui`) — a small Qt app on your Mac/Windows
  machine. Start/stop, live waveform for both tracks, a save-folder setting. It
  records to disk and streams a copy to the server. **No Whisper, no models, no
  ML dependencies on the laptop.**
- **Server** (Docker, another machine on the same network) — receives audio and
  does all the transcription.

There are deliberately two transcription passes: a **live** one over the stream
for a rough preview while you talk, and a **final** one over the complete
uploaded recording, which is the transcript you keep. The local recording is
always the source of truth, so a dropped connection cannot lose audio — and if
the server is down entirely, recording continues and the session is queued and
uploaded later.

See [architecture.md](architecture.md) for the protocol and the reasoning.

## Why not a virtual microphone?

The obvious design is a virtual mic and speaker that the meeting app connects
to, passing audio through and recording it in the middle. That was the original
plan here, and it was rejected. Two reasons:

1. **You cannot create a virtual audio device from Python.** They are
   driver/kernel objects — a signed driver on Windows, an AudioServer HAL
   plug-in on macOS. Both need admin, neither is scriptable. Only Linux
   (PipeWire/PulseAudio null sinks) can do it unprivileged.
2. **It destroys the information you actually want.** Passing everything
   through one virtual device mixes your voice and theirs into a single stream,
   so the transcript cannot tell you who said what. It also inserts latency into
   your own monitor path and interferes with the meeting app's echo
   cancellation.

Capturing two tracks instead sidesteps all of that. Your microphone is opened in
shared mode *alongside* the meeting app (both can hold it at once), and system
audio is captured separately. Nothing about your existing audio routing changes,
and speaker attribution comes for free.

## Install

### Windows client from the server UI

For a normal Windows installation, sign in to the Meeting Notes web UI and
select **Install client agent** in the lower-right. The installation page walks
through downloading and running `Install-MeetingNotes.ps1` without administrator
access. The installer:

- downloads the self-contained Windows release;
- installs it under `%LOCALAPPDATA%\MeetingNotes`;
- includes Python, Qt, NumPy, SoundCard, HTTP/WebSocket libraries, and their
  native runtime files -- Python and pip are not required on the meeting PC;
- preserves the existing server token, recording folder, and client preferences
  during upgrades;
- creates Start Menu and desktop shortcuts;
- writes `How to run Meeting Notes.txt`, checks server reachability, and launches
  the client.

On first run, open **Settings**, enter the same token used for the server web UI,
and confirm the preconfigured server address. Windows 10 or 11 64-bit and
PowerShell 5.1 or newer are required. No audio driver or compiler is required.

The install page also provides a one-step command that downloads the
server-hosted installer and runs it directly in PowerShell:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "irm 'http://meeting.lan/install/client-agent.ps1' | iex"
```

Use the server address shown by your own install page in place of
`http://meeting.lan`.

### Development install

**On the machine that runs the meetings** (the recorder):

```bash
python -m venv .venv
.venv/bin/pip install -e '.[client]'   # Windows: .venv\Scripts\pip install -e .[client]
meeting-notes-ui
```

That pulls `numpy`, `soundcard`, PySide6 and an HTTP client. No compiler, no
admin rights, and no ML stack.

**On the machine that does the transcription** (the server):

```bash
cd docker && docker compose up -d
```

Point the recorder at it under Settings → Server URL.

The desktop recorder also has **Upload** for adding an existing meeting. It
opens one dialog with three choices. **Audio recording** accepts WAV, MP3,
M4A/MP4, FLAC, OGG/OGA, Opus, AAC, and WebM; the upload runs in the background
and the server performs decoding and STT. **Transcript file** (`.txt`, `.vtt`,
`.srt`) and **Paste a transcript** add a transcript you already have, for
example from Teams or Zoom, without transcribing anything again
(`POST /v1/sessions/transcript`; the web Home page has the same under
**Add a meeting**). Times and speaker names are kept when the text has them
(`[00:12:34] Jane: ...`, WebVTT/SRT cues, Teams copy-paste); otherwise the
timeline is estimated and marked approximate. Transcripts can be up to 2 MB.
Notes are generated if auto-generate is on. During
a live recording, **Mute you** and **Mute them** independently silence one
source while keeping the recorder, timeline, and other source running.

**Re-upload a saved recording** (the "..." menu) puts recordings that are still
in your save folder back on the upload queue, for example after a meeting was
deleted on the server. It lists the folders it finds (newest first, with length,
size and whether each is already queued), lets you tick several or browse to a
folder elsewhere, and rejects folders that are not real recordings. Each one is
re-sent in full under its original session id and transcribed again; its
per-track "already uploaded" record is cleared so nothing is skipped. Each row also shows what the server
knows about it (Uploaded, transcript ready / transcribing / failed, Uploading 40%, Waiting to upload, Upload failed,
Not on server, In server trash), checked with the server in the background and cached for 30 seconds, and the
window can **Delete from this computer** (to the Recycle Bin or Trash; refused for the recording in progress and
for one that is uploading right now). When the server has no copy, the confirmation says in red that this
permanently removes the only copy.

**Settings → Local recordings** can remove old recordings from this computer
after they are safely on the server: Forever (the default), 7, 30 or 90 days.
A recording is removed only when it is off the upload queue, older than the
chosen age, and the server (reached with a valid token at that moment) returns
the meeting, does not mark it deleted or trashed, and shows both the upload and
the transcription as complete. Anything else keeps it, and every skip or
deletion is written to the client log. Removed folders go to the Windows
Recycle Bin. The policy runs a couple of minutes after start-up and every six
hours while the app is idle, or on demand with **Clean up now** (which shows what
will be freed and asks first).

### Server web UI

Open the server address in a browser to manage recordings. Home shows connected
live transcription sessions and recent history, and accepts uploaded meeting
recordings. Upload progress is visible while the file is sent; the server then
uses ffmpeg to normalize supported formats and runs the configured STT model.

Meetings (`/meetings`, also reachable at `/transcriptions`) provides a searchable list (names, transcripts, or a meeting number like `M-0142`) with upload/transcription
pipeline status and percentages. It is one continuous list, newest meeting start first (ties by id), with a gap and a day heading (Today, Yesterday, then "Monday, Sep 28", plus the year when not this year; the browser's timezone) above each day's meetings. The server and the page use the same order, so Load more never repeats or skips a row. Tick several rows (the checkbox appears on hover) for **Build Meeting Notes**,
retranscription, audio-only deletion, or deletion. Meetings with finished notes open on the notes; the transcript is one click away. On hover, a meeting without notes shows a **Generate** button. Selecting a row opens the audio players,
transcript, and matching status checklist in a document view with Notes and
Transcript tabs and a You/Them timeline. The same view shows meeting notes after
**Build Meeting Notes**; meeting names and generated summary titles can be edited
independently (click the title, or use the ... menu). Press Escape or use Back to
close it. The Install client agent link opens the installation guide.
**Recently deleted.** Deleting a meeting (from the row checkboxes or the meeting's ... menu) asks in a dialog that
lists each meeting, then moves it to **Recently deleted** (`/meetings/trash`, linked from the Meetings header) instead of
removing it; a toast offers **Undo**. Recordings, transcripts, notes and jobs are all kept for 30 days, during which you
can **Restore** a meeting exactly as it was, **Delete permanently**, or **Empty trash**; after 30 days the server purges
it (the retention worker logs each purge). Deleted meetings disappear from the list, search, Home, the agent API/MCP and
audio retention. If the Windows client re-uploads a meeting that is in Recently deleted, the server restores it first.
JSON: `GET /v1/trash`, `POST /v1/trash/{id}/restore`, `DELETE /v1/trash/{id}`, `POST /v1/trash/empty` (web token only).
**Delete audio** is separate and stays permanent.
**Split and combine.** A meeting's ... menu has **Split meeting...**: a dialog shows the You/Them timeline with
recommended split points (marked, with the reason on hover or tap) for silence on every track lasting two minutes or
more, stretches where a device dropped out, and, with an AI provider on, **Suggest with AI** topic changes (an async
job for the bridge; older bridges never receive it). Click the timeline or a transcript line, or type a time, to add your
own points; name each part, then **Split into N meetings**. Each part gets its own audio slice, timing log and the
matching part of the finished transcript (nothing is retranscribed), is named "<name> (part N)" unless you rename it, and
appears as complete; the original moves to Recently deleted, and the toast offers **Undo**. Ticking two or more rows
enables **Combine** in the bulk bar (disabled, with the reason, if more than 20 are ticked or one is still transcribing or has no audio): the dialog lists them in time order with the gap between each pair, and joins
the audio per track with the real gap filled with silence (at most 10 minutes; a longer gap is shortened to exactly
10 minutes) and shown as an audio-lost gap in the transcript. Combining needs every meeting's audio (a meeting whose
audio was deleted is refused with a clear message). A meeting that started within five minutes of another meeting from the
same device ending (for example a recording that was cut off and restarted) shows a dismissible "Looks like a continuation
of ... Combine?" banner. The menu also always offers **Combine with another meeting...** (disabled, with the
reason, while this meeting is still transcribing or has no audio): a searchable picker lists the other meetings,
those close in time and the suggested continuation first, then newest first, and hands the choice to the combine
dialog. Both dialogs can regenerate notes for the new meetings. JSON (web token only, never agent keys):
`GET /v1/sessions/{id}/split-suggestions`, `POST /v1/sessions/{id}/split-suggestions/ai`,
`POST /v1/sessions/{id}/split` `{points, names?, regenerate_notes?}`, `POST /v1/sessions/{id}/unsplit`,
`POST /v1/sessions/combine` `{ids, name?, regenerate_notes?}`, `POST /v1/sessions/{id}/uncombine`,
`GET /v1/sessions/{id}/continuations`.
Settings groups appearance, installation, transcription, meeting-notes AI, and retention
into separate sections, including the editable note types. (The "Remote speaker labels"
section is no longer shown; the diarization settings stay in `settings.json` and the
`/v1/settings` API, and saving the web form leaves them untouched with diarization off.)

**Note types (prompt templates).** A note type decides two things: the kind of summary the AI writes (its prompt, shown as **Summary instructions**) and where the notes are saved in Notion (its **Save to Notion** parent page). Meeting notes can be generated with different note types.
Built in: **Standard** (the original `ai_workflow` prompt; the `ai_workflow` setting *is*
Standard's prompt, so existing servers behave identically), **Quick notes** and **Detailed
webinar**. Add your own in Settings, pick a **Default note type** (used by auto-generate and
the meetings-list Generate button), and choose a note type per meeting from the **Note type** select in the
meeting view (it shows where that type saves to Notion, e.g. "Saves to Notion → Webinars"; a Regenerate button appears when it differs from the
notes' type). Settings JSON: `note_templates` (list of `{id, name, prompt}` for everything
except Standard), `default_template_id`; `GET /v1/note-templates` lists the note types;
`POST /v1/sessions/{id}/review?template=<id or name>` and
`POST /v1/meeting-notes/{id}/retry` (JSON body `{"template": ...}`) choose one. The claim
response's `workflow_url` is per review (`/v1/bridge/review/{id}/workflow.md`), so bridges
need no change. The agent API takes `template` on notes generation and lists note types at
`GET /api/v1/note-templates` (MCP: `meeting_notes_list_note_templates`).
**Copy notes to Notion.** Finished notes can be copied into Notion (server only; no client
change). In Settings, create an internal integration at notion.so/profile/integrations, paste
its token under **Notion** (or set `NOTION_TOKEN` on the server; the environment wins), and for
each parent page you use open it in Notion, then the ••• menu, **Connections**, and add the
integration. Each note type (Standard included) gets an optional **Save to Notion** parent page (a page
link or id) in its editor; empty means that note type is not copied (`GET /v1/notion/parents` resolves the pages' titles for the UI). Under the parent the server
keeps one page per month, titled `<Month>-<YYYY> <note type name>` (for example
`September-2026 Detailed webinar`, month in the server's time zone: set `TZ`), and each meeting is one
toggleable Heading 1 titled `Sep 30 · <meeting name>` (the start time is added when two meetings share a
name and date) holding the notes, newest meeting first by start time whatever order notes
finish in. The **Copy notes to Notion automatically** switch only governs a meeting's *first* copy. A meeting
that is already in Notion is always kept in step when its notes are regenerated, whatever that switch says:
the same note type updates the existing toggle in place, another note type moves it to the other type's page
(insert, then delete the old block), and a note type with no Notion page removes the old toggle and marks the
meeting not in Notion with a note. Renaming the meeting renames the heading. Deleting a meeting
(or purging it from Recently deleted) never touches Notion. Split and combine create new meetings
whose notes are copied as they finish; the original meetings' toggles are left where they are. The
meeting view shows the Notion destination at the top, beside the note type picker, as a path
(`Notion: Webinars › September-2026 Detailed webinar`, each part a link; where the selected note type *would*
save before the first copy) with the state (In Notion with **Open**, Sending…, or Failed with the reason and
**Retry**) and a **Send to Notion** button. A parent page's title is looked up once and kept in the Notion state
file (shown as "Parent page" if unknown). The meetings list shows a small **In Notion** (links to the toggle),
**Sending…** or **Notion failed** chip beside each row's notes badge, read from local state with no Notion calls;
it can also send a selection, and
each note type's **Copy existing notes** button backfills meetings that have no copy yet. The token is stored
in `<data>/notion/token` (mode 0600), never in `settings.json`, and is never returned by any API. API:
`GET /v1/notion`, `GET /v1/notion/parents`, `PUT|DELETE /v1/notion/token`, `POST /v1/notion/test`, `GET|POST /v1/notion/backfill`,
`GET|POST /v1/sessions/{id}/notion`; settings keys `notion_auto_copy` and `notion_parents`. The agent API
shows `notion: {state, url, error}` on a meeting and its notes and has `POST /api/v1/meetings/{id}/notion`
(MCP: `meeting_notes_send_to_notion`, write scope).

**Appearance** is System (follows the browser), Light or Dark; the choice applies
immediately, is stored on the server (`appearance` in `settings.json`), and can also be
switched from the sidebar. The UI uses self-hosted Inter (SIL OFL, `server/static/fonts/`).

Settings also controls the optional meeting-notes review provider. Choose
**Disabled**, **Codex / ChatGPT**, **Claude (subscription)**, or **Ollama
(local)**. Reviews are not created automatically unless you turn on **Automatically build
meeting notes for new meetings** (off by default; applies only to newly transcribed meetings, never to a re-transcription). Otherwise open a meeting and
select **Build Meeting Notes**. The notes view is a single Markdown-oriented
document with populated sections first and empty sections at the bottom; use
**Download .md** to save the complete document.
For Codex, use **Connect ChatGPT** in Settings to complete the one-time device
sign-in; credentials stay in the bridge volume. The model picker shows the
models available to the connected ChatGPT account (or can use the account
default). For Claude, start the bridge with `--profile ai`, click **Connect
Claude**, open the sign-in link it shows, approve access, then paste the code
you're given back into Settings. The login persists in the
`meeting-notes-claude` volume, so recreating the bridge does not require
signing in again; generation runs against your Claude Pro/Max subscription
limits rather than API billing. Choose Sonnet, Opus, Haiku, or the account
default model. For Ollama, enter the base URL and load the models currently
installed on the reachable server.

On Home, click a live meeting to open its full-screen transcript. The transcript
pane is scrollable and preserves your position while new text arrives. A
meeting name can be changed while that meeting is still recording, but not
after it ends.

Multiple recorder clients may connect at the same time. Their live audio and
saved files remain isolated by globally unique session IDs; CPU transcription
work is serialized and queued so accepting several streams does not require
loading several copies of the model.

For a server whose application data is covered by host backups, set
`MEETING_NOTES_DATA_MOUNT` and `MEETING_NOTES_MODELS_MOUNT` in `docker/.env` to
absolute host paths. Recordings are separate: set
`MEETING_NOTES_MEDIA_MOUNT` to the host path where audio should live. The
container uses `/data` for settings, metadata, the index, jobs, and reviews,
and `/media` for session audio. `MEETING_NOTES_SERVER_ADDRESS` seeds the public
address embedded in client installers on the first start.

If `MEETING_NOTES_MEDIA` is not set, the server keeps the legacy single-root
layout for backwards compatibility. When an existing deployment is moved to a
separate media root, startup migrates only audio artifacts (`.wav`, `.raw`,
range sidecars, imported source files, and upload scratch files); session
metadata, timing logs, transcripts, settings, jobs, reviews, and the index stay
on the application-data volume. Back up both volumes before changing mounts,
and verify the migration before removing the old volume.

### Agent access (REST + MCP)

Agents such as Claude Code read meetings, notes, transcripts, action items and
decisions through per-agent API keys, separate from `MEETING_NOTES_TOKEN`
(a key never opens the website or the recorder API, and the shared token never
opens the agent API). Every data endpoint needs a key, and keys can read and,
if you allow it, build notes and rename meetings; nothing can delete.

Create a key in **Settings → AI access** (shown once, revocable there). The
page also gives you the ready-to-paste Claude Code command:

```
claude mcp add --transport http meeting-notes http://meeting.lan/mcp --header "Authorization: Bearer mnk_..."
```

To use curl instead: `curl -X POST http://meeting.lan/v1/agent-keys -H "Authorization: Bearer $MEETING_NOTES_TOKEN" -H "Content-Type: application/json" -d '{"name":"Claude Code","scopes":["read"]}'`
(add `"write"` to `scopes` to allow writes), then call `/api/v1/...` with
`Authorization: Bearer mnk_...`. Discovery needs no key: `/api/v1/manifest`,
`/llms.txt` and `/api-docs.md`. The MCP endpoint needs the `mcp` package (part
of the server extra); without it the REST API still works.

### Recorders page (live presence and remote control)

Every running recorder keeps one authenticated websocket open to the server. **Recorders** in the web UI lists
the ones open right now: device, version, status (Idle / Recording with a live clock / Finishing), both level
meters, devices, warnings and the upload queue, updating live. From there you can start (with a name) and stop a
recording, rename it, mute or unmute either side, refresh devices, retry uploads, check for or install an update
(never while recording) and answer a detected-call prompt or a "meeting seems over" suggestion. It is not a client
manager: nothing is stored, and a recorder disappears the moment its app closes and returns when it reopens.

The recorder's Settings has **Allow control from the server** (on by default); when off, the recorder still
shows up but refuses every command. The recorder shows a brief notice ("Stopped from the server") and logs each
command. Only the web login can command a recorder: agent API keys cannot. Older recorders do not appear.

**Recordings** on each recorder card opens a panel listing every recording in that computer's save folder, newest
first, with length, size and one status: Uploaded · transcript ready (green), Uploaded · transcribing or queued,
Uploading 40% and Waiting to upload (blue), Upload failed: reason and Can't upload (red), Not on server, In server
trash, Partly uploaded and Uploaded · transcription failed (amber). A summary line counts them ("42 recordings · 2
not on server · 1 failed"), and you can search, filter by status, select several, **Re-upload** them (the same queue
path as the recorder's own Re-upload window), **Delete from this computer** (Recycle Bin / Trash, with a red warning
when the server has no copy, never the recording in progress or one that is uploading) or **Open on server**. If the
recorder goes offline the panel says so; if the same computer comes back (a restarted app has a new instance id),
the panel follows it by device name and OS and reloads, unless two same-named computers make that a guess.
The recorder shows a notice and logs each request, and "Allow control from
the server" covers these commands too.

### Client log uploads

The Windows client's **Logs** window can **Send to server**: a redacted zip is
posted to `/v1/client-logs` with the client's token and stored under
`<data>/client-logs/<computer>/` (newest 20 per computer, 25 MB max each).
Settings → Client logs lists them with download links.

### Optional Codex/Claude review bridge

The Compose file also defines an isolated `meeting-notes-bridge` worker. It
polls the server's review queue and runs the locally authenticated Codex CLI
or Claude Code CLI, depending on the provider chosen in Settings; the web
server never receives either CLI's credentials, and the bridge has no
published port. The bridge only gets `MEETING_NOTES_TOKEN` so it can call the
internal server URL (`http://meeting-notes-server:8000`).

After creating `docker/.env` with the same `MEETING_NOTES_TOKEN` used by the
server, build the worker:

```bash
cd docker
docker compose --profile ai build meeting-notes-bridge
docker compose --profile ai up -d meeting-notes-bridge
```

For Codex, perform the one-time ChatGPT subscription login inside its
persistent volume:

```bash
docker compose --profile ai run --rm --no-deps meeting-notes-bridge codex login --device-auth
```

Follow the device-auth URL and code printed by `codex login`. The login is
stored in the `meeting-notes-codex` volume, so recreating the worker does not
require logging in again. Do not put `OPENAI_API_KEY` or other Codex
credentials in `docker/.env`; this deployment is intended to use the ChatGPT
subscription login.

For Claude, select **Claude (subscription)** in Settings, click **Connect
Claude**, open the link it shows in your own browser, approve access, and
paste the resulting authorization code back into the Settings page (there is
no separate CLI command -- the login runs entirely through the bridge's
control API). The login is stored in the `meeting-notes-claude` volume, so
recreating the worker does not require signing in again. Do not put
`ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN` in `docker/.env`; this
deployment is intended to use the Claude Pro/Max subscription login, and
usage is subject to that subscription's limits rather than API billing.

Queue a review from the Meeting Notes UI, then inspect the worker with
`docker compose logs -f meeting-notes-bridge`.

### Running it all on one machine

The server split is optional. Everything still works standalone through the CLI,
which transcribes locally:

```bash
pip install -e '.[whisper]'
meeting-notes record --name standup
meeting-notes transcribe recordings/<session>
```

## Standalone command-line quick start

```bash
meeting-notes doctor                  # verify capture will actually work
meeting-notes devices                 # list microphones and system-audio sources
meeting-notes models --download base.en  # one-time, before your first meeting
meeting-notes record --name standup   # Ctrl+C to stop
meeting-notes transcribe recordings/2026-09-20_14-30-00_standup
```

**Run `doctor` before your first real meeting.** It catches the failure modes
that are otherwise invisible until afterwards.

### macOS client from the server UI

For Apple silicon Macs on macOS 13 or newer, open **Install client agent** in the
web UI (or just run this in Terminal; no password, no `sudo`):

```bash
curl -fsSL http://meeting.lan/install/mac.sh | bash
```

The script downloads `MeetingNotes-macOS.zip` from the server, verifies its size
and SHA-256 against `/install/client-manifest-macos.json`, swaps
`~/Applications/Meeting Notes.app` in safely (the old copy is restored if
anything fails), keeps your settings, token and recordings, writes the server
address into `~/.meeting-notes/config.json`, and opens the app. Running it again
updates the app, and the in-app **Update available** button does the same.
The operator publishes the app by copying the build (see
[Desktop builds and releases](#desktop-builds-and-releases)) to
`<data>/client/MeetingNotes-macOS.zip` on the server (the Windows build goes
next to it as `MeetingNotes-Windows.zip`).

## Platform setup

### Windows — nothing to install

Windows can loop back any output device through WASAPI. `record` finds it
automatically. No driver, no admin, no rerouting.

### macOS 13+ — no driver, one permission

macOS has no loopback *device*, but ScreenCaptureKit can hand an app the mix of
everything the Mac is playing. The packaged **Meeting Notes.app** uses it for the
system-audio ("Them") track: no BlackHole, no Multi-Output Device, no admin, and
your speakers and headphones are left alone. Meeting Notes' own sounds are
excluded, and only audio is used (the tiny video stream ScreenCaptureKit
requires is thrown away).

Two one-time permissions, both under **System Settings → Privacy & Security**:

1. **Microphone** — macOS asks the first time you record.
2. **Screen & System Audio Recording** — start a recording once; macOS shows
   its prompt (or open the pane and switch **Meeting Notes** on), then **quit
   and reopen** the app. macOS calls it "screen" recording, but Meeting Notes
   never looks at the screen.

If system audio is not allowed, the app says so ("Allow Screen & System Audio
Recording in System Settings → Privacy & Security") and records your microphone
only. `meeting-notes doctor` reports the permission state.

macOS ties each grant to the *specific executable*: the packaged app
(`lan.meeting.notes`), a venv `python` and a pyenv shim are separate identities,
so granting one does not grant the others. The app is signed ad-hoc with an
identifier-based requirement so grants survive updates.

**Older macOS, or permission denied and no way to grant it:** install
[BlackHole 2ch](https://existential.audio/blackhole/) (a `.pkg`, admin once),
create a Multi-Output Device with BlackHole plus your speakers in Audio MIDI
Setup, and set that as the system output. Meeting Notes still finds BlackHole
and uses it whenever ScreenCaptureKit is unavailable or not allowed. Setting
output to BlackHole *alone* records fine but you will not hear the meeting;
`doctor` warns about that state.

## How it works

Two threads, one per track, each writing mono 16-bit PCM straight to disk.

**Files are crash-safe.** Audio is appended to a headerless `.raw` file while
recording and wrapped into a `.wav` only at the end. A standard WAV writer
patches its length fields in `close()`, so a process killed mid-meeting leaves
an unreadable file — a real risk over a two-hour call. If a session does die,
`meeting-notes repair <dir>` rebuilds the WAVs from the raw data.

**Clocks are tracked, not assumed.** Two capture devices run off two different
crystals, so "48000 frames" means slightly different durations on each, drifting
apart over a long meeting. Each track logs `(frames, monotonic_time)` pairs once
a second, and the transcript merge maps frame positions onto real time by
interpolating that log. Measured drift is reported in `session.json` as ppm.

**A dead device does not end the session.** If a track stops delivering audio
for 30 seconds — a Bluetooth headset dropping, a USB mic unplugged — a watchdog
restarts it and pads the lost stretch with silence, so frame positions keep
matching wall-clock time and every timestamp after the gap stays correct. The
other track is unaffected. Gaps are recorded in `session.json` and marked in the
transcript rather than silently looking like nobody spoke.

That last part is deliberately not "kill the thread and retry": a thread blocked
inside a native `record()` call cannot be interrupted from Python at all, so
recovery abandons it and starts a fresh one instead of waiting.

**Devices are picked up automatically.** The desktop client re-scans for
microphones and speakers every few seconds (and immediately when Windows reports
a device change), so a headset switched on after the app opened just appears in
the window; unless you pinned a device, it also follows the system default.
"Refresh audio devices" still exists but is no longer needed.

- **Start without a microphone (or without system audio).** Recording begins with
  what is there and a red banner says so ("No microphone found -- you are not
  being recorded. Connect one and it will be added automatically.").
- **A device that appears mid-recording is attached on the spot.** The track's
  file opens with silence from the meeting start up to that moment (logged as a
  `late-attach` gap in `session.json` and the timing log), so it is full length
  and lines up with the other track; the transcript shows the silent stretch as
  lost audio, not as nobody speaking. The banner turns green ("Microphone
  connected at 00:03:12 -- recording you from now on") and fades.
- **A device that vanishes mid-recording does not stop the session.** The gap is
  marked, the red banner returns, and the track re-attaches when the same device
  (or else the current default) is available again. A healthy device is never
  swapped just because the system default changed.

## Output

```
recordings/2026-09-20_14-30-00_standup/
  mic.wav              your voice
  system.wav           everyone else
  mic.timing.jsonl     frame -> wall-clock log
  system.timing.jsonl
  session.json         devices, rates, gaps, drift, errors
  transcript.md        after `transcribe`
  transcript.json
```

At 48 kHz this is roughly 345 MB per hour per track. `--rate 16000` cuts that to
about 115 MB per hour with no real loss for transcription, since Whisper
resamples to 16 kHz internally anyway.

## Commands

| Command | What it does |
|---|---|
| `devices` | List microphones and system-audio sources |
| `doctor` | Check capture works; probe levels; catch the macOS output trap |
| `record` | Record until Ctrl+C |
| `transcribe <dir>` | Produce a merged, speaker-labeled transcript |
| `models` | List or pre-download transcription models |
| `upload` | Drain the upload queue headlessly (`--list`, `--once`) |
| `repair <dir>` | Rebuild WAVs from `.raw` after an unclean exit |

Useful `record` flags: `--name`, `--mic`, `--system`, `--rate`, `--save-config`,
`--mic-only`, `--system-only`, `--stall-timeout`.

By default, if only one of the two tracks can be opened, `record` refuses to
start — recording half a conversation and discovering it afterwards is an
expensive mistake. Pass `--mic-only` or `--system-only` to do it deliberately.

## Turning the audio into text

The server (and the standalone `transcribe` command) transcribes **locally** with [faster-whisper](https://github.com/SYSTRAN/faster-whisper).
No audio goes to a third-party service, there is no API key, and it works
offline once a model is cached.

```bash
pip install -e '.[whisper]'
meeting-notes models --download base.en     # one-time, do this before a meeting
meeting-notes transcribe recordings/2026-09-20_14-30-00_standup
```

### Choosing a model

Whisper is not a large language model; even the biggest option here is under
2B parameters, and the default is 74M. Install pulls ctranslate2, not PyTorch,
so it is about 250MB of dependencies rather than several gigabytes.

| Model | Params | Download | CPU speed¹ | RAM |
|---|---|---|---|---|
| `base.en` (default) | 74M | ~145 MB | ~8-15x realtime | ~0.7 GB |
| `small.en` | 244M | ~480 MB | ~3-6x realtime | ~1.2 GB |
| `large-v3-turbo` | 809M | ~1.6 GB | ~1-2x realtime | ~2.5 GB |

¹Rough, for an 8-core laptop CPU at int8. "8x realtime" means an hour of speech
takes about seven minutes. Any other faster-whisper model name also works
(`tiny.en`, `medium.en`, `large-v3`, `distil-large-v3`); these three are just
the ones worth defaulting to on a CPU.

```bash
meeting-notes transcribe <dir> --model small.en
meeting-notes transcribe <dir> --model small.en --save-config   # remember it
```

`large-v3-turbo` is a good accuracy/speed compromise and the only one of the
three that handles non-English audio — `base.en` and `small.en` are
English-only and will decode other languages *as English*, confidently and
wrongly, rather than failing. Note it is also hosted by a third party
(`mobiuslabsgmbh`) rather than by Systran like the others.

### Why it is faster than you would expect

Voice activity detection strips silence before anything reaches the model. Each
of your tracks is mostly silence — your mic while they talk, their audio while
you talk — so a one-hour meeting is nowhere near two hours of decoding. It also
means Whisper never sees dead air, which is where it is most prone to inventing
text.

VAD reports timestamps in **original** audio time, not silence-compressed time,
so the two-track merge stays correct. That is verified empirically in
`tests/test_vad_alignment.py` against real speech, not just assumed.

### Practical notes

- **Apple Silicon has no GPU acceleration here.** CTranslate2 has no Metal
  backend, so a Mac runs on CPU regardless of the chip. Windows can use CUDA if
  you have an NVIDIA card.
- **Interrupting is safe.** Ctrl+C keeps whatever tracks already finished, and
  the transcript is rewritten after each track completes.
- **It is offline-first.** A cached model is used without contacting Hugging
  Face, so transcribing on a plane works.
- **Memory** peaks around 1GB for a 2-hour track: the audio is decoded in full
  before chunking, roughly 460MB per hour.
- Transcription is CPU-heavy. `record --transcribe` chains it immediately after
  a meeting, which is convenient but will make the machine sluggish for minutes;
  `--threads N` limits it.

Useful `transcribe` flags: `--model`, `--compute-type`, `--device`,
`--beam-size`, `--language`, `--threads`, `--no-vad`, `--force`,
`--save-config`.

### When the server is unreachable

Recording never depends on the server. A finished meeting is queued in
`<save folder>/.upload-queue` and uploaded by a background worker that runs
whenever the recorder app is open — so a meeting recorded offline uploads the
next time you open the app, with no action from you. Pending and failed uploads
are shown in the app's status line.

`meeting-notes doctor` reports whether the server is reachable, whether its
protocol version matches, whether your token is accepted, and how many sessions
are waiting.

### Adding another backend

The registry takes any object with `transcribe(wav_path, track) -> list[Segment]`;
call `register("name", Factory)`. A cloud backend could drop in without touching
anything else.

## Tests

```bash
.venv/bin/python -m pytest
```

The whole suite runs with no audio hardware, because `soundcard` sits behind the
`AudioSource` protocol and tests drive a fake. That is what makes it possible to
test the things that actually matter — a device wedging forever, a device
vanishing mid-meeting, a clock running fast — which are nearly impossible to
trigger on purpose with real hardware.

Hardware behavior that genuinely cannot be faked is listed in
[manual-testing.md](manual-testing.md).

### Client compatibility

The server keeps working with the current Windows app and the five releases
before it. Apps report their version to the server (Recorders page, while they are open); an app older than that window is asked to update before it can
start a new upload, and never loses a recording. The promise is enforced by the
contract tests in `tests/compat/` (see its README, including how to add a
release).

## Limitations

- Not real-time. Record first, transcribe after.
- `base.en` and `small.en` are English-only; use `large-v3-turbo` otherwise.
- Remote participants are all labeled `Them` by default. Optional (experimental) pyannote
  diarization can label them `Them 1`, `Them 2`, and so on (see below).
- macOS system audio on 13+ uses ScreenCaptureKit and needs the Screen & System
  Audio Recording permission; on older macOS it needs BlackHole. The macOS build
  is Apple silicon only and signed ad-hoc (not notarized by Apple), so it is
  installed with the script above rather than opened from a browser download.

## Optional remote-speaker diarization (experimental, hidden)

The settings section for this is currently hidden in the web UI; the setting
stays in `settings.json` and the `/v1/settings` API and is off by default.
The server can distinguish speakers inside the mixed system track using the
open-source pyannote Community-1 pipeline. This is opt-in because it adds a
large PyTorch dependency and requires accepting the model's Hugging Face terms.

1. Accept the terms for `pyannote/speaker-diarization-community-1` and create a
   Hugging Face token.
2. Put these values in `docker/.env`:

   ```text
   INSTALL_DIARIZATION=true
   MEETING_NOTES_DIARIZATION=true
   HUGGINGFACE_TOKEN=hf_...
   ```

3. Rebuild with `docker compose build` and start the server. Speaker-count
   limits and the model name can then be changed on the server Settings page.

Without those settings, behavior and dependencies are unchanged.

## Desktop builds and releases

The Windows client is built locally with Nuitka into a self-contained folder and
zipped as `MeetingNotes-Windows.zip`; the server hands that zip to machines that
use its Install page (put it in `<data>/client/`, see the README). The exact
Nuitka command is in the README's Development section and in
`.github/workflows/release.yml`, the only workflow in the repo. It runs when a
`v*` tag is pushed (GitHub Actions is disabled for this repository, so builds
and releases are done by hand; the workflow documents the build steps).

The macOS app cannot be built on Windows. On a Mac (Apple silicon, Xcode Command
Line Tools, no sudo needed) run `tools/build_macos.sh`: it sets up a user-space
Python 3.13 venv with `uv`, compiles the app with Nuitka
(`--macos-create-app-bundle`), writes the Info.plist keys (microphone and screen
capture usage strings, bundle id `lan.meeting.notes`, minimum macOS 13), signs it
ad-hoc, runs `--smoke-test` on the binary and produces
`MeetingNotes-macOS.zip` next to `Meeting Notes.app`. Upload that zip to
`<data>/client/` on the server. Manual permission checks are in
[manual-testing.md](manual-testing.md).

The client's bundled fonts (Barlow and Barlow Condensed, SIL OFL, in
`meeting_notes/client/ui/fonts/`) are package data. The Nuitka command therefore
passes `--include-package-data=meeting_notes` next to
`--include-package=meeting_notes`; keep both flags in any local build, or the
packaged app silently falls back to the system font.

## Recording other people

Recording a conversation without telling the other participants is illegal in
many places, including every two-party-consent jurisdiction. Tell people they
are being recorded. This tool captures only your own machine's audio and does
nothing to hide itself.

## License

MIT. See [LICENSE](LICENSE).
