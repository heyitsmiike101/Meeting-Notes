"""Update check: version comparison, caching, failure handling, endpoint and UI notice."""

import threading

import pytest
from fastapi.testclient import TestClient

from meeting_notes.server import updates
from meeting_notes.server.app import create_app

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated:DeprecationWarning"
)


def src(version):
    return f'"""pkg."""\n\n__version__ = "{version}"\n'


def make_checker(version, current="0.7.8", **kw):
    calls = []

    def fetch():
        calls.append(1)
        if isinstance(version, Exception):
            raise version
        return version

    return updates.UpdateChecker(fetch, current=current, enabled=kw.pop("enabled", True), **kw), calls


def test_numeric_ordering():
    assert updates.is_newer("0.7.10", "0.7.9")
    assert not updates.is_newer("0.7.9", "0.7.10")
    assert not updates.is_newer("0.7.8", "0.7.8")
    assert updates.is_newer("0.8", "0.7.9")
    assert updates.is_newer("1.0.0", "0.9.9")
    assert not updates.is_newer("garbage", "0.7.8")
    assert updates.extract_version(src("0.7.9")) == "0.7.9"
    assert updates.extract_version("nothing here") is None


def test_status_unknown_before_first_check():
    checker, _ = make_checker(src("9.9.9"))
    status = checker.status()
    assert status["latest"] is None and status["update_available"] is False
    assert status["current"] == "0.7.8" and status["repo_url"] == updates.REPO_URL


def test_check_now_sets_status():
    checker, _ = make_checker(src("0.7.9"), clock=lambda: 123.0)
    checker.check_now()
    status = checker.status()
    assert status["latest"] == "0.7.9" and status["update_available"] is True
    assert status["checked_at"] == 123.0


def test_same_version_not_available():
    checker, _ = make_checker(src("0.7.8"))
    checker.check_now()
    assert checker.status()["update_available"] is False


@pytest.mark.parametrize("bad", [OSError("offline"), "no version", src("x.y")])
def test_failures_mean_unknown_and_keep_previous(bad):
    checker, _ = make_checker(bad)
    checker.check_now()  # must not raise
    assert checker.status()["latest"] is None
    good, _ = make_checker(src("0.7.9"))
    good.check_now()
    good._fetch = lambda: (_ for _ in ()).throw(OSError("down"))
    good.check_now()
    assert good.status()["latest"] == "0.7.9"


def test_disabled_never_fetches(monkeypatch):
    checker, calls = make_checker(src("9.9.9"), enabled=False)
    checker.check_now()
    checker.start()
    assert calls == [] and checker.status()["update_available"] is False
    monkeypatch.setenv("MEETING_NOTES_UPDATE_CHECK", "0")
    assert updates.UpdateChecker(lambda: 1 / 0).enabled is False
    monkeypatch.delenv("MEETING_NOTES_UPDATE_CHECK")
    assert updates.UpdateChecker(lambda: "").enabled is True


def test_background_thread_checks_at_start_then_on_interval():
    first = threading.Event()
    calls = []

    def fetch():
        calls.append(1)
        if len(calls) >= 3:
            first.set()
        return src("0.7.9")

    checker = updates.UpdateChecker(fetch, current="0.7.8", interval=0.01, enabled=True)
    checker.start()
    assert first.wait(3)
    checker.stop()
    assert len(calls) >= 3 and checker.status()["update_available"]


def test_default_interval_is_six_hours():
    assert updates.CHECK_INTERVAL_SECONDS == 6 * 3600


def app_with(tmp_path, version):
    checker, calls = make_checker(src(version))
    checker.check_now()
    return create_app(data_root=str(tmp_path / "data"), enable_mcp=False, update_checker=checker), calls


def test_endpoint_and_ui_notice(tmp_path):
    app, _ = app_with(tmp_path, "0.7.9")
    client = TestClient(app)
    body = client.get("/v1/update-status").json()
    assert set(body) == {"current", "latest", "update_available", "checked_at", "repo_url"}
    assert body["update_available"] is True and body["latest"] == "0.7.9"
    home = client.get("/meetings").text
    assert "Update available · v0.7.9" in home and updates.CHANGELOG_URL in home
    assert 'class="banner update"' in client.get("/settings").text


def test_ui_hides_notice_when_current(tmp_path):
    app, _ = app_with(tmp_path, "0.7.8")
    client = TestClient(app)
    assert client.get("/v1/update-status").json()["update_available"] is False
    assert "Update available" not in client.get("/meetings").text
    assert "banner update" not in client.get("/settings").text


def test_default_app_under_pytest_is_disabled_and_silent(tmp_path):
    app = create_app(data_root=str(tmp_path / "data"), enable_mcp=False)
    client = TestClient(app)
    status = client.get("/v1/update-status").json()
    assert status["latest"] is None and status["update_available"] is False
    assert "Update available" not in client.get("/meetings").text
