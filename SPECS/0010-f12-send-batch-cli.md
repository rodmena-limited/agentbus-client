# F12 — `agentbus send-batch` CLI mode

Ticket: issuedb #10
Source: peer report (agentbus-ui-c760a1, batch #3, finding #12).

## EARS spec

- Where the user invokes `agentbus send-batch`, the CLI shall read one
  JSON object per line from stdin, each of the form
  `{to, subject, text, ...optional fields...}`.
- When `send-batch` runs, the CLI shall reuse a single sealing context, a
  single HTTP keep-alive connection, and a single auth setup across all
  sends in the batch.
- When each send succeeds or fails, the CLI shall emit one JSON result
  line per input line to stdout, in input order, so consumers can pair
  results with inputs.
- If a single send fails, then the batch shall continue with the
  remaining inputs (fail-per-line, not fail-fast) unless
  `--stop-on-error` is passed.
- The CLI shall achieve throughput bounded by network + server, not by
  process startup (target: >= 20 sends/s where server burst caps allow).

## Rationale

Per-invocation process startup (~600 ms) currently caps the CLI at
~1.6 sends/s, well under the server's 40-request burst cap. A batch mode
lets tests, CI fixtures, and non-agent scripts push near the network +
server ceiling.

## Deferred

Landing separately from the F7/F8/F9/F11/F13/F14 quick wins and the
coalescer.
