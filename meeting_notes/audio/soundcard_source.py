"""The only module in this package allowed to ``import soundcard``.

Everything else in ``meeting_notes`` talks to audio hardware exclusively
through the :class:`~meeting_notes.audio.source.AudioSource` /
:class:`~meeting_notes.audio.source.Reader` protocols, so the capture
pipeline can be tested with a fake source on a machine that has no audio
libraries installed at all (this repo's own test box included). To keep that
true, ``soundcard`` is imported lazily inside each function here rather than
at module scope -- a module-level import would make this file, and anything
that imports it, fail to even load on such a machine.
"""

from __future__ import annotations

import collections.abc
import sys
import threading
import warnings
from contextlib import contextmanager
from typing import Iterator, Optional

import numpy as np

from .track_recorder import describe_error

# Warning capture is process-global and keyed by thread, NOT done with
# warnings.catch_warnings() around each read. catch_warnings mutates global
# state and is explicitly not thread-safe: with one of these per track thread,
# their enter/exit interleave and the restore rebinds the hook to a discarded
# buffer, so the dropout warnings this exists to collect get silently lost --
# precisely when something is going wrong and you most want to know.
_WARN_LOCK = threading.Lock()
_WARN_BUFFERS: dict = {}
_WARN_HOOK_INSTALLED = False

# Guards installing the WASAPI mix-format monkeypatch below (see
# _install_wasapi_mix_format_patch), the same way _WARN_LOCK guards the
# warning hook: several threads can each try to open a device for the first
# time concurrently (mic + loopback each get their own capture thread), and
# the patch install must happen exactly once and be safe to race on.
_WASAPI_PATCH_LOCK = threading.Lock()


def _install_warning_hook() -> None:
    global _WARN_HOOK_INSTALLED
    with _WARN_LOCK:
        if _WARN_HOOK_INSTALLED:
            return
        previous = warnings.showwarning

        def showwarning(message, category, filename, lineno, file=None, line=None):
            if "soundcard" in str(filename):
                ident = threading.get_ident()
                with _WARN_LOCK:
                    _WARN_BUFFERS.setdefault(ident, []).append(str(message))
                return
            previous(message, category, filename, lineno, file, line)

        warnings.showwarning = showwarning
        # Without this, Python's default "once per location" rule reports a
        # recurring dropout a single time for the whole meeting.
        warnings.filterwarnings("always", module="soundcard")
        _WARN_HOOK_INSTALLED = True


def import_soundcard():
    """The one sanctioned way for the rest of the package to reach ``soundcard``.

    ``meeting_notes.audio.devices`` needs the module-level API (
    ``all_microphones``, ``all_speakers``, ``default_microphone``,
    ``default_speaker``) to enumerate hardware, but this file is the only
    place allowed to say ``import soundcard``. Routing through here keeps
    that contract true while still letting device discovery live in its own
    module. Raises the normal ``ImportError`` if the backend isn't installed;
    callers that need a soft failure should check :func:`soundcard_available`
    first.
    """
    import soundcard

    # Every caller reaches soundcard through here, so this is the one place
    # that is guaranteed to run before anything opens a device -- the natural
    # spot to install the WASAPI workaround below.
    _install_wasapi_mix_format_patch(soundcard)
    return soundcard


def soundcard_available() -> "tuple[bool, str]":
    """Probe whether ``soundcard`` can be imported. Never raises.

    Used by ``doctor`` to report a clean, actionable message instead of a
    traceback when the backend (or one of its native dependencies, e.g.
    PortAudio-less platforms) is missing.
    """
    try:
        import soundcard  # noqa: F401
    except Exception as exc:  # pragma: no cover - exact exception type varies by platform
        return False, describe_error(exc)
    return True, ""


def _install_wasapi_mix_format_patch(soundcard) -> None:
    """Work around bastibe/SoundCard#93 on Windows.

    ``soundcard`` 0.4.6's ``mediafoundation._AudioClient.__init__`` hard-
    asserts that WASAPI's ``GetMixFormat`` returned a ``WAVEFORMATEXTENSIBLE``
    (``wFormatTag == 0xFFFE``) and dies with a bare, unhelpful
    ``AssertionError`` otherwise. In practice some devices' *mix* format --
    distinct from any format they're capable of, this is just what the OS
    happens to be resampling through right now -- is a plain ``WAVEFORMATEX``
    instead, reported as ``wFormatTag == 0x3`` (``WAVE_FORMAT_IEEE_FLOAT``).
    Observed on this project's own test hardware: a Logitech C922 mic reports
    a 0x3 mix format (48kHz/32-bit stereo, cbSize=0), while the WASAPI
    loopback device on the same machine reports 0xFFFE and works fine as-is.
    Upstream has not picked up a fix.

    This replaces ``_AudioClient.__init__`` with a version that handles both
    tags, keeping the 0xFFFE path bit-for-bit identical (module the harmless
    ``SubFormat`` sanity asserts, which read fields nothing downstream uses)
    and adding an equivalent path for 0x3. Any other tag is refused with a
    clear error rather than silently reinterpreting a struct layout nobody
    has seen in practice.

    Windows-only, and a no-op if this ``soundcard`` build has no
    ``mediafoundation`` (i.e. no WASAPI backend) to patch. Installed
    idempotently -- see the marker attribute below -- so it is safe to call
    from every entry point that might be the first to open a device.
    """
    if sys.platform != "win32":
        return
    mf = getattr(soundcard, "mediafoundation", None)
    if mf is None:
        return

    with _WASAPI_PATCH_LOCK:
        if getattr(mf._AudioClient.__init__, "_meeting_notes_patched", False):
            return  # another thread (or an earlier call) already installed it

        _ffi = mf._ffi
        _com = mf._com
        _ole32 = mf._ole32

        def patched_init(self, ptr, samplerate, channels, blocksize, isloopback, exclusive_mode=False):
            self._ptr = ptr

            # Identical to soundcard's own validation: channel maps on WASAPI
            # must be a contiguous range(0, x) -- see soundcard's own comment
            # on the equivalent check this replaces.
            if isinstance(channels, int):
                self.channelmap = list(range(channels))
            elif isinstance(channels, collections.abc.Iterable):
                self.channelmap = channels
            else:
                raise TypeError("channels must be iterable or integer")

            if list(range(len(set(self.channelmap)))) != sorted(set(self.channelmap)):
                raise TypeError(
                    "Due to limitations of WASAPI, channel maps on Windows "
                    "must be a combination of `range(0, x)`."
                )

            if blocksize is None:
                blocksize = self.deviceperiod[0] * samplerate

            ppMixFormat = _ffi.new("WAVEFORMATEXTENSIBLE**")
            hr = self._ptr[0][0].lpVtbl.GetMixFormat(self._ptr[0], ppMixFormat)
            _com.check_error(hr)

            tag = ppMixFormat[0][0].Format.wFormatTag
            if tag == 0xFFFE:
                # WAVEFORMATEXTENSIBLE with room for
                # KSDATAFORMAT_SUBTYPE_IEEE_FLOAT. wValidBitsPerSample is the
                # one field here that actually matters downstream; the
                # SubFormat GUID asserts soundcard used to make alongside it
                # were opportunistic sanity checks on values nothing reads.
                ppMixFormat[0][0].Samples = dict(wValidBitsPerSample=32)
            elif tag != 0x3:
                # Nothing else has turned up on real hardware. Refuse rather
                # than reinterpret a struct layout we've never validated.
                raise RuntimeError(
                    "unsupported WASAPI mix format tag 0x%X for this device "
                    "(expected 0xFFFE WAVE_FORMAT_EXTENSIBLE or 0x3 "
                    "WAVE_FORMAT_IEEE_FLOAT); requested channels=%r "
                    "isloopback=%r" % (tag, channels, isloopback)
                )
            # else tag == 0x3 (a plain WAVEFORMATEX, WAVE_FORMAT_IEEE_FLOAT):
            # the struct already has everything Initialize() needs once the
            # rate/channel overrides below are applied, same as the 0xFFFE case.

            nchannels = len(set(self.channelmap))
            fmt = ppMixFormat[0][0].Format
            fmt.nChannels = nchannels
            fmt.nSamplesPerSec = int(samplerate)
            fmt.nAvgBytesPerSec = int(samplerate) * nchannels * 4
            fmt.nBlockAlign = nchannels * 4
            fmt.wBitsPerSample = 32
            # dwChannelMask: left unset, matching soundcard's own comment
            # ("does not work") on the equivalent line it never enabled either.

            if exclusive_mode:
                sharemode = _ole32.AUDCLNT_SHAREMODE_EXCLUSIVE
            else:
                sharemode = _ole32.AUDCLNT_SHAREMODE_SHARED
            #             resample   | remix      | better-SRC | nopersist
            streamflags = 0x00100000 | 0x80000000 | 0x08000000 | 0x00080000
            if isloopback:
                streamflags |= 0x00020000  # loopback
            bufferduration = int(blocksize / samplerate * 10000000)  # hecto-nanoseconds
            hr = self._ptr[0][0].lpVtbl.Initialize(
                self._ptr[0], sharemode, streamflags, bufferduration, 0, ppMixFormat[0], _ffi.NULL
            )
            _com.check_error(hr)
            _ole32.CoTaskMemFree(ppMixFormat[0])

            # save samplerate for later
            self.samplerate = samplerate
            # placeholder for the last time we had audio input available
            self._idle_start_time = None

        patched_init._meeting_notes_patched = True
        mf._AudioClient.__init__ = patched_init


class _SoundcardReader:
    """Wraps an open ``soundcard`` recorder as a :class:`Reader`.

    ``soundcard.record()`` emits Python warnings (notably "data discontinuity
    in recording", raised when the OS audio callback falls behind) rather
    than exceptions. Letting those scroll past on stderr mid-meeting is easy
    to miss, so every call is captured with ``warnings.catch_warnings`` and
    buffered here for the caller to drain and log on its own schedule.
    """

    def __init__(self, recorder, channels: int):
        self._recorder = recorder
        self._channels = channels
        _install_warning_hook()

    def read(self, numframes: int) -> np.ndarray:
        data = self._recorder.record(numframes=numframes)
        arr = np.asarray(data, dtype=np.float32)
        if arr.ndim == 1:
            arr = arr.reshape(-1, 1)
        return arr

    def drain_warnings(self) -> list:
        """Return and clear warnings raised on THIS thread since the last drain.

        Keyed by thread because each track records on its own thread, so this
        hands each recorder only its own device's dropouts.
        """
        with _WARN_LOCK:
            return _WARN_BUFFERS.pop(threading.get_ident(), [])


class SoundcardSource:
    """An :class:`AudioSource` backed by a ``soundcard`` microphone object.

    ``mic`` is whatever ``soundcard`` handed back from ``all_microphones()``
    or ``default_microphone()`` -- on Windows this may itself be an output
    device wrapped for WASAPI loopback (see ``devices.py``), which from
    ``soundcard``'s point of view is just another microphone-shaped object.
    """

    def __init__(
        self,
        mic,
        *,
        name: str,
        channels: int,
        samplerate: int,
        blocksize: Optional[int] = None,
    ):
        self._mic = mic
        self._name = name
        self._channels = channels
        self._samplerate = samplerate
        self._blocksize = blocksize

    @property
    def name(self) -> str:
        return self._name

    @property
    def channels(self) -> int:
        return self._channels

    @property
    def samplerate(self) -> int:
        return self._samplerate

    @contextmanager
    def open(self) -> Iterator[_SoundcardReader]:
        # Belt-and-suspenders: normally the patch is already installed by the
        # time anyone has a mic object at all, since that object could only
        # have come from import_soundcard() in the first place (devices.py's
        # only way to reach soundcard). Repeating it here is cheap -- it's a
        # sys.modules-cached import plus an idempotent no-op check -- and
        # protects a SoundcardSource built some other way, e.g. directly in a
        # test, from hitting the bare AssertionError this exists to avoid.
        _install_wasapi_mix_format_patch(import_soundcard())

        # Deliberately NOT channels=1: soundcard treats the ``channels``
        # argument to recorder() as a channel *map*, not a downmix request.
        # Internally it does ``data[:, self.channelmap]``, so channels=1 on a
        # stereo device selects the left channel only and silently discards
        # the right. We always open at the device's native channel count and
        # let wav_io.downmix_mono() do the averaging, in this process, where
        # we can see it happen.
        kwargs = dict(samplerate=self._samplerate, channels=self._channels)
        if self._blocksize is not None:
            kwargs["blocksize"] = self._blocksize
        with self._mic.recorder(**kwargs) as recorder:
            yield _SoundcardReader(recorder, self._channels)
