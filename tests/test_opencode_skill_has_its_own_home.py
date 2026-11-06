"""#93 item 17, #94 item 10: the opencode skill has its own path, flavour and help text.

The two harnesses used to share `~/.claude/skills/agentbus/SKILL.md`. `setup opencode` wrote
the opencode flavour there and `skill_state` compared it with the Claude flavour, so a fresh
install read as stale. The double below serves different bytes per flavour, the way the server
does, so a comparison against the wrong one goes red.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from agentbus_client import onboarding
from agentbus_client.onboarding import _claude_setup, _opencode_setup, _skill

CLAUDE_BYTES = ("claude flavour " * 60).encode()
OPENCODE_BYTES = ("opencode flavour " * 60).encode()


class _Resp:
    def __init__(self, status: int, content: bytes) -> None:
        self.status_code = status
        self.content = content
        self.text = content.decode()


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    import httpx

    fetched: list[str] = []

    def get(url: str, **_k: Any) -> _Resp:
        fetched.append(url)
        if url.endswith("/skills/claude-code.md"):
            return _Resp(200, CLAUDE_BYTES)
        if url.endswith("/skills/opencode.md"):
            return _Resp(200, OPENCODE_BYTES)
        return _Resp(404, b"unknown harness")

    monkeypatch.setattr(httpx, "get", get)
    return {"fetched": fetched}


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.chdir(project)
    return {
        "project": project,
        "claude": home / ".claude" / "skills" / "agentbus" / "SKILL.md",
        "opencode": home / ".config" / "opencode" / "skills" / "agentbus" / "SKILL.md",
    }


def _wire_opencode(project: Path) -> None:
    (project / "opencode.json").write_text(json.dumps({"mcp": {"agentbus": {"type": "remote"}}}))


def _wire_claude(project: Path) -> None:
    (project / ".claude").mkdir()
    (project / ".claude" / "settings.local.json").write_text(
        json.dumps({"env": {"AGENTBUS_AGENT": "alpha"}})
    )


def _place(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)


def test_setup_installs_the_opencode_flavour_at_opencodes_own_path(
    host: dict[str, Path], served: Any
) -> None:
    report: list[str] = []
    _opencode_setup._install_opencode_skill("https://x", report)
    assert host["opencode"].read_bytes() == OPENCODE_BYTES
    assert not host["claude"].exists()
    assert report == [f"skill: {host['opencode']} (installed — MANAGED artifact)"]


def test_setup_leaves_an_installed_claude_skill_alone(host: dict[str, Path], served: Any) -> None:
    _place(host["claude"], CLAUDE_BYTES)
    _opencode_setup._install_opencode_skill("https://x", [])
    assert host["claude"].read_bytes() == CLAUDE_BYTES


def test_a_fresh_opencode_install_reads_as_current(host: dict[str, Path], served: Any) -> None:
    _wire_opencode(host["project"])
    _opencode_setup._install_opencode_skill("https://x", [])
    state, detail = onboarding.skill_state("https://x")
    assert state == "current"
    assert str(host["opencode"]) in detail
    assert served["fetched"][-1] == "https://x/skills/opencode.md"


def test_an_outdated_opencode_skill_reads_as_stale_and_the_refresh_clears_it(
    host: dict[str, Path], served: Any
) -> None:
    _wire_opencode(host["project"])
    _place(host["opencode"], b"old")
    state, detail = onboarding.skill_state("https://x")
    assert state == "stale"
    assert "agentbus refresh-skill" in detail

    state, _detail = onboarding.refresh_skill("https://x")
    assert state == "updated"
    assert host["opencode"].read_bytes() == OPENCODE_BYTES
    assert host["opencode"].with_suffix(".md.bak").read_bytes() == b"old"
    assert not host["claude"].exists()
    assert onboarding.skill_state("https://x")[0] == "current"


def test_a_missing_opencode_skill_names_the_opencode_setup(
    host: dict[str, Path], served: Any
) -> None:
    _wire_opencode(host["project"])
    _place(host["claude"], CLAUDE_BYTES)
    state, detail = onboarding.skill_state("https://x")
    assert state == "missing"
    assert str(host["opencode"]) in detail
    assert "agentbus setup opencode" in detail


def test_the_claude_flavour_is_still_compared_in_a_claude_checkout(
    host: dict[str, Path], served: Any
) -> None:
    _wire_claude(host["project"])
    _place(host["claude"], CLAUDE_BYTES)
    state, detail = onboarding.skill_state("https://x")
    assert state == "current"
    assert detail == f"{len(CLAUDE_BYTES)} bytes, matches the served copy"
    assert served["fetched"] == ["https://x/skills/claude-code.md"]


def test_an_unwired_directory_keeps_the_claude_check(host: dict[str, Path], served: Any) -> None:
    _place(host["claude"], CLAUDE_BYTES)
    assert onboarding.skill_state("https://x")[0] == "current"
    assert served["fetched"] == ["https://x/skills/claude-code.md"]


def test_a_checkout_wired_for_both_keeps_the_claude_check(
    host: dict[str, Path], served: Any
) -> None:
    _wire_claude(host["project"])
    _wire_opencode(host["project"])
    _place(host["claude"], CLAUDE_BYTES)
    assert onboarding.skill_state("https://x")[0] == "current"
    assert served["fetched"] == ["https://x/skills/claude-code.md"]


def test_a_subdirectory_of_an_opencode_only_repo_compares_the_opencode_flavour(
    host: dict[str, Path], served: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _wire_opencode(host["project"])
    (host["project"] / "pkg").mkdir()
    monkeypatch.chdir(host["project"] / "pkg")
    monkeypatch.setattr(_skill, "_git_root_or_none", lambda: host["project"])
    _place(host["opencode"], OPENCODE_BYTES)
    assert onboarding.skill_state("https://x")[0] == "current"


@pytest.mark.parametrize(
    "body", [b"{not json", b"[]", b"null", b'{"mcp": null}', b'{"mcp": []}', b"\xff\xfe"]
)
def test_an_opencode_config_without_our_entry_is_not_read_as_opencode(
    host: dict[str, Path], served: Any, body: bytes
) -> None:
    (host["project"] / "opencode.json").write_bytes(body)
    _place(host["claude"], CLAUDE_BYTES)
    assert onboarding.skill_state("https://x")[0] == "current"
    assert served["fetched"] == ["https://x/skills/claude-code.md"]


def test_xdg_config_home_moves_the_opencode_path(
    host: dict[str, Path], served: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    _opencode_setup._install_opencode_skill("https://x", [])
    moved = tmp_path / "xdg" / "opencode" / "skills" / "agentbus" / "SKILL.md"
    assert moved.read_bytes() == OPENCODE_BYTES
    assert not host["opencode"].exists()


def test_a_refresh_in_a_claude_checkout_writes_the_claude_flavour(
    host: dict[str, Path], served: Any
) -> None:
    _place(host["opencode"], b"left alone")
    assert onboarding.refresh_skill("https://x")[0] == "installed"
    assert host["claude"].read_bytes() == CLAUDE_BYTES
    assert host["opencode"].read_bytes() == b"left alone"


def test_setup_claude_installs_the_claude_flavour_in_an_opencode_only_checkout(
    host: dict[str, Path], served: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`setup claude` names its harness. The checkout's wiring does not get a say."""
    _wire_opencode(host["project"])
    monkeypatch.setattr(_claude_setup, "_provision_project_agent", lambda a, r, h: "alpha")
    monkeypatch.setattr(_claude_setup, "_write_worktree_identity", lambda name, report: None)
    monkeypatch.setattr(_claude_setup, "_wire_wake", lambda report: True)
    monkeypatch.setattr(_claude_setup, "_install_rewaker", lambda report: None)
    monkeypatch.setattr(_claude_setup, "_wire_mcp", lambda name, base_url, report: None)
    monkeypatch.setattr(_claude_setup, "_warn_inherited_credential", lambda b, r: None)
    monkeypatch.setattr(_claude_setup, "AgentBus", lambda **k: None)
    _claude_setup._setup_claude(argparse.Namespace(base_url="https://x"))
    assert host["claude"].read_bytes() == CLAUDE_BYTES
    assert not host["opencode"].exists()


def _refresh_skill_help(home: Path) -> str:
    source = Path(onboarding.__file__).parents[2]
    done = subprocess.run(
        [sys.executable, "-m", "agentbus_client.cli", "refresh-skill", "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin", "PYTHONPATH": str(source)},
    )
    return " ".join(done.stdout.split())


def test_the_refresh_skill_help_names_the_home_of_each_flavour(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each path in the rendered help is the directory that flavour is written to."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    text = _refresh_skill_help(tmp_path)
    for harness in ("claude", "opencode"):
        home = _skill._flavour(harness).path.parent
        assert f"~/{home.relative_to(tmp_path)}/ for {harness}" in text


def test_the_refresh_skill_help_does_not_send_every_harness_to_setup_claude(
    tmp_path: Path,
) -> None:
    text = _refresh_skill_help(tmp_path)
    assert "`agentbus setup claude`" not in text
    assert "`agentbus setup` refuses" in text
