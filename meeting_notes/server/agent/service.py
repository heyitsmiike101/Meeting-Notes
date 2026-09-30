"""The agent-facing read/write operations, transport-agnostic.

Every REST route and every MCP tool calls exactly one method here, so the two
surfaces cannot drift. Methods take plain keyword arguments (strings, numbers),
return JSON-able dicts (or a ``str`` when a ``format`` of markdown/text was
asked for) and raise :class:`AgentError` for anything the caller should see.
Nothing here reimplements storage: it reads through the ``Store`` and its
search ``Index`` and leaves the private on-disk formats alone.
"""

from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from .. import board as board_mod
from .. import settings as settings_mod
from .. import store as store_mod
from .errors import AgentError, bad_request, not_found

MAX_LIMIT = 200
DEFAULT_LIMIT = 50
SEARCH_DEFAULT_LIMIT = 20
SEARCH_MAX_LIMIT = 50
SNIPPETS_PER_MEETING = 5
TRANSCRIPT_FORMATS = ("json", "markdown", "text")
NOTES_FORMATS = ("json", "markdown")


# -- small helpers -------------------------------------------------------------


def iso(ts) -> Optional[str]:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def parse_time(value, field: str, *, end_of_day: bool = False) -> Optional[float]:
    """ISO 8601 date or datetime -> epoch seconds. Naive values are UTC."""
    if value is None or value == "":
        return None
    text = str(value).strip()
    date_only = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", text))
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        raise bad_request(f"{field} must be an ISO 8601 date or datetime, got {text!r}", "invalid_date")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    epoch = moment.timestamp()
    if date_only and end_of_day:
        epoch += 86400 - 1e-6
    return epoch


def clamp_limit(limit, default: int = DEFAULT_LIMIT, cap: int = MAX_LIMIT) -> int:
    if limit is None or limit == "":
        return default
    try:
        value = int(limit)
    except (TypeError, ValueError):
        raise bad_request("limit must be an integer", "invalid_limit")
    return max(1, min(value, cap))


def encode_cursor(payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor) -> Optional[dict]:
    if not cursor:
        return None
    try:
        pad = "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(cursor + pad).decode("utf-8"))
    except (ValueError, TypeError):
        raise bad_request("invalid cursor", "invalid_cursor")
    if not isinstance(value, dict):
        raise bad_request("invalid cursor", "invalid_cursor")
    return value


def format_ts(seconds) -> str:
    total = max(int(round(float(seconds or 0))), 0)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _as_str_list(value) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(v) for v in value if v not in (None, "")]


def _action_items(payload: dict) -> List[dict]:
    out = []
    raw = payload.get("action_items") if isinstance(payload, dict) else None
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict):
            action = str(item.get("action") or "").strip()
            if not action:
                continue
            out.append(
                {
                    "action": action,
                    "owner": item.get("owner") or None,
                    "due_date": item.get("due_date") or None,
                    "context": item.get("context") or None,
                }
            )
        elif isinstance(item, str) and item.strip():
            out.append({"action": item.strip(), "owner": None, "due_date": None, "context": None})
    return out


def _snippet(text: str, needle: str, width: int = 120) -> str:
    text = str(text)
    if len(text) <= width * 2 + len(needle):
        return text
    at = text.lower().find(needle.lower())
    if at < 0:
        return text[: width * 2] + "..."
    start = max(0, at - width)
    end = min(len(text), at + len(needle) + width)
    return ("..." if start else "") + text[start:end] + ("..." if end < len(text) else "")


def render_notes_markdown(name: str, created: Optional[str], payload: dict) -> str:
    title = str(payload.get("title") or name)
    lines = [f"# {title}", ""]
    meta = []
    if name and name != title:
        meta.append(f"Meeting: {name}")
    if created:
        meta.append(f"Date: {created}")
    participants = _as_str_list(payload.get("participants"))
    if participants:
        meta.append("Participants: " + ", ".join(participants))
    if meta:
        lines += ["  \n".join(meta), ""]

    def section(heading: str, items: List[str]) -> None:
        if items:
            lines.append(f"## {heading}")
            lines.extend(f"- {item}" for item in items)
            lines.append("")

    summary = str(payload.get("summary") or "").strip()
    if summary:
        lines += ["## Summary", summary, ""]
    section("Decisions", _as_str_list(payload.get("decisions")))
    actions = _action_items(payload)
    if actions:
        lines.append("## Action items")
        for item in actions:
            bits = f"- [ ] {item['action']}"
            if item["owner"]:
                bits += f" (owner: {item['owner']}"
                bits += f", due: {item['due_date']})" if item["due_date"] else ")"
            elif item["due_date"]:
                bits += f" (due: {item['due_date']})"
            if item["context"]:
                bits += f" - {item['context']}"
            lines.append(bits)
        lines.append("")
    section("Key points", _as_str_list(payload.get("key_points")))
    section("Open questions", _as_str_list(payload.get("open_questions")))
    section("Risks", _as_str_list(payload.get("risks")))
    section("Next steps", _as_str_list(payload.get("next_steps")))
    body = str(payload.get("meeting_notes") or "").strip()
    if body:
        lines += ["## Notes", body, ""]
    return "\n".join(lines).rstrip() + "\n"


def render_transcript_markdown(name: str, created: Optional[str], segments: List[dict]) -> str:
    lines = [f"# Transcript: {name}", ""]
    if created:
        lines += [f"Date: {created}", ""]
    i = 0
    while i < len(segments):
        seg = segments[i]
        j, texts = i, []
        while j < len(segments) and segments[j]["speaker"] == seg["speaker"]:
            texts.append(segments[j]["text"])
            j += 1
        body = " ".join(t.strip() for t in texts if t.strip())
        lines += [f"**[{format_ts(seg['start'])}] {seg['speaker']}:** {body}", ""]
        i = j
    return "\n".join(lines).rstrip() + "\n"


def render_transcript_text(segments: List[dict]) -> str:
    return "\n".join(
        f"[{format_ts(s['start'])}] {s['speaker']}: {s['text'].strip()}" for s in segments
    ) + ("\n" if segments else "")


# -- the service ---------------------------------------------------------------


class AgentService:
    def __init__(self, store):
        self.store = store

    # -- lookups ---------------------------------------------------------------

    def _row(self, meeting_id: str) -> dict:
        if not isinstance(meeting_id, str) or not store_mod.is_safe_id(meeting_id):
            raise bad_request(f"invalid meeting id: {meeting_id!r}", "invalid_id")
        row = self.store.session_index_row(meeting_id)
        if row is None:
            raise not_found("unknown meeting", "unknown_meeting")
        return row

    @staticmethod
    def _transcription_state(row: dict) -> str:
        state = row.get("latest_state")
        return {"done": "complete", "error": "error", "running": "transcribing"}.get(state, "pending")

    def _summary(self, row: dict) -> dict:
        sid = row["session_id"]
        review = row.get("review") or {}
        return {
            "id": sid,
            "board": board_mod.board_number(sid),
            "name": row.get("name") or sid,
            "created": iso(row.get("created")),
            "duration_sec": row.get("duration_sec"),
            "device": row.get("device") or None,
            "transcription_state": self._transcription_state(row),
            "notes_state": review.get("status") or "none",
            "has_notes": review.get("status") == "done",
            "updated": iso(row.get("updated")),
        }

    def _done_reviews(self) -> Dict[str, dict]:
        """Newest done review per session id (with a payload)."""
        out: Dict[str, dict] = {}
        for review in self.store.list_reviews():  # newest first
            sid = review.get("session_id")
            if (
                sid
                and sid not in out
                and review.get("status") == "done"
                and isinstance(review.get("payload"), dict)
            ):
                out[sid] = review
        return out

    def _done_review(self, meeting_id: str) -> Optional[dict]:
        for review in self.store.list_reviews(session_id=meeting_id):
            if review.get("status") == "done" and isinstance(review.get("payload"), dict):
                return review
        return None

    def _raw_segments(self, meeting_id: str) -> Optional[List[dict]]:
        job = self.store.latest_done_job(meeting_id)
        transcript = self.store.read_transcript(job["job_id"]) if job else None
        if transcript is None:
            return None
        try:
            payload = json.loads(transcript.get("json") or "{}")
        except (ValueError, TypeError):
            return None
        segments = payload.get("segments") if isinstance(payload, dict) else None
        if not isinstance(segments, list):
            return None
        out = []
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            try:
                start = float(seg.get("start") or 0.0)
                end = float(seg.get("end") if seg.get("end") is not None else start)
            except (TypeError, ValueError):
                continue
            out.append(
                {
                    "start": round(start, 2),
                    "end": round(end, 2),
                    "speaker": str(seg.get("label") or seg.get("speaker") or seg.get("track") or "Unknown"),
                    "text": str(seg.get("text") or ""),
                    "in_gap": bool(seg.get("in_gap")),
                }
            )
        return out

    def _speech_segments(self, meeting_id: str) -> Optional[List[dict]]:
        segments = self._raw_segments(meeting_id)
        if segments is None:
            return None
        return [s for s in segments if not s["in_gap"] and s["text"].strip()]

    def _all_rows(self, q: Optional[str] = None) -> List[dict]:
        index = self.store.index
        digits = board_mod.parse_board_query(q)
        extra_ids = board_mod.matching_ids(index.all_session_ids(), digits) if digits else None
        rows: List[dict] = []
        page = 1
        while True:
            result = index.query_sessions(q=q, page=page, per_page=MAX_LIMIT, extra_ids=extra_ids)
            rows.extend(result["items"])
            if len(rows) >= result["total"] or not result["items"]:
                return rows
            page += 1

    # -- reads -----------------------------------------------------------------

    def list_meetings(
        self,
        q=None,
        since=None,
        date_from=None,
        date_to=None,
        has_notes=None,
        limit=None,
        cursor=None,
    ) -> dict:
        limit = clamp_limit(limit)
        since_ts = parse_time(since, "since")
        from_ts = parse_time(date_from, "from")
        to_ts = parse_time(date_to, "to", end_of_day=True)
        want_notes = _to_bool(has_notes)
        after = decode_cursor(cursor)
        rows = self._all_rows(q.strip() if q else None)

        done_reviews = None
        picked = []
        for row in rows:
            created = float(row.get("created") or 0.0)
            if from_ts is not None and created < from_ts:
                continue
            if to_ts is not None and created > to_ts:
                continue
            if want_notes is not None and bool((row.get("review") or {}).get("status") == "done") != want_notes:
                continue
            if since_ts is not None:
                touched = max(created, float(row.get("updated") or 0.0))
                if touched <= since_ts and (row.get("review") or {}).get("status") == "done":
                    if done_reviews is None:
                        done_reviews = self._done_reviews()
                    review = done_reviews.get(row["session_id"])
                    touched = max(touched, float((review or {}).get("completed_at") or 0.0))
                if touched <= since_ts:
                    continue
            picked.append(row)

        picked.sort(key=lambda r: (float(r.get("created") or 0.0), r["session_id"]), reverse=True)
        if after is not None:
            try:
                key = (float(after["c"]), str(after["i"]))
            except (KeyError, TypeError, ValueError):
                raise bad_request("invalid cursor", "invalid_cursor")
            picked = [r for r in picked if (float(r.get("created") or 0.0), r["session_id"]) < key]
        page = picked[:limit]
        next_cursor = None
        if len(picked) > limit and page:
            last = page[-1]
            next_cursor = encode_cursor({"c": float(last.get("created") or 0.0), "i": last["session_id"]})
        return {
            "items": [self._summary(r) for r in page],
            "count": len(page),
            "next_cursor": next_cursor,
        }

    def get_meeting(self, meeting_id: str) -> dict:
        row = self._row(meeting_id)
        result = self._summary(row)
        result["platform"] = row.get("platform") or None
        result["transcription_error"] = row.get("latest_error") or None
        review = self._done_review(meeting_id)
        result["notes"] = review["payload"] if review else None
        result["notes_completed_at"] = iso(review.get("completed_at")) if review else None
        result["notes_template"] = self._template_of(review)
        segments = self._speech_segments(meeting_id)
        result["transcript"] = {
            "available": segments is not None,
            "segment_count": len(segments) if segments is not None else 0,
            "speakers": sorted({s["speaker"] for s in segments}) if segments else [],
        }
        return result

    def get_notes(self, meeting_id: str, format: str = "json"):
        row = self._row(meeting_id)
        fmt = (format or "json").lower()
        if fmt not in NOTES_FORMATS:
            raise bad_request(f"format must be one of {', '.join(NOTES_FORMATS)}", "invalid_format")
        review = self._done_review(meeting_id)
        if review is None:
            state = (row.get("review") or {}).get("status") or "none"
            if state in ("queued", "running"):
                raise AgentError(409, "notes_pending", f"notes are still being generated ({state})")
            raise not_found(
                "no notes for this meeting yet; use meeting_notes_generate_notes to queue them",
                "notes_not_available",
            )
        summary = self._summary(row)
        payload = review["payload"]
        if fmt == "markdown":
            return render_notes_markdown(summary["name"], summary["created"], payload)
        return {
            "meeting_id": meeting_id,
            "name": summary["name"],
            "created": summary["created"],
            "review_id": review.get("review_id"),
            "completed_at": iso(review.get("completed_at")),
            "template": self._template_of(review),
            "notes": payload,
        }

    def get_transcript(
        self, meeting_id: str, format: str = "json", speaker=None, start_sec=None, end_sec=None
    ):
        row = self._row(meeting_id)
        fmt = (format or "json").lower()
        if fmt not in TRANSCRIPT_FORMATS:
            raise bad_request(f"format must be one of {', '.join(TRANSCRIPT_FORMATS)}", "invalid_format")
        start = _to_float(start_sec, "start_sec")
        end = _to_float(end_sec, "end_sec")
        if start is not None and end is not None and start > end:
            raise bad_request("start_sec must not exceed end_sec", "invalid_window")
        segments = self._speech_segments(meeting_id)
        if segments is None:
            state = self._transcription_state(row)
            raise not_found(
                f"no completed transcript for this meeting (transcription: {state})",
                "transcript_not_available",
            )
        if speaker:
            wanted = str(speaker).strip().lower()
            segments = [s for s in segments if s["speaker"].lower() == wanted]
        if start is not None:
            segments = [s for s in segments if s["end"] >= start]
        if end is not None:
            segments = [s for s in segments if s["start"] <= end]
        summary = self._summary(row)
        if fmt == "markdown":
            return render_transcript_markdown(summary["name"], summary["created"], segments)
        if fmt == "text":
            return render_transcript_text(segments)
        return {
            "meeting_id": meeting_id,
            "name": summary["name"],
            "created": summary["created"],
            "segments": [{k: s[k] for k in ("start", "end", "speaker", "text")} for s in segments],
            "count": len(segments),
            "speakers": sorted({s["speaker"] for s in segments}),
        }

    def search(self, q: str, limit=None) -> dict:
        q = (q or "").strip()
        if not q:
            raise bad_request("q is required", "missing_query")
        limit = clamp_limit(limit, SEARCH_DEFAULT_LIMIT, SEARCH_MAX_LIMIT)
        needle = q.lower()
        rows = {r["session_id"]: r for r in self._all_rows(q)}
        notes_hits: Dict[str, List[dict]] = {}
        for sid, review in self._done_reviews().items():
            hits = _notes_matches(review["payload"], needle, q)
            if hits:
                notes_hits[sid] = hits
                if sid not in rows:
                    row = self.store.session_index_row(sid)
                    if row is not None:
                        rows[sid] = row
        ordered = sorted(
            rows.values(),
            key=lambda r: (float(r.get("created") or 0.0), r["session_id"]),
            reverse=True,
        )
        total = len(ordered)
        items = []
        for row in ordered[:limit]:
            sid = row["session_id"]
            matches: List[dict] = []
            if needle in str(row.get("name") or "").lower():
                matches.append({"source": "name", "text": row.get("name")})
            segments = self._speech_segments(sid) or []
            for seg in segments:
                if needle in seg["text"].lower():
                    matches.append(
                        {
                            "source": "transcript",
                            "start": seg["start"],
                            "end": seg["end"],
                            "speaker": seg["speaker"],
                            "text": _snippet(seg["text"], q),
                        }
                    )
                    if len(matches) >= SNIPPETS_PER_MEETING:
                        break
            matches.extend(notes_hits.get(sid, [])[: max(0, SNIPPETS_PER_MEETING - len(matches))])
            items.append({**self._summary(row), "matches": matches})
        return {"query": q, "items": items, "count": len(items), "total": total}

    def _aggregate(self, kind, since, date_from, date_to, q, owner, limit, cursor) -> dict:
        limit = clamp_limit(limit)
        since_ts = parse_time(since, "since")
        from_ts = parse_time(date_from, "from")
        to_ts = parse_time(date_to, "to", end_of_day=True)
        after = decode_cursor(cursor)
        offset = 0
        if after is not None:
            try:
                offset = max(int(after["o"]), 0)
            except (KeyError, TypeError, ValueError):
                raise bad_request("invalid cursor", "invalid_cursor")
        needle = (q or "").strip().lower()
        owner_needle = (owner or "").strip().lower()
        entries: List[Tuple[float, dict]] = []
        for sid, review in self._done_reviews().items():
            row = self.store.session_index_row(sid)
            if row is None:
                continue
            created = float(row.get("created") or 0.0)
            if from_ts is not None and created < from_ts:
                continue
            if to_ts is not None and created > to_ts:
                continue
            if since_ts is not None and max(created, float(review.get("completed_at") or 0.0)) <= since_ts:
                continue
            base = {
                "meeting_id": sid,
                "meeting_name": row.get("name") or sid,
                "meeting_board": board_mod.board_number(sid),
                "meeting_date": iso(created),
            }
            payload = review["payload"]
            if kind == "action_items":
                for item in _action_items(payload):
                    if owner_needle and owner_needle not in str(item["owner"] or "").lower():
                        continue
                    hay = " ".join(str(item[k] or "") for k in ("action", "owner", "context")).lower()
                    if needle and needle not in hay:
                        continue
                    entries.append((created, {**base, **item}))
            else:
                for decision in _as_str_list(payload.get("decisions")):
                    if needle and needle not in decision.lower():
                        continue
                    entries.append((created, {**base, "decision": decision}))
        # Stable sort keeps each meeting's items in the order the notes list them.
        entries.sort(key=lambda e: e[0], reverse=True)
        chunk = [e[1] for e in entries[offset : offset + limit]]
        more = offset + limit < len(entries)
        return {
            "items": chunk,
            "count": len(chunk),
            "next_cursor": encode_cursor({"o": offset + limit}) if more else None,
        }

    def list_action_items(self, since=None, date_from=None, date_to=None, q=None, owner=None, limit=None, cursor=None):
        return self._aggregate("action_items", since, date_from, date_to, q, owner, limit, cursor)

    def list_decisions(self, since=None, date_from=None, date_to=None, q=None, limit=None, cursor=None):
        return self._aggregate("decisions", since, date_from, date_to, q, None, limit, cursor)

    # -- writes ----------------------------------------------------------------

    def _template_of(self, review: Optional[dict]) -> Optional[dict]:
        if not review:
            return None
        return settings_mod.load_settings(self.store.root).review_template(review)

    def list_note_templates(self) -> dict:
        """The note styles ``generate_notes`` accepts (no prompt text)."""
        current = settings_mod.load_settings(self.store.root)
        default_id = current.default_template()["id"]
        return {
            "default_template_id": default_id,
            "items": [
                {"id": t["id"], "name": t["name"], "builtin": t["builtin"], "default": t["id"] == default_id}
                for t in current.all_templates()
            ],
        }

    def generate_notes(self, meeting_id: str, force=False, template=None) -> dict:
        """Queue AI notes for a meeting: same call as ``POST /v1/sessions/{id}/review``.

        ``template`` is a note style id or name (see ``list_note_templates``);
        omitted means the server's default style. It applies when a review is
        actually queued: without ``force``, existing notes are returned as-is
        (with their own template), so pass ``force`` to regenerate in a new style.
        """
        self._row(meeting_id)
        current = settings_mod.load_settings(self.store.root)
        if template is None or (isinstance(template, str) and not template.strip()):
            chosen = current.default_template()
        else:
            chosen = current.find_template(template)
            if chosen is None:
                raise bad_request(f"unknown note template: {template!r}", "unknown_template")
        try:
            review = self.store.create_review(
                meeting_id, bool(_to_bool(force)), {"id": chosen["id"], "name": chosen["name"]}
            )
        except ValueError as exc:
            raise AgentError(409, "no_transcript", str(exc))
        return {
            "meeting_id": meeting_id,
            "review_id": review.get("review_id"),
            "status": review.get("status"),
            "created": iso(review.get("created")),
            "template": self._template_of(review),
        }

    def rename_meeting(self, meeting_id: str, name) -> dict:
        self._row(meeting_id)
        if not isinstance(name, str) or not name.strip():
            raise bad_request("name must be a non-empty string", "invalid_name")
        value = name.strip()
        if len(value) > 200:
            raise bad_request("name must be 200 characters or fewer", "invalid_name")
        try:
            self.store.rename_session(meeting_id, value)
        except ValueError as exc:
            raise not_found(str(exc), "unknown_meeting")
        return {"meeting_id": meeting_id, "name": value}


# -- module-private ---------------------------------------------------------------


def _to_bool(value) -> Optional[bool]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    raise bad_request(f"expected a boolean, got {value!r}", "invalid_boolean")


def _to_float(value, field: str) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise bad_request(f"{field} must be a number of seconds", "invalid_window")
    if number < 0:
        raise bad_request(f"{field} must not be negative", "invalid_window")
    return number


def _notes_matches(payload: dict, needle: str, q: str) -> List[dict]:
    hits: List[dict] = []
    for field in ("title", "summary", "meeting_notes"):
        text = str(payload.get(field) or "")
        if needle in text.lower():
            hits.append({"source": "notes", "field": field, "text": _snippet(text, q)})
    for field in ("participants", "key_points", "decisions", "open_questions", "risks", "next_steps"):
        for entry in _as_str_list(payload.get(field)):
            if needle in entry.lower():
                hits.append({"source": "notes", "field": field, "text": _snippet(entry, q)})
    for item in _action_items(payload):
        blob = " ".join(str(item[k] or "") for k in ("action", "owner", "context"))
        if needle in blob.lower():
            hits.append({"source": "notes", "field": "action_items", "text": _snippet(item["action"], q)})
    return hits
