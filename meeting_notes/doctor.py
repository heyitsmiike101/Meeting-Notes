"""Pre-flight diagnostics for the audio capture pipeline.

Meant to be run once before a real meeting, not on every recording: several
of these checks open a device and read from it, which is exactly the kind of
thing you don't want happening silently in the background of a session that
someone is depending on.

The checks are ordered so a hard blocker (no ``soundcard``) short-circuits
everything downstream instead of producing a wall of identical, confusing
failures -- see ``run_doctor``.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import List, Optional

import numpy as np

from .audio import devices, soundcard_source


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    fix: str = ""


def _check_soundcard_importable() -> Check:
    ok, reason = soundcard_source.soundcard_available()
    if ok:
        return Check("soundcard importable", True, "soundcard is installed and importable.")
    detail = f"import soundcard failed: {reason}" if reason else "import soundcard failed."
    return Check(
        "soundcard importable",
        False,
        detail,
        fix="pip install soundcard (on Linux it links against libpulse/libasound; "
        "install their -dev packages if the build fails).",
    )


def _check_microphones() -> Check:
    try:
        mics = devices.list_microphones()
    except Exception as exc:  # a real bug, not a "no hardware" situation
        return Check("microphone found", False, f"error while listing microphones: {exc}")
    if mics:
        names = ", ".join(m.name for m in mics[:5])
        return Check("microphone found", True, f"{len(mics)} microphone(s) found: {names}")
    return Check(
        "microphone found",
        False,
        "no microphones were found.",
        fix="Connect/enable a microphone and check the OS sound settings.",
    )


def _check_system_source() -> Check:
    try:
        sources = devices.list_system_sources()
    except Exception as exc:
        return Check("system-audio source found", False, f"error while listing system sources: {exc}")
    if sources:
        names = ", ".join(s.name for s in sources[:5])
        return Check("system-audio source found", True, f"{len(sources)} source(s) found: {names}")

    # Empty is expected and actionable on mac (install a driver); on Windows
    # it means something is actually wrong, since loopback needs no driver.
    if sys.platform == "darwin":
        return Check(
            "system-audio source found",
            False,
            "no virtual loopback driver detected (looked for BlackHole/Soundflower/"
            "Loopback Audio/Existential Audio in the input device list).",
            fix="Install BlackHole 2ch: https://existential.audio/blackhole/ -- a "
            "one-time install that needs admin rights -- then re-run doctor.",
        )
    if sys.platform == "win32":
        return Check(
            "system-audio source found",
            False,
            "no loopback-capable output device found via "
            "soundcard.all_microphones(include_loopback=True). This is "
            "unexpected on Windows, which needs no driver for this.",
            fix="Check that an output device is enabled in Windows Sound settings "
            "and that the soundcard package is up to date.",
        )
    return Check("system-audio source found", False, devices.system_source_platform_note())


def _check_macos_default_output_not_bare_blackhole() -> Optional[Check]:
    """The silent-failure trap: recording succeeds perfectly and the user
    simply never hears the meeting, because nothing about that ever raises.

    It only breaks the meeting when BlackHole is the *entire* output rather
    than one leg of a Multi-Output Device that also reaches real speakers,
    so a name check alone isn't enough -- "Multi-Output" in the default
    device's name means BlackHole is there for capture while sound still
    reaches the user.
    """
    if sys.platform != "darwin":
        return None
    name = "system-audio: default output is not bare BlackHole"
    try:
        sc = soundcard_source.import_soundcard()
        speaker_name = str(sc.default_speaker().name)
    except Exception as exc:
        return Check(name, False, f"could not determine the default output device: {exc}")

    lname = speaker_name.lower()
    if "blackhole" not in lname:
        return Check(name, True, f"default output device is {speaker_name!r}.")
    if "multi-output" in lname or "multi output" in lname:
        return Check(
            name,
            True,
            f"default output is a Multi-Output Device ({speaker_name!r}) that includes BlackHole.",
        )
    return Check(
        name,
        False,
        f"default output device is {speaker_name!r} -- BlackHole alone, not part of "
        "a Multi-Output Device. Recording will capture perfectly and you will hear "
        "nothing during the meeting, with no error anywhere.",
        fix="Open Audio MIDI Setup, create a Multi-Output Device containing both "
        "BlackHole and your real speakers/headphones, and set THAT Multi-Output "
        "Device as the system output -- not BlackHole by itself.",
    )


def _check_macos_mic_permission() -> Optional[Check]:
    """A brief real capture, not just device enumeration, because TCC only
    fails at capture time, not at open() or in the device list.

    macOS ties the grant to the exact executable that calls into CoreAudio,
    so a pyenv shim, this venv's python, and a packaged .app binary are each
    a separate identity to Privacy & Security -- granting one does not grant
    the others, which is the most common cause of a permission failure here.
    """
    if sys.platform != "darwin":
        return None
    name = "microphone permission"
    try:
        mics = devices.list_microphones()
        if not mics:
            return Check(name, False, "no microphone available to probe.")
        default_mic = next((m for m in mics if m.is_default), mics[0])
        source = devices.resolve_source("mic", default_mic.id)
        with source.open() as reader:
            reader.read(max(1, int(source.samplerate * 0.1)))
        return Check(name, True, f"captured briefly from {default_mic.name!r} with no error.")
    except Exception as exc:
        return Check(
            name,
            False,
            f"microphone capture failed: {exc}",
            fix="Grant access in System Settings > Privacy & Security > Microphone "
            "for the specific executable running this (the shim/venv/binary "
            "identities are each separate grants).",
        )


def _level_probe(kind: str) -> Check:
    """Capture ~1s and report peak amplitude, so 'no error' can be told apart
    from 'device opened fine but nothing is actually coming through it'."""
    label = f"{'microphone' if kind == 'mic' else 'system-audio'} level"
    try:
        source = devices.resolve_source(kind)
    except Exception as exc:
        return Check(label, False, f"could not resolve a {kind} device: {exc}")

    try:
        target_frames = max(1, int(source.samplerate * 1.0))
        chunks: list = []
        got = 0
        with source.open() as reader:
            # Bounded iteration count: a misbehaving backend that returns
            # zero frames forever must not hang doctor.
            for _ in range(50):
                if got >= target_frames:
                    break
                block = reader.read(min(4096, target_frames - got))
                if block is None or len(block) == 0:
                    continue
                chunks.append(block)
                got += len(block)
            warnings_seen = reader.drain_warnings()
    except Exception as exc:
        return Check(label, False, f"capture from {source.name!r} failed: {exc}")

    if not chunks:
        return Check(label, False, f"{source.name!r} returned no audio frames.")

    data = np.concatenate(chunks, axis=0)
    peak = float(np.max(np.abs(data))) if data.size else 0.0
    detail = f"{source.name!r}: peak amplitude {peak:.4f} over {got} frame(s)."
    if warnings_seen:
        detail += f" ({len(warnings_seen)} discontinuity warning(s) during capture)"

    if peak <= 0.0005:
        return Check(
            label,
            False,
            detail + " Essentially silent.",
            fix="Make sure the device isn't muted and that audio is actually "
            "playing/speaking during the probe, then re-run doctor.",
        )
    return Check(label, True, detail)


def _check_transcription() -> Check:
    """Whether text output will work, checked before a meeting rather than after.

    Model downloads are 145MB-1.6GB and happen on first use. Discovering that
    at the moment you want a transcript -- possibly offline -- is exactly the
    wrong time, which is why this reports cached models too.
    """
    name = "transcription backend"
    try:
        import faster_whisper  # noqa: F401
    except Exception:
        return Check(
            name,
            False,
            "faster-whisper is not installed, so recordings cannot be turned into text.",
            fix="pip install 'meeting-notes[whisper]'",
        )

    try:
        from meeting_notes.transcribe.faster_whisper_backend import (
            MODEL_CHOICES,
            model_is_downloaded,
        )

        cached = [m for m in MODEL_CHOICES if model_is_downloaded(m)]
    except Exception as exc:
        return Check(name, False, f"faster-whisper is installed but unusable: {exc}")

    if cached:
        return Check(name, True, f"faster-whisper ready; models cached: {', '.join(cached)}")
    return Check(
        name,
        False,
        "faster-whisper is installed but no model is cached yet; the first "
        "transcription will download one.",
        fix="Pre-fetch before your next meeting: meeting-notes models --download base.en",
    )


def run_doctor() -> List[Check]:
    checks: List[Check] = []

    sc_check = _check_soundcard_importable()
    checks.append(sc_check)
    if not sc_check.ok:
        # Every check below needs soundcard; report that once instead of a
        # wall of identical "soundcard not importable" failures.
        skip = "skipped: soundcard is not importable."
        checks.append(Check("microphone found", False, skip))
        checks.append(Check("system-audio source found", False, skip))
        if sys.platform == "darwin":
            checks.append(Check("system-audio: default output is not bare BlackHole", False, skip))
            checks.append(Check("microphone permission", False, skip))
        checks.append(Check("microphone level", False, skip))
        checks.append(Check("system-audio level", False, skip))
        # Transcription is independent of the audio backend, so still report it.
        checks.append(_check_transcription())
        return checks

    checks.append(_check_microphones())
    checks.append(_check_system_source())

    blackhole_check = _check_macos_default_output_not_bare_blackhole()
    if blackhole_check is not None:
        checks.append(blackhole_check)

    permission_check = _check_macos_mic_permission()
    if permission_check is not None:
        checks.append(permission_check)

    checks.append(_level_probe("mic"))
    checks.append(_level_probe("system"))
    checks.append(_check_transcription())

    return checks


def format_report(checks: List[Check]) -> str:
    """Readable console output with OK/WARN/FAIL markers.

    ``Check`` has no separate severity field, so the marker is derived: a
    failure that carries a ``fix`` is something the user can act on (WARN);
    a failure with none is unexpected -- a real error surfaced verbatim,
    worth flagging louder (FAIL).
    """
    lines = []
    for c in checks:
        if c.ok:
            marker = "OK  "
        elif c.fix:
            marker = "WARN"
        else:
            marker = "FAIL"
        lines.append(f"[{marker}] {c.name}: {c.detail}")
        if not c.ok and c.fix:
            lines.append(f"       fix: {c.fix}")
    return "\n".join(lines)
