"""The AI login check: an expired provider login holds the notes queue instead of failing every meeting.

Covers the shared failure classifier (``ai_login.py``), the server's login health (``server/ai_health.py``: requeue
and hold on a login failure, the real test request clearing or setting it, ``/health``, the page banner and the
Settings endpoints) and the bridge's test request (``BridgeControl.verify``).
"""

from __future__ import annotations

import json
import subprocess

import pytest
from fastapi.testclient import TestClient

from meeting_notes import wire
from meeting_notes.ai_login import is_login_failure
from meeting_notes.bridge import BridgeConfig, BridgeControl, BridgeError, BridgeWorker
from meeting_notes.server import ai_health
from meeting_notes.server import settings as settings_mod
from meeting_notes.server.app import create_app

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated:DeprecationWarning"
)

TOKEN = "login-secret"
# What the Claude Code CLI really said on Mike's server when the login expired.
EXPIRED = "claude exited with status 1: Failed to authenticate: OAuth session expired and could not be refreshed"
EXPIRED_401 = ("claude exited with status 1: Failed to authenticate. API Error: 401 OAuth access token has expired. "
               "Re-authenticate to continue.")


def _headers():
    return {"Authorization": f"Bearer {TOKEN}"}


def test_login_failures_are_recognised_and_other_failures_are_not():
    assert is_login_failure(EXPIRED) and is_login_failure(EXPIRED_401)
    assert is_login_failure("codex exited with status 1: Not logged in. Please run codex login")
    for other in ("codex unavailable", "claude did not complete: timed out", "claude exited with status 1: overloaded",
                  "notes.summary must be a string", "", None, 401):
        assert not is_login_failure(other)


class _Verify:
    """A fake bridge test request: answers ``result`` (or ``None`` for no answer) and counts calls."""

    def __init__(self, result="ok"):
        self.result, self.calls = result, []

    def __call__(self, provider):
        self.calls.append(provider)
        return None if self.result is None else {"result": self.result, "detail": "x"}


def _app(tmp_path, monkeypatch, provider="claude", verify=None):
    monkeypatch.setenv("MEETING_NOTES_TOKEN", TOKEN)
    app = create_app(data_root=str(tmp_path / "data"), ai_login_verify=verify or _Verify())
    store = app.state.store
    settings_mod.save_settings(store.root, settings_mod.Settings(model="base.en", ai_provider=provider))
    for sid in ("s1", "s2"):
        store.write_session_meta(sid, {"name": sid})
        job_id = store.create_job(sid)
        store.update_job(job_id, state=wire.JobState.DONE, progress=1.0)
        store.write_transcript(job_id, "# " + sid, json.dumps({"segments": [{"text": "hello"}]}))
    return app


def test_a_login_failure_requeues_the_meeting_and_holds_the_queue_until_the_login_works(tmp_path, monkeypatch):
    verify = _Verify("login_failed")
    app = _app(tmp_path, monkeypatch, verify=verify)
    client, store = TestClient(app), app.state.store
    first = client.post("/v1/sessions/s1/review", headers=_headers()).json()
    second = client.post("/v1/sessions/s2/review", headers=_headers()).json()
    claim = client.get("/v1/bridge/review/claim", headers=_headers()).json()
    failed = client.post(f"/v1/bridge/review/{claim['id']}/failure", headers=_headers(), json={"error": EXPIRED})
    assert failed.status_code == 200 and failed.json()["status"] == "queued"  # not "error": it waits
    assert store.read_review(first["review_id"])["status"] == "queued"
    assert store.read_review(second["review_id"])["status"] == "queued"
    # Nothing is handed out while the login is broken, so no other meeting fails on it.
    assert client.get("/v1/bridge/review/claim", headers=_headers()).status_code == 204
    health = client.get("/health").json()
    assert health["ai_login"] == {"provider": "claude", "expired": True}
    page = client.get("/settings", headers=_headers()).text
    assert "ai-login-banner" in page and "Claude login for meeting notes has expired" in page
    assert 'href="/settings#notes"' in page
    login = client.get("/v1/ai/login", headers=_headers()).json()
    assert login["expired"] and login["applies"] and "OAuth session expired" in login["error"]
    # The real test request still fails: still held. Then the login is fixed and the check goes through.
    assert client.post("/v1/ai/login/check", headers=_headers()).json()["expired"] is True
    verify.result = "ok"
    checked = client.post("/v1/ai/login/check", headers=_headers()).json()
    assert checked["expired"] is False and checked["last_result"] == "ok" and checked["answer"] == "ok"
    assert verify.calls == ["claude", "claude"]
    assert "ai-login-banner" not in client.get("/settings", headers=_headers()).text
    assert client.get("/health").json()["ai_login"]["expired"] is False
    assert client.get("/v1/bridge/review/claim", headers=_headers()).status_code == 200


def test_an_unanswered_or_inconclusive_check_changes_nothing(tmp_path, monkeypatch):
    verify = _Verify(None)
    app = _app(tmp_path, monkeypatch, verify=verify)
    root = app.state.store.root
    ai_health.mark_expired(root, "claude", EXPIRED)
    client = TestClient(app)
    assert client.post("/v1/ai/login/check", headers=_headers()).json()["answer"] == "unavailable"
    assert ai_health.is_expired(root, "claude")
    verify.result = "error"  # e.g. no network: the login may be fine, but it is not proven
    client.post("/v1/ai/login/check", headers=_headers())
    assert ai_health.is_expired(root, "claude") and ai_health.state(root, "claude")["result"] == "error"


def test_a_completed_job_clears_an_expired_login(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    client, root = TestClient(app), app.state.store.root
    client.post("/v1/sessions/s1/review", headers=_headers())
    claim = client.get("/v1/bridge/review/claim", headers=_headers()).json()
    ai_health.mark_expired(root, "claude", EXPIRED)  # e.g. a parallel job failed meanwhile
    notes = {
        "title": "t", "summary": "s", "meeting_notes": "m", "participants": [], "key_points": [], "decisions": [],
        "action_items": [], "open_questions": [], "risks": [], "next_steps": [],
    }
    done = client.post(f"/v1/bridge/review/{claim['id']}/complete", headers=_headers(), json={"notes": notes})
    assert done.status_code == 200 and done.json()["status"] == "done"
    assert not ai_health.is_expired(root, "claude")


@pytest.mark.parametrize("provider", ["ollama", "disabled"])
def test_a_provider_without_a_login_fails_the_job_as_before(tmp_path, monkeypatch, provider):
    app = _app(tmp_path, monkeypatch, provider="claude")
    client, store = TestClient(app), app.state.store
    review = client.post("/v1/sessions/s1/review", headers=_headers()).json()
    claim = client.get("/v1/bridge/review/claim", headers=_headers()).json()
    settings_mod.save_settings(store.root, settings_mod.Settings(model="base.en", ai_provider=provider))
    failed = client.post(f"/v1/bridge/review/{claim['id']}/failure", headers=_headers(), json={"error": EXPIRED_401})
    assert failed.json()["status"] == "error" and store.read_review(review["review_id"])["status"] == "error"
    assert not ai_health.is_expired(store.root, provider)
    assert client.get("/health").json()["ai_login"]["expired"] is False
    login = client.get("/v1/ai/login", headers=_headers()).json()
    assert login["applies"] is False and login["expired"] is False


def test_an_ordinary_failure_still_fails_and_the_login_stays_fine(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    client = TestClient(app)
    client.post("/v1/sessions/s1/review", headers=_headers())
    claim = client.get("/v1/bridge/review/claim", headers=_headers()).json()
    failed = client.post(f"/v1/bridge/review/{claim['id']}/failure", headers=_headers(), json={"error": "overloaded"})
    assert failed.json()["status"] == "error"
    assert not ai_health.is_expired(app.state.store.root, "claude")


def test_the_login_state_belongs_to_one_provider_and_survives_a_restart(tmp_path):
    root = tmp_path
    ai_health.mark_expired(root, "claude", EXPIRED)
    assert ai_health.is_expired(root, "claude") and not ai_health.is_expired(root, "codex")
    assert ai_health.state(root, "claude")["since"]  # read back from ai_health.json
    assert ai_health.banner_state(root, "claude")["name"] == "Claude"
    assert ai_health.banner_state(root, "codex") is None
    ai_health.record_check(root, "claude", "ok")
    assert not ai_health.is_expired(root, "claude") and ai_health.state(root, "claude")["result"] == "ok"
    ai_health.record_check(root, "claude", "login_failed", "401")
    assert ai_health.is_expired(root, "claude")
    (root / ai_health.FILE_NAME).write_text("not json", encoding="utf-8")
    assert not ai_health.is_expired(root, "claude")  # a broken file is "unknown", never a held queue


def test_the_checker_asks_only_for_providers_with_a_login(tmp_path):
    provider = ["ollama"]
    verify = _Verify("login_failed")
    checker = ai_health.LoginChecker(tmp_path, lambda: provider[0], verify, enabled=False)
    assert checker.check_now() is None and verify.calls == []
    provider[0] = "codex"
    assert checker.check_now()["result"] == "login_failed"
    assert ai_health.is_expired(tmp_path, "codex")
    assert checker._next_wait() == checker.expired_interval
    ai_health.mark_ok(tmp_path, "codex")
    assert checker._next_wait() == checker.interval


def test_a_split_job_that_fails_on_the_login_marks_it_expired(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    split_ai = None
    for route in app.routes:
        if getattr(route, "path", "") == "/v1/bridge/split-suggestions/{job_id}/failure":
            split_ai = route
    assert split_ai is not None
    app.state.ai_login_failed(EXPIRED)  # what the split failure route calls for a login failure
    assert ai_health.is_expired(app.state.store.root, "claude")


# -- the bridge's test request --------------------------------------------------


class _Done:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _verify_with(monkeypatch, provider, done, seen=None):
    def fake_run(command, **kwargs):
        if seen is not None:
            seen.update(command=command, **kwargs)
        return done

    monkeypatch.setattr("meeting_notes.bridge.subprocess.run", fake_run)
    return BridgeControl(BridgeConfig("http://server", provider=provider)).verify(provider)


def test_bridge_verify_reports_ok_for_a_working_claude_login(monkeypatch):
    seen = {}
    answer = _verify_with(monkeypatch, "claude", _Done(0, json.dumps({"is_error": False, "result": "OK"})), seen)
    assert answer["result"] == "ok" and answer["detail"] is None
    command = seen["command"]
    assert command[command.index("--model") + 1] == "haiku"  # the cheapest request there is
    assert "--no-session-persistence" in command and seen["timeout"] <= 120
    assert "ANTHROPIC_API_KEY" not in seen["env"]


def test_bridge_verify_recognises_an_expired_claude_login(monkeypatch):
    body = json.dumps({"is_error": True, "result": "Failed to authenticate. API Error: 401 OAuth access token has expired."})
    answer = _verify_with(monkeypatch, "claude", _Done(1, body))
    assert answer["result"] == "login_failed" and "OAuth" in answer["detail"]


def test_bridge_verify_keeps_other_errors_apart(monkeypatch):
    assert _verify_with(monkeypatch, "claude", _Done(1, "", "Network unreachable"))["result"] == "error"

    def timeout(*_a, **_k):
        raise subprocess.TimeoutExpired("claude", 120)

    monkeypatch.setattr("meeting_notes.bridge.subprocess.run", timeout)
    assert BridgeControl(BridgeConfig("http://server", provider="claude")).verify("claude")["result"] == "error"


def test_bridge_verify_for_codex_and_ollama(monkeypatch):
    seen = {}
    assert _verify_with(monkeypatch, "codex", _Done(0, "OK"), seen)["result"] == "ok"
    assert seen["command"][:2] == ["codex", "exec"] and "read-only" in seen["command"]
    assert _verify_with(monkeypatch, "codex", _Done(1, "", "Error: Not logged in"))["result"] == "login_failed"
    assert _verify_with(monkeypatch, "ollama", _Done(1))["result"] == "ok"  # nothing to log in to


def test_a_codex_failure_now_says_why(monkeypatch, tmp_path):
    worker = BridgeWorker(BridgeConfig("http://server"))
    paths = [tmp_path / n for n in ("t.txt", "w.md", "o.json", "s.json")]
    for path in paths:
        path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "meeting_notes.bridge.subprocess.run",
        lambda command, **kwargs: _Done(1, None, "Error: 401 Unauthorized: your refresh token has expired"),
    )
    with pytest.raises(BridgeError) as raised:
        worker._run_codex(paths[0], paths[1], paths[2], paths[3], {})
    assert is_login_failure(str(raised.value))
