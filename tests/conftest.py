"""Shared test setup.

Qt must be told to use the offscreen platform before any QApplication exists,
otherwise importing the UI on a headless machine (CI, a container, a server)
fails on a missing display rather than skipping.
"""

from __future__ import annotations

import os
import tempfile
import threading

# Every test runs with a throwaway home and app-data folders, set before any meeting_notes import: the client
# works out ~/.meeting-notes (its config) and ~/Meeting Notes (the recordings) when it loads, and a test window
# that skipped the per-test fixtures once compressed the developer's real recordings and could as easily have
# run retention on them or written the real config. %LOCALAPPDATA% holds the GPU runtime.
def _keep_ffmpeg_on_path() -> None:
    """Tools found through the real profile (ffmpeg from WinGet lives under %LOCALAPPDATA%) stay reachable: put
    their folder on PATH before the profile is swapped out, or the ffmpeg-backed tests would quietly skip."""
    import glob
    import shutil

    if shutil.which("ffmpeg"):
        return
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return
    found = sorted(glob.glob(os.path.join(local, "Microsoft", "WinGet", "Packages", "*", "**", "ffmpeg.exe"),
                             recursive=True))
    if found:
        os.environ["PATH"] = os.path.dirname(found[0]) + os.pathsep + os.environ.get("PATH", "")


_keep_ffmpeg_on_path()
_SANDBOX = tempfile.mkdtemp(prefix="meeting-notes-test-home-")
for _var, _sub in (("USERPROFILE", ""), ("HOME", ""), ("APPDATA", "AppData/Roaming"), ("LOCALAPPDATA", "AppData/Local")):
    _path = os.path.join(_SANDBOX, _sub) if _sub else _SANDBOX
    os.makedirs(_path, exist_ok=True)
    os.environ[_var] = _path
# The background compression pass never runs in a test window (the compress tests call it directly).
os.environ.setdefault("MEETING_NOTES_NO_BACKGROUND_COMPRESS", "1")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# Never let a test window poll the real registry/windows for a live call.
os.environ.setdefault("MEETING_NOTES_NO_DETECT", "1")
# Windows must not poll the machine's real audio devices in the background.
os.environ.setdefault("MEETING_NOTES_NO_DEVICE_WATCH", "1")
# Windows must not open the remote-control websocket to any server.
os.environ.setdefault("MEETING_NOTES_NO_REMOTE", "1")
# Windows must not open Settings on their own for a missing server password (the tests that cover it unset this).
os.environ.setdefault("MEETING_NOTES_NO_FIRST_RUN_PROMPT", "1")
# No test may ever make macOS show a real Microphone / Screen Recording prompt (the tests that cover the
# first-run prompts unset this and fake the calls).
os.environ.setdefault("MEETING_NOTES_NO_PERMISSION_PROMPT", "1")

# The GPU helper never starts a runtime, shows a tray icon or touches the Run key under test.
os.environ.setdefault("MEETING_NOTES_NO_GPU_HELPER", "1")
# Each test run gets its own single-instance socket.
os.environ.setdefault("MEETING_NOTES_INSTANCE_SUFFIX", "tests")


import pytest  # noqa: E402


TEST_HANG_SECONDS = int(os.environ.get("MEETING_NOTES_TEST_HANG_SECONDS", "600"))
# pytest captures stderr, so the stack dump goes to a file (named in the run's header).
HANG_LOG = os.path.join(tempfile.gettempdir(), f"meeting-notes-test-hang-{os.getpid()}.log")
_hang_log_file = None


def pytest_report_header(config):
    return f"hung-test watchdog: {TEST_HANG_SECONDS}s per test, stacks to {HANG_LOG}"


@pytest.fixture(autouse=True)
def _hang_watchdog(request):
    """A test stuck past TEST_HANG_SECONDS dumps every thread's stack and ends the run, rather than hanging it
    silently (a wedged Qt event loop or audio thread otherwise leaves pytest waiting forever)."""
    import faulthandler

    global _hang_log_file
    if _hang_log_file is None:
        _hang_log_file = open(HANG_LOG, "w", encoding="utf-8")  # noqa: SIM115 - lives for the whole run
    _hang_log_file.seek(0)
    _hang_log_file.truncate()
    _hang_log_file.write(f"hung test: {request.node.nodeid}\n")
    _hang_log_file.flush()
    # faulthandler stops after 100 threads, and a late test can have more leaked workers than that, which
    # pushed the stuck test's own (main) thread out of the dump. A Python timer writes the main thread and a
    # thread census first; faulthandler stays as the backstop that also ends a run wedged with the GIL held.
    timer = threading.Timer(TEST_HANG_SECONDS, _dump_main_thread, args=(threading.main_thread().ident,))
    timer.daemon = True
    timer.start()
    faulthandler.dump_traceback_later(TEST_HANG_SECONDS + 15, exit=True, file=_hang_log_file)
    yield
    timer.cancel()
    faulthandler.cancel_dump_traceback_later()


def _dump_main_thread(main_ident) -> None:
    import collections
    import sys
    import traceback

    frame = sys._current_frames().get(main_ident)
    names = collections.Counter(getattr(getattr(t, "_target", None), "__qualname__", t.name) for t in threading.enumerate())
    _hang_log_file.write(f"Timeout ({TEST_HANG_SECONDS}s). Main thread (most recent call last):\n")
    _hang_log_file.write("".join(traceback.format_stack(frame)) if frame is not None else "  (not found)\n")
    _hang_log_file.write(f"\n{threading.active_count()} threads: {dict(names.most_common())}\n\nAll threads:\n")
    _hang_log_file.flush()


class RealAudioDisabled(RuntimeError):
    """A test reached a real capture device. Fake the source (or set MEETING_NOTES_TEST_REAL_AUDIO=1)."""


@pytest.fixture(autouse=True, scope="session")
def _no_real_audio_capture():
    """No test may open the machine's real microphone, loopback or ScreenCaptureKit stream.

    A test that reached a real device (the full ``doctor`` run, say) left macOS's coreaudiod holding the
    built-in mic for the rest of a half-hour run. Listing devices stays allowed (the smoke test does), only
    opening a capture is refused; tests that fake ``import_soundcard`` or pass a ``stream_factory`` never get
    here. ``MEETING_NOTES_TEST_REAL_AUDIO=1`` lifts this for a deliberate hardware check.
    """
    if os.environ.get("MEETING_NOTES_TEST_REAL_AUDIO"):
        yield
        return

    def refuse(*args, **kwargs):
        raise RealAudioDisabled("real audio capture is disabled under test (MEETING_NOTES_TEST_REAL_AUDIO=1 allows it)")

    with pytest.MonkeyPatch.context() as mp:
        try:
            import soundcard  # noqa: F401 - loads the platform backend
        except Exception:  # noqa: BLE001 - no backend here, nothing to guard
            pass
        import sys

        for backend in ("soundcard.mediafoundation", "soundcard.coreaudio", "soundcard.pulseaudio"):
            module = sys.modules.get(backend)
            if module is not None:
                mp.setattr(module._Microphone, "recorder", refuse)
                mp.setattr(module._Microphone, "record", refuse)
        from meeting_notes.audio import screencapture_source

        mp.setattr(screencapture_source, "_default_stream_factory", refuse)
        yield


@pytest.fixture(autouse=True)
def _stop_leaked_server_workers():
    """Stop the background workers of every server app a test created and left running (a TestClient used
    without ``with`` never runs the app's shutdown). Hundreds of leaked worker threads waking on their timeouts
    starved late tests of the GIL on macOS until the run looked hung. Apps from wider-scoped fixtures already
    exist when this starts, so they are left alone."""
    import sys

    app_mod = sys.modules.get("meeting_notes.server.app")
    before = set(app_mod._UNSTOPPED) if app_mod is not None else set()
    yield
    app_mod = sys.modules.get("meeting_notes.server.app")
    if app_mod is None:
        return
    for key, stop in list(app_mod._UNSTOPPED.items()):
        if key not in before:
            stop()


@pytest.fixture(autouse=True)
def _reset_version_gate():
    """The "server says this client is too old" state is process-wide."""
    from meeting_notes.client import version_gate

    version_gate.clear()
    yield
    version_gate.clear()
