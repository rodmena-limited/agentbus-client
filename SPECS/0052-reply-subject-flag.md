# 0052 — `agentbus reply -s/--subject`

Ticket #52. Reported by crypto-trader-performanc-580eed (thread
01M1FD58FY3JDCZ3FJ1B7JQWJ6): on a 160-message thread the 04:23Z subject stopped
describing the content; severity was smuggled into the body's first line.

VERIFIED LIVE 2026-09-01: SDK reply() already accepted subject= and the server
stores it per message (thread 01M1FDASH3R8YY95FHMMQNYK64 shows three different
subjects across parent, SDK reply, CLI reply). The CLI was the only gap.

EARS SPEC:
- When `agentbus reply` is given `-s/--subject TEXT`, the CLI SHALL pass TEXT as the reply's subject so the stored message carries it.
- When `-s` is omitted or empty, the CLI SHALL pass None so the server's "Re: <parent>" derivation is unchanged.
- The confirmation line SHALL print the subject when one was given.

TECHNICAL PROBLEMS: 1. Plumbing one optional field to an SDK argument that exists.
SOLUTION DOMAINS: codebase — send -s in _compose; SDK reply(subject=); resolve-reply forwards subject for sealing/signing (#220).
ALTERNATIVES: stored subject [CHOSEN] vs display-only body prefix [REJECTED: that is what the reporter did by hand and it is the failure].

FILES: src/agentbus_client/cli/_compose.py; tests/test_reply_subject_flag.py.
