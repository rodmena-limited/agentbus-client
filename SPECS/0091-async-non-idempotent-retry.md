# 0091 — Async client retries non-idempotent POSTs on 5xx

Ticket: issuedb #91.

EARS SPEC:
R1. When a non-idempotent async request receives a 5xx, the async client shall not retry it, matching the sync client (#45).
R2. The async path shall have the same test coverage as tests/test_non_idempotent_calls_are_not_retried.py.
R3. A 5xx that is not retried shall still count against the async breaker, and the caller shall see the original error.

REPRODUCED on published 0.9.105 and 0.9.106, real HTTP, a local server answering 503, default settings:
  sync POST   1 attempt
  async POST  4 attempts, no Idempotency-Key on any
  async GET   4 attempts
AsyncAgentBus.register takes this path, which is the duplicate-identity failure #45 describes.

TECHNICAL PROBLEMS:
1. The safe-to-repeat rule existed only in the sync path, as a copy that could drift.

SOLUTION DOMAINS:
- Safe retry of non-idempotent operations. The concept already exists here as `_NonIdempotentError`.

ALTERNATIVES:
- One classifier in `client/_repeat.py`, used by both clients [CHOSEN].
  A parallel async copy [REJECTED: twin copies are what produced this defect].

RESULTS, measured 2026-11-06: `probe_async_post_not_repeated.py` fails on 0.9.106 and passes on the fix with 1 attempt. Both directions are tested: a read and a POST with an idempotency key still make 4 attempts, on one key.
