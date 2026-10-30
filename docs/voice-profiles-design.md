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
- Superseded jobs: `record_meeting` is called after `_finalize`'s superseded-job early return, next to
  `write_transcript`, wrapped in try/except (never fails a job).

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
- Cosine similarity against each person's centroid (speech-weighted mean of their `auto` + `confirmed`
  appearances' embeddings, renormalised; trashed sessions excluded). Greedy best-score assignment; two
  appearances in one meeting never link to the same person.
- `score ≥ AUTO (0.70)` → `auto`; `SUGGEST (0.55) ≤ score < AUTO` → new unnamed person + `suggested_person_id`
  (meeting page and profile ask "Sounds like Sarah? Yes / No"); below → new unnamed person. Constants, tuned later
  on real meetings.
- The mic self cluster always links to the self person as `confirmed`.

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
  meeting at the timestamp); Merge with… and Delete profile.
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
