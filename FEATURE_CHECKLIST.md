# Feature checklist

Checked items below were verified in the implementation and focused automated
tests on 2026-09-21. Device, browser, and installer walkthroughs remain in
`MANUAL_TESTING.md`.

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
