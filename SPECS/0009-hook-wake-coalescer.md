# Hook-wake coalescer — leading-edge + trailing-window

Ticket: issuedb #9
Source: peer proposal (agentbus-ui-c760a1, test round batch #1). Farshid's
originating idea; peer's approach #3 selected.

## EARS spec

- When the client watcher receives a bus delivery and no coalescing
  window is currently open, the CLI shall fire the wake hook immediately
  (leading-edge) so a solo interactive message has no added latency.
- When a wake fires, the watcher shall open a trailing accumulation
  window (default 2500 ms) during which further deliveries are appended
  to an internal envelope buffer, not fired individually.
- When the trailing window closes with the buffer non-empty, the watcher
  shall fire the wake hook exactly once with an envelope payload
  containing `count` and `items[{id, subject, sender, thread}]`.
- If the message priority is `urgent`, then the watcher shall bypass the
  window and fire immediately, regardless of any open window.
- When the server releases a batch of held mail after a `dnd`->`online`
  status clear, the watcher shall route those deliveries through the
  coalescer rather than firing one wake per released message.
- Where the buffer exceeds a size cap, the watcher shall split into
  ordered envelopes rather than degrade to per-message wakes.
- The wake payload for a single message (`count == 1`) shall remain
  backwards compatible with the current per-message shape, or shall carry
  a `schema_version` field a hook can gate on, so installed hooks do not
  silently break.
- Where the user passes `--coalesce-window=<ms>` or
  `--coalesce-quiet=<ms>`, the watcher shall honour those values; the
  defaults shall be tunable per installation.

## Rationale

A 15-message test burst produced 15 near-identical `UserPromptSubmit`
wake blocks whose boilerplate outweighed the message bodies. Two peers
doing real work made each other's sessions unreadable. Server has no
business modelling downstream cadence; the bus is durable and
cursor-based, so a coalesced client-side wake never loses anything.

## Design decisions (recorded here so the next session can find them)

- Payload shape when `count == 1`: keep current per-message form (no
  `messages[]` envelope) so installed `UserPromptSubmit` hooks tuned to the
  single-message text do not break. Only fire the envelope shape when
  `count >= 2`.
- Envelope key names (confirmed with peer agentbus-ui-c760a1, thread
  01M06J67YSYZ65BATHNA3DEE0N):
  - `messages` (matches `agentbus inbox` output, no mental switch)
  - `count` (reads as English in a hook body)
  - Discriminator: `kind: "coalesced"` — a string field on the envelope,
    leaving room for future kinds (batch, digest, status_change) without
    rev-locking the schema
  - Per-message shape inside `messages[]`: match `agentbus inbox` JSON
    verbatim — `delivery_id, message_id, subject, sender_display,
    thread_id, priority` — one shape across the tool.
- Window opens on the first arrival, not on the wake — the wake IS the
  arrival for leading-edge; the timer starts at that instant.
- Buffer flush on watcher shutdown: flush pending deliveries synchronously
  before exit, so a graceful stop does not eat wakes.
- Defaults confirmed with peer: `--coalesce-window=2500ms`,
  `--coalesce-quiet=800ms`, urgent always bypasses.
