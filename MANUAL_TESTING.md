# Manual testing checklist

The automated suite covers everything that can be faked. These cannot be, and
need a real machine with real devices. Run before relying on this for a meeting
that matters.

## Both platforms

- [ ] `meeting-notes doctor` reports all checks passing.
- [ ] `meeting-notes devices` lists your real microphone and a system source.
- [ ] Start `meeting-notes record --name smoke`, play a video and talk at the
      same time for ~30s, Ctrl+C.
  - [ ] Ctrl+C stops within about a second, no traceback, no hang.
  - [ ] `mic.wav` contains your voice and **not** the video.
  - [ ] `system.wav` contains the video and **not** your voice.
  - [ ] Both files are the same length, give or take a block.
  - [ ] `session.json` shows plausible drift ppm (single or double digits) and
        no unexpected gaps.
- [ ] During recording, both level meters move when the corresponding source
      makes sound.

## Windows specific

- [ ] Works with no driver installed and without running as Administrator.
- [ ] Recording continues, writing silence, through a stretch where nothing is
      playing at all (WASAPI stops delivering buffers when the endpoint idles).
- [ ] Switch the default output device mid-recording; confirm the session
      survives or restarts the track rather than dying.

## macOS specific

- [ ] With BlackHole installed but output set to **BlackHole alone**, `doctor`
      warns that you will not hear the meeting.
- [ ] With a Multi-Output Device selected, you can hear the audio *and*
      `system.wav` captures it.
- [ ] First run triggers the microphone permission prompt; after granting,
      `doctor`'s permission check passes.
- [ ] Repeat the permission check using a different Python (system vs venv vs
      pyenv shim) to confirm the per-executable TCC behavior is explained
      correctly.

## Failure modes

- [ ] Unplug a USB microphone mid-recording. The session must keep the other
      track running, log the error, and finalize a playable file.
- [ ] Disconnect Bluetooth headphones mid-recording, then reconnect. The
      watchdog should restart that track and the recording should continue.
- [ ] `kill -9` the process mid-recording, then run `meeting-notes repair <dir>`
      and confirm both WAVs open with the expected duration.

## Devices appearing and disappearing (Windows)

- [ ] Idle, with the app open: switch a Bluetooth/wireless headset on and off. The
      "You:" device name in the top right updates within a few seconds, with no
      click on "Refresh audio devices". Change the Windows default microphone and
      confirm it follows (unless a microphone is pinned in the config).
- [ ] Switch the headset **off**, press **Start recording**. Recording starts, a
      red banner reads "No microphone found -- you are not being recorded...",
      and the "Mute you" button is disabled.
- [ ] Keep recording, talk, then switch the headset **on**. Within a few seconds
      the banner turns green ("Microphone connected at 00:0x:xx -- recording you
      from now on"), the "You" lane starts moving, and the green banner fades
      after about 25 seconds.
- [ ] Stop and open the transcript on the server: your speech appears at the
      right times relative to the other side, and the stretch before the headset
      came on is marked as lost audio. `session.json` lists `attached_late` for
      the mic track with a `late-attach` gap.
- [ ] Mid-recording, switch the headset off again: red "Microphone disconnected
      at ..." banner, the meeting keeps recording; switch it on and the track
      re-attaches (green "reconnected" banner) with a gap in between.
- [ ] While recording with a working headset, change the Windows default
      microphone. The recording must keep using the headset.
- [ ] Repeat with no speaker/loopback device to see the "Can't hear the meeting"
      banner.

## Recorder UI

- [ ] `meeting-notes-ui` opens; both device names show in the top right.
- [ ] Settings: change the save folder, save, record, and confirm the session
      lands in the new folder.
- [ ] Both waveform lanes move independently: talk (green moves), play a video
      (blue moves). A lane that stays flat while its source makes noise is the
      bug this display exists to catch.
- [ ] During a recording, toggle **Mute you** and **Mute them** separately.
      Confirm the selected track writes aligned silence, the other track keeps
      recording, live preview stays connected, and each button changes to its
      matching Unmute label. Confirm both controls reset after stopping.
- [ ] Use **Upload recording** in the desktop client with each supported format.
      Confirm the UI remains responsive while uploading, reports the server job,
      and the saved transcription appears in the web UI.
- [ ] Stop, and confirm the status line names the saved folder.
- [ ] "Open folder" opens the right directory on both Mac and Windows.

## Meeting detection (Windows)

- [ ] With the client open (or minimized) and idle, join a Teams call. Within a
      few seconds a "Teams call detected" card appears bottom-right with a
      suggested name. Teams names come from window titles and may fall back to
      "Teams call <time>"; that is expected.
- [ ] Edit the name, click **Record**: recording starts under that name.
- [ ] Leave the call. About 20 seconds later the recording stops itself and the
      status line reads "Call ended - recording stopped and queued."
- [ ] Repeat with Zoom and with Google Meet in Chrome/Brave/Edge (Meet names
      should use the meeting title or code).
- [ ] Click **Not now**: no second card for that call. Ignore a card for a
      minute: it disappears.
- [ ] Start a recording manually, then join/leave a call: no card, and the
      recording is never auto-stopped. Stop a prompt-started recording by hand
      mid-call: no new card.
- [ ] Untick the Settings options: no card / no auto-stop respectively.
- [ ] Start a recording by hand, join a call, then leave it and keep the system
      quiet: after the grace a "Meeting seems to have ended" card appears with
      **Stop recording** / **Keep recording**. Wait several minutes: the recording
      is never stopped by itself. **Keep recording** hides it for that call;
      joining another call in the same recording lets it return.
- [ ] Start a recording by hand with no call at all (in-person meeting, or just
      silence) and stay quiet for 5 minutes: a "No audio for 5 minutes" card
      appears. Speak: it disappears. Untick "Suggest stopping when a meeting
      seems over": neither card appears.

## Client updates (Windows)

- [ ] Publish a newer client on the server: the client (idle, "Check the server for
      client updates" on) shows "Update available: x.y.z" with **Update now**. It
      never installs by itself, also with `"auto_update": true` left in an old
      `config.json`.
- [ ] Click **Update now** during a recording: it tells you to stop first. Stop,
      click it again: the installer is downloaded, verified and launched.
- [ ] Serve the manifest with `notes_url` (or `notes`): a **What's new** link
      appears and opens it.
- [ ] Make the server answer HTTP 426 (or set the manifest `min_client_version`
      above the client): the red "no longer supported" banner appears with
      **Update now**; queued recordings stay queued.
- [ ] The server log (or a proxy) shows `X-Meeting-Notes-Client` on uploads, the
      token check, the manifest request and the live-stream connection.

## Docker image

The default image has been built and its health, warning, and volume-backed
settings persistence verified on Windows/Docker Desktop. Repeat these checks
on the intended server host before release:

- [x] `cd docker && docker compose build` succeeds.
- [x] `docker compose up -d`, then `curl http://localhost:8000/health` returns
      `{"status":"ok",...}`.
- [x] `docker compose logs` shows the MEETING_NOTES_TOKEN warning when no token
      is set, and does not show it once one is.
- [ ] Stop and restart the container; a session uploaded before the restart is
      still there (the /data volume persisted).
- [ ] Confirm the `/data` volume contains settings, metadata, index, jobs, and
      reviews, while `/media` contains recording/session audio. Set
      `MEETING_NOTES_DATA_MOUNT`, `MEETING_NOTES_MEDIA_MOUNT`, and
      `MEETING_NOTES_MODELS_MOUNT` to separate host directories and verify each
      mount receives only its documented data.
- [ ] Start a copy of a pre-0.5 deployment with `MEETING_NOTES_MEDIA` pointing
      at a new empty media directory. Confirm startup moves only audio files,
      raw/range sidecars, imported sources, and upload scratch files; metadata,
      timing logs, transcripts, jobs, reviews, settings, and the index remain
      in the data directory. Verify sessions and transcripts before retiring
      the old volume.

## Client and server together

- [ ] Start the server: `cd docker && docker compose up -d`, then
      `curl http://<server>:8000/health`.
- [ ] Set the server URL in Settings; record; confirm live preview text appears.
- [ ] After stopping, confirm the transcript appears in the session folder
      within a few minutes.
- [ ] WITH THE SERVER STOPPED: record a meeting. It must record normally, warn
      that the server is unreachable, and queue the session. Start the server
      and confirm the queued session uploads and its transcript appears without
      you doing anything.
- [ ] Mid-recording, pull the network cable / disable wifi for 30s then restore.
      The recording must be unaffected and the live preview should resume.
      Verify the final transcript covers the whole meeting including the outage.
- [ ] Set a token on the server (MEETING_NOTES_TOKEN) and confirm a client with
      the wrong token is rejected and one with the right token works.
- [ ] Two machines recording to the same server at once both get transcripts.
- [ ] Long meeting (1h+) with one track mostly silent: watch the server's memory
      usage stay flat rather than climbing.

## v0.5.0 web UI, uploads, pipeline status, and meeting-notes review

- [ ] Fix the installer page layout: at some window sizes, the download buttons
      overlap nearby text. Confirm the buttons remain clear of the instructions
      at narrow and wide widths.

- [ ] On Home, choose a short recording in each supported family that is
      available on the test machine (WAV, MP3, M4A/MP4, FLAC, OGG/OGA, Opus,
      AAC, and WebM). Confirm the upload card accepts it, shows byte progress,
      and redirects to the saved transcription after the server accepts it.
      Confirm the server's ffmpeg normalization produces a playable transcript
      and that no codec or STT model is needed on the client.
- [ ] Upload an empty file and an unsupported extension. Confirm the UI shows
      a useful failure and does not create a misleading completed transcription.
- [ ] Open a saved transcription while its upload is pending or active. The
      detail checklist must show upload state and percentage, then transcription
      state and percentage, and finally Complete. The status label in the table
      must agree with the detail checklist at every stage.
- [ ] Select several saved transcriptions and exercise **Build Meeting Notes**,
      **Retranscribe**, and **Delete**. Confirm the selection count, select-all,
      confirmation prompt for deletion, partial failures, and table refresh all
      behave correctly. Confirm the old “Queue for review” wording is absent.
- [ ] Open a completed Meeting Notes item. Confirm the professional detail view
      renders one document with populated sections first, muted empty sections
      grouped at the bottom, and action-item owner/due chips where present.
      Select **Download .md**, open the file, and confirm it is one Markdown
      document in the same populated-then-empty order.
- [ ] On the install page, run the displayed one-step PowerShell command from
      a normal (non-Administrator) PowerShell window. Confirm it downloads the
      server-hosted installer, installs the client, preserves existing settings
      on a repeat run, and launches successfully.

- [ ] On Home, click a live meeting card. A full-screen live transcript opens;
      scroll inside the transcript pane. Confirm new text preserves your
      position unless you were already at the bottom.
- [ ] While a meeting is recording, change its name and confirm the new name
      appears in the live card and saved session. After it ends, confirm the
      name is no longer editable.
- [ ] Open a saved meeting and confirm it does not appear in Meeting notes until
      **Build Meeting Notes** is selected. Selecting it twice must not duplicate it.
- [ ] In Settings → Meeting notes AI, choose **Disabled**, **Codex / ChatGPT**,
      and **Ollama (local)**. Save and reload each choice to verify persistence.
- [ ] With Codex selected, choose **Connect ChatGPT**, follow the displayed
      OpenAI device URL/code, and confirm Connected. Test Disconnect as well.
- [ ] With Ollama selected, enter a reachable base URL and model, save, queue a
      review, and confirm the bridge completes it. An unreachable URL should
      show a review error without losing the transcript.
- [ ] Verify the bridge control port is internal-only (not host-published),
      unauthenticated requests are rejected, and credentials are not placed in
      `docker/.env`.

## Transcription

- [ ] `meeting-notes models` lists the three models and their cached state.
- [ ] `meeting-notes models --download base.en` completes, and re-running
      `models` now shows it cached.
- [ ] `MEETING_NOTES_SLOW_TESTS=1 pytest tests/test_real_model.py -v` passes.
      This is the test that actually proves audio becomes text; it could not be
      run in the container this was built in, because huggingface.co is blocked
      there by egress policy.
- [ ] Transcribe a short recording and confirm the progress bar advances and
      the text is accurate.
- [ ] Ctrl+C partway through a two-track transcription; confirm the finished
      track still produced a transcript rather than everything being lost.
- [ ] Disconnect the network with a model already cached, then transcribe.
      It must work offline rather than hanging on Hugging Face.
- [ ] Check timestamps are sane where one person speaks after a long silence:
      that is where VAD timestamp restoration would show up if it were wrong.

## Long meeting

- [ ] A real meeting of 1 hour or more, then transcribe.
  - [ ] Check `[You]` / `[Them]` interleaving at the **start**, **middle** and
        especially the **end** — clock drift shows up at the end.
  - [ ] Disk usage matches expectations (~345 MB/hour/track at 48 kHz).
  - [ ] `session.json` lists no unexplained gaps or degraded tracks.
  - [ ] Transcribe it and time the run, to calibrate expectations for the model
        you chose. Confirm no runaway repeated text (if you see any, try
        `condition_on_previous_text` back on, or a larger model).
