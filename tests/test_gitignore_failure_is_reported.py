from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from agentbus_client.onboarding import _identity

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.skipif(os.geteuid() == 0, reason="root writes every file")


def _repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(_identity, "_git_root_or_none", lambda: tmp_path)
    return tmp_path


def test_a_read_only_gitignore_is_reported_in_one_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path, monkeypatch)
    gitignore = root / ".gitignore"
    gitignore.write_text("node_modules/\n")
    gitignore.chmod(0o400)
    report: list[str] = []
    _identity._write_worktree_identity("alpha", report)
    assert report == [
        (
            f"gitignore: NOT updated (Permission denied); add .agentbus/ to {gitignore} "
            "by hand so the identity file is never committed"
        ),
        f"worktree identity: {root / '.agentbus' / 'agent'} = alpha",
    ]
    assert gitignore.read_text() == "node_modules/\n"
    assert (root / ".agentbus" / "agent").read_text() == "alpha\n"


def test_a_writable_gitignore_still_gets_the_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path, monkeypatch)
    (root / ".gitignore").write_text("node_modules/\n")
    report: list[str] = []
    _identity._write_worktree_identity("alpha", report)
    assert report[0] == f"gitignore: added .agentbus/ ({root / '.gitignore'})"
    assert (root / ".gitignore").read_text().splitlines() == ["node_modules/", ".agentbus/"]
    assert not any("NOT updated" in line for line in report)
