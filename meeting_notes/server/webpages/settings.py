"""The Settings page."""

from __future__ import annotations

import html
import json
from typing import Optional

from meeting_notes.server import settings as settings_mod
from .gpu import _GPU_HELPERS_JS, _GPU_SETTINGS_JS
from .ingestion import _INGESTION_JS, _SETTINGS_NAV_HTML, _SETTINGS_PAGE_ALIASES, _ingestion_chart_html
from .note_styles import _NOTE_STYLES_JS, _note_styles_html
from .shared import _CONFIRM_DIALOG_HTML, _JS_HELPERS
from .shell import _appearance_control, _icon, _normalize_appearance, _shell, _update_banner


# Settings sections that act immediately through the API (not through the form's
# Save button), so they live outside the <form>: AI access keys and client logs.
_SETTINGS_IMMEDIATE_HTML = r"""
<div class="settings-sheet immediate">
  <section class="sect" data-page="access" aria-labelledby="settings-notion-heading">
    <h2 id="settings-notion-heading">Notion</h2>
    <div class="sect-body">
    <p class="help">Copy finished meeting notes into Notion. Create an internal integration at
    notion.so/profile/integrations, then paste its token here (or set NOTION_TOKEN on the server). Connecting,
    testing and removing the token take effect immediately; they do not wait for Save settings.</p>
    <p class="notion-status" id="notion-status" role="status">Loading...</p>
    <div id="notion-token-area">
      <label class="field">
        <span class="name">Integration token</span>
        <input type="password" id="notion-token" autocomplete="off" spellcheck="false" placeholder="Paste the integration token">
      </label>
      <div class="inline-actions notion-actions">
        <button type="button" class="btn secondary" id="notion-connect">Connect</button>
        <button type="button" class="btn secondary" id="notion-test">Test connection</button>
        <button type="button" class="btn danger" id="notion-remove" hidden>Remove</button>
      </div>
    </div>
    <p class="error-text" id="notion-error" role="alert" hidden></p>
    <p class="help">For each parent page you set on a note type (under Note types): open the page in Notion,
    choose the ••• menu, then Connections, and add the integration. Otherwise Notion reports the page as not
    found. Monthly pages are created under the parent as "&lt;Month&gt;-&lt;YYYY&gt; &lt;note type name&gt;", with the
    newest meeting at the top.</p>
    </div>
  </section>
  <section class="sect" data-page="access" aria-labelledby="settings-agents-heading">
    <h2 id="settings-agents-heading">AI access</h2>
    <div class="sect-body">
    <p class="help">Agents such as Claude can read your meetings, notes, transcripts, action items and decisions
    with their own key. A key is separate from your sign-in token, never opens this website, and cannot delete
    anything. Creating and revoking keys takes effect immediately; it does not wait for Save settings.</p>
    <section class="reveal" id="key-reveal" aria-labelledby="key-reveal-heading" hidden>
      <header>__ICON_KEY__<h3 id="key-reveal-heading" tabindex="-1">Copy your new key now</h3></header>
      <div class="reveal-body">
        <p>This is the only time the full key is shown. Store it somewhere safe. If you lose it, revoke it and create another.</p>
        <div class="field-name">API key</div>
        <pre class="command" id="reveal-key" tabindex="0"></pre>
        <div class="copy-row"><button type="button" class="btn secondary" data-copy-target="reveal-key">__ICON_COPY__<span>Copy key</span></button></div>
        <div class="field-name">Add it to Claude Code</div>
        <pre class="command" id="reveal-cmd" tabindex="0"></pre>
        <div class="copy-row"><button type="button" class="btn secondary" data-copy-target="reveal-cmd">__ICON_COPY__<span>Copy command</span></button></div>
        <div class="field-name">For other agents (no key needed to read these)</div>
        <ul class="reveal-links">
          <li><a href="/api/v1/manifest" target="_blank" rel="noopener">/api/v1/manifest</a> machine-readable description</li>
          <li><a href="/llms.txt" target="_blank" rel="noopener">/llms.txt</a> short guide for language models</li>
          <li><a href="/api-docs.md" target="_blank" rel="noopener">/api-docs.md</a> full reference</li>
        </ul>
        <button type="button" class="btn secondary" id="key-reveal-done">I have saved the key</button>
      </div>
    </section>
    <div id="keys-box" aria-live="polite"><p class="help" role="status">Loading keys...</p></div>
    <div class="subsect">
      <h3 class="subsect-title">Create a key</h3>
      <form id="key-form" novalidate>
        <label class="field">
          <span class="name">Key name</span>
          <input type="text" id="key-name" maxlength="80" autocomplete="off" placeholder="Claude Code on my laptop">
        </label>
        <label class="checkbox">
          <input type="checkbox" id="key-write">
          <span>Allow writes (build notes, rename meetings)</span>
        </label>
        <p class="help">Unchecked, the key can only read. Even with writes on, a key can never delete anything.</p>
        <p class="error-text" id="key-error" role="alert" hidden></p>
        <p><button type="submit" class="btn secondary" id="key-create">__ICON_KEY__<span>Create key</span></button></p>
      </form>
    </div>
    </div>
  </section>
  <section class="sect" data-page="recorders" style="order:2" aria-labelledby="settings-recorders-heading">
    <h2 id="settings-recorders-heading">Recorders</h2>
    <div class="sect-body">
    <p class="help">Running recorders appear on the Recorders page while they are open, where you can see what they
    are doing and control them.</p>
    <p class="inline-actions"><a class="btn secondary" href="/recorders">__ICON_RADIO__<span>Open Recorders</span></a>
    <a class="btn secondary" href="/install">__ICON_DOWNLOAD__<span>Install a recorder</span></a></p>
    </div>
  </section>
  <section class="sect" data-page="recorders" style="order:3" aria-labelledby="settings-logs-heading">
    <h2 id="settings-logs-heading">Client logs</h2>
    <div class="sect-body">
    <p class="help">Diagnostic bundles sent from the Meeting Notes app on Windows or Mac (Logs, then Send to server). The app redacts the
    sign-in token before sending. The newest 20 bundles per computer are kept. Delete a bundle once you are done with it.</p>
    <div id="logs-box" aria-live="polite"><p class="help" role="status">Loading logs...</p></div>
    <div class="inline-actions"><button type="button" class="btn secondary" id="logs-refresh">__ICON_REFRESH__<span>Refresh</span></button><button type="button" class="btn danger" id="logs-delete-all" hidden>__ICON_TRASH__<span>Delete all</span></button></div>
    </div>
  </section>
</div>
"""

_SETTINGS_IMMEDIATE_JS = r"""
(function () {
  // "i" explanations: a tap toggles (touch screens don't always focus a tapped button); Escape or a tap
  // elsewhere closes. Hover and keyboard focus are handled by CSS.
  document.addEventListener('click', function (event) {
    var btn = event.target.closest && event.target.closest('.info-tip-btn');
    document.querySelectorAll('.info-tip.open').forEach(function (t) { if (!btn || t !== btn.parentNode) t.classList.remove('open'); });
    if (btn) { event.preventDefault(); btn.parentNode.classList.toggle('open'); }
  });
  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape') document.querySelectorAll('.info-tip.open').forEach(function (t) { t.classList.remove('open'); });
  });
  var SAVED_ADDRESS = __SERVER_ADDRESS_JSON__;
  function el(id) { return document.getElementById(id); }
  function base() { return String(SAVED_ADDRESS || location.origin).replace(/\/+$/, ''); }

  // ---- Keep your place when saving ----
  // Saving posts the form and the server renders the page again, which would land at the top. Remember the
  // scroll position and the open note types on submit, and put them back when the saved page loads.
  (function () {
    var KEY = 'mn-settings-place', form = document.querySelector('form.settings-sheet');
    // Saved synchronously (the page may unload right after). A save the page blocks as invalid does not
    // reload, and its entry is replaced by the next save or expires.
    // The place is kept relative to the note type (or section) in the middle of the screen, so a page that
    // renders a little differently (the banner, other open note types) still comes back to the same spot.
    function anchorAt() {
      var hit = document.elementFromPoint(window.innerWidth / 2, window.innerHeight / 2);
      var node = hit && hit.closest && (hit.closest('details.style[data-id]') || hit.closest('section.sect[aria-labelledby]'));
      if (!node) return null;
      return {style: node.dataset.id || null, sect: node.getAttribute('aria-labelledby'), top: node.getBoundingClientRect().top};
    }
    function findAnchor(a) {
      if (!a) return null;
      var nodes = document.querySelectorAll(a.style ? 'details.style[data-id]' : 'section.sect[aria-labelledby]');
      for (var i = 0; i < nodes.length; i++) {
        if (a.style ? nodes[i].dataset.id === a.style : nodes[i].getAttribute('aria-labelledby') === a.sect) return nodes[i];
      }
      return null;
    }
    if (form) form.addEventListener('submit', function () {
      var open = Array.prototype.map.call(document.querySelectorAll('details.style[open]'), function (d) { return d.dataset.id; });
      try { sessionStorage.setItem(KEY, JSON.stringify({y: window.scrollY, anchor: anchorAt(), open: open, at: Date.now()})); } catch (e) {}
    });
    var place = null;
    try { place = JSON.parse(sessionStorage.getItem(KEY) || 'null'); sessionStorage.removeItem(KEY); } catch (e) {}
    var banner = document.querySelector('.banner.ok, .banner.err');
    if (!place || !banner || Date.now() - place.at > 120000) return;
    if ('scrollRestoration' in history) history.scrollRestoration = 'manual';
    function restore() {  // once the whole page (and the toast) exists, so the scroll is not clamped
      var wanted = place.open || [];
      Array.prototype.forEach.call(document.querySelectorAll('details.style'), function (d) {
        if (wanted.indexOf(d.dataset.id) !== -1) d.open = true;
      });
      function align() {
        var node = findAnchor(place.anchor);
        if (node) window.scrollBy(0, node.getBoundingClientRect().top - place.anchor.top);
        else window.scrollTo(0, place.y || 0);
      }
      align();
      // Fonts and late styles can still move things once everything has loaded: line up again then,
      // unless the person has already scrolled.
      var lined = window.scrollY;
      window.addEventListener('load', function () { if (Math.abs(window.scrollY - lined) < 2) align(); });
      var text = (banner.textContent || '').trim();
      if (text) notify(text, banner.classList.contains('err') ? 'error' : undefined);
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', restore); else restore();
  })();
  function apiError(status, data) {
    if (status === 401 || status === 403) return 'Your sign-in has expired. Reload the page and sign in again.';
    return (data && typeof data.detail === 'string' && data.detail) || 'Something went wrong (HTTP ' + status + ').';
  }
  function api(method, url, body) {
    var options = {method: method, credentials: 'same-origin'};
    if (body) { options.headers = {'Content-Type': 'application/json'}; options.body = JSON.stringify(body); }
    return fetch(url, options).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (!r.ok) throw new Error(apiError(r.status, data));
        return data;
      });
    }, function () { throw new Error('Could not reach the server. Check your connection and try again.'); });
  }
  function loadError(message, retryAttr) {
    return '<p class="error-text" role="alert">' + escapeHtml(message) + '</p><p><button type="button" class="btn secondary" ' + retryAttr + '>Try again</button></p>';
  }

  // ---- AI access keys ----
  var keysBox = el('keys-box'), reveal = el('key-reveal');
  function renderKeys(items) {
    if (!items.length) {
      keysBox.innerHTML = '<div class="ledger-empty"><h3>No keys yet</h3><p>Create a key below, then paste it into the agent\'s setup and it can start reading your meetings. Give each agent its own key so you can revoke one without breaking the others.</p></div>';
      return;
    }
    items = items.slice().sort(function (a, b) { return (b.created_at || 0) - (a.created_at || 0); });
    var rows = items.map(function (k) {
      var revoked = !!k.revoked_at, write = (k.scopes || []).indexOf('write') >= 0;
      var status = revoked ? badge('none', 'Revoked') : badge('done', 'Active');
      var action = revoked ? '' : '<button type="button" class="btn danger sm row-btn" data-revoke="' + escapeHtml(k.id) + '" data-name="' + escapeHtml(k.name) + '">Revoke<span class="sr-only"> ' + escapeHtml(k.name) + '</span></button>';
      return '<tr' + (revoked ? ' class="revoked"' : '') + '>' +
        '<td class="k-name">' + escapeHtml(k.name) + '</td>' +
        '<td class="k-prefix"><code>' + escapeHtml(k.prefix) + '&hellip;</code></td>' +
        '<td class="k-access">' + (write ? 'Read + write' : 'Read') + '</td>' +
        '<td class="k-created" data-label="Created">' + escapeHtml(fmtDate(k.created_at, true)) + '</td>' +
        '<td class="k-used" data-label="Last used">' + (k.last_used_at ? escapeHtml(fmtDate(k.last_used_at, true)) : 'Never') + '</td>' +
        '<td class="k-status">' + status + '</td>' +
        '<td class="k-act">' + action + '</td></tr>';
    }).join('');
    keysBox.innerHTML = '<div class="ledger-wrap"><table class="ledger keys"><thead><tr><th>Name</th><th>Key</th><th>Access</th><th>Created</th><th>Last used</th><th>Status</th><th><span class="sr-only">Actions</span></th></tr></thead><tbody>' + rows + '</tbody></table></div>';
  }
  function loadKeys() {
    return api('GET', '/v1/agent-keys').then(function (data) { renderKeys(Array.isArray(data.items) ? data.items : []); })
      .catch(function (e) { keysBox.innerHTML = loadError(e.message, 'data-retry="keys"'); });
  }
  function showReveal(record) {
    el('reveal-key').textContent = record.key;
    el('reveal-cmd').textContent = 'claude mcp add --transport http meeting-notes ' + base() + '/mcp --header "Authorization: Bearer ' + record.key + '"';
    reveal.hidden = false;
    var heading = el('key-reveal-heading');
    heading.focus({preventScroll: true});
    reveal.scrollIntoView({block: 'nearest', behavior: 'smooth'});
  }
  function hideReveal() {
    reveal.hidden = true;
    el('reveal-key').textContent = '';
    el('reveal-cmd').textContent = '';
    el('key-name').focus();
  }
  reveal.querySelectorAll('[data-copy-target]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var target = el(btn.dataset.copyTarget);
      if (!target) return;
      copyText(target.textContent).then(function (ok) { notify(ok ? 'Copied.' : 'Could not copy. Select the text and copy it by hand.', ok ? '' : 'error'); });
    });
  });
  el('key-reveal-done').addEventListener('click', hideReveal);
  keysBox.addEventListener('click', function (event) {
    if (event.target.closest('[data-retry]')) { loadKeys(); return; }
    var button = event.target.closest('[data-revoke]');
    if (!button) return;
    var name = button.dataset.name;
    confirmDialog({title: 'Revoke "' + name + '"?', lead: 'Any agent using this key loses access immediately.',
                   warning: 'This cannot be undone.', confirmLabel: 'Revoke key', danger: true}).then(function (ok) {
      if (!ok) return;
      button.disabled = true;
      api('DELETE', '/v1/agent-keys/' + encodeURIComponent(button.dataset.revoke))
        .then(function () { notify('Key revoked.'); return loadKeys(); })
        .catch(function (e) { button.disabled = false; notify(e.message, 'error'); });
    });
  });
  el('key-form').addEventListener('submit', function (event) {
    event.preventDefault();
    var input = el('key-name'), error = el('key-error'), button = el('key-create'), name = input.value.trim();
    function fail(message) { error.textContent = message; error.hidden = false; input.setAttribute('aria-invalid', 'true'); input.focus(); }
    error.hidden = true; input.removeAttribute('aria-invalid');
    if (!name) { fail('Name the key so you can tell it apart later, for example "Claude Code on my laptop".'); return; }
    button.disabled = true; button.classList.add('is-busy');
    api('POST', '/v1/agent-keys', {name: name, scopes: el('key-write').checked ? ['read', 'write'] : ['read']})
      .then(function (record) {
        input.value = ''; el('key-write').checked = false;
        showReveal(record);
        return loadKeys();
      })
      .catch(function (e) { fail(e.message); })
      .finally(function () { button.disabled = false; button.classList.remove('is-busy'); });
  });
  el('key-name').addEventListener('input', function () { el('key-error').hidden = true; this.removeAttribute('aria-invalid'); });

  // ---- Client logs ----
  var logsBox = el('logs-box'), logsItems = [];
  function renderLogs(items) {
    logsItems = items;
    el('logs-delete-all').hidden = !items.length;
    if (!items.length) {
      logsBox.innerHTML = '<div class="ledger-empty"><h3>Nothing sent yet</h3><p>In the Meeting Notes app, open Logs and choose Send to server. The bundle appears here with a download link, ready to open when something misbehaves.</p></div>';
      return;
    }
    var rows = items.map(function (item) {
      var when = fmtDate(item.received_at, true);
      return '<tr>' +
        '<td class="l-device">' + escapeHtml(item.device) + '</td>' +
        '<td class="l-when" title="' + escapeHtml(fmtDate(item.received_at)) + '">' + escapeHtml(when) + '</td>' +
        '<td class="l-size">' + escapeHtml(fmtBytes(item.size)) + '</td>' +
        '<td class="l-act"><a class="btn secondary sm row-btn" href="' + escapeHtml(item.url) + '" download>' + icon('download', 16) + '<span>Download<span class="sr-only"> log bundle from ' + escapeHtml(item.device) + ', ' + escapeHtml(when) + '</span></span></a>' +
        ' <button type="button" class="btn secondary sm row-btn" data-log-delete="' + escapeHtml(item.url) + '" data-log-label="' + escapeHtml(item.device + ', ' + when) + '">' + icon('trash', 16) + '<span>Delete<span class="sr-only"> log bundle from ' + escapeHtml(item.device) + ', ' + escapeHtml(when) + '</span></span></button></td></tr>';
    }).join('');
    logsBox.innerHTML = '<div class="ledger-wrap"><table class="ledger logs"><thead><tr><th>Computer</th><th>Received</th><th>Size</th><th><span class="sr-only">Actions</span></th></tr></thead><tbody>' + rows + '</tbody></table></div>';
  }
  function loadLogs() {
    return api('GET', '/v1/client-logs').then(function (data) { renderLogs(Array.isArray(data.items) ? data.items : []); })
      .catch(function (e) { logsBox.innerHTML = loadError(e.message, 'data-retry="logs"'); });
  }
  logsBox.addEventListener('click', function (event) {
    if (event.target.closest('[data-retry]')) { loadLogs(); return; }
    var del = event.target.closest('[data-log-delete]');
    if (!del) return;
    confirmDialog({title: 'Delete this log bundle?', lead: 'The bundle from ' + del.dataset.logLabel + ' is removed from the server.',
                   note: 'The computer keeps its own logs and can send them again.', confirmLabel: 'Delete', danger: true}).then(function (ok) {
      if (!ok) return;
      del.disabled = true;
      api('DELETE', del.dataset.logDelete).then(function () { notify('Log bundle deleted.'); return loadLogs(); })
        .catch(function (e) { del.disabled = false; notify(e.message, 'error'); });
    });
  });
  el('logs-delete-all').addEventListener('click', function () {
    var button = this, n = logsItems.length;
    confirmDialog({title: 'Delete all ' + n + ' log bundle' + (n === 1 ? '' : 's') + '?', lead: 'Every client log bundle is removed from the server.',
                   note: 'Computers keep their own logs and can send them again.', confirmLabel: 'Delete all', danger: true}).then(function (ok) {
      if (!ok) return;
      button.disabled = true;
      api('DELETE', '/v1/client-logs').then(function (d) { notify('Deleted ' + d.deleted + ' log bundle' + (d.deleted === 1 ? '' : 's') + '.'); return loadLogs(); })
        .catch(function (e) { notify(e.message, 'error'); }).finally(function () { button.disabled = false; });
    });
  });
  el('logs-refresh').addEventListener('click', function () {
    var button = this;
    button.disabled = true;
    loadLogs().then(function () { notify('Client logs refreshed.'); }).finally(function () { button.disabled = false; });
  });

  // ---- Notion ----
  var nStatus = el('notion-status'), nError = el('notion-error'), nToken = el('notion-token');
  var nBtns = ['notion-connect', 'notion-test', 'notion-remove'].map(el);
  function nFail(message) { nError.textContent = message; nError.hidden = false; }
  function nBusy(on) { nBtns.forEach(function (b) { b.disabled = on; }); }
  function renderNotion(d) {
    nError.hidden = true;
    var env = d.source === 'env';
    if (!d.connected) nStatus.textContent = 'Not connected';
    else if (env) nStatus.textContent = 'Using NOTION_TOKEN from the server environment' + (d.workspace_name ? ' (' + d.workspace_name + ')' : '');
    else nStatus.textContent = 'Connected as ' + (d.bot_name || 'your integration') + (d.workspace_name ? ' (' + d.workspace_name + ')' : '');
    nToken.disabled = env; nToken.value = '';
    el('notion-connect').hidden = env;
    nToken.parentNode.hidden = env;
    el('notion-remove').hidden = env || !d.connected;
    el('notion-test').hidden = !d.connected;
    nBusy(false);
  }
  function loadNotion() {
    return api('GET', '/v1/notion').then(renderNotion).catch(function (e) { nStatus.textContent = 'Could not load Notion status.'; nFail(e.message); });
  }
  el('notion-connect').addEventListener('click', function () {
    var token = nToken.value.trim();
    if (!token) { nFail('Paste the integration token first.'); nToken.focus(); return; }
    nError.hidden = true; nBusy(true);
    api('PUT', '/v1/notion/token', {token: token})
      .then(function (d) { renderNotion(d); notify('Notion connected.'); })
      .catch(function (e) { nBusy(false); nFail(e.message); });
  });
  el('notion-test').addEventListener('click', function () {
    nError.hidden = true; nBusy(true);
    api('POST', '/v1/notion/test').then(function (d) {
      nBusy(false);
      if (d.ok) notify('Connected as ' + (d.name || 'your integration') + (d.workspace ? ' (' + d.workspace + ')' : '') + '.');
      else nFail(d.error || 'Notion rejected the connection.');
    }).catch(function (e) { nBusy(false); nFail(e.message); });
  });
  el('notion-remove').addEventListener('click', function () {
    confirmDialog({title: 'Remove the saved Notion token?', lead: 'Meeting notes stop being copied to Notion until you connect again.',
                   note: 'Pages already created stay in Notion.', confirmLabel: 'Remove token', danger: true}).then(function (ok) {
      if (!ok) return;
      nError.hidden = true; nBusy(true);
      api('DELETE', '/v1/notion/token').then(function (d) { renderNotion(d); notify('Notion token removed.'); })
        .catch(function (e) { nBusy(false); nFail(e.message); });
    });
  });
  nToken.addEventListener('input', function () { nError.hidden = true; });
  loadNotion();

  loadKeys();
  loadLogs();
})();
"""


_BEAM_SIZE_TIP = (
    "How many alternative wordings the model weighs before it picks one.\n"
    "\u2022 1: fastest, a few more mistakes.\n"
    "\u2022 5: more accurate on names, accents and mumbled speech, about 2-3x slower.\n"
    "\u2022 Above 5 rarely helps and costs a lot of CPU.\n"
    "Use 5 for the final transcript (3 or 1 if transcripts take too long) and 1 for the live "
    "transcript so it keeps up with both sides talking."
)


_GPU_HELPER_TIP = (
    "A GPU helper is a Windows PC with an NVIDIA graphics card that runs the Meeting Notes app and transcribes "
    "for this server. With this on, each new transcription is offered to a connected helper first; if none takes "
    "it in time, this server does it. A helper pauses while its own PC is recording. Off by default."
)
_DIARIZATION_WITHOUT_HELPER_CHOICES = (
    ("run_here", "Run on this server (slow)"),
    ("queue", "Queue for a GPU helper"),
    ("skip", "Skip"),
)
_ECHO_REMOVAL_CHOICES = (
    ("auto", "On: remove it when it is found"),
    ("off", "Off"),
)
_IN_PERSON_DIARIZATION_CHOICES = (
    ("wait_then_cpu", "Wait for a GPU helper, then use this server"),
    ("off", "Off"),
)


def _info_tip(tip_id: str, label: str, text: str) -> str:
    """A small "i" button that shows ``text`` on hover, keyboard focus or tap. The text is also the button's
    description for screen readers, so nothing is hidden from them."""
    return (
        f'<span class="info-tip"><button type="button" class="info-tip-btn" aria-label="About {html.escape(label)}"'
        f' aria-describedby="{tip_id}">i</button>'
        f'<span class="info-tip-text" role="tooltip" id="{tip_id}">{html.escape(text)}</span></span>'
    )


def _model_options(choices, selected: str) -> str:
    """<option>s for a model dropdown: the friendly label and hint for known ids, the id itself for any other."""
    return "".join(
        f'<option value="{html.escape(choice)}"{" selected" if choice == selected else ""}>'
        f'{html.escape(settings_mod.model_label(choice))}</option>'
        for choice in choices
    )


_EFFORT_LABELS = {
    "": "Default (the CLI's own)", "minimal": "Minimal", "low": "Low", "medium": "Medium", "high": "High",
    "xhigh": "Extra high", "max": "Max",
}
_EFFORT_HINT = ("Higher effort is slower and uses more of your plan, but follows the note instructions "
                "more carefully.")


def _effort_options(choices, selected: str) -> str:
    return "".join(
        f'<option value="{html.escape(choice)}"{" selected" if choice == selected else ""}>'
        f'{html.escape(_EFFORT_LABELS.get(choice, choice))}</option>'
        for choice in choices
    )


def render_settings_page(
    settings,
    *,
    token_configured: bool,
    message: Optional[str] = None,
    error: Optional[str] = None,
) -> str:
    options = _model_options(settings.model_choices(), settings.model)
    gpu_model_options = _model_options(settings.gpu_model_choices(), settings.gpu_model)
    live_options = _model_options(settings.live_model_choices(), settings.live_model)
    gpu_live_options = _model_options(settings.gpu_live_model_choices(), settings.gpu_live_model)
    live_on_helper_checked = " checked" if settings.live_on_helper else ""
    claude_effort_options = _effort_options(settings_mod.CLAUDE_EFFORT_CHOICES, settings.claude_effort)
    codex_effort_options = _effort_options(settings_mod.CODEX_EFFORT_CHOICES, settings.codex_effort)
    checked = "checked" if settings.delete_audio_only_after_success else ""
    ai_options = "".join(
        f'<option value="{choice}"{" selected" if choice == settings.ai_provider else ""}>{label}</option>'
        for choice, label in (
            ("disabled", "Disabled"), ("codex", "Codex / ChatGPT"),
            ("claude", "Claude (subscription)"), ("ollama", "Ollama (local)"),
        )
    )
    codex_model_options = '<option value="">Account default</option>'
    if settings.codex_model:
        escaped = html.escape(settings.codex_model)
        codex_model_options += f'<option value="{escaped}" selected>{escaped}</option>'
    claude_model_options = '<option value="">Account default</option>'
    if settings.claude_model:
        escaped_claude = html.escape(settings.claude_model)
        claude_model_options += f'<option value="{escaped_claude}" selected>{escaped_claude}</option>'
    gpu_offload_checked = " checked" if settings.compute_offload_enabled else ""
    never_local_checked = " checked" if settings.compute_never_local else ""
    nl_note_hidden = "" if settings.compute_never_local else " hidden"
    nl_warning_hidden = "" if settings.compute_never_local and not settings.compute_offload_enabled else " hidden"
    diarization_checked = " checked" if settings.diarization_enabled else ""
    voice_profiles_checked = " checked" if settings.voice_profiles_enabled else ""
    voice_profiles_note = (
        "" if settings.diarization_enabled else
        '<p class="help" id="voice-profiles-note">Voice profiles only work while Speaker labels is on. '
        "Turn Speaker labels on, then save.</p>"
    )
    voice_profiles_delete = (
        '<div class="inline-actions vp-actions"><button type="button" class="btn danger" id="voice-profiles-delete">'
        "Delete all voice profiles</button></div>"
        if settings.voice_profiles_enabled and settings.diarization_enabled else ""
    )
    voice_recheck_checked = " checked" if settings.voice_recheck_enabled else ""
    intro_naming_checked = " checked" if settings.intro_naming else ""
    voice_recheck_hour_options = "".join(
        f'<option value="{h}"{" selected" if h == settings.voice_recheck_hour else ""}>'
        f'{(h % 12) or 12}:00 {"AM" if h < 12 else "PM"}</option>'
        for h in range(24)
    )
    voice_recheck_now = (
        '<div class="inline-actions"><button type="button" class="btn secondary" id="voice-recheck-now">'
        'Recheck now</button></div><p class="help" id="voice-recheck-status" aria-live="polite"></p>'
        if settings.voice_profiles_enabled and settings.diarization_enabled else ""
    )
    without_helper_options = "".join(
        f'<option value="{value}"{" selected" if value == settings.diarization_without_helper else ""}>{html.escape(label)}</option>'
        for value, label in _DIARIZATION_WITHOUT_HELPER_CHOICES
    )
    echo_removal_options = "".join(
        f'<option value="{value}"{" selected" if value == settings.echo_removal else ""}>{html.escape(label)}</option>'
        for value, label in _ECHO_REMOVAL_CHOICES
    )
    in_person_options = "".join(
        f'<option value="{value}"{" selected" if value == settings.in_person_diarization else ""}>{html.escape(label)}</option>'
        for value, label in _IN_PERSON_DIARIZATION_CHOICES
    )
    escaped_ollama_model = html.escape(settings.ollama_model)
    ollama_model_options = (
        f'<option value="{escaped_ollama_model}" selected>{escaped_ollama_model}</option>'
        if settings.ollama_model else ""
    )
    message_html = (
        f'<div class="banner ok" role="status">{_icon("check")}<span>{html.escape(message)}</span></div>'
        if message else ""
    )
    error_html = (
        f'<div class="banner err" role="alert">{_icon("alert")}<span>{html.escape(error)}</span></div>'
        if error else ""
    )

    ingestion_chart = _ingestion_chart_html(settings)
    immediate_html = (
        _SETTINGS_IMMEDIATE_HTML.replace("__ICON_KEY__", _icon("key"))
        .replace("__ICON_COPY__", _icon("copy"))
        .replace("__ICON_RADIO__", _icon("radio"))
        .replace("__ICON_DOWNLOAD__", _icon("download"))
        .replace("__ICON_REFRESH__", _icon("refresh"))
        .replace("__ICON_TRASH__", _icon("trash"))
    )
    note_styles_html = _note_styles_html(settings)
    immediate_js = _SETTINGS_IMMEDIATE_JS.replace(
        "__SERVER_ADDRESS_JSON__", json.dumps(settings.server_address.strip().rstrip("/")).replace("</", "<\\/")
    )

    body = f"""
<div class="page-head"><h1>Settings</h1></div>
{_update_banner()}
{message_html}
{error_html}
<div class="settings-layout">
  <nav class="settings-nav" aria-label="Settings pages">{_SETTINGS_NAV_HTML}</nav>
  <div class="settings-main">
  <form method="post" action="/settings" class="settings-sheet" id="settings-form">
  <input type="hidden" name="revision" value="{settings.revision}">
  <section class="sect" data-page="general" aria-labelledby="settings-appearance-heading">
    <h2 id="settings-appearance-heading">Appearance</h2>
    <div class="sect-body">
    {_appearance_control("appearance", _normalize_appearance(settings.appearance))}
    <input type="hidden" name="appearance_rendered" value="{_normalize_appearance(settings.appearance)}">
    <p class="help">System follows your device's light or dark setting. The choice applies immediately and is
    saved for this server, so every browser shows the same theme.</p>
    </div>
  </section>
  <section class="sect" data-page="general" aria-labelledby="settings-index-heading">
    <h2 id="settings-index-heading">Search index</h2>
    <div class="sect-body">
    <p class="help">Rebuilds the session/transcript search index from what's actually on
    disk. Safe to run any time; only needed if the index looks stale or missing
    (e.g. after restoring the data volume from a backup).</p>
    <div class="inline-actions"><button type="button" class="btn secondary" id="reindex-btn">Rebuild index now</button>
    <span id="reindex-status" class="help" role="status" style="margin:0"></span></div>
    </div>
  </section>
  {ingestion_chart}
  <div class="ing-group-head" id="ing-audio" data-page="ingestion" data-group="audio" tabindex="-1">
    <span class="ing-num" aria-hidden="true">1</span>
    <div><h2 class="ing-title">Audio</h2><p class="help">What happens to a recording before it is transcribed, and how long the audio is kept.</p></div>
  </div>
  <section class="sect" data-page="ingestion" data-group="audio" aria-labelledby="settings-echo-heading">
    <h2 id="settings-echo-heading">Echo removal</h2>
    <div class="sect-body">
    <label class="field">
      <span class="name">Echo from your speakers</span>
      <select name="echo_removal" id="echo-removal">{echo_removal_options}</select>
    </label>
    <p class="help">Without a headset, your microphone also picks up the other side from your speakers, and their
    words show up twice in the transcript. With this on, that echo is taken out of your microphone track before it
    is transcribed, using the other side's own audio. Recordings made with a headset are left as they are.
    Applies to new transcriptions and to Retranscribe.</p>
    </div>
  </section>
  <section class="sect" data-page="ingestion" data-group="audio" aria-labelledby="settings-retention-heading">
    <h2 id="settings-retention-heading">Audio retention</h2>
    <div class="sect-body">
    <label class="field">
      <span class="name">Audio retention (days)</span>
      <input type="number" name="audio_retention_days" min="-1" value="{settings.audio_retention_days}">
    </label>
    <p class="help">Set 0 to delete audio immediately after transcription, a positive
    number to retain it for that many days, or -1 to keep it forever.</p>

    <label class="checkbox">
      <input type="hidden" name="delete_audio_only_after_success" value="0">
      <input type="checkbox" name="delete_audio_only_after_success" value="on" {checked}>
      <span>Only delete audio once a transcription has succeeded</span>
    </label>

    <label class="field" style="margin-top:14px">
      <span class="name">Retention check interval (minutes)</span>
      <input type="number" name="retention_check_interval_minutes" min="1"
             value="{settings.retention_check_interval_minutes}">
    </label>
    </div>
  </section>
  <div class="ing-group-head" id="ing-transcribe" data-page="ingestion" data-group="transcribe" tabindex="-1">
    <span class="ing-num" aria-hidden="true">2</span>
    <div><h2 class="ing-title">Transcribe</h2><p class="help">The speech models that turn the audio into text, on this server and on a GPU helper.</p></div>
  </div>
  <section class="sect" data-page="ingestion" data-group="transcribe" aria-labelledby="settings-transcription-heading">
    <h2 id="settings-transcription-heading">Transcript</h2>
    <div class="sect-body">
    <label class="field">
      <span class="name">Transcript model &mdash; this server</span>
      <select name="model">{options}</select>
    </label>
    <label class="field">
      <span class="name">Transcript model &mdash; GPU helper</span>
      <select name="gpu_model" id="gpu-model">{gpu_model_options}</select>
    </label>
    <p class="help">The speech model for the final transcription pass: this server uses the first, a GPU helper
    the second. Changing either takes effect on the next job, no restart needed. After changing the helper model,
    prepare the helper download again under GPU helpers.</p>
    <label class="field">
      <span class="name">Beam size {_info_tip("tip-beam", "beam size", _BEAM_SIZE_TIP)}</span>
      <input type="number" name="beam_size" min="1" value="{settings.beam_size}">
    </label>
    </div>
  </section>
  <section class="sect" data-page="ingestion" data-group="transcribe" aria-labelledby="settings-live-heading">
    <h2 id="settings-live-heading">Live transcription</h2>
    <div class="sect-body">
    <label class="field">
      <span class="name">Live model &mdash; this server</span>
      <select name="live_model">{live_options}</select>
    </label>
    <label class="field">
      <span class="name">Live model &mdash; GPU helper</span>
      <select name="gpu_live_model" id="gpu-live-model">{gpu_live_options}</select>
    </label>
    <label class="checkbox">
      <input type="hidden" name="live_on_helper" value="0">
      <input type="checkbox" name="live_on_helper" id="gpu-live-on-helper" value="on"{live_on_helper_checked}>
      <span>Send live transcription to GPU helpers</span>
    </label>
    <p class="help">With this on and a helper connected, the live transcript you follow during a meeting is made on
    the helper's graphics card; if it is slow or unavailable, this server does that sentence itself. It only
    applies while Use GPU helpers is on.</p>
    <label class="field">
      <span class="name">Live beam size {_info_tip("tip-live-beam", "live beam size", _BEAM_SIZE_TIP)}</span>
      <input type="number" name="live_beam_size" min="1" value="{settings.live_beam_size}">
    </label>
    <p class="help">The live transcript has to keep up with both sides talking, so a lighter model with beam size 1
    is the default; it is replaced by the final transcript after the meeting. A change applies from the next live
    sentence.</p>
    </div>
  </section>
  <div class="ing-group-head" id="ing-speakers" data-page="ingestion" data-group="speakers" tabindex="-1">
    <span class="ing-num" aria-hidden="true">3</span>
    <div><h2 class="ing-title">Identify speakers</h2><p class="help">Tell the voices apart in the transcript. Everything here is skipped while Speaker labels is off.</p></div>
  </div>
  <section class="sect" data-page="ingestion" data-group="speakers" aria-labelledby="settings-speaker-labels-heading">
    <h2 id="settings-speaker-labels-heading">Speaker labels</h2>
    <div class="sect-body">
    <label class="checkbox">
      <input type="hidden" name="diarization_enabled" value="0">
      <input type="checkbox" name="diarization_enabled" id="gpu-speakers" value="on"{diarization_checked}>
      <span>Speaker labels</span>
    </label>
    <p class="help">Tells the people on the other side of a call apart in the transcript. Helpers do this on the
    graphics card; this server can do it on its processor, which is slow.</p>
    <label class="field">
      <span class="name">Speaker label model</span>
      <input type="text" name="diarization_model" id="gpu-speaker-model" value="{html.escape(settings.diarization_model)}" autocomplete="off" spellcheck="false">
    </label>
    <div class="two-up">
      <label class="field">
        <span class="name">Fewest speakers</span>
        <input type="number" name="diarization_min_speakers" min="1" value="{settings.diarization_min_speakers}">
      </label>
      <label class="field">
        <span class="name">Most speakers</span>
        <input type="number" name="diarization_max_speakers" min="1" value="{settings.diarization_max_speakers}">
      </label>
    </div>
    <label class="field">
      <span class="name">Speaker labels without a helper</span>
      <select name="diarization_without_helper" id="gpu-without-helper">{without_helper_options}</select>
    </label>
    <p class="help">What to do when a transcription runs on this server instead of a helper. <strong>Queue for a
    GPU helper</strong> transcribes now without speaker labels, then adds the labels whenever a helper is free
    (it waits as long as it takes; nothing runs on this server's processor).</p>
    <p class="help nl-note"{nl_note_hidden}>Not used while "Never transcribe on this server" is on.</p>
    <h4 class="field-name">In-person meetings (phone recordings)</h4>
    <label class="field">
      <span class="name">Speaker labels</span>
      <select name="in_person_diarization" id="in-person-diarization">{in_person_options}</select>
    </label>
    <label class="field">
      <span class="name">Wait for a GPU helper (minutes)</span>
      <input type="number" name="in_person_helper_wait_minutes" id="in-person-wait" min="0" max="60" value="{settings.in_person_helper_wait_minutes}">
    </label>
    <p class="help">Meetings recorded on a phone have one voice track, so speakers are labelled "Speaker 1",
    "Speaker 2" and so on. The meeting waits this long for a helper (0 to 60 minutes) before this server labels
    speakers on its processor, in the background; notes start once the labels are done.</p>
    <p class="help nl-note"{nl_note_hidden}>Not used while "Never transcribe on this server" is on.</p>
    </div>
  </section>
  <div class="ing-group-head" id="ing-label" data-page="ingestion" data-group="label" tabindex="-1">
    <span class="ing-num" aria-hidden="true">4</span>
    <div><h2 class="ing-title">Label speakers</h2><p class="help">Recognise the same person across meetings. Needs Speaker labels.</p></div>
  </div>
  <section class="sect" data-page="ingestion" data-group="label" aria-labelledby="settings-voice-profiles-heading">
    <h2 id="settings-voice-profiles-heading">Voice profiles</h2>
    <div class="sect-body">
    <label class="checkbox">
      <input type="hidden" name="voice_profiles_enabled" value="0">
      <input type="checkbox" name="voice_profiles_enabled" id="voice-profiles" value="on"{voice_profiles_checked}>
      <span>Voice profiles</span>
    </label>
    <p class="help">Recognise the same person across meetings by voice; name them on the People page.</p>
    {voice_profiles_note}
    <label class="checkbox">
      <input type="hidden" name="voice_recheck_enabled" value="0">
      <input type="checkbox" name="voice_recheck_enabled" id="voice-recheck" value="on"{voice_recheck_checked}>
      <span>Recheck unnamed voices nightly</span>
    </label>
    <label class="field">
      <span class="name">Run at</span>
      <select name="voice_recheck_hour" id="voice-recheck-hour">{voice_recheck_hour_options}</select>
    </label>
    <label class="field">
      <span class="name">Look back (days)</span>
      <input type="number" name="voice_recheck_days" id="voice-recheck-days" min="1" max="365" value="{settings.voice_recheck_days}">
    </label>
    <p class="help">Once a night, voices in recent meetings that aren't linked to a named person are matched against
    the people again, so someone you name later is recognised in their earlier meetings too. Links you confirmed
    by hand are kept. Uses this server's time zone.</p>
    <label class="field">
      <span class="name">Archive people not heard for (days)</span>
      <input type="number" name="people_archive_days" id="people-archive-days" min="0" max="3650" value="{settings.people_archive_days}">
    </label>
    <p class="help">Named people not heard in a meeting for this long are hidden from the People list (Archived).
    They are still recognised, and come back when heard again. 0 = never.</p>
    <label class="field">
      <span class="name">Forget unnamed voices not heard for (days)</span>
      <input type="number" name="people_forget_days" id="people-forget-days" min="0" max="3650" value="{settings.people_forget_days}">
    </label>
    <p class="help">Unnamed voices not heard for this long are removed from People (the meetings keep their transcripts).
    Voices with a question waiting are kept. 0 = never.</p>
    {voice_recheck_now}
    {voice_profiles_delete}
    <p class="help">Names, spellings and voice profiles for each person are on the <a href="/people">People page</a>.</p>
    </div>
  </section>
  <div class="ing-group-head" id="ing-intro" data-page="ingestion" data-group="intro" tabindex="-1">
    <span class="ing-num" aria-hidden="true">5</span>
    <div><h2 class="ing-title">Name from introductions</h2><p class="help">Before the notes, so they are written with the names. Needs Voice profiles and a notes AI provider.</p></div>
  </div>
  <section class="sect" data-page="ingestion" data-group="intro" aria-labelledby="settings-intro-heading">
    <h2 id="settings-intro-heading">Name new voices</h2>
    <div class="sect-body">
    <label class="checkbox">
      <input type="hidden" name="intro_naming" value="0">
      <input type="checkbox" name="intro_naming" id="intro-naming" value="on"{intro_naming_checked}>
      <span>Name new voices from introductions</span>
    </label>
    <p class="help">Once the speakers are labelled, a separate AI pass looks for people saying their name ("Hi, I'm Dana
    from Acme") or being introduced, and names their voice. The meeting's notes wait for it, so they use those names;
    if it takes more than 20 minutes or fails, the notes go ahead without them. A name it is sure of is applied; one it
    is less sure of is offered on the person's page. You can undo any of them there.</p>
    </div>
  </section>
  <div class="ing-group-head" id="ing-notes" data-page="ingestion" data-group="notes" tabindex="-1">
    <span class="ing-num" aria-hidden="true">6</span>
    <div><h2 class="ing-title">Generate notes</h2><p class="help">Meeting notes are built last, once the transcript, its speaker labels and the names from introductions are done.</p></div>
  </div>
  <section class="sect" data-page="ingestion" data-group="notes" aria-labelledby="settings-ai-heading">
    <h2 id="settings-ai-heading">Meeting notes AI</h2>
    <div class="sect-body">
    <label class="field">
      <span class="name">Provider</span>
      <select name="ai_provider" id="ai-provider">{ai_options}</select>
    </label>
    <p class="help">While a provider is selected, notes are built automatically for new meetings
    whose note type is set to generate notes automatically (see Note types; Disabled turns meeting notes off). Re-transcribing an existing meeting
    never builds notes on its own. Codex / ChatGPT uses the server-side bridge login. Claude uses the bridge's
    Claude Code CLI signed in with your Claude Pro/Max subscription.</p>
    <div id="ai-login-check" class="subsect" style="display:none">
      <div class="row">
        <div><strong>Login check</strong><div class="help" id="ai-login-check-status" role="status">Not checked yet.</div></div>
        <div class="inline-actions"><button type="button" class="btn secondary" id="ai-login-check-now">Check now</button></div>
      </div>
      <p class="help">The server sends the provider a tiny request every six hours (every five minutes while the
      login is broken) to make sure its login still works. If it has expired, notes wait in the queue, every page
      says so, and they continue on their own once you reconnect.</p>
    </div>
    <div id="codex-settings" class="subsect">
      <div class="row">
        <div><strong>ChatGPT connection</strong><div class="help" id="codex-auth-status" role="status">Checking bridge…</div></div>
        <div class="inline-actions"><button type="button" class="btn secondary" id="codex-connect">Connect ChatGPT</button><button type="button" class="btn danger" id="codex-disconnect" style="display:none">Disconnect</button></div>
      </div>
      <div id="codex-device-login" style="display:none">
        <p>Open <a id="codex-login-url" href="https://auth.openai.com/codex/device" target="_blank" rel="noopener">OpenAI device sign-in</a> and enter this one-time code:</p>
        <p><code id="codex-device-code" style="font-size:20px;user-select:all"></code></p>
      </div>
      <label class="field">
        <span class="name">ChatGPT model</span>
        <select name="codex_model" id="codex-model">{codex_model_options}</select>
      </label>
      <label class="field">
        <span class="name">Effort</span>
        <select name="codex_effort" id="codex-effort">{codex_effort_options}</select>
      </label>
      <p class="help">{_EFFORT_HINT}</p>
      <div class="help" id="codex-model-status" role="status">Connect ChatGPT to load available models.</div>
    </div>
    <div id="claude-settings" class="subsect">
      <div class="row">
        <div><strong>Claude connection</strong><div class="help" id="claude-auth-status" role="status">Checking bridge…</div></div>
        <div class="inline-actions"><button type="button" class="btn secondary" id="claude-connect">Connect Claude</button><button type="button" class="btn danger" id="claude-disconnect" style="display:none">Disconnect</button></div>
      </div>
      <div id="claude-login-panel" style="display:none">
        <p>Open <a id="claude-login-url" href="#" target="_blank" rel="noopener">the Claude sign-in link</a>,
        approve access there, then paste the code shown back here.</p>
        <label class="field">
          <span class="name">Authorization code</span>
          <input type="text" id="claude-login-code-input" autocomplete="off">
        </label>
        <p><button type="button" class="btn secondary" id="claude-code-submit">Submit code</button></p>
      </div>
      <label class="field">
        <span class="name">Claude model</span>
        <select name="claude_model" id="claude-model">{claude_model_options}</select>
      </label>
      <label class="field">
        <span class="name">Effort</span>
        <select name="claude_effort" id="claude-effort">{claude_effort_options}</select>
      </label>
      <p class="help">{_EFFORT_HINT}</p>
      <div class="help" id="claude-model-status" role="status">Connect Claude to load available models.</div>
    </div>
    <div id="ollama-settings" class="subsect">
      <label class="field">
        <span class="name">Ollama base URL</span>
        <input type="url" name="ollama_base_url" value="{html.escape(settings.ollama_base_url)}"
               placeholder="http://ollama:11434">
      </label>
      <label class="field">
        <span class="name">Ollama model</span>
        <select name="ollama_model" id="ollama-model">{ollama_model_options}</select>
      </label>
      <div class="inline-actions"><button type="button" class="btn secondary" id="ollama-model-refresh">Load available models</button>
      <span class="help" id="ollama-model-status" role="status" style="margin:0"></span></div>
      <p class="help" style="margin-top:12px">The Ollama service must be reachable from the server or bridge container.</p>
    </div>

    </div>
  </section>
  <section class="sect" data-page="ingestion" data-group="notes" aria-labelledby="settings-notetypes-heading">
    <h2 id="settings-notetypes-heading">Note types</h2>
    <div class="sect-body">
    {note_styles_html}
    </div>
  </section>
  <section class="sect" data-page="gpu" aria-labelledby="gpu-helpers-heading">
    <h2 id="gpu-helpers-heading">GPU helpers</h2>
    <div class="sect-body">
    <label class="checkbox">
      <input type="hidden" name="compute_offload_enabled" value="0">
      <input type="checkbox" name="compute_offload_enabled" id="gpu-offload" value="on"{gpu_offload_checked}>
      <span>Use GPU helpers {_info_tip("tip-gpu-helpers", "GPU helpers", _GPU_HELPER_TIP)}</span>
    </label>
    <label class="field">
      <span class="name">Wait for a helper (seconds)</span>
      <input type="number" name="compute_claim_timeout_seconds" id="gpu-claim-timeout" min="5" value="{settings.compute_claim_timeout_seconds}">
    </label>
    <p class="help">How long a new transcription waits when no helper is online before this server does it instead.
    While a helper is online, even busy, meetings queue for it (up to 15 minutes, or this wait if it is longer).
    At least 5 seconds, no upper limit.</p>
    <p class="help nl-note"{nl_note_hidden}>Not used while "Never transcribe on this server" is on.</p>
    <label class="checkbox">
      <input type="hidden" name="compute_never_local" value="0">
      <input type="checkbox" name="compute_never_local" id="gpu-never-local" value="on"{never_local_checked}>
      <span>Never transcribe on this server - always wait for a GPU helper</span>
    </label>
    <p class="help">Every transcription, speaker-label job and Find speakers request waits for a GPU helper with no time limit and is never run on this server's processor, even when no helper is online, a helper fails, or the server restarts. Meeting notes are not affected.</p>
    <div class="banner warn" id="gpu-nl-warning" role="alert"{nl_warning_hidden}>{_icon("alert")}<span>Never run on this server is on, but Use GPU helpers is off: nothing will be transcribed until you turn GPU helpers on.</span></div>
    <script>
    (function () {{
      var never = document.getElementById("gpu-never-local"), offload = document.getElementById("gpu-offload");
      function sync() {{
        Array.prototype.forEach.call(document.querySelectorAll(".nl-note"), function (el) {{ el.hidden = !never.checked; }});
        document.getElementById("gpu-nl-warning").hidden = !(never.checked && !offload.checked);
      }}
      never.addEventListener("change", sync); offload.addEventListener("change", sync);
    }})();
    </script>
    <div class="subsect gpu-pack" id="gpu-pack">
      <h4 class="subsect-title">Helper download</h4>
      <p class="help">The files helpers install from this server: the GPU runtime and the speech models. Prepare it
      once, and again whenever you change a GPU helper model.</p>
      <p class="gpu-pack-status" id="gpu-pack-status" role="status" aria-live="polite">Loading...</p>
      <div class="progress-track" id="gpu-pack-progress" hidden><i style="transform:scaleX(.05)"></i></div>
      <p class="help" id="gpu-pack-note" role="status" hidden></p>
      <p class="error-text" id="gpu-pack-error" role="alert" hidden></p>
      <div class="inline-actions"><button type="button" class="btn secondary" id="gpu-pack-btn">{_icon("download")}<span>Prepare download</span></button></div>
    </div>
    <h4 class="field-name">Connected helpers</h4>
    <div id="gpu-helpers-box" aria-live="polite"><p class="help" role="status">Loading helpers...</p></div>
    </div>
  </section>
  <section class="sect" data-page="recorders" style="order:1" aria-labelledby="settings-install-heading">
    <h2 id="settings-install-heading">Server and client installation</h2>
    <div class="sect-body">
    <div class="inline-actions"><a class="btn primary" href="/install">{_icon("download")}<span>Install guide</span></a>
    <span class="help" style="margin:0">Windows and macOS installers, update, uninstall and first-run steps.</span></div>
    <label class="field">
      <span class="name">Server address</span>
      <input type="text" name="server_address"
             placeholder="http://meeting-server.local:8000"
             value="{html.escape(settings.server_address)}">
    </label>
    <p class="help">The LAN address embedded into the client-agent installer.
    Leave blank to use the address in the browser when the installer is downloaded.</p>
    </div>
  </section>
  <div class="save-bar" id="settings-save-bar" style="order:99">
    <button type="submit" class="btn primary">Save settings</button><span class="help">Changes take effect after saving.</span>
  </div>
  </form>
  {immediate_html}
  </div>
</div>
{_CONFIRM_DIALOG_HTML}
<script>
{_INGESTION_JS}
</script>
<script>
{_JS_HELPERS}
{immediate_js}
</script>
<script>
{_GPU_HELPERS_JS}
{_GPU_SETTINGS_JS}
</script>
<script>
(function () {{
  var btn = document.getElementById("voice-recheck-now"), out = document.getElementById("voice-recheck-status");
  if (!btn || !out) return;
  function show(st) {{ out.textContent = voiceRecheckLine(st); }}
  voiceRecheckStatus().then(function (st) {{ if (st) show(st); }});
  btn.addEventListener("click", function () {{
    btn.disabled = true;
    runVoiceRecheck().then(function (r) {{
      notify("Voice recheck done: " + voiceRecheckSummary(r) + ".");
      return voiceRecheckStatus().then(function (st) {{ if (st) show(st); }});
    }}).catch(function (e) {{ notify(e.message, "error"); }}).then(function () {{ btn.disabled = false; }});
  }});
}})();
(function () {{
  var del = document.getElementById("voice-profiles-delete");
  if (!del) return;
  del.addEventListener("click", function () {{
    confirmDialog({{title: "Delete all voice profiles?", lead: "Every person, voiceprint and voice sample is removed. Meetings keep their plain speaker labels. This cannot be undone.",
                   confirmLabel: "Delete all", danger: true}}).then(function (ok) {{
      if (!ok) return;
      fetch("/v1/people", {{method: "DELETE", credentials: "same-origin"}}).then(function (r) {{
        if (!r.ok) throw new Error("Could not delete the voice profiles");
        notify("Voice profiles deleted.");
      }}).catch(function (e) {{ notify(e.message, "error"); }});
    }});
  }});
}})();
</script>
<script>
{_NOTE_STYLES_JS}
</script>
<script>
document.getElementById("reindex-btn").addEventListener("click", function () {{
  var status = document.getElementById("reindex-status");
  status.textContent = "Rebuilding...";
  fetch("/v1/reindex", {{ method: "POST", credentials: "same-origin" }})
    .then(function (r) {{ return r.json(); }})
    .then(function (data) {{ status.textContent = "Indexed " + data.reindexed + " session(s)."; }})
    .catch(function () {{ status.textContent = "Rebuild failed."; }});
}});
function updateAiFields() {{
  var provider = document.getElementById("ai-provider").value;
  document.getElementById("ollama-settings").style.display = provider === "ollama" ? "block" : "none";
  document.getElementById("codex-settings").style.display = provider === "codex" ? "block" : "none";
  document.getElementById("claude-settings").style.display = provider === "claude" ? "block" : "none";
  if (provider === "codex") refreshCodexStatus();
  if (provider === "claude") refreshClaudeStatus();
  refreshLoginCheck();
}}
function loginCheckAgo(seconds) {{
  var s = Math.max(0, Math.round(Date.now() / 1000 - seconds));
  if (s < 90) return "just now";
  if (s < 5400) return Math.round(s / 60) + " minutes ago";
  if (s < 129600) return Math.round(s / 3600) + " hours ago";
  return Math.round(s / 86400) + " days ago";
}}
function renderLoginCheck(data) {{
  var box = document.getElementById("ai-login-check"), status = document.getElementById("ai-login-check-status");
  var provider = document.getElementById("ai-provider").value;
  // Only for a saved provider with a login (the check runs against what is saved, not an unsaved choice).
  box.style.display = (data && data.applies && data.provider === provider) ? "block" : "none";
  if (!data || !data.applies) return;
  var when = data.checked_at ? " Last checked " + loginCheckAgo(data.checked_at) + "." : "";
  if (data.expired) {{
    status.textContent = "The login has expired: notes are on hold until you reconnect." + when;
  }} else if (data.last_result === "ok") {{
    status.textContent = "Working." + when;
  }} else if (data.last_result === "error") {{
    status.textContent = "The last check could not reach the provider (the login may still be fine)." + when;
  }} else {{
    status.textContent = "Not checked yet.";
  }}
}}
function refreshLoginCheck() {{
  fetch("/v1/ai/login", {{credentials:"same-origin"}}).then(function(r) {{ return r.ok ? r.json() : null; }}).then(renderLoginCheck).catch(function() {{}});
}}
document.getElementById("ai-login-check-now").addEventListener("click", function() {{
  var button = this, status = document.getElementById("ai-login-check-status");
  button.disabled = true;
  status.textContent = "Checking… (this can take up to a minute)";
  fetch("/v1/ai/login/check", {{method:"POST",credentials:"same-origin"}}).then(function(r) {{ return r.json().then(function(d) {{ if(!r.ok) throw new Error(d.detail||"Check failed"); return d; }}); }}).then(function(data) {{
    renderLoginCheck(data);
    if (data.answer === "unavailable") status.textContent = "The AI bridge did not answer, so the login could not be checked.";
    var provider = document.getElementById("ai-provider").value;
    if (provider === "codex") refreshCodexStatus();
    if (provider === "claude") refreshClaudeStatus();
  }}).catch(function(e) {{ status.textContent = e.message; }}).finally(function() {{ button.disabled = false; }});
}});
var modelLoads = {{codex:false, ollama:false, claude:false}}, codexModelsLoaded = false, claudeModelsLoaded = false;
function loadProviderModels(provider) {{
  if (modelLoads[provider]) return;
  modelLoads[provider] = true;
  var select = document.getElementById(provider === "codex" ? "codex-model" : provider === "claude" ? "claude-model" : "ollama-model");
  var status = document.getElementById(provider === "codex" ? "codex-model-status" : provider === "claude" ? "claude-model-status" : "ollama-model-status");
  var selected = select.value, url = "/v1/ai/models?provider=" + encodeURIComponent(provider);
  if (provider === "ollama") {{
    url += "&ollama_base_url=" + encodeURIComponent(document.querySelector('[name="ollama_base_url"]').value);
  }}
  status.textContent = "Loading available models…";
  fetch(url, {{credentials:"same-origin"}}).then(function(r) {{
    return r.json().then(function(d) {{ if(!r.ok) throw new Error(d.detail || "Unable to load models"); return d; }});
  }}).then(function(data) {{
    var models = Array.isArray(data.models) ? data.models : [];
    select.replaceChildren();
    if (provider === "codex" || provider === "claude") select.add(new Option("Account default", ""));
    models.forEach(function(model) {{
      var value = String(model.id || ""), label = String(model.name || value);
      if (value) select.add(new Option(label, value));
    }});
    if (selected && !Array.from(select.options).some(function(option) {{ return option.value === selected; }})) {{
      select.add(new Option(selected, selected));
    }}
    select.value = selected;
    if (provider === "codex") {{
      codexModelsLoaded = true;
      if (codexPoll) {{ clearInterval(codexPoll); codexPoll = null; }}
    }} else if (provider === "claude") {{
      claudeModelsLoaded = true;
      if (claudePoll) {{ clearInterval(claudePoll); claudePoll = null; }}
    }}
    status.textContent = models.length ? models.length + " model(s) available." : "No models reported by provider.";
  }}).catch(function(error) {{ status.textContent = error.message; }}).finally(function() {{ modelLoads[provider] = false; }});
}}
var codexPoll = null, codexWasConnected = false;
function renderCodexStatus(data) {{
  var state = data.state || "unavailable", connected = !!data.authenticated;
  if (!connected && codexWasConnected) codexModelsLoaded = false;
  codexWasConnected = connected;
  document.getElementById("codex-auth-status").textContent = connected ? "Connected to ChatGPT" : state.replace(/_/g, " ");
  document.getElementById("codex-connect").style.display = connected ? "none" : "";
  document.getElementById("codex-disconnect").style.display = connected ? "" : "none";
  var login = document.getElementById("codex-device-login"), code = data.device_code || "";
  login.style.display = code ? "block" : "none";
  document.getElementById("codex-device-code").textContent = code;
  var url = String(data.login_url || "");
  if (url.indexOf("https://auth.openai.com/") === 0) document.getElementById("codex-login-url").href = url;
  if (connected && !codexModelsLoaded) loadProviderModels("codex");
  if (connected && codexModelsLoaded && codexPoll) {{ clearInterval(codexPoll); codexPoll = null; }}
}}
function refreshCodexStatus() {{
  if (document.getElementById("ai-provider").value !== "codex") return;
  fetch("/v1/bridge/control/status", {{credentials:"same-origin"}}).then(function(r) {{ if(!r.ok) throw new Error("Bridge unavailable"); return r.json(); }}).then(renderCodexStatus).catch(function(e) {{ document.getElementById("codex-auth-status").textContent = e.message; }});
}}
document.getElementById("codex-connect").addEventListener("click", function() {{
  document.getElementById("codex-auth-status").textContent = "Starting secure device sign-in…";
  fetch("/v1/bridge/control/login", {{method:"POST",credentials:"same-origin"}}).then(function(r) {{ return r.json().then(function(d) {{ if(!r.ok) throw new Error(d.detail||"Unable to start login"); return d; }}); }}).then(function(data) {{ renderCodexStatus(data); if(!codexPoll) codexPoll=setInterval(refreshCodexStatus,1500); }}).catch(function(e) {{ document.getElementById("codex-auth-status").textContent=e.message; }});
}});
document.getElementById("codex-disconnect").addEventListener("click", function() {{
  fetch("/v1/bridge/control/logout", {{method:"POST",credentials:"same-origin"}}).then(function(r) {{ return r.json().then(function(d) {{ if(!r.ok) throw new Error(d.detail||"Unable to disconnect"); return d; }}); }}).then(renderCodexStatus).catch(function(e) {{ document.getElementById("codex-auth-status").textContent=e.message; }});
}});
var claudePoll = null, claudeWasConnected = false;
function renderClaudeStatus(data) {{
  var state = data.state || "unavailable", connected = !!data.authenticated;
  if (!connected && claudeWasConnected) claudeModelsLoaded = false;
  claudeWasConnected = connected;
  document.getElementById("claude-auth-status").textContent = connected ? "Connected to Claude" : state.replace(/_/g, " ");
  document.getElementById("claude-connect").style.display = connected ? "none" : "";
  document.getElementById("claude-disconnect").style.display = connected ? "" : "none";
  var url = String(data.login_url || "");
  var panel = document.getElementById("claude-login-panel");
  panel.style.display = (!connected && url && state === "awaiting_user") ? "block" : "none";
  if (url.indexOf("https://claude.com/") === 0 || url.indexOf("https://claude.ai/") === 0 || url.indexOf("https://platform.claude.com/") === 0) {{
    document.getElementById("claude-login-url").href = url;
  }}
  if (connected && !claudeModelsLoaded) loadProviderModels("claude");
  if (connected && claudeModelsLoaded && claudePoll) {{ clearInterval(claudePoll); claudePoll = null; }}
}}
function refreshClaudeStatus() {{
  if (document.getElementById("ai-provider").value !== "claude") return;
  fetch("/v1/bridge/control/status", {{credentials:"same-origin"}}).then(function(r) {{ if(!r.ok) throw new Error("Bridge unavailable"); return r.json(); }}).then(renderClaudeStatus).catch(function(e) {{ document.getElementById("claude-auth-status").textContent = e.message; }});
}}
document.getElementById("claude-connect").addEventListener("click", function() {{
  document.getElementById("claude-auth-status").textContent = "Starting sign-in…";
  fetch("/v1/bridge/control/login", {{method:"POST",credentials:"same-origin"}}).then(function(r) {{ return r.json().then(function(d) {{ if(!r.ok) throw new Error(d.detail||"Unable to start login"); return d; }}); }}).then(function(data) {{ renderClaudeStatus(data); if(!claudePoll) claudePoll=setInterval(refreshClaudeStatus,1500); }}).catch(function(e) {{ document.getElementById("claude-auth-status").textContent=e.message; }});
}});
document.getElementById("claude-disconnect").addEventListener("click", function() {{
  fetch("/v1/bridge/control/logout", {{method:"POST",credentials:"same-origin"}}).then(function(r) {{ return r.json().then(function(d) {{ if(!r.ok) throw new Error(d.detail||"Unable to disconnect"); return d; }}); }}).then(renderClaudeStatus).catch(function(e) {{ document.getElementById("claude-auth-status").textContent=e.message; }});
}});
document.getElementById("claude-code-submit").addEventListener("click", function() {{
  var input = document.getElementById("claude-login-code-input"), code = input.value;
  document.getElementById("claude-auth-status").textContent = "Submitting code…";
  fetch("/v1/bridge/control/login/code", {{method:"POST",credentials:"same-origin",headers:{{"Content-Type":"application/json"}},body:JSON.stringify({{code:code}})}}).then(function(r) {{ return r.json().then(function(d) {{ if(!r.ok) throw new Error(d.detail||"Unable to submit code"); return d; }}); }}).then(function(data) {{ input.value = ""; renderClaudeStatus(data); }}).catch(function(e) {{ document.getElementById("claude-auth-status").textContent=e.message; }});
}});
document.getElementById("ollama-model-refresh").addEventListener("click", function() {{ loadProviderModels("ollama"); }});
document.getElementById("ai-provider").addEventListener("change", updateAiFields);
updateAiFields();
(function () {{
  var links = Array.from(document.querySelectorAll(".settings-nav a[data-page]"));
  var panels = Array.from(document.querySelectorAll(".settings-main [data-page]"));
  var pages = links.map(function (a) {{ return a.dataset.page; }});
  var KEY = "mn-settings-page";
  var ALIASES = {json.dumps(_SETTINGS_PAGE_ALIASES)};
  function target(hash) {{
    var id = ""; try {{ id = decodeURIComponent(String(hash || "").replace(/^#/, "")); }} catch (e) {{ id = ""; }}
    if (!id) return null;
    if (ALIASES[id]) id = ALIASES[id];
    if (id.indexOf("/") > 0) {{
      // "ingestion/speakers": a page and one of its groups
      var parts = id.split("/"), head = document.getElementById("ing-" + parts[1]);
      if (parts[0] === "ingestion" && head) return {{page: "ingestion", el: head, group: parts[1]}};
      id = parts[0];
    }}
    if (pages.indexOf(id) >= 0) return {{page: id, el: null}};
    var el = document.getElementById(id), panel = el && el.closest(".settings-main [data-page]");
    return panel ? {{page: panel.dataset.page, el: el}} : null;
  }}
  function show(page) {{
    panels.forEach(function (p) {{ p.hidden = p.dataset.page !== page; }});
    links.forEach(function (a) {{
      if (a.dataset.page === page) a.setAttribute("aria-current", "true"); else a.removeAttribute("aria-current");
    }});
    var bar = document.getElementById("settings-save-bar");
    if (bar) bar.hidden = page === "access";  // nothing on that page is saved with the form
    try {{ localStorage.setItem(KEY, page); }} catch (e) {{}}
  }}
  var hotTimer = null;
  function highlight(group) {{
    Array.prototype.forEach.call(document.querySelectorAll(".ing-hot"), function (n) {{ n.classList.remove("ing-hot"); }});
    if (!group) return;
    Array.prototype.forEach.call(document.querySelectorAll('.settings-main [data-group="' + group + '"]'), function (n) {{ n.classList.add("ing-hot"); }});
    clearTimeout(hotTimer);
    hotTimer = setTimeout(function () {{ highlight(null); }}, 2600);
  }}
  function open(hash, scroll) {{
    var t = target(hash);
    if (!t) return false;
    show(t.page);
    if (t.el && scroll) {{
      t.el.scrollIntoView();
      if (t.group) {{ try {{ t.el.focus({{preventScroll: true}}); }} catch (e) {{}} highlight(t.group); }}
    }}
    return true;
  }}
  Array.prototype.forEach.call(document.querySelectorAll(".ing-step"), function (a) {{
    a.addEventListener("click", function (event) {{
      event.preventDefault();
      var hash = "#ingestion/" + a.dataset.group;
      try {{ history.replaceState(null, "", hash); }} catch (e) {{}}
      open(hash, true);
    }});
  }});
  links.forEach(function (a) {{
    a.addEventListener("click", function (event) {{
      event.preventDefault();
      show(a.dataset.page);
      try {{ history.replaceState(null, "", "#" + a.dataset.page); }} catch (e) {{}}
      window.scrollTo(0, 0);
    }});
  }});
  window.addEventListener("hashchange", function () {{ open(location.hash, true); }});
  var remembered = ""; try {{ remembered = localStorage.getItem(KEY) || ""; }} catch (e) {{}}
  if (ALIASES[remembered]) remembered = ALIASES[remembered].split("/")[0];
  if (!open(location.hash, true)) show(pages.indexOf(remembered) >= 0 ? remembered : pages[0]);
}})();
</script>
"""
    return _shell(
        "Settings",
        body,
        token_configured=token_configured,
        active="settings",
        main_class="settings-page",
        appearance=settings.appearance,
        people_nav=bool(settings.voice_profiles_enabled and settings.diarization_enabled),
    )
