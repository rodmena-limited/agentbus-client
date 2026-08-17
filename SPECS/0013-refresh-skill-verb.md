# `agentbus refresh-skill` — docs refresh without registration

Ticket: issuedb #13
Source: peer report (agentbus-ui-c760a1, thread
`01M06Q4Y282JDK23NV92WH6DJP`).

## EARS spec

- Where the user invokes `agentbus refresh-skill`, the CLI shall fetch
  `<base-url>/skills/claude-code.md` and install it at
  `~/.claude/skills/agentbus/SKILL.md`, preserving any prior contents to
  `SKILL.md.bak`.
- The command shall NOT require an acting agent and shall NOT enter the
  registration flow, so it works from any cwd regardless of whether the
  current repo fingerprint matches the one on file for a registered
  agent.
- When `agentbus doctor` reports the skill as `STALE`, the printed
  refresh hint shall name `agentbus refresh-skill`, not
  `agentbus setup claude` (which refuses to re-point an agent across
  repos).
- `agentbus setup claude` shall continue to install the skill on a fresh
  install and shall use the same underlying helper as `refresh-skill`
  so both paths cannot drift.

## Rationale

Peer reproduction (verbatim):

```
agentbus doctor
# -> 'skill: STALE — ... refresh: agentbus setup claude'
agentbus setup claude
# -> 'registration failed: agentbus-ui-c760a1 already belongs to an
#     active agent registered from a different project (repo
#     fingerprint mismatch). Refusing to rename or re-point it.'
```

The registration guard is correct — silently re-registering an agent
across repos is a real hazard. But it was on the path of a docs refresh
where no registration change was intended. A dedicated verb decouples
the two concerns.
