# Transcriber Setup Guide

End-to-end setup for meeting transcription: laptop audio → VBAN streaming → configurable transcription host → webhook.

This guide covers both the **client** (your laptop) and the **server**. The server can be either the transcription appliance ("pilot") or this laptop for local transcription.

## Architecture Overview

```text
┌─────────── Your Laptop ─────────────────────────────────────────────┐
│                                                                     │
│  SoundSource:                                                       │
│    Zoom/Teams audio ─┬─► Your speakers (you hear the meeting)       │
│                      └─► BlackHole 2ch (captured for transcription) │
│                                                                     │
│  vban_send.py (launched by meeting_bar.py / meeting.py):             │
│    BlackHole 2ch ──► ┐                                              │
│    (remote audio)    ├─ mix ─► VBAN UDP packets ─► target:6980      │
│    Your mic ───────► ┘                                              │
│    (your voice)                                                     │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘

                        ▼  VBAN over Tailscale / LAN  ▼

┌─────────── Transcriber target (pilot or local laptop) ──────────────┐
│                                                                     │
│  transcriber.py (FastAPI on port 8000):                             │
│    UDP :6980 ─► VBANCapture ─► WAV file                             │
│    WAV file ─► whisper.cpp (small.en-tdrz, Metal GPU) ─► transcript │
│              └► channel analysis ─► [speaker:Edd] labels            │
│    transcript ─► POST to meetingnotesd webhook                      │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘

                        ▼  HTTP webhook  ▼

┌─────────── meetingnotesd ───────────────────────────────────────────┐
│  Receives transcript, runs AI summarization, writes org-mode notes  │
└─────────────────────────────────────────────────────────────────────┘
```

## Prerequisites

| Item | Where | Purpose |
| --- | --- | --- |
| Mac with Apple Silicon | Laptop | Audio capture and VBAN streaming |
| Xcode Command Line Tools | Laptop | Required for building `mic_active` Swift helper (`xcode-select --install`) |
| Mac Mini M1+ | Optional server ("pilot") | Whisper transcription with Metal GPU |
| [Tailscale](https://tailscale.com/) | Both | Secure networking between machines |
| [BlackHole 2ch](https://existential.audio/blackhole/) | Laptop | Virtual audio device for routing |
| [SoundSource](https://rogueamoeba.com/soundsource/) | Laptop | Per-app audio output routing |
| SSH key access | Laptop → pilot | Deployment and management |

---

## Part 1: Server Setup (Pilot)

The server runs the transcriber service — whisper.cpp for speech-to-text, listening for VBAN audio packets from your laptop.

### 1.1 Initial Provisioning

From your laptop, in this repo:

```bash
cd transcriber

# Check connectivity and system info
make check

# Full provisioning (Homebrew, dependencies, whisper.cpp, launchd service)
make provision
```

This runs four scripts in order:

1. **01-homebrew.sh** — Installs Homebrew on pilot
2. **02-dependencies.sh** — Installs ffmpeg and uv
3. **03-whisper.sh** — Clones, builds whisper.cpp with Metal support, downloads the small.en-tdrz model
4. **04-service.sh** — Installs and loads the `com.transcriber` launchd service

### 1.2 Deploy the Transcriber

```bash
make deploy
```

This rsyncs `server/transcriber.py` to `~/transcriber/` on pilot and restarts the service.

### 1.3 Verify

```bash
make status
# Should return: {"status":"ok","service":"transcriber","vban_port":6980,...}

make logs
# Watch for startup messages
```

### 1.4 How It Works

The transcriber is a FastAPI server (`transcriber.py`) running on port 8000:

- **`POST /start`** — Opens a UDP socket on port 6980, starts capturing VBAN packets directly to a WAV file
- **`POST /stop`** — Stops capture, closes the WAV, runs whisper-cli on it, POSTs the transcript to the webhook
- **`GET /status`** — Health check with disk space, current recording state, etc.

The transcriber captures VBAN audio directly — no BlackHole, no ffmpeg, no intermediate services on the server side.

### 1.5 Configuration

Environment variables (set in `com.transcriber.plist`):

| Variable | Default | Description |
| --- | --- | --- |
| `WEBHOOK_URL` | `http://nuctu:9876/webhook` | Where to POST transcripts |
| `VBAN_PORT` | `6980` | UDP port for VBAN audio |
| `WHISPER_CLI` | `~/whisper.cpp/build/bin/whisper-cli` | Path to whisper binary |
| `WHISPER_MODEL` | `~/whisper.cpp/models/ggml-small.en-tdrz.bin` | Whisper model file |
| `RECORDINGS_DIR` | `~/transcriber/recordings` | Where WAV files are stored |
| `TRANSCRIBER_HOST` | `0.0.0.0` | Listen address |
| `TRANSCRIBER_PORT` | `8000` | HTTP API port |
| `WHISPER_THREADS` | unset | Optional `whisper-cli -t` thread cap |
| `WHISPER_NICE` | unset | Optional `nice -n` priority |
| `WHISPER_TASKPOLICY_BACKGROUND` | `false` | Run Whisper with `taskpolicy -b` on macOS |
| `TRANSCRIBE_WHILE_RECORDING` | `true` | If `false`, queued transcription waits while a new recording is active |
| `TRANSCRIPTION_IDLE_DELAY_SECONDS` | `0` | Quiet window before transcription starts |
| `LOCAL_SPEAKER_LABEL` | `Edd` | Label emitted for the local mic channel |
| `LOCAL_SPEAKER_CHANNEL` | `2` | 1-based local mic channel in stereo captures |
| `LOCAL_SPEAKER_MIN_DBFS` | `-40` | Minimum mic level to label a segment |
| `LOCAL_SPEAKER_DOMINANCE_DB` | `4.5` | Required dB lead over the other channel |

---

## Part 2: Local Transcriber Setup (Optional)

Local mode keeps the same VBAN/API architecture, but runs `transcriber.py` on this laptop instead of pilot:

```javascript
laptop meeting_bar.py ──VBAN──► 127.0.0.1:6980
meeting_bar.py ──HTTP──► http://127.0.0.1:8000
local transcriber ──webhook──► http://nuctu:9876/webhook
```

This is useful when you want everything to stay on the laptop, but Whisper must not compete aggressively with Teams. The local launchd plist runs Whisper with these safeguards:

| Variable | Local value | Purpose |
| --- | --- | --- |
| `TRANSCRIBER_HOST` | `127.0.0.1` | Bind the API to localhost only |
| `TRANSCRIBE_WHILE_RECORDING` | `false` | Do not start queued Whisper work during another meeting recording |
| `TRANSCRIPTION_IDLE_DELAY_SECONDS` | `30` | Wait for a quiet window after recording stops |
| `WHISPER_TASKPOLICY_BACKGROUND` | `true` | Put Whisper in macOS background scheduling policy |
| `WHISPER_NICE` | `20` | Lowest CPU priority |
| `WHISPER_THREADS` | `2` | Cap Whisper CPU threads |

### 2.1 Install whisper.cpp locally

The local transcriber expects the same default paths as pilot:

```bash
~/whisper.cpp/build/bin/whisper-cli
~/whisper.cpp/models/ggml-small.en-tdrz.bin
```

You can reuse the setup script locally:

```bash
cd transcriber
./setup/03-whisper.sh
```

Verify that Whisper is Metal-enabled:

```bash
grep -Ei 'GGML_METAL|GGML_AVAILABLE_BACKENDS' ~/whisper.cpp/build/CMakeCache.txt
otool -L ~/whisper.cpp/build/bin/whisper-cli | grep -i metal
```

You should see `GGML_METAL:BOOL=ON`, `ggml-metal` in the available backends,
and `libggml-metal` linked by `whisper-cli`.

For a runtime check:

```bash
cd ~/whisper.cpp
build/bin/whisper-cli -m models/ggml-small.en-tdrz.bin -f samples/jfk.wav -l en --tinydiarize -t 2
```

The startup log should include `use gpu = 1`, `GPU name: MTL0`, and
`using MTL0 backend`.

### 2.2 Start the local transcriber

```bash
cd transcriber
make local-transcriber-install
make local-transcriber-status
```

Logs:

```bash
make local-transcriber-logs
```

The local launchd agent is installed at:

```text
~/Library/LaunchAgents/com.transcriber.local.plist
```

It has `RunAtLoad` and `KeepAlive` enabled, so it restarts after you log in
following a reboot. The service intentionally binds to localhost only and runs
Whisper conservatively:

| Variable | Local value | Purpose |
| --- | --- | --- |
| `TRANSCRIBER_HOST` | `127.0.0.1` | Bind the API to localhost only |
| `TRANSCRIBE_WHILE_RECORDING` | `false` | Do not start queued Whisper work during another meeting recording |
| `TRANSCRIPTION_IDLE_DELAY_SECONDS` | `30` | Wait for a quiet window after recording stops |
| `WHISPER_TASKPOLICY_BACKGROUND` | `true` | Put Whisper in macOS background scheduling policy |
| `WHISPER_NICE` | `20` | Lowest CPU priority |
| `WHISPER_THREADS` | `2` | Cap Whisper CPU threads |

### 2.3 Switch the menu bar to local mode

```bash
cd transcriber
make meeting-bar-local
```

Switch back to pilot at any time:

```bash
make meeting-bar-pilot
```

The underlying configuration knob is `TRANSCRIBER_TARGET_HOST`. The menu bar derives both defaults from it:

| Variable | Default | Description |
| --- | --- | --- |
| `TRANSCRIBER_TARGET_HOST` | `pilot` | Host for both HTTP API and VBAN |
| `TRANSCRIBER_URL` | `http://$TRANSCRIBER_TARGET_HOST:8000` | Optional HTTP API override |
| `PILOT_HOST` | `$TRANSCRIBER_TARGET_HOST` | Legacy VBAN target override |
| `AUDIOMXD_END_QUERY_TTL_SECONDS` | `15` | Cache expensive Teams/Edge audiomxd log queries during end detection only; start detection still checks every poll |

`make meeting-bar-local` writes `TRANSCRIBER_TARGET_HOST=127.0.0.1` into the
installed LaunchAgent plist at:

```text
~/Library/LaunchAgents/com.meeting-bar.plist
```

That setting survives reboot/login. Use `make meeting-bar-pilot` to roll back
to the remote pilot transcriber.

---

## Part 3: Client Setup (Laptop)

The laptop captures meeting audio and streams it to the configured transcription target via VBAN.

### 3.1 Install BlackHole 2ch

```bash
brew install --cask blackhole-2ch
```

**Reboot after installation** — the audio driver needs a restart to load.

After rebooting, verify it appears:

```bash
system_profiler SPAudioDataType | grep -i blackhole
```

You should see "BlackHole 2ch" listed. Do **not** set it as your default audio device.

### 3.2 Configure SoundSource

SoundSource routes per-app audio output. We use it to send Zoom/Teams audio to BlackHole while you still hear it through your speakers.

> **Important:** SoundSource routes per-app *output* only. It cannot route microphone input. Your mic is captured separately by the VBAN sender's dual-input mixing mode.

1. **Open SoundSource** (menu bar icon)
2. **Configure Zoom:**

Find **zoom.us** in the Applications list (click **+** to add if needed)Click the **Output** dropdownSelect **Multi-Output** → check both:✅ Your normal speakers/headphones✅ **BlackHole 2ch**

3. **Configure Microsoft Teams:**

Same as Zoom: set Output to Multi-Output with speakers + BlackHole 2ch

4. **Optional — Save as Profile:**

Save as "Meeting Recording" for quick toggling

After configuration, meeting audio flows to both your ears AND BlackHole simultaneously.

### 3.3 Verify Audio Devices

```bash
cd transcriber
uv run meeting.py devices
```

You should see:

- `BlackHole 2ch` marked as ★ RECOMMENDED
- Your mic (e.g., "Yeti Stereo Microphone", "MacBook Air Microphone") auto-detected for mixing

### 3.4 Test Connectivity

```bash
# Check transcriber is reachable
uv run meeting.py status

# Quick round-trip test: start, speak briefly, stop
uv run meeting.py start "Setup Test"
# ... speak for a few seconds ...
uv run meeting.py stop
```

Check the configured transcriber logs to verify transcription completed:

```bash
cd transcriber
make logs                    # pilot
make local-transcriber-logs  # local
```

---

## Part 4: Daily Usage

Once setup is complete, you have two options:

### First-Time: Build Local Helpers

Before first use, compile the `mic_active` Swift binary (needed for Teams detection):

```bash
cd transcriber
make build
```

### Option A: Menu Bar App (Recommended)

```bash
cd transcriber
uv run meeting_bar.py
```

This puts an icon in your menu bar:

- **🎙** — Idle, ready for meetings
- **🔴** — Recording in progress
- **⚠️** — Error state

Features:

- **Auto-detection**: Automatically starts recording when Zoom or Teams meetings begin, and stops when they end
- **Manual control**: Click Start Recording… / Stop Recording from the menu
- **Pilot status**: Shows connection status with the transcription server
- **Toggle auto-detect**: Disable/enable automatic meeting detection via checkbox

The app detects meetings by:

- **Zoom**: Checks for `CptHost` subprocess (only present during active meetings)
- **Teams**: Two-tier detection (Teams 2.x exposes no window titles and AVCaptureDevice doesn't see its mic usage):
    - *Start*: Native Teams process tree running (`MSTeams` or Teams 2.x WebView/helpers) + recent `audiomxd` recording evidence
    - *End*: Queries macOS `audiomxd` system log for Teams audio session state (`isRecording: true/false`), since our own VBAN sender keeps the physical mic active during recording

**First-time setup**: Build the `mic_active` helper before running:

```bash
cd transcriber
make build
```

**Tip**: To run on login, add `uv run meeting_bar.py` to your login items, or create a launchd plist.

### Option B: Manual CLI Commands

```bash
cd transcriber

# Start of meeting:
uv run meeting.py start "Weekly Standup"

# End of meeting:
uv run meeting.py stop

# Check status anytime:
uv run meeting.py status
```

### What Happens Under the Hood

1. `meeting.py start` detects BlackHole + your mic, launches `vban_send.py` in dual-input mixed mode
2. VBAN sender captures from BlackHole (remote participants) AND your mic (your voice), mixes them, streams UDP packets to the configured target
3. `meeting.py start` calls `POST /start` on the target transcriber, which opens a VBAN capture socket
4. When you run `meeting.py stop`, the target writes the WAV, runs whisper.cpp, and POSTs the transcript to the meetingnotesd webhook
5. meetingnotesd runs AI summarization and writes org-mode notes

### Command Reference

```bash
uv run meeting.py start "Title"     # Start recording
uv run meeting.py stop              # Stop and transcribe
uv run meeting.py status            # Show sender + transcriber state
uv run meeting.py devices           # List audio devices

# Options:
uv run meeting.py start "Title" -d ZoomAudioDevice   # Use specific input device
uv run meeting.py start "Title" -m "MacBook Air Mic" # Use specific mic
```

---

## Part 4: Makefile Reference

All server management happens from your laptop via `make`:

```bash
cd transcriber

# Local build
make build           # Compile mic_active Swift binary (Teams detection)

# Daily operations: pilot
make status          # Pilot transcriber health check
make logs            # Tail pilot transcriber logs

# Daily operations: local transcriber
make local-transcriber-status
make local-transcriber-logs

# Deployment
make deploy          # Rsync server/ to pilot, restart service
make check           # Connectivity + system check

# Provisioning (first-time or rebuilding)
make provision       # Full setup (Homebrew, deps, whisper, service)
make model           # Re-download whisper model

# Cleanup
make clean-vban      # Remove obsolete vban-receiver service from pilot

# Utilities
make ssh             # SSH into pilot
make test            # Quick health check
```

---

## Troubleshooting

### Transcriber not reachable

```bash
make check                    # Is pilot reachable at all?
make status                   # Is the pilot service running?
make local-transcriber-status # Is the local service running?
make logs                     # Check pilot startup errors
make local-transcriber-logs   # Check local startup errors
ssh edd@pilot "launchctl list | grep transcriber"
```

### No audio captured (WAV too small)

- Is the VBAN sender running? Check `uv run meeting.py status`
- Is audio being routed? Play a Zoom/Teams test call and check SoundSource meters
- Check sender log: `cat /tmp/meeting-vban-sender.log`
- Verify ports: sender sends to the target host on UDP :6980, transcriber listens on 6980

### BlackHole 2ch not appearing

- Did you reboot after `brew install --cask blackhole-2ch`?
- Check: `system_profiler SPAudioDataType | grep -i blackhole`

### Only capturing remote audio (no mic)

- Check mic detection: `uv run meeting.py devices`
- System default input should be your real mic, not a virtual device
- Specify mic manually: `uv run meeting.py start "Test" -m "MacBook Air Mic"`
- Check sender log for "mixed mode": `cat /tmp/meeting-vban-sender.log`

### Only capturing mic (no remote audio)

- Verify SoundSource is routing Zoom/Teams output → BlackHole 2ch
- Check SoundSource shows Multi-Output with BlackHole checked for the app
- Make sure the meeting app is actually running and producing audio

### Transcription quality issues

- whisper.cpp with `small.en-tdrz` is the intended configuration because it emits tinydiarize speaker-turn markers
- In dual-input mode, the laptop sends remote audio on the left channel and the local mic on the right channel so the transcriber can emit `[speaker:Edd]` labels for high-confidence local speech
- Very short recordings (< 3s) may produce empty output
- Check WAV quality: `ssh edd@pilot "python3 -c \"import wave; w=wave.open('<path>'); print(f'{w.getframerate()}Hz {w.getnframes()/w.getframerate():.1f}s')\""`

### Redeploying after code changes

```bash
cd transcriber
make deploy           # Pushes server/ to pilot and restarts
```

### Rebuilding whisper.cpp

```bash
make provision-whisper   # Rebuilds from latest source
make model               # Re-downloads model
```

---

## Network Configuration

| Service | Host | Port | Protocol | Direction |
| --- | --- | --- | --- | --- |
| VBAN audio | `TRANSCRIBER_TARGET_HOST` | 6980 | UDP | Laptop → target |
| Transcriber API | `TRANSCRIBER_TARGET_HOST` | 8000 | HTTP | Laptop → target |
| meetingnotesd | nuctu | 9876 | HTTP | Target → nuctu |

When using pilot, the laptop and pilot communicate over Tailscale. Ensure both machines are on the same tailnet and can resolve each other's hostnames. Local mode uses `127.0.0.1` and does not require Tailscale for the audio/API hop.

---

## File Locations

### Laptop

| File | Purpose |
| --- | --- |
| `transcriber/meeting_bar.py` | Menu bar app (auto-detect + manual control) |
| `transcriber/mic_active.swift` | CoreAudio physical mic detector (compiled via `make build`) |
| `transcriber/meeting.py` | CLI command interface |
| `transcriber/vban/vban_send.py` | VBAN audio sender |
| `transcriber/Makefile` | Server management |
| `transcriber/server/transcriber.py` | Server code (deployed to pilot) |
| `transcriber/com.transcriber.plist` | launchd service definition |
| `transcriber/setup/` | Provisioning scripts |
| `/tmp/meeting-vban-sender.log` | VBAN sender log |
| `/tmp/meeting-vban-sender.pid` | VBAN sender PID file |
| `/tmp/meeting-bar.log` | Menu bar app log |

### Pilot (Server)

| File | Purpose |
| --- | --- |
| `~/transcriber/transcriber.py` | Running transcriber server |
| `~/transcriber/recordings/` | WAV files and transcripts |
| `~/Library/Logs/transcriber.log` | Service log |
| `~/Library/LaunchAgents/com.transcriber.plist` | launchd plist |
| `~/whisper.cpp/` | whisper.cpp source and build |
| `~/whisper.cpp/models/ggml-small.en-tdrz.bin` | Whisper tinydiarize model |