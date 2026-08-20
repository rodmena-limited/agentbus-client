# Async client resilience gap — breaker + outer deadline (client)

Ticket: issuedb #18. Found in round-3+ audit (lower blast radius than the
SEV items, but a real gap). This is client-side only; no wire/contract change.

## The asymmetry

The **sync** client (`_run_with_resilience`, client.py) runs every SDK request
through three layers:

    bulkman  <-  resilient_circuit  <-  actual httpx call

- retry-with-backoff (0.5s..8s exp, jitter, max_retries=3)
- a **circuit breaker** (5/5 fail open, 2/2 success close, 30s cooldown) so a
  *sustained* outage opens the breaker and further calls fail fast instead of
  hammering the recovering bus
- **an outer wall-clock deadline** `future.result(timeout=call_timeout + 5)` so
  the whole retry sequence is bounded by the caller's timeout + 5s

The **async** client (`_run_with_resilience_async`, REG-7) hand-rolls retry +
a per-loop bulkhead, but has **no breaker and no outer deadline**. During a
sustained outage every async call independently runs its own 3 retries at full
concurrency (up to the semaphore cap), with no memory of earlier failures and
no upper bound except the retry count — it never fails faster as the outage
persists, unlike the sync client. (resilient_circuit is sync-only, so the
breaker can't be shared directly.)

## EARS spec

- When an `AsyncAgentBus` request fails with a transient error (transport /
  5xx) during a sustained bus outage, the async resilience layer SHALL NOT
  retry at full concurrency without remembering prior failures.
- SHALL open a circuit breaker after N consecutive failing retry-sequences so
  further calls fail fast during the cooldown (30s) instead of hammering the
  recovering bus, and SHALL close it again after clean successes.
- SHALL bound the whole retry sequence by a wall-clock deadline equal to the
  caller's timeout + 5s, raising `TransportError` with a deadline message
  (mirroring the sync `call_timeout+5`).
- `AGENTBUS_SDK_RESILIENCE=0` SHALL keep disabling the resilience layer.
- The REG-10 per-loop bulkhead SHALL be preserved (no `RuntimeError` across
  event loops).
- Tuning knobs SHALL mirror the sync names (`AGENTBUS_SDK_MAX_RETRIES`,
  `AGENTBUS_SDK_CB_COOLDOWN`) with the same defaults.

## Acceptance

- Sustained-failure: after N consecutive failing retry-sequences the breaker
  opens; a call during cooldown fails fast (no retry); after clean successes
  it closes again.
- Outer deadline: a retry sequence that exceeds `call_timeout + 5` raises
  `TransportError` naming the deadline.
- New unit tests pin both behaviours; existing async tests stay green.

## Tests

See `tests/test_async_resilience.py`.

## Addendum — knob parity, 0.9.43 (reliability audit follow-up)

- `AGENTBUS_SDK_CB_FAILURE_LIMIT` / `AGENTBUS_SDK_CB_SUCCESS_LIMIT` are now
  read by BOTH breakers via the shared `_sdk_cb_limits()` (resilience.py). They
  were previously honored only by the async breaker; the sync breaker hardcoded
  5/5 and 2/2 (A1). One operator setting now tunes both surfaces.
- `AGENTBUS_SDK_MAX_QUEUE` applies to the SYNC bulkhead (bulkman queue) only.
  The async client has NO queue by design: its concurrency is a blocking
  `asyncio.Semaphore` bounded by `AGENTBUS_SDK_MAX_CONCURRENT`, so callers
  beyond the cap wait for a slot (bounded by the outer deadline) rather than
  queuing (A2, documented divergence, not a gap). Operators of the async SDK
  bound fan-out with `AGENTBUS_SDK_MAX_CONCURRENT`, not `_MAX_QUEUE`.
