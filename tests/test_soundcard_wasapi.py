"""Tests for the WASAPI mix-format monkeypatch (bastibe/SoundCard#93).

No real audio hardware or even a real ``soundcard`` install is needed: these
build a small fake standing in for ``soundcard.mediafoundation``'s COM
plumbing -- just enough of the double-pointer-to-a-struct shape that
``_AudioClient.__init__`` walks -- and drive the patched ``__init__``
directly. That is deliberate: this bug only reproduces on real WASAPI
hardware (see the module docstring in ``soundcard_source.py``), so a fake is
the only way to exercise the patch logic on this test box, which has none.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from meeting_notes.audio import soundcard_source


# =============================================================================
# fakes standing in for soundcard.mediafoundation's CFFI/COM plumbing
# =============================================================================


class _FakeFormat:
    """Stands in for the ``WAVEFORMATEX`` embedded in ``WAVEFORMATEXTENSIBLE``."""

    def __init__(self, tag: int):
        self.wFormatTag = tag
        self.cbSize = 22 if tag == 0xFFFE else 0
        self.nChannels = 0
        self.nSamplesPerSec = 0
        self.nAvgBytesPerSec = 0
        self.nBlockAlign = 0
        self.wBitsPerSample = 0


class _FakeMixFormat:
    """Stands in for the ``WAVEFORMATEXTENSIBLE`` struct GetMixFormat fills in."""

    def __init__(self, tag: int):
        self.Format = _FakeFormat(tag)
        self.Samples = None


class _Deref:
    """One level of CFFI pointer indirection: ``ptr[0]`` returns ``value``.

    Real CFFI pointers chain (``ppMixFormat[0][0]``), so both the "pointer to
    a pointer" and "pointer to a struct" cases are just this, nested.
    """

    def __init__(self, value):
        self._value = value

    def __getitem__(self, index):
        assert index == 0
        return self._value


class _FakeLpVtbl:
    """The IAudioClient vtable methods ``_AudioClient.__init__`` calls."""

    def __init__(self, mix_format: _FakeMixFormat):
        self.mix_format = mix_format
        self.initialize_calls: list = []

    def GetMixFormat(self, _self_ptr, out_pp):
        # Real GetMixFormat allocates a struct and points *ppMixFormat at it;
        # here that's just handing back a fresh double-deref to our fake.
        out_pp._value = _Deref(self.mix_format)
        return 0  # S_OK

    def Initialize(self, _self_ptr, sharemode, streamflags, bufferduration, reserved, pformat, null):
        self.initialize_calls.append(
            dict(
                sharemode=sharemode,
                streamflags=streamflags,
                bufferduration=bufferduration,
                format=pformat,
            )
        )
        return 0  # S_OK


def _make_fake_mf(tag: int):
    """A fake module standing in for ``soundcard.mediafoundation``."""
    mix_format = _FakeMixFormat(tag)
    lpVtbl = _FakeLpVtbl(mix_format)
    audio_client_struct = SimpleNamespace(lpVtbl=lpVtbl)

    class _AudioClient:
        def __init__(self, *a, **kw):  # pragma: no cover - replaced by the patch
            raise AssertionError("not patched")

    fake_ffi = SimpleNamespace(
        new=lambda typestr: _Deref(None),
        NULL=object(),
    )
    fake_ole32 = SimpleNamespace(
        AUDCLNT_SHAREMODE_EXCLUSIVE=1,
        AUDCLNT_SHAREMODE_SHARED=0,
        CoTaskMemFree=lambda ptr: None,
    )
    fake_com = SimpleNamespace(check_error=lambda hr: None)

    mf = SimpleNamespace(
        _AudioClient=_AudioClient,
        _ffi=fake_ffi,
        _ole32=fake_ole32,
        _com=fake_com,
    )
    return mf, audio_client_struct, lpVtbl


def _make_fake_soundcard(tag: int):
    mf, audio_client_struct, lpVtbl = _make_fake_mf(tag)
    soundcard = SimpleNamespace(mediafoundation=mf)
    return soundcard, mf, audio_client_struct, lpVtbl


@pytest.fixture(autouse=True)
def force_windows(monkeypatch):
    """The patch is a no-op off Windows; force the branch under test."""
    monkeypatch.setattr(soundcard_source.sys, "platform", "win32")


# =============================================================================
# tests
# =============================================================================


def test_extensible_format_is_kept_working(monkeypatch):
    """The 0xFFFE path (already-working devices, e.g. the loopback device)
    must come out the other side able to Initialize, with rate/channels
    overridden the same way soundcard's own code did."""
    soundcard, mf, audio_client_struct, lpVtbl = _make_fake_soundcard(0xFFFE)
    soundcard_source._install_wasapi_mix_format_patch(soundcard)

    client = mf._AudioClient.__new__(mf._AudioClient)
    ptr = _Deref(_Deref(audio_client_struct))
    mf._AudioClient.__init__(
        client, ptr, samplerate=48000, channels=2, blocksize=480, isloopback=False
    )

    assert client.samplerate == 48000
    assert client.channelmap == [0, 1]
    assert len(lpVtbl.initialize_calls) == 1
    assert lpVtbl.mix_format.Format.nChannels == 2
    assert lpVtbl.mix_format.Format.nSamplesPerSec == 48000
    assert lpVtbl.mix_format.Format.wBitsPerSample == 32
    # The one field soundcard's own code sets for the extensible case.
    assert lpVtbl.mix_format.Samples == dict(wValidBitsPerSample=32)


def test_plain_waveformatex_is_now_supported(monkeypatch):
    """The bug this exists to fix: wFormatTag 0x3 (a plain WAVEFORMATEX, as
    reported by this project's own Logitech C922 test hardware) must no
    longer hit soundcard's bare ``assert ... == 0xFFFE`` and must Initialize
    successfully instead."""
    soundcard, mf, audio_client_struct, lpVtbl = _make_fake_soundcard(0x3)
    soundcard_source._install_wasapi_mix_format_patch(soundcard)

    client = mf._AudioClient.__new__(mf._AudioClient)
    ptr = _Deref(_Deref(audio_client_struct))
    mf._AudioClient.__init__(
        client, ptr, samplerate=16000, channels=1, blocksize=1600, isloopback=False
    )

    assert client.samplerate == 16000
    assert len(lpVtbl.initialize_calls) == 1
    assert lpVtbl.mix_format.Format.nChannels == 1
    assert lpVtbl.mix_format.Format.nSamplesPerSec == 16000
    assert lpVtbl.mix_format.Format.nBlockAlign == 4
    assert lpVtbl.mix_format.Format.wBitsPerSample == 32
    # No Samples union on a plain WAVEFORMATEX -- unlike the 0xFFFE case,
    # nothing should touch it.
    assert lpVtbl.mix_format.Samples is None


def test_unsupported_tag_raises_clear_runtime_error(monkeypatch):
    soundcard, mf, audio_client_struct, lpVtbl = _make_fake_soundcard(0x1)  # WAVE_FORMAT_PCM
    soundcard_source._install_wasapi_mix_format_patch(soundcard)

    client = mf._AudioClient.__new__(mf._AudioClient)
    ptr = _Deref(_Deref(audio_client_struct))
    with pytest.raises(RuntimeError) as excinfo:
        mf._AudioClient.__init__(
            client, ptr, samplerate=48000, channels=2, blocksize=480, isloopback=False
        )

    message = str(excinfo.value)
    assert "0x1" in message
    # Never got as far as Initialize with a format we don't understand.
    assert lpVtbl.initialize_calls == []


def test_install_is_idempotent(monkeypatch):
    """Calling the installer twice (as import_soundcard() and
    SoundcardSource.open() both do, belt-and-suspenders) must not re-wrap or
    otherwise disturb an already-patched _AudioClient."""
    soundcard, mf, _audio_client_struct, _lpVtbl = _make_fake_soundcard(0xFFFE)

    soundcard_source._install_wasapi_mix_format_patch(soundcard)
    patched_once = mf._AudioClient.__init__
    assert getattr(patched_once, "_meeting_notes_patched", False) is True

    soundcard_source._install_wasapi_mix_format_patch(soundcard)
    assert mf._AudioClient.__init__ is patched_once  # not re-wrapped


def test_noop_off_windows(monkeypatch):
    monkeypatch.setattr(soundcard_source.sys, "platform", "linux")
    soundcard, mf, _audio_client_struct, _lpVtbl = _make_fake_soundcard(0x3)
    original_init = mf._AudioClient.__init__

    soundcard_source._install_wasapi_mix_format_patch(soundcard)

    assert mf._AudioClient.__init__ is original_init


def test_noop_when_soundcard_has_no_mediafoundation(monkeypatch):
    soundcard = SimpleNamespace()  # e.g. a non-Windows soundcard build
    # Must not raise just because there's nothing to patch.
    soundcard_source._install_wasapi_mix_format_patch(soundcard)
