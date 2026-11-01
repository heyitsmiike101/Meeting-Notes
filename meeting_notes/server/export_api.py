"""Transcript downloads and export zips, installed by ``create_app``."""

from __future__ import annotations

import re
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from .. import scratchpad as scratchpad_mod
from . import auth, meeting_stats as meeting_stats_mod, people as people_mod
from .route_helpers import _safe_session_id_or_400


def install_exports(app: FastAPI, *, store) -> None:
    """Transcript downloads and the one-meeting / bulk export zips."""

    def _with_meeting_details(markdown: str, stats: dict) -> str:
        """The stored transcript Markdown with a "Meeting details" list (start, end, talk time) under its title."""
        lines = meeting_stats_mod.detail_lines(stats)
        if not lines:
            return markdown
        block = "## Meeting details\n" + "\n".join(f"- {line}" for line in lines) + "\n\n"
        text = markdown or ""
        head, sep, rest = text.partition("\n")
        if head.startswith("# "):
            return head + "\n\n" + block + rest.lstrip("\n")
        return block + text

    def _download_name(name, fallback: str) -> str:
        cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', "-", str(name or "")).strip(" .-")[:100]
        return cleaned or fallback

    def _attachment_header(filename: str, ascii_fallback: str) -> str:
        """``Content-Disposition`` with a UTF-8 file name (RFC 5987) and a plain ASCII fallback."""
        from urllib.parse import quote

        ascii_name = filename.encode("ascii", "ignore").decode("ascii").replace('"', "") or ascii_fallback
        return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(filename)}'

    def _transcript_markdown(session_id: str) -> tuple:
        """``(markdown or None, display name)`` for a meeting's latest completed transcript."""
        job = store.latest_done_job(session_id)
        transcript = store.read_transcript(job["job_id"]) if job else None
        meta = store.read_session_meta(session_id) if store.session_exists(session_id) else {}
        if not isinstance(meta, dict):
            meta = {}
        name = _download_name(meta.get("name"), session_id)
        if transcript is None:
            return None, name
        title = str(meta.get("name") or "").strip()
        stored = transcript["markdown"] or ""
        if title and stored.startswith("# Meeting transcript"):
            stored = f"# {title}: transcript" + stored[len("# Meeting transcript"):]
        markdown = _with_meeting_details(
            stored,
            meeting_stats_mod.meeting_stats(meta, meeting_stats_mod.transcript_segments(store, job["job_id"])),
        )
        return markdown, name

    def _note_text(value) -> str:
        if value is None:
            return ""
        if not isinstance(value, dict):
            return str(value)
        text = str(
            value.get("action") or value.get("task") or value.get("text") or value.get("title")
            or value.get("point") or value.get("decision") or value.get("question")
            or value.get("risk") or value.get("step") or ""
        )
        if value.get("owner"):
            text += f" — Owner: {value['owner']}"
        if value.get("due_date") or value.get("due"):
            text += f" — Due: {value.get('due_date') or value.get('due')}"
        if value.get("context"):
            text += f" — {value['context']}"
        return text

    def _notes_markdown(session_id: str) -> Optional[str]:
        """The generated meeting notes as Markdown (same sections as the web page's notes.md), or None."""
        review = next(
            (r for r in store.list_reviews(session_id=session_id) if str(r.get("status") or "").lower() == "done"),
            None,
        )
        payload = review.get("payload") if review and isinstance(review.get("payload"), dict) else None
        if not payload or not payload.get("summary"):
            return None
        meta = store.read_session_meta(session_id) if store.session_exists(session_id) else {}
        meta = meta if isinstance(meta, dict) else {}
        title = str(review.get("title_override") or payload.get("title") or meta.get("name") or session_id)

        def pick(*keys):
            for key in keys:
                if payload.get(key):
                    return payload[key]
            return None

        sections = [
            ("Summary", pick("summary", "overview")),
            ("Meeting notes", pick("polished_meeting_notes", "polished_notes", "meeting_notes", "narrative", "notes")),
            ("Key points", pick("key_points", "keyPoints")),
            ("Decisions", pick("decisions")),
            ("Action items", pick("action_items", "actionItems", "actions")),
            ("Open questions", pick("open_questions", "openQuestions", "questions")),
            ("Risks", pick("risks")),
            ("Next steps", pick("next_steps", "nextSteps")),
            ("Participants", pick("participants", "attendees")),
        ]

        def values(value) -> list:
            items = value if isinstance(value, list) else ([] if value is None else [value])
            return [t for t in (_note_text(v) for v in items) if t.strip()]

        def render(name, value) -> str:
            vals = values(value)
            prose = name in ("Summary", "Meeting notes")
            body = "\n\n".join(vals) if prose else "\n".join(f"- {v}" for v in vals)
            return f"## {name}\n{body}\n"

        filled = [s for s in sections if values(s[1])]
        empty = [s for s in sections if not values(s[1])]
        lines = meeting_stats_mod.detail_lines(meeting_stats_mod.stats_for_review(store, review))
        facts = ("## Meeting details\n" + "\n".join(f"- {x}" for x in lines) + "\n\n") if lines else ""
        text = f"# {title}\n\n{facts}" + "\n".join(render(n, v) for n, v in filled)
        if empty:
            text += "\n---\n\n" + "\n".join(render(n, v) for n, v in empty)
        return text.strip() + "\n"

    def _export_files(session_id: str) -> tuple:
        """``(display name, {kind: markdown})`` for one meeting; the dict is empty when nothing exists."""
        markdown, name = _transcript_markdown(session_id)
        files = {}
        if markdown is not None:
            files["transcript"] = people_mod.apply_names_markdown(store.root, session_id, markdown)
        notes = _notes_markdown(session_id)
        if notes is not None:
            files["notes"] = notes
        try:
            scratchpad = scratchpad_mod.clean(store.read_session_meta(session_id).get("scratchpad"))
        except Exception:  # noqa: BLE001 - a missing meta just means no notes of the user's own
            scratchpad = None
        if scratchpad and files:
            files["your notes"] = scratchpad + "\n"  # raw, exactly as typed during the meeting
        return name, files

    def _meeting_day(session_id: str) -> str:
        """The meeting's date as ``YYYY-MM-DD`` in the recorder's local time, or "" if unknown.

        ``created`` is the recorder's local wall time (no zone), so its date part is the day the user had the meeting;
        desktop session ids start with the same local stamp. ``started_wall`` (an epoch) is the last resort."""
        meta = store.read_session_meta(session_id) if store.session_exists(session_id) else {}
        meta = meta if isinstance(meta, dict) else {}
        for value in (meta.get("created"), meta.get("date"), session_id):
            text = str(value or "")
            if re.match(r"^\d{4}-\d{2}-\d{2}", text):
                return text[:10]
        wall = meta.get("started_wall")
        if isinstance(wall, (int, float)) and wall > 0:
            import datetime as _dt

            return _dt.datetime.fromtimestamp(float(wall)).strftime("%Y-%m-%d")
        return ""

    def _zip_response(entries: dict, zip_name: str, fallback: str) -> Response:
        import io
        import zipfile

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for path, text in entries.items():
                archive.writestr(path, text.encode("utf-8"))
        return Response(
            content=buffer.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": _attachment_header(zip_name, fallback)},
        )

    @app.get("/sessions/{session_id}/transcript.md")
    async def download_transcript_markdown(
        session_id: str, _auth: None = Depends(auth.require_web_token)
    ):
        _safe_session_id_or_400(session_id)
        markdown, name = _transcript_markdown(session_id)
        if markdown is None:
            raise HTTPException(status_code=404, detail="no completed transcript for this session")
        return Response(
            content=markdown,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": _attachment_header(f"{name} - transcript.md", f"{session_id}.md")},
        )

    @app.get("/sessions/{session_id}/export.zip")
    async def export_meeting_zip(session_id: str, _auth: None = Depends(auth.require_web_token)):
        """The meeting's transcript and notes as Markdown files in one zip."""
        _safe_session_id_or_400(session_id)
        name, files = await run_in_threadpool(_export_files, session_id)
        if not files:
            raise HTTPException(status_code=404, detail="Nothing to export: this meeting has no transcript or notes yet.")
        return _zip_response(
            {f"{name} - {kind}.md": text for kind, text in files.items()}, f"{name}.zip", f"{session_id}.zip"
        )

    @app.post("/sessions/export.zip")
    async def export_meetings_zip(request: Request, _auth: None = Depends(auth.require_web_token)):
        """Bulk export: body ``{"ids": [...]}``; one folder per meeting inside a single zip."""
        try:
            body = await request.json()
        except ValueError:
            body = None
        ids = body.get("ids") if isinstance(body, dict) else None
        if not isinstance(ids, list) or not ids:
            raise HTTPException(status_code=400, detail="ids must be a non-empty list of meeting ids")
        ids = list(dict.fromkeys(str(i) for i in ids))
        for sid in ids:
            _safe_session_id_or_400(sid)

        def build() -> dict:
            entries, used = {}, set()
            for sid in ids:
                name, files = _export_files(sid)
                if not files:
                    continue
                # One folder per meeting: "<name> - <YYYY-MM-DD>" (the files inside keep the plain meeting name).
                day = _meeting_day(sid)
                base = f"{name} - {day}" if day else name
                folder, n = base, 1
                while folder.lower() in used:
                    n += 1
                    folder = f"{base} ({n})"
                used.add(folder.lower())
                for kind, text in files.items():
                    entries[f"{folder}/{name} - {kind}.md"] = text
            return entries

        entries = await run_in_threadpool(build)
        if not entries:
            raise HTTPException(
                status_code=404,
                detail="Nothing to export: none of the selected meetings has a transcript or notes yet.",
            )
        count = len({path.split("/", 1)[0] for path in entries})
        return _zip_response(
            entries, f"Meeting notes export ({count} meeting{'' if count == 1 else 's'}).zip", "meeting-notes-export.zip"
        )

    @app.get("/sessions/{session_id}/transcript.json")
    async def download_transcript_json(
        session_id: str, _auth: None = Depends(auth.require_web_token)
    ):
        _safe_session_id_or_400(session_id)
        job = store.latest_done_job(session_id)
        transcript = store.read_transcript(job["job_id"]) if job else None
        if transcript is None:
            raise HTTPException(status_code=404, detail="no completed transcript for this session")
        return Response(
            content=transcript["json"],
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{session_id}.json"'},
        )
