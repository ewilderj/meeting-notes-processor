#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "rumps>=0.4.0",
#     "requests>=2.31.0",
#     "sounddevice>=0.5.0",
# ]
# ///
"""
Meeting Bar — macOS menu bar app for automatic meeting recording.

Sits in the menu bar showing recording state. Detects Zoom/Teams meetings
and automatically starts/stops recording via the VBAN → transcriber pipeline.

States:
  🎙  Idle
  🔴 Recording
  ⚠️  Error

Meeting detection:
  - Zoom: checks for CptHost subprocess (reliable in-meeting indicator)
  - Teams (native app): two-tier detection because new Teams 2.x exposes no window
    titles and AVCaptureDevice doesn't see its mic usage:
    * Start: Teams process tree running + audiomxd recording evidence
    * End: queries macOS audiomxd log for Teams audio session state, since
      our own VBAN sender keeps the mic active during recording
  - Teams PWA (Edge): queries audiomxd log for Edge helper audio sessions
    with isRecording state — works for both start and end detection

Usage:
  uv run meeting_bar.py
"""

import datetime
import logging
import os
import re
import signal
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import requests
import rumps
import sounddevice as sd
from PyObjCTools.AppHelper import callAfter

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TRANSCRIBER_TARGET_HOST = os.getenv("TRANSCRIBER_TARGET_HOST", "pilot")
TRANSCRIBER_URL = os.getenv("TRANSCRIBER_URL", f"http://{TRANSCRIBER_TARGET_HOST}:8000")
VBAN_TARGET_HOST = os.getenv("PILOT_HOST", TRANSCRIBER_TARGET_HOST)
VBAN_PORT = int(os.getenv("VBAN_PORT", "6980"))
POLL_INTERVAL = int(os.getenv("MEETING_POLL_INTERVAL", "5"))  # seconds
# All app detections require this many consecutive positive polls before
# triggering a recording start (each poll is POLL_INTERVAL seconds).
# Default 3 = 15 seconds of sustained detection, which filters out transient
# mic probes (Teams/Edge connectivity checks, PWA startup, etc.).
CONFIRM_POLLS = int(os.getenv("MEETING_CONFIRM_POLLS", "3"))
MANUAL_START_AUDIO_LOOKBACK_SECONDS = int(
    os.getenv("MANUAL_START_AUDIO_LOOKBACK_SECONDS", "600")
)
RECORDING_REMINDER_SECONDS = int(os.getenv("MEETING_RECORDING_REMINDER_SECONDS", "120"))
ROOM_RECORDING_END_GRACE_SECONDS = int(
    os.getenv("ROOM_RECORDING_END_GRACE_SECONDS", "120")
)

# Calendar org file for meeting title lookup (optional)
CALENDAR_ORG = os.getenv("MEETING_CALENDAR_ORG", os.path.expanduser("~/gtd/outlook.org"))

DEVICE_PREFERENCE = [
    "BlackHole 2ch",
    "ZoomAudioDevice",
    "Microsoft Teams",
]

VBAN_SEND_SCRIPT = Path(__file__).parent / "vban" / "vban_send.py"
PID_FILE = Path(os.getenv("MEETING_PID_FILE", "/tmp/meeting-vban-sender.pid"))
LOG_FILE = Path(os.getenv("MEETING_LOG_FILE", "/tmp/meeting-vban-sender.log"))

LOG_PATH = Path("/tmp/meeting-bar.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [meeting-bar] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("meeting-bar")

ICON_IDLE = "🎙"
ICON_RECORDING = "🔴"
ICON_ERROR = "⚠️"

# Compiled Swift helper for CoreAudio mic detection (Teams 2.x)
MIC_ACTIVE_BIN = Path(__file__).parent / "mic_active"


# ---------------------------------------------------------------------------
# Calendar Title Lookup
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CalendarMeeting:
    title: str
    start: datetime.datetime
    end: datetime.datetime
    has_video_call: bool = False

    @property
    def occurrence_id(self) -> str:
        return f"{self.start.isoformat()}|{self.end.isoformat()}|{self.title}"


def read_calendar_meetings(now: datetime.datetime | None = None) -> list[CalendarMeeting]:
    """Return today's timed calendar meetings from the Org calendar."""
    cal_path = Path(CALENDAR_ORG)
    if not cal_path.exists():
        return []

    if now is None:
        now = datetime.datetime.now()

    try:
        content = cal_path.read_text(encoding="utf-8")
    except OSError as e:
        logger.debug(f"Cannot read calendar file: {e}")
        return []

    today_str = now.strftime("%Y-%m-%d")
    entry_re = re.compile(
        r'^\* (.+?) <(\d{4}-\d{2}-\d{2}) \w{3} (\d{2}:\d{2})-(\d{2}:\d{2})>',
        re.MULTILINE,
    )
    join_call_re = re.compile(
        r'\[\[https?://[^\]\n]+\]\[[^\]\n]*Join Call[^\]\n]*\]\]',
        re.IGNORECASE,
    )
    meetings = []
    matches = list(entry_re.finditer(content))
    for index, match in enumerate(matches):
        if match.group(2) != today_str:
            continue
        try:
            start = datetime.datetime.strptime(
                f"{match.group(2)} {match.group(3)}", "%Y-%m-%d %H:%M"
            )
            end = datetime.datetime.strptime(
                f"{match.group(2)} {match.group(4)}", "%Y-%m-%d %H:%M"
            )
        except ValueError:
            continue
        if end <= start:
            continue
        entry_end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        entry_body = content[match.end():entry_end]
        meetings.append(
            CalendarMeeting(
                match.group(1).strip(),
                start,
                end,
                has_video_call=bool(join_call_re.search(entry_body)),
            )
        )
    return meetings


def current_calendar_meeting(
    now: datetime.datetime | None = None,
    meetings: list[CalendarMeeting] | None = None,
    *,
    require_video_call: bool = False,
) -> CalendarMeeting | None:
    """Return the most recently started calendar meeting active at `now`."""
    if now is None:
        now = datetime.datetime.now()
    if meetings is None:
        meetings = read_calendar_meetings(now)
    active = [
        meeting
        for meeting in meetings
        if meeting.start <= now < meeting.end
        and (meeting.has_video_call or not require_video_call)
    ]
    return max(active, key=lambda meeting: meeting.start, default=None)


def lookup_calendar_title(now: datetime.datetime | None = None) -> str | None:
    """Find the best matching calendar entry title for the current time.

    Parses CALENDAR_ORG (org-mode format) and finds the meeting whose start
    time is closest to `now`, subject to these rules:
      - Nothing ever starts more than 5 minutes early
      - If we're more than 25 minutes past a meeting's start time, it's
        likely a spontaneous meeting (return None)
      - Only considers today's entries

    Returns the meeting title string, or None if no match.
    """
    if now is None:
        now = datetime.datetime.now()

    best_title = None
    best_delta = None  # seconds from meeting start to now (positive = we're late)

    for meeting in read_calendar_meetings(now):
        delta_s = (now - meeting.start).total_seconds()

        # Skip if meeting hasn't started yet and we're more than 5 min early
        if delta_s < -300:
            continue
        # Skip if we're more than 25 min past the start
        if delta_s > 1500:
            continue

        abs_delta = abs(delta_s)
        if best_delta is None or abs_delta < best_delta:
            best_delta = abs_delta
            best_title = meeting.title

    return best_title

# ---------------------------------------------------------------------------
# Meeting Detection
# ---------------------------------------------------------------------------


def detect_zoom_meeting() -> bool:
    """Check for CptHost process (only runs during active Zoom meetings)."""
    try:
        result = subprocess.run(
            ["pgrep", "-x", "CptHost"], capture_output=True, timeout=3,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _physical_mic_active() -> bool:
    """Check if any physical microphone has active CoreAudio I/O.

    Calls the compiled mic_active helper (Swift/CoreAudio) which checks
    kAudioDevicePropertyDeviceIsRunningSomewhere on physical input devices,
    ignoring virtual devices (BlackHole, ZoomAudioDevice, Teams Audio, etc.).

    AVCaptureDevice.isInUseByAnotherApplication() does NOT work for Teams 2.x
    because Teams uses CoreAudio directly, not AVCaptureDevice.
    """
    if not MIC_ACTIVE_BIN.exists():
        logger.warning(f"mic_active binary not found at {MIC_ACTIVE_BIN}")
        return False
    try:
        result = subprocess.run(
            [str(MIC_ACTIVE_BIN)], capture_output=True, text=True, timeout=3,
        )
        return result.stdout.strip() == "YES"
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.debug(f"mic_active check failed: {e}")
        return False


def detect_teams_meeting() -> bool:
    """Check if Teams is in an active call (for START detection).

    Uses audiomxd to check if native Teams has an active recording session,
    rather than checking if any physical mic is active.  This correctly
    ignores other apps that open the mic (e.g. Handy dictation, audio
    recorders) while MSTeams is running but idle.

    WARNING: This check is only reliable for START detection. Once our VBAN
    sender is running, it keeps its own audio session, so end detection uses
    _teams_audio_session_active() (which uses the conservative True default).
    """
    try:
        if not _teams_process_running():
            return False
        # Check if native Teams specifically has an active recording session.
        # default_if_no_entries=False: absence of recent evidence means idle.
        # audiomxd logs Teams as "Microsoft Teams" regardless of whether the
        # process tree is legacy MSTeams or newer com.microsoft.teams2 helpers.
        return _audiomxd_session_active(
            "Microsoft Teams",
            default_if_no_entries=False,
            use_cached_sessions=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False


def _teams_process_running() -> bool:
    """Return True when either legacy MSTeams or Teams 2.x helpers are alive."""
    probes = [
        ["pgrep", "-x", "MSTeams"],
        ["pgrep", "-f", r"/Microsoft Teams\.app/.*com\.microsoft\.teams2"],
        ["pgrep", "-f", r"/Microsoft Teams\.app/.*Microsoft Teams WebView"],
    ]
    for probe in probes:
        result = subprocess.run(probe, capture_output=True, timeout=3)
        if result.returncode == 0:
            return True
    return False


_AUDIOMXD_SESSION_RE = re.compile(
    r"sessionID:\s*(0x[0-9a-fA-F]+).*?isRecording:\s*(true|false)"
)
_AUDIOMXD_TRANSITION_RE = re.compile(
    r"MXCoreSession\s+sid:(0x[0-9a-fA-F]+).*?\b(starting|stopping)\s+recording\b"
)
_AUDIOMXD_WINDOW_SECONDS = 120
_AUDIOMXD_END_QUERY_TTL_SECONDS = float(os.getenv("AUDIOMXD_END_QUERY_TTL_SECONDS", "15"))
_AUDIOMXD_SESSION_STATES: dict[str, dict[str, bool]] = {}
_AUDIOMXD_QUERY_CACHE: dict[tuple[str, int], tuple[float, dict[str, bool]]] = {}


def _audiomxd_session_active(
    app_name: str,
    default_if_no_entries: bool = True,
    use_cached_sessions: bool = True,
    window_seconds: int | None = None,
) -> bool:
    """Check if an app has an active audio session via macOS audiomxd logs.

    Queries the system log for audio session state transitions for the given
    app name.  The audiomxd daemon logs 'isRecording: true/false' whenever an
    app starts or stops an audio session (call join/leave).

    Per-session state tracking is required because:
      - Teams maintains multiple concurrent audio sessions (e.g. main session
        plus helper sessions for different audio routes).  Just picking the
        most recent isRecording event mixes sessions and can be masked by a
        stale 'false' from a side session while the call session is 'true'.
      - audiomxd is transition-driven, so a long call may produce zero events
        in a short window.  We need a wide enough window to catch the start
        transition.

    Algorithm:
      1. Fetch all audiomxd events in the last 120s mentioning app_name and
         isRecording (these multi-line events include a bracketed summary
         'sessionID: 0x...  isRecording: true/false').
      2. Walk chronologically, recording the latest state per sessionID.
      3. Merge parsed states into an app-level cache so a true session remains
         active across polling windows until that same session reports false.
      4. Return True iff any tracked session's last state is True.
      5. If no sessions were parsed and no cache is used, return
         default_if_no_entries.

    The cache matters for END detection: Teams often logs the real call
    session's isRecording:true only at call start, then logs unrelated
    "[implicit] Microsoft Teams" side sessions as isRecording:false. Without
    remembering the true session, a later short polling window can contain
    only the unrelated false events and stop a live recording.

    Args:
        default_if_no_entries: Returned when no sessions are parsed (e.g. no
            recent activity, or subprocess error).  Use True for END
            detection (conservative: assume call still active if no info).
            Use False for START detection (no evidence → not in a call).
        use_cached_sessions: Include cached session state in the return value.
            Use False for START detection to avoid stale positives from older
            calls; use True for END detection to bridge quiet log windows.
    """
    log_window_seconds = window_seconds or _AUDIOMXD_WINDOW_SECONDS
    query_ttl = _AUDIOMXD_END_QUERY_TTL_SECONDS if use_cached_sessions else 0
    now = time.monotonic()
    cache_key = (app_name, log_window_seconds)
    cached_query = _AUDIOMXD_QUERY_CACHE.get(cache_key)
    if cached_query is not None and query_ttl > 0 and now - cached_query[0] < query_ttl:
        session_states = cached_query[1]
    else:
        try:
            result = subprocess.run(
                ["log", "show", "--last", f"{log_window_seconds}s",
                 "--predicate", f'process == "audiomxd" AND eventMessage CONTAINS "{app_name}" AND eventMessage CONTAINS "isRecording"',
                 "--style", "compact"],
                capture_output=True, text=True, timeout=15,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.debug(f"audiomxd log check failed: {e}")
            return default_if_no_entries

        # Chronological scan; latest event per sessionID wins.
        session_states = {}
        for line in result.stdout.splitlines():
            m = _AUDIOMXD_SESSION_RE.search(line)
            if m:
                session_states[m.group(1).lower()] = (m.group(2) == "true")
                continue
            m = _AUDIOMXD_TRANSITION_RE.search(line)
            if m:
                session_states[m.group(1).lower()] = (m.group(2) == "starting")
        _AUDIOMXD_QUERY_CACHE[cache_key] = (now, session_states)

    if not session_states:
        cached = _AUDIOMXD_SESSION_STATES.get(app_name, {})
        if use_cached_sessions and cached:
            return any(cached.values())
        return default_if_no_entries

    cached = _AUDIOMXD_SESSION_STATES.setdefault(app_name, {})
    cached.update(session_states)

    if use_cached_sessions:
        return any(cached.values())
    return any(session_states.values())


def _teams_audio_session_active() -> bool:
    """Check if native Teams has an active audio session."""
    return _audiomxd_session_active("Microsoft Teams")


def _teams_audio_session_recently_active(window_seconds: int) -> bool:
    """Check recent native Teams audio state for manual-start recovery."""
    try:
        if not _teams_process_running():
            return False
    except (subprocess.TimeoutExpired, OSError):
        return False

    return _audiomxd_session_active(
        "Microsoft Teams",
        default_if_no_entries=False,
        use_cached_sessions=False,
        window_seconds=window_seconds,
    )


def infer_manual_start_app() -> str:
    """Infer which app should own auto-stop for a user-initiated recording."""
    meeting_app = detect_meeting()
    if meeting_app:
        return meeting_app
    if _teams_audio_session_recently_active(MANUAL_START_AUDIO_LOOKBACK_SECONDS):
        return "Teams"
    if _audiomxd_session_active(
        "Microsoft Edge",
        default_if_no_entries=False,
        use_cached_sessions=False,
        window_seconds=MANUAL_START_AUDIO_LOOKBACK_SECONDS,
    ):
        return "EdgeTeams"
    return "Manual"


def detect_edge_teams_meeting() -> bool:
    """Check if Teams PWA (running in Edge) is in an active call.

    Uses audiomxd to check if Microsoft Edge has an active recording session.
    default_if_no_entries=False means no recent evidence → not in a call,
    avoiding false positives from stale entries or other apps using the mic.
    """
    return _audiomxd_session_active(
        "Microsoft Edge",
        default_if_no_entries=False,
        use_cached_sessions=False,
    )


def detect_meeting() -> str | None:
    """Returns "Zoom", "Teams", "EdgeTeams", or None."""
    if detect_zoom_meeting():
        return "Zoom"
    if detect_teams_meeting():
        return "Teams"
    if detect_edge_teams_meeting():
        return "EdgeTeams"
    return None


# ---------------------------------------------------------------------------
# Audio Device Discovery
# ---------------------------------------------------------------------------


def find_best_device() -> tuple[str | None, str | None]:
    devices = sd.query_devices()
    available = {d["name"]: d for d in devices if d["max_input_channels"] > 0}
    for pref in DEVICE_PREFERENCE:
        for name in available:
            if pref.lower() in name.lower():
                quality = "full" if "blackhole" in name.lower() else "partial"
                return name, quality
    return None, None


def find_mic_device() -> str | None:
    devices = sd.query_devices()
    default_idx = sd.default.device[0]
    if default_idx is not None and default_idx >= 0:
        d = devices[default_idx]
        if d["max_input_channels"] > 0 and not any(
            s in d["name"].lower() for s in ["blackhole", "zoom", "teams"]
        ):
            return d["name"]
    for d in devices:
        if d["max_input_channels"] > 0 and not any(
            s in d["name"].lower() for s in ["blackhole", "zoom", "teams"]
        ):
            return d["name"]
    return None


# ---------------------------------------------------------------------------
# VBAN Sender Management
# ---------------------------------------------------------------------------


def _sender_running() -> int | None:
    if not PID_FILE.exists():
        return None
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (ProcessLookupError, ValueError):
        PID_FILE.unlink(missing_ok=True)
        return None


def start_sender(device: str, mic: str | None = None) -> int:
    existing = _sender_running()
    if existing:
        logger.info(f"VBAN sender already running (PID {existing})")
        return existing
    cmd = ["uv", "run", str(VBAN_SEND_SCRIPT), "-d", device, "-t", VBAN_TARGET_HOST, "-p", str(VBAN_PORT)]
    if mic:
        cmd.extend(["--mic", mic])
    log_fh = open(LOG_FILE, "w")
    proc = subprocess.Popen(cmd, stdout=log_fh, stderr=subprocess.STDOUT, start_new_session=True)
    PID_FILE.write_text(str(proc.pid))
    mode = f"mixed ({device} + {mic})" if mic else device
    logger.info(f"VBAN sender started (PID {proc.pid}) → {mode}")
    return proc.pid


def stop_sender():
    pid = _sender_running()
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
            for _ in range(10):
                try:
                    os.kill(pid, 0)
                    time.sleep(0.2)
                except ProcessLookupError:
                    break
        except ProcessLookupError:
            pass
        PID_FILE.unlink(missing_ok=True)
        logger.info(f"VBAN sender stopped (PID {pid})")


# ---------------------------------------------------------------------------
# DNS pre-resolution (prevents getaddrinfo from stalling the poll loop)
# ---------------------------------------------------------------------------

_dns_ok = True  # track transitions for logging


def _resolve_host(hostname: str, timeout: float = 3.0) -> bool:
    """Resolve hostname with a timeout. Returns True if reachable."""
    result = [None]

    def resolve():
        try:
            result[0] = socket.getaddrinfo(hostname, None, socket.AF_INET)
        except socket.gaierror:
            pass

    t = threading.Thread(target=resolve, daemon=True)
    t.start()
    t.join(timeout)
    return result[0] is not None


def _check_transcriber_dns() -> bool:
    """Pre-check DNS for the configured transcriber host; log transitions."""
    global _dns_ok
    from urllib.parse import urlparse
    hostname = urlparse(TRANSCRIBER_URL).hostname
    ok = _resolve_host(hostname)
    if ok and not _dns_ok:
        logger.info(f"Transcriber DNS reachable again ({hostname})")
    elif not ok and _dns_ok:
        logger.warning(f"Transcriber DNS unreachable ({hostname}) — skipping poll")
    _dns_ok = ok
    return ok


# ---------------------------------------------------------------------------
# Transcriber API
# ---------------------------------------------------------------------------


_transcriber_connected = True  # track connection state for transition logging


def transcriber_status() -> dict | None:
    global _transcriber_connected
    if not _check_transcriber_dns():
        if _transcriber_connected:
            logger.warning("Lost connection to transcriber")
            _transcriber_connected = False
        return None
    try:
        result = requests.get(f"{TRANSCRIBER_URL}/status", timeout=5).json()
        if not _transcriber_connected:
            logger.info("Reconnected to transcriber")
            _transcriber_connected = True
        return result
    except requests.RequestException:
        if _transcriber_connected:
            logger.warning("Lost connection to transcriber")
            _transcriber_connected = False
        return None


def transcriber_start(title: str, capture_mode: str = "standard") -> dict | None:
    try:
        r = requests.post(
            f"{TRANSCRIBER_URL}/start",
            json={"title": title, "capture_mode": capture_mode},
            timeout=10,
        )
        if r.status_code == 409:
            logger.warning(f"Already recording: {r.json().get('detail', '')}")
            return r.json()
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        logger.error(f"Failed to start recording: {e}")
        return None


def transcriber_stop() -> dict | None:
    try:
        r = requests.post(f"{TRANSCRIBER_URL}/stop", timeout=10)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        logger.error(f"Failed to stop recording: {e}")
        return None


# ---------------------------------------------------------------------------
# Menu Bar App
#
# THREADING MODEL:
#   - Main thread: NSApplication run loop. All Cocoa UI access here only.
#   - Poll thread: network I/O + meeting detection. Sets Python-only flags.
#   - Recording threads: short-lived start/stop. Sets Python-only flags.
#
# RULE: Background threads NEVER touch Cocoa objects (no set_callback,
# no .title=, no rumps.alert). They only set Python variables under _lock.
# The main thread reads those variables when the user opens the menu.
# ---------------------------------------------------------------------------


class MeetingBarApp(rumps.App):
    def __init__(self):
        super().__init__(name="Meeting Bar", title=ICON_IDLE, quit_button=None)

        self._lock = threading.Lock()
        self._recording = False
        self._recording_title: str | None = None
        self._recording_app: str | None = None
        self._recording_auto: bool = False
        self._recording_capture_mode = "standard"
        self._recording_expected_end: datetime.datetime | None = None
        self._started_at: datetime.datetime | None = None
        self._detection_enabled = True
        self._busy = False  # True while start/stop in progress
        self._suppress_auto = False  # True after manual stop of auto-started recording
        self._transcriber_text = "Transcriber: checking…"
        self._confirm_app: str | None = None  # app being debounced
        self._confirm_count = 0  # consecutive detection count for debounce
        self._pending_reminder: CalendarMeeting | None = None
        self._handled_meeting_ids: set[str] = set()
        self._snoozed_meeting_ids: dict[str, datetime.datetime] = {}

        # Menu — all items always have callbacks via @rumps.clicked.
        # Keep refs so we can update visibility via callAfter.
        self._status_item = rumps.MenuItem("Status: Idle")
        self._start_item = rumps.MenuItem("Start Recording")
        self._room_start_item = rumps.MenuItem("Start Room Recording")
        self._stop_item = rumps.MenuItem("Stop Recording")
        self._snooze_item = rumps.MenuItem("Remind in 5 Minutes")
        self._not_attending_item = rumps.MenuItem("Not Attending")
        self._transcriber_item = rumps.MenuItem("Transcriber: checking…")
        self.menu = [
            self._status_item,
            None,
            self._start_item,
            self._room_start_item,
            self._stop_item,
            self._snooze_item,
            self._not_attending_item,
            None,
            rumps.MenuItem("Auto-Detect Meetings"),
            self._transcriber_item,
            rumps.MenuItem("View Log…"),
            None,
            rumps.MenuItem("Quit Meeting Bar"),
        ]
        self.menu["Auto-Detect Meetings"].state = 1

        # Initial UI state: hide Stop Recording
        self._stop_item.hidden = True
        self._snooze_item.hidden = True
        self._not_attending_item.hidden = True
        rumps.notifications(self._on_notification)

        # Start background polling
        threading.Thread(target=self._poll_loop, daemon=True).start()

    # -------------------------------------------------------------------
    # Helpers (main thread only)
    # -------------------------------------------------------------------

    @property
    def _duration(self) -> str:
        if not self._started_at:
            return "0:00"
        delta = datetime.datetime.now() - self._started_at
        m, s = divmod(int(delta.total_seconds()), 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    # -------------------------------------------------------------------
    # Main-thread UI sync via callAfter
    # -------------------------------------------------------------------

    def _schedule_ui_update(self):
        """Schedule a UI state sync on the main thread."""
        try:
            callAfter(self._apply_ui_state)
        except Exception as e:
            logger.debug(f"callAfter UI update failed: {e}")

    def _apply_ui_state(self):
        """Apply current state to all UI elements. Runs on main thread."""
        try:
            with self._lock:
                recording = self._recording
                busy = self._busy
                rec_title = self._recording_title
                transcriber = self._transcriber_text
                reminder = self._pending_reminder

            # Icon
            if recording:
                self.title = ICON_RECORDING
            elif busy:
                self.title = "⏳"
            elif reminder:
                self.title = ICON_ERROR
            else:
                self.title = ICON_IDLE

            # Status text
            if recording:
                self._status_item.title = f"Recording: {rec_title} ({self._duration})"
            elif busy:
                self._status_item.title = "Starting…"
            elif reminder:
                self._status_item.title = f"Not recording: {reminder.title}"
            else:
                self._status_item.title = "Status: Idle"

            # Show/hide Start and Stop mutually exclusively
            self._start_item.hidden = recording or busy
            self._room_start_item.hidden = recording or busy
            self._stop_item.hidden = not recording
            self._snooze_item.hidden = reminder is None or recording or busy
            self._not_attending_item.hidden = reminder is None or recording or busy

            # Pilot status
            self._transcriber_item.title = transcriber
        except Exception as e:
            logger.error(f"_apply_ui_state error: {e}", exc_info=True)

    # -------------------------------------------------------------------
    # Background polling
    # -------------------------------------------------------------------

    def _show_recording_reminder(self, meeting: CalendarMeeting):
        """Show an actionable notification on the main thread."""
        with self._lock:
            if self._pending_reminder != meeting or self._recording or self._busy:
                return
        logger.info(f"Recording reminder: '{meeting.title}'")
        rumps.notification(
            "Meeting Bar",
            "Meeting is not being recorded",
            meeting.title,
            data={"kind": "recording_reminder", "occurrence_id": meeting.occurrence_id},
            action_button="Start Room Recording",
            other_button="Not attending",
        )

    def _check_recording_reminder(self, now: datetime.datetime):
        """Schedule one reminder for an unrecorded calendar occurrence."""
        meeting = current_calendar_meeting(now, require_video_call=True)
        with self._lock:
            if self._recording:
                if self._pending_reminder:
                    self._handled_meeting_ids.add(self._pending_reminder.occurrence_id)
                    self._pending_reminder = None
                return
            if self._busy:
                return

            if meeting is None:
                self._pending_reminder = None
                return

            occurrence_id = meeting.occurrence_id
            snoozed_until = self._snoozed_meeting_ids.get(occurrence_id)
            blocked = occurrence_id in self._handled_meeting_ids
            blocked = blocked or (snoozed_until is not None and now < snoozed_until)
            elapsed = (now - meeting.start).total_seconds()
            if blocked or elapsed <= RECORDING_REMINDER_SECONDS:
                if self._pending_reminder == meeting:
                    self._pending_reminder = None
                return

            if self._pending_reminder == meeting:
                return
            self._pending_reminder = meeting

        self._schedule_ui_update()
        callAfter(self._show_recording_reminder, meeting)

    def _resolve_reminder(self, *, handled: bool, snooze_minutes: int = 0):
        with self._lock:
            meeting = self._pending_reminder
            if meeting is None:
                return None
            if handled:
                self._handled_meeting_ids.add(meeting.occurrence_id)
            elif snooze_minutes:
                self._snoozed_meeting_ids[meeting.occurrence_id] = (
                    datetime.datetime.now() + datetime.timedelta(minutes=snooze_minutes)
                )
            self._pending_reminder = None
        self._schedule_ui_update()
        return meeting

    def _on_notification(self, notification):
        """Handle the two supported macOS notification actions."""
        data = notification.data
        if not isinstance(data, dict) or data.get("kind") != "recording_reminder":
            return
        with self._lock:
            meeting = self._pending_reminder
        if meeting is None or data.get("occurrence_id") != meeting.occurrence_id:
            return

        if notification.activation_type == "action_button_clicked":
            self._start_room_recording(meeting)
        elif notification.activation_type == "additional_action_clicked":
            self._resolve_reminder(handled=True)
            logger.info(f"Reminder dismissed as not attending: '{meeting.title}'")

    def _poll_loop(self):
        time.sleep(2)
        logger.info("Detection loop active")
        while True:
            try:
                self._poll_work()
            except Exception as e:
                logger.error(f"Poll error: {e}", exc_info=True)
            time.sleep(POLL_INTERVAL)

    def _poll_work(self):
        # Transcriber status (network I/O)
        status = transcriber_status()
        transcriber_text = "Transcriber: connected" if status else "Transcriber: unreachable"
        if status and status.get("recording"):
            transcriber_text += f" — rec '{status['recording'].get('title', '?')}'"

        with self._lock:
            self._transcriber_text = transcriber_text

        # Schedule full UI update on main thread
        self._schedule_ui_update()

        now = datetime.datetime.now()
        self._check_recording_reminder(now)

        if self._busy:
            return

        with self._lock:
            is_recording = self._recording
            is_auto = self._recording_auto
            rec_app = self._recording_app
            capture_mode = self._recording_capture_mode
            expected_end = self._recording_expected_end

        if is_recording and capture_mode == "room":
            if expected_end and now >= expected_end + datetime.timedelta(
                seconds=ROOM_RECORDING_END_GRACE_SECONDS
            ):
                logger.info(f"Room meeting reached calendar end (was: {rec_app})")
                threading.Thread(target=self._do_stop, daemon=True).start()
            return

        # App-based meeting detection can be disabled without disabling
        # calendar reminders or room-recording end timers.
        if not self._detection_enabled:
            return

        meeting_app = detect_meeting()

        # Debounce all app types to filter transient mic probes.
        # Require CONFIRM_POLLS consecutive detections of the SAME app before
        # treating it as a real call.  Switching apps or going idle resets the
        # count immediately.
        if meeting_app is not None:
            if meeting_app == self._confirm_app:
                self._confirm_count += 1
            else:
                self._confirm_app = meeting_app
                self._confirm_count = 1
            if self._confirm_count < CONFIRM_POLLS:
                logger.debug(
                    f"{meeting_app} tentative detection {self._confirm_count}/{CONFIRM_POLLS}"
                )
                meeting_app = None  # not confirmed yet
        else:
            if self._confirm_count > 0:
                logger.debug(f"{self._confirm_app} detection reset (not sustained)")
            self._confirm_app = None
            self._confirm_count = 0

        if not is_recording and meeting_app:
            if self._suppress_auto:
                pass  # User manually stopped; wait for meeting to end
            else:
                logger.info(f"Meeting detected: {meeting_app}")
                calendar_meeting = current_calendar_meeting(now)
                cal_title = calendar_meeting.title if calendar_meeting else lookup_calendar_title(now)
                if cal_title:
                    title = cal_title
                    logger.info(f"Calendar match: '{cal_title}'")
                else:
                    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
                    title = f"{meeting_app} Meeting {timestamp}"
                threading.Thread(
                    target=self._do_start,
                    args=(title, meeting_app, True),
                    kwargs={"calendar_meeting": calendar_meeting},
                    daemon=True,
                ).start()

        elif is_recording and is_auto:
            # End detection is app-specific because our VBAN sender keeps
            # the physical mic active, making generic detect_meeting()
            # unreliable (it would see "Teams" even during a Zoom recording).
            if rec_app == "Zoom":
                still_active = detect_zoom_meeting()
            elif rec_app == "Teams":
                still_active = _teams_audio_session_active()
            elif rec_app == "EdgeTeams":
                still_active = _audiomxd_session_active("Microsoft Edge")
            else:
                still_active = meeting_app is not None

            if not still_active:
                logger.info(f"Meeting ended (was: {rec_app})")
                threading.Thread(target=self._do_stop, daemon=True).start()

        elif not meeting_app and self._suppress_auto:
            logger.info("Meeting ended, clearing auto-suppress")
            self._suppress_auto = False

    # -------------------------------------------------------------------
    # Recording pipeline (background threads)
    # -------------------------------------------------------------------

    def _do_start(
        self,
        title: str,
        app: str,
        auto: bool = False,
        *,
        capture_mode: str = "standard",
        expected_end: datetime.datetime | None = None,
        calendar_meeting: CalendarMeeting | None = None,
    ):
        with self._lock:
            if self._recording or self._busy:
                return
            self._busy = True

        try:
            logger.info(
                f"Starting recording: '{title}' ({app}, auto={auto}, capture={capture_mode})"
            )

            if capture_mode == "room":
                device_name = find_mic_device()
                quality = "room"
            else:
                device_name, quality = find_best_device()
            if not device_name:
                logger.error("No suitable audio device found")
                return

            mic_name = find_mic_device() if quality == "full" else None
            start_sender(device_name, mic=mic_name)
            time.sleep(3)

            result = transcriber_start(title, capture_mode)
            if not result:
                logger.error("Failed to start recording on transcriber")
                stop_sender()
                return

            with self._lock:
                self._recording = True
                self._recording_title = title
                self._recording_app = app
                self._recording_auto = auto
                self._recording_capture_mode = capture_mode
                self._recording_expected_end = expected_end
                self._started_at = datetime.datetime.now()
                self._confirm_app = None  # reset for next detection cycle
                self._confirm_count = 0
                if calendar_meeting:
                    self._handled_meeting_ids.add(calendar_meeting.occurrence_id)
                if self._pending_reminder == calendar_meeting:
                    self._pending_reminder = None

            self._schedule_ui_update()
            logger.info(f"Recording started: '{title}'")

        except Exception as e:
            logger.error(f"Start failed: {e}", exc_info=True)
        finally:
            with self._lock:
                self._busy = False
            self._schedule_ui_update()

    def _do_stop(self):
        with self._lock:
            if not self._recording or self._busy:
                return
            self._busy = True

        try:
            logger.info("Stopping recording")
            result = transcriber_stop()
            stop_sender()

            with self._lock:
                title = self._recording_title
                duration = self._duration
                self._recording = False
                self._recording_title = None
                self._recording_app = None
                self._recording_auto = False
                self._recording_capture_mode = "standard"
                self._recording_expected_end = None
                self._started_at = None

            self._schedule_ui_update()
            if result:
                logger.info(f"Recording stopped: '{title}' ({duration})")
            else:
                logger.warning("No active recording on transcriber")

        except Exception as e:
            logger.error(f"Stop failed: {e}", exc_info=True)
            with self._lock:
                self._recording = False
                self._recording_title = None
                self._recording_app = None
                self._recording_auto = False
                self._recording_capture_mode = "standard"
                self._recording_expected_end = None
                self._started_at = None
        finally:
            with self._lock:
                self._busy = False
            self._schedule_ui_update()

    # -------------------------------------------------------------------
    # Menu callbacks — @rumps.clicked runs on main thread
    # -------------------------------------------------------------------

    @rumps.clicked("Start Recording")
    def on_start(self, sender):
        try:
            logger.info("on_start callback fired")
            with self._lock:
                if self._recording or self._busy:
                    logger.info("Already recording or busy")
                    return

            app = infer_manual_start_app()
            auto_stop = app != "Manual"
            calendar_meeting = current_calendar_meeting()
            cal_title = calendar_meeting.title if calendar_meeting else lookup_calendar_title()
            if cal_title:
                title = cal_title
                logger.info(f"Manual start with calendar title: '{title}'")
            else:
                title = f"Meeting at {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}"
                logger.info(f"Manual start: '{title}'")
            if auto_stop:
                logger.info(f"Manual recording attached to {app} for auto-stop")
            self._schedule_ui_update()
            threading.Thread(
                target=self._do_start,
                args=(title, app, auto_stop),
                kwargs={"calendar_meeting": calendar_meeting},
                daemon=True,
            ).start()
        except Exception as e:
            logger.error(f"on_start error: {e}", exc_info=True)

    def _start_room_recording(self, meeting: CalendarMeeting | None = None):
        with self._lock:
            if self._recording or self._busy:
                logger.info("Already recording or busy")
                return
        if meeting is None:
            meeting = current_calendar_meeting()
        title = meeting.title if meeting else (
            f"Room Meeting at {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}"
        )
        expected_end = meeting.end if meeting else None
        threading.Thread(
            target=self._do_start,
            args=(title, "Room", expected_end is not None),
            kwargs={
                "capture_mode": "room",
                "expected_end": expected_end,
                "calendar_meeting": meeting,
            },
            daemon=True,
        ).start()

    @rumps.clicked("Start Room Recording")
    def on_room_start(self, sender):
        try:
            logger.info("on_room_start callback fired")
            self._start_room_recording()
        except Exception as e:
            logger.error(f"on_room_start error: {e}", exc_info=True)

    @rumps.clicked("Remind in 5 Minutes")
    def on_snooze_reminder(self, sender):
        meeting = self._resolve_reminder(handled=False, snooze_minutes=5)
        if meeting:
            logger.info(f"Recording reminder snoozed: '{meeting.title}'")

    @rumps.clicked("Not Attending")
    def on_not_attending(self, sender):
        meeting = self._resolve_reminder(handled=True)
        if meeting:
            logger.info(f"Reminder dismissed as not attending: '{meeting.title}'")

    @rumps.clicked("Stop Recording")
    def on_stop(self, sender):
        try:
            logger.info("on_stop callback fired")
            with self._lock:
                if not self._recording:
                    rumps.notification("Meeting Bar", "Not Recording",
                                      "No recording in progress.")
                    return
                if self._busy:
                    logger.info("Stop ignored while another start/stop is in progress")
                    return
                was_auto = self._recording_auto
            # If meeting is still active, suppress auto-restart
            if was_auto and detect_meeting():
                self._suppress_auto = True
                logger.info("Suppressing auto-restart until meeting ends")
            threading.Thread(target=self._do_stop, daemon=True).start()
        except Exception as e:
            logger.error(f"on_stop error: {e}", exc_info=True)

    @rumps.clicked("Auto-Detect Meetings")
    def on_toggle_detection(self, sender):
        try:
            self._detection_enabled = not self._detection_enabled
            sender.state = 1 if self._detection_enabled else 0
            logger.info(f"Auto-detection {'enabled' if self._detection_enabled else 'disabled'}")
        except Exception as e:
            logger.error(f"on_toggle_detection error: {e}", exc_info=True)

    @rumps.clicked("View Log…")
    def on_view_log(self, sender):
        try:
            subprocess.Popen(["open", "-a", "Console", str(LOG_PATH)])
        except Exception as e:
            logger.error(f"on_view_log error: {e}", exc_info=True)

    @rumps.clicked("Quit Meeting Bar")
    def on_quit(self, sender):
        try:
            logger.info("Quit requested")
            if self._recording:
                logger.info("Stopping recording before quit")
                self._do_stop()
            logger.info("Meeting Bar exiting")
        except Exception as e:
            logger.error(f"on_quit error: {e}", exc_info=True)
        os._exit(0)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    logger.info("Meeting Bar starting")
    logger.info(f"  Transcriber: {TRANSCRIBER_URL}")
    logger.info(f"  VBAN target: {VBAN_TARGET_HOST}:{VBAN_PORT}")
    logger.info(f"  Poll interval: {POLL_INTERVAL}s")
    logger.info(f"  Calendar: {CALENDAR_ORG}")
    logger.info(f"  Log: {LOG_PATH}")
    logger.info("  Quit: use Quit menu item, or Ctrl-\\ from terminal")

    app = MeetingBarApp()
    app.run()


if __name__ == "__main__":
    main()
