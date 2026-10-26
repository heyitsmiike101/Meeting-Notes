"""The scratchpad's Markdown renderer (meeting_notes/markdown.py), a port of Life Dash's: safe by construction."""

from __future__ import annotations

import re

import pytest

from meeting_notes import markdown as md
from meeting_notes import scratchpad as sp


def test_empty_and_non_text_render_nothing():
    assert md.render("") == ""
    assert md.render(None) == ""  # type: ignore[arg-type]


def test_user_html_is_escaped_never_emitted():
    out = md.render('<script>alert(1)</script> <img src=x onerror=alert(1)> & "q"')
    assert "<script" not in out and "<img" not in out
    assert "&lt;script&gt;" in out and "&amp;" in out
    # the only tags in the output are ones the renderer builds
    assert set(re.findall(r"</?([a-z0-9]+)", out)) <= {"p"}


@pytest.mark.parametrize("text", [
    "<b onclick=x>hi</b>", "[x](javascript:alert(1))", "[x](data:text/html,<script>1</script>)",
    '"><script>alert(1)</script>', "```\n<script>alert(1)</script>\n```", "| <i> |\n|---|\n| <u> |",
    "> <iframe src=//evil>", "- <svg onload=1>", "# <h1>x</h1>", "https://x.io/\"onmouseover=\"alert(1)",
])
def test_output_never_contains_raw_user_html(text):
    out = md.render(text)
    for bad in ("<script", "<iframe", "<svg", "<img", "<u>", "<i>", "<b ", "<h1", "onmouseover=\""):
        assert bad not in out
    assert "href=\"javascript:" not in out and "href=\"data:" not in out


def test_javascript_and_data_links_become_plain_text():
    out = md.render("[click](javascript:alert(1)) and [d](data:text/html;base64,AAA)")
    assert "<a " not in out and "href" not in out
    assert "[click](javascript:alert(1))" in out


def test_explicit_links_open_in_a_new_tab_safely():
    out = md.render('[Docs](https://example.com/a "the title") and [me](mailto:a@b.io) and [top](#x)')
    assert '<a href="https://example.com/a" target="_blank" rel="noopener noreferrer" title="the title">Docs</a>' in out
    assert 'href="mailto:a@b.io"' in out and 'href="#x"' in out


def test_bare_urls_autolink_with_the_scratchpad_tail_rule():
    out = md.render("see https://example.com.")
    assert '<a href="https://example.com" target="_blank" rel="noopener noreferrer">https://example.com</a>.' in out
    assert 'href="https://example.com"' in md.render("(https://example.com)")
    assert md.render("(https://example.com)").endswith("</a>)</p>")
    wiki = "https://en.wikipedia.org/wiki/Foo_(bar)"
    assert f'href="{wiki}"' in md.render(wiki)
    assert 'href="https://x.io/y"' in md.render("~~https://x.io/y~~") and "<s>" in md.render("~~https://x.io/y~~")
    assert "<a " not in md.render("ftp://nope.example https://.")


@pytest.mark.parametrize("text, expected", [
    ("a https://x.io/a, b", "https://x.io/a"),
    ("https://x.io/a?b=1&c=2.", "https://x.io/a?b=1&amp;c=2"),
    ('say "https://x.io/q"', "https://x.io/q"),
    ("HTTPS://Upper.example/Path!", "HTTPS://Upper.example/Path"),
])
def test_autolink_matches_find_links_and_escapes_the_href(text, expected):
    out = md.render(text)
    assert f'href="{expected}"' in out
    # the renderer and the plain-text rule agree on where each address ends
    assert [url.replace("&", "&amp;") for url in sp.find_links(text)] == [expected]


def test_headings_start_at_h2():
    out = md.render("# One\n## Two\n###### Six\n####### seven")
    assert "<h2>One</h2>" in out and "<h3>Two</h3>" in out and "<h6>Six</h6>" in out
    assert "<h1" not in out and "<p>####### seven</p>" in out


def test_emphasis():
    out = md.render("**bold** *it* ***both*** ~~gone~~ 2 \\* 3")
    assert "<strong>bold</strong>" in out and "<em>it</em>" in out
    assert "<strong><em>both</em></strong>" in out and "<s>gone</s>" in out
    assert "2 * 3" in out and "\\" not in out


def test_inline_and_fenced_code_stay_literal():
    out = md.render("run `**not bold** <b>` now")
    assert "<code>**not bold** &lt;b&gt;</code>" in out and "<strong>" not in out
    out = md.render("```python\nx = 1 < 2 & **y**\n```\nafter")
    assert '<pre class="lang-python" data-lang="python"><code>x = 1 &lt; 2 &amp; **y**</code></pre>' in out
    assert "<p>after</p>" in out
    # an unclosed fence still renders
    assert "<pre><code>half" in md.render("```\nhalf")


def test_lists_nest_and_switch():
    out = md.render("- a\n  - b\n- c\n\n1. one\n2) two")
    assert out.startswith("<ul>\n<li>a\n<ul>\n<li>b</li>\n</ul>\n</li>\n<li>c</li>\n</ul>")
    assert "<ol>\n<li>one</li>\n<li>two</li>\n</ol>" in out


def test_task_list_is_static():
    out = md.render("- [ ] open item\n- [x] done *item*")
    assert out.count('<li class="note-task">') == 2
    assert 'type="checkbox" disabled aria-label="open item">' in out
    assert 'type="checkbox" disabled checked aria-label="done *item*">' in out
    assert "<span>done <em>item</em></span>" in out
    assert "data-task" not in out and "hx-" not in out


def test_blockquote_holds_blocks_and_rules_and_hard_breaks():
    out = md.render("> quoted\n> - item\n\n---\n\nline one\\\nline two")
    assert "<blockquote><p>quoted</p>\n<ul>\n<li>item</li>\n</ul></blockquote>" in out
    assert "<hr>" in out and "<p>line one<br>line two</p>" in out


def test_table_with_alignment_and_a_pipe_in_a_cell():
    out = md.render("| Name | Qty | Note |\n|:--|--:|:-:|\n| a \\| b | **2** |\n| c | 3 | ok | extra |")
    assert '<table class="note-table"><thead><tr><th class="ta-left">Name</th><th class="ta-right">Qty</th>' in out
    assert '<th class="ta-center">Note</th>' in out
    assert '<td class="ta-left">a | b</td><td class="ta-right"><strong>2</strong></td><td class="ta-center"></td>' in out
    assert "extra" not in out  # long rows are cut to the header's width
    # without a delimiter row a pipe is just text
    assert "<table" not in md.render("a | b\nc | d")


def test_line_endings_and_paragraphs():
    assert md.render("one\r\ntwo\r\n\r\nthree") == "<p>one two</p>\n<p>three</p>"
