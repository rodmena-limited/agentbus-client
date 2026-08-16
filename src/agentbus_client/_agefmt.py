"""VENDORED COPY of agentbus.agefmt — see that file for the full reasoning.

The SDK ships independently of the server package, so it carries its own copy
rather than importing one. Both are byte-identical and both are verified by
tests/test_age_format.py against age(1) and against committed vectors, so a
drift between them fails the build rather than producing two incompatible
implementations.

The age v1 file format, X25519 recipients, in pure Python (#189).

WHY A STANDARD FORMAT RATHER THAN ONE OF OUR OWN. A bespoke scheme built from
the same primitives would have been less code. It would also be unverifiable in
the only way that matters: a format nobody else implements can only ever be
tested against itself, and "my encryptor agrees with my decryptor" is the
archetype-1 self-confirming failure — a symmetric round-trip error is invisible
to every test that uses both halves.

age v1 is specified, widely implemented, and `age(1)` exists as an independent
implementation we can drive. So this module is checked by encrypting here and
decrypting THERE, and vice versa. That is a cross-implementation proof.
See tests/test_age_format.py, which refuses to pass without the real binary.

WHY NOT SHELL OUT TO age(1) INSTEAD. The install contract is one curl command
and no complexity for users. Requiring every machine to have age(1) installed
breaks that. `cryptography` is already a dependency, so this costs users
nothing new.

WHY NOT SSH KEYS FOR v1. Farshid's instinct to reuse ~/.ssh was right about
adoption, and age itself supports SSH recipients. Two things made native age
keys the better v1: an SSH key is usually passphrase-protected and ssh-agent
CANNOT decrypt (it only signs), and Ed25519 needs an X25519 conversion that is
one more place to be subtly wrong. A dedicated key generated at signin has
neither problem. SSH recipients remain a clean follow-up — the format supports
them, so it is additive.

Format reference: https://age-encryption.org/v1
    age-encryption.org/v1
    -> X25519 <b64(ephemeral share)>
    <b64(wrapped file key)>
    --- <b64(header MAC)>
    <16-byte nonce><STREAM chunks>
"""

from __future__ import annotations

import io
import os

from cryptography.hazmat.primitives import hashes, hmac
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_V1 = b"age-encryption.org/v1"
_X25519_INFO = b"age-encryption.org/v1/X25519"
_CHUNK = 64 * 1024
_TAG = 16

# ------------------------------------------------------------------ bech32
# age keys are bech32 (age1… public, AGE-SECRET-KEY-1… private). Implemented
# here rather than pulled in as a dependency: it is thirty lines and adding a
# package to the install path for thirty lines is the wrong trade.

_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _polymod(values: list[int]) -> int:
    generator = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for value in values:
        top = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ value
        for i in range(5):
            chk ^= generator[i] if ((top >> i) & 1) else 0
    return chk


def _hrp_expand(hrp: str) -> list[int]:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _convertbits(data: bytes | list[int], frm: int, to: int, pad: bool = True) -> list[int]:
    acc = 0
    bits = 0
    ret: list[int] = []
    maxv = (1 << to) - 1
    for value in data:
        acc = (acc << frm) | value
        bits += frm
        while bits >= to:
            bits -= to
            ret.append((acc >> bits) & maxv)
    if pad and bits:
        ret.append((acc << (to - bits)) & maxv)
    return ret


def bech32_encode(hrp: str, data: bytes) -> str:
    values = _convertbits(data, 8, 5)
    checksum_input = _hrp_expand(hrp) + values + [0, 0, 0, 0, 0, 0]
    polymod = _polymod(checksum_input) ^ 1
    checksum = [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(_CHARSET[d] for d in values + checksum)


def bech32_decode(text: str) -> bytes:
    text = text.strip()
    lowered = text.lower()
    position = lowered.rfind("1")
    if position < 1:
        raise ValueError("not a bech32 string")
    hrp, payload = lowered[:position], lowered[position + 1 :]
    try:
        values = [_CHARSET.index(c) for c in payload]
    except ValueError as exc:
        raise ValueError("bech32 string has a character outside the charset") from exc
    if _polymod(_hrp_expand(hrp) + values) != 1:
        raise ValueError("bech32 checksum failed")
    return bytes(_convertbits(values[:-6], 5, 8, pad=False))


# -------------------------------------------------------------------- keys


def generate_keypair() -> tuple[str, str]:
    """A fresh X25519 keypair as age strings: (private, public)."""
    private = X25519PrivateKey.generate()
    raw_private = private.private_bytes_raw()
    raw_public = private.public_key().public_bytes_raw()
    return (
        bech32_encode("age-secret-key-", raw_private).upper(),
        bech32_encode("age", raw_public),
    )


def public_from_private(private_key: str) -> str:
    raw = bech32_decode(private_key)
    return bech32_encode(
        "age", X25519PrivateKey.from_private_bytes(raw).public_key().public_bytes_raw()
    )


def fingerprint(public_key: str) -> str:
    """A short, stable id for a public key, for display and matching.

    NOT a security boundary — it names a key so a client can find the matching
    private half without guessing, and so an operator can see at a glance
    whether the key on a machine is the one registered.
    """
    digest = hashes.Hash(hashes.SHA256())
    digest.update(bech32_decode(public_key))
    return digest.finalize()[:8].hex()


# ------------------------------------------------------------------ base64
# age uses unpadded standard base64 everywhere in the header.


def _b64(raw: bytes) -> str:
    import base64

    return base64.b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    import base64

    padded = text + "=" * (-len(text) % 4)
    return base64.b64decode(padded)


# ----------------------------------------------------------------- sealing


def _wrap_file_key(file_key: bytes, recipient: str) -> bytes:
    """One `-> X25519` stanza for one recipient."""
    recipient_raw = bech32_decode(recipient)
    ephemeral = X25519PrivateKey.generate()
    share = ephemeral.public_key().public_bytes_raw()
    shared = ephemeral.exchange(X25519PublicKey.from_public_bytes(recipient_raw))
    wrap_key = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=share + recipient_raw, info=_X25519_INFO
    ).derive(shared)
    wrapped = ChaCha20Poly1305(wrap_key).encrypt(b"\x00" * 12, file_key, None)
    return b"-> X25519 " + _b64(share).encode() + b"\n" + _b64(wrapped).encode() + b"\n"


def _header_mac(header: bytes, file_key: bytes) -> bytes:
    mac_key = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"", info=b"header").derive(file_key)
    signer = hmac.HMAC(mac_key, hashes.SHA256())
    signer.update(header)
    return signer.finalize()


def _stream(file_key: bytes, nonce: bytes, plaintext: bytes, *, encrypt: bool) -> bytes:
    """age's STREAM construction: 64KiB chunks, counter nonce, last-chunk flag."""
    stream_key = HKDF(algorithm=hashes.SHA256(), length=32, salt=nonce, info=b"payload").derive(
        file_key
    )
    aead = ChaCha20Poly1305(stream_key)
    size = _CHUNK if encrypt else _CHUNK + _TAG
    out = io.BytesIO()
    source = io.BytesIO(plaintext)
    counter = 0
    while True:
        chunk = source.read(size)
        remaining = source.read(1)
        source.seek(-len(remaining), io.SEEK_CUR) if remaining else None
        last = not remaining
        if not chunk and counter and not last:
            break
        chunk_nonce = counter.to_bytes(11, "big") + (b"\x01" if last else b"\x00")
        out.write(
            aead.encrypt(chunk_nonce, chunk, None)
            if encrypt
            else aead.decrypt(chunk_nonce, chunk, None)
        )
        counter += 1
        if last:
            break
    return out.getvalue()


def seal(plaintext: bytes, recipients: list[str]) -> bytes:
    """Encrypt once, wrap the file key once per recipient.

    THE COST GROWS WITH RECIPIENTS, NOT WITH FLEET SIZE. A three-recipient
    message carries three stanzas whether the workspace has five machines or
    five hundred. Sealing to every key in a workspace was considered and
    rejected for exactly this reason.
    """
    if not recipients:
        raise ValueError("at least one recipient is required to seal a message")
    file_key = os.urandom(16)
    header = _V1 + b"\n"
    for recipient in recipients:
        header += _wrap_file_key(file_key, recipient)
    header += b"---"
    header += b" " + _b64(_header_mac(header, file_key)).encode() + b"\n"
    nonce = os.urandom(16)
    return header + nonce + _stream(file_key, nonce, plaintext, encrypt=True)


class CannotDecrypt(Exception):
    """This identity is not a recipient of this message.

    Deliberately distinct from a corrupt-file error: 'not for me' and 'damaged'
    call for completely different actions, and a reader that cannot tell them
    apart will report the wrong one.
    """


def unseal(sealed: bytes, private_key: str) -> bytes:
    raw_private = bech32_decode(private_key)
    identity = X25519PrivateKey.from_private_bytes(raw_private)
    own_public = identity.public_key().public_bytes_raw()

    split = sealed.find(b"\n---")
    if not sealed.startswith(_V1) or split < 0:
        raise ValueError("not an age v1 file")
    body_start = sealed.find(b"\n", split + 1) + 1
    header = sealed[:split] + b"\n---"
    mac_line = sealed[split + 4 : body_start].strip()

    file_key: bytes | None = None
    lines = sealed[: split + 1].split(b"\n")
    for index, line in enumerate(lines):
        if not line.startswith(b"-> X25519 "):
            continue
        share = _unb64(line[len(b"-> X25519 ") :].decode())
        wrapped = _unb64(lines[index + 1].decode())
        shared = identity.exchange(X25519PublicKey.from_public_bytes(share))
        wrap_key = HKDF(
            algorithm=hashes.SHA256(), length=32, salt=share + own_public, info=_X25519_INFO
        ).derive(shared)
        try:
            file_key = ChaCha20Poly1305(wrap_key).decrypt(b"\x00" * 12, wrapped, None)
            break
        except Exception:
            continue
    if file_key is None:
        raise CannotDecrypt("no stanza in this message unwraps with this identity")

    if _header_mac(header, file_key) != _unb64(mac_line.decode()):
        raise ValueError("header MAC does not verify — the header was altered")

    payload = sealed[body_start:]
    return _stream(file_key, payload[:16], payload[16:], encrypt=False)
