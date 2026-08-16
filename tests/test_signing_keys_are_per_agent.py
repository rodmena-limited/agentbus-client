"""#220: two agents on one machine shared ONE signing key.

MEASURED ON THE OPERATOR'S OWN MACHINE, read back from the server, not inferred:

    agentbus-8dc08d      7b310df47c7de439
    agentbus-ui-c760a1   7b310df47c7de439   <- the same key

`signing_key_path()` took no agent and returned `keys/signing.key`, so whichever
agent ran `agentbus keys sign` first generated the key and every later agent on
that box published the SAME public key as its own.

WHY THAT IS THE WHOLE FEATURE BROKEN. A signature answers exactly one question —
"did THIS agent send this" — and a key held by two agents cannot answer it.
Either could sign as the other, the bus could not tell them apart, and no
examination of the message would reveal which one wrote it.

HOW IT SURFACED. A peer ran `verify-sender` on a message from another agent, got
`verified: true`, and noticed the fingerprint cited as evidence was THEIR OWN
published key. The tool was right that the bytes matched; it was the identity
that was meaningless. They were correct to call it unusable in both directions.

This is the same defect as the sealing key, which `key_path` above already
documents and fixes — made once, and not made one file over. The user's ruling
covers both: each agent holds its own keypair, even on one machine.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from agentbus_client import _signing, sealing
from agentbus_client.client import _Base


def test_two_agents_on_one_machine_get_different_signing_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bug, exactly: same box, same config dir, two agents."""
    monkeypatch.setattr(sealing, "config_dir", lambda: tmp_path)

    a_private, a_public = sealing.ensure_signing_keypair("agent-one")
    b_private, b_public = sealing.ensure_signing_keypair("agent-two")

    assert a_private != b_private, "two agents on one machine share a signing private key"
    assert a_public != b_public, (
        "two agents publish the SAME signing public key, so a signature cannot "
        "distinguish them — which is the only thing a signature is for"
    )

    # and each is stable: asking twice must not mint a new identity
    assert sealing.ensure_signing_keypair("agent-one")[0] == a_private
    assert sealing.load_signing_key("agent-one") == a_private
    assert sealing.load_signing_key("agent-two") == b_private


def test_the_old_shared_key_is_not_adopted_by_anyone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No migration from `signing.key`, deliberately.

    Adopting the machine-wide file would hand every agent on the box the
    colliding key again — the fix would restore the bug it fixes. A fresh
    per-agent key is generated instead; until it is published, verify-sender
    reports `unverifiable`, never `invalid`.
    """
    monkeypatch.setattr(sealing, "config_dir", lambda: tmp_path)
    legacy = tmp_path / "keys" / "signing.key"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    shared, _ = _signing.generate_keypair()
    legacy.write_text(shared + "\n")

    mine, _public = sealing.ensure_signing_keypair("agent-one")
    assert mine != shared, "the shared machine key was adopted; the collision is back"
    assert legacy.read_text().strip() == shared, "the old file was mutated rather than left alone"


def test_a_signing_key_cannot_be_read_without_an_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No acting agent means no key — never a machine-wide fallback.

    A fallback here would quietly rebuild the shared key: any call that forgot
    to thread the agent through would land on one file again. `load_signing_key`
    answers None, which every caller already treats as "send unsigned".
    """
    monkeypatch.setattr(sealing, "config_dir", lambda: tmp_path)
    monkeypatch.delenv("AGENTBUS_AGENT", raising=False)

    with pytest.raises(ValueError):
        sealing.signing_key_path(None)
    assert sealing.load_signing_key(None) is None


def test_the_client_signs_with_the_acting_agents_key() -> None:
    """The call site, not just the helper.

    A per-agent helper called with no agent is still a shared key in practice,
    and that is exactly how the sealing fix could have been half-made.
    """
    src = inspect.getsource(_Base._sign_if_possible)
    assert "load_signing_key(agent or self.agent)" in src, (
        "the signer no longer passes an agent, so every agent on this machine "
        "signs with the same key again"
    )
