"""Inline scripts for GPU helper status on the Settings, Queue and Recorders pages."""

from __future__ import annotations


# Shared by Settings and Recorders: how a helper's status reads, and the helper rows. Needs _JS_HELPERS first.
_GPU_HELPERS_JS = r"""
function gpuHelperCard(h) {
  var g = h && h.gpu, name = g && typeof g === 'object' ? g.name : (g || '');
  return name || 'Not reported';
}
function gpuHelperStatus(h) {
  var pct = h.progress == null ? '' : ' · ' + Math.round(h.progress * 100) + '%';
  if (!h.online) return {cls: 'none', text: 'Offline'};
  if (h.state === 'busy') return {cls: 'running', text: 'Working on ' + (h.current_board || 'a meeting') + pct};
  if (h.paused || h.state === 'paused') return {cls: 'none', text: 'Paused'};
  if (h.state === 'error') return {cls: 'error', text: 'Error', title: h.last_error || ''};
  if (h.state === 'installing') return {cls: 'running', text: 'Installing GPU support'};
  return {cls: 'done', text: 'Idle'};
}
function gpuLiveText(h) {
  var l = h && h.live;
  if (!l || typeof l !== 'object') return '';
  if (!l.connected) return l.last_error ? 'Live: off · ' + String(l.last_error).slice(0, 80) : 'Live: off';
  var t = 'Live: on';
  if (l.avg_ms != null && (l.served === undefined || l.served > 0)) t += ' \u00b7 avg ' + (l.avg_ms / 1000).toFixed(1) + ' s';
  return t;
}
function gpuLastSeen(h) { return h.last_seen ? agoText(h.last_seen) : 'Never'; }
function gpuHelpersEmpty() {
  return '<div class="ledger-empty"><h3>No GPU helpers yet</h3><p>On a Windows PC with an NVIDIA graphics card, open Meeting Notes, go to Settings, then GPU helper, and turn it on. It appears here when it connects.</p></div>';
}
function gpuHelpersTable(list, revokable) {
  if (!list.length) return gpuHelpersEmpty();
  var rows = list.map(function (h) {
    var st = gpuHelperStatus(h);
    var act = revokable ? '<td class="k-act"><button type="button" class="btn danger sm row-btn" data-gpu-revoke="' + escapeHtml(h.helper_id) + '" data-name="' + escapeHtml(h.name) + '">Revoke<span class="sr-only"> ' + escapeHtml(h.name) + '</span></button></td>' : '';
    return '<tr>' +
      '<td class="k-name">' + escapeHtml(h.name || 'Unnamed computer') + '</td>' +
      '<td class="gpu-card" data-label="Graphics card">' + escapeHtml(gpuHelperCard(h)) + '</td>' +
      '<td class="gpu-status" data-label="Status">' + badge(st.cls, st.text, st.title) + (gpuLiveText(h) ? '<div class="gpu-live">' + escapeHtml(gpuLiveText(h)) + '</div>' : '') + '</td>' +
      '<td class="gpu-seen" data-label="Last seen">' + escapeHtml(gpuLastSeen(h)) + '</td>' + act + '</tr>';
  }).join('');
  return '<div class="ledger-wrap"><table class="ledger gpu-helpers"><thead><tr><th>Name</th><th>Graphics card</th><th>Status</th><th>Last seen</th>' + (revokable ? '<th><span class="sr-only">Actions</span></th>' : '') + '</tr></thead><tbody>' + rows + '</tbody></table></div>';
}
"""

# Settings > Transcription > GPU helpers: pack status, progress, and the connected helpers table.
_GPU_SETTINGS_JS = r"""
(function () {
  var box = document.getElementById('gpu-helpers-box');
  if (!box) return;
  var statusEl = document.getElementById('gpu-pack-status'), btn = document.getElementById('gpu-pack-btn'),
      barWrap = document.getElementById('gpu-pack-progress'), bar = barWrap.querySelector('i'),
      noteEl = document.getElementById('gpu-pack-note'), errEl = document.getElementById('gpu-pack-error');
  var packTimer = null, lastPack = null;
  function api(method, url) {
    return fetch(url, {method: method, credentials: 'same-origin'}).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (!r.ok) throw new Error(r.status === 401 || r.status === 403 ? 'Your sign-in has expired. Reload the page and sign in again.' : (data && typeof data.detail === 'string' && data.detail) || 'Something went wrong (HTTP ' + r.status + ').');
        return data;
      });
    }, function () { throw new Error('Could not reach the server. Check your connection and try again.'); });
  }
  function packLine(p) {
    var size = p.total_size ? fmtBytes(p.total_size) : '';
    if (p.state === 'preparing') {
      var step = p.step ? p.step.charAt(0).toUpperCase() + p.step.slice(1) : 'Starting';
      var done = p.bytes_total ? fmtBytes(p.bytes_done) + ' of ' + fmtBytes(p.bytes_total) : (p.bytes_done ? fmtBytes(p.bytes_done) : '');
      return 'Preparing — ' + step + (done ? ', ' + done : '');
    }
    if (p.state === 'error') return 'Error — ' + (p.error || 'the download could not be prepared');
    if (p.has_pack) {
      var detail = [];
      if (p.pack_version) detail.push('version ' + String(p.pack_version).slice(0, 12));
      if (size) detail.push(size);
      if (p.created) detail.push(fmtDate(p.created));
      return (p.out_of_date ? 'Out of date' : 'Ready') + (detail.length ? ' — ' + detail.join(', ') : '') + (p.out_of_date ? '. Refresh it so helpers get the current model and files.' : '');
    }
    return 'Not prepared';
  }
  function renderPack(p) {
    lastPack = p;
    statusEl.textContent = packLine(p);
    var busy = p.state === 'preparing';
    btn.disabled = busy;
    btn.querySelector('span').textContent = p.has_pack ? 'Refresh download' : 'Prepare download';
    barWrap.hidden = !busy;
    if (busy) {
      var frac = p.bytes_total ? Math.max(0.02, Math.min(1, p.bytes_done / p.bytes_total)) : 0.05;
      bar.style.transform = 'scaleX(' + frac + ')';
    }
    noteEl.textContent = p.note || '';
    noteEl.hidden = !p.note;
    if (busy) schedulePackPoll(); else if (packTimer) { clearTimeout(packTimer); packTimer = null; }
  }
  function schedulePackPoll() {
    if (packTimer) return;
    packTimer = setTimeout(function () {
      packTimer = null;
      api('GET', '/v1/compute/pack/status').then(function (p) {
        if (p.state !== 'preparing') { loadAll(); return; }  // finished: reload for the date and the note
        renderPack(Object.assign({}, lastPack || {}, p));
      }).catch(function () { schedulePackPoll(); });
    }, 2000);
  }
  function loadAll() {
    return api('GET', '/settings/gpu-helpers').then(function (d) {
      box.innerHTML = gpuHelpersTable(d.helpers || [], true);
      renderPack(d.pack || {});
    }).catch(function (e) {
      box.innerHTML = '<p class="error-text" role="alert">' + escapeHtml(e.message) + '</p><p><button type="button" class="btn secondary" data-gpu-retry>Try again</button></p>';
    });
  }
  btn.addEventListener('click', function () {
    errEl.hidden = true;
    btn.disabled = true;
    api('POST', '/v1/compute/pack/prepare').then(function (p) { renderPack(Object.assign({}, lastPack || {}, p)); }).catch(function (e) {
      errEl.textContent = e.message; errEl.hidden = false; btn.disabled = false;
    });
  });
  box.addEventListener('click', function (event) {
    if (event.target.closest('[data-gpu-retry]')) { loadAll(); return; }
    var b = event.target.closest('[data-gpu-revoke]');
    if (!b) return;
    var name = b.dataset.name || 'this helper';
    confirmDialog({title: 'Revoke "' + name + '"?', lead: 'This computer stops taking transcription work from this server right away. To use it again, turn the GPU helper off and on in the app on that computer.',
                   warning: 'Work it is doing now goes back to this server.', confirmLabel: 'Revoke helper', danger: true}).then(function (ok) {
      if (!ok) return;
      api('DELETE', '/v1/compute/helpers/' + encodeURIComponent(b.dataset.gpuRevoke)).then(function () {
        notify('Helper revoked.');
        loadAll();
      }).catch(function (e) { notify(e.message, 'error'); });
    });
  });
  setInterval(function () { if (!document.hidden) loadAll(); }, 10000);
  loadAll();
})();
"""

# Recorders page: the connected GPU helpers (a recorder cannot be matched to its helper by id).
_GPU_RECORDERS_JS = r"""
(function () {
  var box = document.getElementById('gpu-helpers-box');
  if (!box) return;
  function load() {
    fetch('/settings/gpu-helpers', {credentials: 'same-origin'}).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    }).then(function (d) {
      var list = d.helpers || [];
      box.innerHTML = gpuHelpersTable(list, false);
      var count = document.getElementById('gpu-helpers-count');
      if (count) count.textContent = list.length ? '(' + list.length + ')' : '';
    }).catch(function () {
      box.innerHTML = '<p class="help" role="status">GPU helpers could not be loaded.</p>';
    });
  }
  setInterval(function () { if (!document.hidden) load(); }, 10000);
  load();
})();
"""
