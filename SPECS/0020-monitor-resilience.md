# SPEC 0020: Monitor and Watch Resilience to Transient Failures

**issuedb ticket**: #20
**status**: in-progress

## Problem

When the LLM inference API (or any upstream dependency) dies with a transient
error (503, network timeout, etc.), a cascade kills the AgentBus plugin monitor:

1. Session restart or stray signal SIGTERMs the `agentbus watch` child
2. The monitor script sees exit status 143 and exits with `EXIT_STREAM_KILLED=6`
3. Claude Code reports "Monitor script failed (exit 6)", waking the session
   with a false failure notification
4. The operator has to manually restart everything

The monitor is the ONLY active wake path. It must survive transient failures
or the agent goes deaf.

## EARS Spec

- If the `agentbus watch` child exits 143 (SIGTERM from outside the monitor),
  then the `monitor script` shall RETRY with backoff instead of exiting
  `EXIT_STREAM_KILLED` (6), unless the session itself is tearing down.
- If the `agentbus watch` child exits with a transient error code
  (3=bus error, 5=ServiceUnavailable), then the `monitor script` shall treat
  it as retryable within the startup budget.
- While the CLI `main()` function is running `cmd_watch`, if a
  `ServiceUnavailable` or `AgentBusError` is raised, then the `CLI` shall NOT
  convert it to a terminal exit code; instead the Watcher reconnect loop shall
  handle it.
- The `monitor script` shall distinguish between a SIGTERM that is a session
  teardown (expected) and one that is transient/external (retryable).
- If the `monitor script`'s retry budget is exhausted, then the `monitor`
  shall PARK (sleep forever) instead of exiting non-zero, because a monitor
  exit wakes the session with a false failure report.
- The `cmd_watch` function shall catch any `AgentBusError` subclass (including
  `ServiceUnavailable`, `QuotaExceeded`) that escapes the Watcher's reconnect
  loop and convert it to a retryable exit instead of letting `main()` assign
  a terminal exit code.

## Changes

### 1. `cli.py` — `cmd_watch` resilience wrapper (this repo)

Wrap the `Watcher.run()` call in `cmd_watch` so any `AgentBusError` that
escapes (now or after future refactors) stays retryable:

- Catch `AgentBusError` (excluding `AuthError`) → return exit code 3
  (generic, retryable by the monitor script)
- Catch `AuthError` → return exit code 8 (terminal, not retried)

This prevents `main()`'s global handlers from converting watch-mode
exceptions into exit codes the monitor script doesn't recognize.

### 2. `agentbus-monitor.sh` — SIGTERM retry + park on exhaustion (agentbus repo)

- **SIGTERM (143) is retryable**: instead of exiting 6 immediately, treat it
  like any other crash — count it against the startup budget, reset if the
  stream ran > 60s, retry with backoff.
- **Park on exhaustion**: when the 5-attempt budget is exhausted, PARK
  (`exec sleep 2147483647`) instead of exiting 5. An exiting monitor wakes
  the session with a false failure; a parked one is silent and costs nothing.
  The diagnostic still goes to stdout before parking so the information is
  delivered.
