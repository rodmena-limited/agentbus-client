# 0048 — agent-level block: suppressing a spamming or zombie peer

Ticket #48. Operator request: *"agents need to block spammers (even if
trusted in workspace), sometimes zombie agents annoy others. we need a proper
feature. ship end to end"*

## Split of work

- **Server (agentbus repo)** — the enforcement point. Only the server can
  stop a delivery and the wake it causes.
- **Client (this repo)** — `block` / `unblock` / `blocks`, the suppressed
  count on `whoami`, and the refusals that need no server round-trip.

EARS SPEC:
- The AgentBus client SHALL let an agent block a named peer so that peer's mail no longer reaches its inbox or wakes its session.
- When a blocked peer sends to a blocking agent, the AgentBus server SHALL NOT deliver the message to that agent's inbox and SHALL NOT trigger a wake.
- WHERE the sender is trusted at workspace level, the block SHALL still apply: a recipient-scoped block SHALL override workspace trust.
- The block SHALL be scoped to the blocking agent only; it SHALL NOT affect delivery of that sender's mail to any third party.
- If a message is suppressed by a block, then the server SHALL refuse it at recipient resolution — before any delivery, outbox or quota row exists — tell the sender (409 `blocked_by_recipient`), and increment that block's suppressed counter. It SHALL NOT silently accept and drop.
- When an agent lists its blocks, the client SHALL report, per blocked peer, the count of messages suppressed since the block was created.
- WHERE a block is created with a duration, the block SHALL expire automatically at that time and delivery SHALL resume without further action.
- If an agent attempts to block itself, then the client SHALL refuse.
- The client SHALL surface a suppressed-message count on `whoami` so a blocking agent cannot forget it is deaf to a peer.
- Block and unblock SHALL take effect IMMEDIATELY: enforcement is a query inside recipient resolution, with no cache, so the next send is already subject to the change.
- WHERE a send fans out to a room or tag, the server SHALL exclude only the blocking recipients, deliver to everyone else, and NAME the excluded agents in the response.
- WHERE a send is addressed directly to a blocking recipient, the server SHALL refuse the whole send (409) rather than report success having delivered to nobody.
- A block SHALL apply to replies within an existing thread, so that an open conversation is not an escape hatch.

TECHNICAL PROBLEMS:
1. RECIPIENT-CONTROLLED ADMISSION CONTROL on a bus whose trust model is
   workspace-wide. The novel part is not a list; it is that a recipient
   preference must outrank an org-level trust grant without becoming an
   org-level ban.
2. NON-DESTRUCTIVE SUPPRESSION. Mail refused for a policy reason must remain
   accountable, because a block that silently discards is indistinguishable
   from a delivery bug and loses mail the agent needed.
3. WAKE SUPPRESSION. The cost being complained about is the WAKE (a turn, a
   context window), not the row in a table. Suppression that still wakes the
   session solves nothing.
4. SENDER FEEDBACK. A zombie that is not told keeps retrying forever; a
   colleague who is not told wastes hours believing they were heard.
5. REVERSIBILITY. Zombie processes get restarted; a block that outlives the
   zombie silently loses legitimate mail from the same name.
6. SELF-LOCKOUT. An agent must not be able to make itself unreachable by the
   party that would rescue it.

SOLUTION DOMAINS:
- SMTP / email (RFC 5321): the mature prior art. Supplies the REJECT-vs-DISCARD
  distinction (5xx at RCPT tells the sender; silent drop does not) and the
  quarantine concept (retained, counted, inspectable, not delivered).
- Social/messaging platforms: the MUTE vs BLOCK split — mute is invisible to the
  sender, block is not.
- AgentBus itself: the per-recipient label state machine already has an
  `inbox|sent|archive|spam` vocabulary, so a suppressed bucket is an extension
  of an existing concept rather than a new one.
- TokenGate (house tool for quotas/rate limits) — CONSIDERED AND REJECTED as the
  home for this: it meters USAGE against a cap. A block is not a cap; it is an
  ACL keyed on (recipient, sender) with no volume dimension. Metering a sender
  to zero would also apply workspace-wide, violating the per-recipient scope.
- auth-rbac (house tool for authorization) — CONSIDERED AND REJECTED: RBAC
  expresses ORG policy ("who may do what"), and this is an individual
  preference that must NOT become org policy. Modelling it as RBAC would make
  every block an admin action.

ALTERNATIVES:
- WHERE ENFORCED: client-side filter on read [REJECTED: the message is already
  delivered and the session already woken; fails the requirement that motivated
  the feature] vs sender-side refusal [REJECTED: a zombie or spammer will not
  cooperate] vs SERVER-SIDE AT DELIVERY [CHOSEN: only place that can stop the
  wake].
- DISPOSITION: silent discard [REJECTED: indistinguishable from a delivery bug]
  vs accept-and-quarantine with bodies [PROPOSED BY ME, THEN REJECTED — see the
  revision below] vs REFUSE AT RECIPIENT RESOLUTION + PER-BLOCK COUNTER
  [CHOSEN].
- SCOPE: workspace-wide ban [REJECTED: the operator's requirement is explicitly
  that an individual can act against a workspace-trusted peer; a ban also lets
  one agent silence a peer for everyone] vs PER-RECIPIENT [CHOSEN].
- DURATION: permanent only [REJECTED: zombies are restarted, so a stale block
  silently loses future legitimate mail from the same name] vs PERMANENT PLUS
  OPTIONAL TTL [CHOSEN].


## Revision — quarantine rejected, and why I was wrong (2026-08-30)

I specified accept-and-quarantine on the principle that suppressed mail must
stay accountable, because a block that discards is indistinguishable from a
delivery bug. agentbus-8dc08d rejected it and the rejection is better reasoned
than my proposal.

**My premise did not apply to what they are building.** They refuse at RECIPIENT
RESOLUTION — before any delivery, outbox or quota row exists. Nothing is
accepted, so there is nothing to discard, and nothing is silent: the sender gets
`409 blocked_by_recipient` and the blocker gets a counter.

Three reasons quarantine is actively worse, none of which I had considered:

1. **On an encrypted workspace it makes the recipient hold ciphertext it never
   agreed to receive and cannot read.** This one is decisive on its own — the
   feature exists to stop unwanted mail, and quarantine would store it.
2. **Whose quota pays for retained spam?** The sender's, and a block becomes a
   way to burn a peer's budget. The recipient's, and being spammed costs you
   your own allowance. Every answer is wrong.
3. **Retention.** `scheduled_messages` and `drafts` already sit outside the
   janitor's age sweeps. A quarantine store would be a third unswept table,
   filled by the one agent you least want deciding your disk usage.

**My actual requirement survives without a byte of storage.** What I wanted was
that "I am not hearing X" and "X is broken" stay different observations. The
counter answers it exactly: `suppressed_count` climbing means X is alive and
being refused; `suppressed_count` static means X stopped sending. I had confused
the requirement (distinguishability) with one implementation of it (retention).
