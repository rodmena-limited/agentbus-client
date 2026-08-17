# SEV-1 — watcher survives `concurrent.futures.TimeoutError` on py3.10

Ticket: (root-caused by macbook-admin-bd8e86 SEV-1 report,
thread `01M08ZBXDD8PQ9J70MM4VDBZR0`; escalated by Farshid).

## EARS spec

- When `_run_with_resilience` catches
  `concurrent.futures.TimeoutError` from `future.result(timeout=...)`,
  the client SHALL translate it into `TransportError` with the original
  exception preserved as `__cause__`. No caller shall ever see a raw
  `concurrent.futures.TimeoutError`.
- `Watcher._backoff_and_drain` SHALL be a total handler — any exception
  the drain raises other than `DeadWakeSocket` MUST be caught and logged;
  `time.sleep(delay)` MUST fire in a `finally` block so backoff always
  happens.
- `cmd_watch`'s startup `bus.whoami()` (a cosmetic state-file label
  lookup) SHALL NOT prevent the watcher process from launching. When it
  fails, the workspace label falls back to `unknown` and the watcher
  enters its backoff loop against the network.
- When a caught exception's `str()` is empty, the log line SHALL include
  `type(exc).__name__` so the signal is not silent.
- A regression test SHALL force-raise
  `concurrent.futures.TimeoutError` from `bus.inbox()` inside
  `_backoff_and_drain` and assert the watcher process survives and
  sleeps for the correct backoff step.

## Root cause

`_run_with_resilience` (client.py) called `future.result(timeout=...)`.
That raises `concurrent.futures._base.TimeoutError` on network stall. On
Python 3.10 that class does NOT subclass `OSError` (that changed in
3.11+ where CFT became an alias of the builtin `TimeoutError`). Every
downstream `except (AgentBusError, OSError, httpx.HTTPError, ...)` guard
in `watch.py` / `cli.py` / `onboarding.py` let it escape — INCLUDING
`_backoff_and_drain`, so the reconnect handler was the thing that
crashed during exactly the condition it existed to recover from.

Additional defects captured by the same log:

- `time.sleep(delay)` sat AFTER the drain in `_backoff_and_drain`,
  unguarded — any exception in the drain skipped the sleep too, so a
  failing drain turned an N-second backoff into a 0-second one and made
  the reconnect loop a 1Hz hammer.
- `_drain_async`'s `except Exception` printed `str(exc)`, which is `''`
  for `concurrent.futures.TimeoutError()` — one log line, no signal.
- Startup `whoami()` guard omitted `httpx.HTTPError` entirely.

## Deferred to 0.9.25

- Persist `_failures` across restarts + add jitter (macbook fix #6)
- Consumer of backend's `GET /v1/agents/{name}/health` for the canary
  distinguishing "watcher alive" from "agent alive" (macbook #8, ui #8)
- Reconnect-loop backlog-cap check (backend caveat A: notice
  "backlog was exactly 100" and re-open with the new cursor)
- Add Python 3.10 and 3.11 to CI (ui's clarification: the transition
  version is where the class is fragile)
- 24h simulated-outage chaos test against a connection-dropping proxy
  (ui #4)
