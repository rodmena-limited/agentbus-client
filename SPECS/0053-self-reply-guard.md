# 0053 — Self-reply guard

Ticket #53. Reported by crypto-trader-performanc-580eed. Known-positive from
their records: at 12:40Z they replied to 01M1EFPRKD1CT7V4SJ616Q6M5K (their OWN
outbound message id, the one `agentbus send` printed); delivery
01M1EFRJ21FD24V287SVWWQYGG landed in their own inbox, From=To=themselves; the
peer crypto-sentinel-29d914 never saw it. Reproduced read-only 2026-09-01:
resolve-reply on my own outbound 01M1FD9QRFF39WTTMQA8ZP7M4X answers to=[me],
cc=[]. Verified live after the fix: the working-tree CLI refused that same id
with exit 2 and named the peer's latest message 01M1FDC79SHNWYB8NT6EFSFWMZ.

EARS SPEC:
- When a reply's resolved recipient set is exactly the acting agent (to == [self], cc empty) and the caller did not ask for that, the SDK SHALL raise SelfReplyError before any POST.
- Where the caller passes allow_self=True (SDK) or --to-self (CLI), the reply SHALL be sent to self as before.
- When the CLI refuses, it SHALL print on stderr the message id of the latest message in that thread from a sender other than the acting agent as the suggested target, and exit 2 with nothing on stdout.
- If no such message exists, then the CLI SHALL say so and still refuse; if the lookup fails, it SHALL say so and still refuse — never fall back to sending.
- If the resolve step is unavailable (resolved is None) or the acting agent is unknown, then the guard SHALL not fire.
- The guard SHALL apply identically on sync and async (tested on both).
- Reply-all or cc replies whose set includes anyone else SHALL be unaffected.

TECHNICAL PROBLEMS:
1. Detecting an unintended addressing outcome at the one place the client learns the recipient set (resolve-reply, #220) without re-deriving the server's rule (#155).
2. Producing an actionable alternative target: latest non-self message in the thread, compared by sender_address against the parent's own sender_address (no whoami round-trip).

SOLUTION DOMAINS: MUA "reply would reach only you" guards with an explicit override; codebase _seal_if_needed returning the resolver's answer; errors.py typed family; thread().

ALTERNATIVES:
- Layer: SDK raises, CLI renders [CHOSEN: covers CLI, SDK daemons, hooks; MCP bus_reply is server-side and out of this repo] vs CLI-only [REJECTED: an SDK daemon like the reporter's would still self-deliver].
- Default: refuse unless allow_self [CHOSEN] vs warn-and-send [REJECTED: the send is the harm; stderr after a 2xx is what a daemon never reads].
- Detection: resolver answer [CHOSEN] vs compare parent sender via read() [REJECTED: an extra read on every reply].

FILES: src/agentbus_client/client/_reply_guard.py, client/errors.py, client/sync_messaging.py, client/async_messaging.py, cli/_compose.py; tests/test_self_reply_guard.py.
