# Changelog

Notable changes per release, newest first. Versions are the packaged client and
server version (`meeting_notes/__init__.py`). Earlier history is in git.

## 0.7.14
- macOS: **quitting now always releases system-audio capture.** A ScreenCaptureKit stream that was still open when the app closed (a recorder thread stuck mid-stop, a stream whose start timed out and then completed late) could keep macOS's purple screen-recording indicator on after Meeting Notes was closed. Every open stream is now tracked and stopped on quit (Cmd+Q, Dock Quit, last window closed, or interpreter exit), after any recording in progress is saved; a stream that times out or fails while starting is stopped instead of left running, and a macOS-only watchdog force-exits a process that is still alive 8 seconds after quitting began. Closing the window while idle already released everything (the pre-recording meter never opens system audio on macOS); behaviour during a recording and on Windows is unchanged.
- macOS: **closing the lid during a recording.** The app now listens for sleep and wake: on wake, a recording in progress gets a fresh system-audio stream straight away (the gap is padded with silence like any reopen and logged as `system-wake`) and the stream that was open across sleep is stopped while the Mac is awake. A stream whose stop macOS doesn't confirm (common right around sleep) is no longer forgotten: it is stopped again in the background on the next open, on wake, and on quit, so it can't keep the screen-recording indicator on after the recording or the app has ended.
- Web: **pick the Notion page from a tree.** In Settings > Note types, **Save to Notion** now loads the pages the integration can see and shows them as a tree with the same hierarchy as Notion (expand/collapse, search that keeps the ancestors visible, **Refresh**, **None**), instead of asking for a pasted link. **Page not listed? Paste a link** still accepts a link or id. Only pages shared with the integration appear. New `GET /v1/notion/pages` (`?refresh=1` skips the one-minute cache) returns `{connected, items: [{id, title, url, parent, icon}], error}`; database rows and trashed pages are left out, and a page whose parent isn't shared shows at the top level. Web only: not exposed to agents.
- Web: **saving settings keeps your place.** Save settings used to reload the page at the top; it now comes back to the same spot (kept relative to the note type or section you were looking at), with the note types you had open still open, and shows "Settings saved." (or the error) as a toast as well as the banner at the top.
- Web: **delete uploaded client logs.** Settings > Client logs has **Delete** on each bundle and **Delete all** (both confirmed). New `DELETE /v1/client-logs/{computer}/{name}` and `DELETE /v1/client-logs` (`{"deleted": n}`); a computer with no bundles left disappears from the list. The help text now mentions the Mac app too.
- Supported recorders are now 0.7.9 to 0.7.14.

## 0.7.13
- Client: renaming the meeting while it records now renames the saved meeting (the name as it reads at Stop wins), and the name field clears once the recording finishes.
- Server (patch): **updates follow the address the client uses.** `client-manifest.json` and `client-manifest-macos.json` now point `installer.url` at new update-only scripts, `/install/update/client-agent.ps1` and `/install/update/mac.sh`, rendered with the address the request arrived on (hash and size match that script) instead of the saved Server address. A recorder that reaches the server by IP because it cannot resolve the LAN name now downloads the package from, and keeps, that address instead of being switched to the unresolvable saved one. The browser and one-line installers (`/install/client-agent.ps1`, `/install/mac.sh`) still use the saved Server address. Already-installed clients (0.7.7 and later) pick this up with no update, because they follow the manifest's installer URL.
- Windows installer: every run appends timestamped steps, errors and a final OK/FAILED line to `%USERPROFILE%\.meeting-notes\logs\update.log` (kept under 256 KB; included in the Logs bundle). The macOS script already logged under the in-app updater and now documents that it always logs. The client logs the manifest URL, installer URL, sizes, verification result and the reason for any update failure.
- Web: **edit the participants on a meeting summary.** In the notes view, **Edit** beside Participants lets you fix a spelling, remove someone or add someone (Save / Cancel; Escape cancels). Renaming a person also fixes the old spelling in that summary's notes text and action-item owners (whole word, case-sensitive; the transcript and title are never touched), and a meeting already in Notion is re-synced. The first edit keeps the AI's original output (`ai_payload` on the review); regenerating the notes drops the edit and uses fresh AI output. New `PUT /v1/meeting-notes/{id}/participants` (`{"participants": [{"name", "was"}]}` or plain names; max 50, 120 characters each).
- Server: **remembered names.** Every participants save feeds a server-wide glossary (`names.json` in the data folder): the saved names, plus a correction for each real rename. It is appended to the notes prompt of every note type when the bridge fetches it (the stored prompt text is not changed), and the names (never the wrong spellings) are passed to faster-whisper as `hotwords` (or a short initial prompt on older versions; retried without on any error), so names you correct also help future transcripts. Settings > **People and names** lists them with Remove and Clear all (`GET /v1/names`, `POST /v1/names/remove`).
- Web: the Notion bar on a meeting that is in Notion now has a **...** menu (**Notion options**) instead of the Send again button. It holds **Send again** and **Remove this note from Notion** (confirmed in the app's own dialog). Removing deletes the meeting's toggle from its month page in the background (a toggle already gone in Notion counts as removed; a refusal is shown on the meeting) and the meeting is not copied again automatically (auto-copy and the "already in Notion always resyncs" rule stay quiet) until you press **Send to Notion**. New `DELETE /v1/sessions/{id}/notion`; the status gains `state: "removing"` and `opted_out`. Not exposed to agents (the agent API never deletes).
- Server: **combining and splitting meetings now follows in Notion.** The original meetings' toggles are removed, and each new meeting is copied to Notion when its notes finish if any original was in Notion (and not removed by you) or its note type copies automatically. A Notion problem never fails the combine/split. Restoring an original from Recently deleted does not recreate its Notion toggle (use **Send to Notion**); undoing a combine/split removes the new meetings' toggles and sends the restored originals again.
- Supported recorders are now 0.7.8 to 0.7.13.

## 0.7.12
- macOS: self-update logs to `~/.meeting-notes/logs/update.log` (included in the Logs bundle) and tries harder to reopen the app (`open`, `open -a`, then the executable, each verified). The app is still ad-hoc signed, so the update bar now says macOS may ask for permissions again and links to **Install manually** (the server's install guide, macOS section); after launching the update it says to open the app from ~/Applications if it doesn't reopen. `tools/macos_signing_setup.sh` (optional) prepares a stable signing identity; the build uses it only once the certificate is trusted for code signing, otherwise it signs ad-hoc.
- Client: **choose your microphone and speakers** (Settings > Audio > Devices). **Microphone** and **Speakers (what you hear)** each list **Automatic (system default)**, the default, then the connected devices by name. The choice applies to the next recording, the level preview before recording and the device names in the main window; a recording in progress keeps its devices. A chosen device that is not connected stays in the list as "<name> (not connected)" and recording falls back to Automatic (logged once; the main window names the fallback), so a missing device never stops a recording. Saved as `audio_devices` (`{"mic": "", "system": ""}`, `""` = Automatic) in `config.json`; the older top-level `mic` / `system` keys still apply while a side is Automatic. On a Mac, what you hear comes from ScreenCaptureKit and is not a device, so the speakers list is empty (and disabled) unless a virtual loopback driver such as BlackHole is installed.
- Client: new **Auto end** choice **When people say goodbye** (after **When the call ends**; Settings > General > Meeting detection; the default stays **On the hour**). It listens to the server's live preview: when either side says a goodbye ("bye", "see you later", "take care", "have a good one", "thanks everyone", ...) and then both sides are quiet for 20 seconds, the recording stops after a 10-second "Meeting seems to be over" countdown (**Keep recording** waits for a new goodbye; audio coming back closes the countdown and the next 20 seconds of quiet still ends it; a goodbye not followed by quiet within 5 minutes is forgotten). It needs **Live preview** on (Settings > Server) and a reachable server; with no live transcript it never ends the recording. The recorder shows "Auto end after goodbyes and 20 s of silence", and the Recorders page accepts the new `bye` mode. `auto_end: "bye"` in `meeting_detection`.
- Client: new **Auto end** choice **When the call ends** (Settings > General > Meeting detection; the default stays **On the hour**). An auto-recorded call stops once the call is over and its audio has been quiet for the grace time, after the usual 15-second "call ended" countdown (**Keep recording** or **Disable auto end** turns it off for that recording). The recorder shows "Auto end when the call ends", on the Recorders page too.
- Server: **notes are generated automatically per note type.** The global "Auto-generate notes" setting is gone; each note type has its own **Generate notes automatically** switch (Settings > Note types; `auto_notes_types` in `settings.json`, the list of type ids, also in `/v1/settings`; `auto_notes` on each `/v1/note-templates` item). A newly transcribed meeting (and an uploaded transcript) is queued for notes when its tagged note type, else the default type, has the switch on and the AI provider is not **Disabled**; otherwise the Generate button still builds them. Every type starts on (existing servers keep every type on, and a new note type starts on). An old `auto_generate_notes` key is ignored.
- Supported recorders are now 0.7.7 to 0.7.12.

## 0.7.11
- Client: **Start recording automatically when a call starts** (Settings > General > Meeting detection, off by default). A detected Teams, Zoom or Google Meet call is recorded at once under its name, with a short "Recording ... call" card instead of the "Record this meeting?" prompt. An **Auto end** choice appears below it: **On the hour** (stops at the end of the hour the call is in; a call joined in the last 10 minutes before the hour runs to the next one, with a one-minute countdown), **After 30 seconds of silence** (a 15-second countdown after 15 quiet seconds, once anything has been heard), or **Manual only**. While an auto-recorded call runs with an Auto end, the recorder shows it with a **Disable auto end** button (also on the card) that turns it off for that recording. New `auto_record` and `auto_end` (`hour`, `silence`, `manual`) keys in `meeting_detection` in `config.json`.
- Client: **Default note type and a per-meeting Note type select.** Settings > General > Notes sets the default (`default_note_type` in `config.json`; **Server default** if unset). A **Note type** select beside the meeting name (shown when the server has 2+ note types) picks the type for the meeting being recorded, changeable until you press Stop and back to the default afterwards; auto-recorded calls use the selected type (the default unless you picked another beforehand). The choice goes to the server as `meta.note_type` (`session.json`, also on a re-upload), so meetings that record get notes of the right type, saved where that type sends them (for example its Notion page).
- Server: a meeting whose recorder tagged it with an existing note type (`meta.note_type`) now gets notes of that type once transcribed when that type generates notes automatically (an AI provider must be selected). Untagged meetings and unknown ids use the default type. The log says which type was used and why.
- Recorders page: a **Note type** select next to the meeting name (for recorders that advertise the new `note_type` capability) and, for an auto-recorded call, the "Auto end at ..." line with **Disable auto end** (`auto_end` capability). New remote commands `set_note_type` and `disable_auto_end`, new refusal code `no_auto_end`; `GET /v1/recorders` items carry `caps`.
- Build: `tools/build_windows.ps1` no longer has a `-GitHubRelease` switch; builds never touch GitHub (no Actions, no GitHub releases).
- Supported recorders are now 0.7.6 to 0.7.11.

## 0.7.10
- Client (macOS): the Microphone and Screen & System Audio Recording prompts now appear on the first launch instead of the first recording. A moment after the window shows, it asks for the microphone, then (after you answer) for screen & system audio recording, then shows the permissions panel with the fresh statuses; the server-password Settings dialog on a first run waits until the panel is closed. Runs once (`permissions_prompted` in `config.json`), also once on the first launch after upgrading; Local Network is prompted by the server check as before. `MEETING_NOTES_NO_PERMISSION_PROMPT=1` skips all of it.
- Web: every "are you sure" is the app's own dialog; revoking an AI access key, removing the Notion token and copying existing notes to Notion no longer use the browser's confirm pop-up.
- Recorders page: the meter traces play level frames back slightly behind real time (about 1.5 frame gaps, adaptive) and animate at the display rate with interpolation and rounded edges, so they scroll smoothly instead of jumping with each frame.
- Client: a missing server password is now said out loud. With a server set but no password saved, a red strip ("Enter the server password to connect...") shows at once with an **Enter password** button, and the very first start opens Settings on the password field (once; the strip stays until a password is saved). The client now calls it "Server password" everywhere, and a refused control channel (close code 4401) shows the strip immediately instead of five minutes later.
- Client (macOS): a "Meeting Notes needs a few permissions" panel overlays the recorder when Microphone, Screen & System Audio Recording or Local Network access is missing, with the status of each, the exact steps, **Open System Settings** deep links, **Allow microphone**, **Quit and reopen** (bundled app only) and **Check again** (also runs when the window regains focus). **Not now** leaves a "Permissions needed - Fix" strip. It reappears when a recording fails to start. Recording the microphone alone still works without system-audio access.
- Client: after a check that could not reach the server, it looks again after 10 s and 30 s (then every 5 minutes), so a just-allowed Local Network permission or a slow network clears quickly.
- macOS app: declares `NSLocalNetworkUsageDescription` so macOS 15+ shows a proper Local Network prompt for the LAN server (without it a LAN server answered "No route to host"), and bundles `AVFoundation` for the microphone status.
- Supported recorders are now 0.7.5 to 0.7.10.

## 0.7.9
- Recorders page: each recorder is now a remote, a replica of the Windows client's main window in the client's own palette and sizes (header with Upload / History / Settings / "...", record card, meter lanes with Mute you / Mute them, Live preview, status line), and its buttons send the matching remote command. Meetings search also matches the computer a meeting was recorded on, which is what the remote's History button uses.
- Settings: Note types is its own card; "Copy notes to Notion automatically" is now set per note type (the old single switch carries over to every type); "Copy existing notes" says what it does; the Installation button reads "Install guide".
- Meeting view: the note type picker moved into the header next to Copy / Download; the Notion line keeps its own thin bar.
- Recorders page cards and the live transcript views follow the Windows client's recording window: clock and devices, meeting name with Start/Stop, level meters with Mute you / Mute them buttons the same height as their meter, a Live preview of "You: / Them:" lines for that recorder's meeting (in meeting-time order), and the client's status line.
- The server checks GitHub `main` for a newer version and shows an "Update available" notice in the sidebar and on Settings (report only; `MEETING_NOTES_UPDATE_CHECK=0` turns it off).
- Web Settings sections are separate cards, and the section list highlights the right one when you jump to it.
- Install moved from the sidebar into Settings (Installation section).
- Live transcript (web): lines in meeting-time order, the whole transcript in the overlay opening at the newest line, an ended meeting is flagged instead of freezing.
- Client: the Mute you / Mute them buttons fill the height of their meter lanes.
- Supported recorders are now 0.7.4 to 0.7.9 (0.7.3 and older are asked to update before starting a new upload).

## 0.7.8
- Upload a transcript (`.txt`, `.vtt`, `.srt` or pasted text) instead of audio, from the web UI and the desktop client.
- Every note type name is editable, built-ins included.
- Client Settings is a sidebar of pages; the Logs window moved into Settings.
- Notion export fixes found in live testing.

## 0.7.7
- Audio levels shown before recording, in the client and on the Recorders page.
- Copy finished notes into Notion (one page per month and note type).
- Meetings list grouped by day.

## 0.7.6
- Live Recorders page with remote control (start, stop, mute, rename, update).
- Per-recorder Recordings list with status, re-upload and delete-from-computer.
- Split a meeting (with suggested split points) and combine meetings.
- Note types: multiple, editable meeting-note prompts.

## 0.7.5
- macOS client: ScreenCaptureKit system audio, call detection, server-hosted installer and updates.

## 0.7.4
- Server stays compatible with the current client and the five releases before it, enforced by contract tests.
- Updates are never installed automatically; the client shows an **Update now** button.
- Audio devices are picked up automatically, including mid-recording.
- Suggests stopping when a meeting seems to be over.

## 0.7.3
- More robust call-end auto-stop; uploads finalize once and resume after a restart.
- Fix uploads failing with "Access is denied" on Windows.

## 0.7.2
- "Recently deleted" trash with 30-day restore.
- Re-upload saved recordings; optional clean-up of old local recordings.

## 0.7.1
- Fix client updates that closed the app without finishing.
- Transcript segments are built from word timestamps.
- Notes state the substance directly instead of narrating the meeting.

## 0.7.0
- Agent access: REST API and MCP server with per-agent API keys.
- Client log uploads to the server.
- New web UI and client look with Light, Dark and System themes.
- Meeting call detection (Teams, Zoom, Meet) with a record prompt.
- Meetings library opens on notes; optional automatic notes.
- Claude (subscription) added as an AI provider.

## 0.6.1
- Transcription progress and notes status are shown separately; completed notes open first.
- Layout polish across the library, meeting, Home, Settings and Install pages.

## 0.6.0
- Read the transcript and build notes in the same meeting view.
- Rename meetings and summaries independently; the AI workflow prompt is editable.

## 0.5.0
- Upload existing recordings (WAV, MP3, M4A/MP4, FLAC, OGG, Opus, AAC, WebM); the server decodes them with ffmpeg.
- One pipeline status for upload and transcription, bulk actions, and a shareable Markdown notes document.
- Mute either track independently while recording.
