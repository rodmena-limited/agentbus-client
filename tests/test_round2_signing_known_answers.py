from __future__ import annotations

import hashlib
import re

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from agentbus_client import _signing, sealing
from agentbus_client._agefmt import bech32_decode, bech32_encode

RFC8032_SEED = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
RFC8032_PUBLIC = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
RFC8032_EMPTY_SIG = bytes.fromhex(
    "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"
)

PRIVATE = "ABSIGSEC1N4SMR800L4DXPW5YFT6F9MPVC3ZYN3TF0VEXJXTS8WKQX89W0ASQ0XV08F"
PUBLIC = "absig16adfsqvzky9t042tlmfujeq88g8wzuhnm2nzxfd0qgdx3ac82ydqm7nhdg"
FINGERPRINT = "21fe31dfa154a261"
EMPTY_SIG = (
    "absigv1u4tyxqxrvzk89yyxutxgqm5z32zgwlc7hrjajaxcw0sx2gjfq924lwyzzkg2xwav"
    "cc0rjuqulx6xh5jm7hc9jka7y3j4zs2r3eapqzcld4kpf"
)

BODY = "ledger balanced\n"
BODY_SHA = "d64a09a1f258e68accb6d2e75cce753bd82efe4bb6452b364337c84b6ec02d17"
CANONICAL = (
    b"agentbus-sig-v1\n"
    b"from: alice-1a2b3c\n"
    b"to: bob,zed\n"
    b"cc: al,carol\n"
    b"subject: Quarterly close\n"
    b"priority: urgent\n"
    b"body-sha256: " + BODY_SHA.encode() + b"\n"
)
CANONICAL_SIG = (
    "absigv1a52wlv256zp5plz6mfqwxeks004yvnc7hy67u20u0slnxkqs860slvflpfv5yjdc3r"
    "xvj8cduaav4lrv0quph5u2l0s9wv0qkjxukpskufzzr"
)
EMPTY_SHA = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def _fields(**overrides):
    base = {
        "sender": "alice-1a2b3c",
        "to": ["zed", "bob"],
        "cc": ["carol", "al"],
        "subject": "Quarterly close",
        "priority": "urgent",
        "body": BODY,
    }
    base.update(overrides)
    return base


def test_body_hash_constant_is_the_real_sha256():
    assert hashlib.sha256(BODY.encode()).hexdigest() == BODY_SHA
    assert hashlib.sha256(b"").hexdigest() == EMPTY_SHA


def test_public_key_matches_rfc8032_vector():
    assert _signing.public_from_private(PRIVATE) == PUBLIC
    assert bech32_decode(PUBLIC) == RFC8032_PUBLIC
    assert bech32_decode(PRIVATE.lower()) == RFC8032_SEED


def test_public_from_private_accepts_lowercase_private():
    assert _signing.public_from_private(PRIVATE.lower()) == PUBLIC


def test_fingerprint_is_sixteen_lowercase_hex_of_sha256_of_raw_key():
    fp = _signing.fingerprint(PUBLIC)
    assert fp == FINGERPRINT
    assert fp == hashlib.sha256(RFC8032_PUBLIC).hexdigest()[:16]
    assert len(fp) == 16
    assert re.fullmatch(r"[0-9a-f]{16}", fp)


def test_fingerprint_rejects_a_sealing_key():
    _priv, age_public = sealing.generate_keypair()
    with pytest.raises(ValueError, match="expected a absig1"):
        _signing.fingerprint(age_public)


def test_sign_empty_message_is_rfc8032_signature():
    sig = _signing.sign(PRIVATE, b"")
    assert sig == EMPTY_SIG
    assert bech32_decode(sig) == RFC8032_EMPTY_SIG


def test_canonical_bytes_known_answer():
    assert _signing.canonical_bytes(**_fields()) == CANONICAL


def test_canonical_bytes_empty_message_known_answer():
    got = _signing.canonical_bytes(sender="a", to=[], cc=None, subject=None, priority="normal")
    assert got == (
        b"agentbus-sig-v1\nfrom: a\nto: \ncc: \nsubject: \npriority: normal\n"
        b"body-sha256: " + EMPTY_SHA.encode() + b"\n"
    )


def test_canonical_bytes_body_sha256_wins_over_body():
    fields = _fields()
    fields.pop("body")
    assert _signing.canonical_bytes(**fields, body_sha256=BODY_SHA) == CANONICAL
    assert _signing.canonical_bytes(**_fields(body="other"), body_sha256=BODY_SHA) == CANONICAL


def test_canonical_bytes_none_body_hashes_empty_string():
    got = _signing.canonical_bytes(**_fields(body=None))
    assert got.endswith(b"body-sha256: " + EMPTY_SHA.encode() + b"\n")


def test_canonical_bytes_sorts_recipients_regardless_of_input_order():
    a = _signing.canonical_bytes(**_fields(to=["bob", "zed"], cc=["al", "carol"]))
    b = _signing.canonical_bytes(**_fields(to=["zed", "bob"], cc=["carol", "al"]))
    assert a == b == CANONICAL


@pytest.mark.parametrize(
    "override",
    [
        {"sender": "mallory-000000"},
        {"to": ["bob"]},
        {"to": ["bob", "zed", "eve"]},
        {"cc": []},
        {"cc": ["carol"]},
        {"subject": "Quarterly close!"},
        {"subject": None},
        {"priority": "normal"},
        {"body": "ledger balanced"},
        {"body": None},
    ],
)
def test_every_signed_field_changes_the_canonical_bytes(override):
    assert _signing.canonical_bytes(**_fields(**override)) != CANONICAL


def test_sign_canonical_known_answer_and_independent_verify():
    sig = _signing.sign(PRIVATE, CANONICAL)
    assert sig == CANONICAL_SIG
    Ed25519PublicKey.from_public_bytes(RFC8032_PUBLIC).verify(bech32_decode(sig), CANONICAL)


def test_sign_matches_cryptography_directly():
    expected = Ed25519PrivateKey.from_private_bytes(RFC8032_SEED).sign(CANONICAL)
    assert bech32_decode(_signing.sign(PRIVATE, CANONICAL)) == expected


def test_verify_accepts_pinned_signature():
    assert _signing.verify(PUBLIC, CANONICAL, CANONICAL_SIG) is None
    assert _signing.verify(PUBLIC, b"", EMPTY_SIG) is None


def test_verify_accepts_signature_made_independently():
    raw = Ed25519PrivateKey.from_private_bytes(RFC8032_SEED).sign(CANONICAL)
    assert _signing.verify(PUBLIC, CANONICAL, bech32_encode("absigv", raw)) is None


@pytest.mark.parametrize(
    "override",
    [
        {"sender": "mallory-000000"},
        {"to": ["bob"]},
        {"cc": []},
        {"subject": "Quarterly close!"},
        {"priority": "background"},
        {"body": "ledger unbalanced\n"},
    ],
)
def test_verify_rejects_tampered_canonical_payload(override):
    tampered = _signing.canonical_bytes(**_fields(**override))
    with pytest.raises(_signing.BadSignature):
        _signing.verify(PUBLIC, tampered, CANONICAL_SIG)


def test_verify_rejects_one_flipped_byte():
    tampered = CANONICAL[:-2] + bytes([CANONICAL[-2] ^ 1]) + CANONICAL[-1:]
    with pytest.raises(_signing.BadSignature):
        _signing.verify(PUBLIC, tampered, CANONICAL_SIG)


def test_verify_rejects_signature_from_another_key():
    other_private, other_public = _signing.generate_keypair()
    sig = _signing.sign(other_private, CANONICAL)
    with pytest.raises(_signing.BadSignature):
        _signing.verify(PUBLIC, CANONICAL, sig)
    assert _signing.verify(other_public, CANONICAL, sig) is None


def test_verify_rejects_garbage_signature_as_bad_signature():
    with pytest.raises(_signing.BadSignature, match="expected a absigv1"):
        _signing.verify(PUBLIC, CANONICAL, "not-a-signature")


def test_verify_rejects_public_key_used_as_signature():
    with pytest.raises(_signing.BadSignature):
        _signing.verify(PUBLIC, CANONICAL, PUBLIC)


def test_verify_rejects_sealing_key_as_public_key():
    _priv, age_public = sealing.generate_keypair()
    with pytest.raises(_signing.BadSignature, match="expected a absig1"):
        _signing.verify(age_public, CANONICAL, CANONICAL_SIG)


def test_verify_bad_signature_carries_reason_text():
    with pytest.raises(_signing.BadSignature) as info:
        _signing.verify(PUBLIC, CANONICAL + b"x", CANONICAL_SIG)
    assert str(info.value) == "signature does not verify"


def test_generate_keypair_shape_and_consistency():
    private, public = _signing.generate_keypair()
    assert private.startswith("ABSIGSEC1")
    assert private == private.upper()
    assert public.startswith("absig1")
    assert public == public.lower()
    assert _signing.public_from_private(private) == public
    assert len(bech32_decode(private.lower())) == 32
    assert len(bech32_decode(public)) == 32
    sig = _signing.sign(private, CANONICAL)
    assert _signing.verify(public, CANONICAL, sig) is None


def test_generate_keypair_is_fresh_each_call():
    assert _signing.generate_keypair()[0] != _signing.generate_keypair()[0]


def test_sign_rejects_a_public_key_as_private():
    with pytest.raises(ValueError, match="expected a absigsec1"):
        _signing.sign(PUBLIC, CANONICAL)
