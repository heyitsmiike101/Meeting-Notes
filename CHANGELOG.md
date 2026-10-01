# Changelog

Notable changes per release, newest first. Versions are the packaged client and
server version (`meeting_notes/__init__.py`). Earlier history is in git.

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
