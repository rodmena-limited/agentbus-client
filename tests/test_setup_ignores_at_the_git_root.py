"""#93 item 18: setup's ignore entries land at the git root from any directory in the checkout.

Each case asks git itself, through `git check-ignore`, whether the file ends up ignored.
"""

from __future__ import annotations

import argparse
import subprocess
from typing import TYPE_CHECKING

import pytest

from agentbus_client.onboarding import _claude_setup, _identity, _opencode_setup

if TYPE_CHECKING:
    from pathlib import Path


def _git(cwd: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *argv], cwd=cwd, capture_output=True, text=True, timeout=30, check=False
    )


def _ignored(repo: Path, rel: str) -> bool:
    return _git(repo, "check-ignore", "-q", rel).returncode == 0


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "pkg" / "deep").mkdir(parents=True)
    assert _git(root, "init", "-q").returncode == 0
    return root


def test_check_ignore_can_say_no_before_setup_runs(repo: Path) -> None:
    assert _ignored(repo, ".claude/settings.local.json") is False
    assert _ignored(repo, "pkg/deep/opencode.json") is False


def test_claude_identity_is_ignored_when_setup_runs_in_a_subdirectory(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo / "pkg" / "deep")
    report: list[str] = []
    _claude_setup._write_project_identity("alpha", report)
    assert (repo / ".claude" / "settings.local.json").exists()
    assert _ignored(repo, ".claude/settings.local.json") is True
    assert not (repo / "pkg" / "deep" / ".gitignore").exists()
    assert any(line.startswith("gitignore: added") for line in report)


def test_claude_identity_is_ignored_when_setup_runs_at_the_root(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    _claude_setup._write_project_identity("alpha", [])
    assert _ignored(repo, ".claude/settings.local.json") is True


def test_claude_setup_does_not_repeat_an_entry_on_a_second_run(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo / "pkg")
    _claude_setup._write_project_identity("alpha", [])
    first = (repo / ".gitignore").read_text()
    report: list[str] = []
    _claude_setup._write_project_identity("alpha", report)
    assert (repo / ".gitignore").read_text() == first
    assert not any(line.startswith("gitignore:") for line in report)


def test_opencode_config_is_ignored_when_setup_runs_in_a_subdirectory(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo / "pkg" / "deep")
    report: list[str] = []
    _opencode_setup._ignore_opencode_json(report)
    assert _ignored(repo, "pkg/deep/opencode.json") is True
    assert not (repo / "pkg" / "deep" / ".gitignore").exists()
    assert report == ["gitignore: added opencode.json (holds the bound key)"]


def test_opencode_config_is_ignored_when_setup_runs_at_the_root(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    _opencode_setup._ignore_opencode_json([])
    assert _ignored(repo, "opencode.json") is True


def test_opencode_setup_keeps_the_config_in_the_working_directory_and_ignores_it(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo / "pkg")
    monkeypatch.setattr(_opencode_setup, "_provision_project_agent", lambda a, r, h: "alpha")
    monkeypatch.setattr(_opencode_setup, "_agent_key", lambda name: "ab_sk_alpha")
    monkeypatch.setattr(_opencode_setup, "doctor_credential_scope", lambda base_url=None: [])
    monkeypatch.setattr(_opencode_setup, "_install_opencode_skill", lambda base_url, report: None)
    rc = _opencode_setup._setup_opencode(argparse.Namespace(base_url="https://example.invalid"))
    assert rc == 0
    assert (repo / "pkg" / "opencode.json").exists()
    assert _ignored(repo, "pkg/opencode.json") is True


def test_a_linked_worktree_keeps_its_ignore_entries_at_its_own_root(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = ("-c", "user.name=t", "-c", "user.email=t@example.invalid")
    assert _git(repo, *identity, "commit", "--allow-empty", "-qm", "x").returncode == 0
    linked = tmp_path / "linked"
    assert _git(repo, "worktree", "add", "-q", str(linked)).returncode == 0
    (linked / "sub").mkdir()
    monkeypatch.chdir(linked / "sub")
    _claude_setup._write_project_identity("alpha", [])
    assert _ignored(linked, ".claude/settings.local.json") is True
    assert not (repo / ".gitignore").exists()


def test_outside_a_repo_no_gitignore_is_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    monkeypatch.setattr(_identity, "_git_root_or_none", lambda: None)
    monkeypatch.setattr(_claude_setup, "_git_root_or_none", lambda: None)
    monkeypatch.setattr(_opencode_setup, "_git_root_or_none", lambda: None)
    report: list[str] = []
    _claude_setup._write_project_identity("alpha", report)
    _opencode_setup._ignore_opencode_json(report)
    assert not (plain / ".gitignore").exists()


def test_a_gitignore_that_is_not_utf8_keeps_its_bytes_and_gains_the_entries(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    (repo / ".gitignore").write_bytes(b"# caf\xe9\nbuild/\n")
    _identity._write_worktree_identity("alpha", [])
    _claude_setup._write_project_identity("alpha", [])
    _opencode_setup._ignore_opencode_json([])
    written = (repo / ".gitignore").read_bytes()
    assert written.startswith(b"# caf\xe9\nbuild/\n")
    assert _ignored(repo, ".claude/settings.local.json") is True
    assert _ignored(repo, ".agentbus/agent") is True
    assert _ignored(repo, "opencode.json") is True


def test_a_gitignore_that_cannot_be_read_is_reported_and_setup_carries_on(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    (repo / ".gitignore").mkdir()
    report: list[str] = []
    _claude_setup._write_project_identity("alpha", report)
    _opencode_setup._ignore_opencode_json(report)
    notes = [line for line in report if line.startswith("gitignore:")]
    assert len(notes) == 2
    assert all("!!! NOT updated" in line for line in notes)
    assert "Add .claude/settings.local.json and .agentbus/" in notes[0]
    assert "opencode.json holds the bound key" in notes[1]
    assert (repo / ".claude" / "settings.local.json").exists()


def test_a_stray_git_directory_in_the_cwd_does_not_move_the_entry(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty `.git` is not a repository, so git's answer stands and the root gets the entry."""
    (repo / "pkg" / ".git").mkdir()
    monkeypatch.chdir(repo / "pkg")
    _opencode_setup._ignore_opencode_json([])
    assert not (repo / "pkg" / ".gitignore").exists()
    assert (repo / ".gitignore").read_text() == "opencode.json\n"
