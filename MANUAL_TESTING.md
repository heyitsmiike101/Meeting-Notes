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

## Recorder UI

- [ ] `meeting-notes-ui` opens; both device names show in the top right.
- [ ] Settings: change the save folder, save, record, and confirm the session
      lands in the new folder.
- [ ] Both waveform lanes move independently: talk (green moves), play a video
      (blue moves). A lane that stays flat while its source makes noise is the
      bug this display exists to catch.
- [ ] Stop, and confirm the status line names the saved folder.
- [ ] "Open folder" opens the right directory on both Mac and Windows.

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
