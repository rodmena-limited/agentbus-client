# SPEC 0021: Comprehensive Reliability Audit & Hardening

**issuedb ticket**: #21
**status**: in-progress

## Context & Motivation

An external bank-grade audit revealed undisclosed reliability issues in `agentbus-client`.
Under the `mission-critical-audit` workflow, we conduct a full, multi-subsystem, falsification-driven audit across all client surfaces (CLI, sync SDK, async SDK, stream watcher, hook subsystem, rewake monitor, sealing/signing, error classification, and resource management).

## EARS Specification

- When any network error, timeout, rate limit (429), or service degradation (503/502/504) occurs across sync or async SDK/CLI paths, the client shall handle it deterministically according to its retry/breaker/failsafe policy without crashing, leaking unhandled exceptions, or deadlocking.
- If an SSE stream disconnects or encounters an error in watch/monitor modes, then the watcher shall back off with jitter, preserve cursor state, and resume without losing messages or abandoning the wake loop.
- When circuit breakers trip under sustained failures, the client shall enter half-open probe states after cooldown and resume full traffic once probes succeed.
- When an async client or sync client executes under concurrency, resources (semaphores, connections, sockets) shall be cleanly acquired and released without leaking tasks, descriptors, or state across event loops.
- All evaluation probes in `audit/evaluations/` shall execute cleanly and verify system invariants against real interfaces in both blocking and releasing directions.

## Audit Plan & Methodology

1. **Audit Evaluation Harness Fixes**: Ensure `audit/evaluations/` runner works reliably and probe imports are isolated.
2. **Subsystem Review 1: Sync & Async SDK Resilience (`client/sync_client.py`, `client/async_client.py`, `client/resilience.py`)**:
   - Inspect timeout handling, circuit breaker transition logic, thread-safety, event loop binding for semaphores/locks, connection reuse and pooling.
   - Sweep for unhandled exceptions in request pipelines (e.g. `httpx` exceptions, `asyncio.TimeoutError`, `concurrent.futures.TimeoutError`).
3. **Subsystem Review 2: Stream Watcher & Reconnection (`watch.py`, `cli.py` watch verb)**:
   - Verify SSE stream reconnection with backoff + jitter under all network drop / HTTP status code conditions.
   - Verify cursor advance vs. delivery handling (at-least-once delivery, duplicate suppression, dead wake socket recovery).
   - Check timeout in `--exec` commands, wake file append race conditions.
4. **Subsystem Review 3: Monitor & Re-wake Architecture (`rewake.py`, `hooks/claude_code.py`)**:
   - Verify Claude Code hook integration, Stop hook timeout bounds, background process isolation, signal propagation.
5. **Subsystem Review 4: Sealing, Signing, & Key Management (`sealing.py`, `_signing.py`, `identity.py`, `onboarding.py`)**:
   - Symmetric cryptographic round-trip verification with foreign payload injection.
   - Key rotation, revoked key detection, path traversal protections.
6. **Subsystem Review 5: Concurrency, Resource Leaks, and Deadlocks**:
   - Async context managers (`aclose`, `__aenter__`/`__aexit__`), memory spikes during large payload handling / attachments.
7. **Create New Audit Probes & Hardening Tests**: Add automated probes under `audit/evaluations/` and unit/integration tests in `tests/`.
