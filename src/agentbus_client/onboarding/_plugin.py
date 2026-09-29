"""The agentbus Claude Code plugin: where Claude Code keeps its settings, and installing it."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

MARKETPLACE_URL = "https://agentbus.rodmena.co.uk/plugin/marketplace.json"
MARKETPLACE_NAME = "rodmena"
PLUGIN_ID = "agentbus@rodmena"
CLI_TIMEOUT_SEC = 120

INSTALL_COMMANDS = (
    f"claude plugin marketplace add {MARKETPLACE_URL}",
    f"claude plugin install {PLUGIN_ID}",
)


def claude_settings_path() -> Path:
    """Claude Code's user settings file, honouring CLAUDE_CONFIG_DIR as Claude Code does."""
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    root = Path(base).expanduser() if base else Path.home() / ".claude"
    return root / "settings.json"


def _tail(proc: subprocess.CompletedProcess[str]) -> str:
    text = (proc.stderr or "").strip() or (proc.stdout or "").strip()
    return text.splitlines()[-1] if text else f"exit {proc.returncode}"


def _install_outcome(stdout: str) -> tuple[str | None, str]:
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                result = json.loads(line)
            except ValueError:
                continue
            return result.get("outcome"), str(result.get("message") or "")
    return None, ""


def install_wake_plugin() -> tuple[bool, str]:
    """Install and enable the agentbus plugin through Claude Code's own CLI.

    Returns (installer_reported_ok, detail). The caller verifies the effect by
    re-reading Claude Code's settings; this return value is not that proof.
    """
    claude = shutil.which("claude")
    if claude is None:
        return False, "Claude Code (`claude`) is not on PATH"

    def run(*argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [claude, *argv],
            capture_output=True,
            text=True,
            timeout=CLI_TIMEOUT_SEC,
            check=False,
        )

    try:
        added = run("plugin", "marketplace", "add", MARKETPLACE_URL)
        if added.returncode != 0:
            updated = run("plugin", "marketplace", "update", MARKETPLACE_NAME)
            if updated.returncode != 0:
                return False, f"could not add the {MARKETPLACE_NAME} marketplace: {_tail(added)}"
        installed = run("plugin", "install", PLUGIN_ID, "--scope", "user", "--json")
    except subprocess.TimeoutExpired as exc:
        return (
            False,
            f"`claude {' '.join(map(str, exc.cmd[1:4]))}` timed out after {CLI_TIMEOUT_SEC}s",
        )
    except OSError as exc:
        return False, f"could not run `claude`: {exc}"

    outcome, message = _install_outcome(installed.stdout)
    if installed.returncode != 0 or outcome != "ok":
        return False, message or f"plugin install failed: {_tail(installed)}"
    return True, message or f"{PLUGIN_ID} installed"
