# GPU helper and GPU server

Transcription and speaker labels are heavy work. On a server with no graphics card they run on the
CPU. This page shows two ways to hand that work to an NVIDIA graphics card. Both are optional and
off by default; with them off, nothing changes.

- [What it does](#what-it-does)
- [Which option to choose](#which-option-to-choose)
- [How the GPU helper works](#how-the-gpu-helper-works)
- [Requirements](#requirements)
- [Set up a GPU helper](#set-up-a-gpu-helper)
- [Daily use](#daily-use)
- [GPU server (Docker)](#gpu-server-docker)
- [Troubleshooting](#troubleshooting)
- [Privacy and security](#privacy-and-security)
- [Removing it](#removing-it)
- [FAQ](#faq)

## What it does

A GPU makes two steps much faster: turning speech into text (Whisper) and, if you use them,
**speaker labels** (telling `Them 1` from `Them 2`). Speaker labels are the slow part on a CPU. As a
rough guide, on a CPU-only server they took about 19 minutes for a 21-minute meeting. A graphics card
does the same in a few minutes. Your numbers depend on the card and the model.

If you do not use speaker labels and your server keeps up with your meetings, you do not need any of
this.

## Which option to choose

| | A. GPU server | B. GPU helper |
|---|---|---|
| What it is | The machine that runs the server has an NVIDIA graphics card. | A Windows PC with an NVIDIA graphics card, running the recorder, does the work for a server that has none. |
| Set up | A second Docker Compose file, one rebuild. | Turn it on in the server's Settings, prepare a download, then install GPU support on the PC. |
| Always available | Yes, as long as the server is up. | Only while that PC is on, awake and signed in to Windows. Otherwise the server does the job itself. |
| Choose it when | Your server is a desktop or workstation with a card. | Your server is a small box or NAS and your own PC has a card. |

You can use both. A job goes to a helper first, and the server does it itself if no helper takes it.

## How the GPU helper works

```text
 Recorder ──upload──► Server ──offer──► Helper PC (polls the server)
                        ▲                   │ claims the job, downloads the audio,
                        │                   │ transcribes on the graphics card
                        └────── result ─────┘
                        │
                        ▼
                  Transcript and notes
```

- **The helper calls the server. Nothing calls the helper.** The helper asks the server every few
  seconds whether there is work. You do not open any port or change any firewall on the helper PC.
- **One download, from the server only.** The server prepares a "helper download" once (Python,
  the CUDA libraries, the Whisper model and, with a Hugging Face token, the speaker-label model).
  Helper PCs download it from your server. They never contact Hugging Face or PyPI.
- **Each helper has its own key.** It only allows GPU work for this server and you can revoke it
  in Settings.
- **Jobs wait their turn for the GPU.** When a meeting needs transcribing, the server offers it to
  helpers right away. A helper claims one job at a time, reports progress every few seconds, and
  sends back the text. Several meetings at once simply queue for the helper: a busy GPU is still far
  faster than the server's processor. The server takes a job back and does it itself if no helper is
  online for the "Wait for a helper" time (30 seconds by default), if a job has waited 15 minutes, if
  the helper goes quiet for a minute while working, or if it takes much longer than expected. A job is
  offered once.
- **Recording pauses the helper.** While the Meeting Notes app on that PC is recording, it does not
  take new work, so your meeting is not slowed down.

The text that comes back is merged and turned into notes by the server exactly as before.

## Requirements

**Server (for a GPU helper)**
- About 5 GB of free disk space in the data folder for the helper download.
- For speaker labels: a free Hugging Face account, a token, and acceptance of the terms of the model
  `pyannote/speaker-diarization-community-1`. The token is set as `HUGGINGFACE_TOKEN` in
  `docker/.env` and the server image must be built with `INSTALL_DIARIZATION=true`. Without a token
  the helper still transcribes, just without speaker labels.
- The server needs internet access when you prepare the download.

**Helper PC**
- Windows 10 or 11, 64-bit, with the Meeting Notes recorder installed from your server's Install page.
- An NVIDIA graphics card with 4 GB or more of video memory (VRAM). Cards from the GeForce GTX 900
  series (Maxwell) through current RTX cards are supported.
- A recent NVIDIA driver (version 525 or newer, which supports CUDA 12).
- About 6 GB of free disk space.
- No administrator rights.
- AMD, Intel and Apple graphics are not supported.

## Set up a GPU helper

### 1. On the server

1. **Hugging Face token (only for speaker labels).**
   - Sign in at huggingface.co and open
     [pyannote/speaker-diarization-community-1](https://huggingface.co/pyannote/speaker-diarization-community-1).
     Accept the terms on that page.
   - Create a token of type **Read** at
     [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens).
   - Put it in `docker/.env`:

     ```text
     INSTALL_DIARIZATION=true
     HUGGINGFACE_TOKEN=hf_...
     ```

2. **Rebuild** so the image includes the speaker-label software:

   ```bash
   cd Meeting-Notes/docker
   docker compose -p meeting-notes up -d --build
   ```

3. In the web UI open **Settings > Transcription > GPU helpers** and:
   - turn on **Use GPU helpers**;
   - turn on **Speaker labels** (optional) and choose the model and the speaker limits if you want
     to change them;
   - under **Speaker labels without a helper** choose what happens when a job runs on the server
     instead: **run here** (slower, but still labelled) or **skip** (fast, no labels);
   - under **Helper pack** press **Prepare download** and wait. It takes a while and shows its
     progress. When it says it is ready, the helper PCs can install.

   Save the settings.

   If you later change the Whisper model, the speaker-label model or update the server, the pack shows
   **Out of date**. Press **Prepare download** again; helpers then show **Update GPU support**.

### 2. On each helper PC

1. Install or update the recorder from your server's **Install** page (see the README).
2. Open **Settings > GPU helper**.
3. Check that **Graphics card** shows your card (name, memory, driver).
4. Press **Install GPU support**. It registers this PC with the server, downloads the helper
   download from the server (several GB; the progress shows how much), unpacks it, installs it, and
   runs a test on the graphics card before switching to it. You can press **Cancel**; an interrupted
   download continues where it stopped the next time.
5. Turn on **Let the server use this computer's graphics card** and leave **Start when I sign in to
   Windows** on if you want the helper to be there after a restart. Press **Save**.

### 3. Check that it works

- On the PC, **Status** in Settings > GPU helper (and the tray icon) shows **Idle**.
- On the server, **Settings > Transcription > GPU helpers** lists the PC under connected helpers with
  its graphics card and status, and the **Recorders** page shows a GPU line on that recorder.
- Record or upload a short meeting. While it runs, the meeting page and the Meetings list say
  "Transcribing on `<helper name>` (GPU)" with a percentage. When it finishes the meeting page says
  "Transcribed on `<helper name>` (GPU)".
- If the helper could not take the job, the meeting says "Transcribed on this server (GPU helper:
  `<reason>`)". See [Troubleshooting](#troubleshooting).

## Daily use

- **The tray icon.** While the helper is on, Meeting Notes puts an icon in the notification area
  (next to the clock; it may be under the `^` arrow). Right-click it for:
  - a status line (`GPU helper: Idle`, `Working on M-0142 · 42%`, `Paused while recording`, ...);
  - **Open Meeting Notes**;
  - **Pause GPU helper** / **Resume GPU helper**;
  - **Quit**.
- **Closing the window does not stop the helper.** The window hides and the helper keeps working
  from the tray. **Quit** from the tray menu ends the helper and the app (it asks first if a recording
  is running).
- **Recording.** The helper pauses itself while you record on that PC and resumes after.
- **Pause** stops it taking new work until you resume. A job already running finishes.
- **Off, asleep or signed out.** The server cannot reach the helper, so it does the job itself. Nothing
  is lost. If the PC goes to sleep in the middle of a job, the server takes the job back after about a
  minute and starts again on the server.
- **After a restart.** With **Start when I sign in to Windows**, the app starts hidden in the tray when
  you sign in. It only starts for the helper if it is turned on.
- **Updates.** When the server prepares a new download, **Settings > GPU helper** and the tray show
  **Update GPU support**. A helper that is out of date gets no new work until you press
  **Update GPU support** in Settings.
- **Laptops.** The helper uses the graphics card whenever it works, on battery too. Pause it or turn it
  off if you do not want that.

## GPU server (Docker)

Use this when the server's own machine has an NVIDIA card.

**You need**
- An NVIDIA driver on the host (Linux, or Windows with Docker Desktop and WSL 2).
- The [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
  so Docker can use the card. Check with `docker run --rm --gpus all ubuntu nvidia-smi`.

**Start the server with the GPU file**

```bash
cd Meeting-Notes/docker
docker compose -p meeting-notes -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
```

The second file reserves the GPU for the server, installs the CUDA libraries into the image
(`INSTALL_GPU=true`) and sets `MEETING_NOTES_DEVICE=cuda` and `MEETING_NOTES_DIARIZATION_DEVICE=cuda`.
Use the same two `-f` options every time you run `docker compose` for this server (including `down` and
`logs`). If you also use the AI bridge, add `--profile ai`.

**Speaker labels on the GPU server.** Set `INSTALL_DIARIZATION=true`, `HUGGINGFACE_TOKEN` and
`MEETING_NOTES_DIARIZATION=true` in `docker/.env` as in [Set up a GPU helper](#1-on-the-server), then
rebuild with the command above. Speaker labels run on the GPU when the GPU file is used.

**Check that the GPU is used**
- `docker compose -p meeting-notes -f docker-compose.yml -f docker-compose.gpu.yml logs meeting-notes-server`
  should not show the warnings below.
- While a meeting is transcribing, run `docker exec <server container name> nvidia-smi`. The
  processes list should show the work, and memory use should go up.

**If the GPU is not usable.** The server still starts. It logs a warning and uses the CPU for that part:
- `MEETING_NOTES_DEVICE=cuda but no usable CUDA device was found; using the CPU`
- `diarization device cuda requested but CUDA is not available; using the CPU`

On older cards (for example the GTX 10 series) transcription automatically uses a compatible number
format; there is nothing to set.

A GPU server can also use GPU helpers on top of its own card; leave **Use GPU helpers** off if you do
not want that.

## Troubleshooting

| What you see | What it means and what to do |
|---|---|
| **No NVIDIA graphics card found** in Settings > GPU helper | Windows could not find an NVIDIA card or driver. Install or update the NVIDIA driver from nvidia.com, restart the app and look again. Laptops with two graphics chips must have the NVIDIA one available to Windows. |
| Install stops with **The GPU self-test failed** | The new software could not use the card. Update the NVIDIA driver (version 525 or newer), make sure the card has 4 GB or more and is supported, and press **Install GPU support** again. The old install, if any, is left as it was. |
| Install says **The server has no GPU helper pack yet** | On the server, open Settings > Transcription > GPU helpers and press **Prepare download**. |
| Install says **This server does not support GPU helpers** | The server is an older version. Update the server first. |
| **Could not reach the server** while installing | Check the server address on the Server page and that the PC can reach it. Pressing Install again resumes the download. |
| Status **Offline** | The helper cannot reach the server, or it just crashed and is restarting. It tries again every few seconds to minutes. Check the server address and that the server is up. |
| Status **Update GPU support** | The server has a newer download. Press **Update GPU support** in Settings > GPU helper. |
| Status **Error: ... Install GPU support again** or the server refuses the helper | The helper key was revoked or is wrong. Press **Install GPU support** again; the PC registers again and gets a new key. |
| Jobs keep running on the server although a helper is idle | Check that **Use GPU helpers** is on and saved, the helper shows **Idle** (not **Paused**, **Offline** or **Update GPU support**) and the download is not **Out of date**. If the claim wait is very short, raise it in Settings. The meeting page names the reason when a job fell back. |
| A job goes to the server after a long run | The helper took longer than its time limit or stopped answering, so the server took the job back. This is normal when a PC sleeps. |
| Speaker labels are missing | Speaker labels need **Speaker labels** turned on, a `HUGGINGFACE_TOKEN` on the server, acceptance of the model terms, and the download prepared after that. If the pack says speaker labels are unavailable, fix the token or terms and press **Prepare download** again. If a speaker-label step fails, the transcript is still produced, with plain `Them`, and the job notes the failure. With **Speaker labels without a helper** set to **skip**, jobs that run on the server get no labels. |
| The Windows PC is slow while it works | The helper uses the graphics card and some CPU. Pause it from the tray when you need the machine. |

## Privacy and security

- Audio leaves the server only for a job that a helper has claimed, and only to that helper, over the
  address you gave the recorder. On your home or office network that is usually fine. For anything
  beyond it, put the server behind HTTPS (a reverse proxy or a VPN) so audio and keys are not sent in
  clear text.
- The text goes back to the server. Nothing is sent to anyone else.
- A helper key only allows GPU work (asking for work, getting that job's audio, sending results). It
  cannot read meetings, notes or settings. You can revoke it in Settings > Transcription > GPU helpers.
- The main server password is never given to the GPU software. It runs with the helper key only.
- The Hugging Face token stays on the server. The server uses it to fetch the speaker-label model;
  helpers receive the model files from the server, not the token.
- The helper software runs as your Windows user, under `%LOCALAPPDATA%\MeetingNotes-GPU`. It
  makes outbound calls only to your server.

## Removing it

- **Stop using the PC:** turn off **Let the server use this computer's graphics card** in Settings and
  save. The tray icon goes away and Start at sign-in is removed.
- **Delete the software:** press **Remove GPU support** in Settings > GPU helper. This deletes the
  downloaded software (several GB) from that PC.
- **Revoke the key:** in the server's Settings > Transcription > GPU helpers, press **Revoke** on the
  PC. It can no longer ask for work.
- **Uninstalling Meeting Notes** with the server's uninstaller removes the sign-in entry and the GPU
  software with the rest of the app. Recordings are never deleted.
- **Turn it off for everyone:** turn off **Use GPU helpers** on the server. Every job runs on the
  server again.

## FAQ

**I have several PCs with graphics cards.** Each can be a helper. A job is claimed by the first helper
that asks for it, and a helper does one job at a time.

**Does it work with AMD, Intel or Mac graphics?** No. It needs an NVIDIA card.

**Does it work on a Mac or Linux PC?** Not as a helper; the helper is Windows only. A Linux server with a
card can use the GPU server option.

**Will it slow down games or video calls?** The helper uses the graphics card while it works on a job,
which is a few minutes per meeting. It pauses while you record in Meeting Notes. Pause it from the tray
if you are gaming.

**Can I use it on a laptop on battery?** Yes, but it will use power while working. Pause it from the tray
when you do not want that.

**What if my PC is off?** The server does the job itself.

**Do I need to set anything per meeting?** No.
