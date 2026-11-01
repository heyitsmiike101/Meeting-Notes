"""HTTP surface for voice profiles ("People"); the logic lives in ``people.py``.

Every route needs the admin token (``auth.require_token``, like the other ``/v1`` routes) and answers 404 while the
feature is off (``voice_profiles_enabled`` with ``diarization_enabled``). ``install_people`` is one call from
``create_app``.
"""

from __future__ import annotations

import json
from typing import Callable, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from . import auth
from . import people as people_mod
from . import store as store_mod

SORTS = ("talking", "recent", "alpha")


def install_people(app: FastAPI, *, store, after_change: Optional[Callable[[], object]] = None) -> None:
    """``after_change``: called (never awaited, errors swallowed) after a change that can make other meetings'
    voices match differently: a person named or renamed, merged, combined, or a voice assigned or confirmed.
    ``create_app`` passes the voice recheck worker's non-blocking ``request_run``."""

    def changed() -> None:
        if after_change is None:
            return
        try:
            after_change()
        except Exception:  # noqa: BLE001 - the edit already happened; the nightly recheck is the backstop
            pass
    def require_enabled() -> None:
        if not people_mod.is_enabled(store.root):
            raise HTTPException(status_code=404, detail="Not Found")

    guard = [Depends(auth.require_token), Depends(require_enabled)]

    async def run(fn, *args, **kwargs):
        """Run blocking work off the event loop, relaying the module's errors as HTTP errors."""
        try:
            return await run_in_threadpool(lambda: fn(*args, **kwargs))
        except people_mod.PersonNotFound as exc:
            raise HTTPException(status_code=404, detail=f"unknown person or meeting: {exc}") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    async def body_object(request: Request) -> dict:
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="invalid JSON body") from exc
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="request body must be an object")
        return body

    def text_field(body: dict, key: str) -> str:
        value = body.get(key)
        if not isinstance(value, str):
            raise HTTPException(status_code=400, detail=f"{key} must be a string")
        return value

    def check_id(value: str, what: str = "id") -> None:
        if not store_mod.is_safe_id(value):
            raise HTTPException(status_code=400, detail=f"invalid {what}: {value!r}")

    @app.get("/v1/people", dependencies=guard)
    async def list_people_api(sort: str = "talking", q: Optional[str] = None):
        if sort not in SORTS:
            raise HTTPException(status_code=400, detail=f"sort must be one of {', '.join(SORTS)}")
        return {"people": await run(people_mod.list_people, store, sort, q)}

    @app.post("/v1/people", dependencies=guard)
    async def add_person_api(request: Request):
        """Add a person by name (no voice yet): ``{name, company?}``. 400 for an empty or already used name."""
        body = await body_object(request)
        name = text_field(body, "name")
        company = body.get("company", "")
        if not isinstance(company, str):
            raise HTTPException(status_code=400, detail="company must be a string")
        person = await run(people_mod.add_person, store.root, name, company)
        return await run(people_mod.get_person, store, person["id"])

    @app.get("/v1/people/companies", dependencies=guard)
    async def companies_api():
        return {"items": await run(people_mod.list_companies, store.root)}

    @app.get("/v1/people/lookup", dependencies=guard)
    async def lookup_api(names: list[str] = Query(default=[])):
        """Which of these names are a person's name or "also heard as" spelling: ``{items: {name: {person_id,
        display, via}}}``. Names matching nobody are left out."""
        return {"items": await run(people_mod.lookup, store.root, names[:200])}

    @app.post("/v1/people/combine", dependencies=guard)
    async def combine_api(request: Request):
        """Combine people into one: ``{ids: [...], into: id}``. 400 for fewer than two, ``into`` not in ids, or the
        "You" profile with someone else kept; 404 for an unknown id."""
        body = await body_object(request)
        ids = body.get("ids")
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise HTTPException(status_code=400, detail="ids must be a list of person ids")
        into = text_field(body, "into")
        for value in [*ids, into]:
            check_id(value, "person id")
        await run(people_mod.combine_people, store.root, ids, into)
        changed()
        return await run(people_mod.get_person, store, into)

    @app.post("/v1/people/appearances", dependencies=guard)
    async def appearance_api(request: Request):
        body = await body_object(request)
        session_id, label, action = (text_field(body, k) for k in ("session_id", "label", "action"))
        check_id(session_id, "session_id")
        person_id = body.get("person_id")
        if person_id is not None and not isinstance(person_id, str):
            raise HTTPException(status_code=400, detail="person_id must be a string")
        result = await run(people_mod.appearance_action, store.root, session_id, label, action, person_id)
        if action in ("confirm", "assign"):
            changed()
        return result

    @app.delete("/v1/people", dependencies=guard)
    async def delete_all_api():
        await run(people_mod.delete_all, store.root)
        return {"ok": True}

    @app.get("/v1/people/{person_id}", dependencies=guard)
    async def get_person_api(person_id: str):
        check_id(person_id, "person id")
        return await run(people_mod.get_person, store, person_id)

    @app.patch("/v1/people/{person_id}", dependencies=guard)
    async def update_person_api(person_id: str, request: Request):
        check_id(person_id, "person id")
        body = await body_object(request)
        fields = {key: body[key] for key in ("name", "email", "company") if key in body}
        for key, value in fields.items():
            if not isinstance(value, str):
                raise HTTPException(status_code=400, detail=f"{key} must be a string")
        await run(people_mod.update_person, store.root, person_id, fields)
        if "name" in fields:
            changed()
        return await run(people_mod.get_person, store, person_id)

    @app.post("/v1/people/{person_id}/merge", dependencies=guard)
    async def merge_person_api(person_id: str, request: Request):
        check_id(person_id, "person id")
        body = await body_object(request)
        into = text_field(body, "into")
        check_id(into, "person id")
        await run(people_mod.merge_people, store.root, person_id, into)
        changed()
        return await run(people_mod.get_person, store, into)

    @app.post("/v1/people/{person_id}/aliases", dependencies=guard)
    async def add_alias_api(person_id: str, request: Request):
        """Add an "also heard as" spelling: ``{wrong}``."""
        check_id(person_id, "person id")
        body = await body_object(request)
        return await run(people_mod.add_alias, store.root, person_id, text_field(body, "wrong"))

    @app.delete("/v1/people/{person_id}/aliases", dependencies=guard)
    async def remove_alias_api(person_id: str, wrong: str):
        check_id(person_id, "person id")
        return await run(people_mod.remove_alias, store.root, person_id, wrong)

    @app.delete("/v1/people/{person_id}", dependencies=guard)
    async def delete_person_api(person_id: str):
        check_id(person_id, "person id")
        await run(people_mod.delete_person, store.root, person_id)
        return {"ok": True}

    @app.get("/v1/people/{person_id}/clips/{session_id}/{name}", dependencies=guard)
    async def clip_api(person_id: str, session_id: str, name: str):
        check_id(person_id, "person id")
        check_id(session_id, "session_id")
        path = await run(people_mod.clip_path, store, person_id, session_id, name)
        if path is None:
            raise HTTPException(status_code=404, detail="Not Found")
        return FileResponse(path, media_type="audio/wav")
