# F8 — add `--all` to `agentbus attachment` (+ skill docs)

Ticket: issuedb #5
Source: peer report (agentbus-ui-c760a1, batch #2, finding #8).
Farshid asked for this by name.

## EARS spec

- Where the user passes `--all` to `agentbus attachment DELIVERY_ID`, the
  CLI shall fetch every attachment on that delivery.
- When fetching with `--all`, the CLI shall write each attachment into the
  current working directory using its original filename.
- If any target file already exists, then the CLI shall refuse to
  overwrite unless `--force` is passed.
- The `agentbus` skill `SKILL.md` shall document `--all` so newcomers do
  not roll their own loop.

## Notes

Existing single-index form (`-i INDEX`) stays the default. `--all` and
`-i` are mutually exclusive.

## Split with backend

The client side of the spec (CLI flag) is complete. The skill-doc side lives
on the server: the `agentbus` skill is served from
`https://agentbus.rodmena.co.uk/skills/claude-code.md`, not shipped by this
client. Message to backend at release time asks them to add `--all` to the
served skill so the docs and the CLI ship together.
