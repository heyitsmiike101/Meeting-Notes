"""The Notion export against a fake Notion server (no real API, no real token)."""

from __future__ import annotations

import json
import logging

import pytest

from fake_notion import TOKEN, VERSION
from notion_helpers import Env, add_meeting, complete_notes, notes

from meeting_notes.server.notion_api import NotionClient, NotionError


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    return Env(tmp_path)


def export(env, sid, name, started, **kw):
    """Create a meeting whose notes complete (auto-copy queues it) and run the worker."""
    add_meeting(env.store, sid, name, started, **kw)
    env.run()


# -- connection ------------------------------------------------------------------------------


def test_token_validation_good_and_bad(env):
    ok = env.sync.test()
    assert ok == {"ok": True, "name": "Meeting Notes bot", "workspace": "Mike's workspace"}
    env.sync.tokens.set("ntn_wrong")
    bad = env.sync.test()
    assert bad["ok"] is False and "rejected the integration token" in bad["error"]
    assert "ntn_wrong" not in json.dumps(bad)


def test_connect_validates_before_saving_and_never_returns_the_token(tmp_path, monkeypatch):
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    env = Env(tmp_path)
    env.sync.tokens.clear()
    assert env.sync.connection()["connected"] is False
    refused = env.sync.connect("ntn_not_valid")
    assert refused["ok"] is False and not env.sync.connected()  # a bad token is not stored
    good = env.sync.connect(TOKEN)
    assert good["ok"] and env.sync.connected()
    info = env.sync.connection()
    assert info == {"connected": True, "source": "settings", "bot_name": "Meeting Notes bot",
                    "workspace_name": "Mike's workspace"}
    assert TOKEN not in json.dumps(info)
    env.sync.disconnect()
    assert env.sync.connection()["connected"] is False and not env.sync.tokens.path.exists()


def test_env_token_wins_and_blocks_entering_another(tmp_path, monkeypatch):
    env = Env(tmp_path)
    env.sync.tokens.clear()
    monkeypatch.setenv("NOTION_TOKEN", TOKEN)
    assert env.sync.connection()["source"] == "env" and env.sync.connected()
    assert env.sync.test()["ok"]
    assert env.sync.connect("ntn_other")["ok"] is False
    assert not env.sync.tokens.path.exists()


def test_every_request_pins_the_version_and_uses_bearer_auth(env):
    export(env, "m1", "Planning", "2026-09-30 10:00")
    assert env.fake.requests and {r["version"] for r in env.fake.requests} == {VERSION}


# -- month pages & structure --------------------------------------------------------------------


def test_creates_month_page_and_toggle_heading_with_notes(env):
    export(env, "m1", "Planning", "2026-09-30 10:00")
    month = env.month_page("September-2026 Standard")
    assert env.fake.nodes[month]["parent"] == env.root_page
    assert env.fake.toggles(month) == ["Sep 30 · Planning"]
    toggle = env.fake.nodes[env.fake.toggle_id(month, "Sep 30 · Planning")]
    assert toggle["type"] == "heading_1" and toggle["heading_1"]["is_toggleable"] is True
    tree = env.fake.tree(toggle["id"])
    types = [t["type"] for t in tree]
    assert types[0] == "heading_2" and tree[0]["text"] == "Summary"
    assert "to_do" in types
    assert tree[-1]["type"] == "paragraph" and "Meeting Notes" in tree[-1]["text"]
    link = env.fake.nodes[toggle["id"]]["children"][-1]
    assert env.fake.nodes[link]["paragraph"]["rich_text"][0]["text"]["link"]["url"] == "http://meeting.lan/sessions/m1"
    status = env.sync.session_status("m1")
    assert status["state"] == "copied" and status["url"].startswith("https://www.notion.so/")
    assert status["url"].endswith("#" + toggle["id"].replace("-", ""))


def test_month_title_uses_local_zone_and_style_name(env):
    # 2026-09-30 22:30 local (UTC-4) is already October in UTC: the local zone decides the month.
    export(env, "late", "Late call", "2026-09-30 22:30")
    assert env.fake.find_page("September-2026 Standard")
    assert env.fake.find_page("October-2026 Standard") is None
    export(env, "next", "Next month", "2026-10-01 09:00")
    assert env.fake.find_page("October-2026 Standard")


def test_style_name_in_title_and_separate_parent(env):
    from meeting_notes.server import settings as settings_mod

    tpl = settings_mod.load_settings(env.store.root).find_template("webinar")
    add_meeting(env.store, "w1", "Launch webinar", "2026-09-12 13:00",
                template={"id": tpl["id"], "name": tpl["name"]})
    env.run()
    page = env.month_page("September-2026 Detailed webinar")
    assert env.fake.nodes[page]["parent"] == env.webinar_page


def test_month_page_found_by_stored_id_without_listing_or_creating(env):
    export(env, "m1", "One", "2026-09-30 10:00")
    before = len(env.fake.calls("POST", "/v1/pages"))
    env.fake.requests.clear()
    export(env, "m2", "Two", "2026-09-30 11:00")
    assert len(env.fake.calls("POST", "/v1/pages")) == 0 and before == 1
    assert not [r for r in env.fake.requests if r["path"].endswith(f"{env.root_page.replace('-', '')}/children")]
    assert len([p for p in env.fake.child_titles(env.root_page)]) == 1


def test_month_page_found_by_title_when_state_is_lost(env):
    export(env, "m1", "One", "2026-09-30 10:00")
    env.sync.state.path.unlink()
    env.fake.requests.clear()
    export(env, "m2", "Two", "2026-09-30 11:00")
    assert env.fake.calls("POST", "/v1/pages") == []  # reused, not duplicated
    assert env.fake.child_titles(env.root_page) == ["September-2026 Standard"]


def test_month_page_recreated_when_it_was_deleted_in_notion(env):
    export(env, "m1", "One", "2026-09-30 10:00")
    old = env.month_page("September-2026 Standard")
    env.fake.trash(old)
    export(env, "m2", "Two", "2026-09-30 11:00")
    pages = [n for n in env.fake.nodes.values() if n["type"] == "page" and n["title"] == "September-2026 Standard"
             and not n["in_trash"]]
    assert len(pages) == 1 and pages[0]["id"] != old
    assert env.fake.toggles(pages[0]["id"]) == ["Sep 30 · Two"]


# -- ordering -------------------------------------------------------------------------------------


def test_newest_first_regardless_of_completion_order(env):
    # Notes finish in this order: oldest day, then middle, then newest, then one in between.
    export(env, "a", "Alpha", "2026-09-10 09:00")
    export(env, "c", "Gamma", "2026-09-28 09:00")
    export(env, "b", "Beta", "2026-09-20 09:00")
    export(env, "d", "Delta", "2026-09-29 09:00")
    page = env.month_page("September-2026 Standard")
    assert env.fake.toggles(page) == ["Sep 29 · Delta", "Sep 28 · Gamma", "Sep 20 · Beta", "Sep 10 · Alpha"]


def test_same_day_orders_by_start_time(env):
    export(env, "m1", "Standup", "2026-09-30 09:00")
    export(env, "m3", "Review", "2026-09-30 16:30")
    export(env, "m2", "Planning", "2026-09-30 12:00")
    page = env.month_page("September-2026 Standard")
    assert env.fake.toggles(page) == ["Sep 30 · Review", "Sep 30 · Planning", "Sep 30 · Standup"]


def test_newest_goes_to_start_and_older_after_next_newer_sibling(env):
    export(env, "old", "Old", "2026-09-01 09:00")
    export(env, "new", "New", "2026-09-02 09:00")
    export(env, "mid", "Mid", "2026-09-01 18:00")
    appends = [r for r in env.fake.calls("PATCH", "/children") if r["body"]["children"][0]["type"] == "heading_1"]
    kinds = [a["body"].get("position", {}).get("type") for a in appends]
    assert kinds == ["start", "start", "after_block"]
    assert all("after" not in a["body"] for a in appends)
    # "mid" follows its next-newer sibling ("new"), not "old"
    page = env.month_page("September-2026 Standard")
    new_block = env.fake.toggle_id(page, "Sep 2 · New")
    assert appends[2]["body"]["position"]["after_block"]["id"] == new_block


def test_anchor_deleted_in_notion_falls_back_to_the_next_neighbour(env):
    export(env, "a", "A", "2026-09-05 09:00")
    export(env, "b", "B", "2026-09-04 09:00")
    page = env.month_page("September-2026 Standard")
    env.fake.trash(env.fake.toggle_id(page, "Sep 5 · A"))
    export(env, "c", "C", "2026-09-03 09:00")
    assert env.fake.toggles(page) == ["Sep 4 · B", "Sep 3 · C"]


def test_same_name_and_date_adds_start_time_to_both(env):
    export(env, "m1", "Weekly sync", "2026-09-30 09:00")
    assert env.fake.toggles(env.month_page("September-2026 Standard")) == ["Sep 30 · Weekly sync"]
    export(env, "m2", "Weekly sync", "2026-09-30 15:45")
    page = env.month_page("September-2026 Standard")
    assert env.fake.toggles(page) == ["Sep 30 · 15:45 · Weekly sync", "Sep 30 · 09:00 · Weekly sync"]


# -- regenerate / restyle / rename --------------------------------------------------------------------


def test_regenerate_same_style_updates_the_toggle_in_place(env):
    export(env, "m1", "Planning", "2026-09-30 10:00")
    export(env, "m2", "Other", "2026-09-29 10:00")
    page = env.month_page("September-2026 Standard")
    block = env.fake.toggle_id(page, "Sep 30 · Planning")
    old_children = list(env.fake.nodes[block]["children"])
    review = env.store.latest_review("m1")
    env.store.retry_review(review["review_id"])
    complete_notes(env.store, review["review_id"], notes(summary="BRAND NEW SUMMARY"))
    assert env.sync.session_status("m1")["state"] == "pending"  # queued automatically, no manual action
    env.run()
    assert env.fake.toggle_id(page, "Sep 30 · Planning") == block  # same block: position and links stay valid
    assert env.fake.toggles(page) == ["Sep 30 · Planning", "Sep 29 · Other"]
    texts = [t["text"] for t in env.fake.tree(block)]
    assert "BRAND NEW SUMMARY" in json.dumps(env.fake.tree(block))
    assert texts.count("Summary") == 1  # old children are gone, not duplicated
    assert all(env.fake.nodes[c]["in_trash"] for c in old_children)
    assert not [r for r in env.fake.calls("DELETE") if r["path"].endswith(block)]
    assert env.sync.session_status("m1")["state"] == "copied"


def test_regenerate_updates_heading_if_meeting_name_changed_meanwhile(env):
    export(env, "m1", "Planning", "2026-09-30 10:00")
    review = env.store.latest_review("m1")
    env.store.retry_review(review["review_id"])
    env.store.rename_session("m1", "Renamed")
    complete_notes(env.store, review["review_id"], notes())
    env.run()
    assert env.fake.toggles(env.month_page("September-2026 Standard")) == ["Sep 30 · Renamed"]


def test_regenerate_inserts_fresh_toggle_when_old_one_was_deleted(env):
    export(env, "m1", "Planning", "2026-09-30 10:00")
    export(env, "m2", "Newer", "2026-10-01 10:00")  # different month page, must be untouched
    export(env, "m3", "Earlier", "2026-09-29 10:00")
    page = env.month_page("September-2026 Standard")
    env.fake.trash(env.fake.toggle_id(page, "Sep 30 · Planning"))
    review = env.store.latest_review("m1")
    env.store.retry_review(review["review_id"])
    complete_notes(env.store, review["review_id"], notes(summary="again"))
    env.run()
    assert env.fake.toggles(page) == ["Sep 30 · Planning", "Sep 29 · Earlier"]
    assert env.sync.session_status("m1")["state"] == "copied"


def test_restyle_moves_the_copy_to_the_other_styles_page(env):
    from meeting_notes.server import settings as settings_mod

    export(env, "m1", "Planning", "2026-09-30 10:00")
    old_page = env.month_page("September-2026 Standard")
    webinar = settings_mod.load_settings(env.store.root).find_template("webinar")
    review = env.store.latest_review("m1")
    env.store.retry_review(review["review_id"], {"id": webinar["id"], "name": webinar["name"]})
    complete_notes(env.store, review["review_id"], notes(summary="webinar style"))
    env.run()
    new_page = env.month_page("September-2026 Detailed webinar")
    assert env.fake.toggles(new_page) == ["Sep 30 · Planning"]
    assert env.fake.toggles(old_page) == []  # removed from the old style's page
    st = env.sync.session_status("m1")
    assert st["state"] == "copied" and st["warning"] is None and st["month_page"] == "September-2026 Detailed webinar"


def test_restyle_when_old_copy_cannot_be_deleted_still_adds_new_and_warns(env):
    from meeting_notes.server import settings as settings_mod

    export(env, "m1", "Planning", "2026-09-30 10:00")
    old_page = env.month_page("September-2026 Standard")
    webinar = settings_mod.load_settings(env.store.root).find_template("webinar")
    review = env.store.latest_review("m1")
    env.store.retry_review(review["review_id"], {"id": webinar["id"], "name": webinar["name"]})
    complete_notes(env.store, review["review_id"], notes())
    env.fake.fail(403, times=5, when=lambda m, p: m == "DELETE",
                  body={"object": "error", "status": 403, "code": "restricted_resource", "message": "no"})
    env.run()
    assert env.fake.toggles(env.month_page("September-2026 Detailed webinar")) == ["Sep 30 · Planning"]
    st = env.sync.session_status("m1")
    assert st["state"] == "copied" and "could not be removed" in st["warning"]
    assert env.fake.toggles(old_page) == ["Sep 30 · Planning"]  # left in place, as warned


def test_restyle_to_a_style_without_a_parent_leaves_notion_alone(env):
    from meeting_notes.server import settings as settings_mod
    import dataclasses

    export(env, "m1", "Planning", "2026-09-30 10:00")
    quick = settings_mod.load_settings(env.store.root).find_template("quick")
    review = env.store.latest_review("m1")
    env.fake.requests.clear()
    env.store.retry_review(review["review_id"], {"id": quick["id"], "name": quick["name"]})
    complete_notes(env.store, review["review_id"], notes())
    assert env.run() == 0 and env.fake.requests == []
    assert env.fake.toggles(env.month_page("September-2026 Standard")) == ["Sep 30 · Planning"]


def test_rename_meeting_updates_the_heading(env):
    export(env, "m1", "Planning", "2026-09-30 10:00")
    page = env.month_page("September-2026 Standard")
    block = env.fake.toggle_id(page, "Sep 30 · Planning")
    env.store.rename_session("m1", "Quarterly planning")
    assert env.run() == 1
    assert env.fake.toggles(page) == ["Sep 30 · Quarterly planning"]
    assert env.fake.toggle_id(page, "Sep 30 · Quarterly planning") == block
    assert env.fake.nodes[block]["heading_1"]["is_toggleable"] is True


def test_rename_of_a_meeting_never_copied_does_nothing(env):
    add_meeting(env.store, "m1", "Planning", "2026-09-30 10:00", complete=False)
    env.fake.requests.clear()
    env.store.rename_session("m1", "Other")
    assert env.run() == 0 and env.fake.requests == []


def test_deleting_or_purging_a_meeting_leaves_notion_alone(env):
    export(env, "m1", "Planning", "2026-09-30 10:00")
    page = env.month_page("September-2026 Standard")
    env.fake.requests.clear()
    env.store.trash_session("m1")
    env.run()
    env.store.purge_trashed("m1")
    env.run()
    assert env.fake.requests == []
    assert env.fake.toggles(page) == ["Sep 30 · Planning"]


# -- limits -----------------------------------------------------------------------------------------


def test_large_notes_are_batched_nested_and_split(env):
    items = "\n".join(f"- point {i}\n  - sub {i}\n    - subsub {i}" for i in range(70))
    body = items + "\n\n" + ("long " * 900) + "\n\n```\n" + "z" * 4500 + "\n```"
    export(env, "big", "Big meeting", "2026-09-30 10:00", payload=notes(
        body=body, key_points=[f"kp {i}" for i in range(260)]))
    st = env.sync.session_status("big")
    assert st["state"] == "copied", st
    block = env.fake.toggle_id(env.month_page("September-2026 Standard"), "Sep 30 · Big meeting")
    tree = json.dumps(env.fake.tree(block))
    assert "kp 259" in tree and "subsub 69" in tree
    appends = env.fake.calls("PATCH", "/children")
    assert max(len(a["body"]["children"]) for a in appends) <= 100
    assert len(appends) >= 5


# -- rate limit, retries, failures ----------------------------------------------------------------------


def test_429_retry_after_is_honoured(env):
    env.fake.fail(429, times=2, headers={"Retry-After": "3"},
                  body={"object": "error", "status": 429, "code": "rate_limited", "message": "slow"})
    export(env, "m1", "Planning", "2026-09-30 10:00")
    assert env.sync.session_status("m1")["state"] == "copied"
    assert env.sleeps.count(3.0) == 2


def test_client_spaces_requests_to_about_three_per_second():
    from fake_notion import FakeNotion

    fake = FakeNotion()
    slept = []
    t = {"now": 0.0}
    client = NotionClient(fake.token, transport=fake.transport(), min_interval=0.35,
                          sleep=lambda s: (slept.append(s), t.__setitem__("now", t["now"] + s)),
                          clock=lambda: t["now"])
    for _ in range(4):
        client.me()
    assert slept == pytest.approx([0.35, 0.35, 0.35])


def test_persistent_429_becomes_a_retryable_failure():
    from fake_notion import FakeNotion

    fake = FakeNotion()
    fake.fail(429, times=99, headers={"Retry-After": "1"})
    client = NotionClient(fake.token, transport=fake.transport(), min_interval=0, sleep=lambda s: None)
    with pytest.raises(NotionError) as info:
        client.me()
    assert info.value.retryable and "rate limiting" in info.value.reason


def test_transient_failure_retries_with_backoff_then_succeeds(env):
    env.fake.fail(500, times=99, when=lambda m, p: m == "POST" and p == "/v1/pages")
    add_meeting(env.store, "m1", "Planning", "2026-09-30 10:00")
    env.run()
    st = env.sync.session_status("m1")
    assert st["state"] == "pending" and st["retrying"] and env.sync.next_due_in() == 10.0
    assert env.run() == 0  # not due yet
    env.clock["t"] += 11
    env.fake.rules.clear()
    assert env.run() == 1
    assert env.sync.session_status("m1")["state"] == "copied"
    assert env.sync.pending_count() == 0


def test_gives_up_after_max_attempts_with_a_readable_reason(env):
    env.fake.fail(503, times=999, when=lambda m, p: m == "POST" and p == "/v1/pages")
    add_meeting(env.store, "m1", "Planning", "2026-09-30 10:00")
    for _ in range(10):
        env.run()
        env.clock["t"] += 1000
    st = env.sync.session_status("m1")
    assert st["state"] == "failed" and "Gave up after 5 attempts" in st["error"]
    assert env.sync.pending_count() == 0
    # Retry: queue it again once Notion is healthy.
    env.fake.rules.clear()
    env.sync.enqueue_export("m1")
    env.run()
    assert env.sync.session_status("m1")["state"] == "copied"


def test_unshared_parent_fails_at_once_and_says_how_to_fix_it(env):
    env.fake.nodes.pop(env.root_page)  # Notion: object_not_found (page not shared with the integration)
    add_meeting(env.store, "m1", "Planning", "2026-09-30 10:00")
    env.run()
    st = env.sync.session_status("m1")
    assert st["state"] == "failed" and "share it with the integration" in st["error"]
    assert env.sync.pending_count() == 0  # a permanent error is not retried


def test_revoked_token_is_reported(env):
    env.sync.tokens.set("ntn_revoked")
    add_meeting(env.store, "m1", "Planning", "2026-09-30 10:00")
    env.run()
    assert "rejected the integration token" in env.sync.session_status("m1")["error"]


def test_not_connected_is_a_clear_failure(env):
    env.sync.tokens.clear()
    add_meeting(env.store, "m1", "Planning", "2026-09-30 10:00")  # auto-copy needs a connection: not queued
    assert env.sync.pending_count() == 0
    assert env.sync.can_send("m1") == (False, "Notion is not connected. Add the integration token in Settings.")
    env.sync.enqueue_export("m1")
    env.run()
    assert "not connected" in env.sync.session_status("m1")["error"]


# -- jobs survive a restart ---------------------------------------------------------------------------------


def test_job_resumes_after_restart(env):
    add_meeting(env.store, "m1", "Planning", "2026-09-30 10:00")  # queued; the old process "dies" before running it
    assert env.sync.pending_count() == 1 and list(env.sync.jobs_dir.glob("*.json"))
    restarted = env.new_sync()
    assert restarted.pending_count() == 0
    assert len(restarted.resume_interrupted()) == 1
    assert restarted.run_pending() == 1
    assert env.fake.toggles(env.month_page("September-2026 Standard")) == ["Sep 30 · Planning"]
    assert not list(restarted.jobs_dir.glob("*.json"))  # finished jobs leave no file behind


def test_running_job_is_requeued_on_restart_and_keeps_its_attempts(env):
    add_meeting(env.store, "m1", "Planning", "2026-09-30 10:00")
    (path,) = env.sync.jobs_dir.glob("*.json")
    job = json.loads(path.read_text())
    job.update(state="running", attempts=2)
    path.write_text(json.dumps(job))
    restarted = env.new_sync()
    restarted.resume_interrupted()
    assert restarted._jobs[job["job_id"]]["state"] == "queued" and restarted._jobs[job["job_id"]]["attempts"] == 2
    restarted.run_pending()
    assert restarted.session_status("m1")["state"] == "copied"


def test_job_for_a_deleted_meeting_fails_without_resurrecting_it(env):
    add_meeting(env.store, "m1", "Planning", "2026-09-30 10:00")
    env.store.trash_session("m1")
    env.run()
    st = env.sync.session_status("m1")
    assert st["state"] == "failed" and "no longer exists" in st["error"]
    assert env.fake.find_page("September-2026 Standard") is None


def test_duplicate_requests_coalesce_into_one_job(env):
    add_meeting(env.store, "m1", "Planning", "2026-09-30 10:00")
    env.sync.enqueue_export("m1")
    env.sync.enqueue_export("m1")
    assert env.sync.pending_count() == 1
    env.run()
    assert len(env.fake.toggles(env.month_page("September-2026 Standard"))) == 1


# -- when exports happen ----------------------------------------------------------------------------------------


def test_auto_copy_off_or_style_without_parent_queues_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    off = Env(tmp_path / "a", auto=False)
    add_meeting(off.store, "m1", "Planning", "2026-09-30 10:00")
    assert off.sync.pending_count() == 0
    assert off.sync.can_send("m1") == (True, None)  # manual send still works
    from meeting_notes.server import settings as settings_mod

    on = Env(tmp_path / "b")
    quick = settings_mod.load_settings(on.store.root).find_template("quick")
    add_meeting(on.store, "m2", "Quick one", "2026-09-30 10:00", template={"id": quick["id"], "name": quick["name"]})
    assert on.sync.pending_count() == 0
    ok, reason = on.sync.can_send("m2")
    assert not ok and "Quick notes" in reason and "parent page" in reason


def test_backfill_selects_meetings_in_the_style_without_a_copy(env):
    from meeting_notes.server import settings as settings_mod
    import dataclasses

    # auto-copy off so nothing is exported while we build the library
    s = settings_mod.load_settings(env.store.root)
    settings_mod.save_settings(env.store.root, dataclasses.replace(s, notion_auto_copy=False))
    webinar = s.find_template("webinar")
    add_meeting(env.store, "s1", "Std one", "2026-09-01 09:00")
    add_meeting(env.store, "s2", "Std two", "2026-09-02 09:00")
    add_meeting(env.store, "s3", "Std three", "2026-09-03 09:00")
    add_meeting(env.store, "w1", "Webinar", "2026-09-04 09:00", template={"id": "webinar", "name": webinar["name"]})
    add_meeting(env.store, "n1", "No notes yet", "2026-09-05 09:00", queue_review=False)
    add_meeting(env.store, "t1", "Trashed", "2026-09-06 09:00")
    env.store.trash_session("t1")
    env.sync.enqueue_export("s1")
    env.run()  # s1 is already in Notion
    assert sorted(env.sync.backfill_candidates("standard")) == ["s2", "s3"]
    assert env.sync.backfill_candidates("webinar") == ["w1"]
    assert env.sync.backfill("standard") == 2
    env.run()
    assert env.fake.toggles(env.month_page("September-2026 Standard")) == [
        "Sep 3 · Std three", "Sep 2 · Std two", "Sep 1 · Std one"]
    assert env.sync.backfill_candidates("standard") == []


# -- secrets -----------------------------------------------------------------------------------------------------------


def test_token_never_appears_in_settings_files_logs_or_errors(env, caplog):
    caplog.set_level(logging.DEBUG)
    env.fake.fail(500, times=3)
    export(env, "m1", "Planning", "2026-09-30 10:00")
    env.sync.tokens.set(TOKEN)
    env.sync.test()
    env.sync.tokens.set("ntn_revoked_SECRETVALUE12345")
    add_meeting(env.store, "m2", "Second", "2026-09-30 11:00")
    env.run()
    env.sync.tokens.set(TOKEN)
    blob = caplog.text + json.dumps(env.sync.session_status("m1")) + json.dumps(env.sync.session_status("m2"))
    assert TOKEN not in blob and "SECRETVALUE12345" not in blob
    settings_text = (env.store.root / "settings.json").read_text()
    state_text = env.sync.state.path.read_text()
    assert TOKEN not in settings_text and TOKEN not in state_text
    assert TOKEN in env.sync.tokens.path.read_text()  # only the dedicated token file holds it


# -- time zone -----------------------------------------------------------------------------------------------------


def test_local_tz_uses_the_tz_environment_variable_and_falls_back(monkeypatch):
    from meeting_notes.server.notion import local_dt, local_tz

    monkeypatch.setenv("TZ", "Not/AZone")
    assert local_tz() is None  # unknown zone: system local time, no crash
    assert local_dt(1_800_000_000.0).tzinfo is not None
    monkeypatch.delenv("TZ")
    assert local_tz() is None
    try:
        import zoneinfo

        zoneinfo.ZoneInfo("America/New_York")
    except Exception:
        pytest.skip("no tz database on this machine (the server image installs tzdata)")
    monkeypatch.setenv("TZ", "America/New_York")
    assert str(local_tz()) == "America/New_York"
    assert local_dt(1_790_000_000.0, local_tz()).utcoffset().total_seconds() == -4 * 3600
