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

## Long meeting

- [ ] A real meeting of 1 hour or more, then transcribe.
  - [ ] Check `[You]` / `[Them]` interleaving at the **start**, **middle** and
        especially the **end** — clock drift shows up at the end.
  - [ ] Disk usage matches expectations (~345 MB/hour/track at 48 kHz).
  - [ ] `session.json` lists no unexplained gaps or degraded tracks.
