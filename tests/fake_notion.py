"""An in-memory fake of the parts of the Notion REST API the exporter uses.

It is an ``httpx.MockTransport`` handler, so ``NotionClient`` talks to it through
its real request/response path. It *enforces* the documented limits instead of
being lenient, so a test fails when the exporter would be rejected by Notion:
a pinned ``Notion-Version``, bearer auth, at most 100 children per request, at
most two levels of nesting per request, ``after_block`` on a trashed block (accepted, appends at the end),
deleting a block under an archived ancestor (rejected), 2000 characters per rich-text item, at
most 100 rich-text items per block, the ``position`` object of the current API
(the removed flat ``after`` is refused) and heading children only on toggleable
headings.
"""

from __future__ import annotations

import json
import uuid
from typing import Callable, Dict, List, Optional

import httpx

TOKEN = "ntn_FAKE0123456789abcdefTOKEN"
VERSION = "2026-03-11"
LEAF_ONLY = {"divider", "code"}


def new_id() -> str:
    return str(uuid.uuid4())


def plain(rich: List[dict]) -> str:
    return "".join((r.get("text") or {}).get("content", "") for r in rich or [])


class FakeNotion:
    def __init__(self, token: str = TOKEN):
        self.token = token
        self.nodes: Dict[str, dict] = {}
        self.requests: List[dict] = []
        self.rules: List[dict] = []  # injected failures
        self.bot_name = "Meeting Notes bot"
        self.workspace = "Mike's workspace"

    # -- test helpers ---------------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def add_page(self, title: str, parent_id: Optional[str] = None, *, shared: bool = True,
                 parent_type: str = "page_id", icon: Optional[str] = None) -> str:
        """``parent_type="database_id"`` makes a database row (``parent_id`` is then just an id, not a node)."""
        pid = new_id()
        self.nodes[pid] = {"id": pid, "type": "page", "parent": parent_id, "children": [], "title": title,
                           "in_trash": False, "parent_type": parent_type, "icon": icon}
        if parent_id and parent_id in self.nodes:
            self.nodes[parent_id]["children"].append(pid)
        return pid

    def fail(self, status: int, *, times: int = 1, when: Optional[Callable[[str, str], bool]] = None,
             headers: Optional[dict] = None, body: Optional[dict] = None) -> None:
        self.rules.append({"status": status, "times": times, "when": when or (lambda m, p: True),
                           "headers": headers or {}, "body": body})

    def calls(self, method: Optional[str] = None, path_contains: str = "") -> List[dict]:
        return [r for r in self.requests
                if (method is None or r["method"] == method) and path_contains in r["path"]]

    def child_titles(self, parent_id: str) -> List[str]:
        out = []
        for cid in self.nodes[parent_id]["children"]:
            n = self.nodes[cid]
            if not n["in_trash"] and n["type"] == "page":
                out.append(n["title"])
        return out

    def find_page(self, title: str, parent_id: Optional[str] = None) -> Optional[str]:
        for nid, n in self.nodes.items():
            if n["type"] == "page" and n["title"] == title and (parent_id is None or n["parent"] == parent_id):
                return nid
        return None

    def toggles(self, page_id: str) -> List[str]:
        """Heading-1 titles of a page, top to bottom (trashed excluded)."""
        return [self.text(c) for c in self.nodes[page_id]["children"]
                if self.nodes[c]["type"] == "heading_1" and not self.nodes[c]["in_trash"]]

    def toggle_id(self, page_id: str, title: str) -> str:
        for c in self.nodes[page_id]["children"]:
            if self.nodes[c]["type"] == "heading_1" and self.text(c) == title and not self.nodes[c]["in_trash"]:
                return c
        raise KeyError(title)

    def text(self, block_id: str) -> str:
        n = self.nodes[block_id]
        return plain((n.get(n["type"]) or {}).get("rich_text") or [])

    def children(self, block_id: str) -> List[dict]:
        return [self.nodes[c] for c in self.nodes[block_id]["children"] if not self.nodes[c]["in_trash"]]

    def tree(self, block_id: str) -> list:
        out = []
        for c in self.children(block_id):
            entry = {"type": c["type"], "text": self.text(c["id"])}
            if c["children"]:
                entry["children"] = self.tree(c["id"])
            out.append(entry)
        return out

    def trash(self, node_id: str) -> None:
        self.nodes[node_id]["in_trash"] = True

    # -- http ---------------------------------------------------------------------------

    @staticmethod
    def _err(status: int, code: str, message: str) -> httpx.Response:
        return httpx.Response(status, json={"object": "error", "status": status, "code": code, "message": message})

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        self.requests.append({"method": request.method, "path": path, "body": body,
                              "params": dict(request.url.params), "version": request.headers.get("notion-version")})
        for rule in self.rules:
            if rule["times"] > 0 and rule["when"](request.method, path):
                rule["times"] -= 1
                payload = rule["body"] or {"object": "error", "status": rule["status"], "code": "x", "message": "x"}
                return httpx.Response(rule["status"], json=payload, headers=rule["headers"])
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return self._err(401, "unauthorized", "API token is invalid.")
        if request.headers.get("notion-version") != VERSION:
            return self._err(400, "invalid_request", "bad Notion-Version")
        segs = [s for s in path.split("/") if s]
        try:
            return self._route(request.method, segs, body, request.url.params)
        except _Reject as exc:
            return self._err(exc.status, exc.code, exc.message)

    @staticmethod
    def _dashed(node_id: str) -> str:
        h = node_id.replace("-", "")
        return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}" if len(h) == 32 else node_id

    def _node(self, node_id: str) -> dict:
        n = self.nodes.get(self._dashed(node_id))
        if n is None:
            raise _Reject(404, "object_not_found", f"Could not find block with ID: {node_id}.")
        return n

    def _serialize(self, n: dict) -> dict:
        if n["type"] == "page":
            return {"object": "page", "id": n["id"], "in_trash": n["in_trash"], "archived": n["in_trash"],
                    "url": "https://www.notion.so/" + n["id"].replace("-", ""),
                    "properties": {"title": {"title": [{"plain_text": n["title"]}]}}}
        out = {"object": "block", "id": n["id"], "type": n["type"], "in_trash": n["in_trash"],
               "has_children": bool(n["children"]), n["type"]: n.get(n["type"])}
        return out

    def _parent_obj(self, n: dict) -> dict:
        kind = n.get("parent_type", "page_id")
        if kind == "page_id" and n["parent"] in self.nodes:
            return {"type": "page_id", "page_id": n["parent"]}
        if kind in ("database_id", "data_source_id"):
            return {"type": kind, kind: n["parent"] or new_id()}
        return {"type": "workspace", "workspace": True}

    def _search(self, body: dict):
        flt = body.get("filter") or {}
        if flt != {"property": "object", "value": "page"}:
            raise _Reject(400, "validation_error", "only the page filter is supported")
        pages = [n for n in self.nodes.values() if n["type"] == "page" and not n["in_trash"]]
        start = int(body["start_cursor"]) if body.get("start_cursor") else 0
        size = min(int(body.get("page_size", 100)), 100)
        chunk = pages[start:start + size]
        more = start + size < len(pages)
        results = []
        for n in chunk:
            out = {**self._serialize(n), "parent": self._parent_obj(n)}
            if n.get("icon"):
                out["icon"] = {"type": "emoji", "emoji": n["icon"]}
            results.append(out)
        return httpx.Response(200, json={"object": "list", "results": results, "has_more": more,
                                         "next_cursor": str(start + size) if more else None})

    def _route(self, method, segs, body, params):
        if segs == ["v1", "users", "me"] and method == "GET":
            return httpx.Response(200, json={"object": "user", "type": "bot", "id": new_id(),
                                             "name": self.bot_name,
                                             "bot": {"workspace_name": self.workspace}})
        if segs == ["v1", "search"] and method == "POST":
            return self._search(body or {})
        if segs == ["v1", "pages"] and method == "POST":
            parent = self._node(body["parent"]["page_id"])
            if parent["in_trash"]:
                raise _Reject(400, "validation_error", "Can't edit block that is in trash.")
            title = plain(body["properties"]["title"]["title"])
            pid = self.add_page(title, parent["id"])
            return httpx.Response(200, json=self._serialize(self.nodes[pid]))
        if len(segs) == 3 and segs[:2] == ["v1", "pages"] and method == "GET":
            return httpx.Response(200, json=self._serialize(self._node(segs[2])))
        if len(segs) == 3 and segs[:2] == ["v1", "pages"] and method == "PATCH":
            n = self._node(segs[2])
            if n["in_trash"]:
                raise _Reject(400, "validation_error", "Can't edit page that is in trash.")
            n["title"] = plain(body["properties"]["title"]["title"])
            return httpx.Response(200, json=self._serialize(n))
        if len(segs) == 4 and segs[:2] == ["v1", "blocks"] and segs[3] == "children":
            return self._children(method, segs[2], body, params)
        if len(segs) == 3 and segs[:2] == ["v1", "blocks"]:
            n = self._node(segs[2])
            if method == "GET":
                return httpx.Response(200, json=self._serialize(n))
            if method == "DELETE":
                if self._archived(n):
                    raise _Reject(400, "validation_error",
                                  "Can't edit block that is archived. You must unarchive the block (or its "
                                  "archived ancestor) before editing.")
                self._trash_tree(n)
                return httpx.Response(200, json=self._serialize(n))
            if method == "PATCH":
                if n["in_trash"]:
                    raise _Reject(400, "validation_error", "Can't edit block that is in trash.")
                t = n["type"]
                if t not in body:
                    raise _Reject(400, "validation_error", f"body.{t} should be defined")
                self._check_rich(body[t].get("rich_text") or [])
                n[t] = {**n[t], **body[t]}
                return httpx.Response(200, json=self._serialize(n))
        raise _Reject(404, "invalid_request_url", f"Invalid request URL: {method} /{'/'.join(segs)}")

    def _archived(self, n: dict) -> bool:
        """The node itself or any ancestor is in the trash."""
        while n is not None:
            if n["in_trash"]:
                return True
            n = self.nodes.get(n.get("parent"))
        return False

    def _trash_tree(self, n: dict) -> None:
        n["in_trash"] = True
        for c in n["children"]:
            self._trash_tree(self.nodes[c])

    def _children(self, method, block_id, body, params):
        parent = self._node(block_id)
        if method == "GET":
            live = [c for c in parent["children"] if not self.nodes[c]["in_trash"]]
            start = 0
            if params.get("start_cursor"):
                start = int(params["start_cursor"])
            size = min(int(params.get("page_size", 100)), 100)
            page = live[start:start + size]
            more = start + size < len(live)
            results = []
            for c in page:
                n = self.nodes[c]
                if n["type"] == "page":
                    results.append({"object": "block", "id": n["id"], "type": "child_page",
                                    "in_trash": False, "child_page": {"title": n["title"]}})
                else:
                    results.append(self._serialize(n))
            return httpx.Response(200, json={"object": "list", "results": results, "has_more": more,
                                             "next_cursor": str(start + size) if more else None})
        if parent["in_trash"]:
            raise _Reject(400, "validation_error", "Can't edit block that is in trash.")
        if "after" in body:
            raise _Reject(400, "validation_error", "body.after is no longer accepted; use position")
        children = body.get("children")
        if not isinstance(children, list) or not children:
            raise _Reject(400, "validation_error", "body.children should be defined")
        if len(children) > 100:
            raise _Reject(400, "validation_error", "body.children.length should be <= 100")
        total = sum(self._validate_block(b, level=1) for b in children)
        if total > 1000:
            raise _Reject(400, "validation_error", "too many blocks in one request")
        if len(json.dumps(body)) > 500_000:
            raise _Reject(400, "validation_error", "payload too large")
        if parent["type"] == "heading_1" and not parent["heading_1"].get("is_toggleable"):
            raise _Reject(400, "validation_error", "heading is not toggleable")
        position = body.get("position") or {"type": "end"}
        index = len(parent["children"])
        if position["type"] == "start":
            index = 0
        elif position["type"] == "after_block":
            ref = self._dashed(position["after_block"]["id"])
            if ref not in parent["children"]:
                raise _Reject(404, "object_not_found", f"Could not find block with ID: {ref}.")
            # Like the real API: an archived/trashed anchor is accepted without error and the new block
            # lands at the END of the page.
            index = len(parent["children"]) if self.nodes[ref]["in_trash"] else parent["children"].index(ref) + 1
        elif position["type"] != "end":
            raise _Reject(400, "validation_error", "bad position")
        created = []
        for b in children:
            nid = self._create(b, parent["id"])
            created.append(nid)
        parent["children"][index:index] = created
        return httpx.Response(200, json={"object": "list", "has_more": False, "next_cursor": None,
                                         "results": [self._serialize(self.nodes[c]) for c in created]})

    def _check_rich(self, rich: list) -> None:
        if len(rich) > 100:
            raise _Reject(400, "validation_error", "rich_text.length should be <= 100")
        for r in rich:
            content = (r.get("text") or {}).get("content", "")
            if len(content) > 2000:
                raise _Reject(400, "validation_error", "text.content.length should be <= 2000")
            link = (r.get("text") or {}).get("link")
            if link and len(link.get("url", "")) > 2000:
                raise _Reject(400, "validation_error", "link url too long")

    def _validate_block(self, b: dict, level: int) -> int:
        t = b.get("type")
        if not t or t not in b:
            raise _Reject(400, "validation_error", "block needs type and a matching body")
        body = b[t]
        self._check_rich(body.get("rich_text") or [])
        kids = body.get("children") or []
        count = 1
        if kids:
            if t in LEAF_ONLY:
                raise _Reject(400, "validation_error", f"{t} cannot have children")
            if level >= 2:
                raise _Reject(400, "validation_error", "Exceeded maximum nesting of two levels in one request")
            if t == "heading_1" and not body.get("is_toggleable"):
                raise _Reject(400, "validation_error", "heading_1 children need is_toggleable")
            count += sum(self._validate_block(k, level + 1) for k in kids)
        return count

    def _create(self, b: dict, parent_id: str) -> str:
        t = b["type"]
        body = dict(b[t])
        kids = body.pop("children", None) or []
        nid = new_id()
        self.nodes[nid] = {"id": nid, "type": t, "parent": parent_id, "children": [], "in_trash": False, t: body}
        for k in kids:
            self.nodes[nid]["children"].append(self._create(k, nid))
        return nid


class _Reject(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message
