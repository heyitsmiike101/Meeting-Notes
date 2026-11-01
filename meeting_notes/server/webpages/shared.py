"""Inline JavaScript helpers and dialog markup shared by several pages (``_JS_HELPERS``)."""

from __future__ import annotations


_JS_HELPERS_SRC = r"""
/* The search query language (mirrors server/search_query.py): words, "quoted phrases" and field filters
   title: attendee: (attendees:, person:) notes: transcript:. Unknown "foo:bar" is plain text; an empty filter is ignored.
   find = the text to highlight in a meeting opened from this search; view = the pane that text lives in. */
var SEARCH_FIELDS={title:'title',attendee:'attendee',attendees:'attendee',person:'attendee',notes:'notes',transcript:'transcript'};
function parseSearchQuery(q){
  var s=String(q||''),n=s.length,i=0,terms=[];
  function quoted(at){var j=s.indexOf('"',at+1);return j<0?[s.slice(at+1),n]:[s.slice(at+1,j),j+1];}
  function white(c){return /\s/.test(c);}
  while(i<n){
    var c=s.charAt(i);
    if(white(c)){i++;continue;}
    if(c==='"'){var r=quoted(i);i=r[1];if(r[0].trim())terms.push({field:null,text:r[0].trim()});continue;}
    var j=i;while(j<n&&!white(s.charAt(j))&&s.charAt(j)!=='"')j++;
    var tok=s.slice(i,j),colon=tok.indexOf(':'),name=colon>0?tok.slice(0,colon).toLowerCase():'';
    var field=name&&Object.prototype.hasOwnProperty.call(SEARCH_FIELDS,name)?SEARCH_FIELDS[name]:null;
    if(field&&colon===tok.length-1&&j<n&&s.charAt(j)==='"'){var r2=quoted(j);i=r2[1];if(r2[0].trim())terms.push({field:field,text:r2[0].trim()});continue;}
    i=j;
    if(field){var value=tok.slice(colon+1);if(value.trim())terms.push({field:field,text:value});}
    else if(tok)terms.push({field:null,text:tok});
  }
  var findable=terms.filter(function(t){return !t.field||t.field==='notes'||t.field==='transcript';});
  var view='';
  if(findable.length&&findable.every(function(t){return t.field==='transcript';}))view='transcript';
  else if(findable.length&&findable.every(function(t){return t.field==='notes';}))view='notes';
  return {terms:terms,find:findable.length?findable[0].text:'',view:view};
}
/* [start, end] of every case-insensitive, literal occurrence of q in text. */
function findRanges(text,q){
  var out=[];if(!q)return out;
  var re=new RegExp(String(q).replace(/[.*+?^${}()|[\]\\]/g,'\\$&'),'gi'),m;
  while((m=re.exec(text))){if(!m[0].length){re.lastIndex++;continue;}out.push([m.index,m.index+m[0].length]);}
  return out;
}
/* Start, end and talk time for a meeting's notes: [[label, value], ...] from the API's ``stats``. */
function meetingFacts(st){
  if(!st)return [];
  function clock(d){return d.toLocaleTimeString([],{hour:'numeric',minute:'2-digit'});}
  function day(d){return d.toLocaleDateString([],{weekday:'short',month:'short',day:'numeric'});}
  function dur(s){if(s==null)return '';var m=Math.round(s/60);if(m<1)return 'under a minute';var h=Math.floor(m/60),mm=m%60;return h&&mm?h+' h '+mm+' min':(h?h+' h':mm+' min');}
  var out=[];
  if(st.started!=null){
    var s=new Date(st.started*1000);out.push(['Start',day(s)+', '+clock(s)]);
    if(st.ended!=null){var e=new Date(st.ended*1000),d=dur(st.duration_sec);out.push(['End',(e.toDateString()===s.toDateString()?'':day(e)+', ')+clock(e)+(d?' ('+d+')':'')]);}
  }
  if(st.talk)out.push(['Talk time','You '+st.talk.me_pct+'% ('+dur(st.talk.me_sec)+') \u00b7 Them '+st.talk.them_pct+'% ('+dur(st.talk.them_sec)+')']);
  return out;
}
/* The note type a meeting's notes use, as a small chip on the Meetings and Home lists. */
function typeChip(row){
  var t=row&&row.note_type;if(!t||!t.name)return '';
  var tip=t.applied?'Notes were made with the '+t.name+' note type':'Notes will use the '+t.name+' note type';
  return '<span class="type-chip'+(t.applied?'':' planned')+'" title="'+escapeHtml(tip)+'"><span class="sr-only">Note type: </span>'+escapeHtml(t.name)+'</span>';
}
function meetingFactsMarkup(st){
  if(!st)return '';
  function clock(d){return d.toLocaleTimeString([],{hour:'numeric',minute:'2-digit'});}
  function dur(s){if(s==null)return '';var m=Math.round(s/60);if(m<1)return 'under a minute';var h=Math.floor(m/60),mm=m%60;return h&&mm?h+' h '+mm+' min':(h?h+' h':mm+' min');}
  var when='';
  if(st.started!=null){
    var s=new Date(st.started*1000),day=s.toLocaleDateString([],{weekday:'short',month:'short',day:'numeric'}),span=clock(s);
    if(st.ended!=null){var e=new Date(st.ended*1000);span+=' \u2013 '+(e.toDateString()===s.toDateString()?'':e.toLocaleDateString([],{month:'short',day:'numeric'})+', ')+clock(e);}
    var d=dur(st.duration_sec);
    when='<p class="facts-day">'+escapeHtml(day)+'</p><p class="facts-time">'+escapeHtml(span)+(d?' <span class="facts-dur">\u00b7 '+escapeHtml(d)+'</span>':'')+'</p>';
  }
  var t=st.talk,talk='';
  if(t){
    var me=Math.max(0,Math.min(100,Number(t.me_pct)||0)),tip='You '+t.me_pct+'% ('+dur(t.me_sec)+') \u00b7 Them '+t.them_pct+'% ('+dur(t.them_sec)+')';
    // Just the bar and its key; the numbers show on hover (or tap/focus on a phone).
    talk='<div class="talk" tabindex="0" aria-label="Talk time: '+escapeHtml(tip)+'"><div class="talk-bar" aria-hidden="true"><i class="talk-me" style="width:'+me+'%"></i></div>'
      +'<div class="talk-legend" aria-hidden="true"><span><b class="sw me"></b>You</span><span><b class="sw them"></b>Them</span></div>'
      +'<span class="talk-tip" role="tooltip">'+escapeHtml(tip)+'</span></div>';
  }
  if(!when&&!talk)return '';
  return '<section class="notes-section meeting-facts"><h3>Meeting</h3>'+when+talk+'</section>';
}
var ICONS = window.MN_ICONS || {};
function icon(name, size) {
  size = size || 16;
  return '<svg class="ic" viewBox="0 0 24 24" width="' + size + '" height="' + size + '" aria-hidden="true" focusable="false">' + (ICONS[name] || '') + '</svg>';
}
function dot() { return '<i class="dot" aria-hidden="true"></i>'; }
function badge(cls, label, title) {
  return '<span class="badge ' + cls + '"' + (title ? ' title="' + escapeHtml(title) + '"' : '') + '>' + dot() + '<span class="badge-text">' + escapeHtml(label) + '</span></span>';
}
var toastTimer = null;
// notify(message, kind, action): action = {label, onClick} (or {label, href} for a link) adds a button to the toast (e.g. Undo)
// and keeps it up longer.
function notify(message, kind, action) {
  var el = document.getElementById('page-toast');
  if (!el) return;
  el.className = 'toast' + (kind === 'error' ? ' error' : '') + (action ? ' has-action' : '');
  el.textContent = message;
  if (action && action.label) {
    var btn = document.createElement(action.href ? 'a' : 'button');
    if (action.href) btn.href = action.href; else btn.type = 'button';
    btn.className = 'toast-action'; btn.textContent = action.label;
    btn.addEventListener('click', function () {
      clearTimeout(toastTimer); el.textContent = ''; el.className = 'toast';
      if (action.onClick) action.onClick();
    });
    el.appendChild(btn);
  }
  clearTimeout(toastTimer);
  toastTimer = setTimeout(function () { el.textContent = ''; el.className = 'toast'; }, action ? 12000 : (kind === 'error' ? 9000 : 4500));
}
// confirmDialog({title, lead, warning, items:[{name, meta}], itemsLabel, note, confirmLabel, danger}) -> Promise<boolean>.
// `warning` is an optional red alert line under the lead (e.g. "This permanently removes the only copy").
// Needs the #confirm-dialog markup (_CONFIRM_DIALOG_HTML). Focus starts on Cancel: nothing is
// confirmed by an accidental Enter.
function confirmDialog(o) {
  var dlg = document.getElementById('confirm-dialog');
  if (!dlg || !dlg.showModal) {
    return Promise.resolve(window.confirm([o.title, o.lead, o.warning].concat((o.items || []).map(function (i) { return '- ' + i.name; })).filter(Boolean).join('\n')));
  }
  return new Promise(function (resolve) {
    dlg.querySelector('.dialog-title').textContent = o.title || '';
    dlg.querySelector('.dialog-lead').textContent = o.lead || '';
    dlg.querySelector('.dialog-note').textContent = o.note || '';
    var warn = dlg.querySelector('.dialog-warning');
    if (warn) {
      warn.textContent = '';
      if (o.warning) {
        var wi = document.createElement('span'), wt = document.createElement('span');
        wi.className = 'dw-ic'; wi.innerHTML = icon('alert');
        wt.textContent = o.warning;
        warn.appendChild(wi); warn.appendChild(wt);
      }
      warn.hidden = !o.warning;
    }
    var list = dlg.querySelector('.dialog-items'), items = o.items || [];
    list.innerHTML = items.map(function (it) {
      return '<li><span class="di-name">' + escapeHtml(it.name) + '</span><span class="di-meta">' + escapeHtml(it.meta || '') + '</span></li>';
    }).join('');
    list.hidden = !items.length;
    list.setAttribute('aria-label', o.itemsLabel || 'Meetings');
    list.scrollTop = 0;
    var ok = dlg.querySelector('[data-dialog-ok]');
    ok.textContent = o.confirmLabel || 'Confirm';
    ok.className = 'btn ' + (o.danger === false ? 'primary' : 'danger');
    dlg.onclose = function () { dlg.onclose = null; resolve(dlg.returnValue === 'ok'); };
    dlg.onclick = function (e) { if (e.target === dlg) dlg.close('cancel'); };
    dlg.returnValue = '';
    dlg.showModal();
    dlg.querySelector('[data-dialog-cancel]').focus();
  });
}
function meetingSummary(row) {
  var d = Math.max(0, Number(row.duration_sec) || 0);
  return {name: row.name || row.session_id, meta: [fmtDate(row.created, true), d ? fmtDuration(d) : ''].filter(Boolean).join(' · ')};
}
function agoText(ts) {
  var s = Math.max(0, Date.now() / 1000 - Number(ts || 0));
  if (s < 60) return 'just now';
  if (s < 3600) { var m = Math.floor(s / 60); return m + (m === 1 ? ' minute ago' : ' minutes ago'); }
  if (s < 86400) { var h = Math.floor(s / 3600); return h + (h === 1 ? ' hour ago' : ' hours ago'); }
  var d = Math.floor(s / 86400); return d + (d === 1 ? ' day ago' : ' days ago');
}
function daysLeftText(n) { return n <= 0 ? 'Removed soon' : n + (n === 1 ? ' day left' : ' days left'); }
function plural(n, one, many) { return n + ' ' + (n === 1 ? one : many); }
/* Voice recheck: Settings, People and the Meetings bulk action all call POST /v1/voice-recheck and word the result here.
   ids = exact meetings (all of their voices); none = the nightly window (voices nobody has named yet). */
function voiceRecheckSummary(r) {
  if (!r) return '';
  var m = r.meetings || 0, meetings = plural(m, 'meeting', 'meetings');
  var what = r.changed_meetings
    ? plural(r.changed_speakers, 'voice', 'voices') + ' matched in ' + r.changed_meetings + ' of ' + meetings
    : (m ? 'nothing new in ' + meetings : (r.trigger === 'selected' ? 'no voice data in those meetings' : 'no unnamed voices to check'));
  return what + (r.skipped && m ? ' (' + r.skipped + ' without voice data)' : '') + (r.failed ? ' (' + r.failed + ' failed)' : '');
}
function voiceRecheckLine(s) {
  function when(ts) { return fmtDate(ts, true); }
  var parts = [];
  if (s && s.last_result) parts.push('Last checked ' + when(s.last_result.ran) + ': ' + voiceRecheckSummary(s.last_result) + '.');
  if (s && s.enabled && s.next_run) parts.push('Next run ' + when(s.next_run) + '.');
  return parts.join(' ');
}
function voiceRecheckStatus() {
  return fetch('/v1/voice-recheck', {credentials: 'same-origin'}).then(function (r) { return r.ok ? r.json() : null; }).catch(function () { return null; });
}
function runVoiceRecheck(ids) {
  var opt = {method: 'POST', credentials: 'same-origin'};
  if (ids) { opt.headers = {'Content-Type': 'application/json'}; opt.body = JSON.stringify({session_ids: ids}); }
  return fetch('/v1/voice-recheck', opt).then(function (r) {
    return r.json().catch(function () { return {}; }).then(function (b) {
      if (!r.ok) throw new Error(typeof b.detail === 'string' && b.detail ? b.detail : 'Could not recheck voices');
      return b;
    });
  });
}
function copyText(text) {
  var previous = document.activeElement;
  function legacy() {
    try {
      var ta = document.createElement('textarea');
      ta.value = text; ta.setAttribute('readonly', ''); ta.style.position = 'fixed'; ta.style.opacity = '0';
      document.body.appendChild(ta); ta.select();
      var ok = document.execCommand('copy');
      document.body.removeChild(ta);
      if (previous && previous.focus) previous.focus();
      return ok;
    } catch (_) { return false; }
  }
  if (navigator.clipboard && window.isSecureContext) {
    return navigator.clipboard.writeText(text).then(function () { return true; }, function () { return legacy(); });
  }
  return Promise.resolve(legacy());
}
function trapFocus(event, root) {
  if (event.key !== 'Tab') return;
  var focusable = Array.from(root.querySelectorAll('button:not([disabled]),a[href],input:not([disabled]),select:not([disabled]),textarea:not([disabled]),audio[controls],summary,[tabindex="0"]')).filter(function (el) {
    return el.tabIndex >= 0 && el.getClientRects().length > 0;
  });
  if (!focusable.length) return;
  var first = focusable[0], last = focusable[focusable.length - 1];
  if (event.shiftKey && (document.activeElement === first || !root.contains(document.activeElement))) { event.preventDefault(); last.focus(); }
  else if (!event.shiftKey && (document.activeElement === last || !root.contains(document.activeElement))) { event.preventDefault(); first.focus(); }
}
function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
    return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];
  });
}
// ---- Transcript upload: shared by Home's "Add a meeting" panel and the Meetings page dialog ----
var TRANSCRIPT_MAX_BYTES = 2 * 1024 * 1024;
function localInputValue(date) {
  function two(n) { return String(n).padStart(2, '0'); }
  return date.getFullYear() + '-' + two(date.getMonth() + 1) + '-' + two(date.getDate()) + 'T' + two(date.getHours()) + ':' + two(date.getMinutes());
}
// readTranscriptFile(file, cb): cb(errorMessage) or cb(null, {text, filename, name, when}); `name` is the file name
// without its extension and `when` the file's modified time as a datetime-local value (both used as form defaults).
function readTranscriptFile(file, cb) {
  if (file.size > TRANSCRIPT_MAX_BYTES) { cb('That file is over the 2 MB limit for transcripts.'); return; }
  var reader = new FileReader();
  reader.onload = function () {
    cb(null, {text: String(reader.result || ''), filename: file.name, name: file.name.replace(/[.][^.]+$/, ''), when: file.lastModified ? localInputValue(new Date(file.lastModified)) : ''});
  };
  reader.onerror = function () { cb('Could not read that file.'); };
  reader.readAsText(file);
}
// buildTranscriptPayload({text, name, filename, when}) -> {payload} or {error}. `when` is a datetime-local value.
function buildTranscriptPayload(o) {
  var text = o.text || '';
  if (!text.trim()) return {error: 'Choose a file or paste the transcript first.'};
  var bytes = typeof Blob === 'function' ? new Blob([text]).size : text.length;
  if (bytes > TRANSCRIPT_MAX_BYTES) return {error: 'That transcript is over the 2 MB limit.'};
  if (!o.when) return {error: 'Enter the date and time of the meeting.'};
  var when = new Date(o.when);
  if (isNaN(when.getTime())) return {error: 'Enter a valid date and time.'};
  var payload = {text: text, name: String(o.name || '').trim(), source: o.filename ? 'file' : 'pasted', started_at: Math.floor(when.getTime() / 1000)};
  if (o.filename) payload.filename = o.filename;
  return {payload: payload};
}
// postTranscript(payload) -> Promise<{session_id, ...}>; rejects with an Error whose message is fit to show the user.
function postTranscript(payload) {
  return fetch('/v1/sessions/transcript', {method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)})
    .then(function (r) { return r.json().catch(function () { return {}; }).then(function (data) { return {ok: r.ok, data: data}; }); },
          function () { throw new Error('Could not reach the server. Check the connection and try again.'); })
    .then(function (res) {
      if (!res.ok) throw new Error(typeof res.data.detail === 'string' && res.data.detail ? res.data.detail : 'Could not add the transcript. Please try again.');
      return res.data;
    });
}
// Small, safe Markdown renderer for AI-generated notes. Everything is escaped first (escapeHtml), then a strict
// subset is converted: paragraphs, #..#### headings, one-level ul/ol, **bold**, *italic*/_italic_, `code`,
// [text](http/https url). Raw HTML can never pass through; other link schemes stay plain text.
function mdEmphasis(s) {
  s = s.replace(/\*\*(?=\S)([\s\S]*?\S)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|[^\w])__(?=\S)([\s\S]*?\S)__(?!\w)/g, "$1<strong>$2</strong>");
  s = s.replace(/(^|[^*])\*([^\s*](?:[^*\n]*?[^\s*])?)\*(?!\*)/g, "$1<em>$2</em>");
  s = s.replace(/(^|[^\w])_([^\s_](?:[^_\n]*?[^\s_])?)_(?!\w)/g, "$1<em>$2</em>");
  return s;
}
function mdInline(raw) {
  var stash = [];
  function hold(html) { stash.push(html); return "\u0000" + (stash.length - 1) + "\u0001"; }
  var s = escapeHtml(String(raw == null ? "" : raw).replace(/[\u0000\u0001]/g, ""));
  s = s.replace(/`([^`\n]+)`/g, function (m, code) { return hold("<code>" + code + "</code>"); });
  s = s.replace(/\[([^\]\n]+)\]\(([^\s()]+)\)/g, function (m, label, url) {
    if (!/^https?:\/\/[^\s<>"']+$/i.test(url)) return m;
    return hold('<a href="' + url + '" rel="noopener noreferrer" target="_blank">' + mdEmphasis(label) + "</a>");
  });
  s = mdEmphasis(s);
  for (var i = 0; i < 3 && s.indexOf("\u0000") >= 0; i++) {
    s = s.replace(/\u0000(\d+)\u0001/g, function (m, n) { return stash[+n]; });
  }
  return s;
}
function renderMarkdown(src) {
  var lines = String(src == null ? "" : src).replace(/\r\n?/g, "\n").split("\n");
  var levels = [];
  lines.forEach(function (l) {
    var m = /^ {0,3}(#{1,4})\s+\S/.exec(l);
    if (m && levels.indexOf(m[1].length) < 0) levels.push(m[1].length);
  });
  levels.sort(function (a, b) { return a - b; }); // document outline: shallowest heading -> h4, next -> h5 (sections use h3)
  var out = [], para = [], list = null, blank = false;
  function flushPara() {
    if (!para.length) return;
    var raw = "";
    para.forEach(function (l, i) {
      if (i) raw += /\s{2,}$/.test(para[i - 1]) ? "\n" : " ";
      raw += l.trim();
    });
    para = [];
    var cls = /^(\*\*|__)[^*_\n]+\1[:.]?$/.test(raw) ? ' class="md-sub"' : (/^(\*\*|__)[^*_\n]+\1/.test(raw) ? ' class="md-lead"' : "");
    out.push("<p" + cls + ">" + mdInline(raw).replace(/\n/g, "<br>") + "</p>");
  }
  function listHtml(l) {
    return "<" + l.type + ">" + l.items.map(function (it) {
      return "<li>" + mdInline(it.text) + (it.sub ? listHtml(it.sub) : "") + "</li>";
    }).join("") + "</" + l.type + ">";
  }
  function flushList() { if (list) { out.push(listHtml(list)); list = null; } }
  lines.forEach(function (line) {
    if (!line.trim()) { flushPara(); blank = true; return; }
    var h = /^ {0,3}(#{1,4})\s+(.+?)\s*#*\s*$/.exec(line);
    if (h) {
      flushPara(); flushList(); blank = false;
      var lv = Math.min(4 + Math.max(levels.indexOf(h[1].length), 0), 6);
      out.push("<h" + lv + ">" + mdInline(h[2]) + "</h" + lv + ">");
      return;
    }
    if (/^ {0,3}([-*_])( *\1){2,} *$/.test(line)) { flushPara(); flushList(); blank = false; out.push("<hr>"); return; }
    var m = /^(\s*)([-*+]|\d{1,3}[.)])\s+(.*)$/.exec(line);
    if (m) {
      flushPara();
      var type = /^\d/.test(m[2]) ? "ol" : "ul", indent = m[1].replace(/\t/g, "    ").length;
      if (indent >= 2 && list && list.items.length) {
        var last = list.items[list.items.length - 1];
        if (!last.sub) last.sub = { type: type, items: [] };
        last.sub.items.push({ text: m[3] });
      } else {
        if (list && list.type !== type) flushList();
        if (!list) list = { type: type, items: [] };
        list.items.push({ text: m[3], sub: null });
      }
      blank = false;
      return;
    }
    if (list && !blank && /^\s+\S/.test(line)) {
      var top = list.items[list.items.length - 1], tgt = top.sub ? top.sub.items[top.sub.items.length - 1] : top;
      tgt.text += " " + line.trim();
      return;
    }
    flushList(); blank = false; para.push(line);
  });
  flushPara(); flushList();
  return out.join("");
}
function mdBlock(src) { return '<div class="md">' + renderMarkdown(src) + "</div>"; }
function fmtDate(value, relative) {
  // One compact formatter for every date: "Sep 28, 3:07 PM"; the year only when it is not this year;
  // with `relative`, "Today 3:07 PM" / "Yesterday 3:07 PM".
  if (!value) return "unknown date";
  var numeric = typeof value === "number" || /^[0-9]+([.][0-9]+)?$/.test(String(value));
  var raw = numeric ? Number(value) : value;
  var dateOnly = !numeric && /^[0-9]{4}-[0-9]{2}-[0-9]{2}$/.test(String(value));
  // Server indexes use epoch seconds while session metadata uses ISO-8601.
  // Also tolerate millisecond epochs from future integrations.
  var d = dateOnly ? new Date(Number(raw.slice(0, 4)), Number(raw.slice(5, 7)) - 1, Number(raw.slice(8, 10))) : new Date(numeric && raw < 100000000000 ? raw * 1000 : raw);
  if (isNaN(d.getTime())) return "unknown date";
  var now = new Date();
  var time = dateOnly ? "" : d.toLocaleTimeString(undefined, {hour: "numeric", minute: "2-digit"});
  function dayKey(x) { return x.getFullYear() * 10000 + x.getMonth() * 100 + x.getDate(); }
  if (relative) {
    var yesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
    if (dayKey(d) === dayKey(now)) return "Today" + (time ? " " + time : "");
    if (dayKey(d) === dayKey(yesterday)) return "Yesterday" + (time ? " " + time : "");
  }
  var opts = {month: "short", day: "numeric"};
  if (d.getFullYear() !== now.getFullYear()) opts.year = "numeric";
  return d.toLocaleDateString(undefined, opts) + (time ? ", " + time : "");
}
function fmtDuration(seconds) {
  seconds = Math.max(0, Math.round(seconds || 0));
  var h = Math.floor(seconds / 3600), m = Math.floor((seconds % 3600) / 60), s = seconds % 60;
  function two(n) { return (n < 10 ? "0" : "") + n; }
  return h > 0 ? (h + ":" + two(m) + ":" + two(s)) : (m + ":" + two(s));
}
/* Meetings list helpers (pure, so they can be tested under node).
   meetingTime: a row's start time in epoch seconds, NaN when unknown.
   sortMeetings: newest first, ties broken by id descending -- the same total order the server uses
   (ORDER BY created DESC, session_id DESC), so pages and live updates always agree.
   groupMeetingsByDay: consecutive rows that share a calendar day in the browser's local timezone. */
function meetingTime(row) {
  var v = row && row.created;
  if (v == null || v === "") return NaN;
  if (typeof v === "number" || /^[0-9]+([.][0-9]+)?$/.test(String(v))) { var n = Number(v); return n >= 100000000000 ? n / 1000 : n; }
  var s = String(v), m = /^([0-9]{4})-([0-9]{2})-([0-9]{2})$/.exec(s);
  var ms = m ? new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])).getTime() : Date.parse(s);
  return isNaN(ms) ? NaN : ms / 1000;
}
function sortMeetings(rows) {
  return rows.slice().sort(function (a, b) {
    var ta = meetingTime(a), tb = meetingTime(b), na = isNaN(ta), nb = isNaN(tb);
    if (na !== nb) return na ? 1 : -1;
    if (!na && ta !== tb) return tb - ta;
    var ia = String(a.session_id), ib = String(b.session_id);
    return ia < ib ? 1 : (ia > ib ? -1 : 0);
  });
}
function dayKey(ts) {
  if (ts == null || isNaN(ts)) return "unknown";
  var d = new Date(ts * 1000);
  function two(n) { return (n < 10 ? "0" : "") + n; }
  return d.getFullYear() + "-" + two(d.getMonth() + 1) + "-" + two(d.getDate());
}
function dayLabel(ts, now, locale) {
  if (ts == null || isNaN(ts)) return "Unknown date";
  now = now || new Date();
  var d = new Date(ts * 1000), key = dayKey(ts);
  if (key === dayKey(now.getTime() / 1000)) return "Today";
  if (key === dayKey(new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1).getTime() / 1000)) return "Yesterday";
  var opts = {weekday: "long", month: "short", day: "numeric"};
  if (d.getFullYear() !== now.getFullYear()) opts.year = "numeric";
  return d.toLocaleDateString(locale, opts);
}
function groupMeetingsByDay(rows, now, locale) {
  var groups = [];
  rows.forEach(function (row) {
    var ts = meetingTime(row), key = dayKey(ts), last = groups[groups.length - 1];
    if (!last || last.key !== key) { last = {key: key, label: dayLabel(ts, now, locale), rows: []}; groups.push(last); }
    last.rows.push(row);
  });
  return groups;
}
function fmtBytes(bytes) {
  if (!bytes) return "0 B";
  var units = ["B", "KB", "MB", "GB"];
  var i = 0, n = bytes;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return n.toFixed(i === 0 ? 0 : 1) + " " + units[i];
}
function initials(label) {
  var parts = String(label || '').trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return '?';
  if (parts.length === 1) return parts[0].charAt(0).toUpperCase();
  var last = parts[parts.length - 1];
  return (parts[0].charAt(0) + (/^[0-9]+$/.test(last) ? last : last.charAt(0))).toUpperCase().slice(0, 2);
}
/* One transcript row: avatar, speaker name, timestamp, text. You and Them differ only in the
   avatar tint and the name weight, not in colour lanes. */
function speakerRow(o) {
  return '<div class="segment ' + (o.mic ? 'you' : 'them') + '"' + (o.attrs ? ' ' + o.attrs : '') + '><span class="avatar" aria-hidden="true">' + escapeHtml(initials(o.label)) + '</span><div class="seg-body"><div class="seg-head"><span class="label">' + (o.personId ? '<a href="/people/' + encodeURIComponent(o.personId) + '">' + escapeHtml(o.label) + '</a>' : escapeHtml(o.label)) + '</span><span class="ts">' + fmtDuration(o.start) + '</span></div><div class="text' + (o.approximate ? ' approximate' : '') + '">' + escapeHtml(o.text) + '</div></div></div>';
}
/* Live preview, the way the Windows client shows it: a running "You: ..." / "Them: ..." text, no
   avatars or times. Partials arrive per track as each one is transcribed, so one side's line can land
   well after the other side's later lines: show them in meeting-time order. */
var LIVE_EMPTY_TEXT = 'A rough live transcript appears here while recording. The transcript you keep is made from the full recording after the meeting.';
function livePartials(item) {
  return ((item && item.partials) || []).slice().sort(function (a, b) { return (a.start || 0) - (b.start || 0); });
}
function liveWho(p) { return p.track === 'mic' ? 'You' : 'Them'; }
function liveLinesHtml(item, limit) {
  var rows = livePartials(item);
  if (limit) rows = rows.slice(-limit);
  return rows.map(function (p) {
    return '<p class="lp-line ' + (p.track === 'mic' ? 'you' : 'them') + '"><span class="lp-who">' + liveWho(p) + ':</span> ' + escapeHtml(p.text) + '</p>';
  }).join('');
}
/* Same lines built as DOM nodes (textContent only). Keeps the reader's place: it follows new lines only
   while already at the bottom. */
function liveFillLines(el, item) {
  var rows = livePartials(item), sig = JSON.stringify(rows.map(function (p) { return [p.track, p.start, p.text]; }));
  if (el._sig === sig) return;
  var first = el._sig === undefined, atBottom = first || el.scrollHeight - el.scrollTop - el.clientHeight < 24;
  el._sig = sig;
  el.textContent = '';
  if (!rows.length) {
    var empty = document.createElement('p'); empty.className = 'lp-empty'; empty.textContent = LIVE_EMPTY_TEXT; el.appendChild(empty);
  }
  rows.forEach(function (p) {
    var line = document.createElement('p'), who = document.createElement('span');
    line.className = 'lp-line ' + (p.track === 'mic' ? 'you' : 'them');
    who.className = 'lp-who'; who.textContent = liveWho(p) + ':';
    line.appendChild(who); line.appendChild(document.createTextNode(' ' + (p.text == null ? '' : String(p.text))));
    el.appendChild(line);
  });
  if (atBottom) el.scrollTop = el.scrollHeight;
}
function stateBadge(row) {
  var state = row.latest_state;
  if (!state) return badge('none', 'No job yet');
  var trs = (row.pipeline || {}).transcription || {};
  if (state === "running" && trs.stage === "speakers" && trs.speakers) {
    return badge('running', 'Identifying speakers' + (trs.speakers.percent == null ? '' : ' ' + Math.round(trs.speakers.percent) + '%'));
  }
  if (state === "running" || state === "queued") {
    var pct = row.latest_progress != null ? Math.round(row.latest_progress * 100) + "%" : "";
    return badge('running', state.charAt(0).toUpperCase() + state.slice(1) + (pct ? " " + pct : ""));
  }
  if (state === "error") {
    return badge('error', 'Error' + (row.latest_error ? ": " + row.latest_error : ""));
  }
  return badge('done', 'Done');
}
function processingStateBase(row) {
  var pipeline = row.pipeline || {}, upload = pipeline.upload || {}, transcription = pipeline.transcription || {};
  var uploadState = String(upload.state || "").toLowerCase(), transcriptionState = String(transcription.state || "").toLowerCase();
  function complete(s) { return s === "complete" || s === "completed" || s === "done" || s === "uploaded" || s === "ready"; }
  if (uploadState.indexOf("error") >= 0 || uploadState.indexOf("fail") >= 0) {
    return {key:"error", label:"Upload failed", pct:null, detail:upload.error || ""};
  }
  if (uploadState && !complete(uploadState)) {
    return {key:uploadState.indexOf("upload") >= 0 ? "uploading" : "upload", label:uploadState.indexOf("upload") >= 0 ? "Uploading" : "Pending end of meeting", pct:uploadState === "pending" ? null : upload.percent};
  }
  if (transcriptionState) {
    if (complete(transcriptionState)) {
      var sl = pipeline.speaker_labels;  // speaker labels still to come for a finished transcript
      if (sl && sl.state === "waiting") return {key:"queued", label:"Waiting for a GPU helper", pct:null, where:"Speaker labels: waiting for a GPU helper"};
      if (sl && sl.state === "running") return {key:"transcribing", label:"Identifying speakers", pct:sl.percent};
      if (sl && sl.state === "queued") return {key:"queued", label:"Speaker labels queued", pct:null};
      return {key:"complete", label:"Complete", pct:100};
    }
    if (transcriptionState.indexOf("error") >= 0 || transcriptionState.indexOf("fail") >= 0) return {key:"error", label:"Needs attention", pct:null};
    if (transcriptionState.indexOf("transcrib") >= 0 || transcriptionState === "running") {
      if (transcription.stage === "speakers" && transcription.speakers) return {key:"transcribing", label:"Identifying speakers", pct:transcription.speakers.percent};
      return {key:"transcribing", label:"Transcribing", pct:transcription.transcribe_percent != null ? transcription.transcribe_percent : transcription.percent};
    }
    return {key:"queued", label:"Queued", pct:transcription.percent};
  }
  var state = row.latest_state;
  if (!row.has_audio) return {key:"upload", label:"Pending end of meeting", pct:null};
  if (state === "running") return {key:"transcribing", label:"Transcribing", pct:row.latest_progress == null ? null : row.latest_progress * 100};
  if (state === "queued") return {key:"queued", label:"Queued", pct:row.latest_progress == null ? null : row.latest_progress * 100};
  if (state === "error") return {key:"error", label:"Needs attention", pct:null};
  if (state === "done") return {key:"complete", label:"Complete", pct:100};
  return {key:"upload", label:"Ready to transcribe", pct:null};
}
/* Where the transcript ran, from pipeline.transcription.compute (status, helper_name, fallback_reason only).
   Empty when the job never involved a GPU helper. */
function computeWhere(tr) {
  var c = tr && tr.compute;
  if (!c || !c.status || tr.state === 'error') return '';
  var done = tr.state === 'complete', who = c.helper_name || 'a GPU helper';
  var trPct = tr.transcribe_percent != null ? tr.transcribe_percent : tr.percent;
  var pct = (trPct == null || tr.stage === 'speakers') ? '' : ' · ' + Math.round(trPct) + '%';
  if (c.status === 'offered') return done ? '' : 'Waiting for a GPU helper';
  if (c.status === 'leased' || c.status === 'done') {
    return done ? 'Transcribed on ' + who + ' (GPU)' : 'Transcribing on ' + who + ' (GPU)' + pct;
  }
  if (c.status === 'local' && c.fallback_reason) {
    return (done ? 'Transcribed' : 'Transcribing') + ' on this server (GPU helper: ' + c.fallback_reason + ')';
  }
  return '';
}
function processingState(row) {
  var st = processingStateBase(row);
  st.where = computeWhere((row.pipeline || {}).transcription) || st.where;
  return st;
}
function processingBadge(row) {
  var status = processingState(row), rawPct = status.pct, pct = rawPct == null || status.key === "complete" ? "" : " " + Math.round(rawPct) + "%";
  var cls = status.key === "complete" ? "done" : (status.key === "error" ? "error" : (status.key === "transcribing" || status.key === "queued" || status.key === "uploading" ? "running" : "none"));
  var errorDetail = status.detail || row.latest_error || "";
  return badge(cls, status.label + pct, status.key === "error" ? errorDetail : (status.where || ""));
}
"""

_JS_HELPERS = _JS_HELPERS_SRC

# Native <dialog> used by confirmDialog() (JS helpers above); styled by .dialog in app.css.
_NOTE_TYPE_TIP = (
    "A note type decides two things: how the AI writes the summary, and which Notion page the notes are saved under. "
    "Changing the type and regenerating rewrites the summary and moves the Notion copy to that type's page."
)

_CONFIRM_DIALOG_HTML = """<dialog class="dialog" id="confirm-dialog" aria-labelledby="confirm-title" aria-describedby="confirm-lead">
  <form method="dialog" class="dialog-form">
    <h2 class="dialog-title" id="confirm-title"></h2>
    <p class="dialog-lead" id="confirm-lead"></p>
    <p class="dialog-warning" role="alert" hidden></p>
    <ul class="dialog-items" tabindex="0" aria-label="Meetings" hidden></ul>
    <p class="dialog-note"></p>
    <div class="dialog-foot">
      <button type="submit" value="cancel" class="btn secondary" data-dialog-cancel>Cancel</button>
      <button type="submit" value="ok" class="btn danger" data-dialog-ok>Confirm</button>
    </div>
  </form>
</dialog>"""
