"""AI provider control routes, installed by ``create_app``."""

from __future__ import annotations

import os
from typing import Optional
from urllib.parse import urlparse

import httpx
from fastapi import Depends, FastAPI, HTTPException
from starlette.concurrency import run_in_threadpool

from . import ai_health, auth, settings as settings_mod


def bridge_verifier(store):
    def _bridge_verify(provider: str) -> Optional[dict]:
        """Ask the bridge for one tiny real request (its ``/v1/bridge/control/verify``); ``None`` if it can't say."""
        base_url = os.environ.get(
            "MEETING_NOTES_BRIDGE_CONTROL_URL", "http://meeting-notes-bridge:8765"
        ).rstrip("/")
        token = os.environ.get("MEETING_NOTES_TOKEN") or ""
        if not token:
            return None
        current = settings_mod.load_settings(store.root)
        try:
            response = httpx.post(
                base_url + "/v1/bridge/control/verify",
                json={"provider": provider, "codex_model": current.codex_model},
                headers={"Authorization": f"Bearer {token}"},
                timeout=150.0,  # the bridge allows the CLI two minutes
            )
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None  # e.g. an older bridge without the endpoint: no answer, nothing changes
        try:
            body = response.json()
        except ValueError:
            return None
        return body if isinstance(body, dict) else None
    return _bridge_verify


def install_ai(app: FastAPI, *, store, login_checker) -> None:
    """AI provider controls: bridge control proxy, AI login check, model list."""

    async def bridge_control_request(method: str, path: str, payload: Optional[dict] = None):
        """Proxy bridge login controls without exposing its port to the LAN."""
        base_url = os.environ.get(
            "MEETING_NOTES_BRIDGE_CONTROL_URL", "http://meeting-notes-bridge:8765"
        ).rstrip("/")
        token = os.environ.get("MEETING_NOTES_TOKEN") or ""
        if not token:
            raise HTTPException(status_code=503, detail="bridge control requires a server token")
        try:
            # Provider discovery may include a cold Codex app-server start or
            # a 10-second Ollama request; keep the outer proxy comfortably
            # beyond either bridge-side deadline.
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.request(
                    method,
                    base_url + path,
                    json=payload,
                    headers={"Authorization": f"Bearer {token}"},
                )
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=503, detail="AI bridge is unavailable") from exc
        try:
            body = response.json()
        except ValueError:
            body = {"detail": "AI bridge returned an invalid response"}
        if response.status_code >= 400:
            raise HTTPException(
                status_code=502 if response.status_code >= 500 else response.status_code,
                detail=str(body.get("detail") or "AI bridge request failed"),
            )
        return body

    # The bridge control listener exists only on the private Compose network.
    # These authenticated proxy routes are the sole browser-facing path to it.
    @app.get("/v1/bridge/control/status")
    async def bridge_control_status_api(_auth: None = Depends(auth.require_token)):
        provider = settings_mod.load_settings(store.root).ai_provider
        if provider == "disabled":
            return {"provider": provider, "state": "disabled", "authenticated": False}
        if provider == "ollama":
            return {"provider": provider, "state": "configured", "authenticated": True}
        status = await bridge_control_request(
            "GET", f"/v1/bridge/control/status?provider={provider}"
        )
        login = await run_in_threadpool(ai_health.state, store.root, provider)
        if login["expired"] and isinstance(status, dict):
            finished = status.get("finished_at")
            if status.get("authenticated") and isinstance(finished, (int, float)) and finished > (login["since"] or 0):
                # A login finished in Settings after the failure: try it for real before releasing the queue.
                answer = await run_in_threadpool(login_checker.check_now)
                login = await run_in_threadpool(ai_health.state, store.root, provider)
                if answer is None:
                    await run_in_threadpool(ai_health.mark_ok, store.root, provider)
                    login = await run_in_threadpool(ai_health.state, store.root, provider)
            if login["expired"]:
                # "auth status" still reads as logged in after an expired session; say what is really true.
                status = {**status, "authenticated": False, "state": "login_expired"}
        return status

    @app.get("/v1/ai/login")
    async def ai_login_api(_auth: None = Depends(auth.require_token)):
        """The AI login check's last answer (Settings -> AI)."""
        provider = settings_mod.load_settings(store.root).ai_provider
        return await run_in_threadpool(ai_health.as_public, store.root, provider)

    @app.post("/v1/ai/login/check")
    async def ai_login_check_api(_auth: None = Depends(auth.require_token)):
        """Check now: one tiny real request through the provider (Settings -> AI's "Check now")."""
        provider = settings_mod.load_settings(store.root).ai_provider
        answer = await run_in_threadpool(login_checker.check_now)
        public = await run_in_threadpool(ai_health.as_public, store.root, provider)
        public["answer"] = (answer or {}).get("result") or "unavailable"
        return public

    @app.post("/v1/bridge/control/login")
    async def bridge_control_login_api(_auth: None = Depends(auth.require_token)):
        provider = settings_mod.load_settings(store.root).ai_provider
        if provider not in ("codex", "claude"):
            raise HTTPException(
                status_code=409, detail="Select Codex / ChatGPT or Claude before connecting"
            )
        return await bridge_control_request(
            "POST", "/v1/bridge/control/login", {"provider": provider}
        )

    @app.post("/v1/bridge/control/login/code")
    async def bridge_control_login_code_api(
        body: dict, _auth: None = Depends(auth.require_token)
    ):
        provider = settings_mod.load_settings(store.root).ai_provider
        if provider != "claude":
            raise HTTPException(
                status_code=409, detail="Select Claude before submitting a login code"
            )
        code = body.get("code")
        if not isinstance(code, str) or not code.strip():
            raise HTTPException(status_code=400, detail="code must be a non-empty string")
        return await bridge_control_request(
            "POST", "/v1/bridge/control/login/code", {"provider": provider, "code": code}
        )

    @app.post("/v1/bridge/control/logout")
    async def bridge_control_logout_api(_auth: None = Depends(auth.require_token)):
        provider = settings_mod.load_settings(store.root).ai_provider
        if provider not in ("codex", "claude"):
            raise HTTPException(
                status_code=409, detail="Select Codex / ChatGPT or Claude before disconnecting"
            )
        return await bridge_control_request(
            "POST", "/v1/bridge/control/logout", {"provider": provider}
        )

    @app.get("/v1/ai/models")
    async def ai_models_api(
        provider: str,
        ollama_base_url: Optional[str] = None,
        _auth: None = Depends(auth.require_token),
    ):
        provider = provider.strip().lower()
        if provider not in ("codex", "claude", "ollama"):
            raise HTTPException(status_code=400, detail="provider must be codex, claude, or ollama")
        payload = {"provider": provider}
        if provider == "ollama":
            base_url = (ollama_base_url or "").strip().rstrip("/")
            parsed = urlparse(base_url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise HTTPException(
                    status_code=400,
                    detail="ollama_base_url must be a valid http:// or https:// URL",
                )
            payload["ollama_base_url"] = base_url
        return await bridge_control_request(
            "POST", "/v1/bridge/control/models", payload
        )
