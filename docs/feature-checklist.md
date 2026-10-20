# Feature checklist

Checked items below were verified in the implementation and focused automated
tests on 2026-09-21. Device, browser, and installer walkthroughs remain in
[manual-testing.md](manual-testing.md).

- [x] Saved transcription detail shows upload, transcription, and completion
      status. The table uses the same pipeline data. Pending uploads say
      “Pending end of meeting”; active uploads and transcription show percent.
      The detail view now refreshes during upload as well as transcription.
- [x] Saved transcriptions support bulk Build Meeting Notes, Retranscribe,
      and Delete. The table refreshes after mixed success and reports the
      number of failed actions.
- [x] Replace the user-facing “Queue for review” wording with
      “Build Meeting Notes.”
- [x] Meeting Notes display one document with filled sections first and
      empty sections last. Download produces one Markdown file in that order.
- [x] Give the Meeting Notes detail page a stronger visual hierarchy. A
      design review recommended a meeting-specific masthead, readable lead
      summary, and clearer section headings; these were applied.
- [x] Home can upload popular recording formats for transcription, including
      MP4 and OGA in the file chooser and help text.
- [x] The Windows client can upload a recording and mute each source while
      recording.
- [x] The Upload dialog also takes a transcript file (.txt/.vtt/.srt) or pasted
      text (no audio, nothing transcribed); Home has the same under Add a meeting.
- [x] Client Settings is a sidebar of pages (General, Audio, Recordings, Server,
      Remote control, Logs, About); the Logs window is the Logs page.
- [x] The client's status line shows one-off results briefly, then the live status.
- [x] Docker supports separate configurable app-data and audio mounts through
      `MEETING_NOTES_DATA_MOUNT` and `MEETING_NOTES_MEDIA_MOUNT`.
- [x] The install page provides a one-step PowerShell command that downloads
      and runs the server-hosted installer.
- [x] Next version: client mute buttons sit beside their corresponding audio
      waveform lanes.
- [x] Fix the installer page layout: the download button is in normal document
      flow and no longer overlaps text. Checked in the browser at a 780 px
      viewport and through responsive layout tests.

## Version 0.6.0

- [x] A blank-area click or Escape closes full-page meeting overlays; focus and
      page scrolling are restored when they close.
- [x] Saved meeting names and summary titles are editable independently. AI
      titles apply only to the summary, and a stale recording update cannot
      overwrite a user-edited meeting name.
- [x] Saved transcription rows update in place without a five-second page
      flash or a persistent Loading placeholder.
- [x] Transcripts and meeting notes share the same meeting overlay. Transcript
      is the default; Build Meeting Notes shows notes there when requested.
- [x] Settings has sections and an editable, persisted AI workflow. The default
      prompt follows the transcript-grounded decision, action, owner, and due
      date rules supplied for this release.
- [x] Polished loading, retry, installer, and narrow-screen layouts. Focused
      browser checks and automated tests cover the main interactions.

## Version 0.6.1

- [x] Saved table distinguishes transcription from meeting notes status. A
      completed note is labeled “Notes ready” in the list and opens as the
      primary meeting view; transcript and recording details remain available.
- [x] Reviewed Home, Saved Transcriptions, meeting detail, Settings, and
      Install in the browser. Refined responsive list cards, Home badges,
      action visibility, meeting details, and Settings navigation. The notes
      header no longer overlaps its status text.
- [x] Polling updates row status and keeps checkbox focus and selection while
      preserving stable row nodes. Notes status is maintained in the index so
      the library does not scan review files on every refresh.

## Version 0.7.0

- [x] Claude (subscription) is a meeting-notes provider alongside Codex and
      Ollama; sign-in runs through the bridge from Settings.
- [x] Web UI redesigned to the category standard (Linear/Notion/Granola
      level): sidebar + meeting list, notes-first document view with Notes and
      Transcript tabs and a You/Them timeline, Inter, light/dark/system themes
      (Settings > Appearance), compact phone layout with a bottom tab bar.
- [x] "Saved transcriptions" renamed to Meetings; per-note-type automatic notes for new
      meetings; hover Generate on meetings without notes; bulk Delete audio.
- [x] Agent access: per-agent API keys (Settings → AI access), REST under
      /api/v1, MCP server at /mcp, manifest/llms.txt/api-docs.md.
- [x] Client: console restyle, call detection prompt (Teams/Zoom/Meet) with
      auto-stop, wrong-token alert strip and Test connection, full Logs window
      with zip export and upload to the server, recordings kept out of the app
      folder.
- [x] Installer: config read as UTF-8, safe folder swap, never removes an app
      folder holding recordings; a settings change retries every waiting upload.

## Version 0.8.0

- [x] GPU helper: a Windows PC with an NVIDIA graphics card can transcribe (and label speakers) for the
      server. Server settings, helper pack, helper keys with revoke, offer / claim / fallback, recorder
      Settings > GPU helper page, tray icon, start at sign-in. See [gpu-helper.md](gpu-helper.md).
- [x] GPU server: `docker/docker-compose.gpu.yml` runs the server's transcription and speaker labels on its
      own NVIDIA GPU; falls back to the CPU with a log warning if CUDA is not usable.
- [x] Speaker-label settings shown again in Settings > Transcription; a speaker-label failure no longer
      fails a transcript.
- [x] The Notion heading for an unnamed meeting uses the notes title.

## Version 0.8.1

- [x] Settings is split into pages (General; Transcription & speakers; Meeting notes; Notion & AI access;
      Recorders & install) with a left list, deep links (`/settings#recorders`) and one Save for all of
      them. GPU helpers live on Recorders & install; speaker labels on Transcription & speakers. The Recorders
      page has an **Install a recorder** link.
- [x] Settings saves carry a revision: a save made from a stale page is refused with a message instead of
      overwriting newer settings (`PUT /v1/settings` answers 409). Unchecked boxes on other pages no longer
      reset anything.
- [x] Whisper models are named Good, Better and Best (live also Fastest) in every list, with separate
      choices for this server and for GPU helpers (`gpu_model`, `gpu_live_model`). Defaults: Best for helper
      transcripts, Better for helper live.
- [x] Live transcription on a GPU helper: each finished sentence is transcribed on the helper's graphics card
      over an outbound connection, with the server as the fallback when the helper is slow or gone.
      **Send live transcription to GPU helpers** (`live_on_helper`) switches it. A helper keeps serving live
      while its own PC records. See [gpu-helper.md](gpu-helper.md).
- [x] Reasoning effort selects for the Claude and ChatGPT note providers (`claude_effort`, `codex_effort`).
