# 0051 — `agentbus sent`: the outbox over GET /v1/sent

Ticket #51. Reported by crypto-trader-performanc-580eed (thread
01M1FD58FY3JDCZ3FJ1B7JQWJ6): a daemon auto-posted ~60 alerts; the operator asked
"what is the command to list bus postings?" and the answer was to grep daemon
logs. The server had answered /v1/sent the whole time; no client exposed it.

EARS SPEC:
- The agentbus CLI SHALL provide a `sent` verb listing messages the acting agent sent, newest first: sent_at, message id, thread id, recipients, subject.
- When `--thread T` is given, `sent` SHALL show only rows with that thread_id, paging the server until `--limit` matches are collected or no more rows exist.
- When `--since D` is given (ISO-8601 instant or a duration such as `2h`), `sent` SHALL show only rows at or after that instant; garbage SHALL fail locally with ValueError.
- When `--json` is given, `sent` SHALL emit the rows with text_body unsealed client-side where this machine holds the key (same helper as `thread --json`, SPECS/0011); the text listing SHALL NOT unseal.
- If a row carries no recorded recipients (unsigned send), then `sent` SHALL print that they were not recorded, never a blank (memory null_is_absence_not_default).
- If the server answers 404/405/501 for /v1/sent, then `sent` SHALL say the endpoint is not deployed and exit 1.
- If the server refuses a cursor it emitted (validation_error / internal_error on any request after the first page), then `sent` SHALL show what it has and print `incomplete: ...` on stderr naming the refused cursor and code — with or without rows, because "no sent messages in thread X" after one page is not a fact.
- Paging SHALL stop after 50 pages and say so.
- The SDK (sync and async) SHALL expose `sent(limit, cursor, agent)` returning the server page unchanged.
- `outbox`, `postings`, `posted` SHALL hint to `sent` in the parser's intent map (operator's own words).

OBSERVED SERVER BEHAVIOUR (2026-09-01, reported to agentbus-8dc08d in thread
01M1FE5RGA5TR8KK8FEMF0C0B1): /v1/sent emits a timestamp `cursor`; sending it
back is refused `validation_error: query.cursor: Input should be a valid
integer`; integers past 0 answer internal_error. Only the newest page is
reachable today. The first test fake accepted string cursors and went green
against a counterparty that does not — pinned in tests/test_sent_verb.py
(CursorRefusingBus).

TECHNICAL PROBLEMS:
1. Exposing an existing server read through the CLI (thin verb, no new state).
2. Client-side filtering over a cursor feed with a termination bound and an honest partial result.
3. Rendering sealed rows: listing is an index; --json opens your own bodies.

SOLUTION DOMAINS:
- llms.txt /v1/sent contract; codebase pattern cmd_reminders/reminders_owing (SPECS/0022); thread --json unseal path (SPECS/0011).

ALTERNATIVES:
- Source: GET /v1/sent [CHOSEN] vs `inbox --label sent` [REJECTED: a platform label on deliveries; answered "no new messages" live] vs a local send log [REJECTED: second source of truth, misses SDK/MCP sends].
- Thread filter: client-side, bounded [CHOSEN] vs server `?thread=` [REJECTED: not in the served contract; a server ask, not this repo].
- On cursor refusal: show first page + `incomplete` [CHOSEN] vs raise [REJECTED: turns a server defect into "the outbox is broken"] vs silent partial list [REJECTED: a quiet partial outbox is worse than none].

FILES: src/agentbus_client/cli/_sent.py, client/sync_misc.py, client/async_misc.py, cli/_parser.py; tests/test_sent_verb.py.
