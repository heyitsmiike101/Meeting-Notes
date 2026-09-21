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
    assert "Build Meeting Notes" in page


def test_saved_transcriptions_offer_processing_checklist_and_bulk_actions():
    page = render_transcriptions_page(token_configured=True)
    assert 'id="transcription-checklist"' in page
    assert "Upload audio" in page
    assert "Transcribe recording" in page
    assert 'id="select-all"' in page
    assert 'id="bulk-build"' in page
    assert 'id="bulk-retranscribe"' in page
    assert 'id="bulk-delete"' in page
    assert "processingBadge(row)" in page
    assert "Build Meeting Notes" in page


def test_home_accepts_popular_recording_formats_and_uploads_to_api():
    from meeting_notes.server.web import render_home_page

    page = render_home_page(token_configured=True)
    assert 'id="recording-upload"' in page
    assert 'accept="audio/*,.mp3,.wav,.m4a,.flac,.ogg,.opus,.aac,.webm"' in page
    assert "xhr.open('POST', '/v1/uploads')" in page
    assert "xhr.upload.addEventListener('progress'" in page


def test_notes_render_as_one_markdown_document_and_offer_download():
    page = render_meeting_notes_page(token_configured=True)
    assert 'id="notes-markdown"' in page
    assert 'id="notes-document"' in page
    assert "renderNotesDocument(n,meta)" in page
    assert 'id="notes-download"' in page
    assert "buildMarkdown(n,meta)" in page
    assert "Empty sections" in page


def test_install_page_has_server_hosted_one_step_powershell_command():
    from meeting_notes.server.web import render_install_page

    page = render_install_page("http://meeting.lan", token_configured=False)
    assert "irm 'http://meeting.lan/install/client-agent.ps1' | iex" in page
