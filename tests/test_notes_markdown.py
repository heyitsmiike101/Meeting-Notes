"""The notes view renders AI Markdown through a small safe renderer (web.py ``_JS_HELPERS``), run here in node."""
import json
import re
import shutil
import subprocess

import pytest

from meeting_notes.server import web

node = shutil.which("node")
pytestmark = pytest.mark.skipif(node is None, reason="node is not installed")


def _run(cases):
    """Evaluate the page's escapeHtml + Markdown renderer in node; returns one HTML string per case."""
    src = web._JS_HELPERS
    start = src.index("function escapeHtml")
    end = src.index("function mdBlock")
    end = src.index("\n", end) + 1
    code = src[start:end] + "\nvar cases=" + json.dumps(cases) + ";process.stdout.write(JSON.stringify(cases.map(function(c){return c[0]==='inline'?mdInline(c[1]):renderMarkdown(c[1]);})));"
    result = subprocess.run([node, "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def md(text):
    return _run([["block", text]])[0]


def test_paragraphs_and_line_breaks():
    assert md("One\nstill one\n\nTwo") == "<p>One still one</p><p>Two</p>"
    assert md("Hard  \nbreak") == "<p>Hard<br>break</p>"
    assert md("") == ""


def test_headings_map_below_section_h3():
    assert md("### Session A\n\ntext") == "<h4>Session A</h4><p>text</p>"
    assert md("## Top\n### Sub\n#### Deep") == "<h4>Top</h4><h5>Sub</h5><h6>Deep</h6>"
    assert md("#### Only") == "<h4>Only</h4>"


def test_lists_nested_and_ordered():
    assert md("- a\n- b\n  - b1\n  - b2\n- c") == "<ul><li>a</li><li>b<ul><li>b1</li><li>b2</li></ul></li><li>c</li></ul>"
    assert md("* x\n+ y") == "<ul><li>x</li><li>y</li></ul>"
    assert md("1. one\n2. two\n   - sub") == "<ol><li>one</li><li>two<ul><li>sub</li></ul></li></ol>"
    assert md("intro\n- a\n\nafter") == "<p>intro</p><ul><li>a</li></ul><p>after</p>"


def test_bold_lead_in_and_inline_styles():
    out = md("**Opening remarks.** The speaker began.\n\n**Standalone heading**\n\nUse `a < b` and *em* and _em2_ and **bold**.")
    assert '<p class="md-lead"><strong>Opening remarks.</strong> The speaker began.</p>' in out
    assert '<p class="md-sub"><strong>Standalone heading</strong></p>' in out
    assert "<code>a &lt; b</code>" in out and "<em>em</em>" in out and "<em>em2</em>" in out and "<strong>bold</strong>" in out


def test_snake_case_and_math_are_not_italicised():
    assert "<em>" not in md("call my_var_name and 2 * 3 * 4")


def test_safe_links_only():
    out = md("[site](https://example.com/a?x=1&y=2) and [bad](javascript:alert(1)) and [data](data:text/html,x)")
    assert '<a href="https://example.com/a?x=1&amp;y=2" rel="noopener noreferrer" target="_blank">site</a>' in out
    assert "<a " in out and out.count("<a ") == 1
    assert "[bad](javascript:alert(1))" in out


def test_link_url_is_not_mangled_by_emphasis():
    out = md("[x](https://e.com/_a_b_/*c*/d)")
    assert 'href="https://e.com/_a_b_/*c*/d"' in out


def test_raw_html_is_escaped():
    out = md('<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>\n\n- <b onclick="x">hi</b>\n\n### <i>h</i>')
    assert "<script" not in out and "<img" not in out and "<b " not in out and "<i>" not in out
    assert "&lt;script&gt;" in out and "&lt;img src=x onerror=alert(1)&gt;" in out


def test_ampersands_and_quotes_escaped():
    out = md("Tom & Jerry said \"hi\" and 'bye'")
    assert out == "<p>Tom &amp; Jerry said &quot;hi&quot; and &#39;bye&#39;</p>"


def test_link_label_cannot_break_out_of_attribute():
    out = md('[a"onmouseover="x](https://e.com/"onmouseover="y)')
    assert out.count('"') == 6  # only the href/rel/target attribute delimiters; user quotes stay entities
    assert out.count("<a ") == 1


def test_inline_mode_for_list_items():
    (html,) = _run([["inline", "**Bold** <b>x</b> `c`"]])
    assert html == "<strong>Bold</strong> &lt;b&gt;x&lt;/b&gt; <code>c</code>"


def test_realistic_webinar_notes():
    out = md("Intro para.\n\n### Session 1 — Ana Lopez\n\n- Point **one**\n- Point two\n  - detail\n\nClosing.")
    assert out == ("<p>Intro para.</p><h4>Session 1 — Ana Lopez</h4>"
                   "<ul><li>Point <strong>one</strong></li><li>Point two<ul><li>detail</li></ul></li></ul><p>Closing.</p>")


def test_pages_use_renderer_for_summary_and_body():
    page = web.render_transcriptions_page(token_configured=True)
    assert "function renderMarkdown(" in page and "function mdInline(" in page
    assert "list(s[1],s[3],s[0]==='Summary'||s[0]==='Meeting notes')" in page
    assert "if(prose)return mdBlock(" in page
    assert "mdInline(label)" in page and "mdInline(action.context)" in page
    legacy = web.render_meeting_notes_page(token_configured=True)
    assert "getElementById('notes-narrative').innerHTML=narrative?mdBlock(narrative)" in legacy
    assert "getElementById('notes-summary').innerHTML=mdBlock(" in legacy
    assert "notes-narrative').textContent" not in legacy


def test_download_markdown_is_still_raw():
    page = web.render_transcriptions_page(token_configured=True)
    m = re.search(r"function buildMarkdown\(note,title\)\{.*", page)
    assert m and "mdBlock" not in m.group(0).split("\n")[0] and "renderMarkdown" not in m.group(0).split("\n")[0]


def test_markdown_css_present():
    css = web.stylesheet_text()
    for rule in (".md h4", ".md ul", ".md strong", ".md code", ".md p.md-lead"):
        assert rule in css


def test_notes_page_scripts_parse(tmp_path):
    """The renderer is spliced into Python strings; a bad escape would silently break the whole page script."""
    pages = {
        "meetings": web.render_transcriptions_page(token_configured=True),
        "legacy-notes": web.render_meeting_notes_page(token_configured=True),
    }
    for name, page in pages.items():
        for i, script in enumerate(re.findall(r"<script>(.*?)</script>", page, re.S)):
            path = tmp_path / f"{name}-{i}.js"
            path.write_text(script, encoding="utf-8")
            result = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
            assert result.returncode == 0, f"{name} script {i}: {result.stderr[:400]}"
