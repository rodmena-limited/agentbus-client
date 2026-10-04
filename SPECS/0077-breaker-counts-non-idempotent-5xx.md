# 0077 — Breaker never counts non-idempotent 5xx (contradicts resilience.py:353)

Ticket: issuedb #77. Found by the #73 mutation-testing round.

## SPEC AND EVIDENCE (from the ticket)

EARS SPEC:
- When a non-idempotent call fails with a 5xx, the SDK circuit breaker shall count it as a failure (as resilience.py:353 states), while the retry classifier still refuses to retry it.
DEFECT: _breaker_should_handle -> _is_transient_sdk_error returns False for _NonIdempotent (resilience.py:131).
REPRODUCED 2026-10-03 via _run_with_resilience, AGENTBUS_SDK_MAX_RETRIES=0, 8 calls raising AgentBusError(status=500): idempotent=False -> 8/8 reached the server, breaker never opened; control idempotent=True -> breaker OPEN after 5.
FOUND BY: #73 triage (resilience forkserver group).

## FIX

`_breaker_should_handle` unwraps `_NonIdempotent` to its original error before classifying; the retry classifier still refuses to retry it.

## VERIFICATION

tests/test_breaker_counts_non_idempotent_5xx.py (breaker opens after the failure limit; still one attempt per call; 4xx never opens it). Each new test was run against the unfixed code and failed there.
