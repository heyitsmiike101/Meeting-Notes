"""Meeting detection: pure logic with injected fakes (no registry, no windows)."""

from __future__ import annotations

import os
from datetime import datetime

import pytest

from meeting_notes import config as config_mod
from meeting_notes.client import meeting_detect as md
from meeting_notes.client.meeting_detect import (
    MeetingDetector,
    MeetingEnded,
    MeetingStarted,
    MicUse,
    classify,
    suggest_name,
)

NOW = datetime(2026, 9, 29, 14, 30)


def use(key, in_use=True):
    if key.startswith("MSTeams_"):
        return MicUse(key=key, exe_path="", exe_name=key.lower(), in_use=in_use)
    path = key.replace("#", "\\")
    return MicUse(key=key, exe_path=path, exe_name=path.rsplit("\\", 1)[-1].lower(), in_use=in_use)


TEAMS = "MSTeams_8wekyb3d8bbwe"
ZOOM = "C:#Users#alex#AppData#Roaming#Zoom#bin#Zoom.exe"
CHROME = "C:#Program Files#Google#Chrome#Application#chrome.exe"
DISCORD = "C:#Users#alex#AppData#Local#Discord#app-1#Discord.exe"
OWN = "C:#Python314#python.exe"


def test_classification():
    assert classify("MSTeams_8wekyb3d8bbwe") == "teams"
    assert classify("ms-teams.exe") == "teams"
    assert classify("Teams.exe") == "teams"
    assert classify("zoom.exe") == "zoom"
    for exe in ("chrome.exe", "brave.exe", "msedge.exe", "firefox.exe"):
        assert classify(exe) == "browser"
    assert classify("discord.exe") is None
    assert classify("vmware.exe") is None


# Every known app has a window open unless a test says otherwise.
OPEN_WINDOWS = (
    ("ms-teams.exe", "Microsoft Teams"),
    ("zoom.exe", "Zoom Workplace"),
    ("chrome.exe", "New Tab - Google Chrome"),
)


def make(usage_holder, titles=OPEN_WINDOWS, **kw):
    return MeetingDetector(
        read_usage=lambda: usage_holder["usage"],
        read_titles=lambda: list(titles),
        own_executable=kw.pop("own", "C:\\Python314\\python.exe"),
        wall_clock=lambda: NOW,
        **kw,
    )


def test_ignores_unknown_apps_and_own_executable():
    h = {"usage": [use(DISCORD), use(OWN), use(ZOOM, in_use=False)]}
    det = make(h)
    for t in range(0, 60, 2):
        assert det.poll(float(t)) == []


@pytest.mark.skipif(os.name != "nt", reason="compares Windows exe paths")
def test_own_exe_excluded_even_if_it_were_a_known_app():
    h = {"usage": [use(CHROME)]}
    det = make(h, own="c:\\program files\\google\\chrome\\application\\CHROME.exe")
    assert det.poll(0) == [] and det.poll(10) == []


def test_debounce_then_single_start():
    h = {"usage": [use(ZOOM)]}
    det = make(h, start_debounce_sec=4)
    assert det.poll(0) == []
    assert det.poll(3) == []
    events = det.poll(4)
    assert len(events) == 1 and isinstance(events[0], MeetingStarted)
    assert events[0].kind == "zoom" and events[0].label == "Zoom"
    assert events[0].suggested_name == "Zoom call 2:30 PM"
    assert det.poll(6) == [] and det.poll(100) == []


def test_brief_use_below_debounce_never_starts():
    h = {"usage": [use(ZOOM)]}
    det = make(h, start_debounce_sec=4)
    det.poll(0)
    h["usage"] = []
    det.poll(2)
    h["usage"] = [use(ZOOM)]
    assert det.poll(3) == []
    assert det.poll(5) == []  # timer restarted at 3
    assert len(det.poll(7)) == 1


def test_grace_short_drop_does_not_end_long_does():
    h = {"usage": [use(TEAMS)]}
    det = make(h, start_debounce_sec=2, end_grace_sec=20)
    det.poll(0)
    assert isinstance(det.poll(2)[0], MeetingStarted)
    h["usage"] = []
    assert det.poll(10) == []
    assert det.poll(20) == []  # 18s since last seen
    h["usage"] = [use(TEAMS)]  # reconnects inside the grace
    assert det.poll(21) == []
    h["usage"] = []
    assert det.poll(30) == []
    events = det.poll(41)
    assert len(events) == 1 and isinstance(events[0], MeetingEnded)
    assert events[0].kind == "teams" and events[0].label == "Teams"
    assert det.poll(60) == []


def test_only_first_meeting_while_active_then_next_after_end():
    h = {"usage": [use(ZOOM), use(CHROME)]}
    det = make(h, start_debounce_sec=2, end_grace_sec=5)
    det.poll(0)
    first = det.poll(2)
    assert [e.kind for e in first] == ["zoom"]
    # Chrome using the mic while Zoom is active is ignored.
    assert det.poll(10) == []
    h["usage"] = [use(CHROME)]
    assert det.poll(12) == []  # zoom not seen for 2s
    assert [type(e) for e in det.poll(16)] == [MeetingEnded]
    assert det.poll(17) == []
    assert [type(e) for e in det.poll(19)] == [MeetingStarted]


def test_probe_failure_is_survivable():
    def boom():
        raise OSError("nope")

    det = MeetingDetector(read_usage=boom, read_titles=boom, own_executable="x")
    assert det.poll(0) == []


def test_read_mic_usage_and_titles_safe_off_windows(monkeypatch):
    monkeypatch.setattr(md.sys, "platform", "linux")
    assert md.read_mic_usage() == []
    assert md.list_window_titles() == []


def test_titles_filtered_by_kind_for_suggestion():
    h = {"usage": [use(TEAMS)]}
    titles = [("chrome.exe", "Other - Google Chrome"), ("ms-teams.exe", "Roadmap sync | Microsoft Teams")]
    det = make(h, titles=titles, start_debounce_sec=0)
    events = det.poll(0)
    assert events[0].suggested_name == "Roadmap sync"


# -- suggest_name ---------------------------------------------------------


def test_teams_title_stripping():
    assert suggest_name("teams", ["Weekly Sync | Microsoft Teams"], NOW) == ("Teams", "Weekly Sync")
    assert suggest_name("teams", ["Design review | Microsoft Teams (work or school)"], NOW)[1] == "Design review"
    assert suggest_name("teams", ["Standup (Meeting) | Microsoft Teams"], NOW)[1] == "Standup"


def test_teams_generic_titles_ignored_and_real_preferred():
    generic = [
        "Chat | Microsoft Teams", "Activity | Microsoft Teams", "Calendar | Microsoft Teams",
        "Microsoft Teams", "Meeting compact view", "Meeting controls", "Sharing control bar",
        "Chat | Contoso | mike@contoso.com | Microsoft Teams",
    ]
    assert suggest_name("teams", generic, NOW) == ("Teams", "Teams call 2:30 PM")
    _, name = suggest_name("teams", generic + ["Q3 planning | Microsoft Teams"], NOW)
    assert name == "Q3 planning"


def test_meet_code_and_title():
    assert suggest_name("browser", ["Meet - abc-defg-hij - Google Chrome"], NOW) == (
        "Google Meet", "Meet abc-defg-hij")
    assert suggest_name("browser", ["Meet \u2013 Team standup - Brave"], NOW) == ("Google Meet", "Team standup")
    assert suggest_name(
        "browser", ["Meet - Sprint demo - Personal - Microsoft\u200b Edge"], NOW
    ) == ("Google Meet", "Sprint demo")
    assert suggest_name("browser", ["Meet - Retro \u2014 Mozilla Firefox"], NOW)[1] == "Retro"


def test_teams_on_web_in_browser():
    assert suggest_name("browser", ["Sales call | Microsoft Teams - Google Chrome"], NOW) == (
        "Teams", "Sales call")


def test_browser_without_meeting_falls_back():
    assert suggest_name("browser", ["Inbox - Gmail - Google Chrome"], NOW) == (
        "Browser", "Browser call 2:30 PM")


def test_zoom_generic_titles_fall_back_with_time():
    assert suggest_name("zoom", ["Zoom Meeting", "Zoom Workplace"], NOW) == ("Zoom", "Zoom call 2:30 PM")
    assert suggest_name("zoom", [], datetime(2026, 1, 1, 0, 5))[1] == "Zoom call 12:05 AM"
    assert suggest_name("zoom", [], datetime(2026, 1, 1, 12, 0))[1] == "Zoom call 12:00 PM"
    assert suggest_name("zoom", [], datetime(2026, 1, 1, 9, 7))[1] == "Zoom call 9:07 AM"


def test_name_sanitized():
    _, name = suggest_name("teams", ["  Big   \n  meeting  | Microsoft Teams"], NOW)
    assert name == "Big meeting"
    _, long = suggest_name("teams", ["x" * 300 + " | Microsoft Teams"], NOW)
    assert len(long) == 120


# -- config ---------------------------------------------------------------


def test_meeting_detection_settings_defaults_and_clamping():
    assert config_mod.meeting_detection_settings({}) == {
        "enabled": True, "auto_stop": True, "suggest_stop": True, "end_grace_sec": 60}
    assert config_mod.meeting_detection_settings(
        {"meeting_detection": {"end_grace_sec": 1}})["end_grace_sec"] == 5
    assert config_mod.meeting_detection_settings(
        {"meeting_detection": {"end_grace_sec": 9999}})["end_grace_sec"] == 300
    assert config_mod.meeting_detection_settings(
        {"meeting_detection": {"end_grace_sec": "junk", "enabled": False}}) == {
        "enabled": False, "auto_stop": True, "suggest_stop": True, "end_grace_sec": 60}
    assert config_mod.meeting_detection_settings({"meeting_detection": "bad"})["enabled"] is True


def test_stale_in_use_entry_without_a_window_never_prompts():
    # A crashed Zoom can leave LastUsedTimeStop at 0; with no Zoom window open
    # that must not look like a call.
    h = {"usage": [use(ZOOM)]}
    det = make(h, titles=(("chrome.exe", "Inbox - Google Chrome"),), start_debounce_sec=0)
    for t in range(0, 30, 2):
        assert det.poll(float(t)) == []


def test_call_end_does_not_depend_on_windows():
    h = {"usage": [use(ZOOM)]}
    windows = {"titles": list(OPEN_WINDOWS)}
    det = MeetingDetector(
        read_usage=lambda: h["usage"], read_titles=lambda: windows["titles"],
        own_executable="x", start_debounce_sec=0, end_grace_sec=20, wall_clock=lambda: NOW,
    )
    assert [e.kind for e in det.poll(0)] == ["zoom"]
    windows["titles"] = []  # e.g. minimised to tray: still in the call
    assert det.poll(10) == [] and det.poll(60) == []


# -- end of call needs the call window gone too ------------------------------------


def _active(h, titles_holder, debounce=0, grace=20):
    det = MeetingDetector(
        read_usage=lambda: h["usage"], read_titles=lambda: titles_holder["titles"],
        own_executable="x", start_debounce_sec=debounce, end_grace_sec=grace, wall_clock=lambda: NOW,
    )
    assert isinstance(det.poll(0)[0], MeetingStarted)
    return det


def test_mic_released_but_zoom_meeting_window_open_is_not_the_end():
    h = {"usage": [use(ZOOM)]}
    w = {"titles": [("zoom.exe", "Zoom Meeting")]}
    det = _active(h, w)
    h["usage"] = []  # attendee muted / listen-only
    for t in range(2, 400, 2):
        assert det.poll(float(t)) == []
    # window closes: the grace period counts from then
    w["titles"] = [("zoom.exe", "Zoom Workplace")]
    assert det.poll(400.0) == [] and det.poll(410.0) == []
    assert [type(e) for e in det.poll(421.0)] == [MeetingEnded]


def test_mic_released_but_teams_meeting_window_open_is_not_the_end():
    h = {"usage": [use(TEAMS)]}
    w = {"titles": [("ms-teams.exe", "Weekly Sync | Microsoft Teams")]}
    det = _active(h, w)
    h["usage"] = []
    for t in range(2, 200, 2):
        assert det.poll(float(t)) == []
    w["titles"] = [("ms-teams.exe", "Chat | Microsoft Teams")]  # back at the chat list
    det.poll(200.0)
    assert [type(e) for e in det.poll(221.0)] == [MeetingEnded]


def test_browser_meet_tab_keeps_call_alive_until_it_is_gone():
    h = {"usage": [use(CHROME)]}
    w = {"titles": [("chrome.exe", "Meet - abc-defg-hij - Google Chrome")]}
    det = _active(h, w)
    h["usage"] = []
    for t in range(2, 100, 2):
        assert det.poll(float(t)) == []
    w["titles"] = [("chrome.exe", "Inbox - Google Chrome")]
    det.poll(100.0)
    assert [type(e) for e in det.poll(121.0)] == [MeetingEnded]


def test_has_call_window_is_conservative_but_ignores_idle_apps():
    assert md.has_call_window("zoom", ["Zoom Webinar"])
    assert not md.has_call_window("zoom", ["Zoom Workplace", "Settings"])
    assert md.has_call_window("teams", ["Meeting compact view"])
    assert not md.has_call_window("teams", ["Chat | Microsoft Teams", "Microsoft Teams"])
    assert not md.has_call_window("browser", ["New Tab - Google Chrome"])
