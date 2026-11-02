# Changelog

Notable changes per release, newest first. Versions are the packaged client and
server version (`meeting_notes/__init__.py`). Earlier history is in git.

## Unreleased
- Server: **the Recorders page draws each recorder like the new single-view app.** The card's record box has the
  meeting name, note type and Start/Stop on top, then a smaller clock and one row per side: a mic or speaker icon (lit
  while that side is heard; click to mute or unmute while recording, struck through and red while muted), a short
  audio lane and the device name on one line (scrolls on hover when it is too long). Your notes come next, then the
  live preview folded to its last two lines (**Show all** / **Show less**, remembered in that browser) and the status
  line. The big lanes and the Mute you / Mute them buttons are gone, as in the app.
- Server + client: **edit Your notes from the Recorders page.** A recorder on this version shows an **Edit** button
  on its notes: the notes open as text (Markdown, the way the app stores them) and every pause in typing saves them on
  that computer, where they show in the app as if typed there; **Done** shows them rendered again. If someone typed in
  the app meanwhile, the page does not overwrite it: it says the notes changed and offers **Use theirs** or **Keep
  mine**. Not while a recording is finishing. New `set_notes` command and `notes_edit` capability; older recorders
  keep read-only notes on the page.

## 0.9.0-beta.5 (dev pre-release)
- Client: **one recorder view, with the notes in it.** The window now has a single layout: the recording card on top
  (meeting name, People, note type and Start/Stop; under them a smaller clock, then one row per side with a mic or
  speaker icon, a short audio lane drawn like the old level meters, and that side's device name on one line), Your
  notes in the middle taking the free height, and the live preview at the bottom folded to its last two lines
  (**Show all** / **Show less**, remembered). The icons light up while that side is heard; clicking one mutes that
  side (the icon turns red and struck through, its lane dims) and clicking again unmutes it, while recording. A device
  name too long for its row is cut with "..." and scrolls while the mouse is over it; why Automatic chose a device is
  in its tooltip. This replaces the big level meters with Mute you / Mute them buttons, the notes button and the side
  notes pane with its drag edge, and beta.4's collapse-into-a-bar button. Compact view (the slim bar on the right edge
  of the screen) is unchanged.

## 0.9.0-beta.4 (dev pre-release)
- Server: **Archive and forget people over time.** With the nightly voice recheck (and **Recheck now**), named people
  not heard in a meeting for 180 days are archived: hidden from the People list (an **Archived (N)** switch shows
  them) but still recognised, and back as soon as they are heard again. Unnamed voices not heard for 90 days are
  forgotten (their voice data and samples are removed; the meetings and transcripts stay), unless a question about
  them is waiting. You is never archived. A person's page has **Archive** and **Restore**, and the People page and
  Settings show what the last cleanup did. Both limits are under Settings > Ingestion > Label speakers (0 = never).
  New `POST /v1/people/{id}/archive`; `GET /v1/voice-recheck` also returns `last_cleanup`.
- Server: **Name new voices from introductions.** When voice profiles leave a voice unnamed ("Person 9") and that
  person says their name in the meeting ("Hi, I'm Dana from Acme") or is introduced ("this is Dana, she runs ops"),
  a small separate AI pass names them. It runs after the notes are done, never delays or changes them, and only
  looks at the start of the meeting plus later lines that sound like introductions. Every name must be backed by
  words that really are in the transcript. A name it is sure of is applied: the voice gets the name, or joins the
  person of that name when they have no voice yet, or (when that person already has a voice) you get the usual
  "Sounds like X?" question. A first name alone ("Dana") counts as the one person whose name starts with it ("Dana
  Reyes"); when two people share it, it is only offered. A name it is less sure of is offered on the person's page as "Introduced as Dana?" with
  **Use this name** and **Not them**. Anything it applied shows "Named from introduction" with the quote and an
  **Undo** button, and an undone name is never applied again when the meeting is processed again. Retranscribe and
  Find speakers again run it again and replace the old result. On by default; Settings > Ingestion > Name from
  introductions (`intro_naming`). Bridges older than this version never get these jobs.
- Client: **collapse the recorder into a thin bar.** The button left of the "Meeting Notes" title is now always there
  and collapses the recorder instead of hiding it: the recording card, level meters and live preview fold into a
  thin bar under the header with the status dot, the elapsed time, two small level meters and Start/Stop (a red
  audio-device alert shows in the bar too). With Your notes open the bar sits above the notes and the window narrows
  to them; with the notes closed the whole window shrinks to a short, wide bar. Collapsing and expanding animate
  (about a quarter second), expanding gives the window its size from before, and the collapsed state is remembered
  across restarts. Closing the notes or switching to compact view no longer brings the recorder back by itself.
- Server + client: **an updated recorder is no longer listed as a new one.** Each launch of the app (and every update
  restarts it) used to connect with a new id, so the Recorders page piled up offline copies of the same computer. The
  server now keeps one row per computer: offline rows with the same name and platform merge into the one that
  connects (its "first seen" is kept), and existing duplicates are merged when the server starts, so this works for
  recorders already installed. The recorder now also keeps one id for good (saved in its config with the computer's
  name), and if another copy of the app takes over that id it waits a minute before reconnecting instead of trading
  the connection back and forth.

## 0.9.0-beta.3 (dev pre-release)
- Server: **Find speakers works on 1:1 meetings.** A meeting with a people count of 2 skipped speaker separation, so
  "Find speakers again" finished at once, never showed in the Queue and changed nothing, and with voice profiles on
  the other person's voice was never recorded. The one person on the other side is now still diarized (capped at
  one speaker) when voice profiles are on; when there really is nothing to separate the button says why.
- Server: **Regenerate notes uses the newest transcript.** It used to rebuild from the transcript the notes were first
  made from, so new speakers or text from a re-run never reached the notes. The notes panel also says when the
  transcript changed after the notes were made.
- Server: **Retranscribe and Delete audio no longer pile up or break work.** A second Retranscribe while one is still
  waiting returns that job (`already_queued`) instead of stacking a duplicate; a running one can still be restarted.
  Delete audio while a job still needs the audio is refused (409) instead of making that job fail. Retranscribe also
  confirms it started.
- Server: **Changing the people count offers "Find speakers again"** (the text stays) instead of a full
  Retranscribe, when speaker labels are set up.
- Server: **Nightly voice recheck.** Once a night (2:00 AM by default) voices in meetings from the last 30 days
  that aren't linked to a named person are matched against the people again, so someone named later is recognised
  in their earlier meetings. Hand-confirmed links are kept. On/off, the hour and the look-back window are under
  Settings > Ingestion > Label speakers, with **Recheck now** and the last run's result. New
  `GET/POST /v1/voice-recheck`.
- Server: **Bulk "Update voice profiles" reports what changed** instead of always saying the profiles were updated.
- Server: **A re-run replaces the notes by itself.** Retranscribe and Find speakers again on a meeting that already
  has finished notes now regenerate them when the re-run ends (same note type, the old notes are replaced), once
  after the speaker labels if a retranscribe has that step. A failed re-run leaves the notes alone, a meeting
  without notes still gets none, and Notion resyncs when the rebuilt notes complete. Until then the notes panel says
  they will rebuild by themselves.
- Server: **Naming a person re-matches recent meetings right away.** Naming or renaming someone, combining or merging
  people, and assigning or confirming a voice start a background voice recheck over the nightly window a few seconds
  later (never overlapping another recheck, never blocking the edit); it shows in Settings as the last run.
  Hand-confirmed links never change.
- Server: **One voice-recheck action.** `POST /v1/voice-recheck` takes an optional `{"session_ids": [...]}` and the
  Meetings page's bulk Update voice profiles now makes that one call instead of one per meeting. People has a
  **Recheck voices** button with the last and next run; Settings, People and Meetings word the result the same way.
- Server: **Combine people.** The People list has checkboxes and a **Combine** button (two or more): choose who to
  keep and everyone else moves into them (the "You" profile always stays). The person page's "Merge with..." is now
  "Combine with...". New `POST /v1/people/combine`.
- Server: **Voice profiles pick people up better over time.** Confirmed matches count double, one long meeting can't
  dominate, a wrong automatic match can't drag a profile off, and a voice heard on a different setup is matched
  against each confirmed sample as well as the average. When you confirm or name someone, other meetings' unnamed
  voices that sound like them get a "Sounds like X?" question (never an automatic change), and "Not them" is
  remembered. The person page shows how many meetings the voice profile is built from.
- Server: **Company is a dropdown** on the person page (companies already used, None, or add a new one).
- Server: **People is the one place for names.** Add a person by name (no voice yet), "Also heard as" spellings on the
  person page, and named people feed the notes prompt and transcription. Existing names from the old names list
  become people.
- Server: **Participants in meeting notes link to People.** A name that matches a person (or one of their spellings)
  links to them; one that matches nobody is marked "Not in People" and can be added or linked to someone.
- Server: **The notes AI is told who was recognised by voice.** The prompt gets an "Identified speakers" section
  (after Known names) listing the named people heard in the meeting with their company, saying their transcript
  labels are names and not guesses; the recorder stays "You" in the transcript the AI reads even when the "You"
  profile has a name (the name is in that section instead), so action-item owners keep working. Nothing is added
  while voice profiles are off or nobody in the meeting is named.
- Server: **The meeting timeline keeps every speaker.** It used to fold any meeting with more than six labels back
  into two "You" and "Them" lanes, so a five-person call looked unchanged after "Find speakers again" even though the
  transcript below had the new labels. Now every speaker or recognised person has its own lane, however many there are.
- Server: **Participants come from the voice profiles.** The Participants rail beside the notes now starts from who
  was recognised by voice in the meeting (`GET /v1/meeting-notes/{id}` carries `people`): each row is the profile's
  avatar and name linking to the person, with company, "Heard by voice" and how long they talked ("Your voice" for the
  recorder, "Not named yet" for a voice without a name, so it can be named from there). The names the AI wrote merge
  into those rows (by name or an "also heard as" spelling), a known name that was not heard keeps its own row, and a
  name People does not know still gets "Not in People". A voice with no name offers **Link to a person…** and a name
  that only exists as a voice-less person offers **Same person as…**: both combine that entry into the person chosen
  (the voice moves to them, or the name becomes another spelling of theirs) and the notes and transcript are drawn
  again. Edit starts with the recognised people already in the list. Voice profiles off: plain names as before.
- Server: **A name that belongs to a voice folds into it on its own.** When notes complete (and whenever a meeting's
  notes are opened), a participant name that fits exactly one voice heard in that meeting, such as "Grant" when the
  only "Grant …" heard is Grant Batt, becomes that person's: a voice-less person of that name is combined into the
  voice (which keeps the name you gave it, and "Grant" becomes a spelling of it), a bare name becomes a spelling. A
  name that fits two voices, has a voice of its own, or is already someone's spelling is left for you to decide.
- Server: **Settings > Ingestion.** A new second Settings page follows the pipeline every recording goes through, with a
  left-to-right chart at the top: Audio, Transcribe, Identify speakers, Label speakers, Generate notes. Each step is a
  link with a one-line status that scrolls to and highlights its settings below (`#ingestion/speakers`); Identify
  speakers, Label speakers and Generate notes grey out (live, before saving) while Speaker labels, Voice profiles or
  the AI provider are off. It replaces the Transcription & speakers and Meeting notes pages (`#transcription` and
  `#notes` still work); echo removal and audio retention are in the Audio group. **GPU helpers** is now its own
  page (it was on Recorders & install). People and names moved to the People pages; Settings links to them. No field
  was renamed, so saving and the API are unchanged.
- Server: **Notes follow the pipeline.** With Speaker labels without a helper set to Queue for a GPU helper, a new
  meeting's notes now wait until the queued speaker labels are done (or failed, or were withdrawn, in which case the
  notes are made from the unlabelled transcript). The wait is recorded on the job, so a restart keeps it. Find speakers
  again and Retranscribe on an old meeting still never create notes.
- Server: **Queue page as a table.** One table of the work (Meeting, Step, Status, Where, Progress, Time; running first,
  then waiting), a table of GPU helpers and a table of recent failures. The queue settings form is gone from the page;
  those settings are under Settings > GPU helpers and Ingestion. `GET /v1/queue` rows gain `pipeline_step`, `status`,
  `status_label`, `since` and `notes_follow`; nothing was removed.
- Server: **no maximum on Wait for a helper.** The wait keeps its 5 second minimum but the 300 second limit is gone
  (Settings > GPU helpers). The sweeper's 15 minute cap on a waiting offer now gives way to a longer wait.
- Server: **Never transcribe on this server** (`compute_never_local`, off by default; Settings > GPU helpers and the
  Queue page's settings). When on, every transcription (calls and in-person meetings), speaker-label job and Find
  speakers request is offered to GPU helpers with no time limit and is never run on the server's processor: not when
  no helper is online, not after a helper fails, goes quiet or runs over, not after a restart (the job is offered
  again). A job that keeps failing on helpers is given up in error after 5 attempts instead of running here. With
  Use GPU helpers off the jobs simply wait, and Settings and the Queue page show a warning. Meeting notes are not
  affected. The wait and "without a helper" settings are marked as not applying while it is on.
- Server: **Queue for a GPU helper**, a third choice for **Speaker labels without a helper** (next to Run on this
  server and Skip). A call is transcribed on the server at once, without labels, and finishes as usual (transcript
  and automatic notes). A speakers-only job then waits for a GPU helper with no time limit and never runs on the
  server's processor; the helper only diarizes, and the labels (and voice profiles) are applied to the existing text.
  Audio retention waits for that job to end. With GPU helpers switched off it keeps waiting, and the meeting shows
  "Speaker labels: waiting for a GPU helper". With this choice a transcription also keeps waiting for a helper that is
  online but busy (no 15 minute cap, no fallback to the server), so a bulk Retranscribe of many meetings no longer
  spills onto the server's processor; with no helper online at all it is transcribed here and the labels are queued.
- Server + helper: **Find speakers again** (meeting menu, and **Find speakers** in the bulk bar; `POST
  /v1/sessions/{id}/find-speakers`). Re-runs speaker labelling on the stored audio and re-labels the meeting's latest
  transcript without transcribing again: the text stays identical, no automatic notes. It runs on a GPU helper when
  one is online (a diarize-only lease: `"transcribe": false`; a helper that ignores the flag returns a full
  transcript and the server keeps its own text), else on the server's low-priority speakers worker (a manual request
  overrides Skip). A newer Retranscribe supersedes it. Needs audio and Speaker labels on.
- Server: **Queue page** (left nav, `/queue`; data from `GET /v1/queue`). Shows what is running now (transcriptions,
  speaker labels and meeting notes, with where each runs, its step, percent and elapsed time), what is waiting and
  why (offered to a GPU helper, waiting with no time limit, queued for this server, queued notes, and how long each
  has waited), the GPU helpers, and recent failures. The settings that decide where work runs (Use GPU helpers, wait
  for a helper, Speaker labels without a helper, the in-person speaker-label choice and wait) can be changed there
  and save like Settings does.
- Server: **Update voice profiles** (meeting menu and bulk bar, only with Voice profiles on; `POST
  /v1/sessions/{id}/reprofile`). Matches a meeting's stored voices against the current people again with no audio
  or diarization: your Yes/Move choices and your own voice stay, automatic links and suggestions are worked out
  again, so people named or merged since are recognised in older meetings.
- Supported recorders are still 0.8.5 to 0.9.0-beta.3: beta.3 replaces beta.2 in the release list (same base
  version), and a beta.2 recorder stays supported and updates to beta.3. The wire protocol and endpoints are unchanged
  since 0.8.9; only the recorder's local upload-claim handling and the server's own pages and routes changed.
- Client: **opening Your notes narrows the recorder to the width it needs, and the notes take the rest.** Dragging
  the pane's edge or resizing the window now changes only the notes (the window grows or shrinks with the edge); the
  recorder keeps its width and both sides fill the window's height. Closing the notes puts the window's width back.
  The pane's width is remembered however it was set.
- Client: **hide the recorder and keep Your notes open.** While the notes pane is open, a button left of the "Meeting
  Notes" title folds the recorder away: the window narrows to the notes, the header's Upload, History and Settings
  become icons, and a red "Recording" mark shows while a meeting is being recorded. Press it again (or close the
  notes, or switch to compact view) to bring the recorder back at its old width. Not remembered across restarts.
- Client: **Update now no longer strands a meeting's upload.** Pressed while a recording is compressing or
  uploading (or just after Stop, before the upload starts), the update waits and installs by itself once the upload
  is done; starting a new recording drops the wait. And an upload cut off by a closed app now resumes as soon as the
  app runs again: a claim left by a process that has exited is taken over at once instead of after 6 hours (also for
  Re-upload a saved recording).
- Server + bridge: **an AI login check.** An expired Claude (or ChatGPT) login used to fail every meeting's notes one
  by one, because `claude auth status` still reads as logged in once the OAuth session has expired. Now a job that
  fails on the login goes back in the queue instead of failing, the queue holds every notes job until the login
  works, and every page shows a banner linking to Settings → AI to reconnect. The server also sends the provider a
  tiny real request (Haiku for Claude) every six hours, and every five minutes while the login is broken; the queue
  continues by itself once it goes through, after a re-login in Settings, or when a job completes. Settings → AI has a
  Login check line with **Check now**, `/health` reports `ai_login`, and the state survives a restart
  (`ai_health.json`). Codex failures now include the CLI's (redacted) error so they can be recognised too. Needs the
  bridge updated together with the server (its new `/v1/bridge/control/verify`); an older bridge only gets the
  job-failure half.
- Client (Windows): **meeting detection works again on PCs where Windows stopped updating its mic-use record.**
  The detector read only the registry's per-app microphone-use timestamps; on some Windows 11 PCs those stopped
  updating (with no OS update), so no call starts or ends were detected. It now also asks the audio system which
  processes have a live capture session (WASAPI), and an app counts as using the mic if either source says so.
  While recording, the client checks that it can see its own mic capture and logs "meeting detection is blind"
  once if it can't for a minute. A detection error is now logged (once) instead of being silently ignored.
- Server: **dropdown arrows sit 12px in from the edge** (a drawn chevron instead of the browser's own, which hugged
  the border), and the client's combo boxes match.
- Server: **voice profiles, an optional way to recognise the same person across meetings.** Settings → Speaker labels has a new Voice profiles switch. It is off by default and needs Speaker labels to be on; while it is off the feature is invisible (no People tab, no names, nothing stored). Stored profiles are kept if you switch it off.
- Server: **the same voice is recognised from meeting to meeting.** Each speaker with enough speech is linked to a person, either one already known or a new unnamed "Person 7". A voice that only nearly matches becomes a new person with a "sounds like" suggestion rather than a guess.
- Server: **the mic track is split too, so people in the room with you become "In room 1", "In room 2".** You stay "You"; very short bits and speaker echo are folded back into you. Meetings with a people count of 1 or 2 skip speaker labelling as before.
- Server: **a People tab in the sidebar.** The list sorts by Most talking, Most recent or Alphabetical. A person's page has their name, optional email and company, voice samples to play, the meetings they were in, and what they said (each quote opens the meeting at that moment). You can merge two people, delete a profile, or move a speaker to another person or unlink it.
- Server: **"sounds like" suggestions on the meeting page.** A speaker that nearly matches someone shows "Sounds like Sarah?" with Yes or Not them.
- Server: **names show up everywhere.** The meeting page shows names (unnamed people appear as "Person N"); the AI notes and Export all use the names of named people only.
- Server: **four read-only agent/MCP tools:** `meeting_notes_list_people`, `meeting_notes_get_person`, `meeting_notes_person_quotes` and `meeting_notes_who_said` ("what did Sarah say in that meeting?"). They answer an error while the switch is off.
- Server: **Delete all voice profiles** in Settings removes every person, voiceprint and voice sample at once. Voiceprints and samples never go into Export all, Notion or agent output.
- GPU helpers: **they need the updated pack.** The server prepares it automatically, as with any pack change.
- Server: **older meetings are not backfilled.** To add one to People, retranscribe it (needs its audio).
- Server: **fix: saving settings through the API no longer resets the speaker-label model and speaker range.** A
  `PUT /v1/settings` that left out `diarization_model` / `diarization_min_speakers` / `diarization_max_speakers`
  put them back to their defaults; the web form and the API now keep whatever a save does not send.
- Server: **`/meeting-notes` now opens the Meetings page** (with any query string) instead of an older duplicate page.
  The old form-style routes `POST /sessions/{id}/retranscribe`, `/delete-audio`, `/delete`, `/find-speakers` and
  `/reprofile` are gone; the web UI and every client already use the `/v1/sessions/{id}/...` routes.

## 0.9.0-beta.2 (dev pre-release)
- Supported recorders are still 0.8.5 to 0.9.0-beta.2: beta.2 replaces beta.1 in the release list (same base
  version), and a beta.1 recorder stays supported and updates to beta.2.
- Server: **the Recorders page always shows Your notes on each recorder card.** With nothing typed it says so; a
  recorder too old to send its notes gets a line asking to update it. Before, the section stayed hidden until text arrived.
- Client: **a notes draft reaches the server at launch.** A draft saved before a restart or an update was only sent
  once you typed again; the recorder now hands it to the control channel as it starts, so the Recorders page shows it.

## 0.9.0-beta.1 (dev pre-release)
Pre-releases run on Mike's own server from the `dev` branch before a release goes to `main`. The version string is
SemVer (`0.9.0-beta.1`, which the recorder's updater orders after 0.8.9 and before 0.9.0); Python packaging reads it
as `0.9.0b1`.
- Supported recorders are now 0.8.5 to 0.9.0-beta.1 (the beta's network layer is unchanged since 0.8.9).
- Client: **Your notes is now Life Dash's notes editor, ported to Qt.** The pane shows the rendered note as you edit it (Markdown is still what is stored and sent), with Life Dash's formatting bar (bold, italic, strikethrough, H1 to H3, paragraph, bulleted, numbered and to-do lists, quote, code block, inline code, link) and its "/" block menu; `**bold**`, `*italic*`, `` `code` `` and `~~strike~~` format as you type, lists behave the Notion way (Enter on an empty item leaves the list, Tab nests, Backspace at the start turns a block back into text), Ctrl+K opens a small link card, and the keyboard shortcuts are Life Dash's. The Preview button is gone, since the editor is the rendered view. The same editor is in the window pane and the window beside the compact bar.
- Client: **Your notes can be made larger or smaller, and the size is remembered.** Drag the pane's left edge (arrow keys work too once it has focus; double-click it for the default width); the compact-view notes window resizes like any window. Both are saved in the config's `ui_state` (`notes_width`, `notes_popout_size`) and used next time.
- Settings → About has a Check for updates button; the result is shown in words (up to date, update available, or why the check failed). The server's Recorders page already had this command.

## 0.8.9
- Client + server: **Your notes, a notes scratchpad.** The recorder's new notes button (header, and on the compact bar) opens a "Your notes" pane for links and your own notes during the meeting; in compact view it opens beside the bar. It is Markdown, rendered the way Life Dash renders its notes (headings, lists and task lists, code, tables, quotes, links; up to 200,000 characters): a Preview button in the pane shows it rendered, and the meeting page shows it rendered under "Your notes", always open. It saves as you type (`scratchpad.md` in the recording's folder, or a draft that the next recording takes over), so a crash loses at most the last pause in typing, and links open with Ctrl+click (Cmd+click on macOS) while editing. The server sees it live: the recorder sends the text as it is typed, so the Recorders page and the live meeting view show it while the meeting is still going. The text goes to the server with the recording (`meta.scratchpad`, kept exactly as typed), the AI notes get it in the same request as the transcript, as context written by you (links kept under a "Links" heading), Export all adds `<name> - your notes.md`, the agent API and MCP `get_meeting` return it as `scratchpad`, and Combine joins every part's notes. Links follow Life Dash's rule (http(s), sentence punctuation stays text).
- Supported recorders are now 0.8.4 to 0.8.9.

## 0.8.8
- Client (Windows): **Bluetooth headsets record the other side during a call.** A Bluetooth headset is two outputs, `Headphones (X)` and `Headset (X)`; once a meeting app opens the headset's mic the call plays through `Headset (X)`, and recording `Headphones (X)` alone heard nothing of the other side (a user had to disconnect his earbuds to get a recording). The system track now records both outputs of the same headset and adds them up, so the other side is there whichever one the call uses; an extra output that goes away is dropped and the recording carries on.
- Client (Windows): **Automatic never opens a Bluetooth headset's microphone unless your meeting apps use it.** Opening "Headset (X)" puts the earbuds in call mode, where anything sent to "Headphones (X)" is lost; a recorder that was the only app using that mic muted the user's own call. Automatic now records from a headset mic only when Windows lists it as the communications default (the microphone Teams, Zoom and the rest use), and otherwise takes that microphone, or any other, instead. The choice is written under the device names in the window (the whole reason on hover), to the log, and into the recording (`session.json`). Picking a headset mic by hand still works; Settings then shows what it means for the call.
- Client: **a track that hears nothing is flagged.** When one side has been digitally silent (not just quiet) while the other carries sound, the device strip says so and names the device: after 1 minute for your microphone, 3 for the other side. It is logged once too, so a sent log shows it.
- Client: an older GPU support install that ends after a newer one started only cleans up itself; it used to delete the newer one's installer ("Signal source has been deleted" in 0.8.6 logs), and a second install never starts while one is running.
- Client (macOS): **a menu-bar icon beside the window.** The recorder now keeps an icon in the Mac menu bar for as long as it runs, alongside its window (before, it was the window or nothing). Its menu shows the status (Idle, Recording 12:34, Finishing), Start / Stop recording, Mute you / Mute them while recording, Show Meeting Notes and Quit Meeting Notes; the icon turns red while recording. Closing the window still quits, as before.
- Client: **a server address that redirects is followed.** When the configured address answers with a redirect to the same API call elsewhere (http to https, `meeting.lan` to a public name), the recorder moves to the new address for the rest of the session: API calls, uploads, the live stream and remote-control websockets, the GPU download and update checks all go there. A small call is repeated at once; a streamed upload is retried by the upload queue straight to the new address. Redirects to another page (a login page) are not followed, https is never moved to plain http, and if the new address stops answering the configured one is tried again.
- Client: **the GPU helper follows a new server address.** Changing the address in Settings (or from the server's Settings page) restarts the helper's runtime on the new address; before, it kept working for the old one until the app restarted. A stopped runtime's late exit no longer looks like a crash of its replacement.
- Client: **the GPU helper starts again under the installed recorder.** The runtime was started in the recorder's install folder, so its Python 3.12 picked up the recorder's own 3.13 `_socket.pyd` and quit at once ("Module use of python313.dll conflicts"), over and over. It now starts in its own work folder with the current folder kept off its import path (`PYTHONSAFEPATH`), and its environment comes only from the runtime's own settings.
- Server: **echo from your speakers is taken out of the mic before transcribing.** Without a headset the mic also hears the other side, and their words showed up twice (as Them and again as You). The server now runs WebRTC's echo canceller (AEC3, from the `livekit` package) over the mic track, using the system track as the reference, before it is transcribed here or by a GPU helper. The echo's delay is measured from the recording itself, so Bluetooth delays, a mic that runs ahead and clock drift over a long meeting are handled; a headset recording, where no echo is found, is left exactly as it is. On a simulated speakerphone call, 79 duplicated lines went to none. Played audio and the Both sides mix keep the original. Settings → Transcript → Echo from your speakers (on by default); applies to new transcriptions and Retranscribe. Needs a server image rebuild (new dependency).
- Server: shutting down also stops the phone-recorder preparation worker, and `app.state.stop_workers()` stops every background thread of an app. Tests now stop the workers of apps they leave running: hundreds of leaked threads made late tests crawl on macOS until the run looked hung.
- Tests: no test can open the real microphone, loopback or ScreenCaptureKit stream (a full `doctor` run in the suite held the Mac's mic). The hang watchdog writes the stuck test's own thread and a thread count first, so a run with 100+ leaked threads still shows where it stopped.
- Supported recorders are now 0.8.3 to 0.8.8.

## 0.8.7
- Client: **GPU support installs reliably.** Only one Meeting Notes process can install it at a time (a lock in the GPU folder; a second copy waits and tries again in 5 minutes instead of fighting over files). A download cut off by a server restart or a network blip is retried after 5 minutes rather than backing off to 2 hours. Closing or updating the app mid-install now cancels the install cleanly instead of crashing its thread ("Signal source has been deleted").
- Web: exporting several meetings puts each one in a folder named **<meeting name> - <date>** (the meeting's local date, YYYY-MM-DD).
- Supported recorders are now 0.8.2 to 0.8.7.

## 0.8.6
- Client + server + phone recorder: **number of people.** A small People box next to the meeting name (blank = Auto) tells the server how many people are in the meeting, including you; the server then separates exactly that many speakers instead of guessing (a 1:1 is no longer split into Them 1/2/3). A meeting page has Set number of people in its menu (and offers to retranscribe), and the API accepts `people_count` on `PATCH /v1/sessions/{id}` and `PATCH /v1/live/{id}`.
- Supported recorders are now 0.8.1 to 0.8.6.

## 0.8.5
- Web: **Export all.** A meeting's **...** menu has **Export all (.zip)**, and the Meetings page bulk bar has **Export**: they download the transcript and the notes as Markdown in one zip (a folder per meeting when several are selected). Meetings with nothing to export are skipped. New `GET /sessions/{id}/export.zip` and `POST /sessions/export.zip` (`{"ids": [...]}`).
- Client + server: **recordings are about 98% smaller.** After a recording ends, each track is compressed to Opus (24 kbps, 16 kHz mono): about 2 MB per 20 minutes instead of 120 MB. The recorder keeps only the compressed copy on this computer and uploads it; the server decodes it (ffmpeg) and carries on exactly as before, so transcripts are effectively unchanged. Recordings made offline are compressed in the background while the app is idle, and the compressed copy is verified (decoded and length-checked) before the original is removed. Turn it off with **Settings > Recordings > Keep recordings compressed (Opus)** (also on the server's recorder Settings page). Re-upload, history, clean-up and `meeting-notes transcribe` all accept the compressed files.
- Server: **`/health` lists `track_formats`** (`pcm16`, `opus`), and the track upload route accepts an Ogg Opus body (`Content-Type: audio/ogg` or `X-Track-Format: opus`; 413 over the size limit, 422 if it cannot be decoded). A recorder uploads Opus only to a server that advertises it; to an older server it decodes the Opus copy and sends PCM as before. A recorder without the audio library (an older Mac build) keeps WAV and PCM.
- Client: **compact view.** A Compact button shrinks the recorder to a slim bar on the right edge of the screen: recording dot and time, microphone and meeting-audio level meters, mute buttons for each, and Expand (or double-click). It stays on top, can be dragged up and down, and comes back in compact mode if the app was closed that way.
- Client: **pop-ups sit at the top centre of the screen** as one wide, short strip (the call prompt, the auto-record card, the call-ending and stop-suggestion prompts, the stopped-automatically notice). In the bottom right they covered Teams' and Windows' own notifications.
- Client: **a recording picks up the meeting's real name.** Calls are named from their window when they start, often before the meeting shows its title ("Browser call 11:00 AM" instead of "Mike/Tara 1:1"). For the next 90 seconds the recorder keeps looking, and when the meeting's own name appears it renames the recording -- only while the name is still the one it filled in (typing a name, or renaming from the web, stops it).

- Client: **a GPU support download no longer fails when antivirus is scanning it** ("The process cannot access the file because it is being used by another process"): each file is retried for up to 30 seconds.
- Supported recorders are now 0.8.0 to 0.8.5 (the 0.7.17 Mac recorder is no longer accepted).

## 0.8.4
- Client + GPU helper: **pause / recording / stop always reach the GPU helper.** On Windows, replacing the helper's control file failed with "Access is denied" whenever the helper was reading it at that instant, and the recorder dropped the change silently; and a read that failed mid-replace looked like "all off" to the helper for a moment (a spurious resume or "not recording", so it could start a big job while this PC recorded). The write now retries briefly and a failed read keeps the last state. The helper side arrives with the next GPU support update (automatic since 0.8.2).
- Server: **a job a GPU helper fails goes to another helper first.** If a helper goes quiet, gives up or runs too long on a meeting, the server offers it once to another online helper (the one that failed can't take it again) before transcribing it itself. With one helper, nothing changes.
- Supported recorders are now 0.7.17 to 0.8.4.

## 0.8.3
- Client: **recordings end again when a Teams or browser call ends.** Teams and browsers put an unread count in front of window titles ("(1) Chat | Project team | Microsoft Teams"); with it, the recorder did not recognise the Chat view and treated it as a live call window, so after the call it kept recording ("mic released but call window still open") until the 5-minute silence prompt. Unread counts are now ignored, so the recording ends about a minute after the call.
- Server (patch): **People and names entries can be edited** in place (Edit next to Remove): fix a person's spelling, or both spellings of a correction. Renaming a person also updates the corrections that point to them. New `POST /v1/names/edit`.
- Supported recorders are now 0.7.16 to 0.8.3.

## 0.8.2
- Client: **GPU support installs and updates itself.** With **Let the server use this machine** on, the recorder downloads and installs GPU support from the server in the background, and updates it whenever the server's helper download changes (after a server update). Nothing needs pressing; closing Settings no longer stops an install (it used to cancel it). It starts 15 seconds after the app opens, checks every hour and right after a settings save, waits while this PC records, and retries a failed install after 5 minutes, 30 minutes, then every 2 hours. **Install now** / **Update now** starts it at once; **Cancel** pauses the automatic retries until the next settings change or restart; **Remove GPU support** also turns the helper off. The tray and Settings show "Installing GPU support: 40%".
- Client: **a helper key the server rejects is replaced automatically.** If the server refuses this PC's helper key (revoked, or a key from another server), the recorder registers again and restarts the helper instead of stopping with an error.
- Client: **old recordings are cleaned up sooner.** The keep-recordings and delete-after-upload clean-up now looks 20 seconds after the app opens (was 2 minutes), every hour (was every 6 hours), right after Settings are saved, and as soon as a recording ends when a look was skipped because it was recording.
- Server (patch): **add people and corrections by hand.** Settings > Transcription & speakers > **People and names** has **Add a person** and **Add a correction** (heard as → correct spelling), so names can be taught before a meeting instead of only by editing a summary's Participants. They apply at once (new notes and transcription), like Remove. New `POST /v1/names/add`.
- Server (patch): **changing a note type's Notion page can move its notes.** **Copy existing notes** now also copies notes that are on a previous Notion page (moved to the current one; the old copy is removed) and notes whose Notion copy was deleted (copied again), not only meetings that were never copied. After the page is changed, the note type's card says how many meetings' notes are still on the previous page, with **Move them here**.
- Web (patch): **Check for updates on the Recorders page tells the truth.** The recorder answers as soon as its check starts, so the page used to say "up to date" before the check finished and then show **Update now**. It now says "Checking for updates on …" and reports what the recorder found once its state says so (or "up to date" after 20 seconds).
- Web (patch): **the Meetings page filters by note type with tabs** across the top (All, then one tab per note type; shown once there are two or more types) instead of a drop-down. Its **Upload transcript** button is gone: adding a transcript is on Home (**Add a meeting**).
- Server (patch): **the Recorders page remembers recorders that are offline.** The server keeps a small list of recorders that have connected before (`recorders/seen.json`). `GET /v1/recorders` and the events feed now also carry `seen`, and `DELETE /v1/recorders/seen/{id}` forgets one.
- Web (patch): **offline recorders show greyed, live ones come first.** Recorders that connected in the last 30 days but are not connected now are listed under **Not connected** with when they were last seen and a **Forget** button; ones gone longer are in a collapsed **Inactive** group. Live recorders are always on top, the ones recording first.
- Server (patch): **record in-person meetings on your phone.** `https://<server>/record/` is a phone recorder for iPhone (Safari) and Android (Chrome), installable to the Home Screen and usable offline: audio is saved on the phone every few seconds and uploaded while recording, recovered after a closed tab or lost connection, and kept on the timeline across screen locks. Optional live transcript (off by default) shows on the page and the Meetings page. The meeting is one in-person track with speakers labelled Speaker 1, Speaker 2; it waits up to 5 minutes for a GPU helper, then labels speakers here on a low-priority worker before notes start (Settings > Transcription & speakers > In-person meetings). New `/v1/mobile/...` routes, stream tickets for the live socket, `MEETING_NOTES_ALLOWED_ORIGINS`, and an Origin check on the phone recorder's signed-in changes. See `docs/mobile-recorder-design.md`.
- Server (patch): **signing in returns to the page you opened** (`/login?next=`, local paths only), and the sign-in cookie is `Secure` when signing in on an https address.
- Supported recorders are now 0.7.15 to 0.8.2.

## 0.8.1
- Server + client: **live transcription on GPU helpers.** Each finished live sentence goes to a connected GPU helper first and comes back in well under a second (GTX 1060: ~0.4 s with Better, ~1.2-1.7 s with Best); if no helper answers within 2.5 s the server transcribes it as before. Helpers keep one outbound connection (`/v1/compute/live`); the server never holds its model lock while waiting, cancels by request id, and skips a helper for 60 s after repeated failures. While the helper's own PC records, it pauses the big transcription jobs but keeps serving live; the tray's Pause stops both. Settings and Recorders show "Live: on · avg 0.4 s".
- Server: **Good / Better / Best.** Transcription models are shown as Good (base.en), Better (small.en) and Best (large-v3-turbo); live models also offer Fastest (tiny.en). The model for **this server** and the model for **GPU helpers** are now separate, for transcripts and for live: defaults are Best on GPU helpers for transcripts (a 21-minute meeting in ~45 s on a GTX 1060, as fast as Better) and Better for live. The helper download carries every GPU model.
- Server: **effort for AI notes.** Meeting notes has an Effort setting next to the Claude model (`--effort`: low to max) and the Codex model (reasoning effort: minimal to xhigh). Default keeps each CLI's own.
- Web: **Settings is split into pages**: General; Transcription & speakers; Meeting notes; Notion & AI access; Recorders & install (GPU helpers live here). One Save; `/settings#<page>` links to a page; the last page is remembered. A save from a stale tab is refused with "Settings were changed somewhere else; nothing was saved. Reload to see them." (also `PUT /v1/settings` with an old `revision` returns 409), and checkboxes post explicit values so a page can never switch something off by leaving it out.
- Web: the Recorders page has an **Install a recorder** link.
- Server + client: **speaker labels have their own progress step.** The processing checklist (web meeting page and the recorder's Processing panel) shows **Identify speakers** between Transcribe recording and Transcript ready, with its own percentage, when the meeting gets speaker labels; badges read "Transcribing 42%" or "Identifying speakers 30%". The transcribe step shows transcription only. Speaker labelling reports its own progress (it used to sit at a fixed 90%), and a GPU helper sends progress about every 2 seconds instead of every 15, so both bars move smoothly. `GET /v1/jobs/{id}` adds `stage`, `transcribe_percent` and `speakers_percent`; the session's `pipeline.transcription` adds `stage`, `transcribe_percent` and `speakers`. Helpers before 0.8.1 still work and show one Transcribing bar.
- Server: **the one-line Windows install no longer fails with "Unexpected attribute 'CmdletBinding'"** on some machines: the installer script dropped its unused parameter header.
- Supported recorders are now 0.7.14 to 0.8.1.

## 0.8.0
- Server + client: **GPU helper.** A Windows PC with an NVIDIA graphics card (4 GB or more) can do transcription and speaker labels for a CPU-only server. On the server, **Settings > Transcription > GPU helpers** has **Use GPU helpers**, the wait for a helper, **Speaker labels without a helper** (run here / skip), a **helper pack** with **Prepare download** (the server downloads Python, the CUDA libraries and the models once; helpers download only from the server) and a list of connected helpers with **Revoke**. In the Windows recorder, **Settings > GPU helper** shows the graphics card and has **Install GPU support** / **Update GPU support** / **Remove GPU support**, the on switch and **Start when I sign in to Windows**. The helper polls the server (nothing to open on the network), claims a job with a time limit, and the server takes the job back and does it itself if no helper claims it, the helper goes quiet or runs too long. The helper pauses while that PC records. A tray icon shows the status with **Pause GPU helper** and **Quit**; closing the window keeps the helper running in the tray. Each helper has its own key (`mnh_...`) that only allows GPU work and can be revoked. Meetings show where they were transcribed. New endpoints under `/v1/compute/` (`docs/reference.md`, `docs/gpu-helper-design.md`); guide in `docs/gpu-helper.md`.
- Server: **GPU server Docker option.** `docker/docker-compose.gpu.yml` (used with `docker-compose.yml`) runs transcription and speaker labels on the NVIDIA GPU of the server's own machine. New `INSTALL_GPU` build argument and `MEETING_NOTES_DEVICE` / `MEETING_NOTES_DIARIZATION_DEVICE` settings in `docker/.env` (`cpu` or `cuda`). When CUDA is not usable the server logs a warning and uses the CPU.
- Server: **the speaker-label settings are shown again.** Speaker labels (on/off, model, minimum and maximum speakers) are back in Settings > Transcription, in the GPU helpers block, no longer hidden.
- Server: **a speaker-label failure no longer fails the transcript.** If the speaker-label step raises an error, the transcript is kept with plain "Them" and the job records a `speaker labels failed: ...` warning.
- Server: **meetings queue for a busy GPU helper.** New jobs are offered to helpers the moment they are queued (a dispatcher thread, so a long run on the server never holds them back) and claimed oldest first. While a helper is online, even busy, meetings wait their turn on the GPU instead of falling back to the server's processor; a job falls back when no usable helper is online for the "Wait for a helper" time, after 15 minutes of waiting, when the helper goes silent for a minute, or when it runs over its time budget. Tested with three meetings (92 minutes of audio) at once on a GTX 1060: all on the GPU, done in about 8 minutes.
- Server: **GPU detected but CUDA libraries missing falls back to the CPU.** A server whose driver reports a GPU but that lacks cuBLAS/cuDNN (or `MEETING_NOTES_DEVICE=cuda` with no usable GPU) transcribes on the CPU with a logged warning instead of failing the job; older GPUs (GTX 10xx) automatically use `int8_float32`.
- Server: **speaker labels read WAV tracks directly** instead of through torchcodec/FFmpeg, which a Windows GPU helper does not have.
- Server: **PyAV pinned below 19.** PyAV 19 removed an argument faster-whisper 1.2 passes, which broke decoding on a fresh install.
- Server: GPU-helper lease details (attempt id, lease hash) never appear in API responses.
- Supported recorders are now 0.7.13 to 0.8.0. The GPU helper is Windows-only; the Mac app stays on 0.7.17.
- Server: **Notion heading for an unnamed meeting.** A meeting that was never named now gets the notes' title as its Notion heading instead of its session id.
- Server: **better default note prompts** (Standard, Quick notes, Detailed webinar). Notes now keep the details that matter (prices, quantities, names, technical specifics, what each side wants), state each fact in exactly one section instead of repeating it across summary, key points, risks and next steps, and catch every commitment as an action item ("You" for the person who recorded, a named owner only when the transcript makes it clear, due dates only for stated deadlines, relative dates resolved from the meeting date). The prompts explain the transcript's labels ("Them" mixes everyone else, including the recorder's colleagues; microphone echo), fix obviously misheard product and company names or mark unclear ones "(?)", drop small talk and meta-commentary, and keep figures tied only to the product the transcript supports. Quick notes has hard length limits; Detailed webinar drops event logistics and invented Q&A. Servers that already saved their prompts keep them; the new text is the default for new installs.

## 0.7.17
- Server (patch): **the live transcript's model is a setting.** Settings > Transcription has a **Live transcription** part with **Live model** (Same as the final transcript, tiny.en, base.en, small.en) and **Live beam size**, next to the final transcript's Model and Beam size; a change applies from the next live sentence. Defaults stay base.en / 1 (`MEETING_NOTES_LIVE_MODEL` only sets the starting value). Both beam size fields have an **i** that explains beam size and how to choose it (hover, keyboard focus or tap). Saved as `live_model` / `live_beam_size`; a save that leaves them out keeps them.
- Server: **transcription progress moves continuously.** The percentage used to jump 0%, 50%, 100% on a two-track meeting, with long gaps; it now follows faster-whisper through each track (the share of the audio decoded so far), weighted by each track's length, so a 70-minute meeting with a short mic track and a long system track advances smoothly and never goes backwards. It stays below 100% until the transcript is really written, and store writes are throttled (about every half a percentage point or 2 seconds). Transcribers get an optional `progress=` callback; ones that do not accept it still work (per-track steps as before).
- Client: **"server unreachable" no longer sticks after the server comes back.** While waiting for a transcript, every successful answer from the server now clears an earlier "server unreachable" (a server restart, a network blip) from the recording's status, so the status line and Processing section show the live "Transcribing 42%" instead. A transcription that really failed keeps its error.
- Web: **a "Both sides" player on the meeting page** (meetings with both tracks). It is first above the "You · microphone" and "Them · system audio" players and plays both tracks mixed into one (`GET /sessions/{id}/audio/mixed`, same sign-in as the other audio). The mix is mono 16-bit WAV at 16 kHz (or the lower rate of the two tracks), each side at half volume so it cannot clip, built in blocks so long meetings stay light, cached next to the recording as `mixed.mix` (rebuilt if a track changes, never counted as a track or as audio) and removed with the audio (Delete audio, retention, Recently deleted purge). Seeking works. With only one track recorded it plays that track.
- Supported recorders are now 0.7.12 to 0.7.17.

## 0.7.16
- Client: pressing **Stop now** on a stop countdown (meeting closed, on the hour, silence, goodbyes) no longer shows the **Recording stopped automatically** card or flashes the taskbar: you stopped it. The card is only for a countdown that runs out.
- Server: **the live transcript is closer to realtime.** It looks for finished sentences every 2 seconds instead of 8, cuts long stretches of talking every 9 seconds instead of 20, and runs its own lighter model (`base.en`, single-pass decoding) so it keeps up; the transcript that is kept still comes from the configured model over the whole recording. `MEETING_NOTES_LIVE_MODEL` changes the live model (`same` = the configured one); if the live model can't be loaded, live falls back to the configured model.
- Web: the notes' **Meeting** box is quieter: the day, one time line ("5:48 – 6:35 PM · 47 min"), and the talk-time bar with just a You / Them key; the percentages and minutes show on hover (or tap/focus on a phone).
- Web: the download buttons say what they download: **notes.md** on the notes view and **transcript.md** on the transcript view, each with the download icon (the file names stay visible on a phone).
- Web: **the Meetings list is now a full-width table.** Columns are **Name** (with device, length and board id in small type underneath), **Time**, **Transcript**, **Note type**, **In Notion** and **Notes generated**, with a header row that stays at the top while you scroll and day groups ("Today", "Yesterday", ...) as full-width rows. The coloured pills and buttons are gone: Transcript is plain text with a small status dot ("Complete", "Transcribing 42%", "Needs attention" in red), a note type that is only planned (no notes yet) is shown in italic grey, In Notion and Notes generated show a check mark and "Yes" (each a link to the Notion page or the notes), "Sending…", "Building…", "Failed" in red or a dash, and **Generate** is a quiet text link in the Notes generated cell. The page uses the full width of the window. On a phone each meeting becomes a card (name and time on top, then Transcript, Note type, In Notion and Notes as a labelled two-column grid) with no sideways scrolling. The list is exposed to screen readers as a table with column headers.
- Web: **the Install page is organised by platform.** It has a **Windows** and a **macOS** section (with a Jump to line at the top, and the existing `#macos` anchor), each with Install, Download only, Update, Uninstall and Troubleshooting. The Mac section now has a **Download the Mac app (.zip)** button (`/install/MeetingNotes-macOS.zip`, disabled with a note when the server has no Mac package) with the manual steps (unzip, move to Applications, right-click > Open, permissions, Settings), and a **Mac uninstaller**: `curl -fsSL <server>/install/uninstall-mac.sh | bash` (also a download button). The script quits the app, removes it (only when its bundle id is ours) and `~/.meeting-notes` (`--keep-settings` keeps it), resets the app's Microphone and Screen Recording permissions, never deletes recordings, and refuses, changing nothing, if the recordings folder is inside something it would remove. Safe to run twice.
- Client: **Delete from this computer after the server confirms the upload.** A new setting in Settings > Recordings > Local recordings (off by default, also editable from the Recorders page's Settings panel). When on, a recording's audio folder is removed (Recycle Bin / Trash, like **Clean up now**) as soon as the server has confirmed it received the whole recording, without waiting for the transcript; the transcript and notes stay on the server, and it overrides **Keep recordings** for recordings that upload successfully. Confirmed means the upload finished and the server's own record of the meeting shows the upload complete with the audio present and the meeting not in its trash. A failed or partial upload, an unreachable server, a rejected password, the recording in progress or one queued for upload is never deleted; if the server cannot confirm, the recording is kept and retried at the next start-up or six-hourly clean-up pass. `delete_after_upload` in `config.json` (`docs/reference.md`).
- Web + agent API: **search filters.** The Meetings and sidebar search (and `GET /v1/sessions?q=`, `GET /api/v1/meetings?q=`, `GET /api/v1/search?q=` and the MCP `meeting_notes_search` / `meeting_notes_list_meetings` tools) understand `title:`, `attendee:` (also `attendees:`, `person:`), `notes:` and `transcript:`, each with a word or a "quoted phrase" (`attendee:"Tara Swart"`). `title:tara` finds meetings whose name (or notes title) contains tara, even when every transcript mentions her. Filters AND together and are case-insensitive. Plain words still search the name, computer, meeting number (`M-0142`) and transcript, and now also the notes and attendees; every plain term has to match somewhere. An unknown `foo:bar` is plain text and an empty filter (`title:`) is ignored. A **?** beside the search box lists the filters (click one to insert it), and "No meetings found" names the terms that were searched.
- Server: the search index gains the text of each meeting's newest finished notes (`notes_text`, full-text) and their title and attendees (`notes_meta`). They follow regeneration, participant edits, renames, delete and Recently deleted/restore, and an index from an older release is filled in at startup. List queries still never read files.
- Web: **find inside a meeting.** A search button in the meeting's toolbar (or Ctrl/Cmd+F, or `/`, while a meeting is open) opens a find bar for the view you are reading, Notes or Transcript. It highlights every match, shows "3 of 12", and Enter / Shift+Enter or the arrows step through them. Esc closes it; switching between Notes and Transcript searches the new view. A meeting opened from a list search starts with that search's words (or `notes:` / `transcript:` text) already found, and a link can carry `?find=`.
- Supported recorders are now 0.7.11 to 0.7.16. 0.7.16 ships without a Mac build (the Mac stays on 0.7.14).

## 0.7.15
- Web + client: **the app's Settings can be changed from the Recorders page, and it is the same Settings.** **Settings** on a recorder's window now opens a panel with the app's own Settings dialog: the same pages (General, Audio, Recordings, Server, Remote control, About), sections, field names, help text, choices and order, with the same rules (for example **Also end** only shows while **Start recording automatically** is on) and a Save / Cancel that saves everything or nothing, with the problem shown next to the field it is about. Audio devices are the ones connected to that computer, note types the app's own list, and the save folder is typed (the app checks it exists or can be created and written). The server address asks first ("This computer will reconnect to ...; if it is wrong the recorder will drop off this page") and so does turning **Allow control from the server** off ("You will lose remote control of this computer"). The sign-in password is never sent to the server and cannot be changed from it (the page only says Set or Not set). The panel is drawn from a description the app sends (`client/settings_schema.py`), the same one its own dialog is built from, and a test fails if a setting is in one and not the other. The app shows "Settings changed from the server" and refuses a change while its own Settings window is open or remote control is off. Apps older than 0.7.15 keep today's note ("can only be changed on the computer it runs on") with the links to the server's recorder settings and Client logs. New remote commands `get_settings` and `set_settings` (capability `settings`; `docs/reference.md`).
- Client: **an auto-recorded call now stops when its meeting closes**, whatever the Auto end choice. The **Stop when the meeting closes** setting (Settings > General > Meeting detection, on by default, now always shown) applies to prompted and auto-recorded calls alike: once the call window is gone, the mic is released and the call audio has been quiet, the usual countdown runs and the recording stops. The **Auto end** choice is now **Also end**, an extra rule on top (**Nothing else**, **On the hour**, **After 30 seconds of silence**, **When people say goodbye**); whichever fires first stops it. A saved **When the call ends** becomes **Nothing else** with the close rule on. **Disable auto end** turns both off for that recording; **Keep recording** on the close countdown turns off only the close rule. The card and recorder strip (and the Recorders page) describe the combination, e.g. "Auto end when the meeting closes or on the hour (3:00 PM)". A call that shows up again during that countdown cancels it: Teams' "Meeting join" window closing as the meeting opens in another window looks like the call ending, and used to be able to stop the recording minutes in.
- Client: **a Processing section shows where each recording is.** Below the recorder card, one row per meeting waiting to upload or be transcribed (oldest first, three at most, then "and N more") with its name, a status badge in the server's wording ("Uploading 42%", "Transcribing 63%", "Queued", "Waiting to upload", "Retrying at 3:05 PM", "Upload failed") and a thin progress bar; the current meeting also lists the server's three steps (Upload audio, Transcribe recording, Transcript ready) with status dots, which fold away in a short window or with **Hide details**. A failed upload shows its reason and a **Retry** button. The section is hidden when nothing is waiting, and the status line no longer repeats the upload counts and percentages.
- Client: **a device that drops out and comes back mid-recording is picked up again.** A track moved to another device because its own disappeared (for example the headset's speakers falling back to the laptop's when the headset blinks off) now moves back to the original as soon as it is offered again, instead of staying on the fallback until the recording is restarted. A device that stops delivering without reporting an error (common for a removed Windows audio endpoint) is now noticed after 2 seconds of silence while the scan no longer offers it: it is shown as lost, moved to the fallback (unless a device is chosen in Settings), and reopened fresh once it is back.
- Client: **auto end is hard to miss.** The countdown before an auto end and the stop itself flash the taskbar until the window is used, and a stop by auto end leaves a **Recording stopped automatically** card (with the reason and time, **Open Meeting Notes** and **Dismiss**) that stays until dismissed. Prompts and cards now open on the screen under the mouse instead of always the primary screen (with a docked laptop that could be a screen nobody was looking at).
- Web: **meeting details on the notes.** The notes view shows a **Meeting** box with the start and end time (and length) and the talk time split between you and the other side, with a bar. Talk time is the total seconds of transcribed speech per side (your microphone vs system audio), so silence counts for nobody. The same "Meeting details" (start, end, talk time) are in the downloaded notes, the Meeting notes page and the Notion copy. `GET /v1/meeting-notes/{id}` gains `stats: {started, ended, duration_sec, talk: {me_sec, them_sec, me_pct, them_pct}}` (talk is null for an imported transcript with named speakers).
- Web: **Download .md on the transcript view** too (it was only on the notes view). The file is named after the meeting ("<name> - transcript.md"), its title is the meeting name and it starts with the same meeting details.
- Web: the **Meetings** and **Home** lists show each meeting's note type as a chip: the type the notes were made with, or (dashed) the type they will use: the recorder's tag, else the default. `GET /v1/sessions` items gain `note_type: {id, name, applied}`.
- Web: **filter the Meetings list by note type** (**All note types** beside **All states**, shown once there is more than one type). It uses the same rule as the chips: the notes' type when notes exist, else the recorder's tag, else the default (a type that was deleted counts as the default). `GET /v1/sessions?note_type=<id>`. The search index stores each meeting's tag and its newest notes' type (`note_type_tag`, `review_statuses.template_id/template_name`); an index from an older release is rebuilt once at startup to fill them.
- Web: **Home is first** in the sidebar and the phone's bottom bar (Home, Meetings, Recorders, Settings).
- Server: the macOS update manifest announces the version of the app actually in the published `MeetingNotes-macOS.zip` (from its `Info.plist`), not the server's version, and the Recorders page's "behind" badge for a Mac uses it too. A release can then ship without a new Mac build: Mac recorders stay on the published version instead of reinstalling it over and over. 0.7.15 ships without a Mac build (the Mac stays on 0.7.14).
- Supported recorders are now 0.7.10 to 0.7.15.

## 0.7.14
- macOS: **quitting now always releases system-audio capture.** A ScreenCaptureKit stream that was still open when the app closed (a recorder thread stuck mid-stop, a stream whose start timed out and then completed late) could keep macOS's purple screen-recording indicator on after Meeting Notes was closed. Every open stream is now tracked and stopped on quit (Cmd+Q, Dock Quit, last window closed, or interpreter exit), after any recording in progress is saved; a stream that times out or fails while starting is stopped instead of left running, and a macOS-only watchdog force-exits a process that is still alive 8 seconds after quitting began. Closing the window while idle already released everything (the pre-recording meter never opens system audio on macOS); behaviour during a recording and on Windows is unchanged.
- macOS: **closing the lid during a recording.** The app now listens for sleep and wake: on wake, a recording in progress gets a fresh system-audio stream straight away (the gap is padded with silence like any reopen and logged as `system-wake`) and the stream that was open across sleep is stopped while the Mac is awake. A stream whose stop macOS doesn't confirm (common right around sleep) is no longer forgotten: it is stopped again in the background on the next open, on wake, and on quit, so it can't keep the screen-recording indicator on after the recording or the app has ended.
- Web: **pick the Notion page from a tree.** In Settings > Note types, **Save to Notion** now loads the pages the integration can see and shows them as a tree with the same hierarchy as Notion (expand/collapse, search that keeps the ancestors visible, **Refresh**, **None**), instead of asking for a pasted link. **Page not listed? Paste a link** still accepts a link or id. Only pages shared with the integration appear. New `GET /v1/notion/pages` (`?refresh=1` skips the one-minute cache) returns `{connected, items: [{id, title, url, parent, icon}], error}`; database rows and trashed pages are left out, and a page whose parent isn't shared shows at the top level. Web only: not exposed to agents.
- Web: **saving settings keeps your place.** Save settings used to reload the page at the top; it now comes back to the same spot (kept relative to the note type or section you were looking at), with the note types you had open still open, and shows "Settings saved." (or the error) as a toast as well as the banner at the top.
- Web: **delete uploaded client logs.** Settings > Client logs has **Delete** on each bundle and **Delete all** (both confirmed). New `DELETE /v1/client-logs/{computer}/{name}` and `DELETE /v1/client-logs` (`{"deleted": n}`); a computer with no bundles left disappears from the list. The help text now mentions the Mac app too.
- Supported recorders are now 0.7.9 to 0.7.14.

## 0.7.13
- Client: renaming the meeting while it records now renames the saved meeting (the name as it reads at Stop wins), and the name field clears once the recording finishes.
- Server (patch): **updates follow the address the client uses.** `client-manifest.json` and `client-manifest-macos.json` now point `installer.url` at new update-only scripts, `/install/update/client-agent.ps1` and `/install/update/mac.sh`, rendered with the address the request arrived on (hash and size match that script) instead of the saved Server address. A recorder that reaches the server by IP because it cannot resolve the LAN name now downloads the package from, and keeps, that address instead of being switched to the unresolvable saved one. The browser and one-line installers (`/install/client-agent.ps1`, `/install/mac.sh`) still use the saved Server address. Already-installed clients (0.7.7 and later) pick this up with no update, because they follow the manifest's installer URL.
- Windows installer: every run appends timestamped steps, errors and a final OK/FAILED line to `%USERPROFILE%\.meeting-notes\logs\update.log` (kept under 256 KB; included in the Logs bundle). The macOS script already logged under the in-app updater and now documents that it always logs. The client logs the manifest URL, installer URL, sizes, verification result and the reason for any update failure.
- Web: **edit the participants on a meeting summary.** In the notes view, **Edit** beside Participants lets you fix a spelling, remove someone or add someone (Save / Cancel; Escape cancels). Renaming a person also fixes the old spelling in that summary's notes text and action-item owners (whole word, case-sensitive; the transcript and title are never touched), and a meeting already in Notion is re-synced. The first edit keeps the AI's original output (`ai_payload` on the review); regenerating the notes drops the edit and uses fresh AI output. New `PUT /v1/meeting-notes/{id}/participants` (`{"participants": [{"name", "was"}]}` or plain names; max 50, 120 characters each).
- Server: **remembered names.** Every participants save feeds a server-wide glossary (`names.json` in the data folder): the saved names, plus a correction for each real rename. It is appended to the notes prompt of every note type when the bridge fetches it (the stored prompt text is not changed), and the names (never the wrong spellings) are passed to faster-whisper as `hotwords` (or a short initial prompt on older versions; retried without on any error), so names you correct also help future transcripts. Settings > **People and names** lists them with Remove and Clear all (`GET /v1/names`, `POST /v1/names/remove`).
- Web: the Notion bar on a meeting that is in Notion now has a **...** menu (**Notion options**) instead of the Send again button. It holds **Send again** and **Remove this note from Notion** (confirmed in the app's own dialog). Removing deletes the meeting's toggle from its month page in the background (a toggle already gone in Notion counts as removed; a refusal is shown on the meeting) and the meeting is not copied again automatically (auto-copy and the "already in Notion always resyncs" rule stay quiet) until you press **Send to Notion**. New `DELETE /v1/sessions/{id}/notion`; the status gains `state: "removing"` and `opted_out`. Not exposed to agents (the agent API never deletes).
- Server: **combining and splitting meetings now follows in Notion.** The original meetings' toggles are removed, and each new meeting is copied to Notion when its notes finish if any original was in Notion (and not removed by you) or its note type copies automatically. A Notion problem never fails the combine/split. Restoring an original from Recently deleted does not recreate its Notion toggle (use **Send to Notion**); undoing a combine/split removes the new meetings' toggles and sends the restored originals again.
- Supported recorders are now 0.7.8 to 0.7.13.

## 0.7.12
- macOS: self-update logs to `~/.meeting-notes/logs/update.log` (included in the Logs bundle) and tries harder to reopen the app (`open`, `open -a`, then the executable, each verified). The app is still ad-hoc signed, so the update bar now says macOS may ask for permissions again and links to **Install manually** (the server's install guide, macOS section); after launching the update it says to open the app from ~/Applications if it doesn't reopen. `tools/macos_signing_setup.sh` (optional) prepares a stable signing identity; the build uses it only once the certificate is trusted for code signing, otherwise it signs ad-hoc.
- Client: **choose your microphone and speakers** (Settings > Audio > Devices). **Microphone** and **Speakers (what you hear)** each list **Automatic (system default)**, the default, then the connected devices by name. The choice applies to the next recording, the level preview before recording and the device names in the main window; a recording in progress keeps its devices. A chosen device that is not connected stays in the list as "<name> (not connected)" and recording falls back to Automatic (logged once; the main window names the fallback), so a missing device never stops a recording. Saved as `audio_devices` (`{"mic": "", "system": ""}`, `""` = Automatic) in `config.json`; the older top-level `mic` / `system` keys still apply while a side is Automatic. On a Mac, what you hear comes from ScreenCaptureKit and is not a device, so the speakers list is empty (and disabled) unless a virtual loopback driver such as BlackHole is installed.
- Client: new **Auto end** choice **When people say goodbye** (after **When the call ends**; Settings > General > Meeting detection; the default stays **On the hour**). It listens to the server's live preview: when either side says a goodbye ("bye", "see you later", "take care", "have a good one", "thanks everyone", ...) and then both sides are quiet for 20 seconds, the recording stops after a 10-second "Meeting seems to be over" countdown (**Keep recording** waits for a new goodbye; audio coming back closes the countdown and the next 20 seconds of quiet still ends it; a goodbye not followed by quiet within 5 minutes is forgotten). It needs **Live preview** on (Settings > Server) and a reachable server; with no live transcript it never ends the recording. The recorder shows "Auto end after goodbyes and 20 s of silence", and the Recorders page accepts the new `bye` mode. `auto_end: "bye"` in `meeting_detection`.
- Client: new **Auto end** choice **When the call ends** (Settings > General > Meeting detection; the default stays **On the hour**). An auto-recorded call stops once the call is over and its audio has been quiet for the grace time, after the usual 15-second "call ended" countdown (**Keep recording** or **Disable auto end** turns it off for that recording). The recorder shows "Auto end when the call ends", on the Recorders page too.
- Server: **notes are generated automatically per note type.** The global "Auto-generate notes" setting is gone; each note type has its own **Generate notes automatically** switch (Settings > Note types; `auto_notes_types` in `settings.json`, the list of type ids, also in `/v1/settings`; `auto_notes` on each `/v1/note-templates` item). A newly transcribed meeting (and an uploaded transcript) is queued for notes when its tagged note type, else the default type, has the switch on and the AI provider is not **Disabled**; otherwise the Generate button still builds them. Every type starts on (existing servers keep every type on, and a new note type starts on). An old `auto_generate_notes` key is ignored.
- Supported recorders are now 0.7.7 to 0.7.12.

## 0.7.11
- Client: **Start recording automatically when a call starts** (Settings > General > Meeting detection, off by default). A detected Teams, Zoom or Google Meet call is recorded at once under its name, with a short "Recording ... call" card instead of the "Record this meeting?" prompt. An **Auto end** choice appears below it: **On the hour** (stops at the end of the hour the call is in; a call joined in the last 10 minutes before the hour runs to the next one, with a one-minute countdown), **After 30 seconds of silence** (a 15-second countdown after 15 quiet seconds, once anything has been heard), or **Manual only**. While an auto-recorded call runs with an Auto end, the recorder shows it with a **Disable auto end** button (also on the card) that turns it off for that recording. New `auto_record` and `auto_end` (`hour`, `silence`, `manual`) keys in `meeting_detection` in `config.json`.
- Client: **Default note type and a per-meeting Note type select.** Settings > General > Notes sets the default (`default_note_type` in `config.json`; **Server default** if unset). A **Note type** select beside the meeting name (shown when the server has 2+ note types) picks the type for the meeting being recorded, changeable until you press Stop and back to the default afterwards; auto-recorded calls use the selected type (the default unless you picked another beforehand). The choice goes to the server as `meta.note_type` (`session.json`, also on a re-upload), so meetings that record get notes of the right type, saved where that type sends them (for example its Notion page).
- Server: a meeting whose recorder tagged it with an existing note type (`meta.note_type`) now gets notes of that type once transcribed when that type generates notes automatically (an AI provider must be selected). Untagged meetings and unknown ids use the default type. The log says which type was used and why.
- Recorders page: a **Note type** select next to the meeting name (for recorders that advertise the new `note_type` capability) and, for an auto-recorded call, the "Auto end at ..." line with **Disable auto end** (`auto_end` capability). New remote commands `set_note_type` and `disable_auto_end`, new refusal code `no_auto_end`; `GET /v1/recorders` items carry `caps`.
- Build: `tools/build_windows.ps1` no longer has a `-GitHubRelease` switch; builds never touch GitHub (no Actions, no GitHub releases).
- Supported recorders are now 0.7.6 to 0.7.11.

## 0.7.10
- Client (macOS): the Microphone and Screen & System Audio Recording prompts now appear on the first launch instead of the first recording. A moment after the window shows, it asks for the microphone, then (after you answer) for screen & system audio recording, then shows the permissions panel with the fresh statuses; the server-password Settings dialog on a first run waits until the panel is closed. Runs once (`permissions_prompted` in `config.json`), also once on the first launch after upgrading; Local Network is prompted by the server check as before. `MEETING_NOTES_NO_PERMISSION_PROMPT=1` skips all of it.
- Web: every "are you sure" is the app's own dialog; revoking an AI access key, removing the Notion token and copying existing notes to Notion no longer use the browser's confirm pop-up.
- Recorders page: the meter traces play level frames back slightly behind real time (about 1.5 frame gaps, adaptive) and animate at the display rate with interpolation and rounded edges, so they scroll smoothly instead of jumping with each frame.
- Client: a missing server password is now said out loud. With a server set but no password saved, a red strip ("Enter the server password to connect...") shows at once with an **Enter password** button, and the very first start opens Settings on the password field (once; the strip stays until a password is saved). The client now calls it "Server password" everywhere, and a refused control channel (close code 4401) shows the strip immediately instead of five minutes later.
- Client (macOS): a "Meeting Notes needs a few permissions" panel overlays the recorder when Microphone, Screen & System Audio Recording or Local Network access is missing, with the status of each, the exact steps, **Open System Settings** deep links, **Allow microphone**, **Quit and reopen** (bundled app only) and **Check again** (also runs when the window regains focus). **Not now** leaves a "Permissions needed - Fix" strip. It reappears when a recording fails to start. Recording the microphone alone still works without system-audio access.
- Client: after a check that could not reach the server, it looks again after 10 s and 30 s (then every 5 minutes), so a just-allowed Local Network permission or a slow network clears quickly.
- macOS app: declares `NSLocalNetworkUsageDescription` so macOS 15+ shows a proper Local Network prompt for the LAN server (without it a LAN server answered "No route to host"), and bundles `AVFoundation` for the microphone status.
- Supported recorders are now 0.7.5 to 0.7.10.

## 0.7.9
- Recorders page: each recorder is now a remote, a replica of the Windows client's main window in the client's own palette and sizes (header with Upload / History / Settings / "...", record card, meter lanes with Mute you / Mute them, Live preview, status line), and its buttons send the matching remote command. Meetings search also matches the computer a meeting was recorded on, which is what the remote's History button uses.
- Settings: Note types is its own card; "Copy notes to Notion automatically" is now set per note type (the old single switch carries over to every type); "Copy existing notes" says what it does; the Installation button reads "Install guide".
- Meeting view: the note type picker moved into the header next to Copy / Download; the Notion line keeps its own thin bar.
- Recorders page cards and the live transcript views follow the Windows client's recording window: clock and devices, meeting name with Start/Stop, level meters with Mute you / Mute them buttons the same height as their meter, a Live preview of "You: / Them:" lines for that recorder's meeting (in meeting-time order), and the client's status line.
- The server checks GitHub `main` for a newer version and shows an "Update available" notice in the sidebar and on Settings (report only; `MEETING_NOTES_UPDATE_CHECK=0` turns it off).
- Web Settings sections are separate cards, and the section list highlights the right one when you jump to it.
- Install moved from the sidebar into Settings (Installation section).
- Live transcript (web): lines in meeting-time order, the whole transcript in the overlay opening at the newest line, an ended meeting is flagged instead of freezing.
- Client: the Mute you / Mute them buttons fill the height of their meter lanes.
- Supported recorders are now 0.7.4 to 0.7.9 (0.7.3 and older are asked to update before starting a new upload).

## 0.7.8
- Upload a transcript (`.txt`, `.vtt`, `.srt` or pasted text) instead of audio, from the web UI and the desktop client.
- Every note type name is editable, built-ins included.
- Client Settings is a sidebar of pages; the Logs window moved into Settings.
- Notion export fixes found in live testing.

## 0.7.7
- Audio levels shown before recording, in the client and on the Recorders page.
- Copy finished notes into Notion (one page per month and note type).
- Meetings list grouped by day.

## 0.7.6
- Live Recorders page with remote control (start, stop, mute, rename, update).
- Per-recorder Recordings list with status, re-upload and delete-from-computer.
- Split a meeting (with suggested split points) and combine meetings.
- Note types: multiple, editable meeting-note prompts.

## 0.7.5
- macOS client: ScreenCaptureKit system audio, call detection, server-hosted installer and updates.

## 0.7.4
- Server stays compatible with the current client and the five releases before it, enforced by contract tests.
- Updates are never installed automatically; the client shows an **Update now** button.
- Audio devices are picked up automatically, including mid-recording.
- Suggests stopping when a meeting seems to be over.

## 0.7.3
- More robust call-end auto-stop; uploads finalize once and resume after a restart.
- Fix uploads failing with "Access is denied" on Windows.

## 0.7.2
- "Recently deleted" trash with 30-day restore.
- Re-upload saved recordings; optional clean-up of old local recordings.

## 0.7.1
- Fix client updates that closed the app without finishing.
- Transcript segments are built from word timestamps.
- Notes state the substance directly instead of narrating the meeting.

## 0.7.0
- Agent access: REST API and MCP server with per-agent API keys.
- Client log uploads to the server.
- New web UI and client look with Light, Dark and System themes.
- Meeting call detection (Teams, Zoom, Meet) with a record prompt.
- Meetings library opens on notes; optional automatic notes.
- Claude (subscription) added as an AI provider.

## 0.6.1
- Transcription progress and notes status are shown separately; completed notes open first.
- Layout polish across the library, meeting, Home, Settings and Install pages.

## 0.6.0
- Read the transcript and build notes in the same meeting view.
- Rename meetings and summaries independently; the AI workflow prompt is editable.

## 0.5.0
- Upload existing recordings (WAV, MP3, M4A/MP4, FLAC, OGG, Opus, AAC, WebM); the server decodes them with ffmpeg.
- One pipeline status for upload and transcription, bulk actions, and a shareable Markdown notes document.
- Mute either track independently while recording.
