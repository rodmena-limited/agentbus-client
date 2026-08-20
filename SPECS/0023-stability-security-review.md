# SPEC 0023: Comprehensive stability / security review of agentbus-client

**issuedb ticket**: #23 (review) — findings filed as #24–#33
**status**: review complete 2026-08-20; findings open
**report**: `audit/review-2026-08-20-stability.md`
**probes**: `audit/evaluations/probe_*` (run `audit/evaluations/run_all.sh`; a FAIL is an open finding)

## EARS spec (the review itself)

- The review shall enumerate every race condition, TOCTOU, crash path, resource leak,
  security weakness, and code-quality defect found in agentbus-client, each with severity
  argued by customer harm, a concrete failure scenario, the smallest correct fix, and the
  class it belongs to.
- When a candidate defect can be reproduced locally (unit-level or against a local fake bus)
  without touching the production bus, the review shall reproduce it and mark it CONFIRMED;
  otherwise it shall be reported as SUSPECTED and never blurred with CONFIRMED.
- If a finding is already tracked as in-progress under issuedb #20 or #21, then the review
  shall reference the existing ticket instead of re-reporting it as new, and shall state
  whether the existing mitigation is adequate.
- The review shall prioritise the active wake path (watch → rewake → Claude Code hook → SDK
  resilience layer) because stale-monitor and network-drop crash reports originate there.
- The review shall close with the three-paragraph closing statement: what was exercised,
  what was not tested, what remains uncertain.
- Where a source file exceeds the 500-line soft / 550-line hard cap, the review shall list it
  as a code-quality finding.

## Invariants the findings turn into (durable; copy to tickets as they are fixed)

- When the resilience layer's outer deadline fires, the client shall stop issuing further
  attempts for that request and shall not block interpreter exit on abandoned work.
- When N consecutive retry-sequences fail transiently, both the sync SDK breaker and the
  rewake poll breaker shall OPEN; an open breaker shall surface as `TransportError`.
- If an SSE stream ends without an exception, then the watcher shall log it and back off
  exactly as for a dropped stream.
- If an inbox page does not advance the cursor, then `_drain` shall stop and release the lock.
- Before signalling a PID from a pidfile, the CLI shall verify the process is an agentbus
  watcher for that agent.
- Key material shall be created atomically (`O_CREAT|O_EXCL`, mode 0600) and a credential
  shall never be written world-readable, even transiently.
- Module-level resilience singletons shall be created exactly once per process.
- A delivery shall wake a session at most once across concurrent Stop-hook monitors.
