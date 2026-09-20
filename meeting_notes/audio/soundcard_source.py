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

import warnings
from contextlib import contextmanager
from typing import Iterator, Optional

import numpy as np


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
        return False, f"{type(exc).__name__}: {exc}"
    return True, ""


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
        self._warnings: list = []

    def read(self, numframes: int) -> np.ndarray:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            data = self._recorder.record(numframes=numframes)
            for w in caught:
                self._warnings.append(str(w.message))
        arr = np.asarray(data, dtype=np.float32)
        if arr.ndim == 1:
            arr = arr.reshape(-1, 1)
        return arr

    def drain_warnings(self) -> list:
        """Return and clear any warnings captured since the last drain."""
        out, self._warnings = self._warnings, []
        return out


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
