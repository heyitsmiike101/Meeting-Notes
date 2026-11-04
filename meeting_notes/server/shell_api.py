"""Static assets, sign-in/sign-out and the home page, installed by ``create_app``."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlencode, urlparse

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from . import auth, settings as settings_mod, web
from .route_helpers import _safe_next, appearance_reader


_STATIC_FONT_DIR = Path(__file__).resolve().parent / "static" / "fonts"

_STATIC_FONT_NAME = re.compile(r"^[a-z0-9-]+\.(woff2|txt)$", re.IGNORECASE)

_STATIC_ASSETS = {
    "app.css": "text/css; charset=utf-8",
    "icons.js": "text/javascript; charset=utf-8",
    "splitmerge.css": "text/css; charset=utf-8",
}

LOGIN_COOKIE_MAX_AGE = 180 * 24 * 3600


def install_shell(app: FastAPI, *, store) -> None:
    """Static assets, sign-in/sign-out and the home page."""

    _appearance = appearance_reader(store)
    # -- web UI: self-hosted fonts (public: nothing secret, and the sign-in
    # page needs them before anyone is authenticated) ----------------------

    @app.get("/static/fonts/{name}")
    async def static_font(name: str):
        if not _STATIC_FONT_NAME.match(name):
            raise HTTPException(status_code=404, detail="not found")
        path = _STATIC_FONT_DIR / name
        if not path.is_file():
            raise HTTPException(status_code=404, detail="not found")
        return FileResponse(
            path,
            media_type="font/woff2" if name.endswith(".woff2") else "text/plain",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    @app.get("/static/{name}")
    async def static_asset(name: str):
        media_type = _STATIC_ASSETS.get(name)
        path = _STATIC_FONT_DIR.parent / name
        if media_type is None or not path.is_file():
            raise HTTPException(status_code=404, detail="not found")
        return FileResponse(
            path,
            media_type=media_type,
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        if not auth.token_is_configured():
            # Nothing to sign in for -- matches the API's own "no token
            # means open" behaviour.
            return RedirectResponse(url="/", status_code=303)
        return web.render_login_page(
            error=bool(request.query_params.get("error")), appearance=_appearance(),
            next_path=_safe_next(request.query_params.get("next") or ""),
        )

    @app.post("/login")
    async def login_submit(request: Request, token: str = Form(...), next: str = Form("")):  # noqa: A002 - field name
        target = _safe_next(next)
        if not auth.token_is_valid(token):
            query = {"error": "1", **({"next": target} if target not in ("", "/") else {})}
            return RedirectResponse(url="/login?" + urlencode(query), status_code=303)
        response = RedirectResponse(url=target or "/", status_code=303)
        # httponly: invisible to page JS (nothing it does needs to read the
        # token back). samesite=lax: not sent on a cross-site request, but
        # still attached to same-origin fetch() calls the web UI's own pages
        # make against /v1/... -- see auth.require_token's cookie fallback.
        # secure: only when the configured server_address is https (trusted settings, never a request header).
        # Only when signing in on the https address itself: a Secure cookie set over plain http (http://meeting.lan
        # next to https://meeting.example.com) is dropped by the browser and sign-in would loop.
        configured = urlparse((settings_mod.load_settings(store.root).server_address or "").strip())
        secure = configured.scheme == "https" and (request.headers.get("host") or "").lower() == configured.netloc.lower()
        # Kept for 180 days so the phone recorder (and the web UI) stay signed in between visits; a session cookie is
        # dropped whenever iOS closes the Home Screen app.
        response.set_cookie(auth.WEB_TOKEN_COOKIE, token, httponly=True, samesite="lax", secure=secure,
                            max_age=LOGIN_COOKIE_MAX_AGE)
        return response

    @app.post("/logout")
    async def logout():
        response = RedirectResponse(
            url="/login" if auth.token_is_configured() else "/", status_code=303
        )
        response.delete_cookie(auth.WEB_TOKEN_COOKIE)
        return response

    # -- web UI: sessions -------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def home_page(_auth: None = Depends(auth.require_web_token)):
        return web.render_home_page(
            token_configured=auth.token_is_configured(), appearance=_appearance()
        )
