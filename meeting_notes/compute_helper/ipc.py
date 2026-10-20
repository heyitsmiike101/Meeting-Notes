"""Talking to the supervising client: status out on stdout, commands in through a control file.

Out (stdout JSON lines): ``{"event": "status", state, job, board, progress, gpu, error}``. Logging goes to stderr.

In: the client writes ``{"paused": bool, "stop": bool}`` to a control file the runtime checks twice a second.
Not stdin: on Windows a thread blocked reading an inherited stdin pipe stalls faster-whisper's decoding in the
same process (seen on a real GTX 1060: 0% progress for minutes, 37 s without the reader). The runtime also stops
when the supervising process is gone, which is what stdin EOF used to signal.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional, TextIO

COMMANDS = ("pause", "resume", "stop")


class Ipc:
    def __init__(self, out: Optional[TextIO] = None, inp: Optional[TextIO] = None):
        self._out = out if out is not None else sys.stdout
        self._in = inp if inp is not None else sys.stdin
        self._lock = threading.Lock()
        self._last: Optional[dict] = None

    def emit(self, event: dict) -> None:
        line = json.dumps(event, separators=(",", ":"))
        with self._lock:
            try:
                self._out.write(line + "\n")
                self._out.flush()
            except (OSError, ValueError):
                pass  # supervisor went away; the control watcher's parent check stops us

    def status(self, state: str, *, job: Optional[str] = None, board: Optional[str] = None,
               progress: Optional[float] = None, gpu: Optional[dict] = None,
               error: Optional[str] = None) -> None:
        event = {"event": "status", "state": state, "job": job, "board": board,
                 "progress": progress, "gpu": gpu, "error": error}
        with self._lock:
            if event == self._last:
                return
            self._last = event
        self.emit(event)

    def start_reader(self, on_command: Callable[[str], None]) -> threading.Thread:
        """Read commands on a daemon thread. EOF (the supervisor died) is treated as ``stop``."""

        def loop() -> None:
            try:
                for raw in self._in:
                    try:
                        message = json.loads(raw)
                    except ValueError:
                        continue
                    cmd = message.get("cmd") if isinstance(message, dict) else None
                    if cmd in COMMANDS:
                        on_command(cmd)
                        if cmd == "stop":
                            return
            except (OSError, ValueError):
                pass
            on_command("stop")

        thread = threading.Thread(target=loop, name="compute-helper-ipc", daemon=True)
        thread.start()
        return thread


def read_control(path: Path) -> dict:
    """The control file's ``{"paused", "stop"}``; missing or half-written files read as all-false."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_control(path: Path, *, paused: bool, stop: bool = False) -> None:
    """Atomically replace the control file (the client side)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({"paused": bool(paused), "stop": bool(stop)}), encoding="utf-8")
    os.replace(tmp, path)


def process_alive(pid: int) -> bool:
    """True while process ``pid`` runs. Windows needs OpenProcess; ``os.kill(pid, 0)`` elsewhere."""
    if pid <= 0:
        return True
    if sys.platform.startswith("win"):
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def start_control_watcher(path: Optional[Path], on_command: Callable[[str], None], *,
                          parent_pid: Optional[int] = None, interval: float = 0.5,
                          alive: Callable[[int], bool] = process_alive) -> threading.Thread:
    """Turn control-file changes into ``pause``/``resume``/``stop`` calls; ``stop`` when the parent exits."""

    def loop() -> None:
        paused = False
        while True:
            if parent_pid and not alive(parent_pid):
                on_command("stop")
                return
            if path is not None:
                control = read_control(path)
                if control.get("stop"):
                    on_command("stop")
                    return
                want = bool(control.get("paused"))
                if want != paused:
                    paused = want
                    on_command("pause" if want else "resume")
            time.sleep(interval)

    thread = threading.Thread(target=loop, name="compute-helper-control", daemon=True)
    thread.start()
    return thread
