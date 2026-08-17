# F7 — pre-upload attachment size check

Ticket: issuedb #4
Source: peer report (agentbus-ui-c760a1, batch #2, finding #7).

## EARS spec

- When `agentbus send` is invoked with attachments, the CLI shall check
  each attachment file size against the known 10 MiB per-attachment cap
  before opening any upload.
- If any attachment exceeds the cap, then the CLI shall fail fast with a
  clear error naming the offending file and its actual size, and shall
  not seal or upload anything.

## Rationale

Currently the client streams ~11 MiB through the sealing pipeline and the
network before the server returns 413 (~53.7 s wasted per rejected send).
The server-side cap is documented at 10,485,760 bytes; the client knows
it and can fail immediately.
