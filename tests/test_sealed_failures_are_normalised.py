"""A facade must not leak binascii.Error at its caller.

FOUND FROM OUTSIDE by macbook-admin-bd8e86 against 0.5.2 installed from PyPI:
`unseal_bytes_with_any` / `unseal_with_any` caught only CannotDecrypt, so a
truncated attachment raised InvalidTag, a flipped base64 character raised
binascii.Error, and a file that was never age raised ValueError — all straight
through the function whose entire promise is "opens it with whichever of this
machine's keys fits, or tells you it cannot".

Their point about WHY it matters here specifically: `unseal_bytes` is a
primitive, and a caller reaching for it might reasonably know the layers
underneath. `*_with_any` reads as a facade, and the obvious caller is

    try: unseal_bytes_with_any(raw)
    except CannotDecrypt: show "this attachment cannot be opened"

which the pre-fix version would fall straight past.

THE FIX KEEPS BOTH PROPERTIES. MalformedSealed subclasses CannotDecrypt, so the
facade's promise holds for anyone catching the general case, while the type
still distinguishes "damaged" from "not for me" — which need different actions
(re-fetch the file, versus find the old key). Collapsing them is the failure the
parent class was written to avoid.

The loop still continues ONLY on CannotDecrypt. A corrupt payload fails at once
rather than being retried against every key: retrying is pointless and slower,
and it would report damage as "no key fits", sending the reader hunting for a
key that would not have helped.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from agentbus_client import sealing


@pytest.fixture()
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(sealing, "config_dir", lambda: tmp_path)
    # ensure_keypair() with no explicit agent falls back to $AGENTBUS_AGENT (a
    # sealing key belongs to ONE agent). In a wired session the hook exports it;
    # a bare `pytest` shell has nothing set, so give these tests their own acting
    # agent rather than depending on the ambient environment.
    monkeypatch.setenv("AGENTBUS_AGENT", "sealing-test")
    return tmp_path


def _corrupt_inside_payload(armored: bytes) -> bytes:
    """Damage the BASE64, not the header.

    My first attempt flipped the first 'A' in the file — which lands in
    '-----BEGIN AGE ENCRYPTED FILE-----'. `_dearmor` discards every line
    starting with '-----', so the payload was untouched and the "corrupted"
    attachment decrypted perfectly. A corruption fixture that does not corrupt
    anything is the purest form of the vacuous check.
    """
    lines = armored.split(b"\n")
    middle = len(lines) // 2
    lines[middle] = lines[middle][::-1]
    return b"\n".join(lines)


@pytest.mark.usefixtures("store")
def test_the_intact_case_opens_which_is_what_makes_the_rest_meaningful() -> None:
    _private, public = sealing.ensure_keypair()
    blob = bytes(range(256)) * 275  # multi-chunk
    assert sealing.unseal_bytes_with_any(sealing.seal_for_bytes(blob, [public])) == blob


@pytest.mark.parametrize("damage", ["truncated", "flipped", "not-age"])
@pytest.mark.usefixtures("store")
def test_damaged_input_raises_a_normalised_error(damage: str) -> None:
    _private, public = sealing.ensure_keypair()
    good = sealing.seal_for_bytes(b"x" * 70000, [public])
    bad = {
        "truncated": good[:200],
        "flipped": _corrupt_inside_payload(good),
        "not-age": b"hello world, never sealed",
    }[damage]
    assert bad != good, "the fixture must actually differ from the intact input"

    with pytest.raises(sealing.MalformedSealed) as caught:
        sealing.unseal_bytes_with_any(bad)
    # The facade's promise: a caller catching the general case still catches it.
    assert isinstance(caught.value, sealing.CannotDecrypt)


@pytest.mark.usefixtures("store")
def test_a_message_for_someone_else_is_NOT_reported_as_damaged() -> None:
    """The other direction, and the reason MalformedSealed is a separate type:
    'not for me' must not be dressed up as corruption, or the reader re-fetches
    a file that was never broken."""
    sealing.ensure_keypair()
    _other_private, other_public = sealing.generate_keypair()
    sealed = sealing.seal_for_bytes(b"for another machine", [other_public])

    with pytest.raises(sealing.CannotDecrypt) as caught:
        sealing.unseal_bytes_with_any(sealed)
    assert not isinstance(caught.value, sealing.MalformedSealed)


@pytest.mark.usefixtures("store")
def test_bodies_behave_the_same_as_attachments() -> None:
    _private, public = sealing.ensure_keypair()
    body = sealing.seal_for("readable", [public])
    assert sealing.unseal_with_any(body) == "readable"
    with pytest.raises(sealing.MalformedSealed):
        sealing.unseal_with_any("-----BEGIN AGE ENCRYPTED FILE-----\nnot base64!!\n")


@pytest.mark.usefixtures("store")
def test_no_key_at_all_says_so_rather_than_no_key_fits() -> None:
    _other, public = sealing.generate_keypair()
    sealed = sealing.seal_for("x", [public])
    with pytest.raises(sealing.CannotDecrypt) as caught:
        sealing.unseal_with_any(sealed)
    assert "holds no sealing key" in str(caught.value)


def test_the_primitive_must_not_raise_the_normalised_error_itself() -> None:
    """The unwritten invariant, written down. Raised by macbook-admin-bd8e86.

    `_open_with_each` continues its loop on CannotDecrypt and MalformedSealed
    is a subclass of it. If `unseal_body` ever normalised internally — the
    obvious tidy-up for anyone reading this file — a damaged payload would be
    retried against every key, which the docstring forbids.

    The loop now re-raises MalformedSealed before the continue-clause, so the
    ordering protects it. This pins the other half: the primitive raises RAW
    errors, and if that ever changes this test says so rather than the
    behaviour quietly degrading.
    """
    _private, public = sealing.generate_keypair()
    private, _other = sealing.generate_keypair()
    corrupt = sealing.seal_for("x", [public])[:120]
    # Broad on purpose: the assertion below is about the TYPE, not the message.
    with pytest.raises(Exception) as caught:
        sealing.unseal_body(corrupt, private)
    assert not isinstance(caught.value, sealing.CannotDecrypt), (
        "the primitive now raises a CannotDecrypt subclass; _open_with_each's "
        "clause ordering is what keeps that from meaning 'try the next key'"
    )


@pytest.mark.usefixtures("store")
def test_a_malformed_error_from_below_is_not_retried_against_every_key() -> None:
    """The injection agentbus-frontend-5e9d03 measured, run against the fix.

    Before the clause reorder: MalformedSealed IS-A CannotDecrypt, so the
    continue-clause caught it, both keys were tried, and a DAMAGED payload was
    reported as "no key fits" — sending the reader to hunt for a key that would
    not have helped.

    After: it propagates on the first attempt. Two keys held on purpose, because
    with one key the two behaviours are indistinguishable — which is why nobody
    saw it until someone rotated.
    """
    sealing.ensure_keypair()
    path = sealing.key_path()
    path.with_suffix(".key.superseded").write_text(path.read_text())
    path.unlink()
    sealing.ensure_keypair()
    assert len(sealing.load_private_keys()) == 2, "the fixture needs TWO keys to mean anything"

    attempts: list[str] = []

    def _damaged(key: str) -> None:
        attempts.append(key)
        raise sealing.MalformedSealed("damaged, raised by the primitive")

    with pytest.raises(sealing.MalformedSealed):
        sealing._open_with_each(_damaged)
    assert len(attempts) == 1, f"retried a damaged payload {len(attempts)} times"
