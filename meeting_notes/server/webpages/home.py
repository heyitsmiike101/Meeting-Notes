"""The sign-in page and the Home page."""

from __future__ import annotations

import html

from .shared import _JS_HELPERS
from .shell import _icon, _shell


def render_login_page(error: bool = False, *, appearance: str = "system", next_path: str = "") -> str:
    error_html = (
        '<p class="error-text" role="alert">Invalid token. Check it and try again.</p>' if error else ""
    )
    body = f"""
<div class="login-wrap">
  <div class="login-card">
    <span class="mark login-mark">{_icon("mark", 20)}</span>
    <h1>Sign in to Meeting Notes</h1>
    <form method="post" action="/login">
      <label class="field">
        <span class="name">Server token</span>
        <input type="password" name="token" autofocus required autocomplete="current-password"{' aria-invalid="true"' if error else ""}>
      </label>
      {error_html}
      {f'<input type="hidden" name="next" value="{html.escape(next_path, quote=True)}">' if next_path else ""}
      <button type="submit" class="btn primary block">Sign in</button>
    </form>
  </div>
</div>
"""
    return _shell("Sign in", body, token_configured=True, nav=False, main_class="auth-page", appearance=appearance)


def render_home_page(*, token_configured: bool, appearance: str = "system") -> str:
    skeleton = "".join(
        '<li class="mrow skel" aria-hidden="true"><span class="skel-bar w1"></span><span class="skel-bar w2"></span></li>'
        for _ in range(4)
    )
    body = (
        f"""
<div class="page">
<header class="page-head">
  <h1>Home</h1>
  <p class="summary-line" aria-label="Library summary"><span id="total-count">—</span> meetings · <span id="live-count">0</span> live · <span id="audio-count">—</span> with audio</p>
</header>
<section id="live-section" style="display:none" aria-labelledby="live-heading">
  <div class="sec-head"><h2 id="live-heading"><span class="live-dot" aria-hidden="true"></span>Live transcription</h2></div>
  <div id="live-list" class="live-list"></div>
</section>
<div class="home-grid">
  <section aria-labelledby="recent-heading">
    <div class="sec-head"><h2 id="recent-heading">Recent meetings</h2><a href="/meetings">View all meetings</a></div>
    <ul id="recent-list" class="mlist" aria-busy="true">{skeleton}</ul>
  </section>
  <section class="side-panel" aria-labelledby="upload-heading">
    <h2 id="upload-heading">Add a meeting</h2>
    <div class="tabs upload-tabs" role="group" aria-label="What to add">
      <button class="active" id="tab-recording" type="button" aria-pressed="true">Recording</button>
      <button id="tab-transcript" type="button" aria-pressed="false">Transcript</button>
    </div>
    <p class="help" id="upload-help">Drop in an audio file and Meeting Notes will upload and transcribe it. MP3, WAV, M4A, MP4, FLAC, OGG, OGA, Opus, AAC, and WebM are supported.</p>
    <form id="recording-upload" class="upload-form">
      <label class="field"><span class="name">Recording</span><input id="recording-file" type="file" accept="audio/*,.mp3,.wav,.m4a,.mp4,.flac,.ogg,.oga,.opus,.aac,.webm" required></label>
      <label class="field"><span class="name">Meeting name <span class="optional">(optional)</span></span><input id="recording-name" type="text" maxlength="200" placeholder="e.g. Weekly standup"></label>
      <button type="submit" class="btn primary" id="upload-submit">Upload recording</button>
    </form>
    <div class="upload-status" id="upload-status" role="status" aria-live="polite"></div>
    <div class="progress-track" id="upload-progress-track" hidden><i id="upload-progress"></i></div>
    <form id="transcript-upload" class="upload-form" hidden>
      <label class="field"><span class="name">Transcript file <span class="optional">(.txt, .vtt or .srt)</span></span><input id="transcript-file" type="file" accept=".txt,.vtt,.srt,text/plain,text/vtt"></label>
      <label class="field"><span class="name">Or paste the transcript</span><textarea id="transcript-text" rows="6" placeholder="Jane: Hello everyone&#10;Bob: Thanks for joining"></textarea></label>
      <label class="field"><span class="name">Meeting name <span class="optional">(optional)</span></span><input id="transcript-name" type="text" maxlength="200" placeholder="e.g. Weekly standup"></label>
      <label class="field"><span class="name">Date and time</span><input id="transcript-when" type="datetime-local" required></label>
      <button type="submit" class="btn primary" id="transcript-submit">Upload transcript</button>
    </form>
    <div class="upload-status" id="transcript-status" role="status" aria-live="polite" hidden></div>
  </section>
</div>
</div>
<script>
"""
        + _JS_HELPERS
        + r"""
var uploadForm = document.getElementById('recording-upload');
uploadForm.addEventListener('submit', function(event) {
  event.preventDefault();
  var file = document.getElementById('recording-file').files[0];
  var status = document.getElementById('upload-status'), submit = document.getElementById('upload-submit');
  var track = document.getElementById('upload-progress-track'), bar = document.getElementById('upload-progress');
  if (!file) return;
  var xhr = new XMLHttpRequest(), form = new FormData();
  form.append('file', file); form.append('name', document.getElementById('recording-name').value.trim());
  status.classList.remove('err');
  submit.disabled = true; track.hidden = false; bar.style.transform = 'scaleX(0)'; status.textContent = 'Uploading ' + file.name + '…';
  xhr.upload.addEventListener('progress', function(e) { if (e.lengthComputable) { var pct = Math.round(e.loaded / e.total * 100); bar.style.transform = 'scaleX(' + (pct / 100) + ')'; status.textContent = 'Uploading… ' + pct + '%'; } });
  xhr.addEventListener('load', function() {
    submit.disabled = false;
    var data = {}; try { data = JSON.parse(xhr.responseText || '{}'); } catch (_) {}
    if (xhr.status < 200 || xhr.status >= 300) { status.classList.add('err'); status.textContent = data.detail || 'Upload failed. Please try again.'; return; }
    bar.style.transform = 'scaleX(1)'; status.textContent = 'Upload complete. Transcription queued' + (data.session_id ? ' — opening meeting…' : '.');
    if (data.session_id) setTimeout(function() { location.href = '/sessions/' + encodeURIComponent(data.session_id); }, 500);
  });
  xhr.addEventListener('error', function() { submit.disabled = false; status.classList.add('err'); status.textContent = 'Upload failed. Check the server connection and try again.'; });
  xhr.open('POST', '/v1/uploads'); xhr.withCredentials = true; xhr.send(form);
});
var uploadHelp = {
  recording: 'Drop in an audio file and Meeting Notes will upload and transcribe it. MP3, WAV, M4A, MP4, FLAC, OGG, OGA, Opus, AAC, and WebM are supported.',
  transcript: 'Already have a transcript, from Teams, Zoom or a text file? Add it here: nothing is transcribed again. Times and speaker names are kept when the text has them.'
};
function showUploadTab(which) {
  var transcript = which === 'transcript';
  document.getElementById('recording-upload').hidden = transcript;
  document.getElementById('transcript-upload').hidden = !transcript;
  document.getElementById('upload-status').hidden = transcript;
  document.getElementById('transcript-status').hidden = !transcript;
  document.getElementById('tab-recording').classList.toggle('active', !transcript);
  document.getElementById('tab-transcript').classList.toggle('active', transcript);
  document.getElementById('tab-recording').setAttribute('aria-pressed', String(!transcript));
  document.getElementById('tab-transcript').setAttribute('aria-pressed', String(transcript));
  document.getElementById('upload-help').textContent = uploadHelp[which];
}
document.getElementById('tab-recording').addEventListener('click', function() { showUploadTab('recording'); });
document.getElementById('tab-transcript').addEventListener('click', function() { showUploadTab('transcript'); });
var transcriptForm = document.getElementById('transcript-upload'), transcriptFileName = '';
document.getElementById('transcript-when').value = localInputValue(new Date());
document.getElementById('transcript-file').addEventListener('change', function() {
  var file = this.files[0], input = this, status = document.getElementById('transcript-status');
  status.classList.remove('err'); status.textContent = '';
  if (!file) { transcriptFileName = ''; return; }
  readTranscriptFile(file, function(error, info) {
    if (error) { input.value = ''; transcriptFileName = ''; status.classList.add('err'); status.textContent = error; return; }
    transcriptFileName = info.filename;
    document.getElementById('transcript-text').value = info.text;
    var name = document.getElementById('transcript-name');
    if (!name.value.trim()) name.value = info.name;
    if (info.when) document.getElementById('transcript-when').value = info.when;
  });
});
transcriptForm.addEventListener('submit', function(event) {
  event.preventDefault();
  var status = document.getElementById('transcript-status'), submit = document.getElementById('transcript-submit');
  status.classList.remove('err');
  var built = buildTranscriptPayload({text: document.getElementById('transcript-text').value, name: document.getElementById('transcript-name').value, filename: transcriptFileName, when: document.getElementById('transcript-when').value});
  if (built.error) { status.classList.add('err'); status.textContent = built.error; return; }
  submit.disabled = true; status.textContent = 'Adding the transcript…';
  postTranscript(built.payload).then(function(data) {
    submit.disabled = false;
    status.textContent = 'Transcript added — opening meeting…';
    setTimeout(function() { location.href = '/sessions/' + encodeURIComponent(data.session_id); }, 400);
  }, function(err) { submit.disabled = false; status.classList.add('err'); status.textContent = err.message; });
});
var liveItems = [];
var activeLiveId = null;
var liveOverlayPreviousFocus = null;

// Lines come from the shared live-preview helpers (livePartials sorts by meeting time).
function liveText(item, limit) { return liveLinesHtml(item, limit); }
// The "Your notes" typed on the recorder, rendered by the server (/v1/live/{id}/notes.html), cached per meeting and
// re-fetched only when /v1/live reports a different notes_seq.
var liveNotes = {};
function liveNotesBlock(item) {
  var n = liveNotes[item.session_id];
  if (!n || !n.html) return '';
  return '<div class="live-notes"><p class="live-section">Your notes</p><div class="scratchpad-rendered" aria-label="Your notes">' + n.html + '</div></div>';
}
function syncLiveNotes(items) {
  var wanted = {}, pending = [];
  items.forEach(function (item) {
    var id = item.session_id, seq = item.notes_seq;
    wanted[id] = true;
    if (seq == null) { delete liveNotes[id]; return; }
    if (liveNotes[id] && liveNotes[id].seq === seq) return;
    pending.push(fetch('/v1/live/' + encodeURIComponent(id) + '/notes.html', {credentials:'same-origin'}).then(function (r) {
      if (r.status === 404) { delete liveNotes[id]; return null; }
      if (!r.ok) throw new Error('notes failed');
      return r.text();
    }).then(function (html) { if (html !== null) liveNotes[id] = {seq: seq, html: html}; }).catch(function () {}));
  });
  Object.keys(liveNotes).forEach(function (id) { if (!wanted[id]) delete liveNotes[id]; });
  return Promise.all(pending);
}
function renderLiveNotes(item) {
  var box = document.getElementById('live-overlay-notes'), body = document.getElementById('live-overlay-notes-body');
  if (!box || !body || !item) return;
  var n = liveNotes[item.session_id], html = (n && n.html) || '';
  box.hidden = !html;
  if (body._lastMarkup !== html) { body.innerHTML = html; body._lastMarkup = html; }
}
function liveCard(item) {
  return '<div class="live-card" role="button" tabindex="0" data-live-id="' + escapeHtml(item.session_id) + '" aria-label="Open live transcript for ' + escapeHtml(item.name) + '"><div class="live-card-head"><span class="badge live">' + dot() + 'Live</span><h3>' + escapeHtml(item.name) + '</h3><span class="open-hint">Open transcript ' + icon('open') + '</span></div><div class="help">' + escapeHtml(item.device) + ' · started ' + fmtDate(item.started_wall) + '</div>' + '<div class="live-preview">' + (liveText(item, 6) || '<p class="lp-empty">' + LIVE_EMPTY_TEXT + '</p>') + '</div>' + liveNotesBlock(item) + '</div>';
}
function liveItem(id) {
  return liveItems.find(function (item) { return item.session_id === id; });
}
function renderLiveOverlay(item, preservePosition) {
  var scroll = document.getElementById('live-transcript-scroll');
  var wasAtBottom = scroll && (scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 24);
  var oldTop = scroll ? scroll.scrollTop : 0;
  // On phones the overlay page scrolls instead of the panel; follow it the same way.
  var page = document.querySelector('#live-overlay .doc-scroll');
  var pageAtBottom = page && (page.scrollHeight - page.scrollTop - page.clientHeight < 24);
  var content = document.getElementById('live-transcript-content');
  renderLiveNotes(item);
  if (!content || !item) return;
  var next = liveText(item) || '<p class="lp-empty">' + LIVE_EMPTY_TEXT + '</p>';
  if (content._lastMarkup === next) return;
  content.innerHTML = next; content._lastMarkup = next;
  if (scroll && preservePosition) {
    scroll.scrollTop = wasAtBottom ? scroll.scrollHeight : oldTop;
  } else if (scroll) {
    scroll.scrollTop = scroll.scrollHeight;
  }
  if (page && (pageAtBottom || !preservePosition)) page.scrollTop = page.scrollHeight;
}
function openLive(item) {
  if (!item) return;
  activeLiveId = item.session_id;
  liveOverlayPreviousFocus = document.activeElement;
  var overlay = document.getElementById('live-overlay');
  document.getElementById('live-overlay-title').textContent = item.name || 'Live transcript';
  document.getElementById('live-name').value = item.name || '';
  document.getElementById('live-overlay-meta').textContent = (item.device || 'Unknown device') + ' · started ' + fmtDate(item.started_wall);
  renderLiveOverlay(item, false);
  overlay.classList.add('open');
  overlay.setAttribute('aria-hidden', 'false');
  document.body.classList.add('overlay-open');
  // Scroll only once the overlay is visible (a hidden panel has no height), and
  // also on reopen, when the unchanged text skips the render above.
  var liveScroll = document.getElementById('live-transcript-scroll');
  if (liveScroll) liveScroll.scrollTop = liveScroll.scrollHeight;
  // On phones the panel grows to full height and the overlay itself scrolls.
  var docScroll = overlay.querySelector('.doc-scroll');
  if (docScroll) docScroll.scrollTop = docScroll.scrollHeight;
  history.pushState({liveTranscript: item.session_id}, '', '#live-' + encodeURIComponent(item.session_id));
  document.getElementById('live-close').focus();
}
function closeLive(fromHistory) {
  var overlay = document.getElementById('live-overlay');
  if (!overlay.classList.contains('open')) return;
  overlay.classList.remove('open');
  overlay.setAttribute('aria-hidden', 'true');
  document.body.classList.remove('overlay-open');
  activeLiveId = null;
  if (!fromHistory && location.hash.indexOf('#live-') === 0) history.back();
  if (liveOverlayPreviousFocus && liveOverlayPreviousFocus.focus) liveOverlayPreviousFocus.focus();
}
function bindLiveCards() {
  document.querySelectorAll('[data-live-id]').forEach(function (card) {
    card.addEventListener('click', function (event) {
      if (event.target.closest('a')) return;  // a link in the Your notes block opens the link, not the overlay
      openLive(liveItem(card.dataset.liveId));
    });
    card.addEventListener('keydown', function (event) {
      if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); openLive(liveItem(card.dataset.liveId)); }
    });
  });
}
function updateLiveOverlay() {
  if (!activeLiveId) return;
  var item = liveItem(activeLiveId);
  if (!item) {
    // The meeting stopped streaming: say so instead of freezing on its last lines.
    document.getElementById('live-overlay-meta').textContent = 'This meeting has ended. Its full transcript appears under Meetings once processing finishes.';
    return;
  }
  document.getElementById('live-overlay-title').textContent = item.name || 'Live transcript';
  var nameInput = document.getElementById('live-name');
  if (nameInput && document.activeElement !== nameInput) nameInput.value = item.name || '';
  document.getElementById('live-overlay-meta').textContent = (item.device || 'Unknown device') + ' · started ' + fmtDate(item.started_wall);
  renderLiveOverlay(item, true);
}
document.addEventListener('keydown', function (event) {
  var overlay = document.getElementById('live-overlay');
  if (!overlay.classList.contains('open')) return;
  if (event.key === 'Escape') closeLive(false);
  else trapFocus(event, overlay);
});
window.addEventListener('popstate', function () { closeLive(true); });
function recentRow(row) {
  var notesReady = row.review && row.review.status === 'done';
  var length = Number(row.duration_sec) > 0 ? ' · ' + fmtDuration(row.duration_sec) : '';
  return '<li class="mrow"><a class="mrow-main" href="/sessions/' + encodeURIComponent(row.session_id) + '"><span class="mrow-title">' + escapeHtml(row.name || row.session_id) + '</span><span class="mrow-sub">' + fmtDate(row.created, true) + ' · ' + escapeHtml(row.device || 'Unknown device') + length + '</span></a><span class="mrow-badges">' + typeChip(row) + processingBadge(row) + (notesReady ? badge('done', 'Notes ready') : '') + '</span></li>';
}
function loadOverview() {
  fetch('/v1/sessions?per_page=8', {credentials:'same-origin'}).then(function (r) { if (!r.ok) throw new Error('load failed'); return r.json(); }).then(function (data) {
    document.getElementById('total-count').textContent = data.total;
    document.getElementById('audio-count').textContent = data.audio_total == null ? '—' : data.audio_total;
    var recent = document.getElementById('recent-list');
    var markup = data.items.length ? data.items.map(recentRow).join('') : '<li class="empty-teach"><h3>No meetings yet</h3><p>Record a meeting with the Windows client, or upload a recording here. Finished meetings appear in your library with their notes.</p><div class="row"><a class="btn primary" href="/install">Install the Windows client</a></div></li>';
    if (recent._lastMarkup !== markup) { recent.innerHTML = markup; recent._lastMarkup = markup; }
    recent.setAttribute('aria-busy', 'false');
  }).catch(function () {
    var recent = document.getElementById('recent-list');
    if (recent._lastMarkup == null) { recent.innerHTML = '<li class="empty">Could not load recent meetings. Retrying…</li>'; recent.setAttribute('aria-busy', 'false'); }
  });
}
function loadLive() {
  fetch('/v1/live', {credentials:'same-origin'}).then(function (r) { if (!r.ok) throw new Error('load failed'); return r.json(); }).then(function (data) {
    var items = data.items || [];
    return syncLiveNotes(items).then(function () {
      liveItems = items;
      document.getElementById('live-count').textContent = data.total;
      var section = document.getElementById('live-section');
      section.style.display = data.total ? '' : 'none';
      var list=document.getElementById('live-list'), markup=liveItems.map(liveCard).join('');
      if(list._lastMarkup!==markup){list.innerHTML=markup;list._lastMarkup=markup;bindLiveCards();}
      updateLiveOverlay();
    });
  }).catch(function () {});
}
loadOverview(); loadLive(); setInterval(loadOverview, 10000); setInterval(loadLive, 2500);
</script>
<div class="overlay live-overlay" id="live-overlay" role="dialog" aria-modal="true" aria-hidden="true" aria-labelledby="live-overlay-title">
  <div class="overlay-inner"><div class="sheet">
    <div class="doc-bar"><button class="btn ghost" id="live-close" type="button" aria-label="Close live transcript">"""
        + _icon("back")
        + r"""<span>Back</span></button></div>
    <div class="doc-scroll"><div class="doc-wrap">
      <header class="doc-title"><h1 id="live-overlay-title">Live transcript</h1><div class="meta" id="live-overlay-meta"></div></header>
      <form class="rename-live" id="live-name-form"><label class="sr-only" for="live-name">Meeting name</label><input type="text" id="live-name" maxlength="200" autocomplete="off" required placeholder="Meeting name"><button type="submit" class="btn secondary">Save name</button><span class="help" id="live-name-status" role="status"></span></form>
      <p class="live-section">Live preview</p>
      <div class="live-transcript-scroll" id="live-transcript-scroll" tabindex="0" aria-label="Live transcript text" aria-live="polite"><div id="live-transcript-content" class="live-preview"></div></div>
      <div class="live-notes" id="live-overlay-notes" hidden><p class="live-section">Your notes</p><div class="scratchpad-rendered" id="live-overlay-notes-body" aria-label="Your notes"></div></div>
    </div></div>
  </div></div>
</div>
<script>
document.getElementById('live-close').addEventListener('click', function () { closeLive(false); });
document.getElementById('live-overlay').addEventListener('click', function (event) { if (event.target === this || event.target.classList.contains('overlay-inner')) closeLive(false); });
document.getElementById('live-name-form').addEventListener('submit', function (event) {
  event.preventDefault();
  var item = activeLiveId && liveItem(activeLiveId);
  var input = document.getElementById('live-name');
  var status = document.getElementById('live-name-status');
  if (!item || !input.value.trim()) return;
  status.textContent = 'Saving…';
  fetch('/v1/live/' + encodeURIComponent(item.session_id), {method:'PATCH', credentials:'same-origin', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:input.value.trim()})})
    .then(function (response) { return response.json().then(function (data) { if (!response.ok) throw new Error(data.detail || 'Could not save name'); return data; }); })
    .then(function (data) { item.name = data.name; status.textContent = 'Saved'; updateLiveOverlay(); document.getElementById('live-list').innerHTML = liveItems.map(liveCard).join(''); bindLiveCards(); })
    .catch(function (error) { status.textContent = error.message; });
});
</script>
"""
    )
    return _shell("Home", body, token_configured=token_configured, active="home", appearance=appearance)
