"""A SQLite index over sessions and jobs, so the web UI and JSON API can list,
search, and paginate without walking the sessions directory on every request.

Why this exists: this server is meant to accumulate hundreds or thousands of
real recordings over the life of a deployment, and answering "list sessions,
newest first, page 3, matching this search term" by globbing
``sessions/*/session.json`` and stat-ing every session's audio files on every
page load does not scale to that -- it gets slower with every meeting ever
recorded, forever. So every write that already knows what changed (a new
session's meta, a job's state, a written transcript) updates this index
incrementally, and every read that lists or searches goes through SQLite
instead of the filesystem.

The database is a read-optimization, not the source of truth: everything in
it is derived from files already on disk (session.json, job records,
transcripts), so ``Store.reindex()`` can always rebuild it from scratch if
it's ever missing, corrupt, or suspected stale. That's also why it lives in
the data root as a plain file rather than needing its own backup story, and
why every table here is a cache of on-disk state rather than something
anything else treats as authoritative.

WAL mode is turned on because this database is hit from several threads at
once in normal operation: request handlers (via FastAPI's threadpool), the
job worker thread, and the retention worker thread. A single ``threading.Lock``
around every statement keeps this module simple (SQLite's Python driver is not
safe to share across threads without one when opened with
``check_same_thread=False``) at the cost of serializing writes -- fine here,
since none of these are hot paths compared to actually running Whisper.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Dict, List, Optional

_SCHEMA_SESSIONS = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created REAL NOT NULL,
    duration_sec REAL,
    device TEXT,
    platform TEXT,
    has_audio INTEGER NOT NULL DEFAULT 0,
    audio_bytes INTEGER NOT NULL DEFAULT 0,
    latest_job_id TEXT,
    latest_state TEXT,
    latest_progress REAL,
    latest_error TEXT,
    updated REAL NOT NULL
)
"""

_SCHEMA_JOBS = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    state TEXT,
    progress REAL,
    error TEXT,
    created REAL NOT NULL,
    updated REAL NOT NULL
)
"""

_SCHEMA_JOBS_INDEX = "CREATE INDEX IF NOT EXISTS jobs_session_idx ON jobs(session_id)"
_SCHEMA_SESSIONS_CREATED_INDEX = "CREATE INDEX IF NOT EXISTS sessions_created_idx ON sessions(created)"

# The newest review for each session.  It deliberately lives separately from
# ``sessions`` because ordinary session metadata writes must not accidentally
# overwrite its state.  It is still derived from the review JSON files and is
# rebuilt with the rest of this read index.
_SCHEMA_REVIEW_STATUSES = """
CREATE TABLE IF NOT EXISTS review_statuses (
    session_id TEXT PRIMARY KEY,
    review_id TEXT NOT NULL,
    status TEXT NOT NULL,
    created REAL NOT NULL
)
"""


class Index:
    """Owns the ``index.sqlite`` connection and every statement against it."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self._lock = threading.Lock()
        # check_same_thread=False: this connection is shared across the
        # request threadpool, the job worker thread, and the retention
        # worker thread. self._lock (not sqlite3's own thread-affinity
        # check) is what makes that safe.
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute(_SCHEMA_SESSIONS)
            self._ensure_session_columns()
            self._conn.execute(_SCHEMA_JOBS)
            self._conn.execute(_SCHEMA_JOBS_INDEX)
            self._conn.execute(_SCHEMA_REVIEW_STATUSES)
            self._conn.execute(_SCHEMA_SESSIONS_CREATED_INDEX)
            self.fts_enabled = self._ensure_transcript_table()
            self._conn.commit()

    def _ensure_session_columns(self) -> None:
        """Small in-place migrations for indexes created by older releases."""
        columns = {
            row[1] for row in self._conn.execute("PRAGMA table_info(sessions)").fetchall()
        }
        for name in ("device", "platform"):
            if name not in columns:
                self._conn.execute(f"ALTER TABLE sessions ADD COLUMN {name} TEXT")

    def _ensure_transcript_table(self) -> bool:
        """Create the transcript-text table, preferring FTS5 full-text search.

        FTS5 is a compile-time option in SQLite; most builds (including
        Python's bundled ``sqlite3`` on recent CPython) have it, but nothing
        here should hard-fail on a build that doesn't. Falls back to a plain
        table searched with ``LIKE`` -- slower and less precise, but the same
        query shape from the caller's point of view (see
        ``_matching_transcript_ids``).
        """
        try:
            self._conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS transcript_text "
                "USING fts5(session_id UNINDEXED, text)"
            )
            return True
        except sqlite3.OperationalError:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS transcript_text ("
                "session_id TEXT PRIMARY KEY, text TEXT)"
            )
            return False

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- sessions ----------------------------------------------------------

    def upsert_session(
        self,
        *,
        session_id: str,
        name: str,
        created: float,
        duration_sec: Optional[float],
        has_audio: bool,
        audio_bytes: int,
        latest_job_id: Optional[str],
        latest_state: Optional[str],
        latest_progress: Optional[float],
        latest_error: Optional[str],
        updated: float,
        device: Optional[str] = None,
        platform: Optional[str] = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO sessions (
                    session_id, name, created, duration_sec, device, platform, has_audio, audio_bytes,
                    latest_job_id, latest_state, latest_progress, latest_error, updated
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    name=excluded.name,
                    created=excluded.created,
                    duration_sec=excluded.duration_sec,
                    device=excluded.device,
                    platform=excluded.platform,
                    has_audio=excluded.has_audio,
                    audio_bytes=excluded.audio_bytes,
                    latest_job_id=excluded.latest_job_id,
                    latest_state=excluded.latest_state,
                    latest_progress=excluded.latest_progress,
                    latest_error=excluded.latest_error,
                    updated=excluded.updated
                """,
                (
                    session_id,
                    name,
                    created,
                    duration_sec,
                    device,
                    platform,
                    1 if has_audio else 0,
                    audio_bytes,
                    latest_job_id,
                    latest_state,
                    latest_progress,
                    latest_error,
                    updated,
                ),
            )
            self._conn.commit()

    def get_session(self, session_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT sessions.*, review_statuses.review_id, review_statuses.status AS review_status
                FROM sessions LEFT JOIN review_statuses USING (session_id)
                WHERE sessions.session_id = ?
                """,
                (session_id,),
            ).fetchone()
            return _row_to_session(row) if row else None

    def count_with_audio(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM sessions WHERE has_audio = 1").fetchone()[0]

    def all_session_ids(self) -> List[str]:
        with self._lock:
            return [r[0] for r in self._conn.execute("SELECT session_id FROM sessions").fetchall()]

    def delete_session(self, session_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
            self._conn.execute("DELETE FROM jobs WHERE session_id = ?", (session_id,))
            self._conn.execute("DELETE FROM transcript_text WHERE session_id = ?", (session_id,))
            self._conn.execute("DELETE FROM review_statuses WHERE session_id = ?", (session_id,))
            self._conn.commit()

    def query_sessions(
        self,
        *,
        q: Optional[str] = None,
        state: Optional[str] = None,
        page: int = 1,
        per_page: int = 50,
        extra_ids: Optional[List[str]] = None,
    ) -> Dict:
        """Paginated, optionally filtered/searched session listing.

        ``q`` matches either a session's name or its transcript text (via
        ``_matching_transcript_ids``); ``state`` matches ``latest_state``
        exactly. Both go through parameterized SQL -- nothing here ever
        f-strings user input into a query, only column names/placeholders
        this module itself controls.
        """
        page = max(int(page), 1)
        per_page = max(1, min(int(per_page), 200))

        where: List[str] = []
        params: List = []
        if state:
            where.append("latest_state = ?")
            params.append(state)
        if q:
            ids = self._matching_transcript_ids(q)
            if extra_ids:
                ids = list(dict.fromkeys([*ids, *extra_ids]))
            like = f"%{q}%"
            if ids:
                placeholders = ",".join("?" for _ in ids)
                where.append(f"(name LIKE ? OR session_id IN ({placeholders}))")
                params.append(like)
                params.extend(ids)
            else:
                where.append("name LIKE ?")
                params.append(like)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""

        with self._lock:
            total = self._conn.execute(
                f"SELECT COUNT(*) FROM sessions {where_sql}", params
            ).fetchone()[0]
            rows = self._conn.execute(
                f"""
                SELECT sessions.*, review_statuses.review_id, review_statuses.status AS review_status
                FROM sessions LEFT JOIN review_statuses USING (session_id)
                {where_sql} ORDER BY sessions.created DESC LIMIT ? OFFSET ?
                """,
                [*params, per_page, (page - 1) * per_page],
            ).fetchall()

        return {
            "items": [_row_to_session(r) for r in rows],
            "total": total,
            "page": page,
            "per_page": per_page,
        }

    def _matching_transcript_ids(self, q: str) -> List[str]:
        with self._lock:
            if self.fts_enabled:
                try:
                    # Quoting the whole query as one FTS5 phrase sidesteps its
                    # query-syntax operators (AND/OR/NOT/*/columns:...) --
                    # a user typing e.g. "budget - q3" should search for that
                    # text, not have "-" parsed as FTS5's NOT operator and
                    # blow up with an OperationalError.
                    escaped = q.replace('"', '""')
                    rows = self._conn.execute(
                        "SELECT session_id FROM transcript_text WHERE transcript_text MATCH ?",
                        (f'"{escaped}"',),
                    ).fetchall()
                    return [r[0] for r in rows]
                except sqlite3.OperationalError:
                    pass  # fall through to LIKE below
            rows = self._conn.execute(
                "SELECT session_id FROM transcript_text WHERE text LIKE ?",
                (f"%{q}%",),
            ).fetchall()
            return [r[0] for r in rows]

    def set_transcript_text(self, session_id: str, text: str) -> None:
        with self._lock:
            if self.fts_enabled:
                # FTS5 has no ON CONFLICT / UPSERT support in older SQLite
                # builds -- delete-then-insert is the portable way to replace
                # a row.
                self._conn.execute(
                    "DELETE FROM transcript_text WHERE session_id = ?", (session_id,)
                )
                self._conn.execute(
                    "INSERT INTO transcript_text (session_id, text) VALUES (?, ?)",
                    (session_id, text),
                )
            else:
                self._conn.execute(
                    "INSERT INTO transcript_text (session_id, text) VALUES (?, ?) "
                    "ON CONFLICT(session_id) DO UPDATE SET text=excluded.text",
                    (session_id, text),
                )
            self._conn.commit()

    # -- jobs ----------------------------------------------------------------

    def upsert_job(
        self,
        *,
        job_id: str,
        session_id: str,
        state: Optional[str],
        progress: Optional[float],
        error: Optional[str],
        created: float,
        updated: float,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO jobs (job_id, session_id, state, progress, error, created, updated)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    state=excluded.state,
                    progress=excluded.progress,
                    error=excluded.error,
                    updated=excluded.updated
                """,
                (job_id, session_id, state, progress, error, created, updated),
            )
            self._conn.commit()

    def jobs_for_session(self, session_id: str) -> List[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE session_id = ? ORDER BY created DESC", (session_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def all_jobs(self) -> List[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM jobs ORDER BY created DESC").fetchall()
            return [dict(r) for r in rows]

    # -- review status ----------------------------------------------------

    def upsert_review_status(
        self, *, session_id: str, review_id: str, status: str, created: float
    ) -> None:
        """Record a review lifecycle state if it is this session's newest.

        Updates to an older regenerated review must not displace the current
        note.  Updates to the current review retain its original ``created``
        value, so the id match is also allowed to update its status.
        """
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO review_statuses (session_id, review_id, status, created)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    review_id=excluded.review_id,
                    status=excluded.status,
                    created=excluded.created
                WHERE review_statuses.review_id=excluded.review_id
                   OR excluded.created >= review_statuses.created
                """,
                (session_id, review_id, status, created),
            )
            self._conn.commit()

    def clear_review_statuses(self) -> None:
        """Discard cached review state before rebuilding it from JSON files."""
        with self._lock:
            self._conn.execute("DELETE FROM review_statuses")
            self._conn.commit()

    # -- rebuild ---------------------------------------------------------

    def clear(self) -> None:
        """Empty every table, ahead of a full rebuild from disk (see
        ``Store.reindex``). Deliberately not ``DROP TABLE`` -- that would
        need every ``CREATE`` (FTS5-or-fallback included) re-run, whereas
        clearing rows against tables that already exist is simpler and just
        as complete."""
        with self._lock:
            self._conn.execute("DELETE FROM sessions")
            self._conn.execute("DELETE FROM jobs")
            self._conn.execute("DELETE FROM transcript_text")
            self._conn.execute("DELETE FROM review_statuses")
            self._conn.commit()

    def sessions_between(self, created_lo: float, created_hi: float) -> List[dict]:
        """Sessions whose start time falls in ``[created_lo, created_hi]``, oldest first
        (used for the "looks like a continuation" hint; bounded by the time window)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT session_id, name, created, duration_sec, device FROM sessions "
                "WHERE created BETWEEN ? AND ? ORDER BY created",
                (created_lo, created_hi),
            ).fetchall()
            return [dict(r) for r in rows]


def _row_to_session(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["has_audio"] = bool(d.get("has_audio"))
    review_id = d.pop("review_id", None)
    review_status = d.pop("review_status", None)
    d["review"] = (
        {"review_id": review_id, "status": review_status}
        if review_id and review_status
        else {"status": "none"}
    )
    return d
