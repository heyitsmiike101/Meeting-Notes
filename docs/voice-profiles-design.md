# Voice profiles ("People") — design

Status: built on feat/voice-profiles, not released. Optional feature, off by default.

## Goal
Recognise the same person across meetings by voice, with no setup. Each speaker diarization finds in a meeting
(with enough speech) is linked to a **person**: an existing one when the voice matches, otherwise a new unnamed
one ("Person 7"). On the People page Mike plays a couple of clips, gives the person a name (plus optional email
and company), and every meeting they appear in shows that name. AI agents can then ask "who said X" or "what did
Sarah say in meeting Y".

## Feature switch
- New setting `voice_profiles_enabled: bool = False` (settings dataclass, `_SETTINGS_BOOL_KEYS`, `/v1/settings`,
  checkbox in the settings page "Speaker labels" section). It only has effect while `diarization_enabled` is on;
  the checkbox shows a note when diarization is off.
- **Effective state** `people.enabled(settings) = voice_profiles_enabled and diarization_enabled`. When not
  effective, everything is invisible: no People nav item (sidebar + mobile tabbar), `/people*` pages and
  `/v1/people*` return 404, the read-time overlay is empty (meeting pages, agent output, notes, export show plain
  labels exactly as before), no mic-track diarization, no voiceprints or clips captured. Agent people tools stay
  registered (the spec/MCP drift guard needs them) but answer `409 voice_profiles_off`.
- Turning it off keeps stored data. Settings has "Delete all voice profiles" (confirm) → `DELETE /v1/people`
  removes `<data>/people/` entirely (people, voiceprints, clips).

## Data (`<data>/people/`, data root; never in export zip, Notion, or agent output)
- `people.json` — `{"revision", "next_number", "people": [Person]}`. Person: `id` (`p_` + 10 hex), `number`
  (for "Person 7"), `name`, `email`, `company` (optional strings, cleaned; email loosely validated, ≤ 254 chars),
  `is_self` (exactly one, created on first use, with number 0 so it never uses up a "Person N"), `created`, `updated`. **No centroid stored** — centroids are
  derived in memory from the voiceprint files (single source of truth), cached by a revision counter.
- `voiceprints/<session_id>.json` — `{"session_id", "job_id", "created", "speakers": [Appearance]}`.
  Appearance: `track` (`mic`/`system`), `raw` (pyannote id, e.g. `SPEAKER_01`), `label` (exactly the transcript
  label: `Them 2`, `Speaker 1`, `You`, `In room 1`), `embedding` (L2-normalised floats), `speech_sec`,
  `person_id` (or null), `link` (`auto` | `suggested` | `confirmed`), `score`, `suggested_person_id` (or null),
  `clips` (list of clip file names).
- `clips/<session_id>/<label-slug>-<n>.wav` — up to 3 clips per appearance (longest single-speaker turns, each
  trimmed to ≤ 12 s, 16 kHz mono PCM16), cut at record time so they survive audio retention/deletion.
- Display name: `name` if set, else `Person {number}`; the self person shows as `You` until named.
- All reads/writes go through one module-level `RLock` (like `names.py`); every file is written atomically.
  Matching runs inside the lock and re-reads people; pyannote never runs under the lock.

## Trash, delete, combine, split, retranscribe
- Trash/restore: voiceprints stay where they are; every read filters out sessions that are trashed or missing
  (`store.is_trashed` / session exists). So restore needs nothing.
- Purge (`purge_trashed`, `purge_expired_trash`) and hard `delete_session` call `people.forget_session(id)`:
  delete that voiceprint + clips, then garbage-collect people left with no appearances unless they have a name,
  email or company.
- `record_meeting` skips trashed sessions.
- Combine: the combined meeting gets no voiceprint (its labels from different parts collide); the originals are
  trashed so their appearances are hidden until uncombine restores them. Retranscribing the combined meeting
  creates a fresh voiceprint. Split: same — parts have no voiceprint until retranscribed. Documented, not fixed.
- Retranscribe: the new job's `_finalize` replaces the voiceprint file (old one kept until the new job succeeds,
  because it is only overwritten in `_finalize`). `confirmed` decisions carry over to the new appearance whose
  embedding is closest to the old confirmed one (cos ≥ AUTO). If the new job produced no embeddings (diarization
  skipped by `people_count` or failed), the old voiceprint is deleted (no stale labels).
- Find speakers again (`JobQueue.enqueue_speakers`): a speakers-only job re-diarizes the stored audio, relabels the
  latest transcript's segments (text untouched) and then calls `record_meeting` exactly as `_finalize` does, so the
  voiceprint is replaced and `confirmed` decisions carry over. Without embeddings the old voiceprint is dropped.
- Update voice profiles (`people.reprofile(store, session_id)`): no audio, no diarization. Rows with `link:
  confirmed` (and the self person) are kept as they are; the others are matched again against the centroids built
  from every other meeting, through the same `_link_candidates` that `record_meeting` uses. An unnamed person only
  this meeting knows is reused rather than replaced by a new "Person N"; clips stay; `_gc` drops people left
  unused. `NoVoiceprint` (route: 409) when nothing is stored. Route `POST /v1/sessions/{id}/reprofile`
  answers 404 while the feature is off. The bulk bar and the People page go through
  `POST /v1/voice-recheck` (`voice_recheck.run_recheck`, with `session_ids` for exact meetings). Naming, merging,
  combining or confirming a person asks `VoiceRecheckWorker.request_run` for a windowed run (`people_api` hook, so
  `people.py` stays free of the worker); confirmed links are never touched.
- Superseded jobs: `record_meeting` is called after `_finalize`'s superseded-job early return, next to
  `write_transcript`, wrapped in try/except (never fails a job).

## Names from introductions (optional AI pass; `intro_naming`, default on)
A voice left unnamed by matching can be named by what people say in the meeting. A separate, small bridge job
(`server/intro_names.py`, kind `intro_names`) reads the start of the transcript and answers, for each unnamed voice,
whether that speaker gave their name or was introduced. It is queued after `record_meeting` (new meeting, Retranscribe,
Find speakers again) when the setting is on, voice profiles are effective, the provider is not disabled and
`people.intro_candidates` finds an unnamed, non-self voice (`heard` rows with `named` false). It is claimed after any
queued notes and never changes them.
- The server drops anything it cannot verify (label not a candidate, quote not in the excerpt, name not in the quote,
  empty or long name), then `people.apply_intro_names` runs under `_lock`. Skipped without a trace: the person has a
  name now or is you, the appearance link is `confirmed`, or the user undid/dismissed this same name for this voice
  (`rejected_intro_names` on the appearance, carried over to a re-run by embedding like `rejected_person_ids`).
- `high`, no person of that name: the person gets the name (and the company if blank); known name added; provenance
  `named_from: [{session_id, label, name, quote, mode: "named", company_set, at}]` on the person.
- `high`, the name is a person Q with no voice anywhere who is not you: `_merge_locked(P into Q)`, so Q keeps id and name
  and gains the voice (link `confirmed`); Q gets a `mode: "merged"` entry. `_merge_locked` carries `named_from` along.
- The name is a person with a voice (or you): nothing is merged; the appearance gets `suggested_person_id = Q`, link
  `suggested` (the "Sounds like Q?" question) unless Q is in `rejected_person_ids`.
- `low`, no person of that name: the appearance keeps `intro_suggestion {name, company, quote}` and nothing is applied
  (the person page shows "Introduced as Dana?" with **Use this name** = `accept_intro_suggestion`, which behaves like
  `high`, and **Not them** = `dismiss_intro_suggestion`). `low` with a match is a "Sounds like Q?" question.
- **Undo** (`undo_intro_name`): a plain naming clears the name and the company it filled in; a merge moves that
  appearance to a new unnamed person (Q keeps their name). Both remember the name as rejected for that voice.
  `reprofile` keeps a voice on the person it named in that meeting (`_named_here`).
- After a naming or merge the same hook as People edits asks the voice recheck for a run, and
  `refresh_suggestions` looks at other meetings. Routes: `POST /v1/people/appearances/intro` (`undo|use|dismiss`).

## Capture
- `PyannoteDiarizer.diarize_full(...) -> Diarization(turns, embeddings)`; `diarize()` delegates and returns
  turns as today (Protocol, fakes and `diarize_call` unchanged). `diarize_call_full` uses `diarize_full` when the
  diarizer has it, else returns no embeddings.
- Embeddings: `getattr(output, "speaker_embeddings", None)`; rows follow `output.speaker_diarization.labels()`;
  map by label, keep only labels present in the exclusive turns; check row count matches; drop non-finite /
  zero-norm rows; L2-normalise; anything unexpected (legacy tuple, None, shape mismatch) → no embeddings, logged,
  never an error. Nothing stored on `self` (instance is shared by the job thread and speakers worker).
- `transcribe/diarize.py`: new pure `speaker_labels(turns, prefix) -> {raw: label}`; `assign_speakers` uses it
  (same numbering as today). New `assign_labels(segments, turns, labels)` that takes an explicit map (turns whose
  raw id is not in the map are ignored, so those segments keep the track default label).
- **System track**: unchanged diarization; embeddings when enabled.
- **Mic track** (enabled only): diarized too (no pin; min 1, max 4), so people in the room with Mike are
  separated. Mapping (pure function in `people.py`, server-side, used by local and helper paths):
  1. Self cluster = closest to the self centroid if one exists (cos ≥ SUGGEST), else the cluster with most speech.
  2. Fold into self (no label, no person): clusters with < MIN_SPEECH (10 s), and **echo** clusters whose
     embedding has cos ≥ ECHO (0.70) to any system-track cluster of the same meeting.
  3. Remaining clusters → `In room 1`, `In room 2` by first appearance. Self → `You` (default label).
  Applied with `assign_labels`; folded/self turns are dropped so those segments stay `You`.
- **people_count**: when the people count leaves one speaker on the system track (1 or 2 people on a call), diarization is skipped and so is mic diarization (nothing to separate). Otherwise, when mic diarization runs, the system track's pin becomes a bound
  (`max_speakers = people − 1`, no min) instead of an exact count, since some people may be in the room.
- **In-person** (single `system` track, `Speaker N`): unchanged diarization + embeddings. Matching can link one of
  them to the self person, which then displays as Mike's name / `You`.
- Local paths: `_transcribe_local` and `speakers_worker.process` gather `{track: Diarization}` and hand it to
  `_finalize`, which calls `people.record_meeting(...)` before writing the transcript.
- **GPU helper** (pack version changes automatically: it hashes `compute_helper/` + `transcribe/` code). Lease
  `diarization` gains `mic: bool` and `embeddings: bool` (additive; old helpers ignore unknown keys, and old
  helpers can't get leases anyway once the code hash changes). `_capture_compute_settings` sets them only when
  enabled. Helper: diarize mic with its own bounds (no shared-instance mutation: pass min/max per call), second
  progress share. Result gains optional `speaker_turns_mic` (validated against the mic track length) and
  `speaker_embeddings: {"system": {raw: [floats]}, "mic": {...}}` — validated leniently (bad vectors dropped and
  logged, never 422; ≤ 32 per track, length 64–1024). `_validate_result` / `complete` / `finish_remote` carry the
  extra data through. The mic mapping and echo guard run server-side in `finish_remote`.
- Clips are cut from the audio the server has at `_finalize` time (`wav_paths`), before any immediate audio
  deletion.

## Matching (`meeting_notes/server/people.py`)
- Only appearances with `speech_sec ≥ MIN_SPEECH` (10 s) get a person; shorter ones are recorded with
  `person_id: null` (not shown on People, overlay leaves their label alone).
- Cosine similarity against each person (see *Better pickup over time* below), trashed sessions excluded. Greedy best-score assignment; two
  appearances in one meeting never link to the same person.
- `score ≥ AUTO (0.70)` → `auto`; `SUGGEST (0.55) ≤ score < AUTO` → new unnamed person + `suggested_person_id`
  (meeting page and profile ask "Sounds like Sarah? Yes / No"); below → new unnamed person. Constants, tuned later
  on real meetings.
- The mic self cluster always links to the self person as `confirmed`.

## Better pickup over time (`people._models`, `_score`, `refresh_suggestions`)
- **Centroid**: weighted mean of a person's `auto` + `confirmed` appearances. A confirmed link counts
  `CONFIRMED_WEIGHT` (2x) as much as an auto one, and one appearance counts for at most `MAX_WEIGHT_SEC` (600 s)
  of speech. When the person has confirmed appearances, an `auto` one whose cosine to the confirmed-only centroid is
  below `SUGGEST` is left out, so one bad automatic link cannot drag the profile away.
- **Score** of a voice against a person = `max(centroid cosine, best cosine to any single confirmed appearance of that
  person)` (up to `MAX_EXEMPLARS` = 40, most speech first). People heard on different setups (headset, room, phone) are
  still picked up. `AUTO` / `SUGGEST` thresholds are unchanged.
- **Learning back**: `refresh_suggestions(store_or_root, person_id)` runs when a person gets new confirmed data
  (appearance confirm/assign, combine/merge) or is named. In the other (visible) meetings, a voice linked to an
  *unnamed* person (not you) by an `auto` link, with no question pending, that now scores >= `SUGGEST` against that
  named person gets `suggested_person_id` and link `suggested` ("Sounds like X?"). It never relinks, asks once per
  meeting, skips meetings that already contain that person, and skips trashed ones.
- **Rejections**: "Not them" appends the person to the appearance's `rejected_person_ids`. `_link_candidates`
  (record, reprofile) and `refresh_suggestions` never suggest a rejected person for that appearance; a retranscribe
  carries the list to the new appearance closest (>= `AUTO`) to the old one.
- The person page shows "Voice profile from N meetings (M confirmed by you)" (`voice_profile` in `GET /v1/people/{id}`).
  Stored transcripts are still never rewritten.

## Archive and forget over time (`people.cleanup`, run by `voice_recheck.run_recheck`)
- "Last heard" is the newest meeting start (session index `created`, else the voiceprint's `created`, as
  `recheck_candidates` dates meetings) among visible meetings with an appearance linked to the person; trashed meetings
  do not count.
- Archive: a named, non-self person not heard for `people_archive_days` (default 180, 0 = never) gets
  `person["archived"] = {"at", "auto": True}`. Archived people stay in `_models`, so they match exactly as before; only the
  People list hides them (Archived (N) switch, an "Archived" tag). `POST /v1/people/{id}/archive` sets `auto: False` or
  clears it; the self person cannot be archived. Restore: the cleanup clears `archived` for anyone heard in a meeting
  after `archived.at` (so a hand archive survives until they are heard again), and `record_meeting`, `reprofile` (only
  for a meeting inside the archive window) and the confirm/assign actions clear it at link time.
- Forget: an unnamed person (no name, email, company; not self; not archived) not heard for `people_forget_days`
  (default 90, 0 = never) loses all their appearances (and those clips) from the voiceprint files and is removed; the
  rest of each meeting's voiceprint and the transcripts are untouched, so those voices show their transcript label.
  Kept while any of their appearances has an `intro_suggestion` or a `suggested` link, or another appearance suggests
  them (`suggested_person_id`).
- The outcome of a cleanup that changed something is `last_cleanup` in `people.json` (see reference.md).

## Combine
- People list: checkbox per row (select all in the header, the Meetings page bulk bar) and a **Combine** button that
  needs two or more. The dialog asks who to keep (default: the "You" profile if selected, else the named person
  with the most talking time). `POST /v1/people/combine {ids, into}` merges every other person into `into` under one
  lock after checking everything: fewer than two different people or `into` not in `ids` -> 400; unknown id -> 404;
  the "You" profile among the others -> 400 (it cannot be merged away; keep it instead). The person page's old
  "Merge with..." is now "Combine with..." (same `POST /v1/people/{id}/merge`).
- When both have names, the kept person gets the other name as an "also heard as" spelling (and the other's own
  spellings move over), so notes written with the old name still link and are corrected.

## Names live in People
- Every known name is a person. A named person counts as a known name for the notes prompt and transcription
  hotwords (`names.all_names` reads `people.json`); names that exist only in `names.json` become voice-less
  people on the next People list load (`people.sync_known_names`, idempotent). Deleting a person also forgets the
  name, or the glossary would bring them back. `names.json` keeps working for older code paths (participants saves
  still record into it).
- **Add a person** (`POST /v1/people`, People page button): name + optional company, no voice yet ("No voice yet").
- **Also heard as** on the person page = the existing `name_corrections` whose right spelling is the person's name
  (`POST/DELETE /v1/people/{id}/aliases`). Renaming a person keeps their spellings pointing at the new name
  (`names.rename_person`).
- **Company** is a dropdown of the companies already used (`GET /v1/people/companies`, case-insensitive, sorted) plus
  None and "Add a new company..." (shows a text box).

## Participants in meeting notes link to People
- The notes view (Meetings page) and the Meeting notes page mark each participant with `data-pname`; `npDecorate`
  asks `GET /v1/people/lookup` (404 while People is off: nothing changes). A match (person name or "also heard as"
  spelling, case-insensitive) links to `/people/{id}`; no match shows a "Not in People" button that opens a dialog:
  **Add to People** (voice-less person) or **Link to someone...** (named people only; records the AI's spelling as
  an "also heard as" of that person, so the name then links to them). Stored notes are not rewritten by this.

## Edits (web + REST, admin token like the rest of the web UI)
- `GET /v1/people?sort=talking|recent|alpha` and `GET /v1/people/{id}` (profile + appearances + quotes).
- `PATCH /v1/people/{id}` — name, email, company.
- `POST /v1/people/{id}/merge` `{into}` — move appearances (`confirmed`), fill blank fields of `into`, delete source.
- `POST /v1/people/appearances` `{session_id, label, action}`: `confirm` (accept suggestion → move to suggested
  person, `confirmed`), `assign` `{person_id}` (→ `confirmed`), `unlink` (→ new unnamed person, `confirmed`).
  "Not them" on a suggestion = clear `suggested_person_id`.
- `DELETE /v1/people/{id}` — delete the profile; its appearances become `person_id: null` (plain labels).
- `DELETE /v1/people` — delete all voice-profile data.
- `GET /v1/people/{id}/clips/{session_id}/{name}` — serve a stored clip (audio/wav).
- **Backfill of older meetings is deferred** to a follow-up (most old audio is already deleted by retention, and
  mapping new turns onto old segments is fragile). Retranscribing a meeting with audio is the manual path.

## Showing names (read-time overlay; stored transcripts are never rewritten)
`people.overlay(data_root, session_id)` → `{label: {person_id, display, link, suggested_display}}`, empty when not
enabled. Used by:
- `store.session_detail` consumers in app.py → each segment gets `person_id` + `display`; the web transcript shows
  `display`, links it to `/people/{id}`; quote links use the time anchor `/sessions/{id}#t=SECONDS`; one small banner per pending suggestion.
- Agent `_raw_segments` → label becomes the display name ("Person N" for unnamed people, so agent output matches what the web UI shows); `speaker` filters accept display name, person id or raw
  label; segments carry `person_id`. MCP description of `speaker` updated (no longer only "You"/"Them").
- Bridge transcript for AI notes and the export zip transcript → display names for **named** people only.
- `In room N` segments keep the mic track style (avatar colour, lane) — acceptable; talk-time stats stay
  track-based (in-room counts as Mike's side). Stated, not changed.

## People page (web)
- `_NAV` row `("/people", "People", "people", "users")` between Meetings and Recorders + a `users` icon in
  `_ICON_PATHS`; only emitted when enabled (both sidebar and tabbar).
- `/people` list: avatar initials, display name, company, meetings count, total talk time, last heard. Sort
  control: **Most talking** (default), **Most recent**, **Alphabetical** (named first, then Person N by number);
  remembered in localStorage (try/catch). Suggestion hint on unnamed people.
- `/people/{id}` profile: editable name, email, company (optional); stats; **Voice samples** (up to 3 clips from
  different meetings, play buttons); **Meetings** (meeting name/date, label there, talk time, link state;
  actions Yes/Not them for suggestions, Move to…, Unlink); **What they said** (latest 20 quotes, links to the
  meeting at the timestamp); Combine with… and Delete profile.
- Follows `docs/design.md` and the server-web surface contract; light + dark. Web only (no Qt client surface).

## Agent tools (read-only; spec.py ROUTES + TOOLS, rest.py, mcp_server.py)
Routes: `GET /api/v1/people`, `GET /api/v1/people/{id}` (id or display name), `GET /api/v1/people/{id}/quotes`, `GET /api/v1/who-said`.
- `meeting_notes_list_people` — `sort`, `q`; id, display name, email, company, meetings, talk seconds, last heard.
- `meeting_notes_get_person` — profile + appearances (never embeddings or clips).
- `meeting_notes_person_quotes` — `person` (name or id), optional `meeting_id`, `q`, `limit`.
- `meeting_notes_who_said` — `q`, optional `meeting_id`; matching segments with display name + person id.
All four 409 when not enabled.

## Tests
Unit (`tests/test_people.py`): speaker_labels/assign_labels, embedding extraction from a fake DiarizeOutput
(good, mismatched, NaN, tuple), matching thresholds, one-person-per-meeting, MIN_SPEECH, centroid weighting, mic
mapping (self pick, echo fold, short fold, In room numbering), overlay, merge/unlink/confirm, retranscribe
carry-over + no-embeddings deletion, trash filter, purge GC, delete all. Integration with a fake diarizer that has
`diarize_full`: local call job (system + mic), in-person job, helper result with embeddings (and lenient bad
vectors), feature off = no files and unchanged transcript. Web: nav hidden when off/shown when on, list sorts,
profile edit, clip route, 404s when off. Agent: drift tests + 409 when off + who_said/person_quotes.
