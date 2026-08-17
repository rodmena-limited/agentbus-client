# F11 — show plaintext attachment size in `agentbus show`

Ticket: issuedb #6
Source: peer report (agentbus-ui-c760a1, batch #2, finding #11).

## EARS spec

- When `agentbus show` renders an attachment header, the CLI shall report
  the recovered plaintext size, not the on-wire sealed size.
- Where useful, the CLI may additionally note the sealed byte size in
  parentheses or a debug field.

## Rationale

A 50 KB file (`51200` bytes plaintext) currently displays as `69675 bytes`
in the header — the sealed size after base64 + age overhead. A consumer
asking "how big is my file" gets the wrong answer.
