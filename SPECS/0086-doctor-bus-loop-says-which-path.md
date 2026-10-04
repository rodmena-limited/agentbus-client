# 0086 — doctor: the self-test is a bus loop, and says which path it took

Ticket: issuedb #86.

## SPEC AND EVIDENCE (from the ticket)

EARS SPEC:
- The doctor's self-test line shall be labelled 'bus loop', never 'smtp loop'.
- When the self-test message arrives and reads back, the doctor shall say which path it took: 'in-band, sealed' when the stored body is sealed, 'via the mail-api relay' otherwise.
- The doctor shall state that the self-test does not show that email from outside the bus arrives.
EVIDENCE: agentbus-8dc08d, thread 01M4466AWK1NYNWRWTZWVTD1HT (2026-10-04), measured against their code: on unsealed workspaces agent-to-agent delivery goes egress -> mail-api API -> Postfix (internal smtpd) -> mail-api inbound -> ingest (transport.py:283); on encrypted workspaces the mail mirror is suppressed and delivery is in-database (transport.py:134). Neither path is public SMTP ingress. Motivated by mail-api-f3dc60's 4 'lost' mails (thread 01M427BAPHET7R72897XDQ9XDX), which were policy refusals.
SYNTHESIS (localised): observable signal = the stored (raw) body of the self-test delivery is sealed or not; that is the server's own branch condition.

## FIX

The four self-test lines are labelled `bus loop`. On a readable arrival, `_loop_path` reads the delivery raw: a sealed stored body means in-band (sealed), otherwise the mail-api relay; a failed raw read says the path was not determined. A second line says the self-test does not show that outside email arrives.

## VERIFICATION

tests/test_doctor_bus_loop_says_which_path.py (sealed, unsealed, undetermined; all three fail on the old code). Live 2026-10-04 against the bus on encrypted workspace rodmena-test-02: `bus loop: OK (arrived and READABLE in 0.0s; in-band, sealed: it never left the bus)`. The unsealed (relay) path was not exercised live: no unencrypted workspace was available.
