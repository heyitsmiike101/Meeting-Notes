"""``python -m meeting_notes.compute_helper {run,self-test,gpu-info}``."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Optional

from . import gpu as gpu_mod
from .ipc import Ipc, start_control_watcher


def _run(args: argparse.Namespace) -> int:
    ipc = Ipc()
    try:
        key = Path(args.key_file).read_text(encoding="utf-8").strip()
    except OSError as exc:
        ipc.status("error", error=f"cannot read the helper key file: {exc.strerror or exc}")
        return 2
    if not key or not args.server.lower().startswith(("http://", "https://")):
        ipc.status("error", error="the helper key file is empty or the server URL is invalid")
        return 2
    models = Path(args.models)
    if not (models / "whisper").is_dir():
        ipc.status("error", error=f"no Whisper model in {models / 'whisper'}")
        return 2

    from .engine import Engine
    from .worker import Worker, WorkerConfig

    info = gpu_mod.query_gpu()
    config = WorkerConfig(
        server=args.server, key=key, pack_version=args.pack_version,
        poll_seconds=args.poll_seconds, work_dir=args.work_dir)
    worker = Worker(config, Engine(models, gpu_name=info["name"] if info else None), ipc, gpu=info)
    # Commands come through a control file, never stdin (see ipc.py for why).
    start_control_watcher(
        Path(args.control_file) if args.control_file else None, worker.command,
        parent_pid=args.parent_pid or None)
    try:
        return worker.run()
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001 - report, never a bare traceback on the supervisor's pipe
        logging.getLogger("meeting_notes.compute_helper").exception("helper runtime crashed")
        ipc.status("error", gpu=info, error=f"{type(exc).__name__}: {exc}"[:300])
        return 1


def _self_test(args: argparse.Namespace) -> int:
    from . import selftest

    return selftest.main(Path(args.models), args.whisper)


def _gpu_info(_args: argparse.Namespace) -> int:
    info = gpu_mod.query_gpu()
    print(json.dumps({"available": True, **info} if info else {"available": False}))
    return 0


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="meeting_notes.compute_helper", description="Meeting Notes GPU helper runtime")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="poll the server for transcription work")
    run.add_argument("--server", required=True)
    run.add_argument("--key-file", required=True, help="file holding the helper key (never logged)")
    run.add_argument("--models", required=True, help="the pack's models/ directory")
    run.add_argument("--pack-version", default="")
    run.add_argument("--poll-seconds", type=float, default=5.0)
    run.add_argument("--work-dir", default=None, help="where per-job temp audio goes (default: system temp)")
    run.add_argument("--control-file", default=None,
                     help="JSON {paused, stop} written by the supervising client")
    run.add_argument("--parent-pid", type=int, default=0, help="stop when this process exits")
    run.set_defaults(func=_run)

    test = sub.add_parser("self-test", help="verify torch/CTranslate2 CUDA by transcribing a short clip")
    test.add_argument("--models", required=True)
    test.add_argument("--whisper", default=None, help="Whisper model folder name (default: first found)")
    test.set_defaults(func=_self_test)

    info = sub.add_parser("gpu-info", help="print GPU name, VRAM and driver as JSON")
    info.set_defaults(func=_gpu_info)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
