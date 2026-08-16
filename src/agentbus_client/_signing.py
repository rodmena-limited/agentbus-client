"""Per-message sender signatures (#173) — VENDORED COPY.

Byte-identical to src/agentbus/signing.py except for the import of the bech32
helpers, and a test fails the build if the two drift. The SDK ships alone, so it
carries its own copy for the same reason _agefmt.py does: a client whose crypto
depends on the server package being importable runs different code in a
developer checkout than a customer gets.


WHAT CHANGES. Until now the answer to "did this agent really send this" was
"the platform says so" — and that answer is genuinely strong: `sender_key_id` is
recorded from the AUTHENTICATED principal and never from the payload, and
`platform_attested` means a key bound to that agent alone. Agent-to-agent
spoofing was already impossible.

What did not exist is a claim a recipient can check WITHOUT trusting us. That is
the delta, and it is a trust-model change rather than a feature: a signature
moves "who sent this" from something we assert to something you verify.

WHY A SEPARATE KEY FROM SEALING. The sealing keys (#189) are age X25519, which
does key agreement and cannot sign. Ed25519 signs and cannot seal. Deriving one
from the other is possible and is a bad idea here: it couples the lifetime of
"who can read my mail" to "who can prove I wrote it", so rotating one silently
rotates the other. They share the key TABLE, told apart by `algorithm`.

WHAT IS SIGNED, and this is the part a second implementation has to match
exactly. The canonical form covers only fields the SENDER controls — a signature
over a server-assigned id or timestamp could never be produced by the client
that has to make it:

    agentbus-sig-v1\\n
    from: <sender agent name>\\n
    to: <recipients, sorted, comma-joined>\\n
    cc: <cc recipients, sorted, comma-joined>\\n
    subject: <subject>\\n
    priority: <urgent|normal|background>\\n
    body-sha256: <hex of the stored body bytes>\\n

BODY-SHA256 IS OVER THE STORED BYTES, not the plaintext, and that choice is
deliberate. On an encrypted workspace the stored body is age ciphertext, so:

  * the platform can still verify, because it holds exactly those bytes;
  * a recipient verifies the ciphertext BEFORE decrypting it, and then decrypts
    with its own key. Signature-over-ciphertext plus ciphertext-decrypts-to-X
    binds the signer to X as tightly as signing X would, because nobody else
    could have produced that signature over that ciphertext.

Signing the plaintext instead would make every signature unverifiable by the
platform on an encrypted workspace — an `unverifiable` on every message, which
is a state nobody reads after the first week.
"""

from __future__ import annotations

import hashlib

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from ._agefmt import bech32_decode, bech32_encode

SIGNATURE_VERSION = "agentbus-sig-v1"

# Bech32 like the sealing keys, and deliberately a DIFFERENT prefix: a human or
# a script that pastes a sealing key where a signing key belongs gets a parse
# error rather than a key that registers and then verifies nothing.
PUBLIC_PREFIX = "absig"
PRIVATE_PREFIX = "absigsec"
SIGNATURE_PREFIX = "absigv"


class BadSignature(Exception):
    """The signature does not verify. Distinct from 'no signature' on purpose:
    a bad signature is WORSE than none, because it looks like an attestation
    until someone checks."""


def _decode(text: str, expected: str) -> bytes:
    """Decode, CHECKING THE PREFIX, which bech32_decode does not do.

    It returns the payload for any well-formed bech32 string, so without this a
    sealing key (`age1…`) pasted where a signing key belongs decodes happily to
    32 bytes and is then used as an Ed25519 key. It would register, and every
    signature it made would fail to verify against it — a key that is wrong in a
    way that only shows up later, on someone else's read.
    """
    lowered = text.strip().lower()
    if not lowered.startswith(f"{expected}1"):
        raise ValueError(f"expected a {expected}1… key, got {lowered[:12]}…")
    return bech32_decode(lowered)


def generate_keypair() -> tuple[str, str]:
    """(private, public), both bech32. Generated locally, never transmitted."""
    private = Ed25519PrivateKey.generate()
    raw_private = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    encoded = bech32_encode(PRIVATE_PREFIX, raw_private).upper()
    return encoded, public_from_private(encoded)


def public_from_private(private_key: str) -> str:
    raw = _decode(private_key, PRIVATE_PREFIX)
    public = Ed25519PrivateKey.from_private_bytes(raw).public_key()
    return bech32_encode(
        PUBLIC_PREFIX,
        public.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        ),
    )


def fingerprint(public_key: str) -> str:
    """A short stable name for a key. Raises on anything that is not one, which
    is what makes registration reject a typo instead of storing it."""
    raw = _decode(public_key, PUBLIC_PREFIX)
    return hashlib.sha256(raw).hexdigest()[:16]


def canonical_bytes(
    *,
    sender: str,
    to: list[str],
    cc: list[str] | None,
    subject: str | None,
    priority: str,
    body: str | None = None,
    body_sha256: str | None = None,
) -> bytes:
    """The exact bytes both sides sign and verify.

    SORTED RECIPIENTS, because two clients that disagree on ordering produce two
    different signatures for one message, and the failure would look like
    tampering rather than like a bug — the worst possible way to be wrong about
    a security field.

    Every value is newline-terminated and the body is reduced to a hash, so the
    signed input is a fixed size regardless of a 10MB message.
    """
    # EITHER the body or its hash. A recipient verifying on an encrypted
    # workspace has read the PLAINTEXT — the client unsealed it — but the
    # signature covers the stored ciphertext, which it no longer holds. The read
    # surface publishes `body_sha256` over exactly those stored bytes, so the
    # verifier uses the hash directly and never needs the ciphertext back.
    #
    # This is also what stops verification from being a privilege: the hash is
    # on every read surface, so anyone who can see the message can check it.
    digest = body_sha256 or hashlib.sha256((body or "").encode()).hexdigest()
    lines = [
        SIGNATURE_VERSION,
        f"from: {sender}",
        f"to: {','.join(sorted(to))}",
        f"cc: {','.join(sorted(cc or []))}",
        f"subject: {subject or ''}",
        f"priority: {priority}",
        f"body-sha256: {digest}",
    ]
    return ("\n".join(lines) + "\n").encode()


def sign(private_key: str, payload: bytes) -> str:
    raw = _decode(private_key, PRIVATE_PREFIX)
    signature = Ed25519PrivateKey.from_private_bytes(raw).sign(payload)
    return bech32_encode(SIGNATURE_PREFIX, signature)


def verify(public_key: str, payload: bytes, signature: str) -> None:
    """Returns None on success, raises BadSignature otherwise.

    RAISES RATHER THAN RETURNING A BOOL. A boolean gets used in an `if` that
    someone eventually writes as `if verify(...)` with the call in a truthy
    position — and a function that returns None on success would then read as
    failure. An exception cannot be ignored by accident.
    """
    try:
        raw_sig = _decode(signature, SIGNATURE_PREFIX)
        raw_key = _decode(public_key, PUBLIC_PREFIX)
        Ed25519PublicKey.from_public_bytes(raw_key).verify(raw_sig, payload)
    except (InvalidSignature, ValueError, TypeError) as exc:
        raise BadSignature(str(exc) or "signature does not verify") from exc
