"""The server's side of the compatibility policy: the window arithmetic, the
lenient client header, HTTP 426 for recorders older than the window, and the
recorder registry. (The old-recorder behaviour itself is test_compat_contract.py.)"""

from __future__ import annotations

import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from meeting_notes import __version__, wire
from meeting_notes.server import compat
from meeting_notes.server.app import create_app

OLD = {compat.CLIENT_HEADER: "0.6.1; windows"}
NEW = {compat.CLIENT_HEADER: f"{__version__}; windows"}


# -- window arithmetic --------------------------------------------------------


def test_default_window_covers_every_shipped_release():
    assert compat.SUPPORTED_CLIENT_WINDOW == 5
    assert compat.RELEASES == sorted(compat.RELEASES, key=compat.version_key)
    assert compat.released_versions()[-1] == __version__ or compat.version_key(__version__) >= compat.version_key(
        compat.RELEASES[-1]
    )
    assert compat.min_client_version() == compat.supported_versions()[0]
    assert len(compat.supported_versions()) <= compat.SUPPORTED_CLIENT_WINDOW + 1


def test_current_version_is_appended_when_missing():
    assert compat.released_versions("9.9.9")[-1] == "9.9.9"
    assert compat.released_versions(compat.RELEASES[-1]).count(compat.RELEASES[-1]) == 1


def test_window_rolls_forward_as_releases_are_added(monkeypatch):
    monkeypatch.setattr(compat, "RELEASES", ["0.1.0", "0.2.0", "0.3.0", "0.4.0", "0.5.0", "0.6.0", "0.7.0"])
    assert compat.supported_versions("0.7.0") == ["0.2.0", "0.3.0", "0.4.0", "0.5.0", "0.6.0", "0.7.0"]
    assert compat.min_client_version("0.7.0") == "0.2.0"
    assert compat.is_too_old("0.1.0", "0.7.0") and not compat.is_too_old("0.2.0", "0.7.0")
    # A new release pushes the floor up by exactly one.
    assert compat.min_client_version("0.8.0") == "0.3.0"


def test_versions_compare_numerically_not_lexically():
    assert compat.version_key("0.10.0") > compat.version_key("0.9.0")
    assert compat.version_key("0.7.3rc1") == compat.version_key("0.7.3")
    assert compat.version_key("garbage") is None


@pytest.mark.parametrize(
    "value,expected",
    [
        ("0.7.4; windows", ("0.7.4", "windows")),
        ("0.7.4", ("0.7.4", "")),
        ("  v0.8.0rc1 ;Windows 11 (x64)", ("v0.8.0rc1", "Windows 11 (x64)")),
        ("0.7.4;", ("0.7.4", "")),
    ],
)
def test_client_header_parses_leniently(value, expected):
    info = compat.parse_client_header(value)
    assert (info.version, info.platform) == expected


@pytest.mark.parametrize("value", [None, "", ";", "windows", "abc; windows", "\x00"])
def test_unparseable_header_is_a_legacy_client(value):
    assert compat.parse_client_header(value) is None


def test_hostile_platform_text_is_sanitized():
    info = compat.parse_client_header("0.7.4; <script>alert(1)</script>" + "x" * 500)
    assert "<" not in info.platform and len(info.platform) <= 60


# -- HTTP behaviour -----------------------------------------------------------


class _Empty:
    def transcribe(self, wav_path, track):
        return []


@pytest.fixture()
def http(tmp_path, monkeypatch):
    monkeypatch.delenv("MEETING_NOTES_TOKEN", raising=False)
    app = create_app(
        transcriber_factory=lambda **_: _Empty(),
        data_root=str(tmp_path / "data"),
        media_root=str(tmp_path / "media"),
    )
    with TestClient(app) as client:
        client.app_store = app.state.store
        yield client


@pytest.fixture()
def narrow(monkeypatch):
    """Shrink the window to the current release plus one, so 0.6.1 is too old."""
    monkeypatch.setattr(compat, "SUPPORTED_CLIENT_WINDOW", 1)
    assert compat.is_too_old("0.6.1")


def _pipeline(http, sid, headers=None, state="pending", **extra):
    return http.put(
        wire.pipeline_path(sid),
        json={"state": state, "percent": 0, "bytes_received": 0, "bytes_total": 0, **extra},
        headers=headers or {},
    )


def _assert_426(response):
    assert response.status_code == 426, response.text
    body = response.json()
    assert "update the recorder" in body["detail"].lower()
    assert body["min_client_version"] == compat.min_client_version()


def test_health_and_manifest_publish_the_floor(http):
    health = http.get(wire.HEALTH).json()
    assert health["min_client_version"] == compat.min_client_version()
    assert health["version"] == __version__
    assert health["protocol"] == wire.PROTOCOL_VERSION  # existing fields untouched
    package = http.app_store.root / "client" / "MeetingNotes-Windows.zip"
    package.parent.mkdir(parents=True, exist_ok=True)
    package.write_bytes(b"zip")
    manifest = http.get("/install/client-manifest.json").json()
    assert manifest["min_client_version"] == compat.min_client_version()
    assert manifest["version"] == __version__ and manifest["installer"]["url"]


def test_missing_header_is_never_rejected_even_far_outside_the_window(http, narrow):
    assert _pipeline(http, "legacy-1").status_code == 200
    assert http.post(wire.track_upload_path("legacy-1", "mic"), content=b"\0\0" * 8).status_code == 200
    assert http.post(wire.track_upload_path("legacy-2", "mic"), content=b"\0\0" * 8).status_code == 200
    assert http.post(wire.finalize_path("legacy-2"), json={"meta": {}, "timing": {}}).status_code == 200


def test_garbage_header_is_treated_as_legacy(http, narrow):
    headers = {compat.CLIENT_HEADER: "not-a-version"}
    assert _pipeline(http, "garbage-1", headers).status_code == 200


def test_in_window_client_is_accepted(http, narrow):
    assert _pipeline(http, "new-1", NEW).status_code == 200


def test_too_old_client_is_told_to_update_when_starting_a_new_upload(http, narrow):
    _assert_426(_pipeline(http, "old-1", OLD))
    _assert_426(http.post(wire.track_upload_path("old-2", "mic"), content=b"\0\0" * 8, headers=OLD))
    upload = http.post(wire.upload_path(), files={"file": ("a.wav", b"RIFF", "audio/wav")}, headers=OLD)
    _assert_426(upload)
    # Nothing was created for any of them: no half-made meetings.
    store = http.app_store
    assert not store.session_exists("old-1") and not store.session_exists("old-2")
    assert http.get("/v1/sessions").json()["total"] == 0


def test_too_old_client_can_always_finish_what_is_already_on_the_server(http, narrow):
    """No audio is ever lost: an upload already begun (or made by a legacy
    recorder) is finished, polled and read by a recorder outside the window."""
    assert _pipeline(http, "begun-1").status_code == 200  # started before the window closed
    assert _pipeline(http, "begun-1", OLD, state="uploading", bytes_received=1, bytes_total=2).status_code == 200
    assert http.post(wire.track_upload_path("begun-1", "mic"), content=b"\0\0" * 8, headers=OLD).status_code == 200
    finalize = http.post(wire.finalize_path("begun-1"), json={"meta": {}, "timing": {}}, headers=OLD)
    assert finalize.status_code == 200 and finalize.json()["job_id"]
    job_id = finalize.json()["job_id"]
    assert http.get(wire.job_path(job_id), headers=OLD).status_code == 200
    assert http.get("/v1/sessions", headers=OLD).status_code == 200
    assert http.get("/v1/sessions/begun-1", headers=OLD).status_code == 200
    assert http.get(wire.HEALTH, headers=OLD).status_code == 200


def test_too_old_client_can_still_send_logs(http, narrow):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a.log", "x")
    response = http.post(
        "/v1/client-logs", files={"file": ("l.zip", buf.getvalue(), "application/zip")},
        data={"device": "old-box"}, headers=OLD,
    )
    assert response.status_code == 201


def test_too_old_live_stream_is_refused_with_a_permanent_close_and_no_session(http, narrow):
    with http.websocket_connect(wire.STREAM, headers=OLD) as ws:
        ws.send_json(wire.to_json(wire.Hello(session_id="old-live", name="x", device="old-box")))
        error = ws.receive_json()
        assert error["type"] == "error" and "update" in error["detail"].lower()
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
        assert closed.value.code == 4400  # the code every released streamer treats as "stop retrying"
    assert not http.app_store.session_exists("old-live")
    # An in-window (or header-less) recorder streams normally.
    for headers in (NEW, {}):
        with http.websocket_connect(wire.STREAM, headers=headers) as ws:
            ws.send_json(wire.to_json(wire.Hello(session_id=f"ok-live-{len(headers)}", device="box")))
            ws.send_bytes(wire.encode_audio_frame("mic", 0, b"\0\0" * 160))
            assert ws.receive_json()["type"] == "ack"


# -- registry + session meta --------------------------------------------------


def test_registry_lists_recorders_with_version_and_flags_outdated(http, narrow):
    _pipeline(http, "reg-1", NEW, device="laptop-a")  # in window
    _pipeline(http, "reg-2", None, device="legacy-box")  # header-less
    http.app.state.client_registry.touch(compat.ClientInfo("0.6.1", "windows"), address="10.9.9.9")  # old, no device
    items = {i["device"]: i for i in http.get("/v1/clients").json()["items"]}
    assert items["laptop-a"]["version"] == __version__ and items["laptop-a"]["platform"] == "windows"
    assert items["laptop-a"]["outdated"] is False
    assert items["legacy-box"]["version"] == "" and items["legacy-box"]["outdated"] is False
    body = http.get("/v1/clients").json()
    assert body["min_client_version"] == compat.min_client_version()
    assert any(i["outdated"] for i in body["items"])


def test_version_lands_in_session_meta_and_survives_finalize(http):
    _pipeline(http, "meta-1", NEW, device="laptop-a")
    http.post(wire.track_upload_path("meta-1", "mic"), content=b"\0\0" * 8, headers=NEW)
    assert http.post(wire.finalize_path("meta-1"), json={"meta": {"name": "n"}, "timing": {}}).status_code == 200
    meta = http.app_store.read_session_meta("meta-1")
    assert meta["client"] == {"version": __version__, "platform": "windows"}
    # Legacy recorders leave no client block at all.
    _pipeline(http, "meta-2")
    assert "client" not in http.app_store.read_session_meta("meta-2")


def test_registry_persists_across_restart(tmp_path):
    a = compat.ClientRegistry(tmp_path)
    a.touch(compat.ClientInfo("0.7.3", "windows"), address="10.0.0.5", device="laptop-a")
    b = compat.ClientRegistry(tmp_path)
    assert [r["device"] for r in b.list()] == ["laptop-a"]
    assert json.loads((tmp_path / "clients.json").read_text())["clients"]["laptop-a"]["version"] == "0.7.3"


def test_header_less_request_never_overwrites_a_known_version(tmp_path):
    reg = compat.ClientRegistry(tmp_path)
    reg.touch(compat.ClientInfo("0.7.3", "windows"), address="10.0.0.5", device="laptop-a")
    reg.touch(None, address="10.0.0.5", device="laptop-a")
    assert reg.list()[0]["version"] == "0.7.3"


def test_settings_page_has_connected_recorders_section(http):
    page = http.get("/settings").text
    assert 'id="settings-recorders-heading">Connected recorders<' in page
    assert 'href="#settings-recorders-heading"' in page and "/v1/clients" in page
