from meeting_notes.server.web import render_meeting_notes_page, render_transcriptions_page
from meeting_notes import __version__


def test_sidebar_and_meeting_notes_page_are_present():
    page = render_meeting_notes_page(token_configured=True)
    assert 'href="/meeting-notes"' in page
    assert "GET /v1/meeting-notes" not in page  # endpoint is used by fetch, not prose
    assert "fetch('/v1/meeting-notes?page=" in page
    assert "fetch('/v1/meeting-notes/'+encodeURIComponent(id)" in page
    assert "summary" in page
    assert 'id="notes-narrative"' in page
    assert 'id="notes-decisions"' in page
    assert "Action items" in page
    assert "Open questions" in page
    assert "Risks" in page
    assert "Next steps" in page
    assert 'id="notes-participants"' in page
    assert 'href="/v1/bridge/workflow.md"' in page
    assert "<details class=\"card\">" in page
    assert f"v{__version__}" in page
    assert 'aria-label="Meeting Notes version"' in page


def test_meeting_notes_actions_and_safe_model_rendering():
    page = render_meeting_notes_page(token_configured=True)
    assert "'/retry'" in page
    assert "method:'POST'" in page
    assert "escapeHtml" in page
    assert "textContent=n.summary" in page
    assert "textContent=n.polished_meeting_notes" in page
    assert "setList('notes-decisions',n.decisions" in page
    assert "setList('notes-risks'" in page
    assert "setList('notes-next-steps'" in page
    assert "textContent=itemText(item)" in page
    # Model output must be escaped or assigned as text, never trusted HTML.
    assert "innerHTML=n.summary" not in page


def test_saved_transcription_can_queue_review():
    page = render_transcriptions_page(token_configured=True)
    assert 'id="queue-review"' in page
    assert 'id="review-status"' in page
    assert "action('/review')" in page
    assert "Queue for review" in page
