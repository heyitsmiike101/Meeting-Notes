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

## macOS specific (BlackHole fallback, macOS older than 13)

Only needed when ScreenCaptureKit is not in play (macOS 12 or older, or Screen &
System Audio Recording refused and BlackHole installed). The main Mac checklist
is "macOS client" below.

- [ ] With BlackHole installed but output set to **BlackHole alone**, `doctor`
      warns that you will not hear the meeting.
- [ ] With a Multi-Output Device selected, you can hear the audio *and*
      `system.wav` captures it.
- [ ] First run triggers the microphone permission prompt; after granting,
      `doctor`'s permission check passes.
- [ ] Repeat the permission check using a different Python (system vs venv vs
      pyenv shim) to confirm the per-executable TCC behavior is explained
      correctly.

## macOS client (Apple silicon, macOS 13+): ScreenCaptureKit, detection, install

Everything the automated suite and the build machine could verify without a
person at the keyboard is already done (unit tests, build, ad-hoc signature,
`--smoke-test`, the installer script's logic). These steps need the permission
dialogs and real audio, so they need the Mac's owner. Do them in order, on a
Mac that has never granted Meeting Notes anything (or reset it with
`tccutil reset All lan.meeting.notes`).

### 1. Install
- [ ] In Terminal: `curl -fsSL http://meeting.lan/install/mac.sh | bash`. It prints
      each step, ends with "Installation complete" and opens Meeting Notes from
      `~/Applications`. The server URL in Settings is already `http://meeting.lan`.
- [ ] Gatekeeper does not block the launch (the script clears the quarantine
      flag). If macOS still says the app "is damaged" or "cannot be opened", note
      the exact wording.
- [ ] On the very first start, Settings opens on **Server password** by itself,
      saying "Paste the same password you use to sign in at http://meeting.lan".
      Cancel it: a red strip "Enter the server password to connect..." with an
      **Enter password** button stays, and Settings does **not** open by itself on
      the next launch. Enter the password and Save: the strip goes away.
- [ ] A **Meeting Notes needs a few permissions** panel covers the recorder card
      (header and strips stay visible). It lists Microphone, Screen & System Audio
      Recording and Local Network with Granted / Not granted / Unknown and the
      steps; each **Open System Settings** opens the right Privacy pane.
- [ ] **Local Network** (macOS 15+): if the server cannot be reached ("No route to
      host" in `~/.meeting-notes/logs/client.log`), the panel marks Local Network
      as not granted. Switch **Meeting Notes** on under Privacy & Security -> Local
      Network; coming back to the window re-checks on its own, and the server
      check passes within about 10 s.
- [ ] **Not now** hides the panel and leaves a "Permissions needed - Fix" strip;
      **Fix** brings it back. **Check again** closes the panel once all needed
      permissions are on. The main window shows `You:` and `Them:` device lines;
      before permission is granted `Them:` may say "Allow Screen & System Audio
      Recording in System Settings ...".

### 2. Permissions (first run)
- [ ] On the very first launch (no `permissions_prompted` in `config.json`),
      macOS asks for the **Microphone** right after the window shows, before any
      recording: Allow. Only then does the **Screen & System Audio Recording**
      prompt appear (never two system dialogs at once), and the permissions
      panel follows with fresh statuses. The server-password Settings dialog opens
      only after the panel is closed (**Not now**, or **Check again** with
      everything on), and not at all when a password is already saved. Relaunch:
      nothing is asked again. An upgraded install gets the permission prompts
      once on its next launch.
- [ ] In the panel, **Allow microphone** shows macOS's Microphone prompt (only
      while macOS has not been asked yet): Allow. Pressing **Start recording**
      asks for Screen & System Audio Recording once, only if the first-run prompt
      never ran.
- [ ] If **Screen & System Audio Recording** was not allowed in the prompt, the app
      appears, switched off, under Privacy & Security -> Screen & System Audio
      Recording: switch **Meeting Notes** on, then press **Quit and reopen** in
      the panel (the app comes back by itself after a few seconds). macOS may
      also say the app needs to quit and reopen.
- [ ] Recording before the second permission is granted must not hang or crash:
      it records the mic and the UI explains what to allow. Note what the status
      line says.
- [ ] After reopening, `Them:` shows "System audio (ScreenCaptureKit)".

### 3. Record a real call (Zoom, Google Meet or Teams)
- [ ] Join a call with a colleague (or a second device) and talk for ~1 minute
      each way. Play a YouTube video for 10 seconds while nothing else is
      speaking, too.
- [ ] Both waveform lanes move: **You** when you speak, **Them** when the other
      side speaks or the video plays. Your own voice does **not** show on the Them
      lane (Meeting Notes' own audio is excluded; the call app's playback of the
      other person is not).
- [ ] Stop. In `~/Meeting Notes/<session>/` there are `mic.wav` and `system.wav`;
      both play back, `system.wav` contains the other participant and no echo of
      you beyond what the call itself plays.
- [ ] The transcript on the server labels lines **You** / **Them** and timestamps
      line up between the two tracks (a phrase you say right after they finish
      appears in order).
- [ ] Headphones plugged in, then unplugged mid-recording: `system.wav` keeps
      recording. Switching the output device mid-recording is fine or recovers
      (note any silence gap; `session.json` lists gaps).
- [ ] Leave the Mac completely silent (nothing playing, call muted) for 60 s
      mid-recording. The Them lane stays flat, the recording is **not** marked
      degraded, and `session.json` has no "delivered no audio" / stall events.
      (If it does, ScreenCaptureKit stops sending buffers during silence: tell
      the developer; `_STALL_SECONDS` in `screencapture_source.py` is the knob.)
- [ ] A 30-minute recording does not drift: `session.json` `drift_ppm` for the
      system track is small and speech near the end still lines up.

### 4. Call detection
- [ ] With Meeting Notes open and idle, join a Zoom call: within a few seconds a
      "Zoom call detected" card appears bottom-right. Without Screen Recording
      permission the name is "Zoom call <time>"; with it, a Meet/Teams call is
      named from the window title.
- [ ] Click **Record**: recording starts under that name.
- [ ] Leave the call. After the grace period (default 20 s) plus 60 s of silence
      the stop countdown appears and the recording stops itself
      ("Call ended - recording stopped and queued."). **Keep recording** cancels it.
- [ ] Mute yourself for a minute mid-call with the Zoom window still open: no
      countdown (the call window is still there).
- [ ] A recording you started by hand is never auto-stopped. Meeting Notes' own
      microphone use never triggers a card.
- [ ] Revoke Meeting Notes' Screen Recording permission (or deny it): the card
      still appears, named after the app.

### 5. Data safety and platform behaviour
- [ ] Settings -> Local recordings: cleanup sends recordings to the **Trash**
      (recoverable in Finder), not permanent deletion.
- [ ] Settings shows recordings folder `~/Meeting Notes`; choosing a folder inside
      `Meeting Notes.app` is refused.
- [ ] Settings > Logs opens (the "Logs..." menu item opens Settings on that page) and its folder is `~/.meeting-notes`.
- [ ] Dark and light system appearance are followed (System setting).

### 6. Update
- [ ] Bump the server version, publish a new `MeetingNotes-macOS.zip`. The app
      shows **Update available** (not while recording); **Update** quits and
      reopens Meeting Notes at the new version, keeping settings, token and
      recordings. **Microphone and Screen Recording do not need to be granted
      again.** (If they do, tell the developer: the build was probably signed
      ad-hoc because `tools/macos_signing_setup.sh` was never run on the build Mac.
      The one update that switches from ad-hoc to the stable identity does ask
      once more; later ones must not.) If the app does not reopen, read
      `~/.meeting-notes/logs/update.log` (each step, the relaunch method that worked, the
      final status).
- [ ] Running the `curl ... | bash` one-liner again does the same, and refuses if
      the recordings folder is inside the app.

## Failure modes

- [ ] Unplug a USB microphone mid-recording. The session must keep the other
      track running, log the error, and finalize a playable file.
- [ ] Disconnect Bluetooth headphones mid-recording, then reconnect. The
      watchdog should restart that track and the recording should continue.
- [ ] `kill -9` the process mid-recording, then run `meeting-notes repair <dir>`
      and confirm both WAVs open with the expected duration.

## Choosing audio devices (Windows)

- [ ] Settings > Audio > Devices: **Microphone** and **Speakers (what you hear)** both show **Automatic (follows
      the system default)** and list your connected devices.
- [ ] Pick a specific microphone (not the Windows default), Save: the main window's "You:" name changes within a
      few seconds and the level meter moves with that microphone. Start a recording: the **You** lane follows it.
      Do the same for the speakers and the "Them" lane.
- [ ] Unplug the chosen microphone, then reopen Settings > Audio: it is listed as "<name> (not connected)". With it
      still unplugged press **Start recording**: recording starts on the default microphone (no red banner), the
      main window says "(automatic; <name> is not connected)", and the log has one line about the fallback.
      Plug it back in: idle, the window switches to it within a few seconds.
- [ ] Unplug the chosen microphone **during** a recording: the usual red "disconnected" banner shows; plug it back
      in and it is re-attached (green banner). It is not swapped for the default microphone.
- [ ] Change the microphone in Settings while recording: the running recording keeps its microphone; the next one
      uses the new choice.
- [ ] Set both back to **Automatic** and Save: the window follows the Windows default again.
- [ ] macOS: the speakers list is disabled with only Automatic (unless BlackHole is installed) and opening Settings
      shows no permission prompt.

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
- [ ] Use **Upload > Transcript file** with a Teams .vtt and a plain .txt, and **Paste a transcript**; confirm the
      meeting appears with speakers, no audio player, and notes (any AI provider but Disabled).
- [ ] Use **Upload** (Audio recording) in the desktop client with each supported format.
      Confirm the UI remains responsive while uploading, reports the server job,
      and the saved transcription appears in the web UI.
- [ ] Stop, and confirm the status line names the saved folder.
- [ ] "Open folder" opens the right directory on both Mac and Windows.

## Meeting detection (Windows; the Mac version is in the macOS client section)

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

### Auto record (Windows)

- [ ] Settings > General > Meeting detection: tick **Start recording automatically
      when a call starts**. An **Auto end** row appears below it and "Stop prompted
      recordings when the call ends" disappears. Untick "Offer to record ...": auto
      record greys out.
- [ ] Choose **When the call ends**, join a real Teams call (then Zoom, then Meet) and leave it: the card says
      "Stops when the call ends", the recorder shows "Auto end when the call ends". After you hang up and the call
      audio goes quiet a "call ending" countdown appears and the recording stops (Keep recording or Disable auto
      end: it goes on and the line goes away). This works with "Stop prompted recordings ..." unticked.
- [ ] With auto record on and **On the hour**, join a real Teams call (then Zoom, then
      Meet): recording starts without a prompt, a "Recording Teams call" card shows
      the name and "Stops at ..." (the next hour; the one after if under 10 minutes
      away) and goes away after about 20 seconds. The recorder shows "Auto end at ...".
- [ ] Stay past one minute before that hour (or set the PC clock close to it): a
      "Meeting time is up" countdown appears and the recording stops at the hour.
      Repeat and press **Keep recording**: it keeps going and never asks again.
- [ ] Choose **When people say goodbye** (a note under it says it needs Live preview; turn Live preview off in
      Settings > Server and the note says it is turned off). With Live preview on and a real call: the card says
      "Stops after goodbyes and 20 seconds of silence", the recorder "Auto end after goodbyes and 20 s of silence".
      Stay quiet for a minute: nothing stops it. Say "okay, bye everyone", then stay quiet: after about 10 seconds a
      "Meeting seems to be over" countdown appears and the recording stops about 10 seconds later ("Goodbyes said and
      20 seconds of silence"). Repeat but talk again during the countdown: it closes and the recording goes on;
      stay quiet again and it ends. Repeat and press **Keep recording**: it goes on and a later goodbye plus quiet
      ends it. Say "by the way" or "take care of that": nothing happens.
- [ ] Choose **After 30 seconds of silence**, join a call and stay silent in the lobby:
      nothing stops it. Talk, then stop talking and mute the call audio: a
      "No audio for a while" countdown appears after about 15 seconds and the
      recording stops 15 seconds later. Make a sound before it ends: the countdown goes away.
- [ ] Choose **Manual only**: the card has no **Disable auto end** button, the recorder
      shows no auto end line, and nothing stops the recording by itself.
- [ ] In any mode with an auto end, press **Disable auto end** (once on the card, once
      on the recorder): the line and card go away, "Auto end off for this recording"
      appears, and the recording runs until you stop it.
- [ ] Change Auto end in Settings mid-recording: the running recording keeps its choice.

### Note types (Windows)

- [ ] With the server reachable, open the client: a **Note type** select appears between the meeting
      name and **Start recording** (it stays hidden against a server with a single note type). Settings >
      General > **Notes** > Default note type lists **Server default (<name>)** and every type. Pick one and
      Save: the select changes to it.
- [ ] Record a short meeting with a type that is **not** the default and whose Notion page is set (server Settings
      > Note types > Save to Notion, with Copy notes automatically on for it), with an AI provider selected and **Generate notes automatically** on for that type: after Stop the notes appear by themselves, written with that type's prompt, and land in
      that type's Notion page for the month. Change the select while recording: the Stop-time choice wins.
- [ ] After the recording the select is back at the default. Auto-record a call (see Auto record): it uses the default.
- [ ] Open **Recorders** on the web: the card shows the same select next to the name; change it there and the window
      follows (a notice "Note type changed from the server" appears); change it in the window and the page follows.
      With an auto-recorded call running, "Auto end at ..." and **Disable auto end** show on the page too; pressing
      it turns the auto end off in the window.
- [ ] Stop the server, record with a type picked, restart it: the saved recording uploads with its note type
      (also after "Re-upload a saved recording...").

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

## Recorders page and remote control (0.7.6)
- [ ] With the app open on a computer, the server's Recorders page (phone or desktop) shows it within a few
      seconds: device name, platform icon, version, Idle. Close the app: the card disappears within seconds
      (no stale card). Reopen: it returns.
- [ ] From the phone, Start recording (with a name): the computer starts recording, shows "Recording started from
      the server", and the card shows the live clock and moving You / Them meters while someone speaks.
- [ ] Mute and unmute both sides from the page: the window's buttons follow and the session has silence for that
      side. Rename the meeting from the card, then Stop recording (confirm): the saved meeting has the new name.
- [ ] Unplug the headset mic while recording: the card shows "Not connected" and the banner; plug it back in.
- [ ] A Teams/Zoom call starts while idle: the prompt shows on the page; Record there starts recording, Not now
      dismisses the window's prompt. After the call ends, answer "meeting seems over" from the page (Stop / Keep).
- [ ] Update: with a newer version on the server the card offers Update; it is disabled while recording and the
      window never installs during a recording.
- [ ] Turn off Settings, "Allow control from the server": the card stays but says control is off and every command
      is refused. Turn it on again.
- [ ] Stop the server container while a recorder is open, then start it: the recorder reappears by itself.
- [ ] Mac and Windows both behave the same; an older recorder (0.7.5) never appears.

## Audio levels before recording (0.7.7)
- [ ] With the window open and idle, the You / Them bars move with the mic and speaker audio, greyed, tagged
      "Preview - not recording". Press Start: they switch to full colour at once with no device error and no gap
      at the start of the recording; Stop: back to the greyed preview.
- [ ] Minimize the window: the Windows / macOS "microphone in use" indicator goes away. Restore: it returns.
- [ ] Settings, "Show audio levels before recording" off: no indicator, bars stay flat, the page says
      "Levels show while recording".
- [ ] Open the Recorders page: the card bars move (dimmed, "Preview") even with the app minimized, and the mic
      indicator stays on only while the page is visible; switch tabs or close the page and it goes away within ~25 s.
- [ ] Plug in / pick another mic while idle: the bar follows the new device. macOS: only the mic previews; the
      system bar shows a dash.
- [ ] An older recorder (0.7.6) card shows empty bars and "Levels show while recording".

## Recordings list, re-upload and delete (0.7.6)
- [ ] Recorders page, **Recordings** on a card: every recording in that computer's save folder is listed newest
      first with length, size and a status. A recording that uploaded earlier shows "Uploaded · transcript ready"
      and **Open on server** goes to that meeting.
- [ ] Delete a meeting on the server (it goes to Recently deleted): the row becomes "In server trash". Empty the
      trash: it becomes "Not on server". Re-upload it from the panel: "Waiting to upload" / "Uploading N%", then
      Uploaded, and the meeting is back on the server.
- [ ] Turn the server off (or block it), re-upload one: "Upload failed" / "Waiting to upload" with the reason; turn it
      on and Retry uploads: it finishes.
- [ ] Delete from this computer on a row the server has: the confirmation is calm; it goes to the Recycle Bin
      (Windows) or Trash (Mac) and is restorable from there. Delete a row that is "Not on server": the confirmation
      shows the red "The server has no copy" warning.
- [ ] While recording, the current recording shows "Recording now" and can be neither re-uploaded nor deleted; a
      recording that is mid-upload can not be deleted either. Nothing else in the save folder is touched.
- [ ] Select several rows: bulk Re-upload and bulk Delete work; a refused one is reported by name.
- [ ] Close the app while the panel is open: it says "Recorder went offline"; reopen the app and Refresh works.
- [ ] Turn off "Allow control from the server": the panel says remote control is off and nothing is listed or changed.
- [ ] On the computer, the "..." menu, Re-upload a saved recording: each row shows the same status as the web panel
      (after a moment), Delete from this computer asks first with the same red warning, and while the server is
      unreachable the rows say the server status is unknown. A request from the server shows a brief notice.
- [ ] Phone: the panel is a full-screen sheet, rows stack, buttons are easy to tap, the confirm dialog fits.

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

### 7. Device hot-plug and stop suggestions (0.7.5)
- [ ] Start recording with no headset, then connect a USB or Bluetooth microphone: a
      green "connected ... recording from now on" banner appears within a few seconds and
      the session's mic track has a late-attach gap. Unplug it: a red banner, then it
      returns when reconnected.
- [ ] Start with Screen & System Audio Recording denied: the prompt appears once and the
      window says system audio is unavailable. After allowing it (and reopening the app if
      macOS asks), system audio attaches without restarting the recording.
- [ ] After a call ends (Zoom/Teams/Meet), the "Meeting seems over, stop?" suggestion
      appears for a manually started recording; "Keep recording" silences it for that call.
- [ ] Update shows the Update bar only (never installs by itself); the update request
      carries `X-Meeting-Notes-Client` (visible in the server's client list).
