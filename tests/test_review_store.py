from __future__ import annotations

import json
import threading
import time

import pytest

from meeting_notes import wire
from meeting_notes.server.store import Store, _atomic_write_json


def ready_store(tmp_path):
    store = Store(str(tmp_path))
    store.write_session_meta("session-1", {"name": "Planning"})
    job_id = store.create_job("session-1")
    store.update_job(job_id, state=wire.JobState.DONE, progress=1.0)
    store.write_transcript(job_id, "# Transcript", json.dumps({"segments": [{"text": "hello"}]}))
    return store, job_id


def test_create_review_requires_completed_transcript_and_is_idempotent(tmp_path):
    store = Store(str(tmp_path))
    store.write_session_meta("session-1", {})
    with pytest.raises(ValueError):
        store.create_review("session-1")
    with pytest.raises(ValueError):
        store.create_review("missing")

    store, job_id = ready_store(tmp_path / "ready")
    first = store.create_review("session-1")
    second = store.create_review("session-1")
    assert first["review_id"] == second["review_id"]
    assert first["transcript_job_id"] == job_id
    forced = store.create_review("session-1", force=True)
    assert forced["review_id"] != first["review_id"]


def test_review_lifecycle_persists_and_validates_payload(tmp_path):
    store, _ = ready_store(tmp_path)
    review = store.create_review("session-1")
    claimed = store.claim_next_review()
    assert claimed["review_id"] == review["review_id"]
    assert claimed["status"] == "running"
    with pytest.raises(ValueError):
        store.complete_review(review["review_id"], {"bad": object()})
    done = store.complete_review(review["review_id"], {"summary": "Decided", "action_items": []})
    assert done["status"] == "done"
    assert store.read_review(review["review_id"])["payload"]["summary"] == "Decided"
    assert store.review_for_session("session-1")["summary"] == "Decided"


def test_fail_and_retry(tmp_path):
    store, _ = ready_store(tmp_path)
    review = store.create_review("session-1")
    failed = store.fail_review(review["review_id"], "bridge unavailable")
    assert failed["status"] == "error"
    retried = store.retry_review(review["review_id"])
    assert retried["status"] == "queued"
    assert store.claim_next_review()["review_id"] == review["review_id"]


def test_completed_review_can_be_regenerated_and_new_transcript_gets_new_review(tmp_path):
    store, _ = ready_store(tmp_path)
    first = store.create_review("session-1")
    store.complete_review(first["review_id"], {"summary": "old"})
    retried = store.retry_review(first["review_id"])
    assert retried["status"] == "queued"
    assert retried["payload"] is None

    store.fail_review(first["review_id"], "stop this run")
    second_job = store.create_job("session-1")
    store.update_job(second_job, state=wire.JobState.DONE, progress=1.0)
    store.write_transcript(second_job, "# New", json.dumps({"segments": [{"text": "new"}]}))
    second = store.create_review("session-1")
    assert second["review_id"] != first["review_id"]
    assert second["transcript_job_id"] == second_job


def test_stale_running_review_is_requeued_and_claimed(tmp_path):
    store, _ = ready_store(tmp_path)
    review = store.create_review("session-1")
    claimed = store.claim_next_review()
    claimed["claimed_at"] = time.time() - 100
    # Simulate a worker that stopped after claiming; the record is still
    # written atomically by the store in normal operation.
    from meeting_notes.server.store import _atomic_write_json
    _atomic_write_json(store.review_path(review["review_id"]), claimed)
    recovered = store.claim_next_review(stale_after=10)
    assert recovered["review_id"] == review["review_id"]
    assert recovered["status"] == "running"
    assert recovered["claimed_at"] > time.time() - 10


def test_list_is_newest_first_and_session_delete_removes_reviews(tmp_path):
    store, _ = ready_store(tmp_path)
    older = store.create_review("session-1")
    newer = store.create_review("session-1", force=True)
    listed = store.list_reviews("session-1")
    assert [item["review_id"] for item in listed] == [newer["review_id"], older["review_id"]]
    assert store.session_detail("session-1")["review"]["status"] == "queued"
    store.delete_session("session-1")
    assert store.list_reviews("session-1") == []
    assert store.read_review(older["review_id"]) is None


def test_session_list_uses_indexed_review_statuses_and_skips_bad_review_files(tmp_path, monkeypatch):
    """Review files are indexed at rebuild time; list polling stays disk-free."""
    store = Store(str(tmp_path))
    statuses = {
        "queued": "queued",
        "running": "running",
        "done": "done",
        "error": "error",
        "none": None,
    }
    for sequence, session_id in enumerate(statuses):
        store.write_session_meta(session_id, {"name": session_id, "started_wall": 100 + sequence})
        status = statuses[session_id]
        if status:
            _atomic_write_json(
                store.review_path(f"review-{session_id}"),
                {
                    "review_id": f"mismatched-{session_id}",
                    "session_id": session_id,
                    "status": status,
                    "created": sequence,
                },
            )

    # The same meeting may have an older regenerated note.  The compact
    # status must follow the same newest-review rule as session detail.
    _atomic_write_json(
        store.review_path("review-queued-new"),
        {"session_id": "queued", "status": "done", "created": 100},
    )

    # A bad JSON document, an invalid status, and a malformed session id must
    # be invisible to list polling.
    store.review_path("bad-json").write_text("{broken", encoding="utf-8")
    _atomic_write_json(store.review_path("bad-status"), {"session_id": "queued", "status": "unknown"})
    _atomic_write_json(store.review_path("bad-session"), {"session_id": [], "status": "done"})

    store.reindex()

    def should_not_touch_review_files(*_args, **_kwargs):
        raise AssertionError("list_sessions must not scan the review directory")

    monkeypatch.setattr("os.scandir", should_not_touch_review_files)
    result = store.list_sessions(per_page=10, page=1)

    assert result["total"] == 5
    assert len(result["items"]) == 5
    expected = {
        "queued": {"review_id": "review-queued-new", "status": "done"},
        "running": {"review_id": "review-running", "status": "running"},
        "done": {"review_id": "review-done", "status": "done"},
        "error": {"review_id": "review-error", "status": "error"},
        "none": {"status": "none"},
    }
    for item in result["items"]:
        status = statuses[item["session_id"]]
        if status is None:
            assert item["review"] == expected[item["session_id"]]
        else:
            assert item["review"] == expected[item["session_id"]]


def test_review_status_index_tracks_lifecycle_regeneration_and_reload(tmp_path):
    store, _ = ready_store(tmp_path)

    first = store.create_review("session-1")
    assert store.list_sessions()["items"][0]["review"] == {
        "review_id": first["review_id"], "status": "queued"
    }
    store.claim_next_review()
    assert store.list_sessions()["items"][0]["review"]["status"] == "running"
    store.complete_review(first["review_id"], {"summary": "Done"})
    assert store.list_sessions()["items"][0]["review"]["status"] == "done"
    store.retry_review(first["review_id"])
    assert store.list_sessions()["items"][0]["review"]["status"] == "queued"

    regenerated = store.create_review("session-1", force=True)
    assert regenerated["review_id"] != first["review_id"]
    assert store.list_sessions()["items"][0]["review"] == {
        "review_id": regenerated["review_id"], "status": "queued"
    }

    reloaded = Store(str(tmp_path))
    assert reloaded.list_sessions()["items"][0]["review"] == {
        "review_id": regenerated["review_id"], "status": "queued"
    }


def test_session_rename_does_not_lose_concurrent_upload_metadata(tmp_path):
    store = Store(str(tmp_path))
    store.write_session_meta(
        "session-1",
        {"name": "Original", "upload": {"state": "pending", "percent": 0}},
    )
    start = threading.Barrier(3)

    def rename() -> None:
        start.wait()
        store.rename_session("session-1", "User rename")

    def update_upload() -> None:
        start.wait()
        store.update_session_meta(
            "session-1",
            lambda meta: meta | {"upload": {"state": "uploading", "percent": 50}},
        )

    rename_thread = threading.Thread(target=rename)
    upload_thread = threading.Thread(target=update_upload)
    rename_thread.start()
    upload_thread.start()
    start.wait()
    rename_thread.join(timeout=5)
    upload_thread.join(timeout=5)
    assert not rename_thread.is_alive()
    assert not upload_thread.is_alive()
    meta = store.read_session_meta("session-1")
    assert meta["name"] == "User rename"
    assert isinstance(meta["name_updated_at"], float)
    assert meta["upload"] == {"state": "uploading", "percent": 50}
