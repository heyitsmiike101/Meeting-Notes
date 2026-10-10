"""Copy finished meeting notes into Notion.

How it fits together
--------------------
* **Connection.** A Notion internal-integration token: ``NOTION_TOKEN`` from the
  environment wins, otherwise one entered in Settings is kept in
  ``<data>/notion/token`` (mode 0600), outside ``settings.json`` so it can never
  be returned by ``GET /v1/settings`` or the agent API. It is write-only from the
  UI's point of view.
* **Destination.** Each note type may have a parent page
  (``Settings.notion_parents``). Under it there is one child page per month,
  titled ``"<Month>-<YYYY> <note type name>"``. Each meeting is one *toggleable
  Heading 1* block on that page holding the notes.
* **Newest first.** Notion's "append block children" takes a ``position``
  (API version ``2026-03-11``): ``{"type": "start"}`` or
  ``{"type": "after_block", ...}``. The exporter keeps, per month page, each
  meeting's toggle block id and start time (``<data>/notion/state.json``) and
  inserts a meeting directly *after the next-newer meeting's toggle*, or at
  ``start`` when it is the newest. Order therefore follows meeting start time
  no matter in which order notes finish.
* **Jobs.** Exports run on a single background thread. A job is a small JSON
  file under ``<data>/notion/jobs`` that exists while it is active; a restart
  re-queues whatever was queued or running (``resume_interrupted``). Transient
  failures (network, 429, 5xx) retry with exponential backoff; anything else
  fails at once with a readable reason that the meeting view shows.

The notes pipeline never waits for any of this: a listener on
``Store.complete_review`` only writes a job file.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import httpx

from . import notion_blocks as nb
from . import settings as settings_mod
from . import store as store_mod
from .notion_api import NotConnected, NotionClient, NotionError, dashed, is_trashed

logger = logging.getLogger("meeting_notes.server.notion")

MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September",
          "October", "November", "December")
MONTH_ABBR = tuple(m[:3] for m in MONTHS)
MAX_ATTEMPTS = 5
BACKOFF_BASE = 30.0
BACKOFF_CAP = 900.0


def _backoff(attempt: int) -> float:
    return min(BACKOFF_CAP, BACKOFF_BASE * (2 ** max(attempt - 1, 0)))


# -- time -----------------------------------------------------------------------


def local_tz():
    """The server's configured zone: the ``TZ`` environment variable when it names a
    zone the system knows, else the machine's local zone (``None``)."""
    name = os.environ.get("TZ", "").strip()
    if name:
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(name.lstrip(":"))
        except Exception:  # noqa: BLE001 - unknown zone / no tzdata: fall back to local time
            logger.warning("TZ=%r is not a known time zone; using the system local time", name)
    return None


def local_dt(ts: float, tz=None) -> datetime:
    return datetime.fromtimestamp(ts, tz) if tz is not None else datetime.fromtimestamp(ts).astimezone()


def month_title(dt: datetime, style_name: str) -> str:
    return f"{MONTHS[dt.month - 1]}-{dt.year} {style_name}"


def heading_title(name: str, dt: datetime, *, with_time: bool = False) -> str:
    name = " ".join(str(name or "").split()) or "Untitled meeting"
    day = f"{MONTH_ABBR[dt.month - 1]} {dt.day}"
    return f"{day} · {dt:%H:%M} · {name}" if with_time else f"{day} · {name}"


# -- persistence -------------------------------------------------------------------


def _atomic_write(path: Path, text: str, mode: Optional[int] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{uuid.uuid4().hex[:6]}.tmp")
    tmp.write_text(text, encoding="utf-8")
    if mode is not None:
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
    os.replace(tmp, path)


class TokenStore:
    """The integration token. ``NOTION_TOKEN`` first, else a file in ``<data>/notion``."""

    def __init__(self, root: Path):
        self.path = Path(root) / "notion" / "token"

    @staticmethod
    def env() -> str:
        return os.environ.get("NOTION_TOKEN", "").strip()

    def stored(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def get(self) -> Tuple[str, Optional[str]]:
        env = self.env()
        if env:
            return env, "env"
        stored = self.stored()
        return (stored, "settings") if stored else ("", None)

    def set(self, token: str) -> None:
        _atomic_write(self.path, token.strip(), 0o600)

    def clear(self) -> bool:
        try:
            self.path.unlink()
            return True
        except OSError:
            return False


class NotionState:
    """``{"bot": {...}, "months": {key: {...}}, "meetings": {session_id: {...}}}``."""

    def __init__(self, root: Path):
        self.path = Path(root) / "notion" / "state.json"
        self._lock = threading.RLock()

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data.setdefault("bot", None)
        data.setdefault("months", {})
        data.setdefault("meetings", {})
        data.setdefault("parents", {})
        data.setdefault("style_names", {})  # note type id -> its earlier names (newest first)
        return data

    def read(self) -> dict:
        with self._lock:
            return self._load()

    def update(self, fn: Callable[[dict], None]) -> dict:
        with self._lock:
            data = self._load()
            fn(data)
            _atomic_write(self.path, json.dumps(data, indent=1, ensure_ascii=False))
            return data

    def meeting(self, session_id: str) -> Optional[dict]:
        return self.read()["meetings"].get(session_id)

    def set_meeting(self, session_id: str, **fields) -> None:
        def go(d):
            d["meetings"].setdefault(session_id, {}).update(fields)
        self.update(go)


class _ExportError(NotionError):
    """A problem found before talking to Notion (nothing to copy, no parent page...)."""


# -- the service ----------------------------------------------------------------------


def _page_title(page: dict) -> str:
    """The plain-text title of a Notion page object ('' when it has none)."""
    for prop in ((page or {}).get("properties") or {}).values():
        if isinstance(prop, dict) and (prop.get("type") == "title" or "title" in prop):
            return "".join(str(t.get("plain_text") or "") for t in prop.get("title") or []).strip()
    return ""


class NotionSync:
    def __init__(
        self,
        store: store_mod.Store,
        *,
        transport: Optional[httpx.BaseTransport] = None,
        sleep: Callable[[float], None] = time.sleep,
        min_interval: Optional[float] = None,
        now: Callable[[], float] = time.time,
        tz=None,
        backoff: Callable[[int], float] = _backoff,
        client_factory: Optional[Callable[[str], NotionClient]] = None,
    ):
        self.store = store
        self.root = store.root
        self.tokens = TokenStore(self.root)
        self.state = NotionState(self.root)
        self.jobs_dir = self.root / "notion" / "jobs"
        self._transport = transport
        self._sleep = sleep
        self._min_interval = min_interval
        self._now = now
        self._tz = tz
        self._backoff = backoff
        self._client_factory = client_factory
        self._parent_retry: Dict[str, float] = {}
        self._pages_cache: Optional[Tuple[float, dict]] = None
        self._pages_lock = threading.Lock()
        self._jobs: Dict[str, dict] = {}
        self._jobs_lock = threading.RLock()
        self._work_lock = threading.Lock()  # one export at a time
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        store.add_listener("review_completed", self._on_review_completed)
        store.add_listener("session_renamed", self._on_session_renamed)
        store.add_listener("review_edited", self._on_review_edited)
        store.add_listener("sessions_derived", self._on_sessions_derived)

    # -- connection ----------------------------------------------------------------

    def _make_client(self, token: str) -> NotionClient:
        if self._client_factory:
            return self._client_factory(token)
        kwargs = {"transport": self._transport, "sleep": self._sleep}
        if self._min_interval is not None:
            kwargs["min_interval"] = self._min_interval
        return NotionClient(token, **kwargs)

    def _client(self) -> NotionClient:
        token, _source = self.tokens.get()
        if not token:
            raise NotConnected()
        return self._make_client(token)

    def connected(self) -> bool:
        return bool(self.tokens.get()[0])

    def parent_pages(self, parents: Dict[str, str]) -> Dict[str, dict]:
        """``{note type id: {id, url, title}}`` for the configured parent pages.

        The title is looked up once per parent page (``GET /v1/pages``) and then kept in the Notion state
        file, so viewing pages never calls Notion again. It is ``None`` when it is unknown (not connected,
        or the page could not be read; a failed lookup is not retried for a few minutes).
        """
        cached = self.state.read().get("parents") or {}
        out: Dict[str, dict] = {}
        client = None
        learned: Dict[str, str] = {}
        for tid, page_id in (parents or {}).items():
            if not page_id:
                continue
            title = (cached.get(page_id) or {}).get("title") or learned.get(page_id)
            if not title and self.connected() and self._now() >= self._parent_retry.get(page_id, 0.0):
                try:
                    client = client or self._client()
                    title = _page_title(client.get_page(page_id)) or None
                except Exception:  # best effort: the destination still shows as a link
                    title = None
                if title:
                    learned[page_id] = title
                else:
                    self._parent_retry[page_id] = self._now() + 300.0
            out[tid] = {"id": page_id, "url": f"https://www.notion.so/{page_id.replace('-', '')}", "title": title}
        if client is not None:
            client.close()
        if learned:
            self.state.update(lambda d: d["parents"].update({pid: {"title": t} for pid, t in learned.items()}))
        return out

    def list_pages(self, refresh: bool = False) -> dict:
        """Pages the integration can see, for the page picker: ``{connected, items, error}``.

        Each item is ``{id, title, url, parent, icon}`` where ``parent`` is the dashed id of the parent page
        (``None`` for a root: a workspace-level page, a page under a block, or one whose parent is not shared
        with the integration). Database rows and trashed pages are left out. Cached for a minute.
        """
        if not self.connected():
            return {"connected": False, "items": [], "error": None}
        with self._pages_lock:
            cached = self._pages_cache
            if not refresh and cached and self._now() - cached[0] < 60.0:
                return cached[1]
            client = None
            try:
                client = self._client()
                raw = list(client.search_pages())
            except NotionError as exc:
                return {"connected": True, "items": [], "error": str(exc)}
            finally:
                if client is not None:
                    client.close()
            items: Dict[str, dict] = {}
            for page in raw:
                if not isinstance(page, dict) or is_trashed(page) or not page.get("id"):
                    continue
                parent = page.get("parent") or {}
                kind = parent.get("type")
                if kind in ("database_id", "data_source_id"):
                    continue
                pid = dashed(page["id"])
                icon = page.get("icon")
                emoji = icon.get("emoji") if isinstance(icon, dict) and icon.get("type") == "emoji" else None
                items[pid] = {
                    "id": pid,
                    "title": _page_title(page) or "Untitled",
                    "url": page.get("url") or f"https://www.notion.so/{pid.replace('-', '')}",
                    "parent": dashed(parent["page_id"]) if kind == "page_id" and parent.get("page_id") else None,
                    "icon": emoji or None,
                }
            for item in items.values():
                if item["parent"] is not None and (item["parent"] not in items or item["parent"] == item["id"]):
                    item["parent"] = None
            ordered = sorted(items.values(), key=lambda i: (i["title"].lower(), i["id"]))
            result = {"connected": True, "items": ordered, "error": None}
            self._pages_cache = (self._now(), result)
        try:
            known = self.state.read().get("parents") or {}
            titles = {i["id"]: i["title"] for i in ordered}
            learn = {}
            for stored in (settings_mod.load_settings(self.root).notion_parents or {}).values():
                if stored and stored not in known and dashed(stored) in titles:
                    learn[stored] = titles[dashed(stored)]
            if learn:
                self.state.update(lambda d: d["parents"].update({pid: {"title": t} for pid, t in learn.items()}))
        except Exception:  # best effort only
            pass
        return result

    def destination(self, session_id: str, template: Optional[str] = None) -> dict:
        """Where a meeting's notes go (or would go) for a note type: ``{style, parent, month}``.

        ``parent`` is ``{id, url, title}`` (None when the type has no Notion page); ``month`` is
        ``{title, url}`` where ``url`` is only known once that month page exists. No Notion calls
        except the one-time parent title lookup.
        """
        settings = settings_mod.load_settings(self.root)
        review = self._notes_review(session_id)
        found = settings.find_template(template) if template else None
        if found:
            style = {"id": found["id"], "name": found["name"]}
        elif review:
            style = self._style_of(settings, review)
        else:
            default = settings.default_template()
            style = {"id": default["id"], "name": default["name"]}
        pid = settings.notion_parents.get(style["id"])
        if not pid:
            return {"style": style, "parent": None, "month": None}
        parent = self.parent_pages({style["id"]: pid})[style["id"]]
        rec = self.state.meeting(session_id) or {}
        if rec.get("block_id") and rec.get("style_id") == style["id"] and rec.get("page_url"):
            month = {"title": rec.get("month_title"), "url": rec["page_url"]}
        else:
            row = self.store.session_index_row(session_id) or {}
            start = float(rec.get("start") or row.get("created") or 0.0) or self._now()
            dt = local_dt(start, self._tz)
            entry = self.state.read()["months"].get(f"{pid}|{style['id']}|{dt.year:04d}-{dt.month:02d}")
            month = {"title": month_title(dt, style["name"]), "url": (entry or {}).get("url")}
        return {"style": style, "parent": parent, "month": month}

    @staticmethod
    def _bot_info(me: dict) -> dict:
        bot = me.get("bot") or {}
        owner_workspace = (bot.get("workspace_name") or "")
        return {"name": me.get("name") or "Notion integration", "workspace": owner_workspace}

    def test(self, token: Optional[str] = None) -> dict:
        """``GET /v1/users/me``. Uses ``token`` when given (not stored), else the saved one."""
        tok = (token or "").strip() or self.tokens.get()[0]
        if not tok:
            return {"ok": False, "error": "Notion is not connected. Add the integration token first."}
        client = self._make_client(tok)
        try:
            info = self._bot_info(client.me())
        except NotionError as exc:
            return {"ok": False, "error": exc.reason}
        finally:
            client.close()
        if not token:
            self.state.update(lambda d: d.__setitem__("bot", info))
        return {"ok": True, **info}

    def connect(self, token: str) -> dict:
        """Validate and save a token entered in Settings. The token is never returned."""
        token = (token or "").strip()
        if not token:
            return {"ok": False, "error": "Paste the integration token."}
        if self.tokens.env():
            return {"ok": False, "error": "NOTION_TOKEN is set on the server and takes priority. "
                                          "Remove it from the environment to use a token entered here."}
        result = self.test(token)
        if result.get("ok"):
            self.tokens.set(token)
            info = {"name": result["name"], "workspace": result["workspace"]}
            self.state.update(lambda d: d.__setitem__("bot", info))
        return result

    def disconnect(self) -> None:
        self.tokens.clear()
        self.state.update(lambda d: d.__setitem__("bot", None))

    def connection(self) -> dict:
        token, source = self.tokens.get()
        bot = self.state.read().get("bot") if token else None
        return {"connected": bool(token), "source": source, "bot_name": (bot or {}).get("name"),
                "workspace_name": (bot or {}).get("workspace")}

    # -- context for one meeting -------------------------------------------------------

    def _notes_review(self, session_id: str) -> Optional[dict]:
        for review in self.store.list_reviews(session_id=session_id):
            if review.get("status") == "done" and isinstance(review.get("payload"), dict):
                return review
        return None

    def _style_of(self, settings, review: dict) -> dict:
        found = settings.review_template(review)
        if found is None:
            found = settings.default_template()
        return {"id": found["id"], "name": found["name"]}

    def can_send(self, session_id: str) -> Tuple[bool, Optional[str]]:
        if not self.connected():
            return False, "Notion is not connected. Add the integration token in Settings."
        review = self._notes_review(session_id)
        if review is None:
            return False, "This meeting has no notes yet."
        settings = settings_mod.load_settings(self.root)
        style = self._style_of(settings, review)
        if not settings.notion_parents.get(style["id"]):
            return False, f"The \"{style['name']}\" note type has no Notion parent page. Set one in Settings."
        return True, None

    def _context(self, session_id: str) -> dict:
        if not self.store.session_exists(session_id) or self.store.is_trashed(session_id):
            raise _ExportError("This meeting no longer exists, so there is nothing to copy.")
        review = self._notes_review(session_id)
        if review is None:
            raise _ExportError("This meeting has no notes yet.")
        settings = settings_mod.load_settings(self.root)
        style = self._style_of(settings, review)
        parent = settings.notion_parents.get(style["id"])
        if not parent:
            raise _ExportError(f"The \"{style['name']}\" note type has no Notion parent page.")
        row = self.store.session_index_row(session_id) or {}
        start = float(row.get("created") or 0.0) or self._now()
        dt = local_dt(start, self._tz)
        return {
            "sid": session_id, "review": review, "style": style, "parent": parent,
            "name": row.get("name") or session_id, "start": start, "dt": dt,
            "month_title": month_title(dt, style["name"]),
            "month_key": f"{parent}|{style['id']}|{dt.year:04d}-{dt.month:02d}",
            "server_address": settings.server_address,
        }

    # -- status for the UI / agents ------------------------------------------------------

    def session_status(self, session_id: str, template: Optional[str] = None, *, with_destination: bool = True) -> dict:
        rec = self.state.meeting(session_id) or {}
        with self._jobs_lock:
            active = any(j["session_id"] == session_id and j["kind"] == "export" for j in self._jobs.values())
            removing = any(j["session_id"] == session_id and j["kind"] == "remove" and j.get("quiet")
                           for j in self._jobs.values())
        if removing:
            state = "removing"
        else:
            state = "pending" if active else (rec.get("status") if rec.get("status") in ("copied", "failed") else "none")
        ok, reason = self.can_send(session_id)
        review = self._notes_review(session_id)
        style = self._style_of(settings_mod.load_settings(self.root), review) if review else None
        return {
            "state": state,
            "url": rec.get("url") if rec.get("block_id") else None,
            "page_url": rec.get("page_url"),
            "error": rec.get("error") if state == "failed" else None,
            "warning": rec.get("warning") if state in ("copied", "none") else None,
            "retrying": bool(active and rec.get("error")),
            "opted_out": bool(rec.get("opted_out")),
            "month_page": rec.get("month_title"),
            "copied_style": rec.get("style_id") if rec.get("block_id") else None,
            "style": style,
            "can_send": ok,
            "reason": reason,
            "connected": self.connected(),
            "destination": self.destination(session_id, template) if with_destination else None,
        }

    def list_status(self, session_ids) -> Dict[str, dict]:
        """Notion state for a page of meetings, read in bulk from the state file and the job table.

        No Notion API calls and no settings reads. Only meetings with something to show appear:
        ``{sid: {state: pending|copied|failed, url, error, retrying}}``; the rest are absent.
        """
        meetings = self.state.read()["meetings"]
        with self._jobs_lock:
            active = {j["session_id"] for j in self._jobs.values() if j["kind"] == "export"}
            removing = {j["session_id"] for j in self._jobs.values() if j["kind"] == "remove" and j.get("quiet")}
        out: Dict[str, dict] = {}
        for sid in session_ids:
            rec = meetings.get(sid) or {}
            if sid in removing:
                state = "removing"
            elif sid in active:
                state = "pending"
            elif rec.get("status") in ("copied", "failed"):
                state = rec["status"]
            else:
                continue
            out[sid] = {
                "state": state,
                "url": rec.get("url") if rec.get("block_id") else None,
                "error": rec.get("error") if state == "failed" else None,
                "retrying": bool(sid in active and rec.get("error")),
            }
        return out

    def agent_status(self, session_id: str) -> dict:
        s = self.session_status(session_id, with_destination=False)
        return {"state": s["state"], "url": s["url"], "error": s["error"]}

    # -- listeners (the notes pipeline never waits on Notion) -----------------------------

    def _auto_wanted(self, session_id: str) -> bool:
        if not self.connected():
            return False
        settings = settings_mod.load_settings(self.root)
        if not settings.notion_auto_types or self.store.is_trashed(session_id):
            return False
        review = self._notes_review(session_id)
        if not review:
            return False
        type_id = self._style_of(settings, review)["id"]
        return type_id in settings.notion_auto_types and bool(settings.notion_parents.get(type_id))

    def _has_copy(self, session_id: str) -> bool:
        """True when this meeting already has a Notion entry: a copy, or a failed/pending attempt."""
        rec = self.state.meeting(session_id) or {}
        if rec.get("block_id") or rec.get("status") in ("copied", "failed", "pending"):
            return True
        with self._jobs_lock:
            return any(j["session_id"] == session_id and j["kind"] == "export" for j in self._jobs.values())

    def _on_review_completed(self, session_id: str) -> None:
        """Notes finished. The note type's auto-copy setting only decides FIRST-time copies; a meeting that is already in
        Notion is always kept in step (same type: update in place; other type/month: move). A meeting the owner removed
        from Notion (``opted_out``) is left alone until they send it again by hand. A meeting made by combining or
        splitting meetings that were in Notion (``send_on_notes``) is sent whatever its note type's auto-copy says."""
        try:
            rec = self.state.meeting(session_id) or {}
            if rec.get("opted_out"):
                return
            if self._has_copy(session_id):
                self._resync_existing(session_id)
            elif rec.get("send_on_notes"):
                self.state.set_meeting(session_id, send_on_notes=False)
                if self._can_send_quietly(session_id):
                    self.enqueue_export(session_id, source="inherit")
                else:
                    logger.info("session %s: not copied to Notion after combine/split (not connected, or its note "
                                "type has no Notion page)", session_id)
            elif self._auto_wanted(session_id):
                self.enqueue_export(session_id, source="auto")
        except Exception:  # noqa: BLE001 - must never disturb the notes pipeline
            logger.exception("session %s: could not queue the Notion copy", session_id)

    def _on_review_edited(self, session_id: str) -> None:
        """Notes were edited by hand (participants): keep an existing Notion copy in step.
        Never makes a first copy."""
        try:
            if self._has_copy(session_id):
                self._resync_existing(session_id)
        except Exception:  # noqa: BLE001
            logger.exception("session %s: could not queue the Notion resync", session_id)

    def _can_send_quietly(self, session_id: str) -> bool:
        return self.can_send(session_id)[0] and not self.store.is_trashed(session_id)

    # -- removing a meeting from Notion, and combine/split following along ---------------------

    def request_remove(self, session_id: str, *, opt_out: bool = True) -> bool:
        """Take this meeting's copy out of Notion in the background (the toggle block goes to Notion's trash).

        ``opt_out`` (the owner's "Remove this note from Notion") also stops automatic copies and re-syncs until
        they send it again by hand. Queued sends are cancelled. Returns False when nothing was in Notion.
        """
        with self._jobs_lock:
            for job in [j for j in self._jobs.values()
                        if j["session_id"] == session_id and j["kind"] == "export" and j["state"] == "queued"]:
                self._drop_job(job["job_id"])
        rec = self.state.meeting(session_id) or {}
        has_block = bool(rec.get("block_id") or rec.get("orphan_block"))
        if has_block:
            if opt_out:
                self.state.set_meeting(session_id, opted_out=True)
            self._enqueue("remove", session_id, source="manual" if opt_out else "derived", quiet=True,
                          opt_out=opt_out)
            return True
        if rec.get("status") in ("failed", "pending"):
            self._mark_removed(session_id, None)  # a failed/queued send with nothing in Notion: just forget it
        return False

    def _on_sessions_derived(self, kind: str, sources: list, results: list) -> None:
        """Meetings were combined/split (or that was undone): ``sources`` were replaced by ``results``.

        The sources' Notion copies are removed. A result is sent to Notion when its notes are complete if any
        source was in Notion (its note type's auto-copy decides otherwise, as for any meeting)."""
        try:
            wants = any(self._in_notion(sid) for sid in sources)
            for sid in sources:
                try:
                    self.request_remove(sid, opt_out=False)
                except Exception:  # noqa: BLE001 - Notion trouble must never fail a combine/split
                    logger.exception("session %s: could not queue the Notion removal after %s", sid, kind)
            if not wants:
                return
            for sid in results:
                try:
                    self.state.set_meeting(sid, send_on_notes=True, opted_out=False)
                    if self._notes_review(sid) is not None:  # an undo: the restored meetings already have notes
                        self.state.set_meeting(sid, send_on_notes=False)
                        if self._can_send_quietly(sid):
                            self.enqueue_export(sid, source="inherit")
                except Exception:  # noqa: BLE001
                    logger.exception("session %s: could not queue the Notion copy after %s", sid, kind)
        except Exception:  # noqa: BLE001
            logger.exception("could not follow a %s in Notion", kind)

    def _in_notion(self, session_id: str) -> bool:
        rec = self.state.meeting(session_id) or {}
        return self._has_copy(session_id) and not rec.get("opted_out")

    def _resync_existing(self, session_id: str) -> None:
        if not self.connected() or self.store.is_trashed(session_id):
            return
        review = self._notes_review(session_id)
        if review is None:
            return
        settings = settings_mod.load_settings(self.root)
        style = self._style_of(settings, review)
        if settings.notion_parents.get(style["id"]):
            self.enqueue_export(session_id, source="resync")
            return
        # The new note type has no Notion page: take the old copy out and mark the meeting as not in Notion.
        rec = self.state.meeting(session_id) or {}
        if rec.get("block_id") or rec.get("orphan_block"):
            self._enqueue("remove", session_id, source="resync")
        else:
            self._mark_removed(session_id, style["name"])

    def _mark_removed(self, session_id: str, style_name: Optional[str]) -> None:
        """Forget the Notion copy. ``style_name`` (a note type without a Notion page) adds a warning saying why."""
        warning = f"Removed from Notion: the \"{style_name}\" note type has no Notion page." if style_name else None

        def go(d):
            rec = d["meetings"].get(session_id)
            if rec is None:
                return
            for k in ("page_id", "block_id", "url", "page_url", "orphan_block", "month_title", "exported_at"):
                rec.pop(k, None)
            rec.update(status="none", error=None, warning=warning)
        self.state.update(go)

    def _remove(self, session_id: str, *, quiet: bool = False) -> None:
        """Delete the meeting's toggle (and a stale earlier copy) from Notion and forget it in the state.

        ``quiet`` is a removal the owner asked for, or a combine/split: a block that is already gone counts as
        success, but a refusal (no permission) is reported instead of pretending it worked. Otherwise (the note
        type changed to one without a Notion page) a refusal is best effort."""
        rec = self.state.meeting(session_id) or {}
        review = self._notes_review(session_id)
        style_name = self._style_of(settings_mod.load_settings(self.root), review)["name"] if review else "new"
        client = self._client()
        try:
            for block in (rec.get("block_id"), rec.get("orphan_block")):
                if not block:
                    continue
                try:
                    client.delete_block(block)
                except NotionError as exc:
                    if exc.retryable:
                        raise
                    if quiet and not self._already_gone(exc):
                        raise
                    # Gone already, or (type change) not allowed: best effort, the old toggle may need removing by hand.
            if rec.get("block_id"):
                self._untwin(client, rec.get("page_id"), rec.get("base_title"), exclude=session_id)
        finally:
            client.close()
        self._mark_removed(session_id, None if quiet else style_name)

    def _on_session_renamed(self, session_id: str) -> None:
        try:
            rec = self.state.meeting(session_id) or {}
            if rec.get("block_id") and self.connected():
                self._enqueue("rename", session_id, source="rename")
        except Exception:  # noqa: BLE001
            logger.exception("session %s: could not queue the Notion rename", session_id)

    # -- job queue ----------------------------------------------------------------------

    def _job_path(self, job_id: str) -> Path:
        return self.jobs_dir / f"{job_id}.json"

    def _save_job(self, job: dict) -> None:
        _atomic_write(self._job_path(job["job_id"]), json.dumps(job))

    def _drop_job(self, job_id: str) -> None:
        with self._jobs_lock:
            self._jobs.pop(job_id, None)
        try:
            self._job_path(job_id).unlink()
        except OSError:
            pass

    def _enqueue(self, kind: str, session_id: str, *, source: str, base_url: Optional[str] = None,
                 **extra) -> str:
        if not store_mod.is_safe_id(session_id):
            raise ValueError("invalid session id")
        with self._jobs_lock:
            for job in self._jobs.values():
                if job["kind"] == kind and job["session_id"] == session_id and job["state"] == "queued":
                    job.update(next_at=0.0, attempts=0, source=source, **extra)
                    if base_url:
                        job["base_url"] = base_url
                    self._save_job(job)
                    self._wake.set()
                    return job["job_id"]
            job = {"job_id": uuid.uuid4().hex, "kind": kind, "session_id": session_id, "state": "queued",
                   "attempts": 0, "next_at": 0.0, "created": self._now(), "source": source,
                   "base_url": base_url or "", **extra}
            self._jobs[job["job_id"]] = job
            self._save_job(job)
        self._wake.set()
        return job["job_id"]

    def enqueue_export(self, session_id: str, *, source: str = "manual", base_url: Optional[str] = None) -> str:
        """Queue a copy. A send the owner asks for ("manual", "agent") clears "Remove this note from Notion";
        every automatic source returns "" (nothing queued) for a meeting they removed."""
        explicit = source in ("manual", "agent")
        if not explicit and (self.state.meeting(session_id) or {}).get("opted_out"):
            return ""
        job_id = self._enqueue("export", session_id, source=source, base_url=base_url)
        fields = {"opted_out": False} if explicit else {}
        self.state.set_meeting(session_id, status="pending", error=None, warning=None, **fields)
        return job_id

    def resume_interrupted(self) -> List[str]:
        """Reload job files left by a previous process (running ones go back to queued)."""
        ids = []
        if self.jobs_dir.exists():
            for path in sorted(self.jobs_dir.glob("*.json")):
                try:
                    job = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if not isinstance(job, dict) or not job.get("job_id") or not job.get("session_id"):
                    continue
                if not store_mod.is_safe_id(str(job["session_id"])):
                    continue
                job["state"] = "queued"
                job.setdefault("attempts", 0)
                job.setdefault("next_at", 0.0)
                with self._jobs_lock:
                    self._jobs[job["job_id"]] = job
                self._save_job(job)
                ids.append(job["job_id"])
        if ids:
            logger.info("notion: resumed %d interrupted job(s)", len(ids))
            self._wake.set()
        return ids

    def pending_count(self) -> int:
        with self._jobs_lock:
            return len(self._jobs)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="meeting-notes-notion", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                did = self.run_pending()
            except Exception:  # noqa: BLE001
                logger.exception("notion worker error")
                did = 0
            if not did:
                self._wake.wait(timeout=1.0)
                self._wake.clear()

    def run_pending(self) -> int:
        """Process every job that is due, oldest first. Returns how many ran."""
        ran = 0
        with self._work_lock:
            while not self._stop.is_set():
                now = self._now()
                with self._jobs_lock:
                    due = sorted((j for j in self._jobs.values()
                                  if j["state"] == "queued" and float(j.get("next_at") or 0) <= now),
                                 key=lambda j: j.get("created", 0))
                    job = due[0] if due else None
                    if job:
                        job["state"] = "running"
                if job is None:
                    break
                self._process(job)
                ran += 1
        return ran

    def next_due_in(self) -> Optional[float]:
        """Seconds until the earliest retry is due (None when nothing is waiting). For tests/diagnostics."""
        with self._jobs_lock:
            times = [float(j.get("next_at") or 0) for j in self._jobs.values() if j["state"] == "queued"]
        return max(0.0, min(times) - self._now()) if times else None

    def _process(self, job: dict) -> None:
        sid = job["session_id"]
        try:
            if job["kind"] == "retitle_month":
                self._retitle_month(str(job.get("month_key") or ""))
            elif job["kind"] == "rename":
                self._rename(sid)
            elif job["kind"] == "remove":
                self._remove(sid, quiet=bool(job.get("quiet")))
            else:
                self._export(sid, job.get("base_url") or "")
            self._drop_job(job["job_id"])
        except NotionError as exc:
            self._handle_failure(job, exc)
        except Exception:  # noqa: BLE001
            logger.exception("notion job %s (session %s) crashed", job["job_id"], sid)
            self._handle_failure(job, NotionError("Unexpected error while copying to Notion (see the server log)."))

    def _handle_failure(self, job: dict, exc: NotionError) -> None:
        sid = job["session_id"]
        job["attempts"] = int(job.get("attempts") or 0) + 1
        if exc.retryable and job["attempts"] < MAX_ATTEMPTS:
            delay = self._backoff(job["attempts"])
            job.update(state="queued", next_at=self._now() + delay)
            self._save_job(job)
            logger.warning("notion job for %s failed (attempt %d), retrying in %.0fs: %s",
                           sid, job["attempts"], delay, exc.reason)
            if job["kind"] == "export":
                self.state.set_meeting(sid, status="pending", error=f"Retrying: {exc.reason}")
            return
        logger.warning("notion job for %s failed: %s", sid, exc.reason)
        reason = exc.reason
        if exc.retryable:
            reason = f"Gave up after {job['attempts']} attempts: {reason}"
        if job["kind"] == "export":
            self.state.set_meeting(sid, status="failed", error=reason, warning=None)
        elif job["kind"] == "retitle_month":
            pass  # logged above; the next export to that month page retries the rename
        elif job["kind"] == "remove":
            if job.get("opt_out"):
                # Still in Notion: keep it in step again (the owner can try the removal once more).
                self.state.set_meeting(sid, opted_out=False)
            self.state.set_meeting(sid, warning=f"Could not remove the old copy from Notion: {reason}")
        else:
            self.state.set_meeting(sid, warning=f"Could not update the heading in Notion: {reason}")
        self._drop_job(job["job_id"])

    # -- the export itself ------------------------------------------------------------------

    def _month_page(self, client: NotionClient, ctx: dict) -> dict:
        key = ctx["month_key"]
        entry = self.state.read()["months"].get(key)
        if entry:
            try:
                page = client.get_page(entry["page_id"])
                if not is_trashed(page):
                    if entry.get("title") and entry["title"] != ctx["month_title"]:
                        # The note type was renamed and the background retitle has not run yet.
                        self._apply_month_title(client, key, entry, ctx["month_title"])
                        entry = self.state.read()["months"].get(key) or entry
                    return entry
            except NotionError as exc:
                if not exc.not_found:
                    raise
            self._forget_month(key, entry["page_id"], keep=ctx["sid"])
        found = None
        # The current title first, then titles from before the note type was renamed.
        titles = [ctx["month_title"]] + [
            month_title(ctx["dt"], old) for old in self.state.read()["style_names"].get(ctx["style"]["id"], [])
        ]
        children = [c for c in client.iter_children(ctx["parent"])
                    if c.get("type") == "child_page" and not is_trashed(c)]
        for wanted in titles:
            found = next((c for c in children if (c.get("child_page") or {}).get("title") == wanted), None)
            if found:
                break
        if found:
            page = client.get_page(found["id"])
            if (found.get("child_page") or {}).get("title") != ctx["month_title"]:
                client.update_page_title(page["id"], ctx["month_title"])
        else:
            page = client.create_page(ctx["parent"], ctx["month_title"])
        entry = {"page_id": page["id"], "url": page.get("url") or f"https://www.notion.so/{page['id'].replace('-', '')}",
                 "title": ctx["month_title"]}
        self.state.update(lambda d: d["months"].__setitem__(key, entry))
        return entry

    def _apply_month_title(self, client: NotionClient, key: str, entry: dict, title: str) -> None:
        """Rename a known month page in Notion and record the new title (state keeps the page id)."""
        client.update_page_title(entry["page_id"], title)

        def go(d):
            cur = d["months"].get(key)
            if cur:
                cur["title"] = title
            for rec in d["meetings"].values():
                if rec.get("page_id") == entry["page_id"]:
                    rec["month_title"] = title
        self.state.update(go)

    def queue_month_retitle(self, style_id: str, old_name: str, new_name: str) -> int:
        """A note type was renamed: remember the old name (for the by-title fallback) and queue a
        background rename of every month page this state knows for it. Returns how many were queued."""
        def go(d):
            names = [n for n in d["style_names"].get(style_id, []) if n not in (old_name, new_name)]
            d["style_names"][style_id] = ([old_name] + names)[:10]
        self.state.update(go)
        if not self.connected():
            return 0  # the next export to each month page renames it (see _month_page)
        count = 0
        for key in list(self.state.read()["months"]):
            parts = key.split("|")
            if len(parts) == 3 and parts[1] == style_id:
                self._enqueue("retitle_month", "month_" + key.replace("|", "_"), source="rename-type",
                              month_key=key)
                count += 1
        return count

    def _retitle_month(self, key: str) -> None:
        entry = self.state.read()["months"].get(key)
        parts = key.split("|")
        if not entry or len(parts) != 3:
            return
        found = settings_mod.load_settings(self.root).find_template(parts[1])
        if found is None or found["id"] != parts[1]:
            return  # the type was deleted
        try:
            year, month = (int(x) for x in parts[2].split("-"))
            target = month_title(datetime(year, month, 1), found["name"])
        except ValueError:
            return
        if entry.get("title") == target:
            return
        client = self._client()
        try:
            try:
                page = client.get_page(entry["page_id"])
                if is_trashed(page):
                    return
                self._apply_month_title(client, key, entry, target)
            except NotionError as exc:
                if not exc.not_found:
                    raise
        finally:
            client.close()

    def _forget_month(self, key: str, page_id: str, keep: str) -> None:
        """``keep`` is the meeting being exported right now: its status is left alone, its block is dropped."""
        def go(d):
            d["months"].pop(key, None)
            for sid, rec in list(d["meetings"].items()):
                if rec.get("page_id") == page_id:
                    # Everything on a gone month page is gone with it, including the meeting being re-sent
                    # (its old block must not be treated as an orphan to delete).
                    for k in ("page_id", "block_id", "url", "page_url"):
                        rec.pop(k, None)
                    if sid != keep:
                        rec["status"] = "none"
        self.state.update(go)

    def _siblings(self, page_id: str, exclude: str) -> List[Tuple[float, str, str]]:
        meetings = self.state.read()["meetings"]
        return [(float(r.get("start") or 0), sid, r["block_id"]) for sid, r in meetings.items()
                if r.get("page_id") == page_id and r.get("block_id") and sid != exclude]

    def _block_alive(self, client: NotionClient, block_id: str) -> bool:
        """False when the block is gone in Notion (404, archived or in the trash)."""
        try:
            return not is_trashed(client.get_block(block_id))
        except NotionError as exc:
            if not exc.not_found:
                raise
            return False

    def _insert_toggle(self, client: NotionClient, ctx: dict, page_id: str, title: str) -> str:
        """Insert the (empty) toggle heading at the position that keeps the page newest-first.

        Notion accepts ``after_block`` on a deleted block and silently appends at the end of the page, so every
        anchor is checked first (one GET each); a deleted neighbour is forgotten and the next newer one is tried.
        """
        mine = (ctx["start"], ctx["sid"])
        skipped = set()
        while True:
            newer = sorted(e for e in self._siblings(page_id, ctx["sid"])
                           if (e[0], e[1]) > mine and e[1] not in skipped)
            anchor = newer[0] if newer else None
            if anchor is not None and not self._block_alive(client, anchor[2]):
                skipped.add(anchor[1])
                gone = self.state.meeting(anchor[1]) or {}
                self._forget_block(anchor[1])
                self._untwin(client, page_id, gone.get("base_title"), exclude=anchor[1])
                continue
            position = ({"type": "after_block", "after_block": {"id": anchor[2]}} if anchor
                        else {"type": "start"})
            try:
                resp = client.append_children(page_id, [nb.toggle_heading(title)], position)
                return resp["results"][0]["id"]
            except NotionError as exc:
                if anchor is None or exc.retryable or not exc.not_found:
                    raise
                # The anchor vanished between the check and the insert: forget it and pick the next one.
                skipped.add(anchor[1])
                self._forget_block(anchor[1])

    def _forget_block(self, session_id: str) -> None:
        def go(d):
            rec = d["meetings"].get(session_id)
            if rec:
                rec.pop("block_id", None)
                rec.pop("url", None)
                rec["status"] = "none"
                rec["warning"] = "Its copy in Notion was deleted there. Send it again to restore it."
        self.state.update(go)

    def _untwin(self, client: NotionClient, page_id: Optional[str], base: Optional[str], exclude: str) -> None:
        """A meeting left ``page_id``: if exactly one same-titled toggle remains and still carries the start
        time (added because of the twin), put its heading back to the base title."""
        if not page_id or not base:
            return
        left = [(sid, r) for sid, r in self.state.read()["meetings"].items()
                if sid != exclude and r.get("page_id") == page_id and r.get("block_id")
                and r.get("base_title") == base]
        if len(left) != 1:
            return
        sid, rec = left[0]
        if rec.get("title") == base:
            return
        try:
            client.update_block(rec["block_id"], nb.heading_update_body(base))
        except NotionError as exc:
            if exc.retryable:
                raise
            return  # deleted or not editable: cosmetic only
        self.state.set_meeting(sid, title=base)

    def _titles(self, ctx: dict, page_id: str) -> Tuple[str, str, List[Tuple[str, dict]]]:
        """(title, base_title, others-with-the-same-base-title). A shared name+date adds the start time."""
        base = heading_title(ctx["name"], ctx["dt"])
        meetings = self.state.read()["meetings"]
        same = [(sid, r) for sid, r in meetings.items()
                if sid != ctx["sid"] and r.get("page_id") == page_id and r.get("block_id")
                and r.get("base_title") == base]
        title = heading_title(ctx["name"], ctx["dt"], with_time=True) if same else base
        return title, base, same

    def _fix_collisions(self, client: NotionClient, same: List[Tuple[str, dict]]) -> List[str]:
        warnings = []
        for sid, rec in same:
            if rec.get("title") != rec.get("base_title"):
                continue
            dt = local_dt(float(rec.get("start") or 0), self._tz)
            timed = heading_title(rec.get("name") or "", dt, with_time=True)
            try:
                client.update_block(rec["block_id"], nb.heading_update_body(timed))
                self.state.set_meeting(sid, title=timed)
            except NotionError as exc:
                warnings.append(f"Could not add the start time to a same-named meeting: {exc.reason}")
        return warnings

    def _export(self, session_id: str, base_url: str) -> None:
        ctx = self._context(session_id)
        client = self._client()
        try:
            self._export_with(client, ctx, base_url)
        finally:
            client.close()

    def _export_with(self, client: NotionClient, ctx: dict, base_url: str) -> None:
        sid = ctx["sid"]
        month = self._month_page(client, ctx)
        page_id = month["page_id"]
        title, base_title, same = self._titles(ctx, page_id)
        address = (base_url or ctx["server_address"] or "").rstrip("/")
        link = f"{address}/sessions/{sid}" if address else None
        children = nb.notes_to_blocks(ctx["review"]["payload"], meeting_url=link)
        append = lambda parent, kids: client.append_children(parent, kids)  # noqa: E731
        warnings: List[str] = []
        old = self.state.meeting(sid) or {}
        old_block = old.get("block_id")
        block_id = None

        if old_block and old.get("page_id") == page_id:
            # Same month page: update the toggle in place (keeps its position and any links to it).
            if self._block_alive(client, old_block):
                block_id = old_block
            if block_id:
                client.update_block(block_id, nb.heading_update_body(title))
                stale = [c["id"] for c in client.iter_children(block_id)]
                nb.append_all(append, block_id, children)
                for child_id in stale:
                    try:
                        client.delete_block(child_id)
                    except NotionError as exc:
                        if not exc.not_found:
                            raise
        orphan = old.get("orphan_block")
        if block_id is None:
            if old_block and old.get("page_id") == page_id:
                old_block = None  # that toggle was deleted in Notion: nothing to clean up
            block_id = self._insert_toggle(client, ctx, page_id, title)
            if old_block and old_block != block_id:
                orphan = old_block
            # Record the block before filling it so a failed fill is repaired by a retry.
            self._record(ctx, month, block_id, title, base_title, link, warnings, status="pending", orphan=orphan)
            nb.append_all(append, block_id, children)
        if orphan and orphan != block_id:
            # The earlier copy (other style/month, or a toggle that was deleted): best effort.
            try:
                client.delete_block(orphan)
                orphan = None
            except NotionError as exc:
                if exc.retryable:
                    raise
                if self._already_gone(exc):
                    orphan = None
                else:
                    warnings.append("The previous copy of these notes could not be removed from "
                                    f"{old.get('month_title') or 'its old page'}: {exc.reason}")
                    orphan = None
        warnings += self._fix_collisions(client, same)
        self._record(ctx, month, block_id, title, base_title, link, warnings, status="copied", orphan=None)
        if old.get("block_id") and (old.get("page_id") != page_id or old.get("base_title") != base_title):
            # This meeting left its old page/title: a same-named neighbour no longer needs the start time.
            self._untwin(client, old.get("page_id"), old.get("base_title"), exclude=sid)

    @staticmethod
    def _already_gone(exc: NotionError) -> bool:
        """Notion refuses to touch a block that is archived or sits under an archived page: nothing to remove."""
        text = (exc.reason or "").lower()
        return exc.not_found or "archived" in text or "in trash" in text

    def _record(self, ctx, month, block_id, title, base_title, link, warnings, *, status, orphan=None) -> None:
        self.state.set_meeting(
            ctx["sid"], status=status, error=None, warning=" ".join(warnings) or None,
            style_id=ctx["style"]["id"], style_name=ctx["style"]["name"], parent_id=ctx["parent"],
            page_id=month["page_id"], month_title=ctx["month_title"], block_id=block_id,
            url=f"{month['url']}#{block_id.replace('-', '')}", page_url=month["url"],
            start=ctx["start"], name=ctx["name"], title=title, base_title=base_title,
            review_id=ctx["review"].get("review_id"), orphan_block=orphan,
            exported_at=self._now() if status == "copied" else None,
        )

    def _rename(self, session_id: str) -> None:
        rec = self.state.meeting(session_id) or {}
        if not rec.get("block_id"):
            return
        row = self.store.session_index_row(session_id) or {}
        name = row.get("name") or session_id
        start = float(rec.get("start") or row.get("created") or 0.0)
        dt = local_dt(start, self._tz)
        base = heading_title(name, dt)
        meetings = self.state.read()["meetings"]
        clash = any(sid != session_id and r.get("page_id") == rec.get("page_id") and r.get("base_title") == base
                    for sid, r in meetings.items())
        title = heading_title(name, dt, with_time=True) if clash else base
        client = self._client()
        try:
            client.update_block(rec["block_id"], nb.heading_update_body(title))
            self.state.set_meeting(session_id, name=name, title=title, base_title=base, warning=None)
            if rec.get("base_title") != base:
                self._untwin(client, rec.get("page_id"), rec.get("base_title"), exclude=session_id)
        finally:
            client.close()

    # -- backfill ------------------------------------------------------------------------------

    def backfill_candidates(self, template_id: str) -> List[str]:
        """Meetings whose newest notes are in style ``template_id`` and that have no Notion copy yet."""
        settings = settings_mod.load_settings(self.root)
        meetings = self.state.read()["meetings"]
        seen, out = set(), []
        for review in self.store.list_reviews():  # newest first
            sid = review.get("session_id")
            if not sid or sid in seen:
                continue
            if review.get("status") != "done" or not isinstance(review.get("payload"), dict):
                continue
            seen.add(sid)
            if self._style_of(settings, review)["id"] != template_id:
                continue
            if not store_mod.is_safe_id(sid) or not self.store.session_exists(sid) or self.store.is_trashed(sid):
                continue
            if (meetings.get(sid) or {}).get("block_id") or (meetings.get(sid) or {}).get("opted_out"):
                continue
            out.append(sid)
        return out

    def backfill(self, template_id: str) -> int:
        ids = self.backfill_candidates(template_id)
        # Oldest first, so the monthly pages fill in naturally.
        for sid in reversed(ids):
            self.enqueue_export(sid, source="backfill")
        return len(ids)
