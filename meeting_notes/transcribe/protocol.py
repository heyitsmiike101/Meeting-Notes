"""The seam between this package and a real transcription backend.

Why a registry instead of importing backends directly: ``faster-whisper`` pulls
in torch/ctranslate2 and is a large, optional install. Nothing in this package
should require it just to be imported -- a person who only wants to *record*
meetings (never transcribe them) shouldn't need it on disk, and our own tests
must be able to import this module without it being installed. So backend
modules are only imported lazily, inside ``get_transcriber``, at the moment a
caller actually asks for that specific backend by name.

Everything downstream of this module (merging, rendering) is expressed in
terms of ``Segment`` and the ``Transcriber`` protocol, the same seam pattern as
``meeting_notes.audio.source`` uses for capture.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol, runtime_checkable


@dataclass
class Segment:
    """One transcribed utterance, in seconds into ITS OWN track's WAV file.

    ``merge.merge_tracks`` is what converts these onto the shared session
    timeline -- a bare Segment doesn't know anything about wall-clock time.
    """

    start: float
    end: float
    text: str
    track: str = ""


@runtime_checkable
class Transcriber(Protocol):
    """Anything that can turn one track's WAV into a list of segments."""

    def transcribe(self, wav_path: Path, track: str) -> list[Segment]: ...


class BackendUnavailableError(RuntimeError):
    """Raised when a named backend can't actually be used right now."""


# name -> zero/kwarg factory returning a Transcriber. Populated eagerly for the
# built-in "null" backend, and lazily (via _LAZY_MODULES) for real backends,
# so importing this module never imports a heavy optional dependency.
_REGISTRY: dict[str, Callable[..., Transcriber]] = {}

# name -> dotted module path. Importing that module is expected to call
# ``register()`` for the name(s) it provides (see faster_whisper_backend.py).
_LAZY_MODULES: dict[str, str] = {
    "faster-whisper": "meeting_notes.transcribe.faster_whisper_backend",
}


def register(name: str, factory: Callable[..., Transcriber]) -> None:
    """Register a transcriber factory under ``name``.

    A backend module calls this itself when imported, rather than this module
    importing the backend -- that's what keeps resolution lazy.
    """
    _REGISTRY[name] = factory


def available_backends() -> list[str]:
    """Every backend name that could be resolved, whether or not it's loaded yet."""
    return sorted(set(_REGISTRY) | set(_LAZY_MODULES))


def get_transcriber(name: str, **kwargs) -> Transcriber:
    """Resolve a backend by name and construct it.

    Only imports the backend's module (and therefore its heavy dependencies)
    at this point, and only for the requested name.
    """
    if name not in _REGISTRY and name in _LAZY_MODULES:
        importlib.import_module(_LAZY_MODULES[name])
    if name not in _REGISTRY:
        raise KeyError(
            f"Unknown transcription backend {name!r}. "
            f"Available backends: {', '.join(available_backends())}"
        )
    return _REGISTRY[name](**kwargs)


def _null_factory(**_kwargs) -> Transcriber:
    real = sorted(set(available_backends()) - {"null"})
    raise BackendUnavailableError(
        "No transcription backend is configured (using the 'null' backend, "
        "which never produces text). "
        f"Real backends available: {', '.join(real) or '(none registered)'}. "
        "Install one with: pip install 'meeting-notes[whisper]' "
        "then select it with backend='faster-whisper'."
    )


# The default backend: always present, never silently pretends to work.
register("null", _null_factory)
