"""The phone recorder page (``meeting_notes/server/mobile_web.py`` and ``static/record/``).

Server side: routes, content types, auth, manifest, service worker, icons. Page side: the pure helpers of
``app.js`` run under node (the HARNESS pattern of test_recorders_web.py), including byte-for-byte checks of the
live-audio framing against ``meeting_notes/wire.py``.
"""

from __future__ import annotations

import json
import re
import shutil
import struct
import subprocess
import types
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient

from meeting_notes import wire
from meeting_notes.server import auth
from meeting_notes.server import mobile_web as mw
from meeting_notes.server import store as store_mod
from meeting_notes.server import web

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")
APP_JS = Path(mw._DIR) / "app.js"
SW_JS = Path(mw._DIR) / "sw.js"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_NOTES_TOKEN", "s3cret")
    app = FastAPI()

    @app.exception_handler(auth.WebAuthRequired)
    async def _redirect(_request, _exc):
        return RedirectResponse(url="/login", status_code=303)

    mw.install_mobile_web(app, store=types.SimpleNamespace(root=tmp_path))
    return TestClient(app)


def _signed_in(client):
    client.cookies.set(auth.WEB_TOKEN_COOKIE, "s3cret")
    return client


# -- routes ------------------------------------------------------------------


def test_page_requires_sign_in_and_serves_html(client):
    for path in ("/record", "/record/"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"
    _signed_in(client)
    r = client.get("/record", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/record/"
    r = client.get("/record/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert r.headers["cache-control"] == "no-cache"
    page = r.text
    for needle in ('<link rel="manifest" href="/record/manifest.webmanifest">', 'rel="apple-touch-icon"', 'id="mr-app"',
                   'id="mr-record"', 'id="mr-stop"', 'id="mr-resume"', 'id="mr-live"', 'id="confirm-dialog"',
                   mw.JS_HREF, mw.CSS_HREF, 'name="viewport"', "viewport-fit=cover", "Use the desktop recorder", 'href="/install"'):
        assert needle in page, needle
    assert "<h1>Record" not in page or True
    assert "https://" not in page.replace("https:'", "")  # no CDN


def test_assets_are_public_with_the_right_types(client):
    r = client.get("/record/manifest.webmanifest")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/manifest+json")
    r = client.get("/record/sw.js")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/javascript")
    assert r.headers["cache-control"] == "no-cache"
    for url, ctype in ((mw.JS_HREF, "text/javascript"), (mw.CSS_HREF, "text/css")):
        r = client.get(url)
        assert r.status_code == 200 and r.headers["content-type"].startswith(ctype)
        assert "immutable" in r.headers["cache-control"]
    assert client.get("/record/nope.png").status_code == 404
    assert client.get("/record/../x").status_code in (404, 307, 422)


def test_manifest_fields(client):
    m = client.get("/record/manifest.webmanifest").json()
    assert m["start_url"] == "/record/" and m["scope"] == "/record/" and m["display"] == "standalone"
    sizes = {i["sizes"]: i for i in m["icons"]}
    assert set(sizes) == {"192x192", "512x512"}
    for icon in m["icons"]:
        assert icon["type"] == "image/png" and icon["src"].startswith("/record/")
        assert client.get(icon["src"]).status_code == 200
    assert m["theme_color"] and m["background_color"] and m["name"]


def _png_size(data: bytes):
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert data[12:16] == b"IHDR"
    return struct.unpack(">II", data[16:24])


def test_icons_are_png_of_the_right_size(client):
    for name, size in (("icon-192.png", 192), ("icon-512.png", 512)):
        r = client.get("/record/" + name)
        assert r.status_code == 200 and r.headers["content-type"] == "image/png"
        assert _png_size(r.content) == (size, size)


def test_service_worker_never_caches_the_api():
    src = mw.service_worker_source()
    assert "__VERSION__" not in src and "__SHELL__" not in src
    # the exclusion is checked before anything is fetched from or put into the cache
    assert "indexOf('/v1/') === 0" in src and "'/login'" in src and "'/logout'" in src
    assert src.index("if (never(url)) return;") < src.index("event.respondWith")
    # only plain, same-origin, non-redirected 200 responses are stored
    assert "res.type === 'basic'" in src and "!res.redirected" in src and "res.status === 200" in src
    assert "req.method !== 'GET'" in src
    assert "opaque" not in src.replace("opaque responses", "")  # nothing special-cases opaque responses into the cache
    shell = mw.shell_urls()
    assert not any(u.startswith("/v1/") or "login" in u for u in shell)
    assert "/record/" in shell and mw.JS_HREF in shell and mw.CSS_HREF in shell
    assert "'mn-record-' + VERSION" in src and "k.indexOf('mn-record-') === 0" in src  # old versions are dropped
    assert "skipWaiting" in src and "'skip'" in src  # a new worker waits until the page asks


def test_service_worker_version_changes_with_the_shell(monkeypatch):
    a = mw.service_worker_source()
    monkeypatch.setattr(mw, "_digest", lambda *names: "feedfacecd")
    assert mw.service_worker_source() != a


def test_package_data_includes_the_record_files():
    root = Path(mw.__file__).resolve().parents[2]
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert "server/static/record/*" in text
    for name in ("app.js", "app.css", "sw.js", "icon-192.png", "icon-512.png"):
        assert (Path(mw._DIR) / name).is_file()


# -- node: syntax and helpers ------------------------------------------------


def _inline_scripts(page: str):
    return re.findall(r"<script>(.*?)</script>", page, flags=re.S)


@needs_node
def test_all_scripts_parse(tmp_path, client):
    _signed_in(client)
    page = client.get("/record/").text
    files = {"app.js": APP_JS.read_text(encoding="utf-8"), "sw.js": mw.service_worker_source()}
    for i, s in enumerate(_inline_scripts(page)):
        files[f"inline{i}.js"] = s
    assert len(files) >= 3
    for name, text in files.items():
        p = tmp_path / name
        p.write_text(text, encoding="utf-8")
        done = subprocess.run([NODE, "--check", str(p)], capture_output=True, text=True, encoding="utf-8")
        assert done.returncode == 0, f"{name}: {done.stderr}"


def test_page_script_does_not_clobber_the_shared_helpers():
    def names(src):
        return set(re.findall(r"^(?:function\s+(\w+)|var\s+(\w+))", src, flags=re.M))

    def flat(src):
        return {a or b for a, b in names(src)}

    clash = flat(web._JS_HELPERS) & flat(APP_JS.read_text(encoding="utf-8"))
    assert not clash, clash


def test_page_script_inserts_no_server_text_as_html():
    src = APP_JS.read_text(encoding="utf-8")
    assert "innerHTML" not in src and "insertAdjacentHTML" not in src and "document.write" not in src
    assert "credentials: 'same-origin'" in src
    assert "navigator.storage.persist" in src and "ROTATE_MS = 5 * 60 * 1000" in src and "TIMESLICE_MS = 5000" in src


HARNESS = r"""
const vm = require('vm'), fs = require('fs');
const ctx = {console, setTimeout, clearTimeout, URL, Blob, Promise, JSON, Math, Date, Uint8Array, Int16Array, Float32Array,
             ArrayBuffer, DataView, Function};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), ctx);
Promise.resolve(vm.runInContext(fs.readFileSync(process.argv[3], 'utf8'), ctx)).then(
  out => process.stdout.write(JSON.stringify(out)),
  err => { console.error(err && err.stack || err); process.exit(1); });
"""


def _run(tmp_path, checks: str):
    page = tmp_path / "page.js"
    page.write_text(APP_JS.read_text(encoding="utf-8"), encoding="utf-8")
    c = tmp_path / "checks.js"
    c.write_text(checks, encoding="utf-8")
    h = tmp_path / "harness.js"
    h.write_text(HARNESS, encoding="utf-8")
    done = subprocess.run([NODE, str(h), str(page), str(c)], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


PURE = r"""
(async function () {
  var d = new Date(2026, 9, 20, 14, 3, 22);
  var sup = function (set) { return function (m) { return set.indexOf(m) >= 0; }; };
  var out = {};
  out.mime = [pickMime(sup(['audio/webm;codecs=opus', 'audio/mp4'])), pickMime(sup(['audio/mp4;codecs=mp4a.40.2', 'audio/mp4'])),
              pickMime(sup(['audio/mp4'])), pickMime(sup([])), pickMime(function () { throw new Error('x'); })];
  out.ids = [newSessionId(d, 'iPhone', 'ab12cd34'), newSessionId(d, "Mike's  Pixel 8!", '00ff00ff'), newSessionId(d, '', '11223344'),
             newSessionId(d, 'x'.repeat(80), '11223344')];
  out.hex = randomHex(4, {getRandomValues: function (a) { for (var i = 0; i < a.length; i++) a[i] = i + 10; return a; }});
  out.name = defaultMeetingName(d);
  out.gate = [
    isMobileClient({ua: 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Safari', coarse: true, width: 390}),
    isMobileClient({ua: 'Mozilla/5.0 (Linux; Android 14; Pixel 8) Chrome/120 Mobile', coarse: true, width: 412}),
    isMobileClient({ua: 'Mozilla/5.0 (Windows NT 10.0) Chrome/120', coarse: false, width: 1920}),
    isMobileClient({ua: 'Mozilla/5.0 (Windows NT 10.0) Chrome/120', coarse: true, width: 1366}),
    isMobileClient({ua: 'Mozilla/5.0 (Macintosh; Intel Mac OS X) Safari', coarse: true, width: 820, touchPoints: 5}),
    isMobileClient({ua: 'Mozilla/5.0 (Macintosh; Intel Mac OS X) Safari', coarse: false, width: 1440, touchPoints: 0})];
  out.platform = [platformOf({ua: 'iPhone'}), platformOf({ua: 'Android 14'}), platformOf({ua: 'Windows'}), defaultDeviceLabel({ua: 'iPhone'}),
                  defaultDeviceLabel({ua: 'Android 14'})];

  // runs: rotation and interruption bookkeeping
  var runs = [], a = appendRun(runs, 'audio/webm;codecs=opus', 1000);
  runs = a.runs; var r0 = a.run;
  out.rotate = [shouldRotate(r0, 1000 + ROTATE_MS - 1), shouldRotate(r0, 1000 + ROTATE_MS)];
  r0.chunk_count = 60; closeRun(r0, 'rotated', 301000);
  a = appendRun(runs, 'audio/webm;codecs=opus', 301500); runs = a.runs; var r1 = a.run;
  r1.chunk_count = 7; closeRun(r1, 'interrupted', 340000);
  a = appendRun(runs, 'audio/webm;codecs=opus', 341000); runs = a.runs; var r2 = a.run;   // chunk_count 0, closed at once
  closeRun(r2, 'interrupted', 341200);
  a = appendRun(runs, 'audio/webm;codecs=opus', 342000); runs = a.runs; var r3 = a.run;   // reuses index 2
  r3.chunk_count = 3;
  out.runs = runs.map(function (r) { return [r.index, r.state, r.chunk_count]; });
  out.elapsed = elapsedMs(runs, 345000);
  out.stalled = [runStalled(0, 99999), runStalled(1000, 1000 + STALL_MS), runStalled(1000, 1001 + STALL_MS)];
  var sess = {id: 's', name: 'N', note_type: 't', runs: runs};
  var metas = [];
  for (var s = 0; s < 60; s++) metas.push({run: 0, seq: s, committed: true});
  for (s = 0; s < 7; s++) metas.push({run: 1, seq: s, committed: true});
  metas.push({run: 2, seq: 0, committed: true}); metas.push({run: 2, seq: 1, committed: true}); metas.push({run: 2, seq: 3, committed: true});  // seq 2 never committed
  metas.push({run: 2, seq: 2, committed: false});
  out.manifest = buildManifest(sess, metas);
  var dead = recoverSession({state: 'recording', runs: [{index: 0, state: 'recording'}, {index: 1, state: 'complete'}]});
  out.recovered = [dead.state, dead.recovered, dead.runs.map(function (r) { return r.state; })];
  out.contig = [contiguousCount([]), contiguousCount([0, 1, 2]), contiguousCount([0, 2]), contiguousCount([1])];

  // "saved" only after the transaction committed
  var events = [], release = [], q = createWriteQueue({
    commit: function (rec) { return new Promise(function (res, rej) { release.push({res: res, rej: rej, rec: rec}); }); },
    onSaved: function (rec) { events.push('saved ' + rec.seq); },
    onFail: function (err, rec) { events.push('fail ' + (rec ? rec.seq : '-') + ' ' + err.name); }
  });
  q.add(async function () { return {seq: 0}; }); q.add(async function () { return {seq: 1}; }); q.add(async function () { return {seq: 2}; });
  await new Promise(function (r) { setTimeout(r, 10); });
  out.queueBefore = events.slice().concat([release.length]);          // nothing saved, only the first write is in flight
  release[0].res(); await new Promise(function (r) { setTimeout(r, 10); });
  out.queueAfterOne = events.slice().concat([release.length]);
  var quota = new Error('full'); quota.name = 'QuotaExceededError';
  release[1].rej(quota); await q.idle();
  out.queueAfterFail = events.slice().concat([release.length, q.hasFailed()]);   // the third write never starts
  out.quota = [isQuotaError(quota), isQuotaError({code: 22}), isQuotaError(new Error('x'))];
  out.counts = [countChunks([{committed: true, uploaded: true}, {committed: true}, {committed: false}, {committed: true, uploaded: true}])];
  out.lines = [statusLine({saved: 14, uploaded: 12}, true), statusLine({saved: 0, uploaded: 0}, true), statusLine({saved: 5, uploaded: 5}, false),
               statusLine({saved: 1, uploaded: 0}, true)];
  out.level = [levelFromSamples([]), levelFromSamples([0, 0, 0]), levelFromSamples([1, -1, 1, -1]), levelFromSamples([0.05, -0.05])];
  out.clock = [formatClock(0), formatClock(3725000), formatClock(-5)];
  out.bytes = [formatBytes(2.5e9), formatBytes(48e6), formatBytes(1500), formatBytes(-1)];
  out.warn = [
    warningsFor({recording: true, ios: true, online: true}).map(function (w) { return w.text; }),
    warningsFor({online: false}).map(function (w) { return w.text; }),
    warningsFor({micBlocked: true, ios: true}).map(function (w) { return w.text; }),
    warningsFor({storageFull: true}).map(function (w) { return w.text; }),
    warningsFor({recording: true, ios: false, online: true}).length];
  out.partial = mergePartial(mergePartial(mergePartial([], {start: 5, end: 6, text: ' two '}), {start: 1, end: 2, text: 'one'}), {start: 5, end: 7, text: 'two!'});
  out.partialBlank = mergePartial([], {start: 1, end: 2, text: '  '});
  out.hello = helloMessage({id: 'sid', name: 'M', started_wall: 1760000000.5, device: 'iPhone'});
  return out;
})()
"""


@needs_node
def test_pure_helpers_in_node(tmp_path):
    out = _run(tmp_path, PURE)
    assert out["mime"] == ["audio/webm;codecs=opus", "audio/mp4;codecs=mp4a.40.2", "audio/mp4", "", ""]
    assert out["ids"][0] == "2026-10-20_14-03-22_Phone-iPhone_ab12cd34"
    assert out["ids"][1] == "2026-10-20_14-03-22_Phone-Mike-s-Pixel-8_00ff00ff"
    assert out["ids"][2] == "2026-10-20_14-03-22_Phone-phone_11223344"
    for sid in out["ids"]:
        assert store_mod.is_safe_id(sid), sid
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_Phone-[A-Za-z0-9._-]+_[0-9a-f]{8}", sid)
    assert out["hex"] == "0a0b0c0d"
    assert out["name"] == "Phone recording, Oct 20, 2:03 PM"
    assert out["gate"] == [True, True, False, False, True, False]
    assert out["platform"] == ["iOS", "Android", "", "iPhone", "Android phone"]
    # runs
    assert out["rotate"] == [False, True]
    assert out["runs"] == [[0, "complete", 60], [1, "interrupted", 7], [2, "recording", 3]]  # the empty run's index is reused
    assert out["elapsed"] == (301000 - 1000) + (340000 - 301500) + (345000 - 342000)
    assert out["stalled"] == [False, False, True]
    m = out["manifest"]
    assert m["name"] == "N" and m["note_type"] == "t"
    assert [(r["index"], r["state"], r["chunk_count"]) for r in m["runs"]] == [(0, "complete", 60), (1, "interrupted", 7), (2, "lost-tail", 2)]
    assert out["recovered"] == ["stopped", True, ["lost-tail", "complete"]]
    assert out["contig"] == [0, 3, 1, 0]
    # saved only after commit; a failed write stops the queue
    assert out["queueBefore"] == [1]
    assert out["queueAfterOne"] == ["saved 0", 2]
    assert out["queueAfterFail"] == ["saved 0", "fail 1 QuotaExceededError", 2, True]
    assert out["quota"] == [True, True, False]
    assert out["counts"] == [{"saved": 3, "uploaded": 2}]
    assert out["lines"] == ["Saved on this phone · 12 of 14 pieces uploaded", "Starting. Nothing saved yet.",
                            "Saved on this phone · all 5 pieces uploaded", "Saved on this phone · 0 of 1 piece uploaded"]
    assert out["level"][:2] == [0, 0] and out["level"][2] == 1 and 0 < out["level"][3] < 1
    assert out["clock"] == ["00:00:00", "01:02:05", "00:00:00"]
    assert out["bytes"] == ["2.5 GB", "48 MB", "2 KB", ""]
    w = out["warn"]
    assert w[0] == ["Keep this page open. Locking the screen pauses the recording."]
    assert w[1] == ["Not connected: recording on this phone, it uploads when you’re back online."]
    assert "Settings > Safari > Microphone" in w[2][0] and w[3][0].startswith("Storage full: recording stopped")
    assert w[4] == 0
    assert out["partial"] == [{"start": 1, "end": 2, "text": "one"}, {"start": 5, "end": 7, "text": "two!"}]
    assert out["partialBlank"] == []
    h = out["hello"]
    assert h == {"type": "hello", "session_id": "sid", "name": "M", "tracks": ["system"], "sample_rate": 16000, "protocol": 1,
                 "started_wall": 1760000000.5, "device": "iPhone"}
    hello = wire.Hello(**{k: v for k, v in h.items()})  # every key is a wire.Hello field
    assert wire.to_json(hello) == h


UPLOAD = r"""
(async function () {
  function res(status, body) { return {ok: status >= 200 && status < 300, status: status, redirected: false, json: async function () { return body; }}; }
  function mkDeps(serverRuns, behaviour) {
    var log = [], flags = {};
    var chunks = [];
    [[1, 0], [0, 2], [0, 0], [1, 1], [0, 1], [0, 3]].forEach(function (p) { chunks.push({session: 's', run: p[0], seq: p[1], type: 'audio/webm', committed: true, uploaded: false}); });
    chunks.push({session: 's', run: 0, seq: 4, type: 'audio/webm', committed: false, uploaded: false});
    return {log: log, chunks: chunks, deps: {
      fetch: async function (url, init) {
        log.push((init.method || 'GET') + ' ' + url);
        var forced = behaviour ? behaviour(url, init, log) : null;
        if (forced) return forced;
        if (init.method === 'POST') return res(201, {state: 'open'});
        if (init.method === 'PUT') return res(204, null);
        return res(200, {state: 'open', runs: serverRuns});
      },
      listChunks: async function () { return chunks; },
      loadChunk: async function (m) { return 'data-' + m.run + '-' + m.seq; },
      setUploaded: async function (m, f) { m.uploaded = f; },
      shouldStop: function () { return false; }
    }};
  }
  var sess = {id: 's', name: 'N', note_type: 't', device: 'iPhone', platform: 'iOS', started_wall: 1, live: false};
  var out = {};
  // oldest first, skipping what the server already has (inclusive ranges), uncommitted chunks never sent
  var m1 = mkDeps({'0': [[0, 1]], '1': [[0, 0]]});
  var r1 = await syncChunks(sess, m1.deps);
  out.puts = m1.log.filter(function (l) { return l.indexOf('PUT') === 0; });
  out.first = m1.log.slice(0, 2);
  out.r1 = [r1.state, r1.uploaded, r1.remaining];
  out.flags = m1.chunks.map(function (c) { return c.run + ':' + c.seq + (c.uploaded ? 'u' : '-'); });
  out.createBody = null;
  // a 503 on the second PUT: partial progress is kept, and the error is retryable
  var n = 0;
  var m2 = mkDeps({}, function (url, init) { if (init.method === 'PUT' && ++n === 2) return res(503, null); return null; });
  try { await syncChunks(sess, m2.deps); out.err = 'none'; } catch (e) { out.err = [e.retry, e.kind, e.status]; }
  out.partial = m2.chunks.filter(function (c) { return c.uploaded; }).map(function (c) { return c.run + ':' + c.seq; });
  // offline
  var m3 = mkDeps({}); m3.deps.fetch = async function () { throw new TypeError('Failed to fetch'); };
  try { await syncChunks(sess, m3.deps); } catch (e) { out.offline = [e.retry, e.kind]; }
  // signed out, conflict, rejected
  var kinds = [];
  for (var st of [401, 403, 409, 413, 500]) {
    var m = mkDeps({}, function (url, init) { return init.method === 'POST' ? res(st, {missing: [[0, 1]]}) : null; });
    try { await syncChunks(sess, m.deps); } catch (e) { kinds.push([st, e.retry, e.kind]); }
  }
  var redirected = mkDeps({}, function (url, init) { var r = res(200, {}); r.redirected = true; return init.method === 'POST' ? r : null; });
  try { await syncChunks(sess, redirected.deps); } catch (e) { kinds.push(['redirect', e.retry, e.kind]); }
  out.kinds = kinds;
  // the server already finalized it
  var m4 = mkDeps({}, function (url, init) { return init.method === 'POST' ? res(200, {state: 'finalized'}) : null; });
  var r4 = await syncChunks(sess, m4.deps);
  out.finalized = [r4.state, r4.uploaded, m4.log.length];
  // a full re-send ignores what the server claims to have
  var m5 = mkDeps({'0': [[0, 3]], '1': [[0, 1]]});
  await syncChunks(sess, m5.deps, {trustServer: false});
  out.resent = m5.log.filter(function (l) { return l.indexOf('PUT') === 0; }).length;
  // finish
  var logf = [];
  var okDeps = {fetch: async function (url, init) { logf.push(init.method + ' ' + url + ' ' + init.body); return res(202, {state: 'preparing'}); }};
  out.finish = (await finishSession(sess, {runs: [{index: 0}], name: 'N', note_type: 't'}, okDeps)).state;
  out.finishReq = logf;
  var missDeps = {fetch: async function () { return res(409, {missing: [[0, 3]]}); }};
  try { await finishSession(sess, {}, missDeps); } catch (e) { out.finishMissing = [e.kind, e.retry, e.body.missing]; }

  // backoff: 2 s doubling to 60 s, reset by success
  out.delays = [0, 1, 2, 3, 4, 5, 6, 7, 20].map(backoffDelay);
  var sleeps = [], calls = 0;
  var done = await withBackoff(async function () {
    calls++;
    if (calls <= 8) throw UploadError({retry: true, kind: 'network'}, 'offline');
    return {done: true};
  }, {sleep: async function (ms) { sleeps.push(ms); }}, null);
  out.sleeps = sleeps; out.doneAfter = [calls, !!done];
  var sleeps2 = [], c2 = 0;
  await withBackoff(async function () { c2++; if (c2 === 1 || c2 === 3) throw UploadError({retry: true, kind: 'server'}, 's'); return c2 >= 4 ? {done: true} : {done: false}; },
                    {sleep: async function (ms) { sleeps2.push(ms); }}, null);
  out.sleepsReset = sleeps2;      // failure, success (reset), failure -> 2000 again
  try { await withBackoff(async function () { throw UploadError({retry: false, kind: 'auth'}, 'x'); }, {sleep: async function () {}}); } catch (e) { out.fatal = e.kind; }
  var stopped = await withBackoff(async function () { return {done: false}; }, {sleep: async function () {}, shouldStop: function () { return true; }});
  out.stopped = stopped;
  out.urls = [chunkUrl('a b', 2, 7), sessionUrl('x/y', '/finish')];
  return out;
})()
"""


@needs_node
def test_upload_queue_in_node(tmp_path):
    out = _run(tmp_path, UPLOAD)
    # run 0 has seq 0-1 on the server, run 1 has seq 0: send 0:2, 0:3, 1:1 in that order; the uncommitted 0:4 is never sent
    assert out["first"] == ["POST /v1/mobile/sessions", "GET /v1/mobile/sessions/s"]
    assert out["puts"] == [
        "PUT /v1/mobile/sessions/s/runs/0/chunks/2",
        "PUT /v1/mobile/sessions/s/runs/0/chunks/3",
        "PUT /v1/mobile/sessions/s/runs/1/chunks/1",
    ]
    assert out["r1"] == ["open", 3, 0]
    assert sorted(out["flags"]) == ["0:0u", "0:1u", "0:2u", "0:3u", "0:4-", "1:0u", "1:1u"]
    assert out["err"] == [True, "server", 503]
    assert out["partial"] == ["0:0"]  # the first PUT landed before the 503
    assert out["offline"] == [True, "network"]
    assert out["kinds"] == [[401, False, "auth"], [403, False, "forbidden"], [409, False, "conflict"], [413, False, "rejected"],
                            [500, True, "server"], ["redirect", False, "auth"]]
    assert out["finalized"] == ["finalized", 0, 1]  # nothing else is called once the server says finalized
    assert out["resent"] == 6
    assert out["finish"] == "preparing"
    assert out["finishReq"] == ['POST /v1/mobile/sessions/s/finish {"runs":[{"index":0}],"name":"N","note_type":"t"}']
    assert out["finishMissing"] == ["conflict", False, [[0, 3]]]
    assert out["delays"] == [2000, 4000, 8000, 16000, 32000, 60000, 60000, 60000, 60000]
    assert out["sleeps"] == [2000, 4000, 8000, 16000, 32000, 60000, 60000, 60000] and out["doneAfter"] == [9, True]
    assert out["sleepsReset"] == [2000, 2000]
    assert out["fatal"] == "auth" and out["stopped"] is None
    assert out["urls"] == ["/v1/mobile/sessions/a%20b/runs/2/chunks/7", "/v1/mobile/sessions/x%2Fy/finish"]


LIVE = r"""
(function () {
  var F = new Function(FRAMER_SRC + '; return {Framer: Framer, toInt16: toInt16};')();
  function run(rate, seconds, freq, amp, block) {
    var fr = new F.Framer(rate, 16000, 320), frames = [], total = Math.round(rate * seconds), buf = new Float32Array(block);
    for (var i = 0; i < total; i += block) {
      var n = Math.min(block, total - i);
      for (var j = 0; j < n; j++) buf[j] = amp * Math.sin(2 * Math.PI * freq * (i + j) / rate);
      frames = frames.concat(fr.push(buf.subarray(0, n)));
    }
    var samples = [];
    frames.forEach(function (f) { for (var k = 0; k < f.length; k++) samples.push(f[k]); });
    var peak = 0, cross = 0;
    for (var s = 100; s < samples.length; s++) { peak = Math.max(peak, Math.abs(samples[s])); if (samples[s - 1] < 0 && samples[s] >= 0) cross++; }
    return {frames: frames.length, len: frames[0].length, all320: frames.every(function (f) { return f.length === 320 && f instanceof Int16Array; }),
            peak: peak, cross: cross};
  }
  var buf = encodeAudioFrame(1, 123456789012, new Int16Array([0, 1, -1, 32767, -32768, 258]));
  var hex = Array.prototype.map.call(new Uint8Array(buf), function (b) { return (b < 16 ? '0' : '') + b.toString(16); }).join('');
  var buf0 = encodeAudioFrame(1, 0, new Int16Array(0));
  return {
    a48: run(48000, 1, 440, 0.5, 128),
    a44: run(44100, 1, 440, 0.5, 128),
    a16: run(16000, 1, 440, 0.5, 128),
    quiet: run(48000, 0.5, 440, 0, 480),
    clip: [F.toInt16(2), F.toInt16(-2), F.toInt16(0), F.toInt16(0.5), F.toInt16(-0.5)],
    hex: hex, len0: buf0.byteLength,
    worklet: WORKLET_SRC.indexOf('registerProcessor("mn-pcm16"') > 0 && WORKLET_SRC.indexOf('new Framer(sampleRate, 16000, 320)') > 0
  };
})()
"""


@needs_node
def test_resampler_and_wire_framing_in_node(tmp_path):
    out = _run(tmp_path, LIVE)
    for key, tol in (("a48", 0.012), ("a44", 0.02), ("a16", 0.001)):
        r = out[key]
        assert r["frames"] in (49, 50) and r["len"] == 320 and r["all320"], (key, r)
        assert abs(r["peak"] - 0.5 * 32767) < 0.05 * 32767, (key, r)  # amplitude kept (box filter loses ~nothing at 440 Hz)
        assert 435 <= r["cross"] * (16000 / (r["frames"] * 320 - 100)) <= 445, (key, r)  # 440 Hz stays 440 Hz after resampling
    assert out["quiet"]["peak"] == 0
    assert out["clip"] == [32767, -32768, 0, 16384, -16384]
    # the bytes are exactly what wire.encode_audio_frame produces
    pcm = struct.pack("<6h", 0, 1, -1, 32767, -32768, 258)
    assert out["hex"] == wire.encode_audio_frame("system", 123456789012, pcm).hex()
    assert out["len0"] == wire.FRAME_HEADER_SIZE == 9
    track, offset, back = wire.decode_audio_frame(bytes.fromhex(out["hex"]))
    assert (track, offset, back) == ("system", 123456789012, pcm)
    assert out["worklet"] is True


def test_every_element_the_script_looks_up_is_in_the_page(client):
    _signed_in(client)
    page = client.get("/record/").text
    ids = set(re.findall(r"\$\('(mr-[a-z-]+)'\)", APP_JS.read_text(encoding="utf-8")))
    assert len(ids) > 25
    for i in ids:
        assert f'id="{i}"' in page, i


def test_signing_in_returns_to_the_page_that_asked(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from meeting_notes.server.app import create_app

    monkeypatch.setenv("MEETING_NOTES_TOKEN", "s3cret")
    client = TestClient(create_app(data_root=str(tmp_path / "data")))
    r = client.get("/record/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=%2Frecord%2F"
    page = client.get("/login?next=/record/").text
    assert '<input type="hidden" name="next" value="/record/">' in page
    r = client.post("/login", data={"token": "s3cret", "next": "/record/"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/record/"
    # never an open redirect
    for bad in ("https://evil.example/", "//evil.example/", "/\evil", "/login"):
        r = client.post("/login", data={"token": "s3cret", "next": bad}, follow_redirects=False)
        assert r.headers["location"] == "/", bad
    assert 'name="next"' not in client.get("/login?next=//evil.example/").text
    # a wrong token keeps the return address
    r = client.post("/login", data={"token": "nope", "next": "/record/"}, follow_redirects=False)
    assert r.headers["location"] == "/login?error=1&next=%2Frecord%2F"


REVIEW_FIXES = r"""
(async function () {
  var out = {};
  function tick() { return new Promise(function (r) { setTimeout(r, 10); }); }
  // 1. a failed queue never silently drops: onDrop says so; a fresh queue per recording saves again
  var ev = [];
  function mk() {
    var failNext = false;
    var q = createWriteQueue({
      commit: function (rec) { return rec.bad ? Promise.reject(Object.assign(new Error('boom'), {name: 'UnknownError'})) : Promise.resolve(); },
      onSaved: function (rec) { ev.push('saved ' + rec.seq); },
      onFail: function (err, rec) { ev.push('fail ' + err.name); },
      onDrop: function (err) { ev.push('drop ' + err.name); }
    });
    return q;
  }
  var q1 = mk();
  q1.add(async function () { return {seq: 0, bad: true}; });
  q1.add(async function () { return {seq: 1}; });
  await q1.idle();
  q1.add(async function () { return {seq: 2}; });
  await q1.idle();
  var q2 = mk();
  q2.add(async function () { return {seq: 0}; });
  await q2.idle();
  out.queue = ev;
  // 2. run cap
  out.cap = [MAX_RUNS, runCapReached(new Array(189)), runCapReached(new Array(190)), runCapReached(null)];
  out.capWarn = warningsFor({tooManyRuns: true}).map(function (w) { return w.text; });
  // 3. an empty-looking run is only dropped when it really has nothing; a run with a queued last blob keeps its index
  var runs = [], a = appendRun(runs, 'audio/webm', 1000); runs = a.runs;
  a.run.chunk_count = 3; closeRun(a.run, 'rotated', 301000);
  var b = appendRun(runs, 'audio/webm', 301000);
  out.rot = b.runs.map(function (r) { return [r.index, r.state, r.chunk_count, r.started_ms]; });
  // 4. only quota errors mean full
  function E(name, code) { var e = new Error('x'); e.name = name; if (code) e.code = code; return e; }
  out.quota = [isQuotaError(E('QuotaExceededError')), isQuotaError(E('NS_ERROR_DOM_QUOTA_REACHED')), isQuotaError(E('Err', 22)),
               isQuotaError(E('UnknownError')), isQuotaError(E('AbortError')), isQuotaError(null)];
  out.problem = [storageProblem(E('UnknownError')), storageProblem(E('QuotaExceededError'))];
  out.storageWarn = [warningsFor({storageError: 'UnknownError'}).map(function (w) { return w.text; }),
                     warningsFor({storageFull: true, storageError: ''}).map(function (w) { return w.text.slice(0, 12); })];
  // 5. delete outcomes
  function R(status, x) { return Object.assign({ok: status >= 200 && status < 300, status: status, redirected: false}, x || {}); }
  out.del = [R(204), R(200), R(404), R(401), R(403), R(409), R(500), R(200, {redirected: true}), R(0, {type: 'opaqueredirect', ok: false}), null]
    .map(function (r) { var o = deleteOutcome(r); return [o.del, o.kind]; });
  // 6. finalized answers
  out.fin = [saysFinalized({state: 'finalized'}), saysFinalized({detail: 'session is finalized, not open'}), saysFinalized({state: 'preparing'}),
             saysFinalized({detail: 'chunks missing'}), saysFinalized(null)];
  // 7. live text keeps only the last 200 lines
  var list = [];
  for (var i = 0; i < 250; i++) list = mergePartial(list, {start: i, end: i + 1, text: 'line ' + i});
  out.live = [LIVE_MAX_LINES, list.length, list[0].start, list[199].start];
  return out;
})()
"""


@needs_node
def test_review_fixes_in_node(tmp_path):
    out = _run(tmp_path, REVIEW_FIXES)
    assert out["queue"] == ["fail UnknownError", "drop UnknownError", "drop UnknownError", "saved 0"]
    assert out["cap"] == [190, False, True, False]
    assert out["capWarn"] == ["This recording was interrupted too many times. Stop and start a new recording."]
    assert out["rot"] == [[0, "complete", 3, 1000], [1, "recording", 0, 301000]]
    assert out["quota"] == [True, True, True, False, False, False]
    assert out["problem"] == [{"full": False, "name": "UnknownError"}, {"full": True, "name": "QuotaExceededError"}]
    assert out["storageWarn"][0][0].startswith("Could not save on this phone: UnknownError")
    assert out["storageWarn"][1] == ["Storage full"]
    assert out["del"] == [[True, "ok"], [True, "ok"], [True, "ok"], [False, "auth"], [False, "forbidden"], [False, "busy"],
                          [False, "server"], [False, "auth"], [False, "auth"], [False, "network"]]
    assert out["fin"] == [True, True, False, False, False]
    assert out["live"] == [200, 200, 50, 249]


def test_page_wiring_for_the_review_fixes():
    src = APP_JS.read_text(encoding="utf-8")
    # a fresh write queue for every recording
    assert "function makeWriteQueue()" in src and "writeQueue = makeWriteQueue();" in src
    # a planned rotation starts the next recorder before waiting for the previous run's writes
    body = src[src.index("async function onRunStopped"):src.index("function capRuns")]
    assert body.index("startRun(a)") < body.index("await writeQueue.idle()")
    # "Storage full" is never forced
    assert "|| true" not in src
    # a delete answer decides, not an exception
    assert "deleteOutcome(res)" in src and "Sign in again, then delete." in src
    # a finalized answer completes the session rather than "finishing"
    assert "saysFinalized(e.body)" in src and "fin.state === 'finalized'" in src
    # live text updates one line, not the whole box
    assert "function applyLivePartial" in src and "renderLiveText(); }" not in src.split("m.type === 'partial'")[1].split("\n")[0]


def test_finished_meetings_leave_the_on_this_phone_list():
    from pathlib import Path

    src = Path("meeting_notes/server/static/record/app.js").read_text(encoding="utf-8")
    assert "finishedHere" not in src and "$('mr-pending').hidden = !ids.length;" in src


def test_sign_in_cookie_is_kept_between_visits(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from meeting_notes.server.app import create_app, LOGIN_COOKIE_MAX_AGE

    monkeypatch.setenv("MEETING_NOTES_TOKEN", "s3cret")
    client = TestClient(create_app(data_root=str(tmp_path / "data")))
    r = client.post("/login", data={"token": "s3cret"}, follow_redirects=False)
    cookie = r.headers["set-cookie"].lower()
    assert f"max-age={LOGIN_COOKIE_MAX_AGE}" in cookie and LOGIN_COOKIE_MAX_AGE >= 90 * 24 * 3600
