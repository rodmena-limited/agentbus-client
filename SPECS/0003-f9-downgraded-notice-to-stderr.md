# F9 — route "downgraded to unsigned" notice to stderr

Ticket: issuedb #3
Source: peer report (agentbus-ui-c760a1, test round batch #2, finding #9).

## EARS spec

- When `agentbus send` emits informational notices such as
  "message downgraded to unsigned", the CLI shall write them to stderr,
  not stdout.
- If `--json` is passed, then stdout shall contain only the JSON block,
  so consumers using `| jq` or `| python -m json` parse successfully.

## Reproduction (before fix)

```
$ agentbus send self -s x -b hi --json | jq .
agentbus: message downgraded to unsigned. agentbus-sig-v1 only covers plain text,
but html, attachments, or a payload was present.
{"id":"...", ...}
```

`jq` fails: the notice above the JSON is not valid JSON.
