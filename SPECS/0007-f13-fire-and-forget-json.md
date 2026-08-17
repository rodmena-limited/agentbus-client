# F13 — emit accepted-status JSON for fire_and_forget send

Ticket: issuedb #7
Source: peer report (agentbus-ui-c760a1, batch #3, finding #13).

## EARS spec

- When `agentbus send --guarantee fire_and_forget --json` returns, the CLI
  shall emit a non-empty JSON object such as
  `{"status":"accepted","guarantee":"fire_and_forget"}`.
- If the server has nothing to return (no id, no delivery_count), then the
  CLI shall still emit valid JSON so consumers using `jq` do not crash on
  `{}`.

## Follow-up (not in this ticket)

`fire_and_forget` currently takes ~650 ms — investigate whether the client
is waiting on a server ack that defeats the guarantee. If so, that is a
separate ticket for the client, or a report to backend.
