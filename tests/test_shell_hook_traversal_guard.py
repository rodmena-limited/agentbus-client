"""REG-8d SHELL SIBLING (audit 0.9.40): the harness hook shell snippets must
not let a hostile AGENTBUS_AGENT source the OPERATOR key via traversal.

The Python-side `_agent_key` was sanitized through bound_env_filename (REG-8d),
but the shell snippets in onboarding.py interpolate `${AGENTBUS_AGENT}` into
`$HOME/.config/agentbus/keys/${AGENTBUS_AGENT}.env` with no guard. A hostile
`.claude/settings.local.json` or `.agentbus/agent` setting
`AGENTBUS_AGENT=../operator` would source `$HOME/.config/agentbus/operator.env`
— the OPERATOR key that can MINT a bound key for any agent — on EVERY session
start. Reproduced live before the fix.

These tests run the actual emitted shell snippets with a hostile agent name and
assert the operator key is NOT sourced, plus the known-positive that a legit
agent still sources its own key.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from agentbus_client import onboarding


def _run_snippet(snippet: str, home: Path, agent: str) -> str:
    """Run a shell snippet with a given HOME + AGENTBUS_AGENT, return stdout.

    Appends an echo that reveals the sourced key, and neutralises the
    STOP_REWAKE_SH `exec agentbus-hook monitor` (which would hang) by
    replacing it with an echo of the key."""
    # Neutralise the exec so the script terminates instead of running the monitor.
    snippet = snippet.replace("exec agentbus-hook monitor", "echo KEY=${AGENTBUS_API_KEY:-NONE}")
    # Append an echo to reveal what got sourced (the real snippets don't print it).
    snippet += "; echo KEY=${AGENTBUS_API_KEY:-NONE}"
    env = {
        "HOME": str(home),
        "AGENTBUS_AGENT": agent,
        "PATH": os.environ.get("PATH", ""),
    }
    proc = subprocess.run(
        ["sh", "-c", snippet],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    return proc.stdout


def _seed(home: Path) -> None:
    """operator.env at the traversal target + a legit bound key."""
    cfg = home / ".config" / "agentbus"
    (cfg / "keys").mkdir(parents=True, exist_ok=True)
    (cfg / "operator.env").write_text(
        "export AGENTBUS_API_KEY=ab_sk_OPERATOR_SECRET_SHOULD_NOT_BE_SOURCED\n"
    )
    (cfg / "keys" / "legit-agent.env").write_text("export AGENTBUS_API_KEY=ab_sk_LEGIT_BOUND_KEY\n")


def test_session_start_cmd_blocks_traversal(tmp_path):
    """The SessionStart hook snippet must NOT source operator.env when
    AGENTBUS_AGENT=../operator."""
    _seed(tmp_path)
    out = _run_snippet(onboarding._SESSION_START_CMD, tmp_path, "../operator")
    assert "OPERATOR_SECRET" not in out, (
        "the SessionStart hook sourced the OPERATOR key via ../operator traversal"
    )


def test_session_start_cmd_sources_legit_key(tmp_path):
    """KNOWN-POSITIVE: a legit agent name must still source its own key."""
    _seed(tmp_path)
    out = _run_snippet(onboarding._SESSION_START_CMD, tmp_path, "legit-agent")
    assert "KEY=ab_sk_LEGIT_BOUND_KEY" in out, "the legit agent's key was not sourced"


def test_pending_cmd_blocks_traversal(tmp_path):
    """_PENDING_CMD is derived from _SESSION_START_CMD — same guard must hold."""
    _seed(tmp_path)
    out = _run_snippet(onboarding._PENDING_CMD, tmp_path, "../operator")
    assert "OPERATOR_SECRET" not in out


def test_stop_rewake_sh_blocks_traversal(tmp_path):
    """The re-waker/monitor snippet must also block the traversal."""
    _seed(tmp_path)
    out = _run_snippet(onboarding.STOP_REWAKE_SH, tmp_path, "../operator")
    assert "OPERATOR_SECRET" not in out


def test_stop_rewake_sh_sources_legit_key(tmp_path):
    """KNOWN-POSITIVE for the re-waker snippet."""
    _seed(tmp_path)
    out = _run_snippet(onboarding.STOP_REWAKE_SH, tmp_path, "legit-agent")
    assert "KEY=ab_sk_LEGIT_BOUND_KEY" in out


def test_shell_metachar_is_rejected(tmp_path):
    """A backtick / $() in the agent name must not execute — the guard rejects
    any char outside [a-zA-Z0-9._-]."""
    _seed(tmp_path)
    marker = tmp_path / "PWNED"
    out = _run_snippet(onboarding._SESSION_START_CMD, tmp_path, f"`touch {marker}`")
    assert not marker.exists(), "shell metacharacter executed a command"
    assert "OPERATOR_SECRET" not in out
