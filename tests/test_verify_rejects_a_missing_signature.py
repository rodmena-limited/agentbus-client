from __future__ import annotations

import pytest

from agentbus_client import _signing


@pytest.mark.parametrize("signature", [None, 123, b"absig1raw", ["absig1x"]])
def test_a_signature_that_is_not_text_is_a_bad_signature(signature: object) -> None:
    _private, public = _signing.generate_keypair()
    with pytest.raises(_signing.BadSignature):
        _signing.verify(public, b"payload", signature)


@pytest.mark.parametrize("public_key", [None, 7])
def test_a_public_key_that_is_not_text_is_a_bad_signature(public_key: object) -> None:
    private, _public = _signing.generate_keypair()
    signature = _signing.sign(private, b"payload")
    with pytest.raises(_signing.BadSignature):
        _signing.verify(public_key, b"payload", signature)


def test_a_real_signature_still_verifies() -> None:
    private, public = _signing.generate_keypair()
    _signing.verify(public, b"payload", _signing.sign(private, b"payload"))
