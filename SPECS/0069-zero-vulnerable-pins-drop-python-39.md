# 0069 — zero vulnerable pins in the lock; drop Python 3.9

Ticket: issuedb #69
Reported by: infra-manager-c13110 (director requirement: zero open findings at every severity
before Cyber Essentials), delivery 01M3G62FYMSZR0T7GQEMMFBBGN, scan of main@f57baab8406c.
Decision: Farshid, 2026-09-27 ("yes, please fix and bump and do everything").

## EARS SPEC

- The uv.lock of every release shall contain no package version with a known vulnerability in
  OSV, at any severity.
- The OSV sweep of the lock shall include a known-vulnerable control, and shall report the sweep
  void if the control is not flagged.
- Where a supported Python version admits no fixed release of a dependency, the project shall
  not advertise support for that Python version.
- The lock shall record this project at the version pyproject declares.

## FINDINGS, REPRODUCED

`audit/evaluations/probe_osv_lockfile.py`, control `jinja2 2.10` flagged (12 advisories):

| package | locked | advisories | locked for |
|---|---|---|---|
| anyio | 4.12.1 | GHSA-5p39-cfhj-2xmp, GHSA-82r6-8w77-94w6 | python < 3.10 only |
| click | 8.1.8 | PYSEC-2026-2132 | python < 3.10 only |
| cryptography | 47.0.0 | 7 advisories | python 3.9.0 only |
| pytest | 8.4.2 | GHSA-6w46-j5rx-g56g, PYSEC-2026-1845 | python < 3.10 only |
| python-dotenv | 1.2.1 | GHSA-mf9w-mj56-hr94, PYSEC-2026-2270 | python < 3.10 only |

Each was a second copy kept for Python 3.9; the lock already held a clean version for 3.10+.

## WHY NO UPGRADE COULD FIX IT

From PyPI metadata: every fixed release of anyio (4.15.1+), click (8.3.3+), pytest (9.0.3+) and
python-dotenv (1.2.2+) requires Python >=3.10. The newest 3.9-compatible release of each is the
vulnerable one. cryptography 50.x excludes 3.9.0 and 3.9.1.

Not theoretical: 0.9.98 installed on CPython 3.9.25 runs, and resolves anyio 4.12.1, click
8.1.8 and python-dotenv 1.2.1.

## ALTERNATIVES

- CHOSEN: `requires-python = ">=3.10"`. The lock and what a consumer installs both become
  clean. Python 3.9 reached end of life in October 2025. 3.9 users are not broken: pip keeps
  them on 0.9.98.
- REJECTED: restrict only the lock via `[tool.uv] environments` and keep advertising 3.9.
  Provenance would read zero while every 3.9 consumer still installed the three vulnerable
  packages — the scanner satisfied, the risk unchanged. Infra advised against narrowing
  consumers "needlessly"; here the narrowing removes exactly the population that can only get
  vulnerable versions.
- REJECTED: keep 3.9 and justify the five findings (VEX). Fails the zero-findings requirement.

## ALSO FIXED

The released lock at f57baab recorded `rodmena-agentbus 0.9.96`; neither the 0.9.97 nor the
0.9.98 bump re-locked, so any SBOM generated from it misstated this component's version.
`tests/test_lock_records_the_declared_version.py` now fails on that drift. It was shown to fail
against the f57baab lock before being trusted.

A pyproject comment claimed the CI matrix "still RUNS pytest on 3.9". `tests/run_all_pythons.sh`
has run 3.10, 3.11 and 3.13 — never 3.9. The 3.9 promise had never been tested; corrected.

## HOUSE-LIBRARY FLOORS (operator request, same release)

Farshid: "ensure you have the latest ... stabilize, bulkman, resilient_circuit all have been
updated ... they have important fixes." This client does not depend on stabilize.

- `resilient-circuit>=0.8.6` (was `>=0.5`, locked 0.7.0) and `bulkman>=2.0.4` (was `>=2.0`,
  locked 2.0.3). A floor rather than a lock-only bump, because the floor is what consumers get
  and the operator named the fixes as important.
- The lock could not reach resilient-circuit 0.8.x before: bulkman 2.0.3 pins
  `resilient-circuit<0.8`; 2.0.4 widens it to `<0.9`.
- Exposure check for the 0.8.5 coroutine fix: resilient-circuit policies wrap only sync
  callables here (`client/resilience.py` SDK safety net, `rewake.py` poll); the async client
  uses `_AsyncCircuitBreaker` plus an asyncio.Semaphore bulkhead. So this client never relied
  on the broken path.
- House rule held: the single `BulkheadConfig` sets `circuit_breaker_enabled=False`.
- psycopg (pure, not binary) appears in the lock only via resilient-circuit's `[postgres]` extra,
  which this client never installs.

FOLLOW-UP, NOT DONE HERE: `_AsyncCircuitBreaker` exists because resilient-circuit was
synchronous-only. Since 0.8.5 it protects coroutines, so the hand-rolled async breaker could
move to the house library. That changes behaviour, and deserves its own ticket and tests
rather than riding on a dependency bump.
