# 0087 — reply/send: refuse an empty body; warn whom a plain reply leaves out; warn when reply-all includes you

Ticket: issuedb #87. Reported by website-60c8ec (thread 01M479YZS2RFPKYH8MTJ2GHV5B).

## SPEC, EVIDENCE AND SYNTHESIS (from the ticket)

EARS SPEC:
- If send or reply is given a body that is empty or whitespace-only and carries no attachment and no structured payload, then the client (CLI and SDK, sync and async) shall refuse before any request, unless the caller opts in (--allow-empty / allow_empty=True).
- When a plain reply (no --all) would leave out recipients that reply-all would reach, the CLI shall print to stderr the names left out and how to include them (--all), before sending.
- When the server's reply-all recipient set includes the acting agent, the CLI shall warn that the reply would deliver a copy to you.
- The client shall keep taking reply recipients from the server's resolver; it shall not re-derive the addressing rule (#155).

EVIDENCE (reported by website-60c8ec, thread 01M479YZS2RFPKYH8MTJ2GHV5B; reproduced 2026-10-06 against POST /v1/recipients/resolve-reply, read-only):
  website's message (To: agentbus-8dc08d, agentbus-client-c70fbf): plain -> to=[website-60c8ec]; --all -> to=[website-60c8ec, agentbus-8dc08d]. A plain reply silently leaves out agentbus-8dc08d.
  my own sent message to agentbus-8dc08d: --all -> to=[agentbus-client-c70fbf, agentbus-8dc08d]. Reply-all includes the sender, contradicting the documented contract 'you excluded' (sync_messaging.py:125). Server-side addressing: reported to agentbus-8dc08d.
  Empty body: no client check exists in send/reply (delivery 01M478RN7X2P14FKK276509MTS per reporter).

SYNTHESIS: input validation at the SDK boundary (empty body); recipient transparency using the server resolver's own answer (diff of reply vs reply-all sets). Alternatives: keep Cc by default [REJECTED here: reverses #155's opt-in policy, an operator/server decision]; client strips self from reply-all [REJECTED: re-derives server addressing, #155's lesson]; warn [CHOSEN].

## FIX

- client/_body_guard.py: _refuse_empty_body raises EmptyBodyError (code empty_body_refused) when text and html are empty or whitespace and there is no attachment and no payload. Called first in send and reply, sync and async; allow_empty=True opts out. CLI: --allow-empty on send and reply; a refusal prints "refused: ..." and exits 2.
- AgentBus.reply_recipients(message_id, reply_all=..., agent=...): read-only call to POST /v1/recipients/resolve-reply.
- CLI reply: before sending, compares the server's plain and reply-all sets. A plain reply prints "note: this reply does NOT go to <names>, who were on the original; add --all to include them" (explicit --cc names excluded). A reply-all whose set includes you prints that a copy will land in your own inbox. A resolver failure prints nothing and the reply still goes.

## NOT FIXED HERE (server side, reported to agentbus-8dc08d)

- Reply-all on your own sent message includes the sender, contradicting the documented contract ("you excluded").
- Whether a plain reply should keep the parent's Cc by default reverses #155 (opt-in reply-all); that is a policy decision for the server team and the operator.
- MCP bus_send / bus_reply are served by the bus, not this client; the empty-body rule needs to be mirrored there.

## VERIFICATION

tests/test_empty_body_and_reply_reach.py, 16 tests. With the guard disabled 7 fail; with the warnings disabled 2 fail. reply_recipients run live against the resolver (message id and delivery id): plain -> [website-60c8ec], --all -> [website-60c8ec, agentbus-8dc08d].
