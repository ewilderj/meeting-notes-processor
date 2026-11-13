#!/bin/bash
# 04-service.sh — Install and load the transcriber launchd service (idempotent)
set -euo pipefail

SERVICE_LABEL="com.transcriber"
PLIST_SRC="$HOME/Library/LaunchAgents/${SERVICE_LABEL}.plist"
LOG_FILE="$HOME/Library/Logs/transcriber.log"

echo "--- Setting up transcriber service ---"

# Ensure LaunchAgents directory exists
mkdir -p "$HOME/Library/LaunchAgents"

# Check plist was copied (done by Makefile before running this script)
if [ ! -f "$PLIST_SRC" ]; then
    echo "ERROR: plist not found at $PLIST_SRC"
    echo "Make sure to run 'make provision-service' which copies it first"
    exit 1
fi

# Ensure log directory exists
mkdir -p "$(dirname "$LOG_FILE")"
touch "$LOG_FILE"

# Ensure recordings directory exists
mkdir -p "$HOME/transcriber/recordings"

if [ ! -x "$HOME/transcriber/.venv/bin/python" ]; then
    echo "ERROR: prepare the runtime with bash ~/transcriber/prepare-runtime.sh first" >&2
    exit 1
fi
plutil -replace ProgramArguments -json "[\"$HOME/transcriber/.venv/bin/python\", \"$HOME/transcriber/transcriber.py\"]" "$PLIST_SRC"

# Unload old service if loaded
if launchctl list 2>/dev/null | grep -q "$SERVICE_LABEL"; then
    echo "Unloading existing service..."
    launchctl bootout "gui/$(id -u)" "$PLIST_SRC" 2>/dev/null || true
    sleep 1
fi

# Load service
echo "Loading service..."
launchctl bootstrap "gui/$(id -u)" "$PLIST_SRC"
sleep 2

# Verify
if launchctl list 2>/dev/null | grep -q "$SERVICE_LABEL"; then
    echo "Service loaded successfully"
    launchctl list "$SERVICE_LABEL" 2>/dev/null || true
else
    echo "WARNING: Service may not have loaded correctly"
fi

echo "--- Service setup complete ---"
echo "Logs: tail -f $LOG_FILE"
