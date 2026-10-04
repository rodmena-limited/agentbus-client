# 0084 — attachment(agent=X) and read(agent=X) unseal with the client's own agent, not X

Ticket: issuedb #84. Found by the #80 round-2 tests.

## SPEC AND EVIDENCE (from the ticket)

EARS SPEC:
- When attachment() or read() is called with agent=X, the client shall unseal with X's keys, the same agent it fetched as.
DEFECT: sync_verify.py:265 unseals with self.agent; read() -> unseal_message uses self.agent. An operator client (agent=None) cannot open X's sealed attachment although X's key is on disk.
FOUND BY: #80 round-2 tests (verify group).

## FIX

unseal_message takes an optional agent; read() passes the agent it fetched as; attachment() checks and unseals with `agent or self.agent`; sync and async.

## VERIFICATION

tests/test_read_and_attachment_unseal_as_the_named_agent.py (operator client opens a named agent's sealed body and attachment; own agent still works; an agent without the key gets sealed_unreadable; async twin). Each test was run against the unfixed code and failed there.
