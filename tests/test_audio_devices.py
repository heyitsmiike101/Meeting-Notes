from types import SimpleNamespace

import pytest

from meeting_notes.audio import devices


def test_resolve_source_preserves_soundcard_enumeration_error(monkeypatch):
    cause = RuntimeError("Error 0x80070490")

    class BrokenSoundcard:
        def all_microphones(self):
            raise cause

    monkeypatch.setattr(devices.soundcard_source, "import_soundcard", lambda: BrokenSoundcard())

    with pytest.raises(devices.DeviceDiscoveryError) as raised:
        devices.resolve_source("mic")

    assert raised.value.cause is cause
    assert "0x80070490" in str(raised.value)
    assert isinstance(raised.value.__cause__, RuntimeError)


def test_missing_windows_mic_explains_remote_session(monkeypatch):
    class NoMicSoundcard:
        def all_microphones(self):
            return []

        def default_microphone(self):
            raise RuntimeError("Error 0x80070490")

    monkeypatch.setattr(devices.sys, "platform", "win32")
    monkeypatch.setenv("SESSIONNAME", "RDP-Tcp#4")
    monkeypatch.setattr(devices.soundcard_source, "import_soundcard", lambda: NoMicSoundcard())

    with pytest.raises(devices.DeviceNotFound) as raised:
        devices.resolve_source("mic")

    message = str(raised.value)
    assert "no microphones" in message.lower()
    assert "remote desktop" in message.lower()
    assert "record from this computer" in message.lower()


def test_resolve_source_uses_available_mic_when_default_lookup_fails(monkeypatch):
    class DefaultMicBroken:
        def all_microphones(self):
            return [SimpleNamespace(name="USB Mic", id="mic-1", channels=1)]

        def default_microphone(self):
            raise RuntimeError("Error 0x80070490")

    monkeypatch.setattr(devices.soundcard_source, "import_soundcard", lambda: DefaultMicBroken())
    source = devices.resolve_source("mic")
    assert source.name == "USB Mic"


def test_audio_diagnostic_report_keeps_exact_backend_errors(monkeypatch):
    class FakeSoundcard:
        def all_microphones(self, include_loopback=False):
            if include_loopback:
                raise RuntimeError("loopback unavailable")
            return [SimpleNamespace(name="USB Mic")]

        def all_speakers(self):
            return [SimpleNamespace(name="USB Speaker")]

        def default_microphone(self):
            raise RuntimeError("Error 0x80070490")

        def default_speaker(self):
            return SimpleNamespace(name="USB Speaker")

    monkeypatch.setattr(devices.soundcard_source, "import_soundcard", lambda: FakeSoundcard())
    report = devices.audio_diagnostic_report()

    assert "microphones=OK" in report
    assert "microphones_loopback=ERROR RuntimeError" in report
    assert "loopback unavailable" in report
    assert "default_microphone=ERROR RuntimeError" in report
    assert "0x80070490" in report
