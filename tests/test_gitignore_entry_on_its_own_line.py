"""#94 item 1: every .gitignore writer leaves the existing last line intact.

A file with no trailing newline used to gain the new entry on its last line, which broke both
patterns. Each case asks git, through `git check-ignore`, whether the patterns still work.
"""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

import pytest

from agentbus_client.onboarding import _claude_setup, _identity, _opencode_setup

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

CLAUDE = b".claude/settings.local.json\n.agentbus/\n"
OPENCODE = b"opencode.json\n"
WORKTREE = b".agentbus/\n"

WRITERS: list[tuple[Callable[[list[str]], None], bytes, str]] = [
    (_claude_setup._ignore_identity_files, CLAUDE, ".claude/settings.local.json"),
    (_opencode_setup._ignore_opencode_json, OPENCODE, "opencode.json"),
    (lambda report: _identity._write_worktree_identity("alpha", report), WORKTREE, ".agentbus/x"),
]
IDS = ["claude", "opencode", "worktree"]


def _ignored(repo: Path, rel: str) -> bool:
    argv = ["git", "check-ignore", "-q", rel]
    return subprocess.run(argv, cwd=repo, check=False, timeout=30).returncode == 0


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, timeout=30)
    monkeypatch.chdir(root)
    return root


def test_check_ignore_goes_red_on_the_two_patterns_run_together(repo: Path) -> None:
    (repo / ".gitignore").write_bytes(b"node_modulesopencode.json\n")
    assert _ignored(repo, "node_modules/x") is False
    assert _ignored(repo, "opencode.json") is False


@pytest.mark.parametrize(("writer", "added", "rel"), WRITERS, ids=IDS)
def test_a_last_line_with_no_newline_stays_and_the_entry_gets_its_own_line(
    repo: Path, writer: Callable[[list[str]], None], added: bytes, rel: str
) -> None:
    (repo / ".gitignore").write_bytes(b"dist\nnode_modules")
    writer([])
    assert (repo / ".gitignore").read_bytes() == b"dist\nnode_modules\n" + added
    assert _ignored(repo, "node_modules/x") is True
    assert _ignored(repo, rel) is True


@pytest.mark.parametrize(("writer", "added", "rel"), WRITERS, ids=IDS)
def test_a_file_ending_in_a_newline_gains_no_blank_line(
    repo: Path, writer: Callable[[list[str]], None], added: bytes, rel: str
) -> None:
    (repo / ".gitignore").write_bytes(b"node_modules\n")
    writer([])
    assert (repo / ".gitignore").read_bytes() == b"node_modules\n" + added
    assert _ignored(repo, rel) is True


@pytest.mark.parametrize(("writer", "added", "rel"), WRITERS, ids=IDS)
def test_a_missing_file_holds_only_the_entries(
    repo: Path, writer: Callable[[list[str]], None], added: bytes, rel: str
) -> None:
    writer([])
    assert (repo / ".gitignore").read_bytes() == added
    assert _ignored(repo, rel) is True


@pytest.mark.parametrize(("writer", "added", "rel"), WRITERS, ids=IDS)
def test_an_empty_file_gains_no_leading_blank_line(
    repo: Path, writer: Callable[[list[str]], None], added: bytes, rel: str
) -> None:
    (repo / ".gitignore").write_bytes(b"")
    writer([])
    assert (repo / ".gitignore").read_bytes() == added
    assert _ignored(repo, rel) is True


@pytest.mark.parametrize(("writer", "added", "rel"), WRITERS, ids=IDS)
def test_a_last_line_that_is_not_utf8_keeps_its_bytes(
    repo: Path, writer: Callable[[list[str]], None], added: bytes, rel: str
) -> None:
    (repo / ".gitignore").write_bytes(b"caf\xe9")
    writer([])
    assert (repo / ".gitignore").read_bytes() == b"caf\xe9\n" + added
    assert _ignored(repo, os.fsdecode(b"caf\xe9")) is True
    assert _ignored(repo, rel) is True


@pytest.mark.parametrize(("writer", "added", "rel"), WRITERS, ids=IDS)
def test_a_crlf_file_keeps_its_line_endings(
    repo: Path, writer: Callable[[list[str]], None], added: bytes, rel: str
) -> None:
    (repo / ".gitignore").write_bytes(b"node_modules\r\n")
    writer([])
    assert (repo / ".gitignore").read_bytes() == b"node_modules\r\n" + added
    assert _ignored(repo, "node_modules/x") is True
    assert _ignored(repo, rel) is True


@pytest.mark.parametrize(("writer", "added", "rel"), WRITERS, ids=IDS)
def test_a_second_run_adds_nothing(
    repo: Path, writer: Callable[[list[str]], None], added: bytes, rel: str
) -> None:
    (repo / ".gitignore").write_bytes(b"node_modules")
    writer([])
    report: list[str] = []
    writer(report)
    assert (repo / ".gitignore").read_bytes() == b"node_modules\n" + added
    assert not any(line.startswith("gitignore:") for line in report)
    assert _ignored(repo, rel) is True


def test_a_partly_present_claude_block_adds_only_the_missing_entry(repo: Path) -> None:
    (repo / ".gitignore").write_bytes(b".agentbus/")
    report: list[str] = []
    _claude_setup._ignore_identity_files(report)
    assert (repo / ".gitignore").read_bytes() == b".agentbus/\n.claude/settings.local.json\n"
    assert report == ["gitignore: added .claude/settings.local.json"]
