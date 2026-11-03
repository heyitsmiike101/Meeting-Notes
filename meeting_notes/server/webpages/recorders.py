"""The Recorders page and its inline script."""

from __future__ import annotations

from .gpu import _GPU_HELPERS_JS, _GPU_RECORDERS_JS
from .shared import _CONFIRM_DIALOG_HTML, _JS_HELPERS
from .shell import _icon, _shell


_RECORDERS_JS = r"""
/* Recorders page. Every server value goes into the DOM with textContent / value / setAttribute, never
   into markup; innerHTML only ever receives the static templates and icon() output below. */
/* The remote draws each recorder as a copy of the desktop client's main window (client/ui/main_window.py): the same
   header, record card (name, note type, Start/Stop; then the clock and one row per side: mute icon, audio lane,
   device name), Your notes, the folded Live preview and the status line, in that order. Pressing a button sends the
   command the client's own button would run. */
var REC_TRACKS = [
  {key: 'mic', who: 'You', what: 'Microphone', button: 'you', icon: 'c-mic', heard: 'your microphone', ariaMute: 'Mute your microphone', ariaUnmute: 'Unmute your microphone'},
  {key: 'system', who: 'Them', what: 'System audio', button: 'them', icon: 'c-speaker', heard: 'the meeting audio', ariaMute: 'Mute system audio', ariaUnmute: 'Unmute system audio'}
];
var REC_LIT_LEVEL = 0.02, REC_LIT_HOLD = 350;   // a side's icon lights at this level and stays lit this long (ms), as the client's
var REC_FOLD_KEY = 'mn-rec-preview-open';         // the Live preview opened out (Show all), remembered in this browser
var REC_TRACE_SEC = 13;      // the client's trace holds 400 frames at 30 fps
var REC_UNSUPPORTED_TEXT = 'This version is no longer supported by the server — update to keep uploading';
// the client's strip order under the header (the unsupported strip is first, the update strip last)
var REC_BANNER_ORDER = ['no_mic', 'no_system', 'device_lost', 'device_back', 'token_rejected', 'recordings_in_app_folder', 'server_unreachable'];
var recLive = [];            // /v1/live items: the running meetings, with their live-preview lines
var recs = new Map();        // instance_id -> {item, at}  (at = Date.now() when the frame arrived)
var recSeen = new Map();     // instance_id -> {item, at}: recorders that connected before but are offline now (item.days_ago at `at`)
var recSeenRows = new Map(); // instance_id -> <li> in the Not connected / Inactive lists
var REC_INACTIVE_DAYS = 30;  // offline longer than this -> the collapsed Inactive group (same as the server)
var recCards = new Map();    // instance_id -> window element
var recHist = new Map();     // instance_id -> {status, gap, mic: [[ms, level]], system: [...]}: what the meter lanes draw
// Level frames arrive about 5 times a second and not evenly. The traces play them back a little behind real time
// (about one and a half frame gaps, adapting to the stream) and animate at the display rate, interpolating between
// frames, so they scroll smoothly instead of jumping with each frame.
var REC_PLAYOUT_MIN = 150, REC_PLAYOUT_MAX = 1200;
var recRaf = 0;
var recTypes = null, recDefaultType = '', recTypesTimer = 0;   // note types from /v1/note-templates ([{id, name}]), fetched once per page load
var recReady = false, recSocket = null, recBackoff = 1000, recReconnect = null, recEverClosed = false, recLinkDown = false;

function recP2(n) { return (n < 10 ? '0' : '') + n; }
function recClock(sec) {
  sec = Math.max(0, Math.floor(Number(sec) || 0));
  return recP2(Math.floor(sec / 3600)) + ':' + recP2(Math.floor(sec % 3600 / 60)) + ':' + recP2(sec % 60);
}
function recState(item) {
  var s = (item && item.state) || {};
  return {
    status: s.status === 'recording' || s.status === 'finishing' ? s.status : 'idle',
    meeting: s.meeting || {},
    tracks: s.tracks || {},
    banners: Array.isArray(s.banners) ? s.banners : [],
    update: s.update || {},
    uploads: s.uploads || {},
    call: s.call || {},
    suggestion: s.suggestion || null,
    preview: s.preview || {},
    stream: s.stream == null ? null : String(s.stream),
    noteType: typeof s.note_type === 'string' && s.note_type ? s.note_type : null,
    autoEnd: s.auto_end && typeof s.auto_end === 'object' ? s.auto_end : null,
    allowed: !(s.control && s.control.allowed === false)
  };
}
/* What a recorder said it can do in its hello (``caps``): the page only offers a control for a capability it has. */
function recCaps(item) { return item && Array.isArray(item.caps) ? item.caps : []; }
function recHasType(types, id) { return !!id && (types || []).some(function (t) { return t.id === id; }); }
/* The server's note types, fetched once (retried every 30 s while that fails); the pickers appear when there are 2 or more. */
function recLoadTypes() {
  fetch('/v1/note-templates', {credentials: 'same-origin'}).then(function (r) {
    if (!r.ok) throw new Error('note types');
    return r.json();
  }).then(function (data) {
    var items = Array.isArray(data.items) ? data.items : [];
    recTypes = items.filter(function (t) { return t && typeof t.id === 'string' && t.id; }).map(function (t) { return {id: t.id, name: String(t.name || t.id)}; });
    recDefaultType = typeof data.default_template_id === 'string' ? data.default_template_id : '';
    recRender();
  }).catch(function () { clearTimeout(recTypesTimer); recTypesTimer = setTimeout(recLoadTypes, 30000); });
}
/* The live meeting a recorder is streaming: matched by the session id the recorder reports, else (an older
   recorder that does not send one) by computer name when exactly one live meeting comes from it. */
function recLiveFor(item) {
  var s = recState(item), sid = s.meeting.session_id;
  if (s.status === 'idle') return null;
  if (sid) return recLive.find(function (l) { return l.session_id === sid; }) || null;
  var same = recLive.filter(function (l) { return l.device && l.device === item.device; });
  return same.length === 1 ? same[0] : null;
}
/* The client's footer: "Ready." / "Recording. live preview connected  |  2 uploads pending". */
function recStatusLine(s) {
  var up = recUploadLine(s.uploads).text, tail = up ? '  |  ' + up : '';
  if (s.status === 'finishing') return 'Finishing up...';
  if (s.status === 'recording') {
    var note = {connected: 'live preview connected', connecting: 'connecting to server...',
      disconnected: 'server unreachable; recording locally and will upload later', off: 'live preview off'}[s.stream] || s.stream || '';
    return 'Recording.' + (note ? ' ' + note : '') + tail;
  }
  return 'Ready.' + tail;
}
/* What one lane draws: 'live' while recording; idle with the recorder's input preview running, 'preview' (greyed)
   or 'unavail' (a track it cannot preview: a dash); else 'flat' (the idle line at 0%). */
function recMeterMode(s, key) {
  var pv = s.preview || {};
  if (s.status === 'recording') return 'live';
  if (s.status !== 'idle' || pv.active !== true) return 'flat';
  return Array.isArray(pv.tracks) && pv.tracks.indexOf(key) < 0 ? 'unavail' : 'preview';
}
function recLevel(level) {
  var n = Number(level);
  return isNaN(n) ? 0 : Math.min(1, Math.max(0, n));
}
/* Text, badge and dimming of one lane, as waveform.py paints them (width picks the short "Preview" badge). */
function recLaneView(s, key, width) {
  var tr = (s.tracks && s.tracks[key]) || {}, mode = recMeterMode(s, key), recording = s.status === 'recording';
  var level = mode === 'live' || mode === 'preview' ? recLevel(tr.level) : 0;
  var muted = recording && !!tr.muted, bad = recording && (tr.degraded === true || tr.connected === false), badge = null;
  if (mode === 'preview' || mode === 'unavail') badge = {text: mode === 'unavail' || width < 380 ? 'Preview' : 'Preview · not recording', tone: ''};
  else if (muted) badge = {text: 'Muted', tone: 'muted'};
  else if (bad) badge = {text: 'No signal', tone: 'bad'};
  else if (level >= 0.99) badge = {text: 'Clipping', tone: 'bad'};
  return {mode: mode, level: level, pct: mode === 'unavail' ? '—' : Math.floor(level * 100) + '%', badge: badge,
    dim: muted || bad, quiet: mode === 'preview' || mode === 'unavail'};
}
function recElapsed(entry, now) {
  var s = recState(entry.item), e = s.meeting.elapsed_sec;
  if (e == null || isNaN(Number(e))) return null;
  return Number(e) + (s.status === 'recording' ? Math.max(0, (now - entry.at) / 1000) : 0);
}
/* The client's queue note, appended to the status line: "2 uploads pending, 1 failed, uploading 40%". */
function recUploadLine(u) {
  u = u || {};
  var pending = Number(u.pending) || 0, failed = Number(u.failed) || 0, waiting = Number(u.awaiting_transcript) || 0, bits = [];
  var uploading = Math.max(0, pending - waiting), pct = u.current_percent != null && !isNaN(Number(u.current_percent)) ? Math.round(Number(u.current_percent)) : 0;
  if (uploading) bits.push(plural(uploading, 'upload', 'uploads') + ' pending');
  if (failed) bits.push(failed + ' failed');
  if (u.state === 'uploading') bits.push('uploading ' + pct + '%');
  else if (u.state === 'pending' && !uploading) bits.push('upload pending');
  else if (u.state === 'transcribing') bits.push('Uploaded · transcribing ' + pct + '%');
  else if (u.state === 'queued') bits.push('Uploaded · transcribing (queued)');
  return {text: bits.join(', '), failed: failed > 0};
}
/* The title of the client's call prompt: "Zoom call detected". The suggested name goes in its name field. */
function recCallText(prompt) {
  var label = String((prompt && prompt.label) || '').trim();
  return (label ? label + ' call' : 'Call') + ' detected';
}
function recToast(cmd, args, device, state) {
  var d = device || 'the recorder', track = args && args.track === 'mic' ? 'Microphone' : 'Meeting audio';
  switch (cmd) {
    case 'start': case 'accept_call_prompt': return 'Recording started on ' + d + '.';
    case 'stop': case 'stop_suggested': return 'Recording stopped on ' + d + '.';
    case 'mute': return track + ' muted on ' + d + '.';
    case 'unmute': return track + ' unmuted on ' + d + '.';
    case 'refresh_devices': return 'Devices refreshed on ' + d + '.';
    case 'dismiss_call_prompt': return 'Call prompt dismissed on ' + d + '.';
    case 'keep_recording': return 'Still recording on ' + d + '.';
    case 'retry_uploads': return 'Retrying uploads on ' + d + '.';
    case 'check_update': return state && state.update && state.update.available ? 'An update is available for ' + d + '.' : d + ' is up to date.';
    case 'install_update': return 'Updating ' + d + '. The app restarts when it is done.';
    case 'set_name': return 'Meeting renamed on ' + d + '.';
    case 'set_note_type': return 'Note type changed on ' + d + '.';
    case 'disable_auto_end': return 'Auto end turned off on ' + d + '.';
  }
  return 'Done.';
}
/* Live recorders: the ones recording first, then the rest; each group by device name (then id). */
function recSorted() {
  function rank(id) { return recState(recs.get(id).item).status === 'recording' ? 0 : 1; }
  return Array.from(recs.keys()).sort(function (a, b) {
    var ra = rank(a), rb = rank(b);
    if (ra !== rb) return ra - rb;
    var da = String(recs.get(a).item.device || '').toLowerCase(), db = String(recs.get(b).item.device || '').toLowerCase();
    return da < db ? -1 : da > db ? 1 : a < b ? -1 : a > b ? 1 : 0;
  });
}
/* How long ago an offline recorder was last connected, in seconds: the server's days_ago when the frame arrived plus
   the time since (so the text keeps counting without another frame). */
function recSeenAge(entry, nowMs) {
  var d = Number(entry && entry.item && entry.item.days_ago);
  return Math.max(0, (isFinite(d) ? d : 0) * 86400 + (nowMs - (Number(entry && entry.at) || nowMs)) / 1000);
}
function recSeenText(ageSec) {
  var s = Math.max(0, Number(ageSec) || 0);
  if (s < 60) return 'Last seen just now';
  if (s < 3600) { var m = Math.floor(s / 60); return 'Last seen ' + m + (m === 1 ? ' minute ago' : ' minutes ago'); }
  if (s < 86400) { var h = Math.floor(s / 3600); return 'Last seen ' + h + (h === 1 ? ' hour ago' : ' hours ago'); }
  var d = Math.floor(s / 86400); return 'Last seen ' + d + (d === 1 ? ' day ago' : ' days ago');
}
/* Offline recorders split into the last 30 days (shown greyed) and older (the collapsed Inactive group), newest first.
   An id that is live right now is never listed. */
function recSeenGroups(seen, live, nowMs) {
  var rows = [];
  seen.forEach(function (entry, id) { if (!live.has(id)) rows.push({id: id, age: recSeenAge(entry, nowMs)}); });
  rows.sort(function (a, b) { return a.age - b.age || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0); });
  return {
    recent: rows.filter(function (r) { return r.age <= REC_INACTIVE_DAYS * 86400; }),
    inactive: rows.filter(function (r) { return r.age > REC_INACTIVE_DAYS * 86400; })
  };
}
function recSet(el, text) { text = text == null ? '' : String(text); if (el.textContent !== text) el.textContent = text; }
function recSetIcon(el, name, size) { if (el._ic !== name) { el._ic = name; el.innerHTML = icon(name, size || 16); } }

/* The window, top to bottom as in the client: header (name, Upload, History, Settings, More), strips, the record card
   (clock, devices, meeting name, Start/Stop), the two meter lanes with their Mute buttons, Live preview, status line. */
var REC_CARD_HTML =
  '<header class="cw-top"><h2 class="cw-brand" data-r="device"></h2><span class="cw-version" data-r="version"></span><span class="cw-grow"></span>'
  + '<a class="cw-tool" data-r="upload" href="/#upload-heading" aria-label="Upload" title="Send an audio recording to the server for transcription, or add a transcript you already have">' + icon('c-upload', 18) + '<span>Upload</span></a>'
  + '<a class="cw-tool" data-r="history" href="/meetings" aria-label="History">' + icon('c-history', 18) + '<span>History</span></a>'
  + '<div class="cw-menu-wrap"><button type="button" class="cw-tool" data-r="settings" data-pop="settings" aria-haspopup="true" aria-expanded="false" aria-label="Settings">' + icon('c-settings', 18) + '<span>Settings</span></button>'
  + '<div class="cw-pop cw-pop-wide" data-r="settingsPop" hidden><p class="cw-pop-note">The settings of this app (devices, folders, uploads, remote control) can only be changed on the computer it runs on. The server keeps its own settings.</p>'
  + '<div class="cw-pop-sep"></div>'
  + '<a href="/settings#settings-recorders-heading">' + icon('c-settings', 16) + '<span>Recorder settings on the server</span></a>'
  + '<a href="/settings#settings-logs-heading">' + icon('c-logs', 16) + '<span>Client logs</span></a></div></div>'
  + '<div class="cw-menu-wrap"><button type="button" class="cw-more" data-r="more" data-pop="more" aria-haspopup="menu" aria-expanded="false" aria-label="More actions" title="More: recordings folder, re-upload, audio devices, logs">' + icon('c-more', 20) + '</button>'
  + '<div class="cw-pop" data-r="morePop" role="menu" hidden>'
  + '<button type="button" role="menuitem" data-act="recordings" data-r="folder">' + icon('c-folder', 16) + '<span>Open recordings folder</span></button>'
  + '<button type="button" role="menuitem" data-act="recordings" data-r="reupload" title="Send recordings kept on this computer to the server again, for example after a meeting was deleted there">' + icon('c-upload', 16) + '<span>Re-upload a saved recording...</span></button>'
  + '<button type="button" role="menuitem" data-act="refresh" data-r="refresh" data-title="Re-scan microphones and speakers">' + icon('c-refresh', 16) + '<span>Refresh audio devices</span></button>'
  + '<a role="menuitem" data-r="logs" href="/settings#settings-logs-heading" title="Client log, audio devices, upload queue and configuration; save or send them">' + icon('c-logs', 16) + '<span>Logs...</span></a></div></div></header>'
  + '<div class="cw-body"><div class="cw-strips" data-r="strips"></div>'
  + '<div class="cw-card"><div class="cw-controls"><input type="text" class="cw-name" data-r="name" maxlength="200" autocomplete="off" aria-label="Meeting name" placeholder="Meeting name (optional)">'
  + '<select class="cw-type" data-r="type" aria-label="Note type" data-title="The note type decides how the notes are written and where they are saved" hidden></select>'
  + '<button type="button" class="cw-rec" data-act="record" data-r="rec"><span data-r="recIc" aria-hidden="true"></span><span data-r="recText">Start recording</span></button></div>'
  + '<div class="cw-statusrow"><span class="cw-clock" data-r="clock" aria-label="Elapsed recording time">00:00:00</span>'
  + '<div class="cw-meters">' + REC_TRACKS.map(function (t) {
    return '<button type="button" class="cw-src" data-act="mute" data-track="' + t.key + '" aria-pressed="false" aria-label="' + t.ariaMute + '"></button>'
      + '<div class="cw-lane" data-track="' + t.key + '"><canvas class="cw-trace" aria-hidden="true"></canvas></div>'
      + '<span class="cw-dev" data-track="' + t.key + '"><span class="cw-dev-in"></span></span>';
  }).join('') + '</div></div>'
  + '<div class="cw-autoend" data-r="autoEnd" hidden><span class="cw-subtle" data-r="autoEndText"></span>'
  + '<button type="button" class="cw-btn" data-act="disable_auto_end" data-r="autoEndBtn" aria-label="Disable auto end for this recording">Disable auto end</button></div></div>'
  // Your notes, as in the app: no title row; the note on the text, Saved and Edit share the line below the notes
  + '<div class="cw-notes" data-r="notes" role="group" aria-label="Your notes">'
  + '<p class="cw-subtle" data-r="notesEmpty"></p>'
  + '<div class="scratchpad-rendered" data-r="notesBody" tabindex="0" aria-label="Your notes" hidden></div>'
  + '<textarea class="cw-notes-edit" data-r="notesText" spellcheck="true" aria-label="Your notes" placeholder="Links and notes for this meeting. They are saved as you type and sent with the recording." hidden></textarea>'
  + '<div class="cw-notes-foot"><span class="cw-subtle">Links are kept in the notes. <span data-r="notesNote"></span></span>'
  + '<span class="cw-grow"></span><span class="cw-subtle" data-r="notesSave" role="status"></span>'
  + '<button type="button" class="cw-btn" data-act="notes_edit" data-r="notesEdit" hidden>Edit</button></div>'
  + '</div>'
  + '<div class="cw-preview-head"><p class="cw-label">Live preview</p><span class="cw-grow"></span>'
  + '<button type="button" class="cw-tool cw-fold" data-act="fold" data-r="fold" aria-expanded="false"><span data-r="foldIc" aria-hidden="true"></span><span data-r="foldText"></span></button></div>'
  + '<div class="cw-preview" data-r="preview" role="log" tabindex="0" aria-label="Live preview transcript"></div>'
  + '<p class="cw-status"><span data-r="statusText"></span> <button type="button" class="cw-link" data-act="retry" data-r="retry" hidden>Retry uploads</button></p>'
  + '<div class="cw-prompts">'
  + '<div class="cw-prompt" data-r="call" role="group" aria-label="Call detected" hidden><div class="cw-prompt-head">' + icon('c-mic', 18) + '<span class="cw-prompt-title" data-r="callTitle"></span></div>'
  + '<p class="cw-subtle">Record this meeting?</p><input type="text" class="cw-name" data-r="callName" maxlength="200" autocomplete="off" aria-label="Meeting name">'
  + '<div class="cw-prompt-row"><button type="button" class="cw-btn" data-act="dismiss_call" data-r="callLater">Not now</button>'
  + '<button type="button" class="cw-btn primary" data-act="accept_call" data-r="callRecord">' + icon('c-mic', 16) + '<span>Record</span></button></div></div>'
  + '<div class="cw-prompt" data-r="suggest" role="group" aria-label="Stop recording?" hidden><div class="cw-prompt-head">' + icon('c-info', 18) + '<span class="cw-prompt-title" data-r="suggestTitle"></span></div>'
  + '<p class="cw-subtle" data-r="suggestSub"></p>'
  + '<div class="cw-prompt-row" data-r="sugAsk"><button type="button" class="cw-btn" data-act="keep" data-r="sugKeepA">Keep recording</button>'
  + '<button type="button" class="cw-btn primary" data-act="stop_suggested" data-r="sugStopA">Stop recording</button></div>'
  + '<div class="cw-prompt-row" data-r="sugCount" hidden><button type="button" class="cw-btn" data-act="stop_suggested" data-r="sugStopB">Stop now</button>'
  + '<button type="button" class="cw-btn primary" data-act="keep" data-r="sugKeepB">Keep recording</button></div></div>'
  + '</div></div>';

function recMakeCard(id) {
  var card = document.createElement('article');
  card.className = 'cw';
  card.dataset.id = id;
  card.innerHTML = REC_CARD_HTML;
  var r = {};
  card.querySelectorAll('[data-r]').forEach(function (el) { r[el.dataset.r] = el; });
  r.lanes = {};
  card.querySelectorAll('.cw-lane').forEach(function (lane) {
    var key = lane.dataset.track;
    r.lanes[key] = {
      wrap: lane, canvas: lane.querySelector('canvas'), view: null, lit: 0,
      mute: card.querySelector('.cw-src[data-track="' + key + '"]'),
      dev: card.querySelector('.cw-dev[data-track="' + key + '"]')
    };
  });
  card._r = r; card._busy = {}; card._nameDirty = false; card._status = '';
  card._edit = null;   // notes being edited here: {base, timer, saving, again, pull, pulling}
  recSetFold(card, recFoldOpen());
  return card;
}
/* "Your notes": the text typed on the recorder, rendered by the server. The item carries seq/session_id/length; the
   rendered HTML is fetched (debounced) only when the seq changes, and a late answer for an older seq is dropped.
   The section is always on the card: with no text it says so (or, for a recorder too old to share its notes, that
   an update adds them), like the recorder's own empty notes pane. */
var REC_NOTES_EMPTY = 'Nothing typed yet. Notes typed on the recorder show here as they are typed.';
var REC_NOTES_EMPTY_EDIT = 'Nothing typed yet. Notes typed here or on the recorder show here.';
var REC_NOTES_OLD = 'This recorder’s version doesn’t share its notes. Update it to see them here.';
var REC_NOTES_SAVE_MS = 300;   // co-editing: the pause in typing before the text goes to the recorder
function recNotesEditable(it) {
  var s = recState(it);
  return recCaps(it).indexOf('notes_edit') >= 0 && s.status !== 'finishing' && !recLockReason(s);
}
function recUpdateNotes(card, it) {
  var r = card._r, n = it.notes, canEdit = recCaps(it).indexOf('notes_edit') >= 0;
  if (card._edit) {   // being edited here: the recorder's typing is merged into the text box as it comes
    recSet(r.notesNote, n && n.length && !n.session_id ? '(draft, not recording yet)' : '');
    r.notesText.disabled = recState(it).status === 'finishing';
    var ekey = n && typeof n === 'object' ? n.seq + '|' + (n.session_id || '') : '';
    if (card._notesKey !== ekey) { card._notesKey = ekey; recNotesPull(card); }
    return;
  }
  r.notesEdit.hidden = !canEdit;
  if (canEdit) recBtn(card, r.notesEdit, 'notes_edit', recNotesEditable(it), 'The recording is finishing; its notes can no longer change.');
  if (!n || typeof n !== 'object' || !n.length) {
    card._notesKey = null; clearTimeout(card._notesTimer);
    if (r.notesBody._html) r.notesBody.replaceChildren();
    r.notesBody._html = null; r.notesBody.hidden = true;
    recSet(r.notesNote, '');
    recSet(r.notesEmpty, canEdit ? REC_NOTES_EMPTY_EDIT : recCaps(it).indexOf('notes') >= 0 ? REC_NOTES_EMPTY : REC_NOTES_OLD);
    r.notesEmpty.hidden = false;
    return;
  }
  r.notesEmpty.hidden = true; r.notesBody.hidden = false;
  var note = [];
  if (!n.session_id) note.push('(draft, not recording yet)');
  if (n.truncated) note.push(n.length + ' characters');
  recSet(r.notesNote, note.join(' · '));
  var key = n.seq + '|' + (n.session_id || '');
  if (card._notesKey === key) return;
  card._notesKey = key;
  clearTimeout(card._notesTimer);
  card._notesTimer = setTimeout(function () {
    fetch('/v1/recorders/' + encodeURIComponent(card.dataset.id) + '/notes.html', {credentials: 'same-origin'}).then(function (resp) {
      if (resp.status === 404) return null;
      if (!resp.ok) throw new Error('notes failed');
      return resp.text();
    }).then(function (html) {
      if (html === null || card._notesKey !== key) return;
      if (r.notesBody._html !== html) { r.notesBody.innerHTML = html; r.notesBody._html = html; }
    }).catch(function () { if (card._notesKey === key) card._notesKey = null; });
  }, 500);
}
/* Co-editing the notes here, as in Life Dash: Edit loads the full text into a plain text box (the notes are Markdown,
   as the client stores them). Every pause in typing sends it to the recorder with ``base``, the recorder's text this
   page last saw; the recorder merges what changed here since then into what is typed there now
   (``meeting_notes/notes_merge.py``), and this page reads the result back and merges it into the box the same way
   (recNotesMerge), so both sides can type at once. A line both changed takes this page's edit. Done shows the
   rendered notes again. */
function recNotesFull(id) {
  return fetch('/v1/recorders/' + encodeURIComponent(id) + '/notes', {credentials: 'same-origin'}).then(function (resp) {
    if (resp.status === 404) return '';
    if (!resp.ok) throw new Error('Could not load the notes.');
    return resp.json().then(function (d) { return d && typeof d.text === 'string' ? d.text : ''; });
  });
}
/* Line-level diff of two arrays of lines: [tag, i1, i2, j1, j2] runs like Python's SequenceMatcher.get_opcodes
   (the common head and tail are trimmed first; a middle too big to compare is one replaced run). */
var REC_DIFF_CELLS = 250000;   // notes_merge.DIFF_CELLS
function recLineOps(a, b) {
  var n = a.length, m = b.length, pre = 0, suf = 0;
  while (pre < n && pre < m && a[pre] === b[pre]) pre++;
  while (suf < n - pre && suf < m - pre && a[n - 1 - suf] === b[m - 1 - suf]) suf++;
  var ops = [], an = n - pre - suf, bm = m - pre - suf;
  var push = function (tag, i1, i2, j1, j2) {
    if (i1 === i2 && j1 === j2) return;
    var last = ops[ops.length - 1];
    if (last && (tag === 'equal') === (last[0] === 'equal')) {
      last[2] = i2; last[4] = j2;
      if (tag !== 'equal') last[0] = last[1] === last[2] ? 'insert' : last[3] === last[4] ? 'delete' : 'replace';
      return;
    }
    ops.push([tag, i1, i2, j1, j2]);
  };
  var change = function (i1, i2, j1, j2) { push(i1 === i2 ? 'insert' : j1 === j2 ? 'delete' : 'replace', i1, i2, j1, j2); };
  push('equal', 0, pre, 0, pre);
  if (an && bm && an * bm <= REC_DIFF_CELLS) {
    var w = bm + 1, L = new Uint32Array((an + 1) * w), i, j;
    for (i = an - 1; i >= 0; i--) for (j = bm - 1; j >= 0; j--) {
      L[i * w + j] = a[pre + i] === b[pre + j] ? L[(i + 1) * w + j + 1] + 1 : Math.max(L[(i + 1) * w + j], L[i * w + j + 1]);
    }
    var ci = 0, cj = 0;
    i = 0; j = 0;
    while (i < an && j < bm) {
      if (a[pre + i] === b[pre + j]) {
        change(pre + ci, pre + i, pre + cj, pre + j);
        push('equal', pre + i, pre + i + 1, pre + j, pre + j + 1);
        i++; j++; ci = i; cj = j;
      } else if (L[(i + 1) * w + j] >= L[i * w + j + 1]) i++;
      else j++;
    }
    change(pre + ci, pre + an, pre + cj, pre + bm);
  } else {
    change(pre, pre + an, pre, pre + bm);
  }
  push('equal', n - suf, n, m - suf, m);
  return ops;
}
/* Three-way merge of base -> mine and base -> theirs, line by line: notes_merge.merge3's policy exactly (one side
   changed a run: that side; both inserted at the same spot: both, theirs first; both changed it: mine). */
function recNotesMerge(base, mine, theirs) {
  if (mine === theirs || theirs === base) return mine;
  if (mine === base) return theirs;
  var lines = function (t) { return t ? t.split('\n') : []; };   // empty notes have no lines
  var B = lines(base), all = [];
  [[lines(mine), 'mine'], [lines(theirs), 'theirs']].forEach(function (pair) {
    recLineOps(B, pair[0]).forEach(function (op) {
      if (op[0] !== 'equal') all.push([op[1], op[2], pair[1], pair[0].slice(op[3], op[4])]);
    });
  });
  all.sort(function (x, y) { return x[0] - y[0] || x[1] - y[1]; });
  var regions = [];
  all.forEach(function (h) {
    var r = regions[regions.length - 1];
    if (r && (h[0] < r.end || (h[0] === h[1] && h[0] === r.end && r.start === r.end))) {
      r.end = Math.max(r.end, h[1]); r.hunks.push(h); return;
    }
    regions.push({start: h[0], end: h[1], hunks: [h]});
  });
  var take = function (r, side) {
    var out = [], pos = r.start;
    r.hunks.forEach(function (h) {
      if (h[2] !== side) return;
      out = out.concat(B.slice(pos, h[0]), h[3]); pos = h[1];
    });
    return out.concat(B.slice(pos, r.end));
  };
  var out = [], pos = 0;
  regions.forEach(function (r) {
    out = out.concat(B.slice(pos, r.start));
    var mineIn = r.hunks.some(function (h) { return h[2] === 'mine'; });
    var theirsIn = r.hunks.some(function (h) { return h[2] === 'theirs'; });
    if (mineIn && theirsIn && r.start === r.end) out = out.concat(take(r, 'theirs'), take(r, 'mine'));
    else out = out.concat(take(r, mineIn ? 'mine' : 'theirs'));
    pos = r.end;
  });
  return out.concat(B.slice(pos)).join('\n');
}
/* Where a caret at ``pos`` in ``old`` belongs in ``next``: before the changed middle it stays, after it it moves by
   the change in length, inside it it goes to the end of the new middle. */
function recCaretAfter(old, next, pos) {
  var pre = 0, suf = 0, max = Math.min(old.length, next.length);
  while (pre < max && old.charCodeAt(pre) === next.charCodeAt(pre)) pre++;
  while (suf < max - pre && old.charCodeAt(old.length - 1 - suf) === next.charCodeAt(next.length - 1 - suf)) suf++;
  if (pos <= pre) return pos;
  if (pos >= old.length - suf) return pos + next.length - old.length;
  return next.length - suf;
}
/* Put a merged text in the box with the caret, selection and scroll where they were. */
function recNotesShow(box, text) {
  if (box.value === text) return;
  var old = box.value, a = box.selectionStart, b = box.selectionEnd, top = box.scrollTop;
  var focused = document.activeElement === box;
  box.value = text;
  if (focused) box.setSelectionRange(recCaretAfter(old, text, a), recCaretAfter(old, text, b));
  box.scrollTop = top;
}
function recNotesBegin(card) {
  var r = card._r;
  if (card._edit) return;
  r.notesEdit.disabled = true;
  recNotesFull(card.dataset.id).then(function (text) {
    card._edit = {base: text, timer: 0, saving: false, again: false, pull: false, pulling: 0};
    r.notesText.value = text;
    r.notesBody.hidden = true; r.notesEmpty.hidden = true; r.notesText.hidden = false;
    recSet(r.notesEdit, 'Done'); r.notesEdit.dataset.act = 'notes_done'; r.notesEdit.disabled = false; r.notesEdit.hidden = false;
    recSet(r.notesSave, '');
    r.notesText.focus();
  }, function (err) { r.notesEdit.disabled = false; notify(err.message || 'Could not load the notes.', 'error'); });
}
function recNotesTyped(card) {
  var e = card._edit;
  if (!e) return;
  recSet(card._r.notesSave, 'Saving...');
  clearTimeout(e.timer);
  e.timer = setTimeout(function () { recNotesSave(card); }, REC_NOTES_SAVE_MS);
}
/* The recorder's notes changed (typed there): read them and merge them into the box. Never while a save is on its
   way (its answer reads them back anyway); a read that a save overtook is dropped and done again. */
function recNotesPull(card) {
  var e = card._edit;
  if (!e) return Promise.resolve();
  if (e.saving) { e.pull = true; return Promise.resolve(); }
  var mark = ++e.pulling;
  return recNotesFull(card.dataset.id).then(function (theirs) {
    if (card._edit !== e) return;
    if (e.saving || mark !== e.pulling) { e.pull = true; return; }
    recNotesAdopt(card, e.base, theirs);
  }, function () { /* the next change reads them again */ });
}
/* ``theirs`` is the recorder's text, which already holds ``sent`` (what this page last agreed on or sent): the box
   keeps what was typed here since and takes everything else from the recorder. */
function recNotesAdopt(card, sent, theirs) {
  var e = card._edit, r = card._r;
  e.base = theirs;
  recNotesShow(r.notesText, recNotesMerge(sent, r.notesText.value, theirs));
  if (r.notesText.value !== theirs) recNotesTyped(card);   // typed here and not on the recorder yet
  else if (!e.timer) recSet(r.notesSave, 'Saved');
}
/* Send the text box to the recorder, then read back what it merged. */
function recNotesSave(card) {
  var e = card._edit, r = card._r;
  if (!e) return Promise.resolve(true);
  clearTimeout(e.timer); e.timer = 0;
  var text = r.notesText.value;
  if (e.saving) { e.again = true; return Promise.resolve(false); }
  if (text === e.base) { recSet(r.notesSave, 'Saved'); return Promise.resolve(true); }
  e.saving = true; e.pull = false; e.pulling++;
  return recPost(card.dataset.id, 'set_notes', {text: text, base: e.base}).then(function (res) {
    res = res || {};
    if (res.ok === false) {
      recSet(r.notesSave, 'Not saved');
      notify(res.error || 'The recorder could not save the notes.', 'error');
      return false;
    }
    return recNotesFull(card.dataset.id).then(function (theirs) {
      if (card._edit !== e) return true;
      e.saving = false;
      recNotesAdopt(card, text, theirs);
      return true;
    });
  }).catch(function (err) {
    recSet(r.notesSave, 'Not saved'); notify(err.message || 'Could not save the notes.', 'error'); return false;
  }).then(function (ok) {
    e.saving = false;
    if (card._edit === e) {
      if (e.again) { e.again = false; recNotesTyped(card); }
      if (e.pull) { e.pull = false; recNotesPull(card); }
    }
    return ok;
  });
}
function recNotesEnd(card) {
  var e = card._edit, r = card._r;
  if (!e) return;
  recNotesSave(card).then(function (ok) {
    if (!ok || card._edit !== e) return;
    clearTimeout(e.timer);
    card._edit = null; card._notesKey = null;
    r.notesText.hidden = true;
    recSet(r.notesEdit, 'Edit'); r.notesEdit.dataset.act = 'notes_edit';
    recSet(r.notesSave, '');
    var cur = recs.get(card.dataset.id);
    if (cur) recUpdateCard(card, cur);
  });
}

/* The Live preview folds to its last two lines (Show all / Show less, remembered in this browser), as in the client. */
function recFoldOpen() {
  try { return window.localStorage.getItem(REC_FOLD_KEY) === '1'; } catch (e) { return false; }
}
function recSetFold(card, open) {
  var r = card._r;
  card._foldOpen = !!open;
  card.classList.toggle('cw-folded', !open);
  r.fold.setAttribute('aria-expanded', open ? 'true' : 'false');
  r.fold.title = open ? 'Fold the live preview to its last lines' : 'Show the whole live preview';
  recSetIcon(r.foldIc, open ? 'chev-down' : 'chev-up', 16);
  recSet(r.foldText, open ? 'Show less' : 'Show all');
  if (!open) r.preview.scrollTop = r.preview.scrollHeight;
}
function recToggleFold(card) {
  var open = !card._foldOpen;
  try { window.localStorage.setItem(REC_FOLD_KEY, open ? '1' : '0'); } catch (e) { /* private window: this page only */ }
  recCards.forEach(function (c) { recSetFold(c, open); });
}
/* A device name too long for its row scrolls to its end and back while the mouse is over it, as the client's
   MarqueeLabel does (1 px a tick, a pause at each end). It is driven by the pointer, so it runs with reduced motion too. */
var REC_DEV_TICK = 16, REC_DEV_PAUSE = 45;
function recDevHover(dev, on) {
  clearInterval(dev._scroll); dev._scroll = 0;
  dev.scrollLeft = 0;
  var over = dev.scrollWidth - dev.clientWidth;   // the box's content width (its text is an inline span)
  dev.classList.toggle('scroll', !!on && over > 0);
  if (!on || over <= 0) return;
  var dir = 1, pause = REC_DEV_PAUSE;
  dev._scroll = setInterval(function () {
    if (!dev.isConnected) { clearInterval(dev._scroll); return; }
    if (pause) { pause--; return; }
    dev.scrollLeft += dir;
    if ((dir > 0 && dev.scrollLeft >= over) || (dir < 0 && dev.scrollLeft <= 0)) { dir = -dir; pause = REC_DEV_PAUSE; }
  }, REC_DEV_TICK);
}

/* Buttons: disabled unless remote control is allowed, the link is up and `on`; busy while their command is in flight. */
function recLockReason(s) {
  if (!s.allowed) return 'Remote control is turned off on this computer.';
  if (recLinkDown) return 'Reconnecting to the server...';
  return '';
}
function recBtn(card, btn, key, on, title) {
  var entry = recs.get(card.dataset.id), busy = !!card._busy[key], why = entry ? recLockReason(recState(entry.item)) : '';
  btn.disabled = !!why || busy || !on;
  btn.classList.toggle('is-busy', busy);
  var tip = why || (on === false && title) || btn.dataset.title || '';
  if (tip) btn.title = tip; else btn.removeAttribute('title');
}

/* The strips under the header, in the client's order: unsupported, device errors, device back, token, recordings
   folder, server unreachable, update. Device texts of one kind are one strip with a line each, as the client does. */
function recStripList(it, s, idle) {
  var out = [], dev = [], ok = [], upd = s.update, updateText = '', unsupported = !!it.outdated;
  function add(rank, kind, ic, text, btn) { out.push({rank: rank, kind: kind, icon: ic, text: text, btn: btn || null}); }
  if (recLinkDown) add(-3, 'warn', 'c-alert', 'Lost the connection to the server. Reconnecting...');
  else if (!s.allowed) add(-3, 'warn', 'c-alert', 'Remote control is turned off on this computer.');
  s.banners.forEach(function (b) {
    if (!b) return;
    var id = String(b.id || ''), text = String(b.text || ''), rank = REC_BANNER_ORDER.indexOf(id);
    if (rank < 0) rank = REC_BANNER_ORDER.length;
    if (id === 'update_available') updateText = text;
    else if (id === 'unsupported_version') unsupported = true;
    else if (id === 'no_mic' || id === 'no_system' || id === 'device_lost') dev.push(text);
    else if (id === 'device_back' || b.level === 'ok') ok.push(text);
    else if (id === 'token_rejected' || b.level === 'error') add(rank, 'err', 'c-alert-circle', text);
    else if (b.level === 'info') add(rank, 'info', 'c-info', text);
    else add(rank, 'warn', 'c-alert', text);
  });
  if (dev.length) add(0, 'device', 'c-alert-circle', dev.join('\n'));
  if (ok.length) add(3, 'ok', 'c-check-circle', ok.join('\n'));
  if (unsupported) {
    var got = !!upd.available;
    add(-2, 'err', 'c-alert-circle', REC_UNSUPPORTED_TEXT, {label: 'Update now', cmd: got ? 'install_update' : 'check_update', primary: true, on: !got || (idle && !upd.installing), title: 'Finish the recording before updating.'});
  }
  if (upd.available || upd.installing || updateText) {
    add(99, 'info', 'c-info', upd.version ? 'Update available: ' + upd.version : updateText || 'Update available',
      {label: 'Update now', cmd: 'install_update', primary: true, on: idle && !upd.installing, title: upd.installing ? 'Installing the update...' : 'Finish the recording before updating.'});
  } else if (it.behind) {
    add(99, 'info', 'c-info', 'This app is older than the server.', {label: 'Check for updates', cmd: 'check_update', on: true});
  }
  return out.sort(function (a, b) { return a.rank - b.rank; });
}
function recStripNode(entry) {
  var node = document.createElement('div'), ic = document.createElement('span'), text = document.createElement('span');
  node.className = 'cw-strip ' + entry.kind;
  node.setAttribute('role', entry.kind === 'err' || entry.kind === 'device' ? 'alert' : 'status');
  ic.className = 'cw-strip-ic'; recSetIcon(ic, entry.icon, 20);
  text.className = 'cw-strip-text'; text.textContent = entry.text;
  node.appendChild(ic); node.appendChild(text);
  if (entry.btn) {
    var b = document.createElement('button');
    b.type = 'button'; b.className = 'cw-btn' + (entry.btn.primary ? ' primary' : ''); b.dataset.act = 'update'; b.dataset.cmd = entry.btn.cmd;
    b.textContent = entry.btn.label;
    node.appendChild(b);
    entry.node = b;
  }
  return node;
}
function recUpdateStrips(card, it, s, idle) {
  var r = card._r, list = recStripList(it, s, idle), sig = JSON.stringify(list.map(function (e) { return [e.kind, e.text, e.btn && e.btn.label, e.btn && e.btn.cmd]; }));
  if (r.strips._sig !== sig) {
    r.strips._sig = sig;
    r.strips.textContent = '';
    list.forEach(function (e) { r.strips.appendChild(recStripNode(e)); });
    r.strips._list = list;
  }
  (r.strips._list || []).forEach(function (old, i) {
    if (!old.btn || !old.node || !list[i]) return;
    recBtn(card, old.node, list[i].btn.cmd, list[i].btn.on, list[i].btn.title);
  });
}

/* Lane traces, drawn the way waveform.py paints them: a filled envelope around a centre line, newest at the right. */
function recRgba(hex, a) {
  var m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})/i.exec(String(hex).trim());
  return m ? 'rgba(' + parseInt(m[1], 16) + ',' + parseInt(m[2], 16) + ',' + parseInt(m[3], 16) + ',' + a.toFixed(3) + ')' : String(hex);
}
function recNoteLevels(id, s, now) {
  var h = recHist.get(id);
  if (!h) { h = {status: '', mic: [], system: []}; recHist.set(id, h); }
  if (s.status === 'recording' && h.status !== 'recording' && h.status !== 'finishing') { h.mic = []; h.system = []; }   // a new recording starts with an empty trace
  h.status = s.status;
  if (h.last) {
    var dt = Math.min(2000, Math.max(40, now - h.last));
    h.gap = h.gap ? h.gap * 0.85 + dt * 0.15 : dt;   // smoothed gap between frames
  }
  h.last = now;
  var any = false;
  REC_TRACKS.forEach(function (t) {
    var mode = recMeterMode(s, t.key), arr = h[t.key];
    if (mode !== 'live' && mode !== 'preview') return;
    any = true;
    arr.push([now, recLevel((s.tracks[t.key] || {}).level)]);
    while (arr.length && arr[0][0] < now - REC_TRACE_SEC * 1000 - REC_PLAYOUT_MAX - 1000) arr.shift();
  });
  if (any) recStartAnimation();
}
function recPlayoutDelay(h) {
  return Math.min(REC_PLAYOUT_MAX, Math.max(REC_PLAYOUT_MIN, (h && h.gap ? h.gap : 250) * 1.5));
}
/* The level at time ``t`` from a [[ms, level]] list (linear between frames; holds the newest when the buffer runs dry). */
function recLevelAt(arr, t) {
  if (!arr.length) return 0;
  if (t >= arr[arr.length - 1][0]) return arr[arr.length - 1][1];
  for (var i = arr.length - 1; i > 0; i--) {
    if (arr[i - 1][0] <= t) {
      var a = arr[i - 1], b = arr[i], f = (t - a[0]) / Math.max(1, b[0] - a[0]);
      return a[1] + (b[1] - a[1]) * f;
    }
  }
  return arr[0][1];
}
function recAnimating() {
  var any = false;
  recCards.forEach(function (card) {
    REC_TRACKS.forEach(function (t) { var v = card._r.lanes[t.key].view; if (v && (v.mode === 'live' || v.mode === 'preview')) any = true; });
  });
  return any;
}
function recStartAnimation() {
  if (recRaf || document.hidden || !window.requestAnimationFrame) return;
  recRaf = requestAnimationFrame(function tick() {
    recRaf = 0;
    if (document.hidden || !recAnimating()) return;   // a hidden tab or idle lanes stop the loop; the next frame restarts it
    recDrawAll();
    recRaf = requestAnimationFrame(tick);
  });
}
function recDrawTrace(card, key) {
  var d = card._r.lanes[key], cv = d.canvas, v = d.view;
  if (!v || !cv.getContext) return;
  var w = cv.clientWidth, h = cv.clientHeight;
  if (!w || !h) return;
  var ratio = window.devicePixelRatio || 1;
  if (cv.width !== Math.round(w * ratio) || cv.height !== Math.round(h * ratio)) { cv.width = Math.round(w * ratio); cv.height = Math.round(h * ratio); }
  var g = cv.getContext('2d'), cs = getComputedStyle(card);
  var border = cs.getPropertyValue('--cl-border').trim(), ink = cs.getPropertyValue(key === 'mic' ? '--cl-meter-you' : '--cl-meter-them').trim();
  var centre = h / 2, reach = Math.max(2, h / 2 - 1), live = card.dataset.status !== 'idle', i;
  g.setTransform(ratio, 0, 0, ratio, 0, 0);
  g.clearRect(0, 0, w, h);
  g.lineWidth = 1; g.strokeStyle = border;
  g.beginPath(); g.moveTo(0, Math.round(centre) + 0.5); g.lineTo(w, Math.round(centre) + 0.5); g.stroke();
  if (v.mode === 'unavail') return;
  var rec = recHist.get(card.dataset.id) || {}, pts = [], hist = rec[key] || [], span = REC_TRACE_SEC * 1000;
  if ((v.mode === 'live' || v.mode === 'preview') && hist.length) {
    // The right edge is the playhead, a little behind now; frames newer than it wait in the buffer, and the
    // edge value is interpolated so the trace slides instead of stepping.
    var head = Date.now() - recPlayoutDelay(rec);
    hist.forEach(function (p) {
      if (p[0] > head) return;
      var x = w - (head - p[0]) / span * w;
      if (x >= -2) pts.push([Math.max(0, x), p[1]]);
    });
    if (hist[0][0] <= head) pts.push([w, recLevelAt(hist, head)]);
  }
  if (!pts.length) pts = [[0, 0], [w, 0]];
  else if (pts[0][0] > 0) pts.unshift([0, 0], [pts[0][0], 0]);
  var line = v.quiet ? 95 : v.dim ? 80 : 255, fill = v.quiet ? 28 : v.dim ? 40 : live ? 70 : 40;
  // Rounded envelope: quadratic curves through the midpoints between frames (top edge left to right, bottom back).
  function edge(sign, forward) {
    var n = pts.length, j, a, b;
    for (j = 0; j < n - 1; j++) {
      a = pts[forward ? j : n - 1 - j]; b = pts[forward ? j + 1 : n - 2 - j];
      g.quadraticCurveTo(a[0], centre + sign * a[1] * reach, (a[0] + b[0]) / 2, centre + sign * ((a[1] + b[1]) / 2) * reach);
    }
    a = pts[forward ? n - 1 : 0];
    g.lineTo(a[0], centre + sign * a[1] * reach);
  }
  g.beginPath(); g.moveTo(pts[0][0], centre - pts[0][1] * reach);
  edge(-1, true);
  edge(1, false);
  g.closePath();
  g.fillStyle = recRgba(ink, fill / 255); g.fill();
  g.lineWidth = 1.3; g.lineJoin = 'round'; g.strokeStyle = recRgba(ink, line / 255); g.stroke();
}
function recDrawAll() {
  recCards.forEach(function (card) { REC_TRACKS.forEach(function (t) { recDrawTrace(card, t.key); }); });
}
/* Lanes and their icons (also called on every idle ``levels`` frame, so it must stay light). An icon is lit while
   its side is heard (REC_LIT_LEVEL, held REC_LIT_HOLD) and struck through while muted, like the client's. */
function recUpdateMeters(card, s) {
  var r = card._r, now = Date.now();
  REC_TRACKS.forEach(function (t) {
    var d = r.lanes[t.key], v = recLaneView(s, t.key, d.wrap.clientWidth || 999), tr = s.tracks[t.key] || {};
    var muted = s.status === 'recording' && !!tr.muted;
    d.view = v;
    if ((v.mode === 'live' || v.mode === 'preview') && v.level >= REC_LIT_LEVEL) d.lit = now;
    var lit = !muted && now - d.lit <= REC_LIT_HOLD;
    d.wrap.classList.toggle('dim', v.dim || v.quiet);
    d.mute.classList.toggle('lit', lit);
    recSetIcon(d.mute, muted ? t.icon + '-off' : t.icon, 20);
    recDrawTrace(card, t.key);
  });
}

function recUpdateCard(card, entry, now) {
  now = now || Date.now();
  var it = entry.item, s = recState(it), r = card._r;
  var rec = s.status === 'recording', fin = s.status === 'finishing', idle = s.status === 'idle', lock = recLockReason(s);
  var host = it.device || 'Unknown computer', href = '/meetings?q=' + encodeURIComponent(it.device || '');
  card.dataset.status = s.status;
  card.setAttribute('aria-label', host);

  // header: the computer's name, its platform and app version, then the client's buttons
  recSet(r.device, host);
  r.device.title = it.device || '';
  recSet(r.version, [it.platform_text, it.version ? 'v' + it.version : ''].filter(Boolean).join(' · ') || 'Version unknown');
  if (r.history.getAttribute('href') !== href) r.history.setAttribute('href', href);
  r.history.title = it.device ? 'Meetings recorded on ' + it.device : 'All meetings';
  recBtn(card, r.refresh, 'refresh_devices', true);
  var canSettings = rsSupported(it);
  r.settings.setAttribute('aria-haspopup', canSettings ? 'dialog' : 'true');
  r.settings.title = canSettings ? 'Change the settings of this app from here' : 'Settings of this app';
  recUpdateStrips(card, it, s, idle);
  recUpdateNotes(card, it);

  // call prompt and stop suggestion: the client's pop-up cards, here floating over the window's corner
  var prompt = s.call.prompt, sug = s.suggestion;
  r.call.hidden = !prompt;
  if (prompt) {
    var key = String(prompt.label || '') + '|' + String(prompt.name || '');
    recSet(r.callTitle, recCallText(prompt));
    if (card._callKey !== key && document.activeElement !== r.callName) { card._callKey = key; r.callName.value = prompt.name || ''; }
  }
  recBtn(card, r.callRecord, 'accept_call_prompt', true);
  recBtn(card, r.callLater, 'dismiss_call_prompt', true);
  r.suggest.hidden = !sug;
  if (sug) {
    var countdown = sug.kind === 'countdown' || sug.seconds_left != null;
    var left = sug.seconds_left == null ? null : Math.max(0, Math.round(Number(sug.seconds_left) - (now - entry.at) / 1000));
    recSet(r.suggestTitle, sug.title || (countdown ? 'Call seems to have ended' : 'Meeting seems to have ended'));
    recSet(r.suggestSub, countdown ? 'Stopping in ' + (left == null ? 0 : left) + ' s unless you keep recording.' : 'Stop recording?');
    r.sugAsk.hidden = countdown;
    r.sugCount.hidden = !countdown;
  }
  ['sugKeepA', 'sugKeepB'].forEach(function (k) { recBtn(card, r[k], 'keep_recording', true); });
  ['sugStopA', 'sugStopB'].forEach(function (k) { recBtn(card, r[k], 'stop_suggested', true); });

  // record card: clock, devices, meeting name, Start / Stop
  var el = recElapsed(entry, now);
  recSet(r.clock, recClock(rec || fin ? el : 0));
  r.clock.classList.toggle('live', rec || fin);
  REC_TRACKS.forEach(function (t) {
    var dev = r.lanes[t.key].dev, text = t.who + ': ' + ((s.tracks[t.key] && s.tracks[t.key].device) || 'unavailable');
    recSet(dev.firstChild, text);
    if (dev.title !== text) dev.title = text;
  });
  if (card._status !== s.status) { card._status = s.status; card._nameDirty = false; }
  var name = s.meeting.name || '';
  if (!card._nameDirty && document.activeElement !== r.name && r.name.value !== name) r.name.value = name;
  r.name.disabled = !!lock || fin;
  r.name.title = lock;
  // note type: the recorder's own picker, only for a recorder that advertises it and when there is a choice; it
  // follows what the recorder reports and is left alone while it is open or its command is in flight
  var caps = recCaps(it), types = recTypes || [], showType = caps.indexOf('note_type') >= 0 && types.length >= 2;
  r.type.hidden = !showType;
  if (showType) {
    var typeSig = types.map(function (t) { return t.id + '\n' + t.name; }).join('\n\n');
    if (r.type._sig !== typeSig) {
      r.type._sig = typeSig;
      r.type.textContent = '';
      types.forEach(function (t) { var o = document.createElement('option'); o.value = t.id; o.textContent = t.name; r.type.appendChild(o); });
    }
    var wantType = recHasType(types, s.noteType) ? s.noteType : recHasType(types, recDefaultType) ? recDefaultType : types[0].id;
    if (document.activeElement !== r.type && !card._busy.set_note_type && r.type.value !== wantType) r.type.value = wantType;
    r.type.disabled = !!lock || fin;
    r.type.title = lock || r.type.dataset.title || '';
  }
  // the quiet "Auto end at 3:00 PM" strip of an auto-recorded call, with its Disable auto end button
  var ae = s.autoEnd, showAuto = caps.indexOf('auto_end') >= 0 && rec && !!ae && (ae.mode === 'call' || ae.mode === 'bye' || ae.mode === 'hour' || ae.mode === 'silence');
  r.autoEnd.hidden = !showAuto;
  if (showAuto) recSet(r.autoEndText, ae.label || 'Auto end is on');
  recBtn(card, r.autoEndBtn, 'disable_auto_end', showAuto);
  var look = idle ? 'start' : fin ? 'finishing' : 'stop';
  if (r.rec._look !== look) {
    r.rec._look = look;
    r.rec.className = 'cw-rec' + (idle ? '' : ' stop');
    recSetIcon(r.recIc, idle ? 'c-mic' : 'c-stop', 18);
    recSet(r.recText, idle ? 'Start recording' : fin ? 'Finishing...' : 'Stop recording');
  }
  recBtn(card, r.rec, idle ? 'start' : 'stop', idle || rec);

  // the lanes and their mute icons (the client's tooltips: Mute your microphone / Unmute the meeting audio ...)
  recUpdateMeters(card, s);
  REC_TRACKS.forEach(function (t) {
    var tr = s.tracks[t.key] || {}, d = r.lanes[t.key], muted = rec && !!tr.muted;
    d.mute.setAttribute('aria-pressed', muted ? 'true' : 'false');
    d.mute.setAttribute('aria-label', muted ? t.ariaUnmute : t.ariaMute);
    d.mute.dataset.title = muted ? 'Unmute ' + t.heard : 'Mute ' + t.heard;
    recBtn(card, d.mute, (muted ? 'unmute' : 'mute') + t.key, rec, 'Mute is there while recording.');
  });

  // live preview: this recorder's running meeting, in the client's "You: ... / Them: ..." lines; folded, it shows
  // the newest two lines
  liveFillLines(r.preview, recLiveFor(it) || {partials: []});
  if (!card._foldOpen) r.preview.scrollTop = r.preview.scrollHeight;

  // status line
  recSet(r.statusText, recStatusLine(s));
  var pend = (Number(s.uploads.pending) || 0) + (Number(s.uploads.failed) || 0);
  r.retry.hidden = pend <= 0;
  recBtn(card, r.retry, 'retry_uploads', true);
}

function recMakeSeenRow(id) {
  var li = document.createElement('li'), name = document.createElement('span'), meta = document.createElement('span'),
      when = document.createElement('span'), btn = document.createElement('button');
  li.className = 'rec-off-row'; li.dataset.id = id;
  name.className = 'rec-off-name'; name.dataset.r = 'name';
  meta.className = 'rec-off-meta'; meta.dataset.r = 'meta';
  when.className = 'rec-off-when'; when.dataset.r = 'when';
  btn.type = 'button'; btn.className = 'btn ghost sm rec-off-forget'; btn.dataset.r = 'forget'; btn.textContent = 'Forget';
  li.appendChild(name); li.appendChild(meta); li.appendChild(when); li.appendChild(btn);
  return li;
}
/* The offline recorders: greyed compact rows (no controls), the older ones inside the collapsed Inactive group. */
function recRenderSeen(now) {
  var groups = recSeenGroups(recSeen, recs, now), keep = new Set();
  [['rec-off-list', groups.recent], ['rec-inactive-list', groups.inactive]].forEach(function (pair) {
    var list = document.getElementById(pair[0]);
    pair[1].forEach(function (r, i) {
      keep.add(r.id);
      var row = recSeenRows.get(r.id), item = recSeen.get(r.id).item;
      if (!row) { row = recMakeSeenRow(r.id); recSeenRows.set(r.id, row); }
      if (list.children[i] !== row) list.insertBefore(row, list.children[i] || null);
      var device = String(item.device || 'Recorder');
      recSet(row.querySelector('[data-r="name"]'), device);
      recSet(row.querySelector('[data-r="meta"]'), [item.platform_text, item.version ? 'v' + item.version : ''].filter(Boolean).join(' \u00b7 '));
      recSet(row.querySelector('[data-r="when"]'), recSeenText(r.age));
      row.querySelector('[data-r="forget"]').setAttribute('aria-label', 'Forget ' + device);
    });
  });
  recSeenRows.forEach(function (row, id) { if (!keep.has(id)) { row.remove(); recSeenRows.delete(id); } });
  var off = document.getElementById('rec-off'), inactive = document.getElementById('rec-inactive');
  off.hidden = !(recReady && groups.recent.length);
  recSet(document.getElementById('rec-off-count'), groups.recent.length ? '(' + groups.recent.length + ')' : '');
  inactive.hidden = !(recReady && groups.inactive.length);
  recSet(document.getElementById('rec-inactive-sum'), 'Inactive (' + groups.inactive.length + ')');
  document.getElementById('rec-none').hidden = !(recReady && recs.size === 0 && (groups.recent.length || groups.inactive.length));
  return groups.recent.length + groups.inactive.length;
}
function recForget(row) {
  var id = row.dataset.id, entry = recSeen.get(id);
  if (!entry) return;
  var device = String(entry.item.device || 'this recorder');
  confirmDialog({title: 'Forget ' + device + '?', lead: 'It shows again the next time it connects.', confirmLabel: 'Forget', danger: false}).then(function (ok) {
    if (!ok) return;
    fetch('/v1/recorders/seen/' + encodeURIComponent(id), {method: 'DELETE', credentials: 'same-origin'}).then(function (r) {
      if (r.status === 401 || r.status === 403) { window.location = '/login'; return; }
      if (r.status === 409) { notify(device + ' is connected right now.', 'error'); return; }
      if (!r.ok && r.status !== 404) throw new Error('forget failed');
      recSeen.delete(id);
      recRender();
    }).catch(function () { notify('Could not forget ' + device + '.', 'error'); });
  });
}

function recRender() {
  var grid = document.getElementById('rec-grid'), now = Date.now(), ids = recSorted();
  recCards.forEach(function (card, id) {
    if (!recs.has(id)) { clearTimeout(card._notesTimer); if (card._edit) clearTimeout(card._edit.timer); card.remove(); recCards.delete(id); }
  });
  ids.forEach(function (id, i) {
    var card = recCards.get(id);
    if (!card) { card = recMakeCard(id); recCards.set(id, card); }
    if (grid.children[i] !== card) grid.insertBefore(card, grid.children[i] || null);
    recUpdateCard(card, recs.get(id), now);
  });
  var n = ids.length, live = ids.filter(function (id) { return recState(recs.get(id).item).status === 'recording'; }).length;
  recSet(document.getElementById('rec-count'), recReady ? plural(n, 'recorder', 'recorders') + (live ? ' · ' + live + ' recording' : '') : '');
  var offline = recRenderSeen(now);
  document.getElementById('rec-empty').hidden = !(recReady && n === 0 && offline === 0);
  document.getElementById('rec-loading').hidden = recReady;
  grid.hidden = n === 0;
  grid.setAttribute('aria-busy', recReady ? 'false' : 'true');
}

/* Fetches /v1/live and repaints. Skipped while the tab is hidden or no recorder is recording. */
function recLoadLive() {
  if (document.hidden) return;
  var busy = false;
  recs.forEach(function (entry) { if (recState(entry.item).status !== 'idle') busy = true; });
  if (!busy) { if (recLive.length) { recLive = []; recRender(); } return; }
  fetch('/v1/live', {credentials: 'same-origin'}).then(function (r) {
    if (!r.ok) throw new Error('load failed');
    return r.json();
  }).then(function (data) {
    recLive = Array.isArray(data.items) ? data.items : [];
    recRender();
  }).catch(function () {});
}
function recSetConn(ok) {
  var note = document.getElementById('rec-conn'), down = !ok && recEverClosed;
  note.hidden = !down;
  if (down !== recLinkDown) { recLinkDown = down; if (recReady) recRender(); }   // the windows say why their buttons are off
}
function recApply(msg) {
  if (!msg || typeof msg !== 'object') return;
  if (msg.type === 'snapshot' && Array.isArray(msg.items)) {
    var now = Date.now();
    recs = new Map();
    msg.items.forEach(function (it) { if (it && it.instance_id) { recs.set(it.instance_id, {item: it, at: now}); recNoteLevels(it.instance_id, recState(it), now); } });
    recHist.forEach(function (_, id) { if (!recs.has(id)) recHist.delete(id); });
    recSeen = new Map();
    (Array.isArray(msg.seen) ? msg.seen : []).forEach(function (it) { if (it && it.instance_id) recSeen.set(it.instance_id, {item: it, at: now}); });
    recReady = true;
  } else if (msg.type === 'seen' && msg.item && msg.item.instance_id) {
    if (!recs.has(msg.item.instance_id)) recSeen.set(msg.item.instance_id, {item: msg.item, at: Date.now()});
  } else if (msg.type === 'unseen' && msg.instance_id) {
    recSeen.delete(msg.instance_id);
  } else if (msg.type === 'upsert' && msg.item && msg.item.instance_id) {
    recSeen.delete(msg.item.instance_id);
    recs.set(msg.item.instance_id, {item: msg.item, at: Date.now()});
    recNoteLevels(msg.item.instance_id, recState(msg.item), Date.now());
  } else if (msg.type === 'remove' && msg.instance_id) {
    recs.delete(msg.instance_id);
    recHist.delete(msg.instance_id);
  } else if (msg.type === 'levels' && msg.instance_id) {
    recApplyLevels(msg);   // idle level preview: bars only, no re-render
    return;
  } else { return; }
  recRender();
  recPanelOnFrame();
  rsOnFrame();
}
function recApplyLevels(msg) {
  var entry = recs.get(msg.instance_id);
  if (!entry || !msg.tracks || typeof msg.tracks !== 'object') return;
  var st = entry.item.state || (entry.item.state = {});
  if (st.status && st.status !== 'idle') return;
  st.tracks = st.tracks || {};
  REC_TRACKS.forEach(function (t) {
    var v = msg.tracks[t.key];
    if (typeof v !== 'number' || isNaN(v)) return;
    var tr = st.tracks[t.key] || (st.tracks[t.key] = {});
    tr.level = v; tr.peak = v;
  });
  recNoteLevels(msg.instance_id, recState(entry.item), Date.now());
  var card = recCards.get(msg.instance_id);
  if (card) recUpdateMeters(card, recState(entry.item));
}
/* Tells the server whether this page is visible: recorders only stream idle levels while someone looks. */
function recSendWatch() {
  if (!recSocket || recSocket.readyState !== 1) return;
  try { recSocket.send(JSON.stringify({type: 'watch', visible: !document.hidden})); } catch (_) {}
}
function recCheckAuth() {
  return fetch('/v1/recorders', {credentials: 'same-origin'}).then(function (r) {
    if (r.status === 401 || r.status === 403) window.location = '/login';
  }).catch(function () {});
}
function recConnect() {
  var ws, proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  try { ws = new WebSocket(proto + '//' + location.host + '/v1/recorders/events'); } catch (_) { recScheduleReconnect(); return; }
  recSocket = ws;
  ws.onopen = recSendWatch;
  ws.onmessage = function (event) {
    var msg; try { msg = JSON.parse(event.data); } catch (_) { return; }
    if (msg && msg.type === 'snapshot') { recBackoff = 1000; recSetConn(true); }
    recApply(msg);
  };
  ws.onerror = function () { try { ws.close(); } catch (_) {} };
  ws.onclose = function () {
    if (recSocket !== ws) return;
    recSocket = null; recEverClosed = true; recSetConn(false);
    recScheduleReconnect();
    recCheckAuth();
  };
}
function recScheduleReconnect() {
  clearTimeout(recReconnect);
  recReconnect = setTimeout(recConnect, recBackoff);
  recBackoff = Math.min(15000, recBackoff * 2);
}

function recPost(id, command, args) {
  return fetch('/v1/recorders/' + encodeURIComponent(id) + '/commands', {
    method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({command: command, args: args || {}})
  }).then(function (r) {
    if (r.status === 401 || r.status === 403) { window.location = '/login'; throw new Error('Signed out'); }
    if (r.status === 404) { var gone = new Error('That recorder is no longer connected.'); gone.status = 404; throw gone; }
    if (!r.ok) {
      return r.json().catch(function () { return {}; }).then(function (b) {
        var bad = new Error(typeof b.detail === 'string' && b.detail ? b.detail : 'The recorder did not respond.');
        bad.status = r.status;
        throw bad;
      });
    }
    return r.json();
  });
}
/* Sends one command; resolves true on success. Busy state comes from card._busy[key]. */
/* After Check for updates: tell the user what the recorder found, once its state says so (or after 20 s). */
function recAwaitUpdate(id, device) {
  var started = Date.now();
  (function look() {
    var cur = recs.get(id), upd = cur && cur.item && cur.item.state && cur.item.state.update;
    if (upd && (upd.available || upd.installing)) { notify('An update is available for ' + (device || 'the recorder') + '.'); return; }
    if (Date.now() - started > 20000) { notify((device || 'The recorder') + ' is up to date.'); return; }
    setTimeout(look, 500);
  })();
}
function recSend(card, key, command, args) {
  var id = card.dataset.id, entry = recs.get(id);
  if (!entry || card._busy[key]) return Promise.resolve(false);
  var device = entry.item.device;
  card._busy[key] = true; recUpdateCard(card, entry);
  return recPost(id, command, args).then(function (res) {
    res = res || {};
    var cur = recs.get(id);
    if (cur && res.state && typeof res.state === 'object') { cur.item.state = res.state; cur.at = Date.now(); }
    if (res.ok === false) { notify(res.error || 'The recorder could not do that.', 'error'); return false; }
    if (command === 'check_update' && !(res.state && res.state.update && res.state.update.available)) {
      // The recorder answers as soon as its check has started; the result arrives with a later state update.
      notify('Checking for updates on ' + (device || 'the recorder') + '...');
      recAwaitUpdate(id, device);
      return true;
    }
    notify(recToast(command, args, device, res.state));
    return true;
  }, function (err) { notify(err.message || 'Request failed', 'error'); return false; }).then(function (ok) {
    delete card._busy[key];
    var cur = recs.get(id);
    if (cur && recCards.get(id) === card) recUpdateCard(card, cur);
    return ok;
  });
}
function recCommitName(card) {
  var entry = recs.get(card.dataset.id), input = card._r.name;
  if (!entry || !card._nameDirty) return;
  var current = recState(entry.item).meeting.name || '';
  if (recState(entry.item).status !== 'recording') return;   // before recording the text only travels with Start recording
  var value = input.value.replace(/\s+/g, ' ').trim();
  if (!value || value === current) { card._nameDirty = false; input.value = current; return; }
  recSend(card, 'set_name', 'set_name', {name: value}).then(function (ok) {
    card._nameDirty = false;
    var cur = recs.get(card.dataset.id);
    if (!ok && cur) input.value = recState(cur.item).meeting.name || '';
  });
}
/* The Settings popover and the "..." menu: one open at a time, closed by a click elsewhere or Escape. */
function recClosePops(except) {
  recCards.forEach(function (card) {
    [['settings', 'settingsPop'], ['more', 'morePop']].forEach(function (pair) {
      var pop = card._r[pair[1]], btn = card._r[pair[0]];
      if (pop !== except && !pop.hidden) { pop.hidden = true; btn.setAttribute('aria-expanded', 'false'); }
    });
  });
}
function recTogglePop(card, which) {
  var pop = card._r[which + 'Pop'], btn = card._r[which], open = pop.hidden;
  recClosePops(open ? pop : null);
  pop.hidden = !open;
  btn.setAttribute('aria-expanded', open ? 'true' : 'false');
  if (open && which === 'more') { var first = pop.querySelector('[role=menuitem]:not([disabled])'); if (first) first.focus(); }
}
function recOnClick(event) {
  var toggle = event.target.closest('[data-pop]');
  if (toggle) {
    var owner = toggle.closest('.cw'), own = owner && recs.get(owner.dataset.id);
    if (toggle.dataset.pop === 'settings' && own && rsSupported(own.item)) { recClosePops(null); rsOpen(owner.dataset.id, toggle); return; }
    recTogglePop(owner, toggle.dataset.pop);
    return;
  }
  var btn = event.target.closest('[data-act]');
  if (!btn || btn.disabled) return;
  var card = btn.closest('.cw'), entry = card && recs.get(card.dataset.id);
  if (!entry) return;
  var s = recState(entry.item), act = btn.dataset.act, device = entry.item.device || 'this computer';
  if (btn.closest('.cw-pop')) recClosePops(null);
  if (act === 'recordings') { recPanelOpen(card.dataset.id, card._r.more); return; }
  if (act === 'record') {
    if (s.status === 'idle') {
      var name = card._r.name.value.replace(/\s+/g, ' ').trim();
      recSend(card, 'start', 'start', name ? {name: name} : {}).then(function (ok) { if (ok) card._nameDirty = false; });
    } else if (s.status === 'recording') {
      confirmDialog({title: 'Stop recording on ' + device + '?', lead: 'The recording ends and is uploaded for transcription.', confirmLabel: 'Stop recording'}).then(function (ok) {
        if (ok) recSend(card, 'stop', 'stop', {});
      });
    }
  } else if (act === 'mute') {
    var track = btn.dataset.track, muted = !!(s.tracks[track] && s.tracks[track].muted), cmd = muted ? 'unmute' : 'mute';
    recSend(card, cmd + track, cmd, {track: track});
  } else if (act === 'refresh') { recSend(card, 'refresh_devices', 'refresh_devices', {}); }
  else if (act === 'retry') { recSend(card, 'retry_uploads', 'retry_uploads', {}); }
  else if (act === 'update') { recSend(card, btn.dataset.cmd, btn.dataset.cmd, {}); }
  else if (act === 'accept_call') {
    var nm = card._r.callName.value.replace(/\s+/g, ' ').trim();
    recSend(card, 'accept_call_prompt', 'accept_call_prompt', nm ? {name: nm} : {});
  } else if (act === 'dismiss_call') { recSend(card, 'dismiss_call_prompt', 'dismiss_call_prompt', {}); }
  else if (act === 'stop_suggested') { recSend(card, 'stop_suggested', 'stop_suggested', {}); }
  else if (act === 'keep') { recSend(card, 'keep_recording', 'keep_recording', {}); }
  else if (act === 'disable_auto_end') { recSend(card, 'disable_auto_end', 'disable_auto_end', {}); }
  else if (act === 'notes_edit') { recNotesBegin(card); }
  else if (act === 'notes_done') { recNotesEnd(card); }
  else if (act === 'fold') { recToggleFold(card); }
}
function recOnKey(event) {
  var t = event.target, card = t.closest && t.closest('.cw');
  if (!card) return;
  var pop = t.closest('.cw-pop');
  if (event.key === 'Escape' && pop) {
    var which = pop === card._r.morePop ? 'more' : 'settings';
    recClosePops(null); card._r[which].focus();
    return;
  }
  if (pop === card._r.morePop && (event.key === 'ArrowDown' || event.key === 'ArrowUp')) {
    var items = Array.from(pop.querySelectorAll('[role=menuitem]:not([disabled])')), at = items.indexOf(document.activeElement);
    if (items.length) { event.preventDefault(); items[(at + (event.key === 'ArrowDown' ? 1 : items.length - 1)) % items.length].focus(); }
    return;
  }
  if (t.matches('[data-r="callName"]') && event.key === 'Enter') { event.preventDefault(); card._r.callRecord.click(); }
  else if (t.matches('[data-r="name"]')) {
    var idle = recState((recs.get(card.dataset.id) || {}).item).status === 'idle';
    if (event.key === 'Enter') { event.preventDefault(); if (idle) card._r.rec.click(); else t.blur(); }
    else if (event.key === 'Escape') { card._nameDirty = false; var e = recs.get(card.dataset.id); t.value = e ? recState(e.item).meeting.name || '' : ''; t.blur(); }
  }
}

/* ---- Recordings panel: every recording saved on one recorder, with its upload status ---- */
var REC_TONE_CLASS = {ok: 'done', info: 'running', warn: 'warn', error: 'error', muted: 'none'};
var REC_FILTERS = {
  uploaded: ['uploaded_ready', 'uploaded_transcribing', 'uploaded_queued', 'uploaded_error'],
  moving: ['uploading', 'waiting'],
  missing: ['not_on_server', 'in_trash', 'partial'],
  failed: ['failed', 'invalid']
};
var REC_OFFLINE_TEXT = 'Recorder went offline. Close this panel or wait for it to reconnect.';
var REC_LOCKED_TEXT = 'Remote control is turned off on this computer.';
var recPanel = null, recPanelUi = null;

function recToneClass(tone) { return REC_TONE_CLASS[tone] || 'none'; }
function recInFlight(row) { return !!row && (row.status === 'uploading' || row.status === 'waiting'); }
/* "42 recordings · 2 not on server · 1 failed": only the groups that need a look are listed. */
function recRowsSummary(summary, rowCount) {
  var by = (summary && summary.by_status) || {}, total = summary && summary.total != null ? Number(summary.total) : Number(rowCount) || 0;
  function n(k) { return Number(by[k]) || 0; }
  var parts = [plural(total, 'recording', 'recordings')];
  var missing = n('not_on_server') + n('partial'), trash = n('in_trash'), failed = n('failed') + n('invalid'), moving = n('uploading') + n('waiting');
  if (missing) parts.push(missing + ' not on server');
  if (trash) parts.push(trash + ' in server trash');
  if (failed) parts.push(failed + ' failed');
  if (moving) parts.push(moving + ' uploading');
  return parts.join(' · ');
}
function recRowText(row) {
  return [row.name, row.started ? fmtDate(row.started) : '', row.started ? fmtDate(row.started, true) : '', row.label].join(' ').toLowerCase();
}
function recFilterRows(rows, query, filter) {
  var words = String(query || '').toLowerCase().split(/\s+/).filter(Boolean), group = REC_FILTERS[filter];
  return (rows || []).filter(function (row) {
    if (group && group.indexOf(row.status) < 0) return false;
    if (!words.length) return true;
    var text = recRowText(row);
    return words.every(function (w) { return text.indexOf(w) >= 0; });
  });
}
function recCanReupload(row) {
  if (row.active || row.status === 'recording') return {ok: false, why: 'This recording is still in progress.'};
  if (row.status === 'uploading') return {ok: false, why: 'This recording is uploading right now.'};
  if (row.valid === false) return {ok: false, why: row.reason ? 'Cannot upload: ' + row.reason : 'This recording is not valid, so it cannot be uploaded.'};
  return {ok: true, why: ''};
}
function recCanDelete(row) {
  if (row.active || row.status === 'recording') return {ok: false, why: 'Stop the recording before deleting it.'};
  if (row.status === 'uploading') return {ok: false, why: 'Wait for the upload to finish before deleting it.'};
  return {ok: true, why: ''};
}
function recDeleteNeedsWarning(rows) {
  return (rows || []).some(function (r) { return r.server_has_copy === false; });
}
function recDeleteWarning(trashName) {
  return 'The server has no copy. This permanently removes the only copy (it goes to this computer\'s ' + (trashName || 'Recycle Bin') + ').';
}
function recMetaText(row) {
  var parts = [row.started ? fmtDate(row.started, true) : 'Unknown date'];
  if (row.duration_sec != null && !isNaN(Number(row.duration_sec))) parts.push(fmtDuration(row.duration_sec));
  parts.push(fmtBytes(row.size_bytes));
  return parts.join(' · ');
}
/* Options for confirmDialog() when deleting `rows` from the recorder. */
function recDeleteDialog(rows, trashName, device) {
  var risky = recDeleteNeedsWarning(rows), bare = rows.filter(function (r) { return r.server_has_copy === false; });
  var items = bare.concat(rows.filter(function (r) { return r.server_has_copy !== false; })).map(function (r) {
    return {name: r.name, meta: r.server_has_copy === false ? 'Not on server' : (r.started ? fmtDate(r.started, true) : 'Unknown date') + ' · ' + fmtBytes(r.size_bytes)};
  });
  return {
    title: 'Delete from this computer?',
    lead: risky
      ? 'Removing ' + plural(rows.length, 'recording', 'recordings') + ' from ' + (device || 'this computer') + '. ' + bare.length + (bare.length === 1 ? ' of them is' : ' of them are') + ' not on the server.'
      : 'They stay on the server. This only frees space on this computer (it goes to this computer\'s ' + (trashName || 'Recycle Bin') + ').',
    warning: risky ? recDeleteWarning(trashName) : '',
    items: items,
    itemsLabel: 'Recordings to delete',
    note: risky ? 'To get one back, restore it from the ' + (trashName || 'Recycle Bin') + ' on that computer.' : '',
    confirmLabel: 'Delete',
    danger: true
  };
}
function recSafeMeetingUrl(url) {
  url = typeof url === 'string' ? url : '';
  return url.indexOf('/sessions/') === 0 && /^[A-Za-z0-9._%~-]+$/.test(url.slice(10)) && url.indexOf('..') < 0 ? url : null;
}
/* One problem out of an HTTP answer: null when it worked. `offline` = the recorder is gone. */
function recProblem(status, data) {
  data = data || {};
  if (status === 404 || status === 409) return {offline: true, text: REC_OFFLINE_TEXT};
  if (status === 504) return {text: 'The recorder did not answer in time. Try again.'};
  if (status === 429) return {text: 'The recorder is busy with earlier commands. Try again in a moment.'};
  if (status < 200 || status >= 300) return {text: typeof data.detail === 'string' && data.detail ? data.detail : 'Could not reach the recorder.'};
  if (data.ok === false) {
    var locked = data.code === 'remote_control_disabled';
    return {text: locked ? REC_LOCKED_TEXT : (data.error || 'The recorder could not do that.'), locked: locked};
  }
  return null;
}
/* Toast / notice text for a finished reupload or delete; `names` maps session id -> name. */
function recActionMessage(kind, res, names, device) {
  res = res || {};
  var results = Array.isArray(res.results) ? res.results : [], refused = results.filter(function (r) { return !r.ok; });
  var okCount = results.length - refused.length, del = kind === 'delete', parts = [];
  var done = Number(del ? res.deleted : res.queued);
  if (isNaN(done)) done = okCount;
  if (del) {
    if (done > 0) parts.push('Deleted ' + plural(done, 'recording', 'recordings') + ' from ' + device + '.');
  } else {
    var already = Number(res.already_queued) || 0;
    if (done > 0) parts.push(plural(done, 'recording', 'recordings') + ' queued for upload on ' + device + '.');
    else if (already > 0) parts.push(plural(already, 'recording is', 'recordings are') + ' already in the upload queue on ' + device + '.');
  }
  if (refused.length) {
    var first = refused[0], why = String(first.error || 'The recorder refused.');
    parts.push('Could not ' + (del ? 'delete ' : 're-upload ') + ((names && names[first.session_id]) || 'a recording') + ': ' + why.replace(/\s+$/, '') + (/[.!?]$/.test(why) ? '' : '.')
      + (refused.length > 1 ? ' (' + (refused.length - 1) + ' more could not be ' + (del ? 'deleted' : 're-uploaded') + '.)' : ''));
  }
  if (!parts.length) parts.push(del ? 'Nothing was deleted.' : 'Nothing was queued.');
  return {text: parts.join(' '), error: refused.length > 0 && done <= 0, refused: refused.length};
}

function recPanelUiInit() {
  var g = function (id) { return document.getElementById(id); };
  recPanelUi = {
    dlg: g('rec-panel'), title: g('rp-title'), summary: g('rp-summary'), close: g('rp-close'), search: g('rp-search'), filter: g('rp-filter'),
    refresh: g('rp-refresh'), all: g('rp-all'), shown: g('rp-shown'), alert: g('rp-alert'), alertIc: g('rp-alert-ic'), alertText: g('rp-alert-text'),
    notice: g('rp-notice'), noticeIc: g('rp-notice-ic'), noticeText: g('rp-notice-text'), note: g('rp-note'), body: g('rp-body'), list: g('rp-list'),
    state: g('rp-state'), stateTitle: g('rp-state-title'), stateText: g('rp-state-text'), retry: g('rp-retry'), skel: g('rp-skel'),
    bulk: g('rp-bulk'), selCount: g('rp-selcount'), bulkRe: g('rp-bulk-reupload'), bulkDel: g('rp-bulk-delete'), bulkClear: g('rp-bulk-clear')
  };
  return recPanelUi;
}

var REC_ROW_HTML =
  '<label class="rp-check"><input type="checkbox" data-r="check"></label>'
  + '<div class="rp-main"><p class="rp-name" data-r="name"></p><p class="rp-meta" data-r="meta"></p><p class="rp-detail" data-r="detail" hidden></p></div>'
  + '<div class="rp-status"><span class="badge" data-r="badge">' + dot() + '<span class="badge-text" data-r="badgeText"></span></span></div>'
  + '<div class="rp-actions">'
  + '<a class="btn ghost sm" data-rp="open" hidden>' + icon('open', 14) + '<span class="rp-short">Open</span><span class="rp-long">Open on server</span></a>'
  + '<button type="button" class="btn secondary sm" data-rp="reupload">' + icon('refresh', 14) + '<span>Re-upload</span></button>'
  + '<button type="button" class="btn danger sm" data-rp="delete">' + icon('trash', 14) + '<span class="rp-short">Delete</span><span class="rp-long">Delete from this computer</span></button></div>';

function recRowMake() {
  var li = document.createElement('li');
  li.className = 'rp-row';
  li.innerHTML = REC_ROW_HTML;
  var r = {};
  li.querySelectorAll('[data-r]').forEach(function (el) { r[el.dataset.r] = el; });
  li.querySelectorAll('[data-rp]').forEach(function (el) { r[el.dataset.rp] = el; });
  li._r = r;
  return li;
}
function recRowUpdate(li, row, P) {
  var r = li._r, id = row.session_id, sel = P.sel.has(id), idle = P.offline || P.busy;
  li.dataset.id = id;
  li.classList.toggle('is-selected', sel);
  r.check.checked = sel;
  r.check.setAttribute('aria-label', 'Select ' + row.name);
  recSet(r.name, row.name); r.name.title = row.name;
  recSet(r.meta, recMetaText(row));
  r.badge.className = 'badge ' + recToneClass(row.tone);
  recSet(r.badgeText, row.label);
  r.detail.hidden = !row.detail;
  recSet(r.detail, row.detail || '');
  var ru = recCanReupload(row), de = recCanDelete(row);
  r.reupload.disabled = idle || !ru.ok;
  r.reupload.setAttribute('aria-label', 'Re-upload ' + row.name);
  r.reupload.title = ru.ok ? '' : ru.why;
  if (ru.ok) r.reupload.removeAttribute('title');
  r.delete.disabled = idle || !de.ok;
  r.delete.setAttribute('aria-label', 'Delete ' + row.name + ' from this computer');
  r.delete.title = de.ok ? 'Move this recording to the ' + P.trash + ' on this computer' : de.why;
  var url = recSafeMeetingUrl(row.meeting_url);
  r.open.hidden = !url;
  if (url) { r.open.setAttribute('href', url); r.open.setAttribute('aria-label', 'Open ' + row.name + ' on the server'); }
  else r.open.removeAttribute('href');
}

function recPanelSetBanner(el, icEl, textEl, text, icName) {
  el.hidden = !text;
  if (text) { recSetIcon(icEl, icName, 16); recSet(textEl, text); }
}
function recPanelRender() {
  var P = recPanel, ui = recPanelUi;
  if (!P || !ui) return;
  var hasRows = P.rows.length > 0, visible = recFilterRows(P.rows, P.q, P.f);
  recSet(ui.title, 'Recordings on ' + P.device);
  recSet(ui.summary, P.data ? recRowsSummary(P.data.summary, P.rows.length) : (P.loaded ? '' : 'Loading recordings...'));
  ui.body.setAttribute('aria-busy', !P.loaded && !P.error && !P.offline ? 'true' : 'false');
  ui.body.classList.toggle('is-offline', P.offline);
  ui.refresh.classList.toggle('is-busy', P.inflight);
  ui.refresh.setAttribute('aria-busy', P.inflight ? 'true' : 'false');
  ui.search.disabled = !hasRows; ui.filter.disabled = !hasRows;

  // alerts: offline (kept list dimmed) or a refresh problem while a list is showing
  var alertText = hasRows ? (P.offline ? REC_OFFLINE_TEXT : (P.error || '')) : '';
  recPanelSetBanner(ui.alert, ui.alertIc, ui.alertText, alertText, 'alert');
  ui.alert.className = 'banner rp-banner ' + (P.offline ? 'warn' : 'err');
  var truncated = P.data && P.data.truncated;
  ui.note.hidden = !truncated;
  recSet(ui.note, truncated ? 'Showing the newest ' + P.rows.length + ' of ' + (P.data.total || P.rows.length) + ' recordings.' : '');
  if (P.noticeText) { ui.notice.hidden = false; ui.notice.className = 'banner rp-banner ' + (P.noticeKind === 'err' ? 'err' : 'ok'); recSetIcon(ui.noticeIc, P.noticeKind === 'err' ? 'alert' : 'check', 16); recSet(ui.noticeText, P.noticeText); }
  else ui.notice.hidden = true;

  // state area: loading, empty, error, offline, no match
  var stateTitle = '', stateText = '', showRetry = false;
  if (!P.loaded && !P.error && !P.offline) { stateTitle = 'Loading recordings...'; }
  else if (!hasRows && P.error) { stateTitle = 'Could not load recordings'; stateText = P.error; showRetry = true; }
  else if (!hasRows && P.offline) { stateTitle = 'Recorder went offline'; stateText = REC_OFFLINE_TEXT; }
  else if (!hasRows) { stateTitle = 'No saved recordings on this computer.'; stateText = 'Recordings show up here after a meeting is recorded on this computer, and stay until they are deleted from it.'; }
  else if (!visible.length) { stateTitle = 'No recordings match.'; stateText = 'Try a different search or choose All statuses.'; }
  ui.state.hidden = !stateTitle;
  ui.skel.hidden = P.loaded || !!P.error || P.offline;
  recSet(ui.stateTitle, stateTitle); recSet(ui.stateText, stateText);
  ui.stateText.hidden = !stateText;
  ui.retry.hidden = !showRetry;

  // list (rows are reused by id, so focus and scroll survive a refresh)
  var keep = {};
  visible.forEach(function (row) { keep[row.session_id] = true; });
  P.els.forEach(function (li, id) { if (!keep[id]) { li.remove(); P.els.delete(id); } });
  visible.forEach(function (row, i) {
    var li = P.els.get(row.session_id);
    if (!li) { li = recRowMake(); P.els.set(row.session_id, li); }
    if (ui.list.children[i] !== li) ui.list.insertBefore(li, ui.list.children[i] || null);
    recRowUpdate(li, row, P);
  });
  ui.list.hidden = !visible.length;

  // select-all-visible and the count of what the filter hides
  var nSel = visible.filter(function (r) { return P.sel.has(r.session_id); }).length;
  ui.all.checked = visible.length > 0 && nSel === visible.length;
  ui.all.indeterminate = nSel > 0 && nSel < visible.length;
  ui.all.disabled = !visible.length || P.offline || P.busy;
  recSet(ui.shown, hasRows && visible.length !== P.rows.length ? 'Showing ' + visible.length + ' of ' + P.rows.length : '');

  // bulk bar
  var chosen = P.rows.filter(function (r) { return P.sel.has(r.session_id); });
  var canRe = chosen.filter(function (r) { return recCanReupload(r).ok; }).length, canDel = chosen.filter(function (r) { return recCanDelete(r).ok; }).length;
  ui.bulk.hidden = !chosen.length;
  recSet(ui.selCount, chosen.length + ' selected');
  ui.bulkRe.disabled = P.offline || P.busy || !canRe;
  ui.bulkDel.disabled = P.offline || P.busy || !canDel;
  ui.bulkRe.title = canRe && canRe < chosen.length ? (chosen.length - canRe) + ' cannot be re-uploaded right now and are skipped.' : '';
  ui.bulkDel.title = canDel && canDel < chosen.length ? (chosen.length - canDel) + ' cannot be deleted right now and are skipped.' : '';
  if (!ui.bulkRe.title) ui.bulkRe.removeAttribute('title');
  if (!ui.bulkDel.title) ui.bulkDel.removeAttribute('title');
}
function recPanelNotice(text, kind) {
  var P = recPanel;
  if (!P) return;
  clearTimeout(P.noticeTimer);
  P.noticeText = text; P.noticeKind = kind;
  P.noticeTimer = setTimeout(function () { if (recPanel === P) { P.noticeText = ''; recPanelRender(); } }, 12000);
  recPanelRender();
}

function recApi(P, path, body) {
  var opts = {credentials: 'same-origin'};
  if (body) { opts.method = 'POST'; opts.headers = {'Content-Type': 'application/json'}; opts.body = JSON.stringify(body); }
  return fetch('/v1/recorders/' + encodeURIComponent(P.id) + path, opts).then(function (r) {
    if (r.status === 401 || r.status === 403) { window.location = '/login'; throw new Error('Signed out'); }
    return r.json().catch(function () { return {}; }).then(function (data) { return {status: r.status, data: data || {}}; });
  });
}
function recPanelLoad() {
  var P = recPanel;
  if (!P) return Promise.resolve();
  if (P.inflight) { P.again = true; return Promise.resolve(); }
  P.inflight = true; clearTimeout(P.timer); clearTimeout(P.reload);
  recPanelRender();
  var asked = P.id;
  return recApi(P, '/recordings').then(function (res) {
    if (recPanel !== P) return;
    if (asked !== P.id) { P.again = true; return; } // the panel was rebound to a restarted recorder: ask again
    var prob = recProblem(res.status, res.data);
    if (!prob) {
      P.data = res.data; P.rows = Array.isArray(res.data.recordings) ? res.data.recordings : [];
      P.error = null; P.offline = false; P.loaded = true;
      if (res.data.device) P.device = res.data.device;
      if (res.data.trash_name) P.trash = res.data.trash_name;
      var ids = {};
      P.rows.forEach(function (r) { ids[r.session_id] = true; });
      Array.from(P.sel).forEach(function (id) { if (!ids[id]) P.sel.delete(id); });
    } else if (prob.offline) { P.offline = true; P.offlineBy = 'http'; P.loaded = true; }
    else {
      P.error = prob.text; P.loaded = true;
      if (prob.locked) { P.data = null; P.rows = []; P.sel.clear(); }
    }
  }, function () {
    if (recPanel === P) { P.error = 'Could not reach the server. Check the connection and try again.'; P.loaded = true; }
  }).then(function () {
    if (recPanel !== P) return;
    P.inflight = false;
    recPanelRender();
    if (P.again) { P.again = false; return recPanelLoad(); }
    // while something is uploading, keep the list fresh; stop as soon as nothing is in flight
    if (!P.offline && !P.error && P.rows.some(recInFlight)) P.timer = setTimeout(recPanelLoad, 4000);
  });
}
function recPanelRun(P, kind, rows, skipped) {
  var names = {}, ids = rows.map(function (r) { names[r.session_id] = r.name; return r.session_id; });
  P.busy = true; recPanelRender();
  var asked = P.id;
  return recApi(P, '/recordings/' + kind, {session_ids: ids}).then(function (res) {
    if (recPanel !== P) return;
    var prob = recProblem(res.status, res.data);
    if (prob) {
      if (prob.offline && asked === P.id) { P.offline = true; P.offlineBy = 'http'; }
      recPanelNotice(prob.text, 'err'); notify(prob.text, 'error');
      return;
    }
    var msg = recActionMessage(kind, res.data, names, P.device);
    var text = msg.text + (skipped ? ' ' + plural(skipped, 'selected recording was', 'selected recordings were') + ' skipped because ' + (skipped === 1 ? 'it' : 'they') + ' cannot be ' + (kind === 'delete' ? 'deleted' : 're-uploaded') + ' right now.' : '');
    recPanelNotice(text, msg.refused ? 'err' : 'ok'); notify(text, msg.error ? 'error' : undefined);
    (res.data.results || []).forEach(function (r) { if (r.ok) P.sel.delete(r.session_id); });
    if (kind === 'delete') recPanelLoad(); else P.reload = setTimeout(recPanelLoad, 1000);
  }, function (err) {
    if (recPanel === P && !(err && err.message === 'Signed out')) { recPanelNotice('Request failed. Check the connection and try again.', 'err'); }
  }).then(function () {
    if (recPanel !== P) return;
    P.busy = false; recPanelRender();
  });
}
function recPanelReupload(rows) {
  var P = recPanel;
  if (!P || P.busy || P.offline) return;
  var ok = rows.filter(function (r) { return recCanReupload(r).ok; }), skipped = rows.length - ok.length;
  if (!ok.length) { recPanelNotice('None of the selected recordings can be re-uploaded right now.', 'err'); return; }
  var go = ok.length > 1 ? confirmDialog({
    title: 'Re-upload ' + plural(ok.length, 'recording', 'recordings') + '?',
    lead: 'They go back in the upload queue on ' + P.device + ' and are sent to the server again.',
    items: ok.map(function (r) { return {name: r.name, meta: r.label}; }), itemsLabel: 'Recordings to re-upload',
    confirmLabel: 'Re-upload', danger: false
  }) : Promise.resolve(true);
  go.then(function (yes) { if (yes && recPanel === P && !P.busy) recPanelRun(P, 'reupload', ok, skipped); });
}
function recPanelDelete(rows) {
  var P = recPanel;
  if (!P || P.busy || P.offline) return;
  var ok = rows.filter(function (r) { return recCanDelete(r).ok; }), skipped = rows.length - ok.length;
  if (!ok.length) { recPanelNotice('None of the selected recordings can be deleted right now.', 'err'); return; }
  confirmDialog(recDeleteDialog(ok, P.trash, P.device)).then(function (yes) {
    if (yes && recPanel === P && !P.busy) recPanelRun(P, 'delete', ok, skipped);
  });
}
function recPanelRowById(id) {
  var P = recPanel, hit = null;
  if (P) P.rows.forEach(function (r) { if (r.session_id === id) hit = r; });
  return hit;
}
function recPanelOpen(id, launcher) {
  var ui = recPanelUi || recPanelUiInit(), entry = recs.get(id);
  if (!ui.dlg || !ui.dlg.showModal) return;
  recPanel = {
    id: id, device: (entry && entry.item.device) || 'this computer', trash: (entry && entry.item.platform === 'macos') ? 'Trash' : 'Recycle Bin',
    launcher: launcher || null, last: (entry && entry.item) || null, rows: [], data: null, sel: new Set(), q: '', f: 'all', loaded: false, error: null, offline: false, offlineBy: '',
    inflight: false, again: false, busy: false, timer: null, reload: null, noticeTimer: null, noticeText: '', noticeKind: '', els: new Map(), downOnBackdrop: false
  };
  ui.list.textContent = ''; ui.search.value = ''; ui.filter.value = 'all';
  recPanelRender();
  ui.dlg.showModal();
  ui.close.focus();
  recPanelLoad();
}
function recPanelClosed() {
  var P = recPanel;
  if (!P) return;
  clearTimeout(P.timer); clearTimeout(P.reload); clearTimeout(P.noticeTimer);
  recPanel = null;
  var l = P.launcher;
  if (l && document.body.contains(l)) l.focus();
}
/* A restarted client gets a NEW instance id, so the panel follows the machine, not the id. The presence frames carry
   no machine id; the stable identity is the device name plus the OS family. */
function recSameMachine(prev, item) {
  if (!prev || !item) return false;
  var a = String(prev.device || '').trim().toLowerCase(), b = String(item.device || '').trim().toLowerCase();
  if (!a || a !== b) return false;
  var pa = String(prev.platform || ''), pb = String(item.platform || '');
  return !pa || !pb || pa === pb;
}
/* The instance id `prev` (its last presence item) came back as, or null. Only an unambiguous match counts: exactly one
   other live recorder on the same machine, and it connected no earlier than `prev` did (a restart, not a twin that was
   already there). Two same-named computers, or none, mean "do not guess". */
function recFindRestarted(prev, items) {
  if (!prev) return null;
  var same = (items || []).filter(function (it) { return it && it.instance_id && it.instance_id !== prev.instance_id && recSameMachine(prev, it); });
  if (same.length !== 1) return null;
  return Number(same[0].connected_at || 0) >= Number(prev.connected_at || 0) ? same[0].instance_id : null;
}
/* Called after every recorder frame: the open panel follows its recorder going away and coming back. */
function recPanelOnFrame() {
  var P = recPanel;
  if (!P) return;
  var entry = recs.get(P.id);
  if (entry) { P.last = entry.item; if (entry.item.device) P.device = entry.item.device; }
  if (!entry) {
    var next = recFindRestarted(P.last, Array.from(recs.values()).map(function (e) { return e.item; }));
    if (next) {
      // Same computer, new instance: rebind, drop the offline state, and load its list.
      P.id = next; P.last = recs.get(next).item; P.offline = false; P.offlineBy = ''; P.error = null; P.sel.clear();
      P.device = P.last.device || P.device;
      clearTimeout(P.timer);
      recPanelRender(); recPanelLoad();
      return;
    }
    if (!P.offline || P.offlineBy !== 'ws') { P.offline = true; P.offlineBy = 'ws'; clearTimeout(P.timer); recPanelRender(); }
  } else if (P.offline && P.offlineBy === 'ws') {
    P.offline = false; P.offlineBy = '';
    recPanelRender(); recPanelLoad();
  }
}
function recPanelInit() {
  var ui = recPanelUiInit();
  if (!ui.dlg) return;
  ui.close.addEventListener('click', function () { ui.dlg.close(); });
  ui.dlg.addEventListener('close', recPanelClosed);
  ui.dlg.addEventListener('mousedown', function (e) { if (recPanel) recPanel.downOnBackdrop = e.target === ui.dlg; });
  ui.dlg.addEventListener('click', function (e) { if (e.target === ui.dlg && recPanel && recPanel.downOnBackdrop) ui.dlg.close(); });
  ui.refresh.addEventListener('click', function () { if (recPanel && !recPanel.inflight) { recPanel.noticeText = ''; recPanelLoad(); } });
  ui.retry.addEventListener('click', function () { recPanelLoad(); });
  ui.search.addEventListener('input', function () { if (recPanel) { recPanel.q = ui.search.value; recPanelRender(); } });
  ui.filter.addEventListener('change', function () { if (recPanel) { recPanel.f = ui.filter.value; recPanelRender(); } });
  ui.all.addEventListener('change', function () {
    var P = recPanel;
    if (!P) return;
    recFilterRows(P.rows, P.q, P.f).forEach(function (r) { if (ui.all.checked) P.sel.add(r.session_id); else P.sel.delete(r.session_id); });
    recPanelRender();
  });
  ui.list.addEventListener('change', function (e) {
    var P = recPanel, li = e.target.closest && e.target.closest('.rp-row');
    if (!P || !li || !e.target.matches('[data-r="check"]')) return;
    if (e.target.checked) P.sel.add(li.dataset.id); else P.sel.delete(li.dataset.id);
    recPanelRender();
  });
  ui.list.addEventListener('click', function (e) {
    var b = e.target.closest('[data-rp]'), li = b && b.closest('.rp-row');
    if (!b || !li || b.disabled) return;
    var row = recPanelRowById(li.dataset.id);
    if (!row) return;
    if (b.dataset.rp === 'reupload') recPanelReupload([row]);
    else if (b.dataset.rp === 'delete') recPanelDelete([row]);
  });
  function chosen() { var P = recPanel; return P ? P.rows.filter(function (r) { return P.sel.has(r.session_id); }) : []; }
  ui.bulkRe.addEventListener('click', function () { recPanelReupload(chosen()); });
  ui.bulkDel.addEventListener('click', function () { recPanelDelete(chosen()); });
  ui.bulkClear.addEventListener('click', function () { if (recPanel) { recPanel.sel.clear(); recPanelRender(); } });
}

/* ---- Settings panel: the app's own Settings dialog, drawn from the schema the app sends (get_settings) ----
   Nothing about the settings is written here: pages, sections, labels, help, choices, order and visibility rules all
   come from the recorder (client/settings_schema.py, the same source its dialog is built from), so the two cannot
   drift. Every value goes into the DOM with textContent / value, never into markup. */
var RS_NOT_REMOTE = 'This can only be changed on the computer itself.';
var RS_STALE_TEXT = 'The app was restarted. Its settings were loaded again.';
var rs = null, rsUi = null, rsSeq = 0;

function rsSupported(item) { return recCaps(item).indexOf('settings') >= 0; }
/* The schema's conditions: every one must hold. {key, eq} / {key, ne} against the form's current values. */
function rsCond(conds, vals) {
  return (conds || []).every(function (c) {
    var have = vals[c.key];
    if ('eq' in c && have !== c.eq) return false;
    if ('ne' in c && have === c.ne) return false;
    if ('has' in c) {
      var text = String(have == null ? '' : have).toLowerCase();
      if (!(c.has || []).some(function (w) { return text.indexOf(String(w).toLowerCase()) >= 0; })) return false;
    }
    return true;
  });
}
/* A note's wording now: the first variant whose conditions hold, else its own text. */
function rsNoteText(item, vals) {
  var v = item.variants || [];
  for (var i = 0; i < v.length; i++) if (rsCond(v[i]['if'], vals)) return v[i].text;
  return item.text || '';
}
function rsFill(template, name, value) {
  return String(template || '').split('{' + name + '}').join(String(value));
}
/* The choices of a device field: Automatic first, then the connected devices; a saved one that is not connected stays, marked. */
function rsDeviceOptions(spec, names, saved) {
  var out = [{value: '', label: spec.first_label || 'Automatic'}];
  (names || []).forEach(function (n) { out.push({value: n, label: n}); });
  if (saved && (names || []).indexOf(saved) < 0) out.push({value: saved, label: rsFill(spec.missing_label || '{name}', 'name', saved)});
  return out;
}
/* The default note type: "Server default" (with that type's name when known), each type; a saved id the list lacks stays. */
function rsNoteTypeOptions(spec, types, dflt, saved) {
  var name = '', known = {};
  (types || []).forEach(function (t) { if (t.id === dflt) name = t.name; });
  var out = [{value: '', label: name ? rsFill(spec.first_label_named, 'name', name) : spec.first_label}];
  (types || []).forEach(function (t) { known[t.id] = true; out.push({value: t.id, label: t.name}); });
  if (saved && !known[saved]) out.push({value: saved, label: rsFill((types || []).length ? spec.missing_label : spec.missing_label_unknown, 'id', saved)});
  return out;
}
function rsFieldOptions(P, item) {
  var saved = P.values[item.key];
  if (item.kind === 'choice') return (item.choices || []).map(function (c) { return {value: c[0], label: c[1]}; });
  if (item.kind === 'device') return rsDeviceOptions(item, (P.devices || {})[item.device_kind] || [], typeof saved === 'string' ? saved : '');
  return rsNoteTypeOptions(item, P.types || [], P.dflt || '', typeof saved === 'string' ? saved : '');
}
function rsIsEditable(item) { return item && item.type === 'field' && item.kind !== 'secret' && item.kind !== 'readonly'; }
function rsSame(item, a, b) {
  if (item.kind === 'text' || item.kind === 'path') {
    var fix = function (v) { v = String(v == null ? '' : v).trim(); return item.key === 'server.url' ? v.replace(/\/+$/, '') : v; };
    return fix(a) === fix(b);
  }
  return a === b;
}
/* What the person changed: {key: value} for every editable field whose form value differs from what the app has. */
function rsChanges(P) {
  var out = {};
  Object.keys(P.els || {}).forEach(function (k) {
    var item = P.els[k].item;
    if (rsIsEditable(item) && !rsSame(item, P.draft[k], P.values[k])) out[k] = P.draft[k];
  });
  return out;
}
/* The fields whose change must be confirmed first (the schema says which: the server address, turning remote control off). */
function rsConfirms(P, changes) {
  var out = [];
  Object.keys(changes).forEach(function (k) {
    var c = P.els[k] && P.els[k].item.confirm;
    if (!c) return;
    var hit = c.on === 'change' || (c.on === 'false' && changes[k] === false) || (c.on === 'true' && changes[k] === true);
    if (hit) out.push({title: c.title, lead: rsFill(c.lead, 'value', changes[k] === '' || changes[k] == null ? 'no server' : changes[k]),
      confirmLabel: c.button || 'Confirm', danger: c.on === 'false'});
  });
  return out;
}
function rsEl(tag, cls, text) {
  var e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}
function rsBlocked(P) { return !!(P.offline || P.locked || P.saving || !P.loaded); }

function rsBuildItem(P, sec, item) {
  if (item.type === 'section') { var h = rsEl('h4', 'rs-section', item.title); sec.appendChild(h); P.els[item.id] = {item: item, row: h}; return; }
  if (item.type === 'note') { var n = rsEl('p', 'rs-note'); sec.appendChild(n); P.els[item.id] = {item: item, row: n}; return; }
  var key = item.key, kind = item.kind, id = 'rs-f-' + (++rsSeq), isCheck = kind === 'bool';
  var row = rsEl('div', 'rs-field' + (isCheck ? ' is-check' : '')), ctl = rsEl('div', 'rs-control'), input = null;
  var e = {item: item, row: row, err: rsEl('p', 'rs-error'), input: null, stat: null};
  e.err.hidden = true; e.err.id = id + '-err'; e.err.setAttribute('role', 'alert');
  if (kind === 'secret' || kind === 'readonly') row.appendChild(rsEl('span', 'rs-label', item.label));
  else if (isCheck) row.appendChild(rsEl('span', 'rs-label'));
  else { var lab = rsEl('label', 'rs-label', item.label); lab.setAttribute('for', id); row.appendChild(lab); }
  if (isCheck) {
    var box = rsEl('label', 'rs-check');
    input = document.createElement('input'); input.type = 'checkbox'; input.id = id;
    box.appendChild(input); box.appendChild(rsEl('span', null, item.label)); ctl.appendChild(box);
    input.addEventListener('change', function () { P.draft[key] = input.checked; rsRefresh(P, key); });
  } else if (kind === 'text' || kind === 'path') {
    input = document.createElement('input'); input.type = 'text'; input.id = id; input.autocomplete = 'off'; input.spellcheck = false;
    input.maxLength = item.max_len || 1000;
    if (item.placeholder) input.placeholder = item.placeholder;
    ctl.appendChild(input);
    input.addEventListener('input', function () { P.draft[key] = input.value; rsRefresh(P, key); });
  } else if (kind === 'choice' || kind === 'device' || kind === 'note_type') {
    input = document.createElement('select'); input.id = id; ctl.appendChild(input);
    e.opts = rsFieldOptions(P, item);
    e.opts.forEach(function (o, i) { var opt = document.createElement('option'); opt.value = String(i); opt.textContent = o.label; input.appendChild(opt); });
    input.addEventListener('change', function () { var o = e.opts[Number(input.value)]; if (o) { P.draft[key] = o.value; rsRefresh(P, key); } });
  } else if (kind === 'secret') {
    e.stat = rsEl('span', 'rs-static'); ctl.appendChild(e.stat);
    ctl.appendChild(rsEl('p', 'rs-help', RS_NOT_REMOTE));
  } else {
    e.stat = rsEl('span', 'rs-static rs-wrap'); ctl.appendChild(e.stat);
  }
  var described = [e.err.id];
  if (item.help) {
    var help = rsEl('p', 'rs-help', item.help); help.id = id + '-help'; ctl.appendChild(help); described.push(help.id);
    if (input) input.title = item.help;
  }
  ctl.appendChild(e.err);
  if (input) { input.setAttribute('aria-describedby', described.join(' ')); input.dataset.key = key; }
  e.input = input;
  row.appendChild(ctl); sec.appendChild(row); P.els[key] = e;
}
function rsBuild(P) {
  var ui = rsUi;
  P.els = {}; P.pages = {};
  ui.nav.textContent = ''; ui.page.textContent = '';
  (P.schema.pages || []).forEach(function (pg) {
    var tab = rsEl('button', 'rs-tab', pg.title);
    tab.type = 'button'; tab.id = 'rs-tab-' + pg.key; tab.dataset.page = pg.key;
    tab.setAttribute('role', 'tab'); tab.setAttribute('aria-controls', 'rs-pg-' + pg.key);
    ui.nav.appendChild(tab);
    var sec = rsEl('section', 'rs-pg');
    sec.id = 'rs-pg-' + pg.key; sec.setAttribute('role', 'tabpanel'); sec.setAttribute('aria-labelledby', tab.id);
    sec.appendChild(rsEl('h3', 'rs-heading', pg.title));
    (pg.items || []).forEach(function (item) { rsBuildItem(P, sec, item); });
    ui.page.appendChild(sec);
    P.pages[pg.key] = {tab: tab, sec: sec};
  });
  var keys = (P.schema.pages || []).map(function (pg) { return pg.key; });
  P.page = keys.indexOf(P.page) >= 0 ? P.page : keys[0];
}
/* Draft values into the controls (after a load or a save). */
function rsSyncControls(P) {
  Object.keys(P.els).forEach(function (k) {
    var e = P.els[k], item = e.item;
    if (item.type !== 'field') return;
    var v = P.draft[k];
    if (e.stat) { recSet(e.stat, item.kind === 'secret' ? (P.values[k] ? 'Set' : 'Not set') : (P.values[k] == null || P.values[k] === '' ? '—' : String(P.values[k]))); return; }
    if (item.kind === 'bool') e.input.checked = v === true;
    else if (item.kind === 'text' || item.kind === 'path') { if (e.input.value !== String(v == null ? '' : v)) e.input.value = String(v == null ? '' : v); }
    else {
      var at = 0;
      e.opts.forEach(function (o, i) { if (o.value === v) at = i; });
      e.input.value = String(at);
    }
  });
}
/* Visibility, enabled state, note wording and errors, from the schema's own rules and the current draft. */
function rsApplyRules(P) {
  var blocked = rsBlocked(P);
  Object.keys(P.els).forEach(function (k) {
    var e = P.els[k], item = e.item;
    e.row.hidden = !rsCond(item.show_if, P.draft);
    if (item.type === 'note') { recSet(e.row, rsNoteText(item, P.draft)); if (!e.row.textContent) e.row.hidden = true; return; }
    if (item.type !== 'field') return;
    var err = (P.errors || {})[k];
    e.err.hidden = !err; recSet(e.err, err || '');
    if (e.input) {
      var off = blocked || !rsCond(item.enabled_if, P.draft)
        || (item.disable_if_empty && ((P.devices || {})[item.device_kind] || []).length === 0);
      e.input.disabled = !!off;
      if (err) e.input.setAttribute('aria-invalid', 'true'); else e.input.removeAttribute('aria-invalid');
    }
  });
}
function rsShowPage(P, key) {
  P.page = key;
  Object.keys(P.pages || {}).forEach(function (k) {
    var on = k === key, pg = P.pages[k];
    pg.sec.hidden = !on;
    pg.tab.setAttribute('aria-selected', on ? 'true' : 'false');
    pg.tab.tabIndex = on ? 0 : -1;
    pg.tab.classList.toggle('is-on', on);
    var bad = Object.keys(P.errors || {}).some(function (k2) { return P.els[k2] && pg.sec.contains(P.els[k2].row); });
    pg.tab.classList.toggle('has-error', bad);
  });
}
function rsRefresh(P, key) {
  if (!P || rs !== P) return;
  if (key && P.errors) delete P.errors[key];   // editing a field clears its error
  if (P.status) { P.status = ''; P.statusKind = ''; }
  if (P.loaded) { rsApplyRules(P); rsShowPage(P, P.page); }
  rsRenderFoot(P);
}
function rsDirty(P) { return !!P.loaded && Object.keys(rsChanges(P)).length > 0; }
function rsRenderFoot(P) {
  var ui = rsUi, dirty = rsDirty(P);
  ui.save.disabled = rsBlocked(P) || !dirty;
  ui.save.classList.toggle('is-busy', !!P.saving);
  recSet(ui.saveText, P.saving ? 'Saving...' : 'Save');
  ui.cancel.disabled = !!P.saving;
  recSet(ui.status, P.status || (dirty ? 'Unsaved changes' : ''));
  ui.status.className = 'rs-status' + (P.statusKind ? ' ' + P.statusKind : '');
}
function rsRender() {
  var P = rs, ui = rsUi;
  if (!P || !ui) return;
  recSet(ui.title, 'Settings on ' + P.device);
  recSet(ui.summary, P.loaded ? 'The same settings as in the app on that computer.' : '');
  ui.main.setAttribute('aria-busy', P.loading ? 'true' : 'false');
  var alertText = P.offline ? REC_OFFLINE_TEXT : P.locked ? REC_LOCKED_TEXT : (P.loaded && P.error ? P.error : '');
  recPanelSetBanner(ui.alert, ui.alertIc, ui.alertText, alertText, 'alert');
  ui.alert.className = 'banner rp-banner ' + (P.offline || P.locked ? 'warn' : 'err');
  var stateTitle = '', stateText = '', retry = false;
  if (!P.loaded) {
    if (P.loading) stateTitle = 'Loading settings...';
    else if (P.offline) { stateTitle = 'Recorder went offline'; stateText = REC_OFFLINE_TEXT; }
    else if (P.locked) { stateTitle = 'Remote control is turned off'; stateText = REC_LOCKED_TEXT; }
    else if (P.error) { stateTitle = 'Could not load the settings'; stateText = P.error; retry = true; }
  }
  ui.state.hidden = !stateTitle;
  recSet(ui.stateTitle, stateTitle); recSet(ui.stateText, stateText);
  ui.stateText.hidden = !stateText; ui.retry.hidden = !retry;
  ui.nav.hidden = !P.loaded; ui.page.hidden = !P.loaded;
  ui.body.classList.toggle('is-blocked', P.offline || P.locked);
  if (P.loaded) { rsApplyRules(P); rsShowPage(P, P.page); }
  rsRenderFoot(P);
}

function rsLoad() {
  var P = rs;
  if (!P || P.loading) return;
  P.loading = true; P.error = ''; rsRender();
  var id = P.id;
  recPost(id, 'get_settings', {}).then(function (res) {
    if (rs !== P) return;
    P.loading = false;
    if (id !== P.id) { rsLoad(); return; }   // the panel was rebound to a restarted recorder: ask it
    res = res || {};
    var data = res.result || {};
    if (res.ok === false) {
      if (res.code === 'remote_control_disabled') P.locked = true;
      else P.error = res.error || 'The recorder could not do that.';
    } else if (!data.schema || !Array.isArray(data.schema.pages) || !data.schema.pages.length) {
      P.error = 'The app sent no settings.';
    } else {
      P.schema = data.schema; P.values = data.values || {}; P.devices = data.devices || {mic: [], system: []};
      P.types = data.note_types || []; P.dflt = data.server_default_note_type || '';
      P.draft = Object.assign({}, P.values); P.errors = {}; P.locked = false; P.error = '';
      rsBuild(P); rsSyncControls(P); P.loaded = true;
    }
    rsRender();
  }, function (err) {
    if (rs !== P) return;
    P.loading = false;
    if (err && (err.status === 404 || err.status === 409)) P.offline = true; else P.error = (err && err.message) || 'Could not reach the recorder.';
    rsRender();
  });
}

function rsAskAll(list) {
  return list.reduce(function (p, ask) { return p.then(function (yes) { return yes ? confirmDialog(ask) : false; }); }, Promise.resolve(true));
}
function rsSave() {
  var P = rs;
  if (!P || rsBlocked(P)) return;
  var changes = rsChanges(P);
  if (!Object.keys(changes).length) return;
  rsAskAll(rsConfirms(P, changes)).then(function (yes) {
    if (!yes || rs !== P || rsBlocked(P)) return;
    P.saving = true; P.errors = {}; P.status = ''; P.statusKind = ''; rsRender();
    var id = P.id, device = P.device;
    return recPost(id, 'set_settings', {changes: changes}).then(function (res) {
      if (rs !== P) return;
      P.saving = false;
      res = res || {};
      var data = res.result || {}, cur = recs.get(id);
      if (cur && res.state && typeof res.state === 'object') { cur.item.state = res.state; cur.at = Date.now(); }
      if (res.ok === false) {
        P.errors = data.errors && typeof data.errors === 'object' ? data.errors : {};
        if (data.values) P.values = Object.assign(P.values, data.values);
        var first = Object.keys(P.errors).filter(function (k) { return P.els[k]; })[0];
        if (first) {
          Object.keys(P.pages).forEach(function (pk) { if (P.pages[pk].sec.contains(P.els[first].row)) P.page = pk; });
          P.status = 'Not saved. Fix the settings marked below.';
        } else if (res.code === 'remote_control_disabled') { P.locked = true; P.status = REC_LOCKED_TEXT; }
        else { P.status = res.error || 'The recorder could not save.'; }
        P.statusKind = 'err';
        rsRender();
        notify(P.status || 'The recorder could not save.', 'error');
        return;
      }
      P.values = Object.assign(P.values, data.values || {});
      Object.keys(changes).forEach(function (k) { P.draft[k] = P.values[k] !== undefined ? P.values[k] : changes[k]; });
      if (P.values.remote_control_allowed === false) P.locked = true;   // it was just turned off: nothing more can be changed here
      rsSyncControls(P);
      P.status = 'Saved'; P.statusKind = 'ok';
      rsRender();
      notify('Settings saved on ' + device + '.');
    }, function (err) {
      if (rs !== P) return;
      P.saving = false;
      if (err && (err.status === 404 || err.status === 409)) P.offline = true;
      else { P.status = (err && err.message) || 'Request failed. Check the connection and try again.'; P.statusKind = 'err'; }
      rsRender();
    });
  });
}

function rsOpen(id, launcher) {
  var ui = rsUi || rsUiInit(), entry = recs.get(id);
  if (!ui.dlg || !ui.dlg.showModal) return;
  rs = {
    id: id, device: (entry && entry.item.device) || 'this computer', launcher: launcher || null, last: (entry && entry.item) || null,
    schema: null, values: {}, draft: {}, devices: null, types: [], dflt: '', els: {}, pages: {}, page: 'general', errors: {},
    loaded: false, loading: false, saving: false, offline: false, locked: !!(entry && !recState(entry.item).allowed), error: '',
    status: '', statusKind: '', downOnBackdrop: false
  };
  ui.nav.textContent = ''; ui.page.textContent = '';
  rsRender();
  ui.dlg.showModal();
  ui.close.focus();
  if (!rs.locked) rsLoad();
}
function rsClosed() {
  var P = rs;
  if (!P) return;
  rs = null;
  var l = P.launcher;
  if (l && document.body.contains(l)) l.focus();
}
/* Cancel / the close button / Escape: asks first when something is unsaved. */
function rsRequestClose() {
  var P = rs, ui = rsUi;
  if (!P || P.saving) return;
  if (!rsDirty(P)) { ui.dlg.close(); return; }
  confirmDialog({title: 'Discard your changes?', lead: 'The changes on this page have not been saved to ' + P.device + '.', confirmLabel: 'Discard', danger: true}).then(function (yes) {
    if (yes && rs === P) ui.dlg.close();
  });
}
/* After every recorder frame: the open panel follows its recorder going away, coming back, being restarted or locked. */
function rsOnFrame() {
  var P = rs;
  if (!P) return;
  var entry = recs.get(P.id);
  if (entry) { P.last = entry.item; if (entry.item.device) P.device = entry.item.device; }
  if (!entry) {
    var next = recFindRestarted(P.last, Array.from(recs.values()).map(function (e) { return e.item; }));
    if (next) {
      // Same computer, new instance: the settings come from the new app instance, so load them again.
      P.id = next; P.last = recs.get(next).item; P.offline = false; P.loaded = false; P.errors = {}; P.saving = false;
      P.device = P.last.device || P.device; P.status = RS_STALE_TEXT; P.statusKind = '';
      P.locked = !recState(P.last).allowed;
      rsRender();
      if (!P.locked) rsLoad();
      return;
    }
    if (!P.offline) { P.offline = true; rsRender(); }
    return;
  }
  var locked = !recState(entry.item).allowed;
  if (P.offline) { P.offline = false; if (!rsDirty(P) && !locked) { P.loaded = false; rsRender(); rsLoad(); return; } rsRender(); }
  if (locked !== P.locked) {
    P.locked = locked;
    rsRender();
    if (!locked && !P.loaded) rsLoad();
  }
}
function rsUiInit() {
  var g = function (id) { return document.getElementById(id); };
  rsUi = {
    dlg: g('set-panel'), title: g('rs-title'), summary: g('rs-summary'), close: g('rs-close'), alert: g('rs-alert'),
    alertIc: g('rs-alert-ic'), alertText: g('rs-alert-text'), main: g('rs-main'), nav: g('rs-nav'), body: g('rs-body'),
    page: g('rs-page'), state: g('rs-state'), stateTitle: g('rs-state-title'), stateText: g('rs-state-text'), retry: g('rs-retry'),
    status: g('rs-status'), cancel: g('rs-cancel'), save: g('rs-save'), saveText: g('rs-save-text')
  };
  return rsUi;
}
function rsInit() {
  var ui = rsUiInit();
  if (!ui.dlg) return;
  ui.close.addEventListener('click', rsRequestClose);
  ui.cancel.addEventListener('click', rsRequestClose);
  ui.save.addEventListener('click', rsSave);
  ui.retry.addEventListener('click', function () { rsLoad(); });
  ui.dlg.addEventListener('close', rsClosed);
  ui.dlg.addEventListener('cancel', function (e) { if (rs && (rs.saving || rsDirty(rs))) { e.preventDefault(); rsRequestClose(); } });
  ui.dlg.addEventListener('mousedown', function (e) { if (rs) rs.downOnBackdrop = e.target === ui.dlg; });
  ui.dlg.addEventListener('click', function (e) { if (e.target === ui.dlg && rs && rs.downOnBackdrop) rsRequestClose(); });
  ui.nav.addEventListener('click', function (e) {
    var tab = e.target.closest('[data-page]');
    if (tab && rs && rs.loaded) { rsShowPage(rs, tab.dataset.page); }
  });
  ui.nav.addEventListener('keydown', function (e) {
    if (!rs || !rs.loaded || (e.key !== 'ArrowDown' && e.key !== 'ArrowUp' && e.key !== 'ArrowLeft' && e.key !== 'ArrowRight')) return;
    var keys = (rs.schema.pages || []).map(function (pg) { return pg.key; }), at = keys.indexOf(rs.page);
    var step = e.key === 'ArrowDown' || e.key === 'ArrowRight' ? 1 : -1, to = keys[(at + step + keys.length) % keys.length];
    e.preventDefault(); rsShowPage(rs, to); rs.pages[to].tab.focus();
  });
}

function recInit() {
  var grid = document.getElementById('rec-grid');
  recPanelInit();
  rsInit();
  grid.addEventListener('click', recOnClick);
  ['rec-off', 'rec-inactive'].forEach(function (id) {
    document.getElementById(id).addEventListener('click', function (event) {
      var btn = event.target.closest && event.target.closest('[data-r="forget"]');
      if (btn) recForget(btn.closest('.rec-off-row'));
    });
  });
  grid.addEventListener('keydown', recOnKey);
  grid.addEventListener('input', function (event) {
    if (event.target.matches('[data-r="name"]')) event.target.closest('.cw')._nameDirty = true;
    else if (event.target.matches('[data-r="notesText"]')) recNotesTyped(event.target.closest('.cw'));
  });
  grid.addEventListener('mouseover', function (event) {
    var dev = event.target.closest && event.target.closest('.cw-dev');
    if (dev && !dev.contains(event.relatedTarget)) recDevHover(dev, true);
  });
  grid.addEventListener('mouseout', function (event) {
    var dev = event.target.closest && event.target.closest('.cw-dev');
    if (dev && !dev.contains(event.relatedTarget)) recDevHover(dev, false);
  });
  grid.addEventListener('change', function (event) {
    if (!event.target.matches('[data-r="type"]')) return;
    var card = event.target.closest('.cw'), sel = event.target;
    recSend(card, 'set_note_type', 'set_note_type', {note_type: sel.value}).then(function (ok) {
      if (ok) return;   // refused: show what the recorder still has
      var cur = recs.get(card.dataset.id), back = cur ? recState(cur.item).noteType : null;
      sel.value = recHasType(recTypes, back) ? back : recDefaultType;
    });
  });
  grid.addEventListener('focusout', function (event) {
    if (event.target.matches('[data-r="name"]')) recCommitName(event.target.closest('.cw'));
  });
  document.addEventListener('click', function (event) { if (!event.target.closest('.cw-menu-wrap')) recClosePops(null); });
  // The clock and countdowns tick locally between frames (no polling); a resize redraws the lane traces.
  setInterval(function () { if (recs.size || recSeen.size) recRender(); }, 1000);
  window.addEventListener('resize', function () { requestAnimationFrame(recDrawAll); });
  // The live-preview lines of the meetings being recorded (only polled while some recorder is recording).
  setInterval(recLoadLive, 2500);
  recLoadLive();
  // Keep telling the server this page is (not) visible; a quiet or hidden page lets recorders stop metering.
  document.addEventListener('visibilitychange', recSendWatch);
  setInterval(recSendWatch, 10000);
  recLoadTypes();
  fetch('/v1/recorders', {credentials: 'same-origin'}).then(function (r) {
    if (r.status === 401 || r.status === 403) { window.location = '/login'; throw new Error('Signed out'); }
    if (!r.ok) throw new Error('Unable to load');
    return r.json();
  }).then(function (data) {
    if (!recReady) { recApply({type: 'snapshot', items: data.items || [], seen: data.seen || []}); }
  }).catch(function () {});
  recConnect();
}
if (typeof document !== 'undefined' && document.getElementById && document.getElementById('rec-grid')) recInit();
"""


def render_recorders_page(*, token_configured: bool, appearance: str = "system") -> str:
    """Live recorders: one card per running Meeting Notes app, with remote control.

    Rendered client-side. First paint comes from ``GET /v1/recorders``; after that the page
    holds a websocket to ``/v1/recorders/events`` (frames ``snapshot`` / ``upsert`` / ``remove``
    / ``seen`` / ``unseen`` / ``ping``) and patches cards in place, keyed by ``instance_id``. Buttons POST
    ``/v1/recorders/{instance_id}/commands`` (the protocol lives in ``meeting_notes/remote.py``).
    """
    body = (
        f"""
<div class="page recorders-page">
<header class="page-head">
  <h1>Recorders</h1>
  <span class="count" id="rec-count" aria-live="polite"></span>
  <div class="head-tools"><span class="rec-conn" id="rec-conn" role="status" hidden>{_icon("refresh")}<span>Reconnecting...</span></span>
    <a class="btn secondary" id="rec-install" href="/install">{_icon("download")}<span>Install a recorder</span></a></div>
</header>
<p class="rec-loading" id="rec-loading">Loading recorders...</p>
<div class="rec-grid" id="rec-grid" aria-busy="true" hidden></div>
<p class="rec-none" id="rec-none" hidden>No recorders are connected right now.</p>
<section class="rec-off" id="rec-off" aria-labelledby="rec-off-heading" hidden>
  <h2 class="sect-sub" id="rec-off-heading">Not connected <span class="count" id="rec-off-count"></span></h2>
  <ul class="rec-off-list" id="rec-off-list"></ul>
</section>
<details class="rec-inactive" id="rec-inactive" hidden>
  <summary id="rec-inactive-sum">Inactive</summary>
  <p class="help">Not connected for more than 30 days.</p>
  <ul class="rec-off-list" id="rec-inactive-list"></ul>
</details>
<div class="empty-teach rec-empty" id="rec-empty" hidden>
  <h2>No recorders are running</h2>
  <p>Open Meeting Notes on a computer and it appears here.</p>
  <p class="help">A computer that is asleep, offline or has the app closed is not listed. Once it is running you can see what it is recording and control it from this page.</p>
</div>
<section class="gpu-helpers-section" aria-labelledby="gpu-helpers-heading">
  <h2 class="sect-sub" id="gpu-helpers-heading">GPU helpers <span class="count" id="gpu-helpers-count"></span></h2>
  <p class="help">Computers that transcribe for this server on their graphics card. Turn this on under
  <a href="/settings#gpu-helpers-heading">Settings, GPU helpers</a>.</p>
  <div id="gpu-helpers-box" aria-live="polite"><p class="help" role="status">Loading helpers...</p></div>
</section>
</div>
<dialog class="dialog rec-panel" id="rec-panel" aria-labelledby="rp-title">
  <div class="rp-head">
    <div class="rp-titles">
      <h2 class="rp-title" id="rp-title">Recordings</h2>
      <p class="rp-summary" id="rp-summary" role="status" aria-live="polite"></p>
    </div>
    <button type="button" class="btn ghost icon-only" id="rp-close" aria-label="Close recordings">{_icon("x")}</button>
  </div>
  <div class="rp-tools">
    <label class="rp-search"><span class="sr-only">Search recordings</span><input type="text" id="rp-search" autocomplete="off" spellcheck="false" placeholder="Search by name or date"></label>
    <label class="rp-filter"><span class="sr-only">Filter by status</span><select id="rp-filter">
      <option value="all">All statuses</option>
      <option value="uploaded">Uploaded</option>
      <option value="moving">Uploading or waiting</option>
      <option value="missing">Not on server</option>
      <option value="failed">Failed or invalid</option>
    </select></label>
    <button type="button" class="btn secondary" id="rp-refresh">{_icon("refresh")}<span>Refresh</span></button>
  </div>
  <div class="rp-pick"><label><input type="checkbox" id="rp-all"><span>Select all visible</span></label><span class="rp-shown" id="rp-shown"></span></div>
  <div class="rp-banners">
    <div class="banner rp-banner" id="rp-alert" role="status" hidden><span id="rp-alert-ic"></span><span id="rp-alert-text"></span></div>
    <div class="banner rp-banner" id="rp-notice" role="status" hidden><span id="rp-notice-ic"></span><span id="rp-notice-text"></span></div>
    <p class="rp-note" id="rp-note" hidden></p>
  </div>
  <div class="rp-body" id="rp-body" aria-busy="true">
    <ul class="rp-list" id="rp-list" role="list" hidden></ul>
    <div class="rp-skel" id="rp-skel" aria-hidden="true"><i></i><i></i><i></i><i></i></div>
    <div class="rp-state" id="rp-state" hidden>
      <p class="rp-state-title" id="rp-state-title"></p>
      <p class="rp-state-text" id="rp-state-text" hidden></p>
      <button type="button" class="btn secondary" id="rp-retry" hidden>Retry</button>
    </div>
  </div>
  <div class="rp-bulk" id="rp-bulk" hidden>
    <span class="rp-selcount" id="rp-selcount"></span>
    <button type="button" class="btn secondary" id="rp-bulk-reupload">{_icon("refresh")}<span>Re-upload</span></button>
    <button type="button" class="btn danger" id="rp-bulk-delete">{_icon("trash")}<span>Delete from this computer</span></button>
    <button type="button" class="btn ghost" id="rp-bulk-clear">Clear</button>
  </div>
</dialog>
<dialog class="dialog rec-panel rs-panel" id="set-panel" aria-labelledby="rs-title">
  <div class="rp-head">
    <div class="rp-titles">
      <h2 class="rp-title" id="rs-title">Settings</h2>
      <p class="rp-summary" id="rs-summary" role="status" aria-live="polite"></p>
    </div>
    <button type="button" class="btn ghost icon-only" id="rs-close" aria-label="Close settings">{_icon("x")}</button>
  </div>
  <div class="rp-banners">
    <div class="banner rp-banner" id="rs-alert" role="status" hidden><span id="rs-alert-ic"></span><span id="rs-alert-text"></span></div>
  </div>
  <div class="rs-main" id="rs-main" aria-busy="true">
    <nav class="rs-nav" id="rs-nav" role="tablist" aria-label="Settings pages" aria-orientation="vertical" hidden></nav>
    <div class="rs-body" id="rs-body">
      <div class="rp-state" id="rs-state">
        <p class="rp-state-title" id="rs-state-title">Loading settings...</p>
        <p class="rp-state-text" id="rs-state-text" hidden></p>
        <button type="button" class="btn secondary" id="rs-retry" hidden>Retry</button>
      </div>
      <div class="rs-page" id="rs-page" hidden></div>
    </div>
  </div>
  <div class="rs-foot">
    <p class="rs-links"><a href="/settings#settings-recorders-heading">Recorder settings on the server</a><a href="/settings#settings-logs-heading">Client logs</a></p>
    <span class="rs-status" id="rs-status" role="status" aria-live="polite"></span>
    <div class="rs-buttons">
      <button type="button" class="btn secondary" id="rs-cancel">Cancel</button>
      <button type="button" class="btn primary" id="rs-save" disabled><span id="rs-save-text">Save</span></button>
    </div>
  </div>
</dialog>
{_CONFIRM_DIALOG_HTML}
<script>
"""
        + _JS_HELPERS
        + _RECORDERS_JS
        + """
</script>
<script>
"""
        + _GPU_HELPERS_JS
        + _GPU_RECORDERS_JS
        + """
</script>
"""
    )
    return _shell(
        "Recorders",
        body,
        token_configured=token_configured,
        active="recorders",
        main_class="recorders-page-main",
        appearance=appearance,
    )
