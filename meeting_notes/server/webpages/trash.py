"""The Recently deleted page."""

from __future__ import annotations

from meeting_notes.server.store import TRASH_RETENTION_DAYS
from .shared import _CONFIRM_DIALOG_HTML, _JS_HELPERS
from .shell import _icon, _shell


def render_trash_page(*, token_configured: bool, appearance: str = "system") -> str:
    """Recently deleted: meetings moved to trash, restorable for 30 days.

    Rendered client-side from ``GET /v1/trash``; restore is
    ``POST /v1/trash/{id}/restore``, permanent delete ``DELETE /v1/trash/{id}``,
    and Empty trash ``POST /v1/trash/empty``.
    """
    body = (
        f"""
<div class="page trash-page">
<header class="page-head">
  <a class="btn ghost" href="/meetings" id="trash-back">{_icon("back")}<span>Meetings</span></a>
  <h1>Recently deleted</h1>
  <span class="count" id="trash-count" aria-live="polite"></span>
  <div class="head-tools"><button type="button" class="btn danger" id="empty-trash" hidden>Empty trash</button></div>
</header>
<p class="help trash-help" id="trash-help" hidden>Deleted meetings stay here for {TRASH_RETENTION_DAYS} days, then they are removed permanently.</p>
<div class="list-error" id="trash-error" role="alert" hidden><span>Could not load Recently deleted. Try again in a moment.</span><button type="button" class="btn secondary" id="trash-retry">Try again</button></div>
<ul class="mlist" id="trash-rows" aria-busy="true"></ul>
</div>
{_CONFIRM_DIALOG_HTML}
<script>
"""
        + _JS_HELPERS
        + f"var RETENTION_DAYS = {TRASH_RETENTION_DAYS};"
        + r"""
var trashItems = [];
function trashRow(item) {
  var s = meetingSummary(item), id = escapeHtml(item.session_id), name = escapeHtml(s.name);
  var what = [s.meta, item.has_audio ? fmtBytes(item.audio_bytes) + ' audio' : 'No audio'].filter(Boolean).join(' · ');
  var when = 'Deleted ' + agoText(item.deleted_at) + ' · ' + daysLeftText(item.days_left);
  return '<li class="mrow trash-row" data-id="' + id + '"><div class="mrow-main"><span class="mrow-title">' + name + '</span><span class="mrow-sub"><span>' + escapeHtml(what) + '</span><span class="tr-sep"> · </span><span class="tr-when">' + escapeHtml(when) + '</span></span></div>'
    + '<div class="trash-actions"><button type="button" class="btn primary sm" data-restore="' + id + '" aria-label="Restore ' + name + '">Restore</button>'
    + '<button type="button" class="btn danger sm" data-purge="' + id + '" aria-label="Delete ' + name + ' permanently">Delete permanently</button></div></li>';
}
function renderTrash(items) {
  trashItems = items;
  var rows = document.getElementById('trash-rows');
  document.getElementById('trash-count').textContent = items.length ? plural(items.length, 'meeting', 'meetings') : '';
  document.getElementById('empty-trash').hidden = !items.length;
  document.getElementById('trash-help').hidden = !items.length;
  rows.innerHTML = items.length ? items.map(trashRow).join('') : '<li class="state-row"><div class="empty-teach"><h2>Nothing in Recently deleted</h2><p>Meetings you delete stay here for ' + RETENTION_DAYS + ' days, so you can restore them. After that they are removed permanently.</p><a class="btn secondary" href="/meetings">Back to Meetings</a></div></li>';
  rows.setAttribute('aria-busy', 'false');
}
function loadTrash() {
  return fetch('/v1/trash', {credentials: 'same-origin'}).then(function (r) {
    if (r.status === 401 || r.status === 403) { window.location = '/login'; throw new Error('Signed out'); }
    if (!r.ok) throw new Error('Unable to load');
    return r.json();
  }).then(function (data) { document.getElementById('trash-error').hidden = true; renderTrash(data.items || []); return true; })
    .catch(function () { document.getElementById('trash-error').hidden = false; document.getElementById('trash-rows').setAttribute('aria-busy', 'false'); return false; });
}
function findTrash(id) { return trashItems.filter(function (i) { return i.session_id === id; })[0] || {session_id: id}; }
function trashCall(id, method, path) {
  return fetch('/v1/trash/' + encodeURIComponent(id) + path, {method: method, credentials: 'same-origin'}).then(function (r) {
    if (!r.ok) return r.json().catch(function () { return {}; }).then(function (b) { throw new Error(b.detail || 'Request failed'); });
  });
}
document.getElementById('trash-rows').addEventListener('click', function (e) {
  var restore = e.target.closest('[data-restore]'), purge = e.target.closest('[data-purge]');
  if (restore) {
    var id = restore.dataset.restore, item = findTrash(id);
    restore.disabled = true;
    trashCall(id, 'POST', '/restore').then(function () { notify('Restored ' + meetingSummary(item).name + '.'); return loadTrash(); })
      .catch(function (err) { restore.disabled = false; notify(err.message, 'error'); });
  } else if (purge) {
    var pid = purge.dataset.purge, pitem = findTrash(pid);
    confirmDialog({title: 'Delete this meeting permanently?', lead: 'Its recording, transcript and notes are removed for good. This cannot be undone.', items: [meetingSummary(pitem)], confirmLabel: 'Delete permanently'}).then(function (ok) {
      if (!ok) return;
      trashCall(pid, 'DELETE', '').then(function () { notify('Deleted permanently.'); return loadTrash(); }).catch(function (err) { notify(err.message, 'error'); });
    });
  }
});
document.getElementById('empty-trash').onclick = function () {
  if (!trashItems.length) return;
  confirmDialog({title: 'Empty trash?', lead: plural(trashItems.length, 'meeting is', 'meetings are') + ' removed permanently, with recordings, transcripts and notes. This cannot be undone.', items: trashItems.map(meetingSummary), confirmLabel: 'Delete ' + (trashItems.length === 1 ? 'permanently' : 'all permanently')}).then(function (ok) {
    if (!ok) return;
    fetch('/v1/trash/empty', {method: 'POST', credentials: 'same-origin'}).then(function (r) { if (!r.ok) throw new Error('Could not empty the trash'); return r.json(); })
      .then(function (d) { notify(plural(d.purged, 'meeting', 'meetings') + ' deleted permanently.'); return loadTrash(); })
      .catch(function (err) { notify(err.message, 'error'); });
  });
};
document.getElementById('trash-retry').onclick = loadTrash;
loadTrash();
</script>
"""
    )
    return _shell(
        "Recently deleted",
        body,
        token_configured=token_configured,
        active="transcriptions",
        main_class="meetings-page",
        appearance=appearance,
    )
