"""#70: `setup claude` must leave an agent that a fresh session can be woken on.

The only thing that wakes a Claude Code session nobody has typed into is the
agentbus plugin's monitor, which Claude Code starts at session start. 0.9.99's
setup detected the plugin but never installed it: on a machine without it,
setup wrote passive hooks, printed "the monitor arms at session start", and
exited 0 — a deaf agent reported as wired. Measured by infra-manager-c13110 on
the apidays demo box; the README's own `pip install` path leads there.

The `claude` used here is a fake on PATH that behaves like the real CLI in each
mode (the real one was exercised against an isolated CLAUDE_CONFIG_DIR when this
was built) and logs every invocation, so setup runs its real subprocess path.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
from typing import TYPE_CHECKING, Any

import pytest

from agentbus_client.onboarding import _claude_setup, _doctor, _plugin

if TYPE_CHECKING:
    from pathlib import Path

FAKE_CLAUDE = r"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
log = Path(os.environ["FAKE_CLAUDE_LOG"])
with log.open("a") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\n")
mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
args = sys.argv[1:]
settings = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "settings.json"
if args[:3] == ["plugin", "marketplace", "add"]:
    sys.exit(0)
if args[:2] == ["plugin", "install"]:
    if mode == "fail":
        print(json.dumps({
            "command": "install",
            "outcome": "failed",
            "message": "Plugin not found in marketplace",
        }))
        sys.exit(1)
    if mode == "ok":
        settings.parent.mkdir(parents=True, exist_ok=True)
        data = json.loads(settings.read_text()) if settings.exists() else {}
        data.setdefault("enabledPlugins", {})["agentbus@rodmena"] = True
        settings.write_text(json.dumps(data))
    print(json.dumps({
        "command": "install",
        "outcome": "ok",
        "message": "Successfully installed plugin: agentbus@rodmena",
    }))
    sys.exit(0)
sys.exit(0)
"""


@pytest.fixture
def box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A machine with Claude Code but no agentbus plugin, and no network."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "claude"
    fake.write_text(FAKE_CLAUDE)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "claude-calls.log"
    log.touch()
    cc = tmp_path / "claude-config"
    cc.mkdir()
    project = tmp_path / "project"
    project.mkdir()

    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cc))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(project)

    def provision(args: Any, report: Any, harness: Any) -> str:
        report.append("agent: demo-agent")
        return "demo-agent"

    class _NoNetwork:
        def __init__(self, *a: Any, **k: Any) -> None:
            msg = "no network in this test"
            raise ConnectionError(msg)

    monkeypatch.setattr(_claude_setup, "_provision_project_agent", provision)
    monkeypatch.setattr(_claude_setup, "_write_worktree_identity", lambda name, report: None)
    monkeypatch.setattr(_claude_setup, "_project_claude_dir", lambda: project / ".claude")
    monkeypatch.setattr(_claude_setup, "_config_dir", lambda: tmp_path / "agentbus-config")
    monkeypatch.setattr(_claude_setup, "AgentBus", _NoNetwork)
    monkeypatch.setattr(_claude_setup, "refresh_skill", lambda base_url=None: ("current", "stub"))
    monkeypatch.setattr(_claude_setup, "doctor_credential_scope", lambda base_url=None: [])
    return {"settings": cc / "settings.json", "log": log}


def _setup(capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    rc = _claude_setup._setup_claude(argparse.Namespace(base_url="https://example.invalid"))
    return rc, capsys.readouterr().out


def _installs(log: Path) -> list[str]:
    return [ln for ln in log.read_text().splitlines() if ln.startswith("plugin install")]


def test_setup_installs_the_plugin_when_it_is_missing(
    box: Any, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ok")
    rc, out = _setup(capsys)
    assert rc == 0, out
    assert "agentbus@rodmena installed" in out
    assert "monitor arms at session start" in out
    assert _installs(box["log"]) == ["plugin install agentbus@rodmena --scope user --json"]


def test_setup_does_not_overwrite_the_plugin_it_just_installed(
    box: Any, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Setup rewrites settings.json for its hooks. Loaded BEFORE the install, its
    stale copy would be written back over the plugin's entry — the install would
    succeed and then be silently undone by setup itself."""
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ok")
    _setup(capsys)
    settings = json.loads(box["settings"].read_text())
    assert settings.get("enabledPlugins", {}).get("agentbus@rodmena") is True


def test_the_plugin_owns_the_wake_so_no_stop_hook_is_written(
    box: Any, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """SPECS/0022: two wake paths wake the session twice."""
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ok")
    _setup(capsys)
    settings = json.loads(box["settings"].read_text())
    assert "Stop" not in (settings.get("hooks") or {})


def test_an_installer_that_claims_success_is_not_believed(
    box: Any, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE EFFECT, NOT THE RETURN CODE. The fake reports ok and changes nothing."""
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "lie")
    rc, out = _setup(capsys)
    assert rc == 1
    assert "installer reported success but" in out
    assert "monitor arms at session start" not in out
    assert "NOT LISTENING YET" in out


def test_a_failed_install_is_loud_and_exits_non_zero(
    box: Any, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "fail")
    rc, out = _setup(capsys)
    assert rc == 1
    assert "NOT INSTALLED" in out
    assert "Plugin not found in marketplace" in out
    assert "claude plugin install agentbus@rodmena" in out
    assert "monitor arms at session start" not in out


def test_no_claude_on_path_is_loud_and_exits_non_zero(
    box: Any, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "/nonexistent")
    rc, out = _setup(capsys)
    assert rc == 1
    assert "not on PATH" in out
    assert "monitor arms at session start" not in out


def test_an_already_enabled_plugin_is_not_reinstalled(
    box: Any, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    box["settings"].write_text(json.dumps({"enabledPlugins": {"agentbus@rodmena": True}}))
    rc, out = _setup(capsys)
    assert rc == 0
    assert "already enabled" in out
    assert _installs(box["log"]) == []


def test_settings_path_follows_claude_config_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cc"))
    assert _plugin.claude_settings_path() == tmp_path / "cc" / "settings.json"
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert _plugin.claude_settings_path() == tmp_path / "home" / ".claude" / "settings.json"


def _doctor_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings_path: Path,
    settings: dict[str, Any],
) -> str:
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(settings_path.parent))
    monkeypatch.setattr(_doctor, "_resolve_agent_name", lambda explain=None: "demo-agent")
    monkeypatch.setattr(_doctor, "_agent_key", lambda name: None)
    monkeypatch.setattr("agentbus_client.onboarding._paths.agy_is_wired", lambda root: False)
    monkeypatch.setattr(_doctor, "_monitor_pids", lambda *a, **k: [])
    _doctor.doctor_wake(argparse.Namespace(base_url="https://example.invalid"))
    return capsys.readouterr().out


def test_doctor_fails_a_claude_setup_without_the_plugin(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    out = _doctor_output(monkeypatch, capsys, tmp_path / "cc" / "settings.json", {"hooks": {}})
    assert "FAIL: the agentbus plugin is not enabled" in out
    assert "nobody has typed into" in out


def test_doctor_does_not_raise_that_failure_when_the_plugin_is_enabled(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Known-negative: the new failure must be able to stay silent."""
    out = _doctor_output(
        monkeypatch,
        capsys,
        tmp_path / "cc" / "settings.json",
        {"enabledPlugins": {"agentbus@rodmena": True}},
    )
    assert "the agentbus plugin is not enabled" not in out
