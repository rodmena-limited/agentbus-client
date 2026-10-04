# 0085 — verify crashes with AttributeError when a signed delivery has no signature string

Ticket: issuedb #85. Found by the #80 round-2 tests.

## SPEC AND EVIDENCE (from the ticket)

EARS SPEC:
- If a delivery is marked signed but its signature or public key is missing or not a string, then verify shall report the signature invalid, never raise AttributeError.
REPRODUCED 2026-10-04: _signing.verify(pub, b'x', None) and (pub, b'x', 123) raise AttributeError; '' raises BadSignature. sync_verify.py:127 and async_misc.py catch only BadSignature.
FOUND BY: #80 round-2 tests (signing group).

## FIX

_signing.verify raises BadSignature for a non-text signature or key; the verify paths read the field with .get, so a missing field is invalid rather than KeyError.

## VERIFICATION

tests/test_verify_rejects_a_missing_signature.py and tests/test_round2_verify_signature.py::test_a_signed_delivery_with_a_missing_or_broken_signature_is_invalid. Each test was run against the unfixed code and failed there.
