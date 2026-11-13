"""Installation must authenticate only package preparation and fail before startup."""

import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("offline", [False, True])
def test_runtime_preparation_uses_cfs_metadata_and_persistent_python(tmp_path, offline):
    tools = tmp_path / "tools"
    tools.mkdir()
    log = tmp_path / "commands"
    for tool in ("uv", "gh"):
        command = tools / tool
        command.write_text(
            '#!/bin/bash\n'
            'printf "%s|%s|%s\\n" "$(basename "$0")" "$*" "$UV_CONFIG_FILE" >> "$COMMAND_LOG"\n'
        )
        command.chmod(0o755)
    runtime = tmp_path / "runtime"
    (runtime / "bin").mkdir(parents=True)
    python = runtime / "bin/python"
    python.write_text("#!/bin/bash\nexit 0\n")
    python.chmod(0o755)
    config = tmp_path / "uv-cfs.toml"
    script = tmp_path / "service.py"
    env = {
        **os.environ,
        "PATH": f"{tools}:/usr/bin:/bin",
        "COMMAND_LOG": str(log),
        "UV_OFFLINE": str(offline).lower(),
    }
    installer = Path(__file__).parents[1] / "transcriber/server/prepare-runtime.sh"
    subprocess.run(
        ["bash", str(installer), str(runtime), str(config), str(script)],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    commands = log.read_text().splitlines()
    expected = (
        "uv|pip install --offline"
        if offline else "gh|cfs -- uv pip install"
    )
    assert commands[0] == f"{expected} --python {python} --requirements {script}|{config}"
    assert commands[1] == f"uv|pip check --python {python}|{config}"


def test_runtime_preparation_propagates_install_failure(tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    runtime = tmp_path / "runtime"
    (runtime / "bin").mkdir(parents=True)
    python = runtime / "bin/python"
    python.touch()
    python.chmod(0o755)
    gh = tools / "gh"
    gh.write_text("#!/bin/bash\nexit 17\n")
    gh.chmod(0o755)
    installer = Path(__file__).parents[1] / "transcriber/server/prepare-runtime.sh"
    result = subprocess.run(
        ["bash", str(installer), str(runtime), str(tmp_path / "config"), "service.py"],
        env={**os.environ, "PATH": f"{tools}:/usr/bin:/bin", "UV_OFFLINE": "false"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 17
    assert "Runtime ready" not in result.stdout
