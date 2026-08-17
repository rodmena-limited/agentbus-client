# Disk-bound key wins over env when agent is named (.env poisoning defense)

Ticket: issuedb #16
Source: backend agentbus-8dc08d root-cause diagnosis
(thread `01M08QS3M10M49WKT8WVX3P2P7`).

## EARS spec

- When `AgentBus(agent=NAME)` is constructed AND a bound key exists at
  `~/.config/agentbus/keys/NAME.env`, the client SHALL use the disk key
  regardless of the value of `$AGENTBUS_API_KEY`.
- When `AgentBus()` is constructed without a named agent, `$AGENTBUS_API_KEY`
  shall continue to win over disk (the operator-CLI path — `agentbus signin`,
  `agentbus register`).
- An explicit `api_key=...` constructor argument shall always beat every
  other source.
- When the caller names an agent AND neither disk nor env yields a key,
  the existing `AuthError("no API key for agent 'NAME'...")` continues to
  fire.

## Root cause

`resilient_circuit/storage.py` calls `load_dotenv()` at IMPORT time.
`find_dotenv()` uses `usecwd=False` by default, which walks UP from the
CALLING module's file — for a venv install that path is
`.venv/lib/python3.13/site-packages/resilient_circuit/storage.py`. So the
search walks:

  .../site-packages/resilient_circuit/ → .../site-packages → .../python3.13
  → .../lib → .venv → <project root>

Any `.env` in the project root containing `AGENTBUS_API_KEY=<other-key>`
therefore stomps `os.environ["AGENTBUS_API_KEY"]` before any AgentBus
code runs. If that stomped key is bound to a deleted workspace, every
downstream call sees `WorkspaceDeleted("this workspace has been
deleted")` — even though the correct freshly-minted bound key is sitting
on disk at `~/.config/agentbus/keys/<agent>.env`.

Any Rodmena operator/dev with a project-tree `.env` containing an
`AGENTBUS_API_KEY` for a different workspace (e.g. server-dev credential
in `~/develop/agentbus/.env`) hit this identically.

## Trade-off recorded

The old contract was "env-wins-over-disk when named". A legitimate
operator use case was: `export AGENTBUS_API_KEY=<test-key>` and act as
agent X with a temporary override. That path now needs an explicit
`--api-key` argument. Small ergonomic cost; silent `.env` poisoning was
catastrophic.
