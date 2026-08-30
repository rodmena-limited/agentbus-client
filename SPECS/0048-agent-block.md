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
- If a message is suppressed by a block, then the server SHALL retain it in a countable, inspectable form; it SHALL NOT be silently discarded.
- When an agent lists its blocks, the client SHALL report, per blocked peer, the count of messages suppressed since the block was created.
- WHERE a block is created with a duration, the block SHALL expire automatically at that time and delivery SHALL resume without further action.
- If an agent attempts to block itself, then the client SHALL refuse.
- The client SHALL surface a suppressed-message count on `whoami` so a blocking agent cannot forget it is deaf to a peer.
- Block and unblock SHALL each take effect within 60 s of the call returning (bounded by server cache TTL, not by session restart).

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
- DISPOSITION: silent discard [REJECTED: violates non-destructive suppression;
  indistinguishable from a bug] vs hard reject at send [REJECTED alone: loses
  the record on the recipient's side] vs ACCEPT-AND-QUARANTINE, counted and
  inspectable, no wake [CHOSEN], with the sender told their message was
  suppressed so a zombie can stop retrying.
- SCOPE: workspace-wide ban [REJECTED: the operator's requirement is explicitly
  that an individual can act against a workspace-trusted peer; a ban also lets
  one agent silence a peer for everyone] vs PER-RECIPIENT [CHOSEN].
- DURATION: permanent only [REJECTED: zombies are restarted, so a stale block
  silently loses future legitimate mail from the same name] vs PERMANENT PLUS
  OPTIONAL TTL [CHOSEN].
