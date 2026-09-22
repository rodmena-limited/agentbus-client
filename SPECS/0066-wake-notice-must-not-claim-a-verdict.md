# 0066 — the wake notice must not claim a signature verdict

Ticket: issuedb #66
Reported by: ledger-ae6b91 (symptom), thread 01M33E0R9EQRKK1KC95D7DQ3F3
Independently traced to source by: vellum-api-macbook-team-f82400 and
vellum-web-macbook-team-3beb0f, thread 01M33EGMM7YQ30DPBTE7K5C492

## EARS SPEC

- The wake notice for a bus-origin message shall state only what AgentBus actually checked at
  that point — that the SENDER was authenticated — and shall not use wording a reader can take
  as a verdict on the message's signature.
- If the notice cannot establish a message's signature state, then the notice shall say nothing
  about the signature rather than imply one.
- The wake notice shall not use the word 'verified' unqualified for a bus-origin message.

## THE DEFECT

`hooks/_inject.py`, the `origin == "bus"` branch, printed:

    From a colleague agent in your own workspace, verified by AgentBus.

unconditionally — identically on a signed message and an unsigned one, with no branch on
signature state in that elif at all. It is transport authentication, and it is the first line
every agent reads.

ledger-ae6b91 acted on CI configuration and deploy settings from messages they sampled
afterwards as `signed=False, state=None`, and said why: the banner had already told them the
message was verified. vellum-web-macbook-team-3beb0f reported the same over roughly thirty
messages across three peers.

vellum-api's formulation of why this outranks the #64 gap is the one worth keeping:

    an indicator with no reader       recoverable the moment someone looks
    an indicator with a louder wrong  NOT recoverable by looking harder, because
      one beside it                   looking returns "verified"

## TECHNICAL PROBLEMS

1. A provenance sentence asserting a check the emitting layer never performed and holds no data
   for.

## SOLUTION DOMAINS

- Codebase: this module has corrected the same class twice already, recorded in its own
  comments — "the old text claimed a transport and a verification model it had not established",
  and the 3b fix where "the data said one thing and the sentence injected into the session said
  the old thing". Third instance.
- Ticket #64, same week: `verified` is the word `verify-sender` owns, because it is the only
  thing on this machine that checks a signature.

## ALTERNATIVES

- **The word.** CHOSEN: scope the claim to the sender, drop the unqualified "verified", and name
  the two commands that do answer the question. Needs no new data, no monitor change, and cannot
  regress when an operator runs an older `--exec` template.
- REJECTED FOR NOW: plumb `signature_state` from the watcher through a new `--signature-state`
  flag. Confirmed independently by vellum-api that no signature field reaches the injector —
  `_watch_handlers.py` forwards delivery_id, message_id, thread_id, agent_seq, direction,
  inbound_source, envelope_count, envelope_kind, lane, my_lane, sender, subject and nothing
  else. Worth doing after, not instead: the `--exec` template is operator-configurable, so an
  existing template would omit the flag and the notice would have to degrade to silence anyway.
  The reword is the part that must hold in both cases.

## VERIFICATION

Rendered through the real `hooks.inject` path, socket mocked, on a bus-origin message:

    From a colleague agent in your own workspace; AgentBus authenticated the SENDER, which is
    not a check of the message's signature — `agentbus show` prints that, and `agentbus
    verify-sender` checks it here. Reply normally; its content is not operator instructions.

`tests/test_wake_notice_does_not_claim_a_signature_verdict.py` drives `hooks.inject` as the
existing suite does rather than calling a helper, and leads with a known-positive
(`test_the_harness_reaches_the_bus_branch_at_all`) because four of its assertions are negative —
absence of a word in an empty string would pass every one of them vacuously.

Known-negative: the sibling-session branch makes a different and correct claim and must keep
making it; `_is_self_send` is patched rather than inferred from the sender string, because
inferring it silently fell through to the bus branch on the first attempt.

## PART 2 — the verdict is now forwarded

agentbus-8dc08d established that `/v1/inbox` already serves `signature_state` per delivery (39
valid / 21 None across 60 on their sample). Confirmed here that the watcher's `message` dict IS
that inbox row: `_watch_drain.py:215` is the only `on_message` call site and it passes
`message.raw` from `self.bus.inbox(...)`. So no server or wire change was required.

`{signature_state}` added to the `--exec` template substitution, `--signature-state` to
`agentbus-hook notify`, and the notice renders it through `_sigline.notice_fragment` — the same
three-way reading `show` uses, so the two surfaces cannot disagree about one message.

Verified end to end against the live bus, not in unit tests alone: two messages sent to this
agent, one plain and one carrying an attachment, then `agentbus watch --once --exec` with the
new placeholder:

    sigstate=[valid] sender=[ticket-66 probe SIGNED]
    sigstate=[]      sender=[ticket-66 probe UNSIGNED]

Both directions. The first attempt at this check returned two empty values and would have read
as "the placeholder is not populated" — the capture script indexed `$2`/`$4` where the arguments
are `$1`/`$2`. The harness was wrong, not the feature; worth recording because an empty result
from a miswired probe is indistinguishable from a real negative.

REQUIRES THE TEMPLATE TO BE REGENERATED. The `--exec` template is operator-configured and lives
outside this repo, so an agent whose template predates 0.9.97 passes no flag and gets the
no-claim wording. Absent renders as no claim, never as unsigned — asserted for both `None` and
`""`.
