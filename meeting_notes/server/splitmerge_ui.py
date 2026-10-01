"""Markup and script for "Split meeting" and "Combine meetings" on the Meetings page.

Kept out of ``web.py`` so the page only needs a few hook lines (a menu item, a bulk
button, the continuation banner, and ``+ splitmerge_ui.JS``). The script relies on
globals the Meetings page already defines: ``currentSession``, ``rowInfo``,
``aiEnabled``, ``loadRows``, ``closeOverlay``, ``notify``, ``fmtDuration``,
``fmtDate``, ``fmtAxis``, ``niceStep``, ``segLane``, ``escapeHtml``, ``icon``,
``processingState``, ``selectedIds`` and ``updateSelection``. Styles are in
``static/splitmerge.css`` (design tokens from ``app.css`` only).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

_CSS_PATH = Path(__file__).resolve().parent / "static" / "splitmerge.css"


def css_href() -> str:
    try:
        digest = hashlib.sha1(_CSS_PATH.read_bytes()).hexdigest()[:10]
    except OSError:
        digest = "0"
    return f"/static/splitmerge.css?v={digest}"


def continuation_hint_html(icon_html: str, dismiss_icon_html: str) -> str:
    """The dismissible "looks like a continuation" banner, placed under the meeting title."""
    return (
        '<div class="banner cont-hint" id="continue-hint" role="status" hidden>'
        f"{icon_html}"
        '<span class="cont-text" id="continue-text"></span>'
        '<span class="cont-actions"><button type="button" class="btn secondary sm" id="continue-combine">Combine…</button>'
        f'<button type="button" class="btn ghost sm icon-only" id="continue-dismiss" aria-label="Dismiss" title="Dismiss">{dismiss_icon_html}</button></span>'
        "</div>"
    )


def dialogs_html(icon) -> str:
    """The two native ``<dialog>`` elements. ``icon`` is ``web._icon``."""
    return f"""<link rel="stylesheet" href="{css_href()}">
<dialog class="dialog split-dialog" id="split-dialog" aria-labelledby="split-title" aria-describedby="split-lead">
  <div class="sp-shell">
    <div class="sp-head">
      <h2 class="dialog-title" id="split-title" tabindex="-1">Split meeting</h2>
      <p class="dialog-lead" id="split-lead"></p>
    </div>
    <div class="sp-body" id="split-body">
      <section class="sp-block" aria-label="Timeline">
        <div class="sp-tl-head">
          <span class="strip-title">Timeline</span>
          <span class="sp-help">Click the timeline or a transcript line to add a split point.</span>
          <button type="button" class="btn secondary sm" id="split-ai" hidden>{icon("sparkles", 14)}<span>Suggest with AI</span></button>
        </div>
        <div class="strip sp-tl" id="split-tl" aria-label="Meeting timeline, You and Them"></div>
        <p class="sp-info" id="split-info" role="status"></p>
        <div class="sp-add">
          <label class="sr-only" for="split-time">Add a split point at a time</label>
          <input type="text" id="split-time" inputmode="numeric" autocomplete="off" placeholder="Add at m:ss" maxlength="9">
          <button type="button" class="btn secondary sm" id="split-add">Add point</button>
        </div>
      </section>
      <section class="sp-block" aria-labelledby="split-sug-title">
        <div class="sp-sec-head"><h3 id="split-sug-title">Suggested split points</h3><button type="button" class="btn ghost sm" id="split-use-all" hidden>Use all</button></div>
        <ul class="sp-sugs" id="split-sugs"></ul>
      </section>
      <section class="sp-block" aria-labelledby="split-parts-title">
        <h3 id="split-parts-title">Parts</h3>
        <ol class="sp-parts" id="split-parts"></ol>
      </section>
      <details class="sp-lines-wrap" id="split-lines-wrap">
        <summary>Transcript</summary>
        <div class="sp-lines" id="split-lines" aria-label="Transcript lines. Choose one to split the meeting there."></div>
      </details>
      <label class="sp-check" id="split-regen-row"><input type="checkbox" id="split-regen"><span>Regenerate notes for each part</span></label>
      <p class="err" id="split-error" role="alert"></p>
    </div>
    <div class="dialog-foot sp-foot">
      <button type="button" class="btn secondary" id="split-cancel">Cancel</button>
      <button type="button" class="btn primary" id="split-go" disabled>Split meeting</button>
    </div>
  </div>
</dialog>
<dialog class="dialog pick-dialog" id="pick-dialog" aria-labelledby="pick-title" aria-describedby="pick-lead">
  <div class="sp-shell">
    <div class="sp-head">
      <h2 class="dialog-title" id="pick-title" tabindex="-1">Combine with another meeting</h2>
      <p class="dialog-lead" id="pick-lead"></p>
    </div>
    <div class="sp-body">
      <div class="pk-search">
        <label class="sr-only" for="pick-q">Search meetings</label>
        {icon("search", 16)}
        <input type="text" id="pick-q" placeholder="Search meetings by name" autocomplete="off">
      </div>
      <div class="pk-scroll" id="pick-scroll"><ul class="pk-list" id="pick-list"></ul></div>
      <p class="err" id="pick-error" role="alert"></p>
    </div>
    <div class="dialog-foot sp-foot">
      <button type="button" class="btn secondary" id="pick-cancel">Cancel</button>
      <button type="button" class="btn primary" id="pick-go" disabled>Choose a meeting</button>
    </div>
  </div>
</dialog>
<dialog class="dialog combine-dialog" id="combine-dialog" aria-labelledby="combine-title" aria-describedby="combine-lead">
  <div class="sp-shell">
    <div class="sp-head">
      <h2 class="dialog-title" id="combine-title" tabindex="-1">Combine meetings</h2>
      <p class="dialog-lead" id="combine-lead">The recordings are joined in time order. The real time between them is filled with silence (at most 10 minutes). The originals move to Recently deleted.</p>
    </div>
    <div class="sp-body">
      <ol class="cb-list" id="combine-list"></ol>
      <label class="field"><span class="name">Name</span><input type="text" id="combine-name" maxlength="200" autocomplete="off"></label>
      <label class="sp-check" id="combine-regen-row"><input type="checkbox" id="combine-regen"><span>Regenerate notes for the combined meeting</span></label>
      <p class="err" id="combine-error" role="alert"></p>
    </div>
    <div class="dialog-foot sp-foot">
      <button type="button" class="btn secondary" id="combine-cancel">Cancel</button>
      <button type="button" class="btn primary" id="combine-go" disabled>Combine meetings</button>
    </div>
  </div>
</dialog>"""


JS = r"""
/* ---- Split / combine meetings (server/splitmerge_ui.py) ---- */
var SP_MIN = 10, CB_CAP = 600, CB_MAX = 20; // keep equal to splitmerge.COMBINE_MAX (a test checks)
var sp = null, cb = null;
var spDlg = document.getElementById('split-dialog'), cbDlg = document.getElementById('combine-dialog');
function spFmt(t) { return fmtDuration(Math.round(t)); }
function spParseTime(text) {
  var s = String(text || '').trim();
  if (!/^\d+(:\d{1,2}){0,2}(\.\d+)?$/.test(s)) return null;
  var parts = s.split(':').map(Number), sec = 0;
  parts.forEach(function (p) { sec = sec * 60 + p; });
  return sec;
}
function spSorted() { return sp.points.slice().sort(function (a, b) { return a - b; }); }
function spParts() {
  var pts = spSorted(), edges = [0].concat(pts, [sp.total]), out = [];
  for (var i = 0; i < edges.length - 1; i++) out.push({i: i, start: edges[i], end: edges[i + 1], len: edges[i + 1] - edges[i]});
  return out;
}
function spReason(t) {
  for (var i = 0; i < sp.suggestions.length; i++) if (Math.abs(sp.suggestions[i].time_sec - t) < 0.75) return sp.suggestions[i];
  return null;
}
function spInfo(text, kind) {
  var el = document.getElementById('split-info');
  el.textContent = text || '';
  el.className = 'sp-info' + (kind ? ' ' + kind : '');
}
function spAddPoint(t, source) {
  t = Math.round(t * 10) / 10;
  if (!(t > 0 && t < sp.total)) { spInfo('That is outside the meeting.', 'warn'); return false; }
  var near = sp.points.some(function (p) { return Math.abs(p - t) < 0.75; });
  if (near) { spInfo('There is already a split point there.', 'warn'); return false; }
  var edges = [0].concat(spSorted(), [sp.total]);
  for (var i = 0; i < edges.length; i++) {
    if (Math.abs(edges[i] - t) < SP_MIN) { spInfo('Each part must be at least ' + SP_MIN + ' seconds long.', 'warn'); return false; }
  }
  sp.points.push(t);
  spInfo('Split point added at ' + spFmt(t) + (source ? ' (' + source + ')' : '') + '.');
  spRender();
  return true;
}
function spRemovePoint(t) {
  sp.points = sp.points.filter(function (p) { return Math.abs(p - t) >= 0.05; });
  spInfo('Split point removed.');
  spRender();
}
function spRender() { spRenderTimeline(); spRenderSuggestions(); spRenderParts(); spRenderLines(); spRenderFoot(); }
function spRenderFoot() {
  var parts = spParts(), bad = parts.some(function (p) { return p.len < SP_MIN; });
  var go = document.getElementById('split-go'), n = parts.length;
  go.textContent = sp.points.length ? 'Split into ' + n + ' meetings' : 'Split meeting';
  go.disabled = !sp.points.length || bad || sp.busy;
}
function spRenderTimeline() {
  var root = document.getElementById('split-tl'), total = sp.total, lanes = [], byLabel = {};
  sp.segments.forEach(function (s) {
    if (!validSpan(s) || s.in_gap) return;
    var mic = s.track === 'mic', label = segLane(s);
    if (!byLabel[label]) { byLabel[label] = {label: label, mic: mic, items: []}; lanes.push(byLabel[label]); }
    byLabel[label].items.push(s);
  });
  lanes.sort(function (a, b) { return (b.mic ? 1 : 0) - (a.mic ? 1 : 0); });
  if (lanes.length > 2) {
    var you = {label: 'You', mic: true, items: []}, them = {label: 'Them', mic: false, items: []};
    lanes.forEach(function (l) { var t = l.mic ? you : them; t.items = t.items.concat(l.items); });
    lanes = [you, them].filter(function (l) { return l.items.length; });
  }
  if (!lanes.length) lanes = [{label: 'Audio', mic: true, items: []}];
  function pct(v) { return (v / total * 100).toFixed(3) + '%'; }
  var gapMarkup = sp.segments.filter(function (s) { return s.in_gap && validSpan(s); }).map(function (g) {
    return '<span class="gap-band" style="left:' + pct(g.start) + ';width:' + pct(g.end - g.start) + '" title="Audio lost from ' + spFmt(g.start) + ' to ' + spFmt(g.end) + '"></span>';
  }).join('');
  var laneMarkup = lanes.map(function (l) {
    var blocks = l.items.map(function (s) {
      return '<span class="blk' + (l.mic ? '' : ' them') + '" style="left:' + pct(s.start) + ';width:' + pct(Math.min(s.end, total) - s.start) + '"></span>';
    }).join('');
    return '<div class="lane"><div class="lane-label" title="' + escapeHtml(l.label) + '"><i class="sw' + (l.mic ? '' : ' them') + '"></i><span>' + escapeHtml(l.label) + '</span></div><div class="lane-track">' + gapMarkup + blocks + '</div></div>';
  }).join('');
  var laneW = parseInt(getComputedStyle(root).getPropertyValue('--lane-w'), 10) || 72;
  var plotW = Math.max(200, root.getBoundingClientRect().width - laneW), step = niceStep(total, Math.max(2, Math.floor(plotW / 72)));
  var ticks = '';
  for (var t = 0; t <= total; t += step) ticks += '<span' + (t / total > 0.96 ? ' class="end"' : '') + ' style="left:' + pct(t) + '">' + fmtAxis(t) + '</span>';
  var chosen = spSorted(), marks = '';
  var items = chosen.map(function (t) { return {t: t, on: true}; });
  sp.suggestions.forEach(function (s) {
    if (!chosen.some(function (p) { return Math.abs(p - s.time_sec) < 0.75; })) items.push({t: s.time_sec, on: false});
  });
  items.sort(function (a, b) { return a.t - b.t; });
  items.forEach(function (m) {
    var why = spReason(m.t), label = why ? why.label : '';
    marks += '<button type="button" class="sp-mark' + (m.on ? ' on' : ' sug') + '" data-t="' + m.t + '" style="left:' + pct(m.t) + '" aria-label="' +
      (m.on ? 'Split at ' + spFmt(m.t) + (label ? ', ' + escapeHtml(label) : '') + '. Activate to remove it.' : 'Suggested split at ' + spFmt(m.t) + ', ' + escapeHtml(label) + '. Activate to use it.') + '"><i></i></button>';
  });
  root.style.setProperty('--lanes', lanes.length);
  root.innerHTML = laneMarkup + '<div class="sp-marks" id="split-marks">' + marks + '<span class="sp-ghost" id="split-ghost" hidden></span></div><div class="axis" aria-hidden="true">' + ticks + '</div>';
  var last = -Infinity;
  root.querySelectorAll('.axis span').forEach(function (span) { var r = span.getBoundingClientRect(); if (r.left < last + 8) span.hidden = true; else last = r.right; });
}
function spRenderSuggestions() {
  var list = document.getElementById('split-sugs'), chosen = sp.points;
  document.getElementById('split-use-all').hidden = !sp.suggestions.length;
  if (!sp.suggestions.length) {
    list.innerHTML = '<li class="sp-none">' + (sp.ai.status === 'queued' || sp.ai.status === 'running' ? 'Looking for topic changes…' : 'No obvious split points. Click the timeline or a transcript line to add your own.') + '</li>';
    return;
  }
  list.innerHTML = sp.suggestions.map(function (s, i) {
    var used = chosen.some(function (p) { return Math.abs(p - s.time_sec) < 0.75; });
    return '<li class="sp-sug' + (used ? ' used' : '') + '" data-i="' + i + '"><span class="sp-time">' + spFmt(s.time_sec) + '</span><span class="sp-sug-main"><span class="sp-sug-label">' + escapeHtml(s.label) + '</span><span class="sp-sug-why">' + escapeHtml(s.reason) + '</span></span><span class="sp-conf">' + escapeHtml(s.confidence.charAt(0).toUpperCase() + s.confidence.slice(1)) + ' confidence</span><button type="button" class="btn secondary sm" data-use="' + i + '">' + (used ? 'Remove' : 'Use') + '</button></li>';
  }).join('');
}
function spRenderParts() {
  var parts = spParts(), root = document.getElementById('split-parts'), title = sp.name;
  root.innerHTML = parts.map(function (p) {
    var key = String(p.start), value = sp.names[key] || '', short = p.len < SP_MIN;
    return '<li class="sp-part' + (short ? ' bad' : '') + '" data-key="' + key + '"><span class="sp-num">' + (p.i + 1) + '</span><span class="sp-name"><input type="text" maxlength="200" autocomplete="off" value="' + escapeHtml(value) + '" placeholder="' + escapeHtml(title + ' (part ' + (p.i + 1) + ')') + '" aria-label="Name of part ' + (p.i + 1) + '"></span><span class="sp-range">' + spFmt(p.start) + ' to ' + spFmt(p.end) + '</span><span class="sp-len">' + spFmt(p.len) + (short ? ' too short' : '') + '</span>' +
      (p.i ? '<button type="button" class="btn ghost sm icon-only sp-rm" data-rm="' + p.start + '" aria-label="Remove the split at ' + spFmt(p.start) + '" title="Remove this split point">' + icon('x', 14) + '</button>' : '<span class="sp-rm-gap"></span>') + '</li>';
  }).join('');
}
function spRenderLines() {
  var root = document.getElementById('split-lines'), pts = spSorted(), k = 0, html = '';
  sp.lines.forEach(function (s) {
    while (k < pts.length && pts[k] <= s.start) { html += '<div class="sp-cut">Split at ' + spFmt(pts[k]) + '</div>'; k++; }
    html += '<button type="button" class="sp-line" data-start="' + s.start + '"><span class="ts">' + spFmt(s.start) + '</span><span class="lb">' + escapeHtml(segLane(s)) + '</span><span class="tx">' + escapeHtml(s.text) + '</span></button>';
  });
  while (k < pts.length) { html += '<div class="sp-cut">Split at ' + spFmt(pts[k]) + '</div>'; k++; }
  var scroll = root.scrollTop;
  root.innerHTML = html || '<p class="sp-none">No transcript lines.</p>';
  root.scrollTop = scroll;
}
function spApplySuggestions(data) {
  sp.suggestions = data.suggestions || [];
  sp.ai = data.ai || {status: 'none'};
  var btn = document.getElementById('split-ai'), label = btn.querySelector('span');
  btn.hidden = !data.ai_available;
  var working = sp.ai.status === 'queued' || sp.ai.status === 'running';
  btn.disabled = working;
  label.textContent = working ? 'Looking for topics…' : (sp.ai.status === 'done' ? 'Suggest again' : 'Suggest with AI');
  if (sp.ai.status === 'error') spInfo('AI suggestions failed: ' + (sp.ai.error || 'the AI worker reported an error') + '.', 'warn');
}
function spPollAi() {
  clearTimeout(sp && sp.pollTimer);
  if (!sp || !(sp.ai.status === 'queued' || sp.ai.status === 'running')) return;
  var id = sp.id;
  sp.pollTimer = setTimeout(function () {
    if (!sp || sp.id !== id) return;
    fetch('/v1/sessions/' + encodeURIComponent(id) + '/split-suggestions', {credentials: 'same-origin'}).then(function (r) { return r.ok ? r.json() : null; }).then(function (data) {
      if (!sp || sp.id !== id || !data) return;
      var before = sp.ai.status;
      spApplySuggestions(data);
      spRender();
      if (before !== sp.ai.status && sp.ai.status === 'done') spInfo(sp.suggestions.some(function (s) { return s.kind === 'topic'; }) ? 'AI found topic changes. They are marked on the timeline.' : 'AI found no clear topic changes.');
      spPollAi();
    }).catch(function () { spPollAi(); });
  }, 3000);
}
function spErr(message) { document.getElementById('split-error').textContent = message || ''; }
function openSplit(id) {
  if (!id) return;
  sp = {id: id, total: 0, segments: [], lines: [], points: [], suggestions: [], names: {}, ai: {status: 'none'}, name: (rowInfo[id] || {}).name || 'Meeting', busy: false, pollTimer: null};
  spErr(''); spInfo('');
  document.getElementById('split-lead').textContent = 'Break this recording into separate meetings. Each part keeps its own audio and transcript; nothing is retranscribed.';
  document.getElementById('split-tl').innerHTML = '<span class="sr-only">Loading…</span><div class="notes-loading" aria-hidden="true"><span class="skel-bar"></span><span class="skel-bar" style="width:70%"></span></div>';
  document.getElementById('split-sugs').innerHTML = '';
  document.getElementById('split-parts').innerHTML = '';
  document.getElementById('split-lines').innerHTML = '';
  document.getElementById('split-go').disabled = true;
  document.getElementById('split-regen').checked = !!aiEnabled;
  document.getElementById('split-regen-row').hidden = !aiEnabled;
  document.getElementById('split-ai').hidden = true;
  spDlg.returnValue = '';
  spDlg.showModal();
  document.getElementById('split-title').focus();
  var me = sp;
  Promise.all([
    fetch('/v1/sessions/' + encodeURIComponent(id), {credentials: 'same-origin'}).then(function (r) { if (!r.ok) throw new Error('Could not load this meeting.'); return r.json(); }),
    fetch('/v1/sessions/' + encodeURIComponent(id) + '/split-suggestions', {credentials: 'same-origin'}).then(function (r) {
      return r.json().then(function (body) { if (!r.ok) throw new Error(body.detail || 'Could not get split suggestions.'); return body; });
    })
  ]).then(function (res) {
    if (sp !== me) return;
    var detail = res[0], data = res[1];
    sp.name = (detail.meta || {}).name || sp.name;
    sp.segments = (detail.segments || []).filter(function (s) { return validSpan(s); });
    sp.lines = sp.segments.filter(function (s) { return !s.in_gap && String(s.text || '').trim(); });
    sp.total = Number(data.duration_sec) || Number((detail.meta || {}).duration_sec) || 0;
    document.getElementById('split-title').textContent = 'Split ' + sp.name;
    spApplySuggestions(data);
    spRender();
    spPollAi();
  }).catch(function (e) {
    if (sp !== me) return;
    spDlg.close('cancel');
    notify(e.message || 'Could not open the split dialog.', 'error');
  });
}
function spClose() { if (sp) clearTimeout(sp.pollTimer); spDlg.close('cancel'); }
function spSubmit() {
  if (!sp || sp.busy) return;
  var parts = spParts(), id = sp.id;
  var names = parts.map(function (p) { var v = (sp.names[String(p.start)] || '').trim(); return v || null; });
  sp.busy = true; spRenderFoot(); spErr('');
  var go = document.getElementById('split-go'); go.textContent = 'Splitting…';
  fetch('/v1/sessions/' + encodeURIComponent(id) + '/split', {method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({points: spSorted(), names: names, regenerate_notes: !!aiEnabled && document.getElementById('split-regen').checked})})
    .then(function (r) { return r.json().then(function (body) { if (!r.ok) throw new Error(body.detail || 'Could not split this meeting.'); return body; }); })
    .then(function (body) {
      spClose();
      if (currentSession === id) closeOverlay();
      var n = body.parts.length;
      return loadRows(true).then(function () {
        notify('Split into ' + n + ' meetings', '', {label: 'Undo', onClick: function () { undoSplit(id); }});
      });
    })
    .catch(function (e) { if (sp) { sp.busy = false; spRenderFoot(); spErr(e.message); } });
}
function undoSplit(id) {
  fetch('/v1/sessions/' + encodeURIComponent(id) + '/unsplit', {method: 'POST', credentials: 'same-origin'})
    .then(function (r) { return r.json().then(function (b) { if (!r.ok) throw new Error(b.detail || 'Could not undo the split.'); return b; }); })
    .then(function () { return loadRows(true).then(function () { notify('Split undone. The original meeting is back.'); }); })
    .catch(function (e) { notify(e.message + ' You can restore it from Recently deleted.', 'error'); });
}
document.getElementById('split-meeting').onclick = function () { if (currentSession) openSplit(currentSession); };
spDlg.addEventListener('keydown', function (e) { e.stopPropagation(); });
spDlg.addEventListener('close', function () { if (sp) { clearTimeout(sp.pollTimer); } });
spDlg.addEventListener('click', function (e) { if (e.target === spDlg) spClose(); });
document.getElementById('split-cancel').onclick = spClose;
document.getElementById('split-go').onclick = spSubmit;
document.getElementById('split-add').onclick = function () {
  var t = spParseTime(document.getElementById('split-time').value);
  if (t === null) { spInfo('Enter a time like 12:30 or 1:05:00.', 'warn'); return; }
  if (spAddPoint(t, 'typed')) document.getElementById('split-time').value = '';
};
document.getElementById('split-time').addEventListener('keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); document.getElementById('split-add').click(); } });
document.getElementById('split-ai').onclick = function () {
  if (!sp) return;
  var id = sp.id, force = sp.ai.status === 'done' || sp.ai.status === 'error';
  fetch('/v1/sessions/' + encodeURIComponent(id) + '/split-suggestions/ai' + (force ? '?force=true' : ''), {method: 'POST', credentials: 'same-origin'})
    .then(function (r) { return r.json().then(function (b) { if (!r.ok) throw new Error(b.detail || 'Could not start AI suggestions.'); return b; }); })
    .then(function (ai) {
      if (!sp || sp.id !== id) return;
      sp.ai = ai; spApplySuggestions({suggestions: sp.suggestions, ai: ai, ai_available: true}); spRenderSuggestions(); spPollAi();
      spInfo('Looking for topic changes. This can take a minute.');
    })
    .catch(function (e) { spInfo(e.message, 'warn'); });
};
document.getElementById('split-use-all').onclick = function () {
  if (!sp) return;
  var added = 0;
  sp.suggestions.forEach(function (s) { if (!sp.points.some(function (p) { return Math.abs(p - s.time_sec) < 0.75; }) && spAddPoint(s.time_sec, s.label)) added++; });
  spInfo(added ? 'Added ' + added + ' suggested split point' + (added === 1 ? '' : 's') + '.' : 'Nothing more could be added.');
};
document.getElementById('split-sugs').addEventListener('click', function (e) {
  var btn = e.target.closest('[data-use]'); if (!btn || !sp) return;
  var s = sp.suggestions[Number(btn.dataset.use)];
  if (!s) return;
  var existing = sp.points.filter(function (p) { return Math.abs(p - s.time_sec) < 0.75; })[0];
  if (existing !== undefined) spRemovePoint(existing); else spAddPoint(s.time_sec, s.label);
});
document.getElementById('split-sugs').addEventListener('mouseover', function (e) {
  var li = e.target.closest('.sp-sug'); if (li && sp) { var s = sp.suggestions[Number(li.dataset.i)]; if (s) spInfo(spFmt(s.time_sec) + ' · ' + s.reason); }
});
document.getElementById('split-parts').addEventListener('input', function (e) {
  var li = e.target.closest('.sp-part'); if (li && sp) sp.names[li.dataset.key] = e.target.value;
});
document.getElementById('split-parts').addEventListener('click', function (e) {
  var btn = e.target.closest('[data-rm]'); if (btn && sp) spRemovePoint(Number(btn.dataset.rm));
});
document.getElementById('split-lines').addEventListener('click', function (e) {
  var line = e.target.closest('.sp-line'); if (line && sp) spAddPoint(Number(line.dataset.start), 'transcript line');
});
(function () {
  var tl = document.getElementById('split-tl');
  function plot() { return document.getElementById('split-marks'); }
  function timeAt(e) { var r = plot().getBoundingClientRect(); return Math.min(Math.max((e.clientX - r.left) / r.width, 0), 1) * sp.total; }
  tl.addEventListener('click', function (e) {
    if (!sp || !plot()) return;
    var mark = e.target.closest('.sp-mark');
    if (mark) {
      var t = Number(mark.dataset.t);
      if (mark.classList.contains('on')) spRemovePoint(t); else spAddPoint(t, (spReason(t) || {}).label);
      return;
    }
    if (e.target.closest('.sp-marks, .lane')) spAddPoint(timeAt(e), 'timeline');
  });
  tl.addEventListener('mousemove', function (e) {
    if (!sp || !plot()) return;
    var ghost = document.getElementById('split-ghost'), r = plot().getBoundingClientRect();
    if (e.target.closest('.sp-mark') || e.clientX < r.left || e.clientX > r.right) { ghost.hidden = true; return; }
    ghost.hidden = false; ghost.style.left = ((e.clientX - r.left) / r.width * 100) + '%'; ghost.dataset.label = spFmt(timeAt(e));
  });
  tl.addEventListener('mouseleave', function () { var g = document.getElementById('split-ghost'); if (g) g.hidden = true; });
  function describe(e) {
    var mark = e.target.closest && e.target.closest('.sp-mark'); if (!mark || !sp) return;
    var t = Number(mark.dataset.t), why = spReason(t);
    spInfo(spFmt(t) + (why ? ' · ' + why.reason : (mark.classList.contains('on') ? ' · Your split point. Click to remove it.' : '')));
  }
  tl.addEventListener('mouseover', describe);
  tl.addEventListener('focusin', describe);
  var resize; window.addEventListener('resize', function () { clearTimeout(resize); resize = setTimeout(function () { if (sp && spDlg.open && sp.total) spRenderTimeline(); }, 150); });
})();

/* Combine */
function ensureRows(ids) {
  return Promise.all(ids.map(function (id) {
    var row = rowInfo[id];
    if (row && row.created != null && row.duration_sec != null) return Promise.resolve(row);
    return fetch('/v1/sessions/' + encodeURIComponent(id), {credentials: 'same-origin'}).then(function (r) { if (!r.ok) throw new Error('Could not load a selected meeting.'); return r.json(); }).then(function (d) {
      var m = d.meta || {};
      rowInfo[id] = Object.assign({session_id: id, name: m.name || id, created: m.started_wall || m.created, duration_sec: m.duration_sec, has_audio: d.has_audio, pipeline: d.pipeline, device: m.device}, rowInfo[id] || {});
      return rowInfo[id];
    });
  }));
}
function cbGapText(sec) {
  if (sec < 1) return {text: sec < -1 ? 'Overlaps the next recording by ' + fmtDuration(-sec) + '. No silence is added.' : 'Back to back. No silence is added.', cls: ''};
  if (sec > CB_CAP) return {text: fmtDuration(sec) + ' apart. Shortened to ' + fmtDuration(CB_CAP) + ' of silence, marked as a gap.', cls: 'cap'};
  return {text: fmtDuration(sec) + ' of silence is added, marked as a gap.', cls: ''};
}
function cbRender() {
  var list = document.getElementById('combine-list'), html = '', problems = [];
  cb.rows.forEach(function (row, i) {
    var d = Math.max(0, Number(row.duration_sec) || 0), bad = '';
    if (processingState(row).key !== 'complete') bad = (row.name || row.session_id) + ' has not finished transcribing.';
    else if (!row.has_audio) bad = 'Audio was deleted for ' + (row.name || row.session_id) + '; combining needs audio.';
    if (bad) problems.push(bad);
    html += '<li class="cb-item' + (bad ? ' bad' : '') + '"><span class="cb-num">' + (i + 1) + '</span><span class="cb-main"><span class="cb-name">' + escapeHtml(row.name || row.session_id) + '</span><span class="cb-meta">' + fmtDate(row.created, true) + ' · ' + (d ? fmtDuration(d) : 'unknown length') + (row.device ? ' · ' + escapeHtml(row.device) : '') + '</span>' + (bad ? '<span class="cb-bad">' + escapeHtml(bad) + '</span>' : '') + '</span></li>';
    var next = cb.rows[i + 1];
    if (next) {
      var gap = cbGapText(Number(next.created) - (Number(row.created) + d));
      html += '<li class="cb-gap' + (gap.cls ? ' ' + gap.cls : '') + '" aria-hidden="false">' + gap.text + '</li>';
    }
  });
  list.innerHTML = html;
  cb.problems = problems;
  var go = document.getElementById('combine-go');
  go.textContent = 'Combine ' + cb.rows.length + ' meetings';
  go.disabled = problems.length > 0 || cb.busy;
  document.getElementById('combine-error').textContent = cb.error || problems[0] || '';
}
function openCombine(ids) {
  if (!ids || ids.length < 2) return;
  ensureRows(ids).then(function (rows) {
    rows = rows.slice().sort(function (a, b) { return (Number(a.created) - Number(b.created)) || String(a.session_id).localeCompare(String(b.session_id)); });
    cb = {rows: rows, busy: false, error: '', problems: []};
    document.getElementById('combine-name').value = rows[0].name || '';
    document.getElementById('combine-regen').checked = !!aiEnabled;
    document.getElementById('combine-regen-row').hidden = !aiEnabled;
    cbRender();
    cbDlg.returnValue = '';
    cbDlg.showModal();
    document.getElementById('combine-title').focus();
  }).catch(function (e) { notify(e.message, 'error'); });
}
function cbSubmit() {
  if (!cb || cb.busy) return;
  var ids = cb.rows.map(function (r) { return r.session_id; }), name = document.getElementById('combine-name').value.trim();
  cb.busy = true; cb.error = ''; cbRender();
  document.getElementById('combine-go').textContent = 'Combining…';
  fetch('/v1/sessions/combine', {method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ids: ids, name: name || undefined, regenerate_notes: !!aiEnabled && document.getElementById('combine-regen').checked})})
    .then(function (r) { return r.json().then(function (b) { if (!r.ok) throw new Error(b.detail || 'Could not combine these meetings.'); return b; }); })
    .then(function (body) {
      cbDlg.close('ok');
      if (currentSession && ids.indexOf(currentSession) >= 0) closeOverlay();
      document.querySelectorAll('.row-select').forEach(function (box) { box.checked = false; });
      return loadRows(true).then(function () {
        notify('Combined ' + ids.length + ' meetings into ' + body.name, '', {label: 'Undo', onClick: function () { undoCombine(body.session_id); }});
      });
    })
    .catch(function (e) { cb.busy = false; cb.error = e.message; cbRender(); });
}
function undoCombine(id) {
  fetch('/v1/sessions/' + encodeURIComponent(id) + '/uncombine', {method: 'POST', credentials: 'same-origin'})
    .then(function (r) { return r.json().then(function (b) { if (!r.ok) throw new Error(b.detail || 'Could not undo the combine.'); return b; }); })
    .then(function () { return loadRows(true).then(function () { notify('Combine undone. The original meetings are back.'); }); })
    .catch(function (e) { notify(e.message + ' You can restore them from Recently deleted.', 'error'); });
}
cbDlg.addEventListener('keydown', function (e) { e.stopPropagation(); });
cbDlg.addEventListener('click', function (e) { if (e.target === cbDlg) cbDlg.close('cancel'); });
document.getElementById('combine-cancel').onclick = function () { cbDlg.close('cancel'); };
document.getElementById('combine-go').onclick = cbSubmit;
document.getElementById('bulk-combine').onclick = function () { openCombine(selectedIds()); };
/* Why the current selection cannot be combined ('' when it can). Mirrors the server's checks in plan_combine. */
function combineBlocker(ids) {
  if (ids.length < 2) return 'Select two or more meetings to combine';
  if (ids.length > CB_MAX) return 'At most ' + CB_MAX + ' meetings can be combined at once';
  for (var i = 0; i < ids.length; i++) {
    var row = rowInfo[ids[i]];
    if (!row) continue;
    var label = row.name || row.session_id;
    if (processingState(row).key !== 'complete') return label + ' has not finished transcribing';
    if (!row.has_audio) return 'Audio was deleted for ' + label + '; combining needs audio';
  }
  return '';
}
(function () {
  var baseUpdate = updateSelection;
  updateSelection = function () {
    baseUpdate();
    var ids = selectedIds(), why = combineBlocker(ids), btn = document.getElementById('bulk-combine');
    btn.disabled = !!why;
    btn.title = why || 'Combine the selected meetings into one';
    if (ids.length >= 2 && why) document.getElementById('selection-count').textContent = ids.length + ' selected. Cannot combine: ' + why + '.';
  };
  updateSelection();
})();

/* Combine from the meeting's ••• menu: pick the other meeting(s), then hand over to the combine dialog above. */
var pkDlg = document.getElementById('pick-dialog'), pk = {rows: [], chosen: [], q: '', seq: 0};
var PK_NEAR_SEC = 2 * 3600;
function pickBlocker() {
  var row = currentSession && rowInfo[currentSession];
  if (!row) return '';
  if (processingState(row).key !== 'complete') return 'This meeting has not finished transcribing';
  if (!row.has_audio) return 'Audio was deleted for this meeting; combining needs audio';
  return '';
}
function syncCombineItem() {
  var b = document.getElementById('combine-meeting'), why = pickBlocker();
  b.disabled = !!why;
  b.title = why || 'Join this meeting with one or more others into a single meeting';
}
moreBtn.addEventListener('click', syncCombineItem, true);
function pkGap(a, b) { // seconds between two meetings (0 when they overlap)
  var aS = Number(a.created), aE = aS + (Number(a.duration_sec) || 0), bS = Number(b.created), bE = bS + (Number(b.duration_sec) || 0);
  if (bS >= aE) return bS - aE;
  if (aS >= bE) return aS - bE;
  return 0;
}
function pkWhy(row) {
  if (processingState(row).key !== 'complete') return 'Not finished transcribing';
  if (!row.has_audio) return 'Audio was deleted';
  return '';
}
function pkRender() {
  var cur = rowInfo[currentSession] || {}, list = document.getElementById('pick-list'), html = '';
  var near = [], rest = [];
  pk.rows.forEach(function (r) {
    if (r.session_id === currentSession) return;
    var gap = pkGap(cur, r), sug = r.session_id === contState.other;
    if (sug || gap <= PK_NEAR_SEC) near.push({row: r, gap: gap, sug: sug}); else rest.push({row: r, gap: gap, sug: false});
  });
  near.sort(function (a, b) { return (b.sug - a.sug) || (a.gap - b.gap); });
  rest.sort(function (a, b) { return Number(b.row.created) - Number(a.row.created); });
  function item(e) {
    var r = e.row, id = r.session_id, bad = pkWhy(r), on = pk.chosen.indexOf(id) >= 0;
    var d = Math.max(0, Number(r.duration_sec) || 0);
    var tags = (e.sug ? '<span class="pk-tag sug">Suggested</span>' : '') + (!e.sug && e.gap <= PK_NEAR_SEC ? '<span class="pk-tag">' + (e.gap < 60 ? 'Right next to this one' : fmtDuration(e.gap) + ' apart') + '</span>' : '');
    return '<li class="pk-item' + (bad ? ' bad' : '') + (on ? ' on' : '') + '"><label><input type="checkbox" class="pk-check" value="' + escapeHtml(id) + '"' + (on ? ' checked' : '') + (bad ? ' disabled' : '') + '>'
      + '<span class="pk-main"><span class="pk-name">' + escapeHtml(r.name || id) + '</span><span class="pk-meta">' + fmtDate(r.created, true) + (d ? ' · ' + fmtDuration(d) : '') + (bad ? ' · ' + bad : '') + '</span></span>'
      + (tags ? '<span class="pk-tags">' + tags + '</span>' : '') + '</label></li>';
  }
  if (near.length) html += '<li class="pk-head" role="presentation">Close in time</li>' + near.map(item).join('');
  if (rest.length) html += '<li class="pk-head" role="presentation">' + (near.length ? 'Other meetings, newest first' : 'Meetings, newest first') + '</li>' + rest.map(item).join('');
  if (!html) html = '<li class="pk-empty">' + (pk.q ? 'No meetings match that search.' : 'There are no other meetings yet.') + '</li>';
  list.innerHTML = html;
  var go = document.getElementById('pick-go'), n = pk.chosen.length;
  go.disabled = n < 1;
  go.textContent = n < 1 ? 'Choose a meeting' : (n === 1 ? 'Continue with 1 meeting' : 'Continue with ' + n + ' meetings');
}
function pkLoad() {
  var ticket = ++pk.seq, url = '/v1/sessions?page=1&per_page=200' + (pk.q ? '&q=' + encodeURIComponent(pk.q) : '');
  fetch(url, {credentials: 'same-origin'}).then(function (r) { if (!r.ok) throw new Error('Could not load meetings.'); return r.json(); }).then(function (d) {
    if (ticket !== pk.seq) return;
    pk.rows = d.items || [];
    pk.rows.forEach(function (r) { rowInfo[r.session_id] = Object.assign(rowInfo[r.session_id] || {}, r); });
    document.getElementById('pick-error').textContent = '';
    pkRender();
  }).catch(function (e) { if (ticket === pk.seq) document.getElementById('pick-error').textContent = e.message; });
}
function openPicker() {
  var why = pickBlocker();
  if (!currentSession || why) { if (why) notify(why + '.', 'error'); return; }
  var cur = rowInfo[currentSession] || {};
  pk = {rows: [], chosen: [], q: '', seq: pk.seq};
  document.getElementById('pick-q').value = '';
  document.getElementById('pick-lead').innerHTML = 'Choose the meeting or meetings to join with <strong>' + escapeHtml(cur.name || 'this meeting') + '</strong>. Meetings close in time are listed first.';
  document.getElementById('pick-list').innerHTML = '<li class="pk-empty">Loading meetings…</li>';
  pkRender();
  document.getElementById('pick-list').innerHTML = '<li class="pk-empty">Loading meetings…</li>';
  pkDlg.returnValue = '';
  pkDlg.showModal();
  document.getElementById('pick-title').focus();
  pkLoad();
}
document.getElementById('combine-meeting').onclick = openPicker;
document.getElementById('pick-list').addEventListener('change', function (e) {
  var box = e.target.closest('.pk-check');
  if (!box) return;
  var i = pk.chosen.indexOf(box.value);
  if (box.checked && i < 0) {
    if (pk.chosen.length >= CB_MAX - 1) { box.checked = false; document.getElementById('pick-error').textContent = 'At most ' + CB_MAX + ' meetings can be combined at once.'; return; }
    pk.chosen.push(box.value);
  } else if (!box.checked && i >= 0) pk.chosen.splice(i, 1);
  document.getElementById('pick-error').textContent = '';
  box.closest('.pk-item').classList.toggle('on', box.checked);
  var n = pk.chosen.length, go = document.getElementById('pick-go');
  go.disabled = n < 1;
  go.textContent = n < 1 ? 'Choose a meeting' : (n === 1 ? 'Continue with 1 meeting' : 'Continue with ' + n + ' meetings');
});
var pkTimer = null;
document.getElementById('pick-q').addEventListener('input', function () {
  var v = this.value.trim();
  clearTimeout(pkTimer);
  pkTimer = setTimeout(function () { pk.q = v; pkLoad(); }, 250);
});
document.getElementById('pick-cancel').onclick = function () { pkDlg.close('cancel'); };
document.getElementById('pick-go').onclick = function () {
  if (!currentSession || !pk.chosen.length) return;
  var ids = [currentSession].concat(pk.chosen);
  pkDlg.close('ok');
  openCombine(ids);
};
pkDlg.addEventListener('keydown', function (e) { e.stopPropagation(); });
pkDlg.addEventListener('click', function (e) { if (e.target === pkDlg) pkDlg.close('cancel'); });

/* "Looks like a continuation" hint on an open meeting */
var contState = {id: null, other: null};
function contKey(a, b) { return [a, b].sort().join('|'); }
function contDismissed(key) { try { return (JSON.parse(localStorage.getItem('mn-cont-dismissed') || '[]')).indexOf(key) >= 0; } catch (_) { return false; } }
function contDismiss(key) { try { var list = JSON.parse(localStorage.getItem('mn-cont-dismissed') || '[]'); if (list.indexOf(key) < 0) list.push(key); localStorage.setItem('mn-cont-dismissed', JSON.stringify(list.slice(-200))); } catch (_) {} }
function contGap(sec) { return sec < 90 ? Math.max(1, Math.round(sec)) + ' seconds' : Math.round(sec / 60) + ' minutes'; }
function contLoad(id) {
  if (contState.id === id) return;
  contState.id = id; contState.other = null;
  var hint = document.getElementById('continue-hint'); hint.hidden = true;
  fetch('/v1/sessions/' + encodeURIComponent(id) + '/continuations', {credentials: 'same-origin'}).then(function (r) { return r.ok ? r.json() : null; }).then(function (d) {
    if (!d || currentSession !== id || contState.id !== id) return;
    var prev = d.previous, next = d.next, other = prev || next;
    if (!other || contDismissed(contKey(id, other.session_id))) return;
    contState.other = other.session_id;
    var name = '<strong>' + escapeHtml(other.name) + '</strong>';
    document.getElementById('continue-text').innerHTML = prev
      ? 'Looks like a continuation of ' + name + ', which ended ' + contGap(prev.gap_sec) + ' before this one started.'
      : 'Looks like ' + name + ' continues this meeting. It started ' + contGap(next.gap_sec) + ' after this one ended.';
    hint.hidden = false;
  }).catch(function () {});
}
document.getElementById('continue-combine').onclick = function () { if (currentSession && contState.other) openCombine([contState.other, currentSession]); };
document.getElementById('continue-dismiss').onclick = function () {
  if (currentSession && contState.other) contDismiss(contKey(currentSession, contState.other));
  document.getElementById('continue-hint').hidden = true;
};
(function () {
  var baseOpen = openSession, baseClose = closeOverlay;
  openSession = function (id) { var out = baseOpen.apply(this, arguments); contLoad(id); return out; };
  closeOverlay = function () { contState.id = null; contState.other = null; document.getElementById('continue-hint').hidden = true; return baseClose.apply(this, arguments); };
  document.getElementById('close-overlay').onclick = closeOverlay;
})();
"""
