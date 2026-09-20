# Meeting Notes

Records a meeting as **two separate audio tracks** — your microphone and your
system audio — then transcribes them into a single, speaker-labeled transcript.

```
**[00:04:12] You:**  Can we push the launch to the 30th?
**[00:04:19] Them:** That works, I'll update the tracker.
```

Runs on Windows and macOS. On Windows it needs **no driver and no admin**.

## Why not a virtual microphone?

The obvious design is a virtual mic and speaker that the meeting app connects
to, passing audio through and recording it in the middle. That was the original
plan here, and it was rejected. Two reasons:

1. **You cannot create a virtual audio device from Python.** They are
   driver/kernel objects — a signed driver on Windows, an AudioServer HAL
   plug-in on macOS. Both need admin, neither is scriptable. Only Linux
   (PipeWire/PulseAudio null sinks) can do it unprivileged.
2. **It destroys the information you actually want.** Passing everything
   through one virtual device mixes your voice and theirs into a single stream,
   so the transcript cannot tell you who said what. It also inserts latency into
   your own monitor path and interferes with the meeting app's echo
   cancellation.

Capturing two tracks instead sidesteps all of that. Your microphone is opened in
shared mode *alongside* the meeting app (both can hold it at once), and system
audio is captured separately. Nothing about your existing audio routing changes,
and speaker attribution comes for free.

## Install

```bash
python -m venv .venv
.venv/bin/pip install -e .            # Windows: .venv\Scripts\pip install -e .
```

Core dependencies are `numpy` and `soundcard`, both pure Python (`cffi`-based).
No compiler and no admin rights are needed.

For local transcription:

```bash
pip install -e '.[whisper]'
```

## Quick start

```bash
meeting-notes doctor                  # verify capture will actually work
meeting-notes devices                 # list microphones and system-audio sources
meeting-notes record --name standup   # Ctrl+C to stop
meeting-notes transcribe recordings/2026-09-20_14-30-00_standup
```

**Run `doctor` before your first real meeting.** It catches the failure modes
that are otherwise invisible until afterwards.

## Platform setup

### Windows — nothing to install

Windows can loop back any output device through WASAPI. `record` finds it
automatically. No driver, no admin, no rerouting.

### macOS — one-time setup, needs admin once

macOS has no OS-level loopback API, so capturing what other participants say
requires a virtual audio driver.

1. Install [BlackHole 2ch](https://existential.audio/blackhole/). This is a
   `.pkg` install and **does require admin**, once. It is the only step in this
   project that does.
2. Open **Audio MIDI Setup**, click **+** → **Create Multi-Output Device**, and
   tick both **BlackHole 2ch** and your real speakers or headphones.
3. Set that **Multi-Output Device** as your system output.

Step 3 matters more than it looks. If you set output to **BlackHole alone**,
recording works perfectly and you simply will not hear the meeting — nothing
errors, nothing looks wrong. `doctor` checks for exactly this state and warns
loudly.

Your microphone also needs permission under **System Settings → Privacy &
Security → Microphone**. macOS ties that grant to the *specific executable*, so
a pyenv shim, a venv `python` and a packaged binary each count as a separate
identity; granting one does not grant the others.

## How it works

Two threads, one per track, each writing mono 16-bit PCM straight to disk.

**Files are crash-safe.** Audio is appended to a headerless `.raw` file while
recording and wrapped into a `.wav` only at the end. A standard WAV writer
patches its length fields in `close()`, so a process killed mid-meeting leaves
an unreadable file — a real risk over a two-hour call. If a session does die,
`meeting-notes repair <dir>` rebuilds the WAVs from the raw data.

**Clocks are tracked, not assumed.** Two capture devices run off two different
crystals, so "48000 frames" means slightly different durations on each, drifting
apart over a long meeting. Each track logs `(frames, monotonic_time)` pairs once
a second, and the transcript merge maps frame positions onto real time by
interpolating that log. Measured drift is reported in `session.json` as ppm.

**A dead device does not end the session.** If a track stops delivering audio
for 30 seconds — a Bluetooth headset dropping, a USB mic unplugged — a watchdog
restarts it and pads the lost stretch with silence, so frame positions keep
matching wall-clock time and every timestamp after the gap stays correct. The
other track is unaffected. Gaps are recorded in `session.json` and marked in the
transcript rather than silently looking like nobody spoke.

That last part is deliberately not "kill the thread and retry": a thread blocked
inside a native `record()` call cannot be interrupted from Python at all, so
recovery abandons it and starts a fresh one instead of waiting.

## Output

```
recordings/2026-09-20_14-30-00_standup/
  mic.wav              your voice
  system.wav           everyone else
  mic.timing.jsonl     frame -> wall-clock log
  system.timing.jsonl
  session.json         devices, rates, gaps, drift, errors
  transcript.md        after `transcribe`
  transcript.json
```

At 48 kHz this is roughly 345 MB per hour per track. `--rate 16000` cuts that to
about 115 MB per hour with no real loss for transcription, since Whisper
resamples to 16 kHz internally anyway.

## Commands

| Command | What it does |
|---|---|
| `devices` | List microphones and system-audio sources |
| `doctor` | Check capture works; probe levels; catch the macOS output trap |
| `record` | Record until Ctrl+C |
| `transcribe <dir>` | Produce a merged, speaker-labeled transcript |
| `repair <dir>` | Rebuild WAVs from `.raw` after an unclean exit |

Useful `record` flags: `--name`, `--mic`, `--system`, `--rate`, `--save-config`,
`--mic-only`, `--system-only`, `--stall-timeout`.

By default, if only one of the two tracks can be opened, `record` refuses to
start — recording half a conversation and discovering it afterwards is an
expensive mistake. Pass `--mic-only` or `--system-only` to do it deliberately.

## Transcription backends

Transcription is behind a small registry so the recorder does not depend on it.
`faster-whisper` ships as an optional backend:

```bash
meeting-notes transcribe <dir> --backend faster-whisper --model small.en
```

Adding another backend means implementing `transcribe(wav_path, track) ->
list[Segment]` and calling `register()`.

## Tests

```bash
.venv/bin/python -m pytest
```

The whole suite runs with no audio hardware, because `soundcard` sits behind the
`AudioSource` protocol and tests drive a fake. That is what makes it possible to
test the things that actually matter — a device wedging forever, a device
vanishing mid-meeting, a clock running fast — which are nearly impossible to
trigger on purpose with real hardware.

Hardware behavior that genuinely cannot be faked is listed in
[MANUAL_TESTING.md](MANUAL_TESTING.md).

## Limitations

- Not real-time. Record first, transcribe after.
- No diarization *within* the system track: multiple remote participants are all
  labeled `Them`.
- macOS system audio needs BlackHole. Capturing it via ScreenCaptureKit (macOS
  13+, no admin) would remove that step but is fragile from Python; not built.

## Recording other people

Recording a conversation without telling the other participants is illegal in
many places, including every two-party-consent jurisdiction. Tell people they
are being recorded. This tool captures only your own machine's audio and does
nothing to hide itself.
