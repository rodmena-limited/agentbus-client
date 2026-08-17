# F14 — reword `verify-sender` UNSIGNED line

Ticket: issuedb #8
Source: peer report (agentbus-ui-c760a1, batch #3, finding #14).

## EARS spec

- When `agentbus verify-sender` describes an unsigned message, the CLI
  shall not repeat the word "unsigned" across an em-dash separator.
- The message shall lead with reassurance so an operator does not read
  "UNSIGNED" as a defect (e.g. `UNSIGNED — no signature attached to
  verify`).

## Rationale

Current output opens with `UNSIGNED — unsigned` which looks like a display
glitch and reads as failure rather than a benign "sender did not sign".
