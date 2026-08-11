# Agent Instructions — Transcriber Subsystem

This document is for AI agents working on the `transcriber/` subtree of
meeting-notes-processor. Read this before making any changes.

## What This Is

A two-machine audio transcription pipeline:

1. **Laptop** runs `meeting_bar.py` (macOS menu bar app). Detects Zoom/Teams
   meetings, captures audio via VBAN, streams it to the configured transcriber.
2. **pilot** (Mac Mini M1) or the laptop runs `transcriber.py` (FastAPI server).
   It receives VBAN audio, writes WAV, runs whisper.cpp, POSTs the transcript
   to `meetingnotesd` on nuctu.

```
laptop                          pilot (Mac Mini)                nuctu
┌──────────────────┐     UDP    ┌─────────────────────┐  HTTP   ┌─────────────┐
│ meeting_bar.py   │───VBAN────▶│ transcriber.py      │──POST──▶│meetingnotesd│
│ + vban_send.py   │   :6980   │ + whisper-cli       │  :9876  │             │
└──────────────────┘            └─────────────────────┘         └─────────────┘
```

## Critical Rules

1. **NEVER edit files directly on pilot.** Pilot is deployed from this repo.
   Edit `transcriber/server/transcriber.py` here, commit, then `make deploy`.
2. **Always use `uv run`**, never `python3` or `pip`. All scripts use PEP 723
   inline script metadata for dependencies.
3. **The `server/` directory is the deployment unit.** Only files in `server/`
   get rsynced to pilot. Everything else (meeting_bar.py, vban/, setup/) runs
   on the laptop or is used for provisioning.

## File Map

| File | Runs on | Purpose |
|------|---------|---------|
| `server/transcriber.py` | pilot | FastAPI server: VBAN capture → WAV → whisper → webhook |
| `meeting_bar.py` | laptop | macOS menu bar app, auto meeting detection + recording |
| `meeting.py` | laptop | CLI for manual start/stop/status/devices |
| `vban/vban_send.py` | laptop | VBAN audio sender with optional dual-input mixing |
| `vban/vban_recv.py` | — | **Obsolete.** VBAN capture is now built into transcriber.py |
| `mic_active.swift` | laptop | CoreAudio helper: detects physical mic activity |
| `mic_active` | laptop | Compiled binary of above (git-ignored, `make build`) |
| `Makefile` | laptop | Build, deploy, provision, status, logs |
| `com.transcriber.plist` | pilot | launchd service definition |
| `setup/*.sh` | pilot (via ssh) | Provisioning scripts (homebrew, deps, whisper, service) |
| `SETUP.md` | — | Human setup guide with architecture diagram |

## Development & Deploy Workflow

### Changing the transcription server

```bash
cd transcriber

# 1. Edit the server code
$EDITOR server/transcriber.py

# 2. Commit
git add -A && git commit -m "description"

# 3. Deploy to pilot (rsyncs server/ + restarts launchd service)
make deploy

# 4. Verify
make status   # curl /status on pilot
make logs     # tail -f the service log
```

`make deploy` does:
- `rsync -avz --delete --exclude='recordings/' server/ edd@pilot:~/transcriber/`
- `launchctl bootout` + `launchctl bootstrap` to restart `com.transcriber`

## Changing the menu bar app

`meeting_bar.py` runs locally on the laptop as a **launchd agent**. After
editing, restart via:

```bash
cd transcriber
make meeting-bar-restart
make meeting-bar-logs       # verify healthy startup
```

To install for the first time:
```bash
make meeting-bar-install
```

**IMPORTANT**: Do NOT run `meeting_bar.py` via `nohup` — the closed file
descriptors cause `vban_send.py` subprocesses to crash with
`OSError: Bad file descriptor`. Always use the launchd agent.

### Changing mic_active

```bash
make build    # compiles mic_active.swift → mic_active binary
```

### Full provisioning (fresh pilot setup)

```bash
make provision   # runs setup/01-04 scripts remotely via SSH
make model       # downloads whisper model to pilot
make deploy      # deploys server code
```

## Network & Ports

| Port | Protocol | From → To | Purpose |
|------|----------|-----------|---------|
| 6980 | UDP | laptop → pilot | VBAN audio stream |
| 8000 | HTTP | laptop → pilot | Transcriber API (start/stop/status) |
| 9876 | HTTP | pilot → nuctu | Webhook POST of transcript |

Hosts `pilot` and `nuctu` are expected to be resolvable (Tailscale, /etc/hosts,
or mDNS).

## Environment Variables

### transcriber.py (pilot)

| Variable | Default | Notes |
|----------|---------|-------|
| `WHISPER_CLI` | `~/whisper.cpp/build/bin/whisper-cli` | |
| `WHISPER_MODEL` | `~/whisper.cpp/models/ggml-small.en-tdrz.bin` | Tinydiarize-enabled English model |
| `RECORDINGS_DIR` | `~/transcriber/recordings` | WAV + txt files |
| `WEBHOOK_URL` | `http://nuctu:9876/webhook` | Set in launchd plist |
| `VBAN_PORT` | `6980` | |
| `TRANSCRIBER_HOST` | `0.0.0.0` | |
| `TRANSCRIBER_PORT` | `8000` | |
| `WHISPER_THREADS` | unset | Optional `whisper-cli -t` thread cap; local launchd sets `2` |
| `WHISPER_NICE` | unset | Optional `nice -n` priority; local launchd sets `20` |
| `WHISPER_TASKPOLICY_BACKGROUND` | `false` | Run whisper through `taskpolicy -b`; local launchd sets `true` |
| `TRANSCRIBE_WHILE_RECORDING` | `true` | If `false`, queued transcriptions wait while a new recording is active |
| `TRANSCRIPTION_IDLE_DELAY_SECONDS` | `0` | Quiet window before Whisper starts; local launchd sets `30` |
| `LOCAL_SPEAKER_LABEL` | `Edd` | Label emitted for the local mic channel |
| `LOCAL_SPEAKER_CHANNEL` | `2` | 1-based channel index for the local mic in stereo captures |
| `LOCAL_SPEAKER_MIN_DBFS` | `-40` | Minimum mic level required before labeling a segment |
| `LOCAL_SPEAKER_DOMINANCE_DB` | `4.5` | Required dB lead over other channels to label the local speaker |

### meeting_bar.py (laptop)

| Variable | Default | Notes |
|----------|---------|-------|
| `TRANSCRIBER_TARGET_HOST` | `pilot` | Default host for both HTTP API and VBAN target; set `127.0.0.1` for local mode |
| `TRANSCRIBER_URL` | `http://$TRANSCRIBER_TARGET_HOST:8000` | Overrides HTTP API target when set |
| `PILOT_HOST` | `$TRANSCRIBER_TARGET_HOST` | Legacy override for VBAN target |
| `VBAN_PORT` | `6980` | |
| `MEETING_POLL_INTERVAL` | `5` | Seconds between detection checks |
| `AUDIOMXD_END_QUERY_TTL_SECONDS` | `15` | Seconds to cache expensive Teams/Edge audiomxd `log show` queries for end detection only |
| `MEETING_CALENDAR_ORG` | `~/gtd/outlook.org` | Org-mode calendar for title lookup |

## Whisper Configuration

- **Model**: `small.en-tdrz` (`ggml-small.en-tdrz.bin`) for English-only
  transcription with tinydiarize speaker-turn markers.
- **Flags**: `-m <model> -f <wav> -l en --print-progress --tinydiarize`.
  Timestamps are enabled (no `--no-timestamps` flag).
- **Metal GPU**: whisper.cpp is built with Metal acceleration on the M1.
- **Tinydiarize (`-tdrz`)**: Required for downstream speaker-turn markers.
- **Local speaker labeling**: In dual-input mode, the laptop now sends remote
  audio on the left channel and the local mic on the right channel. The
  transcriber annotates high-confidence mic-dominant segments as
  `[speaker:Edd]` before handing the transcript to the summarizer.

## Meeting Detection — Key Constraints

### Zoom
Simple: check for `CptHost` subprocess via `pgrep -xq CptHost`.

### Teams (complex — read carefully before changing)

Teams 2.x (new Electron) exposes no reliable window titles and
`AVCaptureDevice` cannot see its mic usage. Detection uses two tiers:

- **Start detection**: native Teams process tree running (`MSTeams` or Teams
  2.x WebView/helper processes) AND recent `audiomxd` recording evidence.
  Do not require an exact `MSTeams` binary; current Teams 2.x can expose only
  `com.microsoft.teams2.*` and `Microsoft Teams WebView` helpers.

- **End detection**: Cannot use mic state because our own VBAN sender keeps the
  mic active during recording. Instead queries macOS `audiomxd` system log for
  Teams audio session events.

### Teams PWA (Edge browser)

When Teams runs as a PWA in Edge, native process detection (`pgrep MSTeams`)
doesn't work. Instead, both start and end detection use `audiomxd` system logs
directly — the same technique as native Teams end detection:

- `audiomxd` logs `isRecording: true/false` for Edge helper processes
  (`com.microsoft.edgemac.helper`) when a call starts/stops.
- The detection function queries the last 120s of logs for
  `"Microsoft Edge"` + `"isRecording"`.
- This also works for Chrome if the PWA is ever moved there (change the app
  name string to `"Google Chrome"`).

The `detect_meeting()` function returns `"EdgeTeams"` for this case (distinct
from `"Teams"` for the native app).

If you change detection logic, test both start and end transitions for Zoom,
Teams, and EdgeTeams.

## Threading Model (meeting_bar.py)

The app uses `rumps` (Cocoa NSStatusBar wrapper). Key rules:

1. **Main thread** = Cocoa run loop. All UI updates must happen here.
2. **Poll thread** runs `_poll_loop()` for meeting detection every N seconds.
3. **Recording threads** handle start/stop I/O (VBAN sender, API calls).
4. **Background threads MUST NOT touch Cocoa objects.** Use
   `callAfter(fn, *args)` from `PyObjCTools.AppHelper` to dispatch to the
   main thread. Never use `rumps.Timer.set_callback()` from a background
   thread.
5. State transitions (idle/recording) are guarded by `self._recording_lock`.

## Audio Routing

The VBAN sender (`vban_send.py`) supports two modes:

- **Single device**: Captures from one audio input (e.g., ZoomAudioDevice).
- **Dual-input mixing**: Captures from a primary device (e.g., BlackHole 2ch
  for remote participants) AND a microphone (for local voice), mixes them in
  software. Use `--mic` flag.

Device preference order in meeting_bar.py: BlackHole 2ch → ZoomAudioDevice →
Microsoft Teams.

BlackHole requires a reboot after installation to register as a system audio
device.

## Calendar Title Lookup

When a meeting is detected (or manually started), `meeting_bar.py` optionally
looks up the meeting title from an org-mode calendar file (`~/gtd/outlook.org`
by default, configurable via `MEETING_CALENDAR_ORG`).

Matching rules:
- Only considers today's entries with timestamps
- Finds the entry whose start time is closest to now
- Nothing starts more than 5 minutes early (future entries beyond 5 min skipped)
- If more than 25 minutes past a meeting's start, assumes spontaneous meeting
  (no calendar match)
- Falls back to generic "App Meeting YYYY-MM-DD HH:MM" title if no match

The matched title is shown in the menu bar status, used as the recording title
sent to the transcriber, and logged.

## Makefile Reference

| Target | Where | What |
|--------|-------|------|
| `build` | local | Compile `mic_active.swift` → `mic_active` |
| `meeting-bar-install` | local | Install `meeting_bar.py` as launchd agent |
| `meeting-bar-restart` | local | Restart `meeting_bar.py` launchd agent |
| `meeting-bar-logs` | local | `tail -f` the meeting-bar log |
| `meeting-bar-status` | local | Show meeting-bar service status |
| `deploy` | remote | rsync `server/` to pilot + restart service |
| `check` | remote | Full health report (OS, brew, whisper, model, service) |
| `status` | remote | `curl /status` on pilot |
| `logs` | remote | `tail -f` the transcriber log |
| `ssh` | remote | Open shell on pilot |
| `provision` | remote | Run all 4 setup scripts |
| `model` | remote | Download/update whisper model |
| `test` | remote | Round-trip test |
| `clean-vban` | remote | Remove obsolete vban-receiver service |

## Common Pitfalls

- **Running meeting_bar.py via nohup**: Causes `vban_send.py` subprocesses to
  crash with `OSError: Bad file descriptor` because nohup closes stdout/stderr.
  Always use the launchd agent (`make meeting-bar-install`).
- **Editing pilot directly**: `make deploy` does `--delete`, so direct edits
  on pilot will be overwritten. Always edit in this repo.
- **VBAN sender keeps mic alive**: This is why Teams end-detection uses
  `audiomxd` logs instead of mic state. Don't "fix" this by checking mic state
  for end detection.
- **recordings/ directory**: `make deploy` excludes it (`--exclude='recordings/'`).
  Never add recordings to git.
- **Python 3.14 + rumps**: The combination works as of rumps 0.4.0. Earlier
  versions have pyobjc incompatibilities.
- **Cocoa threading**: SEGV on exit or random crashes usually mean a background
  thread touched a Cocoa object. See Threading Model above.
