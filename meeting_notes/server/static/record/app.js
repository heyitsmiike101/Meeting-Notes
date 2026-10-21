/* Meeting Notes mobile recorder (the /record/ page). Design contract: docs/mobile-recorder-design.md.

   Three layers in one file:
   1. Pure helpers (top of file): mime choice, session ids, run bookkeeping, the upload plan and backoff, the live-audio
      resampler and wire framing. They touch no DOM, so tests/test_mobile_web.py runs them under node.
   2. Storage and upload (IndexedDB "mn-record", the /v1/mobile/sessions API), written against injected deps.
   3. The page (boot() at the bottom): only runs when the recorder markup is present.

   Server values reach the DOM only through textContent. */
'use strict';

/* ---------------------------------------------------------------- 1. pure helpers */

var TIMESLICE_MS = 5000;           // MediaRecorder.start(timeslice)
var ROTATE_MS = 5 * 60 * 1000;     // a new recorder (run) every 5 minutes: a lost run costs at most ~5 minutes
var STALL_MS = 20000;              // no chunk for this long while "recording" = the browser paused us
var BACKOFF_MIN_MS = 2000;
var BACKOFF_MAX_MS = 60000;
var POLL_MS = 3000;
var API = '/v1/mobile/sessions';
var SYSTEM_TRACK_INDEX = 1;        // wire.TRACKS = ("mic", "system"): a phone recording is the "system" track
var LIVE_FRAME_SAMPLES = 320;      // 20 ms at 16 kHz
var MAX_RUNS = 190;                // the server accepts run indexes 0..199: stop restarting well before
var LIVE_MAX_LINES = 200;          // partial transcript lines kept on the page
var MIME_CANDIDATES = ['audio/webm;codecs=opus', 'audio/mp4;codecs=mp4a.40.2', 'audio/mp4'];

/* First supported type of MIME_CANDIDATES ('' when none: the browser then picks its own and we record recorder.mimeType). */
function pickMime(isSupported) {
  for (var i = 0; i < MIME_CANDIDATES.length; i++) {
    var ok = false;
    try { ok = !!isSupported(MIME_CANDIDATES[i]); } catch (e) { ok = false; }
    if (ok) return MIME_CANDIDATES[i];
  }
  return '';
}

function pad2(n) { return (n < 10 ? '0' : '') + n; }

function slugify(text, max) {
  var t = String(text == null ? '' : text).replace(/[^A-Za-z0-9._-]+/g, '-').replace(/^-+|-+$/g, '');
  return t.slice(0, max || 24).replace(/-+$/, '');
}

/* YYYY-MM-DD_HH-MM-SS_Phone-<device>_<8 hex>: the same shape the desktop recorder uses, so it passes store.is_safe_id. */
function newSessionId(date, device, hex8) {
  var stamp = date.getFullYear() + '-' + pad2(date.getMonth() + 1) + '-' + pad2(date.getDate()) + '_' +
    pad2(date.getHours()) + '-' + pad2(date.getMinutes()) + '-' + pad2(date.getSeconds());
  return stamp + '_Phone-' + (slugify(device, 24) || 'phone') + '_' + String(hex8);
}

function randomHex(bytes, cryptoObj) {
  var out = '', i, b;
  if (cryptoObj && cryptoObj.getRandomValues) {
    var a = new Uint8Array(bytes); cryptoObj.getRandomValues(a);
    for (i = 0; i < a.length; i++) out += (a[i] < 16 ? '0' : '') + a[i].toString(16);
    return out;
  }
  for (i = 0; i < bytes; i++) { b = Math.floor(Math.random() * 256); out += (b < 16 ? '0' : '') + b.toString(16); }
  return out;
}

function defaultMeetingName(date) {
  var months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  var h = date.getHours(), ap = h >= 12 ? 'PM' : 'AM';
  h = h % 12 || 12;
  return 'Phone recording, ' + months[date.getMonth()] + ' ' + date.getDate() + ', ' + h + ':' + pad2(date.getMinutes()) + ' ' + ap;
}

/* The mobile gate. env = {coarse, width, ua, touchPoints}. */
function platformOf(env) {
  var ua = String((env && env.ua) || '');
  if (/iPhone|iPad|iPod/.test(ua) || (/Macintosh/.test(ua) && (env.touchPoints || 0) > 1)) return 'iOS';
  if (/Android/.test(ua)) return 'Android';
  return '';
}
function isMobileClient(env) {
  if (platformOf(env)) return true;
  return !!(env && env.coarse && env.width && env.width <= 820 && /Mobile/.test(String(env.ua || '')));
}
function defaultDeviceLabel(env) {
  var ua = String((env && env.ua) || '');
  if (/iPad/.test(ua)) return 'iPad';
  if (platformOf(env) === 'iOS') return 'iPhone';
  return 'Android phone';
}

/* ---- runs: one MediaRecorder from start() to stop() ---- */

function newRun(index, mime, nowMs) {
  return {index: index, mime: mime, state: 'recording', started_ms: nowMs, ended_ms: null, chunk_count: 0};
}
/* Append a run. A previous run that closed without saving anything is dropped and its index reused, so the
   manifest never has an empty run in the middle. */
function appendRun(runs, mime, nowMs) {
  var list = runs.slice();
  var last = list[list.length - 1];
  if (last && last.state !== 'recording' && last.chunk_count === 0) list.pop();
  var prev = list[list.length - 1];
  var run = newRun(prev ? prev.index + 1 : 0, mime, nowMs);
  list.push(run);
  return {runs: list, run: run};
}
/* reason: 'rotated' | 'stopped' -> complete; 'interrupted' -> interrupted. */
function closeRun(run, reason, nowMs) {
  run.state = reason === 'interrupted' ? 'interrupted' : 'complete';
  run.ended_ms = nowMs;
  return run;
}
function shouldRotate(run, nowMs) {
  return !!run && run.state === 'recording' && nowMs - run.started_ms >= ROTATE_MS;
}
/* The run looks paused by the browser (screen locked, app switched): nothing arrived for STALL_MS. */
function runStalled(lastChunkMs, nowMs) {
  return lastChunkMs > 0 && nowMs - lastChunkMs > STALL_MS;
}
function elapsedMs(runs, nowMs) {
  var total = 0;
  for (var i = 0; i < runs.length; i++) {
    var end = runs[i].ended_ms != null ? runs[i].ended_ms : nowMs;
    total += Math.max(0, end - runs[i].started_ms);
  }
  return total;
}
function contiguousCount(seqs) {
  var have = {}, i;
  for (i = 0; i < seqs.length; i++) have[seqs[i]] = true;
  var n = 0;
  while (have[n]) n++;
  return n;
}
/* The finish manifest, from the chunks whose IndexedDB write committed. A run still marked "recording" (the tab was
   killed) goes up as "lost-tail"; a run with nothing saved is left out. */
function buildManifest(sess, chunkMetas) {
  var byRun = {};
  chunkMetas.forEach(function (c) {
    if (c.committed === false) return;
    (byRun[c.run] = byRun[c.run] || []).push(c.seq);
  });
  var runs = [];
  (sess.runs || []).forEach(function (r) {
    var count = contiguousCount(byRun[r.index] || []);
    if (!count) return;
    runs.push({index: r.index, mime: r.mime, state: r.state === 'recording' ? 'lost-tail' : r.state,
               chunk_count: count, started_ms: r.started_ms});
  });
  return {runs: runs, name: sess.name, note_type: sess.note_type};
}
/* On open: a session still "recording" belongs to a page that died. Its current run may not decode. */
function recoverSession(sess) {
  if (sess.state !== 'recording') return sess;
  sess.state = 'stopped';
  sess.recovered = true;
  (sess.runs || []).forEach(function (r) { if (r.state === 'recording') r.state = 'lost-tail'; });
  return sess;
}

/* ---- the "saved" counters: a chunk counts only once its IndexedDB transaction committed ---- */

function countChunks(metas) {
  var saved = 0, uploaded = 0;
  metas.forEach(function (c) {
    if (c.committed === false) return;
    saved++;
    if (c.uploaded) uploaded++;
  });
  return {saved: saved, uploaded: uploaded};
}
function statusLine(counts, recording) {
  if (!counts.saved) return recording ? 'Starting. Nothing saved yet.' : 'Nothing saved yet.';
  var up = counts.uploaded >= counts.saved ? 'all ' + counts.saved + ' ' + (counts.saved === 1 ? 'piece' : 'pieces') + ' uploaded'
    : counts.uploaded + ' of ' + counts.saved + ' ' + (counts.saved === 1 ? 'piece' : 'pieces') + ' uploaded';
  return 'Saved on this phone · ' + up;
}
/* Serial write queue. add(job) runs jobs one after another; job() returns the record to commit, commit(record)
   resolves only when the transaction completed, and only then onSaved fires. After a failure nothing more is written. */
function createWriteQueue(o) {
  var chain = Promise.resolve(), failed = false, lastErr = null, q = {};
  q.add = function (job) {
    chain = chain.then(function () {
      if (failed) { if (o.onDrop) { try { o.onDrop(lastErr); } catch (e) { /* surfacing must not break the chain */ } } return null; }
      return Promise.resolve().then(job).then(function (rec) {
        if (!rec) return null;
        return Promise.resolve(o.commit(rec)).then(function () { o.onSaved(rec); }, function (err) { failed = true; lastErr = err; o.onFail(err, rec); });
      }, function (err) { failed = true; lastErr = err; o.onFail(err, null); });
    });
    return chain;
  };
  q.idle = function () { return chain; };
  q.hasFailed = function () { return failed; };
  return q;
}
/* Only "out of space" means full: QuotaExceededError and Safari's older names/codes. Any other storage error is not "full". */
function isQuotaError(err) {
  if (!err) return false;
  var n = String(err.name || '');
  return n === 'QuotaExceededError' || n === 'NS_ERROR_DOM_QUOTA_REACHED' || n === 'QUOTA_EXCEEDED_ERR' ||
    err.code === 22 || err.code === 1014 || /quota/i.test(String(err.message || ''));
}
/* {full, name}: how a storage error is told to the person. */
function storageProblem(err) {
  return {full: isQuotaError(err), name: String((err && err.name) || (err && err.message) || 'error')};
}
function runCapReached(runs) { return (runs || []).length >= MAX_RUNS; }
/* A finish/status answer (or a 409 body) that says the meeting is finalized. */
function saysFinalized(body) {
  if (!body) return false;
  return body.state === 'finalized' || /finalized/i.test(String(body.detail || ''));
}
/* What a DELETE answer means for the local copy. del: true only when the server has no copy left (2xx) or never had it (404). */
function deleteOutcome(res) {
  if (!res) return {del: false, kind: 'network'};
  if (res.type === 'opaqueredirect' || res.redirected || res.status === 401) return {del: false, kind: 'auth'};
  if (res.ok || res.status === 404) return {del: true, kind: 'ok'};
  if (res.status === 403) return {del: false, kind: 'forbidden'};
  if (res.status === 409) return {del: false, kind: 'busy'};
  return {del: false, kind: 'server'};
}

/* ---- the level meter ---- */
function levelFromSamples(samples) {
  if (!samples || !samples.length) return 0;
  var sum = 0, i, v;
  for (i = 0; i < samples.length; i++) { v = samples[i]; sum += v * v; }
  var rms = Math.sqrt(sum / samples.length);
  return Math.max(0, Math.min(1, Math.sqrt(rms) * 1.6));
}

/* ---- upload plan, backoff, response classes ---- */

function rangesHas(ranges, seq) {
  if (!ranges) return false;
  for (var i = 0; i < ranges.length; i++) if (seq >= ranges[i][0] && seq <= ranges[i][1]) return true;
  return false;
}
/* chunks = [{run, seq, committed, uploaded}], serverRuns = status.runs ({"0": [[from, to], ...]}, inclusive).
   todo: committed chunks the server lacks, oldest first. have: chunks the server already holds (skip them). */
function planUploads(chunks, serverRuns) {
  var todo = [], have = [];
  chunks.forEach(function (c) {
    if (c.committed === false) return;
    if (rangesHas((serverRuns || {})[String(c.run)], c.seq)) have.push(c); else todo.push(c);
  });
  todo.sort(function (a, b) { return a.run - b.run || a.seq - b.seq; });
  return {todo: todo, have: have};
}
function backoffDelay(attempt) {
  return Math.min(BACKOFF_MAX_MS, BACKOFF_MIN_MS * Math.pow(2, Math.max(0, attempt)));
}
/* null when the response is fine; else {retry, kind, status}. */
function classifyResponse(res) {
  if (res.type === 'opaqueredirect' || res.redirected || res.status === 401) return {retry: false, kind: 'auth', status: res.status};
  if (res.ok) return null;
  if (res.status === 403) return {retry: false, kind: 'forbidden', status: 403};
  if (res.status === 409) return {retry: false, kind: 'conflict', status: 409};
  if (res.status === 400 || res.status === 413) return {retry: false, kind: 'rejected', status: res.status};
  return {retry: true, kind: 'server', status: res.status};   // 404 (re-create), 429, 5xx, 507
}
function UploadError(info, message) {
  var e = new Error(message || 'upload failed');
  e.retry = !!info.retry; e.kind = info.kind; e.status = info.status || 0; e.body = info.body;
  return e;
}
function networkError() { return UploadError({retry: true, kind: 'network', status: 0}, 'offline'); }

function chunkUrl(sessionId, run, seq) {
  return API + '/' + encodeURIComponent(sessionId) + '/runs/' + run + '/chunks/' + seq;
}
function sessionUrl(sessionId, tail) { return API + '/' + encodeURIComponent(sessionId) + (tail || ''); }

/* deps = {fetch(url, init), listChunks(id), loadChunk(meta), markUploaded(meta), setUploaded(meta, bool), now(), shouldStop()}.
   Sends the new-session request, asks the server what it has, then PUTs what is missing, in order.
   Returns {state, uploaded}. Throws UploadError (e.retry says whether to try again). */
async function syncChunks(sess, deps, opts) {
  opts = opts || {};
  async function call(url, init) {
    var res;
    try { res = await deps.fetch(url, init); } catch (e) { throw networkError(); }
    var bad = classifyResponse(res);
    if (bad) { try { bad.body = await res.json(); } catch (e2) { bad.body = null; } throw UploadError(bad, bad.kind + ' ' + bad.status); }
    return res;
  }
  var created = await call(API, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({
    session_id: sess.id, name: sess.name, note_type: sess.note_type, device: sess.device, platform: sess.platform,
    started_wall: sess.started_wall, live: !!sess.live})});
  var info = {};
  try { info = await created.json(); } catch (e3) { info = {}; }
  if (info && info.state && info.state !== 'open') return {state: info.state, uploaded: 0, info: info};
  var status = await (await call(sessionUrl(sess.id), {method: 'GET'})).json();
  if (status.state && status.state !== 'open') return {state: status.state, uploaded: 0, info: status};
  var chunks = await deps.listChunks(sess.id);
  var plan = planUploads(chunks, opts.trustServer === false ? {} : status.runs);
  var i;
  for (i = 0; i < plan.have.length; i++) if (!plan.have[i].uploaded) await deps.setUploaded(plan.have[i], true);
  // chunks flagged uploaded but missing on the server (it was cleaned up): send them again
  for (i = 0; i < plan.todo.length; i++) if (plan.todo[i].uploaded) await deps.setUploaded(plan.todo[i], false);
  var sent = 0;
  for (i = 0; i < plan.todo.length; i++) {
    if (deps.shouldStop && deps.shouldStop()) break;
    var meta = plan.todo[i];
    var data = await deps.loadChunk(meta);
    if (!data) continue;
    await call(chunkUrl(sess.id, meta.run, meta.seq), {method: 'PUT', headers: {'Content-Type': meta.type || 'application/octet-stream'}, body: data});
    await deps.setUploaded(meta, true);
    sent++;
  }
  return {state: 'open', uploaded: sent, remaining: plan.todo.length - sent, info: status};
}

/* POST finish. 202 -> {state}. A 409 with "missing" asks for a full re-send (e.throws kind 'conflict'). */
async function finishSession(sess, manifest, deps) {
  var res;
  try {
    res = await deps.fetch(sessionUrl(sess.id, '/finish'), {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(manifest)});
  } catch (e) { throw networkError(); }
  var bad = classifyResponse(res);
  if (bad) {
    try { bad.body = await res.json(); } catch (e2) { bad.body = null; }
    throw UploadError(bad, bad.kind + ' ' + bad.status);
  }
  var body = {};
  try { body = await res.json(); } catch (e3) { body = {}; }
  return {state: body.state || 'preparing'};
}

/* Run task() until it reports done. A retryable failure waits backoffDelay(n) (2 s, 4 s ... 60 s) and tries again;
   a success resets the count. Other failures propagate. deps = {sleep(ms), shouldStop()}. */
async function withBackoff(task, deps, onWait) {
  var attempt = 0;
  for (;;) {
    if (deps.shouldStop && deps.shouldStop()) return null;
    try {
      var r = await task();
      attempt = 0;
      if (r && r.done) return r;
    } catch (e) {
      if (!e || !e.retry) throw e;
      var wait = backoffDelay(attempt++);
      if (onWait) onWait(wait, e);
      await deps.sleep(wait);
    }
  }
}

/* ---- warnings and wording ---- */

/* s = {recording, interrupted, ios, online, micBlocked, storageFull, signedOut, forbidden} -> [{kind, text}] */
function warningsFor(s) {
  var out = [];
  if (s.storageFull) out.push({kind: 'err', text: 'Storage full: recording stopped. What is saved can still be uploaded.'});
  else if (s.storageError) out.push({kind: 'err', text: 'Could not save on this phone: ' + s.storageError + '. Recording stopped. What is saved can still be uploaded.'});
  if (s.tooManyRuns) out.push({kind: 'err', text: 'This recording was interrupted too many times. Stop and start a new recording.'});
  if (s.micBlocked) out.push({kind: 'err', text: s.ios ? 'Microphone blocked: allow it in Settings > Safari > Microphone, then tap Record.'
    : 'Microphone blocked: allow it in your browser’s site settings, then tap Record.'});
  if (s.signedOut) out.push({kind: 'err', text: 'You are signed out, so nothing can upload. Sign in again; your recording stays on this phone.', link: '/login', linkText: 'Sign in'});
  if (s.forbidden) out.push({kind: 'err', text: 'The server refused this page. Open Meeting Notes from the address set in its Settings.'});
  if (s.interrupted) out.push({kind: 'warn', text: 'Recording was interrupted (screen locked, a call or another app). What was recorded before is saved.'});
  if (s.recording && s.ios) out.push({kind: 'warn', text: 'Keep this page open. Locking the screen pauses the recording.'});
  if (s.online === false) out.push({kind: 'warn', text: 'Not connected: recording on this phone, it uploads when you’re back online.'});
  return out;
}
function formatBytes(n) {
  if (!(n >= 0)) return '';
  if (n >= 1e9) return (n / 1e9).toFixed(1) + ' GB';
  if (n >= 1e6) return Math.round(n / 1e6) + ' MB';
  return Math.round(n / 1e3) + ' KB';
}
function formatClock(ms) {
  var s = Math.max(0, Math.floor(ms / 1000));
  return pad2(Math.floor(s / 3600)) + ':' + pad2(Math.floor(s / 60) % 60) + ':' + pad2(s % 60);
}

/* ---- live transcript: partial messages ---- */
function mergePartial(list, p) {
  if (!p || typeof p.text !== 'string' || !p.text.trim()) return list;
  var seg = {start: Number(p.start) || 0, end: Number(p.end) || 0, text: p.text.trim()};
  var out = list.filter(function (x) { return x.start !== seg.start; });
  out.push(seg);
  out.sort(function (a, b) { return a.start - b.start; });
  return out.slice(-LIVE_MAX_LINES);
}

/* ---- live transcript: wire bytes (meeting_notes/wire.py) ---- */
function helloMessage(sess) {
  return {type: 'hello', session_id: sess.id, name: sess.name || '', tracks: ['system'], sample_rate: 16000, protocol: 1,
          started_wall: sess.started_wall, device: sess.device || ''};
}
/* "<BQ" header (track index u8, absolute frame offset u64, little endian) then int16 LE PCM. */
function encodeAudioFrame(trackIndex, frameOffset, pcm) {
  var buf = new ArrayBuffer(9 + pcm.length * 2), dv = new DataView(buf);
  dv.setUint8(0, trackIndex);
  dv.setUint32(1, frameOffset % 4294967296, true);
  dv.setUint32(5, Math.floor(frameOffset / 4294967296), true);
  for (var i = 0; i < pcm.length; i++) dv.setInt16(9 + i * 2, pcm[i], true);
  return buf;
}

/* ---------------------------------------------------------------- 1b. resampler (also runs inside the AudioWorklet) */

/* Source text, because the same code runs on the main thread (tests) and inside the worklet. Resampling is an area
   average over each output sample's span of input (a box filter: it low-passes as it decimates), then 20 ms frames
   of int16. Handles any input rate, including 44.1 kHz. */
var FRAMER_SRC = [
  'function toInt16(v) { v = v < -1 ? -1 : (v > 1 ? 1 : v); return v < 0 ? Math.round(v * 32768) : Math.round(v * 32767); }',
  'class Framer {',
  '  constructor(inRate, outRate, frameSamples) {',
  '    this.step = inRate / outRate; this.need = this.step; this.acc = 0;',
  '    this.size = frameSamples; this.cur = new Int16Array(frameSamples); this.n = 0;',
  '  }',
  '  push(input) {',
  '    var frames = [], step = this.step, need = this.need, acc = this.acc, i, x, avail, take;',
  '    for (i = 0; i < input.length; i++) {',
  '      x = input[i]; avail = 1;',
  '      while (avail > 1e-12) {',
  '        take = avail < need ? avail : need; acc += x * take; need -= take; avail -= take;',
  '        if (need <= 1e-9) {',
  '          this.cur[this.n++] = toInt16(acc / step); acc = 0; need = step;',
  '          if (this.n === this.size) { frames.push(this.cur); this.cur = new Int16Array(this.size); this.n = 0; }',
  '        }',
  '      }',
  '    }',
  '    this.need = need; this.acc = acc;',
  '    return frames;',
  '  }',
  '}'
].join('\n');

var WORKLET_SRC = FRAMER_SRC + '\n' + [
  'class MnPcm16 extends AudioWorkletProcessor {',
  '  constructor() {',
  '    super(); this.framer = new Framer(sampleRate, 16000, ' + LIVE_FRAME_SAMPLES + '); this.on = true;',
  '    this.port.onmessage = function (e) { if (e.data === "stop") this.on = false; }.bind(this);',
  '  }',
  '  process(inputs) {',
  '    if (!this.on) return false;',
  '    var ch = inputs[0] && inputs[0][0];',
  '    if (!ch) return true;',
  '    var frames = this.framer.push(ch);',
  '    for (var i = 0; i < frames.length; i++) this.port.postMessage(frames[i].buffer, [frames[i].buffer]);',
  '    return true;',
  '  }',
  '}',
  'registerProcessor("mn-pcm16", MnPcm16);'
].join('\n');

/* ---------------------------------------------------------------- 2. IndexedDB */

var DB_NAME = 'mn-record', DB_VERSION = 1;

function openDb(idb) {
  return new Promise(function (resolve, reject) {
    var rq = idb.open(DB_NAME, DB_VERSION);
    rq.onupgradeneeded = function () {
      var db = rq.result;
      if (!db.objectStoreNames.contains('sessions')) db.createObjectStore('sessions', {keyPath: 'id'});
      if (!db.objectStoreNames.contains('chunks')) {
        var chunks = db.createObjectStore('chunks', {keyPath: ['session', 'run', 'seq']});
        chunks.createIndex('bySession', 'session');
      }
    };
    rq.onsuccess = function () { resolve(rq.result); };
    rq.onerror = function () { reject(rq.error || new Error('could not open storage')); };
    rq.onblocked = function () { reject(new Error('storage is busy; close other tabs of this page')); };
  });
}
function txDone(tx) {
  return new Promise(function (resolve, reject) {
    tx.oncomplete = function () { resolve(); };
    tx.onerror = function () { reject(tx.error || new Error('storage write failed')); };
    tx.onabort = function () { reject(tx.error || new Error('storage write aborted')); };
  });
}
function reqDone(rq) {
  return new Promise(function (resolve, reject) { rq.onsuccess = function () { resolve(rq.result); }; rq.onerror = function () { reject(rq.error); }; });
}
function clone(o) { return JSON.parse(JSON.stringify(o)); }

function dbPutSession(db, sess) {
  var tx = db.transaction(['sessions'], 'readwrite');
  tx.objectStore('sessions').put(clone(sess));
  return txDone(tx);
}
/* One transaction: the chunk and the session record (run chunk counts). Resolves after the commit. */
function dbCommitChunk(db, sess, rec) {
  var tx = db.transaction(['chunks', 'sessions'], 'readwrite');
  tx.objectStore('chunks').put(rec);
  tx.objectStore('sessions').put(clone(sess));
  return txDone(tx);
}
function dbAllSessions(db) { return reqDone(db.transaction(['sessions'], 'readonly').objectStore('sessions').getAll()); }
/* Chunk metadata without the audio. */
function dbChunkMetas(db, sessionId) {
  return new Promise(function (resolve, reject) {
    var out = [], rq = db.transaction(['chunks'], 'readonly').objectStore('chunks').index('bySession').openCursor(IDBKeyRange.only(sessionId));
    rq.onsuccess = function () {
      var cur = rq.result;
      if (!cur) { resolve(out); return; }
      var v = cur.value;
      out.push({session: v.session, run: v.run, seq: v.seq, size: v.size, type: v.type, committed: v.committed !== false, uploaded: !!v.uploaded});
      cur.continue();
    };
    rq.onerror = function () { reject(rq.error); };
  });
}
function dbGetChunk(db, meta) {
  return reqDone(db.transaction(['chunks'], 'readonly').objectStore('chunks').get([meta.session, meta.run, meta.seq]));
}
function dbSetUploaded(db, meta, flag) {
  return new Promise(function (resolve, reject) {
    var tx = db.transaction(['chunks'], 'readwrite'), store = tx.objectStore('chunks');
    var rq = store.get([meta.session, meta.run, meta.seq]);
    rq.onsuccess = function () { if (rq.result) { rq.result.uploaded = !!flag; store.put(rq.result); } };
    txDone(tx).then(resolve, reject);
  });
}
function dbDeleteSession(db, sessionId) {
  return new Promise(function (resolve, reject) {
    var tx = db.transaction(['chunks', 'sessions'], 'readwrite');
    tx.objectStore('sessions').delete(sessionId);
    var rq = tx.objectStore('chunks').index('bySession').openKeyCursor(IDBKeyRange.only(sessionId));
    rq.onsuccess = function () {
      var cur = rq.result;
      if (cur) { tx.objectStore('chunks').delete(cur.primaryKey); cur.continue(); }
    };
    txDone(tx).then(resolve, reject);
  });
}

/* ---------------------------------------------------------------- 3. the page */

function boot() {
  var $ = function (id) { return document.getElementById(id); };
  var env = {coarse: !!(window.matchMedia && matchMedia('(pointer: coarse)').matches), width: window.innerWidth,
             ua: navigator.userAgent, touchPoints: navigator.maxTouchPoints || 0};
  var gateOk = isMobileClient(env);
  $('mr-desktop').hidden = gateOk;
  $('mr-recorder').hidden = !gateOk;
  if (!gateOk) return;

  var platform = platformOf(env) || 'Android';
  var ios = platform === 'iOS';
  function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private mode: fine */ } }

  // ---- state
  var db = null;
  var sessions = {};            // id -> session record (the pending ones, plus the active one)
  var metas = {};               // id -> [chunk meta] (kept in memory; IndexedDB is the truth)
  var active = null;            // the session being recorded {sess, stream, rr, ...}
  var flags = {online: navigator.onLine !== false, micBlocked: false, storageFull: false, signedOut: false, forbidden: false, interrupted: false};
  var types = [], defaultType = '';
  var uploadWake = null, loopRunning = false, loopAttempt = 0, updateWaiting = null;
  var live = {on: false, ws: null, attempt: 0, timer: 0, node: null, offset: 0, text: [], paused: false, gen: 0};

  // ---- small UI helpers
  function setText(id, text) { $(id).textContent = text; }
  function recording() { return !!(active && active.running); }
  function counts(id) { return countChunks(metas[id] || []); }

  function renderWarnings() {
    var box = $('mr-warnings');
    box.textContent = '';
    warningsFor({recording: recording(), interrupted: flags.interrupted && !!active, ios: ios, online: flags.online, micBlocked: flags.micBlocked,
                 storageFull: flags.storageFull, storageError: flags.storageError, tooManyRuns: flags.tooManyRuns, signedOut: flags.signedOut, forbidden: flags.forbidden}).forEach(function (w) {
      var d = document.createElement('div');
      d.className = 'banner ' + (w.kind === 'err' ? 'err' : 'warn');
      d.setAttribute('role', w.kind === 'err' ? 'alert' : 'status');
      var span = document.createElement('span');
      span.textContent = w.text + ' ';
      if (w.link) { var a = document.createElement('a'); a.href = w.link; a.textContent = w.linkText; span.appendChild(a); }
      d.appendChild(span);
      box.appendChild(d);
    });
  }

  function renderStatus() {
    var id = active && active.sess.id;
    setText('mr-status', id ? statusLine(counts(id), recording()) : '');
    $('mr-status').hidden = !id;
  }
  function renderClock() {
    var runs = active ? active.sess.runs : [];
    setText('mr-clock', formatClock(elapsedMs(runs, Date.now())));
  }
  function renderControls() {
    var rec = recording(), has = !!active, resumeNeeded = has && !rec && !!active.needsResume && !active.capped;
    $('mr-record').hidden = has;
    $('mr-stop').hidden = !has;
    $('mr-resume').hidden = !resumeNeeded;
    $('mr-stage').classList.toggle('is-recording', rec);
    $('mr-name').disabled = false;
    $('mr-type').disabled = has;
    $('mr-clock').hidden = !has;
    $('mr-meter').hidden = !has;
    $('mr-device-row').hidden = has;
    $('mr-storage').hidden = has;
    renderUpdateBanner();
    renderTip();
    renderWarnings();
    renderStatus();
  }
  function renderUpdateBanner() { $('mr-update').hidden = !(updateWaiting && !active); }

  function renderTip() {
    var standalone = (navigator.standalone === true) || (window.matchMedia && matchMedia('(display-mode: standalone)').matches);
    var show = !standalone && !active && lsGet('mn-record.tip') !== '1';
    $('mr-tip').hidden = !show;
    if (show) setText('mr-tip-text', ios
      ? 'To open this quickly, tap the Share button in Safari, then Add to Home Screen.'
      : 'To open this quickly, tap the browser menu (three dots), then Install app or Add to Home screen.');
  }

  // ---- list of recordings on this phone
  function sessionLabel(sess) {
    var c = counts(sess.id), line;
    if (sess.state === 'failed') line = 'Failed on the server' + (sess.error ? ': ' + sess.error : '') + '.';
    else if (sess.state === 'finishing') line = 'Uploaded. The server is preparing it.';
    else if (sess.uploadNote) line = sess.uploadNote;
    else if (c.saved && c.uploaded >= c.saved) line = 'All ' + c.saved + ' pieces uploaded. Tap Upload to finish.';
    else line = c.saved + (c.saved === 1 ? ' piece saved' : ' pieces saved') + ', ' + c.uploaded + ' uploaded.';
    if (sess.recovered) line = 'Stopped unexpectedly. ' + line;
    return line;
  }
  function renderList() {
    var list = $('mr-list');
    list.textContent = '';
    var ids = Object.keys(sessions).filter(function (id) { return !(active && active.sess.id === id); })
      .sort(function (a, b) { return (sessions[a].started_wall || 0) - (sessions[b].started_wall || 0); });
    ids.forEach(function (id) {
      var sess = sessions[id];
      var li = document.createElement('li'); li.className = 'mr-item';
      var t = document.createElement('div'); t.className = 'mr-item-title'; t.textContent = sess.name || id;
      var m = document.createElement('div'); m.className = 'mr-item-meta'; m.textContent = sessionLabel(sess);
      var row = document.createElement('div'); row.className = 'mr-item-actions';
      var up = document.createElement('button'); up.type = 'button'; up.className = 'btn primary mr-btn';
      up.textContent = sess.state === 'failed' ? 'Retry' : 'Upload';
      up.addEventListener('click', function () { userUpload(id); });
      var del = document.createElement('button'); del.type = 'button'; del.className = 'btn danger mr-btn';
      del.textContent = 'Delete from this phone';
      del.addEventListener('click', function () { userDelete(id); });
      if (sess.state !== 'finishing') row.appendChild(up);
      row.appendChild(del);
      li.appendChild(t); li.appendChild(m); li.appendChild(row); list.appendChild(li);
    });
    $('mr-pending').hidden = !ids.length;  // finished meetings leave this list: the phone no longer has them
  }

  // ---- storage and note types
  async function loadStorageNote() {
    try {
      if (navigator.storage && navigator.storage.estimate) {
        var e = await navigator.storage.estimate();
        if (e && e.quota >= 0 && e.usage >= 0) {
          var free = e.quota - e.usage;
          setText('mr-storage', 'Free space for recordings on this phone: about ' + formatBytes(free) + '.');
          $('mr-storage').classList.toggle('is-low', free < 100e6);
          if (free < 100e6) setText('mr-storage', 'Low space on this phone (about ' + formatBytes(free) + ' free). Free some up before a long recording.');
          return;
        }
      }
    } catch (e2) { /* unknown: say nothing */ }
    $('mr-storage').textContent = '';
  }
  function renderTypes() {
    var sel = $('mr-type');
    sel.textContent = '';
    types.forEach(function (t) {
      var o = document.createElement('option'); o.value = t.id; o.textContent = t.name; sel.appendChild(o);
    });
    var saved = lsGet('mn-record.type');
    sel.value = types.some(function (t) { return t.id === saved; }) ? saved : defaultType;
    $('mr-type-field').hidden = types.length < 2;
  }
  async function loadTypes() {
    try {
      var cached = JSON.parse(lsGet('mn-record.types') || 'null');
      if (cached && cached.items) { types = cached.items; defaultType = cached.dflt || ''; renderTypes(); }
    } catch (e) { /* none cached */ }
    try {
      var r = await fetch('/v1/note-templates', {credentials: 'same-origin'});
      if (!r.ok) throw new Error('note types');
      var data = await r.json();
      types = (Array.isArray(data.items) ? data.items : []).filter(function (t) { return t && typeof t.id === 'string' && t.id; })
        .map(function (t) { return {id: t.id, name: String(t.name || t.id)}; });
      defaultType = typeof data.default_template_id === 'string' ? data.default_template_id : '';
      lsSet('mn-record.types', JSON.stringify({items: types, dflt: defaultType}));
      renderTypes();
    } catch (e2) { /* offline: the cached list (if any) stays */ }
  }

  // ---- IndexedDB-backed writes
  // one queue per recording: a storage error in one recording must never silently swallow the writes of the next
  function makeWriteQueue() {
    return createWriteQueue({
      commit: function (rec) {
        var sess = sessions[rec.session];
        return dbCommitChunk(db, sess, rec);
      },
      onSaved: function (rec) {
        (metas[rec.session] = metas[rec.session] || []).push({session: rec.session, run: rec.run, seq: rec.seq, size: rec.size, type: rec.type, committed: true, uploaded: false});
        renderStatus(); kickUploads();
      },
      onFail: function (err, rec) { storageFailed(err, rec); },
      onDrop: function (err) { storageFailed(err, null); }      // a write after the failure is dropped: say so again, never silently
    });
  }
  var writeQueue = makeWriteQueue();
  function noteStorageError(err) {
    var p = storageProblem(err);
    flags.storageFull = p.full;
    flags.storageError = p.full ? '' : p.name;
    flags.storageFullMessage = String((err && err.message) || err);
  }
  function storageFailed(err, rec) {
    // the tentative run count never committed: take it back
    if (rec) {
      var sess = sessions[rec.session], run = sess && sess.runs.filter(function (r) { return r.index === rec.run; })[0];
      if (run && run.chunk_count > rec.seq) run.chunk_count = rec.seq;
    }
    noteStorageError(err);
    if (active) stopRecording('storage');
    renderControls();
  }

  // ---- recording
  var constraints = {audio: {echoCancellation: false, noiseSuppression: false, autoGainControl: true, channelCount: 1}};

  function trackLive(stream) {
    var t = stream && stream.getAudioTracks()[0];
    return !!t && t.readyState === 'live';
  }
  function bindStream(a, stream) {
    a.stream = stream;
    var t = stream.getAudioTracks()[0];
    if (!t) return;
    t.onended = function () { if (active === a) interrupt('the microphone stopped'); };
    t.onmute = function () { if (active === a) interrupt('the microphone was taken'); };
    t.onunmute = function () { if (active === a && a.interrupted) tryResume(false); };
    connectMeter(a, stream);
  }
  function connectMeter(a, stream) {
    try {
      if (!a.ctx) { var AC = window.AudioContext || window.webkitAudioContext; if (!AC) return; a.ctx = new AC(); a.analyser = a.ctx.createAnalyser(); a.analyser.fftSize = 1024; a.buf = new Float32Array(a.analyser.fftSize); }
      if (a.src) { try { a.src.disconnect(); } catch (e) { /* ok */ } }
      a.src = a.ctx.createMediaStreamSource(stream);
      a.src.connect(a.analyser);
      if (a.ctx.state === 'suspended') a.ctx.resume();
      if (live.on) attachLiveNode(a);
    } catch (e2) { /* the meter is optional */ }
  }
  function drawMeter() {
    var a = active, bar = $('mr-meter-bar');
    if (!a || !a.analyser) { bar.style.transform = 'scaleX(0)'; return; }
    a.analyser.getFloatTimeDomainData(a.buf);
    var lvl = recording() ? levelFromSamples(a.buf) : 0;
    bar.style.transform = 'scaleX(' + lvl.toFixed(3) + ')';
  }

  function startRun(a) {
    var sess = a.sess;
    var mime = a.mime || '';
    var opts = {audioBitsPerSecond: 32000};
    if (mime) opts.mimeType = mime;
    var recorder;
    try { recorder = new MediaRecorder(a.stream, opts); } catch (e) { recorder = new MediaRecorder(a.stream); }
    var added = appendRun(sess.runs, recorder.mimeType || mime || 'audio/webm', Date.now());
    sess.runs = added.runs;
    var rr = {recorder: recorder, run: added.run, nextSeq: 0, expect: null, timer: 0};
    rr.closed = new Promise(function (res) { rr.resolveClosed = res; });
    a.rr = rr; a.running = true; a.needsResume = false; a.lastChunk = Date.now();
    recorder.ondataavailable = function (ev) { onData(a, rr, ev); };
    recorder.onerror = function () { if (a.rr === rr && !rr.expect) interrupt('the recorder failed'); };
    recorder.onstop = function () { onRunStopped(a, rr); };
    recorder.start(TIMESLICE_MS);
    rr.timer = setTimeout(function () { rotate(a, rr); }, ROTATE_MS);
    sessions[sess.id] = sess;
    dbPutSession(db, sess).catch(function (err) { storageFailed(err, null); });
    renderControls();
  }
  function onData(a, rr, ev) {
    var blob = ev.data;
    if (!blob || !blob.size) return;
    var now = Date.now(), stalled = runStalled(a.lastChunk, now);
    a.lastChunk = now;
    var seq = rr.nextSeq++, run = rr.run, sess = a.sess, mime = rr.recorder.mimeType || run.mime;
    // tentative, set at once (not when the write runs) so a run is never seen as empty while its last blob is still queued;
    // storageFailed() takes it back if the write never commits
    if (run.chunk_count < seq + 1) run.chunk_count = seq + 1;
    writeQueue.add(async function () {
      var data = await blob.arrayBuffer();
      return {session: sess.id, run: run.index, seq: seq, data: data, type: mime, size: data.byteLength, committed: true, uploaded: false, at: now};
    });
    if (rr.expect) return;
    if (stalled) interrupt('the browser paused the recording');
    else if (shouldRotate(run, now)) rotate(a, rr);
  }
  function rotate(a, rr) {
    if (a.rr !== rr || rr.expect || rr.recorder.state === 'inactive') return;
    if (runCapReached(a.sess.runs)) return;      // no new run would be accepted: keep this one going
    rr.expect = 'rotate';
    try { rr.recorder.stop(); } catch (e) { onRunStopped(a, rr); }
  }
  async function onRunStopped(a, rr) {
    if (rr.done) return;
    rr.done = true;
    clearTimeout(rr.timer);
    var reason = rr.expect === 'rotate' ? 'rotated' : (rr.expect === 'stop' || rr.expect === 'storage' ? 'stopped' : 'interrupted');
    closeRun(rr.run, reason, Date.now());
    a.running = false;
    if (rr.expect === 'rotate' && !a.finished) {
      // planned rotation: the stream is fine, so the next recorder starts now (no gap); this run's last blob
      // (its dataavailable came before this stop event) is written in the background, in order, by the same queue
      try { startRun(a); } catch (e) { interrupt('the recorder could not restart'); }
    }
    await writeQueue.idle();
    dbPutSession(db, a.sess).catch(function () { /* the next chunk write retries it */ });
    rr.resolveClosed();
    if (a.finished) return;
    if (rr.expect === 'rotate' || rr.expect === 'stop' || rr.expect === 'storage') return;
    flags.interrupted = true;
    a.interrupted = true;
    if (runCapReached(a.sess.runs)) { capRuns(a); return; }
    renderControls();
    if (!document.hidden) tryResume(false);
  }
  /* Too many runs for the server: no more automatic restarts. The person can still Stop; what is saved uploads normally. */
  function capRuns(a) {
    a.capped = true; a.needsResume = false;
    flags.tooManyRuns = true;
    renderControls();
  }
  function interrupt(why) {
    var a = active;
    if (!a || a.finished) return;
    a.interruptWhy = why;
    flags.interrupted = true; a.interrupted = true;
    var rr = a.rr;
    if (rr && !rr.done && rr.recorder.state !== 'inactive') {
      if (!rr.expect) rr.expect = 'interrupt';
      try { rr.recorder.stop(); } catch (e) { onRunStopped(a, rr); }
    } else if (runCapReached(a.sess.runs)) {
      a.running = false; capRuns(a);
    } else {
      a.running = false; a.needsResume = true; renderControls();
    }
  }
  /* Start recording again after an interruption. Without a tap the browser may refuse the microphone: then the
     Resume recording button asks for one. */
  async function tryResume(fromTap) {
    var a = active;
    if (!a || a.finished || a.running || a.resuming) return;
    if (a.capped || runCapReached(a.sess.runs)) { capRuns(a); return; }
    a.resuming = true;
    try {
      if (!trackLive(a.stream) || (a.stream.getAudioTracks()[0] || {}).muted) {
        if (a.stream) a.stream.getTracks().forEach(function (t) { t.onended = null; t.onmute = null; try { t.stop(); } catch (e) { /* ok */ } });
        var stream = await navigator.mediaDevices.getUserMedia(constraints);
        flags.micBlocked = false;
        bindStream(a, stream);
      }
      startRun(a);
      a.interrupted = false; flags.interrupted = false;
      acquireWake();
    } catch (e) {
      a.needsResume = true;
      flags.micBlocked = !!(e && (e.name === 'NotAllowedError' || e.name === 'SecurityError')) && fromTap;
    } finally { a.resuming = false; }
    renderControls();
  }

  async function startRecording() {
    if (active) return;
    if (!window.isSecureContext || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || typeof MediaRecorder === 'undefined') {
      notify('This browser cannot record here. Use Safari on iPhone or Chrome on Android, over https.', 'error');
      return;
    }
    flags.micBlocked = false; flags.storageFull = false; flags.storageError = ''; flags.tooManyRuns = false; flags.interrupted = false; flags.storageFullMessage = '';
    writeQueue = makeWriteQueue();     // fresh per recording: an earlier storage failure must not follow into this one
    var stream;
    try { stream = await navigator.mediaDevices.getUserMedia(constraints); }
    catch (e) { flags.micBlocked = true; renderControls(); return; }
    try { if (navigator.storage && navigator.storage.persist) await navigator.storage.persist(); } catch (e2) { /* asking only */ }
    var now = new Date();
    var device = lsGet('mn-record.device') || defaultDeviceLabel(env);
    var sess = {id: newSessionId(now, device, randomHex(4, window.crypto)), name: $('mr-name').value.trim() || defaultMeetingName(now),
                note_type: $('mr-type').value || defaultType || '', device: device, platform: platform, state: 'recording',
                live: $('mr-live').checked, started_wall: now.getTime() / 1000, runs: [], recovered: false};
    try { await dbPutSession(db, sess); }
    catch (err) {
      stream.getTracks().forEach(function (t) { t.stop(); });
      noteStorageError(err); renderControls(); return;
    }
    sessions[sess.id] = sess; metas[sess.id] = [];
    var a = active = {sess: sess, mime: pickMime(function (m) { return window.MediaRecorder && MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(m); }),
                      running: false, finished: false, interrupted: false, needsResume: false, lastChunk: 0};
    bindStream(a, stream);
    try { startRun(a); }
    catch (e3) {
      stream.getTracks().forEach(function (t) { t.stop(); });
      active = null; delete sessions[sess.id];
      notify('Could not start the recorder: ' + (e3 && e3.message || e3), 'error'); renderControls(); return;
    }
    acquireWake();
    if (sess.live) setLive(true);
    startTimers();
    kickUploads();
    renderList();
  }

  async function stopRecording(why) {
    var a = active;
    if (!a || a.finished) return;
    a.finished = true;
    var rr = a.rr;
    if (rr && !rr.done && rr.recorder.state !== 'inactive') {
      rr.expect = why === 'storage' ? 'storage' : 'stop';
      try { rr.recorder.stop(); } catch (e) { onRunStopped(a, rr); }
      await rr.closed;
    } else if (rr && !rr.done) {
      // a stop (rotation) is already under way: let its last blob arrive before the microphone is released
      await Promise.race([rr.closed, new Promise(function (r) { setTimeout(r, 3000); })]);
    }
    await writeQueue.idle();
    a.running = false;
    if (a.stream) a.stream.getTracks().forEach(function (t) { t.onended = null; t.onmute = null; try { t.stop(); } catch (e2) { /* ok */ } });
    if (a.src) { try { a.src.disconnect(); } catch (e3) { /* ok */ } }
    if (a.ctx) { try { a.ctx.close(); } catch (e4) { /* ok */ } }
    stopLive(true);
    releaseWake();
    stopTimers();
    var sess = a.sess;
    sess.name = $('mr-name').value.trim() || sess.name;
    sess.state = 'stopped';
    active = null;
    try { await dbPutSession(db, sess); } catch (e5) { /* state is also recoverable from the chunks */ }
    flags.interrupted = false;
    $('mr-name').value = '';
    drawMeter();
    renderControls(); renderList(); kickUploads();
  }

  // ---- wake lock
  var wake = null;
  async function acquireWake() {
    try {
      if (!navigator.wakeLock || (wake && !wake.released)) return;
      wake = await navigator.wakeLock.request('screen');
      wake.addEventListener('release', function () { wake = null; });
    } catch (e) { wake = null; }
  }
  function releaseWake() { try { if (wake) wake.release(); } catch (e) { /* ok */ } wake = null; }

  // ---- timers: clock, meter
  var timers = [];
  function startTimers() {
    stopTimers();
    timers.push(setInterval(renderClock, 500));
    timers.push(setInterval(drawMeter, 100));
    renderClock();
  }
  function stopTimers() { timers.forEach(clearInterval); timers = []; }

  document.addEventListener('visibilitychange', function () {
    var a = active;
    if (document.hidden) {
      // ask for what the recorder holds now, before the page may be frozen
      if (a && a.rr && a.rr.recorder.state === 'recording') { try { a.rr.recorder.requestData(); } catch (e) { /* ok */ } }
      return;
    }
    if (a && !a.finished) {
      acquireWake();
      var t = a.stream && a.stream.getAudioTracks()[0];
      var bad = !a.running || !t || t.readyState !== 'live' || t.muted || runStalled(a.lastChunk, Date.now());
      if (bad && a.running) interrupt('the page was in the background');
      else if (!a.running) tryResume(false);
    }
    if (live.on) liveEnsure();
    kickUploads();
    renderControls();
  });
  window.addEventListener('online', function () { flags.online = true; renderControls(); kickUploads(); if (live.on) liveEnsure(); });
  window.addEventListener('offline', function () { flags.online = false; renderControls(); });
  window.addEventListener('beforeunload', function (ev) { if (recording()) { ev.preventDefault(); ev.returnValue = ''; } });

  // ---- uploading
  function kickUploads() { if (uploadWake) { uploadWake(); } if (!loopRunning) uploadLoop(); }
  function waitKick(ms) {
    return new Promise(function (resolve) {
      var t = setTimeout(done, ms);
      function done() { clearTimeout(t); uploadWake = null; resolve(); }
      uploadWake = done;
    });
  }
  var uploadDeps = {
    fetch: function (url, init) { return fetch(url, Object.assign({credentials: 'same-origin', redirect: 'manual'}, init)); },
    listChunks: function (id) { return Promise.resolve((metas[id] || []).slice()); },
    loadChunk: async function (meta) {
      var rec = await dbGetChunk(db, meta);
      return rec ? new Blob([rec.data], {type: rec.type}) : null;
    },
    setUploaded: async function (meta, flag) { meta.uploaded = flag; await dbSetUploaded(db, meta, flag); renderStatus(); },
    shouldStop: function () { return false; }
  };
  function needsWork(sess) {
    if (sess.paused || sess.dead) return false;
    var id = sess.id, pending = (metas[id] || []).some(function (c) { return c.committed && !c.uploaded; });
    if (active && active.sess.id === id) return pending || !sess.synced;
    if (sess.state === 'failed') return false;
    if (sess.state === 'finishing') return true;
    return pending || (sess.state === 'stopped' && !sess.holdFinish) || !sess.synced;
  }
  function noteUploadError(sess, e) {
    if (e && e.kind === 'auth') flags.signedOut = true;
    if (e && e.kind === 'forbidden') flags.forbidden = true;
    if (e && e.kind === 'network') flags.online = navigator.onLine !== false ? flags.online : false;
    sess.uploadNote = e && e.retry ? 'Waiting for a connection. Uploading again shortly.' : 'The server did not accept this upload (' + (e && e.status) + ').';
    renderWarnings();
  }
  async function syncOne(sess) {
    var firstSync = !sess.synced;
    var id = sess.id;
    // a session we have not checked on this visit: reconcile with what the server really has
    var out = await syncChunks(sess, uploadDeps, {});
    flags.signedOut = false; flags.forbidden = false; sess.uploadNote = '';
    sess.synced = true;
    if (out.state === 'finalized') { await completeSession(sess); return true; }
    if (out.state === 'failed') { sess.state = 'failed'; sess.error = String(out.info && out.info.error || 'the server could not prepare it'); await dbPutSession(db, sess); return false; }
    if (out.state === 'preparing') { if (sess.state !== 'finishing') { sess.state = 'finishing'; await dbPutSession(db, sess); } return false; }
    var stillPending = (metas[id] || []).some(function (c) { return c.committed && !c.uploaded; });
    var wantFinish = sess.state === 'stopped' && !(active && active.sess.id === id) && !sess.holdFinish;
    if (wantFinish && !stillPending) {
      var manifest = buildManifest(sess, metas[id] || []);
      if (!manifest.runs.length) { sess.uploadNote = 'Nothing was saved in this recording.'; sess.holdFinish = true; return false; }
      var fin;
      try {
        fin = await finishSession(sess, manifest, uploadDeps);
      } catch (e) {
        if (e && e.kind === 'conflict' && saysFinalized(e.body)) {
          fin = {state: 'finalized'};               // 409 because it is already finalized: that is the answer we wanted
        } else if (e && e.kind === 'conflict' && e.body && e.body.missing && !sess.resent) {
          sess.resent = true;                       // the server lacks pieces we think it has: send everything again
          (metas[id] || []).forEach(function (c) { c.uploaded = false; });
          await syncChunks(sess, uploadDeps, {trustServer: false});
          fin = await finishSession(sess, buildManifest(sess, metas[id] || []), uploadDeps);
        } else throw e;
      }
      if (fin && fin.state === 'finalized') { await completeSession(sess); return true; }
      sess.state = 'finishing';
      await dbPutSession(db, sess);
    }
    return out.uploaded > 0 || firstSync;
  }
  async function pollOne(sess) {
    var res;
    try { res = await uploadDeps.fetch(sessionUrl(sess.id), {method: 'GET'}); } catch (e) { throw networkError(); }
    var bad = classifyResponse(res);
    if (bad) throw UploadError(bad, bad.kind);
    var st = await res.json();
    if (st.state === 'finalized') { await completeSession(sess); return; }
    if (st.state === 'failed') { sess.state = 'failed'; sess.error = String(st.error || 'the server could not prepare it'); await dbPutSession(db, sess); }
  }
  async function completeSession(sess) {
    // the server has the finished meeting: only now is the local copy deleted
    try { await dbDeleteSession(db, sess.id); } catch (e) { /* retried on the next open */ }
    delete sessions[sess.id]; delete metas[sess.id];
    notify('Meeting uploaded: ' + (sess.name || sess.id), null, {label: 'Open', href: '/sessions/' + encodeURIComponent(sess.id)});
  }
  async function uploadLoop() {
    if (loopRunning) return;
    loopRunning = true;
    try {
      for (;;) {
        var work = Object.keys(sessions).map(function (id) { return sessions[id]; }).filter(needsWork)
          .sort(function (a, b) { return (a.started_wall || 0) - (b.started_wall || 0); });
        if (!work.length) break;
        var failed = false, busy = false;
        for (var i = 0; i < work.length; i++) {
          var sess = work[i];
          try {
            if (sess.state === 'finishing') await pollOne(sess); else if (await syncOne(sess)) busy = true;
          } catch (e) {
            if (e && e.kind === 'conflict' && saysFinalized(e.body)) { try { await completeSession(sess); } catch (e9) { /* retried */ } busy = true; renderList(); renderStatus(); continue; }
            noteUploadError(sess, e);
            if (e && e.retry) failed = true; else sess.paused = true;     // a refusal waits for the person (Upload / Retry)
          }
          renderList(); renderStatus();
        }
        if (failed) { await waitKick(backoffDelay(loopAttempt++)); continue; }
        loopAttempt = 0;
        if (!busy) await waitKick(work.some(function (s) { return s.state === 'finishing'; }) ? POLL_MS : TIMESLICE_MS);
      }
    } finally { loopRunning = false; renderList(); }
  }
  async function userUpload(id) {
    var sess = sessions[id];
    if (!sess) return;
    if (sess.state === 'failed') {
      try {
        var r = await uploadDeps.fetch(sessionUrl(id, '/retry'), {method: 'POST'});
        var bad = classifyResponse(r);
        if (bad) throw UploadError(bad, bad.kind);
        sess.state = 'finishing'; sess.error = '';
        await dbPutSession(db, sess);
      } catch (e) { notify('Could not retry. Check the connection and try again.', 'error'); return; }
    }
    sess.paused = false; sess.holdFinish = false; sess.dead = false; sess.resent = false;
    if (sess.state === 'recording') recoverSession(sess);
    await dbPutSession(db, sess).catch(function () {});
    flags.signedOut = false;
    renderList(); kickUploads();
  }
  async function userDelete(id) {
    var sess = sessions[id];
    if (!sess) return;
    var c = counts(id);
    var yes = await confirmDialog({title: 'Delete from this phone?', lead: 'The recording is removed from this phone and from the server. It cannot be recovered.',
      items: [{name: sess.name || id, meta: c.saved + (c.saved === 1 ? ' piece' : ' pieces')}], itemsLabel: 'Recording', confirmLabel: 'Delete'});
    if (!yes) return;
    sess.dead = true;
    // the local copy may be the only one left: it goes only when the server has none (2xx) or never had it (404)
    var res = null;
    try { res = await uploadDeps.fetch(sessionUrl(id), {method: 'DELETE'}); } catch (e) { res = null; }
    var outcome = deleteOutcome(res);
    if (!outcome.del) {
      sess.dead = false;
      if (outcome.kind === 'auth') { flags.signedOut = true; renderWarnings(); notify('Sign in again, then delete.', 'error'); }
      else if (outcome.kind === 'forbidden') { flags.forbidden = true; renderWarnings(); notify('The server refused to delete it, so it stays on this phone.', 'error'); }
      else if (outcome.kind === 'busy') notify('The server is still preparing this one. Try again in a minute.', 'error');
      else if (outcome.kind === 'network') notify('No connection to the server, so it was not deleted. Try again when you are online.', 'error');
      else notify('The server could not delete it (' + res.status + '), so it stays on this phone. Try again later.', 'error');
      return;
    }
    try { await dbDeleteSession(db, id); } catch (e2) { notify('Could not delete it from this phone: ' + (e2 && e2.message || e2), 'error'); sess.dead = false; return; }
    delete sessions[id]; delete metas[id];
    renderList();
  }

  // ---- live transcript (off by default; never affects the recording)
  function setLiveNote(text) { setText('mr-live-note', text); }
  /* Full rebuild: only to clear (or reset) the box. */
  function renderLiveText() {
    var box = $('mr-live-text');
    box.textContent = '';
    live.nodes = {};
    live.text.forEach(function (seg) { var p = document.createElement('p'); p.textContent = seg.text; p.mnStart = seg.start; live.nodes[seg.start] = p; box.appendChild(p); });
    box.scrollTop = box.scrollHeight;
  }
  /* One partial arrived: live.text is already merged (last LIVE_MAX_LINES lines). Touch only the line that changed. */
  function applyLivePartial(m) {
    var box = $('mr-live-text'), start = Number(m.start) || 0, nodes = live.nodes = live.nodes || {};
    var idx = -1, i;
    for (i = live.text.length - 1; i >= 0; i--) if (live.text[i].start === start) { idx = i; break; }
    if (idx < 0) return;                                       // blank, or already trimmed away
    var atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    var p = nodes[start];
    if (!p) {
      p = document.createElement('p'); p.mnStart = start; nodes[start] = p;
      var next = live.text[idx + 1], ref = next && nodes[next.start];
      box.insertBefore(p, ref || null);
    }
    if (p.textContent !== live.text[idx].text) p.textContent = live.text[idx].text;
    while (box.firstChild && box.childNodes.length > live.text.length) { delete nodes[box.firstChild.mnStart]; box.removeChild(box.firstChild); }
    if (atBottom) box.scrollTop = box.scrollHeight;
  }
  function setLive(on) {
    live.on = on;
    $('mr-live-box').hidden = !on;
    if (!on) { stopLive(false); return; }
    live.gen++;
    live.attempt = 0;
    setLiveNote('Connecting...');
    if (active) attachLiveNode(active);
    liveEnsure();
  }
  function stopLive(final) {
    live.gen++;
    clearTimeout(live.timer);
    if (live.ws) { try { live.ws.onclose = null; live.ws.close(); } catch (e) { /* ok */ } live.ws = null; }
    if (live.node) { try { live.node.port.postMessage('stop'); live.node.disconnect(); } catch (e2) { /* ok */ } live.node = null; }
    live.paused = false;
    if (final) { live.on = false; live.text = []; live.offset = 0; $('mr-live-box').hidden = true; renderLiveText(); }
  }
  async function attachLiveNode(a) {
    if (!a.ctx || !a.src || live.node || !a.ctx.audioWorklet) return;
    try {
      var url = URL.createObjectURL(new Blob([WORKLET_SRC], {type: 'text/javascript'}));
      await a.ctx.audioWorklet.addModule(url);
      URL.revokeObjectURL(url);
      if (!live.on || active !== a || live.node) return;
      var node = new AudioWorkletNode(a.ctx, 'mn-pcm16', {numberOfInputs: 1, numberOfOutputs: 0, channelCount: 1, channelCountMode: 'explicit'});
      node.port.onmessage = function (e) {
        var pcm = new Int16Array(e.data), off = live.offset;
        live.offset += pcm.length;
        var ws = live.ws;
        if (ws && ws.readyState === 1 && ws.bufferedAmount < 1e6) { try { ws.send(encodeAudioFrame(SYSTEM_TRACK_INDEX, off, pcm)); } catch (err) { /* dropped: live is disposable */ } }
      };
      a.src.connect(node);
      live.node = node;
    } catch (e) { setLiveNote('Live transcript is not available in this browser. Recording is not affected.'); }
  }
  function liveEnsure() {
    if (!live.on || !active || (live.ws && live.ws.readyState <= 1)) return;
    var gen = live.gen, sess = active.sess;
    clearTimeout(live.timer);
    (async function () {
      try {
        var created = await uploadDeps.fetch(API, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({
          session_id: sess.id, name: sess.name, note_type: sess.note_type, device: sess.device, platform: sess.platform, started_wall: sess.started_wall, live: true})});
        if (classifyResponse(created)) throw new Error('session');
        var tr = await uploadDeps.fetch(sessionUrl(sess.id, '/stream-ticket'), {method: 'POST'});
        if (classifyResponse(tr)) throw new Error('ticket');
        var t = await tr.json();
        if (gen !== live.gen) return;
        var ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/v1/stream', ['mn-ticket.' + t.ticket]);
        ws.binaryType = 'arraybuffer';
        live.ws = ws;
        ws.onopen = function () { ws.send(JSON.stringify(helloMessage(sess))); live.attempt = 0; live.paused = false; setLiveNote('Live'); };
        ws.onmessage = function (ev) {
          if (typeof ev.data !== 'string') return;
          var m; try { m = JSON.parse(ev.data); } catch (e) { return; }
          if (m && m.type === 'partial') { live.text = mergePartial(live.text, m); applyLivePartial(m); }
          else if (m && m.type === 'error') setLiveNote('Live transcript: ' + String(m.detail || 'error'));
        };
        ws.onclose = function (ev) { if (live.ws === ws) { live.ws = null; liveClosed(gen, ev && ev.code); } };
        ws.onerror = function () { /* onclose follows */ };
      } catch (e2) { liveDown(gen); }
    })();
  }
  /* Close codes of the ticketed /v1/stream: 4401 ticket or origin refused (new ticket, backoff); 4403 session or track
   mismatch; 4409 another connection holds the session; 4410 replaced by a newer connection; 4411 upload accepted
   (stop quietly); 4400 bad frames or rate limit. Everything else (a drop) reconnects with a new ticket. */
function liveStopCode(code) { return code === 4400 || code === 4403 || code === 4409 || code === 4410 || code === 4411; }
function liveClosed(gen, code) {
    if (!live.on || gen !== live.gen) return;
    if (liveStopCode(code)) {
      live.on = false; stopLive(false); $('mr-live').checked = false;
      setLiveNote(code === 4411 ? '' : code === 4410 || code === 4409 ? 'Live transcript stopped: another connection took over this meeting.'
        : 'Live transcript unavailable. Recording is not affected.');
      return;
    }
    liveDown(gen);
  }
function liveDown(gen) {
    if (!live.on || gen !== live.gen) return;
    live.paused = true;
    setLiveNote('Live transcript paused. Recording continues and will be transcribed in full.');
    live.timer = setTimeout(liveEnsure, backoffDelay(live.attempt++));
  }

  // ---- wiring
  $('mr-record').addEventListener('click', startRecording);
  $('mr-stop').addEventListener('click', function () { stopRecording('user'); });
  $('mr-resume').addEventListener('click', function () { tryResume(true); });
  $('mr-live').addEventListener('change', function () {
    var on = $('mr-live').checked;
    if (active) { active.sess.live = on; dbPutSession(db, active.sess).catch(function () {}); }
    setLive(on);
  });
  $('mr-type').addEventListener('change', function () { lsSet('mn-record.type', $('mr-type').value); });
  $('mr-name').addEventListener('change', function () {
    if (active) { active.sess.name = $('mr-name').value.trim() || active.sess.name; dbPutSession(db, active.sess).catch(function () {}); }
  });
  $('mr-tip-dismiss').addEventListener('click', function () { lsSet('mn-record.tip', '1'); renderTip(); });
  $('mr-update-reload').addEventListener('click', function () {
    if (updateWaiting) updateWaiting.postMessage('skip'); else location.reload();
  });
  // device label: editable once
  function renderDevice() {
    var label = lsGet('mn-record.device') || defaultDeviceLabel(env);
    setText('mr-device', label);
    $('mr-device-edit').hidden = lsGet('mn-record.device-edited') === '1';
  }
  $('mr-device-edit').addEventListener('click', function () {
    var input = $('mr-device-input');
    input.value = lsGet('mn-record.device') || defaultDeviceLabel(env);
    $('mr-device-form').hidden = false; $('mr-device-edit').hidden = true; input.focus();
  });
  $('mr-device-form').addEventListener('submit', function (ev) {
    ev.preventDefault();
    var v = $('mr-device-input').value.trim().slice(0, 40);
    if (v) { lsSet('mn-record.device', v); lsSet('mn-record.device-edited', '1'); }
    $('mr-device-form').hidden = true; renderDevice();
  });

  // ---- service worker: only the shell is cached; a new version waits for a moment when nothing is recording
  function registerWorker() {
    if (!('serviceWorker' in navigator)) return;
    navigator.serviceWorker.register('/record/sw.js', {scope: '/record/'}).then(function (reg) {
      function watch(w) {
        w.addEventListener('statechange', function () {
          if (w.state === 'installed' && navigator.serviceWorker.controller) { updateWaiting = w; renderUpdateBanner(); }
        });
      }
      if (reg.waiting && navigator.serviceWorker.controller) { updateWaiting = reg.waiting; renderUpdateBanner(); }
      reg.addEventListener('updatefound', function () { if (reg.installing) watch(reg.installing); });
    }).catch(function () { /* no worker: the page still works online */ });
    var reloading = false;
    navigator.serviceWorker.addEventListener('controllerchange', function () {
      if (reloading || recording()) return;
      reloading = true; location.reload();
    });
  }

  // ---- open: load what is on this phone, then upload it
  (async function init() {
    renderDevice(); renderControls(); loadStorageNote(); loadTypes();
    try {
      db = await openDb(indexedDB);
    } catch (e) {
      $('mr-record').disabled = true;
      flags.storageFull = false;
      notify('This browser cannot store recordings (' + (e && e.message || e) + '). Private browsing can cause this.', 'error');
      return;
    }
    try {
      var all = await dbAllSessions(db);
      for (var i = 0; i < all.length; i++) {
        var sess = all[i];
        if (sess.state === 'recording') { recoverSession(sess); await dbPutSession(db, sess).catch(function () {}); }
        sessions[sess.id] = sess;
        metas[sess.id] = await dbChunkMetas(db, sess.id);
        if (sess.state === 'stopped' && sess.recovered) sess.holdFinish = true;   // a dead page: the person chooses Upload
      }
    } catch (e2) { notify('Could not read the recordings on this phone: ' + (e2 && e2.message || e2), 'error'); }
    renderList(); renderControls();
    registerWorker();
    kickUploads();
  })();
}

if (typeof document !== 'undefined' && document.getElementById && document.getElementById('mr-app')) boot();
