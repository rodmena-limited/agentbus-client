# 0.9.25 — startup drain deferred + persisted backoff + immediate version stamp

Source: macbook-admin-bd8e86's blackhole-URL live-fire test against 0.9.24
(thread `01M08ZWE0XCTPJG1R0ZBXP8K7P`, follow-up
`01M090J44EAYQ496DR95CCXBWX`).

## EARS spec

- When `Watcher.run()` starts and its startup drain raises any exception
  other than `DeadWakeSocket`, the watcher SHALL defer the drain,
  print `agentbus watch: startup drain deferred (<type>); entering
  reconnect loop` to stderr, and enter the reconnect loop rather than
  exit. A blackhole `base_url` must not prevent the watcher from
  launching.
- The watcher SHALL persist `_failures` and `_last_failure_at` to the
  state file after every backoff step, so an OS-supervisor
  crash-and-restart during an outage resumes the ladder at the correct
  step rather than resetting to 1s.
- The watcher SHALL reset `_failures` to 0 when the persisted
  `last_failure_at` is older than `_FAILURES_TTL_SECONDS` (default
  900 s) — otherwise a long-uptime watcher would restart its next
  reconnect at some high step for no reason.
- The watcher SHALL apply `_BACKOFF_JITTER_FRACTION` (default ±15%)
  jitter to each sleep so N watchers coming back after a shared bus
  restart do not reconnect in lockstep and trip the server's 30 QPS
  bulkhead.
- The watcher SHALL write its state file at startup (before entering
  the loop or any network call), so `client_version` in the persisted
  state is fresh on process start rather than lagging until the next
  cursor advance.
- The successful stream open SHALL persist the reset (`_failures = 0`,
  `_last_failure_at = 0.0`) so a subsequent restart does not reload
  stale failures.
- `agentbus watch`'s startup banner SHALL include the running client
  version so a stale watcher is self-evident in the log.
- `agentbus watch-status` SHALL surface the running watcher's
  `client_version` when it differs from the CLI's own version, so a
  post-upgrade "same process is still running old code" case is
  detected at the first place someone would look.

## Root cause of the 0.9.24 startup miss

`Watcher.run()` had:

  with self._drain_lock:
    delivered = self._drain()      # NETWORK CALL, UNGUARDED
  ...
  while True:
    try:
      self._stream_once()          # only THIS was in the reconnect envelope
    except Exception as exc:
      self._backoff_and_drain(...)

The startup drain sits OUTSIDE the try. 0.9.24's fix #1 translated
`concurrent.futures.TimeoutError` to `TransportError`, and the CLI's
existing `except AgentBusError` handler catches it — so the exit is now
clean (exit 3 with a formatted message) instead of a raw traceback. But
the process still exits. macbook: "the failure got prettier, not
survivable."

## Also-shipped correction

The rollout instruction for the fleet was BROKEN in two forms
(`pip install -U` and `uv tool upgrade rodmena-agentbus`) — both
reported success while leaving the running binary on the pre-fix
version. The only form that reliably upgrades a `uv tool install` box
is:

  uv tool install rodmena-agentbus@latest
  agentbus --version    # MUST print 0.9.25

Documented in the fleet notice for this release.
