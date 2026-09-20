# Architecture

Two pieces, split so the machine in the meeting does as little work as possible.

```
  Laptop in the meeting                    Server on the LAN (Docker)
 ┌───────────────────────────┐            ┌────────────────────────────┐
 │  meeting-notes-ui  (Qt)   │            │  FastAPI + faster-whisper  │
 │                           │            │                            │
 │  mic ──┐                  │  16kHz     │   live pass  ──► partials  │
 │        ├─► recorder ──────┼──ws────────┼─► (fast model, disposable) │
 │  sys ──┘      │           │  PCM       │                            │
 │               ▼           │            │                            │
 │        48kHz WAV on disk  │  upload    │   final pass ──► transcript│
 │        (the real artifact)├──http──────┼─► (any model, authoritative)│
 │               │           │            │                            │
 │        upload queue ◄─────┘            └────────────────────────────┘
 │        (retries forever)  │
 └───────────────────────────┘
```

## Why it is split this way

**The client stays light.** It needs `soundcard`, `numpy`, PySide6 and an HTTP
client. No Whisper, no model files, no ML stack on the laptop. Installing on a
second machine costs nothing but the UI.

**One machine does the compute.** Models are downloaded and cached once, on the
server, instead of on every laptop.

## The two passes

A CPU-only server cannot run a large model against two live streams and keep up
with realtime, so there are deliberately two different jobs:

| | Live pass | Final pass |
|---|---|---|
| Input | streamed 16kHz PCM | the complete recording, uploaded |
| Model | fast (`base.en`) | whatever you configure |
| Purpose | see it working, read along | the transcript you keep |
| If it fails | nothing is lost | retried until it succeeds |

The live preview is explicitly disposable. That is what makes a dropped wifi
connection a non-event: the laptop keeps recording to disk regardless, and the
authoritative transcript is produced afterwards from that complete local file.

## Why everything on the wire is 16kHz mono int16

It is Whisper's native input, so sending it costs nothing in transcription
quality — the model resamples to 16kHz internally anyway. It is 32 KB/s per
track, which is nothing on a LAN. And it needs no audio codec on the client, so
there is no FLAC or Opus dependency to install or to go wrong.

The full-quality 48kHz recording never leaves the machine that made it. It stays
local as the archive copy.

## Resumable streaming

Every binary frame carries its absolute frame offset, and the server
acknowledges the highest contiguous offset it has stored per track. After a
reconnect the client resumes from that offset. Duplicate or out-of-order frames
are therefore idempotent: writing the same offset twice is a no-op rather than
corruption. This is why a brief network drop does not even disturb the live
view, let alone the recording.

The protocol lives in one file, `meeting_notes/wire.py`, imported by both sides
so it cannot drift.

## The offline queue

Finished recordings are queued unconditionally, even when the server is
reachable at that moment. A background worker uploads, waits for the job, and
writes `transcript.md` / `transcript.json` back into the session directory. If
the server is down the entry simply stays pending and is retried, including
across restarts of the app.

This is the mechanism behind "record anyway": there is no state in which
starting a meeting depends on the server being up.

## Layout

```
meeting_notes/
  wire.py              protocol shared by both sides
  audio/               capture (client side)
  wav_io.py timing.py  recording format and clock alignment (shared)
  transcribe/          Whisper backend and transcript merge (server side)
  client/
    controller.py      bridges recorder, stream and queue
    resample.py        48kHz -> 16kHz with anti-aliasing
    streamer.py        websocket with reconnect and resume
    queue.py           offline upload queue
    api.py             HTTP client
    ui/                Qt window, waveform, settings
  server/
    app.py store.py jobs.py live.py auth.py
docker/                Dockerfile, compose
```

## Security

The upload endpoints require a bearer token shared between client and server
(`MEETING_NOTES_TOKEN`). If it is unset the server allows everything and says so
loudly at startup — convenient on a trusted home LAN, but it is an open audio
endpoint, so it should be a deliberate choice rather than a default nobody
noticed.
