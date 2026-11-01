"""The source text of the page renderers: ``server/web.py`` plus every ``server/webpages/*.py`` module.

Some tests assert on inline JavaScript that lives inside those Python strings.
"""

from __future__ import annotations

from pathlib import Path

from meeting_notes.server import web


def web_source() -> str:
    root = Path(web.__file__)
    paths = [root, *sorted(root.with_name("webpages").glob("*.py"))]
    return "".join(p.read_text(encoding="utf-8") for p in paths)
