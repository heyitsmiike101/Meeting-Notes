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


def test_update_installer_follows_the_address_the_recorder_used(client, compat_server, tmp_path):
    """A recorder that reaches the server by an address other than the saved
    ``server_address`` (e.g. by IP because it cannot resolve the LAN name) must be
    handed an installer that downloads from, and configures, that same address."""
    from meeting_notes.server import settings as settings_mod

    root = compat_server.store.root
    saved = settings_mod.load_settings(root)
    settings_mod.save_settings(
        root, settings_mod.Settings(model=saved.model, server_address="http://saved.example.lan")
    )
    try:
        updater = client.update.ClientUpdater(compat_server.base_url, compat_server.token, current_version="0.0.1")
        found = updater.check()
        assert found is not None
        path = updater.download(found, tmp_path / "Install.ps1")
        text = path.read_text(encoding="utf-8")
        assert compat_server.base_url in text
        assert "saved.example.lan" not in text
    finally:
        settings_mod.save_settings(root, saved)


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


def test_upload_transcript_from_the_recorder_upload_dialog(client, compat_server):
    """POST /v1/sessions/transcript (0.7.8+): a transcript the person already has becomes a finished meeting."""
    if not hasattr(client.api.ServerClient, "upload_transcript"):
        pytest.skip("this release cannot upload a transcript")
    with _api(client, compat_server) as api:
        body = api.upload_transcript(
            "[00:00:05] Jane: Hello\n[00:00:12] Bob: Hi there",
            name=f"Transcript {client.version}",
            started_at=1_790_605_800,
            source="file",
            filename="call.txt",
        )
        assert body["session_id"] and body["state"] == "done" and body["segments"] == 2
        detail = api.session_detail(body["session_id"])
        assert detail["meta"]["name"] == f"Transcript {client.version}"
        assert [s["label"] for s in detail["segments"]] == ["Jane", "Bob"]
        assert detail["has_audio"] is False


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
    assert not bad.ok and ("token" in bad.message.lower() or "password" in bad.message.lower())
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


def test_frozen_control_channel_connects_is_listed_gets_a_command_and_acks(client, compat_server):
    """The 0.7.6+ recorder's real control channel (frozen) against the current server:
    it connects, appears on the Recorders list with a friendly OS name, receives a
    remote command and acks it, and leaves the list when it stops."""
    if not hasattr(client, "control_channel"):
        pytest.skip("recorder predates remote control")
    import threading

    from meeting_notes import remote

    auth = {"Authorization": f"Bearer {compat_server.token}"}
    instance = uuid.uuid4().hex
    received = []
    holder = {}

    def on_command(command_id, name, args):
        received.append((command_id, name, dict(args or {})))
        holder["channel"].send_ack(command_id, True)

    channel = client.control_channel.ControlChannel(
        lambda: (compat_server.base_url, compat_server.token),
        on_command,
        instance_id=instance,
        device="frozen-box",
        platform_text="Darwin 25.0.0",  # what an older Mac reports: the server shows "macOS 26"
        version=client.version,
        backoff_initial=0.05,
        backoff_max=0.2,
        idle_poll=0.05,
    )
    holder["channel"] = channel
    channel.start()
    try:
        def listed():
            items = httpx.get(compat_server.base_url + remote.LIST, headers=auth).json()["items"]
            return next((i for i in items if i["instance_id"] == instance), None)

        item = _wait(listed)
        assert item, "frozen control channel never appeared on the Recorders list"
        assert item["device"] == "frozen-box" and item["version"] == client.version
        assert item["platform"] == "macos" and item["platform_text"] == "macOS 26"

        result = {}

        def post():
            result["r"] = httpx.post(compat_server.base_url + remote.command_path(instance),
                                     json={"command": "refresh_devices"}, headers=auth, timeout=10)

        t = threading.Thread(target=post)
        t.start()
        t.join(10)
        assert result["r"].status_code == 200 and result["r"].json()["ok"] is True
        assert [name for _, name, _ in received] == ["refresh_devices"]
    finally:
        channel.stop(join_timeout=3.0)
    assert _wait(lambda: listed() is None)


def test_frozen_control_channel_idle_levels_follow_the_watch_lease_and_older_ones_are_left_alone(client, compat_server):
    """Idle level preview (0.7.7+). A recorder that advertises ``idle_levels`` is told to stream while a
    page is looking, its ``levels`` frames reach the page, and it is told to stop when the page leaves.
    Every older recorder (0.7.6 and before) is never sent a watch, stays connected and listed, and
    its snapshots are unaffected: the server must not send an old client anything new."""
    if not hasattr(client, "control_channel"):
        pytest.skip("recorder predates remote control")
    import json

    from websockets.sync.client import connect

    from meeting_notes import remote

    auth = {"Authorization": f"Bearer {compat_server.token}"}
    instance = uuid.uuid4().hex
    hub = compat_server.app.state.recorder_hub
    channel = client.control_channel.ControlChannel(
        lambda: (compat_server.base_url, compat_server.token),
        lambda command_id, name, args: None,
        instance_id=instance,
        device="levels-box",
        platform_text="Windows 11",
        version=client.version,
        backoff_initial=0.05,
        backoff_max=0.2,
        idle_poll=0.05,
    )
    channel.start()
    ws_url = compat_server.base_url.replace("http://", "ws://") + remote.EVENTS
    try:
        assert _wait(lambda: hub.get(instance) is not None), "recorder never connected"
        rec = hub.get(instance)
        supports = hasattr(client.remote, "CAP_IDLE_LEVELS")
        assert (remote.CAP_IDLE_LEVELS in rec.caps) is supports
        with connect(ws_url, additional_headers=auth, open_timeout=5) as page:
            page.send(json.dumps({"type": "watch", "visible": True}))
            if supports:
                assert _wait(lambda: channel.watched), "a watching page never reached the recorder"
                channel.publish_levels({"mic": 0.5, "system": 0.25})
                seen = None
                for _ in range(40):
                    frame = json.loads(page.recv(timeout=5))
                    if frame.get("type") == "levels" and frame.get("instance_id") == instance:
                        seen = frame
                        break
                assert seen == {"type": "levels", "instance_id": instance, "tracks": {"mic": 0.5, "system": 0.25}}
            else:
                time.sleep(0.6)
                assert rec.watching is False and rec.caps == ()
                assert channel.connected and not hasattr(channel, "watched")
        if supports:
            assert _wait(lambda: not channel.watched), "the recorder kept streaming after the page left"
        else:
            assert channel.connected and hub.get(instance) is rec    # still listed, never disturbed
    finally:
        channel.stop(join_timeout=3.0)
    assert _wait(lambda: hub.get(instance) is None)


def _seed_compat_session(compat_server, sid, *, done=True):
    store = compat_server.store
    store.write_session_meta(sid, {"name": f"Compat {sid}", "started_wall": 1000.0, "duration_sec": 5, "device": "x"})
    if done:
        job_id = store.create_job(sid)
        store.update_job(job_id, state="done", progress=1.0)


def test_frozen_control_channel_answers_list_recordings_with_a_joined_result(client, compat_server):
    """The 0.7.6+ recorder (frozen) answers ``list_recordings`` with a ``result`` ack; the CURRENT server
    joins it with what it knows (Recorders page -> saved recordings). Older recorders never open the
    control channel, so they are unaffected (the test above and the upload tests cover them)."""
    if not hasattr(client, "control_channel"):
        pytest.skip("recorder predates remote control")
    import inspect

    if "result" not in inspect.signature(client.control_channel.ControlChannel.send_ack).parameters:
        pytest.skip("this recorder's control channel cannot send a recordings result")

    from meeting_notes import remote

    auth = {"Authorization": f"Bearer {compat_server.token}"}
    instance = uuid.uuid4().hex
    tag = uuid.uuid4().hex[:8]
    on_server, missing = f"compat-rec-{tag}-ready", f"compat-rec-{tag}-missing"
    _seed_compat_session(compat_server, on_server)
    received = []
    holder = {}

    def row(sid, started, **queue):
        return {"session_id": sid, "name": f"Rec {sid}", "started": started, "duration_sec": 5.0, "size_bytes": 99,
                "valid": True, "reason": None, "active": False,
                "queue": {"state": "not_queued", "percent": None, "error": None, "attempts": 0, **queue}}

    def on_command(command_id, name, args):
        received.append((name, dict(args or {})))
        result = {"recordings": [row(missing, 2000.0, state="pending", error="connection refused"),
                                 row(on_server, 1000.0)], "total": 2, "offset": 0, "next_offset": None}
        holder["channel"].send_ack(command_id, True, result=result)

    channel = client.control_channel.ControlChannel(
        lambda: (compat_server.base_url, compat_server.token), on_command,
        instance_id=instance, device="frozen-box", platform_text="Windows 11", version=client.version,
        backoff_initial=0.05, backoff_max=0.2, idle_poll=0.05,
    )
    holder["channel"] = channel
    channel.start()
    try:
        def listed():
            items = httpx.get(compat_server.base_url + remote.LIST, headers=auth).json()["items"]
            return next((i for i in items if i["instance_id"] == instance), None)

        assert _wait(listed), "frozen control channel never appeared on the Recorders list"
        r = httpx.get(f"{compat_server.base_url}/v1/recorders/{instance}/recordings", headers=auth, timeout=15)
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True and received == [("list_recordings", {})]
        rows = {x["session_id"]: x for x in body["recordings"]}
        assert rows[on_server]["status"] == "uploaded_ready" and rows[on_server]["server_has_copy"] is True
        assert rows[on_server]["label"] == "Uploaded \u00b7 transcript ready"
        assert rows[on_server]["meeting_url"] == f"/sessions/{on_server}"
        assert rows[missing]["status"] == "waiting" and rows[missing]["label"] == "Waiting to upload"
        assert rows[missing]["meeting_url"] is None
        assert [x["session_id"] for x in body["recordings"]] == [missing, on_server]  # newest first
    finally:
        channel.stop(join_timeout=3.0)
    assert _wait(lambda: listed() is None)


def test_frozen_client_recordings_status_works_against_the_current_server(client, compat_server):
    """The 0.7.6+ recorder's ``ServerClient.recordings_status`` (the re-upload window's server check)."""
    if not hasattr(client.api.ServerClient, "recordings_status"):
        pytest.skip("recorder predates the recordings status check")
    tag = uuid.uuid4().hex[:8]
    ready, partial, gone = f"compat-st-{tag}-ready", f"compat-st-{tag}-partial", f"compat-st-{tag}-none"
    _seed_compat_session(compat_server, ready)
    _seed_compat_session(compat_server, partial, done=False)
    with _api(client, compat_server) as api:
        found = api.recordings_status([ready, partial, gone, "../bad", ready])
    assert set(found) == {ready, partial, gone}
    assert found[ready]["on_server"] is True and found[ready]["has_copy"] is True
    assert found[ready]["transcription"] in ("complete", "queued", "transcribing")
    assert found[partial]["on_server"] is True and found[partial]["has_copy"] is False
    assert found[gone] == {"on_server": False, "has_copy": False, "in_trash": False, "transcription": None,
                           "error": None}
