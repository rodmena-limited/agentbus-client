"""#220 (ticket 220): `keys revoke` described a SEALING key while revoking a SIGNING one.

Revoking an ed25519 signing key printed warning text written for age X25519:

    FORWARD ONLY: this stops peers sealing NEW mail to it. Anything already
    sealed to it stays sealed to it, and re-publishing will not undo that.
    Its private half is NOT on this machine. ...
    every message sealed to it is ALREADY unreadable ...

Nothing is sealed to a signing key, so every consequence named there is wrong —
and the locality line was FALSE in the case that motivated this: the signing key
was sitting in `keys/signing-<agent>.key` on that very machine, while the check
consulted the sealing locations only.

WHY IT MATTERS MORE THAN WORDING. An operator revoking a possibly-compromised key
decides on exactly one fact: is the private half still somewhere it should not
be. The warning answered that question confidently and wrongly. The revoke
itself was always correct — it matches on fingerprint regardless of algorithm —
so this never lost data; it only ever misinformed the person deciding.

The fix ASKS the server which algorithm a fingerprint belongs to rather than
guessing from the string, and has a real third answer — `unknown`, for a
fingerprint in neither published list — which claims nothing rather than picking
an algorithm and being wrong half the time.
"""

from __future__ import annotations

import argparse
import contextlib
import io
from typing import Any

import pytest

from agentbus_client import cli


class _Bus:
    """A stand-in server that publishes one sealing key and one signing key."""

    SEALING = "aaaaaaaaaaaaaaaa"
    SIGNING = "bbbbbbbbbbbbbbbb"

    def __init__(self) -> None:
        self.calls: list[Any] = []

    def _request(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        self.calls.append((method, path, kw.get("params")))
        algorithm = (kw.get("params") or {}).get("algorithm")
        fingerprint = self.SIGNING if algorithm == "ed25519" else self.SEALING
        return {"keys": [{"fingerprint": fingerprint, "public_key": "x"}]}


def _revoke(bus: _Bus, fingerprint: str, mine: str | None = None) -> str:
    """Run the warning path (no --yes) and return what it printed."""
    args = argparse.Namespace(fingerprint=fingerprint, yes=False, reason=None, json=False)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli._keys_revoke(bus, args, "an-agent", mine)
    assert rc == 2, "the warning path must refuse without --yes"
    return buf.getvalue()


def test_revoking_a_signing_key_does_not_talk_about_sealing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bug, exactly."""
    monkeypatch.setattr(cli._keys, "_local_signing_fingerprint", lambda _agent: None)
    monkeypatch.setattr(cli._keys, "_superseded_fingerprints", lambda: set())

    out = _revoke(_Bus(), _Bus.SIGNING)

    assert "SIGNING key" in out
    for wrong in ("sealing NEW mail", "sealed to it stays sealed", "ALREADY unreadable"):
        assert wrong not in out, f"signing-key revoke still claims {wrong!r}"
    assert "VERIFY" in out, "it must say what revoking a signing key actually costs"
    assert "unverifiable" in out, (
        "an operator needs to know past signatures become unverifiable rather "
        "than invalid — nothing starts reading as forged"
    )


def test_the_locality_line_is_true_for_a_signing_key_held_here(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The line that was actually false.

    KNOWN-POSITIVE PAIR: the same call must say the opposite when the key is
    NOT held here, otherwise this passes against a version that hardcodes one
    answer — which is how the original bug behaved.
    """
    monkeypatch.setattr(cli._keys, "_superseded_fingerprints", lambda: set())

    monkeypatch.setattr(cli._keys, "_local_signing_fingerprint", lambda _agent: _Bus.SIGNING)
    held = _revoke(_Bus(), _Bus.SIGNING)
    assert "private half IS still on this machine" in held

    monkeypatch.setattr(cli._keys, "_local_signing_fingerprint", lambda _agent: "something-else")
    absent = _revoke(_Bus(), _Bus.SIGNING)
    assert "private half IS still on this machine" not in absent
    assert "not in this agent's signing key file" in absent


def test_revoking_a_sealing_key_still_says_what_it_always_said(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression guard. The sealing wording was correct and must survive."""
    monkeypatch.setattr(cli._keys, "_local_signing_fingerprint", lambda _agent: None)
    monkeypatch.setattr(cli._keys, "_superseded_fingerprints", lambda: set())

    out = _revoke(_Bus(), _Bus.SEALING)

    assert "SEALING key" in out
    assert "stops peers sealing NEW mail to it" in out
    assert "VERIFY" not in out, "a sealing revoke must not talk about signatures"


def test_an_unknown_fingerprint_claims_nothing_about_either_algorithm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fingerprint in neither list — already revoked, or another agent's.

    Guessing here is what the original code did in effect. Claiming no
    consequence is the only honest answer when the algorithm is unestablished.
    """
    monkeypatch.setattr(cli._keys, "_local_signing_fingerprint", lambda _agent: None)
    monkeypatch.setattr(cli._keys, "_superseded_fingerprints", lambda: set())

    out = _revoke(_Bus(), "cccccccccccccccc")

    assert "not in" in out and "published sealing or signing keys" in out
    for claim in ("sealing NEW mail", "ALREADY unreadable", "stop being able"):
        assert claim not in out, f"unknown-algorithm revoke still claims {claim!r}"


def test_the_algorithm_is_asked_of_the_server_not_guessed() -> None:
    """A fingerprint is an opaque digest; nothing in it names its keypair.

    Guarding the mechanism, because a future 'optimisation' that infers the
    algorithm from the string would reintroduce the bug silently.
    """
    bus = _Bus()
    assert cli._key_algorithm(bus, "an-agent", _Bus.SIGNING) == "ed25519"
    assert cli._key_algorithm(bus, "an-agent", _Bus.SEALING) == "age"
    assert cli._key_algorithm(bus, "an-agent", "ffffffffffffffff") == "unknown"
    assert any((params or {}).get("algorithm") == "ed25519" for _m, _p, params in bus.calls), (
        "the ed25519 list was never consulted"
    )
