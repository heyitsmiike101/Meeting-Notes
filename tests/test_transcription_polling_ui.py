"""Regression contracts for the saved-transcriptions polling update."""

from meeting_notes.server.web import render_transcriptions_page


def test_polling_updates_row_status_without_replacing_row_or_losing_checkbox_focus():
    """A five-second refresh must preserve both keyboard context and row identity.

    Status changes (including Notes ready) alter the row class.  The refresh may
    replace its cells, but the row (``li``) itself must survive so screen readers and
    keyboard users do not experience a table-wide redraw.  If the selected
    checkbox was focused, focus must move to its replacement after the cell
    update.
    """
    page = render_transcriptions_page(token_configured=True)

    assert "class=\"mrow'+(status==='done'?' notes-ready'" in page
    assert "oldRow.replaceChildren.apply(oldRow" in page
    assert "oldRow.className=replacement.className" in page
    assert "var focusedCheckbox = document.activeElement" in page
    assert "document.activeElement.classList.contains('row-select')" in page
    assert "focusedRow.querySelector('.row-select').focus()" in page
