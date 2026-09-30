"""Saved recordings seen and managed from the server's Recorders page (0.7.6).

Covers, bottom to top: the protocol helpers (``remote.clean_command`` / ``sanitize_result``), the shared
status wording (``recording_status``), the recorder's on-disk half (``client/remote_recordings``), the
control channel carrying a ``result``, the server's joined endpoints with a fake recorder on a real
websocket, the recorder's ``ServerClient.recordings_status``, and the real ``MainWindow`` handler.
No audio hardware and nothing leaves localhost.
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import httpx
import pytest
from websockets.exceptions import ConnectionClosed

from meeting_notes import recording_status, remote
from meeting_notes.client import recordings, remote_recordings
from meeting_notes.client.api import ServerClient
from meeting_notes.client.queue import SessionQueue
from tests.test_client_transport import _make_session_dir
from tests.test_recorders_server import TOKEN, Env, start, wait_until
from tests.test_remote_client import (  # noqa: F401 - fixtures used by name
    CMD_ID,
    FakeChannel,
    _pump,
    _recording,
    _wait,
    make_channel,
    qt_app,
    server,
    window,
)

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _ids(n: int, prefix: str = "rec") -> List[str]:
    return [f"{prefix}-{i:04d}" for i in range(n)]


# =============================================================================
# 1. protocol: remote.clean_command / sanitize_result
# =============================================================================


def test_list_recordings_command_args():
    assert remote.clean_command("list_recordings", None) == ("list_recordings", {})
    assert remote.clean_command("list_recordings", {}) == ("list_recordings", {})
    assert remote.clean_command("list_recordings", {"offset": 0}) == ("list_recordings", {})
    assert remote.clean_command("list_recordings", {"offset": None}) == ("list_recordings", {})
    assert remote.clean_command("list_recordings", {"offset": 40}) == ("list_recordings", {"offset": 40})


@pytest.mark.parametrize("offset", [-1, True, False, "5", 1.5, [1], {"a": 1}, 10**6 + 1])
def test_list_recordings_rejects_bad_offsets(offset):
    with pytest.raises(ValueError, match="offset"):
        remote.clean_command("list_recordings", {"offset": offset})


@pytest.mark.parametrize("name", ["list_recordings", "reupload", "delete_local"])
def test_recordings_commands_reject_extra_arguments(name):
    args = {"session_ids": ["a"]} if name != "list_recordings" else {}
    args["bogus"] = 1
    with pytest.raises(ValueError, match="unexpected argument: bogus"):
        remote.clean_command(name, args)


def test_list_recordings_does_not_take_session_ids_and_ids_commands_do_not_take_offset():
    with pytest.raises(ValueError, match="unexpected argument"):
        remote.clean_command("list_recordings", {"session_ids": ["a"]})
    with pytest.raises(ValueError, match="unexpected argument"):
        remote.clean_command("reupload", {"session_ids": ["a"], "offset": 1})


@pytest.mark.parametrize("name", ["reupload", "delete_local"])
def test_ids_commands_accept_and_dedupe(name):
    assert remote.clean_command(name, {"session_ids": ["a", "b", "a", "c", "b"]}) == (
        name, {"session_ids": ["a", "b", "c"]})
    many = _ids(remote.MAX_IDS)
    assert remote.clean_command(name, {"session_ids": many})[1] == {"session_ids": many}
    longest = "x" * remote.MAX_ID_LEN
    assert remote.clean_command(name, {"session_ids": [longest]})[1] == {"session_ids": [longest]}


@pytest.mark.parametrize("name", ["reupload", "delete_local"])
@pytest.mark.parametrize(
    "ids, message",
    [
        (None, "non-empty list"),
        ([], "non-empty list"),
        ("abc", "non-empty list"),
        ({"a": 1}, "non-empty list"),
        (_ids(remote.MAX_IDS + 1), "at most"),
        (["../x"], "invalid session id"),
        (["a/b"], "invalid session id"),
        (["a\\b"], "invalid session id"),
        (["a\x00b"], "invalid session id"),
        (["."], "invalid session id"),
        ([".."], "invalid session id"),
        ([""], "invalid session id"),
        ([5], "invalid session id"),
        ([None], "invalid session id"),
        (["ok", "x" * (remote.MAX_ID_LEN + 1)], "invalid session id"),
    ],
)
def test_ids_commands_reject_bad_ids(name, ids, message):
    with pytest.raises(ValueError, match=message):
        remote.clean_command(name, {"session_ids": ids})


def test_ids_commands_need_the_argument_and_an_object():
    for name in ("reupload", "delete_local"):
        with pytest.raises(ValueError):
            remote.clean_command(name, {})
        with pytest.raises(ValueError):
            remote.clean_command(name, None)
        with pytest.raises(ValueError, match="object"):
            remote.clean_command(name, ["a"])


def test_valid_session_id():
    assert remote.valid_session_id("20260930-101500-Weekly sync-PC")
    for bad in ("", ".", "..", "a/b", "a\\b", "a\x00", None, 3, "x" * (remote.MAX_ID_LEN + 1)):
        assert not remote.valid_session_id(bad), bad


def test_sanitize_result_is_none_for_other_commands():
    for name in ("start", "stop", "mute", "retry_uploads", "set_name", "nonsense"):
        assert remote.sanitize_result(name, {"recordings": []}) is None


def _messy_row(**over):
    row = {"session_id": "rec-1", "name": "  Weekly\n sync  ", "started": 1.7e9, "duration_sec": 61.26,
           "size_bytes": 123, "valid": True, "reason": None, "active": False,
           "queue": {"state": "uploading", "percent": 250, "error": None, "attempts": 2}, "evil": "dropped"}
    row.update(over)
    return row


def test_sanitize_list_result_drops_junk_rows_and_clamps():
    raw = {
        "recordings": [
            _messy_row(),
            _messy_row(session_id="../x"),
            _messy_row(session_id=""),
            _messy_row(session_id=None),
            "junk",
            None,
            _messy_row(session_id="rec-2", queue={"state": "bananas", "percent": -5, "attempts": "x"},
                       size_bytes=-4, started="nope", duration_sec=-1, name=""),
            _messy_row(session_id="rec-3", queue="not a dict", valid=0, reason="r" * 500),
        ],
        "total": 99999999, "offset": -3, "next_offset": 7, "extra": 1,
    }
    out = remote.sanitize_result("list_recordings", raw)
    assert set(out) == {"recordings", "total", "offset", "next_offset"}
    assert [r["session_id"] for r in out["recordings"]] == ["rec-1", "rec-2", "rec-3"]
    first, second, third = out["recordings"]
    assert first["name"] == "Weekly sync" and "evil" not in first
    assert first["queue"] == {"state": "uploading", "percent": 100.0, "error": None, "attempts": 2}
    assert first["duration_sec"] == 61.3 and first["started"] == 1.7e9
    # unknown queue state -> not_queued, percent clamped to 0, junk numbers -> defaults, name falls back to the id
    assert second["queue"] == {"state": "not_queued", "percent": 0.0, "error": None, "attempts": 0}
    assert second["size_bytes"] == 0 and second["started"] is None and second["duration_sec"] is None
    assert second["name"] == "rec-2"
    assert third["queue"]["state"] == "not_queued" and third["valid"] is False and len(third["reason"]) == 200
    assert out["total"] == 10**6 and out["offset"] == 0 and out["next_offset"] == 7


def test_sanitize_list_result_defaults_and_bad_input():
    assert remote.sanitize_result("list_recordings", None) == {
        "recordings": [], "total": 0, "offset": 0, "next_offset": None}
    assert remote.sanitize_result("list_recordings", {"recordings": "nope", "next_offset": None})["recordings"] == []
    assert remote.sanitize_result("list_recordings", {"next_offset": "junk"})["next_offset"] == 0


def test_sanitize_reupload_result():
    raw = {"results": [
        {"session_id": "a", "ok": True, "code": None, "error": None},
        {"session_id": "b", "ok": "yes", "code": "not_found", "error": " no\n such "},
        {"session_id": "c", "ok": False, "code": "made_up", "error": "e" * 900},
        {"session_id": "../d", "ok": True},
        {"ok": True},
        "junk",
    ], "queued": 3, "already_queued": -2, "extra": 1}
    out = remote.sanitize_result("reupload", raw)
    assert set(out) == {"results", "queued", "already_queued"}
    assert [r["session_id"] for r in out["results"]] == ["a", "b", "c"]
    a, b, c = out["results"]
    assert a == {"session_id": "a", "ok": True, "code": None, "error": None}
    assert b["ok"] is False  # only a real True counts
    assert b["code"] == "not_found" and b["error"] == "no such"
    assert c["code"] is None and len(c["error"]) == 300
    assert out["queued"] == 3 and out["already_queued"] == 0


def test_sanitize_delete_result_keeps_bytes_and_method():
    raw = {"results": [
        {"session_id": "a", "ok": True, "bytes": 1234, "method": "recycle-bin", "code": None, "error": None},
        {"session_id": "b", "ok": False, "bytes": -1, "method": "m" * 100, "code": "uploading", "error": "x"},
        {"session_id": "c", "ok": True, "bytes": "many", "method": None},
    ], "deleted": 1, "freed_bytes": 1234}
    out = remote.sanitize_result("delete_local", raw)
    assert set(out) == {"results", "deleted", "freed_bytes"}
    a, b, c = out["results"]
    assert (a["bytes"], a["method"]) == (1234, "recycle-bin")
    assert b["bytes"] == 0 and b["method"] == "m" * 20 and b["code"] == "uploading"
    assert c["bytes"] == 0 and c["method"] is None
    assert out["deleted"] == 1 and out["freed_bytes"] == 1234


@pytest.mark.parametrize("name", ["reupload", "delete_local"])
def test_sanitize_outcomes_are_capped_at_max_ids(name):
    raw = {"results": [{"session_id": f"r{i}", "ok": True} for i in range(remote.MAX_IDS + 40)]}
    assert len(remote.sanitize_result(name, raw)["results"]) == remote.MAX_IDS


@pytest.mark.parametrize("name", ["reupload", "delete_local"])
def test_sanitize_outcomes_of_garbage(name):
    out = remote.sanitize_result(name, "garbage")
    assert out["results"] == [] and out.get("queued", out.get("deleted")) == 0


# =============================================================================
# 2. recording_status
# =============================================================================


def loc(state="not_queued", *, percent=None, error=None, active=False, valid=True, reason=None):
    return {"active": active, "valid": valid, "reason": reason,
            "queue": {"state": state, "percent": percent, "error": error, "attempts": 0}}


def srv(*, on_server=False, has_copy=False, in_trash=False, transcription=None, error=None):
    return {"on_server": on_server, "has_copy": has_copy, "in_trash": in_trash,
            "transcription": transcription, "error": error}


def test_recording_now_beats_everything():
    done = srv(on_server=True, has_copy=True, transcription="complete")
    for local in (loc(active=True), loc("recording")):
        out = recording_status.combine(local, done)
        assert (out["status"], out["label"], out["tone"]) == ("recording", "Recording now", "info")
        assert out["server_has_copy"] is True


def test_uploading_shows_percent():
    out = recording_status.combine(loc("uploading", percent=40), None)
    assert (out["status"], out["label"], out["tone"]) == ("uploading", "Uploading 40%", "info")
    assert recording_status.combine(loc("uploading", percent=39.6), None)["label"] == "Uploading 40%"
    assert recording_status.combine(loc("uploading"), None)["label"] == "Uploading"


def test_local_uploading_beats_a_server_copy():
    ready = srv(on_server=True, has_copy=True, transcription="complete")
    assert recording_status.combine(loc("uploading", percent=10), ready)["status"] == "uploading"
    assert recording_status.combine(loc("failed", error="boom"), ready)["status"] == "failed"
    assert recording_status.combine(loc("pending"), ready)["status"] == "waiting"


def test_failed_shows_the_reason_and_truncates_a_long_one():
    out = recording_status.combine(loc("failed", error="HTTP 500"), srv())
    assert (out["status"], out["label"], out["tone"]) == ("failed", "Upload failed: HTTP 500", "error")
    assert recording_status.combine(loc("failed"), srv())["label"] == "Upload failed: unknown error"
    long = recording_status.combine(loc("failed", error="word " * 200), srv())["label"]
    assert long.startswith("Upload failed: word") and long.endswith("…")
    assert len(long) <= len("Upload failed: ") + 160


def test_waiting_with_and_without_a_last_error():
    plain = recording_status.combine(loc("pending"), srv())
    assert (plain["status"], plain["label"], plain["tone"], plain["detail"]) == (
        "waiting", "Waiting to upload", "info", None)
    detailed = recording_status.combine(loc("pending", error="connection refused"), srv())
    assert detailed["label"] == "Waiting to upload" and detailed["detail"] == "Last try failed: connection refused"


@pytest.mark.parametrize(
    "transcription, status, label, tone",
    [
        ("complete", "uploaded_ready", "Uploaded · transcript ready", "ok"),
        ("transcribing", "uploaded_transcribing", "Uploaded · transcribing", "info"),
        ("queued", "uploaded_queued", "Uploaded · transcription queued", "info"),
        ("error", "uploaded_error", "Uploaded · transcription failed", "warn"),
    ],
)
def test_uploaded_states(transcription, status, label, tone):
    out = recording_status.combine(loc(), srv(on_server=True, has_copy=True, transcription=transcription))
    assert (out["status"], out["label"], out["tone"], out["server_has_copy"]) == (status, label, tone, True)


def test_awaiting_transcript_falls_through_to_the_server():
    local = loc("awaiting_transcript", percent=100.0)
    ready = recording_status.combine(local, srv(on_server=True, has_copy=True, transcription="complete"))
    assert ready["status"] == "uploaded_ready"
    assert recording_status.combine(local, None)["status"] == "unknown"
    assert recording_status.combine(local, srv())["status"] == "not_on_server"


def test_uploaded_error_detail_is_the_server_error_truncated():
    row = srv(on_server=True, has_copy=True, transcription="error", error="whisper exploded")
    assert recording_status.combine(loc(), row)["detail"] == "whisper exploded"
    assert recording_status.combine(loc(), srv(on_server=True, has_copy=True, transcription="error"))["detail"] is None
    long = recording_status.combine(loc(), srv(on_server=True, has_copy=True, transcription="error", error="e " * 300))
    assert long["detail"].endswith("…") and len(long["detail"]) <= 160
    assert recording_status.combine(loc(), srv(on_server=True, has_copy=True, transcription="complete",
                                               error="stale"))["detail"] is None


def test_partial_is_on_server_without_a_job():
    out = recording_status.combine(loc(), srv(on_server=True))
    assert (out["status"], out["label"], out["tone"], out["server_has_copy"]) == (
        "partial", "Partly uploaded", "warn", False)
    assert out["detail"]


def test_in_trash_not_on_server_invalid_and_unknown():
    trash = recording_status.combine(loc(), srv(in_trash=True))
    assert (trash["status"], trash["label"], trash["tone"], trash["server_has_copy"]) == (
        "in_trash", "In server trash", "warn", False)
    assert "30 days" in trash["detail"]
    none = recording_status.combine(loc(), srv())
    assert (none["status"], none["label"], none["tone"]) == ("not_on_server", "Not on server", "warn")
    bad = recording_status.combine(loc(valid=False, reason="wav is empty"), srv())
    assert (bad["status"], bad["label"], bad["tone"]) == ("invalid", "Can't upload: wav is empty", "error")
    assert recording_status.combine(loc(valid=False), srv())["label"] == "Can't upload: not a valid recording"
    unknown = recording_status.combine(loc(), None)
    assert (unknown["status"], unknown["label"], unknown["tone"], unknown["server_has_copy"]) == (
        "unknown", "Server status unknown", "muted", False)
    # An invalid recording the server does have is still reported as what the server holds.
    assert recording_status.combine(loc(valid=False, reason="x"),
                                    srv(on_server=True, has_copy=True, transcription="complete"))["status"] == "uploaded_ready"
    # A folder in the server's trash is reported as trash even when it is also invalid.
    assert recording_status.combine(loc(valid=False, reason="x"), srv(in_trash=True))["status"] == "in_trash"


def test_tone_table_covers_every_status():
    assert recording_status.TONES == {
        "recording": "info", "uploading": "info", "waiting": "info", "failed": "error",
        "uploaded_ready": "ok", "uploaded_transcribing": "info", "uploaded_queued": "info",
        "uploaded_error": "warn", "partial": "warn", "in_trash": "warn", "not_on_server": "warn",
        "invalid": "error", "unknown": "muted",
    }
    assert set(recording_status.TONES.values()) == {"ok", "info", "warn", "error", "muted"}


@pytest.mark.parametrize(
    "row, in_trash, expected",
    [
        (None, False, dict(on_server=False, has_copy=False, in_trash=False, transcription=None, error=None)),
        (None, True, dict(on_server=False, has_copy=False, in_trash=True, transcription=None, error=None)),
        ({"latest_state": "done"}, False, dict(on_server=True, has_copy=True, in_trash=False,
                                               transcription="complete", error=None)),
        ({"latest_state": "running"}, False, dict(on_server=True, has_copy=True, in_trash=False,
                                                  transcription="transcribing", error=None)),
        ({"latest_state": "queued"}, False, dict(on_server=True, has_copy=True, in_trash=False,
                                                 transcription="queued", error=None)),
        ({"latest_state": "error", "latest_error": "kaboom"}, False,
         dict(on_server=True, has_copy=True, in_trash=False, transcription="error", error="kaboom")),
        ({"latest_state": None}, False, dict(on_server=True, has_copy=False, in_trash=False,
                                             transcription=None, error=None)),
        ({"latest_state": "done", "latest_error": "old"}, False,
         dict(on_server=True, has_copy=True, in_trash=False, transcription="complete", error=None)),
        ({"latest_state": "done"}, True, dict(on_server=True, has_copy=True, in_trash=False,
                                              transcription="complete", error=None)),
        ({}, False, dict(on_server=True, has_copy=False, in_trash=False, transcription=None, error=None)),
    ],
)
def test_server_state_from_row(row, in_trash, expected):
    assert recording_status.server_state_from_row(row, in_trash) == expected


def test_server_has_copy_is_true_exactly_when_a_job_exists_on_the_live_row():
    for latest, expected in (("done", True), ("running", True), ("queued", True), ("error", True), (None, False)):
        server_row = recording_status.server_state_from_row({"latest_state": latest})
        assert recording_status.combine(loc(), server_row)["server_has_copy"] is expected, latest
    assert recording_status.combine(loc(), recording_status.server_state_from_row(None, True))["server_has_copy"] is False
    assert recording_status.combine(loc(), None)["server_has_copy"] is False


def test_delete_warning_names_the_trash():
    assert "Recycle Bin" in recording_status.delete_warning()
    assert "Trash" in recording_status.delete_warning("Trash")


# =============================================================================
# 3. client/remote_recordings on real folders
# =============================================================================


def make_rec(save: Path, name: str, *, started: Optional[float] = None, title: Optional[str] = None) -> Path:
    folder = _make_session_dir(save, name)
    meta = json.loads((folder / "session.json").read_text(encoding="utf-8"))
    if started is not None:
        meta["started_wall"] = started
    if title:
        meta["name"] = title
    (folder / "session.json").write_text(json.dumps(meta), encoding="utf-8")
    return folder


def make_invalid(save: Path, name: str, started: float) -> Path:
    folder = save / name
    folder.mkdir()
    (folder / "session.json").write_text(json.dumps({"started_wall": started, "tracks": {}}), encoding="utf-8")
    return folder


@pytest.fixture
def disk(tmp_path):
    save = tmp_path / "Meeting Notes"
    save.mkdir()
    return save, SessionQueue.for_save_dir(save)


def test_queue_view_not_queued(disk):
    save, queue = disk
    folder = make_rec(save, "a")
    assert remote_recordings.queue_view(queue, folder) == {
        "state": "not_queued", "percent": None, "error": None, "attempts": 0}


def test_queue_view_pending_then_with_a_last_error(disk):
    save, queue = disk
    folder = make_rec(save, "a")
    entry = queue.enqueue(folder)
    assert remote_recordings.queue_view(queue, folder) == {
        "state": "pending", "percent": None, "error": None, "attempts": 0}
    queue.mark_attempt_failed(entry, "connection refused")
    assert remote_recordings.queue_view(queue, folder) == {
        "state": "pending", "percent": None, "error": "connection refused", "attempts": 1}


def test_queue_view_failed(disk):
    save, queue = disk
    folder = make_rec(save, "a")
    entry = queue.enqueue(folder)
    queue.mark_attempt_failed(entry, "HTTP 500", terminal=True)
    assert remote_recordings.queue_view(queue, folder) == {
        "state": "failed", "percent": None, "error": "HTTP 500", "attempts": 1}


def test_queue_view_uploading_by_claim_and_by_upload_state(disk):
    save, queue = disk
    claimed = make_rec(save, "claimed")
    entry = queue.enqueue(claimed)
    assert queue.claim(entry)
    assert remote_recordings.queue_view(queue, claimed)["state"] == "uploading"
    queue.release(entry)
    assert remote_recordings.queue_view(queue, claimed)["state"] == "pending"

    progress = make_rec(save, "progress")
    entry = queue.enqueue(progress)
    state = queue.read_state(entry)
    state.update(upload_state="uploading", upload_percent=42.5)
    queue.write_state(entry, state)
    view = remote_recordings.queue_view(queue, progress)
    assert view["state"] == "uploading" and view["percent"] == 42.5 and view["error"] is None


def test_queue_view_awaiting_transcript_unless_someone_holds_the_claim(disk):
    save, queue = disk
    folder = make_rec(save, "a")
    entry = queue.enqueue(folder)
    state = queue.read_state(entry)
    state.update(finalized=True, job_id="j1", last_error="stale")
    queue.write_state(entry, state)
    assert remote_recordings.queue_view(queue, folder) == {
        "state": "awaiting_transcript", "percent": 100.0, "error": None, "attempts": 0}
    assert queue.claim(entry)
    assert remote_recordings.queue_view(queue, folder)["state"] == "uploading"


def test_describe_marks_the_active_dir(disk):
    save, queue = disk
    folder = make_rec(save, "live", started=2000.0, title="Board call")
    info = recordings.inspect_recording(folder, queue)
    quiet = remote_recordings.describe(info, queue, None)
    assert quiet["session_id"] == "live" and quiet["name"] == "Board call" and quiet["started"] == 2000.0
    assert quiet["valid"] is True and quiet["reason"] is None and quiet["active"] is False
    assert quiet["size_bytes"] > 0 and quiet["duration_sec"]
    assert quiet["queue"]["state"] == "not_queued"
    active = remote_recordings.describe(info, queue, folder)
    assert active["active"] is True and active["valid"] is False and active["reason"] == "Recording in progress"
    assert active["queue"] == {"state": "recording", "percent": None, "error": None, "attempts": 0}
    other = remote_recordings.describe(info, queue, save / "other")
    assert other["active"] is False
    # What it describes survives the wire sanitizer unchanged.
    assert remote.sanitize_recording(active) == active


def test_list_recordings_newest_first_with_invalid_ones_and_a_reason(disk):
    save, queue = disk
    make_rec(save, "old", started=1000.0)
    make_rec(save, "new", started=3000.0)
    make_invalid(save, "broken", 2000.0)
    (save / "not-a-recording").mkdir()  # no session.json / wav: not listed
    (save / ".upload-queue" / "x").mkdir(exist_ok=True)
    out = remote_recordings.list_recordings(save, queue)
    assert [r["session_id"] for r in out["recordings"]] == ["new", "broken", "old"]
    assert out["total"] == 3 and out["offset"] == 0 and out["next_offset"] is None
    broken = out["recordings"][1]
    assert broken["valid"] is False and "No audio tracks" in broken["reason"]
    assert out["recordings"][0]["valid"] is True
    assert remote.sanitize_result("list_recordings", out) == out


def test_list_recordings_marks_the_active_one(disk):
    save, queue = disk
    live = make_rec(save, "live", started=5.0)
    make_rec(save, "done", started=1.0)
    rows = {r["session_id"]: r for r in remote_recordings.list_recordings(save, queue, live)["recordings"]}
    assert rows["live"]["active"] is True and rows["live"]["queue"]["state"] == "recording"
    assert rows["done"]["active"] is False


def test_list_recordings_empty_save_folder(disk):
    save, queue = disk
    assert remote_recordings.list_recordings(save, queue) == {
        "recordings": [], "total": 0, "offset": 0, "next_offset": None}
    missing = save / "nope"
    assert remote_recordings.list_recordings(missing, queue)["total"] == 0


def test_list_recordings_pages_by_budget_and_the_chain_returns_every_one_once(disk):
    save, queue = disk
    names = [f"rec{i:02d}" for i in range(7)]
    for i, name in enumerate(names):
        make_rec(save, name, started=1000.0 + i)
    expected = list(reversed(names))  # newest first
    # A budget too small for even one row still returns one row per page (progress is guaranteed).
    seen, offset, pages = [], 0, 0
    while offset is not None:
        page = remote_recordings.list_recordings(save, queue, None, offset, budget=1)
        assert page["offset"] == offset and page["total"] == 7 and len(page["recordings"]) == 1
        seen += [r["session_id"] for r in page["recordings"]]
        offset = page["next_offset"]
        pages += 1
        assert pages <= 7
    assert seen == expected and pages == 7
    # A budget that holds about two rows per page.
    one = len(json.dumps(remote_recordings.list_recordings(save, queue)["recordings"][0], separators=(",", ":"))) + 1
    seen, offset = [], 0
    while offset is not None:
        page = remote_recordings.list_recordings(save, queue, None, offset, budget=one * 2 + 5)
        assert 1 <= len(page["recordings"]) <= 2
        seen += [r["session_id"] for r in page["recordings"]]
        offset = page["next_offset"]
    assert seen == expected
    # Offset past the end is an empty last page.
    assert remote_recordings.list_recordings(save, queue, None, 50) == {
        "recordings": [], "total": 7, "offset": 50, "next_offset": None}


def test_list_recordings_limit_caps_the_rows_but_not_the_total(disk):
    save, queue = disk
    for i in range(5):
        make_rec(save, f"r{i}", started=100.0 + i)
    page = remote_recordings.list_recordings(save, queue, limit=3)
    assert [r["session_id"] for r in page["recordings"]] == ["r4", "r3", "r2"]
    assert page["total"] == 5 and page["next_offset"] is None
    # Paging in tiny steps never walks past the limit either.
    offset, got = 0, []
    while offset is not None:
        part = remote_recordings.list_recordings(save, queue, None, offset, budget=1, limit=3)
        got += [r["session_id"] for r in part["recordings"]]
        offset = part["next_offset"]
    assert got == ["r4", "r3", "r2"]


def _submit(queue):
    return lambda folders: recordings.reupload(queue, folders)


def test_reupload_queues_then_reports_already_queued(disk):
    save, queue = disk
    folder = make_rec(save, "a")
    out = remote_recordings.reupload(save, queue, ["a"], _submit(queue))
    assert out == {"results": [{"session_id": "a", "ok": True, "code": None, "error": None}],
                   "queued": 1, "already_queued": 0}
    assert queue.is_queued(folder)
    again = remote_recordings.reupload(save, queue, ["a"], _submit(queue))
    assert again["queued"] == 0 and again["already_queued"] == 1 and again["results"][0]["ok"] is True
    assert remote.sanitize_result("reupload", again) == again


def test_reupload_refuses_unknown_active_and_invalid_and_keeps_request_order(disk):
    save, queue = disk
    good = make_rec(save, "good")
    live = make_rec(save, "live")
    make_invalid(save, "broken", 1.0)
    calls = []

    def submit(folders):
        calls.append([f.name for f in folders])
        return recordings.reupload(queue, folders)

    ids = ["broken", "ghost", "good", "live", "../good"]
    out = remote_recordings.reupload(save, queue, ids, submit, active_dir=live)
    assert [r["session_id"] for r in out["results"]] == ids
    by_id = {r["session_id"]: r for r in out["results"]}
    assert by_id["good"]["ok"] is True and by_id["good"]["code"] is None
    assert by_id["ghost"]["code"] == "not_found" and by_id["ghost"]["ok"] is False
    assert by_id["../good"]["code"] == "not_found"
    assert by_id["live"]["code"] == "active_recording" and by_id["live"]["ok"] is False
    assert by_id["broken"]["code"] == "invalid" and "No audio tracks" in by_id["broken"]["error"]
    assert out["queued"] == 1 and out["already_queued"] == 0
    assert sorted(calls[0]) == ["broken", "good"]  # the active and unknown ids never reach the queue
    assert queue.is_queued(good) and not queue.is_queued(live)


def test_reupload_does_not_call_submit_when_nothing_is_eligible(disk):
    save, queue = disk
    live = make_rec(save, "live")

    def boom(_folders):
        raise AssertionError("must not be called")

    out = remote_recordings.reupload(save, queue, ["live", "ghost"], boom, active_dir=live)
    assert [r["code"] for r in out["results"]] == ["active_recording", "not_found"]
    assert out["queued"] == 0 and out["already_queued"] == 0


class Bin:
    """A stand-in for the Recycle Bin: really moves the folder out of the save folder."""

    def __init__(self, root: Path, fail: Optional[set] = None):
        self.root = root
        self.fail = fail or set()
        self.calls: List[str] = []

    def __call__(self, folder: Path) -> str:
        self.calls.append(Path(folder).name)
        if Path(folder).name in self.fail:
            raise OSError("Access is denied")
        self.root.mkdir(exist_ok=True)
        shutil.move(str(folder), str(self.root / Path(folder).name))
        return "recycle-bin"


def test_delete_local_moves_folders_drops_the_queue_entry_and_counts_bytes(disk, tmp_path):
    save, queue = disk
    a, b, keep = make_rec(save, "a"), make_rec(save, "b"), make_rec(save, "keep")
    queue.enqueue(a)
    queue.enqueue(keep)
    size_a, size_b = recordings.dir_size(a), recordings.dir_size(b)
    bin_ = Bin(tmp_path / "bin")
    out = remote_recordings.delete_local(save, queue, ["a", "b"], None, bin_)
    assert out["deleted"] == 2 and out["freed_bytes"] == size_a + size_b > 0
    assert [(r["session_id"], r["ok"], r["code"], r["bytes"], r["method"]) for r in out["results"]] == [
        ("a", True, None, size_a, "recycle-bin"), ("b", True, None, size_b, "recycle-bin")]
    assert not a.exists() and not b.exists() and keep.exists()
    assert (tmp_path / "bin" / "a").is_dir()
    assert not queue.is_queued(a) and queue.is_queued(keep)
    assert remote.sanitize_result("delete_local", out) == out


def test_delete_local_refuses_the_active_recording(disk, tmp_path):
    save, queue = disk
    live = make_rec(save, "live")
    bin_ = Bin(tmp_path / "bin")
    out = remote_recordings.delete_local(save, queue, ["live"], live, bin_)
    assert out["deleted"] == 0 and out["freed_bytes"] == 0
    assert out["results"][0]["code"] == "active_recording" and out["results"][0]["ok"] is False
    assert live.exists() and bin_.calls == []


def test_delete_local_refuses_a_recording_that_is_uploading(disk, tmp_path):
    save, queue = disk
    folder = make_rec(save, "up")
    entry = queue.enqueue(folder)
    assert queue.claim(entry)
    bin_ = Bin(tmp_path / "bin")
    out = remote_recordings.delete_local(save, queue, ["up"], None, bin_)
    assert out["results"][0]["code"] == "uploading" and out["deleted"] == 0
    assert folder.exists() and queue.is_queued(folder) and bin_.calls == []
    queue.release(entry)
    assert remote_recordings.delete_local(save, queue, ["up"], None, bin_)["deleted"] == 1


def test_delete_local_only_matches_folders_in_the_save_folder_never_paths(disk, tmp_path):
    save, queue = disk
    make_rec(save, "inside")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("keep me")
    sibling = tmp_path / "Meeting Notes - sibling"
    sibling.mkdir()
    bin_ = Bin(tmp_path / "bin")
    ids = ["..", ".", str(outside), str(outside).replace("\\", "/"), "../outside", "..\\outside",
           "../Meeting Notes - sibling", "inside/..", ""]
    out = remote_recordings.delete_local(save, queue, ids, None, bin_)
    assert [r["code"] for r in out["results"]] == ["not_found"] * len(ids)
    assert out["deleted"] == 0 and out["freed_bytes"] == 0
    assert bin_.calls == []
    assert (outside / "precious.txt").read_text() == "keep me"
    assert sibling.is_dir() and (save / "inside").is_dir() and save.is_dir()
    # A hidden folder (the queue's own) is not a recording either.
    assert remote_recordings.delete_local(save, queue, [".upload-queue"], None, bin_)["results"][0]["code"] == "not_found"
    assert queue.queue_dir.is_dir()


def test_delete_local_a_failing_remover_fails_that_id_and_the_rest_proceed(disk, tmp_path, caplog):
    save, queue = disk
    a, b, c = make_rec(save, "a"), make_rec(save, "b"), make_rec(save, "c")
    for f in (a, b, c):
        queue.enqueue(f)
    bin_ = Bin(tmp_path / "bin", fail={"b"})
    with caplog.at_level(logging.WARNING):
        out = remote_recordings.delete_local(save, queue, ["a", "b", "c"], None, bin_)
    by_id = {r["session_id"]: r for r in out["results"]}
    assert by_id["a"]["ok"] and by_id["c"]["ok"]
    assert by_id["b"]["ok"] is False and by_id["b"]["code"] == "failed" and "Access is denied" in by_id["b"]["error"]
    assert out["deleted"] == 2
    assert b.exists() and queue.is_queued(b)  # still there and still queued for upload
    assert not a.exists() and not c.exists()
    assert bin_.calls == ["a", "b", "c"]


# =============================================================================
# 4. ControlChannel.send_ack(result=...) against a fake server
# =============================================================================


def _row(sid="rec-1", **over):
    row = {"session_id": sid, "name": f"Recording {sid}", "started": 1_700_000_000.0, "duration_sec": 60.0,
           "size_bytes": 1000, "valid": True, "reason": None, "active": False,
           "queue": {"state": "not_queued", "percent": None, "error": None, "attempts": 0}}
    row.update(over)
    return row


def _answering(holder, result_for: Callable[[str, dict], Any], got: Optional[list] = None):
    def on_command(command_id, name, args):
        if got is not None:
            got.append((command_id, name, args))
        holder["channel"].send_ack(command_id, True, None, None, None, result=result_for(name, args))

    return on_command


def test_ack_carries_the_sanitized_result_of_a_recordings_command(server, make_channel):
    holder, got = {}, []
    messy = {"recordings": [_row("rec-1", queue={"state": "uploading", "percent": 400, "error": None}),
                            {"session_id": "../evil"}, "junk"], "total": 3, "offset": 0, "next_offset": None,
             "extra": "dropped"}
    holder["channel"] = make_channel(on_command=_answering(holder, lambda n, a: messy, got))
    assert _wait(lambda: server.of_type("hello"))
    server.send_command("list_recordings", {"offset": 2})
    assert _wait(lambda: server.of_type("ack"))
    ack_ = server.of_type("ack")[0]
    assert got == [(CMD_ID, "list_recordings", {"offset": 2})]
    assert ack_["ok"] is True and ack_["command_id"] == CMD_ID
    assert ack_["result"] == remote.sanitize_result("list_recordings", messy)
    assert [r["session_id"] for r in ack_["result"]["recordings"]] == ["rec-1"]
    assert ack_["result"]["recordings"][0]["queue"]["percent"] == 100.0
    assert "extra" not in ack_["result"]


@pytest.mark.parametrize("name", ["reupload", "delete_local"])
def test_ack_result_for_the_id_commands(server, make_channel, name):
    holder = {}
    raw = {"results": [{"session_id": "a", "ok": True, "bytes": 5, "method": "recycle-bin"},
                       {"session_id": "b", "ok": False, "code": "uploading", "error": "busy"}],
           "queued": 1, "already_queued": 0, "deleted": 1, "freed_bytes": 5}
    holder["channel"] = make_channel(on_command=_answering(holder, lambda n, a: raw))
    assert _wait(lambda: server.of_type("hello"))
    server.send_command(name, {"session_ids": ["a", "b"]})
    assert _wait(lambda: server.of_type("ack"))
    assert server.of_type("ack")[0]["result"] == remote.sanitize_result(name, raw)


def test_a_result_bigger_than_the_cap_is_dropped_but_the_ack_still_goes_out(server, make_channel):
    holder = {}
    huge = {"recordings": [_row(f"r{i}", name="n" * 200) for i in range(3000)], "total": 3000}
    assert len(json.dumps(remote.sanitize_result("list_recordings", huge), separators=(",", ":"))) > remote.MAX_RESULT_BYTES
    holder["channel"] = make_channel(on_command=_answering(holder, lambda n, a: huge))
    assert _wait(lambda: server.of_type("hello"))
    server.send_command("list_recordings")
    assert _wait(lambda: server.of_type("ack"))
    ack_ = server.of_type("ack")[0]
    assert ack_["ok"] is True and ack_["command_id"] == CMD_ID and "result" not in ack_
    assert set(ack_["state"]) == set(remote.sanitize_state({}))


def test_a_large_result_under_the_cap_is_delivered_in_one_frame(server, make_channel):
    holder = {}
    big = {"recordings": [_row(f"r{i:04d}", name="n" * 200) for i in range(1000)], "total": 1000}
    holder["channel"] = make_channel(on_command=_answering(holder, lambda n, a: big))
    assert _wait(lambda: server.of_type("hello"))
    server.send_command("list_recordings")
    assert _wait(lambda: server.of_type("ack"), timeout=8)
    ack_ = server.of_type("ack")[0]
    assert len(ack_["result"]["recordings"]) == 1000
    assert len(json.dumps(ack_)) <= remote.MAX_ACK_FRAME_BYTES


def test_only_recordings_commands_ever_carry_a_result(server, make_channel):
    holder = {}
    holder["channel"] = make_channel(on_command=_answering(holder, lambda n, a: {"recordings": [_row()]}))
    assert _wait(lambda: server.of_type("hello"))
    server.send_command("stop")
    server.send_command("retry_uploads", command_id="aaaaaaaa11")
    assert _wait(lambda: len(server.of_type("ack")) == 2)
    assert all("result" not in a for a in server.of_type("ack"))


def test_a_result_for_an_unknown_command_id_is_dropped(server, make_channel):
    channel = make_channel()
    assert _wait(lambda: server.of_type("hello"))
    channel.send_ack("deadbeefdead", True, result={"recordings": [_row()]})
    assert _wait(lambda: server.of_type("ack"))
    assert "result" not in server.of_type("ack")[0]


def test_ack_without_a_result_is_unchanged(server, make_channel):
    holder = {}
    holder["channel"] = make_channel(on_command=_answering(holder, lambda n, a: None))
    assert _wait(lambda: server.of_type("hello"))
    server.send_command("list_recordings")
    assert _wait(lambda: server.of_type("ack"))
    assert "result" not in server.of_type("ack")[0]


def test_the_channel_accepts_a_command_frame_up_to_the_command_cap(server, make_channel):
    got = []
    make_channel(on_command=lambda *a: got.append(a))
    assert _wait(lambda: server.of_type("hello"))
    ids = [("%03d-" % i) + "x" * (remote.MAX_ID_LEN - 4) for i in range(remote.MAX_IDS)]
    frame = {"type": "command", "command_id": CMD_ID, "command": "delete_local", "args": {"session_ids": ids}}
    room = remote.MAX_COMMAND_BYTES - len(json.dumps(frame)) - 40
    frame["pad"] = "p" * room  # a frame just under the cap; a valid one can't be this big without padding
    assert remote.MAX_MESSAGE_BYTES < len(json.dumps(frame)) <= remote.MAX_COMMAND_BYTES
    server.outbox.put(frame)
    assert _wait(lambda: got)
    assert got[0][1] == "delete_local" and got[0][2]["session_ids"] == ids


def test_a_frame_over_the_command_cap_drops_the_connection_and_it_reconnects(server, make_channel):
    got = []
    make_channel(on_command=lambda *a: got.append(a))
    assert _wait(lambda: server.of_type("hello"))
    server.outbox.put({"type": "command", "command_id": CMD_ID, "command": "stop",
                       "pad": "p" * (remote.MAX_COMMAND_BYTES + 100)})
    assert _wait(lambda: server.connections >= 2, timeout=5)
    assert got == []


# =============================================================================
# 5. the server end to end with a fake recorder on a real websocket
# =============================================================================


@pytest.fixture()
def env(tmp_path, monkeypatch):
    server_, e = start(tmp_path, monkeypatch)
    e.hub.command_timeout = 0.5
    yield e
    server_.stop()


@pytest.fixture()
def secured(tmp_path, monkeypatch):
    server_, e = start(tmp_path, monkeypatch, TOKEN)
    e.hub.command_timeout = 0.5
    yield e
    server_.stop()


class FakeRecorder:
    """A recorder on a real websocket. ``handler(command, args, command_id)`` returns the ack fields as a
    dict, a ready-made frame as a str (sent verbatim), or ``None`` (stay silent)."""

    def __init__(self, env: Env, handler: Callable[[str, dict, str], Any], **hello: Any):
        self.ws = env.recorder(**hello)
        self.instance_id = self.ws.instance_id
        self.handler = handler
        self.seen: List[tuple] = []
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop:
            try:
                message = self.ws.recv(timeout=0.1)
            except TimeoutError:
                continue
            except ConnectionClosed:
                return
            frame = json.loads(message)
            if frame.get("type") != "command":
                continue
            self.seen.append((frame["command"], frame["args"]))
            reply = self.handler(frame["command"], frame["args"], frame["command_id"])
            try:
                if isinstance(reply, str):
                    self.ws.send(reply)
                elif reply is not None:
                    self.ws.send(json.dumps({"type": "ack", "command_id": frame["command_id"], **reply}))
            except ConnectionClosed:
                return

    def commands(self) -> List[str]:
        return [c for c, _ in self.seen]

    def close(self) -> None:
        self._stop = True
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001
            pass


def ack(result: Optional[dict] = None, **over: Any) -> dict:
    reply = {"ok": True, "code": None, "error": None, "state": {"status": "idle"}}
    if result is not None:
        reply["result"] = result
    reply.update(over)
    return reply


def rec_row(sid, started, *, state="not_queued", percent=None, error=None, valid=True, reason=None,
            active=False, name=None):
    return {"session_id": sid, "name": name or f"Rec {sid}", "started": started, "duration_sec": 60.0,
            "size_bytes": 1000, "valid": valid, "reason": reason, "active": active,
            "queue": {"state": state, "percent": percent, "error": error, "attempts": 0}}


def lister(rows: List[dict], page: Optional[int] = None):
    def handle(command, args, _cid):
        assert command == "list_recordings", command
        start_at = args.get("offset", 0)
        end = len(rows) if page is None else start_at + page
        return ack({"recordings": rows[start_at:end], "total": len(rows), "offset": start_at,
                    "next_offset": end if end < len(rows) else None})

    return handle


def seed(store, sid, job=None, *, error=None, trash=False):
    """A server-side meeting: metadata, then (optionally) a job in the given state, then (optionally) trashed."""
    store.write_session_meta(sid, {"name": f"Meeting {sid}", "started_wall": 1000.0, "duration_sec": 60,
                                   "device": "Laptop"})
    if job is not None:
        job_id = store.create_job(sid)
        if job != "queued":
            fields = {"state": job, "progress": 1.0 if job == "done" else 0.5}
            if error:
                fields["error"] = error
            store.update_job(job_id, **fields)
    if trash:
        store.trash_session(sid)


def get_recordings(env: Env, iid: str, **kw) -> httpx.Response:
    with env.web() as http:
        return http.get(f"/v1/recorders/{iid}/recordings", **kw)


def test_recordings_are_joined_with_what_the_server_knows(env):
    store = env.app.state.store
    seed(store, "s-ready", "done")
    seed(store, "s-trans", "running")
    seed(store, "s-queued", "queued")
    seed(store, "s-err", "error", error="whisper exploded")
    seed(store, "s-partial")
    seed(store, "s-trash", "done", trash=True)
    seed(store, "s-up", "done")  # local uploading wins even though the server has a copy
    rows = [
        rec_row("s-ready", 1100.0),
        rec_row("s-trans", 1200.0),
        rec_row("s-queued", 1300.0),
        rec_row("s-err", 1400.0),
        rec_row("s-partial", 1500.0),
        rec_row("s-trash", 1600.0),
        rec_row("s-none", 1700.0),
        rec_row("s-up", 1800.0, state="uploading", percent=40.0),
        rec_row("s-fail", 1900.0, state="failed", error="HTTP 500"),
        rec_row("s-wait", 2000.0, state="pending"),
        rec_row("s-rec", 2100.0, active=True, valid=False, reason="Recording in progress"),
        rec_row("s-bad", 2200.0, valid=False, reason="wav is empty"),
    ]
    recorder = FakeRecorder(env, lister(rows), device="Desk PC", platform="Windows 11")
    r = get_recordings(env, recorder.instance_id)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["instance_id"] == recorder.instance_id and body["device"] == "Desk PC"
    assert body["trash_name"] == "Recycle Bin" and body["total"] == 12 and body["truncated"] is False
    out = body["recordings"]
    assert [x["session_id"] for x in out] == [x["session_id"] for x in reversed(rows)]  # newest first
    by_id = {x["session_id"]: x for x in out}
    expected = {
        "s-ready": ("uploaded_ready", "Uploaded · transcript ready", "ok", True),
        "s-trans": ("uploaded_transcribing", "Uploaded · transcribing", "info", True),
        "s-queued": ("uploaded_queued", "Uploaded · transcription queued", "info", True),
        "s-err": ("uploaded_error", "Uploaded · transcription failed", "warn", True),
        "s-partial": ("partial", "Partly uploaded", "warn", False),
        "s-trash": ("in_trash", "In server trash", "warn", False),
        "s-none": ("not_on_server", "Not on server", "warn", False),
        "s-up": ("uploading", "Uploading 40%", "info", True),
        "s-fail": ("failed", "Upload failed: HTTP 500", "error", False),
        "s-wait": ("waiting", "Waiting to upload", "info", False),
        "s-rec": ("recording", "Recording now", "info", False),
        "s-bad": ("invalid", "Can't upload: wav is empty", "error", False),
    }
    for sid, (status, label, tone, copy) in expected.items():
        item = by_id[sid]
        assert (item["status"], item["label"], item["tone"], item["server_has_copy"]) == (status, label, tone, copy), sid
    assert by_id["s-err"]["detail"] == "whisper exploded"
    on_server = {"s-ready", "s-trans", "s-queued", "s-err", "s-partial", "s-up"}
    for sid, item in by_id.items():
        assert item["meeting_url"] == (f"/sessions/{sid}" if sid in on_server else None), sid
    assert by_id["s-trash"]["server"]["in_trash"] is True and by_id["s-trash"]["server"]["on_server"] is False
    assert by_id["s-ready"]["server"]["transcription"] == "complete"
    assert by_id["s-ready"]["queue"]["state"] == "not_queued" and by_id["s-ready"]["name"] == "Rec s-ready"
    summary = body["summary"]
    assert summary["total"] == 12 and summary["uploaded"] == 4
    assert summary["by_status"] == {
        "uploaded_ready": 1, "uploaded_transcribing": 1, "uploaded_queued": 1, "uploaded_error": 1,
        "partial": 1, "in_trash": 1, "not_on_server": 1, "uploading": 1, "failed": 1, "waiting": 1,
        "recording": 1, "invalid": 1}
    assert recorder.seen == [("list_recordings", {})]
    recorder.close()


def test_the_trash_is_called_trash_for_a_mac_recorder(env):
    recorder = FakeRecorder(env, lister([]), platform="macOS 26")
    body = get_recordings(env, recorder.instance_id).json()
    assert body["trash_name"] == "Trash" and body["recordings"] == [] and body["total"] == 0
    recorder.close()


def test_a_refusal_is_passed_through_as_ok_false(env):
    recorder = FakeRecorder(env, lambda c, a, i: ack(ok=False, code="remote_control_disabled",
                                                     error="Remote control is turned off in this app's Settings."))
    r = get_recordings(env, recorder.instance_id)
    assert r.status_code == 200
    assert r.json() == {"ok": False, "code": "remote_control_disabled",
                        "error": "Remote control is turned off in this app's Settings."}
    recorder.close()


def test_an_unknown_offline_or_badly_named_recorder(env):
    with env.web() as http:
        assert http.get(f"/v1/recorders/{uuid.uuid4().hex}/recordings").status_code == 404
        assert http.get("/v1/recorders/not-an-id/recordings").status_code == 400
        assert http.post("/v1/recorders/not-an-id/recordings/reupload", json={"session_ids": ["a"]}).status_code == 400
        assert http.post("/v1/recorders/not-an-id/recordings/delete", json={"session_ids": ["a"]}).status_code == 400
    recorder = FakeRecorder(env, lister([]))
    iid = recorder.instance_id
    recorder.close()
    wait_until(lambda: not [i for i in env.hub.list_items() if i["instance_id"] == iid])
    assert get_recordings(env, iid).status_code == 404


def test_a_recorder_paging_three_at_a_time_is_stitched(env):
    rows = [rec_row(f"p-{i:02d}", 100.0 + i) for i in range(8)]
    recorder = FakeRecorder(env, lister(rows, page=3))
    body = get_recordings(env, recorder.instance_id).json()
    assert [x["session_id"] for x in body["recordings"]] == [f"p-{i:02d}" for i in reversed(range(8))]
    assert body["total"] == 8 and body["truncated"] is False
    assert recorder.seen == [("list_recordings", {}), ("list_recordings", {"offset": 3}),
                             ("list_recordings", {"offset": 6})]
    recorder.close()


def test_a_recorder_that_never_advances_does_not_loop_forever(env):
    def stuck(command, args, _cid):
        return ack({"recordings": [rec_row("only", 1.0)], "total": 5, "offset": 0, "next_offset": 0})

    recorder = FakeRecorder(env, stuck)
    body = get_recordings(env, recorder.instance_id).json()
    assert [x["session_id"] for x in body["recordings"]] == ["only"]
    assert body["truncated"] is True and len(recorder.seen) == 1
    recorder.close()


def test_the_recordings_cap_sets_truncated(env, monkeypatch):
    monkeypatch.setattr(remote, "MAX_RECORDINGS", 4)
    rows = [rec_row(f"c-{i:02d}", 100.0 + i) for i in range(10)]
    recorder = FakeRecorder(env, lister(rows, page=3))
    body = get_recordings(env, recorder.instance_id).json()
    assert len(body["recordings"]) == 4 and body["total"] == 10 and body["truncated"] is True
    assert body["summary"]["total"] == 4
    assert len(recorder.seen) == 2  # stopped paging once the cap was reached
    recorder.close()


def _big_rows(count: int, name_len: int = 200) -> List[dict]:
    return [rec_row(f"big-{i:04d}-" + "s" * 20, 5000.0 + i, name="n" * name_len) for i in range(count)]


def test_one_big_ack_frame_beyond_the_old_16kb_cap_is_delivered(env):
    rows = _big_rows(1000)
    result = {"recordings": rows, "total": 1000, "offset": 0, "next_offset": None}
    frame = json.dumps({"type": "ack", "command_id": "x" * 32, **ack(result)})
    assert remote.MAX_MESSAGE_BYTES * 20 < len(frame) <= remote.MAX_ACK_FRAME_BYTES

    def handler(command, args, cid):
        return json.dumps({"type": "ack", "command_id": cid, **ack(result)})

    recorder = FakeRecorder(env, handler)
    r = get_recordings(env, recorder.instance_id)
    assert r.status_code == 200
    body = r.json()
    assert len(body["recordings"]) == 1000 and body["truncated"] is False and body["total"] == 1000
    assert len(recorder.seen) == 1
    recorder.close()


def test_an_ack_frame_over_the_ack_cap_is_junk_and_the_command_times_out(env):
    env.hub.command_timeout = 0.15

    def handler(command, args, cid):
        return json.dumps({"type": "ack", "command_id": cid,
                           **ack({"recordings": [], "pad": "p" * remote.MAX_ACK_FRAME_BYTES})})

    recorder = FakeRecorder(env, handler)
    r = get_recordings(env, recorder.instance_id)
    assert r.status_code == 504
    assert [i["instance_id"] for i in env.hub.list_items()] == [recorder.instance_id]  # still connected
    recorder.close()


def test_a_non_ack_frame_over_16kb_is_junk(env):
    env.hub.command_timeout = 0.15

    def handler(command, args, cid):
        return json.dumps({"type": "state", "state": {"status": "recording"}, "pad": "p" * 20_000})

    recorder = FakeRecorder(env, handler)
    r = get_recordings(env, recorder.instance_id)
    assert r.status_code == 504
    assert env.hub.list_items()[0]["state"]["status"] == "idle"  # the big state frame was not believed
    recorder.close()


def test_a_reupload_ack_over_16kb_is_delivered(env):
    def handler(command, args, cid):
        return ack({"results": [{"session_id": f"x{i}", "ok": False, "code": "invalid", "error": "e" * 250}
                                for i in range(100)], "queued": 0, "already_queued": 0})

    recorder = FakeRecorder(env, handler)
    with env.web() as http:
        r = http.post(f"/v1/recorders/{recorder.instance_id}/recordings/reupload", json={"session_ids": ["a"]})
    assert r.status_code == 200 and r.json()["ok"] is True and len(r.json()["results"]) == 100
    recorder.close()


def test_recordings_commands_wait_longer_than_ordinary_ones(env):
    env.hub.command_timeout = 0.2  # list / reupload wait 4x (0.8 s), delete_local 12x, others 1x

    def slow(command, args, cid):
        time.sleep(0.5)
        if command == "list_recordings":
            return ack({"recordings": [], "total": 0, "offset": 0, "next_offset": None})
        if command == "delete_local":
            return ack({"results": [{"session_id": "a", "ok": True}], "deleted": 1, "freed_bytes": 0})
        return ack()

    recorder = FakeRecorder(env, slow)
    with env.web() as http:
        assert http.get(f"/v1/recorders/{recorder.instance_id}/recordings").status_code == 200
        assert http.post(f"/v1/recorders/{recorder.instance_id}/recordings/delete",
                         json={"session_ids": ["a"]}).status_code == 200
        assert http.post(remote.command_path(recorder.instance_id), json={"command": "stop"}).status_code == 504
    assert remote.TIMEOUT_FACTOR == {"list_recordings": 4, "reupload": 4, "delete_local": 12}
    recorder.close()


def test_the_generic_commands_route_returns_the_result_and_still_refuses_unknown_commands(env):
    rows = [rec_row("g-1", 1.0)]
    recorder = FakeRecorder(env, lister(rows))
    with env.web() as http:
        r = http.post(remote.command_path(recorder.instance_id), json={"command": "list_recordings"})
        assert r.status_code == 200 and r.json()["result"]["recordings"][0]["session_id"] == "g-1"
        assert http.post(remote.command_path(recorder.instance_id), json={"command": "wipe_disk"}).status_code == 400
        bad = http.post(remote.command_path(recorder.instance_id),
                        json={"command": "delete_local", "args": {"session_ids": ["../x"]}})
        assert bad.status_code == 400
        assert http.post(remote.command_path(recorder.instance_id),
                         json={"command": "reupload", "args": {}}).status_code == 400
    assert recorder.commands() == ["list_recordings"]
    recorder.close()


def echo_ids(command, args, _cid):
    ids = args["session_ids"]
    if command == "reupload":
        return ack({"results": [{"session_id": i, "ok": True, "code": None, "error": None} for i in ids],
                    "queued": len(ids), "already_queued": 0})
    return ack({"results": [{"session_id": i, "ok": True, "code": None, "error": None, "bytes": 10,
                             "method": "recycle-bin"} for i in ids],
                "deleted": len(ids), "freed_bytes": 10 * len(ids)})


@pytest.mark.parametrize("path, command", [("reupload", "reupload"), ("delete", "delete_local")])
def test_reupload_and_delete_are_forwarded_with_the_right_command(env, path, command):
    recorder = FakeRecorder(env, echo_ids)
    with env.web() as http:
        r = http.post(f"/v1/recorders/{recorder.instance_id}/recordings/{path}",
                      json={"session_ids": ["a", "b", "a", "c"]})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and [x["session_id"] for x in body["results"]] == ["a", "b", "c"]
    assert body["state"]["status"] == "idle"
    assert recorder.seen == [(command, {"session_ids": ["a", "b", "c"]})]
    recorder.close()


def test_more_than_100_ids_are_chunked_and_the_results_merged_in_order(env):
    ids = _ids(250)
    recorder = FakeRecorder(env, echo_ids)
    with env.web() as http:
        up = http.post(f"/v1/recorders/{recorder.instance_id}/recordings/reupload", json={"session_ids": ids}).json()
        gone = http.post(f"/v1/recorders/{recorder.instance_id}/recordings/delete", json={"session_ids": ids}).json()
    chunks = [a["session_ids"] for _, a in recorder.seen]
    assert [len(c) for c in chunks] == [100, 100, 50, 100, 100, 50]
    assert all(len(c) <= remote.MAX_IDS for c in chunks)
    assert chunks[0] + chunks[1] + chunks[2] == ids
    assert recorder.commands() == ["reupload"] * 3 + ["delete_local"] * 3
    assert up["ok"] is True and [x["session_id"] for x in up["results"]] == ids
    assert up["queued"] == 250 and up["already_queued"] == 0
    assert gone["ok"] is True and [x["session_id"] for x in gone["results"]] == ids
    assert gone["deleted"] == 250 and gone["freed_bytes"] == 2500
    recorder.close()


def test_exactly_1000_ids_are_accepted_as_ten_commands(env):
    recorder = FakeRecorder(env, echo_ids)
    with env.web() as http:
        r = http.post(f"/v1/recorders/{recorder.instance_id}/recordings/reupload", json={"session_ids": _ids(1000)})
    assert r.status_code == 200 and len(r.json()["results"]) == 1000 and len(recorder.seen) == 10
    recorder.close()


def test_a_refusal_on_the_first_chunk_is_returned_as_ok_false(env):
    recorder = FakeRecorder(env, lambda c, a, i: ack(ok=False, code="remote_control_disabled", error="Turned off."))
    with env.web() as http:
        r = http.post(f"/v1/recorders/{recorder.instance_id}/recordings/delete", json={"session_ids": _ids(250)})
    assert r.status_code == 200
    assert r.json() == {"ok": False, "code": "remote_control_disabled", "error": "Turned off."}
    assert len(recorder.seen) == 1  # it did not keep sending chunks
    recorder.close()


def test_a_refusal_on_a_later_chunk_keeps_the_earlier_results(env):
    calls = []

    def handler(command, args, cid):
        calls.append(1)
        if len(calls) == 2:
            return ack(ok=False, code="busy", error="Busy right now.")
        return echo_ids(command, args, cid)

    recorder = FakeRecorder(env, handler)
    with env.web() as http:
        r = http.post(f"/v1/recorders/{recorder.instance_id}/recordings/reupload", json={"session_ids": _ids(250)})
    body = r.json()
    assert r.status_code == 200 and body["ok"] is False and body["error"] == "Busy right now."
    assert len(body["results"]) == 100 and body["queued"] == 100
    assert len(recorder.seen) == 2
    recorder.close()


@pytest.mark.parametrize("body", [
    {}, {"session_ids": []}, {"session_ids": "abc"}, {"session_ids": None}, {"session_ids": ["../x"]},
    {"session_ids": ["a/b"]}, {"session_ids": ["a\\b"]}, {"session_ids": [""]}, {"session_ids": [7]},
    {"session_ids": ["ok", ".."]}, {"session_ids": _ids(1001)}, ["a"], "a", 5,
])
@pytest.mark.parametrize("path", ["reupload", "delete"])
def test_bad_id_bodies_are_400_and_never_reach_the_recorder(env, path, body):
    recorder = FakeRecorder(env, echo_ids)
    with env.web() as http:
        r = http.post(f"/v1/recorders/{recorder.instance_id}/recordings/{path}", json=body)
        assert r.status_code == 400 and r.json()["detail"]
        nonjson = http.post(f"/v1/recorders/{recorder.instance_id}/recordings/{path}", content=b"nope",
                            headers={"content-type": "application/json"})
        assert nonjson.status_code == 400
    time.sleep(0.15)
    assert recorder.seen == []
    recorder.close()


def test_reupload_and_delete_of_a_gone_recorder_are_404(env):
    with env.web() as http:
        for path in ("reupload", "delete"):
            r = http.post(f"/v1/recorders/{uuid.uuid4().hex}/recordings/{path}", json={"session_ids": ["a"]})
            assert r.status_code == 404


def test_a_silent_recorder_gives_504_for_reupload(env):
    env.hub.command_timeout = 0.1
    recorder = FakeRecorder(env, lambda c, a, i: None)
    with env.web() as http:
        r = http.post(f"/v1/recorders/{recorder.instance_id}/recordings/reupload", json={"session_ids": ["a"]})
    assert r.status_code == 504
    recorder.close()


# -- POST /v1/recordings/status ----------------------------------------------------


def test_status_reports_live_trash_unknown_ids(env):
    store = env.app.state.store
    seed(store, "st-ready", "done")
    seed(store, "st-run", "running")
    seed(store, "st-queued", "queued")
    seed(store, "st-err", "error", error="nope")
    seed(store, "st-partial")
    seed(store, "st-trash", "done", trash=True)
    ids = ["st-ready", "st-run", "st-queued", "st-err", "st-partial", "st-trash", "st-unknown", "st-ready"]
    with env.web() as http:
        r = http.post("/v1/recordings/status", json={"session_ids": ids})
    assert r.status_code == 200
    items = r.json()["items"]
    assert set(items) == set(ids)
    assert items["st-ready"] == {"on_server": True, "has_copy": True, "in_trash": False,
                                 "transcription": "complete", "error": None}
    assert items["st-run"]["transcription"] == "transcribing" and items["st-run"]["has_copy"] is True
    assert items["st-queued"]["transcription"] == "queued"
    assert items["st-err"]["transcription"] == "error" and items["st-err"]["error"] == "nope"
    assert items["st-partial"] == {"on_server": True, "has_copy": False, "in_trash": False,
                                   "transcription": None, "error": None}
    assert items["st-trash"] == {"on_server": False, "has_copy": False, "in_trash": True,
                                 "transcription": None, "error": None}
    assert items["st-unknown"] == {"on_server": False, "has_copy": False, "in_trash": False,
                                   "transcription": None, "error": None}


@pytest.mark.parametrize("body", [
    {}, {"session_ids": []}, {"session_ids": "abc"}, {"session_ids": ["../x"]}, {"session_ids": ["a/b"]},
    {"session_ids": [3]}, {"session_ids": _ids(1001)}, ["a"], "a",
])
def test_status_bad_bodies_are_400(env, body):
    with env.web() as http:
        assert http.post("/v1/recordings/status", json=body).status_code == 400
        assert http.post("/v1/recordings/status", content=b"nope",
                         headers={"content-type": "application/json"}).status_code == 400


def test_status_accepts_exactly_1000_ids(env):
    with env.web() as http:
        r = http.post("/v1/recordings/status", json={"session_ids": _ids(1000)})
    assert r.status_code == 200 and len(r.json()["items"]) == 1000


# =============================================================================
# 6. auth: the server token only (never an agent key)
# =============================================================================


def _endpoints(iid: str):
    return [
        ("GET", f"/v1/recorders/{iid}/recordings", None),
        ("POST", f"/v1/recorders/{iid}/recordings/reupload", {"session_ids": ["a"]}),
        ("POST", f"/v1/recorders/{iid}/recordings/delete", {"session_ids": ["a"]}),
        ("POST", "/v1/recordings/status", {"session_ids": ["a"]}),
    ]


def test_every_recordings_endpoint_needs_the_server_token(secured):
    def handler(command, args, cid):
        if command == "list_recordings":
            return ack({"recordings": [], "total": 0, "offset": 0, "next_offset": None})
        return echo_ids(command, args, cid)

    recorder = FakeRecorder(secured, handler)
    agent_key = secured.app.state.agent_keys.create("robot", ["read", "write"])["key"]
    with httpx.Client(base_url=secured.base, timeout=10) as anon:
        for method, path, body in _endpoints(recorder.instance_id):
            def call(**kw):
                return anon.request(method, path, json=body, **kw)

            assert call().status_code == 401, path
            assert call(headers={"Authorization": f"Bearer {agent_key}"}).status_code == 403, path
            assert call(headers={"Authorization": "Bearer mnk_not_the_server_token"}).status_code == 403, path
            assert call(headers={"Authorization": "Bearer wrong"}).status_code == 403, path
            assert call(headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200, path
            assert call(cookies={"meeting_notes_token": TOKEN}).status_code == 200, path
    # Only the authorised calls reached the recorder: 2 x (list, reupload, delete) = 6.
    assert len(recorder.seen) == 6
    recorder.close()


def test_the_agent_api_cannot_reach_the_recordings_routes(secured):
    agent_key = secured.app.state.agent_keys.create("robot", ["read", "write"])["key"]
    with httpx.Client(base_url=secured.base, timeout=10,
                      headers={"Authorization": f"Bearer {agent_key}"}) as agent:
        assert agent.get(f"/api/v1/recorders/{uuid.uuid4().hex}/recordings").status_code in (401, 403, 404)
        assert agent.post("/api/v1/recordings/status", json={"session_ids": ["a"]}).status_code in (401, 403, 404)


def test_the_generic_commands_route_keeps_refusing_unknown_commands_with_a_token(secured):
    recorder = FakeRecorder(secured, lister([]))
    with secured.web() as http:
        assert http.post(remote.command_path(recorder.instance_id), json={"command": "wipe_disk"}).status_code == 400
        assert http.post(remote.command_path(recorder.instance_id), json={"command": "list_recordings"}).status_code == 200
    with httpx.Client(base_url=secured.base) as anon:
        assert anon.post(remote.command_path(recorder.instance_id), json={"command": "list_recordings"}).status_code == 401
    recorder.close()


# =============================================================================
# 7. ServerClient.recordings_status
# =============================================================================


def test_client_recordings_status_against_a_real_server(tmp_path, monkeypatch):
    server_, env_ = start(tmp_path, monkeypatch, TOKEN)
    try:
        store = env_.app.state.store
        seed(store, "cs-ready", "done")
        seed(store, "cs-trash", "queued", trash=True)
        with ServerClient(env_.base, TOKEN, timeout=10) as api:
            found = api.recordings_status(["cs-ready", "cs-trash", "cs-missing", "cs-ready"])
        assert set(found) == {"cs-ready", "cs-trash", "cs-missing"}
        assert found["cs-ready"]["has_copy"] is True and found["cs-ready"]["transcription"] == "complete"
        assert found["cs-trash"]["in_trash"] is True and found["cs-trash"]["on_server"] is False
        assert found["cs-missing"]["on_server"] is False and found["cs-missing"]["in_trash"] is False
        with ServerClient(env_.base, "wrong-token", timeout=10) as bad, pytest.raises(httpx.HTTPStatusError) as err:
            bad.recordings_status(["cs-ready"])
        assert err.value.response.status_code == 403
    finally:
        server_.stop()


def _mock_client(handler) -> ServerClient:
    api = ServerClient("http://server.invalid", "tok")
    api._client.close()
    api._client = httpx.Client(base_url="http://server.invalid", transport=httpx.MockTransport(handler))
    return api


def test_client_recordings_status_filters_invalid_ids_and_chunks_by_500():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append((request.url.path, request.headers.get("authorization"), body["session_ids"]))
        return httpx.Response(200, json={"items": {i: {"on_server": True, "has_copy": False, "in_trash": False,
                                                        "transcription": None, "error": None}
                                                   for i in body["session_ids"]}})

    ids = _ids(1100)
    messy = ["../x", "a/b", "a\\b", "", ".", "..", "a\x00b", ids[0]] + ids + ["x" * 200]
    with _mock_client(handler) as api:
        found = api.recordings_status(messy)
    assert set(found) == set(ids)
    assert [len(r[2]) for r in requests] == [500, 500, 100]
    assert all(r[0] == "/v1/recordings/status" and r[1] == "Bearer tok" for r in requests)
    assert [i for r in requests for i in r[2]] == ids  # deduped, in order, nothing invalid
    with _mock_client(handler) as api:
        requests.clear()
        assert api.recordings_status(["../x", ""]) == {}
        assert requests == []  # nothing valid left to ask


def test_client_recordings_status_ignores_a_malformed_reply():
    def handler(request):
        return httpx.Response(200, json={"items": {"ok-1": {"on_server": True}, "bad": "nope"}})

    with _mock_client(handler) as api:
        assert api.recordings_status(["ok-1", "bad"]) == {"ok-1": {"on_server": True}}
    with _mock_client(lambda r: httpx.Response(200, json={"items": ["not", "a", "dict"]})) as api:
        assert api.recordings_status(["a"]) == {}
    with _mock_client(lambda r: httpx.Response(200, content=b"null")) as api:
        assert api.recordings_status(["a"]) == {}


def test_client_recordings_status_surfaces_a_404_from_an_older_server():
    with _mock_client(lambda r: httpx.Response(404, json={"detail": "Not Found"})) as api:
        with pytest.raises(httpx.HTTPStatusError) as err:
            api.recordings_status(["a"])
    assert err.value.response.status_code == 404


# =============================================================================
# 8. the real MainWindow handling the recordings commands
# =============================================================================


class ResultChannel(FakeChannel):
    """The window tests' fake channel, extended to take the ack ``result``."""

    def __init__(self, on_command):
        super().__init__(on_command)
        self.results: List[tuple] = []

    def send_ack(self, command_id, ok, code=None, error=None, snapshot=None, result=None):
        self.acks.append((command_id, ok, code, error, snapshot))
        self.results.append((command_id, ok, code, error, result))


@pytest.fixture
def rwin(window, tmp_path):
    """The window with a save folder of its own, a result-aware channel and no real uploader."""
    save = tmp_path / "Recordings"
    save.mkdir()
    (tmp_path / "config.json").write_text(json.dumps({"save_dir": str(save)}))
    channel = ResultChannel(window._on_remote_command)
    window._remote = channel
    queue = SessionQueue.for_save_dir(save)
    window.controller._queue = queue
    window.controller.start_uploader = lambda force=False: True  # never start a real uploader
    window.save = save
    window.queue = queue
    window.channel = channel
    window.bin = Bin(tmp_path / "bin")
    return window


def _bin_deletes(monkeypatch, window):
    """Replace the Recycle Bin. ``delete_local`` may bind its default remover when it is defined, so besides
    patching ``retention.move_to_recycle_bin`` the function is wrapped to inject the recorder."""
    from meeting_notes.client import retention

    monkeypatch.setattr(retention, "move_to_recycle_bin", window.bin)
    original = remote_recordings.delete_local

    def wrapped(save_dir, queue, session_ids, active_dir=None, remover=None):
        return original(save_dir, queue, session_ids, active_dir, remover or window.bin)

    monkeypatch.setattr(remote_recordings, "delete_local", wrapped)


def _answered(window, n=1, timeout=5.0):
    assert _pump(lambda: len(window.channel.results) >= n, timeout), "no ack arrived"
    return window.channel.results


def _settle():
    from PySide6.QtWidgets import QApplication

    for _ in range(5):
        QApplication.processEvents()
        time.sleep(0.01)


def test_list_recordings_runs_on_a_worker_and_acks_with_the_result(rwin, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="meeting_notes.client.ui")
    make_rec(rwin.save, "older", started=1000.0)
    make_rec(rwin.save, "newer", started=2000.0)
    make_invalid(rwin.save, "broken", 1500.0)
    seen_threads = []
    real = remote_recordings.list_recordings

    def spy(*a, **k):
        seen_threads.append(threading.current_thread())
        return real(*a, **k)

    monkeypatch.setattr(remote_recordings, "list_recordings", spy)
    rwin._on_recordings_command(CMD_ID, "list_recordings", {})
    (ack_,) = _answered(rwin)
    command_id, ok, code, error, result = ack_
    assert (command_id, ok, code, error) == (CMD_ID, True, None, None)
    assert [r["session_id"] for r in result["recordings"]] == ["newer", "broken", "older"]
    assert result["total"] == 3 and result["next_offset"] is None
    assert seen_threads and seen_threads[0] is not threading.current_thread()
    assert "remote command: list_recordings (source=server)" in caplog.text


def test_list_recordings_with_an_offset_and_through_the_dispatcher(rwin):
    for i in range(3):
        make_rec(rwin.save, f"r{i}", started=100.0 + i)
    rwin._on_remote_command(CMD_ID, "list_recordings", {"offset": 1})
    (ack_,) = _answered(rwin)
    assert [r["session_id"] for r in ack_[4]["recordings"]] == ["r1", "r0"]
    assert ack_[4]["offset"] == 1


def test_a_command_from_the_channel_thread_reaches_the_recordings_handler(rwin):
    make_rec(rwin.save, "a", started=1.0)
    threading.Thread(target=rwin.channel.on_command, args=(CMD_ID, "list_recordings", {})).start()
    (ack_,) = _answered(rwin)
    assert ack_[1] is True and ack_[4]["total"] == 1


def test_list_marks_the_recording_in_progress_as_active(rwin, tmp_path):
    live = make_rec(rwin.save, "live", started=50.0)
    make_rec(rwin.save, "done", started=10.0)
    _recording(rwin, tmp_path)
    rwin.controller.session_dir = live
    assert rwin._recording_active_dir() == live
    rwin._on_recordings_command(CMD_ID, "list_recordings", {})
    (ack_,) = _answered(rwin)
    rows = {r["session_id"]: r for r in ack_[4]["recordings"]}
    assert rows["live"]["active"] is True and rows["live"]["queue"]["state"] == "recording"
    assert rows["done"]["active"] is False


def test_the_active_dir_is_none_when_idle(rwin):
    assert rwin._recording_active_dir() is None


def test_reupload_queues_through_the_controller_and_toasts(rwin, caplog):
    caplog.set_level(logging.INFO, logger="meeting_notes.client.ui")
    a = make_rec(rwin.save, "a")
    make_rec(rwin.save, "b")
    submitted = []
    real = rwin.controller.reupload_recordings

    def spy(folders):
        submitted.append([Path(f).name for f in folders])
        return real(folders)

    rwin.controller.reupload_recordings = spy
    rwin._on_recordings_command(CMD_ID, "reupload", {"session_ids": ["a", "ghost", "a"]})
    (ack_,) = _answered(rwin)
    _, ok, code, _, result = ack_
    assert ok is True and code is None
    assert submitted == [["a"]]
    assert [(r["session_id"], r["ok"], r["code"]) for r in result["results"]] == [
        ("a", True, None), ("ghost", False, "not_found")]
    assert result["queued"] == 1 and result["already_queued"] == 0
    assert rwin.queue.is_queued(a) and not rwin.queue.is_queued(rwin.save / "b")
    assert "Re-upload asked from the server" in rwin._toast.text()
    assert "remote command: reupload (source=server)" in caplog.text
    # Asking again reports it as already queued.
    rwin._on_recordings_command("0123456789abcdee", "reupload", {"session_ids": ["a"]})
    second = _answered(rwin, 2)[1][4]
    assert second["queued"] == 0 and second["already_queued"] == 1


def test_delete_local_moves_to_the_bin_on_a_worker_and_toasts_on_the_gui_thread(rwin, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="meeting_notes.client.ui")
    _bin_deletes(monkeypatch, rwin)
    a, b, keep = make_rec(rwin.save, "a"), make_rec(rwin.save, "b"), make_rec(rwin.save, "keep")
    rwin.queue.enqueue(a)
    rwin._on_recordings_command(CMD_ID, "delete_local", {"session_ids": ["a", "b", "ghost"]})
    (ack_,) = _answered(rwin)
    _, ok, code, _, result = ack_
    assert ok is True and code is None
    assert [(r["session_id"], r["ok"], r["code"]) for r in result["results"]] == [
        ("a", True, None), ("b", True, None), ("ghost", False, "not_found")]
    assert result["deleted"] == 2 and result["freed_bytes"] > 0
    assert not a.exists() and not b.exists() and keep.exists()
    assert not rwin.queue.is_queued(a)
    assert sorted(rwin.bin.calls) == ["a", "b"]
    assert _pump(lambda: rwin._toast.text().startswith("Deleted 2 recordings"))
    assert "remote command: delete_local (source=server)" in caplog.text


def test_delete_local_of_one_recording_uses_the_singular(rwin, monkeypatch):
    _bin_deletes(monkeypatch, rwin)
    make_rec(rwin.save, "solo")
    rwin._on_recordings_command(CMD_ID, "delete_local", {"session_ids": ["solo"]})
    _answered(rwin)
    assert _pump(lambda: rwin._toast.text().startswith("Deleted 1 recording on"))


def test_delete_local_that_deletes_nothing_does_not_toast(rwin, monkeypatch):
    _bin_deletes(monkeypatch, rwin)
    rwin._on_recordings_command(CMD_ID, "delete_local", {"session_ids": ["ghost"]})
    (ack_,) = _answered(rwin)
    assert ack_[1] is True and ack_[4]["results"][0]["code"] == "not_found"
    _settle()
    assert rwin._toast.isHidden()


def test_the_recording_in_progress_is_refused_for_reupload_and_delete(rwin, tmp_path, monkeypatch):
    _bin_deletes(monkeypatch, rwin)
    live = make_rec(rwin.save, "live")
    _recording(rwin, tmp_path)
    rwin.controller.session_dir = live
    rwin._on_recordings_command(CMD_ID, "reupload", {"session_ids": ["live"]})
    rwin._on_recordings_command("0123456789abcdee", "delete_local", {"session_ids": ["live"]})
    results = {r[0]: r for r in _answered(rwin, 2)}
    assert results[CMD_ID][4]["results"][0]["code"] == "active_recording"
    assert results["0123456789abcdee"][4]["results"][0]["code"] == "active_recording"
    assert live.exists() and rwin.bin.calls == [] and not rwin.queue.is_queued(live)


def test_a_recording_that_is_uploading_is_refused_for_delete(rwin, monkeypatch):
    _bin_deletes(monkeypatch, rwin)
    folder = make_rec(rwin.save, "up")
    assert rwin.queue.claim(rwin.queue.enqueue(folder))
    rwin._on_recordings_command(CMD_ID, "delete_local", {"session_ids": ["up"]})
    (ack_,) = _answered(rwin)
    assert ack_[4]["results"][0]["code"] == "uploading" and folder.exists()


@pytest.mark.parametrize("name, args", [
    ("reupload", {"session_ids": []}),
    ("reupload", {}),
    ("reupload", {"session_ids": ["../x"]}),
    ("delete_local", {"session_ids": "abc"}),
    ("delete_local", {"session_ids": _ids(101)}),
    ("list_recordings", {"offset": -1}),
    ("list_recordings", {"offset": True}),
    ("list_recordings", {"surprise": 1}),
    ("reupload", ["a"]),
])
def test_bad_arguments_are_acked_bad_args(rwin, name, args):
    rwin._on_recordings_command(CMD_ID, name, args)
    (ack_,) = rwin.channel.results
    assert ack_[:3] == (CMD_ID, False, "bad_args") and ack_[3] and ack_[4] is None
    assert rwin._toast.isHidden()


@pytest.mark.parametrize("name, args", [
    ("list_recordings", {}), ("reupload", {"session_ids": ["a"]}), ("delete_local", {"session_ids": ["a"]}),
])
def test_everything_is_refused_when_remote_control_is_switched_off(rwin, monkeypatch, caplog, tmp_path, name, args):
    caplog.set_level(logging.INFO, logger="meeting_notes.client.ui")
    _bin_deletes(monkeypatch, rwin)
    folder = make_rec(rwin.save, "a")
    (tmp_path / "config.json").write_text(json.dumps({"save_dir": str(rwin.save), "remote_control_allowed": False}))
    rwin._on_recordings_command(CMD_ID, name, args)
    (ack_,) = rwin.channel.results
    assert ack_[:3] == (CMD_ID, False, "remote_control_disabled") and "turned off" in ack_[3] and ack_[4] is None
    assert f"remote command: {name} (source=server) -> refused(remote_control_disabled)" in caplog.text
    assert folder.exists() and rwin.bin.calls == [] and not rwin.queue.is_queued(folder)
    assert rwin._toast.isHidden()


def test_an_unusable_save_folder_is_acked_no_save_folder(rwin, monkeypatch):
    def boom():
        raise OSError("disk unplugged")

    monkeypatch.setattr(rwin.controller, "session_queue", boom)
    rwin._on_recordings_command(CMD_ID, "list_recordings", {})
    (ack_,) = rwin.channel.results
    assert ack_[:3] == (CMD_ID, False, "no_save_folder") and "disk unplugged" in ack_[3]


def test_a_failure_while_listing_is_acked_failed(rwin, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("scan blew up")

    monkeypatch.setattr(remote_recordings, "list_recordings", boom)
    rwin._on_recordings_command(CMD_ID, "list_recordings", {})
    (ack_,) = _answered(rwin)
    assert ack_[:3] == (CMD_ID, False, "failed") and "scan blew up" in ack_[3]


def test_the_handler_survives_having_no_channel(rwin):
    rwin._remote = None
    rwin._on_recordings_command(CMD_ID, "reupload", {"session_ids": ["a"]})  # must not raise
    rwin._on_recordings_command(CMD_ID, "nonsense", {})
