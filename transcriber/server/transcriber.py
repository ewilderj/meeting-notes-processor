#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "fastapi>=0.115.0",
#     "uvicorn>=0.34.0",
#     "httpx>=0.28.0",
#     "pyyaml>=6.0.0",
# ]
# ///
"""
Transcriber — FastAPI server for the local or pilot transcription host.

Captures audio via VBAN (UDP) directly to WAV and transcribes via whisper.cpp.
On completion, POSTs results to meetingnotesd.py with YAML front matter
containing meeting start/end timestamps.

Run with: uv run transcriber.py
Config:   Set WEBHOOK_URL to your meetingnotesd endpoint (default: http://localhost:9876/webhook)

API:
  GET  /status      — Health check, active recordings, disk space
  POST /start       — Begin recording: {"title": "Meeting Name"}
  POST /stop        — Stop recording, queue for transcription
  GET  /recordings  — List recent recordings and status
"""

import asyncio
import logging
import math
import os
import re
import shutil
import socket
import threading
import time
import wave
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    if value.lower() in {"1", "true", "yes", "on"}:
        return True
    if value.lower() in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value, got {value!r}")


def _env_int(
    name: str,
    default: int | None,
    *,
    min_value: int | None = None,
    max_value: int | None = None,
) -> int | None:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    parsed = int(value)
    if min_value is not None and parsed < min_value:
        raise ValueError(f"{name} must be >= {min_value}, got {parsed}")
    if max_value is not None and parsed > max_value:
        raise ValueError(f"{name} must be <= {max_value}, got {parsed}")
    return parsed


def _env_float(name: str, default: float, *, min_value: float | None = None) -> float:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    parsed = float(value)
    if min_value is not None and parsed < min_value:
        raise ValueError(f"{name} must be >= {min_value}, got {parsed}")
    return parsed


WHISPER_CLI = os.getenv("WHISPER_CLI", os.path.expanduser("~/whisper.cpp/build/bin/whisper-cli"))
WHISPER_MODEL = os.getenv("WHISPER_MODEL", os.path.expanduser("~/whisper.cpp/models/ggml-small.en-tdrz.bin"))
WHISPER_THREADS = _env_int("WHISPER_THREADS", None, min_value=1)
WHISPER_NICE = _env_int("WHISPER_NICE", None, min_value=-20, max_value=20)
WHISPER_TASKPOLICY_BACKGROUND = _env_bool("WHISPER_TASKPOLICY_BACKGROUND", False)
TRANSCRIPTION_IDLE_DELAY_SECONDS = _env_float("TRANSCRIPTION_IDLE_DELAY_SECONDS", 0.0, min_value=0.0)
TRANSCRIBE_WHILE_RECORDING = _env_bool("TRANSCRIBE_WHILE_RECORDING", True)
RECORDINGS_DIR = Path(os.getenv("RECORDINGS_DIR", os.path.expanduser("~/transcriber/recordings")))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "http://nuctu:9876/webhook")
VBAN_PORT = int(os.getenv("VBAN_PORT", "6980"))  # UDP port for VBAN audio packets
RECORDING_MAX_AGE_DAYS = int(os.getenv("RECORDING_MAX_AGE_DAYS", "7"))  # Delete recordings older than this
HOST = os.getenv("TRANSCRIBER_HOST", "0.0.0.0")
PORT = int(os.getenv("TRANSCRIBER_PORT", "8000"))
LOCAL_SPEAKER_LABEL = os.getenv("LOCAL_SPEAKER_LABEL", "Edd")
LOCAL_SPEAKER_CHANNEL = int(os.getenv("LOCAL_SPEAKER_CHANNEL", "2"))
LOCAL_SPEAKER_MIN_DBFS = float(os.getenv("LOCAL_SPEAKER_MIN_DBFS", "-40"))
LOCAL_SPEAKER_DOMINANCE_DB = float(os.getenv("LOCAL_SPEAKER_DOMINANCE_DB", "4.5"))

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("transcriber")

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class RecordingState(str, Enum):
    RECORDING = "recording"
    TRANSCRIBING = "transcribing"
    COMPLETED = "completed"
    FAILED = "failed"


class Recording:
    def __init__(self, title: str, audio_path: Path):
        self.title = title
        self.audio_path = audio_path
        self.transcript_path: Optional[Path] = None
        self.state = RecordingState.RECORDING
        self.meeting_start = datetime.now(timezone.utc)
        self.meeting_end: Optional[datetime] = None
        self.error: Optional[str] = None
        self.vban_capture: Optional["VBANCapture"] = None
        self.webhook_sent = False

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "state": self.state.value,
            "audio_path": str(self.audio_path),
            "transcript_path": str(self.transcript_path) if self.transcript_path else None,
            "meeting_start": self.meeting_start.isoformat(),
            "meeting_end": self.meeting_end.isoformat() if self.meeting_end else None,
            "error": self.error,
            "webhook_sent": self.webhook_sent,
        }


# Active recording (only one at a time)
active_recording: Optional[Recording] = None
# Recent completed recordings (ring buffer)
recent_recordings: list[Recording] = []
MAX_RECENT = 20

# Sequential transcription queue — ensures only one whisper-cli runs at a time
# Created lazily in startup() to bind to the correct event loop
_transcription_queue: Optional[asyncio.Queue] = None
_queue_worker_task: Optional[asyncio.Task] = None


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Transcriber", version="0.1.0")


class StartRequest(BaseModel):
    title: str


class StopResponse(BaseModel):
    status: str
    title: str
    duration_seconds: float
    message: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _disk_free_gb() -> float:
    """Return free disk space in GB."""
    stat = os.statvfs(str(RECORDINGS_DIR))
    return (stat.f_bavail * stat.f_frsize) / (1024**3)


def _find_executable(name: str, fallback_paths: tuple[str, ...] = ()) -> str | None:
    """Find an executable, including launchd-safe absolute fallback paths."""
    found = shutil.which(name)
    if found:
        return found
    for fallback_path in fallback_paths:
        if Path(fallback_path).exists():
            return fallback_path
    return None


def _build_whisper_command(audio_path: Path) -> list[str]:
    """Build the whisper.cpp command, including optional resource controls."""
    cmd = [
        WHISPER_CLI,
        "-m", WHISPER_MODEL,
        "-f", str(audio_path),
        "-l", "en",
        "--print-progress",
        "--no-fallback",     # prevent temperature fallback (reduces hallucination)
        "--suppress-nst",    # suppress non-speech tokens
        "--tinydiarize",     # insert [SPEAKER_TURN] tokens (requires tdrz model)
    ]

    if WHISPER_THREADS is not None:
        cmd.extend(["-t", str(WHISPER_THREADS)])

    prefix: list[str] = []
    if WHISPER_TASKPOLICY_BACKGROUND:
        taskpolicy = _find_executable("taskpolicy", ("/usr/sbin/taskpolicy",))
        if taskpolicy:
            prefix.extend([taskpolicy, "-b"])
        else:
            logger.warning("WHISPER_TASKPOLICY_BACKGROUND requested, but taskpolicy was not found")

    if WHISPER_NICE is not None:
        nice = _find_executable("nice", ("/usr/bin/nice",))
        if nice:
            prefix.extend([nice, "-n", str(WHISPER_NICE)])
        else:
            logger.warning("WHISPER_NICE requested, but nice was not found")

    return prefix + cmd


async def _wait_for_transcription_slot(recording: Recording) -> None:
    """Optionally wait for a quiet window before starting Whisper."""
    if TRANSCRIBE_WHILE_RECORDING and TRANSCRIPTION_IDLE_DELAY_SECONDS <= 0:
        return

    quiet_since = time.monotonic()
    logged_recording_wait = False
    while True:
        if not TRANSCRIBE_WHILE_RECORDING and active_recording is not None:
            if not logged_recording_wait:
                logger.info(
                    "Deferring transcription for '%s' while another recording is active",
                    recording.title,
                )
                logged_recording_wait = True
            quiet_since = time.monotonic()
            await asyncio.sleep(5)
            continue

        logged_recording_wait = False
        quiet_for = time.monotonic() - quiet_since
        remaining = TRANSCRIPTION_IDLE_DELAY_SECONDS - quiet_for
        if remaining <= 0:
            return
        await asyncio.sleep(min(5, remaining))


def cleanup_old_recordings(recordings_dir: Path = None, max_age_days: int = None) -> int:
    """Delete .wav and .txt files older than max_age_days from recordings_dir.

    Returns the number of files deleted.
    """
    recordings_dir = recordings_dir or RECORDINGS_DIR
    max_age_days = max_age_days if max_age_days is not None else RECORDING_MAX_AGE_DAYS
    cutoff = time.time() - (max_age_days * 86400)
    deleted = 0

    if not recordings_dir.exists():
        return 0

    for f in recordings_dir.iterdir():
        if f.is_file() and f.suffix in (".wav", ".txt"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    logger.info(f"Cleaned up old recording: {f.name}")
                    deleted += 1
            except OSError as e:
                logger.warning(f"Failed to delete {f.name}: {e}")

    if deleted:
        logger.info(f"Cleanup: removed {deleted} file(s) older than {max_age_days} days")
    return deleted


async def _cleanup_loop() -> None:
    """Periodically clean up old recordings (runs every 6 hours)."""
    while True:
        await asyncio.sleep(6 * 3600)  # 6 hours
        try:
            cleanup_old_recordings()
        except Exception as e:
            logger.error(f"Cleanup error: {e}", exc_info=True)


class VBANCapture:
    """Captures VBAN audio packets from the network directly to a WAV file.

    Replaces the previous receiver → BlackHole → ffmpeg chain with a single
    UDP listener that writes PCM data straight to disk.
    """

    HEADER_SIZE = 28
    MAGIC = b"VBAN"
    SR_TABLE = [
        6000, 12000, 24000, 48000, 96000, 192000, 384000,
        8000, 16000, 32000, 64000, 128000, 256000, 512000,
        11025, 22050, 44100, 88200, 176400, 352800, 705600,
    ]

    def __init__(self, audio_path: Path, port: int = 6980):
        self.audio_path = audio_path
        self.port = port
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.sample_rate: Optional[int] = None
        self.total_samples = 0

    def start(self):
        """Start capturing VBAN packets in a background thread."""
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._capture_loop, daemon=True, name="vban-capture",
        )
        self._thread.start()

    def stop(self):
        """Stop capturing and finalize the WAV file."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def _capture_loop(self):
        """Receive VBAN packets and write PCM data to WAV."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", self.port))
        sock.settimeout(1.0)  # allow periodic stop checks

        wav_file: Optional[wave.Wave_write] = None

        try:
            while not self._stop_event.is_set():
                try:
                    data, addr = sock.recvfrom(2048)
                except socket.timeout:
                    continue

                if len(data) <= self.HEADER_SIZE or data[:4] != self.MAGIC:
                    continue

                # Parse sample rate and channels from VBAN header
                sr_index = data[4] & 0x1F
                channels = (data[6] & 0xFF) + 1
                pcm_data = data[self.HEADER_SIZE:]

                if wav_file is None:
                    self.sample_rate = (
                        self.SR_TABLE[sr_index]
                        if sr_index < len(self.SR_TABLE)
                        else 48000
                    )
                    wav_file = wave.open(str(self.audio_path), "wb")
                    wav_file.setnchannels(channels)
                    wav_file.setsampwidth(2)  # 16-bit
                    wav_file.setframerate(self.sample_rate)
                    logger.info(
                        f"VBAN capture: {self.sample_rate}Hz {channels}ch from {addr[0]}"
                    )

                wav_file.writeframes(pcm_data)
                self.total_samples += len(pcm_data) // (2 * channels)

        except Exception as e:
            logger.error(f"VBAN capture error: {e}", exc_info=True)
        finally:
            if wav_file:
                wav_file.close()
                duration = (
                    self.total_samples / self.sample_rate if self.sample_rate else 0
                )
                logger.info(
                    f"VBAN capture saved: {duration:.1f}s, "
                    f"{self.total_samples} samples → {self.audio_path}"
                )
            sock.close()


# ---------------------------------------------------------------------------
# Hallucination removal
# ---------------------------------------------------------------------------

# Regex to strip timestamp prefix: "[00:01:23.000 --> 00:01:27.000]   text"
_TS_RE = re.compile(r"^\[[\d:.]+\s*-->\s*[\d:.]+\]\s*")

# Consecutive identical lines beyond this threshold are considered hallucination
_HALLUCINATION_REPEAT_THRESHOLD = 3


def _remove_hallucinated_lines(transcript: str) -> str:
    """Remove hallucinated repetitive lines from a whisper transcript.

    Whisper models can generate the same phrase
    over and over during silence or low-signal audio. This function detects
    runs of consecutive lines with identical text content (ignoring timestamps)
    and collapses them. Runs shorter than _HALLUCINATION_REPEAT_THRESHOLD are
    kept intact (normal conversational repetition like "yeah" / "okay").
    """
    lines = transcript.split("\n")
    if not lines:
        return transcript

    kept: list[str] = []
    prev_text: str | None = None
    run_length = 0

    for line in lines:
        stripped = _TS_RE.sub("", line).strip()

        if stripped == prev_text and stripped:
            run_length += 1
        else:
            # Flush previous run
            if run_length > 0 and run_length < _HALLUCINATION_REPEAT_THRESHOLD:
                # Short run — keep all lines (normal repetition)
                kept.extend(_pending_run)
            elif run_length >= _HALLUCINATION_REPEAT_THRESHOLD:
                # Long run — hallucination, drop all
                logger.info(
                    f"Removed {run_length} hallucinated repetitions of: "
                    f"{prev_text!r}"
                )
            # Start new run
            prev_text = stripped
            run_length = 1
            _pending_run = [line]
            continue

        _pending_run.append(line)

    # Flush final run
    if run_length > 0 and run_length < _HALLUCINATION_REPEAT_THRESHOLD:
        kept.extend(_pending_run)
    elif run_length >= _HALLUCINATION_REPEAT_THRESHOLD:
        logger.info(
            f"Removed {run_length} hallucinated repetitions of: "
            f"{prev_text!r}"
        )

    removed = len(lines) - len(kept)
    if removed > 0:
        logger.info(
            f"Hallucination filter: removed {removed}/{len(lines)} lines "
            f"({removed/len(lines)*100:.0f}%)"
        )
    return "\n".join(kept)


# Regex to parse start/end times from timestamp lines
_TS_PARSE_RE = re.compile(
    r"^\[(\d+):(\d+):(\d+\.\d+)\s*-->\s*(\d+):(\d+):(\d+\.\d+)\]\s*(.*)"
)


def _timestamp_parts_to_seconds(hours: str, minutes: str, seconds: str) -> float:
    """Convert regex-captured timestamp parts to seconds."""
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _rms_to_dbfs(rms: float) -> float | None:
    """Convert a PCM RMS value to dBFS."""
    if rms <= 0:
        return None
    return 20.0 * math.log10(rms / 32767.0)


class _LocalSpeakerChannelAnalyzer:
    """Identify segments dominated by the local microphone channel."""

    def __init__(self, audio_path: Path):
        self.audio_path = audio_path
        self._wav = wave.open(str(audio_path), "rb")
        self.channels = self._wav.getnchannels()
        self.sample_width = self._wav.getsampwidth()
        self.sample_rate = self._wav.getframerate()
        self.frame_count = self._wav.getnframes()

    @property
    def available(self) -> bool:
        return self.sample_width == 2 and self.channels >= LOCAL_SPEAKER_CHANNEL

    def close(self) -> None:
        self._wav.close()

    def is_local_speaker(self, start_sec: float, end_sec: float) -> bool:
        """Return True when the local mic channel clearly dominates the segment."""
        if not self.available:
            return False

        start_frame = max(0, int(start_sec * self.sample_rate))
        end_frame = min(self.frame_count, max(start_frame + 1, int(end_sec * self.sample_rate)))
        if end_frame <= start_frame:
            return False

        self._wav.setpos(start_frame)
        raw = self._wav.readframes(end_frame - start_frame)
        if not raw:
            return False

        samples = memoryview(raw).cast("h")
        channel_energy = [0.0] * self.channels
        channel_counts = [0] * self.channels
        for idx, sample in enumerate(samples):
            channel = idx % self.channels
            channel_energy[channel] += sample * sample
            channel_counts[channel] += 1

        rms_values: list[float] = []
        for energy, count in zip(channel_energy, channel_counts):
            rms_values.append(math.sqrt(energy / count) if count else 0.0)

        local_index = LOCAL_SPEAKER_CHANNEL - 1
        local_rms = rms_values[local_index]
        local_dbfs = _rms_to_dbfs(local_rms)
        if local_dbfs is None or local_dbfs < LOCAL_SPEAKER_MIN_DBFS:
            return False

        other_rms_values = [rms for i, rms in enumerate(rms_values) if i != local_index]
        other_rms = max(other_rms_values, default=0.0)
        if other_rms <= 0:
            return True

        dominance_db = 20.0 * math.log10(local_rms / other_rms)
        return dominance_db >= LOCAL_SPEAKER_DOMINANCE_DB


def _annotate_local_speaker_segments(transcript: str, audio_path: Path) -> str:
    """Insert explicit local-speaker labels when the mic channel dominates."""
    try:
        analyzer = _LocalSpeakerChannelAnalyzer(audio_path)
    except (FileNotFoundError, OSError, wave.Error) as e:
        logger.warning(f"Local speaker analysis unavailable for {audio_path.name}: {e}")
        return transcript

    if not analyzer.available:
        analyzer.close()
        return transcript

    lines = transcript.split("\n")
    result: list[str] = []
    seen_timestamped_line = False
    labels_added = 0

    try:
        for line in lines:
            m = _TS_PARSE_RE.match(line)
            if not m:
                result.append(line)
                continue

            h1, m1, s1, h2, m2, s2, text = m.groups()
            start_sec = _timestamp_parts_to_seconds(h1, m1, s1)
            end_sec = _timestamp_parts_to_seconds(h2, m2, s2)
            stripped = text.strip()
            has_turn_marker = "[SPEAKER_TURN]" in stripped
            bare_text = stripped.replace("[SPEAKER_TURN]", "").strip()

            if analyzer.is_local_speaker(start_sec, end_sec) and (has_turn_marker or not seen_timestamped_line):
                stripped = f"[speaker:{LOCAL_SPEAKER_LABEL}] {bare_text}".strip()
                labels_added += 1
            elif has_turn_marker:
                stripped = f"[SPEAKER_TURN] {bare_text}".strip()
            else:
                stripped = bare_text

            prefix = f"[{h1}:{m1}:{s1} --> {h2}:{m2}:{s2}]"
            result.append(f"{prefix}   {stripped}" if stripped else prefix)
            seen_timestamped_line = True
    finally:
        analyzer.close()

    if labels_added:
        logger.info(
            "Annotated %d transcript segment(s) as %s using the local mic channel",
            labels_added,
            LOCAL_SPEAKER_LABEL,
        )
    return "\n".join(result)


def _strip_timestamps_with_gaps(transcript: str) -> str:
    """Strip timestamp prefixes and normalize speaker markers.

    Converts timestamped whisper output (with tinydiarize speaker turn
    markers) into plain text. Each [SPEAKER_TURN] token is replaced with
    an [S] marker that the downstream LLM uses to distinguish speakers.
    Explicit speaker labels like [speaker:Edd] are preserved.
    """
    lines = transcript.split("\n")
    result: list[str] = []

    for line in lines:
        m = _TS_PARSE_RE.match(line)
        if not m:
            # Non-timestamped line (blank, etc.) — pass through
            result.append(line)
            continue

        _h1, _m1, _s1, _h2, _m2, _s2, text = m.groups()
        text = text.strip()

        # Replace tinydiarize speaker turn markers
        text = text.replace("[SPEAKER_TURN]", "[S]")

        result.append(text if text else "")

    # Remove leading/trailing blank lines and collapse triple+ blanks
    cleaned = "\n".join(result).strip()
    while "\n\n\n" in cleaned:
        cleaned = cleaned.replace("\n\n\n", "\n\n")
    return cleaned


async def _transcribe(recording: Recording) -> None:
    """Run whisper.cpp on the recorded audio and post result to webhook."""
    recording.state = RecordingState.TRANSCRIBING
    transcript_path = recording.audio_path.with_suffix(".txt")

    try:
        # Run whisper.cpp — stdout gives us timestamped transcript.
        cmd = _build_whisper_command(recording.audio_path)
        logger.info(f"Starting transcription: {recording.title}")

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            error_msg = stderr.decode().strip()[-500:]
            recording.state = RecordingState.FAILED
            recording.error = f"whisper-cli failed: {error_msg}"
            logger.error(f"Transcription failed for {recording.title}: {recording.error}")
            return

        # Use stdout (timestamped) as the canonical transcript
        raw_text = stdout.decode().strip()
        if not raw_text:
            recording.state = RecordingState.FAILED
            recording.error = "whisper-cli produced empty output"
            logger.error(f"Empty transcription for {recording.title}")
            return

        transcript_text = _remove_hallucinated_lines(raw_text)
        transcript_text = _annotate_local_speaker_segments(transcript_text, recording.audio_path)
        transcript_text = _strip_timestamps_with_gaps(transcript_text)

        # Write cleaned transcript to file
        transcript_path.write_text(transcript_text)
        recording.transcript_path = transcript_path

        # Build YAML front matter with timestamps
        local_tz = datetime.now(timezone.utc).astimezone().tzinfo
        start_local = recording.meeting_start.astimezone(local_tz)
        end_local = recording.meeting_end.astimezone(local_tz) if recording.meeting_end else start_local

        front_matter = (
            "---\n"
            f"meeting_start: {start_local.isoformat()}\n"
            f"meeting_end: {end_local.isoformat()}\n"
            f"recording_source: transcriber\n"
            "---\n\n"
        )
        full_transcript = front_matter + transcript_text

        # POST to meetingnotesd webhook
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                WEBHOOK_URL,
                json={"title": recording.title, "transcript": full_transcript},
            )
            if resp.status_code == 200:
                recording.webhook_sent = True
                logger.info(f"Transcript posted to webhook for: {recording.title}")
            else:
                logger.warning(
                    f"Webhook returned {resp.status_code} for {recording.title}: {resp.text[:200]}"
                )

        recording.state = RecordingState.COMPLETED
        logger.info(f"Transcription complete: {recording.title}")

    except Exception as e:
        recording.state = RecordingState.FAILED
        recording.error = str(e)
        logger.error(f"Transcription error for {recording.title}: {e}", exc_info=True)


async def _transcription_worker() -> None:
    """Background worker that processes transcriptions sequentially.

    Pulls recordings from the queue one at a time so that only a single
    whisper-cli process runs at once, avoiding CPU/memory contention.
    """
    while True:
        recording = await _transcription_queue.get()
        queue_depth = _transcription_queue.qsize()
        if queue_depth > 0:
            logger.info(f"Transcription queue: starting '{recording.title}' "
                        f"({queue_depth} more waiting)")
        try:
            await _wait_for_transcription_slot(recording)
            await _transcribe(recording)
        except Exception as e:
            logger.error(f"Transcription worker error: {e}", exc_info=True)
        finally:
            _archive_recording(recording)
            _transcription_queue.task_done()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/status")
async def status():
    """Health check with system info."""
    return {
        "status": "ok",
        "service": "transcriber",
        "recording": active_recording.to_dict() if active_recording else None,
        "transcription_queue_depth": _transcription_queue.qsize(),
        "recording_max_age_days": RECORDING_MAX_AGE_DAYS,
        "disk_free_gb": round(_disk_free_gb(), 1),
        "whisper_model": WHISPER_MODEL,
        "whisper_threads": WHISPER_THREADS,
        "whisper_nice": WHISPER_NICE,
        "whisper_taskpolicy_background": WHISPER_TASKPOLICY_BACKGROUND,
        "transcription_idle_delay_seconds": TRANSCRIPTION_IDLE_DELAY_SECONDS,
        "transcribe_while_recording": TRANSCRIBE_WHILE_RECORDING,
        "webhook_url": WEBHOOK_URL,
        "vban_port": VBAN_PORT,
        "recent_count": len(recent_recordings),
    }


@app.post("/start")
async def start(req: StartRequest):
    """Begin recording audio."""
    global active_recording

    if active_recording and active_recording.state == RecordingState.RECORDING:
        raise HTTPException(
            status_code=409,
            detail=f"Already recording: {active_recording.title}",
        )

    # Create recordings directory
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)

    # Generate audio filename
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_title = "".join(c if c.isalnum() or c in "-_ " else "" for c in req.title)
    safe_title = safe_title.strip().replace(" ", "-")[:50]
    audio_path = RECORDINGS_DIR / f"{ts}-{safe_title}.wav"

    recording = Recording(title=req.title, audio_path=audio_path)

    try:
        recording.vban_capture = VBANCapture(audio_path, port=VBAN_PORT)
        recording.vban_capture.start()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to start recording: {e}")

    active_recording = recording
    logger.info(f"Recording started: {req.title} → {audio_path}")

    return {
        "status": "recording",
        "title": req.title,
        "audio_path": str(audio_path),
        "meeting_start": recording.meeting_start.isoformat(),
    }


@app.post("/stop")
async def stop():
    """Stop recording, queue for transcription."""
    global active_recording

    if not active_recording or active_recording.state != RecordingState.RECORDING:
        raise HTTPException(status_code=404, detail="No active recording")

    recording = active_recording
    recording.meeting_end = datetime.now(timezone.utc)

    # Stop VBAN capture and finalize WAV
    if recording.vban_capture:
        recording.vban_capture.stop()

    duration = (recording.meeting_end - recording.meeting_start).total_seconds()
    logger.info(f"Recording stopped: {recording.title} ({duration:.0f}s)")

    # Verify audio file exists and has content
    if not recording.audio_path.exists() or recording.audio_path.stat().st_size < 1000:
        recording.state = RecordingState.FAILED
        recording.error = "Audio file missing or too small"
        _archive_recording(recording)
        active_recording = None
        raise HTTPException(status_code=500, detail="Recording failed: no audio captured")

    # Enqueue for sequential transcription
    await _transcription_queue.put(recording)
    queue_depth = _transcription_queue.qsize()
    active_recording = None

    message = f"Transcription queued. Audio: {recording.audio_path.name}"
    if queue_depth > 1:
        message += f" ({queue_depth} in queue)"

    return StopResponse(
        status="transcribing",
        title=recording.title,
        duration_seconds=round(duration, 1),
        message=message,
    )


def _archive_recording(recording: Recording) -> None:
    """Move recording to recent list."""
    recent_recordings.insert(0, recording)
    while len(recent_recordings) > MAX_RECENT:
        recent_recordings.pop()


class RetranscribeRequest(BaseModel):
    filename: str  # WAV filename in recordings dir


@app.post("/retranscribe")
async def retranscribe(req: RetranscribeRequest):
    """Queue an existing WAV file for (re-)transcription.

    Useful for recordings that fell between deploys or need re-processing.
    The filename should be relative to the recordings directory.
    """
    audio_path = RECORDINGS_DIR / req.filename
    if not audio_path.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {req.filename}")
    if audio_path.suffix != ".wav":
        raise HTTPException(status_code=400, detail="Only .wav files can be transcribed")
    if audio_path.stat().st_size < 1000:
        raise HTTPException(status_code=400, detail="Audio file too small")

    # Derive title from filename: strip date prefix and extension
    # e.g. "20260206-113236-Sync-on-tented-model.wav" → "Sync-on-tented-model"
    stem = audio_path.stem
    parts = stem.split("-", 2)  # split on first two hyphens (date, time, rest)
    title = parts[2] if len(parts) > 2 else stem

    # Infer meeting timestamps from file modification time
    mtime = datetime.fromtimestamp(audio_path.stat().st_mtime, tz=timezone.utc)
    try:
        import wave
        with wave.open(str(audio_path)) as w:
            duration_secs = w.getnframes() / w.getframerate()
    except Exception:
        duration_secs = 0
    start_time = mtime - timedelta(seconds=duration_secs) if duration_secs else mtime

    recording = Recording(title=title, audio_path=audio_path)
    recording.meeting_start = start_time
    recording.meeting_end = mtime

    await _transcription_queue.put(recording)
    queue_depth = _transcription_queue.qsize()

    message = f"Retranscription queued: {req.filename}"
    if queue_depth > 1:
        message += f" ({queue_depth} in queue)"
    logger.info(message)

    return {
        "status": "queued",
        "title": title,
        "filename": req.filename,
        "duration_seconds": round(duration_secs, 1),
        "message": message,
    }


@app.get("/recordings")
async def recordings():
    """List recent recordings."""
    items = [r.to_dict() for r in recent_recordings]
    if active_recording:
        items.insert(0, active_recording.to_dict())
    return {"recordings": items, "total": len(items)}


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


@app.on_event("startup")
async def startup():
    """Validate environment on startup."""
    global _queue_worker_task, _transcription_queue

    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)

    # Create the queue in the running event loop
    _transcription_queue = asyncio.Queue()

    if not Path(WHISPER_CLI).exists():
        logger.warning(f"whisper-cli not found at {WHISPER_CLI}")
    if not Path(WHISPER_MODEL).exists():
        logger.warning(f"Whisper model not found at {WHISPER_MODEL}")

    # Start the sequential transcription worker
    _queue_worker_task = asyncio.create_task(_transcription_worker())

    # Run initial cleanup and start periodic cleanup task
    cleanup_old_recordings()
    asyncio.create_task(_cleanup_loop())

    logger.info(f"Transcriber starting on {HOST}:{PORT}")
    logger.info(f"  Whisper CLI:  {WHISPER_CLI}")
    logger.info(f"  Model:        {WHISPER_MODEL}")
    logger.info(f"  Recordings:   {RECORDINGS_DIR}")
    logger.info(f"  Webhook URL:  {WEBHOOK_URL}")
    logger.info(f"  VBAN port:    {VBAN_PORT}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)
