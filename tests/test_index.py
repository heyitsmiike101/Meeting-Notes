"""Tests for the SQLite session/job index (meeting_notes.server.index and the
Store methods that keep it in sync -- see index.py's module docstring for why
it exists at all: this server has to scale to hundreds or thousands of
sessions without ever falling back to a directory walk on a page load.

These tests exercise the index mostly through Store (write_session_meta,
create_job, update_job, write_transcript, delete_*), since that's the
integration surface every other test and the app actually uses -- the index
module's own API is deliberately an internal implementation detail.
"""

from __future__ import annotations

import time

from meeting_notes import wire
from meeting_notes.server import index as index_mod
from meeting_notes.server import store as store_mod


def make_store(tmp_path) -> store_mod.Store:
    return store_mod.Store(str(tmp_path / "data"))


# -- incremental updates ------------------------------------------------------


def test_write_session_meta_indexes_the_session(tmp_path):
    store = make_store(tmp_path)
    store.write_session_meta("sess-1", {"created": "2026-09-20", "name": "Standup"})

    row = store.session_index_row("sess-1")
    assert row is not None
    assert row["name"] == "Standup"
    assert row["latest_state"] is None
    assert row["has_audio"] is False


def test_create_job_updates_session_latest_state(tmp_path):
    store = make_store(tmp_path)
    store.write_session_meta("sess-1", {"created": "2026-09-20"})

    job_id = store.create_job("sess-1", {"labels": {"mic": "You"}})

    row = store.session_index_row("sess-1")
    assert row["latest_job_id"] == job_id
    assert row["latest_state"] == wire.JobState.QUEUED


def test_update_job_reflects_state_progress_and_error(tmp_path):
    store = make_store(tmp_path)
    store.write_session_meta("sess-1", {"created": "2026-09-20"})
    job_id = store.create_job("sess-1")

    store.update_job(job_id, state=wire.JobState.RUNNING, progress=0.5)
    row = store.session_index_row("sess-1")
    assert row["latest_state"] == wire.JobState.RUNNING
    assert row["latest_progress"] == 0.5

    store.update_job(job_id, state=wire.JobState.ERROR, error="boom", progress=1.0)
    row = store.session_index_row("sess-1")
    assert row["latest_state"] == wire.JobState.ERROR
    assert row["latest_error"] == "boom"


def test_write_transcript_indexes_text_for_search(tmp_path):
    store = make_store(tmp_path)
    store.write_session_meta("sess-1", {"created": "2026-09-20"})
    job_id = store.create_job("sess-1")
    json_text = '{"segments": [{"text": "the quarterly budget review"}]}'
    store.write_transcript(job_id, "# md", json_text)

    result = store.list_sessions(q="budget")
    assert [s["session_id"] for s in result["items"]] == ["sess-1"]

    result = store.list_sessions(q="nonexistent-word-xyz")
    assert result["items"] == []


def test_audio_bytes_and_has_audio_reflect_disk(tmp_path):
    store = make_store(tmp_path)
    store.write_session_meta("sess-1", {"created": "2026-09-20"})
    row = store.session_index_row("sess-1")
    assert row["has_audio"] is False

    wav_path = store.track_wav_path("sess-1", "mic")
    wav_path.write_bytes(b"\x00" * 100)
    store._index_upsert_session("sess-1")

    row = store.session_index_row("sess-1")
    assert row["has_audio"] is True
    assert row["audio_bytes"] == 100


def test_delete_session_audio_updates_index_but_keeps_row(tmp_path):
    store = make_store(tmp_path)
    store.write_session_meta("sess-1", {"created": "2026-09-20"})
    store.track_wav_path("sess-1", "mic").write_bytes(b"\x00" * 50)
    store._index_upsert_session("sess-1")
    assert store.session_index_row("sess-1")["has_audio"] is True

    store.delete_session_audio("sess-1")

    row = store.session_index_row("sess-1")
    assert row is not None
    assert row["has_audio"] is False
    assert row["audio_bytes"] == 0


def test_delete_session_removes_row_and_jobs(tmp_path):
    store = make_store(tmp_path)
    store.write_session_meta("sess-1", {"created": "2026-09-20"})
    job_id = store.create_job("sess-1")
    store.write_transcript(job_id, "# md", '{"segments": []}')

    store.delete_session("sess-1")

    assert store.session_index_row("sess-1") is None
    assert store.list_sessions()["items"] == []
    assert store.index.jobs_for_session("sess-1") == []


# -- reindex -----------------------------------------------------------------


def test_reindex_from_disk_matches_incremental_index(tmp_path):
    store = make_store(tmp_path)
    for i in range(3):
        sid = f"sess-{i}"
        store.write_session_meta(sid, {"created": "2026-09-20", "name": f"Meeting {i}"})
        job_id = store.create_job(sid)
        if i == 0:
            store.update_job(job_id, state=wire.JobState.DONE, progress=1.0)
            store.write_transcript(job_id, "# md", '{"segments": [{"text": "hello world"}]}')

    before = {s["session_id"]: dict(s) for s in store.list_sessions(per_page=200)["items"]}

    count = store.reindex()

    after = {s["session_id"]: dict(s) for s in store.list_sessions(per_page=200)["items"]}
    assert count == 3
    assert set(before) == set(after)
    for sid in before:
        # 'updated' legitimately changes on reindex; everything else must not.
        b, a = dict(before[sid]), dict(after[sid])
        b.pop("updated"), a.pop("updated")
        assert b == a

    # Search still works after a full rebuild.
    result = store.list_sessions(q="hello")
    assert result["items"][0]["session_id"] == "sess-0"


def test_reindex_runs_automatically_when_index_file_is_missing(tmp_path):
    data_root = tmp_path / "data"
    store1 = store_mod.Store(str(data_root))
    store1.write_session_meta("sess-1", {"created": "2026-09-20"})
    store1.index.close()

    # Remove the db file and WAL-mode sidecars, simulating "the index is
    # entirely gone" (e.g. a data volume restored from a backup that didn't
    # include it) rather than merely truncated.
    for suffix in ("", "-wal", "-shm"):
        path = data_root / ("index.sqlite" + suffix)
        if path.exists():
            path.unlink()

    # A fresh Store over the same data root, with no index file, must recover
    # the existing session on construction -- not report an empty list until
    # someone notices and reindexes by hand.
    store2 = store_mod.Store(str(data_root))
    assert [s["session_id"] for s in store2.list_sessions()["items"]] == ["sess-1"]


# -- pagination and search ----------------------------------------------------


def test_pagination_pages_through_newest_first(tmp_path):
    store = make_store(tmp_path)
    base = time.time()
    for i in range(5):
        sid = f"sess-{i}"
        store.write_session_meta(sid, {"created": base + i})  # increasing created time

    page1 = store.list_sessions(page=1, per_page=2)
    assert page1["total"] == 5
    assert [s["session_id"] for s in page1["items"]] == ["sess-4", "sess-3"]

    page2 = store.list_sessions(page=2, per_page=2)
    assert [s["session_id"] for s in page2["items"]] == ["sess-2", "sess-1"]

    page3 = store.list_sessions(page=3, per_page=2)
    assert [s["session_id"] for s in page3["items"]] == ["sess-0"]


def test_state_filter_matches_latest_job_state(tmp_path):
    store = make_store(tmp_path)
    store.write_session_meta("sess-done", {"created": "2026-09-20"})
    job_done = store.create_job("sess-done")
    store.update_job(job_done, state=wire.JobState.DONE)

    store.write_session_meta("sess-error", {"created": "2026-09-20"})
    job_error = store.create_job("sess-error")
    store.update_job(job_error, state=wire.JobState.ERROR, error="oops")

    result = store.list_sessions(state=wire.JobState.DONE)
    assert [s["session_id"] for s in result["items"]] == ["sess-done"]

    result = store.list_sessions(state=wire.JobState.ERROR)
    assert [s["session_id"] for s in result["items"]] == ["sess-error"]


def test_search_matches_name_as_well_as_transcript_text(tmp_path):
    store = make_store(tmp_path)
    store.write_session_meta("sess-1", {"created": "2026-09-20", "name": "Roadmap sync"})
    store.write_session_meta("sess-2", {"created": "2026-09-20", "name": "1:1"})

    result = store.list_sessions(q="Roadmap")
    assert [s["session_id"] for s in result["items"]] == ["sess-1"]


def test_search_matches_the_computer_a_meeting_was_recorded_on(tmp_path):
    """The Recorders page's History button opens /meetings?q=<computer name>."""
    store = make_store(tmp_path)
    store.write_session_meta("sess-1", {"created": "2026-09-20", "name": "Roadmap sync", "device": "OFFICE-PC-01"})
    store.write_session_meta("sess-2", {"created": "2026-09-20", "name": "1:1", "device": "Mac-mini"})

    assert [s["session_id"] for s in store.list_sessions(q="OFFICE-PC-01")["items"]] == ["sess-1"]
    assert [s["session_id"] for s in store.list_sessions(q="mac-mini")["items"]] == ["sess-2"]


def test_fts_query_with_special_characters_does_not_raise(tmp_path):
    """FTS5 query syntax treats '-', '"', '*' etc specially -- a user's search
    text is not FTS5 query syntax and must never blow up as one."""
    store = make_store(tmp_path)
    store.write_session_meta("sess-1", {"created": "2026-09-20"})
    job_id = store.create_job("sess-1")
    store.write_transcript(job_id, "# md", '{"segments": [{"text": "budget - q3 numbers"}]}')

    result = store.list_sessions(q='budget - "q3"')
    assert isinstance(result["items"], list)  # must not raise


# -- FTS5 fallback -------------------------------------------------------


def test_like_fallback_when_fts5_unavailable(tmp_path, monkeypatch):
    """Simulates a SQLite build with no FTS5 (a compile-time option, not
    guaranteed everywhere): sqlite3.Connection is a builtin C type and can't
    be monkeypatched method-by-method, so this forces Index's own
    _ensure_transcript_table to take the fallback branch directly, and checks
    search still works via the plain-table + LIKE path."""

    def fallback_only(self):
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS transcript_text (session_id TEXT PRIMARY KEY, text TEXT)"
        )
        return False

    monkeypatch.setattr(index_mod.Index, "_ensure_transcript_table", fallback_only)

    store = make_store(tmp_path)
    assert store.index.fts_enabled is False

    store.write_session_meta("sess-1", {"created": "2026-09-20"})
    job_id = store.create_job("sess-1")
    store.write_transcript(job_id, "# md", '{"segments": [{"text": "hello from the fallback"}]}')

    result = store.list_sessions(q="fallback")
    assert [s["session_id"] for s in result["items"]] == ["sess-1"]


# -- scale: no per-session filesystem access ----------------------------------


def test_list_sessions_touches_no_filesystem_even_with_hundreds_of_sessions(tmp_path, monkeypatch):
    """The whole point of the index: list_sessions must answer purely from
    SQLite, with cost independent of how many sessions exist on disk. Proven
    here by populating the index directly (bypassing Store's own disk-backed
    helpers) and then breaking directory iteration -- if list_sessions/
    query_sessions ever fell back to walking the sessions directory, this
    would raise instead of returning a page.
    """
    store = make_store(tmp_path)
    now = time.time()
    for i in range(300):
        store.index.upsert_session(
            session_id=f"sess-{i:04d}",
            name=f"Meeting {i}",
            created=now - i,
            duration_sec=60.0,
            has_audio=True,
            audio_bytes=1000,
            latest_job_id=None,
            latest_state=wire.JobState.DONE,
            latest_progress=1.0,
            latest_error=None,
            updated=now,
        )

    def boom(*_args, **_kwargs):
        raise AssertionError("list_sessions must not touch the filesystem")

    monkeypatch.setattr("os.scandir", boom)
    monkeypatch.setattr(store_mod.Path, "iterdir", boom)

    started = time.monotonic()
    result = store.list_sessions(page=1, per_page=50)
    elapsed = time.monotonic() - started

    assert result["total"] == 300
    assert len(result["items"]) == 50
    assert elapsed < 1.0  # bounded time regardless of session count

    result_p2 = store.list_sessions(page=2, per_page=50)
    assert len(result_p2["items"]) == 50
    assert result_p2["items"][0]["session_id"] != result["items"][0]["session_id"]
