"""Contract tests: every supported released recorder vs the CURRENT server.

Runs the real, frozen network code of each release under ``clients/`` (parametrized
by the ``client`` fixture) over real HTTP and websockets against the current
server (``compat_server``). If one of these fails, the server change broke a
recorder that is still in the field: fix the server, not the fixture.
"""

from __future__ import annotations

import hashlib
import io
import time
import uuid
import zipfile

import httpx
import pytest

import compat_support as support


def _sid(client, label: str) -> str:
    return f"compat-{client.version.replace('.', '')}-{label}-{uuid.uuid4().hex[:8]}"


def _wait(predicate, timeout=10.0, step=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    return predicate()


def _api(client, server, token="ok"):
    return client.api.ServerClient(server.base_url, server.token if token == "ok" else token, timeout=15.0)


# ---------------------------------------------------------------------------
# What this recorder sends, and the manifest/health it reads
# ---------------------------------------------------------------------------


def test_frozen_client_predates_version_header(client):
    """Premise of the leniency rule: no shipped recorder sends the client header,
    so a missing header must always mean 'legacy, allow'."""
    for mod in (client.api, client.streamer, client.update, client.queue):
        source = open(mod.__file__, encoding="utf-8").read()
        assert "X-Meeting-Notes-Client" not in source, mod.__name__


def test_health_probe(client, compat_server):
    with _api(client, compat_server) as api:
        health = api.health()
    assert health["status"] == "ok"
    assert health["protocol"] == client.wire.PROTOCOL_VERSION


def test_manifest_parses_and_update_downloads(client, compat_server, tmp_path):
    """The update path: old ``UpdateManifest.from_json`` must accept the current
    manifest (unknown fields such as ``min_client_version`` ignored), and the
    installer it points at must verify."""
    raw = httpx.get(compat_server.base_url + "/install/client-manifest.json").json()
    assert "min_client_version" in raw  # new optional field is present...
    manifest = client.update.UpdateManifest.from_json(raw, compat_server.base_url)  # ...and ignored
    assert manifest.version == raw["version"]
    assert manifest.size > 0 and len(manifest.sha256) == 64

    updater = client.update.ClientUpdater(compat_server.base_url, compat_server.token, current_version="0.0.1")
    found = updater.check()
    assert found is not None and found.version == raw["version"]
    path = updater.download(found, tmp_path / "Install.ps1")
    assert path.stat().st_size == found.size

    # An up-to-date recorder sees no update, and never errors on the manifest.
    current = client.update.ClientUpdater(
        compat_server.base_url, compat_server.token, current_version=raw["version"]
    )
    assert current.check() is None


def test_macos_manifest_and_installer(client, compat_server, tmp_path, monkeypatch):
    """The macOS update path (0.7.5+ recorders on a Mac): the mac manifest is served
    with the same version and window, its package and installer script verify, and
    a recorder pointed at it sees the update."""
    if not hasattr(client.update, "MANIFEST_PATH_MACOS"):
        pytest.skip("recorder predates the macOS client")
    base = compat_server.base_url
    raw = httpx.get(base + client.update.MANIFEST_PATH_MACOS).json()
    assert raw["min_client_version"]
    assert raw["sha256"] == compat_server.mac_package_sha
    assert raw["url"].endswith("/install/MeetingNotes-macOS.zip")
    manifest = client.update.UpdateManifest.from_json(raw, base)
    assert manifest.version == raw["version"] and manifest.size > 0 and len(manifest.sha256) == 64

    script = httpx.get(raw["installer"]["url"])
    assert script.status_code == 200
    assert hashlib.sha256(script.content).hexdigest() == raw["installer"]["sha256"]
    assert script.text.startswith("#!")

    monkeypatch.setattr(client.update, "manifest_path", lambda platform=None: client.update.MANIFEST_PATH_MACOS)
    updater = client.update.ClientUpdater(base, compat_server.token, current_version="0.0.1")
    found = updater.check()
    assert found is not None and found.version == raw["version"]
    path = updater.download(found, tmp_path / "MeetingNotes-macOS.zip")
    assert path.stat().st_size == found.size
    current = client.update.ClientUpdater(base, compat_server.token, current_version=raw["version"])
    assert current.check() is None


# ---------------------------------------------------------------------------
# Live stream (websocket)
# ---------------------------------------------------------------------------


def test_live_stream_hello_audio_ack_and_close(client, compat_server):
    sid = _sid(client, "live")
    streamer = client.streamer.LiveStreamer(compat_server.base_url, compat_server.token, buffer_seconds=5.0)
    chunk = support.pcm_tone(0.5)
    streamer.start(sid, "Compat live", time.time())
    try:
        for _ in range(4):
            streamer.submit("mic", chunk)
            streamer.submit("system", chunk)
        assert _wait(lambda: streamer.state == "connected"), streamer.last_error
        # Everything acked: the recorder's unacked buffers drain to empty.
        assert _wait(
            lambda: all(len(streamer._buffers.get(t, [])) == 0 for t in ("mic", "system"))
        ), "server never acknowledged the streamed audio"
        assert streamer.permanent_error is None
        live = httpx.get(
            compat_server.base_url + "/v1/live", headers={"Authorization": f"Bearer {compat_server.token}"}
        ).json()
        assert sid in {s["session_id"] for s in live.get("items", live.get("sessions", []))}
    finally:
        streamer.stop(join_timeout=3.0)
    raw = compat_server.store.track_raw_path(sid, "mic")
    assert _wait(lambda: raw.exists() and raw.stat().st_size == len(chunk) * 4)


def test_live_stream_bad_token_is_permanent_rejection(client, compat_server):
    streamer = client.streamer.LiveStreamer(compat_server.base_url, "wrong-token")
    streamer.start(_sid(client, "badtoken"), "Bad token", time.time())
    try:
        assert _wait(lambda: streamer.state == "rejected")
        assert "unauthorized" in (streamer.permanent_error or "").lower()
    finally:
        streamer.stop(join_timeout=2.0)


def test_live_stream_invalid_session_id_is_permanent_rejection(client, compat_server):
    streamer = client.streamer.LiveStreamer(compat_server.base_url, compat_server.token)
    streamer.start("../not a safe id", "Bad id", time.time())
    try:
        assert _wait(lambda: streamer.state == "rejected")
    finally:
        streamer.stop(join_timeout=2.0)


# ---------------------------------------------------------------------------
# Pipeline PUT -> track uploads -> finalize -> job polling
# ---------------------------------------------------------------------------


def test_pipeline_track_upload_finalize_and_job(client, compat_server, tmp_path):
    sid = _sid(client, "manual")
    pcm = support.pcm_tone(0.4)
    frames = len(pcm) // 2
    pcm_file = tmp_path / "t.pcm"
    pcm_file.write_bytes(pcm)
    with _api(client, compat_server) as api:
        # The exact payload shape the queue worker reports (see queue.py report()).
        pipe = api.report_upload_status(
            sid, {"state": "pending", "percent": 0, "bytes_received": 0, "bytes_total": 0,
                  "name": "Compat manual", "device": "compat-box"}
        )
        assert pipe["session_id"] == sid
        api.report_upload_status(
            sid, {"state": "uploading", "percent": 50, "bytes_received": 10, "bytes_total": 20}
        )
        for track in ("mic", "system"):
            seen = []
            body = api.upload_track(sid, track, pcm_file, frames, progress_callback=lambda a, b: seen.append(a))
            assert body["frames"] == frames and body["track"] == track
            assert seen and seen[-1] == len(pcm)
        job_id = api.finalize(
            sid,
            {"name": "Compat manual", "device": "compat-box", "created": "2026-01-01T00:00:00"},
            {"mic": [{"event": "open", "segment": 0, "frames": 0, "t": 0.0, "wall": 0.0,
                      "samplerate": 48000, "channels": 1, "device": "t"},
                     {"event": "close", "segment": 0, "frames": 6400, "t": 0.4}],
             "system": [{"event": "open", "segment": 0, "frames": 0, "t": 0.0, "wall": 0.0,
                         "samplerate": 48000, "channels": 1, "device": "t"},
                        {"event": "close", "segment": 0, "frames": 6400, "t": 0.4}]},
        )
        assert isinstance(job_id, str) and job_id
        # Finalize is idempotent for a retried request.
        assert api.finalize(sid, {"name": "Compat manual"}, {}) == job_id

        def done():
            job = api.job(job_id)
            return job if job["state"] in (client.wire.JobState.DONE, client.wire.JobState.ERROR) else None

        job = _wait(done, timeout=20)
        assert job and job["state"] == client.wire.JobState.DONE, job
        transcript = api.transcript(job_id)
        assert "hello from mic" in transcript["markdown"]
        assert isinstance(transcript["json"], (str, dict))


def test_upload_track_validation_errors_are_plain_http(client, compat_server, tmp_path):
    """Bad track / bad id / bad X-Frames surface as HTTPStatusError (a real
    error), never as 'server unavailable' -- the queue treats them differently."""
    pcm_file = tmp_path / "t.pcm"
    pcm_file.write_bytes(support.pcm_tone(0.1))
    with _api(client, compat_server) as api:
        with pytest.raises(httpx.HTTPStatusError) as exc:
            api.upload_track(_sid(client, "badframes"), "mic", pcm_file, 999999)
        assert exc.value.response.status_code == 400
        with pytest.raises(httpx.HTTPStatusError) as exc:
            api.upload_track(_sid(client, "badtrack"), "nope", pcm_file, 1)
        assert exc.value.response.status_code == 400


# ---------------------------------------------------------------------------
# The offline queue worker, end to end
# ---------------------------------------------------------------------------


def test_upload_queue_worker_end_to_end(client, compat_server, tmp_path):
    session_dir = support.make_session_dir(tmp_path, f"queued-{client.version}")
    queue = client.queue.SessionQueue(tmp_path / ".upload-queue")
    queue.enqueue(session_dir)
    progress = []
    worker = client.queue.UploadWorker(
        queue, compat_server.base_url, compat_server.token, poll_interval=0.01,
        on_progress=lambda state: progress.append(dict(state)),
    )
    worker.run_once()
    assert queue.pending() == [], "the queue entry was not cleared"
    md = (session_dir / "transcript.md").read_text(encoding="utf-8")
    assert "hello from mic" in md and "hello from system" in md
    assert (session_dir / "transcript.json").exists()
    assert progress, "no progress callbacks"
    # The meeting is now in the server's history with a complete upload.
    with _api(client, compat_server) as api:
        listing = api.list_sessions(per_page=200)
    row = next(i for i in listing["items"] if i["name"] == session_dir.name or session_dir.name in str(i.get("name")))
    assert row["latest_state"] == "done"


def test_queue_with_bad_token_keeps_recording_and_stays_pending(client, compat_server, tmp_path):
    session_dir = support.make_session_dir(tmp_path, f"badauth-{client.version}")
    queue = client.queue.SessionQueue(tmp_path / ".upload-queue")
    queue.enqueue(session_dir)
    worker = client.queue.UploadWorker(
        queue, compat_server.base_url, "wrong-token", poll_interval=0.01, initial_backoff=0.01
    )
    worker.run_once()
    assert len(queue.pending()) == 1
    assert (session_dir / "mic.wav").exists() and (session_dir / "system.wav").exists()
    assert not (session_dir / "transcript.md").exists()


def test_queue_with_server_down_keeps_entry(client, tmp_path):
    session_dir = support.make_session_dir(tmp_path, f"down-{client.version}")
    queue = client.queue.SessionQueue(tmp_path / ".upload-queue")
    queue.enqueue(session_dir)
    worker = client.queue.UploadWorker(
        queue, f"http://127.0.0.1:{support.free_port()}", "x", poll_interval=0.01, initial_backoff=0.01
    )
    worker.run_once()
    assert len(queue.pending()) == 1


# ---------------------------------------------------------------------------
# History dialog / retention / connection check
# ---------------------------------------------------------------------------


def _upload_and_finish(client, server, tmp_path, label):
    session_dir = support.make_session_dir(tmp_path, f"{label}-{client.version}")
    queue = client.queue.SessionQueue(tmp_path / ".upload-queue")
    queue.enqueue(session_dir)
    client.queue.UploadWorker(queue, server.base_url, server.token, poll_interval=0.01).run_once()
    assert queue.pending() == []
    return session_dir


def test_history_dialog_contract(client, compat_server, tmp_path):
    """The keys the recorder's History window and local-retention check read."""
    _upload_and_finish(client, compat_server, tmp_path, "history")
    with _api(client, compat_server) as api:
        listing = api.list_sessions(q="hello", per_page=200)
        assert isinstance(listing["items"], list) and int(listing["total"]) >= 1
        item = listing["items"][0]
        assert item["session_id"] and "created" in item and item["latest_state"]
        assert api.list_sessions(state="done", per_page=5)["items"]
        assert api.list_sessions(page=1, per_page=1)["items"]

        detail = api.session_detail(item["session_id"])
        assert detail["session_id"] == item["session_id"]
        assert isinstance(detail["meta"], dict)
        assert detail["jobs"] and detail["jobs"][0]["state"] == "done"
        assert isinstance(detail["markdown"], str) and detail["markdown"]
        assert "has_audio" in detail
        assert detail["transcript_job_id"]
        assert detail["pipeline"]["upload"]["state"] == "complete"
        assert detail["pipeline"]["transcription"]["state"] == "complete"

        # Connection check (client/authcheck.py): list_sessions(per_page=1) succeeds...
        assert api.list_sessions(per_page=1)["items"] is not None
    # ...and a wrong token is a 401 the recorder reports as "token rejected".
    with _api(client, compat_server, token="wrong") as bad:
        with pytest.raises(httpx.HTTPStatusError) as exc:
            bad.list_sessions(per_page=1)
        assert exc.value.response.status_code in (401, 403)


def test_history_actions_retranscribe_delete_audio_delete(client, compat_server, tmp_path):
    session_dir = _upload_and_finish(client, compat_server, tmp_path, "actions")
    with _api(client, compat_server) as api:
        listing = api.list_sessions(per_page=200)
        sid = next(i["session_id"] for i in listing["items"] if session_dir.name in str(i.get("name")))
        result = api.retranscribe_session(sid)
        assert isinstance(result, dict)
        _wait(lambda: api.session_detail(sid)["jobs"][0]["state"] in ("done", "error"), timeout=20)
        assert isinstance(api.delete_session_audio(sid), dict)
        assert isinstance(api.delete_session(sid), dict)
        with pytest.raises(httpx.HTTPStatusError) as exc:
            api.session_detail(sid)
        assert exc.value.response.status_code == 404


def test_import_recording_upload(client, compat_server, tmp_path):
    """POST /v1/uploads, the recorder's 'import a recording' path (multipart)."""
    if not hasattr(client.api.ServerClient, "upload_recording"):
        pytest.skip("this release has no import-recording feature")
    wav = support.write_wav_16k(tmp_path / f"import-{client.version}.wav")
    with _api(client, compat_server) as api:
        body = api.upload_recording(wav, name=f"Imported {client.version}")
        assert body["session_id"] and body["job_id"]
        job = _wait(lambda: (j := api.job(body["job_id"]))["state"] in ("done", "error") and j, timeout=20)
        assert job["state"] == "done"


# ---------------------------------------------------------------------------
# Client logs (Logs window -> Send to server), 0.7.0 onward
# ---------------------------------------------------------------------------


def test_client_logs_send(client, compat_server):
    if not hasattr(client, "logs_send"):
        pytest.skip("this release predates client-log uploads")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("client.log", "hello")
    result = client.logs_send.send_zip(
        compat_server.base_url, compat_server.token, buf.getvalue(), f"logs-{client.version}.zip"
    )
    assert result.ok, result.message
    assert result.remote_id
    # And the two ways it fails are still reported as such, not as crashes.
    bad = client.logs_send.send_zip(compat_server.base_url, "wrong-token", buf.getvalue(), "x.zip")
    assert not bad.ok and "token" in bad.message.lower()
    with httpx.Client(headers={"Authorization": f"Bearer {compat_server.token}"}) as http:
        listing = http.get(compat_server.base_url + "/v1/client-logs").json()
    assert listing["items"]


# ---------------------------------------------------------------------------
# Live recorder presence (0.7.6+): released recorders never open the control channel
# ---------------------------------------------------------------------------


def test_old_recorder_works_beside_a_control_channel_and_never_appears(client, compat_server):
    """A recorder from before remote control only uses the old endpoints. While a
    newer recorder holds a control socket open, the old one streams and uploads as
    always, is never listed as a live recorder, and the new one still gets commands."""
    import json
    import threading

    from websockets.sync.client import connect

    from meeting_notes import remote

    auth = {"Authorization": f"Bearer {compat_server.token}"}
    instance = uuid.uuid4().hex
    ws_url = compat_server.base_url.replace("http://", "ws://") + remote.CONNECT
    with connect(ws_url, additional_headers=auth) as ws:
        ws.send(json.dumps({"type": "hello", "protocol": remote.PROTOCOL_VERSION, "instance_id": instance,
                            "device": "new-box", "platform": "Windows 11", "version": "9.9.9", "state": {}}))
        assert json.loads(ws.recv(timeout=5))["type"] == "welcome"

        # The old recorder's whole network surface keeps working ...
        sid = _sid(client, "ctl")
        streamer = client.streamer.LiveStreamer(compat_server.base_url, compat_server.token, buffer_seconds=5.0)
        streamer.start(sid, "Compat beside control", time.time())
        try:
            streamer.submit("mic", support.pcm_tone(0.5))
            assert _wait(lambda: streamer.state == "connected"), streamer.last_error
            with _api(client, compat_server) as api:
                assert api.health()["status"] == "ok"
            # ... and it is not a live recorder: only the control socket is listed.
            listed = httpx.get(compat_server.base_url + remote.LIST, headers=auth).json()["items"]
            assert [i["instance_id"] for i in listed] == [instance]
        finally:
            streamer.stop(join_timeout=3.0)

        # Commands still reach the new recorder.
        result = {}

        def post():
            result["r"] = httpx.post(compat_server.base_url + remote.command_path(instance),
                                     json={"command": "refresh_devices"}, headers=auth, timeout=10)

        t = threading.Thread(target=post)
        t.start()
        frame = json.loads(ws.recv(timeout=5))
        assert frame["type"] == "command" and frame["command"] == "refresh_devices"
        ws.send(json.dumps({"type": "ack", "command_id": frame["command_id"], "ok": True, "state": {}}))
        t.join(10)
        assert result["r"].status_code == 200 and result["r"].json()["ok"] is True
    assert _wait(lambda: not httpx.get(compat_server.base_url + remote.LIST, headers=auth).json()["items"])
