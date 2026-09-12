from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SRC = str(Path(__file__).resolve().parents[1] / "src")
UNREACHABLE = {"AGENTBUS_API_KEY": "ab_sk_fake", "AGENTBUS_BASE_URL": "http://127.0.0.1:9"}


def _cli(tmp_path: Path, *argv: str, **env: str) -> subprocess.CompletedProcess[str]:
    home = tmp_path / "home"
    work = home / "work"
    work.mkdir(parents=True, exist_ok=True)
    environment = {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": SRC,
        "AGENTBUS_CONFIG_DIR": str(home / ".config" / "agentbus"),
    }
    environment.update(env)
    return subprocess.run(
        [sys.executable, "-m", "agentbus_client.cli", *argv],
        capture_output=True,
        text=True,
        env=environment,
        cwd=str(work),
        timeout=60,
        stdin=subprocess.DEVNULL,
    )


@pytest.mark.parametrize(
    "argv,exit_code,mentions",
    [
        (["tag", "team:x"], 3, "cannot reach AgentBus"),
        (["undeliverable", "--limit", "soonish"], 2, "--limit must be a whole number"),
        (["sent", "--since", "soonish"], 2, "--since takes a duration"),
        (["join", "sometoken", "someone"], 3, "cannot reach AgentBus"),
        (["qr", "--agent", "\U0001f4a5"], 2, "agent names use only"),
        (["--agent", "../operator", "whoami"], 2, "agent names use only"),
    ],
)
def test_malformed_input_is_a_clean_error(tmp_path, argv, exit_code, mentions):
    result = _cli(tmp_path, *argv, **UNREACHABLE)
    assert "Traceback" not in result.stderr, result.stderr
    assert result.returncode == exit_code, result.stderr
    assert mentions in result.stderr


def test_a_malformed_env_agent_is_a_clean_error(tmp_path):
    result = _cli(tmp_path, "whoami", AGENTBUS_AGENT="\U0001f4a5", **UNREACHABLE)
    assert "Traceback" not in result.stderr, result.stderr
    assert result.returncode == 2
    assert "not an agent name" in result.stderr


def test_a_valid_agent_name_is_still_accepted(tmp_path):
    result = _cli(tmp_path, "--agent", "build.er_01-x", "whoami", **UNREACHABLE)
    assert "agent names use only" not in result.stderr
    assert result.returncode == 3
