#!/bin/bash
set -euo pipefail

# With no arguments, prepare the independently deployed transcriber server.
HERE="$(cd "$(dirname "$0")" && pwd)"
ENV_DIR="${1:-$HERE/.venv}"
CONFIG="${2:-$HERE/uv-cfs.toml}"
if [ "$#" -ge 2 ]; then
    shift 2
elif [ "$#" -ne 0 ]; then
    echo "Usage: bash prepare-runtime.sh [ENV_DIR CONFIG SCRIPT ...]" >&2
    exit 1
fi
if [ "$#" -eq 0 ]; then
    set -- "$HERE/transcriber.py"
fi

export UV_CONFIG_FILE="$CONFIG"
if [ ! -x "$ENV_DIR/bin/python" ]; then
    uv venv --python '>=3.11' "$ENV_DIR"
fi

requirements=()
for script in "$@"; do
    requirements+=(--requirements "$script")
done

if [ "${UV_OFFLINE:-false}" = true ] || [ "${UV_OFFLINE:-0}" = 1 ]; then
    # Recovery from an expired login can use already-cached CFS packages only.
    uv pip install --offline --python "$ENV_DIR/bin/python" "${requirements[@]}"
else
    gh cfs -- uv pip install --python "$ENV_DIR/bin/python" "${requirements[@]}"
fi
uv pip check --python "$ENV_DIR/bin/python"
echo "Runtime ready: $ENV_DIR/bin/python (no package manager needed at startup)"
