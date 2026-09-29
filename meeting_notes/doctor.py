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
from pathlib import Path
from typing import List, Optional

import numpy as np

from .audio import devices, soundcard_source
from .audio.track_recorder import describe_error


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
        return Check("microphone found", False, f"error while listing microphones: {describe_error(exc)}")
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
        return Check("system-audio source found", False, f"error while listing system sources: {describe_error(exc)}")
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
        return Check(name, False, f"could not determine the default output device: {describe_error(exc)}")

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
            f"microphone capture failed: {describe_error(exc)}",
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
        return Check(label, False, f"could not resolve a {kind} device: {describe_error(exc)}")

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
        return Check(label, False, f"capture from {source.name!r} failed: {describe_error(exc)}")

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
        return Check(name, False, f"faster-whisper is installed but unusable: {describe_error(exc)}")

    if cached:
        return Check(name, True, f"faster-whisper ready; models cached: {', '.join(cached)}")

    # No local model cached. Whether that deserves a WARN depends on whether
    # a server is configured to do the final pass instead (see _check_server):
    # if so, a missing local model is expected and not a problem -- it is
    # only needed for the fully-local `meeting-notes transcribe` path.
    server_url = ""
    try:
        from . import config as config_mod

        server_url = (config_mod.server_settings().get("url") or "").strip()
    except Exception:
        pass  # a corrupt config is _check_server's problem to report, not this one's

    if server_url:
        return Check(
            name,
            True,
            f"no local model is cached, but server at {server_url} does the "
            "final pass; a local model is only needed for "
            "`meeting-notes transcribe`.",
        )

    return Check(
        name,
        False,
        "faster-whisper is installed but no model is cached yet; the first "
        "transcription will download one.",
        fix="Pre-fetch before your next meeting: meeting-notes models --download base.en",
    )


def _check_server() -> Check:
    """Whether the LAN transcription server (if any) is usable.

    A server is optional -- ``meeting-notes transcribe`` running locally is a
    fully supported mode on its own (see ``config.server_settings``), so "no
    server configured" is reported as OK, not as a missing piece. When a URL
    *is* configured, this distinguishes "can't reach it at all" from "reached
    it but it disagrees with us" from "reached it but our token is wrong",
    since each of those needs a different fix and lumping them into one
    generic failure would send someone chasing the wrong problem.
    """
    from . import config as config_mod
    from . import wire
    from .client.api import ServerClient, ServerUnavailable

    name = "transcription server"
    try:
        server = config_mod.server_settings()
    except Exception as exc:  # a corrupt config must not crash doctor
        return Check(name, False, f"could not read server settings: {describe_error(exc)}")

    url = (server.get("url") or "").strip()
    if not url:
        return Check(
            name,
            True,
            "no server configured; transcription will run locally via "
            "`meeting-notes transcribe`. Set a server URL in the Settings "
            "dialog if you want the LAN server to do the final pass instead.",
        )

    token = server.get("token") or None
    client = ServerClient(url, token=token, timeout=5.0)
    try:
        try:
            health = client.health()
        except ServerUnavailable as exc:
            return Check(
                name,
                False,
                f"server at {url} is unreachable: {describe_error(exc)}",
                fix="Check that the server (the Docker container) is running "
                "and reachable on the LAN -- same network, correct "
                "host/port, no firewall in the way -- then re-run doctor.",
            )
        except Exception as exc:  # unexpected shape of a reachable server's reply
            return Check(name, False, f"server at {url} returned an unexpected error: {describe_error(exc)}")

        server_protocol = health.get("protocol")
        if server_protocol != wire.PROTOCOL_VERSION:
            return Check(
                name,
                False,
                f"protocol mismatch: this client speaks version "
                f"{wire.PROTOCOL_VERSION}, server at {url} speaks version "
                f"{server_protocol!r}. The client and server are running "
                "different releases.",
                fix="Update whichever side is behind so client and server "
                "run the same meeting-notes version, then re-run doctor.",
            )

        # /health needs no auth (see server/app.py), so a healthy /health
        # response tells us nothing about whether OUR token is accepted.
        # Probe a real authenticated route instead, with a job id that can't
        # exist: a 404 means the token was accepted and the server simply
        # doesn't know this job (the expected, harmless outcome); a 401/403
        # means the token itself was rejected before the route logic ran.
        try:
            client.job("doctor-probe")
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (401, 403):
                return Check(
                    name,
                    False,
                    f"server at {url} rejected our credentials (HTTP {status}).",
                    fix="The token is wrong or missing -- set the correct one "
                    "in the Settings dialog (or MEETING_NOTES_TOKEN on the "
                    "server) and re-run doctor.",
                )
            if status != 404:
                return Check(name, False, f"server at {url} returned an unexpected error: {describe_error(exc)}")
            # else: 404 is expected -- fall through as auth-accepted.

        model = health.get("model", "?")
        device = health.get("device", "?")
        return Check(
            name, True, f"server at {url} is reachable and healthy (model={model}, device={device})."
        )
    finally:
        client.close()


def _check_upload_queue() -> Check:
    """How much is waiting to reach the server, and whether any of it is stuck.

    Zero queued is the common case and not worth a word beyond "empty";
    entries still retrying are informational; an entry that has exhausted its
    retries (``status == "failed"``) is worth flagging since otherwise it
    just sits silently in ``.upload-queue`` forever with nobody the wiser.
    """
    from . import config as config_mod
    from .client.queue import SessionQueue

    name = "upload queue"
    try:
        queue = SessionQueue.for_save_dir(config_mod.save_dir())
        entries = queue.pending()
    except Exception as exc:  # disk/permission trouble must not crash doctor
        return Check(name, False, f"could not inspect the upload queue: {describe_error(exc)}")

    if not entries:
        return Check(name, True, "upload queue is empty.")

    failed = [e for e in entries if e.get("status") == "failed"]
    if failed:
        worst = failed[0]
        session_name = Path(worst.get("session_dir") or "?").name
        detail = (
            f"{len(entries)} session(s) queued, {len(failed)} of them failed "
            f"after exhausting retries -- e.g. {session_name}: "
            f"{worst.get('last_error')}"
        )
        return Check(
            name,
            False,
            detail,
            fix="Fix the underlying problem (server reachability, auth, disk "
            "space), then run `meeting-notes upload` to retry, or "
            "`meeting-notes upload --list` to see every entry's error.",
        )

    return Check(name, True, f"{len(entries)} session(s) queued for upload, none failed yet.")


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
        # Server/queue awareness is independent of the local audio backend,
        # so still report it even when soundcard itself is unusable.
        checks.append(_check_server())
        checks.append(_check_upload_queue())
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
    checks.append(_check_server())
    checks.append(_check_upload_queue())

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
