"""_open_with_each MUST NOT retry a MalformedSealed against every key (#234 SEV-2-E).

The docstring on sealing._open_with_each says explicitly:

    Today nothing below raises it: the primitives raise raw binascii.Error /
    InvalidTag and normalisation happens only here. So this is safe by an
    accident of layering, and the obvious next tidy-up — normalising inside
    unseal_body — would silently make the docstring false with no test failing.

This is the exact "landmine with a sign next to it" that the audit warned about.
The runtime should assert it, not the documentation. If a future refactor moves
MalformedSealed normalisation deeper — into unseal_body, for example — this
test fails and prevents the docstring from silently becoming a lie.
"""

from __future__ import annotations

import pytest

from agentbus_client import sealing


def test_malformed_sealed_short_circuits_before_trying_every_key(monkeypatch):
    """If attempt(key) raises MalformedSealed for the first key tried, the
    loop must NOT try the remaining keys — a damaged payload does not fit any
    key on this machine, and retrying wastes work AND reports 'no key fits'
    for something that was damaged in transit.
    """
    fake_keys = ["key-alpha", "key-beta", "key-gamma"]
    monkeypatch.setattr(sealing, "load_private_keys", lambda _agent: fake_keys)

    attempts: list[str] = []

    def attempt(key: str):
        attempts.append(key)
        raise sealing.MalformedSealed("this payload is not readable as age v1")

    with pytest.raises(sealing.MalformedSealed):
        sealing._open_with_each(attempt, agent="test-agent")

    assert attempts == ["key-alpha"], (
        f"MalformedSealed must short-circuit the key loop, but it tried {attempts!r}. "
        "A future refactor that moves MalformedSealed detection into unseal_body "
        "would silently break this — the docstring warned about it, and this test "
        "is the runtime assertion."
    )


def test_cannot_decrypt_still_tries_every_key(monkeypatch):
    """The complement: CannotDecrypt is the RIGHT reason to try the next key,
    and this exercises the successful iteration path so the short-circuit test
    above is not accidentally passing because the loop is broken."""
    fake_keys = ["k1", "k2", "k3"]
    monkeypatch.setattr(sealing, "load_private_keys", lambda _agent: fake_keys)

    attempts: list[str] = []

    def attempt(key: str):
        attempts.append(key)
        raise sealing.CannotDecrypt(f"key {key} does not fit")

    with pytest.raises(sealing.CannotDecrypt):
        sealing._open_with_each(attempt, agent="test-agent")

    assert attempts == fake_keys, (
        f"CannotDecrypt should try every key, but the loop stopped after {attempts!r}."
    )


def test_cannot_decrypt_succeeds_on_a_later_key(monkeypatch):
    """The happy path: an older key opens it after newer keys reject it."""
    fake_keys = ["new-key", "old-key"]
    monkeypatch.setattr(sealing, "load_private_keys", lambda _agent: fake_keys)

    def attempt(key: str):
        if key == "old-key":
            return "PLAINTEXT"
        raise sealing.CannotDecrypt("wrong key")

    result = sealing._open_with_each(attempt, agent="test-agent")
    assert result == "PLAINTEXT"


def test_no_keys_reports_the_right_error(monkeypatch):
    """When the machine holds NO keys at all, the loop reports that
    specifically — never a CannotDecrypt derived from a stale exception."""
    monkeypatch.setattr(sealing, "load_private_keys", lambda _agent: [])

    def attempt(_key):
        raise AssertionError("attempt() must never be called when there are no keys")

    with pytest.raises(sealing.CannotDecrypt, match="no sealing key"):
        sealing._open_with_each(attempt, agent="test-agent")


def test_a_stray_generic_exception_is_normalised_to_malformed(monkeypatch):
    """An unexpected exception from the primitives is reclassified as
    MalformedSealed — matching the docstring's promise that "damaged or was
    never sealed" is one answer, not a raw traceback."""
    fake_keys = ["k1"]
    monkeypatch.setattr(sealing, "load_private_keys", lambda _agent: fake_keys)

    def attempt(_key):
        raise ValueError("garbage in")

    with pytest.raises(sealing.MalformedSealed):
        sealing._open_with_each(attempt, agent="test-agent")
