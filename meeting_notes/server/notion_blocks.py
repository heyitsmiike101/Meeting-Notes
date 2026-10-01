"""Meeting notes -> Notion blocks.

Two jobs:

* :func:`markdown_to_blocks` converts Markdown (what the notes' ``meeting_notes``
  body is written in) into Notion block objects: headings, bulleted/numbered
  lists with nesting, to-dos, quotes, dividers, code blocks and paragraphs, with
  **bold**, *italic*, ``code``, ~~strike~~ and links as rich text;
* :func:`notes_to_blocks` lays a whole notes payload out the way the web UI does
  (Summary, Decisions, Action items, ...), action items as to-do blocks.

Nothing here talks to Notion. The limits it must respect are enforced here and
in :func:`plan_append`: a rich-text item holds at most 2000 characters (longer
text is split), a block at most 100 rich-text items, a request at most 100
blocks and two levels of nesting (deeper levels are appended afterwards).
"""

from __future__ import annotations

import json
import re
from typing import Callable, List, Optional, Tuple

MAX_TEXT = 2000
MAX_RICH_ITEMS = 100
MAX_BLOCKS_PER_REQUEST = 100
# Nesting allowed inside one request. Notion documents "two levels"; to be safe
# whichever way that is counted, a block sent with its children keeps those
# children leaf-only (top-level block + one level below).
MAX_INLINE_DEPTH = 1
MAX_REQUEST_BLOCKS = 400
MAX_REQUEST_BYTES = 400_000

_CODE_LANGS = {
    "py": "python", "python": "python", "js": "javascript", "javascript": "javascript", "ts": "typescript",
    "typescript": "typescript", "json": "json", "bash": "bash", "sh": "shell", "shell": "shell",
    "yaml": "yaml", "yml": "yaml", "sql": "sql", "html": "html", "css": "css", "go": "go", "java": "java",
    "c": "c", "cpp": "c++", "csharp": "c#", "cs": "c#", "rust": "rust", "ruby": "ruby", "php": "php",
    "powershell": "powershell", "ps1": "powershell", "markdown": "markdown", "md": "markdown", "xml": "xml",
    "diff": "diff", "docker": "docker", "dockerfile": "docker",
}

# -- inline ------------------------------------------------------------------

_Seg = Tuple[str, frozenset, Optional[str]]  # (text, formats, link)
_LINK_RE = re.compile(r'\[([^\]\n]+)\]\(\s*<?([^)\s>]+)>?(?:\s+"[^"]*")?\s*\)')
_ESCAPABLE = set("\\`*_{}[]()#+-.!~|>")


def _safe_url(url: str) -> Optional[str]:
    url = url.strip()
    if len(url) > MAX_TEXT:
        return None
    if re.match(r"^(https?://|mailto:)", url, re.I):
        return url
    return None


def parse_inline(text: str) -> List[_Seg]:
    segs = _parse(text, frozenset(), None)
    merged: List[_Seg] = []
    for t, f, l in segs:
        if not t:
            continue
        if merged and merged[-1][1] == f and merged[-1][2] == l:
            merged[-1] = (merged[-1][0] + t, f, l)
        else:
            merged.append((t, f, l))
    return merged


def _parse(s: str, fmt: frozenset, link: Optional[str]) -> List[_Seg]:
    out: List[_Seg] = []
    buf: List[str] = []

    def flush() -> None:
        if buf:
            out.append(("".join(buf), fmt, link))
            buf.clear()

    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n and s[i + 1] in _ESCAPABLE:
            buf.append(s[i + 1])
            i += 2
            continue
        if c == "`":
            j = s.find("`", i + 1)
            if j > i + 1:
                flush()
                out.append((s[i + 1:j], fmt | {"code"}, link))
                i = j + 1
                continue
        if c == "[":
            m = _LINK_RE.match(s, i)
            if m:
                flush()
                url = _safe_url(m.group(2))
                out.extend(_parse(m.group(1), fmt, url or link))
                i = m.end()
                continue
        if s.startswith(("**", "__"), i):
            marker = s[i:i + 2]
            j = s.find(marker, i + 2)
            if j > i + 2 and not s[i + 2].isspace():
                flush()
                out.extend(_parse(s[i + 2:j], fmt | {"bold"}, link))
                i = j + 2
                continue
        if s.startswith("~~", i):
            j = s.find("~~", i + 2)
            if j > i + 2:
                flush()
                out.extend(_parse(s[i + 2:j], fmt | {"strikethrough"}, link))
                i = j + 2
                continue
        if c in "*_" and not s.startswith(c * 2, i):
            word_ok = c == "*" or i == 0 or not s[i - 1].isalnum()
            j = i + 1
            end = -1
            while word_ok and j < n:
                j = s.find(c, j)
                if j < 0:
                    break
                if j + 1 < n and s[j + 1] == c:
                    j += 2
                    continue
                if not s[j - 1].isspace() and (c == "*" or j + 1 >= n or not s[j + 1].isalnum()):
                    end = j
                break
            if end > i + 1 and not s[i + 1].isspace():
                flush()
                out.extend(_parse(s[i + 1:end], fmt | {"italic"}, link))
                i = end + 1
                continue
        buf.append(c)
        i += 1
    flush()
    return out


def _chunks(text: str, size: int = MAX_TEXT) -> List[str]:
    """Split ``text`` into pieces of at most ``size`` chars, preferring to break at whitespace."""
    out = []
    while len(text) > size:
        cut = text.rfind(" ", size // 2, size)
        if cut < 0:
            cut = text.rfind("\n", size // 2, size)
        cut = size if cut < 0 else cut + 1
        out.append(text[:cut])
        text = text[cut:]
    if text or not out:
        out.append(text)
    return out


def rich_text(segs: List[_Seg], *, color: Optional[str] = None) -> List[dict]:
    """Segments -> Notion ``rich_text`` items, each content <= 2000 chars."""
    items: List[dict] = []
    for text, fmt, link in segs:
        for piece in _chunks(text):
            if not piece:
                continue
            item: dict = {"type": "text", "text": {"content": piece}}
            if link:
                item["text"]["link"] = {"url": link}
            ann = {k: True for k in ("bold", "italic", "strikethrough", "code") if k in fmt}
            if color:
                ann["color"] = color
            if ann:
                item["annotations"] = ann
            items.append(item)
    return items


def plain_rich_text(text: str, **kw) -> List[dict]:
    return rich_text([(text, frozenset(), None)], **kw) if text else []


# -- block construction ------------------------------------------------------


def _blocks_for(btype: str, rt: List[dict], *, children: Optional[list] = None, **extra) -> List[dict]:
    """One block, or several when the rich text has more than 100 items."""
    out = []
    first = True
    for k in range(0, max(len(rt), 1), MAX_RICH_ITEMS):
        body = {"rich_text": rt[k:k + MAX_RICH_ITEMS], **(extra if first else {})}
        t = btype if first else "paragraph"
        if first and children:
            body["children"] = children
        out.append({"object": "block", "type": t, t: body})
        first = False
    return out


def block(btype: str, text: str = "", *, children: Optional[list] = None, color: Optional[str] = None,
          **extra) -> dict:
    """Convenience for callers that build a block from plain/inline-Markdown text."""
    return _blocks_for(btype, rich_text(parse_inline(text), color=color), children=children, **extra)[0]


def toggle_heading(title: str) -> dict:
    """A toggleable Heading 1 (no children; they are appended afterwards)."""
    rt = plain_rich_text(title[:MAX_TEXT])
    return {"object": "block", "type": "heading_1",
            "heading_1": {"rich_text": rt, "is_toggleable": True}}


def heading_update_body(title: str) -> dict:
    return {"heading_1": {"rich_text": plain_rich_text(title[:MAX_TEXT]), "is_toggleable": True}}


_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_HR_RE = re.compile(r"^ {0,3}([-*_])( *\1){2,} *$")
_LIST_RE = re.compile(r"^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$")
_TODO_RE = re.compile(r"^\[( |x|X)\]\s+(.*)$")
_QUOTE_RE = re.compile(r"^ {0,3}>\s?(.*)$")
_FENCE_RE = re.compile(r"^ {0,3}(```+|~~~+)\s*([\w+#.-]*)")


def _expand(line: str) -> str:
    return line.replace("\t", "    ")


def markdown_to_blocks(md: str, *, heading_base: int = 2) -> List[dict]:
    """Convert Markdown to a list of Notion blocks (nested via ``children``).

    ``heading_base`` is the Notion heading level the shallowest Markdown heading
    maps to (2 or 3); deeper levels map one step down and Notion has no level 4+,
    so those become ``heading_3``.
    """
    lines = [_expand(x) for x in str(md or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    levels = sorted({len(m.group(1)) for m in (_HEADING_RE.match(x) for x in lines) if m})
    out: List[dict] = []
    para: List[str] = []
    stack: List[Tuple[int, dict]] = []  # (indent, list-item block)
    i = 0

    def heading_type(level: int) -> str:
        rank = levels.index(level) if level in levels else 0
        return f"heading_{min(3, heading_base + rank)}"

    def flush_para() -> None:
        if not para:
            return
        text = ""
        for k, ln in enumerate(para):
            if k:
                text += "\n" if (para[k - 1].endswith("  ") or para[k - 1].endswith("\\")) else " "
            text += ln.strip().rstrip("\\")
        para.clear()
        out.extend(_blocks_for("paragraph", rich_text(parse_inline(text))))

    def end_list() -> None:
        stack.clear()

    def add_item(btype: str, indent: int, text: str, **extra) -> None:
        item = _blocks_for(btype, rich_text(parse_inline(text)), **extra)[0]
        while stack and stack[-1][0] >= indent:
            stack.pop()
        if stack:
            parent = stack[-1][1]
            parent[parent["type"]].setdefault("children", []).append(item)
        else:
            out.append(item)
        stack.append((indent, item))

    def item_text_append(extra_text: str) -> None:
        item = stack[-1][1]
        rt = item[item["type"]]["rich_text"]
        add = rich_text(parse_inline(" " + extra_text.strip()))
        if len(rt) + len(add) <= MAX_RICH_ITEMS:
            rt.extend(add)

    while i < len(lines):
        line = lines[i]
        fence = _FENCE_RE.match(line)
        if fence:
            flush_para()
            end_list()
            marker, lang = fence.group(1), fence.group(2).lower()
            code: List[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(marker[:3]):
                code.append(lines[i])
                i += 1
            i += 1
            language = _CODE_LANGS.get(lang, "plain text")
            body = "\n".join(code)
            out.append({"object": "block", "type": "code", "code": {
                "rich_text": [{"type": "text", "text": {"content": p}} for p in _chunks(body)] if body else [],
                "language": language}})
            continue
        if not line.strip():
            flush_para()
            i += 1
            # A blank line does not end a list; a following unindented non-item does.
            continue
        h = _HEADING_RE.match(line)
        if h:
            flush_para()
            end_list()
            t = heading_type(len(h.group(1)))
            out.extend(_blocks_for(t, rich_text(parse_inline(h.group(2)))))
            i += 1
            continue
        if _HR_RE.match(line):
            flush_para()
            end_list()
            out.append({"object": "block", "type": "divider", "divider": {}})
            i += 1
            continue
        q = _QUOTE_RE.match(line)
        if q:
            flush_para()
            end_list()
            qlines = []
            while i < len(lines) and _QUOTE_RE.match(lines[i]):
                qlines.append(_QUOTE_RE.match(lines[i]).group(1))
                i += 1
            text = "\n".join(qlines).strip()
            out.extend(_blocks_for("quote", rich_text(parse_inline(text))))
            continue
        if line.lstrip().startswith("|") and line.count("|") >= 2:
            flush_para()
            end_list()
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append(lines[i].rstrip())
                i += 1
            body = "\n".join(rows)
            out.append({"object": "block", "type": "code", "code": {
                "rich_text": [{"type": "text", "text": {"content": p}} for p in _chunks(body)],
                "language": "plain text"}})
            continue
        m = _LIST_RE.match(line)
        if m:
            flush_para()
            indent = len(m.group(1))
            marker, text = m.group(2), m.group(3)
            todo = _TODO_RE.match(text)
            if todo:
                add_item("to_do", indent, todo.group(2), checked=todo.group(1).lower() == "x")
            elif marker[0].isdigit():
                add_item("numbered_list_item", indent, text)
            else:
                add_item("bulleted_list_item", indent, text)
            i += 1
            continue
        if stack and line.startswith(" "):
            item_text_append(line)  # continuation of the previous list item
            i += 1
            continue
        end_list()
        para.append(line)
        i += 1
    flush_para()
    return out


# -- notes payload -> blocks --------------------------------------------------


def _as_list(value) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v or "").strip()]


def _bullets(items: List[str]) -> List[dict]:
    return [block("bulleted_list_item", t) for t in items]


def _section(title: str, blocks: List[dict]) -> List[dict]:
    return ([block("heading_2", title)] + blocks) if blocks else []


def _action_item_block(item) -> Optional[dict]:
    if isinstance(item, str):
        item = {"action": item}
    if not isinstance(item, dict):
        return None
    action = str(item.get("action") or "").strip()
    if not action:
        return None
    segs = list(parse_inline(action))
    meta = []
    if item.get("owner"):
        meta.append(f"Owner: {item['owner']}")
    if item.get("due_date"):
        meta.append(f"Due: {item['due_date']}")
    rt = rich_text(segs)
    if meta:
        rt += plain_rich_text("  " + " · ".join(meta), color="gray")
    if item.get("context"):
        rt += plain_rich_text(" - " + str(item["context"]).strip(), color="gray")
    return _blocks_for("to_do", rt, checked=False)[0]


def notes_to_blocks(payload: dict, *, meeting_url: Optional[str] = None) -> List[dict]:
    """The blocks that go inside a meeting's toggle heading."""
    payload = payload if isinstance(payload, dict) else {}
    out: List[dict] = []
    summary = str(payload.get("summary") or "").strip()
    out += _section("Summary", markdown_to_blocks(summary, heading_base=3))
    out += _section("Decisions", _bullets(_as_list(payload.get("decisions"))))
    todos = [b for b in (_action_item_block(x) for x in (payload.get("action_items") or [])) if b]
    out += _section("Action items", todos)
    out += _section("Key points", _bullets(_as_list(payload.get("key_points"))))
    body = str(payload.get("meeting_notes") or "").strip()
    out += _section("Meeting notes", markdown_to_blocks(body, heading_base=3))
    out += _section("Participants", _bullets(_as_list(payload.get("participants"))))
    out += _section("Open questions", _bullets(_as_list(payload.get("open_questions"))))
    out += _section("Risks", _bullets(_as_list(payload.get("risks"))))
    out += _section("Next steps", _bullets(_as_list(payload.get("next_steps"))))
    if not out:
        out.append(block("paragraph", "No notes were recorded for this meeting."))
    if meeting_url:
        out.append({"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{
            "type": "text",
            "text": {"content": "Open this meeting in Meeting Notes", "link": {"url": meeting_url}},
            "annotations": {"color": "gray"},
        }]}})
    return out


# -- request planning ----------------------------------------------------------


def _depth(b: dict) -> int:
    kids = (b.get(b.get("type")) or {}).get("children") or []
    return 0 if not kids else 1 + max(_depth(k) for k in kids)


def _count(b: dict) -> int:
    kids = (b.get(b.get("type")) or {}).get("children") or []
    return 1 + sum(_count(k) for k in kids)


def _strip_children(b: dict) -> Tuple[dict, list]:
    body = dict(b[b["type"]])
    kids = body.pop("children", None) or []
    return {**b, b["type"]: body}, kids


def plan_append(blocks: List[dict]) -> List[List[Tuple[dict, list]]]:
    """Split ``blocks`` into request batches.

    Returns a list of batches; a batch is a list of ``(block_to_send, deferred_children)``.
    A block whose subtree is too deep (or too big) is sent *without* children and
    its children are returned to be appended to the created block afterwards.
    Every batch has at most 100 blocks, :data:`MAX_REQUEST_BLOCKS` blocks in
    total and :data:`MAX_REQUEST_BYTES` serialized bytes.
    """
    batches: List[List[Tuple[dict, list]]] = []
    cur: List[Tuple[dict, list]] = []
    cur_blocks = cur_bytes = 0
    for b in blocks:
        if _depth(b) <= MAX_INLINE_DEPTH and _count(b) <= 50:
            item: Tuple[dict, list] = (b, [])
        else:
            item = _strip_children(b)
        size = len(json.dumps(item[0], ensure_ascii=False))
        cnt = _count(item[0])
        if cur and (len(cur) >= MAX_BLOCKS_PER_REQUEST or cur_blocks + cnt > MAX_REQUEST_BLOCKS
                    or cur_bytes + size > MAX_REQUEST_BYTES):
            batches.append(cur)
            cur, cur_blocks, cur_bytes = [], 0, 0
        cur.append(item)
        cur_blocks += cnt
        cur_bytes += size
    if cur:
        batches.append(cur)
    return batches


def append_all(append: Callable[[str, list], dict], parent_id: str, blocks: List[dict]) -> int:
    """Append ``blocks`` under ``parent_id`` using ``append(parent_id, children) -> response``,
    descending into deferred children. Returns the number of requests made."""
    calls = 0
    for batch in plan_append(blocks):
        resp = append(parent_id, [b for b, _ in batch])
        calls += 1
        results = resp.get("results") or []
        for (b, deferred), created in zip(batch, results):
            if deferred and created.get("id"):
                calls += append_all(append, created["id"], deferred)
    return calls
