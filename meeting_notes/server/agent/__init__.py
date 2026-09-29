"""Agent access: REST under ``/api/v1``, MCP at ``/mcp``, per-agent API keys.

One integration point for ``create_app``::

    from .agent import install_agent_access
    agent_lifespan = install_agent_access(app, store=store, base_url_getter=...)
    # ... and inside the app lifespan:  async with agent_lifespan: yield

Everything else (keys, service, spec tables, router, MCP server) is internal.
The MCP half needs the optional ``mcp`` package; without it the REST routes
still work and ``/mcp`` is simply not mounted.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Callable, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import JSONResponse

from .errors import AgentError
from .keys import AgentKeyStore
from .rest import agent_error_response, build_router
from .service import AgentService

logger = logging.getLogger("meeting_notes.server.agent")

__all__ = ["install_agent_access", "AgentKeyStore", "AgentService", "AgentError"]


class _ReusableLifespan:
    """An async context manager that can be entered again after it exits.

    The app lifespan may run more than once per process (tests re-enter it),
    and the MCP session manager can only ``run()`` once per instance, so each
    entry builds a fresh one underneath.
    """

    def __init__(self, factory: Optional[Callable[[], contextlib.AbstractAsyncContextManager]]):
        self._factory = factory
        self._stack: list = []

    async def __aenter__(self):
        if self._factory is None:
            return None
        cm = self._factory()
        await cm.__aenter__()
        self._stack.append(cm)
        return None

    async def __aexit__(self, *exc_info):
        if self._factory is None or not self._stack:
            return False
        return await self._stack.pop().__aexit__(*exc_info)


class _BareMcpPath:
    """Serve ``/mcp`` (no trailing slash) directly.

    A Starlette ``Mount("/mcp")`` only matches ``/mcp/...``; redirecting POSTs
    is unreliable for MCP clients, so the bare path is forwarded in place.
    """

    def __init__(self, asgi):
        self.asgi = asgi

    async def __call__(self, scope, receive, send):
        # Starlette derives the sub-app's route path as "path minus root_path";
        # for the bare path that is "" (no match), so present it as "/mcp/".
        scope = dict(
            scope,
            root_path=scope.get("root_path", "") + "/mcp",
            path=scope["path"].rstrip("/") + "/",
            raw_path=scope["path"].rstrip("/").encode("utf-8") + b"/",
        )
        await self.asgi(scope, receive, send)


def install_agent_access(
    app: FastAPI,
    *,
    store,
    base_url_getter: Optional[Callable[[], str]] = None,
    enable_mcp: bool = True,
) -> contextlib.AbstractAsyncContextManager:
    """Wire the agent REST routes and (if ``mcp`` is installed) ``/mcp`` onto ``app``.

    ``store`` is the server ``Store`` (its data root holds ``agent_keys.json``
    and its search index is reused). ``base_url_getter`` optionally returns the
    server's public address (e.g. the ``server_address`` setting) for the
    manifest/llms.txt; if it returns nothing, the request's own base URL is used.

    Returns an async context manager that MUST be entered inside the app
    lifespan (it runs the MCP session manager). Safe to enter repeatedly.
    """
    keys = AgentKeyStore(store.root)
    service = AgentService(store)
    app.state.agent_keys = keys
    app.state.agent_service = service

    host = None
    if enable_mcp:
        try:
            from .mcp_server import McpHost, build_mcp

            host = McpHost(build_mcp(service, keys, base_url_getter=lambda: (base_url_getter() if base_url_getter else "") or ""))
        except ImportError as exc:
            logger.warning(
                "the 'mcp' package is not installed (%s): agent REST API is available "
                "but /mcp is NOT mounted. Install with: pip install 'mcp>=2.2,<3'", exc
            )

    def request_base_url(request: Request) -> str:
        configured = (base_url_getter() if base_url_getter else "") or ""
        return configured.rstrip("/") or str(request.base_url).rstrip("/")

    app.include_router(
        build_router(service, keys, base_url_getter=request_base_url, mcp_enabled=host is not None)
    )
    app.add_exception_handler(AgentError, agent_error_response)

    async def validation_handler(request: Request, exc: RequestValidationError):
        if request.url.path.startswith("/api/v1/"):
            return JSONResponse(
                status_code=422,
                content={
                    "detail": "invalid request parameters",
                    "code": "validation_error",
                    "errors": [
                        {"loc": list(e.get("loc", [])), "msg": e.get("msg", "")} for e in exc.errors()
                    ],
                },
            )
        return await request_validation_exception_handler(request, exc)

    app.add_exception_handler(RequestValidationError, validation_handler)

    if host is not None:
        app.add_route("/mcp", _BareMcpPath(host.asgi), include_in_schema=False)
        app.mount("/mcp", host.asgi)
        app.state.agent_mcp = host

    return _ReusableLifespan(host.lifespan if host is not None else None)
