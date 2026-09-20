"""A fake AudioSource, so the whole capture pipeline is testable with no hardware.

This is the payoff of keeping ``soundcard`` behind the AudioSource protocol: the
failure modes that matter most -- a device that wedges forever, a device that
disappears mid-meeting, a device whose clock runs fast -- are all trivial to
provoke here and nearly impossible to provoke on purpose with real hardware.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Optional

import numpy as np

# Set by tests at teardown to release any thread parked in a simulated stall,
# so a wedged fake never outlives the test run.
RELEASE = threading.Event()


class FakeReader:
    def __init__(self, source: "FakeSource"):
        self.source = source
        self.reads = 0
        self._phase = 0

    def read(self, numframes: int) -> np.ndarray:
        src = self.source
        self.reads += 1

        if src.raise_after is not None and self.reads > src.raise_after:
            raise OSError(f"{src.name}: device disconnected")

        if src.stall_after is not None and self.reads > src.stall_after:
            # Mimic a native blocking read that never returns. Nothing in the
            # recorder is allowed to hang because of this.
            RELEASE.wait(timeout=src.stall_seconds)
            if not RELEASE.is_set():
                RELEASE.wait(timeout=src.stall_seconds)

        if src.realtime:
            # rate_scale > 1 means the device delivers frames faster than its
            # nominal rate claims, i.e. positive clock drift.
            time.sleep(numframes / (src.samplerate * src.rate_scale))

        idx = np.arange(self._phase, self._phase + numframes, dtype=np.float32)
        self._phase += numframes
        mono = src.amplitude * np.sin(2 * np.pi * src.freq * idx / src.samplerate)
        return np.tile(mono.reshape(-1, 1), (1, src.channels)).astype(np.float32)


class FakeSource:
    def __init__(
        self,
        name: str = "Fake Device",
        channels: int = 2,
        samplerate: int = 1000,
        *,
        freq: float = 50.0,
        amplitude: float = 0.5,
        realtime: bool = True,
        rate_scale: float = 1.0,
        stall_after: Optional[int] = None,
        raise_after: Optional[int] = None,
        stall_seconds: float = 30.0,
    ):
        self._name = name
        self._channels = channels
        self._samplerate = samplerate
        self.freq = freq
        self.amplitude = amplitude
        self.realtime = realtime
        self.rate_scale = rate_scale
        self.stall_after = stall_after
        self.raise_after = raise_after
        self.stall_seconds = stall_seconds
        self.opens = 0
        self.readers: list = []

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
    def open(self):
        self.opens += 1
        reader = FakeReader(self)
        self.readers.append(reader)
        try:
            yield reader
        finally:
            pass


class HealsOnReopenSource(FakeSource):
    """Fails on the first open, then works -- a USB mic being plugged back in."""

    @contextmanager
    def open(self):
        self.opens += 1
        if self.opens == 1:
            raise OSError(f"{self.name}: cannot open device")
        reader = FakeReader(self)
        self.readers.append(reader)
        yield reader


class RightChannelOnlyReader(FakeReader):
    """Signal on the right channel, digital silence on the left."""

    def read(self, numframes: int) -> np.ndarray:
        block = super().read(numframes)
        block[:, 0] = 0.0
        return block


class RightChannelOnlySource(FakeSource):
    """Proves we are not silently keeping only the left channel.

    This is the shape of the ``soundcard`` channels=1 trap: asking the backend
    for one channel yields ``data[:, [0]]``, so audio panned right vanishes with
    no error. Recording this source must still produce audible samples.
    """

    @contextmanager
    def open(self):
        self.opens += 1
        reader = RightChannelOnlyReader(self)
        self.readers.append(reader)
        yield reader
