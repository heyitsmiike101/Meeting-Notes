"""The Notion bar's "..." menu ("Send again" / "Remove this note from Notion"), the opt-out that Remove records,
and combine / split following along in Notion. A fake Notion stands behind the client; no real API."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from fake_notion import TOKEN
from notion_helpers import TZ, Env, add_meeting, complete_notes, notes
from meeting_notes.server import splitmerge as sm
from meeting_notes.server.app import create_app
from tests.split_helpers import make_meeting, ramp, seg

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated:DeprecationWarning"
)

WEB_TOKEN = "menu-secret"
WEB = {"Authorization": f"Bearer {WEB_TOKEN}"}
T0 = 1_790_000_000.0  # 2026-09-21 in the test zone


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    return Env(tmp_path)


@pytest.fixture
def manual_env(tmp_path, monkeypatch):
    """Auto-copy is off for every note type: only meetings the owner sent (or inherited) are in Notion."""
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    return Env(tmp_path, auto=False)


class Ctx:
    pass


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_NOTES_TOKEN", WEB_TOKEN)
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    c = Ctx()
    e = Env(tmp_path / "x")
    c.env = e
    c.fake = e.fake
    c.app = create_app(
        data_root=str(tmp_path / "x" / "data"),
        notion_options={"transport": e.fake.transport(), "sleep": lambda s: None, "min_interval": 0.0, "tz": TZ},
    )
    c.client = TestClient(c.app)
    c.store = c.app.state.store
    c.notion = c.app.state.notion
    c.notion.stop()  # jobs are run by hand (run_pending) so the tests are deterministic
    c.notion._stop.clear()
    c.notion.tokens.set(TOKEN)
    c.page = e.fake.add_page("Notes root")
    from meeting_notes.server import settings as settings_mod
    import dataclasses

    cur = settings_mod.load_settings(c.store.root)
    settings_mod.save_settings(c.store.root, dataclasses.replace(
        cur, notion_parents={"standard": c.page}, notion_auto_types=["standard"], notion_auto_copy=True))
    return c


def meeting_in_notion(env, sid, name="Planning", started="2026-09-30 10:00"):
    add_meeting(env.store, sid, name, started)
    env.run()
    assert env.sync.session_status(sid)["state"] == "copied"


def trio(store):
    """Two short transcript-and-audio meetings to combine, and one long one to split."""
    make_meeting(store, "a", "Kickoff", T0, 120, [seg(5, 30, "You", "a-one")], mic=ramp(120))
    make_meeting(store, "b", "Kickoff again", T0 + 180, 90, [seg(5, 10, "You", "b-one")], mic=ramp(90, start_sec=2000))
    make_meeting(store, "long", "Long one", T0 + 7200, 400, [seg(10, 90, "You", "first"), seg(250, 300, "You", "second")],
                 mic=ramp(400))


def notes_for(env, sid):
    review = env.store.create_review(sid)
    complete_notes(env.store, review["review_id"], notes(title=sid))


def block_alive(env, sid):
    rec = env.sync.state.meeting(sid) or {}
    return bool(rec.get("block_id"))


# -- the menu in the meeting view ---------------------------------------------------------------------


def test_meeting_view_has_the_notion_options_menu_with_send_again_and_remove(ctx):
    add_meeting(ctx.store, "m1", "Planning", "2026-09-30 10:00")
    page = ctx.client.get("/sessions/m1", headers=WEB).text
    for marker in ("Notion options", "notion-more", "notion-menu", "aria-haspopup=\"menu\"", "role=\"menu\"",
                   "Send again", "Remove this note from Notion", "id=\"notion-remove\"", "danger-item",
                   "method:'DELETE'", "Remove from Notion", "bindNotionMenu", "closeNotionMenu"):
        assert marker in page, marker
    # the in-app dialog, never the browser's
    start = page.index("var rm=document.getElementById('notion-remove')")
    handler = page[start:start + 1500]
    assert "confirmDialog(" in handler and "danger:true" in handler and "window.confirm" not in handler
    # "Send again" is no longer a plain button: it only exists as a menu item, while Send/Retry stay buttons
    assert "state==='none'?'Send to Notion':'Retry'" in page
    assert "'Send again':" not in page and "?'Send to Notion':(state==='failed'?'Retry':'Send again')" not in page
    assert 'class="btn secondary sm" id="notion-send"' in page


def test_meetings_list_chip_knows_the_removing_state(ctx):
    assert "n.state==='removing'" in ctx.client.get("/meetings", headers=WEB).text


# -- Remove: endpoint, state, opt-out --------------------------------------------------------------------


def test_remove_deletes_the_toggle_clears_state_and_records_the_opt_out(ctx):
    add_meeting(ctx.store, "m1", "Planning", "2026-09-30 10:00")
    ctx.notion.run_pending()
    month = ctx.env.month_page("September-2026 Standard")
    assert ctx.fake.toggles(month) == ["Sep 30 · Planning"]
    r = ctx.client.delete("/v1/sessions/m1/notion", headers=WEB)
    assert r.status_code == 200 and r.json()["state"] == "removing"
    listed = ctx.notion.list_status(["m1"])
    assert listed["m1"]["state"] == "removing"
    ctx.notion.run_pending()
    status = ctx.client.get("/v1/sessions/m1/notion", headers=WEB).json()
    assert status["state"] == "none" and status["url"] is None and status["opted_out"] is True
    assert status["warning"] is None
    assert ctx.fake.toggles(month) == []
    assert len(ctx.fake.calls("DELETE", "/v1/blocks/")) == 1
    assert ctx.notion.pending_count() == 0


def test_remove_needs_the_token_a_known_meeting_and_a_connection(ctx):
    add_meeting(ctx.store, "m1", "Planning", "2026-09-30 10:00")
    assert ctx.client.delete("/v1/sessions/m1/notion").status_code in (401, 403)
    assert ctx.client.delete("/v1/sessions/nope/notion", headers=WEB).status_code == 404
    ctx.notion.tokens.clear()
    r = ctx.client.delete("/v1/sessions/m1/notion", headers=WEB)
    assert r.status_code == 409 and "not connected" in r.json()["detail"]


def test_remove_when_nothing_is_in_notion_changes_nothing(ctx):
    add_meeting(ctx.store, "m1", "Planning", "2026-09-30 10:00", queue_review=False)
    r = ctx.client.delete("/v1/sessions/m1/notion", headers=WEB)
    assert r.status_code == 200 and r.json()["state"] == "none" and r.json()["opted_out"] is False
    assert ctx.fake.calls("DELETE") == []


@pytest.mark.parametrize("how", ["trashed", "missing"])
def test_a_page_already_gone_in_notion_counts_as_removed(ctx, how):
    add_meeting(ctx.store, "m1", "Planning", "2026-09-30 10:00")
    ctx.notion.run_pending()
    block = ctx.notion.state.meeting("m1")["block_id"]
    if how == "trashed":
        ctx.fake.trash(block)
    else:
        del ctx.fake.nodes[block]
    assert ctx.client.delete("/v1/sessions/m1/notion", headers=WEB).status_code == 200
    ctx.notion.run_pending()
    status = ctx.client.get("/v1/sessions/m1/notion", headers=WEB).json()
    assert status["state"] == "none" and status["opted_out"] is True and status["warning"] is None


def test_a_refused_removal_is_reported_and_the_meeting_stays_in_step(ctx):
    add_meeting(ctx.store, "m1", "Planning", "2026-09-30 10:00")
    ctx.notion.run_pending()
    ctx.fake.fail(403, when=lambda m, p: m == "DELETE", body={"code": "restricted_resource", "message": "no"})
    assert ctx.client.delete("/v1/sessions/m1/notion", headers=WEB).status_code == 200
    ctx.notion.run_pending()
    status = ctx.client.get("/v1/sessions/m1/notion", headers=WEB).json()
    assert status["state"] == "copied" and status["opted_out"] is False
    assert "Could not remove" in status["warning"]


def test_opt_out_stops_automatic_resync_and_auto_copy_until_a_manual_send(ctx):
    add_meeting(ctx.store, "m1", "Planning", "2026-09-30 10:00")
    ctx.notion.run_pending()
    ctx.client.delete("/v1/sessions/m1/notion", headers=WEB)
    ctx.notion.run_pending()
    ctx.fake.requests.clear()
    # regenerated notes: the "already in Notion always resyncs" rule and the auto-copy rule both stay quiet
    review = ctx.store.latest_review("m1")
    ctx.store.retry_review(review["review_id"])
    complete_notes(ctx.store, review["review_id"], notes(title="Planning v2"))
    assert ctx.notion.pending_count() == 0 and ctx.notion.run_pending() == 0
    assert ctx.fake.requests == []
    assert ctx.notion.session_status("m1")["state"] == "none"
    assert ctx.notion.enqueue_export("m1", source="auto") == ""  # no automatic source gets through either
    assert ctx.notion.backfill_candidates("standard") == []
    # the owner sends it again: the opt-out is gone and it is kept in step from then on
    assert ctx.client.post("/v1/sessions/m1/notion", headers=WEB).json()["state"] == "pending"
    ctx.notion.run_pending()
    status = ctx.client.get("/v1/sessions/m1/notion", headers=WEB).json()
    assert status["state"] == "copied" and status["opted_out"] is False
    ctx.store.retry_review(review["review_id"])
    complete_notes(ctx.store, review["review_id"], notes(title="Planning v3"))
    assert ctx.notion.pending_count() == 1  # resync is back


def test_a_send_waiting_in_the_queue_is_cancelled_by_remove(ctx):
    add_meeting(ctx.store, "m1", "Planning", "2026-09-30 10:00")
    ctx.notion.run_pending()
    ctx.notion.enqueue_export("m1", source="manual")  # queued, not yet run
    ctx.client.delete("/v1/sessions/m1/notion", headers=WEB)
    ctx.notion.run_pending()
    assert ctx.notion.session_status("m1")["state"] == "none"
    month = ctx.env.month_page("September-2026 Standard")
    assert ctx.fake.toggles(month) == []


def test_removing_one_meeting_keeps_the_anchors_other_meetings_rely_on(env):
    for sid, day in (("m1", "2026-09-10"), ("m2", "2026-09-20"), ("m3", "2026-09-30")):
        meeting_in_notion(env, sid, name=sid.upper(), started=f"{day} 10:00")
    month = env.month_page("September-2026 Standard")
    assert env.fake.toggles(month) == ["Sep 30 · M3", "Sep 20 · M2", "Sep 10 · M1"]
    assert env.sync.request_remove("m2") is True
    env.run()
    assert env.fake.toggles(month) == ["Sep 30 · M3", "Sep 10 · M1"]
    assert not block_alive(env, "m2") and block_alive(env, "m1") and block_alive(env, "m3")
    # a meeting between the two survivors lands between them (the removed one is not used as an anchor)
    meeting_in_notion(env, "m4", name="M4", started="2026-09-25 10:00")
    assert env.fake.toggles(month) == ["Sep 30 · M3", "Sep 25 · M4", "Sep 10 · M1"]
    # and so does the newest one being removed: the next one still has a valid anchor
    env.sync.request_remove("m3")
    env.run()
    meeting_in_notion(env, "m5", name="M5", started="2026-09-27 10:00")
    assert env.fake.toggles(month) == ["Sep 27 · M5", "Sep 25 · M4", "Sep 10 · M1"]


def test_removing_a_twin_drops_the_time_suffix_of_the_other(env):
    meeting_in_notion(env, "t1", name="Standup", started="2026-09-30 09:00")
    meeting_in_notion(env, "t2", name="Standup", started="2026-09-30 14:00")
    month = env.month_page("September-2026 Standard")
    assert len(env.fake.toggles(month)) == 2 and all("·" in t and ":" in t for t in env.fake.toggles(month))
    env.sync.request_remove("t2")
    env.run()
    assert env.fake.toggles(month) == ["Sep 30 · Standup"]


# -- combine ------------------------------------------------------------------------------------------


def test_combine_removes_source_pages_and_the_result_syncs_when_its_notes_complete(manual_env):
    env = manual_env
    trio(env.store)
    for sid in ("a", "b"):
        notes_for(env, sid)
        env.sync.enqueue_export(sid, source="manual")
    env.run()
    month = env.month_page("September-2026 Standard")
    assert len(env.fake.toggles(month)) == 2
    cid = sm.combine_sessions(env.store, ["a", "b"])["session_id"]
    assert env.sync.session_status("a")["state"] == "removing"
    env.run()
    assert env.fake.toggles(month) == []
    assert all(not block_alive(env, s) for s in ("a", "b"))
    # no notes yet, so nothing is sent; once they complete the combined meeting follows its sources into Notion
    assert env.sync.session_status(cid)["state"] == "none"
    notes_for(env, cid)
    env.run()
    assert env.sync.session_status(cid)["state"] == "copied"
    assert env.fake.toggles(month) == ["Sep 21 · Kickoff"]


def test_combine_of_meetings_not_in_notion_is_not_sent_unless_the_type_auto_copies(manual_env):
    env = manual_env
    trio(env.store)
    notes_for(env, "a")  # has notes but was never sent, and auto-copy is off
    cid = sm.combine_sessions(env.store, ["a", "b"])["session_id"]
    env.run()
    notes_for(env, cid)
    env.run()
    assert env.sync.session_status(cid)["state"] == "none" and env.fake.calls("POST", "/v1/pages") == []


def test_combine_result_auto_copies_when_its_note_type_does_even_if_no_source_was_in_notion(env):
    trio(env.store)  # sources have no notes, so none of them is in Notion
    cid = sm.combine_sessions(env.store, ["a", "b"])["session_id"]
    env.run()
    assert env.sync.session_status(cid)["state"] == "none"
    notes_for(env, cid)
    env.run()
    assert env.sync.session_status(cid)["state"] == "copied"


def test_one_source_in_notion_is_enough(manual_env):
    env = manual_env
    trio(env.store)
    notes_for(env, "b")
    env.sync.enqueue_export("b", source="manual")
    env.run()
    cid = sm.combine_sessions(env.store, ["a", "b"])["session_id"]
    env.run()
    notes_for(env, cid)
    env.run()
    assert env.sync.session_status(cid)["state"] == "copied"


def test_a_source_the_owner_removed_does_not_pull_the_result_into_notion(manual_env):
    env = manual_env
    trio(env.store)
    notes_for(env, "a")
    env.sync.enqueue_export("a", source="manual")
    env.run()
    env.sync.request_remove("a")
    env.run()
    cid = sm.combine_sessions(env.store, ["a", "b"])["session_id"]
    env.run()
    notes_for(env, cid)
    env.run()
    assert env.sync.session_status(cid)["state"] == "none"


def test_restoring_a_trashed_source_does_not_recreate_its_notion_page(manual_env):
    env = manual_env
    trio(env.store)
    notes_for(env, "a")
    env.sync.enqueue_export("a", source="manual")
    env.run()
    sm.combine_sessions(env.store, ["a", "b"])
    env.run()
    env.fake.requests.clear()
    env.store.restore_session("a")  # from /meetings/trash
    assert env.run() == 0 and env.fake.requests == []
    status = env.sync.session_status("a")
    assert status["state"] == "none" and status["url"] is None and status["can_send"] is True
    # normal rules from here on: regenerated notes of a non-auto type stay out of Notion
    review = env.store.latest_review("a")
    env.store.retry_review(review["review_id"])
    complete_notes(env.store, review["review_id"], notes(title="again"))
    assert env.run() == 0


def test_uncombine_removes_the_result_and_puts_the_originals_back_in_notion(manual_env):
    env = manual_env
    trio(env.store)
    for sid in ("a", "b"):
        notes_for(env, sid)
        env.sync.enqueue_export(sid, source="manual")
    env.run()
    cid = sm.combine_sessions(env.store, ["a", "b"])["session_id"]
    env.run()
    notes_for(env, cid)
    env.run()
    month = env.month_page("September-2026 Standard")
    assert env.fake.toggles(month) == ["Sep 21 · Kickoff"]
    sm.uncombine(env.store, cid)
    env.run()
    assert env.fake.toggles(month) == ["Sep 21 · Kickoff again", "Sep 21 · Kickoff"]  # newest first
    assert env.sync.session_status("a")["state"] == "copied" and env.sync.session_status("b")["state"] == "copied"
    assert not block_alive(env, cid)


# -- split --------------------------------------------------------------------------------------------


def test_split_removes_the_original_page_and_each_part_syncs_when_its_notes_complete(manual_env):
    env = manual_env
    trio(env.store)
    notes_for(env, "long")
    env.sync.enqueue_export("long", source="manual")
    env.run()
    month = env.month_page("September-2026 Standard")
    assert env.fake.toggles(month) == ["Sep 21 · Long one"]
    parts = sm.split_session(env.store, "long", [200])["parts"]
    ids = [p["session_id"] for p in parts]
    env.run()
    assert env.fake.toggles(month) == [] and not block_alive(env, "long")
    for pid in ids:
        assert env.sync.session_status(pid)["state"] == "none"
        notes_for(env, pid)
    env.run()
    assert all(env.sync.session_status(pid)["state"] == "copied" for pid in ids)
    assert len(env.fake.toggles(month)) == 2


def test_split_of_a_meeting_not_in_notion_stays_out_when_the_type_does_not_auto_copy(manual_env):
    env = manual_env
    trio(env.store)
    notes_for(env, "long")  # notes exist, never sent, auto off
    ids = [p["session_id"] for p in sm.split_session(env.store, "long", [200])["parts"]]
    for pid in ids:
        notes_for(env, pid)
    env.run()
    assert all(env.sync.session_status(pid)["state"] == "none" for pid in ids)
    assert env.fake.calls("POST", "/v1/pages") == []


def test_split_of_a_meeting_not_in_notion_follows_the_note_type_auto_copy(env):
    trio(env.store)  # the original has no notes, so it was never in Notion
    ids = [p["session_id"] for p in sm.split_session(env.store, "long", [200])["parts"]]
    env.run()
    for pid in ids:
        notes_for(env, pid)
    env.run()
    assert all(env.sync.session_status(pid)["state"] == "copied" for pid in ids)


def test_unsplit_removes_the_parts_and_sends_the_original_back(manual_env):
    env = manual_env
    trio(env.store)
    notes_for(env, "long")
    env.sync.enqueue_export("long", source="manual")
    env.run()
    ids = [p["session_id"] for p in sm.split_session(env.store, "long", [200])["parts"]]
    env.run()
    for pid in ids:
        notes_for(env, pid)
    env.run()
    month = env.month_page("September-2026 Standard")
    assert len(env.fake.toggles(month)) == 2
    sm.unsplit(env.store, "long")
    env.run()
    assert env.fake.toggles(month) == ["Sep 21 · Long one"]
    assert env.sync.session_status("long")["state"] == "copied"


# -- Notion trouble never fails the operation -------------------------------------------------------------


def test_combine_and_split_succeed_when_notion_is_not_connected(manual_env):
    env = manual_env
    trio(env.store)
    for sid in ("a", "long"):
        notes_for(env, sid)
        env.sync.enqueue_export(sid, source="manual")
    env.run()
    env.sync.tokens.clear()
    cid = sm.combine_sessions(env.store, ["a", "b"])["session_id"]
    parts = sm.split_session(env.store, "long", [200])["parts"]
    assert env.store.session_exists(cid) and len(parts) == 2
    env.run()  # the removal jobs fail without a connection: they are logged, not raised
    assert env.sync.pending_count() == 0


def test_a_failing_notion_listener_does_not_fail_combine(manual_env, monkeypatch):
    env = manual_env
    trio(env.store)
    notes_for(env, "a")
    env.sync.enqueue_export("a", source="manual")
    env.run()

    def boom(*a, **k):
        raise RuntimeError("notion exploded")

    monkeypatch.setattr(env.sync, "request_remove", boom)
    result = sm.combine_sessions(env.store, ["a", "b"])
    assert env.store.session_exists(result["session_id"])


def test_notion_api_errors_during_removal_retry_in_the_background(manual_env):
    env = manual_env
    trio(env.store)
    notes_for(env, "a")
    env.sync.enqueue_export("a", source="manual")
    env.run()
    env.fake.fail(500, times=10, when=lambda m, p: m == "DELETE")
    sm.combine_sessions(env.store, ["a", "b"])
    env.run()
    assert env.sync.pending_count() == 1  # queued again with a back-off, not dropped
    env.fake.rules.clear()
    env.clock["t"] += 10_000
    env.run()
    assert env.sync.pending_count() == 0 and not block_alive(env, "a")
