# Changelog

Notable changes per release, newest first. Versions are the packaged client and
server version (`meeting_notes/__init__.py`). Earlier history is in git.

## Unreleased
- Recorders page: the meter traces play level frames back slightly behind real time (about 1.5 frame gaps, adaptive) and animate at the display rate with interpolation and rounded edges, so they scroll smoothly instead of jumping with each frame.

## 0.7.10
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
