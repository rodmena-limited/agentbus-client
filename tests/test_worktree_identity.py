"""Identity must resolve per CHECKOUT, from anywhere inside it (issuedb #98).

`.agentbus/agent` was always read from `git rev-parse --show-toplevel`, while
`.claude/settings.local.json` was read relative to the CWD — in the Python
client, in the Claude Code hooks, and in the shell monitor. The two disagreed
everywhere except the top of a checkout:

    .claude/settings.local.json at the root, resolved AT the root  -> found
    .claude/settings.local.json at the root, resolved in `sdk/`    -> None

`settings.local.json`'s `env` block is the only identity declaration Claude Code
injects into hook and monitor processes, so it is the one a Claude-wired project
actually relies on. And since the kill switch (#95), finding no identity means
AgentBus goes SILENTLY INERT rather than erroring — correct for a project that
never opted in, and indistinguishable from it for a wired project we simply
failed to resolve. The failure mode was silence, so nothing reported it.

BOTH DIRECTIONS THROUGHOUT. Resolving identity from the repo root is only
correct if an UNWIRED checkout still resolves nothing — otherwise the fix would
have quietly disabled the kill switch, which is a worse bug than the one it
closes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from agentbus_client import identity as identity_mod
from agentbus_client import onboarding as ob

MONITOR = REPO / "marketplace" / "plugins" / "agentbus" / "scripts" / "agentbus-monitor.sh"


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real git checkout with a subdirectory, and no identity yet."""
    monkeypatch.delenv("AGENTBUS_AGENT", raising=False)
    root = tmp_path / "checkout"
    (root / "sdk").mkdir(parents=True)
    _git("init", "-q", ".", cwd=root)
    return root


def _resolve_from(directory: Path) -> str | None:
    cwd = os.getcwd()
    try:
        os.chdir(directory)
        return ob._resolve_agent_name([])
    finally:
        os.chdir(cwd)


def _wire_claude(root: Path, name: str) -> None:
    (root / ".claude").mkdir(exist_ok=True)
    (root / ".claude" / "settings.local.json").write_text(
        json.dumps({"env": {"AGENTBUS_AGENT": name}})
    )


def _wire_agentbus(root: Path, name: str) -> None:
    (root / ".agentbus").mkdir(exist_ok=True)
    (root / ".agentbus" / "agent").write_text(f"{name}\n")


# --------------------------------------------------------------------------
# The bug: identity declared the Claude Code way, resolved from a subdirectory.
# --------------------------------------------------------------------------


def test_claude_settings_identity_resolves_from_a_subdirectory(repo: Path) -> None:
    _wire_claude(repo, "wired-agent")
    assert _resolve_from(repo) == "wired-agent"
    assert _resolve_from(repo / "sdk") == "wired-agent", (
        "a command run below the checkout root must resolve the same identity"
    )


def test_agentbus_file_identity_resolves_from_a_subdirectory(repo: Path) -> None:
    """The control: this path already worked, and must keep working."""
    _wire_agentbus(repo, "declared-agent")
    assert _resolve_from(repo) == "declared-agent"
    assert _resolve_from(repo / "sdk") == "declared-agent"


def test_env_var_still_outranks_both_files(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#90: a stored default must never override an explicit env instruction."""
    _wire_claude(repo, "from-file")
    _wire_agentbus(repo, "from-worktree-file")
    monkeypatch.setenv("AGENTBUS_AGENT", "from-env")
    assert _resolve_from(repo / "sdk") == "from-env"


def test_agentbus_file_outranks_claude_settings(repo: Path) -> None:
    _wire_claude(repo, "from-claude")
    _wire_agentbus(repo, "from-worktree-file")
    assert _resolve_from(repo / "sdk") == "from-worktree-file"


# --------------------------------------------------------------------------
# THE KILL SWITCH (#95) must survive the change. These are the assertions that
# stop "resolve from the repo root" from becoming "attach any directory to
# something".
# --------------------------------------------------------------------------


def test_an_unwired_checkout_resolves_nothing(repo: Path) -> None:
    assert _resolve_from(repo) is None
    assert _resolve_from(repo / "sdk") is None


def test_a_non_git_directory_resolves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AGENTBUS_AGENT", raising=False)
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    assert _resolve_from(plain) is None


def test_a_nested_repo_does_not_inherit_the_outer_identity(repo: Path) -> None:
    """`--show-toplevel` returns the INNERMOST checkout, and that is what we want.

    An unwired repo vendored inside a wired one must stay unwired; inheriting
    would attach a project nobody opted in to another project's inbox.
    """
    _wire_agentbus(repo, "outer-agent")
    nested = repo / "vendor" / "inner"
    nested.mkdir(parents=True)
    _git("init", "-q", ".", cwd=nested)

    assert _resolve_from(repo / "sdk") == "outer-agent"
    assert _resolve_from(nested) is None


# --------------------------------------------------------------------------
# Worktrees are distinct agents on one repo.
# --------------------------------------------------------------------------


def test_two_worktrees_are_two_agents_on_one_repo(repo: Path, tmp_path: Path) -> None:
    """Same repo_fingerprint (one repo), different session_key (two checkouts)."""
    _git("config", "user.email", "t@example.com", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    _git("remote", "add", "origin", "git@github.com:example/demo.git", cwd=repo)
    (repo / "f.txt").write_text("x")
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "init", cwd=repo)

    worktree = tmp_path / "wt"
    _git("worktree", "add", "-q", "--detach", str(worktree), "HEAD", cwd=repo)

    main_desc = identity_mod.describe(str(repo))
    wt_desc = identity_mod.describe(str(worktree))

    assert main_desc["repo_fingerprint"] == wt_desc["repo_fingerprint"], "one repo"
    assert main_desc["session_key"] != wt_desc["session_key"], "two checkouts, two agents"

    # And each worktree's own declaration wins inside it.
    _wire_agentbus(repo, "main-agent")
    _wire_agentbus(worktree, "worktree-agent")
    assert _resolve_from(repo) == "main-agent"
    assert _resolve_from(worktree) == "worktree-agent"


def test_project_claude_dir_points_at_the_checkout_root(repo: Path) -> None:
    cwd = os.getcwd()
    try:
        os.chdir(repo / "sdk")
        assert ob._project_claude_dir() == repo / ".claude"
    finally:
        os.chdir(cwd)


# --------------------------------------------------------------------------
# The shell monitor must resolve identically — #90 was these two disagreeing.
#
# Behavioural, not a grep: with an identity but no credential the monitor names
# the agent and exits 0; with no identity it exits 0 SILENTLY. That difference
# is the resolution result, observable without a network or a real watcher.
# --------------------------------------------------------------------------


def _run_monitor(cwd: Path, home: Path) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "PATH": os.environ.get("PATH", ""),
    }
    env.pop("AGENTBUS_AGENT", None)
    env.pop("AGENTBUS_API_KEY", None)
    return subprocess.run(
        ["sh", str(MONITOR)],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.mark.skipif(not MONITOR.is_file(), reason="monitor script not present")
def test_monitor_resolves_claude_settings_from_a_subdirectory(repo: Path, tmp_path: Path) -> None:
    """Resolution probe UPDATED for #118: the no-credential branch is now
    SILENT (it used to print 'no credential for <agent>', which was the third
    hostage variant). So the observable proof of resolution is the ENGAGING
    path: give the resolved identity a key file, and the monitor announces the
    agent it is watching before the (dead) stream fails."""
    _wire_claude(repo, "monitor-agent")
    home = tmp_path / "home"
    (home / ".config" / "agentbus" / "keys").mkdir(parents=True)
    (home / ".config" / "agentbus" / "keys" / "monitor-agent.env").write_text(
        "AGENTBUS_API_KEY=ab_sk_dead_dead\n"
    )

    proc = subprocess.Popen(
        ["sh", str(MONITOR)],
        cwd=repo / "sdk",
        env={
            **{
                k: v
                for k, v in os.environ.items()
                if k not in ("AGENTBUS_AGENT", "AGENTBUS_API_KEY")
            },
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        out = ""
        for _ in range(40):  # up to ~20s; the dead key fails fast
            time.sleep(0.5)
            if proc.poll() is not None:
                out = proc.stdout.read() if proc.stdout else ""
                break
    finally:
        if proc.poll() is None:
            proc.terminate()
            out = proc.communicate(timeout=10)[0]
    assert "monitor-agent" in out, (
        f"the monitor must resolve the checkout's identity from a subdirectory; got output={out!r}"
    )


@pytest.mark.skipif(not MONITOR.is_file(), reason="monitor script not present")
def test_monitor_parks_forever_in_an_unwired_checkout(repo: Path, tmp_path: Path) -> None:
    """The kill switch, at the monitor: OFF means SILENT, not "ended" (#108).

    It used to `exit 0`, and exit 0 is not silence — the harness reports every
    ended monitor as a task notification INTO the session. That notification
    wakes Claude with no user interaction, and a woken Claude "helpfully"
    checks a nonexistent inbox and then tries to REGISTER an agent in a project
    the operator deliberately left unwired (observed in container-registry,
    2026-08-13). So an unwired monitor must PARK: no output, no exit while the
    session lives, and die cleanly on the SessionEnd reap's TERM.
    """
    home = tmp_path / "home"
    home.mkdir()
    env = {
        **os.environ,
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "PATH": os.environ.get("PATH", ""),
    }
    env.pop("AGENTBUS_AGENT", None)
    env.pop("AGENTBUS_API_KEY", None)

    proc = subprocess.Popen(
        ["sh", str(MONITOR)],
        cwd=repo / "sdk",
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        time.sleep(3)
        assert proc.poll() is None, (
            "an unwired monitor must PARK, not exit — an exit becomes a harness "
            "notification that wakes Claude in an opted-out project"
        )
    finally:
        proc.terminate()
        out, _err = proc.communicate(timeout=10)
    assert out.strip() == "", f"an unwired checkout must say nothing: {out!r}"
