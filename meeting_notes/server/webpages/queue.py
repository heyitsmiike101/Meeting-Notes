"""The Queue page."""

from __future__ import annotations

from .gpu import _GPU_HELPERS_JS
from .shared import _JS_HELPERS
from .shell import _shell


# -- Queue --------------------------------------------------------------------------------------------------------

_QUEUE_JS = r"""
var Q = {fetchedAt: 0, timer: null, busy: false};
function $(id) { return document.getElementById(id); }
function qDur(sec) {
  sec = Math.max(0, Math.round(sec || 0));
  if (sec < 60) return sec + ' s';
  var m = Math.floor(sec / 60), s = sec % 60;
  if (m < 60) return m + ' min' + (m < 10 ? ' ' + (s < 10 ? '0' : '') + s + ' s' : '');
  var h = Math.floor(m / 60); m = m % 60;
  return h + ' h ' + (m < 10 ? '0' : '') + m + ' min';
}
function qTime(base, cls) {
  return '<span class="' + cls + '" data-base="' + Math.round(base) + '">' + escapeHtml(qDur(base)) + '</span>';
}
function qTick() {
  var extra = Q.fetchedAt ? (Date.now() - Q.fetchedAt) / 1000 : 0;
  document.querySelectorAll('#queue-page [data-base]').forEach(function (el) { el.textContent = qDur(Number(el.dataset.base) + extra); });
}
function qMeeting(r) {
  var t = escapeHtml(r.title || r.board || 'Meeting');
  var link = r.session_id ? '<a class="mrow-title" href="/sessions/' + encodeURIComponent(r.session_id) + '">' + t + '</a>' : '<span class="mrow-title">' + t + '</span>';
  return link + (r.board && r.board !== r.title ? '<span class="qsub">' + escapeHtml(r.board) + '</span>' : '');
}
function qStep(r) {
  var detail = r.status === 'running' && r.step ? r.step : (r.template_name ? r.template_name : '');
  var follow = r.notes_follow ? 'Notes start after this' : '';
  return '<span class="qstep">' + escapeHtml(r.pipeline_step || r.kind_label || '') + '</span>'
    + [detail, follow].filter(Boolean).map(function (t) { return '<span class="qsub">' + escapeHtml(t) + '</span>'; }).join('');
}
function qStatus(r) {
  var cls = r.status === 'running' ? 'running' : (r.status === 'failed' ? 'error' : 'none');
  var pos = r.status === 'waiting' && r.position ? ' #' + r.position : '';
  return badge(cls, (r.status_label || '') + pos, r.reason || r.error || '');
}
function qBar(pct) {
  var f = pct == null ? 0.05 : Math.max(0.02, Math.min(1, pct / 100));
  return '<div class="progress-track" role="presentation"><i style="transform:scaleX(' + f.toFixed(3) + ')"></i></div>';
}
function qProgress(r) {
  if (r.status !== 'running') return '<span class="cell-none">&mdash;</span>';
  return '<span class="qpct">' + (r.percent == null ? 'Starting' : Math.round(r.percent) + '%') + '</span>' + qBar(r.percent);
}
function qSince(r) {
  if (r.status === 'running') return r.elapsed_seconds == null ? '&mdash;' : 'Started ' + qTime(r.elapsed_seconds, 'qel') + ' ago';
  if (r.status === 'waiting') return 'Waiting ' + qTime(r.waited_seconds, 'qel');
  return '';
}
function qRow(r) {
  var why = r.status === 'waiting' && r.reason ? '<span class="qsub qwhy">' + escapeHtml(r.reason) + '</span>' : '';
  return '<tr><td class="q-meeting">' + qMeeting(r) + '</td><td>' + qStep(r) + '</td><td>' + qStatus(r) + why + '</td>'
    + '<td>' + escapeHtml(r.where || '') + '</td><td class="q-progress">' + qProgress(r) + '</td><td class="qtime">' + qSince(r) + '</td></tr>';
}
function failureRow(r) {
  return '<tr><td class="q-meeting">' + qMeeting(r) + '</td><td>' + qStep(r) + '</td><td>' + badge('error', 'Failed') + '</td>'
    + '<td class="qerr">' + escapeHtml(r.error || 'No reason recorded') + '</td><td>' + (r.failed_at ? escapeHtml(agoText(r.failed_at)) : '') + '</td></tr>';
}
function helperRow(h) {
  var st = gpuHelperStatus(h), busy = h.online && h.state === 'busy';
  var job = busy ? escapeHtml(h.current_board || 'A meeting') + (h.progress == null ? '' : ' &middot; ' + Math.round(h.progress * 100) + '%') : '<span class="cell-none">&mdash;</span>';
  return '<tr><td class="k-name">' + escapeHtml(h.name || 'Unnamed computer') + '</td><td>' + escapeHtml(gpuHelperCard(h)) + '</td><td>'
    + badge(busy ? 'running' : st.cls, busy ? 'Working' : st.text, st.title) + '</td><td>' + job + '</td></tr>';
}
function renderQueue(d) {
  Q.fetchedAt = Date.now();
  var jobs = (d.running || []).concat(d.waiting || []), fails = d.failures || [], helpers = d.helpers || [];
  $('q-count').textContent = jobs.length ? '(' + jobs.length + ')' : '';
  $('q-jobs-wrap').hidden = !jobs.length; $('q-empty').hidden = jobs.length > 0;
  $('q-jobs').innerHTML = jobs.map(qRow).join('');
  $('q-order-hint').hidden = (d.waiting || []).length < 2;
  $('q-order-hint').textContent = d.order_hint || '';
  $('q-helper-count').textContent = helpers.length ? '(' + helpers.length + ')' : '';
  $('q-helper-wrap').hidden = !helpers.length; $('q-helper-empty').hidden = helpers.length > 0;
  $('q-helpers').innerHTML = helpers.map(helperRow).join('');
  $('q-fail-wrap').hidden = !fails.length;
  $('q-fail-count').textContent = fails.length ? '(' + fails.length + ')' : '';
  $('q-fail').innerHTML = fails.map(failureRow).join('');
  $('queue-error').hidden = true;
  $('queue-page').setAttribute('aria-busy', 'false');
  qTick();
}
function qLoad() {
  if (Q.busy) return Promise.resolve();
  Q.busy = true;
  return fetch('/v1/queue', {credentials: 'same-origin'}).then(function (r) {
    if (r.status === 401 || r.status === 403) { window.location = '/login'; throw new Error('Signed out'); }
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }).then(renderQueue).catch(function () { $('queue-error').hidden = false; }).then(function () { Q.busy = false; });
}
function qSchedule() {
  if (Q.timer) clearTimeout(Q.timer);
  Q.timer = setTimeout(function () { (document.hidden ? Promise.resolve() : qLoad()).then(qSchedule); }, 3000);
}
function qInit() {
  $('queue-retry').addEventListener('click', qLoad);
  document.addEventListener('visibilitychange', function () { if (!document.hidden) qLoad(); });
  setInterval(qTick, 1000);
  qLoad().then(qSchedule);
}
qInit();
"""


def render_queue_page(settings, *, token_configured: bool) -> str:
    """Queue: one table of the work running and waiting (in ingestion order: Transcribe, Identify speakers, Generate
    notes), the GPU helpers and recent failures.

    Rendered client-side from ``GET /v1/queue`` (polled every few seconds while visible). It has no settings of its
    own: where and when work runs is set under Settings > GPU helpers, the steps under Settings > Ingestion.
    """
    script = _JS_HELPERS + _GPU_HELPERS_JS + _QUEUE_JS
    body = f"""
<div class="page queue-page" id="queue-page" aria-busy="true">
<header class="page-head">
  <h1>Queue</h1>
</header>
<p class="help">Queue settings are in <a href="/settings#gpu-helpers-heading">Settings &rsaquo; GPU helpers</a>; the steps themselves are in <a href="/settings#ingestion">Settings &rsaquo; Ingestion</a>.</p>
<div class="list-error" id="queue-error" role="alert" hidden><span>Could not load the queue. Try again in a moment.</span><button type="button" class="btn secondary" id="queue-retry">Try again</button></div>

<section class="qsect" aria-labelledby="q-jobs-heading">
  <h2 class="sect-sub" id="q-jobs-heading">Work in the queue <span class="count" id="q-count"></span></h2>
  <p class="help" id="q-order-hint" hidden></p>
  <div class="table-wrap qtable" id="q-jobs-wrap" hidden>
    <table>
      <thead><tr><th scope="col">Meeting</th><th scope="col">Step</th><th scope="col">Status</th><th scope="col">Where</th><th scope="col">Progress</th><th scope="col">Time</th></tr></thead>
      <tbody id="q-jobs" aria-live="polite"></tbody>
    </table>
  </div>
  <p class="help qempty" id="q-empty">Nothing is running or waiting.</p>
</section>

<section class="qsect" aria-labelledby="q-helper-heading">
  <h2 class="sect-sub" id="q-helper-heading">GPU helpers <span class="count" id="q-helper-count"></span></h2>
  <div class="table-wrap qtable" id="q-helper-wrap" hidden>
    <table>
      <thead><tr><th scope="col">Name</th><th scope="col">GPU</th><th scope="col">State</th><th scope="col">Current job</th></tr></thead>
      <tbody id="q-helpers"></tbody>
    </table>
  </div>
  <p class="help qempty" id="q-helper-empty">No GPU helpers are connected. <a href="/settings#gpu-helpers-heading">Set up a helper in Settings</a>.</p>
</section>

<section class="qsect qfail" id="q-fail-wrap" aria-labelledby="q-fail-heading" hidden>
  <h2 class="sect-sub" id="q-fail-heading">Recent failures <span class="count" id="q-fail-count"></span></h2>
  <p class="help">Jobs that failed in the last 24 hours. A job replaced by a newer one is not listed.</p>
  <div class="table-wrap qtable">
    <table>
      <thead><tr><th scope="col">Meeting</th><th scope="col">Step</th><th scope="col">Status</th><th scope="col">Reason</th><th scope="col">When</th></tr></thead>
      <tbody id="q-fail"></tbody>
    </table>
  </div>
</section>
</div>
<script>
{script}</script>
"""
    return _shell("Queue", body, token_configured=token_configured, active="queue",
                  main_class="queue-page-main", appearance=settings.appearance)
