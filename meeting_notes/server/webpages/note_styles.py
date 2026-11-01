"""The Note styles section of Settings."""

from __future__ import annotations

import html
import json

from meeting_notes.server import settings as settings_mod
from .shell import _icon


_NOTE_STYLES_JS = r"""
(function () {
  var list = document.getElementById("style-list");
  var json = document.getElementById("note-templates-json");
  var def = document.getElementById("default-template-id");
  var form = list.closest("form");
  var addBtn = document.getElementById("style-add");
  var tpl = document.getElementById("style-tpl");
  var errBox = document.getElementById("style-error");
  function userStyles() { return Array.prototype.slice.call(list.querySelectorAll(".style:not([data-standard])")); }
  function allStyles() { return Array.prototype.slice.call(list.querySelectorAll(".style")); }
  function nameOf(el) { return (el.querySelector(".style-name-input").value || "").trim(); }
  function refresh() {
    var current = def.value;
    def.innerHTML = "";
    allStyles().forEach(function (el) {
      var o = document.createElement("option");
      o.value = el.dataset.id;
      o.textContent = nameOf(el) || "Untitled note type";
      def.appendChild(o);
      el.querySelector(".style-title").textContent = nameOf(el) || "Untitled note type";
    });
    if (Array.prototype.some.call(def.options, function (o) { return o.value === current; })) def.value = current;
    else def.value = "standard";
    allStyles().forEach(function (el) { el.querySelector(".style-default").hidden = el.dataset.id !== def.value; });
  }
  var parentInfo = {};
  function renderDest(el) {
    var nIn = el.querySelector(".style-notion-input"), sum = el.querySelector(".style-sum"), link = el.querySelector(".style-notion-link");
    if (!nIn || !sum) return;
    var v = nIn.value.trim(), saved = (nIn.dataset.saved || "").trim(), info = parentInfo[el.dataset.id];
    var known = v && v === saved && info;
    sum.textContent = !v ? "· Not saved to Notion" : (known && info.title ? "· Notion: " + info.title : "· Notion page set");
    link.textContent = "";
    if (!v) { link.textContent = "Not saved to Notion"; return; }
    if (v !== saved) { link.textContent = "Save settings to check this page."; return; }
    var a = document.createElement("a");
    a.href = info ? info.url : "https://www.notion.so/" + v.replace(/-/g, "");
    a.target = "_blank"; a.rel = "noopener";
    a.textContent = (info && info.title) || "Open Notion page";
    link.appendChild(document.createTextNode("Saves to "));
    link.appendChild(a);
  }
  function loadParents() {
    fetch("/v1/notion/parents", {credentials: "same-origin"}).then(function (r) { return r.ok ? r.json() : {}; })
      .then(function (d) { parentInfo = (d && d.items) || {}; allStyles().forEach(function (st) { renderDest(st); if (st._pickUpdate) st._pickUpdate(); }); }).catch(function () {});
  }
  var pages = {state: "idle", items: [], connected: true, error: null}, pageWaiters = [];
  function bareId(v) { return String(v || "").replace(/-/g, "").toLowerCase(); }
  function pageById(v) {
    var b = bareId(v);
    if (!b) return null;
    for (var i = 0; i < pages.items.length; i++) if (bareId(pages.items[i].id) === b) return pages.items[i];
    return null;
  }
  function notifyPages() { pageWaiters.slice().forEach(function (fn) { fn(); }); }
  function loadPages(refresh) {
    pages.state = "loading";
    notifyPages();
    return fetch("/v1/notion/pages" + (refresh ? "?refresh=1" : ""), {credentials: "same-origin"})
      .then(function (r) { return r.ok ? r.json() : r.json().catch(function () { return {}; }).then(function (d) { throw new Error((d && d.detail) || ("Could not load Notion pages (HTTP " + r.status + ").")); }); })
      .then(function (d) {
        pages.connected = d.connected !== false; pages.items = d.items || []; pages.error = d.error || null;
        pages.state = pages.error ? "error" : "ready";
      })
      .catch(function (e) { pages.items = []; pages.error = e.message || "Could not load Notion pages."; pages.state = "error"; })
      .then(notifyPages);
  }
  function setupPicker(el) {
    var nIn = el.querySelector(".style-notion-input"), btn = el.querySelector(".style-notion-pick"), panel = el.querySelector(".style-pick-panel");
    if (!nIn || !btn || !panel) return;
    var label = btn.querySelector(".style-pick-label");
    var expanded = {}, query = "", pasteShown = false, wantExpand = false;
    var search, tree, note, pasteWrap, pasteInput;
    function titleOf(v) {
      var pg = pageById(v), info = parentInfo[el.dataset.id];
      if (pg) return pg.title;
      if (info && info.title && v === (nIn.dataset.saved || "").trim()) return info.title;
      return "Notion page set";
    }
    function updateLabel() {
      var v = nIn.value.trim();
      label.textContent = v ? titleOf(v) : "Choose a Notion page";
      btn.title = label.textContent;
    }
    el._pickUpdate = updateLabel;
    function isOpen() { return !panel.hidden; }
    function close(refocus) {
      if (!isOpen()) return;
      panel.hidden = true; btn.setAttribute("aria-expanded", "false");
      var k = pageWaiters.indexOf(onPages); if (k >= 0) pageWaiters.splice(k, 1);
      if (refocus) btn.focus();
    }
    function choose(value) {
      nIn.value = value;
      nIn.dispatchEvent(new Event("input", {bubbles: true}));
      updateLabel();
      close(true);
    }
    function expandToSelected() {
      expanded = {};
      var pg = pageById(nIn.value.trim()), guard = 0;
      while (pg && pg.parent && guard++ < 100) { expanded[pg.parent] = true; pg = pageById(pg.parent); }
    }
    function build() {
      panel.textContent = "";
      search = document.createElement("input");
      search.type = "search"; search.className = "style-pick-search"; search.placeholder = "Search pages";
      search.setAttribute("aria-label", "Search Notion pages"); search.autocomplete = "off"; search.spellcheck = false;
      search.addEventListener("input", function () { query = search.value.trim().toLowerCase(); render(); });
      search.addEventListener("keydown", function (e) {
        if (e.key === "Enter") e.preventDefault();
        if (e.key === "ArrowDown") { var first = tree.querySelector("button.style-pick-row"); if (first) { e.preventDefault(); first.focus(); } }
      });
      tree = document.createElement("div"); tree.className = "style-pick-tree";
      note = document.createElement("p"); note.className = "style-pick-note"; note.setAttribute("role", "status");
      var foot = document.createElement("div"); foot.className = "style-pick-foot";
      var refreshBtn = document.createElement("button");
      refreshBtn.type = "button"; refreshBtn.className = "btn ghost sm style-pick-refresh"; refreshBtn.textContent = "Refresh";
      refreshBtn.addEventListener("click", function () { loadPages(true); });
      var pasteBtn = document.createElement("button");
      pasteBtn.type = "button"; pasteBtn.className = "btn ghost sm style-pick-paste-btn"; pasteBtn.textContent = "Page not listed? Paste a link";
      pasteBtn.addEventListener("click", function () { pasteShown = !pasteShown; render(); if (pasteShown) pasteInput.focus(); });
      foot.appendChild(refreshBtn); foot.appendChild(pasteBtn);
      pasteWrap = document.createElement("div"); pasteWrap.className = "style-pick-pastewrap";
      pasteInput = document.createElement("input");
      pasteInput.type = "text"; pasteInput.className = "style-pick-paste"; pasteInput.placeholder = "Paste a Notion page link or id, then press Enter";
      pasteInput.setAttribute("aria-label", "Notion page link or id"); pasteInput.autocomplete = "off"; pasteInput.spellcheck = false;
      function applyPaste() {
        var raw = pasteInput.value.trim();
        if (!raw) return;
        var m = raw.replace(/-/g, "").match(/[0-9a-fA-F]{32}(?![0-9a-fA-F])/), known = m ? pageById(m[0]) : null;
        pasteInput.value = "";
        choose(known ? bareId(known.id) : raw);
      }
      pasteInput.addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); applyPaste(); } });
      pasteInput.addEventListener("change", applyPaste);
      pasteWrap.appendChild(pasteInput);
      panel.appendChild(search); panel.appendChild(tree); panel.appendChild(note); panel.appendChild(foot); panel.appendChild(pasteWrap);
    }
    function row(depth, item, hasKids, forceOpen, dim) {
      var wrap = document.createElement("div");
      wrap.className = "style-pick-line"; wrap.style.paddingLeft = (depth * 16) + "px";
      wrap.dataset.pid = item.id;
      var open = forceOpen || !!expanded[item.id];
      if (hasKids) {
        var tg = document.createElement("button");
        tg.type = "button"; tg.className = "style-pick-toggle";
        tg.setAttribute("aria-label", open ? "Collapse" : "Expand"); tg.setAttribute("aria-expanded", open ? "true" : "false");
        tg.textContent = "›"; tg.dataset.open = open ? "1" : "0";
        tg.addEventListener("click", function () {
          expanded[item.id] = !open; render();
          var again = tree.querySelector('[data-pid="' + item.id + '"] .style-pick-toggle'); if (again) again.focus();
        });
        wrap.appendChild(tg);
      } else {
        var sp = document.createElement("span"); sp.className = "style-pick-spacer"; wrap.appendChild(sp);
      }
      var b = document.createElement("button");
      b.type = "button"; b.className = "style-pick-row" + (dim ? " dim" : "");
      if (bareId(item.id) === bareId(nIn.value)) { b.classList.add("selected"); b.setAttribute("aria-current", "true"); }
      if (item.icon) { var ic = document.createElement("span"); ic.className = "style-pick-icon"; ic.textContent = item.icon; ic.setAttribute("aria-hidden", "true"); b.appendChild(ic); }
      var t = document.createElement("span"); t.className = "style-pick-title"; t.textContent = item.title; b.appendChild(t);
      b.addEventListener("click", function () { choose(bareId(item.id)); });
      wrap.appendChild(b);
      return wrap;
    }
    function render() {
      if (!isOpen()) return;
      var keepSearch = document.activeElement === search;
      tree.textContent = "";
      var none = document.createElement("button");
      none.type = "button"; none.className = "style-pick-row style-pick-none" + (nIn.value.trim() ? "" : " selected");
      none.textContent = "None (don't save to Notion)";
      none.addEventListener("click", function () { choose(""); });
      tree.appendChild(none);
      var msg = "";
      if (pages.state === "loading" || pages.state === "idle") msg = "Loading Notion pages...";
      else if (!pages.connected) { msg = "Connect Notion above to choose a page."; pasteShown = true; }
      else if (pages.error) msg = pages.error;
      else if (!pages.items.length) msg = "No pages are shared with the integration yet.";
      var kidsOf = {}, roots = [];
      pages.items.forEach(function (it) {
        if (it.parent) (kidsOf[it.parent] = kidsOf[it.parent] || []).push(it); else roots.push(it);
      });
      function byTitle(a, b) { var x = a.title.toLowerCase(), y = b.title.toLowerCase(); return x < y ? -1 : (x > y ? 1 : 0); }
      var show = null;
      if (query) {
        show = {};
        pages.items.forEach(function (it) {
          if (it.title.toLowerCase().indexOf(query) < 0) return;
          var cur = it, guard = 0;
          while (cur && guard++ < 100) { show[cur.id] = cur === it ? 2 : (show[cur.id] || 1); cur = cur.parent ? pageById(cur.parent) : null; }
        });
        if (!Object.keys(show).length && !msg) msg = "No pages match.";
      }
      note.textContent = msg; note.hidden = !msg;
      note.classList.toggle("err", !!pages.error && pages.state === "error");
      function walk(list, depth) {
        list.slice().sort(byTitle).forEach(function (it) {
          if (show && !show[it.id]) return;
          var kids = (kidsOf[it.id] || []).filter(function (k) { return !show || show[k.id]; });
          tree.appendChild(row(depth, it, kids.length > 0, !!show, !!show && show[it.id] === 1));
          if (kids.length && (show || expanded[it.id])) walk(kids, depth + 1);
        });
      }
      walk(roots, 0);
      pasteWrap.hidden = !pasteShown;
      if (keepSearch) search.focus();
    }
    function onPages() {
      if (wantExpand && pages.state === "ready") { wantExpand = false; expandToSelected(); }
      render();
    }
    function open() {
      if (!panel.firstChild) build();
      panel.hidden = false; btn.setAttribute("aria-expanded", "true");
      expandToSelected(); query = ""; search.value = ""; pasteShown = false;
      wantExpand = pages.state !== "ready";  // the list is not loaded yet: open the selected page's path once it is
      pageWaiters.push(onPages);
      if (pages.state === "idle") loadPages(false); else render();
      search.focus();
    }
    btn.addEventListener("click", function () { if (isOpen()) close(false); else open(); });
    panel.addEventListener("keydown", function (e) {
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(true); return; }
      if ((e.key === "ArrowRight" || e.key === "ArrowLeft") && tree.contains(e.target)) {
        var line = e.target.closest(".style-pick-line"), tgl = line && line.querySelector(".style-pick-toggle");
        if (tgl && (tgl.dataset.open === "1") === (e.key === "ArrowLeft")) {
          e.preventDefault();
          var pid = line.dataset.pid;
          tgl.click();  // re-renders; keep focus on the same page's row
          var back = tree.querySelector('[data-pid="' + pid + '"] .style-pick-row'); if (back) back.focus();
        }
        return;
      }
      if ((e.key !== "ArrowDown" && e.key !== "ArrowUp") || !tree.contains(e.target)) return;
      // Up/Down move between rows (and chevrons); Up from the first row goes back to the search box.
      var rows = Array.prototype.slice.call(tree.querySelectorAll("button.style-pick-row"));
      var i = rows.indexOf(e.target.closest("button.style-pick-row") || e.target);
      if (i < 0) { var row = e.target.parentNode && e.target.parentNode.querySelector("button.style-pick-row"); i = rows.indexOf(row); }
      e.preventDefault();
      if (e.key === "ArrowDown") { if (i < rows.length - 1) rows[i + 1].focus(); }
      else if (i > 0) rows[i - 1].focus(); else search.focus();
    });
    document.addEventListener("click", function (e) {
      if (!el.isConnected || !e.target.isConnected) return;
      if (isOpen() && !panel.contains(e.target) && !btn.contains(e.target)) close(false);
    });
    pageWaiters.push(updateLabel);
    updateLabel();
  }
  function randomId() {
    var a = new Uint8Array(6), out = "t";
    (window.crypto || window.msCrypto).getRandomValues(a);
    for (var i = 0; i < a.length; i++) out += ("0" + a[i].toString(16)).slice(-2);
    return out;
  }
  function wire(el) {
    var nameInput = el.querySelector(".style-name-input"), resetBtn = el.querySelector(".style-name-reset");
    function syncReset() { if (resetBtn) resetBtn.hidden = nameInput.value.trim() === resetBtn.dataset["default"]; }
    nameInput.addEventListener("input", function () { this.removeAttribute("aria-invalid"); syncReset(); refresh(); });
    if (resetBtn) resetBtn.addEventListener("click", function () { nameInput.value = resetBtn.dataset["default"]; nameInput.removeAttribute("aria-invalid"); syncReset(); refresh(); });
    var del = el.querySelector(".style-delete");
    if (del) del.addEventListener("click", function () { el.remove(); refresh(); errBox.hidden = true; });
    var nIn = el.querySelector(".style-notion-input");
    var nBtn = el.querySelector(".style-notion-backfill"), nMsg = el.querySelector(".style-notion-msg");
    if (nIn) nIn.addEventListener("input", function () { renderDest(el); if (el._pickUpdate) el._pickUpdate(); });
    setupPicker(el);
    renderDest(el);
    var nMove = el.querySelector(".style-notion-moved");
    function plural(n, one, many) { return n + " " + (n === 1 ? one : many); }
    // Notes copied under the note type's previous Notion page: offer to move them to the current one.
    if (nMove && nIn && (nIn.dataset.saved || "").trim()) {
      fetch("/v1/notion/backfill?quick=1&template=" + encodeURIComponent(el.dataset.id), {credentials: "same-origin"})
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) {
          if (!d || !d.moved) return;
          nMove.querySelector(".style-notion-moved-text").textContent =
            plural(d.moved, "meeting's notes are", "meetings' notes are") + " still on the previous Notion page.";
          nMove.hidden = false;
        }).catch(function () {});
    }
    if (nMove) nMove.querySelector("button").addEventListener("click", function () { if (nBtn) nBtn.click(); });
    if (nBtn) nBtn.addEventListener("click", function () {
      var id = el.dataset.id;
      function say(m) { nMsg.textContent = m; }
      if ((nIn.value || "").trim() !== (nIn.dataset.saved || "").trim()) { say("Save settings first."); return; }
      if (!nIn.value.trim()) { say("Set a Notion parent page and save settings first."); return; }
      nBtn.disabled = true; say("Checking...");
      function readErr(r) { return r.json().catch(function () { return {}; }).then(function (d) { throw new Error((d && d.detail) || ("Something went wrong (HTTP " + r.status + ").")); }); }
      fetch("/v1/notion/backfill?template=" + encodeURIComponent(id), {credentials: "same-origin"})
        .then(function (r) { return r.ok ? r.json() : readErr(r); })
        .then(function (d) {
          if (!d.count) { say("Nothing to copy: every meeting with notes of this note type is already on this Notion page."); return null; }
          var styleName = (d.template && d.template.name) || nameOf(el) || "this note type", parts = [];
          if (d["new"]) parts.push(plural(d["new"], "meeting", "meetings") + " not in Notion yet");
          if (d.moved) parts.push(plural(d.moved, "meeting", "meetings") + " on the previous Notion page (moved here; the old copy is removed)");
          if (d.gone) parts.push(plural(d.gone, "meeting", "meetings") + " whose Notion copy was deleted (copied again)");
          return confirmDialog({title: "Copy " + plural(d.count, "meeting", "meetings") + " to Notion?",
                                lead: styleName + ": " + parts.join("; ") + ". They go into the monthly pages in order, newest at the top.",
                                note: "This runs in the background.", confirmLabel: "Copy to Notion"}).then(function (ok) {
            if (!ok) { say(""); return null; }
            return fetch("/v1/notion/backfill", {method: "POST", credentials: "same-origin", headers: {"Content-Type": "application/json"}, body: JSON.stringify({template: id})})
              .then(function (r) { return r.ok ? r.json() : readErr(r); })
              .then(function (q) { if (nMove) nMove.hidden = true; say("Queued " + q.queued + " meeting" + (q.queued === 1 ? "" : "s") + ". They will appear in Notion over the next few minutes."); });
          });
        })
        .catch(function (e) { say(e.message); })
        .finally(function () { nBtn.disabled = false; });
    });
  }
  allStyles().forEach(wire);
  def.addEventListener("change", refresh);
  addBtn.addEventListener("click", function () {
    var node = tpl.content.firstElementChild.cloneNode(true);
    node.dataset.id = randomId();
    node.querySelector(".style-prompt-input").value = document.querySelector('textarea[name="ai_workflow"]').value;
    list.appendChild(node);
    wire(node);
    node.open = true;
    refresh();
    var input = node.querySelector(".style-name-input");
    input.focus();
    if (node.scrollIntoView) node.scrollIntoView({block: "nearest"});
  });
  function fail(el, field, message) {
    el.open = true;
    field.setAttribute("aria-invalid", "true");
    errBox.textContent = message;
    errBox.hidden = false;
    field.focus();
  }
  form.addEventListener("submit", function (event) {
    var parents = {}, hidden = document.getElementById("notion-parents-json");
    allStyles().forEach(function (st) { var f = st.querySelector(".style-notion-input"); parents[st.dataset.id] = f ? f.value.trim() : ""; });
    if (hidden) hidden.value = JSON.stringify(parents);
    var autoTypes = [], autoHidden = document.getElementById("notion-auto-types-json");
    allStyles().forEach(function (st) { var c = st.querySelector(".style-notion-auto"); if (c && c.checked) autoTypes.push(st.dataset.id); });
    if (autoHidden) autoHidden.value = JSON.stringify(autoTypes);
    var notesTypes = [], notesHidden = document.getElementById("auto-notes-types-json");
    allStyles().forEach(function (st) { var c = st.querySelector(".style-auto-notes"); if (c && c.checked) notesTypes.push(st.dataset.id); });
    if (notesHidden) notesHidden.value = JSON.stringify(notesTypes);
    errBox.hidden = true;
    var seen = {}, out = [];
    var styles = allStyles();
    for (var i = 0; i < styles.length; i++) {
      var el = styles[i], id = el.dataset.id;
      var nameField = el.querySelector(".style-name-input"), promptField = el.querySelector(".style-prompt-input");
      var name = nameOf(el);
      if (!name) { event.preventDefault(); return fail(el, nameField, "Give every note type a name."); }
      if (seen[name.toLowerCase()]) { event.preventDefault(); return fail(el, nameField, "Note type names must be unique: " + name); }
      seen[name.toLowerCase()] = 1;
      if (el.hasAttribute("data-standard")) continue;
      if (!promptField.value.trim()) { event.preventDefault(); return fail(el, promptField, "The summary instructions for " + (name || "this note type") + " can't be empty."); }
      out.push({id: id, name: name, prompt: promptField.value});
    }
    if (!document.querySelector('textarea[name="ai_workflow"]').value.trim()) {
      event.preventDefault();
      return fail(list.querySelector("[data-standard]"), document.querySelector('textarea[name="ai_workflow"]'), "The Standard summary instructions can't be empty.");
    }
    json.value = JSON.stringify(out);
  });
  refresh();
  loadParents();
})();
"""


def _note_style_item(template: dict, *, standard: bool, builtin: bool, default_id: str, notion_parent: str = "", notion_auto: bool = False, auto_notes: bool = False) -> str:
    tid = html.escape(template["id"])
    name = html.escape(template["name"])
    prompt = html.escape(template["prompt"])
    attrs = f' data-id="{tid}"'
    if standard:
        attrs += " data-standard"
    if builtin:
        attrs += " data-builtin"
    prompt_name = ' name="ai_workflow"' if standard else ""
    name_attr = ' name="standard_name"' if standard else ""
    default_name = settings_mod.BUILTIN_TEMPLATES.get(template["id"], ("", ""))[0]
    default_hidden = "" if template["id"] == default_id else " hidden"
    badge = '<span class="style-badge">Built-in</span>' if builtin else ""
    footer = (
        '<a class="btn secondary sm" href="/v1/bridge/workflow.md" download>Download prompt</a>'
        if standard else (
            "" if builtin else
            f'<button type="button" class="btn danger sm style-delete">{_icon("trash", 14)}<span>Delete note type</span></button>'
        )
    )
    reset_hidden = "" if template["name"] != default_name else " hidden"
    name_help = (
        f'<span class="help" style="margin:0"><button type="button" class="btn secondary sm style-name-reset"'
        f' data-default="{html.escape(default_name, quote=True)}"{reset_hidden}>Reset name</button></span>'
        if builtin else ""
    )
    notion_value = html.escape(notion_parent or "", quote=True)
    pick_label = "Notion page set" if notion_parent else "Choose a Notion page"
    if notion_parent:
        notion_sum = "· Notion page set"
        notion_link = (f'Saves to <a href="https://www.notion.so/{html.escape(notion_parent.replace("-", ""), quote=True)}"'
                       f' target="_blank" rel="noopener">Open Notion page</a>')
    else:
        notion_sum = "· Not saved to Notion"
        notion_link = "Not saved to Notion"
    return f"""<details class="style"{attrs}>
  <summary><span class="style-title">{name}</span>{badge}<span class="style-badge style-default"{default_hidden}>Default</span><span class="style-sum">{notion_sum}</span></summary>
  <div class="style-body">
    <label class="field"><span class="name">Name</span>
      <input type="text" class="style-name-input"{name_attr} maxlength="{settings_mod.MAX_TEMPLATE_NAME_CHARS}" value="{name}" autocomplete="off" placeholder="e.g. Customer call">
      {name_help}</label>
    <div class="style-part">
    <label class="field"><span class="name">Summary instructions</span>
      <span class="help" style="margin:0">What the AI writes for this type of meeting.</span>
      <textarea{prompt_name} class="style-prompt-input" rows="16">{prompt}</textarea></label>
    </div>
    <div class="style-part">
      <label class="checkbox"><input type="checkbox" class="style-auto-notes"{" checked" if auto_notes else ""}>
        <span>Generate notes automatically</span></label>
      <p class="help" style="margin:0">When a meeting of this note type finishes transcribing, build its notes without a click. If this is off, those meetings can still get notes with the Generate button.</p>
    </div>
    <div class="style-notion style-part">
      <div class="field style-pick-field"><span class="name">Save to Notion</span>
        <span class="help" style="margin:0">Notes of this type go into month pages under the Notion page you choose.</span>
        <input type="hidden" class="style-notion-input" value="{notion_value}" data-saved="{notion_value}">
        <button type="button" class="btn secondary style-notion-pick" aria-haspopup="true" aria-expanded="false"><span class="style-pick-label">{pick_label}</span><span class="style-pick-caret" aria-hidden="true">&#9662;</span></button>
        <div class="style-pick-panel" hidden></div>
        <span class="help" style="margin:0">Only pages shared with the integration are listed (page ••• menu, then Connections). Choose None to keep this note type out of Notion.</span></div>
      <p class="style-notion-link" role="status">{notion_link}</p>
      <label class="checkbox"><input type="checkbox" class="style-notion-auto"{" checked" if notion_auto else ""}>
        <span>Copy notes to Notion automatically</span></label>
      <p class="help" style="margin:0 0 10px">When notes of this type finish (whether generated automatically or with the Generate button), copy them to this Notion page. Re-generated notes update the existing copy.</p>
      <div class="inline-actions"><button type="button" class="btn secondary sm style-notion-backfill">Copy existing notes</button><span class="help style-notion-explain" style="margin:0">Copies notes of this type that are missing from this Notion page: never copied, still on a previous page, or deleted in Notion.</span><span class="help style-notion-msg" role="status"></span></div>
      <div class="banner style-notion-moved" role="status" hidden><span class="style-notion-moved-text"></span> <button type="button" class="btn secondary sm">Move them here</button></div>
    </div>
    <div class="inline-actions">{footer}</div>
  </div>
</details>"""


def _note_styles_html(settings) -> str:
    default_id = settings.default_template()["id"]
    items = [
        _note_style_item(t, standard=t["id"] == settings_mod.STANDARD_TEMPLATE_ID, builtin=t["builtin"], default_id=default_id,
            notion_parent=(getattr(settings, "notion_parents", None) or {}).get(t["id"], ""),
            notion_auto=t["id"] in (getattr(settings, "notion_auto_types", None) or []),
            auto_notes=t["id"] in (getattr(settings, "auto_notes_types", None) or []))
        for t in settings.all_templates()
    ]
    blank = _note_style_item(
        {"id": "new", "name": "", "prompt": ""}, standard=False, builtin=False, default_id=default_id,
        auto_notes=True,
    )
    options = "".join(
        f'<option value="{html.escape(t["id"])}"{" selected" if t["id"] == default_id else ""}>{html.escape(t["name"])}</option>'
        for t in settings.all_templates()
    )
    items_html = "".join(items)
    stored = html.escape(json.dumps(settings.note_templates, ensure_ascii=False), quote=True)
    return f"""
    <input type="hidden" name="notion_parents" id="notion-parents-json" value="">
    <input type="hidden" name="notion_auto_types" id="notion-auto-types-json" value="">
    <input type="hidden" name="auto_notes_types" id="auto-notes-types-json" value="">
    <label class="field">
      <span class="name">Default note type</span>
      <select name="default_template_id" id="default-template-id">{options}</select>
    </label>
    <p class="help">Used when notes are built automatically for a meeting with no note type chosen, by the Generate button on the
    meetings list, and whenever a meeting is generated without picking a note type. You can choose a
    different note type for any single meeting from the meeting view.</p>

    <div class="styles" id="note-styles">
      <div class="styles-head">
        <div><p class="help" style="margin:0">A note type sets how the summary is written and where it's saved in Notion. Note types do not rename the saved meeting. Changes apply when you save.</p></div>
        <button type="button" class="btn secondary sm" id="style-add">{_icon("sparkles", 14)}<span>Add note type</span></button>
      </div>
      <input type="hidden" name="note_templates" id="note-templates-json" value="{stored}">
      <div class="banner err" id="style-error" role="alert" hidden></div>
      <div id="style-list">{items_html}</div>
      <template id="style-tpl">{blank}</template>
    </div>"""
