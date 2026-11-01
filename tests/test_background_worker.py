"""The shared worker base: start once, wake, stop, startup delay, a bad pass does not end the loop."""

from __future__ import annotations

import time

from meeting_notes.server.worker import BackgroundWorker


class _Counter(BackgroundWorker):
    thread_name = "test-counter"

    def __init__(self, **kw):
        super().__init__()
        self.passes = 0
        self.fail_first = kw.get("fail_first", False)
        self.poll_seconds = kw.get("poll", 30.0)
        self.startup_delay = kw.get("delay", 0.0)

    def run_once(self):
        self.passes += 1
        if self.fail_first and self.passes == 1:
            raise RuntimeError("boom")


def _wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_start_once_wake_and_stop():
    w = _Counter()
    w.start()
    first = w._thread
    w.start()  # already running: no second thread
    assert w._thread is first and first.name == "test-counter" and first.daemon
    assert _wait_for(lambda: w.passes == 1)
    w.wake()  # cuts the 30 s nap short
    assert _wait_for(lambda: w.passes == 2)
    w.stop()
    assert not first.is_alive()


def test_stop_ends_a_worker_asleep_in_its_nap_and_a_restart_runs_again():
    w = _Counter()
    w.start()
    assert _wait_for(lambda: w.passes == 1)
    started = time.monotonic()
    w.stop()
    assert time.monotonic() - started < 2.0
    w.start()  # restart: the wake that stop left behind must not cause a stray extra pass first
    assert _wait_for(lambda: w.passes == 2)
    time.sleep(0.2)
    assert w.passes == 2
    w.stop()


def test_startup_delay_is_cut_short_by_stop_only():
    w = _Counter(delay=30.0)
    w.start()
    w.wake()
    time.sleep(0.2)
    assert w.passes == 0
    w.stop()
    assert w.passes == 0 and not w._thread.is_alive()


def test_a_failing_pass_does_not_end_the_worker():
    w = _Counter(fail_first=True, poll=0.01)
    w.start()
    assert _wait_for(lambda: w.passes >= 3)
    w.stop()
