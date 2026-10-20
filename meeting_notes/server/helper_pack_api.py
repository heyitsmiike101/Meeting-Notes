"""HTTP API for the GPU helper pack: manifest, resumable file downloads, prepare and status.

Wired in by ``app.py`` with ``make_router(store_root, builder)``. Manifest and file routes accept a helper key
or the main token (``compute.require_helper_key_or_token``); prepare and status take the main/web token.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from typing import Iterator, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from .auth import require_token
from .helper_pack import HelperPackBuilder

_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
_CHUNK = 1024 * 1024


async def helper_auth(request: Request) -> None:
    """Helper key or main token. Resolved at call time so this module imports without ``compute``.

    ``require_helper_key_or_token`` is a FastAPI dependency; call it with the request (and the Authorization
    header for any parameter named ``authorization``) and await the result if it is a coroutine.
    """
    from . import compute

    dep = compute.require_helper_key_or_token
    kwargs = {}
    for name in inspect.signature(dep).parameters:
        if name == "request":
            kwargs[name] = request
        elif name == "authorization":
            kwargs[name] = request.headers.get("authorization")
    result = dep(**kwargs)
    if inspect.isawaitable(result):
        await result


def parse_range(header: str, size: int) -> Optional[Tuple[int, int]]:
    """``(start, end)`` inclusive for a single byte range, or None if unsatisfiable. Raises ValueError if malformed."""
    m = _RANGE_RE.match(header.strip())
    if not m or (not m.group(1) and not m.group(2)):
        raise ValueError("bad range")
    if not m.group(1):  # suffix: last N bytes
        n = int(m.group(2))
        if n == 0:
            return None
        return max(0, size - n), size - 1
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else size - 1
    if start >= size or end < start:
        return None
    return start, min(end, size - 1)


def _stream(path: Path, start: int, end: int) -> Iterator[bytes]:
    with open(path, "rb") as fh:
        fh.seek(start)
        left = end - start + 1
        while left > 0:
            block = fh.read(min(_CHUNK, left))
            if not block:
                break
            left -= len(block)
            yield block


def make_router(store_root: Path, builder: HelperPackBuilder) -> APIRouter:  # noqa: ARG001 - store_root kept for symmetry
    router = APIRouter()

    @router.get("/v1/compute/pack/manifest", dependencies=[Depends(helper_auth)])
    async def pack_manifest():
        manifest = builder.manifest()
        if manifest is None:
            return JSONResponse({"error": "no pack"}, status_code=404)
        return manifest

    @router.get("/v1/compute/pack/files/{path:path}", dependencies=[Depends(helper_auth)])
    async def pack_file(path: str, request: Request, version: Optional[str] = None):
        if version and builder.version_dir(version) is None:
            # The helper started from a manifest of a pack the builder has since pruned: it must start over.
            raise HTTPException(
                status_code=410,
                detail=f"pack version {version} is no longer available; fetch the manifest again",
            )
        target = builder.file_path(path, version)
        if target is None:
            raise HTTPException(status_code=404, detail="no such pack file")
        size = target.stat().st_size
        header = request.headers.get("range")
        if not header:
            return FileResponse(target, media_type="application/octet-stream", headers={"Accept-Ranges": "bytes"})
        try:
            rng = parse_range(header, size)
        except ValueError:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        if rng is None:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        start, end = rng
        return StreamingResponse(
            _stream(target, start, end),
            status_code=206,
            media_type="application/octet-stream",
            headers={
                "Content-Range": f"bytes {start}-{end}/{size}",
                "Content-Length": str(end - start + 1),
                "Accept-Ranges": "bytes",
            },
        )

    @router.post("/v1/compute/pack/prepare", dependencies=[Depends(require_token)])
    async def pack_prepare():
        started = builder.start()
        return JSONResponse({"started": started, **builder.status()}, status_code=202 if started else 200)

    @router.get("/v1/compute/pack/status", dependencies=[Depends(require_token)])
    async def pack_status():
        return builder.status()

    return router
