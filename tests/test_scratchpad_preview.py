"""The recorder's "Your notes" pane can show the text rendered the way the meeting page renders it."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")


@pytest.fixture
def view():
    from PySide6.QtWidgets import QApplication

    from meeting_notes.client.ui.scratchpad import ScratchpadModel, ScratchpadView

    QApplication.instance() or QApplication([])
    model = ScratchpadModel(lambda text: True)
    return ScratchpadView(model, closable=False)


def _pump(seconds: float = 1.0) -> None:
    from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer

    loop = QEventLoop()
    QTimer.singleShot(int(seconds * 1000), loop.quit)
    loop.exec()
    QCoreApplication.processEvents()


def test_preview_renders_markdown_like_the_meeting_page(view):
    view.model.set_text("# Agenda\n\n- [x] intro\n- [ ] pricing https://x.io/deck.\n\n**bold** <script>x</script>")
    assert not view.previewing and view.preview_button.text() == "Preview"
    view.preview_button.setChecked(True)
    assert view.previewing and view.preview_button.text() == "Edit"
    html = view.preview.toHtml()
    assert "Agenda" in html and "☑" in html and "☐" in html
    assert 'href="https://x.io/deck"' in html and "<script>" not in html
    assert "bold" in html and "&lt;script&gt;" in html
    assert view.hint.text().startswith("Rendered as the meeting page")


def test_a_showing_preview_follows_edits_made_elsewhere(view):
    view.model.set_text("first")
    view.set_previewing(True)
    assert "first" in view.preview.toPlainText()
    view.model.set_text("second draft")  # the pop-out and the pane share the document
    _pump(0.8)
    assert "second draft" in view.preview.toPlainText()


def test_back_to_edit_focuses_the_editor_and_keeps_the_text(view):
    view.model.set_text("keep me")
    view.set_previewing(True)
    view.set_previewing(False)
    assert not view.previewing and view.pages.currentWidget() is view.editor
    assert view.model.text() == "keep me" and not view.preview_button.isChecked()


def test_preview_html_turns_task_boxes_into_characters():
    from meeting_notes.client.ui.scratchpad import preview_html

    out = preview_html("- [x] a\n- [ ] b")
    assert "<input" not in out and out.count("☑") == 1 and out.count("☐") == 1
