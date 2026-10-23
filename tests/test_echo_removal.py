"""Echo removal: the far end's voice, heard by the mic from the speakers, is taken out before transcription."""

from __future__ import annotations

import os
import shutil
import wave
from pathlib import Path

import numpy as np
import pytest

from meeting_notes import wire
from meeting_notes.server import settings as settings_mod
from meeting_notes.transcribe import echo
from tests.compute_helpers import Rig

R = echo.RATE


def speechlike(seconds: float, seed: int, *, on: float = 1.2, off: float = 0.8) -> np.ndarray:
    """Band-limited noise in talk spurts: correlates like speech, not like a tone."""
    rng = np.random.default_rng(seed)
    n = int(seconds * R)
    x = np.convolve(rng.standard_normal(n), np.hanning(9), "same")
    x = x - np.convolve(x, np.ones(64) / 64, "same")  # no rumble
    period = int((on + off) * R)
    envelope = ((np.arange(n) % period) < on * R).astype(np.float64)
    return 6000.0 * x / np.std(x) * envelope


def echo_of(ref: np.ndarray, lag_seconds: float, gain: float = 0.4) -> np.ndarray:
    """``ref`` as a mic hears it from a speaker: late by ``lag_seconds`` (may be negative), quieter, a short tail."""
    rng = np.random.default_rng(99)
    tail = rng.standard_normal(int(0.05 * R)) * np.exp(-np.arange(int(0.05 * R)) / (0.01 * R))
    tail[0] += 3.0
    tail *= gain / np.sqrt(np.sum(tail ** 2))
    shifted = np.interp(np.arange(len(ref)) - lag_seconds * R, np.arange(len(ref)), ref, left=0, right=0)
    return np.convolve(shifted, tail)[: len(ref)]


def write_wav(path: Path, samples: np.ndarray) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(R)
        w.writeframes(np.clip(np.rint(samples), -32768, 32767).astype("<i2").tobytes())
    return path


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, R)
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float64)


def db(x) -> float:
    return 10 * np.log10(np.mean(np.square(x)) + 1e-9)


def call(tmp_path: Path, lag: float, seconds: float = 60.0):
    """A call: the far end talks in the first half, the user in the second, both bleed + voice on the mic."""
    far = speechlike(seconds, 1)
    far[int(seconds / 2 * R):] = 0
    you = speechlike(seconds, 2)
    you[: int(seconds / 2 * R)] = 0
    mic = you + echo_of(far, lag)
    return write_wav(tmp_path / "mic.wav", mic), write_wav(tmp_path / "system.wav", far), you, mic


# -- the canceller (needs the livekit package, a server dependency) ---------------------------------------


livekit = pytest.importorskip("livekit")


@pytest.mark.parametrize("lag", [0.17, 0.9, -0.2])
def test_the_echo_is_removed_and_your_voice_is_kept(tmp_path, lag):
    mic_wav, system_wav, you, mic = call(tmp_path, lag)
    result = echo.remove_echo(mic_wav, system_wav)
    assert result.applied and result.blocks >= 1
    assert result.lag_ms[0] == pytest.approx(lag * 1000, abs=5) and result.lag_ms[1] == pytest.approx(lag * 1000, abs=5)
    out = read_wav(echo.cleaned_path(mic_wav))
    assert len(out) == len(mic)  # frame n is still frame n: the timing log keeps applying
    half = len(mic) // 2
    settled = slice(int(5 * R), half - int(1 * R))  # after AEC3 has converged, before the user starts
    assert db(mic[settled]) - db(out[settled]) > 25
    yours = slice(half + int(1 * R), len(mic))
    assert abs(db(out[yours]) - db(you[yours])) < 1.5
    # and in time with the original: the cleaned voice lines up with the recorded one at zero offset
    a, b = out[yours], mic[yours]
    lags = range(-200, 201)
    best = max(lags, key=lambda d: float(np.dot(a[200 + d: len(a) - 200 + d], b[200: len(b) - 200])))
    assert abs(best) <= 2


def test_a_headset_recording_is_left_alone(tmp_path):
    far = speechlike(40, 1)
    you = speechlike(40, 2)  # nothing of the far end on the mic
    mic_wav, system_wav = write_wav(tmp_path / "mic.wav", you), write_wav(tmp_path / "system.wav", far)
    result = echo.remove_echo(mic_wav, system_wav)
    assert not result.applied and result.reason == "no echo found"
    assert not echo.cleaned_path(mic_wav).exists()


def test_the_result_is_reused_until_a_track_changes(tmp_path, monkeypatch):
    mic_wav, system_wav, _you, mic = call(tmp_path, 0.2, seconds=40)
    first = echo.remove_echo(mic_wav, system_wav)
    assert first.applied
    calls = []
    real = echo.measure_lags
    monkeypatch.setattr(echo, "measure_lags", lambda *a: calls.append(1) or real(*a))
    again = echo.remove_echo(mic_wav, system_wav)
    assert again.applied and again.lag_ms == first.lag_ms and calls == []
    later = os.path.getmtime(echo.cleaned_path(mic_wav)) + 5
    os.utime(mic_wav, (later, later))  # the mic was rewritten (a re-upload)
    assert echo.remove_echo(mic_wav, system_wav).applied and calls == [1]


def test_silence_on_the_far_end_needs_no_canceller(tmp_path):
    mic_wav = write_wav(tmp_path / "mic.wav", speechlike(30, 2))
    system_wav = write_wav(tmp_path / "system.wav", np.zeros(30 * R))

    def no_canceller():
        raise AssertionError("the canceller should not be built")

    result = echo.remove_echo(mic_wav, system_wav, factory=no_canceller)
    assert not result.applied


# -- the job ------------------------------------------------------------------------------------------------


def _fake_removal(calls):
    """Stands in for remove_echo on the silent test uploads: writes a recognisable cleaned copy."""

    def fake(mic_wav, system_wav):
        calls.append((Path(mic_wav).name, Path(system_wav).name))
        shutil.copyfile(mic_wav, echo.cleaned_path(mic_wav))
        with open(echo.cleaned_path(mic_wav), "ab") as fh:
            fh.write(b"CLEANED")
        return echo.EchoResult(applied=True, lag_ms=(170.0, 172.0), blocks=4)

    return fake


class _PathRecorder:
    def __init__(self):
        self.paths = {}

    def transcribe(self, wav_path, track):
        from meeting_notes.transcribe.protocol import Segment

        self.paths[track] = Path(wav_path)
        return [Segment(start=0.0, end=1.0, text=f"local {track}", track=track)]


def test_a_local_job_transcribes_the_cleaned_mic(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("meeting_notes.server.jobs.echo_mod.remove_echo", _fake_removal(calls))
    recorder = _PathRecorder()
    rig = Rig(tmp_path, monkeypatch, offload=False, transcriber=recorder)
    job_id = rig.new_job()
    job = rig.wait_state(job_id)
    assert calls == [("mic.wav", "system.wav")]
    assert recorder.paths["mic"].name == "mic.aec" and recorder.paths["system"].name == "system.wav"
    assert job["echo"]["applied"] is True and job["echo"]["lag_ms"] == [170.0, 172.0]


def test_a_gpu_helper_downloads_the_cleaned_mic(tmp_path, monkeypatch):
    monkeypatch.setattr("meeting_notes.server.jobs.echo_mod.remove_echo", _fake_removal([]))
    rig = Rig(tmp_path, monkeypatch)
    job_id = rig.offered_job()
    tracks = {t["name"]: t for t in rig.job(job_id)["compute"]["tracks"]}
    assert tracks["mic"]["audio"] == "echo_removed" and "audio" not in tracks["system"]
    lease = rig.claim()
    mic = rig.client.get(f"/v1/compute/jobs/{job_id}/audio/mic", headers=rig.lease_headers(lease))
    system = rig.client.get(f"/v1/compute/jobs/{job_id}/audio/system", headers=rig.lease_headers(lease))
    assert mic.status_code == 200 and mic.content.endswith(b"CLEANED")
    assert system.status_code == 200 and not system.content.endswith(b"CLEANED")


def test_off_in_settings_transcribes_the_mic_as_recorded(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("meeting_notes.server.jobs.echo_mod.remove_echo", _fake_removal(calls))
    recorder = _PathRecorder()
    rig = Rig(tmp_path, monkeypatch, offload=False, transcriber=recorder, echo_removal="off")
    job = rig.wait_state(rig.new_job())
    assert calls == [] and recorder.paths["mic"].name == "mic.wav" and "echo" not in job


def test_a_failure_falls_back_to_the_recorded_mic(tmp_path, monkeypatch):
    def broken(mic_wav, system_wav):
        raise RuntimeError("boom")

    monkeypatch.setattr("meeting_notes.server.jobs.echo_mod.remove_echo", broken)
    recorder = _PathRecorder()
    rig = Rig(tmp_path, monkeypatch, offload=False, transcriber=recorder)
    job = rig.wait_state(rig.new_job())
    assert job["state"] == wire.JobState.DONE and recorder.paths["mic"].name == "mic.wav"
    assert job["echo"] == {"applied": False, "reason": "failed: boom", "lag_ms": None, "blocks": 0, "seconds": 0.0}


def test_a_meeting_with_only_one_track_is_not_touched(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("meeting_notes.server.jobs.echo_mod.remove_echo", _fake_removal(calls))
    rig = Rig(tmp_path, monkeypatch, offload=False)
    rig.wait_state(rig.new_job(tracks=("system",)))
    assert calls == []


def test_the_cleaned_copy_goes_with_the_audio(tmp_path, monkeypatch):
    monkeypatch.setattr("meeting_notes.server.jobs.echo_mod.remove_echo", _fake_removal([]))
    rig = Rig(tmp_path, monkeypatch, offload=False)
    rig.wait_state(rig.new_job())
    media = rig.store.track_wav_path("s1", "mic").parent
    (media / "mic.aec.json").write_text("{}", encoding="utf-8")
    (media / "mic.aec.1234abcd.tmp").write_bytes(b"partial")
    assert (media / "mic.aec").exists()
    rig.store.delete_session_audio("s1")
    assert not list(media.glob("*.aec*"))


# -- the setting ---------------------------------------------------------------------------------------------


def test_the_setting_defaults_to_auto_and_repairs_bad_values(tmp_path):
    assert settings_mod.Settings().echo_removal == "auto"
    (tmp_path / "settings.json").write_text('{"echo_removal": "sometimes"}', encoding="utf-8")
    assert settings_mod.load_settings(tmp_path).echo_removal == "auto"
    base = settings_mod.Settings(model="small.en").to_dict()
    assert settings_mod.validate({**base, "echo_removal": "off"}).echo_removal == "off"
    with pytest.raises(settings_mod.ValidationError):
        settings_mod.validate({**base, "echo_removal": "sometimes"})


def test_an_api_caller_that_predates_the_setting_keeps_it(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, echo_removal="off")
    payload = settings_mod.load_settings(rig.store.root).to_dict()
    payload.pop("echo_removal")
    resp = rig.client.put("/v1/settings", json=payload)
    assert resp.status_code == 200, resp.text
    assert resp.json()["echo_removal"] == "off"


def test_the_settings_page_offers_it(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, echo_removal="off")
    page = rig.client.get("/settings").text
    assert 'name="echo_removal"' in page and '<option value="off" selected>' in page


def test_a_new_version_redoes_even_a_no_echo_result(tmp_path, monkeypatch):
    mic_wav = write_wav(tmp_path / "mic.wav", speechlike(30, 2))
    system_wav = write_wav(tmp_path / "system.wav", np.zeros(30 * R))
    assert not echo.remove_echo(mic_wav, system_wav).applied
    assert echo.cached_result(mic_wav, system_wav) is not None
    monkeypatch.setattr(echo, "VERSION", echo.VERSION + 1)
    assert echo.cached_result(mic_wav, system_wav) is None


def test_two_jobs_at_once_build_it_once(tmp_path, monkeypatch):
    import threading

    mic_wav, system_wav, _you, _mic = call(tmp_path, 0.2, seconds=40)
    builds = []
    real = echo._build
    monkeypatch.setattr(echo, "_build", lambda *a: builds.append(1) or real(*a))
    results = []
    threads = [threading.Thread(target=lambda: results.append(echo.remove_echo(mic_wav, system_wav))) for _ in range(3)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert builds == [1] and len(results) == 3 and all(r.applied for r in results)


def test_finishing_a_helper_job_does_not_redo_it(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("meeting_notes.server.jobs.echo_mod.remove_echo", _fake_removal(calls))
    rig = Rig(tmp_path, monkeypatch)
    job_id = rig.offered_job()
    lease = rig.claim()
    assert len(calls) == 1
    resp = rig.client.post(f"/v1/compute/jobs/{job_id}/complete", json=rig.good_result(lease), headers=rig.helper_headers)
    assert resp.status_code == 200, resp.text
    job = rig.wait_state(job_id)
    assert len(calls) == 1 and job["echo"]["applied"] is True
