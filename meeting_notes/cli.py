"""Command line entry point: devices | doctor | record | transcribe | repair | upload."""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from pathlib import Path
from typing import Optional

from meeting_notes import config as config_mod
from meeting_notes.audio import devices as devices_mod
from meeting_notes.audio.session import LABELS, RecordingSession, create_session_dir
from meeting_notes.wav_io import finalize_session

DEFAULT_OUTPUT_DIR = Path("recordings")
DEFAULT_MODEL = "base.en"


# -- helpers -----------------------------------------------------------------


def _meter(peak: float, width: int = 12) -> str:
    filled = min(width, int(round(peak * width)))
    return "#" * filled + "-" * (width - filled)


def _hms(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


def _status_line(session: RecordingSession) -> None:
    parts = [f"[{_hms(session.elapsed)}]"]
    for track, rec in session.recorders.items():
        flag = "!" if rec.degraded else " "
        parts.append(f"{track}{flag}{_meter(rec.last_peak)}")
    try:
        sys.stdout.write("\r" + "  ".join(parts) + "   ")
        sys.stdout.flush()
    except (BrokenPipeError, ValueError):
        # Losing the terminal is not a reason to lose the recording.
        pass


# -- commands ----------------------------------------------------------------


def cmd_devices(args) -> int:
    mics = devices_mod.list_microphones()
    systems = devices_mod.list_system_sources()

    print("Microphones (your voice):")
    if not mics:
        print("  (none found)")
    for info in mics:
        print(f"  {'*' if info.is_default else ' '} {info.name}  [{info.channels}ch]  id={info.id}")

    print("\nSystem audio sources (everyone else's voice):")
    if not systems:
        note = devices_mod.system_source_platform_note()
        print(f"  (none found) {note}")
        if sys.platform == "darwin":
            print("  macOS needs BlackHole 2ch: https://existential.audio/blackhole/")
    for info in systems:
        suffix = f"  # {info.note}" if info.note else ""
        print(f"  {'*' if info.is_default else ' '} {info.name}  [{info.channels}ch]  id={info.id}{suffix}")
    print("\n('*' marks the default. Run `meeting-notes doctor` to verify capture works.)")
    return 0


def cmd_doctor(args) -> int:
    from meeting_notes.doctor import format_report, run_doctor

    checks = run_doctor()
    print(format_report(checks))
    failed = [c for c in checks if not c.ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed.")
    return 1 if failed else 0


def cmd_record(args) -> int:
    saved = config_mod.load_config()
    mic_req = args.mic or saved.get("mic")
    sys_req = args.system or saved.get("system")
    rate = args.rate or saved.get("samplerate")

    sources = {}
    problems = []

    if not args.system_only:
        try:
            sources["mic"] = devices_mod.resolve_source("mic", mic_req, samplerate=rate)
        except Exception as exc:
            problems.append(f"mic: {exc}")

    if not args.mic_only:
        try:
            sources["system"] = devices_mod.resolve_source("system", sys_req, samplerate=rate)
        except Exception as exc:
            problems.append(f"system: {exc}")

    for problem in problems:
        print(f"warning: {problem}", file=sys.stderr)

    if not sources:
        print("\nNo devices could be opened. Run `meeting-notes doctor`.", file=sys.stderr)
        return 2
    if len(sources) == 1 and not (args.mic_only or args.system_only):
        # Recording one side of a conversation is usually not what someone
        # wants, and finding out afterwards is expensive. Make them opt in.
        print(
            "\nOnly one track could be opened, so you would capture half the meeting.\n"
            "Fix the problem above, or re-run with --mic-only / --system-only to "
            "record one side deliberately.",
            file=sys.stderr,
        )
        return 2

    if args.save_config:
        saved.update(
            {
                "mic": mic_req,
                "system": sys_req,
                **({"samplerate": rate} if rate else {}),
            }
        )
        path = config_mod.save_config(saved)
        print(f"Saved device preferences to {path}")

    session_dir = create_session_dir(Path(args.output_dir), args.name)
    session = RecordingSession(
        session_dir=session_dir,
        sources=sources,
        block_seconds=args.block_seconds,
        stall_timeout=args.stall_timeout,
    )

    print(f"Recording to {session_dir}")
    for track, source in sources.items():
        print(f"  {track:<7} <- {source.name} [{source.channels}ch @ {source.samplerate} Hz]")
    print("Press Ctrl+C to stop.\n")

    # SIGINT only ever lands in the main thread, so the handler's whole job is
    # to flip the flag the capture threads poll between reads.
    def handle_sigint(signum, frame):
        session.request_stop()

    previous = signal.signal(signal.SIGINT, handle_sigint)
    try:
        session.start()
        session.supervise(on_status=_status_line)
    finally:
        signal.signal(signal.SIGINT, previous)
        # In a finally block on purpose: whatever went wrong, the audio already
        # on disk still has to be wrapped into a valid WAV with the right
        # sample rate recorded alongside it.
        meta = session.finalize()
        try:
            sys.stdout.write("\n")
            print("Finalizing...")
            _print_summary(meta, session_dir)
        except (BrokenPipeError, ValueError):
            pass

    if getattr(args, "transcribe", False):
        saved = config_mod.load_config()
        settings = _transcribe_settings(_TranscribeDefaults(), saved)
        print(
            f"\nTranscribing with {settings['model']}. This will use your CPU "
            f"heavily for several minutes; Ctrl+C keeps whatever finished."
        )
        return _run_transcription(session_dir, meta, settings, "faster-whisper")
    return 0


class _TranscribeDefaults:
    """Stands in for parsed transcribe flags when chaining from `record`."""

    model = compute_type = device = language = None
    beam_size = threads = None
    no_vad = False


def _print_summary(meta: dict, session_dir: Path) -> None:
    print(f"\nSession: {session_dir}")
    print(f"Duration: {_hms(meta.get('duration_sec') or 0)}")
    for track, info in meta.get("tracks", {}).items():
        bits = [f"{info.get('duration_sec', 0):.1f}s", info.get("device", "?")]
        drift = [s.get("drift_ppm") for s in info.get("segments", []) if s.get("drift_ppm")]
        if drift:
            bits.append(f"drift {drift[0]:+.0f} ppm")
        if info.get("degraded"):
            bits.append("DEGRADED")
        if info.get("gaps"):
            lost = sum(g.get("seconds_lost", 0) for g in info["gaps"])
            bits.append(f"{len(info['gaps'])} gap(s), {lost:.1f}s lost")
        print(f"  {track:<7} {info.get('wav', '?'):<12} {'  '.join(str(b) for b in bits)}")
    for event in meta.get("events", []):
        print(f"  ! {event['kind']} on {event['track']} at {_hms(event['at'])}: {event['detail']}")
    print(f"\nNext: meeting-notes transcribe {session_dir} --backend faster-whisper")


def _progress_printer():
    """Per-track percent bar. CPU transcription takes minutes; silence looks hung."""
    seen = {}

    def report(track: str, fraction: float, speech_seconds: float) -> None:
        pct = int(fraction * 100)
        if seen.get(track) == pct and fraction < 1.0:
            return
        seen[track] = pct
        mins = speech_seconds / 60.0
        try:
            sys.stdout.write(
                f"\r  {track:<7} [{_meter(fraction, 24)}] {pct:3d}%  "
                f"({mins:.1f} min of speech)   "
            )
            sys.stdout.flush()
            if fraction >= 1.0:
                sys.stdout.write("\n")
        except (BrokenPipeError, ValueError):
            pass

    return report


def _transcribe_settings(args, saved: dict) -> dict:
    """CLI flags win over saved config, which wins over defaults."""
    cfg = dict(saved.get("transcribe") or {})
    if args.model:
        cfg["model"] = args.model
    if args.compute_type:
        cfg["compute_type"] = args.compute_type
    if args.device:
        cfg["device"] = args.device
    if args.beam_size is not None:
        cfg["beam_size"] = args.beam_size
    if args.language:
        cfg["language"] = args.language
    if args.threads is not None:
        cfg["threads"] = args.threads
    if args.no_vad:
        cfg["vad_filter"] = False
    cfg.setdefault("model", DEFAULT_MODEL)
    cfg.setdefault("compute_type", "auto")
    cfg.setdefault("device", "auto")
    cfg.setdefault("beam_size", 5)
    cfg.setdefault("language", None)
    cfg.setdefault("threads", 0)
    cfg.setdefault("vad_filter", True)
    cfg.setdefault("condition_on_previous_text", False)
    return cfg


def cmd_models(args) -> int:
    from meeting_notes.transcribe.faster_whisper_backend import (
        MODEL_CHOICES,
        MODEL_NOTES,
        download_model,
        model_is_downloaded,
    )

    if args.download:
        print(f"Downloading {args.download}... (this is a one-time download)")
        try:
            path = download_model(args.download)
        except Exception as exc:
            print(f"Download failed: {exc}", file=sys.stderr)
            return 2
        print(f"Ready: {path}")
        return 0

    print("Whisper models (CPU speeds are rough, for an 8-core laptop):\n")
    print(f"  {'MODEL':<16} {'SIZE':<10} {'CACHED':<8} NOTES")
    for name in MODEL_CHOICES:
        size, note = MODEL_NOTES.get(name, ("?", ""))
        cached = "yes" if model_is_downloaded(name) else "no"
        marker = "*" if name == DEFAULT_MODEL else " "
        print(f"{marker} {name:<16} {size:<10} {cached:<8} {note}")
    print("\n('*' is the default. Pre-fetch before a meeting with "
          "`meeting-notes models --download <name>`.)")
    return 0


def _run_transcription(session_dir: Path, meta: dict, settings: dict, backend: str) -> int:
    from meeting_notes.timing import load_timing_log
    from meeting_notes.transcribe import merge as merge_mod
    from meeting_notes.transcribe.protocol import available_backends, get_transcriber

    if backend == "faster-whisper":
        try:
            import meeting_notes.transcribe.faster_whisper_backend  # noqa: F401
        except Exception as exc:
            print(f"Could not load faster-whisper: {exc}", file=sys.stderr)
            print("Install it with: pip install 'meeting-notes[whisper]'", file=sys.stderr)
            return 2

    kwargs = dict(settings)
    model = kwargs.pop("model", DEFAULT_MODEL)
    try:
        transcriber = get_transcriber(
            backend, model_size=model, on_progress=_progress_printer(), **kwargs
        )
    except TypeError:
        # A backend that doesn't take our tuning knobs still deserves to run.
        transcriber = get_transcriber(backend)
    except Exception as exc:
        print(f"{exc}", file=sys.stderr)
        print(f"Available backends: {', '.join(available_backends())}", file=sys.stderr)
        return 2

    warn = getattr(transcriber, "warn_if_language_unsupported", lambda: None)()
    if warn:
        print(f"warning: {warn}", file=sys.stderr)

    track_segments: dict = {}
    clocks: dict = {}
    labels: dict = {}
    interrupted = False

    for track, info in (meta.get("tracks") or {}).items():
        wav_path = session_dir / (info.get("wav") or f"{track}.wav")
        timing_path = session_dir / f"{track}.timing.jsonl"
        if not wav_path.exists():
            print(f"skipping {track}: {wav_path.name} missing", file=sys.stderr)
            continue
        # decode + VAD run eagerly, before any segment is produced, so say so
        # rather than showing a 0% bar that sits still for a minute.
        print(f"Analyzing {track} ({wav_path.name})...")
        try:
            track_segments[track] = transcriber.transcribe(wav_path, track)
        except KeyboardInterrupt:
            # Losing an already-finished track because the second one was
            # interrupted would throw away potentially 20 minutes of work.
            print(f"\nInterrupted during {track}; keeping completed tracks.", file=sys.stderr)
            interrupted = True
            break
        clocks[track] = load_timing_log(timing_path) if timing_path.exists() else None
        labels[track] = info.get("label") or LABELS.get(track, track)
        _write_transcript(session_dir, track_segments, clocks, labels, meta, merge_mod)

    if not track_segments:
        print("Nothing was transcribed.", file=sys.stderr)
        return 2

    _write_transcript(session_dir, track_segments, clocks, labels, meta, merge_mod)
    print(f"\nWrote {session_dir / 'transcript.md'}")
    print(f"Wrote {session_dir / 'transcript.json'}")
    return 1 if interrupted else 0


def _write_transcript(session_dir, track_segments, clocks, labels, meta, merge_mod) -> None:
    """Rewrite the transcript from whatever has finished so far."""
    usable = {k: v for k, v in clocks.items() if v is not None}
    ready = {k: v for k, v in track_segments.items() if k in usable}
    if not ready:
        return
    merged = merge_mod.merge_tracks(ready, usable, labels)
    (session_dir / "transcript.md").write_text(
        merge_mod.render_markdown(merged, meta), encoding="utf-8"
    )
    (session_dir / "transcript.json").write_text(
        merge_mod.render_json(merged, meta), encoding="utf-8"
    )


def cmd_transcribe(args) -> int:
    session_dir = Path(args.session_dir)
    meta_path = session_dir / "session.json"
    if not meta_path.exists():
        print(f"No session.json in {session_dir}", file=sys.stderr)
        return 2
    meta = json.loads(meta_path.read_text())

    if (session_dir / "transcript.md").exists() and not args.force:
        print(
            f"{session_dir / 'transcript.md'} already exists. Use --force to redo it.",
            file=sys.stderr,
        )
        return 2

    saved = config_mod.load_config()
    settings = _transcribe_settings(args, saved)
    if args.save_config:
        saved["transcribe"] = settings
        print(f"Saved transcription settings to {config_mod.save_config(saved)}")

    print(f"Model: {settings['model']}  (backend: {args.backend})")
    return _run_transcription(session_dir, meta, settings, args.backend)


def _print_queue_entry(entry: dict) -> None:
    session_name = Path(entry.get("session_dir") or "?").name
    status = entry.get("status", "?")
    attempts = entry.get("attempts", 0)
    line = f"  {session_name:<30} {status:<8} attempts={attempts}"
    if entry.get("last_error"):
        line += f"  last_error={entry['last_error']}"
    print(line)


def _run_upload_pass(worker, queue, before: Optional[list] = None) -> None:
    """One ``UploadWorker.run_once()`` pass, printing what it moved.

    Diffs the queue before/after rather than teaching ``UploadWorker`` to
    report progress itself -- it has no notion of a "CLI run", and reusing
    its upload logic unchanged is the whole point of this command.
    """
    before_by_id = {e["id"]: e for e in (before if before is not None else queue.pending())}
    worker.run_once()
    after_by_id = {e["id"]: e for e in queue.pending()}

    for entry_id in before_by_id.keys() - after_by_id.keys():
        session_name = Path(before_by_id[entry_id].get("session_dir") or entry_id).name
        print(f"  uploaded: {session_name}")

    for entry_id, entry in after_by_id.items():
        prev = before_by_id.get(entry_id)
        if entry.get("status") == "failed" and (prev is None or prev.get("status") != "failed"):
            session_name = Path(entry.get("session_dir") or entry_id).name
            print(f"  failed: {session_name}: {entry.get('last_error')}")


def cmd_upload(args) -> int:
    """Drain the upload queue without the UI.

    For recovering a backlog headlessly -- the UI wasn't running, or a laptop
    sat closed for a week with sessions still queued -- and for seeing upload
    errors on a terminal instead of only in the app's log.
    """
    from meeting_notes.client.queue import SessionQueue, UploadWorker

    saved = config_mod.load_config()
    queue = SessionQueue.for_save_dir(config_mod.save_dir(saved))

    if args.list:
        entries = queue.pending()
        if not entries:
            print("Upload queue is empty.")
            return 0
        print(f"{len(entries)} session(s) queued:")
        for entry in entries:
            _print_queue_entry(entry)
        return 0

    # Checked before looking at the queue's contents: "no server configured"
    # is a setup problem worth reporting even when there happens to be
    # nothing queued right now.
    server = config_mod.server_settings(saved)
    url = (server.get("url") or "").strip()
    if not url:
        print(
            "No server is configured -- set one in the Settings dialog (or "
            "config.json's server.url) before uploading.",
            file=sys.stderr,
        )
        return 2

    entries = queue.pending()
    if not entries:
        print("Upload queue is empty; nothing to do.")
        return 0

    worker = UploadWorker(queue, url, token=server.get("token") or None)

    print(f"Uploading {len(entries)} session(s) to {url}...")
    if args.once:
        _run_upload_pass(worker, queue, before=entries)
    else:
        # Keep taking passes as long as they make progress. A pass that
        # changes nothing means every remaining entry is either exhausted
        # (status "failed") or still inside its own backoff window -- either
        # way, calling run_once() again right now would only spin.
        while True:
            before = queue.pending()
            if not before or all(e.get("status") == "failed" for e in before):
                break
            _run_upload_pass(worker, queue, before=before)
            after = queue.pending()
            if after != before:
                continue
            if not args.wait:
                break
            # --wait: the server is down (or every entry is mid-backoff).
            # Found on a real run: without this, `upload` started while the
            # server was off made one attempt and exited, and the backlog
            # sat there until someone remembered to re-run it. Sleep until
            # the earliest entry is allowed another try, then go again --
            # Ctrl+C is the way out.
            live = [e for e in after if e.get("status") != "failed"]
            if not live:
                break
            due = min(float(e.get("next_attempt_at") or 0.0) for e in live)
            delay = max(1.0, due - time.time())
            print(f"  server unreachable; retrying in {int(delay)}s (Ctrl+C to stop)")
            try:
                time.sleep(delay)
            except KeyboardInterrupt:
                print("\nStopped; the queue is kept for next time.")
                break

    remaining = queue.pending()
    if not remaining:
        print("Done: upload queue is empty.")
        return 0

    print(f"\n{len(remaining)} session(s) still in the queue:")
    for entry in remaining:
        _print_queue_entry(entry)
    return 1


def cmd_repair(args) -> int:
    session_dir = Path(args.session_dir)
    results = finalize_session(session_dir, remove_raw=not args.keep_raw)
    if not results:
        print(f"No .raw files to repair in {session_dir}")
        return 0
    for track, info in results.items():
        guessed = " (sample rate guessed)" if info.get("rate_guessed") else ""
        print(f"{track}: wrote {Path(info['wav']).name}, {info['duration_sec']}s{guessed}")
    return 0


# -- parser ------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="meeting-notes",
        description="Record a meeting as two separate tracks (your mic and system audio), "
        "then transcribe them into one speaker-labeled transcript.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("devices", help="list microphones and system-audio sources").set_defaults(
        func=cmd_devices
    )
    sub.add_parser("doctor", help="check that capture will actually work").set_defaults(
        func=cmd_doctor
    )

    rec = sub.add_parser("record", help="record until Ctrl+C")
    rec.add_argument("--name", help="short label for the session directory")
    rec.add_argument("--mic", help="microphone id or name substring")
    rec.add_argument("--system", help="system-audio source id or name substring")
    rec.add_argument("--rate", type=int, help="capture sample rate (default 48000)")
    rec.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    rec.add_argument("--mic-only", action="store_true", help="record only your microphone")
    rec.add_argument("--system-only", action="store_true", help="record only system audio")
    rec.add_argument("--block-seconds", type=float, default=0.5)
    rec.add_argument(
        "--stall-timeout",
        type=float,
        default=30.0,
        help="restart a track that has delivered no audio for this long",
    )
    rec.add_argument("--save-config", action="store_true", help="remember these devices")
    rec.add_argument(
        "--transcribe",
        action="store_true",
        help="transcribe immediately after stopping (uses your CPU heavily for minutes)",
    )
    rec.set_defaults(func=cmd_record)

    tr = sub.add_parser("transcribe", help="transcribe a recorded session")
    tr.add_argument("session_dir")
    tr.add_argument("--backend", default="faster-whisper")
    tr.add_argument(
        "--model",
        help=f"whisper model (default {DEFAULT_MODEL}). Suggested: base.en, "
        f"small.en, large-v3-turbo. Any faster-whisper name works.",
    )
    tr.add_argument("--compute-type", help="int8 (CPU default), float16, float32, default")
    tr.add_argument("--device", help="auto (default), cpu, cuda")
    tr.add_argument("--beam-size", type=int, help="higher is slower and slightly better (default 5)")
    tr.add_argument("--language", help="force a language code, e.g. en. Default: auto")
    tr.add_argument("--threads", type=int, help="CPU threads, 0 lets the engine decide")
    tr.add_argument(
        "--no-vad",
        action="store_true",
        help="disable voice activity detection (much slower; decodes silence too)",
    )
    tr.add_argument("--force", action="store_true", help="overwrite an existing transcript")
    tr.add_argument("--save-config", action="store_true", help="remember these settings")
    tr.set_defaults(func=cmd_transcribe)

    md = sub.add_parser("models", help="list or pre-download transcription models")
    md.add_argument("--download", metavar="NAME", help="fetch a model into the local cache")
    md.set_defaults(func=cmd_models)

    rp = sub.add_parser("repair", help="rebuild WAVs from .raw after an unclean exit")
    rp.add_argument("session_dir")
    rp.add_argument("--keep-raw", action="store_true")
    rp.set_defaults(func=cmd_repair)

    up = sub.add_parser("upload", help="drain the upload queue to the server without the UI")
    up.add_argument("--once", action="store_true", help="run a single pass over the queue, then exit")
    up.add_argument(
        "--wait",
        action="store_true",
        help="keep retrying through the server being down until the queue is drained "
        "(default: stop as soon as a pass makes no progress)",
    )
    up.add_argument("--list", action="store_true", help="print the queue without uploading anything")
    up.set_defaults(func=cmd_upload)

    return parser


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
