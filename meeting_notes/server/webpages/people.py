"""The People list and person pages (voice profiles)."""

from __future__ import annotations

import json

from .shared import _CONFIRM_DIALOG_HTML, _JS_HELPERS
from .shell import _icon, _shell


_PEOPLE_JS = r"""
/* People (voice profiles). Data comes from /v1/people*; every server string goes through escapeHtml. */
function pApi(method, path, body) {
  var opt = {method: method, credentials: 'same-origin'};
  if (body !== undefined) { opt.headers = {'Content-Type': 'application/json'}; opt.body = JSON.stringify(body); }
  return fetch(path, opt).then(function (r) {
    if (r.status === 401 || r.status === 403) { window.location = '/login'; throw new Error('Signed out'); }
    if (!r.ok) return r.json().catch(function () { return {}; }).then(function (b) { var e = new Error(typeof b.detail === 'string' ? b.detail : 'Request failed'); e.status = r.status; throw e; });
    return r.json();
  });
}
function talkText(sec) {
  sec = Math.max(0, Math.round(Number(sec) || 0));
  if (sec < 60) return sec + ' sec';
  if (sec < 3600) return Math.round(sec / 60) + ' min';
  var h = Math.floor(sec / 3600), m = Math.round((sec % 3600) / 60);
  return h + ' h' + (m ? ' ' + m + ' min' : '');
}
function lastHeardText(ts) { return ts ? fmtDate(ts, true) : 'Not heard yet'; }
function personAvatar(p, cls) {
  return '<span class="avatar ' + (cls || '') + (p.is_self ? ' you-av' : '') + '" aria-hidden="true">' + escapeHtml(initials(p.display)) + '</span>';
}
function linkStateText(m) {
  if (m.link === 'confirmed') return 'Confirmed';
  if (m.link === 'suggested') return 'Possible match';
  return 'Matched by voice';
}
// pickPerson({title, lead, confirmLabel, exclude}) -> Promise<person id | null>; needs the #pick-dialog markup.
function pickPerson(o) {
  return pApi('GET', '/v1/people?sort=alpha').then(function (d) {
    var people = (d.people || []).filter(function (p) { return p.id !== o.exclude && (!o.namedOnly || p.name || (o.withSelf && p.is_self)); });
    var pre = o.prefix || 'pick', dlg = document.getElementById(pre + '-dialog');
    if (!people.length) { notify(o.namedOnly ? 'There is nobody with a name in People yet.' : 'There is nobody else to choose.', 'error'); return null; }
    if (!dlg || !dlg.showModal) return null;
    dlg.querySelector('#' + pre + '-title').textContent = o.title || 'Choose a person';
    dlg.querySelector('#' + pre + '-lead').textContent = o.lead || '';
    dlg.querySelector('#' + pre + '-ok').textContent = o.confirmLabel || 'Choose';
    var sel = dlg.querySelector('#' + pre + '-select');
    sel.innerHTML = people.map(function (p) { return '<option value="' + escapeHtml(p.id) + '">' + escapeHtml(p.display + (p.company ? ' (' + p.company + ')' : '')) + '</option>'; }).join('');
    return new Promise(function (resolve) {
      dlg.onclose = function () { dlg.onclose = null; resolve(dlg.returnValue === 'ok' ? sel.value : null); };
      dlg.onclick = function (e) { if (e.target === dlg) dlg.close('cancel'); };
      dlg.returnValue = '';
      dlg.showModal();
      sel.focus();
    });
  });
}
"""

_PICK_DIALOG_HTML = """<dialog class="dialog" id="pick-dialog" aria-labelledby="pick-title" aria-describedby="pick-lead">
  <form method="dialog" class="dialog-form">
    <h2 class="dialog-title" id="pick-title"></h2>
    <p class="dialog-lead" id="pick-lead"></p>
    <label class="field"><span class="name">Person</span><select id="pick-select"></select></label>
    <div class="dialog-foot">
      <button type="submit" value="cancel" class="btn secondary">Cancel</button>
      <button type="submit" value="ok" class="btn primary" id="pick-ok">Choose</button>
    </div>
  </form>
</dialog>"""

# Own ids: the meeting split/combine dialogs on the Meetings page already use pick-*.
_PPICK_DIALOG_HTML = _PICK_DIALOG_HTML.replace("pick-", "ppick-")

_NP_DIALOG_HTML = """<dialog class="dialog" id="np-dialog" aria-labelledby="np-title" aria-describedby="np-lead">
  <form method="dialog" class="dialog-form">
    <h2 class="dialog-title" id="np-title"></h2>
    <p class="dialog-lead" id="np-lead"></p>
    <div class="dialog-foot">
      <button type="submit" value="cancel" class="btn secondary">Cancel</button>
      <button type="submit" value="link" class="btn secondary">Link to someone...</button>
      <button type="submit" value="add" class="btn primary">Add to People</button>
    </div>
  </form>
</dialog>"""

_PEOPLE_LINK_JS = r"""
/* Participants in the notes -> People. Elements with data-pname are looked up in People (GET /v1/people/lookup):
   a match becomes a link to the person, no match gets a "Not in People" button. When People is off the lookup
   answers 404 and nothing changes. Needs _PEOPLE_JS, _PICK_DIALOG_HTML and _NP_DIALOG_HTML. */
var npLookup = {};
function npKey(n) { return String(n == null ? '' : n).replace(/\s+/g, ' ').trim().toLowerCase(); }
function npResolve(names) {
  var want = [];
  names.forEach(function (n) { var k = npKey(n); if (k && !(k in npLookup) && want.indexOf(n) < 0) want.push(n); });
  if (!want.length) return Promise.resolve('cached');  // true only when something new was fetched
  var q = want.map(function (n) { return 'names=' + encodeURIComponent(n); }).join('&');
  return fetch('/v1/people/lookup?' + q, {credentials: 'same-origin'}).then(function (r) {
    if (!r.ok) return false;
    return r.json().then(function (d) {
      want.forEach(function (n) { npLookup[npKey(n)] = (d.items || {})[n] || null; });
      return true;
    });
  }).catch(function () { return false; });
}
function npMarkup(name) {
  var hit = npLookup[npKey(name)];
  if (hit === undefined) return escapeHtml(name);
  if (hit) return '<a class="np-link" href="/people/' + encodeURIComponent(hit.person_id) + '"' + (hit.via === 'alias' ? ' title="Same person as ' + escapeHtml(hit.display) + '"' : '') + '>' + escapeHtml(name) + '</a>';
  return '<button type="button" class="np-new" data-np-name="' + escapeHtml(name) + '" aria-label="' + escapeHtml(name) + ' is not in People. Add them or link them to someone.">' + escapeHtml(name) + '<span class="np-tag">Not in People</span></button>';
}
function npDecorate(root) {
  if (typeof voiceProfilesOn !== 'undefined' && !voiceProfilesOn) return;
  var els = Array.prototype.slice.call((root || document).querySelectorAll('[data-pname]'));
  if (!els.length) return;
  npResolve(els.map(function (e) { return e.getAttribute('data-pname'); })).then(function (ok) {
    if (!ok) return;
    els.forEach(function (e) { if (e.isConnected) e.innerHTML = npMarkup(e.getAttribute('data-pname')); });
  });
}
function npChoose(name) {
  var dlg = document.getElementById('np-dialog');
  if (!dlg || !dlg.showModal) return;
  dlg.querySelector('#np-title').textContent = name + ' is not in People';
  dlg.querySelector('#np-lead').textContent = 'The notes mention this name, but nobody in People goes by it. Add them as a new person, or link the name to someone who is already there.';
  var again = function () { delete npLookup[npKey(name)]; npDecorate(document); };
  dlg.onclose = function () {
    dlg.onclose = null;
    if (dlg.returnValue === 'add') {
      pApi('POST', '/v1/people', {name: name}).then(function () { notify('Added ' + name + ' to People.'); again(); }).catch(function (err) { notify(err.message, 'error'); });
    } else if (dlg.returnValue === 'link') {
      pickPerson({title: 'Link to someone', lead: 'Who is "' + name + '"? The spelling is remembered as another way to say their name.', confirmLabel: 'Link', namedOnly: true, prefix: 'ppick'}).then(function (pid) {
        if (!pid) return;
        return pApi('POST', '/v1/people/' + encodeURIComponent(pid) + '/aliases', {wrong: name}).then(function () { notify('Linked ' + name + '.'); again(); });
      }).catch(function (err) { notify(err.message, 'error'); });
    }
  };
  dlg.onclick = function (e) { if (e.target === dlg) dlg.close('cancel'); };
  dlg.returnValue = '';
  dlg.showModal();
}
document.addEventListener('click', function (e) {
  var b = e.target.closest ? e.target.closest('[data-np-name]') : null;
  if (b) npChoose(b.getAttribute('data-np-name'));
});
"""

_PEOPLE_LIST_JS = r"""
var SORT_KEY = 'mn-people-sort', SORTS = ['talking', 'recent', 'alpha'], sortMode = 'talking';
try { var savedSort = localStorage.getItem(SORT_KEY); if (SORTS.indexOf(savedSort) >= 0) sortMode = savedSort; } catch (e) {}
function peopleRow(p) {
  var sub = [];
  if (p.company) sub.push(escapeHtml(p.company));
  if (p.suggestion && !p.name) sub.push('<span class="people-hint">Sounds like ' + escapeHtml(p.suggestion.display) + '?</span>');
  if (p.intro_name && !p.name) sub.push('<span class="people-hint">Introduced as ' + escapeHtml(p.intro_name) + '?</span>');
  if (p.is_self) sub.push('Your voice');
  if (p.archived) sub.push('<span class="pill">Archived</span>');
  if (!p.meetings && !p.is_self) sub.push('No voice yet');
  var id = encodeURIComponent(p.id);
  return '<li class="mrow prow" data-href="/people/' + id + '"><label class="sel"><input class="row-select" type="checkbox" value="' + escapeHtml(p.id) + '" aria-label="Select ' + escapeHtml(p.display) + '"></label>' + personAvatar(p, 'lg')
    + '<div class="mrow-main"><a class="mrow-title' + (p.name ? '' : ' unnamed') + '" href="/people/' + id + '">' + escapeHtml(p.display) + '</a><span class="mrow-sub">' + (sub.join(' · ') || '&nbsp;') + '</span></div>'
    + '<span class="pcell">' + plural(p.meetings, 'meeting', 'meetings') + '</span>'
    + '<span class="pcell hide-sm">' + escapeHtml(talkText(p.talk_seconds)) + '</span>'
    + '<span class="pcell hide-sm">' + escapeHtml(lastHeardText(p.last_heard)) + '</span></li>';
}
var showArchived = false, allPeople = [];
var peopleById = {};
function $(id) { return document.getElementById(id); }
function renderPeople(everyone) {
  allPeople = everyone;
  var archivedCount = everyone.filter(function (p) { return p.archived; }).length;
  if (!archivedCount) showArchived = false;
  $('view-archived-text').textContent = 'Archived (' + archivedCount + ')';
  $('view-seg').hidden = !archivedCount;
  Array.prototype.forEach.call(document.querySelectorAll('input[name=people-view]'), function (input) { input.checked = (input.value === 'archived') === showArchived; });
  var list = everyone.filter(function (p) { return !!p.archived === showArchived; });
  peopleById = {};
  list.forEach(function (p) { peopleById[p.id] = p; });
  var rows = document.getElementById('people-rows');
  document.getElementById('people-count').textContent = list.length ? plural(list.length, 'person', 'people') : '';
  document.getElementById('people-head').hidden = !list.length;
  rows.innerHTML = list.length ? list.map(peopleRow).join('') : '<li class="state-row"><div class="empty-teach"><h2>No one here yet</h2><p>People appear after a meeting is transcribed with voice profiles on. Each voice in the meeting becomes a person you can name, and their name then shows in every meeting they speak in.</p><a class="btn secondary" href="/meetings">Back to Meetings</a></div></li>';
  rows.setAttribute('aria-busy', 'false');
  updateSelection();
}
function selectedPeople() {
  return Array.prototype.map.call(document.querySelectorAll('.row-select:checked'), function (b) { return peopleById[b.value]; }).filter(Boolean);
}
function updateSelection() {
  var sel = selectedPeople(), all = document.querySelectorAll('.row-select'), master = $('select-all');
  $('selection-count').textContent = !sel.length ? 'Select people to combine' : sel.length === 1 ? '1 selected. Pick at least one more to combine.' : sel.length + ' selected';
  document.querySelector('.bulk-actions').classList.toggle('has-selection', !!sel.length);
  document.body.classList.toggle('has-bulk', !!sel.length);
  $('bulk-combine').disabled = sel.length < 2;
  master.checked = !!all.length && sel.length === all.length;
  master.indeterminate = sel.length > 0 && sel.length < all.length;
}
function keepDefault(sel) {
  var me = sel.filter(function (p) { return p.is_self; })[0];
  if (me) return me;
  var named = sel.filter(function (p) { return p.name; });
  var pool = named.length ? named : sel;
  return pool.slice().sort(function (a, b) { return b.talk_seconds - a.talk_seconds; })[0];
}
function combineDialog(sel) {
  var dlg = $('combine-dialog'), keep = keepDefault(sel), hasSelf = sel.some(function (p) { return p.is_self; });
  $('combine-lead').textContent = hasSelf
    ? 'Everything for the other people moves to your profile, and their profiles are removed. Your profile always stays.'
    : 'Everything for the other people moves to the one you keep, and their profiles are removed. Blank details on the kept person are filled in from the others.';
  $('combine-opts').innerHTML = sel.map(function (p) {
    return '<li><label><input type="radio" name="combine-keep" value="' + escapeHtml(p.id) + '"' + (p.id === keep.id ? ' checked' : '') + (hasSelf && !p.is_self ? ' disabled' : '') + '>'
      + '<span class="combine-name">' + escapeHtml(p.display) + (p.company ? ' (' + escapeHtml(p.company) + ')' : '') + '</span><span class="combine-meta">' + escapeHtml(plural(p.meetings, 'meeting', 'meetings') + ' · ' + talkText(p.talk_seconds)) + '</span></label></li>';
  }).join('');
  dlg.onclose = function () {
    dlg.onclose = null;
    if (dlg.returnValue !== 'ok') return;
    var chosen = dlg.querySelector('input[name=combine-keep]:checked');
    if (!chosen) return;
    pApi('POST', '/v1/people/combine', {ids: sel.map(function (p) { return p.id; }), into: chosen.value}).then(function (p) {
      notify('Combined ' + plural(sel.length, 'person', 'people') + ' into ' + p.display + '.');
      return loadPeople();
    }).catch(function (err) { notify(err.message, 'error'); });
  };
  dlg.onclick = function (e) { if (e.target === dlg) dlg.close('cancel'); };
  dlg.returnValue = '';
  dlg.showModal();
}
$('bulk-combine').onclick = function () { var sel = selectedPeople(); if (sel.length >= 2) combineDialog(sel); };
$('bulk-clear').onclick = function () { Array.prototype.forEach.call(document.querySelectorAll('.row-select'), function (b) { b.checked = false; }); updateSelection(); };
$('select-all').onchange = function (e) { Array.prototype.forEach.call(document.querySelectorAll('.row-select'), function (b) { b.checked = e.target.checked; }); updateSelection(); };
$('people-rows').addEventListener('change', function (e) { if (e.target.classList.contains('row-select')) updateSelection(); });
$('add-person').onclick = function () {
  var dlg = $('add-dialog');
  $('add-name').value = ''; $('add-company').value = ''; $('add-error').hidden = true;
  dlg.onclick = function (e) { if (e.target === dlg) dlg.close('cancel'); };
  dlg.returnValue = '';
  dlg.showModal();
  $('add-name').focus();
};
$('add-ok').onclick = function () {
  var name = $('add-name').value.replace(/\s+/g, ' ').trim();
  if (!name) { $('add-error').textContent = 'Type a name first.'; $('add-error').hidden = false; $('add-name').focus(); return; }
  $('add-ok').disabled = true;
  pApi('POST', '/v1/people', {name: name, company: $('add-company').value}).then(function (p) {
    $('add-dialog').close('ok');
    notify('Added ' + p.display + '. They have no voice yet; it is learned once they speak in a meeting.');
    return loadPeople();
  }).catch(function (err) { $('add-error').textContent = err.message; $('add-error').hidden = false; }).then(function () { $('add-ok').disabled = false; });
};
$('add-name').addEventListener('keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); $('add-ok').click(); } });
function loadPeople() {
  return pApi('GET', '/v1/people?sort=' + encodeURIComponent(sortMode)).then(function (d) {
    document.getElementById('people-error').hidden = true;
    renderPeople(d.people || []);
  }).catch(function () {
    document.getElementById('people-error').hidden = false;
    document.getElementById('people-rows').setAttribute('aria-busy', 'false');
  });
}
document.getElementById('people-rows').addEventListener('click', function (e) {
  if (e.target.closest('a') || e.target.closest('.sel')) return;
  var row = e.target.closest('[data-href]');
  if (row) window.location = row.dataset.href;
});
Array.prototype.forEach.call(document.querySelectorAll('input[name=people-sort]'), function (input) {
  input.checked = input.value === sortMode;
  input.addEventListener('change', function () {
    sortMode = input.value;
    try { localStorage.setItem(SORT_KEY, sortMode); } catch (e) {}
    loadPeople();
  });
});
Array.prototype.forEach.call(document.querySelectorAll('input[name=people-view]'), function (input) {
  input.addEventListener('change', function () { showArchived = input.value === 'archived'; renderPeople(allPeople); });
});
document.getElementById('people-retry').onclick = loadPeople;
// Recheck voices: the same call and wording as Settings > Voice profiles; people may gain meetings, so reload the list.
function showRecheckStatus() { return voiceRecheckStatus().then(function (st) { if (st) $('recheck-status').textContent = voiceRecheckLine(st); }); }
$('recheck-voices').onclick = function () {
  var btn = $('recheck-voices');
  btn.disabled = true;
  runVoiceRecheck().then(function (r) {
    notify('Voice recheck done: ' + voiceRecheckSummary(r) + '.');
    return Promise.all([showRecheckStatus(), loadPeople()]);
  }).catch(function (e) { notify(e.message, 'error'); }).then(function () { btn.disabled = false; });
};
showRecheckStatus();
loadPeople();
"""

_PEOPLE_SORT_LABELS = (("talking", "Most talking"), ("recent", "Most recent"), ("alpha", "Alphabetical"))


def render_people_page(*, token_configured: bool, appearance: str = "system") -> str:
    """People: everyone voice profiles have found, rendered client-side from ``GET /v1/people``."""
    sort_opts = "".join(
        f'<label class="seg-opt"><input type="radio" name="people-sort" value="{value}"'
        f'{" checked" if value == "talking" else ""}><span>{label}</span></label>'
        for value, label in _PEOPLE_SORT_LABELS
    )
    body = (
        f"""
<div class="page people-page">
<header class="page-head">
  <h1>People</h1>
  <span class="count" id="people-count" aria-live="polite"></span>
  <div class="head-tools people-sort"><button type="button" class="btn secondary" id="recheck-voices" title="Match the voices in recent meetings to the people again, without the audio. Voices you confirmed by hand never change.">Recheck voices</button><button type="button" class="btn secondary" id="add-person">Add a person</button><div class="seg" id="view-seg" role="radiogroup" aria-label="Show" hidden><label class="seg-opt"><input type="radio" name="people-view" value="active" checked><span>People</span></label><label class="seg-opt"><input type="radio" name="people-view" value="archived"><span id="view-archived-text">Archived</span></label></div><span class="lbl" id="people-sort-lbl">Sort</span><div class="seg" role="radiogroup" aria-labelledby="people-sort-lbl">{sort_opts}</div></div>
</header>
<p class="help" id="recheck-status" aria-live="polite"></p>
<div class="list-error" id="people-error" role="alert" hidden><span>Could not load People. Try again in a moment.</span><button type="button" class="btn secondary" id="people-retry">Try again</button></div>
<div class="mlist-head people-list people-head" id="people-head" hidden><label class="sel"><input id="select-all" type="checkbox" aria-label="Select all people"></label><span></span><span>Name</span><span>Meetings</span><span class="hide-sm">Talk time</span><span class="hide-sm">Last heard</span></div>
<ul class="mlist people-list" id="people-rows" aria-busy="true"></ul>
</div>
<div class="bulk-actions" role="region" aria-label="Bulk actions">
  <span class="selection-count" id="selection-count" role="status">Select people to combine</span>
  <span class="bulk-buttons"><button class="btn primary" id="bulk-combine" disabled title="Select two or more people to combine">Combine</button></span>
  <button class="btn ghost icon-only" id="bulk-clear" type="button" aria-label="Clear selection" title="Clear selection">{_icon("x")}</button>
</div>
<dialog class="dialog" id="combine-dialog" aria-labelledby="combine-title" aria-describedby="combine-lead">
  <form method="dialog" class="dialog-form">
    <h2 class="dialog-title" id="combine-title">Combine people</h2>
    <p class="dialog-lead" id="combine-lead"></p>
    <p class="combine-ask" id="combine-ask">Who should be kept?</p>
    <ul class="combine-opts" id="combine-opts" aria-labelledby="combine-ask"></ul>
    <div class="dialog-foot">
      <button type="submit" value="cancel" class="btn secondary">Cancel</button>
      <button type="submit" value="ok" class="btn primary">Combine</button>
    </div>
  </form>
</dialog>
<dialog class="dialog" id="add-dialog" aria-labelledby="add-title">
  <form method="dialog" class="dialog-form">
    <h2 class="dialog-title" id="add-title">Add a person</h2>
    <p class="dialog-lead">Add someone by name so their spelling is used in notes and transcripts. They have no voice yet; it is learned once they speak in a meeting.</p>
    <label class="field"><span class="name">Name</span><input type="text" id="add-name" maxlength="120" autocomplete="off" placeholder="Full name, for example Tara Nguyen"></label>
    <label class="field"><span class="name">Company (optional)</span><input type="text" id="add-company" maxlength="120" autocomplete="off"></label>
    <p class="dialog-warning" id="add-error" role="alert" hidden></p>
    <div class="dialog-foot">
      <button type="submit" value="cancel" class="btn secondary">Cancel</button>
      <button type="button" class="btn primary" id="add-ok">Add</button>
    </div>
  </form>
</dialog>
<script>
"""
        + _JS_HELPERS
        + _PEOPLE_JS
        + _PEOPLE_LIST_JS
        + "</script>\n"
    )
    return _shell("People", body, token_configured=token_configured, active="people",
                  main_class="people-page", appearance=appearance)


_PERSON_JS = r"""
var person = null;
function $(id) { return document.getElementById(id); }
function setHead(p) {
  $('person-avatar').textContent = initials(p.display);
  $('person-avatar').classList.toggle('you-av', !!p.is_self);
  $('person-name').textContent = p.display;
  $('person-sub').textContent = [p.company, p.email].filter(Boolean).join(' · ') || (p.is_self ? 'Your voice' : '');
  document.title = 'Meeting Notes | ' + p.display;
}
function renderStats(p) {
  $('person-stats').innerHTML = '<div><dt>Meetings</dt><dd>' + p.meetings + '</dd></div><div><dt>Talk time</dt><dd>' + escapeHtml(talkText(p.talk_seconds)) + '</dd></div><div><dt>Last heard</dt><dd>' + escapeHtml(lastHeardText(p.last_heard)) + '</dd></div>';
}
var companyList = [];
// Company is a choice among the companies already used, plus None and a new one (typed into the box that appears).
function setCompany(current) {
  var sel = $('f-company-sel'), list = companyList.slice();
  if (current && !list.some(function (c) { return c.toLowerCase() === current.toLowerCase(); })) list.push(current);
  list.sort(function (a, b) { return a.toLowerCase() < b.toLowerCase() ? -1 : 1; });
  sel.innerHTML = '<option value="">None</option>' + list.map(function (c) { return '<option value="' + escapeHtml(c) + '">' + escapeHtml(c) + '</option>'; }).join('') + '<option value="__new__">Add a new company...</option>';
  var match = list.filter(function (c) { return c.toLowerCase() === (current || '').toLowerCase(); })[0];
  sel.value = match || '';
  $('f-company').hidden = true; $('f-company').value = '';
}
function companyValue() { return $('f-company-sel').value === '__new__' ? $('f-company').value : $('f-company-sel').value; }
function renderAliases(p) {
  var box = $('person-aliases'), named = !!p.name;
  $('alias-form').hidden = !named;
  $('alias-help').textContent = named ? 'Other spellings the transcripts or notes use for ' + p.name + '. They are corrected to this name in future notes, and shown as this person in meeting notes.' : 'Give this person a name first, then add spellings they are sometimes heard as.';
  box.innerHTML = (p.aliases || []).map(function (a, i) {
    return '<li><div class="pm-main"><span>' + escapeHtml(a) + '</span></div><button type="button" class="btn ghost sm" data-alias="' + i + '" aria-label="Remove ' + escapeHtml(a) + '">Remove</button></li>';
  }).join('') || (named ? '<li><span class="pm-sub">None yet.</span></li>' : '');
}
function renderVoiceNote(p) {
  var v = p.voice_profile || {meetings: 0, confirmed: 0};
  $('voice-note').textContent = v.meetings ? 'Voice profile from ' + plural(v.meetings, 'meeting', 'meetings') + ' (' + v.confirmed + ' confirmed by you). It gets better as you confirm or fix who is who.' : 'No voice yet. It is learned once this person speaks in a transcribed meeting.';
}
function renderArchive(p) {
  var note = $('archive-note');
  $('archive-person').hidden = !p.name || p.is_self || !!p.archived;
  note.hidden = !p.archived;
  if (p.archived) note.innerHTML = 'Archived ' + escapeHtml(fmtDate(p.archived.at)) + (p.archived.auto ? ' because they have not been heard in a while' : '') + '. They are hidden from the People list but still recognised by voice, and come back when heard in a new meeting. <button type="button" class="btn sm" id="restore-person">Restore</button>';
}
function renderForm(p) {
  $('f-name').value = p.name; $('f-name').placeholder = 'Person ' + (p.number || '');
  $('f-email').value = p.email;
  setCompany(p.company);
  renderAliases(p);
  $('person-hint').hidden = !(p.suggestion && !p.name);
  if (p.suggestion && !p.name) $('person-hint').innerHTML = 'Sounds like <a href="/people/' + encodeURIComponent(p.suggestion.person_id) + '">' + escapeHtml(p.suggestion.display) + '</a>? Open that profile to compare, then use Combine with... below if it is the same person.';
}
// Names the "name from introductions" job gave (with Undo) or is unsure about (Use this name / Not them).
function renderIntro(p) {
  var box = $('person-intro'), html = '';
  (p.named_from || []).forEach(function (e, i) {
    html += '<p class="help person-intro-row">Named from introduction: \u201c' + escapeHtml(e.quote || '') + '\u201d in <a href="/sessions/' + encodeURIComponent(e.session_id) + '">' + escapeHtml(e.session_name || e.session_id) + '</a>'
      + '<button type="button" class="btn sm" data-intro="undo" data-i="' + i + '">Undo</button></p>';
  });
  var s = p.intro_suggestion;
  if (s && !p.name) {
    html += '<p class="help person-intro-row"><span class="pm-ask">Introduced as ' + escapeHtml(s.name) + (s.company ? ' (' + escapeHtml(s.company) + ')' : '') + '?</span> \u201c' + escapeHtml(s.quote || '') + '\u201d in <a href="/sessions/' + encodeURIComponent(s.session_id) + '">' + escapeHtml(s.session_name || s.session_id) + '</a>'
      + '<button type="button" class="btn sm primary" data-intro="use">Use this name</button><button type="button" class="btn sm" data-intro="dismiss">Not them</button></p>';
  }
  box.innerHTML = html;
  box.hidden = !html;
}
function renderSamples(p) {
  var box = $('person-samples');
  if (!p.samples.length) { box.innerHTML = '<li><span class="pm-sub">No voice samples yet.</span></li>'; return; }
  box.innerHTML = p.samples.map(function (s, i) {
    var m = p.appearances.filter(function (a) { return a.session_id === s.session_id; })[0] || {};
    return '<li><button type="button" class="btn icon-only sample-play" data-sample="' + i + '" aria-label="Play sample ' + (i + 1) + '" aria-pressed="false">' + icon('play') + '</button>'
      + '<div class="pm-main"><a href="/sessions/' + encodeURIComponent(s.session_id) + '">' + escapeHtml(m.session_name || s.session_id) + '</a><span class="pm-sub">' + escapeHtml(m.created ? fmtDate(m.created, true) : '') + '</span></div></li>';
  }).join('');
}
var player = null, playing = null;
function stopSample() {
  if (player) { player.pause(); player = null; }
  if (playing) { playing.innerHTML = icon('play'); playing.setAttribute('aria-pressed', 'false'); playing.setAttribute('aria-label', 'Play sample ' + (Number(playing.dataset.sample) + 1)); playing = null; }
}
function toggleSample(btn) {
  var was = playing === btn;
  stopSample();
  if (was) return;
  var s = person.samples[Number(btn.dataset.sample)];
  player = new Audio(s.url);
  playing = btn;
  btn.innerHTML = icon('pause'); btn.setAttribute('aria-pressed', 'true'); btn.setAttribute('aria-label', 'Pause sample ' + (Number(btn.dataset.sample) + 1));
  player.onended = stopSample;
  player.onerror = function () { stopSample(); notify('Could not play that sample.', 'error'); };
  player.play().catch(function () { stopSample(); });
}
function renderMeetings(p) {
  var box = $('person-meetings');
  if (!p.appearances.length) { box.innerHTML = '<li><span class="pm-sub">Not in any meeting yet.</span></li>'; return; }
  box.innerHTML = p.appearances.map(function (m, i) {
    var ask = m.link === 'suggested' && m.suggested_person_id;
    var acts = (ask ? '<span class="pm-ask">Sounds like <a href="/people/' + encodeURIComponent(m.suggested_person_id) + '">' + escapeHtml(m.suggested_display) + '</a>?</span>'
      + '<button type="button" class="btn sm primary" data-act="confirm" data-i="' + i + '">Yes</button><button type="button" class="btn sm" data-act="reject" data-i="' + i + '">Not them</button>' : '')
      + '<button type="button" class="btn sm" data-act="move" data-i="' + i + '">Move to...</button>'
      + '<button type="button" class="btn sm" data-act="unlink" data-i="' + i + '">Unlink</button>';
    return '<li><div class="pm-main"><a href="/sessions/' + encodeURIComponent(m.session_id) + '">' + escapeHtml(m.session_name) + '</a><span class="pm-sub">' + escapeHtml(fmtDate(m.created, true)) + ' · Called ' + escapeHtml(m.label) + ' there</span></div>'
      + '<span class="pm-meta">' + escapeHtml(talkText(m.speech_sec)) + '</span>' + badge(m.link === 'confirmed' ? 'done' : 'none', linkStateText(m))
      + '<div class="pm-actions">' + acts + '</div></li>';
  }).join('');
}
function renderQuotes(p) {
  var box = $('person-quotes');
  if (!p.quotes.length) { box.innerHTML = '<li><span class="pm-sub">Nothing transcribed for this person yet.</span></li>'; return; }
  box.innerHTML = p.quotes.map(function (q) {
    var t = Math.max(0, Math.floor(Number(q.start) || 0));
    return '<li><a class="q-where" href="/sessions/' + encodeURIComponent(q.session_id) + '#t=' + t + '">' + escapeHtml(q.session_name) + ' · ' + escapeHtml(fmtDate(q.created, true)) + ' · ' + fmtDuration(t) + '</a><p class="q-text">' + escapeHtml(q.text) + '</p></li>';
  }).join('');
}
function showPerson(p) {
  stopSample();
  person = p;
  $('person-error').hidden = true; $('person-main').hidden = false;
  setHead(p); renderStats(p); renderForm(p); renderArchive(p); renderIntro(p); renderVoiceNote(p); renderSamples(p); renderMeetings(p); renderQuotes(p);
  $('delete-person').disabled = false;
}
function loadPerson() {
  return pApi('GET', '/v1/people/' + encodeURIComponent(PERSON_ID)).then(showPerson).catch(function (e) {
    $('person-main').hidden = true;
    $('person-error').hidden = false;
    $('person-error-text').textContent = e.status === 404 ? 'This person no longer exists. They may have been combined into someone else or deleted.' : 'Could not load this person. Try again in a moment.';
  });
}
$('person-form').addEventListener('submit', function (e) {
  e.preventDefault();
  var btn = $('person-save');
  btn.disabled = true;
  pApi('PATCH', '/v1/people/' + encodeURIComponent(PERSON_ID), {name: $('f-name').value, email: $('f-email').value, company: companyValue()}).then(function (p) {
    showPerson(p); notify('Saved.');
  }).catch(function (err) { notify(err.message, 'error'); }).then(function () { btn.disabled = false; });
});
$('f-company-sel').addEventListener('change', function () {
  var adding = $('f-company-sel').value === '__new__';
  $('f-company').hidden = !adding;
  if (adding) { $('f-company').value = ''; $('f-company').focus(); }
});
$('alias-form').addEventListener('submit', function (e) {
  e.preventDefault();
  var v = $('alias-input').value.replace(/\s+/g, ' ').trim();
  if (!v) { $('alias-input').focus(); return; }
  pApi('POST', '/v1/people/' + encodeURIComponent(PERSON_ID) + '/aliases', {wrong: v}).then(function () { $('alias-input').value = ''; return loadPerson(); })
    .then(function () { notify('Added.'); }).catch(function (err) { notify(err.message, 'error'); });
});
$('person-aliases').addEventListener('click', function (e) {
  var b = e.target.closest('[data-alias]');
  if (!b) return;
  b.disabled = true;
  pApi('DELETE', '/v1/people/' + encodeURIComponent(PERSON_ID) + '/aliases?wrong=' + encodeURIComponent(person.aliases[Number(b.dataset.alias)])).then(loadPerson).catch(function (err) { b.disabled = false; notify(err.message, 'error'); });
});
$('person-samples').addEventListener('click', function (e) { var b = e.target.closest('[data-sample]'); if (b) toggleSample(b); });
$('person-intro').addEventListener('click', function (e) {
  var b = e.target.closest('[data-intro]');
  if (!b) return;
  var act = b.dataset.intro, src = act === 'undo' ? person.named_from[Number(b.dataset.i)] : person.intro_suggestion;
  b.disabled = true;
  pApi('POST', '/v1/people/appearances/intro', {session_id: src.session_id, label: src.label, action: act}).then(function () {
    notify(act === 'undo' ? 'Name taken back.' : act === 'use' ? 'Named.' : 'Okay, not them.');
    return loadPerson();
  }).catch(function (err) { b.disabled = false; notify(err.message, 'error'); });
});
function appearance(m, action, personId) {
  var body = {session_id: m.session_id, label: m.label, action: action};
  if (personId) body.person_id = personId;
  return pApi('POST', '/v1/people/appearances', body);
}
$('person-meetings').addEventListener('click', function (e) {
  var b = e.target.closest('[data-act]');
  if (!b) return;
  var m = person.appearances[Number(b.dataset.i)], act = b.dataset.act;
  b.disabled = true;
  var done = function (msg) { notify(msg); return loadPerson(); };
  var fail = function (err) { b.disabled = false; notify(err.message, 'error'); };
  if (act === 'confirm') appearance(m, 'confirm').then(function () { return done('Linked to ' + m.suggested_display + '.'); }).catch(fail);
  else if (act === 'reject') appearance(m, 'reject').then(function () { return done('Okay, not them.'); }).catch(fail);
  else if (act === 'unlink') appearance(m, 'unlink').then(function () { return done('Unlinked. That voice is now its own person.'); }).catch(fail);
  else pickPerson({title: 'Move to another person', lead: 'Who is "' + m.label + '" in ' + m.session_name + '?', confirmLabel: 'Move', exclude: PERSON_ID}).then(function (pid) {
    if (!pid) { b.disabled = false; return; }
    return appearance(m, 'assign', pid).then(function () { return done('Moved.'); });
  }).catch(fail);
});
$('merge-person').onclick = function () {
  pickPerson({title: 'Combine with...', lead: 'Everything for ' + person.display + ' moves to the person you choose, and this profile is removed. Blank details on that person are filled in from this one.', confirmLabel: 'Combine', exclude: PERSON_ID}).then(function (pid) {
    if (!pid) return;
    return pApi('POST', '/v1/people/' + encodeURIComponent(PERSON_ID) + '/merge', {into: pid}).then(function () { window.location = '/people/' + encodeURIComponent(pid); });
  }).catch(function (err) { notify(err.message, 'error'); });
};
function setArchived(archived) {
  return pApi('POST', '/v1/people/' + encodeURIComponent(PERSON_ID) + '/archive', {archived: archived}).then(function (p) {
    showPerson(p); notify(archived ? 'Archived ' + p.display + '.' : 'Restored ' + p.display + '.');
  }).catch(function (err) { notify(err.message, 'error'); });
}
$('archive-person').onclick = function () { setArchived(true); };
$('archive-note').addEventListener('click', function (e) { if (e.target.closest('#restore-person')) setArchived(false); });
$('delete-person').onclick = function () {
  confirmDialog({title: 'Delete ' + person.display + '?', lead: 'The profile and its voice samples are removed. Meetings keep their plain speaker labels. This cannot be undone.', confirmLabel: 'Delete profile'}).then(function (ok) {
    if (!ok) return;
    pApi('DELETE', '/v1/people/' + encodeURIComponent(PERSON_ID)).then(function () { window.location = '/people'; }).catch(function (err) { notify(err.message, 'error'); });
  });
};
$('person-retry').onclick = loadPerson;
pApi('GET', '/v1/people/companies').then(function (d) { companyList = d.items || []; }).catch(function () {}).then(loadPerson);
"""


def render_person_page(person_id: str, *, token_configured: bool, appearance: str = "system") -> str:
    """One person: details, voice samples, meetings and quotes, rendered client-side from ``GET /v1/people/{id}``."""
    body = (
        f"""
<div class="page people-page person-page">
<header class="page-head">
  <a class="btn ghost" href="/people">{_icon("back")}<span>People</span></a>
</header>
<div class="list-error" id="person-error" role="alert" hidden><span id="person-error-text">Could not load this person.</span><button type="button" class="btn secondary" id="person-retry">Try again</button></div>
<div id="person-main" hidden>
  <div class="person-head" style="margin-bottom:20px"><span id="person-avatar" class="avatar xl" aria-hidden="true"></span><div style="min-width:0"><h1 id="person-name"></h1><span class="sub" id="person-sub"></span></div></div>
  <dl class="pstats" id="person-stats"></dl>
  <section class="sect" aria-labelledby="details-h"><h2 id="details-h">Details</h2><div class="sect-body">
    <p class="help" id="person-hint" hidden></p>
    <div id="person-intro" hidden></div>
    <form class="person-form" id="person-form" autocomplete="off">
      <label class="field"><span class="name">Name</span><input type="text" id="f-name" maxlength="120"></label>
      <label class="field"><span class="name">Email (optional)</span><input type="text" id="f-email" inputmode="email" maxlength="254"></label>
      <label class="field"><span class="name">Company (optional)</span><select id="f-company-sel"><option value="">None</option><option value="__new__">Add a new company...</option></select><input type="text" id="f-company" maxlength="120" placeholder="Company name" aria-label="New company name" hidden></label>
      <div class="inline-actions"><button type="submit" class="btn primary" id="person-save">Save</button></div>
    </form>
  </div></section>
  <section class="sect" aria-labelledby="alias-h"><h2 id="alias-h">Also heard as</h2><div class="sect-body">
    <p class="help" id="alias-help"></p>
    <ul class="prow-list" id="person-aliases"></ul>
    <form class="names-add-row" id="alias-form" autocomplete="off"><input type="text" id="alias-input" maxlength="120" placeholder="Heard as, for example Tara When" aria-label="Another spelling"><button type="submit" class="btn">Add</button></form>
  </div></section>
  <section class="sect" aria-labelledby="samples-h"><h2 id="samples-h">Voice samples</h2><div class="sect-body"><p class="help" id="voice-note"></p><ul class="prow-list" id="person-samples"></ul></div></section>
  <section class="sect" aria-labelledby="meetings-h"><h2 id="meetings-h">Meetings</h2><div class="sect-body"><ul class="prow-list" id="person-meetings"></ul></div></section>
  <section class="sect" aria-labelledby="quotes-h"><h2 id="quotes-h">What they said</h2><div class="sect-body"><ul class="prow-list quote-list" id="person-quotes"></ul></div></section>
  <section class="sect" aria-labelledby="manage-h"><h2 id="manage-h">Manage</h2><div class="sect-body">
    <p class="help">Combine if two profiles are the same person. Delete if this voice should not be remembered.</p>
    <p class="help" id="archive-note" hidden></p>
    <div class="manage-actions"><button type="button" class="btn" id="merge-person">Combine with...</button><button type="button" class="btn" id="archive-person" title="Hide them from the People list. They are still recognised by voice." hidden>Archive</button><button type="button" class="btn danger" id="delete-person" disabled>Delete profile</button></div>
  </div></section>
</div>
</div>
{_CONFIRM_DIALOG_HTML}
{_PICK_DIALOG_HTML}
"""
        + "<script>\nvar PERSON_ID = "
        + json.dumps(person_id).replace("<", "\\u003c")
        + ";\n"
        + _JS_HELPERS
        + _PEOPLE_JS
        + _PERSON_JS
        + "</script>\n"
    )
    return _shell("Person", body, token_configured=token_configured, active="people",
                  main_class="people-page", appearance=appearance)
