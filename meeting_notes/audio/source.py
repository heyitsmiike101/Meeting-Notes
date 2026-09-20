"""The single seam between this package and a real audio backend.

Everything downstream of this module (recording, drift correction, WAV writing)
is expressed in terms of these two protocols, so the whole capture pipeline can
be exercised in tests against a fake source with no audio hardware present.

``meeting_notes.audio.soundcard_source`` is the only module in the package that
is allowed to import ``soundcard``.
"""

from __future__ import annotations

from typing import ContextManager, Protocol, runtime_checkable

import numpy as np


@runtime_checkable
class Reader(Protocol):
    """An open capture stream."""

    def read(self, numframes: int) -> np.ndarray:
        """Return up to ``numframes`` frames as a ``(frames, channels)`` float32 array.

        May return fewer frames than requested. May block. May raise if the
        underlying device disappears -- callers are expected to treat any
        exception as "this device is gone" rather than as a bug.
        """
        ...


@runtime_checkable
class AudioSource(Protocol):
    """A device that can be opened for capture."""

    @property
    def name(self) -> str: ...

    @property
    def channels(self) -> int: ...

    @property
    def samplerate(self) -> int:
        """The rate frames are delivered at once opened."""
        ...

    def open(self) -> ContextManager[Reader]: ...
