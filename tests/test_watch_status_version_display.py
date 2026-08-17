"""watch-status must surface a stale watcher's client_version (0.9.27).

macbook-admin-bd8e86 caught this MISSED TWICE (thread
01M08ZWE0XCTPJG1R0ZBXP8K7P):

  msg 01M0916R4XW6K2NB248RYPR4DX — 0.9.25 claimed the feature, the
      helper looked at the wrong path (pidfile basename vs state file
      basename), returned None in the wild.
  msg 01M091QDY8KFYZSJPZGTA231ZG — 0.9.26 rewired it, but copied only
      ONE of doctor's two globs. The missing pattern,
      `monitor-<agent>-*.json`, is what the PLUGIN MONITOR names its
      state file — i.e. the production config for every Claude Code
      session. So the fix worked for ad-hoc watchers and silently
      returned None for exactly the watchers that run in production.

The fix now CALLS doctor's `_running_watcher_version` rather than
paraphrasing it — macbook's meta point: "wherever two commands answer
the same question, they should call ONE helper, not two similar ones."

These tests pin BOTH naming schemes plus the exact-state_key path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentbus_client import cli as cli_module


def _write_state(cfg_dir: Path, name: str, version: str) -> Path:
    cfg_dir.mkdir(parents=True, exist_ok=True)
    path = cfg_dir / name
    path.write_text(
        json.dumps(
            {
                "cursor": 42,
                "agent": "test-agent",
                "workspace": "test-ws",
                "client_version": version,
                "failures": 0,
                "last_failure_at": 0.0,
            }
        )
    )
    return path


def test_reads_plugin_monitor_state_file(tmp_path, monkeypatch):
    """THE BUG macbook found in 0.9.26. The plugin monitor names its
    state file `monitor-<agent>-<session-uuid>.json`. That is the
    PRODUCTION configuration; the previous glob missed it entirely."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg = tmp_path / ".config" / "agentbus"
    _write_state(
        cfg,
        "monitor-test-agent-416270d0-46f9-41ca-bdb3-a937ca359c77.json",
        "0.9.25",
    )

    got = cli_module._read_running_client_version("test-agent", None)
    assert got == "0.9.25", (
        "the plugin-monitor state file was not read — this is the exact miss "
        "macbook reported against 0.9.26"
    )


def test_reads_default_watch_state_file(tmp_path, monkeypatch):
    """The other naming scheme — used when no --state is passed."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg = tmp_path / ".config" / "agentbus"
    _write_state(cfg, "watch-test-ws-test-agent.json", "0.9.24")

    got = cli_module._read_running_client_version("test-agent", None)
    assert got == "0.9.24"


def test_prefers_exact_state_key_when_caller_provides_it(tmp_path, monkeypatch):
    """macbook's better suggestion: the caller already knows the state
    path, so use it. Exact beats a naming convention that can drift when
    someone invents a third prefix."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg = tmp_path / ".config" / "agentbus"
    # Two files with DIFFERENT versions — the state_key one must win.
    _write_state(cfg, "monitor-test-agent-aaa.json", "0.9.99-WRONG")
    _write_state(cfg, "my-explicit-state.json", "0.9.26-CORRECT")

    got = cli_module._read_running_client_version("test-agent", "my-explicit-state.json.pid")
    assert got == "0.9.26-CORRECT", "state_key was ignored; the glob fallback answered instead"


def test_newest_by_mtime_wins_across_both_schemes(tmp_path, monkeypatch):
    """Two watchers, two schemes — the most recently written state file
    is the live one."""
    import os
    import time

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg = tmp_path / ".config" / "agentbus"
    old = _write_state(cfg, "watch-test-ws-test-agent.json", "0.9.18-OLD")
    time.sleep(0.01)
    new = _write_state(cfg, "monitor-test-agent-session.json", "0.9.26-NEW")
    # Force distinct mtimes so the test isn't racing filesystem granularity.
    os.utime(old, (1_000_000, 1_000_000))
    os.utime(new, (2_000_000, 2_000_000))

    got = cli_module._read_running_client_version("test-agent", None)
    assert got == "0.9.26-NEW"


def test_returns_none_when_no_state_file(tmp_path, monkeypatch):
    """None is NOT a pass — the caller must render 'cannot confirm'
    rather than assume a match."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".config" / "agentbus").mkdir(parents=True, exist_ok=True)
    assert cli_module._read_running_client_version("test-agent", None) is None


def test_corrupt_state_file_falls_through_gracefully(tmp_path, monkeypatch):
    """A half-written state file must not crash watch-status."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg = tmp_path / ".config" / "agentbus"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "monitor-test-agent-x.json").write_text("{not valid json")

    # Must not raise.
    got = cli_module._read_running_client_version("test-agent", None)
    assert got is None


def test_shares_doctors_implementation_not_a_copy():
    """macbook's meta point, pinned as a test: the two commands that
    answer 'what version is the running watcher' must call ONE helper.
    Two copies is what produced two successive misses.

    Asserts the cli helper delegates to onboarding._running_watcher_version
    rather than reimplementing the glob."""
    import inspect

    src = inspect.getsource(cli_module._read_running_client_version)
    assert "_running_watcher_version" in src, (
        "watch-status no longer delegates to doctor's implementation — "
        "the duplicate-and-diverge pattern macbook flagged has returned"
    )
