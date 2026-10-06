# 0088 — Self-reply text describes the pre-#379 server behaviour

Ticket: issuedb #88.

EARS SPEC:
- The --to-self help and the #53 guard's notes shall describe current server behaviour: since agentbus-8dc08d #379 (846c278, 2026-10-06 23:24Z) a reply to your own sent message addresses its original recipients and never you.
- The #53 guard shall remain, as defence against a server that resolves a reply to the replier alone.
VERIFIED LIVE 2026-10-07 via AgentBus.reply_recipients on own message 01M44CZD120ND7EZGB0GVM2GBV: plain and --all both -> [agentbus-8dc08d] (before #379: [me] and [me, agentbus-8dc08d]). Control (message from website-60c8ec) unchanged.
SYNTHESIS (localised docs fix): keep the guard (defence in depth), correct the user-facing claim.

## FIX

`--to-self` help (cli/_cmds_compose.py) and the #53 guard note (client/_reply_guard.py) now describe current behaviour. The guard is unchanged.

## VERIFICATION

Live resolver probe above (before/after #379). tests/test_self_reply_guard.py, the 0.9.94 parser oracle and the CLI help tests pass. Ships with the next release; not published on its own (help text only).
