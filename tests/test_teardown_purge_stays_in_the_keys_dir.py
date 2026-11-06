"""#92: a checkout cannot name a file for `teardown --purge-key` to delete.

The agent name comes from `.agentbus/agent` or the project's settings, and a cloned
repo controls both. Every other key path goes through `bound_env_filename`.
"""

from __future__ import annotations

import argparse
import contextlib
import json
from typing import TYPE_CHECKING

import pytest

from agentbus_client import onboarding
from agentbus_client.onboarding import _credentials, _opencode_setup, _provision, _signin

if TYPE_CHECKING:
    from pathlib import Path


class _Host:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self.cfg = tmp_path / "cfg"
        self.keys = self.cfg / "keys"
        self.keys.mkdir(parents=True)
        self.project = tmp_path / "project"
        (self.project / ".agentbus").mkdir(parents=True)
        self.operator = self.cfg / "operator.env"
        self.operator.write_text("export AGENTBUS_API_KEY=ab_sk_OPERATOR\n")
        monkeypatch.setenv("AGENTBUS_CONFIG_DIR", str(self.cfg))
        monkeypatch.delenv("AGENTBUS_AGENT", raising=False)
        monkeypatch.chdir(self.project)
        monkeypatch.setattr(_opencode_setup, "_git_root_or_none", lambda: None)

    def declare(self, name: str) -> None:
        (self.project / ".agentbus" / "agent").write_text(f"{name}\n")

    def teardown(self, *, purge_key: bool = True) -> int:
        return onboarding.cmd_teardown(argparse.Namespace(purge_key=purge_key))


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Host:
    return _Host(monkeypatch, tmp_path)


@pytest.mark.parametrize(
    "name", ["../operator", "../../operator", "a/../../operator", "..", "sub/dir", "with space"]
)
def test_a_name_that_is_not_a_key_name_is_refused_and_nothing_is_deleted(
    host: _Host, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    host.declare(name)
    rc = host.teardown()
    assert rc == 1
    assert host.operator.exists()
    assert (host.project / ".agentbus" / "agent").exists()
    out = capsys.readouterr()
    assert "removed" not in out.out.lower().replace("nothing was removed", "")
    assert name in (out.out + out.err)


def test_a_traversal_name_in_the_project_settings_is_refused_too(host: _Host) -> None:
    settings = host.project / ".claude" / "settings.local.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"env": {"AGENTBUS_AGENT": "../operator"}}))
    assert host.teardown() == 1
    assert host.operator.exists()
    assert "AGENTBUS_AGENT" in settings.read_text()


def test_the_purge_never_touches_a_sibling_that_the_sanitiser_would_have_matched(
    host: _Host,
) -> None:
    sibling = host.keys / "a_b.env"
    sibling.write_text("export AGENTBUS_API_KEY=ab_sk_SIBLING\n")
    host.declare("a/b")
    assert host.teardown() == 1
    assert sibling.exists()


def test_a_real_agent_key_is_still_purged(host: _Host) -> None:
    live = host.keys / "real-agent.env"
    dead = host.keys / "real-agent.env.dead"
    other = host.keys / "other-agent.env"
    for path in (live, dead, other):
        path.write_text("export AGENTBUS_API_KEY=ab_sk_x\n")
    host.declare("real-agent")
    assert host.teardown() == 0
    assert not live.exists()
    assert not dead.exists()
    assert other.exists()
    assert host.operator.exists()


def test_without_the_purge_flag_a_bad_name_still_unwires_the_project(host: _Host) -> None:
    host.declare("../operator")
    assert host.teardown(purge_key=False) == 0
    assert not (host.project / ".agentbus").exists()
    assert host.operator.exists()


def test_signin_cannot_overwrite_a_file_outside_the_keys_dir(host: _Host) -> None:
    before = host.operator.read_text()
    _signin._store_single_bound_key("ab_sk_NEW", "../operator")
    assert host.operator.read_text() == before
    assert "ab_sk_NEW" in (host.keys / "__operator.env").read_text()


def test_setup_cannot_retire_a_file_outside_the_keys_dir(host: _Host) -> None:
    with contextlib.suppress(OSError):
        _provision._retire_key_file("../operator", ".env.dead")
    assert host.operator.exists()
    assert not (host.cfg / "operator.env.dead").exists()


def test_setup_retires_a_real_key_next_to_itself(host: _Host) -> None:
    live = host.keys / "real-agent.env"
    live.write_text("export AGENTBUS_API_KEY=ab_sk_x\n")
    _provision._retire_key_file("real-agent", ".env.dead")
    assert not live.exists()
    assert (host.keys / "real-agent.env.dead").exists()


def test_every_key_path_setup_reports_stays_in_the_keys_dir(host: _Host) -> None:
    assert _provision._key_file("../operator").parent == host.keys
    assert _provision._key_file("real-agent") == host.keys / "real-agent.env"


def test_the_refusal_explainer_cannot_probe_outside_the_keys_dir(host: _Host) -> None:
    assert _credentials.explain_refusal("../operator") is None
    (host.keys / "real-agent.env").write_text("x")
    assert _credentials.explain_refusal("real-agent") is not None
