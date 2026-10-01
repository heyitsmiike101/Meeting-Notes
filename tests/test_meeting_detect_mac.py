"""macOS call detection: the probes and their hand-off to MeetingDetector.

The CoreAudio process list, NSWorkspace and CGWindowList are all injected, so
these run anywhere. Live behaviour (a real Zoom/Meet call) is in docs/manual-testing.md.
"""

from __future__ import annotations

import ctypes
from datetime import datetime

from meeting_notes.client import meeting_detect as md
from meeting_notes.client import meeting_detect_mac as mac
from meeting_notes.client.meeting_detect import MeetingDetector, MeetingEnded, MeetingStarted

NOW = datetime(2026, 9, 29, 14, 30)
OWN_PID = 4242

ZOOM = "us.zoom.xos"
CHROME_HELPER = "com.google.Chrome.helper"
TEAMS = "com.microsoft.teams2"


def proc(pid, bundle, running=True):
    return mac.AudioProcess(pid=pid, bundle_id=bundle, running_input=running)


def window(pid, title="", layer=0):
    return {"kCGWindowOwnerPID": pid, "kCGWindowName": title, "kCGWindowLayer": layer}


BUNDLES = {100: ZOOM, 200: "com.google.Chrome", 210: CHROME_HELPER, 300: TEAMS, 400: "com.apple.dock", OWN_PID: "lan.meeting.notes"}


def bundle_of(pid):
    return BUNDLES.get(pid, "")


# -- classification of bundle ids ------------------------------------------------------------


def test_bundle_ids_classify_including_helper_processes():
    assert md.classify("us.zoom.xos") == "zoom"
    assert md.classify("com.microsoft.teams2") == "teams"
    assert md.classify("com.microsoft.teams2.helper") == "teams"
    for bundle in (
        "com.google.Chrome", "com.google.Chrome.helper.Renderer", "com.brave.Browser",
        "com.microsoft.edgemac", "org.mozilla.firefox", "company.thebrowser.Browser",
        "com.apple.Safari", "com.apple.WebKit.GPU",
    ):
        assert md.classify(bundle) == "browser", bundle
    assert md.classify("com.apple.dock") is None
    assert md.classify("lan.meeting.notes") is None


# -- microphone use ------------------------------------------------------------------------------


def test_own_process_is_ignored_and_duplicates_collapse():
    uses = mac.mic_usage_from_processes(
        [proc(OWN_PID, "lan.meeting.notes"), proc(100, ZOOM), proc(101, ZOOM), proc(5, "")], own_pid=OWN_PID
    )
    assert [u.exe_name for u in uses] == [ZOOM]
    assert uses[0].in_use and uses[0].exe_path == ""


def test_running_input_flag_maps_to_in_use():
    uses = mac.mic_usage_from_processes([proc(100, ZOOM, running=False)], own_pid=OWN_PID)
    assert not uses[0].in_use


def test_fallback_attributes_a_live_mic_to_running_meeting_apps_only():
    uses = mac.mic_usage_from_device_state(True, [ZOOM, "com.apple.dock", "com.google.Chrome", ZOOM])
    assert sorted(u.exe_name for u in uses) == ["com.google.chrome", ZOOM]
    assert all(u.in_use for u in uses)
    assert not any(u.in_use for u in mac.mic_usage_from_device_state(False, [ZOOM]))


def test_read_mic_usage_prefers_the_process_list_and_falls_back(monkeypatch):
    monkeypatch.setattr(mac.sys, "platform", "darwin")
    from_processes = mac.read_mic_usage(
        list_processes=lambda: [proc(100, ZOOM)], device_running=lambda: False,
        running_apps=lambda: [], own_pid=OWN_PID,
    )
    assert [u.exe_name for u in from_processes] == [ZOOM] and from_processes[0].in_use
    fallback = mac.read_mic_usage(
        list_processes=lambda: None, device_running=lambda: True,
        running_apps=lambda: [ZOOM], own_pid=OWN_PID,
    )
    assert [u.exe_name for u in fallback] == [ZOOM] and fallback[0].in_use


def test_probe_failures_read_as_nothing_in_use(monkeypatch):
    monkeypatch.setattr(mac.sys, "platform", "darwin")

    def boom():
        raise OSError("CoreAudio gone")

    assert mac.read_mic_usage(list_processes=boom) == []
    assert mac.list_window_titles(windows=boom) == []


def test_probes_are_inert_off_macos(monkeypatch):
    monkeypatch.setattr(mac.sys, "platform", "linux")
    assert mac.read_mic_usage() == []
    assert mac.list_window_titles() == []
    assert mac.list_audio_processes() is None
    assert mac.running_bundle_ids() == []


def test_fourcc_selectors_match_coreaudio_headers():
    assert mac._SEL_PROCESS_LIST == 0x70727323  # 'prs#'
    assert mac._SEL_PROCESS_INPUT == 0x70697269  # 'piri'
    assert mac._SEL_RUNNING_SOMEWHERE == 0x676F6E65  # 'gone'
    assert ctypes.sizeof(mac._Address) == 12


# -- window titles ----------------------------------------------------------------------------------


def test_windows_keep_normal_layers_and_empty_titles():
    windows = [
        window(100, "Zoom Meeting"),
        window(200, ""),  # Screen Recording not granted: title unreadable
        window(400, "Dock", layer=20),
        window(999, "Ghost"),  # pid with no bundle
    ]
    assert mac.windows_to_titles(windows, bundle_of) == [(ZOOM, "Zoom Meeting"), ("com.google.chrome", "")]


# -- MeetingDetector end to end on the mac probes -------------------------------------------------------


class World:
    def __init__(self):
        self.procs = []
        self.windows = []

    def usage(self):
        return mac.mic_usage_from_processes(self.procs, own_pid=OWN_PID)

    def titles(self):
        return mac.windows_to_titles(self.windows, bundle_of)


def detector(world, **kw):
    return MeetingDetector(
        read_usage=world.usage, read_titles=world.titles, own_executable="",
        wall_clock=lambda: NOW, **kw,
    )


def test_zoom_call_without_readable_titles_gets_an_app_name_fallback():
    world = World()
    world.procs = [proc(100, ZOOM)]
    world.windows = [window(100, "")]  # no Screen Recording: title hidden
    d = detector(world)
    assert d.poll(0.0) == []
    events = d.poll(5.0)
    assert events == [MeetingStarted("zoom", "Zoom", "Zoom call 2:30 PM")]


def test_meet_in_chrome_is_named_from_the_tab_title_when_titles_are_readable():
    world = World()
    world.procs = [proc(210, CHROME_HELPER)]
    world.windows = [window(200, "Meet - Weekly sync - Google Chrome")]
    d = detector(world)
    d.poll(0.0)
    (event,) = d.poll(5.0)
    assert (event.kind, event.label, event.suggested_name) == ("browser", "Google Meet", "Weekly sync")


def test_our_own_capture_never_starts_or_holds_a_call():
    world = World()
    world.procs = [proc(OWN_PID, "lan.meeting.notes")]
    world.windows = [window(OWN_PID, "Meeting Notes")]
    d = detector(world)
    assert d.poll(0.0) == [] and d.poll(10.0) == []


def test_a_crashed_app_with_a_stale_mic_flag_and_no_window_is_not_offered():
    world = World()
    world.procs = [proc(100, ZOOM)]
    d = detector(world)
    d.poll(0.0)
    assert d.poll(10.0) == []


def test_call_ends_after_mic_release_no_window_and_the_grace_period():
    world = World()
    world.procs = [proc(100, ZOOM)]
    world.windows = [window(100, "Zoom Meeting")]
    d = detector(world, end_grace_sec=60.0)
    d.poll(0.0)
    assert isinstance(d.poll(5.0)[0], MeetingStarted)
    # Mic released, Zoom's meeting window still open: the call continues.
    world.procs = [proc(100, ZOOM, running=False)]
    assert d.poll(30.0) == [] and d.poll(200.0) == []
    # Window closed as well: only after the grace period does the call end.
    world.windows = []
    assert d.poll(210.0) == []
    assert d.poll(250.0) == []
    (ended,) = d.poll(262.0)
    assert isinstance(ended, MeetingEnded) and ended.kind == "zoom"


def test_muted_attendee_without_readable_titles_still_ends_only_after_grace():
    world = World()
    world.procs = [proc(100, ZOOM)]
    world.windows = [window(100, "")]
    d = detector(world, end_grace_sec=60.0)
    d.poll(0.0)
    d.poll(5.0)
    world.procs = [proc(100, ZOOM, running=False)]
    assert d.poll(10.0) == [] and d.poll(60.0) == []
    assert isinstance(d.poll(80.0)[0], MeetingEnded)
