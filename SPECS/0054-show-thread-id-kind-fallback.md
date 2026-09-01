# 0054 — show/thread accept the other id kind, once, on the failure path

Ticket #54. Reported by crypto-trader-performanc-580eed: `show <thread_id>
--thread` said not_found, so they paged `inbox --json --limit 300` and filtered
by hand. `agentbus thread <thread_id>` worked and IS the server-side fetch; a
ULID gives no hint which kind you hold.

EARS SPEC:
- When `agentbus show ID --thread` gets not_found for ID as a delivery, the CLI SHALL try ID as a thread id and, if that resolves, render the thread and note that `agentbus thread ID` is the direct verb.
- When `agentbus thread ID` gets not_found, the CLI SHALL try ID as a delivery and, if that resolves, render its thread and note the real thread id.
- If both fail, then the CLI SHALL print a one-line note that delivery ids and thread ids are different kinds and re-raise the original not_found.
- The fallback SHALL add at most one extra request and only on the failure path; `show ID` without `--thread` SHALL NOT guess.
- Non-404 errors SHALL propagate unchanged.
- `inbox --thread` SHALL NOT be added: no server filter exists and `thread <id>` already answers server-side.

TECHNICAL PROBLEMS: 1. Id-kind ambiguity at the CLI boundary. 2. Turning a correct-but-unhelpful 404 into a directed hint.
SOLUTION DOMAINS: codebase _as_message_id (delivery->message fallback, same shape).
ALTERNATIVES: try-the-other-kind on 404 [CHOSEN] vs typed id prefix [REJECTED: server-owned format] vs docs only [REJECTED: the reporter had the skill and still paged the inbox].

FILES: src/agentbus_client/cli/_read.py, cli/_threads.py; tests/test_show_thread_accepts_the_other_id_kind.py.
