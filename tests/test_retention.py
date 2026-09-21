"""Tests for the audio retention policy (meeting_notes.server.retention).

``should_delete_audio`` is a pure function over a plain dict (an index row)
and a Settings object, so the rule table is tested directly with fixtures --
no Store, no filesystem, no threads. ``apply_retention`` and
``RetentionWorker`` are tested against a real Store, since that's where the
"actually deletes the files, never the transcript" behaviour lives.
"""

from __future__ import annotations

import json
import time

import pytest

from meeting_notes import wire
from meeting_notes.server import retention
from meeting_notes.server import settings as settings_mod
from meeting_notes.server import store as store_mod

DAY = 86400.0


def make_settings(**overrides) -> settings_mod.Settings:
    return settings_mod.Settings(
        model="base.en",
        audio_retention_days=overrides.pop("audio_retention_days", -1),
        delete_audio_only_after_success=overrides.pop("delete_audio_only_after_success", True),
        **overrides,
    )


def session_row(**overrides) -> dict:
    row = {
        "session_id": "sess-1",
        "has_audio": True,
        "created": 0.0,
        "latest_state": wire.JobState.DONE,
    }
    row.update(overrides)
    return row


# -- should_delete_audio: the rule table --------------------------------


def test_no_audio_is_never_deleted():
    settings = make_settings(audio_retention_days=0)
    row = session_row(has_audio=False)
    assert retention.should_delete_audio(row, settings, now=0.0) is False


def test_no_job_on_record_yet_is_never_deleted():
    settings = make_settings(audio_retention_days=0)
    row = session_row(latest_state=None)
    assert retention.should_delete_audio(row, settings, now=0.0) is False


@pytest.mark.parametrize("state", [wire.JobState.QUEUED, wire.JobState.RUNNING])
def test_active_job_is_never_deleted(state):
    settings = make_settings(audio_retention_days=0)
    row = session_row(latest_state=state)
    assert retention.should_delete_audio(row, settings, now=0.0) is False


def test_only_after_success_skips_a_failed_job():
    settings = make_settings(audio_retention_days=0, delete_audio_only_after_success=True)
    row = session_row(latest_state=wire.JobState.ERROR)
    assert retention.should_delete_audio(row, settings, now=0.0) is False


def test_only_after_success_false_allows_deleting_after_a_failed_job():
    settings = make_settings(audio_retention_days=0, delete_audio_only_after_success=False)
    row = session_row(latest_state=wire.JobState.ERROR)
    assert retention.should_delete_audio(row, settings, now=0.0) is True


def test_retention_days_negative_one_means_never():
    settings = make_settings(audio_retention_days=-1)
    row = session_row(created=0.0)
    assert retention.should_delete_audio(row, settings, now=100 * DAY) is False


def test_retention_days_zero_deletes_once_done():
    settings = make_settings(audio_retention_days=0)
    row = session_row(latest_state=wire.JobState.DONE, created=time.time())
    assert retention.should_delete_audio(row, settings, now=time.time()) is True


def test_retention_days_n_waits_for_age():
    settings = make_settings(audio_retention_days=7)
    row = session_row(created=0.0)

    assert retention.should_delete_audio(row, settings, now=6 * DAY) is False
    assert retention.should_delete_audio(row, settings, now=7 * DAY) is True
    assert retention.should_delete_audio(row, settings, now=30 * DAY) is True


def test_retention_days_n_still_requires_success_when_flag_set():
    settings = make_settings(audio_retention_days=7, delete_audio_only_after_success=True)
    row = session_row(created=0.0, latest_state=wire.JobState.ERROR)
    assert retention.should_delete_audio(row, settings, now=30 * DAY) is False


# -- apply_retention against a real Store --------------------------------


def make_store(tmp_path) -> store_mod.Store:
    return store_mod.Store(str(tmp_path / "data"))


_ONE_SEGMENT = [{"start": 0.0, "end": 1.0, "track": "mic", "label": "You", "text": "hi"}]


def _finish_session(store, session_id, *, created, state=wire.JobState.DONE, error=None, segments=_ONE_SEGMENT):
    store.write_session_meta(session_id, {"created": created})
    store.track_wav_path(session_id, "mic").write_bytes(b"\x00" * 10)
    store._index_upsert_session(session_id)
    job_id = store.create_job(session_id)
    store.update_job(job_id, state=state, error=error, progress=1.0)
    if state == wire.JobState.DONE and segments is not None:
        store.write_transcript(job_id, "# t", json.dumps({"session": {}, "segments": segments}))
    return job_id


def test_apply_retention_deletes_only_sessions_past_their_policy(tmp_path):
    store = make_store(tmp_path)
    now = time.time()

    _finish_session(store, "old-enough", created=now - 10 * DAY)
    _finish_session(store, "too-recent", created=now - 1 * DAY)

    settings = make_settings(audio_retention_days=7)
    deleted = retention.apply_retention(store, settings, now=now)

    assert deleted == ["old-enough"]
    assert store.session_index_row("old-enough")["has_audio"] is False
    assert store.session_index_row("too-recent")["has_audio"] is True
    # The transcript must never be touched by retention.
    assert store.session_meta_path("old-enough").exists()


def test_apply_retention_skips_a_session_with_a_running_job(tmp_path):
    store = make_store(tmp_path)
    now = time.time()

    store.write_session_meta("sess-1", {"created": now - 100 * DAY})
    store.track_wav_path("sess-1", "mic").write_bytes(b"\x00" * 10)
    store._index_upsert_session("sess-1")
    job_id = store.create_job("sess-1")
    store.update_job(job_id, state=wire.JobState.RUNNING, progress=0.3)

    settings = make_settings(audio_retention_days=0)
    deleted = retention.apply_retention(store, settings, now=now)

    assert deleted == []
    assert store.session_index_row("sess-1")["has_audio"] is True


def test_apply_retention_never_deletes_transcripts_or_session_json(tmp_path):
    store = make_store(tmp_path)
    now = time.time()
    job_id = _finish_session(store, "sess-1", created=now - 100 * DAY)
    store.write_transcript(job_id, "# hello", json.dumps({"segments": _ONE_SEGMENT}))

    settings = make_settings(audio_retention_days=0)
    retention.apply_retention(store, settings, now=now)

    assert store.session_meta_path("sess-1").exists()
    assert store.read_transcript(job_id) is not None
    assert not store.track_wav_path("sess-1", "mic").exists()


def test_apply_retention_with_session_ids_only_considers_those_sessions(tmp_path):
    store = make_store(tmp_path)
    now = time.time()
    _finish_session(store, "sess-a", created=now - 100 * DAY)
    _finish_session(store, "sess-b", created=now - 100 * DAY)

    settings = make_settings(audio_retention_days=0)
    deleted = retention.apply_retention(store, settings, now=now, session_ids=["sess-a"])

    assert deleted == ["sess-a"]
    assert store.session_index_row("sess-b")["has_audio"] is True


def test_apply_retention_covers_every_session_across_its_own_pagination(tmp_path):
    """apply_retention walks Store.list_sessions a page (of 200) at a time
    rather than fetching everything in one call -- this pins that every
    session is still considered exactly once, not just "however many fit on
    the first page"."""
    store = make_store(tmp_path)
    now = time.time()
    for i in range(5):
        _finish_session(store, f"sess-{i}", created=now - 100 * DAY)

    settings = make_settings(audio_retention_days=0)
    deleted = retention.apply_retention(store, settings, now=now)

    assert sorted(deleted) == [f"sess-{i}" for i in range(5)]


# -- immediate deletion on job success (JobQueue integration) ----------------


def test_job_queue_deletes_audio_immediately_on_success_when_retention_is_zero(tmp_path):
    from meeting_notes.server.jobs import JobQueue
    from meeting_notes.transcribe.protocol import Segment

    store = make_store(tmp_path)
    settings_mod.save_settings(store.root, make_settings(audio_retention_days=0))

    session_id = "sess-1"
    store.write_session_meta(session_id, {"created": "2026-09-20", "tracks": {"mic": {}}})
    wav_path = store.track_wav_path(session_id, "mic")
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    import wave

    with wave.open(str(wav_path), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(wire.STREAM_SAMPLE_RATE)
        fh.writeframes(b"\x00\x00" * wire.STREAM_SAMPLE_RATE)

    class StubTranscriber:
        def transcribe(self, wav_path, track):
            return [Segment(start=0.0, end=1.0, text="hi", track=track)]

    queue = JobQueue(store, lambda **_kw: StubTranscriber())
    job_id = store.create_job(session_id)
    queue._process(job_id)

    job = store.read_job(job_id)
    assert job["state"] == wire.JobState.DONE
    # 0-day retention: audio must be gone right after the job that produced
    # the transcript finishes, not just "eventually" via the sweep worker.
    assert not wav_path.exists()
    assert store.session_index_row(session_id)["has_audio"] is False
    # The transcript itself must survive.
    assert store.read_transcript(job_id) is not None


def test_job_queue_does_not_delete_audio_when_retention_is_not_zero(tmp_path):
    from meeting_notes.server.jobs import JobQueue
    from meeting_notes.transcribe.protocol import Segment

    store = make_store(tmp_path)
    settings_mod.save_settings(store.root, make_settings(audio_retention_days=-1))

    session_id = "sess-1"
    store.write_session_meta(session_id, {"created": "2026-09-20", "tracks": {"mic": {}}})
    wav_path = store.track_wav_path(session_id, "mic")
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    import wave

    with wave.open(str(wav_path), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(wire.STREAM_SAMPLE_RATE)
        fh.writeframes(b"\x00\x00" * wire.STREAM_SAMPLE_RATE)

    class StubTranscriber:
        def transcribe(self, wav_path, track):
            return [Segment(start=0.0, end=1.0, text="hi", track=track)]

    queue = JobQueue(store, lambda **_kw: StubTranscriber())
    job_id = store.create_job(session_id)
    queue._process(job_id)

    assert wav_path.exists()


# -- RetentionWorker ----------------------------------------------------


def test_worker_start_and_stop_do_not_hang():
    store = None
    try:
        import tempfile

        tmp = tempfile.mkdtemp(prefix="meeting-notes-retention-test-")
        store = store_mod.Store(tmp)
        worker = retention.RetentionWorker(store, interval_minutes=1 / 60.0, startup_delay=0.0)
        worker.start()
        worker.start()  # calling start() twice must be a harmless no-op
        time.sleep(0.1)
        worker.stop(timeout=2.0)
        assert worker._thread is not None
        assert not worker._thread.is_alive()
    finally:
        if store is not None:
            store.index.close()


def test_worker_actually_sweeps(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    now = time.time()
    _finish_session(store, "sess-1", created=now - 100 * DAY)
    settings_mod.save_settings(store.root, make_settings(audio_retention_days=0))

    worker = retention.RetentionWorker(store, interval_minutes=1 / 60.0, startup_delay=0.0)
    worker.start()
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            row = store.session_index_row("sess-1")
            if not row["has_audio"]:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("retention worker never swept the aged-out session")
    finally:
        worker.stop()


def test_wake_applies_a_settings_change_without_waiting_out_the_old_interval(tmp_path):
    """Seen on a real run: retention was switched from "never" to
    "immediately" with a one-minute interval, and nothing happened -- the
    worker was already asleep for the OLD hour-long interval. A settings
    save must be able to poke it."""
    store = make_store(tmp_path)
    _finish_session(store, "sess-1", created=time.time() - 100 * DAY)
    settings_mod.save_settings(store.root, make_settings(audio_retention_days=-1))

    worker = retention.RetentionWorker(store, interval_minutes=60.0, startup_delay=0.0)
    worker.start()
    try:
        time.sleep(0.3)  # first sweep (keep forever) has run; now napping for an hour
        assert store.session_index_row("sess-1")["has_audio"]

        settings_mod.save_settings(store.root, make_settings(audio_retention_days=0))
        worker.wake()

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and store.session_index_row("sess-1")["has_audio"]:
            time.sleep(0.05)
        assert not store.session_index_row("sess-1")["has_audio"]
    finally:
        worker.stop()


def test_success_with_an_empty_transcript_does_not_release_the_audio(tmp_path):
    """Seen on a real run: a re-run on a streamed-but-never-finalized session
    found no WAV, produced a DONE job with zero segments, and 0-day retention
    deleted the only copy of the audio. DONE-but-empty must not count as the
    success that "only after success" is waiting for."""
    store = make_store(tmp_path)
    now = time.time()
    _finish_session(store, "empty", created=now - 10 * DAY, segments=[])
    _finish_session(store, "no-transcript-file", created=now - 10 * DAY, segments=None)
    _finish_session(store, "real", created=now - 10 * DAY)

    deleted = retention.apply_retention(store, make_settings(audio_retention_days=0), now=now)

    assert deleted == ["real"]
    assert store.session_index_row("empty")["has_audio"] is True
    assert store.session_index_row("no-transcript-file")["has_audio"] is True

    # With the safety flag off, the operator has said "delete regardless".
    deleted = retention.apply_retention(
        store, make_settings(audio_retention_days=0, delete_audio_only_after_success=False), now=now
    )
    assert sorted(deleted) == ["empty", "no-transcript-file"]
