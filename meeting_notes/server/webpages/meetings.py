"""The Meetings page and the meeting (session) view."""

from __future__ import annotations

import json
from typing import Optional

from meeting_notes.server import splitmerge_ui
from .people import _NP_DIALOG_HTML, _PEOPLE_JS, _PEOPLE_LINK_JS, _PPICK_DIALOG_HTML
from .shared import _CONFIRM_DIALOG_HTML, _JS_HELPERS, _NOTE_TYPE_TIP
from .shell import _icon, _shell


# -- meetings (formerly "saved transcriptions") ---------------------------------------------------


def render_transcriptions_page(
    *,
    token_configured: bool,
    initial_session_id: Optional[str] = None,
    initial_view: Optional[str] = None,
    ai_enabled: bool = True,
    appearance: str = "system",
    page_title: Optional[str] = None,
    diarization_enabled: bool = False,
    voice_profiles_enabled: bool = False,
) -> str:
    """The Meetings page: the list, and the meeting document (detail overlay).

    ``diarization_enabled`` adds "Find speakers again" (meeting menu and bulk bar); ``voice_profiles_enabled`` (voice
    profiles on, which needs speaker labels on too) adds "Update voice profiles". With either off its markup is
    left out altogether.

    ``initial_view="notes"`` opens ``initial_session_id`` straight onto its
    meeting notes (the server knows the notes are finished). ``ai_enabled``
    gates the per-row "Generate" button shown for meetings without notes.

    Everything in the list comes from ``GET /v1/sessions`` (search, state
    filter and pagination included) and the document from
    ``GET /v1/sessions/{id}``, so the page never has to know how many
    meetings exist. The timeline is drawn from that detail response's
    ``segments`` (``start``/``end`` seconds plus ``track``/``label``).
    """
    initial = json.dumps(initial_session_id)
    initial_view_js = json.dumps(initial_view)
    find_speakers_item = (
        '<button type="button" role="menuitem" id="find-speakers" title="Work out who said what again from the '
        f'audio. The text stays as it is.">{_icon("users")}<span>Find speakers again</span></button>'
        if diarization_enabled else ""
    )
    reprofile_item = (
        '<button type="button" role="menuitem" id="reprofile" title="Match this meeting\'s voices to the people '
        f'again, without the audio">{_icon("users")}<span>Update voice profiles</span></button>'
        if voice_profiles_enabled else ""
    )
    bulk_find_speakers = (
        '<button class="btn secondary" id="bulk-find-speakers" disabled title="Work out who said what again from '
        'the audio. The text stays as it is.">Find speakers</button>' if diarization_enabled else ""
    )
    bulk_reprofile = (
        '<button class="btn secondary" id="bulk-reprofile" disabled title="Match the voices to the people again, '
        'without the audio">Update voice profiles</button>' if voice_profiles_enabled else ""
    )
    skeleton = "".join(
        '<li class="mrow skel" aria-hidden="true"><span class="skel-bar w1"></span><span class="skel-bar w2"></span></li>'
        for _ in range(7)
    )
    body = (
        f"""
<div class="page list-page">
<header class="page-head">
  <h1>Meetings</h1>
  <span class="count" id="meeting-count" aria-live="polite"></span>
  <div class="head-tools">
    <a class="btn ghost" href="/meetings/trash" id="open-trash">{_icon("trash")}<span>Recently deleted</span></a>
    <label class="sr-only" for="state">Filter by state</label>
    <select id="state"><option value="">All states</option><option value="done">Complete</option><option value="running">Running</option><option value="queued">Queued</option><option value="error">Error</option></select>
    <input type="hidden" id="type-filter" value="">
  </div>
</header>
<nav class="tabs type-tabs" id="type-tabs" aria-label="Filter by note type" hidden></nav>
<div class="list-error" id="list-error" role="alert" hidden><span>Could not load the meetings list. It will retry on its own.</span><button type="button" class="btn secondary" id="list-retry">Try again</button></div>
<div class="mtable" role="table" aria-label="Meetings">
<div class="mlist-head" role="row">
  <div class="c-sel" role="columnheader"><label class="sel"><input id="select-all" type="checkbox" aria-label="Select all visible meetings"></label></div>
  <div class="col-name" role="columnheader">Name</div><div class="col-time" role="columnheader">Time</div><div class="col-status" role="columnheader">Transcript</div><div class="col-type" role="columnheader">Note type</div><div class="col-notion" role="columnheader">In Notion</div><div class="col-notes" role="columnheader">Notes generated</div>
</div>
<ul class="mlist" id="rows" role="rowgroup" aria-busy="true">{skeleton}</ul>
</div>
<footer class="pager"><button id="more" class="btn secondary" style="display:none">Load more</button></footer>
</div>
<div class="bulk-actions" role="region" aria-label="Bulk actions">
  <span class="selection-count" id="selection-count" role="status">Select meetings for bulk actions</span>
  <span class="bulk-buttons">
    <button class="btn primary" id="bulk-build" disabled>Build meeting notes</button>
    <button class="btn secondary" id="bulk-notion" disabled title="Copies each meeting's notes to the Notion page set for its note type">Send to Notion</button>
    <button class="btn secondary" id="bulk-export" disabled title="Download the selected meetings' transcripts and notes as Markdown in one zip">Export</button>
    <button class="btn secondary" id="bulk-retranscribe" disabled>Retranscribe</button>
    {bulk_find_speakers}
    {bulk_reprofile}
    <button class="btn secondary" id="bulk-combine" disabled title="Select two or more meetings to combine">Combine</button>
    <button class="btn danger" id="bulk-delete-audio" disabled>Delete audio</button>
    <button class="btn danger" id="bulk-delete" disabled>Delete</button>
  </span>
  <button class="btn ghost icon-only" id="bulk-clear" type="button" aria-label="Clear selection" title="Clear selection">{_icon("x")}</button>
</div>

{_CONFIRM_DIALOG_HTML}
{_PPICK_DIALOG_HTML + _NP_DIALOG_HTML if voice_profiles_enabled else ""}
{splitmerge_ui.dialogs_html(_icon)}
<div class="overlay" id="detail-overlay" role="dialog" aria-modal="true" aria-hidden="true" aria-labelledby="overlay-title">
  <div class="overlay-inner"><div class="sheet" id="sheet" data-view="transcript">
    <div class="doc-bar">
      <button class="btn ghost" id="close-overlay" type="button" aria-label="Back to meetings">{_icon("back")}<span>Meetings</span></button>
      <span class="doc-status" id="review-status" role="status"></span>
      <div class="doc-actions">
        <div class="type-row" id="type-row" hidden>
          <label class="type-label" for="notes-template">Note type</label>
          <select id="notes-template" class="style-select" title="The note type decides how the summary is written and where it is saved in Notion" hidden></select>
          <button class="btn secondary notes-only" id="notes-regen" type="button" hidden title="{_NOTE_TYPE_TIP}">{_icon("refresh")}<span>Regenerate</span></button>
        </div>
        <button class="btn secondary notes-only" id="notes-copy" type="button" aria-label="Copy notes as Markdown">{_icon("copy")}<span>Copy</span></button>
        <button class="btn secondary notes-only" id="notes-download" type="button" aria-label="Download the notes as notes.md" title="Download the notes (Markdown)">{_icon("download")}<span>notes.md</span></button>
        <button class="btn secondary transcript-only" id="transcript-download" type="button" aria-label="Download the transcript as transcript.md" title="Download the transcript (Markdown)" disabled>{_icon("download")}<span>transcript.md</span></button>
        <button class="btn ghost icon-only" id="doc-find" type="button" aria-expanded="false" aria-controls="find-bar" aria-label="Find in this meeting" title="Find in this meeting (Ctrl+F or /)">{_icon("search")}</button>
        <div class="menu-wrap">
          <button class="btn ghost icon-only" id="doc-more" type="button" aria-haspopup="menu" aria-expanded="false" aria-controls="doc-menu" aria-label="More actions" title="More actions">{_icon("more")}</button>
          <div class="menu" id="doc-menu" role="menu" aria-label="Meeting actions" hidden>
            <button type="button" role="menuitem" id="edit-meeting-name" aria-expanded="false">{_icon("edit")}<span>Rename meeting</span></button>
            <button type="button" role="menuitem" id="edit-summary-name" aria-expanded="false">{_icon("edit")}<span>Rename summary</span></button>
            <button type="button" role="menuitem" id="edit-people" aria-expanded="false" title="How many people are in this meeting, including you. Helps tell speakers apart.">{_icon("edit")}<span>Set number of people…</span></button>
            <button type="button" role="menuitem" id="notes-retry">{_icon("refresh")}<span>Regenerate notes</span></button>
            <button type="button" role="menuitem" id="retranscribe">{_icon("refresh")}<span>Retranscribe</span></button>
            {find_speakers_item}
            {reprofile_item}
            <button type="button" role="menuitem" id="split-meeting">{_icon("split")}<span>Split meeting…</span></button>
            <button type="button" role="menuitem" id="combine-meeting">{_icon("merge")}<span>Combine with another meeting…</span></button>
            <button type="button" role="menuitem" id="export-all" title="Download the transcript and notes as Markdown in a zip">{_icon("download")}<span>Export all (.zip)</span></button>
            <div class="menu-sep" role="separator"></div>
            <button type="button" role="menuitem" class="danger-item" id="delete-audio">{_icon("trash")}<span>Delete audio</span></button>
            <button type="button" role="menuitem" class="danger-item" id="delete-entry">{_icon("trash")}<span>Delete meeting</span></button>
          </div>
        </div>
      </div>
    </div>
    <div class="find-bar" id="find-bar" role="search" hidden>
      <span class="find-ic">{_icon("search")}</span>
      <label class="sr-only" for="find-input">Find in this meeting</label>
      <input type="text" id="find-input" autocomplete="off" spellcheck="false" enterkeyhint="search" maxlength="200" placeholder="Find in the transcript">
      <span class="find-count" id="find-count" role="status" aria-live="polite"></span>
      <button class="btn ghost icon-only" id="find-prev" type="button" aria-label="Previous match" title="Previous match (Shift+Enter)" disabled>{_icon("chev-up")}</button>
      <button class="btn ghost icon-only" id="find-next" type="button" aria-label="Next match" title="Next match (Enter)" disabled>{_icon("chev-down")}</button>
      <button class="btn ghost icon-only" id="find-close" type="button" aria-label="Close find" title="Close (Esc)">{_icon("x")}</button>
    </div>
    <div class="doc-scroll" id="sheet-body">
    <div class="type-bar" id="type-bar" hidden>
      <div class="notion-box" id="notion-box" title="{_NOTE_TYPE_TIP}" hidden></div>
    </div><div class="doc-wrap">
      <header class="doc-title">
        <h1 id="overlay-title" title="Click to rename">Meeting</h1>
        <form class="rename" id="rename-form" hidden><label class="sr-only" for="rename-input">Meeting name</label><input type="text" id="rename-input" maxlength="200" autocomplete="off" required><button type="submit" class="btn primary">Save name</button><button type="button" class="btn ghost" id="rename-cancel">Cancel</button><p class="err" id="rename-error" role="alert"></p></form>
        <form class="rename" id="people-form" hidden><label for="people-input">People in this meeting, including you</label><input type="number" id="people-input" min="1" max="50" step="1" inputmode="numeric" placeholder="Auto" style="max-width:110px"><button type="submit" class="btn primary">Save</button><button type="button" class="btn ghost" id="people-cancel">Cancel</button><p class="err" id="people-error" role="alert"></p></form>
        <div class="meta"><span id="overlay-meta"></span><span class="mid" id="overlay-people" role="button" tabindex="0" style="cursor:pointer" title="How many people are in this meeting, including you. Click to change." hidden></span><span class="mid" id="overlay-board" hidden></span><span class="mid" id="overlay-style" title="The note type used for these notes: it set how they were written and where they are saved in Notion" hidden></span></div>
      </header>
      {splitmerge_ui.continuation_hint_html(_icon("merge"), _icon("x"))}
      <div class="tabs" role="group" aria-label="Meeting views">
        <button id="queue-review" type="button" aria-pressed="false">Notes</button>
        <button class="active" id="show-transcript" type="button" aria-pressed="true">Transcript</button>
      </div>
      <section class="strip-wrap" id="strip-wrap" aria-label="Session timeline" hidden>
        <div class="strip-head"><span class="strip-title">Timeline</span><span class="strip-help">Select a block to jump to that moment in the transcript.</span><span class="strip-total" id="strip-total"></span></div>
        <div class="strip" id="session-strip" role="group" aria-label="Session timeline, one block per transcript segment. Arrow keys move between blocks, Enter shows the segment in the transcript."></div>
      </section>
      <section class="transcript-pane" id="transcript-pane" aria-label="Transcript"><div id="people-suggestions" class="people-sugg" hidden></div><div id="overlay-segments" class="transcript"></div></section>
      <section class="notes-head" id="notes-pane" hidden>
        <h2 id="notes-title">Meeting summary</h2>
        <form class="rename" id="summary-rename-form" hidden><label class="sr-only" for="summary-rename-input">Meeting summary name</label><input type="text" id="summary-rename-input" maxlength="200" autocomplete="off" required><button type="submit" class="btn primary">Save name</button><button type="button" class="btn ghost" id="summary-rename-cancel">Cancel</button><p class="err" id="summary-rename-error" role="alert"></p></form>
        <div class="help notes-meta" id="notes-meta"></div>
        <div id="notes-state" class="notes-state" role="status"></div>
      </section>
      <section id="notes-document-pane" hidden><article id="notes-document" class="notes-doc" aria-label="Meeting summary"></article></section>
      <details class="extras your-notes" id="your-notes" open><summary>Your notes</summary><div class="extras-body">
        <p class="help">Typed in the recorder during the meeting. The AI notes use them as context.</p>
        <div class="scratchpad-rendered" id="your-notes-text" aria-label="Your notes"></div>
        <p class="help scratchpad-empty" id="your-notes-empty">Nothing typed in the recorder for this meeting.</p>
      </div></details>
      <details class="extras" id="meeting-extras"><summary>Recording and processing details</summary><div class="extras-body">
        <section aria-labelledby="transcription-progress-heading"><h2 id="transcription-progress-heading">Processing status</h2><ol class="checklist" id="transcription-checklist"></ol></section>
        <div id="audio-players" class="audio-grid"></div>
      </div></details>
    </div></div>
  </div></div>
</div>
<script>
"""
        + _JS_HELPERS
        + "\n"
        + _PEOPLE_JS
        + _PEOPLE_LINK_JS
        + f"var aiEnabled = {json.dumps(bool(ai_enabled))};"
        + f"var diarizationOn = {json.dumps(bool(diarization_enabled))}, voiceProfilesOn = {json.dumps(bool(voice_profiles_enabled))};"
        + r"""
var listState = {page:1, perPage:50, loaded:0, total:0, items:[], seq:0};
var rowInfo = {};
var currentSession = null;
var detailPollTimer = null;
var detailRequest = 0;
var currentReview = null;
var noteTemplates = [], defaultTemplateId = '', reviewTemplateId = '';
var notesPollTimer = null;
var currentMarkdown = '';
var notesRequest = 0;
var notionTimer = null, notionRequest = 0;
var detailReturnFocus = null, detailReturnKey = null;
var transcriptExplicit = false;
var pendingNotesDefault = false;
var stripState = {data:null, drawnFor:null, hit:null};
var renameMeeting = null, renameSummary = null;
var sheetEl = document.getElementById('sheet');
var menuEl = document.getElementById('doc-menu'), moreBtn = document.getElementById('doc-more');

function skeletonLines(){return '<div class="notes-loading" aria-hidden="true"><span class="skel-bar" style="width:70%"></span><span class="skel-bar"></span><span class="skel-bar" style="width:85%"></span><span class="skel-bar" style="width:55%"></span></div><span class="sr-only">Loading…</span>';}
function notesBadge(row) {
  // The "Notes generated" cell: plain text, no pill. "Generate" is a quiet text link, only when there are no notes yet.
  var status=String((row.review||{}).status||'none').toLowerCase(), id=encodeURIComponent(row.session_id||'');
  if(status==='done')return '<a class="cell-yes row-open" href="/sessions/'+id+'" title="Open the meeting notes">'+icon('check',14)+'<span>Yes</span></a>';
  if(status==='running'||status==='queued')return '<span class="cell-run">'+dot()+'<span>Building…</span></span>';
  var generate=(aiEnabled&&processingState(row).key==='complete')?'<button type="button" class="link-btn notes-generate" data-generate="'+escapeHtml(row.session_id)+'" aria-label="Generate meeting notes for '+escapeHtml(row.name||row.session_id)+'"><span>Generate</span></button>':'';
  if(status==='error')return '<span class="cell-err" title="Building the notes failed. Open the meeting to try again.">'+icon('alert',14)+'<span>Failed</span></span>'+generate;
  return '<span class="cell-none" aria-label="No notes yet">—</span>'+generate;
}
function generateNotes(btn) {
  if (btn.disabled) return;
  btn.disabled = true; btn.querySelector('span').textContent = 'Queuing…';
  fetch('/v1/sessions/'+encodeURIComponent(btn.dataset.generate)+'/review', {method:'POST', credentials:'same-origin'})
    .then(function(r) { if (!r.ok) throw new Error('Unable to queue meeting notes'); return r.json(); })
    .then(function() { var cell=btn.closest('.mrow-notes'); if(cell)cell.innerHTML=notesBadge({session_id:btn.dataset.generate,review:{status:'queued'}}); return loadRows(true); })
    .catch(function(e) { btn.disabled = false; btn.querySelector('span').textContent = 'Generate'; notify(e.message,'error'); });
}
function notionChip(row) {
  // The "In Notion" cell: a check and "Yes" (linked to the Notion page), "Sending…", "Failed", or a dash.
  var n=row.notion;if(!n||!n.state)return '<span class="cell-none" aria-label="Not in Notion">—</span>';
  if(n.state==='copied'){var href=n.url||'';return href?'<a class="cell-yes notion-link" href="'+escapeHtml(href)+'" target="_blank" rel="noopener" title="Open this meeting in Notion">'+icon('check',14)+'<span>Yes</span></a>':'<span class="cell-yes">'+icon('check',14)+'<span>Yes</span></span>';}
  if(n.state==='removing')return '<span class="cell-run" title="Removing from Notion">'+dot()+'<span>Removing…</span></span>';
  if(n.state==='pending')return '<span class="cell-run" title="'+escapeHtml(n.retrying&&n.error?n.error:'Sending to Notion')+'">'+dot()+'<span>'+(n.retrying?'Retrying…':'Sending…')+'</span></span>';
  return '<a class="cell-err row-open" href="/sessions/'+encodeURIComponent(row.session_id)+'" title="'+escapeHtml(n.error||'The copy to Notion failed. Open the meeting to retry.')+'">'+icon('alert',14)+'<span>Failed</span></a>';
}
function transcriptCell(row) {
  var st=processingState(row), pct=st.pct==null||st.key==='complete'?'':' '+Math.round(st.pct)+'%';
  var kind=st.key==='complete'?'ok':(st.key==='error'?'err':(st.key==='transcribing'||st.key==='queued'||st.key==='uploading'?'run':'idle'));
  var tip=st.key==='error'?(st.detail||row.latest_error||''):'';
  return '<span class="cell-st '+kind+'"'+(tip?' title="'+escapeHtml(tip)+'"':'')+'>'+dot()+'<span>'+escapeHtml(st.label+pct)+'</span></span>'+(st.where?'<span class="cell-sub">'+escapeHtml(st.where)+'</span>':'');
}
function typeCell(row) {
  var t=row&&row.note_type;if(!t||!t.name)return '<span class="cell-none" aria-label="No note type">—</span>';
  return t.applied?'<span class="cell-type" title="Notes were made with the '+escapeHtml(t.name)+' note type">'+escapeHtml(t.name)+'</span>':'<span class="cell-type planned" title="Notes will use the '+escapeHtml(t.name)+' note type">'+escapeHtml(t.name)+'</span>';
}
function meetingRow(row) {
  var name=escapeHtml(row.name||row.session_id),status=String((row.review||{}).status||'none').toLowerCase(),id=escapeHtml(row.session_id);
  var d=Math.max(0,Number(row.duration_sec)||0);
  var sub=escapeHtml(row.device||row.platform||'Unknown device')+(d?' · '+fmtDuration(d):'');
  return '<li class="mrow'+(status==='done'?' notes-ready':(status==='queued'||status==='running'?' notes-pending':''))+'" role="row" data-id="'+id+'">'
    +'<div class="c-sel" role="cell"><label class="sel"><span class="row-ic">'+icon('notes')+'</span><input class="row-select" type="checkbox" value="'+id+'" aria-label="Select '+name+'"></label></div>'
    +'<div class="mrow-main c-name" role="rowheader"><a class="row-open mrow-title" href="/sessions/'+encodeURIComponent(row.session_id)+'">'+name+'</a><span class="mrow-sub">'+sub+'<span class="sub-id"> · <span class="mid">'+escapeHtml(row.board||'')+'</span></span><span class="sub-audio"> · '+(row.has_audio?fmtBytes(row.audio_bytes):'No audio')+'</span></span></div>'
    +'<div class="c-time" role="cell">'+fmtDate(row.created,true)+'</div>'
    +'<div class="c-field mrow-status" role="cell" data-label="Transcript">'+transcriptCell(row)+'</div>'
    +'<div class="c-field mrow-type" role="cell" data-label="Note type">'+typeCell(row)+'</div>'
    +'<div class="c-field mrow-notion" role="cell" data-label="In Notion">'+notionChip(row)+'</div>'
    +'<div class="c-field mrow-notes" role="cell" data-label="Notes">'+notesBadge(row)+'</div></li>';
}
function emptyRows() {
  var q=document.getElementById('q').value.trim(), st=document.getElementById('state').value, nt=document.getElementById('type-filter').value;
  var ntName=nt?((noteTemplates.filter(function(t){return t.id===nt;})[0]||{}).name||''):'';
  var parsedQ=parseSearchQuery(q);
  if(parsedQ.terms.length||st||nt){
    var chips=parsedQ.terms.map(function(t){return '<code class="q-chip">'+(t.field?'<b>'+escapeHtml(t.field)+':</b> ':'')+escapeHtml(t.text)+'</code>';});
    var also=[];if(st)also.push('that state');if(nt)also.push('the '+escapeHtml(ntName)+' note type');
    var what=chips.concat(also).join(chips.length+also.length>2?', ':' and ');
    var tip=parsedQ.terms.length>1?'<span class="empty-tip">Every term has to match. Try removing one.</span>':'';
    return '<li class="state-row" role="row"><div class="empty" role="cell"><p><strong>No meetings found.</strong> Nothing matches '+what+'.'+tip+'</p><button type="button" class="btn secondary" id="clear-filters">Clear search and filter</button></div></li>';
  }
  return '<li class="state-row" role="row"><div class="empty-teach" role="cell"><h2>No meetings yet</h2><p>Each recording you make becomes a meeting here, with its transcript and notes.</p><ol><li>Install the Windows client and record a call, or</li><li>upload an audio file from Home.</li></ol><div class="row"><a class="btn primary" href="/install">Install the Windows client</a><a class="btn secondary" href="/">Upload a recording</a></div></div></li>';
}
// The bulk buttons on this page ("Find speakers" and "Update voice profiles" only exist when their feature is on).
function bulkIds() {
  return ['bulk-build','bulk-notion','bulk-export','bulk-retranscribe','bulk-find-speakers','bulk-reprofile','bulk-delete-audio','bulk-delete']
    .filter(function(id) { return document.getElementById(id); });
}
function selectedIds() { return Array.from(document.querySelectorAll('.row-select:checked')).map(function(el) { return el.value; }); }
function updateSelection() {
  var ids = selectedIds(), disabled = !ids.length;
  document.getElementById('selection-count').textContent = ids.length ? ids.length + ' selected' : 'Select meetings for bulk actions';
  document.querySelector('.bulk-actions').classList.toggle('has-selection',!!ids.length);
  document.getElementById('rows').classList.toggle('has-sel',!!ids.length);
  document.body.classList.toggle('has-bulk',!!ids.length);
  bulkIds().forEach(function(id) { document.getElementById(id).disabled = disabled; });
  var all = document.querySelectorAll('.row-select'), master = document.getElementById('select-all');
  master.checked = !!all.length && ids.length === all.length;
  master.indeterminate = ids.length > 0 && ids.length < all.length;
}
function meetingItems(ids) { return ids.map(function(id) { return meetingSummary(rowInfo[id] || {session_id:id}); }); }
function deleteAudioDialog(ids) {
  return confirmDialog({
    title: ids.length === 1 ? 'Delete the audio for this meeting?' : 'Delete the audio for ' + ids.length + ' meetings?',
    lead: 'The recordings are removed from the server. Transcripts and notes are kept. This cannot be undone.',
    items: meetingItems(ids), confirmLabel: 'Delete audio'
  });
}
function trashDialog(ids) {
  return confirmDialog({
    title: ids.length === 1 ? 'Move this meeting to Recently deleted?' : 'Move ' + ids.length + ' meetings to Recently deleted?',
    lead: 'You can restore ' + (ids.length === 1 ? 'it' : 'them') + ' from Recently deleted for 30 days. After that ' + (ids.length === 1 ? 'it is' : 'they are') + ' removed permanently.',
    items: meetingItems(ids), confirmLabel: 'Move to Recently deleted'
  });
}
// Soft delete: the server moves each meeting to Recently deleted; the toast offers Undo.
function moveToTrash(ids, via) {
  return Promise.allSettled(ids.map(function(id) {
    return fetch('/v1/sessions/'+encodeURIComponent(id)+'?via='+via, {method:'DELETE', credentials:'same-origin'}).then(function(r) { if (!r.ok) throw new Error('Delete failed'); return id; });
  })).then(function(results) {
    var done = results.filter(function(r) { return r.status === 'fulfilled'; }).map(function(r) { return r.value; });
    var failed = ids.length - done.length;
    return loadRows(true).then(function(refreshed) {
      var message = done.length ? plural(done.length, 'meeting', 'meetings') + ' moved to Recently deleted' + (failed ? '. ' + failed + ' could not be deleted.' : '') : failed + ' could not be deleted.';
      if (done.length) notify(message, '', {label:'Undo', onClick:function() { undoTrash(done); }});
      else notify(message, 'error');
      if (!refreshed) notify('Could not refresh the meetings list. Please try again.', 'error');
    });
  });
}
function undoTrash(ids) {
  Promise.allSettled(ids.map(function(id) {
    return fetch('/v1/trash/'+encodeURIComponent(id)+'/restore', {method:'POST', credentials:'same-origin'}).then(function(r) { if (!r.ok) throw new Error('Restore failed'); });
  })).then(function(results) {
    var failed = results.filter(function(r) { return r.status === 'rejected'; }).length;
    return loadRows(true).then(function() {
      if (failed) notify(failed + ' could not be restored. Find ' + (failed === 1 ? 'it' : 'them') + ' in Recently deleted.', 'error');
      else notify(ids.length === 1 ? 'Meeting restored.' : ids.length + ' meetings restored.');
    });
  });
}
function runBulk(path, method, confirmed) {
  var ids = selectedIds(); if (!ids.length) return;
  var buttons = bulkIds(); buttons.forEach(function(id) { document.getElementById(id).disabled = true; });
  Promise.allSettled(ids.map(function(id) { return fetch('/v1/sessions/'+encodeURIComponent(id)+path, {method:method, credentials:'same-origin'}).then(function(r) { if (!r.ok) throw new Error('Action failed for '+id); return r; }); }))
    .then(function(results) { return loadRows(true).then(function(refreshed) { var failed=results.filter(function(result){return result.status==='rejected';}); if(failed.length)notify(failed.length+' of '+ids.length+' actions failed.','error'); else notify('Done for '+ids.length+' meeting'+(ids.length===1?'':'s')+'.'); if(!refreshed)notify('Could not refresh the meetings list. Please try again.','error'); }); });
}
function downloadZip(url, options, fallbackName) {
  return fetch(url, Object.assign({credentials:'same-origin'}, options || {})).then(function(r) {
    if (r.redirected) throw new Error('Your session has expired. Sign in again.');
    if (!r.ok) return r.json().catch(function() { return {}; }).then(function(body) { throw new Error(body.detail || 'Export failed.'); });
    var disposition = r.headers.get('Content-Disposition') || '', star = /filename\*=UTF-8''([^;]+)/i.exec(disposition), plain = /filename="([^"]+)"/i.exec(disposition), name = fallbackName;
    try { name = star ? decodeURIComponent(star[1]) : (plain ? plain[1] : fallbackName); } catch (e) { name = plain ? plain[1] : fallbackName; }
    return r.blob().then(function(blob) { var href = URL.createObjectURL(blob), link = document.createElement('a'); link.href = href; link.download = name; document.body.appendChild(link); link.click(); link.remove(); setTimeout(function() { URL.revokeObjectURL(href); }, 1000); });
  });
}
function bulkExport() {
  var ids = selectedIds(); if (!ids.length) return;
  var button = document.getElementById('bulk-export'); button.disabled = true;
  downloadZip('/sessions/export.zip', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ids:ids})}, 'meeting-notes-export.zip')
    .catch(function(e) { notify(e.message, 'error'); }).then(function() { updateSelection(); });
}
function bulkNotion() {
  var ids = selectedIds(); if (!ids.length) return;
  var withNotes = ids.filter(function(id) { var row = rowInfo[id]; return row && row.review && String(row.review.status || '').toLowerCase() === 'done'; });
  var skipped = ids.length - withNotes.length;
  if (!withNotes.length) { notify('None of the selected meetings have notes yet.', 'error'); return; }
  var button = document.getElementById('bulk-notion'); button.disabled = true;
  Promise.allSettled(withNotes.map(function(id) {
    return fetch('/v1/sessions/' + encodeURIComponent(id) + '/notion', {method:'POST', credentials:'same-origin'}).then(function(r) {
      if (r.ok) return r;
      return r.json().catch(function() { return {}; }).then(function(d) { throw new Error((d && typeof d.detail === 'string' && d.detail) || ('Could not send to Notion (HTTP ' + r.status + ').')); });
    });
  })).then(function(results) {
    var failed = results.filter(function(x) { return x.status === 'rejected'; }), sent = withNotes.length - failed.length;
    if (sent) notify('Sent ' + sent + ' to Notion.' + (skipped ? ' ' + skipped + ' skipped (no notes).' : ''));
    else if (skipped) notify(skipped + ' skipped (no notes).');
    if (failed.length) notify(failed.length + ' failed: ' + (failed[0].reason && failed[0].reason.message || 'Could not send to Notion.'), 'error');
    updateSelection(); loadRows(true);
  });
}
// Bulk "Find speakers" / "Update voice profiles": meetings that cannot do it (no audio, nothing transcribed yet, no
// voice data) are skipped and counted, not sent. The server has the last word and its refusals are counted as failures.
function bulkSpeakers(path, eligible, doneText, skipText, buttonId) {
  var ids = selectedIds(); if (!ids.length) return;
  var usable = ids.filter(function(id) { return eligible(rowInfo[id] || {}); });
  var skipped = ids.length - usable.length;
  if (!usable.length) { notify(skipped + ' skipped (' + skipText + ').', 'error'); return; }
  document.getElementById(buttonId).disabled = true;
  Promise.allSettled(usable.map(function(id) {
    return fetch('/v1/sessions/' + encodeURIComponent(id) + path, {method:'POST', credentials:'same-origin'}).then(function(r) {
      if (r.ok) return r.json().catch(function() { return {}; });
      return r.json().catch(function() { return {}; }).then(function(d) { throw new Error((d && typeof d.detail === 'string' && d.detail) || ('HTTP ' + r.status)); });
    });
  })).then(function(results) {
    var failed = results.filter(function(x) { return x.status === 'rejected'; }), ok = usable.length - failed.length;
    var values = results.filter(function(x) { return x.status === 'fulfilled'; }).map(function(x) { return x.value || {}; });
    if (ok) notify(doneText(ok, values) + (skipped ? ' ' + skipped + ' skipped (' + skipText + ').' : ''));
    else if (skipped) notify(skipped + ' skipped (' + skipText + ').');
    if (failed.length) notify(failed.length + ' failed: ' + (failed[0].reason && failed[0].reason.message || 'Action failed.'), 'error');
    updateSelection(); loadRows(true);
  });
}
function bulkFindSpeakers() {
  bulkSpeakers('/find-speakers', function(row) {
    var t = (row.pipeline || {}).transcription || {};
    return !!row.has_audio && t.state === 'complete';
  }, function(n) { return 'Finding speakers in ' + plural(n, 'meeting', 'meetings') + '.'; }, 'no audio or no transcript', 'bulk-find-speakers');
}
function bulkReprofile() {
  var ids = selectedIds(); if (!ids.length) return;
  var usable = ids.filter(function(id) { return (rowInfo[id] || {}).has_voiceprint !== false; });
  var skipped = ids.length - usable.length;
  if (!usable.length) { notify(skipped + ' skipped (no voice data).', 'error'); return; }
  var button = document.getElementById('bulk-reprofile'); button.disabled = true;
  runVoiceRecheck(usable).then(function(r) {
    notify('Voice recheck done: ' + voiceRecheckSummary(r) + '.' + (skipped ? ' ' + skipped + ' skipped (no voice data).' : ''));
  }).catch(function(e) { notify(e.message, 'error'); }).then(function() { updateSelection(); loadRows(true); });
}
function dayHeadMarkup(group) { return '<li class="day-head" role="row" data-day="'+escapeHtml(group.key)+'"><div role="columnheader" aria-colspan="7"><h2 class="day-title">'+escapeHtml(group.label)+'</h2></div></li>'; }
// One flat list: a day header, then that day's meetings, newest first. Existing nodes are kept and moved into
// place (never cleared) so the five-second refresh does not flash, and a meeting whose start time changed moves.
function renderMeetingList() {
  var rows = document.getElementById('rows');
  if (!listState.items.length) { rows.innerHTML = emptyRows(); return; }
  var desired = [];
  groupMeetingsByDay(sortMeetings(listState.items), new Date()).forEach(function(group) {
    desired.push({key:'d:'+group.key, html:dayHeadMarkup(group)});
    group.rows.forEach(function(item) { desired.push({key:'r:'+item.session_id, html:meetingRow(item)}); });
  });
  var existing = {}, stale = [];
  Array.from(rows.children).forEach(function(el) { if (el.dataset.key) existing[el.dataset.key] = el; else stale.push(el); });
  var cursor = rows.firstElementChild;
  desired.forEach(function(entry) {
    var scratch = document.createElement('ul'); scratch.innerHTML = entry.html;
    var replacement = scratch.firstElementChild, oldRow = existing[entry.key];
    if (oldRow) {
      delete existing[entry.key];
      var oldBox = oldRow.querySelector('.row-select'), wasChecked = oldBox && oldBox.checked;
      if (oldRow.innerHTML !== replacement.innerHTML) oldRow.replaceChildren.apply(oldRow, Array.from(replacement.childNodes));
      if (wasChecked) oldRow.querySelector('.row-select').checked = true;
      oldRow.className=replacement.className;
    } else { oldRow = replacement; oldRow.dataset.key = entry.key; }
    if (oldRow === cursor) cursor = cursor.nextElementSibling; else rows.insertBefore(oldRow, cursor);
  });
  Object.keys(existing).forEach(function(key) { existing[key].remove(); });
  stale.forEach(function(el) { el.remove(); });
}
function loadRows(reset) {
  var preservedSelection = reset ? selectedIds() : [];
  var previousLoaded = listState.loaded, hadRows = previousLoaded > 0;
  var ticket = ++listState.seq;
  if (reset) { listState.page=1; listState.loaded=0; }
  var url='/v1/sessions?page='+listState.page+'&per_page='+listState.perPage;
  var q=document.getElementById('q').value.trim(), state=document.getElementById('state').value, ntype=document.getElementById('type-filter').value;
  if(q) url+='&q='+encodeURIComponent(q); if(state) url+='&state='+encodeURIComponent(state); if(ntype) url+='&note_type='+encodeURIComponent(ntype);
  return fetch(url,{credentials:'same-origin'}).then(r=>{if(r.status===401||r.status===403){window.location='/login';throw new Error('Signed out');}if(!r.ok)throw new Error('Unable to load meetings');return r.json();}).then(data=>{
    if (ticket !== listState.seq) return true; // a newer request is in flight; its answer wins
    listState.total=data.total;
    data.items.forEach(function(item){rowInfo[item.session_id]=item;});
    var rows=document.getElementById('rows');
    document.getElementById('list-error').hidden=true;
    // Keep existing row nodes during polling. Clearing this list every five
    // seconds made the whole list visibly flash, especially on slower PCs.
    var focusedCheckbox = document.activeElement && document.activeElement.classList.contains('row-select') ? document.activeElement.value : null;
    // A later page can overlap the previous one when meetings arrive in between: merge by id, then re-sort.
    var byId = {}; (reset ? [] : listState.items).concat(data.items).forEach(function(item) { byId[item.session_id] = item; });
    listState.items = Object.keys(byId).map(function(id) { return byId[id]; });
    renderMeetingList();
    if (preservedSelection.length) document.querySelectorAll('.row-select').forEach(function(box) { box.checked = preservedSelection.indexOf(box.value) >= 0; });
    if (focusedCheckbox) { var focusedRow=Array.from(rows.querySelectorAll('li[data-id]')).find(function(row){return row.dataset.id===focusedCheckbox;});if(focusedRow&&document.activeElement!==focusedRow.querySelector('.row-select'))focusedRow.querySelector('.row-select').focus(); }
    listState.loaded+=data.items.length;
    document.getElementById('more').style.display=listState.loaded<listState.total?'':'none';
    document.getElementById('meeting-count').textContent=data.total+(data.total===1?' meeting':' meetings');
    rows.setAttribute('aria-busy','false');
    updateSelection();
    return true;
  }).catch(function(){
    if (ticket !== listState.seq) return false;
    if(reset)listState.loaded=previousLoaded;
    var rows=document.getElementById('rows');
    if(!rows.querySelector('li[data-id]'))rows.innerHTML='<li class="state-row" role="row"><div class="empty" role="cell">The meetings list could not be loaded.</div></li>';
    rows.setAttribute('aria-busy','false');
    document.getElementById('list-error').hidden=false;
    updateSelection();
    return false;
  });
}
var dismissedSuggestions = {};
function renderPeopleSuggestions(list) {
  var root=document.getElementById('people-suggestions');
  var shown=(list||[]).filter(function(s){return !dismissedSuggestions[currentSession+'|'+s.label];});
  var markup=shown.map(function(s){
    var who=escapeHtml(s.display||s.label);
    return '<div class="banner" role="status" data-label="'+escapeHtml(s.label)+'">'+icon('users')+'<span class="ps-text"><b>'+who+'</b> sounds like <b>'+escapeHtml(s.suggested_display)+'</b></span><span class="ps-actions"><button type="button" class="btn sm primary" data-ps="confirm">Yes</button><button type="button" class="btn sm" data-ps="reject">Not them</button><button type="button" class="btn sm ghost icon-only" data-ps="dismiss" aria-label="Dismiss">'+icon('x')+'</button></span></div>';
  }).join('');
  root.hidden=!shown.length;
  if(root._lastMarkup!==markup){root.innerHTML=markup;root._lastMarkup=markup;}
}
document.getElementById('people-suggestions').addEventListener('click',function(e){
  var b=e.target.closest('[data-ps]');if(!b||!currentSession)return;
  var label=b.closest('[data-label]').dataset.label,id=currentSession;
  if(b.dataset.ps==='dismiss'){dismissedSuggestions[id+'|'+label]=true;var root=document.getElementById('people-suggestions');root._lastMarkup=null;b.closest('.banner').remove();root.hidden=!root.querySelector('.banner');return;}
  b.disabled=true;
  fetch('/v1/people/appearances',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:id,label:label,action:b.dataset.ps})}).then(function(r){
    if(!r.ok)throw new Error('Could not save that');
    notify(b.dataset.ps==='confirm'?'Linked.':'Okay, not them.');
    if(currentSession===id)openSession(id);
  }).catch(function(err){b.disabled=false;notify(err.message,'error');});
});
function renderTranscript(segments) {
  var root=document.getElementById('overlay-segments');
  var markup=(!segments||!segments.length)?'<div class="empty">No final transcript yet.</div>':segments.map(function(seg,i){
    if(seg.in_gap)return '<div class="gap-marker" id="seg-'+i+'">Audio lost from '+fmtDuration(seg.start)+' to '+fmtDuration(seg.end)+'</div>';
    return speakerRow({mic:seg.track==='mic',label:seg.display||seg.label||(seg.track==='mic'?'You':'Them'),personId:seg.display?seg.person_id:null,start:seg.start,text:seg.text,approximate:seg.approximate,attrs:'id="seg-'+i+'" data-seg="'+i+'" tabindex="-1"'});
  }).join('');
  if(root._lastMarkup!==markup){root.innerHTML=markup;root._lastMarkup=markup;if(stripState.hit!=null){var again=document.getElementById('seg-'+stripState.hit);if(again)again.classList.add('hit');}findReapply();}
}
/* Session timeline: one block per timed transcript segment, positioned by its
   start and length on a shared time axis (from GET /v1/sessions/{id}
   segments: start, end, track, label, in_gap; total = meta.duration_sec or
   the last segment end). Hidden when no segment carries usable timing. */
function fmtAxis(t){
  if(t===0)return '0';
  if(t%3600===0)return (t/3600)+'h';
  if(t%60===0)return t<3600?(t/60)+'m':Math.floor(t/3600)+'h'+((t%3600)/60)+'m';
  return t<60?t+'s':Math.floor(t/60)+'m'+(t%60)+'s';
}
function pruneAxis(){
  // Never overlap: keep a label only if it clears the previous kept label by 8px.
  var last=-Infinity, spans=document.querySelectorAll('#session-strip .axis span');
  spans.forEach(function(span){span.hidden=false;});
  spans.forEach(function(span){var r=span.getBoundingClientRect();if(r.left<last+8){span.hidden=true;}else{last=r.right;}});
}
function segLane(seg){return seg.display||seg.label||(seg.track==='mic'?'You':'Them');}
function niceStep(total,maxTicks){var steps=[15,30,60,120,300,600,900,1200,1800,3600,7200,14400,28800];for(var i=0;i<steps.length;i++){if(total/steps[i]<=maxTicks)return steps[i];}return steps[steps.length-1];}
function validSpan(s){return s&&typeof s.start==='number'&&isFinite(s.start)&&typeof s.end==='number'&&isFinite(s.end)&&s.end>s.start&&s.start>=0;}
function renderStrip(data) {
  var wrap=document.getElementById('strip-wrap'), root=document.getElementById('session-strip');
  var segments=(data&&data.segments)||[], meta=(data&&data.meta)||{};
  stripState.data=data;
  var blocks=[], gaps=[], maxEnd=0;
  segments.forEach(function(s,i){if(!validSpan(s))return;maxEnd=Math.max(maxEnd,s.end);if(s.in_gap)gaps.push(s);else blocks.push({i:i,s:s});});
  var total=Math.max(Number(meta.duration_sec)||0,maxEnd);
  if(!blocks.length||total<=0){wrap.hidden=true;root.innerHTML='';root._lastMarkup=null;return;}
  wrap.hidden=false;
  var lanes=[],byLabel={};
  blocks.forEach(function(b){var mic=b.s.track==='mic',label=segLane(b.s);if(!byLabel[label]){byLabel[label]={label:label,mic:mic,items:[]};lanes.push(byLabel[label]);}byLabel[label].items.push(b);});
  lanes.sort(function(a,b){return (b.mic?1:0)-(a.mic?1:0);});
  // Every speaker keeps its own lane, however many there are: the strip never folds speakers together.
  var laneW=parseInt(getComputedStyle(root).getPropertyValue('--lane-w'),10)||96;
  var plotW=root.getBoundingClientRect().width-laneW, maxTicks=Math.max(2,Math.floor(plotW/72)), step=niceStep(total,maxTicks);
  function pct(v){return (v/total*100).toFixed(3)+'%';}
  var gapMarkup=gaps.map(function(g){return '<span class="gap-band" style="left:'+pct(g.start)+';width:'+pct(g.end-g.start)+'" title="Audio lost from '+fmtDuration(g.start)+' to '+fmtDuration(g.end)+'"></span>';}).join('');
  var first=true;
  var markup=lanes.map(function(l){
    var blks=l.items.map(function(b){
      var s=b.s, tab=first?0:-1;first=false;
      return '<button type="button" class="blk'+(l.mic?'':' them')+(stripState.hit===b.i?' on':'')+'" data-seg="'+b.i+'" data-start="'+s.start+'" tabindex="'+tab+'" style="left:'+pct(s.start)+';width:'+pct(s.end-s.start)+'" aria-label="'+escapeHtml(l.label)+', '+fmtDuration(s.start)+' to '+fmtDuration(s.end)+'. Show in transcript."></button>';
    }).join('');
    return '<div class="lane"><div class="lane-label" title="'+escapeHtml(l.label)+'"><i class="sw'+(l.mic?'':' them')+'"></i><span>'+escapeHtml(l.label)+'</span></div><div class="lane-track">'+gapMarkup+blks+'</div></div>';
  }).join('');
  var ticks='';for(var t=0;t<=total;t+=step){ticks+='<span'+(t/total>0.96?' class="end"':'')+' style="left:'+pct(t)+'">'+fmtAxis(t)+'</span>';}
  markup+='<div class="axis" aria-hidden="true">'+ticks+'</div>';
  var draw=stripState.drawnFor!==currentSession;
  if(root._lastMarkup!==markup){
    root.style.setProperty('--total',total);root.style.setProperty('--step',step);
    root.innerHTML=markup;root._lastMarkup=markup;
    if(draw){root.classList.add('strip-draw');setTimeout(function(){root.classList.remove('strip-draw');},400);}
  }
  stripState.drawnFor=currentSession;
  pruneAxis();
  document.getElementById('strip-total').textContent=fmtDuration(total)+' total';
}
function jumpToSegment(index) {
  transcriptExplicit=true;notesRequest++;if(notesPollTimer)clearTimeout(notesPollTimer);notesPollTimer=null;
  setDetailView('transcript');
  document.querySelectorAll('.segment.hit').forEach(function(el){el.classList.remove('hit');});
  document.querySelectorAll('.blk.on').forEach(function(el){el.classList.remove('on');});
  stripState.hit=index;
  var blk=document.querySelector('.blk[data-seg="'+index+'"]');if(blk)blk.classList.add('on');
  var el=document.getElementById('seg-'+index);if(!el)return;
  el.classList.add('hit');
  var reduce=window.matchMedia&&window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  el.scrollIntoView({block:'center',behavior:reduce?'auto':'smooth'});
  el.focus({preventScroll:true});
}
function onStripKey(e) {
  var blk=e.target.closest&&e.target.closest('.blk');if(!blk)return;
  var all=Array.from(document.querySelectorAll('#session-strip .blk')).sort(function(a,b){return (Number(a.dataset.start)-Number(b.dataset.start))||(all_index(a)-all_index(b));});
  function all_index(el){return Array.prototype.indexOf.call(el.parentNode.children,el);}
  var i=all.indexOf(blk),next=null;
  if(e.key==='ArrowRight')next=all[Math.min(all.length-1,i+1)];
  else if(e.key==='ArrowLeft')next=all[Math.max(0,i-1)];
  else if(e.key==='Home')next=all[0];
  else if(e.key==='End')next=all[all.length-1];
  else if(e.key==='ArrowUp'||e.key==='ArrowDown'){
    var lanes=Array.from(document.querySelectorAll('#session-strip .lane-track')),li=lanes.indexOf(blk.parentNode),target=lanes[li+(e.key==='ArrowDown'?1:-1)];
    if(target){var start=Number(blk.dataset.start),best=null,dist=Infinity;target.querySelectorAll('.blk').forEach(function(c){var d=Math.abs(Number(c.dataset.start)-start);if(d<dist){dist=d;best=c;}});next=best;}
  } else return;
  e.preventDefault();
  if(next&&next!==blk){blk.tabIndex=-1;next.tabIndex=0;next.focus();}
}
function renderProcessingChecklist(data) {
  if((data.meta||{}).source==='transcript'){document.getElementById('transcription-checklist').innerHTML='<li class="complete">'+dot()+'<span>Transcript uploaded</span><span class="detail">Complete</span></li><li class="complete">'+dot()+'<span>Transcript ready</span><span class="detail">Complete</span></li>';return;}
  var pipeline=data.pipeline||{}, upload=pipeline.upload||{}, transcription=pipeline.transcription||{}, job=(data.jobs||[])[0]||{};
  var uploadState=String(upload.state||data.upload_state||data.upload_status||(data.has_audio?'complete':'pending')).toLowerCase();
  var transcribeState=String(transcription.state||job.state||'pending').toLowerCase();
  var uploadProgress=upload.percent==null?(data.upload_progress==null?null:data.upload_progress):upload.percent;
  var transcribeProgress=transcription.transcribe_percent!=null?transcription.transcribe_percent:(transcription.percent==null?(job.progress==null?null:job.progress*100):transcription.percent);
  var speakers=transcription.speakers||null, speakersStage=!!speakers&&transcription.stage==='speakers';
  function complete(s){return s==='complete'||s==='completed'||s==='done'||s==='uploaded'||s==='ready';}
  var uploadComplete=complete(uploadState)||(!upload.state&&data.has_audio), uploadError=uploadState.indexOf('error')>=0||uploadState.indexOf('fail')>=0, transcribed=complete(transcribeState), transcribeError=transcribeState.indexOf('error')>=0||transcribeState.indexOf('fail')>=0, transcribing=transcribeState.indexOf('transcrib')>=0||transcribeState==='running';
  function percent(value){return value==null?'':Math.round(value)+'%';}
  var uploadDetail=uploadError?('Failed'+(upload.error?': '+escapeHtml(upload.error):'')):(uploadComplete?'Complete':(uploadState==='pending'?'Pending end of meeting':(uploadProgress==null?'Uploading':'Uploading '+percent(uploadProgress))));
  var transcribeQueued=uploadComplete&&(transcribeState==='queued'||transcribeState==='pending');
  var transcribeDetail=transcribed||(transcribing&&speakersStage&&!transcribeError)?'Complete':(transcribeError&&!speakersStage?'Needs attention':(transcribing?(transcribeProgress==null?'Transcribing':'Transcribing '+percent(transcribeProgress)):(transcribeQueued?'Queued':'Waiting')));
  var speakersDetail=transcribed?'Complete':(transcribeError&&speakersStage?'Needs attention':(transcribing&&speakersStage?'Identifying '+percent(speakers.percent):'Waiting'));
  var c1=uploadComplete ? 'complete' : (uploadError ? 'error' : (uploadState==='uploading' ? 'active' : '')), c2=(transcribeError&&!speakersStage) ? 'error' : (speakersStage&&(transcribing||transcribeError) ? 'complete' : (transcribing || transcribeQueued ? 'active' : (transcribed ? 'complete' : ''))), cs=transcribed ? 'complete' : (transcribeError&&speakersStage ? 'error' : (transcribing&&speakersStage ? 'active' : '')), c3=transcribed ? 'complete' : '';
  var where=computeWhere(transcription);
  // A speakers-only job ("Find speakers again", or the labels the "queue" setting adds after the transcript).
  var sl=pipeline.speaker_labels, slRow='';
  if(sl){
    var slDetail=sl.state==='waiting'?'Waiting for a GPU helper':(sl.state==='running'?'Identifying '+percent(sl.percent)+(sl.helper_name?' on '+escapeHtml(sl.helper_name)+' (GPU)':''):(sl.state==='error'?'Needs attention'+(sl.error?': '+escapeHtml(sl.error):''):'Queued'));
    slRow='<li class="'+(sl.state==='error'?'error':(sl.state==='running'?'active':''))+'">'+dot()+'<span>Speaker labels</span><span class="detail">'+slDetail+'</span></li>';
  }
  document.getElementById('transcription-checklist').innerHTML = '<li class="' + c1 + '">' + dot() + '<span>Upload audio</span><span class="detail">' + uploadDetail + '</span></li>' +
    '<li class="' + c2 + '">' + dot() + '<span>Transcribe recording</span><span class="detail">' + transcribeDetail + '</span>' + (where ? '<span class="where">' + escapeHtml(where) + '</span>' : '') + '</li>' +
    (speakers ? '<li class="' + cs + '">' + dot() + '<span>Identify speakers</span><span class="detail">' + speakersDetail + '</span></li>' : '') +
    '<li class="' + c3 + '">' + dot() + '<span>Transcript ready</span><span class="detail">' + (transcribed ? 'Complete' : 'Pending') + '</span></li>' + slRow;
}
function openSession(id, hintView) {
  if (detailPollTimer) clearTimeout(detailPollTimer);
  detailPollTimer=null;
  var request=++detailRequest;
  var changingSession=currentSession!==id;
  currentSession=id;
  if(changingSession){
    detailReturnFocus=document.activeElement;detailReturnKey=(function(el){var row=el&&el.closest?el.closest('li[data-id]'):null;return row?{id:row.dataset.id,open:el.classList.contains('row-open')}:null;})(detailReturnFocus);notesRequest++;currentReview=null;currentMarkdown='';transcriptExplicit=false;pendingNotesDefault=hintView==='notes';
    if(notesPollTimer)clearTimeout(notesPollTimer);notesPollTimer=null;
    if(renameMeeting)renameMeeting.close(false);if(renameSummary)renameSummary.close(false);
    closeMenu(false);
    stripState.drawnFor=null;stripState.hit=null;stripState.data=null;
    setDocTitle('');document.getElementById('overlay-title').textContent='Loading meeting…';document.getElementById('overlay-meta').textContent='';document.getElementById('overlay-board').hidden=true;
    var segRoot=document.getElementById('overlay-segments');segRoot.innerHTML=skeletonLines();segRoot._lastMarkup=null;
    var sugRoot=document.getElementById('people-suggestions');sugRoot.hidden=true;sugRoot.innerHTML='';sugRoot._lastMarkup=null;
    document.getElementById('strip-wrap').hidden=true;
    document.getElementById('notes-title').textContent='Meeting summary';document.getElementById('notes-meta').textContent='';document.getElementById('notes-state').textContent='';
    clearNotion();
    document.getElementById('notes-document').innerHTML=skeletonLines();
    document.getElementById('notes-download').disabled=true;document.getElementById('transcript-download').disabled=true;document.getElementById('notes-copy').disabled=true;document.getElementById('edit-summary-name').disabled=true;document.getElementById('notes-retry').disabled=true;
    document.getElementById('queue-review').disabled=true;document.getElementById('queue-review').textContent='Notes';document.getElementById('review-status').textContent='';
    document.getElementById('meeting-extras').open=false;
    reviewTemplateId='';setTemplateSelect('');document.getElementById('overlay-style').hidden=true;
    setDetailView(hintView==='notes'?'notes':'transcript');
    findClose(false);
    if(pendingFindView==='transcript')transcriptExplicit=true;
    pendingFindView='';
    if(pendingFind){findOpen(pendingFind,false);findState.jump=true;pendingFind=null;}
  }
  var overlay=document.getElementById('detail-overlay');overlay.classList.add('open');overlay.setAttribute('aria-hidden','false');document.body.style.overflow='hidden';
  if(changingSession)document.getElementById('close-overlay').focus();
  history.replaceState(null,'','/sessions/'+encodeURIComponent(id));
  fetch('/v1/sessions/'+encodeURIComponent(id),{credentials:'same-origin'}).then(r=>{if(!r.ok)throw new Error('Unable to load transcription');return r.json();}).then(data=>{
    if (currentSession !== id || request !== detailRequest) return;
    document.getElementById('transcription-progress-heading').textContent='Processing status';
    var meta=data.meta||{}; document.getElementById('overlay-title').textContent=meta.name||id;setDocTitle(meta.name||id);
    rowInfo[id]=Object.assign({session_id:id,created:meta.started_wall||meta.created,duration_sec:meta.duration_sec},rowInfo[id]||{},{name:meta.name||id},data.pipeline?{pipeline:data.pipeline,has_audio:data.has_audio}:{});if(!notionTimer&&!document.getElementById('notion-box').innerHTML)loadNotion();
    var chip=document.getElementById('overlay-board');chip.textContent=data.board||'';chip.hidden=!data.board;
    showPeople(meta.people_count);showScratchpad(meta.scratchpad,id);currentTranscribed=!!(data.segments&&data.segments.length);currentHasAudio=!!data.has_audio;
    document.getElementById('overlay-meta').textContent=[fmtDate(meta.created),fmtDuration(meta.duration_sec),meta.device||meta.platform||'Unknown device'].join(' · ');
    var players=[]; var tracks=meta.tracks||{};
    if(data.has_audio && tracks.mic && tracks.system) players.push('<div class="audio-card audio-both"><strong>'+icon('radio')+'Both sides</strong><audio controls preload="none" src="/sessions/'+encodeURIComponent(id)+'/audio/mixed"></audio></div>');
    ['mic','system'].forEach(track=>{if(data.has_audio && tracks[track]) players.push('<div class="audio-card"><strong>'+icon(track==='mic'?'mic':'speaker')+(track==='mic'?'You · microphone':'Them · system audio')+'</strong><audio controls preload="metadata" src="/sessions/'+encodeURIComponent(id)+'/audio/'+track+'"></audio></div>');});
    var audioRoot=document.getElementById('audio-players'),audioMarkup=players.join('') || '<div class="empty">'+(meta.source==='transcript'?'Transcript uploaded. There is no audio for this meeting.':'Audio has been removed.')+'</div>';
    if(audioRoot._lastMarkup!==audioMarkup){audioRoot.innerHTML=audioMarkup;audioRoot._lastMarkup=audioMarkup;}
    document.getElementById('retranscribe').disabled=!data.has_audio; document.getElementById('delete-audio').disabled=!data.has_audio;
    var findBtn=document.getElementById('find-speakers'),slState=((data.pipeline||{}).speaker_labels||{}).state,slActive=!!slState&&slState!=='error';
    if(findBtn){findBtn.disabled=!data.has_audio||!currentTranscribed||slActive;findBtn.title=!data.has_audio?'There is no audio to find speakers in':(slActive?'Speaker labels are already being worked on':'Work out who said what again from the audio. The text stays as it is.');}
    var reprofileBtn=document.getElementById('reprofile');
    if(reprofileBtn){reprofileBtn.disabled=!data.has_voiceprint;reprofileBtn.title=data.has_voiceprint?'Match this meeting\'s voices to the people again, without the audio':(data.has_audio?'No voice data yet. Use Find speakers again first.':'This meeting has no voice data');}
    renderProcessingChecklist(data);
    var review=data.review||data.review_status||{}; currentReview=review.review_id||review.id||null;
    var reviewStatus=String(review.status||'none').toLowerCase();
    document.getElementById('review-status').textContent=reviewStatus==='done'?'Notes ready':(reviewStatus==='queued'||reviewStatus==='running'?'Building notes…':(reviewStatus==='error'?'Notes need attention':''));
    document.getElementById('queue-review').disabled=false;
    document.getElementById('notes-retry').disabled=!currentReview;
    document.getElementById('queue-review').textContent=currentReview?'Notes':'Generate notes';
    renderTranscript(data.segments);
    renderPeopleSuggestions(data.people_suggestions);
    if(pendingSeek!==null&&data.segments&&data.segments.length){var seekTo=pendingSeek,seekIdx=0;pendingSeek=null;data.segments.forEach(function(sg,si){if(!sg.in_gap&&typeof sg.start==='number'&&sg.start<=seekTo+0.5)seekIdx=si;});jumpToSegment(seekIdx);}
    document.getElementById('transcript-download').disabled=!(data.segments&&data.segments.length);
    renderStrip(data);
    var state=data.pipeline||{}, upload=state.upload||{}, transcription=state.transcription||{};
    if(changingSession)document.getElementById('meeting-extras').open=!!(upload.state==='pending'||upload.state==='uploading'||transcription.state==='pending'||transcription.state==='transcribing'||slActive);
    if(currentReview&&reviewStatus==='done'&&!transcriptExplicit&&(pendingNotesDefault||document.getElementById('transcript-pane').hidden===false)){pendingNotesDefault=false;showNotes();}
    else if(pendingNotesDefault){pendingNotesDefault=false;if(!transcriptExplicit)setDetailView('transcript');}
    if (currentSession === id && (upload.state==='pending' || upload.state==='uploading' || transcription.state==='pending' || transcription.state==='transcribing' || slActive || ((reviewStatus==='queued'||reviewStatus==='running')&&!document.getElementById('transcript-pane').hidden))) detailPollTimer=setTimeout(function(){if(currentSession===id)openSession(id);},3000);
    else if (currentSession === id) loadRows(true);
  }).catch(function(){if(currentSession!==id||request!==detailRequest)return;document.getElementById('transcription-progress-heading').textContent='Processing status · reconnecting…';detailPollTimer=setTimeout(function(){if(currentSession===id)openSession(id);},3000);});
}
function setDetailView(view){var notes=view==='notes';sheetEl.dataset.view=notes?'notes':'transcript';if(typeof findState!=='undefined'&&findState.open)findRun(false);document.getElementById('transcript-pane').hidden=notes;document.getElementById('notes-pane').hidden=!notes;document.getElementById('notes-document-pane').hidden=!notes;document.getElementById('queue-review').classList.toggle('active',notes);document.getElementById('show-transcript').classList.toggle('active',!notes);document.getElementById('queue-review').setAttribute('aria-pressed',String(notes));document.getElementById('show-transcript').setAttribute('aria-pressed',String(!notes));}
function restoreFocus(){
  // Polling rewrites row cells, so the element focused before opening may be gone: find its replacement.
  if(detailReturnFocus&&detailReturnFocus.isConnected){detailReturnFocus.focus();return;}
  if(!detailReturnKey)return;
  var row=Array.from(document.querySelectorAll('#rows li[data-id]')).find(function(r){return r.dataset.id===detailReturnKey.id;});
  var target=row&&(detailReturnKey.open?row.querySelector('.row-open'):row.querySelector('.row-open, .row-select'));
  if(target)target.focus();
}
function setDocTitle(name){document.title='Meeting Notes | '+(name||'Meeting');}
function closeOverlay(){findClose(false);document.title='Meeting Notes | Meetings';clearNotion();currentSession=null;currentReview=null;detailRequest++;notesRequest++;if(detailPollTimer)clearTimeout(detailPollTimer);if(notesPollTimer)clearTimeout(notesPollTimer);detailPollTimer=null;notesPollTimer=null;if(renameMeeting)renameMeeting.close(false);if(renameSummary)renameSummary.close(false);document.getElementById('people-form').hidden=true;closeMenu(false);stripState.drawnFor=null;stripState.hit=null;var overlay=document.getElementById('detail-overlay');overlay.classList.remove('open');overlay.setAttribute('aria-hidden','true');document.body.style.overflow='';history.replaceState(null,'','/meetings');restoreFocus();detailReturnFocus=null;detailReturnKey=null;}
function noteValues(value){return Array.isArray(value)?value:(value==null?[]:[value]);}
function noteText(value){if(value==null)return '';if(typeof value!=='object')return String(value);var text=String(value.action||value.task||value.text||value.title||value.point||value.decision||value.question||value.risk||value.step||'');if(value.owner)text+=' — Owner: '+value.owner;if(value.due_date||value.due)text+=' — Due: '+(value.due_date||value.due);if(value.context)text+=' — '+value.context;return text;}
function personText(value){if(value!=null&&typeof value==='object')return String(value.name||value.email||noteText(value));return value==null?'':String(value);}
function dueText(value){var text=fmtDate(value);return text==='unknown date'?String(value):text;}
function buildMarkdown(note,title){var sections=[['Summary',note.summary||note.overview],['Meeting notes',note.polished_meeting_notes||note.polished_notes||note.meeting_notes||note.narrative||note.notes],['Key points',note.key_points||note.keyPoints],['Decisions',note.decisions],['Action items',note.action_items||note.actionItems||note.actions],['Open questions',note.open_questions||note.openQuestions||note.questions],['Risks',note.risks],['Next steps',note.next_steps||note.nextSteps],['Participants',note.participants||note.attendees]],filled=sections.filter(function(s){return noteValues(s[1]).map(noteText).some(function(v){return v.trim();});}),empty=sections.filter(function(s){return !noteValues(s[1]).map(noteText).some(function(v){return v.trim();});});function section(s){var values=noteValues(s[1]).map(noteText).filter(function(v){return v.trim();}),prose=s[0]==='Summary'||s[0]==='Meeting notes';return '## '+s[0]+'\n'+(values.length?(prose?values.join('\n\n'):values.map(function(v){return '- '+v;}).join('\n')):'')+'\n';}var facts=meetingFacts(note.stats),factsMd=facts.length?'## Meeting details\n'+facts.map(function(f){return '- '+f[0]+': '+f[1];}).join('\n')+'\n\n':'';return ('# '+title+'\n\n'+factsMd+filled.map(section).join('\n')+(empty.length?'\n---\n\n'+empty.map(section).join('\n'):'' )).trim()+'\n';}
function clearNotion(){notionRequest++;if(notionTimer)clearTimeout(notionTimer);notionTimer=null;var box=document.getElementById('notion-box');if(box){box.hidden=true;box.innerHTML='';}}
function notionLink(url,label){return url?'<a href="'+escapeHtml(url)+'" target="_blank" rel="noopener">'+escapeHtml(label)+'</a>':'<span>'+escapeHtml(label)+'</span>';}
function notionPathHtml(d){
  if(!d||!d.parent)return '';
  var out=[notionLink(d.parent.url,d.parent.title||'Parent page')];
  if(d.month)out.push(notionLink(d.month.url,d.month.title||'Month page'));
  return '<span class="notion-path" id="notes-dest">'+out.join('<span class="notion-sep" aria-hidden="true">\u203a</span>')+'</span>';
}
function renderNotion(st,session){
  var box=document.getElementById('notion-box'),state=st.state||'none',parts=['<span class="notion-label">'+icon('notes',14)+'<span>Notion</span></span>'];
  var dest=st.destination||{},path=notionPathHtml(dest),canSend=!!st.can_send,reason=st.reason||'';
  var noNotes=!canSend&&/no notes yet/i.test(reason);
  var sel=templateSelect(),have=st.copied_style||reviewTemplateId,mismatch=!!(sel&&!sel.hidden&&have&&sel.value&&sel.value!==have);
  if(path)parts.push(path);else parts.push('<span class="notion-none" id="notes-dest">Not saved to Notion</span>');
  if(mismatch){/* the selection is not what is saved yet: show where it WOULD go, not the current status */}
  else if(state==='copied'){parts.push(badge('done','In Notion'));if(st.url)parts.push('<a class="notion-open" href="'+escapeHtml(st.url)+'" target="_blank" rel="noopener">Open</a>');}
  else if(state==='pending'){parts.push(badge('running',st.retrying?'Retrying…':'Sending…'));}
  else if(state==='removing'){parts.push(badge('running','Removing…'));}
  else if(state==='failed'){parts.push(badge('error','Failed',st.error||''));if(st.error)parts.push('<span class="error-text notion-error">'+escapeHtml(st.error)+'</span>');}
  else if(!noNotes)parts.push(badge('none','Not in Notion'));
  if(st.warning)parts.push(badge('warn',st.warning));
  if(mismatch&&path)parts.push('<span class="help notion-reason">'+(state==='copied'?'After you regenerate, the Notion copy moves here.':'Regenerate to save to this page.')+'</span>');
  if(state!=='pending'&&state!=='removing'&&!noNotes&&!mismatch){
    var sendTitle=!canSend&&reason?' title="'+escapeHtml(reason)+'"':' title="Copies the notes to the Notion page set for the note type of this meeting"';
    var removeItem='<button type="button" role="menuitem" class="danger-item" id="notion-remove" title="Deletes this note from its month page in Notion and stops it being sent again automatically">'+icon('trash')+'<span>Remove this note from Notion</span></button>';
    if(state==='copied'){
      /* In Notion: the actions live in a "..." menu (Send again, Remove). */
      parts.push(notionMenuHtml('<button type="button" role="menuitem" id="notion-send"'+(canSend?'':' disabled')+sendTitle+'>'+icon('refresh')+'<span>Send again</span></button><div class="menu-sep" role="separator"></div>'+removeItem));
    }else{
      var label=state==='none'?'Send to Notion':'Retry';
      parts.push('<button type="button" class="btn secondary sm" id="notion-send"'+(canSend?'':' disabled')+sendTitle+'>'+escapeHtml(label)+'</button>');
      /* A failed send can leave a half-written toggle behind: allow removing it. */
      if(state==='failed'&&st.url)parts.push(notionMenuHtml(removeItem));
      if(state==='none'&&st.opted_out)parts.push('<span class="help notion-reason">Removed from Notion. It is not sent again automatically.</span>');
      if(state==='none'&&!canSend&&reason)parts.push('<span class="help notion-reason">'+escapeHtml(reason)+'</span>');
    }
  }
  box.innerHTML=parts.join('');box.hidden=false;
  bindNotionMenu();
  var send=document.getElementById('notion-send');
  if(send)send.onclick=function(){
    if(session!==currentSession)return;send.disabled=true;
    box.innerHTML='<span class="notion-label">'+icon('notes',14)+'<span>Notion</span></span>'+badge('running','Sending…');
    fetch('/v1/sessions/'+encodeURIComponent(session)+'/notion',{method:'POST',credentials:'same-origin'}).then(function(r){if(r.ok)return r.json();return r.json().catch(function(){return {};}).then(function(d){throw new Error((d&&typeof d.detail==='string'&&d.detail)||'Could not send to Notion.');});}).then(function(){if(session===currentSession)loadNotion();}).catch(function(e){notify(e.message,'error');if(session===currentSession)loadNotion();});
  };
  var rm=document.getElementById('notion-remove');
  if(rm)rm.onclick=function(){
    if(session!==currentSession)return;
    confirmDialog({title:'Remove this note from Notion?',lead:'The note is deleted from its month page in Notion. It will not be sent again automatically; use Send to Notion if you change your mind.',confirmLabel:'Remove from Notion',danger:true}).then(function(ok){
      if(!ok||session!==currentSession)return;
      box.innerHTML='<span class="notion-label">'+icon('notes',14)+'<span>Notion</span></span>'+badge('running','Removing…');
      fetch('/v1/sessions/'+encodeURIComponent(session)+'/notion',{method:'DELETE',credentials:'same-origin'}).then(function(r){if(r.ok)return r.json();return r.json().catch(function(){return {};}).then(function(d){throw new Error((d&&typeof d.detail==='string'&&d.detail)||'Could not remove the note from Notion.');});}).then(function(){if(session===currentSession)loadNotion();}).catch(function(e){notify(e.message,'error');if(session===currentSession)loadNotion();});
    });
  };
  if(state==='pending'||state==='removing'){if(notionTimer)clearTimeout(notionTimer);notionTimer=setTimeout(function(){if(session===currentSession)loadNotion();},3000);}
}
/* The Notion bar's "..." menu: same markup and keys as the meeting's overflow menu (menu semantics, Escape, outside click). */
function notionMenuHtml(itemsHtml){
  return '<span class="menu-wrap notion-menu-wrap"><button type="button" class="btn ghost icon-only sm" id="notion-more" aria-haspopup="menu" aria-expanded="false" aria-controls="notion-menu" aria-label="Notion options" title="Notion options">'+icon('more',16)+'</button><div class="menu" id="notion-menu" role="menu" aria-label="Notion options" hidden>'+itemsHtml+'</div></span>';
}
function closeNotionMenu(refocus){var m=document.getElementById('notion-menu'),b=document.getElementById('notion-more');if(!m||m.hidden)return;m.hidden=true;if(b){b.setAttribute('aria-expanded','false');if(refocus)b.focus();}}
function bindNotionMenu(){
  var btn=document.getElementById('notion-more'),menu=document.getElementById('notion-menu');if(!btn||!menu)return;
  function items(){return Array.from(menu.querySelectorAll('[role=menuitem]:not([disabled])'));}
  btn.onclick=function(){if(!menu.hidden){closeNotionMenu(true);return;}menu.hidden=false;btn.setAttribute('aria-expanded','true');var it=items();if(it.length)it[0].focus();};
  menu.addEventListener('keydown',function(e){
    var it=items(),i=it.indexOf(document.activeElement);if(!it.length)return;
    if(e.key==='ArrowDown'){e.preventDefault();it[(i+1)%it.length].focus();}
    else if(e.key==='ArrowUp'){e.preventDefault();it[(i-1+it.length)%it.length].focus();}
    else if(e.key==='Home'){e.preventDefault();it[0].focus();}
    else if(e.key==='End'){e.preventDefault();it[it.length-1].focus();}
    else if(e.key==='Escape'){e.preventDefault();e.stopPropagation();closeNotionMenu(true);}
    else if(e.key==='Tab'){closeNotionMenu(false);}
  });
  menu.addEventListener('click',function(e){if(e.target.closest('[role=menuitem]'))closeNotionMenu(false);});
}
document.addEventListener('click',function(e){var m=document.getElementById('notion-menu');if(m&&!m.hidden&&!m.contains(e.target)&&!e.target.closest('#notion-more'))closeNotionMenu(false);});
function loadNotion(){
  var session=currentSession;if(!session)return;
  if(notionTimer)clearTimeout(notionTimer);notionTimer=null;
  var request=++notionRequest;
  var tsel=templateSelect(),tq=tsel&&!tsel.hidden&&tsel.value?'?template='+encodeURIComponent(tsel.value):'';fetch('/v1/sessions/'+encodeURIComponent(session)+'/notion'+tq,{credentials:'same-origin'}).then(function(r){if(!r.ok)throw new Error('Unable to load Notion status');return r.json();}).then(function(st){if(session!==currentSession||request!==notionRequest)return;renderNotion(st,session);}).catch(function(){if(session!==currentSession||request!==notionRequest)return;document.getElementById('notion-box').hidden=true;});
}
/* Participants editor: rename, remove and add people on finished notes. Names are user input and
   only ever reach the page through .value / textContent / setAttribute, never innerHTML. */
var peopleEditFor=null,peopleDraft=null,lastNotesData=null;
function peopleRows(value){return noteValues(value).map(personText).filter(function(v){return v.trim();});}
/* The Participants rail: the people recognised by voice in the meeting (GET /v1/meeting-notes/{id} "people": profile
   name, company, how long they talked) merged with the names the AI wrote. A name that People knows (npLookup, by
   name or an "also heard as" spelling) joins the voice it belongs to, or gets its own row with the profile's avatar
   and company; a name People does not know keeps the "Not in People" button. Voice profiles off: plain names. */
function participantRows(value,heard){
  var rows=[],byId={},byName={};
  (heard||[]).forEach(function(p){var r={person_id:p.person_id,display:p.display,named:!!p.named,company:p.company||'',is_self:!!p.is_self,heard:true,talk:p.talk_seconds||0,labels:p.labels||[],names:[]};byId[p.person_id]=r;if(p.named)byName[npKey(p.display)]=r;rows.push(r);});
  peopleRows(value).forEach(function(name){
    var same=byName[npKey(name)];if(same){same.names.push(name);return;}  // the profile's own spelling needs no lookup
    var hit=voiceProfilesOn?npLookup[npKey(name)]:undefined;
    if(hit&&byId[hit.person_id]){byId[hit.person_id].names.push(name);return;}
    if(hit){var r={person_id:hit.person_id,display:hit.display,named:true,company:hit.company||'',is_self:!!hit.is_self,heard:false,talk:0,labels:[],names:[name],via:hit.via};byId[hit.person_id]=r;rows.push(r);return;}
    rows.push({person_id:null,display:name,named:true,company:'',is_self:false,heard:false,talk:0,labels:[],names:[name],unknown:hit===null});
  });
  return rows;
}
function participantRow(r){
  var sub=[];
  if(r.company)sub.push(escapeHtml(r.company));
  if(r.heard)sub.push(r.is_self?'Your voice':(r.named?'Heard by voice':'Not named yet')+(r.talk>=30?' · '+escapeHtml(talkText(r.talk)):''));
  else if(r.via==='alias'&&npKey(r.names[0])!==npKey(r.display))sub.push('Written as '+escapeHtml(r.names[0]));
  // A voice with no name, or a name that only exists as a voice-less person, can be linked to someone in People:
  // that person keeps the voice (or the name as another spelling) and this row disappears into theirs.
  if(r.person_id&&!r.is_self&&(r.heard?!r.named:true))sub.push('<button type="button" class="link-btn" data-np-same="'+escapeHtml(r.person_id)+'" data-np-who="'+escapeHtml(r.display)+'" data-np-kind="'+(r.heard?'voice':'name')+'">'+(r.heard?'Link to a person…':'Same person as…')+'</button>');
  var who;
  if(r.person_id)who='<a class="np-link'+(r.named?'':' unnamed')+'" href="/people/'+encodeURIComponent(r.person_id)+'">'+escapeHtml(r.display)+'</a>';
  else if(r.unknown)who=npMarkup(r.display);
  else who='<span data-pname="'+escapeHtml(r.display)+'">'+escapeHtml(r.display)+'</span>';
  return '<li>'+personAvatar({display:r.display,is_self:r.is_self},'')+'<div class="pmain">'+who+(sub.length?'<span class="psub">'+sub.join(' · ')+'</span>':'')+'</div></li>';
}
function bindPeopleEditor(doc,current){
  var edit=doc.querySelector('#people-edit');
  if(edit)edit.onclick=function(){peopleEditFor=currentReview;peopleDraft=peopleRows(current).map(function(n){return {name:n,was:n};});
    // people recognised by voice but missing from the AI's list start in the draft, so Save keeps them
    var have={};peopleDraft.forEach(function(d){have[npKey(d.name)]=1;});participantRows(current,(lastNotesData||{}).people).forEach(function(r){if(r.heard&&r.named&&!r.names.length&&!have[npKey(r.display)]&&npKey(r.display)!=='you'){have[npKey(r.display)]=1;peopleDraft.push({name:r.display,was:''});}});
    renderNotes(lastNotesData);var first=document.querySelector('#people-edit-list input,#people-add');if(first)first.focus();};
  var form=doc.querySelector('#people-form');if(!form)return;
  if(!peopleDraft)peopleDraft=peopleRows(current).map(function(n){return {name:n,was:n};});
  var list=form.querySelector('#people-edit-list'),err=form.querySelector('#people-error');
  function paint(focusIndex){
    list.textContent='';
    peopleDraft.forEach(function(row,i){
      var li=document.createElement('li'),input=document.createElement('input'),rm=document.createElement('button');
      input.type='text';input.maxLength=120;input.autocomplete='off';input.value=row.name;input.setAttribute('aria-label','Participant name');
      input.addEventListener('input',function(){row.name=input.value;});
      rm.type='button';rm.className='btn ghost sm';rm.textContent='Remove';rm.setAttribute('aria-label','Remove '+(row.name||'this person'));
      rm.addEventListener('click',function(){peopleDraft.splice(i,1);paint(Math.min(i,peopleDraft.length-1));});
      li.appendChild(input);li.appendChild(rm);list.appendChild(li);
    });
    if(focusIndex>=0&&focusIndex<peopleDraft.length)list.children[focusIndex].querySelector('input').focus();
    else if(focusIndex===-1&&!peopleDraft.length)form.querySelector('#people-add').focus();
  }
  function close(){peopleEditFor=null;peopleDraft=null;renderNotes(lastNotesData);var b=document.getElementById('people-edit');if(b)b.focus();}
  paint(-2);
  form.querySelector('#people-add').onclick=function(){peopleDraft.push({name:'',was:''});paint(peopleDraft.length-1);};
  form.querySelector('#people-cancel').onclick=close;
  form.addEventListener('keydown',function(e){if(e.key==='Escape'){e.preventDefault();e.stopPropagation();close();}});
  form.addEventListener('submit',function(e){
    e.preventDefault();err.textContent='';
    var seen={},body=[];
    for(var i=0;i<peopleDraft.length;i++){var n=peopleDraft[i].name.replace(/\s+/g,' ').trim();if(!n)continue;if(seen[n.toLowerCase()]){err.textContent='"'+n+'" is listed twice.';return;}seen[n.toLowerCase()]=1;body.push({name:n,was:peopleDraft[i].was||null});}
    if(body.length>50){err.textContent='At most 50 people.';return;}
    var submit=form.querySelector('[type=submit]'),session=currentSession,review=currentReview;submit.disabled=true;
    fetch('/v1/meeting-notes/'+encodeURIComponent(review)+'/participants',{method:'PUT',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({participants:body})}).then(function(r){return r.json().catch(function(){return {};}).then(function(d){if(!r.ok)throw new Error(typeof d.detail==='string'?d.detail:'Could not save the participants. Try again.');return d;});}).then(function(){peopleEditFor=null;peopleDraft=null;if(session===currentSession&&review===currentReview){showNotes(true);loadNotion();notify('Participants saved.');}}).catch(function(x){err.textContent=x.message||'Could not save the participants. Try again.';submit.disabled=false;});
  });
}
function renderNotes(data){
  lastNotesData=data;
  var note=data.note||data.meeting_note||data, meta=note.meta||note, title=note.title||meta.title||meta.name||'Meeting summary';
  var status=String(note.status||data.status||'').toLowerCase();
  document.getElementById('notes-title').textContent=title; var tpl=data.template||note.template||null;reviewTemplateId=tpl&&tpl.id?tpl.id:'';setTemplateSelect(reviewTemplateId);var styleChip=document.getElementById('overlay-style');styleChip.textContent=tpl&&tpl.name?'Note type: '+tpl.name:'';styleChip.hidden=!(tpl&&tpl.name);document.getElementById('notes-pane').classList.toggle('same',title===document.getElementById('overlay-title').textContent);document.getElementById('notes-meta').textContent=[fmtDate(meta.created||meta.meeting_time||meta.started),meta.device||meta.platform,tpl&&tpl.name].filter(Boolean).join(' · '); document.getElementById('notes-state').textContent=status==='done'?(data.transcript_outdated?(data.rebuild_pending?'The transcript changed. These notes will rebuild by themselves once the speaker labels finish.':'The transcript changed after these notes were made. Use Regenerate notes to rebuild them from the new one.'):''):(status==='queued'||status==='running'?'Building meeting notes…':(status==='error'?'Notes need attention. Use Regenerate notes to try again.':''));
  if(status==='done')loadNotion();else clearNotion();
  if((status==='queued'||status==='running')&&!note.summary){document.getElementById('notes-document').className='notes-doc no-rail';document.getElementById('notes-document').innerHTML='<p class="notes-empty-state">The summary is being prepared. You can return to the transcript while it runs.</p>'+skeletonLines();currentMarkdown='';document.getElementById('notes-download').disabled=true;document.getElementById('notes-copy').disabled=true;document.getElementById('edit-summary-name').disabled=true;return;}
  var S={summary:note.summary||note.overview,body:note.polished_meeting_notes||note.polished_notes||note.meeting_notes||note.narrative||note.notes,points:note.key_points||note.keyPoints,decisions:note.decisions,actions:note.action_items||note.actionItems||note.actions,questions:note.open_questions||note.openQuestions||note.questions,risks:note.risks,steps:note.next_steps||note.nextSteps,people:note.participants||note.attendees};
  function has(value,mapper){return noteValues(value).map(mapper||noteText).some(function(v){return String(v).trim();});}
  function list(value,mapper,prose){var values=noteValues(value).map(mapper||noteText).filter(function(v){return String(v).trim();});if(prose)return mdBlock(values.join('\n\n'));return values.length===1?'<p>'+mdInline(values[0])+'</p>':'<ul>'+values.map(function(v){return '<li>'+mdInline(v)+'</li>';}).join('')+'</ul>';}
  function people(value){return '<ul class="people">'+participantRows(value,data.people).map(participantRow).join('')+'</ul>';}
  function actions(value){return '<ul class="action-list">'+noteValues(value).map(function(raw){var action=typeof raw==='object'&&raw?raw:{action:raw},label=action.action||action.task||action.text||'',pills=[];if(!label)return '';if(action.owner)pills.push('<span class="pill owner" title="Owner">'+escapeHtml(action.owner)+'</span>');if(action.due_date||action.due)pills.push('<span class="pill due" title="Due date">Due '+escapeHtml(dueText(action.due_date||action.due))+'</span>');return '<li class="action-item"><span class="box" aria-hidden="true"></span><div class="action-body"><div class="what">'+mdInline(label)+'</div>'+(pills.length?'<div class="pills">'+pills.join('')+'</div>':'')+(action.context?'<div class="context">'+mdInline(action.context)+'</div>':'')+'</div></li>';}).join('')+'</ul>';}
  var main=[['Summary',S.summary,'lead',noteText],['Decisions',S.decisions,'',noteText],['Action items',S.actions,'actions',noteText],['Key points',S.points,'',noteText],['Meeting notes',S.body,'',noteText]];
  var rail=[['Participants',S.people,'people',personText],['Open questions',S.questions,'',noteText],['Risks',S.risks,'',noteText],['Next steps',S.steps,'',noteText]];
  var canEditPeople=status==='done'&&!!note.summary, editingPeople=canEditPeople&&peopleEditFor===currentReview;
  function peopleBlock(value){
    if(editingPeople)return '<h3>Participants</h3><form class="people-edit" id="people-form" novalidate><p class="help" style="margin:0 0 8px">Fix a spelling and notes that mention the old one are updated too. Corrections are remembered for future meetings.</p><ul class="people-edit-list" id="people-edit-list"></ul><div class="people-edit-actions"><button type="button" class="btn secondary sm" id="people-add">Add person</button></div><div class="people-edit-actions"><button type="submit" class="btn primary sm">Save</button><button type="button" class="btn ghost sm" id="people-cancel">Cancel</button></div><p class="err" id="people-error" role="alert"></p></form>';
    var rows=participantRows(value,data.people);
    return '<h3 class="people-head">Participants'+(canEditPeople?'<button type="button" class="btn ghost sm" id="people-edit" aria-label="Edit participants">Edit</button>':'')+'</h3>'+(rows.length?people(value):'<p class="notes-none-line" style="margin-top:0">No participants recorded.</p>');
  }
  function section(s){var cls='notes-section'+(s[2]==='lead'?' lead':'');return '<section class="'+cls+'">'+(s[2]==='people'?peopleBlock(s[1]):'<h3>'+escapeHtml(s[0])+'</h3>'+(s[2]==='actions'?actions(s[1]):list(s[1],s[3],s[0]==='Summary'||s[0]==='Meeting notes')))+'</section>';}
  function shown(s){return has(s[1],s[3])||(s[2]==='people'&&(canEditPeople||(data.people||[]).length));}
  var missing=main.concat(rail).filter(function(s){return !has(s[1],s[3])&&s[2]!=='people';}).map(function(s){return s[0];});
  var mainMarkup=main.filter(shown).map(section).join(''), railMarkup=meetingFactsMarkup(note.stats||data.stats)+rail.filter(shown).map(section).join('');
  var doc=document.getElementById('notes-document');
  doc.className='notes-doc'+(railMarkup?'':' no-rail');
  doc.innerHTML='<div class="notes-main">'+(mainMarkup||'<p class="notes-empty-state">No notes were recorded for this meeting yet.</p>')+(missing.length?'<p class="notes-none-line">Nothing recorded for: '+escapeHtml(missing.join(', '))+'.</p>':'')+'</div>'+(railMarkup?'<aside class="notes-rail" aria-label="Details: meeting, participants and follow-ups">'+railMarkup+'</aside>':'');
  bindPeopleEditor(doc,S.people);
  // Names the AI wrote are looked up in People once; when that brings something new the rail is drawn again so a
  // name merges with the voice it belongs to (npDecorate alone would only turn the name into a link).
  if(voiceProfilesOn&&!editingPeople)npResolve(peopleRows(S.people)).then(function(fetched){if(fetched===true&&lastNotesData===data&&peopleEditFor!==currentReview)renderNotes(data);});
  findReapply();
  currentMarkdown=buildMarkdown(note,title);
  document.getElementById('notes-download').disabled=!note.summary;
  document.getElementById('notes-copy').disabled=!note.summary;
  document.getElementById('edit-summary-name').disabled=!note.summary;
}
function showNotes(refresh){
  if(!currentReview){var session=currentSession, request=++notesRequest;document.getElementById('review-status').textContent='Starting notes…';action('/review'+templateQuery()).then(function(result){if(session!==currentSession||request!==notesRequest)return;currentReview=result.review_id||result.id||null;reviewTemplateId=(result.template_id||'');document.getElementById('review-status').textContent='Building notes…';document.getElementById('queue-review').textContent='Notes';document.getElementById('notes-retry').disabled=false;showNotes();loadRows(true);}).catch(function(e){if(session===currentSession&&request===notesRequest){document.getElementById('review-status').textContent='';notify(e.message,'error');}}); return; }
  transcriptExplicit=false;setDetailView('notes');
  if(!refresh){peopleEditFor=null;peopleDraft=null;currentMarkdown='';document.getElementById('notes-download').disabled=true;document.getElementById('notes-copy').disabled=true;document.getElementById('edit-summary-name').disabled=true;document.getElementById('notes-document').className='notes-doc no-rail';document.getElementById('notes-document').innerHTML=skeletonLines();}
  var session=currentSession, review=currentReview, request=++notesRequest;
  fetch('/v1/meeting-notes/'+encodeURIComponent(review),{credentials:'same-origin'}).then(function(r){if(!r.ok)throw new Error('Unable to load meeting notes');return r.json();}).then(function(data){if(session!==currentSession||review!==currentReview||request!==notesRequest)return;renderNotes(data);var status=String((data.note||data).status||data.status||'').toLowerCase();document.getElementById('review-status').textContent=status==='done'?'Notes ready':(status==='error'?'Notes need attention':'Building notes…');if(status==='done'||status==='error')loadRows(true);if((status==='queued'||status==='running')&&currentReview){if(notesPollTimer)clearTimeout(notesPollTimer);notesPollTimer=setTimeout(function(){if(session===currentSession&&review===currentReview)showNotes(true);},3000);}}).catch(function(e){if(session!==currentSession||review!==currentReview||request!==notesRequest)return;document.getElementById('notes-state').textContent=e.message+' · retrying…';notesPollTimer=setTimeout(function(){if(session===currentSession&&review===currentReview)showNotes(true);},3000);});
}
var currentPeople=null,currentTranscribed=false,currentHasAudio=false;
// The notes scratchpad: the server renders the Markdown (meeting_notes/markdown.py escapes first and only emits tags it
// builds), so the HTML it returns is injected as is. Refetched only when the text or the meeting changes.
function showScratchpad(text,sessionId){var body=document.getElementById('your-notes-text'),empty=document.getElementById('your-notes-empty');text=(typeof text==='string'&&text.trim())?text:'';empty.hidden=!!text;body.hidden=!text;if(body._raw===text&&body._sid===sessionId)return;body._raw=text;body._sid=sessionId;if(!text){body.innerHTML='';return;}fetch('/v1/sessions/'+encodeURIComponent(sessionId)+'/scratchpad.html',{credentials:'same-origin'}).then(function(r){if(!r.ok)throw new Error('notes');return r.text();}).then(function(h){if(body._raw===text&&body._sid===sessionId)body.innerHTML=h;}).catch(function(){body._raw=null;body.textContent=text;});}
function showPeople(n){currentPeople=(typeof n==='number'&&n>=1)?n:null;var chip=document.getElementById('overlay-people');chip.textContent=currentPeople?(currentPeople===1?'1 person':currentPeople+' people'):'';chip.hidden=!currentPeople;}
function saveName(url, value, field){return fetch(url,{method:'PATCH',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({[field]:value})}).then(function(r){if(!r.ok)throw new Error('Unable to save name');return r.json();});}
function templateSelect(){return document.getElementById('notes-template');}
function templateQuery(){var sel=templateSelect();return sel&&!sel.hidden&&sel.value?'?template='+encodeURIComponent(sel.value):'';}
function setTemplateSelect(id){var sel=templateSelect();if(!sel||sel.hidden)return;var want=id||defaultTemplateId;if(Array.prototype.some.call(sel.options,function(o){return o.value===want;}))sel.value=want;updateRegenButton();}
function updateNoteDest(){if(currentSession)loadNotion();}
function updateRegenButton(){var sel=templateSelect(),btn=document.getElementById('notes-regen');if(!sel||!btn)return;btn.hidden=!(currentReview&&!sel.hidden&&reviewTemplateId&&sel.value!==reviewTemplateId);}
/* Note-type filter: tabs across the top of the list ("All" plus one per note type), shown once there are two or more
   types. The chosen id lives in the hidden #type-filter, which loadRows reads. */
function fillTypeFilter(){var f=document.getElementById('type-filter'),nav=document.getElementById('type-tabs');if(!f||!nav)return;
  if(f.value&&!noteTemplates.some(function(t){return t.id===f.value;}))f.value='';
  nav.textContent='';
  [{id:'',name:'All'}].concat(noteTemplates).forEach(function(t){var b=document.createElement('button');b.type='button';b.dataset.type=t.id;b.textContent=t.name;nav.appendChild(b);});
  nav.hidden=noteTemplates.length<2&&!f.value;syncTypeTabs();}
function syncTypeTabs(){var v=document.getElementById('type-filter').value;
  Array.prototype.forEach.call(document.querySelectorAll('#type-tabs button'),function(b){var on=b.dataset.type===v;b.classList.toggle('active',on);b.setAttribute('aria-pressed',on?'true':'false');});}
document.getElementById('type-tabs').addEventListener('click',function(e){var b=e.target.closest('button[data-type]');if(!b)return;
  var f=document.getElementById('type-filter');if(f.value===b.dataset.type)return;f.value=b.dataset.type;syncTypeTabs();loadRows(true);});
function loadNoteTemplates(){fetch('/v1/note-templates',{credentials:'same-origin'}).then(function(r){if(!r.ok)throw new Error('templates');return r.json();}).then(function(data){noteTemplates=data.items||[];defaultTemplateId=data.default_template_id||'';fillTypeFilter();var sel=templateSelect();if(!sel)return;sel.innerHTML=noteTemplates.map(function(t){return '<option value="'+escapeHtml(t.id)+'">'+escapeHtml(t.name)+'</option>';}).join('');sel.hidden=noteTemplates.length<2;document.getElementById('type-bar').hidden=sel.hidden;document.getElementById('type-row').hidden=sel.hidden;sel.value=defaultTemplateId;setTemplateSelect(reviewTemplateId);updateNoteDest();}).catch(function(){});}
function regenerateNotes(){if(!currentReview)return;var session=currentSession,review=currentReview,sel=templateSelect(),body=sel&&!sel.hidden&&sel.value?JSON.stringify({template:sel.value}):'';document.getElementById('notes-state').textContent='Queued for regeneration…';document.getElementById('notes-regen').hidden=true;fetch('/v1/meeting-notes/'+encodeURIComponent(review)+'/retry',{method:'POST',credentials:'same-origin',headers:body?{'Content-Type':'application/json'}:{},body:body||undefined}).then(function(r){if(!r.ok)return r.json().then(function(d){throw new Error(d.detail||'Unable to queue regeneration');});return r.json();}).then(function(){if(session===currentSession&&review===currentReview){showNotes();loadRows(true);}}).catch(function(e){if(session===currentSession&&review===currentReview){document.getElementById('notes-state').textContent=e.message;updateRegenButton();}});}
function action(path,method){if(!currentSession)return;return fetch('/v1/sessions/'+encodeURIComponent(currentSession)+path,{method:method||'POST',credentials:'same-origin'}).then(async r=>{if(!r.ok)throw new Error((await r.json()).detail||'Request failed');return r.json();});}
/* Inline rename: the heading swaps for a small form; errors show beside it. */
function bindRename(o){
  function openForm(){o.error.textContent='';o.input.value=o.current();o.heading.hidden=true;o.form.hidden=false;o.btn.setAttribute('aria-expanded','true');o.input.focus();o.input.select();}
  function closeForm(refocus){o.form.hidden=true;o.heading.hidden=false;o.btn.setAttribute('aria-expanded','false');if(refocus){o.btn.focus();if(document.activeElement!==o.btn)moreBtn.focus();}}
  o.btn.onclick=function(){if(o.form.hidden)openForm();else closeForm(true);};
  o.cancel.onclick=function(){closeForm(true);};
  o.input.addEventListener('keydown',function(e){if(e.key==='Escape'){e.stopPropagation();e.preventDefault();closeForm(true);}});
  o.form.addEventListener('submit',function(e){e.preventDefault();var name=o.input.value.trim();if(!name){o.error.textContent='Enter a name to save.';return;}var submit=o.form.querySelector('[type=submit]');submit.disabled=true;o.error.textContent='';Promise.resolve(o.save(name)).then(function(){closeForm(true);}).catch(function(err){o.error.textContent=(err&&err.message)||'Could not save the name. Try again.';}).finally(function(){submit.disabled=false;});});
  return {close:closeForm};
}
/* People count: how many people were in the meeting, including you; a changed count applies when speakers are
   identified again (a retranscription), so offer that when the meeting already has a transcript. */
(function(){
  var form=document.getElementById('people-form'),input=document.getElementById('people-input'),err=document.getElementById('people-error'),btn=document.getElementById('edit-people'),chip=document.getElementById('overlay-people');
  function openForm(){err.textContent='';input.value=currentPeople||'';form.hidden=false;btn.setAttribute('aria-expanded','true');input.focus();input.select();}
  function closeForm(refocus){form.hidden=true;btn.setAttribute('aria-expanded','false');if(refocus){btn.focus();if(document.activeElement!==btn)moreBtn.focus();}}
  btn.onclick=function(){if(form.hidden)openForm();else closeForm(true);};
  chip.onclick=function(){if(currentSession&&form.hidden)openForm();};
  chip.addEventListener('keydown',function(e){if(e.key==='Enter'||e.key===' '){e.preventDefault();chip.click();}});
  document.getElementById('people-cancel').onclick=function(){closeForm(true);};
  input.addEventListener('keydown',function(e){if(e.key==='Escape'){e.stopPropagation();e.preventDefault();closeForm(true);}});
  form.addEventListener('submit',function(e){
    e.preventDefault();
    var session=currentSession;if(!session)return;
    var text=input.value.trim(),value=null;
    if(text){value=Number(text);if(!Number.isInteger(value)||value<1||value>50){err.textContent='Enter a whole number from 1 to 50, or leave it blank for automatic.';return;}}
    var submit=form.querySelector('[type=submit]');submit.disabled=true;err.textContent='';
    fetch('/v1/sessions/'+encodeURIComponent(session),{method:'PATCH',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({people_count:value})}).then(function(r){
      if(!r.ok)return r.json().catch(function(){return {};}).then(function(b){throw new Error(b.detail||'Could not save the number of people.');});
      return r.json();
    }).then(function(){
      var changed=value!==currentPeople;
      if(session===currentSession)showPeople(value);
      closeForm(true);
      if(changed&&session===currentSession&&currentTranscribed&&currentHasAudio){
        // The count only steers speaker separation: re-find the speakers (the text stays) where that is available,
        // and fall back to a full retranscribe only when speaker labels are not set up.
        var relabel=!!document.getElementById('find-speakers');
        confirmDialog({title:'Identify the speakers again?',lead:relabel?'The new number of people is used the next time the speakers are found. Find them again now? The text stays as it is.':'The new number of people only applies when the meeting is transcribed again. Retranscribe now to separate the speakers with it.',confirmLabel:relabel?'Find speakers again':'Retranscribe',danger:false}).then(function(ok){
          if(!ok)return;var pending=action(relabel?'/find-speakers':'/retranscribe');if(pending)pending.then(function(){notify(relabel?'Finding the speakers again. The text stays as it is.':'Transcribing again. It shows in the Queue.');openSession(currentSession);}).catch(function(e){notify(e.message,'error');});
        });
      }
    }).catch(function(e){err.textContent=(e&&e.message)||'Could not save the number of people.';}).finally(function(){submit.disabled=false;});
  });
})();
/* Overflow menu: real menu semantics (arrow keys, Home/End, Escape, outside click). */
function menuItems(){return Array.from(menuEl.querySelectorAll('[role=menuitem]:not([disabled])'));}
function openMenu(){menuEl.hidden=false;moreBtn.setAttribute('aria-expanded','true');var items=menuItems();if(items.length)items[0].focus();}
function closeMenu(refocus){if(menuEl.hidden)return;menuEl.hidden=true;moreBtn.setAttribute('aria-expanded','false');if(refocus)moreBtn.focus();}
moreBtn.onclick=function(){if(menuEl.hidden)openMenu();else closeMenu(true);};
menuEl.addEventListener('keydown',function(e){
  var items=menuItems(),i=items.indexOf(document.activeElement);
  if(e.key==='ArrowDown'){e.preventDefault();items[(i+1)%items.length].focus();}
  else if(e.key==='ArrowUp'){e.preventDefault();items[(i-1+items.length)%items.length].focus();}
  else if(e.key==='Home'){e.preventDefault();items[0].focus();}
  else if(e.key==='End'){e.preventDefault();items[items.length-1].focus();}
  else if(e.key==='Escape'){e.preventDefault();e.stopPropagation();closeMenu(true);}
  else if(e.key==='Tab'){closeMenu(false);}
});
menuEl.addEventListener('click',function(e){if(e.target.closest('[role=menuitem]'))closeMenu(false);});
document.addEventListener('click',function(e){if(!menuEl.hidden&&!menuEl.contains(e.target)&&!moreBtn.contains(e.target))closeMenu(false);});
document.getElementById('overlay-title').addEventListener('click',function(){var btn=document.getElementById('edit-meeting-name');if(currentSession&&document.getElementById('rename-form').hidden)btn.click();});
renameMeeting=bindRename({btn:document.getElementById('edit-meeting-name'),form:document.getElementById('rename-form'),input:document.getElementById('rename-input'),cancel:document.getElementById('rename-cancel'),error:document.getElementById('rename-error'),heading:document.getElementById('overlay-title'),current:function(){return document.getElementById('overlay-title').textContent;},save:function(name){var session=currentSession;if(!session)return Promise.reject(new Error('No meeting is open.'));return saveName('/v1/sessions/'+encodeURIComponent(session),name,'name').then(function(){if(session===currentSession){document.getElementById('overlay-title').textContent=name;setDocTitle(name);}loadRows(true);});}});
renameSummary=bindRename({btn:document.getElementById('edit-summary-name'),form:document.getElementById('summary-rename-form'),input:document.getElementById('summary-rename-input'),cancel:document.getElementById('summary-rename-cancel'),error:document.getElementById('summary-rename-error'),heading:document.getElementById('notes-title'),current:function(){return document.getElementById('notes-title').textContent;},save:function(name){var session=currentSession,review=currentReview;if(!review)return Promise.reject(new Error('No summary to rename yet.'));return saveName('/v1/meeting-notes/'+encodeURIComponent(review),name,'title').then(function(){if(session!==currentSession||review!==currentReview)return;document.getElementById('notes-title').textContent=name;showNotes(true);});}});
/* Find in this meeting. Matches are wrapped in <mark> by splitting TEXT NODES only (never innerHTML), so
   transcript and notes text stays inert and every handler on the page keeps working. The marks are removed
   before each new search; a re-render (polling, participants editor) drops them and findReapply() puts them back. */
var findEl={bar:document.getElementById('find-bar'),input:document.getElementById('find-input'),count:document.getElementById('find-count'),prev:document.getElementById('find-prev'),next:document.getElementById('find-next'),btn:document.getElementById('doc-find')};
var findState={open:false,marks:[],idx:-1,jump:false},pendingFind=null,pendingFindView='',findTimer=null;var pendingSeek=null;
var FIND_SKIP='button,input,textarea,select,script,style,mark,form,.seg-head,[contenteditable="true"],[aria-hidden="true"]';
function findRootEl(){return document.getElementById(sheetEl.dataset.view==='notes'?'notes-document':'overlay-segments');}
function findSkipped(el,root){while(el&&el!==root){if(el.matches&&el.matches(FIND_SKIP))return true;el=el.parentElement;}return false;}
function findClearMarks(){
  var parents=[];
  findState.marks.forEach(function(m){var p=m.parentNode;if(!p)return;while(m.firstChild)p.insertBefore(m.firstChild,m);p.removeChild(m);if(parents.indexOf(p)<0)parents.push(p);});
  parents.forEach(function(p){p.normalize();});
  findState.marks=[];findState.idx=-1;
}
function findUpdate(){
  var n=findState.marks.length,has=!!findEl.input.value.trim();
  findEl.count.textContent=!findState.open||!has?'':(n?(findState.idx+1)+' of '+(n>=1000?'1000+':n):'No matches');
  findEl.count.classList.toggle('none',has&&!n);
  findEl.prev.disabled=findEl.next.disabled=n<2;
}
function findSetActive(i,scroll){
  var old=findState.marks[findState.idx];if(old)old.classList.remove('active');
  findState.idx=i;var mk=findState.marks[i];
  if(mk){mk.classList.add('active');if(scroll)mk.scrollIntoView({block:'center',inline:'nearest'});}
  findUpdate();
}
function findRun(keep){
  var prev=keep?findState.idx:-1;
  findClearMarks();
  findEl.input.placeholder='Find in the '+(sheetEl.dataset.view==='notes'?'notes':'transcript');
  var q=findEl.input.value.trim(),root=findRootEl();
  if(!findState.open||!q||!root){findUpdate();return;}
  var nodes=[],walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT,null),node;
  while((node=walker.nextNode())){if(node.nodeValue.trim()&&!findSkipped(node.parentElement,root))nodes.push(node);}
  var total=0;
  nodes.forEach(function(n){
    if(total>=1000)return;
    var ranges=findRanges(n.nodeValue,q).slice(0,1000-total),local=[];
    for(var k=ranges.length-1;k>=0;k--){var r=document.createRange();r.setStart(n,ranges[k][0]);r.setEnd(n,ranges[k][1]);var mk=document.createElement('mark');mk.className='find-hit';r.surroundContents(mk);local.unshift(mk);}
    total+=local.length;findState.marks=findState.marks.concat(local);
  });
  if(!findState.marks.length){findUpdate();return;}
  if(prev>=0){findSetActive(Math.min(prev,findState.marks.length-1),false);}
  else{var jump=findState.jump;findState.jump=false;findSetActive(0,true);void jump;}
}
function findReapply(){if(findState.open&&findEl.input.value.trim())findRun(true);}
function findOpen(text,focus){
  findState.open=true;findEl.bar.hidden=false;findEl.btn.setAttribute('aria-expanded','true');
  if(typeof text==='string')findEl.input.value=text;
  if(focus!==false){findEl.input.focus();findEl.input.select();}
  findRun(false);
}
function findClose(refocus){
  var was=findState.open;
  findState.open=false;findState.jump=false;clearTimeout(findTimer);findClearMarks();findEl.input.value='';findEl.bar.hidden=true;findEl.btn.setAttribute('aria-expanded','false');findUpdate();
  if(refocus&&was)findEl.btn.focus();
}
function findStep(dir){var n=findState.marks.length;if(!n)return;findSetActive((findState.idx+dir+n)%n,true);}
findEl.btn.addEventListener('click',function(){if(findState.open&&!findEl.bar.hidden){findClose(true);}else findOpen();});
findEl.input.addEventListener('input',function(){clearTimeout(findTimer);findTimer=setTimeout(function(){findState.jump=false;findRun(false);},120);});
findEl.input.addEventListener('keydown',function(e){
  if(e.key==='Enter'){e.preventDefault();e.stopPropagation();clearTimeout(findTimer);if(findState.marks.length&&findState.idx>=0&&findEl.input.value.trim()){findStep(e.shiftKey?-1:1);}else{findRun(false);}}
  else if(e.key==='Escape'){e.preventDefault();e.stopPropagation();findClose(true);}
});
findEl.next.addEventListener('click',function(){findStep(1);});
findEl.prev.addEventListener('click',function(){findStep(-1);});
document.getElementById('find-close').addEventListener('click',function(){findClose(true);});
/* Ctrl/Cmd+F and "/" open the find only while a meeting is open (capture: before the page's own "/" -> search box). */
document.addEventListener('keydown',function(e){
  var overlay=document.getElementById('detail-overlay');
  if(!overlay.classList.contains('open')||document.querySelector('dialog[open]'))return;
  var tag=(e.target&&e.target.tagName)||'';
  var mod=(e.ctrlKey||e.metaKey)&&!e.altKey&&!e.shiftKey&&(e.key==='f'||e.key==='F');
  var slash=e.key==='/'&&!e.ctrlKey&&!e.metaKey&&!e.altKey&&!/^(INPUT|TEXTAREA|SELECT)$/.test(tag)&&!(e.target&&e.target.isContentEditable);
  if(!mod&&!slash)return;
  e.preventDefault();e.stopPropagation();
  if(findState.open&&!findEl.bar.hidden){findEl.input.focus();findEl.input.select();}else findOpen();
},true);
/* Opening a meeting from a search list carries the search's text into the find, so you land on the match. */
function openFromRow(row){
  var info=parseSearchQuery(document.getElementById('q').value);
  pendingFind=info.find||null;pendingFindView=info.view;
  var hint=row.classList.contains('notes-ready')?'notes':'';
  if(info.find&&info.view==='transcript')hint='transcript';
  else if(info.find&&info.view==='notes')hint='notes';
  openSession(row.dataset.id,hint);
}
/* A new tab or copied link from the list carries the find too (?find=). */
['pointerdown','focusin','contextmenu'].forEach(function(type){
  document.getElementById('rows').addEventListener(type,function(e){
    var a=e.target.closest&&e.target.closest('a.row-open'),li=a&&a.closest('li[data-id]');if(!li)return;
    var f=parseSearchQuery(document.getElementById('q').value).find;
    a.setAttribute('href','/sessions/'+encodeURIComponent(li.dataset.id)+(f?'?find='+encodeURIComponent(f):''));
  });
});
document.getElementById('rows').addEventListener('click',e=>{if(e.target.closest('#clear-filters')){document.getElementById('q').value='';document.getElementById('state').value='';document.getElementById('type-filter').value='';syncTypeTabs();loadRows(true);return;}var gen=e.target.closest('.notes-generate');if(gen){e.stopPropagation();generateNotes(gen);return;}var opener=e.target.closest('a.row-open');if(opener){if(e.metaKey||e.ctrlKey||e.shiftKey||e.altKey||e.button!==0)return;e.preventDefault();var target=opener.closest('li[data-id]');if(target)openFromRow(target);return;}if(e.target.closest('input,button,a')){updateSelection();return;}if(e.target.closest('label.sel')){updateSelection();return;}var row=e.target.closest('li[data-id]');if(row)openFromRow(row);});
document.getElementById('session-strip').addEventListener('click',function(e){var blk=e.target.closest('.blk');if(blk)jumpToSegment(Number(blk.dataset.seg));});
document.getElementById('session-strip').addEventListener('keydown',onStripKey);
var stripResize;window.addEventListener('resize',function(){clearTimeout(stripResize);stripResize=setTimeout(function(){if(currentSession&&stripState.data)renderStrip(stripState.data);},150);});
document.getElementById('close-overlay').onclick=closeOverlay;
document.getElementById('detail-overlay').addEventListener('click',function(e){if(e.target===this||e.target.classList.contains('overlay-inner'))closeOverlay();});
document.addEventListener('keydown',function(e){var overlay=document.getElementById('detail-overlay');if(!overlay.classList.contains('open')||document.getElementById('confirm-dialog').open)return;if(e.key==='Escape'){if(!menuEl.hidden){closeMenu(true);return;}closeOverlay();return;}trapFocus(e,overlay);});
document.getElementById('retranscribe').onclick=()=>{var pending=action('/retranscribe');if(pending)pending.then(r=>{notify(r&&r.already_queued?'This meeting is already waiting in the Queue to be transcribed.':'Transcribing again. It shows in the Queue; the new transcript replaces the old one when it is done.');openSession(currentSession);}).catch(e=>notify(e.message,'error'));};
// Participants rail: "Link to a person…" (a voice with no name) / "Same person as…" (a voice-less name) combine that
// person into the one chosen (POST /v1/people/{id}/merge), then the notes and transcript are drawn again.
document.addEventListener('click',function(e){
  var b=e.target.closest?e.target.closest('[data-np-same]'):null;if(!b)return;
  var id=b.dataset.npSame,who=b.dataset.npWho,voice=b.dataset.npKind==='voice',session=currentSession;
  pickPerson({title:voice?'Who is '+who+'?':'Who is "'+who+'"?',lead:voice?'This voice, with its meetings and quotes, moves to the person you choose.':'The name is kept as another way to say theirs, and anything recorded under it moves to them.',confirmLabel:'Link',namedOnly:true,withSelf:true,exclude:id,prefix:'ppick'}).then(function(pid){
    if(!pid)return;
    return pApi('POST','/v1/people/'+encodeURIComponent(id)+'/merge',{into:pid}).then(function(p){notify('Linked to '+(p.display||'them')+'.');npLookup={};if(currentSession===session){showNotes(true);openSession(session);}});
  }).catch(function(err){notify(err.message,'error');});
});
if(document.getElementById('find-speakers'))document.getElementById('find-speakers').onclick=()=>{var pending=action('/find-speakers');if(pending)pending.then(()=>{notify('Finding the speakers again. The text stays as it is.');openSession(currentSession);}).catch(e=>notify(e.message,'error'));};
if(document.getElementById('reprofile'))document.getElementById('reprofile').onclick=()=>{var pending=action('/reprofile');if(pending)pending.then(r=>{notify(r.changed?'Voice profiles updated: '+r.changed+' of '+r.speakers+' speakers changed.':'Voice profiles are up to date.');openSession(currentSession);}).catch(e=>notify(e.message,'error'));};
document.getElementById('queue-review').onclick=showNotes;
document.getElementById('notes-retry').onclick=regenerateNotes;
document.getElementById('notes-regen').onclick=regenerateNotes;
templateSelect().addEventListener('change',function(){updateRegenButton();updateNoteDest();});
if(aiEnabled)loadNoteTemplates();
document.getElementById('show-transcript').onclick=function(){transcriptExplicit=true;notesRequest++;if(notesPollTimer)clearTimeout(notesPollTimer);notesPollTimer=null;setDetailView('transcript');};
document.getElementById('notes-download').onclick=function(){if(!currentMarkdown)return;var blob=new Blob([currentMarkdown],{type:'text/markdown;charset=utf-8'}),url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download=(document.getElementById('notes-title').textContent.trim().replace(/[\/:*?"<>|]+/g,'-').slice(0,100)||'meeting-notes')+'.md';link.click();setTimeout(function(){URL.revokeObjectURL(url);},1000);};
document.getElementById('transcript-download').onclick=function(){if(!currentSession||this.disabled)return;var link=document.createElement('a');link.href='/sessions/'+encodeURIComponent(currentSession)+'/transcript.md';link.download='';document.body.appendChild(link);link.click();link.remove();};
document.getElementById('notes-copy').onclick=function(){if(!currentMarkdown)return;copyText(currentMarkdown).then(function(ok){notify(ok?'Meeting notes copied.':'Could not copy. Use notes.md to download them instead.',ok?'':'error');});};
document.getElementById('delete-audio').onclick=()=>{var id=currentSession;if(!id)return;deleteAudioDialog([id]).then(ok=>{if(!ok||currentSession!==id)return;var pending=action('/delete-audio','POST');if(pending)pending.then(()=>openSession(id)).catch(e=>notify(e.message,'error'));});};
document.getElementById('delete-entry').onclick=()=>{var id=currentSession;if(!id)return;trashDialog([id]).then(ok=>{if(!ok||currentSession!==id)return;closeOverlay();moveToTrash([id],'web');});};
document.getElementById('select-all').onchange=function(e){document.querySelectorAll('.row-select').forEach(function(box){box.checked=e.target.checked;});updateSelection();};
document.getElementById('bulk-build').onclick=function(){runBulk('/review','POST');};
document.getElementById('bulk-notion').onclick=function(){bulkNotion();};
document.getElementById('bulk-export').onclick=function(){bulkExport();};
document.getElementById('export-all').onclick=function(){if(!currentSession)return;downloadZip('/sessions/'+encodeURIComponent(currentSession)+'/export.zip',null,'meeting-notes-export.zip').catch(function(e){notify(e.message,'error');});};
document.getElementById('bulk-retranscribe').onclick=function(){runBulk('/retranscribe','POST');};
if(document.getElementById('bulk-find-speakers'))document.getElementById('bulk-find-speakers').onclick=bulkFindSpeakers;
if(document.getElementById('bulk-reprofile'))document.getElementById('bulk-reprofile').onclick=bulkReprofile;
document.getElementById('bulk-delete-audio').onclick=function(){var ids=selectedIds();if(!ids.length)return;deleteAudioDialog(ids).then(function(ok){if(ok)runBulk('/delete-audio','POST');});};
document.getElementById('bulk-delete').onclick=function(){var ids=selectedIds();if(!ids.length)return;trashDialog(ids).then(function(ok){if(ok)moveToTrash(ids,'bulk');});};
document.getElementById('bulk-clear').onclick=function(){document.querySelectorAll('.row-select').forEach(function(box){box.checked=false;});updateSelection();};
document.getElementById('list-retry').onclick=function(){loadRows(true);};
var qBox=document.getElementById('q'), debounce;
var initialQuery=new URLSearchParams(location.search).get('q'); if(initialQuery)qBox.value=initialQuery;
qBox.oninput=()=>{clearTimeout(debounce);debounce=setTimeout(()=>loadRows(true),250);};
qBox.form.addEventListener('submit',function(e){e.preventDefault();clearTimeout(debounce);loadRows(true);});
document.getElementById('state').onchange=()=>loadRows(true); document.getElementById('more').onclick=()=>{listState.page++;loadRows(false);};
function setPlaceholder(){qBox.placeholder=window.matchMedia('(max-width:860px)').matches?qBox.dataset.full:qBox.dataset.short;}
setPlaceholder();window.matchMedia('(max-width:860px)').addEventListener('change',setPlaceholder);
loadRows(true);
// Once the operator has loaded additional pages, keep that expanded result
// set stable. A page-1 refresh would otherwise discard later pages and their
// selections every five seconds, making "Load more" effectively unusable.
setInterval(function(){if(!currentSession && listState.page===1)loadRows(true);},5000);
"""
        + splitmerge_ui.JS
        + f"if ({initial} !== null) {{ var ut = /^#t=([0-9]+(?:[.][0-9]+)?)$/.exec(location.hash); if (ut) pendingSeek = parseFloat(ut[1]); var uf = new URLSearchParams(location.search).get('find'); if (uf) pendingFind = uf.slice(0, 200); openSession({initial}, {initial_view_js}); }}"
        + """
</script>
"""
    )
    return _shell(
        page_title or "Meetings",
        body,
        token_configured=token_configured,
        active="transcriptions",
        main_class="meetings-page",
        appearance=appearance,
    )
