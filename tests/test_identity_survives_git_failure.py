"""#42: identity must not depend on a `git` subprocess succeeding.

REPORTED as "send and reply disagree on the acting agent in a linked worktree",
same shell, same second. The real cause is worse than a disagreement between two
commands: identity resolution was NON-DETERMINISTIC.

`_repo_root()` shelled out to `git rev-parse` with timeout=5 and returned None on
ANY failure. `_worktree_identity_bleed` then returned None, which
`_resolve_env_agent` reads as "no bleed — trust the environment". So a git that
was missing, slow, or transiently failing made the INJECTED main-worktree
identity win, SILENTLY, with no banner.

Measured in a real linked worktree before the fix:
    git available     -> tracker-manager-0e2462  + banner
    PATH=/nonexistent -> tracker-fbe1b4          + NO banner

"Could not tell" was indistinguishable from "verified safe", and it resolved to
the WRONG SENDER. One message went out under another agent's name; the named
agent concluded a second session of itself was on the bus. Three agents, four
wrong conclusions, ~1 hour — and nobody could reproduce it, because it depended
on whether a subprocess happened to succeed.

THE CONTROL tracker-manager asked for is here too: a worktree where env and
declaration AGREE must still resolve correctly, or the fix passes vacuously in
the only environment most people have.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from agentbus_client.cli import _common
from agentbus_client.hooks import _identity


@pytest.fixture(autouse=True)
def _clear_caches():
    _identity._REPO_ROOT_CACHE.clear()
    yield
    _identity._REPO_ROOT_CACHE.clear()


def _worktree_pair(tmp_path):
    """A real main checkout plus a real LINKED worktree."""
    main = tmp_path / "main"
    main.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=main, check=False)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "i"], cwd=main, check=False)
    (main / ".agentbus").mkdir()
    (main / ".agentbus" / "agent").write_text("main-agent\n")
    linked = tmp_path / "linked"
    subprocess.run(["git", "worktree", "add", "-q", str(linked), "-b", "wt"], cwd=main, check=False)
    (linked / ".agentbus").mkdir(exist_ok=True)
    (linked / ".agentbus" / "agent").write_text("linked-agent\n")
    return main, linked


def _resolve(monkeypatch, cwd, env_agent, break_git=False):
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("AGENTBUS_AGENT", env_agent)
    if break_git:
        # Exactly the production failure: git unavailable / timing out.
        monkeypatch.setenv("PATH", "/nonexistent")
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("no git"))
        )
    _identity._REPO_ROOT_CACHE.clear()
    return _common._resolve_env_agent()


@pytest.mark.skipif(not os.environ.get("PATH"), reason="needs a shell")
def test_linked_worktree_resolves_correctly_with_git(tmp_path, monkeypatch):
    """Known-positive: the path that already worked must keep working."""
    _main, linked = _worktree_pair(tmp_path)
    assert _resolve(monkeypatch, linked, "main-agent") == "linked-agent"


def test_linked_worktree_resolves_the_same_when_git_is_unavailable(tmp_path, monkeypatch):
    """THE REGRESSION. Before the fix this returned 'main-agent', silently."""
    _main, linked = _worktree_pair(tmp_path)
    assert _resolve(monkeypatch, linked, "main-agent", break_git=True) == "linked-agent"


def test_the_two_agree_so_identity_is_deterministic(tmp_path, monkeypatch):
    """The property that actually matters: same inputs, same sender, always."""
    _main, linked = _worktree_pair(tmp_path)
    with_git = _resolve(monkeypatch, linked, "main-agent")
    without = _resolve(monkeypatch, linked, "main-agent", break_git=True)
    assert with_git == without == "linked-agent"


def test_agreeing_env_and_declaration_still_work(tmp_path, monkeypatch):
    """tracker-manager's control: the fix must not pass vacuously.

    A worktree where env and declaration AGREE is the only environment most
    people have; a test written only there passes against the broken code too.
    """
    _main, linked = _worktree_pair(tmp_path)
    assert _resolve(monkeypatch, linked, "linked-agent") == "linked-agent"
    assert _resolve(monkeypatch, linked, "linked-agent", break_git=True) == "linked-agent"


def test_a_deliberate_export_still_wins(tmp_path, monkeypatch):
    """#90 must stay intact: a name a PERSON exported is not an injection.

    Known-negative for the reversal — it must be able to NOT fire.
    """
    _main, linked = _worktree_pair(tmp_path)
    assert _resolve(monkeypatch, linked, "deliberate-name") == "deliberate-name"


@pytest.mark.parametrize("var", ["GIT_DIR", "GIT_WORK_TREE"])
def test_misdirected_git_cannot_change_the_sender(tmp_path, monkeypatch, var):
    """#44: git ANSWERS, but about another repository.

    tracker-manager found this within the hour of #42 shipping, and named the
    gap exactly: every test so far varied whether git WORKS; none varied whether
    git is TELLING THE TRUTH ABOUT THIS DIRECTORY.

    Under GIT_DIR/GIT_WORK_TREE the subprocess SUCCEEDS and reports a different
    repository, so #42's "git failed -> use disk" fallback is never entered and
    the resolver gets a confident wrong answer instead of no answer. Measured on
    0.9.57: tracker-fbe1b4, three times, with ZERO banner lines.

    Not contrived — git exports these itself inside hooks, and CI runners set
    them, so an agent invoked from a pre-commit hook is in this environment.
    """
    _main, linked = _worktree_pair(tmp_path)
    monkeypatch.setenv(var, str(tmp_path / "elsewhere"))
    assert _resolve(monkeypatch, linked, "main-agent") == "linked-agent"


def test_git_dir_pointing_at_a_REAL_other_repo_cannot_change_the_sender(tmp_path, monkeypatch):
    """The sharpest form of #44: git succeeds and answers about a VALID other repo.

    Distinct from the nonexistent-path case above, which git REJECTS — that one
    falls into #42's "git could not answer" branch and is caught by the older
    fix. Here git answers cleanly, with well-formed output, about the wrong
    repository. Nothing downstream can tell that apart from the truth except by
    not asking git in the first place.

    A mutation reverting the common-dir half of #44 passed against the
    nonexistent-path test and fails against this one, which is the whole reason
    it exists.
    """
    _main, linked = _worktree_pair(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=other, check=False)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "i"], cwd=other, check=False)
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    assert _resolve(monkeypatch, linked, "main-agent") == "linked-agent"


def test_misdirected_git_still_lets_a_deliberate_export_win(tmp_path, monkeypatch):
    """Known-negative under the same stimulus: #90 must survive the #44 fix too.

    Without this, making the filesystem primary could have been implemented as
    "always prefer the declaration", which would silently override an operator
    who typed an identity on purpose.
    """
    _main, linked = _worktree_pair(tmp_path)
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere"))
    assert _resolve(monkeypatch, linked, "deliberate-name") == "deliberate-name"


def test_common_dir_is_read_from_disk_for_a_linked_worktree(tmp_path):
    """The mechanism: `.git` is a FILE in a linked worktree, a DIRECTORY in main."""
    main, linked = _worktree_pair(tmp_path)
    assert (linked / ".git").is_file()
    assert _identity._common_dir_from_filesystem(linked) == (main / ".git").resolve()
    assert _identity._common_dir_from_filesystem(main) == (main / ".git").resolve()
