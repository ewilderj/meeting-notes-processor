#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pytest>=8.0.0",
#     "pytest-asyncio>=0.24.0",
#     "fastapi>=0.115.0",
#     "httpx>=0.28.0",
#     "uvicorn>=0.34.0",
#     "pyyaml>=6.0.0",
# ]
# ///
"""
Tests for the transcription queue and recording cleanup in transcriber.py.

Covers:
- Sequential transcription queue (one whisper-cli at a time)
- Queue error recovery and archiving
- Auto-deletion of recordings older than RECORDING_MAX_AGE_DAYS

Run with: uv run pytest tests/test_transcription_queue.py -v
"""

import asyncio
import importlib
import os
import struct
import sys
import time
import wave
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).parent.parent / "transcriber" / "server"))
import transcriber


@pytest.fixture(autouse=True)
def reset_state():
    """Reset global state between tests."""
    transcriber.active_recording = None
    transcriber.recent_recordings.clear()
    # Create a fresh queue bound to the current event loop
    transcriber._transcription_queue = asyncio.Queue()
    yield


def _make_recording(title: str = "Test Meeting", tmp_path: Path = None) -> transcriber.Recording:
    """Create a Recording with a fake audio file."""
    audio_path = (tmp_path or Path("/tmp")) / f"{title.replace(' ', '-')}.wav"
    rec = transcriber.Recording(title=title, audio_path=audio_path)
    rec.meeting_end = datetime.now(timezone.utc)
    return rec


def _write_stereo_wav(path: Path, frames: list[tuple[int, int]], sample_rate: int = 100) -> None:
    """Write a small stereo PCM WAV for channel-dominance tests."""
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(2)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(b"".join(struct.pack("<hh", left, right) for left, right in frames))


def test_default_whisper_model_matches_docs(monkeypatch):
    """Default model should stay aligned with the documented tdrz setup."""
    monkeypatch.delenv("WHISPER_MODEL", raising=False)
    reloaded = importlib.reload(transcriber)
    try:
        assert reloaded.WHISPER_MODEL.endswith("ggml-small.en-tdrz.bin")
    finally:
        importlib.reload(transcriber)


@pytest.mark.asyncio
async def test_transcribe_uses_documented_whisper_flags(tmp_path, monkeypatch):
    """The server should enable tinydiarize by default."""
    recording = _make_recording(tmp_path=tmp_path)
    recording.audio_path.write_bytes(b"not-a-real-wav")

    captured = {}

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return (
                b"[00:00:00.000 --> 00:00:01.000]   Hello there.\n",
                b"",
            )

    async def fake_exec(*cmd, **kwargs):
        captured["cmd"] = cmd
        return FakeProc()

    monkeypatch.setattr(transcriber, "WHISPER_CLI", "/tmp/whisper-cli")
    monkeypatch.setattr(transcriber, "WHISPER_MODEL", "/tmp/ggml-small.en-tdrz.bin")

    with mock.patch.object(transcriber.asyncio, "create_subprocess_exec", side_effect=fake_exec):
        await transcriber._transcribe(recording)

    assert captured["cmd"][0] == "/tmp/whisper-cli"
    assert "--tinydiarize" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("-m") + 1] == "/tmp/ggml-small.en-tdrz.bin"
    assert recording.transcript_path.read_text() == "Hello there."


def test_build_whisper_command_includes_resource_controls(tmp_path, monkeypatch):
    """Local mode can run whisper in the background with bounded threads."""
    monkeypatch.setattr(transcriber, "WHISPER_CLI", "/tmp/whisper-cli")
    monkeypatch.setattr(transcriber, "WHISPER_MODEL", "/tmp/ggml-small.en-tdrz.bin")
    monkeypatch.setattr(transcriber, "WHISPER_THREADS", 2)
    monkeypatch.setattr(transcriber, "WHISPER_NICE", 20)
    monkeypatch.setattr(transcriber, "WHISPER_TASKPOLICY_BACKGROUND", True)
    monkeypatch.setattr(transcriber.shutil, "which", lambda name: f"/usr/bin/{name}")

    cmd = transcriber._build_whisper_command(tmp_path / "meeting.wav")

    assert cmd[:5] == ["/usr/bin/taskpolicy", "-b", "/usr/bin/nice", "-n", "20"]
    assert cmd[5] == "/tmp/whisper-cli"
    assert cmd[cmd.index("-t") + 1] == "2"
    assert "--tinydiarize" in cmd


def test_build_whisper_command_uses_taskpolicy_fallback_path(tmp_path, monkeypatch):
    """launchd PATH may omit /usr/sbin, where macOS provides taskpolicy."""
    monkeypatch.setattr(transcriber, "WHISPER_CLI", "/tmp/whisper-cli")
    monkeypatch.setattr(transcriber, "WHISPER_MODEL", "/tmp/ggml-small.en-tdrz.bin")
    monkeypatch.setattr(transcriber, "WHISPER_THREADS", None)
    monkeypatch.setattr(transcriber, "WHISPER_NICE", None)
    monkeypatch.setattr(transcriber, "WHISPER_TASKPOLICY_BACKGROUND", True)
    monkeypatch.setattr(transcriber.shutil, "which", lambda _name: None)
    monkeypatch.setattr(transcriber.Path, "exists", lambda self: str(self) == "/usr/sbin/taskpolicy")

    cmd = transcriber._build_whisper_command(tmp_path / "meeting.wav")

    assert cmd[:2] == ["/usr/sbin/taskpolicy", "-b"]
    assert cmd[2] == "/tmp/whisper-cli"


def test_room_capture_metadata_is_preserved(tmp_path):
    recording = transcriber.Recording(
        "Tablet Meeting",
        tmp_path / "tablet.wav",
        capture_mode="room",
    )
    recording.meeting_end = datetime.now(timezone.utc)

    metadata = transcriber._build_transcript_front_matter(recording)

    assert recording.to_dict()["capture_mode"] == "room"
    assert "recording_source: transcriber" in metadata
    assert "capture_mode: room" in metadata


def test_start_request_rejects_unknown_capture_mode():
    with pytest.raises(ValueError):
        transcriber.StartRequest(title="Meeting", capture_mode="phone")


@pytest.mark.asyncio
async def test_wait_for_transcription_slot_defers_while_recording(monkeypatch):
    """Local mode should not start Whisper while a new recording is active."""
    queued = _make_recording("Queued Meeting")
    active = _make_recording("Active Meeting")
    transcriber.active_recording = active
    monkeypatch.setattr(transcriber, "TRANSCRIBE_WHILE_RECORDING", False)
    monkeypatch.setattr(transcriber, "TRANSCRIPTION_IDLE_DELAY_SECONDS", 0)

    sleeps = 0

    async def fake_sleep(_delay):
        nonlocal sleeps
        sleeps += 1
        transcriber.active_recording = None

    monkeypatch.setattr(transcriber.asyncio, "sleep", fake_sleep)

    await transcriber._wait_for_transcription_slot(queued)

    assert sleeps == 1


@pytest.mark.asyncio
async def test_queue_processes_sequentially():
    """Transcriptions should run one at a time, not concurrently."""
    execution_log = []
    concurrency = {"current": 0, "max": 0}

    original_transcribe = transcriber._transcribe

    async def mock_transcribe(recording):
        concurrency["current"] += 1
        concurrency["max"] = max(concurrency["max"], concurrency["current"])
        execution_log.append(f"start:{recording.title}")
        await asyncio.sleep(0.05)  # Simulate work
        execution_log.append(f"end:{recording.title}")
        recording.state = transcriber.RecordingState.COMPLETED
        concurrency["current"] -= 1

    with mock.patch.object(transcriber, "_transcribe", side_effect=mock_transcribe):
        # Start the worker
        worker = asyncio.create_task(transcriber._transcription_worker())

        # Enqueue 3 recordings
        for i in range(3):
            rec = _make_recording(f"Meeting {i}")
            await transcriber._transcription_queue.put(rec)

        # Wait for all to be processed
        await transcriber._transcription_queue.join()
        worker.cancel()

    # Should never have run more than 1 concurrently
    assert concurrency["max"] == 1, f"Max concurrency was {concurrency['max']}, expected 1"

    # Should have processed in order
    assert execution_log == [
        "start:Meeting 0", "end:Meeting 0",
        "start:Meeting 1", "end:Meeting 1",
        "start:Meeting 2", "end:Meeting 2",
    ]


@pytest.mark.asyncio
async def test_queue_archives_after_completion():
    """Completed recordings should be archived to recent_recordings."""
    async def mock_transcribe(recording):
        recording.state = transcriber.RecordingState.COMPLETED

    with mock.patch.object(transcriber, "_transcribe", side_effect=mock_transcribe):
        worker = asyncio.create_task(transcriber._transcription_worker())

        rec = _make_recording("Archive Test")
        await transcriber._transcription_queue.put(rec)
        await transcriber._transcription_queue.join()
        worker.cancel()

    assert len(transcriber.recent_recordings) == 1
    assert transcriber.recent_recordings[0].title == "Archive Test"


@pytest.mark.asyncio
async def test_queue_survives_transcription_error():
    """An error in one transcription should not block subsequent ones."""
    call_count = {"n": 0}

    async def mock_transcribe(recording):
        call_count["n"] += 1
        if recording.title == "Bad Meeting":
            raise RuntimeError("whisper exploded")
        recording.state = transcriber.RecordingState.COMPLETED

    with mock.patch.object(transcriber, "_transcribe", side_effect=mock_transcribe):
        worker = asyncio.create_task(transcriber._transcription_worker())

        await transcriber._transcription_queue.put(_make_recording("Good Meeting 1"))
        await transcriber._transcription_queue.put(_make_recording("Bad Meeting"))
        await transcriber._transcription_queue.put(_make_recording("Good Meeting 2"))

        await transcriber._transcription_queue.join()
        worker.cancel()

    # All 3 should have been attempted
    assert call_count["n"] == 3
    # All 3 should be archived (including the failed one)
    assert len(transcriber.recent_recordings) == 3


@pytest.mark.asyncio
async def test_queue_depth_reported():
    """Queue depth should reflect waiting items."""
    gate = asyncio.Event()

    async def slow_transcribe(recording):
        await gate.wait()
        recording.state = transcriber.RecordingState.COMPLETED

    with mock.patch.object(transcriber, "_transcribe", side_effect=slow_transcribe):
        worker = asyncio.create_task(transcriber._transcription_worker())

        # Enqueue 3 — first starts immediately, 2 wait in queue
        for i in range(3):
            await transcriber._transcription_queue.put(_make_recording(f"M{i}"))

        # Give the worker a moment to pick up the first item
        await asyncio.sleep(0.02)
        assert transcriber._transcription_queue.qsize() == 2

        # Release the gate and let everything finish
        gate.set()
        await transcriber._transcription_queue.join()
        worker.cancel()

    assert transcriber._transcription_queue.qsize() == 0


@pytest.mark.asyncio
async def test_status_endpoint_includes_queue_depth(tmp_path):
    """The /status endpoint should include transcription_queue_depth."""
    from httpx import AsyncClient, ASGITransport

    with mock.patch.object(transcriber, "RECORDINGS_DIR", tmp_path):
        async with AsyncClient(
            transport=ASGITransport(app=transcriber.app), base_url="http://test"
        ) as client:
            resp = await client.get("/status")
            data = resp.json()
            assert "transcription_queue_depth" in data
            assert data["transcription_queue_depth"] == 0


# ============================================================================
# cleanup_old_recordings()
# ============================================================================

class TestCleanupOldRecordings:
    """Tests for cleanup_old_recordings()."""

    def test_deletes_old_wav_files(self, tmp_path):
        """Should delete .wav files older than max_age_days."""
        old_file = tmp_path / "old-meeting.wav"
        old_file.write_bytes(b"\x00" * 100)
        # Set mtime to 10 days ago
        old_mtime = time.time() - (10 * 86400)
        os.utime(old_file, (old_mtime, old_mtime))

        deleted = transcriber.cleanup_old_recordings(tmp_path, max_age_days=7)

        assert deleted == 1
        assert not old_file.exists()

    def test_deletes_old_txt_files(self, tmp_path):
        """Should also delete .txt transcript files."""
        old_txt = tmp_path / "old-meeting.txt"
        old_txt.write_text("transcript text")
        old_mtime = time.time() - (10 * 86400)
        os.utime(old_txt, (old_mtime, old_mtime))

        deleted = transcriber.cleanup_old_recordings(tmp_path, max_age_days=7)

        assert deleted == 1
        assert not old_txt.exists()

    def test_keeps_recent_files(self, tmp_path):
        """Should not delete files newer than max_age_days."""
        new_file = tmp_path / "new-meeting.wav"
        new_file.write_bytes(b"\x00" * 100)
        # mtime is now (default), well within 7 days

        deleted = transcriber.cleanup_old_recordings(tmp_path, max_age_days=7)

        assert deleted == 0
        assert new_file.exists()

    def test_ignores_non_recording_files(self, tmp_path):
        """Should not delete files with other extensions."""
        old_json = tmp_path / "config.json"
        old_json.write_text("{}")
        old_mtime = time.time() - (10 * 86400)
        os.utime(old_json, (old_mtime, old_mtime))

        deleted = transcriber.cleanup_old_recordings(tmp_path, max_age_days=7)

        assert deleted == 0
        assert old_json.exists()

    def test_handles_mixed_ages(self, tmp_path):
        """Should only delete old files, keeping recent ones."""
        old_wav = tmp_path / "old.wav"
        old_wav.write_bytes(b"\x00" * 100)
        old_mtime = time.time() - (10 * 86400)
        os.utime(old_wav, (old_mtime, old_mtime))

        new_wav = tmp_path / "new.wav"
        new_wav.write_bytes(b"\x00" * 100)

        old_txt = tmp_path / "old.txt"
        old_txt.write_text("old transcript")
        os.utime(old_txt, (old_mtime, old_mtime))

        new_txt = tmp_path / "new.txt"
        new_txt.write_text("new transcript")

        deleted = transcriber.cleanup_old_recordings(tmp_path, max_age_days=7)

        assert deleted == 2
        assert not old_wav.exists()
        assert not old_txt.exists()
        assert new_wav.exists()
        assert new_txt.exists()

    def test_handles_empty_directory(self, tmp_path):
        """Should return 0 for empty directory."""
        deleted = transcriber.cleanup_old_recordings(tmp_path, max_age_days=7)
        assert deleted == 0

    def test_handles_missing_directory(self, tmp_path):
        """Should return 0 if directory doesn't exist."""
        deleted = transcriber.cleanup_old_recordings(tmp_path / "nonexistent", max_age_days=7)
        assert deleted == 0

    def test_respects_custom_max_age(self, tmp_path):
        """Should use the provided max_age_days value."""
        file_3_days = tmp_path / "three-days.wav"
        file_3_days.write_bytes(b"\x00" * 100)
        mtime_3d = time.time() - (3 * 86400)
        os.utime(file_3_days, (mtime_3d, mtime_3d))

        # 7-day threshold: should keep
        assert transcriber.cleanup_old_recordings(tmp_path, max_age_days=7) == 0

        # 2-day threshold: should delete
        assert transcriber.cleanup_old_recordings(tmp_path, max_age_days=2) == 1

    def test_boundary_exactly_at_cutoff(self, tmp_path):
        """File exactly at the cutoff age should be deleted (< comparison on floats)."""
        file_exact = tmp_path / "boundary.wav"
        file_exact.write_bytes(b"\x00" * 100)
        # Set mtime to exactly 7 days ago
        exact_mtime = time.time() - (7 * 86400)
        os.utime(file_exact, (exact_mtime, exact_mtime))

        # At exact boundary with float arithmetic, this is effectively at the cutoff
        # and will be deleted due to float precision
        deleted = transcriber.cleanup_old_recordings(tmp_path, max_age_days=7)
        assert deleted == 1


# ===========================================================================
# Hallucination removal tests
# ===========================================================================


class TestRemoveHallucinatedLines:
    """Tests for _remove_hallucinated_lines()."""

    def test_no_repetition(self):
        """Normal transcript with no repetition is unchanged."""
        transcript = (
            "[00:00:00.000 --> 00:00:05.000]   Hello there.\n"
            "[00:00:05.000 --> 00:00:10.000]   How are you?\n"
            "[00:00:10.000 --> 00:00:15.000]   I'm fine, thanks."
        )
        result = transcriber._remove_hallucinated_lines(transcript)
        assert result == transcript

    def test_removes_long_repetition(self):
        """Consecutive identical text repeated many times is removed."""
        real_lines = [
            "[00:00:00.000 --> 00:00:05.000]   Hello there.",
            "[00:00:05.000 --> 00:00:10.000]   How are you?",
        ]
        # 10 hallucinated lines
        hallucinated = [
            f"[00:00:{10+i:02d}.000 --> 00:00:{11+i:02d}.000]   I can do everything."
            for i in range(10)
        ]
        more_real = [
            "[00:01:00.000 --> 00:01:05.000]   Anyway, moving on.",
        ]
        transcript = "\n".join(real_lines + hallucinated + more_real)
        result = transcriber._remove_hallucinated_lines(transcript)
        lines = result.split("\n")
        assert len(lines) == 3  # only the real lines kept
        assert "I can do everything" not in result
        assert "Hello there" in result
        assert "How are you" in result
        assert "moving on" in result

    def test_keeps_short_repetition(self):
        """Short repetition (≤2 consecutive) is normal conversation, kept."""
        transcript = (
            "[00:00:00.000 --> 00:00:03.000]   Yeah.\n"
            "[00:00:03.000 --> 00:00:05.000]   Yeah.\n"
            "[00:00:05.000 --> 00:00:10.000]   That sounds good."
        )
        result = transcriber._remove_hallucinated_lines(transcript)
        assert result == transcript  # 2 repeats is below threshold

    def test_threshold_boundary(self):
        """Exactly at threshold (3) → removed."""
        lines = [
            f"[00:00:{i:02d}.000 --> 00:00:{i+1:02d}.000]   Here we go."
            for i in range(3)
        ]
        transcript = "\n".join(lines)
        result = transcriber._remove_hallucinated_lines(transcript)
        assert result == ""

    def test_multiple_hallucination_blocks(self):
        """Multiple different hallucination phrases are all removed."""
        real = "[00:00:00.000 --> 00:00:05.000]   Real content."
        block1 = "\n".join(
            f"[00:01:{i:02d}.000 --> 00:01:{i+1:02d}.000]   And then I have to do this."
            for i in range(5)
        )
        mid = "[00:02:00.000 --> 00:02:05.000]   More real content."
        block2 = "\n".join(
            f"[00:03:{i:02d}.000 --> 00:03:{i+1:02d}.000]   I'm going to be able to do everything."
            for i in range(8)
        )
        end = "[00:04:00.000 --> 00:04:05.000]   Final real line."
        transcript = "\n".join([real, block1, mid, block2, end])
        result = transcriber._remove_hallucinated_lines(transcript)
        lines = result.split("\n")
        assert len(lines) == 3
        assert "Real content" in result
        assert "More real content" in result
        assert "Final real line" in result

    def test_empty_transcript(self):
        """Empty string is returned as-is."""
        assert transcriber._remove_hallucinated_lines("") == ""

    def test_preserves_blank_lines(self):
        """Blank lines don't count as repetition and are preserved."""
        transcript = (
            "[00:00:00.000 --> 00:00:05.000]   Hello.\n"
            "\n"
            "[00:00:05.000 --> 00:00:10.000]   World."
        )
        result = transcriber._remove_hallucinated_lines(transcript)
        assert result == transcript

    def test_realistic_mia_handover_pattern(self):
        """Simulates the actual Mia--Edd-handover hallucination pattern."""
        real_start = [
            "[00:00:30.000 --> 00:00:36.000]   Oh, hello.",
            "[00:00:36.000 --> 00:00:42.340]   Do you mind if I take four minutes?",
            "[00:00:42.340 --> 00:00:44.540]   I mind not a jot.",
        ]
        # Simulate 1000+ hallucinated 1-second segments
        hallucination = [
            f"[00:{m:02d}:{s:02d}.420 --> 00:{m:02d}:{s+1:02d}.420]   And then I have to do this."
            for m in range(8, 32)
            for s in range(0, 59)
        ]
        real_middle = [
            "[00:32:05.000 --> 00:32:10.000]   So the big thing you should know...",
        ]
        hallucination2 = [
            f"[00:{m:02d}:{s:02d}.420 --> 00:{m:02d}:{s+1:02d}.420]   I'm going to be able to do everything."
            for m in range(35, 57)
            for s in range(0, 59)
        ]
        real_end = [
            "[00:57:00.000 --> 00:57:05.000]   Alright, talk to you later.",
        ]

        transcript = "\n".join(
            real_start + hallucination + real_middle + hallucination2 + real_end
        )
        result = transcriber._remove_hallucinated_lines(transcript)
        result_lines = result.split("\n")

        # Should keep only the 5 real lines
        assert len(result_lines) == 5
        assert "Oh, hello" in result
        assert "four minutes" in result
        assert "I mind not a jot" in result
        assert "big thing" in result
        assert "talk to you later" in result
        # Hallucinated content should be gone
        assert "And then I have to do this" not in result
        assert "I'm going to be able to do everything" not in result


# ===========================================================================
# Timestamp stripping tests
# ===========================================================================


class TestStripTimestampsWithGaps:
    """Tests for _strip_timestamps_with_gaps()."""

    def test_strips_timestamps(self):
        """Timestamps are removed, text preserved."""
        transcript = (
            "[00:00:00.000 --> 00:00:05.000]   Hello there.\n"
            "[00:00:05.000 --> 00:00:10.000]   How are you?"
        )
        result = transcriber._strip_timestamps_with_gaps(transcript)
        assert result == "Hello there.\nHow are you?"

    def test_speaker_turn_becomes_marker(self):
        """[SPEAKER_TURN] tokens are converted to [S]."""
        transcript = (
            "[00:00:00.000 --> 00:00:05.000]   First speaker. [SPEAKER_TURN]\n"
            "[00:00:05.000 --> 00:00:10.000]   Second speaker."
        )
        result = transcriber._strip_timestamps_with_gaps(transcript)
        assert result == "First speaker. [S]\nSecond speaker."

    def test_multiple_speaker_turns(self):
        """Multiple [SPEAKER_TURN] markers are each converted."""
        transcript = (
            "[00:00:00.000 --> 00:00:05.000]   Hello. [SPEAKER_TURN]\n"
            "[00:00:05.000 --> 00:00:10.000]   Hi there. [SPEAKER_TURN]\n"
            "[00:00:10.000 --> 00:00:15.000]   How are you?"
        )
        result = transcriber._strip_timestamps_with_gaps(transcript)
        assert result == "Hello. [S]\nHi there. [S]\nHow are you?"

    def test_contiguous_segments(self):
        """Back-to-back segments produce no extra formatting."""
        transcript = (
            "[00:00:00.000 --> 00:00:03.000]   Line one.\n"
            "[00:00:03.000 --> 00:00:06.000]   Line two.\n"
            "[00:00:06.000 --> 00:00:09.000]   Line three."
        )
        result = transcriber._strip_timestamps_with_gaps(transcript)
        assert result == "Line one.\nLine two.\nLine three."

    def test_empty_string(self):
        """Empty input returns empty output."""
        assert transcriber._strip_timestamps_with_gaps("") == ""

    def test_non_timestamped_lines_pass_through(self):
        """Lines without timestamps are kept as-is."""
        transcript = "Just plain text.\nAnother line."
        result = transcriber._strip_timestamps_with_gaps(transcript)
        assert result == transcript

    def test_realistic_conversation(self):
        """Simulates a real tinydiarize conversation with speaker turns."""
        transcript = (
            "[00:00:00.000 --> 00:00:05.000]   Hello. [SPEAKER_TURN]\n"
            "[00:00:05.000 --> 00:00:10.000]   Do you mind if I take a minute? [SPEAKER_TURN]\n"
            "[00:00:10.000 --> 00:00:12.000]   Sure, go ahead.\n"
            "[00:02:12.000 --> 00:02:15.000]   OK I've sent it. [SPEAKER_TURN]\n"
            "[00:02:15.000 --> 00:02:20.000]   Great, so the big news is...\n"
            "[00:02:23.500 --> 00:02:25.000]   Right. [SPEAKER_TURN]\n"
            "[00:02:25.000 --> 00:02:30.000]   Yeah, so basically..."
        )
        result = transcriber._strip_timestamps_with_gaps(transcript)
        lines = result.split("\n")
        assert lines[0] == "Hello. [S]"
        assert lines[1] == "Do you mind if I take a minute? [S]"
        assert lines[2] == "Sure, go ahead."
        assert lines[3] == "OK I've sent it. [S]"
        assert lines[5] == "Right. [S]"


class TestAnnotateLocalSpeakerSegments:
    """Tests for local-speaker channel annotation."""

    def test_labels_first_local_segment(self, tmp_path, monkeypatch):
        audio_path = tmp_path / "speaker.wav"
        _write_stereo_wav(
            audio_path,
            [(100, 10000)] * 100 + [(10000, 100)] * 100,
        )
        transcript = (
            "[00:00:00.000 --> 00:00:01.000]   Opening thought.\n"
            "[00:00:01.000 --> 00:00:02.000]   [SPEAKER_TURN] Reply from the other side."
        )

        monkeypatch.setattr(transcriber, "LOCAL_SPEAKER_LABEL", "Edd")
        monkeypatch.setattr(transcriber, "LOCAL_SPEAKER_CHANNEL", 2)
        monkeypatch.setattr(transcriber, "LOCAL_SPEAKER_MIN_DBFS", -40.0)
        monkeypatch.setattr(transcriber, "LOCAL_SPEAKER_DOMINANCE_DB", 6.0)

        annotated = transcriber._annotate_local_speaker_segments(transcript, audio_path)

        assert "[speaker:Edd] Opening thought." in annotated
        assert "[SPEAKER_TURN] Reply from the other side." in annotated

    def test_leaves_mono_audio_unmodified(self, tmp_path):
        audio_path = tmp_path / "mono.wav"
        with wave.open(str(audio_path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(100)
            wav_file.writeframes(b"".join(struct.pack("<h", 1000) for _ in range(100)))

        transcript = "[00:00:00.000 --> 00:00:01.000]   Hello there."
        annotated = transcriber._annotate_local_speaker_segments(transcript, audio_path)

        assert annotated == transcript

    def test_strip_preserves_explicit_speaker_labels(self):
        transcript = (
            "[00:00:00.000 --> 00:00:05.000]   [speaker:Edd] Hello there.\n"
            "[00:00:05.000 --> 00:00:10.000]   [SPEAKER_TURN] Sounds good."
        )

        stripped = transcriber._strip_timestamps_with_gaps(transcript)

        assert stripped == "[speaker:Edd] Hello there.\n[S] Sounds good."


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
