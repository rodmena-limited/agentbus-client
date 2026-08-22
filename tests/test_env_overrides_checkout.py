"""#127 — $AGENTBUS_AGENT silently outranking a checkout's own wired identity.

The env var winning is deliberate (#90): it is how an operator forces an identity
for one invocation. Doing it INVISIBLY to a checkout already wired to a different
agent is not.

HOW IT PRESENTED. A git worktree was wired to `agentbus-frontend-5e9d03` in both
declaration sites, both gitignored, entirely correct. A session opened there with
the parent checkout's AGENTBUS_AGENT inherited resolved as `agentbus-279ca7`,
collided with the parent's live watcher, and reported "another session is already
registered as agentbus-279ca7". True — and its obvious remedy, "register this
checkout as a separate agent", was WRONG: it already had one, and a new
registration would have minted a third identity for the override to steal next.

Reproduced by hand before writing this: `agentbus whoami` in that directory
returns agentbus-279ca7 with the variable inherited and agentbus-frontend-5e9d03
under `env -u AGENTBUS_AGENT`. Same directory, same files, two identities.

BOTH DIRECTIONS. Warning whenever the variable is merely SET would fire on every
correctly-wired session (the wiring puts the same value in the environment), which
is noise that trains people to ignore it. Only a genuine disagreement counts, and
the agreement case is asserted as hard as the conflict case.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from agentbus_client.hooks import claude_code

PARENT = "agentbus-279ca7"
OWN = "agentbus-frontend-5e9d03"


def _wire(root: Path, agent: str, *, via_settings: bool = False) -> None:
    if via_settings:
        (root / ".claude").mkdir(parents=True, exist_ok=True)
        (root / ".claude" / "settings.local.json").write_text(
            json.dumps({"env": {"AGENTBUS_AGENT": agent}})
        )
    else:
        (root / ".agentbus").mkdir(parents=True, exist_ok=True)
        (root / ".agentbus" / "agent").write_text(agent + "\n")


def test_conflict_is_reported_with_both_names(tmp_path, capsys, monkeypatch):
    """The #127 fix: name what the checkout says, what won, and the remedy."""
    _wire(tmp_path, OWN)
    monkeypatch.setattr(claude_code._identity, "_repo_root", lambda: tmp_path)
    monkeypatch.setenv("AGENTBUS_AGENT", PARENT)

    claude_code._warn_if_env_overrides_this_checkout(PARENT)

    out = capsys.readouterr().out
    assert OWN in out, "the overridden identity is not named, so nobody can act on it"
    assert PARENT in out, "the winning identity is not named"
    assert "WINS" in out
    # The remedy must steer AWAY from registering, which was the wrong instinct
    # the real session actually had.
    assert "Do NOT register a new agent" in out


def test_no_warning_when_checkout_and_env_agree(tmp_path, capsys, monkeypatch):
    """The normal wired session. Firing here would be noise on every startup."""
    _wire(tmp_path, PARENT)
    monkeypatch.setattr(claude_code._identity, "_repo_root", lambda: tmp_path)
    monkeypatch.setenv("AGENTBUS_AGENT", PARENT)

    claude_code._warn_if_env_overrides_this_checkout(PARENT)

    assert capsys.readouterr().out == ""


def test_no_warning_in_an_unwired_checkout(tmp_path, capsys, monkeypatch):
    """Nothing declared here, so nothing was overridden."""
    monkeypatch.setattr(claude_code._identity, "_repo_root", lambda: tmp_path)
    monkeypatch.setenv("AGENTBUS_AGENT", PARENT)

    claude_code._warn_if_env_overrides_this_checkout(PARENT)

    assert capsys.readouterr().out == ""


def test_settings_local_json_is_also_honoured(tmp_path, capsys, monkeypatch):
    """The Claude-Code mirror is a declaration site too — .agentbus/agent is not
    the only file a checkout can be wired through."""
    _wire(tmp_path, OWN, via_settings=True)
    monkeypatch.setattr(claude_code._identity, "_repo_root", lambda: tmp_path)
    monkeypatch.setenv("AGENTBUS_AGENT", PARENT)

    claude_code._warn_if_env_overrides_this_checkout(PARENT)

    assert OWN in capsys.readouterr().out


def test_silent_when_the_env_var_did_not_win(tmp_path, capsys, monkeypatch):
    """Resolution did not come from the environment, so there is no override to
    report — guards against warning on a value that lost."""
    _wire(tmp_path, OWN)
    monkeypatch.setattr(claude_code._identity, "_repo_root", lambda: tmp_path)
    monkeypatch.setenv("AGENTBUS_AGENT", PARENT)

    # The resolved agent is the checkout's own: the env var was not what won.
    claude_code._warn_if_env_overrides_this_checkout(OWN)

    assert capsys.readouterr().out == ""


def test_silent_when_no_env_var_is_set(tmp_path, capsys, monkeypatch):
    _wire(tmp_path, OWN)
    monkeypatch.setattr(claude_code._identity, "_repo_root", lambda: tmp_path)
    monkeypatch.delenv("AGENTBUS_AGENT", raising=False)

    claude_code._warn_if_env_overrides_this_checkout(OWN)

    assert capsys.readouterr().out == ""
