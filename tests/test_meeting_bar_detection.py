#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pytest>=8.0.0",
#     "rumps>=0.4.0",
#     "requests>=2.31.0",
#     "sounddevice>=0.5.0",
#     "pyobjc>=12.0",
# ]
# ///
"""Tests for meeting_bar.py meeting detection helpers."""

import subprocess
import sys
import importlib
import threading
import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "transcriber"))
import meeting_bar


class _FakeCompletedProcess:
    def __init__(self, stdout: str, returncode: int = 0):
        self.stdout = stdout
        self.returncode = returncode


def test_default_transcriber_target_is_pilot(monkeypatch):
    """Pilot remains the default HTTP and VBAN target."""
    monkeypatch.delenv("TRANSCRIBER_TARGET_HOST", raising=False)
    monkeypatch.delenv("TRANSCRIBER_URL", raising=False)
    monkeypatch.delenv("PILOT_HOST", raising=False)
    reloaded = importlib.reload(meeting_bar)
    try:
        assert reloaded.TRANSCRIBER_URL == "http://pilot:8000"
        assert reloaded.VBAN_TARGET_HOST == "pilot"
    finally:
        importlib.reload(meeting_bar)


def test_transcriber_target_host_configures_url_and_vban(monkeypatch):
    """A single target host setting should switch both HTTP and VBAN defaults."""
    monkeypatch.setenv("TRANSCRIBER_TARGET_HOST", "127.0.0.1")
    monkeypatch.delenv("TRANSCRIBER_URL", raising=False)
    monkeypatch.delenv("PILOT_HOST", raising=False)
    reloaded = importlib.reload(meeting_bar)
    try:
        assert reloaded.TRANSCRIBER_URL == "http://127.0.0.1:8000"
        assert reloaded.VBAN_TARGET_HOST == "127.0.0.1"
    finally:
        importlib.reload(meeting_bar)


def test_legacy_overrides_still_work(monkeypatch):
    """Existing TRANSCRIBER_URL/PILOT_HOST launchd config remains compatible."""
    monkeypatch.setenv("TRANSCRIBER_TARGET_HOST", "127.0.0.1")
    monkeypatch.setenv("TRANSCRIBER_URL", "http://pilot:8000")
    monkeypatch.setenv("PILOT_HOST", "pilot")
    reloaded = importlib.reload(meeting_bar)
    try:
        assert reloaded.TRANSCRIBER_URL == "http://pilot:8000"
        assert reloaded.VBAN_TARGET_HOST == "pilot"
    finally:
        importlib.reload(meeting_bar)


def _audiomxd_output(*states: tuple[str, bool]) -> str:
    lines = []
    for session_id, is_recording in states:
        state = "true" if is_recording else "false"
        lines.append(
            f"{{ sessionID: {session_id}, sessionType: 'prim', isRecording: {state} }},"
        )
    return "\n".join(lines)


def test_audiomxd_end_detection_keeps_cached_true_session(monkeypatch):
    """Unrelated Teams false side sessions should not stop a live call."""
    meeting_bar._AUDIOMXD_SESSION_STATES.clear()
    meeting_bar._AUDIOMXD_QUERY_CACHE.clear()
    monkeypatch.setattr(meeting_bar, "_AUDIOMXD_END_QUERY_TTL_SECONDS", 0)
    outputs = iter(
        [
            _audiomxd_output(("0x224002", True)),
            _audiomxd_output(("0x224004", False)),
        ]
    )

    def fake_run(*args, **kwargs):
        return _FakeCompletedProcess(next(outputs))

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert meeting_bar._audiomxd_session_active("Microsoft Teams") is True
    assert meeting_bar._audiomxd_session_active("Microsoft Teams") is True


def test_audiomxd_start_detection_ignores_cached_sessions(monkeypatch):
    """Start detection should not auto-start from an old cached true state."""
    meeting_bar._AUDIOMXD_SESSION_STATES.clear()
    meeting_bar._AUDIOMXD_QUERY_CACHE.clear()
    monkeypatch.setattr(meeting_bar, "_AUDIOMXD_END_QUERY_TTL_SECONDS", 0)
    meeting_bar._AUDIOMXD_SESSION_STATES["Microsoft Teams"] = {"0x224002": True}

    def fake_run(*args, **kwargs):
        return _FakeCompletedProcess("")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert (
        meeting_bar._audiomxd_session_active(
            "Microsoft Teams",
            default_if_no_entries=False,
            use_cached_sessions=False,
        )
        is False
    )


def test_audiomxd_same_session_false_ends_cached_session(monkeypatch):
    """The actual call session should become inactive when it reports false."""
    meeting_bar._AUDIOMXD_SESSION_STATES.clear()
    meeting_bar._AUDIOMXD_QUERY_CACHE.clear()
    monkeypatch.setattr(meeting_bar, "_AUDIOMXD_END_QUERY_TTL_SECONDS", 0)
    outputs = iter(
        [
            _audiomxd_output(("0x224002", True)),
            _audiomxd_output(("0x224002", False)),
        ]
    )

    def fake_run(*args, **kwargs):
        return _FakeCompletedProcess(next(outputs))

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert meeting_bar._audiomxd_session_active("Microsoft Teams") is True
    assert meeting_bar._audiomxd_session_active("Microsoft Teams") is False


def test_audiomxd_end_query_cache_throttles_log_show(monkeypatch):
    """End detection can throttle expensive unified-log queries while recording."""
    meeting_bar._AUDIOMXD_SESSION_STATES.clear()
    meeting_bar._AUDIOMXD_QUERY_CACHE.clear()
    monkeypatch.setattr(meeting_bar, "_AUDIOMXD_END_QUERY_TTL_SECONDS", 15)

    calls = 0

    def fake_run(*args, **kwargs):
        nonlocal calls
        calls += 1
        return _FakeCompletedProcess(_audiomxd_output(("0x224002", True)))

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(meeting_bar.time, "monotonic", lambda: 1000)

    assert meeting_bar._audiomxd_session_active("Microsoft Teams") is True
    assert meeting_bar._audiomxd_session_active("Microsoft Teams") is True
    assert calls == 1


def test_audiomxd_start_detection_bypasses_query_cache(monkeypatch):
    """Start detection keeps 5s polling fidelity instead of reusing query results."""
    meeting_bar._AUDIOMXD_SESSION_STATES.clear()
    meeting_bar._AUDIOMXD_QUERY_CACHE.clear()
    monkeypatch.setattr(meeting_bar, "_AUDIOMXD_END_QUERY_TTL_SECONDS", 15)

    outputs = iter([
        _audiomxd_output(("0x224002", False)),
        _audiomxd_output(("0x224002", True)),
    ])
    calls = 0

    def fake_run(*args, **kwargs):
        nonlocal calls
        calls += 1
        return _FakeCompletedProcess(next(outputs))

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(meeting_bar.time, "monotonic", lambda: 1000)

    assert (
        meeting_bar._audiomxd_session_active(
            "Microsoft Teams",
            default_if_no_entries=False,
            use_cached_sessions=False,
        )
        is False
    )
    assert (
        meeting_bar._audiomxd_session_active(
            "Microsoft Teams",
            default_if_no_entries=False,
            use_cached_sessions=False,
        )
        is True
    )
    assert calls == 2


def test_manual_start_infers_teams_from_recent_audio_session(monkeypatch):
    """Manual starts during muted/listen-only Teams calls can still auto-stop."""
    meeting_bar._AUDIOMXD_SESSION_STATES.clear()
    meeting_bar._AUDIOMXD_QUERY_CACHE.clear()
    monkeypatch.setattr(meeting_bar, "detect_meeting", lambda: None)
    monkeypatch.setattr(meeting_bar, "MANUAL_START_AUDIO_LOOKBACK_SECONDS", 600)

    def fake_audiomxd(app_name, default_if_no_entries, use_cached_sessions, window_seconds=None):
        assert app_name == "Microsoft Teams"
        assert default_if_no_entries is False
        assert use_cached_sessions is False
        assert window_seconds == 600
        return True

    monkeypatch.setattr(meeting_bar, "_teams_process_running", lambda: True)
    monkeypatch.setattr(meeting_bar, "_audiomxd_session_active", fake_audiomxd)

    assert meeting_bar.infer_manual_start_app() == "Teams"


def test_manual_start_falls_back_to_manual_without_recent_audio(monkeypatch):
    """Ad hoc manual recordings should not get a fake app owner."""
    meeting_bar._AUDIOMXD_SESSION_STATES.clear()
    meeting_bar._AUDIOMXD_QUERY_CACHE.clear()
    monkeypatch.setattr(meeting_bar, "detect_meeting", lambda: None)
    monkeypatch.setattr(meeting_bar, "_teams_process_running", lambda: False)

    def fake_audiomxd(*args, **kwargs):
        return False

    monkeypatch.setattr(meeting_bar, "_audiomxd_session_active", fake_audiomxd)

    assert meeting_bar.infer_manual_start_app() == "Manual"


def test_on_start_attaches_manual_recording_to_inferred_app(monkeypatch):
    """The menu callback passes the inferred app and auto-stop flag to _do_start."""
    app = meeting_bar.MeetingBarApp.__new__(meeting_bar.MeetingBarApp)
    app._lock = threading.Lock()
    app._recording = False
    app._busy = False
    app._schedule_ui_update = lambda: None
    starts = []

    monkeypatch.setattr(meeting_bar, "infer_manual_start_app", lambda: "Teams")
    monkeypatch.setattr(meeting_bar, "current_calendar_meeting", lambda: None)
    monkeypatch.setattr(meeting_bar, "lookup_calendar_title", lambda: "Current Meeting")

    class ImmediateThread:
        def __init__(self, target, args=(), kwargs=None, daemon=None):
            self.target = target
            self.args = args
            self.kwargs = kwargs or {}

        def start(self):
            self.target(*self.args, **self.kwargs)

    monkeypatch.setattr(meeting_bar.threading, "Thread", ImmediateThread)
    app._do_start = lambda title, app_name, auto, **kwargs: starts.append(
        (title, app_name, auto, kwargs["calendar_meeting"])
    )

    meeting_bar.MeetingBarApp.on_start(app, None)

    assert starts == [("Current Meeting", "Teams", True, None)]


def test_native_teams_detection_accepts_teams2_helper_process(monkeypatch):
    """Teams 2.x runs WebView/helper processes, not an exact MSTeams binary."""
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[:3] == ["pgrep", "-x", "MSTeams"]:
            return _FakeCompletedProcess("", returncode=1)
        if args[:2] == ["pgrep", "-f"] and "com\\.microsoft\\.teams2" in args[2]:
            return _FakeCompletedProcess("123\n", returncode=0)
        raise AssertionError(f"unexpected subprocess call: {args}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        meeting_bar,
        "_audiomxd_session_active",
        lambda app_name, default_if_no_entries, use_cached_sessions: True,
    )

    assert meeting_bar.detect_teams_meeting() is True
    assert calls == [
        ["pgrep", "-x", "MSTeams"],
        ["pgrep", "-f", r"/Microsoft Teams\.app/.*com\.microsoft\.teams2"],
    ]


def test_native_teams_detection_requires_teams_process(monkeypatch):
    """Stale audiomxd state alone should not start recording when Teams is absent."""

    def fake_run(args, **kwargs):
        if args[0] == "pgrep":
            return _FakeCompletedProcess("", returncode=1)
        raise AssertionError(f"unexpected subprocess call: {args}")

    def fail_audiomxd(*args, **kwargs):
        raise AssertionError("audiomxd should not be queried without Teams")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(meeting_bar, "_audiomxd_session_active", fail_audiomxd)
    assert meeting_bar.detect_teams_meeting() is False


def test_calendar_meeting_includes_start_end_and_stable_identity(tmp_path, monkeypatch):
    calendar = tmp_path / "outlook.org"
    calendar.write_text(
        "* Morning Sync <2026-08-13 Thu 09:00-09:30>\n"
        "* Tomorrow <2026-08-14 Fri 09:00-09:30>\n"
    )
    monkeypatch.setattr(meeting_bar, "CALENDAR_ORG", str(calendar))
    now = datetime.datetime(2026, 8, 13, 9, 5)

    meetings = meeting_bar.read_calendar_meetings(now)

    assert len(meetings) == 1
    assert meetings[0].title == "Morning Sync"
    assert meetings[0].start == datetime.datetime(2026, 8, 13, 9, 0)
    assert meetings[0].end == datetime.datetime(2026, 8, 13, 9, 30)
    assert meeting_bar.current_calendar_meeting(now, meetings) == meetings[0]
    assert meetings[0].occurrence_id == (
        "2026-08-13T09:00:00|2026-08-13T09:30:00|Morning Sync"
    )


def test_recording_reminder_waits_until_more_than_two_minutes(monkeypatch):
    meeting = meeting_bar.CalendarMeeting(
        "Morning Sync",
        datetime.datetime(2026, 8, 13, 9, 0),
        datetime.datetime(2026, 8, 13, 9, 30),
    )
    app = meeting_bar.MeetingBarApp.__new__(meeting_bar.MeetingBarApp)
    app._lock = threading.Lock()
    app._recording = False
    app._busy = False
    app._pending_reminder = None
    app._handled_meeting_ids = set()
    app._snoozed_meeting_ids = {}
    app._schedule_ui_update = lambda: None
    scheduled = []

    monkeypatch.setattr(meeting_bar, "RECORDING_REMINDER_SECONDS", 120)
    monkeypatch.setattr(meeting_bar, "current_calendar_meeting", lambda now: meeting)
    monkeypatch.setattr(
        meeting_bar, "callAfter", lambda callback, value: scheduled.append((callback, value))
    )

    app._check_recording_reminder(datetime.datetime(2026, 8, 13, 9, 2))
    assert app._pending_reminder is None

    app._check_recording_reminder(datetime.datetime(2026, 8, 13, 9, 2, 1))
    app._check_recording_reminder(datetime.datetime(2026, 8, 13, 9, 2, 6))

    assert app._pending_reminder == meeting
    assert scheduled == [(app._show_recording_reminder, meeting)]


def test_busy_start_does_not_dismiss_pending_reminder(monkeypatch):
    meeting = meeting_bar.CalendarMeeting(
        "Morning Sync",
        datetime.datetime(2026, 8, 13, 9, 0),
        datetime.datetime(2026, 8, 13, 9, 30),
    )
    app = meeting_bar.MeetingBarApp.__new__(meeting_bar.MeetingBarApp)
    app._lock = threading.Lock()
    app._recording = False
    app._busy = True
    app._pending_reminder = meeting
    app._handled_meeting_ids = set()
    app._snoozed_meeting_ids = {}

    monkeypatch.setattr(meeting_bar, "current_calendar_meeting", lambda now: meeting)

    app._check_recording_reminder(datetime.datetime(2026, 8, 13, 9, 5))

    assert app._pending_reminder == meeting
    assert meeting.occurrence_id not in app._handled_meeting_ids


def test_room_recording_uses_only_physical_microphone(monkeypatch):
    meeting = meeting_bar.CalendarMeeting(
        "Tablet Meeting",
        datetime.datetime(2026, 8, 13, 9, 0),
        datetime.datetime(2026, 8, 13, 9, 30),
    )
    app = meeting_bar.MeetingBarApp.__new__(meeting_bar.MeetingBarApp)
    app._lock = threading.Lock()
    app._recording = False
    app._busy = False
    app._pending_reminder = meeting
    app._handled_meeting_ids = set()
    app._confirm_app = None
    app._confirm_count = 0
    app._schedule_ui_update = lambda: None
    sender_calls = []
    api_calls = []

    monkeypatch.setattr(meeting_bar, "find_mic_device", lambda: "Desk Microphone")
    monkeypatch.setattr(
        meeting_bar, "start_sender", lambda device, mic=None: sender_calls.append((device, mic))
    )
    monkeypatch.setattr(
        meeting_bar,
        "transcriber_start",
        lambda title, capture_mode: api_calls.append((title, capture_mode))
        or {"status": "recording"},
    )
    monkeypatch.setattr(meeting_bar.time, "sleep", lambda _seconds: None)

    app._do_start(
        meeting.title,
        "Room",
        True,
        capture_mode="room",
        expected_end=meeting.end,
        calendar_meeting=meeting,
    )

    assert sender_calls == [("Desk Microphone", None)]
    assert api_calls == [("Tablet Meeting", "room")]
    assert app._recording_capture_mode == "room"
    assert app._recording_expected_end == meeting.end
    assert meeting.occurrence_id in app._handled_meeting_ids


def test_notification_action_starts_room_recording():
    meeting = meeting_bar.CalendarMeeting(
        "Tablet Meeting",
        datetime.datetime(2026, 8, 13, 9, 0),
        datetime.datetime(2026, 8, 13, 9, 30),
    )
    app = meeting_bar.MeetingBarApp.__new__(meeting_bar.MeetingBarApp)
    app._lock = threading.Lock()
    app._pending_reminder = meeting
    started = []
    app._start_room_recording = lambda value: started.append(value)

    class Notification:
        data = {"kind": "recording_reminder", "occurrence_id": meeting.occurrence_id}
        activation_type = "action_button_clicked"

    app._on_notification(Notification())

    assert started == [meeting]


def test_room_recording_auto_stops_after_calendar_grace(monkeypatch):
    app = meeting_bar.MeetingBarApp.__new__(meeting_bar.MeetingBarApp)
    app._lock = threading.Lock()
    app._recording = True
    app._recording_auto = True
    app._recording_app = "Room"
    app._recording_capture_mode = "room"
    app._recording_expected_end = datetime.datetime.now() - datetime.timedelta(minutes=3)
    app._busy = False
    app._detection_enabled = False
    app._transcriber_text = ""
    app._pending_reminder = None
    app._schedule_ui_update = lambda: None
    app._check_recording_reminder = lambda _now: None
    stops = []

    class ImmediateThread:
        def __init__(self, target, args=(), kwargs=None, daemon=None):
            self.target = target
            self.args = args
            self.kwargs = kwargs or {}

        def start(self):
            self.target(*self.args, **self.kwargs)

    monkeypatch.setattr(meeting_bar, "ROOM_RECORDING_END_GRACE_SECONDS", 120)
    monkeypatch.setattr(meeting_bar, "transcriber_status", lambda: {"status": "ok"})
    monkeypatch.setattr(
        meeting_bar,
        "detect_meeting",
        lambda: (_ for _ in ()).throw(AssertionError("room mode should skip app detection")),
    )
    monkeypatch.setattr(meeting_bar.threading, "Thread", ImmediateThread)
    app._do_stop = lambda: stops.append(True)

    app._poll_work()

    assert stops == [True]
