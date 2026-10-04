# 0080 — Mutation round 2: tests that kill the high-severity survivors of round 1

Ticket: issuedb #80. Follows #73 (round 1, ledger audit/mutation/2026-10-round1.md).

## EARS SPEC

- For each high-severity gap in audit/mutation/2026-10-round1-triage.tsv, the suite shall contain a behaviour test that fails when that mutant is active, or the row shall be re-classified with a recorded reason.
- Tests shall exercise the real counterparty where one exists: real Ed25519 and age keys, the real AgentBus client over httpx.MockTransport, the real hook entry point, real sockets for reachability.
- Tests shall assert values (bytes, fields, exit codes, headers), not only the shape of containers.
- When the new tests land, a forkserver mutmut run on the 11 scoped modules shall report killed / (total - equivalent) >= 90% for sealing.py, _signing.py, hooks/_gate.py and cli/_sigline.py, and shall report the measured score for every module.
- No change to src/ behaviour in this ticket; a test that exposes a real defect gets its own ticket.


## TECHNICAL PROBLEMS

 1. oracle strength for crypto (known-answer vectors, tamper rejection); 2. HTTP contract assertions without a live server; 3. lazily-built singletons that tests must rebuild fresh; 4. filesystem permission and armor-format properties.

## SOLUTION DOMAINS

 mutation-driven test design; known-answer tests (RFC 8032 style); httpx.MockTransport (already in httpx, a dependency); real /usr/bin/age where installed.

## ALTERNATIVES

 per-module behaviour tests [CHOSEN] vs raising coverage with line-hitting tests [REJECTED: covering is not killing; round 1 showed 'covered' sync paths with 0 kills] vs deleting untested code [REJECTED: out of scope, behaviour change].

## RESULT (2026-10-04)

543 new tests (3007 -> 3550 in the suite), 19 files `tests/test_round2_*.py` plus regression tests
for #81-#85. Hand-applied mutation checks in scratch copies (bytecode caching off), by module group:

| group | tests | mutations applied by hand | turned red |
|---|---|---|---|
| _signing, sealing | 108 | 30 | 30 |
| client/sync_verify | 65 | 30 | 30 |
| hooks/_gate | 76 | 29 | 28 (1 equivalent: `break` after a 410) |
| identity, _reply_guard, attachments, _credentials | 180 | 21 | 21 |
| resilience, _sigline, cli/_read | 94 | 42 | 42 |

Real defects found and fixed: #81, #82, #83, #84, #85 (0.9.103).

NOT MEASURED: the >= 90% score target. That needs a mutmut run, which is manual only
(audit/mutation/README.md) and has not been run for round 2. Until it is, the score clause of
this spec is unverified.
