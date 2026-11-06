"""#59: an agent with no published sealing key is unreachable and never told.

FOUND ACROSS THE WHOLE WORKSPACE by agentbus-8dc08d and confirmed here. On an
encrypted workspace an agent with no published age-x25519 key cannot be written
to by anyone, permanently. The condition surfaces only in the SENDER's terminal:

    error: cannot seal: these recipients have published no public key, so they
    could never read this message — prism-dev-24cdee

The affected agent sees nothing. It reads healthy from every angle available to
it — registered, in the phonebook, active, watcher attached, pong_attested true,
availability online, can_send true — and cannot distinguish "unreachable for
three weeks" from "a quiet week". Measured: 51 active agents, exactly one in this
state, alive since 2026-08-18.

The ALGORITHM filter is the whole test. 83 published keys are 68 age-x25519 plus
15 ed25519; counting "has a published key" calls the broken agent clean. A
signing key cannot open a sealed body.
"""

from __future__ import annotations

import argparse
import io
from contextlib import redirect_stdout
from typing import Any

import pytest

from agentbus_client import cli


class _Bus:
    def __init__(self, payload: Any, agent: str = "alpha") -> None:
        self.payload = payload
        self.agent = agent

    def whoami(self) -> dict[str, Any]:
        return {"workspace": {"slug": "w"}, "agent": {"name": self.agent}}

    def _request(self, method: Any, path: Any, **kw: Any) -> Any:
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def _run(monkeypatch: pytest.MonkeyPatch, bus: Any) -> str:
    monkeypatch.setattr(cli._common, "_bus", lambda _a: bus)
    args = argparse.Namespace(json=False, agent=None, api_key=None, base_url=None, qr=False)
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli.cmd_whoami(args)
    return buf.getvalue()


def _keys(*rows: Any) -> dict[str, Any]:
    return {"encrypted": True, "keys": list(rows)}


def test_an_agent_with_no_sealing_key_is_told_it_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = _run(monkeypatch, _Bus(_keys({"agent": "someone-else", "algorithm": "age-x25519"})))
    assert "UNREACHABLE" in out
    assert "NO peer can send you" in out
    assert "agentbus signin" in out, "must name the remedy, not just the problem"


def test_an_agent_with_a_sealing_key_gets_no_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """KNOWN-POSITIVE TWIN. A warning on every whoami teaches nothing."""
    out = _run(monkeypatch, _Bus(_keys({"agent": "alpha", "algorithm": "age-x25519"})))
    assert "UNREACHABLE" not in out


def test_a_signing_key_does_not_count_as_a_sealing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE DISCRIMINATOR. 15 of the 83 keys on the real workspace are ed25519;
    counting 'has a published key' would call the broken agent clean."""
    out = _run(monkeypatch, _Bus(_keys({"agent": "alpha", "algorithm": "ed25519"})))
    assert "UNREACHABLE" in out, "an ed25519 signing key cannot open a sealed body"


def test_a_revoked_sealing_key_does_not_count(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _run(
        monkeypatch,
        _Bus(
            _keys(
                {
                    "agent": "alpha",
                    "algorithm": "age-x25519",
                    "revoked_at": "2026-09-01T00:00:00Z",
                }
            )
        ),
    )
    assert "UNREACHABLE" in out


def test_an_unencrypted_workspace_is_never_warned(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sealing is not required there, so the warning would be a lie."""
    out = _run(monkeypatch, _Bus({"encrypted": False, "keys": []}))
    assert "UNREACHABLE" not in out


def test_a_failed_lookup_claims_nothing_either_way(monkeypatch: pytest.MonkeyPatch) -> None:
    """Absence of an answer is not an answer: whoami must still work, and must
    not assert reachability it did not verify."""
    out = _run(monkeypatch, _Bus(RuntimeError("bus down")))
    assert "UNREACHABLE" not in out
    assert "alpha" in out, "whoami must still do its actual job"


@pytest.mark.parametrize("name", ["", "(no acting agent)"])
def test_no_acting_agent_is_not_warned(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    out = _run(monkeypatch, _Bus(_keys(), agent=name))
    assert "UNREACHABLE" not in out


def test_a_future_age_algorithm_is_not_assumed_to_seal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Raised by the server team: the client used a PREFIX match
    (startswith("age")) while the server matches SEAL_ALGORITHM exactly.

    Identical today, because age-x25519 is the only age-family algorithm that
    exists. The day a second one appears that is NOT the seal algorithm, a prefix
    match says REACHABLE while the server refuses to seal — no warning when there
    should be one, which is the silent direction and recreates the very defect
    this feature fixes, one layer over.

    Two independently written predicates for one question is the thing to avoid,
    so the client names the constant rather than describing it.
    """
    out = _run(monkeypatch, _Bus(_keys({"agent": "alpha", "algorithm": "age-somethingelse"})))
    assert "UNREACHABLE" in out, (
        "an age-family algorithm that is not the seal algorithm must not count as reachability"
    )


def test_the_real_seal_algorithm_still_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    """KNOWN-POSITIVE TWIN: tightening to an exact match must not break the
    algorithm that actually seals."""
    from agentbus_client.cli._directory import SEAL_ALGORITHM

    assert SEAL_ALGORITHM == "age-x25519"
    out = _run(monkeypatch, _Bus(_keys({"agent": "alpha", "algorithm": SEAL_ALGORITHM})))
    assert "UNREACHABLE" not in out
