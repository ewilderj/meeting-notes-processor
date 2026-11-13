#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "requests>=2.31.0",
#     "sounddevice>=0.5.0",
#     "numpy>=1.26.0",
# ]
# ///
"""
Meeting Recorder — one-command meeting capture and transcription.

Manages the full pipeline: VBAN streaming → transcriber recording → transcription.
Designed to be the single command you run when a meeting starts.

When using BlackHole 2ch (recommended), the VBAN sender automatically
opens two input streams — BlackHole for remote participant audio and your
microphone for your voice — and sends them as separate stereo channels
(remote on the left, mic on the right). No external audio routing
software needed for mic capture, and the transcriber can explicitly mark
the local speaker from the mic channel.

Usage:
  uv run meeting.py start "Weekly Standup"       # begin recording
  uv run meeting.py stop                          # stop and transcribe
  uv run meeting.py status                        # check what's happening
  uv run meeting.py devices                       # list audio input devices

Prerequisites:
  - BlackHole 2ch installed on laptop
  - SoundSource routing Zoom/Teams output → BlackHole 2ch
  - Transcriber running on the configured target host
"""

import argparse
import atexit
import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import requests
import sounddevice as sd

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TRANSCRIBER_TARGET_HOST = os.getenv("TRANSCRIBER_TARGET_HOST", "pilot")
TRANSCRIBER_URL = os.getenv("TRANSCRIBER_URL", f"http://{TRANSCRIBER_TARGET_HOST}:8000")
VBAN_TARGET_HOST = os.getenv("PILOT_HOST", TRANSCRIBER_TARGET_HOST)
VBAN_PORT = int(os.getenv("VBAN_PORT", "6980"))

# Audio device preference order (first match wins)
DEVICE_PREFERENCE = [
    "BlackHole 2ch",       # Best: remote audio via SoundSource routing + mic mixed in software
    "ZoomAudioDevice",     # Fallback: Zoom remote participants only
    "Microsoft Teams",     # Fallback: Teams remote participants only
]

# Microphone: use system default input device for dual-input mixing
# (when using BlackHole as primary, we need a real mic for the user's voice)

VBAN_SEND_SCRIPT = Path(__file__).parent / "vban" / "vban_send.py"
PID_FILE = Path(os.getenv("MEETING_PID_FILE", "/tmp/meeting-vban-sender.pid"))
LOG_FILE = Path(os.getenv("MEETING_LOG_FILE", "/tmp/meeting-vban-sender.log"))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [meeting] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("meeting")

# ---------------------------------------------------------------------------
# Audio Device Discovery
# ---------------------------------------------------------------------------


def find_best_device() -> tuple[str, str]:
    """Find the best available audio input device.
    
    Returns (device_name, quality) where quality is 'full' or 'partial'.
    'full' means both sides of conversation (BlackHole + mic captured with
    speaker-preserving channels).
    'partial' means only remote participants (direct virtual device).
    """
    devices = sd.query_devices()
    available = {}
    for d in devices:
        if d["max_input_channels"] > 0:
            available[d["name"]] = d

    for pref in DEVICE_PREFERENCE:
        for name in available:
            if pref.lower() in name.lower():
                quality = "full" if "blackhole" in name.lower() else "partial"
                return name, quality

    return None, None


def find_mic_device() -> str | None:
    """Find the microphone for dual-input mixing.

    Uses the system default input device, unless it's a virtual device
    (BlackHole, Zoom, Teams) — in which case, fall back to any real mic.
    """
    devices = sd.query_devices()
    default_idx = sd.default.device[0]  # system default input device

    # Check if the system default is a real mic (not a virtual device)
    if default_idx is not None and default_idx >= 0:
        default_dev = devices[default_idx]
        name = default_dev["name"].lower()
        if default_dev["max_input_channels"] > 0 and not any(
            skip in name for skip in ["blackhole", "zoom", "teams"]
        ):
            return default_dev["name"]

    # System default is virtual — find any real mic
    for d in devices:
        if d["max_input_channels"] > 0:
            name = d["name"].lower()
            if not any(skip in name for skip in ["blackhole", "zoom", "teams"]):
                return d["name"]

    return None


def list_devices():
    """Show available input devices with recommendations."""
    devices = sd.query_devices()
    print("\n📱 Audio Input Devices:")
    print("-" * 65)
    for d in devices:
        if d["max_input_channels"] > 0:
            name = d["name"]
            markers = []
            if "blackhole" in name.lower():
                markers.append("★ RECOMMENDED (app audio → mix with mic)")
            elif "zoom" in name.lower():
                markers.append("⚡ Zoom (remote audio only)")
            elif "teams" in name.lower():
                markers.append("⚡ Teams (remote audio only)")
            marker_str = f"  {' '.join(markers)}" if markers else ""
            print(f"  [{d['index']}] {name} (ch:{d['max_input_channels']}){marker_str}")

    mic = find_mic_device()
    if mic:
        print(f"\n🎤 Detected mic for mixing: {mic}")
    else:
        print(f"\n⚠️  No mic detected for dual-input mixing")
    print()


# ---------------------------------------------------------------------------
# VBAN Sender Management
# ---------------------------------------------------------------------------


def _sender_running() -> int | None:
    """Check if VBAN sender is running. Returns PID or None."""
    if not PID_FILE.exists():
        return None
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)  # Check if process exists
        return pid
    except (ProcessLookupError, ValueError):
        PID_FILE.unlink(missing_ok=True)
        return None


def start_sender(device: str, mic: str | None = None) -> int:
    """Start the VBAN sender in background. Returns PID.

    If mic is provided, the sender runs in dual-input mixed mode,
    capturing from both the primary device and the microphone.
    """
    existing = _sender_running()
    if existing:
        logger.info(f"VBAN sender already running (PID {existing})")
        return existing

    cmd = [
        sys.executable, str(VBAN_SEND_SCRIPT),
        "-d", device,
        "-t", VBAN_TARGET_HOST,
        "-p", str(VBAN_PORT),
    ]
    if mic:
        cmd.extend(["--mic", mic])

    log_fh = open(LOG_FILE, "w")
    proc = subprocess.Popen(
        cmd,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,  # Detach from terminal
    )

    PID_FILE.write_text(str(proc.pid))
    mode = f"mixed ({device} + {mic})" if mic else device
    logger.info(f"VBAN sender started (PID {proc.pid}) → {mode}")
    return proc.pid


def stop_sender():
    """Stop the VBAN sender."""
    pid = _sender_running()
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
            # Wait briefly for clean shutdown
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
    else:
        logger.info("No VBAN sender running")


# ---------------------------------------------------------------------------
# Transcriber API
# ---------------------------------------------------------------------------


def transcriber_status() -> dict | None:
    """Get transcriber status from the configured target."""
    try:
        r = requests.get(f"{TRANSCRIBER_URL}/status", timeout=5)
        return r.json()
    except requests.RequestException as e:
        logger.error(f"Cannot reach transcriber at {TRANSCRIBER_URL}: {e}")
        return None


def transcriber_start(title: str) -> dict | None:
    """Start recording on the configured target."""
    try:
        r = requests.post(
            f"{TRANSCRIBER_URL}/start",
            json={"title": title},
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
    """Stop recording on the configured target."""
    try:
        r = requests.post(f"{TRANSCRIBER_URL}/stop", timeout=10)
        if r.status_code == 404:
            logger.warning("No active recording to stop")
            return None
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        logger.error(f"Failed to stop recording: {e}")
        return None


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_start(args):
    """Start a meeting recording."""
    title = args.title

    # 1. Check transcriber is reachable
    status = transcriber_status()
    if not status:
        print(f"❌ Cannot reach transcriber at {TRANSCRIBER_URL}. Is it running?")
        print(f"   Try: cd transcriber && make status")
        sys.exit(1)

    if status.get("recording"):
        rec = status["recording"]
        print(f"⚠️  Already recording: {rec['title']}")
        print(f"   Started: {rec['meeting_start']}")
        print(f"   Use 'meeting.py stop' to end the current recording first.")
        sys.exit(1)

    # 2. Find audio device
    if args.device:
        device_name = args.device
        quality = "full" if "blackhole" in device_name.lower() else "partial"
    else:
        device_name, quality = find_best_device()

    if not device_name:
        print("❌ No suitable audio device found.")
        print("   Install BlackHole 2ch and configure SoundSource, or specify with -d")
        list_devices()
        sys.exit(1)

    if quality == "partial":
        print(f"⚠️  Using {device_name} — only remote participants will be captured.")
        print(f"   For full conversation capture, configure SoundSource → BlackHole 2ch")

    # 3. Detect mic for dual-input mixing (only when using BlackHole)
    mic_name = None
    if quality == "full":
        if args.mic:
            mic_name = args.mic
        else:
            mic_name = find_mic_device()

        if mic_name:
            print(f"🎤 Mic for mixing: {mic_name}")
        else:
            print(f"⚠️  No mic detected — only remote participants will be captured.")
            print(f"   Specify with -m/--mic, or check 'meeting.py devices'")

    # 4. Start VBAN sender
    print(f"🎙  Audio source: {device_name}")
    sender_pid = start_sender(device_name, mic=mic_name)

    # Give VBAN a moment to connect
    time.sleep(3)

    # 5. Start recording on the transcriber
    print(f"🔴 Starting recording: {title}")
    result = transcriber_start(title)
    if result:
        print(f"✅ Recording! Audio streaming to {VBAN_TARGET_HOST}:{VBAN_PORT}.")
        print(f"   Title: {title}")
        print(f"   Run 'uv run meeting.py stop' when done.")
    else:
        print("❌ Failed to start recording. Stopping sender...")
        stop_sender()
        sys.exit(1)


def cmd_stop(args):
    """Stop a meeting recording."""
    # 1. Stop recording on the transcriber (triggers transcription)
    print("⏹  Stopping recording...")
    result = transcriber_stop()
    if result:
        print(f"✅ Recording stopped: {result.get('title', '?')}")
        print(f"   Duration: {result.get('duration_seconds', '?')}s")
        print(f"   Transcription queued — will be posted to nuctu webhook automatically.")
    else:
        print("⚠️  No active recording found (may have already been stopped)")

    # 2. Stop VBAN sender
    stop_sender()
    print("🎙  Audio streaming stopped.")


def cmd_status(args):
    """Show current status."""
    # VBAN sender
    sender_pid = _sender_running()
    if sender_pid:
        print(f"🎙  VBAN sender: running (PID {sender_pid})")
    else:
        print(f"🎙  VBAN sender: not running")

    # Audio device
    device_name, quality = find_best_device()
    if device_name:
        q_label = "full conversation" if quality == "full" else "remote only"
        print(f"🔊 Best audio device: {device_name} ({q_label})")
    else:
        print(f"🔊 No suitable audio device found")

    # Transcriber
    status = transcriber_status()
    if status:
        recording = status.get("recording")
        if recording:
            print(f"🔴 Recording: {recording['title']} (started: {recording['meeting_start']})")
        else:
            print(f"⏸  Transcriber: idle")
        print(f"💾 Disk free: {status.get('disk_free_gb', '?')} GB")
        print(f"📊 Recent recordings: {status.get('recent_count', 0)}")
    else:
        print(f"❌ Transcriber: unreachable")


def cmd_devices(args):
    """List audio devices."""
    list_devices()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Meeting Recorder — one-command meeting capture and transcription",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s start "Weekly Standup"
  %(prog)s start "1:1 with Sarah" -d ZoomAudioDevice
  %(prog)s stop
  %(prog)s status
  %(prog)s devices
        """,
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # start
    p_start = subparsers.add_parser("start", help="Start recording a meeting")
    p_start.add_argument("title", help="Meeting title")
    p_start.add_argument(
        "-d", "--device",
        help="Audio device name (default: auto-detect BlackHole > Zoom > Teams)",
    )
    p_start.add_argument(
        "-m", "--mic",
        help="Microphone device for dual-input mixing (default: auto-detect Yeti > built-in)",
    )
    p_start.set_defaults(func=cmd_start)

    # stop
    p_stop = subparsers.add_parser("stop", help="Stop recording and transcribe")
    p_stop.set_defaults(func=cmd_stop)

    # status
    p_status = subparsers.add_parser("status", help="Show current status")
    p_status.set_defaults(func=cmd_status)

    # devices
    p_devices = subparsers.add_parser("devices", help="List audio input devices")
    p_devices.set_defaults(func=cmd_devices)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
