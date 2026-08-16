"""The isolation property nobody could test through the API.

agentbus-ui-c760a1 ran an adversarial cross-agent attempt against a message
addressed to a third agent and reported PASS across NINE routes — CLI show,
verify-sender, thread, SDK raw, forward, ack, and three direct HTTP paths
including /messages/{id}/raw and /deliveries/{id}. Every one returned an
identical `not_found`, with no distinguishable "forbidden" vs "does not exist"
signal, so there is not even an existence oracle.

AND THEY NAMED THE LIMIT OF THEIR OWN RESULT, which is why this file exists:

    "I obtained zero bytes by any route tried, so the ciphertext-with-no-
     matching-stanza case never came up — the access control refused before
     encryption was ever load-bearing."

Exactly right. Nine refusals prove the ACCESS CONTROL holds. They prove nothing
about the ENCRYPTION, because the encryption was never reached. Those two layers
fail differently and independently: if scoping ever regresses — a bad join, a
leaked id, a future endpoint, a database dump, a backup — the only thing between
a peer and the plaintext is whether the body is sealed to keys they do not hold.

That question needs no API and no network. It is a property of the ciphertext
itself, and it is tested here directly: seal to one agent, then attempt to open
it with every OTHER agent's key and confirm each fails. This is the test that
would still mean something on the day the access control does not.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentbus_client import sealing
from agentbus_client.sealing import CannotDecrypt

MARKER = "ISOLATION-MARKER-7Q4X"


def _keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *agents: str
) -> dict[str, tuple[str, str]]:
    """A real keypair per agent, in a real per-agent file, as onboarding writes them."""
    monkeypatch.setattr(sealing, "config_dir", lambda: tmp_path)
    return {name: sealing.ensure_keypair(name) for name in agents}


def test_a_non_recipient_key_cannot_open_the_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The property the nine-route API test could not reach."""
    keys = _keys(tmp_path, monkeypatch, "recipient", "outsider-a", "outsider-b")
    recipient_private, recipient_public = keys["recipient"]

    body = sealing.seal_for(MARKER, [recipient_public])

    # KNOWN-POSITIVE FIRST. If the intended recipient could not read it either,
    # every assertion below would pass against a broken seal — "nobody can read
    # it" is not the property, "only they can" is.
    assert sealing.unseal_body(body, recipient_private) == MARKER

    for outsider in ("outsider-a", "outsider-b"):
        private, _public = keys[outsider]
        with pytest.raises(CannotDecrypt) as exc:
            sealing.unseal_body(body, private)
        assert MARKER not in str(exc.value), (
            f"{outsider} could not decrypt, but the plaintext leaked through the error message"
        )


def test_the_marker_never_appears_in_the_ciphertext(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cheapest check, and the one that catches a seal that silently no-ops.

    A `seal_for` that returned its input unchanged would pass a decrypt test
    trivially — every key "opens" plaintext.
    """
    keys = _keys(tmp_path, monkeypatch, "recipient")
    _private, public = keys["recipient"]

    body = sealing.seal_for(MARKER, [public])

    assert MARKER not in body
    assert body.lstrip().startswith("-----BEGIN AGE ENCRYPTED FILE-----")


def test_adding_a_recipient_does_not_open_it_to_everyone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Multi-recipient is the case where a sloppy implementation leaks.

    Sealing to two agents must produce a body those two can open and a third
    cannot — not a body that anyone with any key can open.
    """
    keys = _keys(tmp_path, monkeypatch, "alice", "bob", "mallory")
    body = sealing.seal_for(MARKER, [keys["alice"][1], keys["bob"][1]])

    for name in ("alice", "bob"):
        assert sealing.unseal_body(body, keys[name][0]) == MARKER, (
            f"{name} was addressed and cannot read it"
        )

    with pytest.raises(CannotDecrypt):
        sealing.unseal_body(body, keys["mallory"][0])


def test_a_rotated_outsider_key_still_cannot_open_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`unseal_with_any` tries EVERY key a machine holds.

    An outsider who has rotated keys several times holds a pile of private keys.
    None of them were ever a recipient, so none must work — and the failure must
    stay a clean refusal rather than becoming an error that leaks the body.
    """
    keys = _keys(tmp_path, monkeypatch, "recipient", "outsider")
    body = sealing.seal_for(MARKER, [keys["recipient"][1]])

    held = [keys["outsider"][0]]
    for _ in range(3):
        private, _public = sealing.generate_keypair()
        held.append(private)

    for private in held:
        with pytest.raises(CannotDecrypt) as exc:
            sealing.unseal_body(body, private)
        assert MARKER not in str(exc.value)
