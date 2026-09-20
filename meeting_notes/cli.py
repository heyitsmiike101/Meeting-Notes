"""Command line entry point: devices | doctor | record | transcribe | repair."""

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
    sys.stdout.write("\r" + "  ".join(parts) + "   ")
    sys.stdout.flush()


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

    sys.stdout.write("\n")
    print("Finalizing...")
    meta = session.finalize()
    _print_summary(meta, session_dir)
    return 0


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


def cmd_transcribe(args) -> int:
    from meeting_notes.timing import load_timing_log
    from meeting_notes.transcribe import merge as merge_mod
    from meeting_notes.transcribe.protocol import available_backends, get_transcriber

    session_dir = Path(args.session_dir)
    meta_path = session_dir / "session.json"
    if not meta_path.exists():
        print(f"No session.json in {session_dir}", file=sys.stderr)
        return 2
    meta = json.loads(meta_path.read_text())

    # Importing the backend module is what registers it, so do it before
    # resolving the name.
    if args.backend == "faster-whisper":
        try:
            import meeting_notes.transcribe.faster_whisper_backend  # noqa: F401
        except Exception as exc:
            print(f"Could not load faster-whisper: {exc}", file=sys.stderr)
            print("Install it with: pip install 'meeting-notes[whisper]'", file=sys.stderr)
            return 2

    try:
        transcriber = get_transcriber(args.backend, **({"model_size": args.model} if args.model else {}))
    except Exception as exc:
        print(f"{exc}", file=sys.stderr)
        print(f"Available backends: {', '.join(available_backends())}", file=sys.stderr)
        return 2

    track_segments = {}
    clocks = {}
    labels = {}
    for track, info in meta.get("tracks", {}).items():
        wav_path = session_dir / (info.get("wav") or f"{track}.wav")
        timing_path = session_dir / f"{track}.timing.jsonl"
        if not wav_path.exists():
            print(f"skipping {track}: {wav_path.name} missing", file=sys.stderr)
            continue
        print(f"Transcribing {track} ({wav_path.name})...")
        track_segments[track] = transcriber.transcribe(wav_path, track)
        clocks[track] = load_timing_log(timing_path) if timing_path.exists() else None
        labels[track] = info.get("label") or LABELS.get(track, track)

    clocks = {k: v for k, v in clocks.items() if v is not None}
    if not track_segments:
        print("Nothing to transcribe.", file=sys.stderr)
        return 2

    merged = merge_mod.merge_tracks(track_segments, clocks, labels)
    (session_dir / "transcript.md").write_text(
        merge_mod.render_markdown(merged, meta), encoding="utf-8"
    )
    (session_dir / "transcript.json").write_text(
        merge_mod.render_json(merged, meta), encoding="utf-8"
    )
    print(f"\nWrote {session_dir / 'transcript.md'}")
    print(f"Wrote {session_dir / 'transcript.json'}")
    return 0


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
    rec.set_defaults(func=cmd_record)

    tr = sub.add_parser("transcribe", help="transcribe a recorded session")
    tr.add_argument("session_dir")
    tr.add_argument("--backend", default="faster-whisper")
    tr.add_argument("--model", help="backend model name, e.g. base.en or small.en")
    tr.set_defaults(func=cmd_transcribe)

    rp = sub.add_parser("repair", help="rebuild WAVs from .raw after an unclean exit")
    rp.add_argument("session_dir")
    rp.add_argument("--keep-raw", action="store_true")
    rp.set_defaults(func=cmd_repair)

    return parser


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
