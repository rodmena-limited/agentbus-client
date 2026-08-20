# SPEC 0023: Comprehensive stability / security review of agentbus-client

**issuedb ticket**: #23 (review) — findings filed as #24–#33
**status**: FIXED in 0.9.44 (branch fix/review-23-stability: 1cfe0cb, ea656a1, cb431b6) — every probe green
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

## Peer-review invariants adopted (agentbus-8dc08d review #269, C1–C6; all in 0.9.44)

- C1 — If the SSE stream ends without an exception, then the watcher shall back off and log
  like any drop (same as the #27 invariant above).
- C2 — When the drain thread and the main thread both publish the state file, the watcher
  shall serialise the writes and use per-writer temp names, so no reader ever sees a torn file.
- C3 — While an SSE stream is healthy (keepalives only), the watcher shall re-validate the
  inject socket on every frame and exit with DeadWakeSocket when it is gone.
- C4 — If the bus returns a REST-confirmed 401, 403 or 410, then the watcher shall stop with
  exit 8 instead of retrying forever.
- C5 — When the bus is unreachable, the PreToolUse gate shall answer within ~3s (1.5s TCP
  reachability check, 4s read budget, no retry on transport failures) and open its fast-fail
  circuit on the FIRST connect failure.
- C6 — The bare CLI shall resolve identity as the hooks do: $AGENTBUS_AGENT, then
  `.agentbus/agent`, then `.claude/settings.local.json`, then the signin default.

## Resolution (2026-08-20, unattended autonomous session)

- Findings #24–#34: fixed in commit 1cfe0cb; acceptance = `audit/evaluations/run_all.sh` 12/12 PASS.
- Peer C1–C6: fixed in 1cfe0cb; acceptance = `~/develop/agentbus/audit/evaluations/client/run_all.sh` 6/6 PASS
  against this tree (their harness bd089db prefers `$AGENTBUS_CLIENT_SRC/../.venv/bin`).
- Suspected items S1–S10, S13, S14 closed by the same commits; S11 (pending() echoes stdin) and
  S12 (wake is at-most-once by design) deliberately left as they are — recorded in the report.
- File-size cap: cli.py, onboarding.py, hooks/claude_code.py, watch.py, client/sync_misc.py split
  (ea656a1, cb431b6); largest source file is now 479 lines. `ruff check src` is clean.
- Release: 0.9.44.
