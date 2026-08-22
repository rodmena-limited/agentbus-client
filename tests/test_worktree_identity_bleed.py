"""#129 — a linked git worktree acting as the MAIN worktree's agent.

Claude Code injects the env block from the MAIN worktree's
`.claude/settings.local.json` into a session opened in a LINKED worktree. Because
the environment outranks the files by design (#90), the worktree's own declaration
never gets a chance and the session silently acts as another LIVE agent.

NOT THEORETICAL, AND THE DAMAGE IS THE INVISIBLE KIND. A worktree session on this
machine, acting as its parent, read a message from the parent's inbox and marked it
seen. Verified rather than accepted: delivery 01KZZ32B4G1... was absent from the
parent's unread list having never been opened there, while the very next probe —
which that session deliberately did not open — was still unread. A prediction that
held in both directions is why the report was believed.

Compounding it, our own SessionStart remedy for a shared identity was "run it from a
separate checkout or GIT WORKTREE, which gets its own settings.local.json". The
worktree does get its own; it is just not the one that wins. Following our advice
reproduced the collision it was meant to cure.

REAL GIT REPOSITORIES HERE, NOT MOCKS. The defect lives in the relationship between
a worktree and its common dir, so a test that stubbed `git rev-parse` would be
asserting the fixture's shape rather than git's behaviour, and would pass just as
happily if the real plumbing changed underneath it.

#90 MUST SURVIVE. The environment is still the operator's word for the session. Only
the exact signature of harness-misinjection is reversed, and the deliberate-override
case is asserted as hard as the bug case.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from agentbus_client.hooks import claude_code

MAIN_AGENT = "agentbus-279ca7"
TREE_AGENT = "agentbus-frontend-5e9d03"


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _declare(root: Path, agent: str) -> None:
    (root / ".agentbus").mkdir(parents=True, exist_ok=True)
    (root / ".agentbus" / "agent").write_text(agent + "\n")
    (root / ".claude").mkdir(parents=True, exist_ok=True)
    (root / ".claude" / "settings.local.json").write_text(
        json.dumps({"env": {"AGENTBUS_AGENT": agent}})
    )


@pytest.fixture
def worktrees(tmp_path):
    """A real main checkout with a real linked worktree, each declaring itself."""
    main = tmp_path / "main"
    main.mkdir()
    _git("init", "-q", "-b", "main", cwd=main)
    _git("config", "user.email", "t@example.com", cwd=main)
    _git("config", "user.name", "t", cwd=main)
    (main / "README").write_text("x\n")
    _git("add", "README", cwd=main)
    _git("commit", "-qm", "init", cwd=main)

    tree = tmp_path / "tree"
    _git("worktree", "add", "-q", "-b", "frontend", str(tree), cwd=main)

    _declare(main, MAIN_AGENT)
    _declare(tree, TREE_AGENT)
    return main, tree


def test_the_fixture_really_is_a_linked_worktree(worktrees):
    """Known-positive for the fixture itself. If this is not a linked worktree,
    every assertion below is about something other than the bug."""
    main, tree = worktrees
    common = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=str(tree),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert Path(common).resolve() == (main / ".git").resolve()
    assert (tree / ".git").is_file(), "a linked worktree's .git is a file, not a dir"


def test_worktree_uses_its_own_identity_despite_the_injected_env(worktrees, monkeypatch, capsys):
    """The #129 fix: the main worktree's value must not win here."""
    _main, tree = worktrees
    monkeypatch.chdir(tree)
    monkeypatch.setattr(claude_code._identity, "_repo_root", lambda: tree)
    monkeypatch.setenv("AGENTBUS_AGENT", MAIN_AGENT)

    assert claude_code._resolve_agent() == TREE_AGENT
    capsys.readouterr()


def test_main_worktree_is_untouched(worktrees, monkeypatch):
    """The correction must not fire in the main checkout — there is no bleed."""
    main, _tree = worktrees
    monkeypatch.chdir(main)
    monkeypatch.setattr(claude_code._identity, "_repo_root", lambda: main)
    monkeypatch.setenv("AGENTBUS_AGENT", MAIN_AGENT)

    assert claude_code._resolve_agent() == MAIN_AGENT


def test_deliberate_operator_override_still_wins(worktrees, monkeypatch):
    """#90 SURVIVES. A value a person chose is not the main worktree's value, so
    it is not the misinjection signature and must be honoured."""
    _main, tree = worktrees
    monkeypatch.chdir(tree)
    monkeypatch.setattr(claude_code._identity, "_repo_root", lambda: tree)
    monkeypatch.setenv("AGENTBUS_AGENT", "some-other-agent-the-operator-chose")

    assert claude_code._resolve_agent() == "some-other-agent-the-operator-chose"


def test_no_correction_when_the_worktree_declares_nothing(worktrees, monkeypatch):
    """An unwired worktree has no identity of its own to prefer."""
    _main, tree = worktrees
    (tree / ".agentbus" / "agent").unlink()
    (tree / ".claude" / "settings.local.json").unlink()
    monkeypatch.chdir(tree)
    monkeypatch.setattr(claude_code._identity, "_repo_root", lambda: tree)
    monkeypatch.setenv("AGENTBUS_AGENT", MAIN_AGENT)

    assert claude_code._resolve_agent() == MAIN_AGENT


def test_no_correction_when_the_two_checkouts_agree(worktrees, monkeypatch):
    """Nothing to correct; must not churn the value."""
    _main, tree = worktrees
    _declare(tree, MAIN_AGENT)
    monkeypatch.chdir(tree)
    monkeypatch.setattr(claude_code._identity, "_repo_root", lambda: tree)
    monkeypatch.setenv("AGENTBUS_AGENT", MAIN_AGENT)

    assert claude_code._resolve_agent() == MAIN_AGENT


def test_unwired_worktree_with_no_env_stays_off(worktrees, monkeypatch):
    """The kill switch is not weakened by any of this."""
    _main, tree = worktrees
    (tree / ".agentbus" / "agent").unlink()
    (tree / ".claude" / "settings.local.json").unlink()
    monkeypatch.chdir(tree)
    monkeypatch.setattr(claude_code._identity, "_repo_root", lambda: tree)
    monkeypatch.delenv("AGENTBUS_AGENT", raising=False)

    assert claude_code._resolve_agent() is None


def test_helper_returns_none_outside_a_git_checkout(tmp_path, monkeypatch):
    """Ambiguity resolves to 'leave the environment alone', never to a guess."""
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    monkeypatch.setattr(claude_code._identity, "_repo_root", lambda: plain)

    assert claude_code._worktree_identity_bleed(MAIN_AGENT) is None


# ---------------------------------------------------------------------------
# #131 — reversing the identity without the credential left #129 half-delivered.
#
# hooks.json sources keys/${AGENTBUS_AGENT}.env BEFORE calling the hook, so the
# environment already holds the MAIN worktree's agent-BOUND key by the time the
# bleed is detected. The session then resolved the right agent and could not
# authenticate as it ("this key may act only as <main>"), trading a wrong-inbox
# read for no mail at all.
#
# Found on client 0.4.69 against plugin 0.6.23 by agentbus-frontend-5e9d03, with
# a known-positive first: the same invocation using the correct agent AND its own
# key returned real unread mail, so the failure was a real negative rather than a
# broken probe.
# ---------------------------------------------------------------------------


def _write_key(cfg: Path, agent: str, key: str) -> None:
    (cfg / "keys").mkdir(parents=True, exist_ok=True)
    (cfg / "keys" / f"{agent}.env").write_text(
        f"export AGENTBUS_API_KEY={key}\nexport AGENTBUS_AGENT={agent}\n"
    )


def test_credential_follows_the_reversed_identity(worktrees, tmp_path, monkeypatch):
    """The #131 fix: adopting the worktree's identity adopts its key too."""
    _main, tree = worktrees
    cfg = tmp_path / "cfg"
    _write_key(cfg, MAIN_AGENT, "ab_sk_MAIN")
    _write_key(cfg, TREE_AGENT, "ab_sk_TREE")
    monkeypatch.setenv("AGENTBUS_CONFIG_DIR", str(cfg))
    monkeypatch.chdir(tree)
    monkeypatch.setattr(claude_code._identity, "_repo_root", lambda: tree)
    # Exactly what hooks.json leaves behind: the MAIN worktree's agent AND its
    # agent-bound key, both already exported.
    monkeypatch.setenv("AGENTBUS_AGENT", MAIN_AGENT)
    monkeypatch.setenv("AGENTBUS_API_KEY", "ab_sk_MAIN")

    assert claude_code._resolve_agent() == TREE_AGENT
    assert os.environ["AGENTBUS_API_KEY"] == "ab_sk_TREE", (
        "identity was reversed but the credential was not: the session resolves "
        "the right agent and cannot authenticate as it"
    )


def test_credential_untouched_when_no_bleed(worktrees, tmp_path, monkeypatch):
    """KNOWN-POSITIVE FOR THE NEGATIVE. In the main worktree nothing is reversed,
    so the operator's key must survive — otherwise the test above would pass on a
    build that rewrites AGENTBUS_API_KEY unconditionally."""
    main, _tree = worktrees
    cfg = tmp_path / "cfg"
    _write_key(cfg, MAIN_AGENT, "ab_sk_MAIN")
    monkeypatch.setenv("AGENTBUS_CONFIG_DIR", str(cfg))
    monkeypatch.chdir(main)
    monkeypatch.setattr(claude_code._identity, "_repo_root", lambda: main)
    monkeypatch.setenv("AGENTBUS_AGENT", MAIN_AGENT)
    monkeypatch.setenv("AGENTBUS_API_KEY", "ab_sk_OPERATOR_CHOSE_THIS")

    assert claude_code._resolve_agent() == MAIN_AGENT
    assert os.environ["AGENTBUS_API_KEY"] == "ab_sk_OPERATOR_CHOSE_THIS"


def test_missing_key_file_leaves_the_environment_alone(worktrees, tmp_path, monkeypatch):
    """A worktree wired but never signed in is a real state, not a crash."""
    _main, tree = worktrees
    cfg = tmp_path / "cfg"
    _write_key(cfg, MAIN_AGENT, "ab_sk_MAIN")  # no key file for the worktree
    monkeypatch.setenv("AGENTBUS_CONFIG_DIR", str(cfg))
    monkeypatch.chdir(tree)
    monkeypatch.setattr(claude_code._identity, "_repo_root", lambda: tree)
    monkeypatch.setenv("AGENTBUS_AGENT", MAIN_AGENT)
    monkeypatch.setenv("AGENTBUS_API_KEY", "ab_sk_MAIN")

    assert claude_code._resolve_agent() == TREE_AGENT
    assert os.environ["AGENTBUS_API_KEY"] == "ab_sk_MAIN"


def test_adopting_a_key_does_not_reassert_the_wrong_identity(tmp_path, monkeypatch):
    """The key file also exports AGENTBUS_AGENT; writing that back would undo the
    correction that just happened."""
    cfg = tmp_path / "cfg"
    _write_key(cfg, TREE_AGENT, "ab_sk_TREE")
    monkeypatch.setenv("AGENTBUS_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("AGENTBUS_AGENT", MAIN_AGENT)

    claude_code._adopt_credential_for(TREE_AGENT)

    assert os.environ["AGENTBUS_API_KEY"] == "ab_sk_TREE"
    assert os.environ["AGENTBUS_AGENT"] == MAIN_AGENT, (
        "the key file's AGENTBUS_AGENT export was applied, re-asserting the "
        "identity the bleed correction had just replaced"
    )
