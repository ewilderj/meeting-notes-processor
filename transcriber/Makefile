# Transcriber Appliance — local build + remote management via SSH
#
# Local targets: build, meeting-bar-*, local-transcriber-*
# Remote targets: provision/deploy/status/logs/check operate on "pilot" over SSH
# Usage: cd transcriber && make <target>

REMOTE_HOST := edd@pilot
REMOTE_DIR := ~/transcriber
WHISPER_DIR := ~/whisper.cpp
WHISPER_MODEL := small.en-tdrz
SERVICE_LABEL := com.transcriber
PLIST := $(SERVICE_LABEL).plist

.PHONY: check provision deploy logs status ssh test model build \
       meeting-bar-install meeting-bar-restart meeting-bar-local meeting-bar-pilot \
       meeting-bar-logs meeting-bar-status \
       local-transcriber-install local-transcriber-restart local-transcriber-stop \
       local-transcriber-status local-transcriber-logs runtime local-transcriber-runtime

# ---------------------------------------------------------------------------
# Local Build
# ---------------------------------------------------------------------------

build: mic_active  ## Build all local binaries

mic_active: mic_active.swift
	swiftc -O -o $@ $<
	@echo "Built mic_active (CoreAudio physical-mic detector)"

# ---------------------------------------------------------------------------
# Meeting Bar (runs locally on this laptop as a launchd agent)
# ---------------------------------------------------------------------------

MEETING_BAR_LABEL := com.meeting-bar
MEETING_BAR_PLIST := $(MEETING_BAR_LABEL).plist
LOCAL_TRANSCRIBER_LABEL := com.transcriber.local
LOCAL_TRANSCRIBER_PLIST := $(LOCAL_TRANSCRIBER_LABEL).plist

runtime:  ## Prepare menu bar, CLI, and VBAN dependencies through CFS
	bash server/prepare-runtime.sh "$(CURDIR)/.venv" "$(abspath ../service-configs/uv-cfs.toml)" meeting_bar.py meeting.py vban/vban_send.py vban/vban_recv.py

local-transcriber-runtime:  ## Prepare local server dependencies through CFS
	bash server/prepare-runtime.sh

meeting-bar-install: runtime  ## Install meeting_bar.py as a launchd agent
	@mkdir -p ~/Library/LaunchAgents
	@test -f ~/Library/LaunchAgents/$(MEETING_BAR_PLIST) || cp $(MEETING_BAR_PLIST) ~/Library/LaunchAgents/$(MEETING_BAR_PLIST)
	-launchctl bootout gui/$$(id -u) ~/Library/LaunchAgents/$(MEETING_BAR_PLIST) 2>/dev/null
	plutil -replace ProgramArguments -json '["$(CURDIR)/.venv/bin/python", "$(CURDIR)/meeting_bar.py"]' ~/Library/LaunchAgents/$(MEETING_BAR_PLIST)
	plutil -replace WorkingDirectory -string "$(CURDIR)" ~/Library/LaunchAgents/$(MEETING_BAR_PLIST)
	@plutil -remove EnvironmentVariables.UV_CONFIG_FILE ~/Library/LaunchAgents/$(MEETING_BAR_PLIST) 2>/dev/null || true
	launchctl bootstrap gui/$$(id -u) ~/Library/LaunchAgents/$(MEETING_BAR_PLIST)
	@sleep 2
	@launchctl list | grep -q $(MEETING_BAR_LABEL) && echo "meeting-bar installed and running" || echo "WARNING: service not loaded"

meeting-bar-restart:  ## Restart meeting_bar.py launchd agent
	@test -f ~/Library/LaunchAgents/$(MEETING_BAR_PLIST) || { echo "Run make meeting-bar-install first"; exit 1; }
	-launchctl bootout gui/$$(id -u) ~/Library/LaunchAgents/$(MEETING_BAR_PLIST) 2>/dev/null
	@sleep 1
	launchctl bootstrap gui/$$(id -u) ~/Library/LaunchAgents/$(MEETING_BAR_PLIST)
	@sleep 2
	@launchctl list | grep -q $(MEETING_BAR_LABEL) && echo "meeting-bar restarted" || echo "WARNING: service not loaded"

meeting-bar-local:  ## Configure meeting_bar.py to send recording audio to the local transcriber
	@test -f ~/Library/LaunchAgents/$(MEETING_BAR_PLIST) || { echo "Run make meeting-bar-install first"; exit 1; }
	-launchctl bootout gui/$$(id -u) ~/Library/LaunchAgents/$(MEETING_BAR_PLIST) 2>/dev/null
	@sleep 1
	@plutil -replace EnvironmentVariables.TRANSCRIBER_TARGET_HOST -string 127.0.0.1 ~/Library/LaunchAgents/$(MEETING_BAR_PLIST)
	launchctl bootstrap gui/$$(id -u) ~/Library/LaunchAgents/$(MEETING_BAR_PLIST)
	@sleep 2
	@launchctl list | grep -q $(MEETING_BAR_LABEL) && echo "meeting-bar restarted" || echo "WARNING: service not loaded"
	@echo "meeting-bar target: local transcriber (127.0.0.1)"

meeting-bar-pilot:  ## Configure meeting_bar.py to send recording audio to pilot
	@test -f ~/Library/LaunchAgents/$(MEETING_BAR_PLIST) || { echo "Run make meeting-bar-install first"; exit 1; }
	-launchctl bootout gui/$$(id -u) ~/Library/LaunchAgents/$(MEETING_BAR_PLIST) 2>/dev/null
	@sleep 1
	@plutil -replace EnvironmentVariables.TRANSCRIBER_TARGET_HOST -string pilot ~/Library/LaunchAgents/$(MEETING_BAR_PLIST)
	launchctl bootstrap gui/$$(id -u) ~/Library/LaunchAgents/$(MEETING_BAR_PLIST)
	@sleep 2
	@launchctl list | grep -q $(MEETING_BAR_LABEL) && echo "meeting-bar restarted" || echo "WARNING: service not loaded"
	@echo "meeting-bar target: pilot"

meeting-bar-logs:  ## Tail meeting_bar.py logs
	tail -f ~/Library/Logs/meeting-bar.log

meeting-bar-status:  ## Show meeting_bar.py service status
	@launchctl list | grep $(MEETING_BAR_LABEL) || echo "meeting-bar: not loaded"
	@echo "---"
	@tail -5 ~/Library/Logs/meeting-bar.log 2>/dev/null || echo "No log file yet"

local-transcriber-install: local-transcriber-runtime  ## Install the local transcriber launchd agent
	@mkdir -p ~/Library/LaunchAgents
	@test -f ~/Library/LaunchAgents/$(LOCAL_TRANSCRIBER_PLIST) || cp $(LOCAL_TRANSCRIBER_PLIST) ~/Library/LaunchAgents/$(LOCAL_TRANSCRIBER_PLIST)
	-launchctl bootout gui/$$(id -u) ~/Library/LaunchAgents/$(LOCAL_TRANSCRIBER_PLIST) 2>/dev/null
	@sleep 1
	plutil -replace ProgramArguments -json '["$(CURDIR)/server/.venv/bin/python", "$(CURDIR)/server/transcriber.py"]' ~/Library/LaunchAgents/$(LOCAL_TRANSCRIBER_PLIST)
	plutil -replace WorkingDirectory -string "$(CURDIR)/server" ~/Library/LaunchAgents/$(LOCAL_TRANSCRIBER_PLIST)
	@plutil -remove EnvironmentVariables.UV_CONFIG_FILE ~/Library/LaunchAgents/$(LOCAL_TRANSCRIBER_PLIST) 2>/dev/null || true
	launchctl bootstrap gui/$$(id -u) ~/Library/LaunchAgents/$(LOCAL_TRANSCRIBER_PLIST)
	@sleep 2
	@launchctl list | grep -q $(LOCAL_TRANSCRIBER_LABEL) && echo "local transcriber installed and running" || echo "WARNING: local transcriber not loaded"

local-transcriber-restart:  ## Restart the local transcriber launchd agent
	@test -f ~/Library/LaunchAgents/$(LOCAL_TRANSCRIBER_PLIST) || { echo "Run make local-transcriber-install first"; exit 1; }
	-launchctl bootout gui/$$(id -u) ~/Library/LaunchAgents/$(LOCAL_TRANSCRIBER_PLIST) 2>/dev/null
	@sleep 1
	launchctl bootstrap gui/$$(id -u) ~/Library/LaunchAgents/$(LOCAL_TRANSCRIBER_PLIST)
	@sleep 2
	@launchctl list | grep -q $(LOCAL_TRANSCRIBER_LABEL) && echo "local transcriber restarted" || echo "WARNING: local transcriber not loaded"

local-transcriber-stop:  ## Stop the local transcriber launchd agent
	-launchctl bootout gui/$$(id -u) ~/Library/LaunchAgents/$(LOCAL_TRANSCRIBER_PLIST) 2>/dev/null

local-transcriber-status:  ## Show local transcriber launchd and HTTP status
	@launchctl list | grep $(LOCAL_TRANSCRIBER_LABEL) || echo "local transcriber: not loaded"
	@echo "---"
	@curl -s http://127.0.0.1:8000/status 2>/dev/null || echo "local transcriber HTTP: unavailable"

local-transcriber-logs:  ## Tail local transcriber logs
	tail -f ~/Library/Logs/transcriber-local.log

# ---------------------------------------------------------------------------
# Connectivity & Status
# ---------------------------------------------------------------------------

check:
	@echo "=== Checking pilot connectivity ==="
	@ssh -o ConnectTimeout=5 $(REMOTE_HOST) '\
		eval "$$(/opt/homebrew/bin/brew shellenv 2>/dev/null)" 2>/dev/null; \
		echo "Hostname:  $$(hostname)"; \
		echo "Arch:      $$(uname -m)"; \
		echo "macOS:     $$(sw_vers -productVersion)"; \
		echo "CPU:       $$(sysctl -n machdep.cpu.brand_string)"; \
		echo "Memory:    $$(sysctl -n hw.memsize | awk "{print \$$1/1073741824 \" GB\"}")"; \
		echo "Disk free: $$(df -h / | tail -1 | awk "{print \$$4}")"; \
		echo "---"; \
		echo "Homebrew:  $$(brew --version 2>/dev/null | head -1 || echo "not installed")"; \
		echo "ffmpeg:    $$(ffmpeg -version 2>/dev/null | head -1 || echo "not installed (optional)")"; \
		echo "uv:        $$(uv --version 2>/dev/null || echo "not installed")"; \
		echo "whisper:   $$(test -x $(WHISPER_DIR)/build/bin/whisper-cli && echo "compiled" || echo "not built")"; \
		echo "model:     $$(test -f $(WHISPER_DIR)/models/ggml-$(WHISPER_MODEL).bin && echo "$(WHISPER_MODEL) downloaded" || echo "not downloaded")"; \
		echo "service:   $$(launchctl list 2>/dev/null | grep -q $(SERVICE_LABEL) && echo "loaded" || echo "not loaded")"; \
		echo "vban-recv: $$(launchctl list 2>/dev/null | grep -q com.vban-receiver && echo "LOADED (obsolete!)" || echo "not loaded (good)")"; \
		echo "---"; \
		echo "Transcriber dir: $$(test -d $(REMOTE_DIR) && echo "exists" || echo "not deployed")"; \
	'
	@echo "=== pilot reachable ==="

ssh:
	ssh $(REMOTE_HOST)

status:
	@ssh $(REMOTE_HOST) 'curl -s http://localhost:8000/status 2>/dev/null || echo "Service not running"'

logs:
	@ssh $(REMOTE_HOST) 'tail -f ~/Library/Logs/transcriber.log 2>/dev/null || echo "No log file found"'

# ---------------------------------------------------------------------------
# Provisioning (run setup scripts remotely, in order)
# ---------------------------------------------------------------------------

provision: provision-homebrew provision-deps provision-whisper provision-service
	@echo "=== Provisioning complete ==="

provision-homebrew:
	@echo "=== [1/4] Homebrew ==="
	@ssh $(REMOTE_HOST) 'bash -s' < setup/01-homebrew.sh

provision-deps:
	@echo "=== [2/4] Dependencies ==="
	@ssh $(REMOTE_HOST) 'bash -s' < setup/02-dependencies.sh

provision-whisper:
	@echo "=== [3/4] whisper.cpp + CoreML ==="
	@ssh $(REMOTE_HOST) 'bash -s' < setup/03-whisper.sh

provision-service:
	@echo "=== [4/4] launchd service ==="
	rsync -avz --delete --exclude='recordings/' --exclude='.venv/' server/ $(REMOTE_HOST):$(REMOTE_DIR)/
	@ssh $(REMOTE_HOST) 'cd $(REMOTE_DIR) && bash prepare-runtime.sh'
	@scp $(PLIST) $(REMOTE_HOST):~/Library/LaunchAgents/$(PLIST)
	@ssh $(REMOTE_HOST) 'bash -s' < setup/04-service.sh

# ---------------------------------------------------------------------------
# Deployment
# ---------------------------------------------------------------------------

deploy:
	@echo "=== Deploying server to pilot ==="
	rsync -avz --delete --exclude='recordings/' --exclude='.venv/' server/ $(REMOTE_HOST):$(REMOTE_DIR)/
	@ssh $(REMOTE_HOST) 'cd $(REMOTE_DIR) && bash prepare-runtime.sh'
	@echo "=== Restarting service ==="
	@ssh $(REMOTE_HOST) '\
		set -e; \
		cd $(REMOTE_DIR); \
		plutil -replace ProgramArguments -json "[\"$$PWD/.venv/bin/python\", \"$$PWD/transcriber.py\"]" ~/Library/LaunchAgents/$(PLIST); \
		launchctl bootout gui/$$(id -u) ~/Library/LaunchAgents/$(PLIST) 2>/dev/null || true; \
		sleep 1; \
		launchctl bootstrap gui/$$(id -u) ~/Library/LaunchAgents/$(PLIST); \
		sleep 2; \
		curl -s http://localhost:8000/status || echo "Warning: service may not have started"; \
	'
	@echo "=== Deploy complete ==="

# ---------------------------------------------------------------------------
# Model management
# ---------------------------------------------------------------------------

model:
	@echo "=== Downloading/updating whisper model ($(WHISPER_MODEL)) ==="
	@ssh $(REMOTE_HOST) 'cd $(WHISPER_DIR) && bash models/download-ggml-model.sh $(WHISPER_MODEL)'

# ---------------------------------------------------------------------------
# Testing
# ---------------------------------------------------------------------------

test:
	@echo "=== Running round-trip test ==="
	@ssh $(REMOTE_HOST) 'curl -s http://localhost:8000/status'

# ---------------------------------------------------------------------------
# Cleanup — remove obsolete vban-receiver service from pilot
# (VBAN capture is now built into the transcriber directly)
# ---------------------------------------------------------------------------

clean-vban:
	@echo "=== Removing obsolete VBAN receiver from pilot ==="
	@ssh $(REMOTE_HOST) '\
		launchctl bootout gui/$$(id -u) ~/Library/LaunchAgents/com.vban-receiver.plist 2>/dev/null && echo "Service unloaded" || echo "Service not loaded"; \
		rm -f ~/Library/LaunchAgents/com.vban-receiver.plist && echo "Plist removed"; \
		rm -f ~/transcriber/vban_recv.py && echo "vban_recv.py removed"; \
	'
	@echo "=== Cleanup complete ==="
