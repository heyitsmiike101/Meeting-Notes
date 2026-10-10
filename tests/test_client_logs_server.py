"""Server side of the client's Logs window: ``/v1/client-logs`` upload, list, download."""

from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from meeting_notes.server import client_logs
from meeting_notes.server.app import create_app

pytestmark = pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated:DeprecationWarning"
)

TOKEN = "web-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _zip_bytes(name="client.log", text="hello") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr(name, text)
    return buf.getvalue()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_NOTES_TOKEN", TOKEN)
    app = create_app(data_root=str(tmp_path / "data"), enable_mcp=False)
    return TestClient(app)


def _upload(client, data=None, device="LAPTOP-1", headers=AUTH, filename="logs.zip"):
    return client.post(
        "/v1/client-logs",
        files={"file": (filename, data if data is not None else _zip_bytes(), "application/zip")},
        data={"device": device},
        headers=headers,
    )


def test_upload_stores_the_zip_and_returns_the_record(client, tmp_path):
    data = _zip_bytes(text="log line")
    response = _upload(client, data)
    assert response.status_code == 201
    body = response.json()
    assert body["device"] == "LAPTOP-1" and body["size"] == len(data)
    assert body["id"] and body["received_at"] > 0
    stored = list((tmp_path / "data" / "client-logs" / "LAPTOP-1").glob("*.zip"))
    assert len(stored) == 1 and stored[0].read_bytes() == data
    assert stored[0].stem == body["id"]


def test_upload_requires_the_client_token(client):
    assert _upload(client, headers={}).status_code == 401
    assert _upload(client, headers={"Authorization": "Bearer nope"}).status_code == 403


def test_upload_rejects_non_zip_and_empty_and_oversized(client, monkeypatch):
    assert _upload(client, b"just some text, not a zip").status_code == 400
    assert _upload(client, b"").status_code in (400,)
    # Right magic bytes, but not a real archive.
    assert _upload(client, b"PK\x03\x04" + b"\x00" * 30).status_code == 400
    monkeypatch.setattr(client_logs, "MAX_BYTES", 1024)
    big = _zip_bytes(text="x" * 5000)
    monkeypatch.setattr("meeting_notes.server.app._CLIENT_LOG_MAX", 1024)
    assert _upload(client, big).status_code == 413


def test_upload_needs_the_file_field(client):
    response = client.post("/v1/client-logs", data={"device": "PC"}, files={"other": ("a.zip", _zip_bytes())}, headers=AUTH)
    assert response.status_code == 400


def test_device_name_is_sanitized(client, tmp_path):
    for hostile in ("../../etc", "a/b\\c", "..", "", "CON", "  spaced name  "):
        response = _upload(client, device=hostile)
        assert response.status_code == 201, hostile
        device = response.json()["device"]
        assert "/" not in device and "\\" not in device and not device.startswith(".")
        assert (tmp_path / "data" / "client-logs" / device).is_dir()
    root = tmp_path / "data" / "client-logs"
    assert not (tmp_path / "data" / "etc").exists()
    assert {p.parent for p in root.rglob("*.zip")} <= {p for p in root.iterdir()}


def test_only_the_newest_twenty_bundles_per_device_are_kept(client, tmp_path):
    folder = tmp_path / "data" / "client-logs" / "PC"
    folder.mkdir(parents=True)
    for index in range(client_logs.KEEP_PER_DEVICE):
        (folder / f"2026010{index % 9 + 1}T0000{index:02d}Z-{index:08x}.zip").write_bytes(_zip_bytes())
    oldest = sorted(folder.glob("*.zip"))[0]
    other = _upload(client, device="OTHER").json()
    newest = _upload(client, device="PC").json()
    names = {p.name for p in folder.glob("*.zip")}
    assert len(names) == client_logs.KEEP_PER_DEVICE
    assert oldest.name not in names and f"{newest['id']}.zip" in names
    assert len(list((tmp_path / "data" / "client-logs" / "OTHER").glob("*.zip"))) == 1 and other["device"] == "OTHER"


def test_list_is_newest_first_and_needs_auth(client):
    first = _upload(client, device="A").json()
    second = _upload(client, device="B").json()
    assert client.get("/v1/client-logs").status_code == 401
    items = client.get("/v1/client-logs", headers=AUTH).json()["items"]
    assert [i["device"] for i in items] in (["B", "A"], ["A", "B"])
    ids = {i["id"] for i in items}
    assert {first["id"], second["id"]} == ids
    assert all(i["url"].startswith("/v1/client-logs/") and i["size"] > 0 for i in items)


def test_download_returns_the_zip_and_is_traversal_safe(client, tmp_path):
    data = _zip_bytes(text="payload")
    body = _upload(client, data).json()
    name = f"{body['id']}.zip"
    response = client.get(f"/v1/client-logs/LAPTOP-1/{name}", headers=AUTH)
    assert response.status_code == 200 and response.content == data
    assert response.headers["content-type"] == "application/zip"
    assert client.get(f"/v1/client-logs/LAPTOP-1/{name}").status_code == 401
    (tmp_path / "data" / "secret.zip").write_bytes(_zip_bytes())
    for device, file in (
        ("..", "secret.zip"),
        ("LAPTOP-1", "..%2F..%2Fsecret.zip"),
        ("LAPTOP-1", "..%5Csecret.zip"),
        ("%2E%2E", name),
        ("LAPTOP-1", "notes.txt"),
        ("NOBODY", name),
    ):
        assert client.get(f"/v1/client-logs/{device}/{file}", headers=AUTH).status_code == 404, (device, file)


def test_the_web_login_cookie_can_list_and_download(client):
    body = _upload(client).json()
    client.cookies.set("meeting_notes_token", TOKEN)
    assert client.get("/v1/client-logs").status_code == 200
    assert client.get(f"/v1/client-logs/LAPTOP-1/{body['id']}.zip").status_code == 200


def test_helpers():
    assert client_logs.sanitize_device("My PC (work)") == "My-PC-work"
    assert client_logs.sanitize_device("...") == "unknown"
    assert client_logs.sanitize_device("nul") == "_nul"
    assert client_logs.looks_like_zip(b"PK\x03\x04rest") and not client_logs.looks_like_zip(b"MZ")


def test_delete_one_bundle_and_traversal_is_refused(client, tmp_path):
    keep = _upload(client, device="A").json()
    gone = _upload(client, device="B").json()
    assert client.delete(f"/v1/client-logs/B/{gone['name']}").status_code == 401
    assert client.delete(f"/v1/client-logs/B/{gone['name']}", headers=AUTH).json() == {"deleted": 1}
    assert client.delete(f"/v1/client-logs/B/{gone['name']}", headers=AUTH).status_code == 404
    assert [i["id"] for i in client.get("/v1/client-logs", headers=AUTH).json()["items"]] == [keep["id"]]
    assert not (tmp_path / "data" / "client-logs" / "B").exists()  # empty computer folder removed
    (tmp_path / "data" / "secret.zip").write_bytes(_zip_bytes())
    for device, file in (("..", "secret.zip"), ("A", "..%2F..%2Fsecret.zip"), ("A", "notes.txt")):
        assert client.delete(f"/v1/client-logs/{device}/{file}", headers=AUTH).status_code == 404
    assert (tmp_path / "data" / "secret.zip").exists()


def test_delete_all_bundles(client):
    for device in ("A", "A", "B"):
        _upload(client, device=device)
    assert client.delete("/v1/client-logs").status_code == 401
    assert client.delete("/v1/client-logs", headers=AUTH).json() == {"deleted": 3}
    assert client.get("/v1/client-logs", headers=AUTH).json()["items"] == []
    assert client.delete("/v1/client-logs", headers=AUTH).json() == {"deleted": 0}


def test_settings_page_offers_log_deletion_and_keeps_place_on_save(client):
    page = client.get("/settings", headers=AUTH).text
    assert 'id="logs-delete-all"' in page and "data-log-delete" in page
    assert "mn-settings-place" in page
