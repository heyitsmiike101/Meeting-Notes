"""Local-recording clean-up: safety decision table, settings, summary, scheduling."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx
import pytest

from meeting_notes import config as config_mod
from meeting_notes.client import retention
from meeting_notes.client.api import ServerUnavailable
from meeting_notes.client.queue import SessionQueue
from tests.test_client_r2 import _close, _configure, _pump, _window, home, qt_app  # noqa: F401
from tests.test_client_transport import _make_session_dir

DAYS = 30


def _detail(session_id, **over):
    """What GET /v1/sessions/{id} returns for a finished, transcribed meeting."""
    doc = {
        "session_id": session_id,
        "meta": {},
        "pipeline": {
            "upload": {"state": "complete"},
            "transcription": {"state": "complete"},
            "state": "complete",
        },
        "transcript_job_id": "job-1",
    }
    doc.update(over)
    return doc


def _status_error(code):
    request = httpx.Request("GET", "http://server/v1/sessions/x")
    return httpx.HTTPStatusError(
        f"{code}", request=request, response=httpx.Response(code, request=request)
    )


class _Server:
    def __init__(self, handler=None):
        self.calls = []
        self.handler = handler or (lambda sid: _detail(sid))

    def __call__(self, session_id):
        self.calls.append(session_id)
        return self.handler(session_id)


def _old(session_dir: Path, days_ago: float) -> Path:
    meta_path = session_dir / "session.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["started_wall"] = time.time() - days_ago * 86400
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    return session_dir


@pytest.fixture
def env(tmp_path):
    save = tmp_path / "Meeting Notes"
    save.mkdir()
    return save, SessionQueue.for_save_dir(save)


def _decide(folder, queue, server, *, active=None, days=DAYS):
    return retention.evaluate_recording(
        folder, days=days, queue=queue, fetch_detail=server, active_dir=active
    )


# -- decision table ----------------------------------------------------------------


def test_complete_and_old_enough_is_deleted(env):
    save, queue = env
    folder = _old(_make_session_dir(save, "done-old"), 45)
    d = _decide(folder, queue, _Server())
    assert d.delete and d.size_bytes > 0


def test_still_queued_is_kept_without_asking_the_server(env):
    save, queue = env
    folder = _old(_make_session_dir(save, "queued"), 45)
    queue.enqueue(folder)
    server = _Server()
    d = _decide(folder, queue, server)
    assert not d.delete and "upload queue" in d.reason
    assert server.calls == []


def test_a_failed_queue_entry_is_also_kept(env):
    save, queue = env
    folder = _old(_make_session_dir(save, "failed"), 45)
    entry = queue.enqueue(folder)
    queue.mark_attempt_failed(entry, "x", terminal=True)
    assert not _decide(folder, queue, _Server()).delete


def test_too_new_is_kept_without_asking_the_server(env):
    save, queue = env
    folder = _old(_make_session_dir(save, "fresh"), 3)
    server = _Server()
    d = _decide(folder, queue, server)
    assert not d.delete and "days old" in d.reason and server.calls == []


def test_missing_on_server_404_is_kept(env):
    save, queue = env
    folder = _old(_make_session_dir(save, "gone"), 45)

    def handler(_sid):
        raise _status_error(404)

    d = _decide(folder, queue, _Server(handler))
    assert not d.delete and "404" in d.reason


@pytest.mark.parametrize("code", [500, 410, 301])
def test_any_other_non_200_is_kept(env, code):
    save, queue = env
    folder = _old(_make_session_dir(save, "odd"), 45)

    def handler(_sid):
        raise _status_error(code)

    assert not _decide(folder, queue, _Server(handler)).delete


@pytest.mark.parametrize(
    "extra",
    [
        {"deleted_at": "2026-09-01T00:00:00"},
        {"trashed": True},
        {"in_trash": True},
        {"meta": {"deleted": True}},
        {"pipeline": {"upload": {"state": "complete"}, "transcription": {"state": "complete"}, "state": "trashed"}},
    ],
)
def test_trashed_on_the_server_is_kept(env, extra):
    save, queue = env
    folder = _old(_make_session_dir(save, "trashed"), 45)
    d = _decide(folder, queue, _Server(lambda sid: _detail(sid, **extra)))
    assert not d.delete and "deleted" in d.reason


@pytest.mark.parametrize(
    "pipeline",
    [
        {"upload": {"state": "complete"}, "transcription": {"state": "transcribing"}},
        {"upload": {"state": "complete"}, "transcription": {"state": "pending"}},
        {"upload": {"state": "uploading"}, "transcription": {"state": "pending"}},
        {"upload": {"state": "complete"}, "transcription": {"state": "error"}},
    ],
)
def test_not_fully_processed_is_kept(env, pipeline):
    save, queue = env
    folder = _old(_make_session_dir(save, "busy"), 45)
    d = _decide(folder, queue, _Server(lambda sid: _detail(sid, pipeline=pipeline)))
    assert not d.delete and "server" in d.reason


def test_complete_but_no_transcript_or_wrong_session_is_kept(env):
    save, queue = env
    folder = _old(_make_session_dir(save, "notx"), 45)
    assert not _decide(folder, queue, _Server(lambda sid: _detail(sid, transcript_job_id=None))).delete
    assert not _decide(folder, queue, _Server(lambda sid: _detail("someone-else"))).delete
    assert not _decide(folder, queue, _Server(lambda sid: ["not", "a", "dict"])).delete


def test_unreachable_server_or_rejected_token_keeps_everything_and_stops_asking(env):
    save, queue = env
    for name in ("a", "b", "c"):
        _old(_make_session_dir(save, name), 45)

    def down(_sid):
        raise ServerUnavailable("no route")

    server = _Server(down)
    plan = retention.plan_cleanup(save, DAYS, queue=queue, fetch_detail=server)
    assert len(plan) == 3 and not any(d.delete for d in plan)
    assert all("unreachable" in d.reason for d in plan)
    assert len(server.calls) == 1  # one failure covers the rest

    def forbidden(_sid):
        raise _status_error(403)

    plan = retention.plan_cleanup(save, DAYS, queue=queue, fetch_detail=_Server(forbidden))
    assert not any(d.delete for d in plan) and "token" in plan[0].reason


def test_active_recording_is_kept(env):
    save, queue = env
    folder = _old(_make_session_dir(save, "live"), 45)
    d = _decide(folder, queue, _Server(), active=folder)
    assert not d.delete and "in progress" in d.reason


def test_forever_and_unreadable_metadata_are_kept(env):
    save, queue = env
    folder = _old(_make_session_dir(save, "keepme"), 400)
    assert not _decide(folder, queue, _Server(), days=0).delete
    (folder / "session.json").write_text("{broken", encoding="utf-8")
    assert not _decide(folder, queue, _Server()).delete


def test_execute_plan_deletes_only_confirmed_and_rechecks_the_queue(env):
    save, queue = env
    keep_me = _old(_make_session_dir(save, "queued-later"), 45)
    delete_me = _old(_make_session_dir(save, "delete-me"), 45)
    plan = retention.plan_cleanup(save, DAYS, queue=queue, fetch_detail=_Server())
    assert sum(d.delete for d in plan) == 2
    queue.enqueue(keep_me)  # re-uploaded between planning and executing
    removed = []
    report = retention.execute_plan(
        plan, queue=queue, remover=lambda p: removed.append(p.name) or "permanent"
    )
    assert removed == ["delete-me"]
    assert [d.folder for d in report.deleted] == [delete_me]
    assert report.freed_bytes > 0
    assert "Moved 1 recording to the Recycle Bin" in report.summary()


def test_policy_run_deletes_with_the_server_and_leaves_the_queue_dir(env, monkeypatch):
    save, queue = env
    old = _old(_make_session_dir(save, "old-done"), 45)
    new = _old(_make_session_dir(save, "new-done"), 2)

    class _Client:
        def session_detail(self, sid):
            return _detail(sid)

        def close(self):
            pass

    removed = []
    report = retention.run_with_server(
        save, DAYS, "http://s", "tok", client_factory=lambda: _Client(),
        remover=lambda p: removed.append(p.name) or "permanent",
    )
    assert removed == ["old-done"] and len(report.kept) == 1
    assert retention.folder_stats(save)["count"] == 2  # counting is separate from deleting


@pytest.mark.skipif(
    os.environ.get("MEETING_NOTES_TEST_RECYCLE") != "1",
    reason="sends a real folder to the Recycle Bin; opt in with MEETING_NOTES_TEST_RECYCLE=1",
)
def test_recycle_bin_removes_the_folder_on_this_machine(tmp_path):
    victim = _make_session_dir(tmp_path, "bin-me")
    method = retention.move_to_recycle_bin(victim)
    assert not victim.exists() and method in {"recycle-bin", "permanent"}


def test_summarize_kept_groups_reasons():
    def keep(reason):
        return retention.Decision(Path("x"), retention.KEEP, reason)

    text = retention.summarize_kept(
        [keep("only 2.0 days old (keeping 30)"), keep("only 1.0 days old (keeping 30)"),
         keep("still on the upload queue")]
    )
    assert text == "2 too recent, 1 still uploading"


# -- config ------------------------------------------------------------------------


def test_local_retention_days_is_validated():
    assert config_mod.local_retention_days({}) == 0
    assert config_mod.local_retention_days({"local_retention_days": 30}) == 30
    assert config_mod.local_retention_days({"local_retention_days": "90"}) == 90
    for bad in (5, -7, "soon", None, True, 3650):
        assert config_mod.local_retention_days({"local_retention_days": bad}) == 0


# -- settings dialog -----------------------------------------------------------------


def test_settings_round_trip_and_default_is_forever(qt_app, home):
    from meeting_notes.client.ui.settings_dialog import SettingsDialog

    _configure(home)
    dialog = SettingsDialog()
    assert dialog.retention_combo.currentData() == 0
    assert [dialog.retention_combo.itemText(i) for i in range(4)] == ["Forever", "7 days", "30 days", "90 days"]
    assert not dialog.cleanup_button.isEnabled()  # nothing to apply for Forever
    dialog.retention_combo.setCurrentIndex(dialog.retention_combo.findData(30))
    assert dialog.cleanup_button.isEnabled()
    dialog.accept()
    assert config_mod.local_retention_days() == 30
    again = SettingsDialog()
    assert again.retention_combo.currentData() == 30
    again.close()


def test_settings_shows_folder_count_and_size(qt_app, home):
    from meeting_notes.client.ui.settings_dialog import SettingsDialog

    _configure(home)
    (home / "rec").mkdir()
    _make_session_dir(home / "rec", "one")
    _make_session_dir(home / "rec", "two")
    dialog = SettingsDialog()
    assert _pump(lambda: dialog.local_stats_label.text().startswith("2 recordings, "))
    dialog.close()


def test_clean_up_now_confirms_then_reports_freed_space(qt_app, home):
    from meeting_notes.client.ui.settings_dialog import SettingsDialog

    _configure(home)
    (home / "rec").mkdir()
    a = _old(_make_session_dir(home / "rec", "a"), 60)
    b = _old(_make_session_dir(home / "rec", "b"), 60)
    kept = _old(_make_session_dir(home / "rec", "c"), 1)
    dialog = SettingsDialog()
    dialog.retention_combo.setCurrentIndex(dialog.retention_combo.findData(30))
    dialog.cleanup_planner = lambda folder, days, url, token, active_dir=None: retention.plan_with_server(
        folder, days, url, token, active_dir=active_dir, client_factory=lambda: _FakeClient()
    )
    removed = []
    dialog.cleanup_executor = lambda plan, queue, active_dir=None: retention.execute_plan(
        plan, queue=queue, active_dir=active_dir, remover=lambda p: removed.append(p.name) or "permanent"
    )
    asked = []
    dialog._confirm_cleanup = lambda count, size, days: asked.append((count, days)) or True
    dialog.cleanup_button.click()
    assert _pump(lambda: dialog.cleanup_result.text().startswith("Moved 2 recordings to the Recycle Bin"))
    assert asked == [(2, 30)]
    assert sorted(removed) == ["a", "b"] and kept.exists()
    assert "freeing" in dialog.cleanup_result.text()
    dialog.close()


def test_clean_up_now_declined_removes_nothing(qt_app, home):
    from meeting_notes.client.ui.settings_dialog import SettingsDialog

    _configure(home)
    (home / "rec").mkdir()
    _old(_make_session_dir(home / "rec", "a"), 60)
    dialog = SettingsDialog()
    dialog.retention_combo.setCurrentIndex(dialog.retention_combo.findData(7))
    dialog.cleanup_planner = lambda folder, days, url, token, active_dir=None: retention.plan_with_server(
        folder, days, url, token, active_dir=active_dir, client_factory=lambda: _FakeClient()
    )
    executed = []
    dialog.cleanup_executor = lambda *a, **k: executed.append(1)
    dialog._confirm_cleanup = lambda *a: False
    dialog.cleanup_button.click()
    assert _pump(lambda: "cancelled" in dialog.cleanup_result.text())
    assert executed == []
    dialog.close()


def test_clean_up_now_with_nothing_eligible_says_why(qt_app, home):
    from meeting_notes.client.ui.settings_dialog import SettingsDialog

    _configure(home)
    (home / "rec").mkdir()
    _old(_make_session_dir(home / "rec", "fresh"), 1)
    dialog = SettingsDialog()
    dialog.retention_combo.setCurrentIndex(dialog.retention_combo.findData(30))
    dialog.cleanup_button.click()  # real planner; nothing is old enough so no network call is made
    assert _pump(lambda: dialog.cleanup_result.text().startswith("Nothing to clean up"))
    assert "1 too recent" in dialog.cleanup_result.text()
    dialog.close()


class _FakeClient:
    def session_detail(self, sid):
        return _detail(sid)

    def close(self):
        pass


# -- background scheduling ----------------------------------------------------------


def test_background_cleanup_never_runs_while_recording(qt_app, home, monkeypatch):
    from meeting_notes.client.controller import IDLE, RECORDING
    from meeting_notes.client.ui import main_window as mw

    _configure(home, local_retention_days=30)
    window = _window(qt_app, monkeypatch)
    try:
        calls = []
        monkeypatch.setattr(mw.retention, "run_with_server", lambda *a, **k: calls.append(a) or None)
        window.controller.state = RECORDING
        window._run_retention()
        assert calls == [] and window._retention_running is False
        window.controller.state = IDLE
        window._run_retention()
        assert _pump(lambda: len(calls) == 1 and window._retention_running is False)
        assert calls[0][1] == 30
    finally:
        window.controller.state = IDLE
        _close(window)


def test_background_cleanup_is_off_by_default(qt_app, home, monkeypatch):
    from meeting_notes.client.ui import main_window as mw

    _configure(home)  # no local_retention_days
    window = _window(qt_app, monkeypatch)
    try:
        calls = []
        monkeypatch.setattr(mw.retention, "run_with_server", lambda *a, **k: calls.append(a))
        window._run_retention()
        time.sleep(0.05)
        assert calls == [] and window._retention_running is False
    finally:
        _close(window)
