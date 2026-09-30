# Product

<!-- impeccable:product-schema 1 -->

## Platform

web (server web UI at `http://meeting.lan`, used on desktop and phone) plus a Windows desktop recorder client
(PySide6/Qt, not a web surface).

## Users

One person: the owner (Mike). He records his own work meetings (Teams, Zoom, Google Meet) and personal calls, then
uses the web app afterwards. No other accounts or roles; sign-in is a single shared token.

## Product Purpose

Record a meeting as two clean tracks (your mic and everyone else via system audio), transcribe it on a home server,
and turn it into meeting notes worth keeping. Success means that after any meeting the notes are waiting, accurate, and
easy to act on, and that any past meeting can be found again quickly.

## Positioning

Self-hosted and private: audio, transcripts and notes stay on the owner's LAN server. Dual-track capture labels "you"
versus "them" without diarization guesswork. AI notes come from the owner's own subscription (Claude, ChatGPT/Codex)
or a local Ollama model, never a third-party meeting-bot service.

## Operating Context

- **After a meeting (primary):** open the meeting, read the summary, decisions and action items, copy or download them.
- **Finding a past meeting:** browse or search the meetings library by name and date, open its notes or transcript.
- **Managing:** bulk-build notes, retranscribe, delete audio or meetings, tune settings (transcription model,
  AI provider, retention, auto-generate notes).
- **Live (secondary):** a live transcript preview exists while recording.
- **Remote (secondary):** the Recorders page shows every open recorder live and can start, stop, mute and
  more from the phone. It is presence, not a device manager: a closed app simply disappears.
- Used at a desktop (1920×1080, sometimes a 3440-wide ultrawide) and fully on a phone: every workflow, including
  settings and bulk actions, must work comfortably at phone width.

## Capabilities and Constraints

- Server-rendered HTML with inline CSS/JS from Python (`meeting_notes/server/web.py`); no front-end build step or
  framework. LAN-only, no external CDNs required at runtime (the server may be offline from the internet).
- Terminology: a **meeting** (formerly "saved transcription"/"session") has a recording, a transcript, and optionally
  **meeting notes** (title, summary, notes, participants, key points, decisions, action items).
- Transcription and notes are asynchronous: states include queued, transcribing, done, error; notes can be
  not created, queued, running, done, error.
- The Windows client records, shows a live preview, and uploads; it also offers to record detected calls.

## Brand Commitments

- **Visual direction (owner's choice, 2026-09-29): the category standard, played straight.** A clean, familiar
  productivity app. The craft bar is Linear, Notion and Granola: quiet neutrals, crisp type, subtle borders,
  content-first pages, no themed decoration or metaphor costume.
- **Theme is a user choice:** Light, Dark or System, selectable in Settings on the web app and in the Windows client.

## Evidence on Hand

Real meetings exist only on the owner's server; design and test with realistic seeded data, never real transcripts
in screenshots or commits.

## Product Principles

1. Notes first: once notes exist they are the meeting; the transcript is supporting detail.
2. Retrieval over decoration: past meetings must be findable and scannable in seconds.
3. Nothing is lost: recording and upload failures are visible and recoverable; destructive actions are explicit.
4. Private by default: nothing leaves the owner's server unless he chooses an AI provider for notes.
