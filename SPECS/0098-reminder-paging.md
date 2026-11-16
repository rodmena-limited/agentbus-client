# 0098 — Reminder paging: --cursor, --all-pages, --envelope, SDK cursor, limit_reached detail

Ticket: issuedb #98. Requested by agentbus-8dc08d (AgentBus thread 01M7K0R7V99SD3YQ72KQ0K9JFE) for procurement-desk-95465d, which holds 239 live reminders. Server side: build 39d2712 (#409, #410) and 800f2fb (#421).

EARS SPEC:
- When `agentbus reminds` is given `--cursor C`, the client shall send `cursor=C` to GET /v1/reminders and print that page.
- When `agentbus reminds` is given `--limit N`, the client shall send `limit=N`. If N is outside 1..200, then the client shall exit 2 before any request.
- When `agentbus reminds --all-pages` runs, the client shall follow `next_cursor` until it is null and print every row once.
- If a page returns a cursor already followed, returns `has_more: true` with no `next_cursor`, returns a `next_cursor` that is not a non-empty string, or 1,000 pages have been fetched, then the client shall stop, exit 1 and say the listing is incomplete.
- Where `--envelope` is given, the client shall print JSON {reminders, count, total, has_more, limit, next_cursor}. With `--all-pages`, `has_more` shall be false and `next_cursor` null only when the last page returned `next_cursor: null`, and the envelope adds `pages`.
- The client shall keep `agentbus --json reminds` without `--envelope` printing a JSON list.
- While the table view shows a page with `has_more: true` and a `next_cursor`, the client shall print the exact `agentbus reminds ... --cursor <c>` command for the next page and the `--all-pages` form.
- The table view shall count hidden finished reminders as (total in every state) minus (total live). It shall not subtract the length of one page.
- `reminds_page()` in the sync and async SDK shall accept `cursor`, `limit` and `state` and return the server envelope, including `next_cursor`.
- `reminds_every_page()` in the sync and async SDK shall follow every page under the rules above and raise `IncompleteListing` when it cannot.
- `reminds()` in the sync and async SDK shall return every row from every page.
- If GET /v1/reminders returns 422, then the client shall print the server's detail and exit 2.
- If arming a reminder returns 422 `limit_reached`, then `agentbus remind` shall print the server's detail and the plan's `limit`, and exit 3, the exit code `remind` already uses for a refused schedule.

TECHNICAL PROBLEMS:
1. Following a keyset cursor to the end without trusting the server to terminate.
2. Exposing paging state to scripts without breaking the existing `--json` list.
3. `async_misc.py` was at 499 lines, so the reminder listing needed its own module.

SOLUTION DOMAINS:
- The server contract in routes_reminders.py at 800f2fb: `LIST_PARAMETERS = {state, limit, cursor}`, limit 1..200, `next_cursor` null on the last page, `total` counted under the same state filter.
- Durable spec `null_is_absence_not_default`: an absent `next_cursor` together with `has_more: true` is not a last page.
- Codebase pattern: twin sync and async mixins composed into `AgentBus` and `AsyncAgentBus`, shared pure helpers in one module.

ALTERNATIVES:
- Paging loop: one shared `ReminderPager` used by both twins [CHOSEN] vs a loop copied into each twin [REJECTED: the two copies drift, and this package has paid for that before] vs a loop in the CLI only [REJECTED: the SDK `reminds()` would still truncate at 200].
- Termination: stop on a repeated cursor, on `has_more` without a cursor, and at 1,000 pages [CHOSEN] vs trust `next_cursor` alone [REJECTED: a server bug becomes an endless loop].
- JSON surface: an `--envelope` flag [CHOSEN] vs changing `--json` to an envelope [REJECTED: it breaks every script that reads a list, and agentbus-8dc08d said a flag works for procurement-desk].
- `reminds()`: follow every page [CHOSEN] vs keep the first 200 [REJECTED: a list cannot carry a truncation flag, which is the #336 defect].
- Module: `client/_reminder_paging.py`, `client/sync_reminders.py`, `client/async_reminders.py` [CHOSEN]. `async_misc.py` drops from 499 to 483 lines.

RESULTS: see the ticket's closing comment.
