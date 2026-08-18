# Ack-tracking — require-ack send surface (client)

Thread: 01M097AQA9KVBTHFJZGSM1PN88
Greenlit by Farshid ("YES, proceed"). Split of concerns: server owns the
delivery_reminders table, sweep, status gate, reply-as-ack (atomic in
messages_service.reply), give-up emitter. Client owns the send-time flag,
batch parity, the reminders verb, and skill guidance.

## EARS spec

- When the user passes `--require-ack` to `agentbus send`, the CLI SHALL
  pass `require_ack=True` to the SDK, which includes `require_ack: true`
  in the `POST /v1/messages` payload.
- When `--ack-window <duration>` is also passed, the CLI SHALL parse the
  duration (`90m`, `2h`, `3d`, or bare seconds) into a timedelta and pass
  it to the SDK, which converts to `ack_window_seconds` in the payload.
  Default 24h when `--require-ack` is set and no window is given.
- The 168h server cap SHALL be enforced client-side too, so a caller gets
  a fast local error instead of a round-trip 422.
- `--require-ack` SHALL bind TO recipients only, NEVER CC (Farshid's
  decision). A cc recipient is copied for information and never obligated
  to ack.
- `AsyncAgentBus.send` SHALL accept `require_ack`/`ack_window` with parity
  to the sync client (no drift — the phonebook(label=) / derived_from
  lesson).
- `agentbus send-batch` SHALL accept `require_ack` and `ack_window` per
  JSONL item, with parity to cmd_send.
- Without `--require-ack`, the payload SHALL be clean (no require_ack, no
  ack_window_seconds) so old servers never see the fields.
- Forward-compatible: a server that predates the delivery_reminders table
  ignores these fields; the flag is safe to pass before the backend ships.

## NOT in this release (pending backend's endpoint)

- `agentbus reminders --owed` / `--owing` verb — needs the backend
  reminders endpoint shape defined.
- Skill guidance on WHEN to use --require-ack (the served skill is
  backend's lane).
- The 10m/30m/45m/2h/6h/24h/24h/24h reminder curve is server-owned.

## Wire format

The client sends `require_ack: true` and `ack_window_seconds: <int>` in
the message payload. Backend: please confirm this matches what your
delivery_reminders table expects, or flag the correction before the
matched drop.
