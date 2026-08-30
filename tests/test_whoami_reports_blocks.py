"""#48: whoami surfaces active blocks, and NULL means unknown.

An agent that has forgotten it is deaf to a peer reads the resulting silence as
"they stopped sending". whoami is the startup call, so the blocks are seen
before the silence is interpreted.

The server wraps this advisory and may return null when the lookup fails.
Rendering null as "0 blocks" would state the OPPOSITE of what is known — it is
the difference between "nobody is blocked" and "I could not find out".
"""

from __future__ import annotations

import argparse
import io
from contextlib import redirect_stdout

import pytest

from agentbus_client.cli import _directory


def _whoami(monkeypatch, result):
    monkeypatch.setattr(
        _directory._common,
        "_bus",
        lambda _a: type("B", (), {"whoami": lambda s: result})(),
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        _directory.cmd_whoami(argparse.Namespace(json=False, agent=None, qr=False))
    return buf.getvalue()


def _base(**extra):
    return {
        "workspace": {"slug": "w"},
        "agent": {"name": "me", "labels": None},
        "address": "a@b",
        **extra,
    }


def test_active_blocks_are_reported(monkeypatch):
    out = _whoami(monkeypatch, _base(blocks={"count": 2, "suppressed_total": 7}))
    assert "2 active" in out
    assert "7 message(s) suppressed" in out


@pytest.mark.parametrize(
    "blocks",
    [None, {"count": 0, "suppressed_total": 0}, {}],
    ids=["null-unknown", "zero", "empty"],
)
def test_nothing_is_claimed_when_there_is_nothing_to_claim(monkeypatch, blocks):
    """null must NOT render as '0 blocks': that asserts something unknown."""
    out = _whoami(monkeypatch, _base(blocks=blocks))
    assert "blocks:" not in out


def test_the_key_being_absent_is_not_an_error(monkeypatch):
    """An older server omits the field entirely; whoami is the startup call and
    must not start failing over an advisory nothing depends on."""
    out = _whoami(monkeypatch, _base())
    assert "agent:" in out
    assert "blocks:" not in out
