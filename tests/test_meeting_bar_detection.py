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

    def fake_run(args, *unused_args, **unused_kwargs):
        if args[:2] == ["pgrep", "-x"]:
            assert args[2] == "MSTeams"
            return _FakeCompletedProcess("", returncode=0)
        assert args[:3] == ["log", "show", "--last"]
        assert args[3] == "600s"
        return _FakeCompletedProcess(_audiomxd_output(("0x224002", True)))

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert meeting_bar.infer_manual_start_app() == "Teams"


def test_manual_start_falls_back_to_manual_without_recent_audio(monkeypatch):
    """Ad hoc manual recordings should not get a fake app owner."""
    meeting_bar._AUDIOMXD_SESSION_STATES.clear()
    meeting_bar._AUDIOMXD_QUERY_CACHE.clear()
    monkeypatch.setattr(meeting_bar, "detect_meeting", lambda: None)

    def fake_run(args, *unused_args, **unused_kwargs):
        if args[:2] == ["pgrep", "-x"]:
            return _FakeCompletedProcess("", returncode=1)
        return _FakeCompletedProcess("")

    monkeypatch.setattr(subprocess, "run", fake_run)

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
    monkeypatch.setattr(meeting_bar, "lookup_calendar_title", lambda: "Current Meeting")

    class ImmediateThread:
        def __init__(self, target, args=(), daemon=None):
            self.target = target
            self.args = args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(meeting_bar.threading, "Thread", ImmediateThread)
    app._do_start = lambda title, app_name, auto: starts.append((title, app_name, auto))

    meeting_bar.MeetingBarApp.on_start(app, None)

    assert starts == [("Current Meeting", "Teams", True)]
