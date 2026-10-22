"""The phone recorder page: ``/record/`` and its PWA files (``docs/mobile-recorder-design.md``).

Only the HTML page needs a sign-in (a signed-out phone is sent to ``/login``). The manifest, service worker, script,
stylesheet and icons hold nothing private and are public like ``/static/app.css``, which also lets the browser
fetch the manifest and the worker without cookies. The page script (``static/record/app.js``) talks to the
``/v1/mobile/sessions`` API with the sign-in cookie.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from . import auth
from . import settings as settings_mod
from . import web

_DIR = Path(__file__).resolve().parent / "static" / "record"
_ICONS = {"icon-192.png": 192, "icon-512.png": 512}

PAGE_PATH = "/record/"
ACCENT = "#3b5bdb"


def _digest(*names: str) -> str:
    h = hashlib.sha1()
    for name in names:
        try:
            h.update((_DIR / name).read_bytes())
        except OSError:
            h.update(name.encode())
    return h.hexdigest()[:10]


JS_HREF = f"/record/app.js?v={_digest('app.js')}"
CSS_HREF = f"/record/app.css?v={_digest('app.css')}"


def shell_urls() -> list:
    """What the service worker keeps for offline use: the page, its script and styles, the shared styles, icons and font."""
    return [
        PAGE_PATH, JS_HREF, CSS_HREF, web._CSS_HREF, web._ICONS_SRC, "/static/fonts/inter-latin-wght-normal.woff2",
        "/record/manifest.webmanifest", "/record/icon-192.png", "/record/icon-512.png",
    ]


def manifest() -> dict:
    return {
        "name": "Meeting Notes recorder",
        "short_name": "Record",
        "description": "Record an in-person meeting on this phone.",
        "start_url": PAGE_PATH,
        "scope": PAGE_PATH,
        "id": PAGE_PATH,
        "display": "standalone",
        "orientation": "portrait",
        "background_color": web._THEME_COLORS["light"],
        "theme_color": ACCENT,
        "icons": [
            {"src": "/record/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/record/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
    }


def service_worker_source() -> str:
    """sw.js with its version (a hash of every shell file, so any change makes browsers fetch a new worker) filled in."""
    source = (_DIR / "sw.js").read_text(encoding="utf-8")
    version = _digest("app.js", "app.css", "sw.js", "icon-192.png", "icon-512.png") + web._CSS_HREF.rsplit("=", 1)[-1]
    return source.replace("__VERSION__", version).replace("__SHELL__", json.dumps(shell_urls()))


def _body() -> str:
    mic = web._icon("mic", 40)
    return f"""
<div class="mr" id="mr-app">
  <section class="mr-desktop" id="mr-desktop" aria-labelledby="mr-desktop-h" hidden>
    <h1 id="mr-desktop-h">Use the desktop recorder</h1>
    <p>This page records from a phone. On a computer, use the Meeting Notes desktop recorder: it also captures the other side of online calls.</p>
    <p><a class="btn primary" href="/install">Install the desktop recorder</a></p>
  </section>
  <section class="mr" id="mr-recorder" hidden>
    <header class="mr-head">
      <div class="mr-brand"><span class="mark">{web._icon("mark", 16)}</span><h1>Record a meeting</h1></div>
    </header>
    <div class="banner update mr-update" id="mr-update" role="status" hidden>
      <span>Update available.</span><button type="button" class="btn secondary" id="mr-update-reload">Reload</button>
    </div>
    <div class="banner mr-tip" id="mr-tip" role="note" hidden>
      <span id="mr-tip-text"></span><button type="button" class="btn secondary" id="mr-tip-dismiss">Got it</button>
    </div>
    <div id="mr-warnings"></div>
    <label class="field"><span class="name">Meeting name <span class="optional">(optional)</span></span>
      <input type="text" id="mr-name" maxlength="120" autocomplete="off" enterkeyhint="done" placeholder="Name this meeting"></label>
    <label class="field"><span class="name">People <span class="optional">(optional)</span></span>
      <input type="number" id="mr-people" min="1" max="50" step="1" inputmode="numeric" placeholder="Auto"
        title="How many people are in this meeting, including you. Helps tell speakers apart."></label>
    <label class="field" id="mr-type-field" hidden><span class="name">Note type</span><select id="mr-type"></select></label>
    <label class="mr-switch" for="mr-live"><input type="checkbox" id="mr-live" role="switch">
      <span class="sw-text"><span class="sw-title">Live transcript</span>
      <span class="sw-help">Shows the words here and on the Meetings page while you record. The full transcript is made after you stop.</span></span></label>
    <div class="mr-device-row" id="mr-device-row"><span>Recording from <strong id="mr-device"></strong></span>
      <button type="button" class="btn link" id="mr-device-edit">Change name</button>
      <form id="mr-device-form" hidden><input type="text" id="mr-device-input" maxlength="40" aria-label="Phone name" autocomplete="off">
        <button type="submit" class="btn primary">Save</button></form></div>
    <div class="mr-stage" id="mr-stage">
      <div class="mr-clock" id="mr-clock" role="timer" hidden>00:00:00</div>
      <div class="mr-meter" id="mr-meter" role="img" aria-label="Microphone level" hidden><i id="mr-meter-bar"></i></div>
      <button type="button" class="mr-rec" id="mr-record" aria-label="Record">{mic}<span>Record</span></button>
      <button type="button" class="btn mr-stop" id="mr-stop" hidden>Stop</button>
      <button type="button" class="btn primary mr-resume" id="mr-resume" hidden>Resume recording</button>
    </div>
    <p class="mr-status" id="mr-status" role="status" aria-live="polite" hidden></p>
    <p class="mr-storage" id="mr-storage"></p>
    <section class="mr-live-box" id="mr-live-box" aria-labelledby="mr-live-h" hidden>
      <h2 id="mr-live-h">Live transcript</h2><p class="mr-live-note" id="mr-live-note" role="status"></p>
      <div class="mr-live-text" id="mr-live-text" aria-live="polite"></div>
    </section>
    <section id="mr-pending" aria-labelledby="mr-pending-h" hidden>
      <h2 id="mr-pending-h">On this phone</h2>
      <ul class="mr-list" id="mr-list"></ul>
    </section>
  </section>
</div>
{web._CONFIRM_DIALOG_HTML}
<script>{web._JS_HELPERS}</script>
<script src="{JS_HREF}" defer></script>"""


def render_record_page(*, token_configured: bool, appearance: str = "system") -> str:
    page = web._shell(
        "Record", _body(), token_configured=token_configured, active="", main_class="mr-main", nav=False, appearance=appearance
    )
    head = (
        '<link rel="manifest" href="/record/manifest.webmanifest">\n'
        '<link rel="apple-touch-icon" href="/record/icon-192.png">\n'
        '<meta name="mobile-web-app-capable" content="yes">\n'
        '<meta name="apple-mobile-web-app-capable" content="yes">\n'
        '<meta name="apple-mobile-web-app-title" content="Record">\n'
        '<meta name="apple-mobile-web-app-status-bar-style" content="default">\n'
        f'<link rel="stylesheet" href="{CSS_HREF}">\n'
    )
    return page.replace("</head>", head + "</head>", 1)


def install_mobile_web(app: FastAPI, *, store) -> None:
    """Register the ``/record`` routes on ``app`` (``store`` supplies the data root for the appearance setting)."""

    @app.get("/record")
    async def record_redirect(_auth: None = Depends(auth.require_web_token)):
        return RedirectResponse(url=PAGE_PATH, status_code=307)

    @app.get(PAGE_PATH, response_class=HTMLResponse)
    async def record_page(_auth: None = Depends(auth.require_web_token)):
        appearance = settings_mod.load_settings(store.root).appearance
        return HTMLResponse(
            render_record_page(token_configured=auth.token_is_configured(), appearance=appearance),
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/record/manifest.webmanifest")
    async def record_manifest():
        return Response(
            json.dumps(manifest()), media_type="application/manifest+json", headers={"Cache-Control": "no-cache"}
        )

    @app.get("/record/sw.js")
    async def record_service_worker():
        return Response(
            service_worker_source(), media_type="text/javascript", headers={"Cache-Control": "no-cache"}
        )

    @app.get("/record/app.js")
    async def record_js():
        return FileResponse(
            _DIR / "app.js", media_type="text/javascript", headers={"Cache-Control": "public, max-age=31536000, immutable"}
        )

    @app.get("/record/app.css")
    async def record_css():
        return FileResponse(
            _DIR / "app.css", media_type="text/css", headers={"Cache-Control": "public, max-age=31536000, immutable"}
        )

    @app.get("/record/{name}")
    async def record_icon(name: str):
        if name not in _ICONS or not (_DIR / name).is_file():
            raise HTTPException(status_code=404, detail="not found")
        return FileResponse(_DIR / name, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})
