"""Markdown / notes -> Notion block conversion and request planning (no network)."""

from __future__ import annotations

import json

from meeting_notes.server import notion_blocks as nb
from meeting_notes.server.notion_api import normalize_page_id

import pytest


def kinds(blocks):
    return [b["type"] for b in blocks]


def text(b):
    return "".join(r["text"]["content"] for r in b[b["type"]]["rich_text"])


def test_headings_map_to_notion_levels():
    md = "# Top\n\n## Next\n\n### Deep\n\n#### Deeper\n\ntext"
    blocks = nb.markdown_to_blocks(md)
    assert kinds(blocks) == ["heading_2", "heading_3", "heading_3", "heading_3", "paragraph"]
    # inside a toggle the section headings are heading_2, so body headings start at heading_3
    assert kinds(nb.markdown_to_blocks(md, heading_base=3))[:2] == ["heading_3", "heading_3"]


def test_lists_nesting_numbers_and_todos():
    md = (
        "- one\n"
        "  - one-a\n"
        "    - one-a-i\n"
        "- two\n"
        "\n"
        "1. first\n"
        "2. second\n"
        "   - nested bullet\n"
        "- [ ] open task\n"
        "- [x] done task\n"
    )
    blocks = nb.markdown_to_blocks(md)
    assert kinds(blocks) == ["bulleted_list_item", "bulleted_list_item", "numbered_list_item",
                             "numbered_list_item", "to_do", "to_do"]
    first = blocks[0]["bulleted_list_item"]
    assert text(first["children"][0]) == "one-a"
    assert text(first["children"][0]["bulleted_list_item"]["children"][0]) == "one-a-i"
    assert blocks[3]["numbered_list_item"]["children"][0]["type"] == "bulleted_list_item"
    assert blocks[4]["to_do"]["checked"] is False and blocks[5]["to_do"]["checked"] is True


def test_quote_divider_code_paragraph_and_table():
    md = "> quoted\n> more\n\n---\n\n```python\nprint('x')\n```\n\nfirst line\nsecond line\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"
    blocks = nb.markdown_to_blocks(md)
    assert kinds(blocks) == ["quote", "divider", "code", "paragraph", "code"]
    assert blocks[0]["quote"]["rich_text"][0]["text"]["content"] == "quoted\nmore"
    assert blocks[2]["code"]["language"] == "python"
    assert text(blocks[3]) == "first line second line"
    assert blocks[4]["code"]["language"] == "plain text"  # the table keeps its columns


def test_inline_formatting_and_links():
    blocks = nb.markdown_to_blocks("A **bold** and *ital* and `code` and ~~gone~~ [site](https://example.com/a?b=1) end")
    items = blocks[0]["paragraph"]["rich_text"]
    by = {i["text"]["content"]: i for i in items}
    assert by["bold"]["annotations"] == {"bold": True}
    assert by["ital"]["annotations"] == {"italic": True}
    assert by["code"]["annotations"] == {"code": True}
    assert by["gone"]["annotations"] == {"strikethrough": True}
    assert by["site"]["text"]["link"] == {"url": "https://example.com/a?b=1"}
    # snake_case and a lone asterisk stay literal; unsafe links are text only
    plain = nb.markdown_to_blocks("my_var_name and 2 * 3 [x](javascript:alert(1))")
    assert text(plain[0]).startswith("my_var_name and 2 * 3 x")
    assert all("link" not in r["text"] for r in plain[0]["paragraph"]["rich_text"])


def test_nested_formatting():
    items = nb.markdown_to_blocks("**bold with *italic* inside**")[0]["paragraph"]["rich_text"]
    assert [i["text"]["content"] for i in items] == ["bold with ", "italic", " inside"]
    assert items[1]["annotations"] == {"bold": True, "italic": True}


def test_long_text_is_split_under_2000_chars_per_item():
    long = ("word " * 1200).strip()  # 5999 chars
    block = nb.markdown_to_blocks(long)[0]
    items = block["paragraph"]["rich_text"]
    assert len(items) >= 3 and all(len(i["text"]["content"]) <= 2000 for i in items)
    assert "".join(i["text"]["content"] for i in items) == long
    unbroken = nb.markdown_to_blocks("x" * 4500)[0]["paragraph"]["rich_text"]
    assert [len(i["text"]["content"]) for i in unbroken] == [2000, 2000, 500]
    code = nb.markdown_to_blocks("```\n" + "y" * 4100 + "\n```")[0]["code"]["rich_text"]
    assert all(len(i["text"]["content"]) <= 2000 for i in code)


def test_more_than_100_rich_text_items_spill_into_extra_blocks():
    md = " ".join(f"**b{i}** x" for i in range(120))
    blocks = nb.markdown_to_blocks(md)
    assert len(blocks) >= 2 and all(len(b[b["type"]]["rich_text"]) <= 100 for b in blocks)


def test_notes_layout_mirrors_the_web_view():
    payload = {
        "title": "T", "summary": "The **summary**.", "meeting_notes": "## Part\n\nBody text",
        "participants": ["Mike"], "key_points": ["kp"], "decisions": ["d1", "d2"],
        "action_items": [{"action": "Do it", "owner": "Sarah", "due_date": "2026-10-01", "context": "why"},
                         {"action": "Plain", "owner": None, "due_date": None}],
        "open_questions": ["q"], "risks": [], "next_steps": ["n"],
    }
    blocks = nb.notes_to_blocks(payload, meeting_url="http://meeting.lan/sessions/abc")
    heads = [text(b) for b in blocks if b["type"] == "heading_2"]
    assert heads == ["Summary", "Decisions", "Action items", "Key points", "Meeting notes",
                     "Participants", "Open questions", "Next steps"]  # empty Risks omitted
    todos = [b for b in blocks if b["type"] == "to_do"]
    assert [b["to_do"]["checked"] for b in todos] == [False, False]
    assert "Owner: Sarah" in text(todos[0]) and "Due: 2026-10-01" in text(todos[0]) and "why" in text(todos[0])
    last = blocks[-1]["paragraph"]["rich_text"][0]
    assert last["text"]["link"]["url"] == "http://meeting.lan/sessions/abc"
    assert not any(b["type"] == "heading_1" for b in blocks)


def test_notes_without_a_public_url_have_no_link_paragraph():
    blocks = nb.notes_to_blocks({"summary": "s"})
    assert blocks[-1]["type"] == "paragraph" and "link" not in json.dumps(blocks[-1])


def _nested(depth):
    b = {"object": "block", "type": "paragraph", "paragraph": {"rich_text": []}}
    for _ in range(depth):
        b = {"object": "block", "type": "bulleted_list_item",
             "bulleted_list_item": {"rich_text": [], "children": [b]}}
    return b


def test_plan_append_batches_at_100_blocks():
    blocks = [nb.block("paragraph", f"p{i}") for i in range(250)]
    batches = nb.plan_append(blocks)
    assert [len(b) for b in batches] == [100, 100, 50]


def test_plan_append_defers_deep_nesting():
    deep = _nested(4)
    (batch,) = nb.plan_append([deep])
    sent, deferred = batch[0]
    assert "children" not in sent[sent["type"]] and len(deferred) == 1
    shallow = nb.plan_append([_nested(1)])[0][0]
    assert shallow[1] == [] and shallow[0][shallow[0]["type"]]["children"]


def test_append_all_descends_into_deferred_children():
    calls = []

    def append(parent, children):
        calls.append((parent, children))
        return {"results": [{"id": f"{parent}/{i}"} for i, _ in enumerate(children)]}

    n = nb.append_all(append, "root", [_nested(4)])
    assert n == len(calls) and n >= 3
    assert calls[1][0] == "root/0"  # the deferred level goes under the created block


def test_more_than_100_children_across_requests():
    blocks = [nb.block("bulleted_list_item", f"i{i}") for i in range(230)]
    sizes = []
    nb.append_all(lambda p, c: sizes.append(len(c)) or {"results": [{"id": str(i)} for i in range(len(c))]},
                  "p", blocks)
    assert sizes == [100, 100, 30]


@pytest.mark.parametrize("value,expected", [
    ("https://www.notion.so/Notes-Home-0123456789abcdef0123456789abcdef", "0123456789abcdef0123456789abcdef"),
    ("https://www.notion.so/workspace/Page-Title-0123456789ABCDEF0123456789ABCDEF?pvs=4",
     "0123456789abcdef0123456789abcdef"),
    ("01234567-89ab-cdef-0123-456789abcdef", "0123456789abcdef0123456789abcdef"),
    ("0123456789abcdef0123456789abcdef", "0123456789abcdef0123456789abcdef"),
    ("notion.so/0123456789abcdef0123456789abcdef", "0123456789abcdef0123456789abcdef"),
])
def test_normalize_page_id(value, expected):
    assert normalize_page_id(value) == expected


@pytest.mark.parametrize("value", ["", "   ", "not a page", "https://evil.example.com/0123456789abcdef0123456789abcdef",
                                   "https://www.notion.so/no-id-here", "1234"])
def test_normalize_page_id_rejects(value):
    with pytest.raises(ValueError):
        normalize_page_id(value)
