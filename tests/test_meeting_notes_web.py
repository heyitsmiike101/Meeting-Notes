from meeting_notes.server.web import render_meeting_notes_page, render_transcriptions_page
from meeting_notes import __version__


def test_sidebar_and_meeting_notes_page_are_present():
    page = render_meeting_notes_page(token_configured=True)
    assert 'href="/meetings"' in page
    assert 'href="/meeting-notes"' not in page
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
    assert "e.target===this||e.target.classList.contains('overlay-inner'))closeNote()" in page
    assert "notes-overlay').classList.contains('open')" in page


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
    assert 'accept="audio/*,.mp3,.wav,.m4a,.mp4,.flac,.ogg,.oga,.opus,.aac,.webm"' in page
    assert "xhr.open('POST', '/v1/uploads')" in page
    assert "xhr.upload.addEventListener('progress'" in page


def test_transcription_detail_tracks_upload_and_partial_bulk_results():
    page = render_transcriptions_page(token_configured=True)
    assert "upload.state==='pending' || upload.state==='uploading'" in page
    assert "Pending end of meeting" in page
    assert "Promise.allSettled(ids.map" in page
    assert "if(failed.length)alert(failed.length+' of '+ids.length+' actions failed.')" in page
    assert "if (currentSession !== id || request !== detailRequest) return;" in page
    assert "if(currentSession===id)openSession(id)" in page
    assert "Processing status · reconnecting…" in page
    assert "transcribeQueued=uploadComplete&&(transcribeState==='queued'||transcribeState==='pending')" in page
    assert "Keep existing row nodes during polling" in page
    assert "oldRow.replaceChildren.apply(oldRow" in page
    assert "if(!refreshed)alert('Could not refresh the meetings list." in page


def test_transcription_overlay_contains_the_meeting_notes_view_and_name_editing():
    page = render_transcriptions_page(token_configured=True)
    assert 'id="notes-pane" hidden' in page
    assert 'id="notes-document-pane" hidden' in page
    assert 'id="show-transcript"' in page
    assert "function showNotes(refresh)" in page
    assert "fetch('/v1/meeting-notes/'+encodeURIComponent(review)" in page
    assert 'id="edit-meeting-name"' in page
    assert 'id="edit-summary-name"' in page
    assert "method:'PATCH'" in page
    assert "e.target===this||e.target.classList.contains('overlay-inner'))closeOverlay()" in page
    assert "overlay.classList.contains('open')" in page
    assert 'id="notes-download"' in page
    assert "function buildMarkdown(note,title)" in page
    assert "value.task" in page
    assert "notesPollTimer=setTimeout(function(){if(session===currentSession" in page
    assert "if(session!==currentSession||review!==currentReview||request!==notesRequest)return;" in page
    # Preserve the count before a polling refresh so the existing table rows
    # can be updated in place rather than briefly cleared and reinserted.
    assert "var previousLoaded = listState.loaded, hadRows = previousLoaded > 0;" in page


def test_settings_have_visible_sections_and_editable_ai_workflow():
    from meeting_notes.server.settings import Settings
    from meeting_notes.server.web import render_settings_page

    page = render_settings_page(Settings(), token_configured=True)
    assert 'id="settings-install-heading"' in page
    assert 'id="settings-transcription-heading"' in page
    assert 'id="settings-ai-heading"' in page
    assert 'id="settings-speakers-heading"' in page
    assert 'id="settings-retention-heading"' in page
    assert 'textarea name="ai_workflow"' in page


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
    assert 'class="btn install-button"' not in page
    assert 'a.btn { display:inline-block' in page
