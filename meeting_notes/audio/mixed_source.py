"""One track recorded from several output devices at once, mixed.

Why this exists: a Bluetooth headset on Windows shows up as two output devices,
``Headphones (X)`` (music quality, A2DP) and ``Headset (X)`` (the hands-free
profile a call uses). When a meeting app opens the headset's microphone, Windows
switches the earbuds to the hands-free profile and the call plays through
``Headset (X)``; recording ``Headphones (X)`` alone then hears nothing of the
other side. Only one of the two carries sound at a time, so recording both and
adding them up gives the other side whichever profile is in use.

The first source is the one that was chosen; it sets the track's rate and its
name, and losing it ends the stream like any device loss. The others are extras:
one that will not open or that fails mid-meeting is dropped with a log line and
the track carries on from the rest.
"""

from __future__ import annotations

import logging
from contextlib import ExitStack, contextmanager
from typing import Iterator, List, Optional, Sequence

import numpy as np

log = logging.getLogger("meeting_notes.audio.mixed_source")


def _mono(block) -> np.ndarray:
    arr = np.asarray(block, dtype=np.float32)
    if arr.ndim == 1:
        return arr
    if arr.shape[1] == 1:
        return arr[:, 0]
    return arr.mean(axis=1)


class _MixedReader:
    def __init__(self, primary, extras: Sequence[tuple]):
        self._primary = primary
        # (name, reader, leftover mono samples read ahead of the primary)
        self._extras: List[list] = [[name, reader, np.zeros(0, dtype=np.float32)] for name, reader in extras]

    def read(self, numframes: int) -> np.ndarray:
        mixed = _mono(self._primary.read(numframes)).copy()
        count = len(mixed)
        if count == 0:
            return mixed.reshape(-1, 1)
        for extra in list(self._extras):
            name, reader, pending = extra
            if len(pending) < count:
                try:
                    pending = np.concatenate([pending, _mono(reader.read(count - len(pending)))])
                except Exception as exc:  # noqa: BLE001 - an extra device going away must not end the track
                    log.warning("stopped also recording %s: %s", name, exc)
                    self._extras.remove(extra)
                    continue
            take = pending[:count]
            if len(take) < count:
                take = np.concatenate([take, np.zeros(count - len(take), dtype=np.float32)])
            mixed += take
            extra[2] = pending[count:]
        np.clip(mixed, -1.0, 1.0, out=mixed)
        return mixed.reshape(-1, 1)

    @property
    def extra_names(self) -> List[str]:
        return [name for name, _reader, _pending in self._extras]

    def drain_warnings(self) -> list:
        messages: list = []
        for reader in [self._primary] + [reader for _name, reader, _pending in self._extras]:
            drain = getattr(reader, "drain_warnings", None)
            if drain is not None:
                messages.extend(drain())
        return messages


class MixedSource:
    """An :class:`~meeting_notes.audio.source.AudioSource` that records ``primary`` plus ``extras``, mixed to mono."""

    def __init__(self, primary, extras: Sequence[object]):
        self.primary = primary
        self.extras = list(extras)

    @property
    def name(self) -> str:
        # The chosen device's name: device matching and the "device replaced" notices compare against it.
        return self.primary.name

    @property
    def channels(self) -> int:
        return 1

    @property
    def samplerate(self) -> int:
        return self.primary.samplerate

    @property
    def extra_names(self) -> List[str]:
        return [extra.name for extra in self.extras]

    @contextmanager
    def open(self) -> Iterator[_MixedReader]:
        with ExitStack() as stack:
            primary = stack.enter_context(self.primary.open())
            opened = []
            for extra in self.extras:
                try:
                    opened.append((extra.name, stack.enter_context(extra.open())))
                except Exception as exc:  # noqa: BLE001 - record what can be recorded
                    log.warning("could not also record %s: %s", extra.name, exc)
            if opened:
                log.info("recording %s together with %s", self.primary.name, ", ".join(n for n, _ in opened))
            yield _MixedReader(primary, opened)


def call_profile_siblings(chosen_name: str, names: Sequence[str]) -> List[str]:
    """The other output devices of the same Bluetooth headset as ``chosen_name``, when it has a call profile.

    Windows names a headset's outputs ``Headphones (X)`` and ``Headset (X)`` (older drivers: ``Headset Earphone
    (X)``, ``Hands-Free ...``). Devices only group when they share the ``(X)`` part and one of them is the call
    (hands-free) profile, so ``Speakers (Realtek)`` and ``Line (Realtek)`` stay separate."""
    chosen = _split(chosen_name)
    if chosen is None:
        return []
    group = [n for n in names if n != chosen_name and (_split(n) or ("", None))[1] == chosen[1]]
    if not group:
        return []
    prefixes = [chosen[0]] + [_split(n)[0] for n in group]
    if not any(_is_call_profile(p) for p in prefixes):
        return []
    return group


def is_call_profile_name(name: str) -> bool:
    """Whether ``name`` is a Bluetooth headset's call-profile endpoint ("Headset (X)", "Hands-Free (X)")."""
    parts = _split(name)
    return parts is not None and _is_call_profile(parts[0])


def _split(name: str) -> Optional[tuple]:
    name = str(name or "").strip()
    if not name.endswith(")") or "(" not in name:
        return None
    cut = name.index("(")
    prefix, device = name[:cut].strip().lower(), name[cut + 1: -1].strip().lower()
    if not device:
        return None
    return prefix, device


def _is_call_profile(prefix: str) -> bool:
    return any(word in prefix for word in ("headset", "hands-free", "handsfree"))
