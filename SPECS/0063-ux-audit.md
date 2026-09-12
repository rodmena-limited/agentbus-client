# 0063 — Adversarial UX audit of the agentbus client

Ticket: issuedb #63 (with #62, the acting-agent default, and #61, the audit harness)

## EARS spec

- If a tool call's input contains a string over the guard's 4096-character field limit, then the gate shall still obtain a verdict (head and tail sent with an elision marker) rather than degrade to an unvetted allow.
- If the guard answers 422, then the degraded record shall carry the server's validation detail.
- If a command receives malformed input that the client validates, then the CLI shall print at most 3 lines naming the problem and `agentbus <verb> --help`, exit 2, and never print a Python traceback (AGENTBUS_DEBUG=1 restores it).
- If no credential exists on the machine, then every verb shall print at most 3 lines naming `agentbus setup` and `agentbus signin`, exit 8.
- If the bus is unreachable, then the CLI shall name the URL it tried and how to change it, exit 3.
- Where --json is given, errors shall be one JSON object on stderr with code, detail and exit_code; exit codes unchanged.
- When `agentbus help [COMMAND...]` is run, the CLI shall print that help and exit 0 without adding a verb to `quickref --verbs`.
- The inbox listing shall not describe itself as "new messages" unless --unread was given, and shall say how to reach the next page and the unread-only view.
- Every option and argument of every verb shall carry help text, and command descriptions shall start with a capital letter.
- `agentbus-hook` run by hand shall say what it is and point at `agentbus`.
- The audit harness default run shall not touch the production bus (live probes opt-in).

## Confirmed findings

F1 HIGH  gate unvetted for tool_input > 4096 chars per field (/v1/guard/check 422 -> allow). Live: 5,092-byte payload -> 422 "tool_input['note'] is 5000 characters, over the 4096"; field: watch-status showed 2 degraded actions at 22:48Z.
F2 MED   `remind --delay soonish` -> Python traceback (ValueError from _parse_duration), rc 1.
F3 MED   no credential -> one 400-char line about operator credentials and registering agents, on every verb.
F4 MED   unreachable bus -> "error: [Errno 111] Connection refused" with no URL or remedy.
F5 MED   --json errors are plain text.
F6 MED   `agentbus help` / `agentbus help send` -> "there is no `help` command".
F7 MED   `inbox` lists the OLDEST 50 (2026-08-17 in this checkout) under help "list new messages"; empty page says "no new messages" regardless of filter.
F8 LOW   52 options/arguments without help text; 59/59 descriptions lowercase.
F9 LOW   bare `agentbus-hook` -> argparse usage wall with no statement of purpose.
F10 LOW  `thread <bad id>` prints its explanation plus a redundant second not_found line.
F11 LOW  audit/evaluations/run_all.sh claims no production bus but runs the live remind probe, which also reads the oldest 25 deliveries (#61).

## Technical problems

1. A security control that treats a contract violation (422) as an absence of a verdict.
2. Exceptions from client-side input validation escaping the CLI boundary.
3. First-run and failure messages written for the implementer, not for the person who hit them.
4. Help that exists as data but is not reachable by the usual reflex (`help`) and is incomplete.
5. A listing whose wording contradicts its ordering.

## Solution domains

- Input shaping against a server contract: send what the contract accepts, rather than skip the check.
- CLI error conventions: short, actionable, exit-coded messages; a debug switch for tracebacks; machine-readable errors under `--json`.
- Codebase patterns: Click root group (#60), `_common` resolution helpers, `ui.py` plain-when-piped.

## Alternatives

| Concept | Chosen | Rejected, and why |
|---|---|---|
| Oversized gate input | send the first and last 2,000 characters of each over-limit string with an elision marker | deny on 422 (a size limit would block every large write and hold sessions hostage, contradicting #107); keep allowing (the bypass stands); send only the head (a destructive tail escapes). Residual risk, stated: content in the middle is not inspected; raised with the server team (`01M2BXDQCTPZ4ANSJ51A7H39PK`). |
| Malformed input | a dedicated `InputError(ValueError)` caught in `main()` as a usage error (exit 2) | catching every `ValueError` (a JSON decode error from a bad server response would become exit 2, which the service unit treats as permanent and would stop a watcher restarting) |
| `help` | handled inside the root group, not registered as a verb | a real `help` verb (changes `quickref --verbs` and fails the server team's documentation ratchet) |
| Inbox ordering | honest wording plus next-page and unread hints; paging unchanged | newest-first by default (breaks every script paging from cursor 0) |
| Harness safety | the live probe runs only with `AUDIT_ALLOW_LIVE=1` | leaving it in the default run (it touches production and could not go green on an inbox of more than 25 deliveries) |

## Malformed-input sweep (2026-09-12)

692 command lines: every non-flag option of every verb given `soonish`,
`{not json`, `-5`, `../../etc/passwd`, an emoji, and an empty string, plus
garbage positionals; unreachable bus, throwaway HOME; `watch`, `watch-stop`,
`as` and `sibling` excluded because they start or stop processes. The first run
on the fixed tree still found 13 traceback kinds in five classes:

| Class | Cause | Fix |
|---|---|---|
| `tag` with no resolvable agent | the SDK raises `ValueError`; `tag` never used the #62 resolver | `tag` resolves through `require_acting_agent` |
| `undeliverable --limit <text>` | `int()` in the handler; the option was untyped in 0.9.94 too | `InputError` naming the flag |
| `sent --since <text>` | `datetime.fromisoformat` uncaught | `InputError` naming both accepted forms |
| `join` against an unreachable bus | `join` calls `urllib` directly and caught only `HTTPError` | `URLError` becomes a `TransportError` |
| non-ASCII agent name | httpx encodes headers as ASCII | names checked against `[A-Za-z0-9._-]+` on `--agent`, in `_bus()`, in the resolver, and on `$AGENTBUS_AGENT` |

The resolver also stopped swallowing `TransportError`: an unreachable bus is now
reported as that, not as "no acting agent". The full suite additionally caught
that `thread` must keep raising `NotFoundError` (`test_thread_both_unknown_raises_the_original`);
it now raises one carrying the explanation, printed once by `main()`.
