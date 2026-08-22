"""#41: `agentbus identity` must answer the question it is named after.

It used to print only the INPUTS to derivation — device_id, workdir,
session_key — and never the resolved agent. So in a directory where two sources
declare DIFFERENT identities it reported neither, and the command an operator
reaches for when confused about identity could not tell them what was winning.

That is how #40's split identity survived 2026-08-17 -> 2026-08-22 across 195
silent gate failures. A peer eventually read the identity out of `whoami`'s 404
text, because the ERROR MESSAGE named it and the DIAGNOSTIC did not.

The losing source is the whole point: two sources that agree are invisible and
harmless, two that disagree are a split identity.
"""

from __future__ import annotations

import argparse
import io
import json
from contextlib import redirect_stdout

import pytest

from agentbus_client.cli import _register


@pytest.fixture(autouse=True)
def _no_env_identity(monkeypatch):
    monkeypatch.delenv("AGENTBUS_AGENT", raising=False)


def _declare(root, name):
    (root / ".agentbus").mkdir(parents=True, exist_ok=True)
    (root / ".agentbus" / "agent").write_text(f"{name}\n")


def _mirror(root, name):
    d = root / ".claude"
    d.mkdir(parents=True, exist_ok=True)
    (d / "settings.local.json").write_text(json.dumps({"env": {"AGENTBUS_AGENT": name}}))


def _agent_line(out: str) -> str:
    """ONLY the `agent:` line.

    Asserting against the whole output is vacuous here and two mutations proved
    it: the resolved name also appears inside the "declared by:" line, and the
    word "none" appears in `repo_remote: None`. Both made a broken build look
    green.
    """
    for line in out.splitlines():
        if line.startswith("agent:"):
            return line
    return ""


def _run(as_json=False):
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert _register.cmd_identity(argparse.Namespace(json=as_json, workdir=None)) == 0
    return buf.getvalue()


def test_the_resolved_agent_is_printed(tmp_path, monkeypatch):
    _declare(tmp_path, "declared-name")
    monkeypatch.chdir(tmp_path)
    assert "declared-name" in _agent_line(_run())


def test_a_disagreeing_source_is_shown_as_ignored(tmp_path, monkeypatch):
    """THE SPLIT-IDENTITY CASE. Both names must appear, and which one lost."""
    _declare(tmp_path, "declared-name")
    _mirror(tmp_path, "mirrored-name")
    monkeypatch.chdir(tmp_path)
    out = _run()
    assert "declared-name" in out
    assert "mirrored-name" in out
    assert "IGNORED" in out
    assert "DISAGREE" in out


def test_agreeing_sources_are_not_reported_as_a_conflict(tmp_path, monkeypatch):
    """Known-negative: the DISAGREE banner must be able to stay silent.

    Without this, a warning that always fires is indistinguishable from one
    that detects something, and operators learn to ignore it.
    """
    _declare(tmp_path, "same-name")
    _mirror(tmp_path, "same-name")
    monkeypatch.chdir(tmp_path)
    out = _run()
    assert "same-name" in out
    assert "DISAGREE" not in out


def test_no_declared_identity_says_so_plainly(tmp_path, monkeypatch):
    """Known-negative for the whole feature: it must not invent a name."""
    monkeypatch.chdir(tmp_path)
    out = _run()
    assert "none" in _agent_line(out).lower()
    assert "DISAGREE" not in out


def test_env_var_is_reported_as_the_winner_and_others_as_ignored(tmp_path, monkeypatch):
    _declare(tmp_path, "declared-name")
    _mirror(tmp_path, "mirrored-name")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENTBUS_AGENT", "env-name")
    out = _run()
    assert "env-name" in _agent_line(out)
    assert out.count("IGNORED") == 2


def test_json_carries_the_same_answer(tmp_path, monkeypatch):
    _declare(tmp_path, "declared-name")
    _mirror(tmp_path, "mirrored-name")
    monkeypatch.chdir(tmp_path)
    d = json.loads(_run(as_json=True))
    assert d["resolved_agent"] == "declared-name"
    assert any("mirrored-name" in s for s in d["identity_sources"])


def test_the_derivation_inputs_are_still_printed(tmp_path, monkeypatch):
    """The original purpose must survive: MCP callers pass these to register."""
    monkeypatch.chdir(tmp_path)
    out = _run()
    for key in ("device_id", "workdir", "session_key"):
        assert key in out
