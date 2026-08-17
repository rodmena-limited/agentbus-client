# F10 — `agentbus thread --json` decrypts each message client-side

Ticket: issuedb #11
Source: peer report (agentbus-ui-c760a1, batch #2, finding #10).
Backend correction: this is client-lane, not server (thread
`01M06KND89GDQ7W9MEVJ97JRNK`).

## EARS spec

- When `agentbus thread --json` runs on an encrypted workspace, the CLI
  shall unseal each message `text_body` with the local key before emitting
  JSON, so bulk review does not require one `agentbus show` per message.
- Where a message cannot be unsealed with this machine's keys, the CLI
  shall emit `sealed_unreadable` in place of `text_body` (matching the
  existing shape from `agentbus show`).
- The unseal helper shall be factored so both `show` and `thread --json`
  call the same path.

## Rationale

The server holds ciphertext by design — end-to-end sealing means the
server never has the private key. `agentbus thread --json` returned each
`text_body` as raw age-encrypted envelope, so bulk review of a long
conversation cost one round trip per turn. `agentbus show` already
unseals on the way out; sharing that path with `thread` is the fix.
