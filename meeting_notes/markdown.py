"""A small, safe Markdown renderer for the notes scratchpad ("Your notes" on the meeting page).

A port of Life Dash's ``app/services/markdown.py`` (same subset, same output shapes, same escaping rules), trimmed to
what a scratchpad needs: no #tags, note refs, @mentions, attachment chips, link-preview cards or htmx attributes, and
task boxes are static (the text is never written back). Why not a parser library: this app keeps its dependencies
few, the subset typed in a meeting is small enough to own, and owning it means the escaping rules are ours rather than
a config flag on somebody else's parser.

**Everything is escaped first**, and only then are the Markdown constructs turned into tags, so no input can emit an
element or attribute this module did not build itself. URLs are scheme-checked before they reach an href. Bare links
use the same tail rule as :mod:`meeting_notes.scratchpad` (GFM's autolink rule), which is reused, not copied.
"""

from __future__ import annotations

import html
import re
from typing import Dict, List, Optional, Tuple

from .scratchpad import _split_url_tail as _split_plain_tail

# http(s), mailto and in-page/relative links. javascript:, data:, vbscript: and the rest render as plain text: the note
# still reads correctly, it just is not a link.
_SAFE_URL = re.compile(r"^(https?://|/|mailto:|#)", re.IGNORECASE)

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_TASK = re.compile(r"^(\s*)[-*+]\s+\[([ xX])\]\s+(.*)$")
_ORDERED = re.compile(r"^(\s*)(\d+)[.)]\s+(.*)$")
_QUOTE = re.compile(r"^>\s?(.*)$")
_RULE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_FENCE = re.compile(r"^\s*```(\w*)\s*$")
# A backslash before a line-start trigger character makes it plain text ("\# not a heading").
_ESCAPED_LINE_START = re.compile(r"^\\([#\-*+>`_\d])")
# The same in the middle of a line ("2 \* 3"); wider than the line-start set because any CommonMark punctuation may
# be escaped and none of it should reach the reader as a backslash.
_ESCAPED_INLINE = re.compile(r"\\([\\`*~\[\]()#!+.>_-])")

# Matched as &quot; (not a quote character) because _inline escapes the whole line before any of these run.
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)(?:\s+&quot;(.*?)&quot;)?\)")
# A bare URL in prose: not after `="` (an attribute), `">` (the label of an anchor already built) or `](` (explicit
# link syntax that did not parse). Where it ENDS is decided by _split_url_tail, not by this class.
_BARE_URL = re.compile(r"(?<![\"=])(?<!\">)(?<!\]\()\bhttps?://[^\s<>\"']+", re.IGNORECASE)
_URL_HAS_HOST = re.compile(r"^https?://[^\s/]", re.IGNORECASE)

_CODE = re.compile(r"`([^`]+)`")
# Bold-italic before bold, or the non-greedy _BOLD would take "**" + "*x". A delimiter pair may contain a whole tag
# but never straddle one (a match that closes inside an href would produce broken HTML).
_BOLD_ITALIC = re.compile(r"\*\*\*((?:[^<>]|<[^<>]*>)+?)\*\*\*")
_BOLD = re.compile(r"\*\*((?:[^<>]|<[^<>]*>)+?)\*\*")
_ITALIC = re.compile(r"(?<![\*\w])\*(?!\s)((?:[^*<>]|<[^<>]*>)+?)(?<!\s)\*(?!\*)")
_STRIKE = re.compile(r"~~((?:[^<>]|<[^<>]*>)+?)~~")

# Inline code and escaped characters are parked behind this sentinel while the emphasis and link passes run.
_SENTINEL = "\x00CODE{}\x00"

# _inline escapes before it finds URLs, so a URL can end in an entity: "&quot;" is a closing quote (sentence
# punctuation), "&amp;" a literal "&" the address owns. Each is swapped for a stand-in character the plain tail rule
# leaves alone, the plain rule runs, and the entities are put back.
_ENTITY = re.compile(r"&quot;|&#x27;|&#39;|&amp;")
_QUOTE_MARK = "\x01"
_AMP_MARK = "\x02"


def _safe_url(url: str) -> Optional[str]:
    url = url.strip()
    return url if _SAFE_URL.match(url) else None


def _split_url_tail(url: str) -> Tuple[str, str]:
    """``scratchpad._split_url_tail`` for escaped text: (address, sentence punctuation riding on its end)."""
    entities: List[str] = []

    def mark(match: "re.Match[str]") -> str:
        entities.append(match.group(0))
        return _AMP_MARK if match.group(0) == "&amp;" else _QUOTE_MARK

    work = _ENTITY.sub(mark, url)
    tail = ""
    while True:
        work, peeled = _split_plain_tail(work)
        tail = peeled + tail
        if not work.endswith(_QUOTE_MARK):
            break
        work, tail = work[:-1], _QUOTE_MARK + tail

    # the stand-ins were numbered left to right over the whole URL: put them back address first, then tail
    pending = iter(entities)
    address, tail = (
        "".join(next(pending) if ch in (_QUOTE_MARK, _AMP_MARK) else ch for ch in part) for part in (work, tail)
    )
    return address, tail


def _inline(text: str) -> str:
    """Escape, then apply inline Markdown. Order matters and is load-bearing."""
    text = html.escape(text, quote=True)
    stash: List[str] = []

    def park(fragment: str) -> str:
        stash.append(fragment)
        return _SENTINEL.format(len(stash) - 1)

    # An escaped character is put beyond the reach of every pass below (and the backslash is consumed).
    text = _ESCAPED_INLINE.sub(lambda m: park(m.group(1)), text)
    text = _CODE.sub(lambda m: park(f"<code>{m.group(1)}</code>"), text)

    def link(match: "re.Match[str]") -> str:
        url = _safe_url(match.group(2))
        if url is None:
            return match.group(0)
        title = f' title="{match.group(3)}"' if match.group(3) else ""
        return f'<a href="{url}" target="_blank" rel="noopener noreferrer"{title}>{match.group(1)}</a>'

    def bare(match: "re.Match[str]") -> str:
        url, tail = _split_url_tail(match.group(0))
        if not _URL_HAS_HOST.match(url):  # "https://." is a scheme and a full stop, not an address
            return match.group(0)
        return f'<a href="{url}" target="_blank" rel="noopener noreferrer">{url}</a>{tail}'

    text = _LINK.sub(link, text)
    # Emphasis BEFORE the bare-URL pass: "~~https://x/y~~" becomes <s>..</s> first and the address is then linked
    # inside it, rather than the autolink swallowing the closing "~~" into the href.
    text = _BOLD_ITALIC.sub(r"<strong><em>\1</em></strong>", text)
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    text = _STRIKE.sub(r"<s>\1</s>", text)
    text = _ITALIC.sub(r"<em>\1</em>", text)
    text = _BARE_URL.sub(bare, text)

    # Backwards: a fragment parked late can contain a sentinel parked early (a link label with `code` in it).
    for i in range(len(stash) - 1, -1, -1):
        text = text.replace(_SENTINEL.format(i), stash[i])
    return text


# ------------------------------------------------------------------- tables
# Pipe tables. Strict on purpose: a line with a pipe is usually just a line with a pipe, and the delimiter row (with a
# matching cell count) is what makes it a table.

_DELIM_CELL = re.compile(r"^\s*:?-{1,}:?\s*$")
_MAX_TABLE_COLS = 32
_MAX_TABLE_ROWS = 200


def _split_row(raw: str) -> List[str]:
    r"""One table row into its cells. Outer pipes are optional; ``\|`` is a pipe inside a cell."""
    text = raw.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|") and not text.endswith("\\|"):
        text = text[:-1]
    cells: List[str] = []
    current: List[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text) and text[i + 1] == "|":
            current.append("|")
            i += 2
            continue
        if ch == "|":
            cells.append("".join(current).strip())
            current = []
            i += 1
            continue
        current.append(ch)
        i += 1
    cells.append("".join(current).strip())
    return cells


def _is_table_row(raw: str) -> bool:
    return bool(re.search(r"(?<!\\)\|", raw))


def _alignments(raw: str, columns: int) -> Optional[List[str]]:
    """The alignment class of each column, or None if ``raw`` is not a delimiter row of ``columns`` cells."""
    cells = _split_row(raw)
    if len(cells) != columns:
        return None
    out = []
    for cell in cells:
        if not _DELIM_CELL.match(cell):
            return None
        cell = cell.strip()
        if cell.startswith(":") and cell.endswith(":"):
            out.append("center")
        elif cell.endswith(":"):
            out.append("right")
        elif cell.startswith(":"):
            out.append("left")
        else:
            out.append("")
    return out


def _table_html(header: List[str], aligns: List[str], rows: List[List[str]]) -> str:
    def cell(tag: str, text: str, align: str) -> str:
        css = f' class="ta-{align}"' if align else ""  # a class, never an inline style
        return f"<{tag}{css}>{_inline(text)}</{tag}>"

    width = len(header)
    out = ['<table class="note-table"><thead><tr>']
    out += [cell("th", text, aligns[i]) for i, text in enumerate(header)]
    out.append("</tr></thead><tbody>")
    for row in rows:
        padded = (row + [""] * width)[:width]  # pad short rows, drop long ones: a half-typed table is normal
        out.append("<tr>")
        out += [cell("td", text, aligns[i]) for i, text in enumerate(padded)]
        out.append("</tr>")
    out.append("</tbody></table>")
    return "".join(out)


_HARD_MARK = "\x1f"  # whitespace-class, so a URL or emphasis run stops at it


def _is_hard(line: str) -> bool:
    """True when ``line`` ends in an odd number of backslashes: a CommonMark hard break."""
    stripped = line.rstrip()
    return (len(stripped) - len(stripped.rstrip("\\"))) % 2 == 1


def _join_lines(lines: List[str]) -> str:
    """Join the lines of one paragraph or list item: a hard-break backslash becomes _HARD_MARK (-> <br> after
    _inline), any other line end a space."""
    out = ""
    for i, line in enumerate(lines):
        last = i == len(lines) - 1
        hard = not last and _is_hard(line)
        out += line.rstrip()[:-1] if hard else line
        if not last:
            out += _HARD_MARK if hard else " "
    return out


def _rich(text: str) -> str:
    return _inline(text).replace(_HARD_MARK, "<br>")


def _blocks(lines: List[str]) -> str:
    """Render one run of block-level lines (the whole text, or the lines inside a blockquote, recursively)."""
    out: List[str] = []
    list_stack: List[str] = []  # "ul"/"ol" per open level
    indents: List[int] = []
    owes_li: List[bool] = []  # whether closing that level also closes the <li> it sits in
    para: List[str] = []
    in_code = False
    code_lang = ""
    code_buf: List[str] = []
    last_li: Dict = {}  # the most recent <li>, so a hard-break continuation line can extend it

    def flush_para() -> None:
        if para:
            out.append(f"<p>{_rich(_join_lines(para))}</p>")
            para.clear()

    def li_html(item: Dict) -> str:
        text = _join_lines(item["lines"])
        body = _rich(text)
        if item["task"] is None:
            return f"<li>{body}</li>"
        label = html.escape(text.replace(_HARD_MARK, " "), quote=True)
        return (
            f'<li class="note-task"><input type="checkbox" disabled{item["task"]} aria-label="{label}">'
            f"<span>{body}</span></li>"
        )

    def emit_li(text: str, task: Optional[str] = None) -> None:
        item = {"lines": [text], "task": task}
        out.append(li_html(item))
        last_li.clear()
        last_li.update(item=item, idx=len(out) - 1)

    def continues_li(raw: str) -> bool:
        """A line right after a list item that ended in a hard-break backslash belongs to that item."""
        if not last_li or last_li["idx"] != len(out) - 1 or in_code:
            return False
        if not _is_hard(last_li["item"]["lines"][-1]):
            return False
        line = raw.strip()
        if not line:
            return False
        if line == "\\" or _ESCAPED_LINE_START.match(line):
            return True
        return not (
            _BULLET.match(line) or _ORDERED.match(line) or _HEADING.match(line) or _QUOTE.match(line)
            or _RULE.match(line) or _FENCE.match(line) or _is_table_row(line)
        )

    def close_one() -> None:
        out.append(f"</{list_stack.pop()}>")
        indents.pop()
        if owes_li.pop():
            out.append("</li>")

    def close_lists() -> None:
        while list_stack:
            close_one()

    def close_all() -> None:
        flush_para()
        close_lists()

    def open_list(tag: str, indent: int) -> None:
        """Open, close or switch list levels so ``indent`` is the current one (indentation is compared, not counted)."""
        while list_stack and indents[-1] > indent:
            close_one()
        if list_stack and indents[-1] == indent:
            if list_stack[-1] == tag:
                return
            carried = owes_li.pop()  # switching ul <-> ol at the same depth: the enclosing <li> carries across
            out.append(f"</{list_stack.pop()}>")
            indents.pop()
        else:
            # Going deeper: a nested list is a child of the item above, so take that item's closing tag back.
            carried = bool(list_stack) and bool(out) and out[-1].endswith("</li>")
            if carried:
                out[-1] = out[-1][: -len("</li>")]
        out.append(f"<{tag}>")
        list_stack.append(tag)
        indents.append(indent)
        owes_li.append(carried)

    skip_to = 0
    for index, raw in enumerate(lines):
        if index < skip_to:
            continue
        if continues_li(raw):
            line = raw.strip()
            if line != "\\" and _ESCAPED_LINE_START.match(line):
                line = line[1:]
            last_li["item"]["lines"].append(line)
            out[last_li["idx"]] = li_html(last_li["item"])
            continue
        if _ESCAPED_LINE_START.match(raw) and not in_code:
            if list_stack:
                close_lists()
            para.append(raw[1:].strip())  # the backslash is consumed, never rendered
            continue

        fence = _FENCE.match(raw)
        if fence:
            if in_code:
                safe_lang = html.escape(code_lang)
                lang = f' class="lang-{safe_lang}" data-lang="{safe_lang}"' if code_lang else ""
                out.append(f"<pre{lang}><code>{html.escape(chr(10).join(code_buf))}</code></pre>")
                code_buf.clear()
                in_code = False
            else:
                close_all()
                in_code = True
                code_lang = fence.group(1)
            continue
        if in_code:
            code_buf.append(raw)
            continue

        if raw.strip() == "\\" and para and _is_hard(para[-1]):
            para.append("\\")  # an empty line inside a paragraph that is mid hard-break
            continue
        if raw.strip() == "\\":
            close_all()  # a deliberate blank line
            out.append('<p class="note-blank"><br></p>')
            continue
        if not raw.strip():
            close_all()
            continue
        if _RULE.match(raw):
            close_all()
            out.append("<hr>")
            continue

        # A table, if the line after this one is a delimiter row (a row of pipes inside a fence is code, handled above).
        if _is_table_row(raw) and index + 1 < len(lines):
            header = _split_row(raw)
            aligns = _alignments(lines[index + 1], len(header))
            if aligns is not None and len(header) <= _MAX_TABLE_COLS:
                rows: List[List[str]] = []
                cursor = index + 2
                while (
                    cursor < len(lines) and lines[cursor].strip() and _is_table_row(lines[cursor])
                    and len(rows) < _MAX_TABLE_ROWS
                ):
                    rows.append(_split_row(lines[cursor]))
                    cursor += 1
                if len(rows) < _MAX_TABLE_ROWS:  # a table past the ceiling stays the text it was
                    close_all()
                    out.append(_table_html(header, aligns, rows))
                    skip_to = cursor
                    continue

        heading = _HEADING.match(raw)
        if heading:
            close_all()
            level = min(len(heading.group(1)) + 1, 6)  # "# " is h2: the page's own title owns h1
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            continue

        if _QUOTE.match(raw):
            close_all()
            inner: List[str] = []
            cursor = index
            while cursor < len(lines):
                quoted = _QUOTE.match(lines[cursor])
                if not quoted:
                    break
                inner.append(quoted.group(1))
                cursor += 1
            out.append(f"<blockquote>{_blocks(inner)}</blockquote>")
            skip_to = cursor
            continue

        task = _TASK.match(raw)
        if task:
            flush_para()
            open_list("ul", len(task.group(1)))
            emit_li(task.group(3), " checked" if task.group(2).lower() == "x" else "")
            continue
        bullet = _BULLET.match(raw)
        if bullet:
            flush_para()
            open_list("ul", len(bullet.group(1)))
            emit_li(bullet.group(2))
            continue
        ordered = _ORDERED.match(raw)
        if ordered:
            flush_para()
            open_list("ol", len(ordered.group(1)))
            emit_li(ordered.group(3))
            continue

        if list_stack:
            close_lists()  # a plain line straight after a list ends it (items are flat, no lazy continuation)
        para.append(raw.strip())

    if in_code:  # an unclosed fence still renders: half-typed notes are the normal state
        out.append(f"<pre><code>{html.escape(chr(10).join(code_buf))}</code></pre>")
    close_all()
    return "\n".join(out)


def render(text: str) -> str:
    """Markdown -> HTML. Never returns anything it did not build itself; '' for empty or non-text input."""
    if not isinstance(text, str) or not text:
        return ""
    return _blocks(text.replace("\r\n", "\n").replace("\r", "\n").split("\n"))
