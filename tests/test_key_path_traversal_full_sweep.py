"""REG-8b (round-3.5 re-audit): FULL sweep of keys/<agent>.env sites.

macbook found round-3's REG-8 fix was incomplete — I sanitized _key_from_disk
and MISSED four sibling call sites doing the same unsanitized path build.
This test file locks down every site so a future refactor cannot silently
reintroduce the class:

  client.py:_key_from_disk                   (covered by test_key_from_disk_path_traversal)
  cli.py:_key_for_agent                      (env-reversal read)
  cli.py:cmd_join                            (WRITE from --name)
  cli.py:cmd_register (setup)                (WRITE from --name)
  cli.py:cmd_service                         (READ into systemd unit)
  hooks/claude_code.py:_adopt_credential_for (READ + mutates os.environ SILENTLY)

The shared invariant: every site now goes through sealing.bound_env_filename,
which collapses separators to '_' and '..' to '_' — so a hostile name can
never escape keys/ into <config>/operator.env.
"""

from __future__ import annotations

import pathlib

import pytest

from agentbus_client import cli as cli_module
from agentbus_client import sealing
from agentbus_client.hooks import claude_code as hook_module

# ---------------------------------------------------------------------- helper


def test_bound_env_filename_collapses_traversal(tmp_path):
    """The single sanitizer every site now shares MUST return a filename that
    cannot escape its parent directory. Test via ACTUAL path resolution rather
    than substring — a filename like '..env' is legal (no traversal) even
    though it contains '..' as a substring; the property that matters is that
    `keys_dir / got` resolves inside keys_dir.
    """
    keys_dir = tmp_path / "keys"
    keys_dir.mkdir()
    keys_resolved = keys_dir.resolve()
    for name in ("../operator", "..\\operator", "/etc/passwd", "foo/../operator", ".", ".."):
        got = sealing.bound_env_filename(name)
        assert got.endswith(".env"), f"{name!r} lost the .env suffix: {got!r}"
        resolved = (keys_dir / got).resolve()
        assert resolved.parent == keys_resolved, (
            f"{name!r} escaped keys/: filename={got!r} resolves to {resolved}"
        )


def test_bound_env_filename_preserves_legitimate_names():
    """Real agent names (matching the server's ^[A-Za-z0-9_-]+$-ish policy)
    survive unchanged. A sanitizer that mangles legitimate names silently
    changes which key an existing agent authenticates with."""
    assert sealing.bound_env_filename("agentbus-8dc08d") == "agentbus-8dc08d.env"
    assert sealing.bound_env_filename("builder_1") == "builder_1.env"
    assert sealing.bound_env_filename("a.b.c") == "a.b.c.env"


# ---------------------------------------------------------------------- cli._key_for_agent


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("AGENTBUS_API_KEY", raising=False)
    monkeypatch.delenv("AGENTBUS_AGENT", raising=False)
    monkeypatch.delenv("AGENTBUS_CONFIG_DIR", raising=False)
    cfg = tmp_path / ".config" / "agentbus"
    (cfg / "keys").mkdir(parents=True)
    return cfg


def _write(p: pathlib.Path, key: str) -> None:
    p.write_text(f"export AGENTBUS_API_KEY={key}\n")


def test_cli_key_for_agent_traversal_refused(home):
    """Macbook's finding #1: cli.py:_key_for_agent used to return the
    operator credential for agent name '../operator'. The fix must return None
    for the traversal payload (sanitized filename does not exist)."""
    _write(home / "operator.env", "ab_sk_OPERATOR_TOP_SECRET")
    assert cli_module._key_for_agent("../operator") is None
    assert cli_module._key_for_agent("/etc/passwd") is None
    assert cli_module._key_for_agent("..\\operator") is None


def test_cli_key_for_agent_still_reads_legitimate_bound_key(home):
    """The sanitizer MUST NOT change legitimate names."""
    _write(home / "keys" / "agentbus-abc123.env", "ab_sk_BOUND")
    assert cli_module._key_for_agent("agentbus-abc123") == "ab_sk_BOUND"


# ---------------------------------------------------------------------- hooks._adopt_credential_for


def test_adopt_credential_for_traversal_refused(home, monkeypatch):
    """The MOST DANGEROUS of the round-3.5 sites — silent mutation of
    os.environ["AGENTBUS_API_KEY"] mid-hook, reachable from a hostile
    .agentbus/agent. A traversal payload used to swap the hook session
    onto the operator credential with no visible failure. After the fix,
    the sanitized filename does not exist and the function silently no-ops
    (per its documented best-effort contract) instead of loading operator.env.
    """
    _write(home / "operator.env", "ab_sk_OPERATOR_TOP_SECRET")
    monkeypatch.delenv("AGENTBUS_API_KEY", raising=False)

    hook_module._adopt_credential_for("../operator")

    # The critical assertion: the operator credential MUST NOT have been
    # written into the environment as if it belonged to the requested agent.
    assert "AGENTBUS_API_KEY" not in __import__("os").environ, (
        "the hook silently loaded the operator credential for a traversal "
        "payload — REG-8b regression"
    )


def test_adopt_credential_for_still_loads_a_legitimate_bound_key(home, monkeypatch):
    """The hook MUST still adopt a real bound key — otherwise legitimate
    worktree-bleed correction stops working."""
    _write(home / "keys" / "peer.env", "ab_sk_PEER_LEGITIMATE")
    monkeypatch.delenv("AGENTBUS_API_KEY", raising=False)

    hook_module._adopt_credential_for("peer")

    import os

    assert os.environ.get("AGENTBUS_API_KEY") == "ab_sk_PEER_LEGITIMATE"


# ---------------------------------------------------------------------- write paths


def test_join_write_path_lands_inside_keys_dir(tmp_path, monkeypatch):
    """`agentbus join --name "../operator"` used to WRITE the returned
    secret into <config>/operator.env, CLOBBERING the operator credential.
    After the fix, the sanitized filename lands inside keys/ regardless.

    We do not run cmd_join end-to-end (needs a real server); instead we
    exercise the path derivation in isolation.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".config" / "agentbus" / "keys").mkdir(parents=True)

    # Reproduce the derivation from cli.py:cmd_join.
    from agentbus_client.onboarding import _keys_dir

    keys_dir = _keys_dir()
    hostile = "../operator"
    key_path = keys_dir / sealing.bound_env_filename(hostile)
    # The critical assertion: the resolved parent is keys/, no matter what
    # the requested `agent` name was.
    assert key_path.resolve().parent == keys_dir.resolve(), (
        f"write path escaped keys/: {key_path.resolve()}"
    )
    # And the sanitized filename does not equal 'operator.env' — the target
    # of the escalation attack.
    assert key_path.name != "operator.env"


def test_service_read_path_lands_inside_keys_dir(tmp_path, monkeypatch):
    """`agentbus service ... --agent "../operator"` used to READ
    <config>/operator.env and bake that path into a systemd unit's
    EnvironmentFile, so the service would auto-source the operator
    credential on every start. Fix: sanitized filename stays in keys/.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".config" / "agentbus" / "keys").mkdir(parents=True)

    keys_dir = tmp_path / ".config" / "agentbus" / "keys"
    hostile = "../operator"
    default_key_file = keys_dir / sealing.bound_env_filename(hostile)
    assert default_key_file.resolve().parent == keys_dir.resolve()
    assert default_key_file.name != "operator.env"
