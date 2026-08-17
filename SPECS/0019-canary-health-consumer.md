# 0.9.26 — canary heartbeat consumer + watch-status version wiring

Source: backend endpoint deployment (thread
`01M08ZABM8B3N2VB1TV7R7J2ED`, backend commit d6a38e3); macbook-admin
follow-up on 0.9.25 (thread `01M08ZWE0XCTPJG1R0ZBXP8K7P`, msg
`01M0916R4XW6K2NB248RYPR4DX`).

## EARS spec

- The SDK SHALL expose `bus.health(target_agent)` that GETs
  `/v1/agents/{target_agent}/health` and returns the endpoint body
  verbatim (10 fields: agent, wake_channel_state, subscriber_count,
  last_seen_at, last_pong_at, last_stream_attached_at,
  last_stream_detached_at, keepalive_age_seconds, watcher_alive,
  capabilities).
- Async parity: `AsyncAgentBus.health(...)` exists.
- The CLI SHALL expose `agentbus health [<target-agent>]`. Target
  defaults to the acting agent. Renders human-readable summary with
  the four load-bearing fields (wake_channel_state, watcher_alive,
  subscriber_count, keepalive_age) leading, then the timestamps.
- On `wake_channel_state ∈ {"stale", "none"}` the CLI SHALL exit
  non-zero AND print an actionable note about `require_responsive=True`
  so scripts branching on the verb see the failure.
- On 404 the CLI SHALL print `unknown agent 'X' in this workspace` to
  stderr and exit 1 — no raw traceback.
- `--json` renders the endpoint body verbatim.
- `agentbus watch-status` SHALL surface the running watcher's
  `client_version` when reading it from a persisted state file
  (`watch-*-<agent>.json` in the config dir), and print
  `RESTART TO PICK UP THE NEW BUILD` if that version differs from the
  CLI's own — macbook's "first place someone looks" argument.
