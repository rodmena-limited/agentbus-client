# Personas — first-class responsibility lanes (POLICY)

Ticket: issuedb #17 (client side of backend #264)
Thread: 01M093JGVWQ2HXT3G6KJRG40T3
Decision: Farshid chose POLICY (server-enforced, admin-assigned)

## EARS spec

- Where the user passes `--persona <lane>` to `agentbus register` or
  `agentbus setup`, the CLI SHALL pass `persona` to the SDK's
  `register()` call, which includes it in the `POST /v1/agents/register`
  payload.
- The server enforces POLICY: only admin-scope keys can set persona;
  non-admin callers have it silently dropped (backwards-compatible, not
  rejected). The `--persona` flag's help text documents this.
- `agentbus whoami` SHALL display `persona: <lane>` when the server
  returns a non-null persona, and SHALL be silent when it does not
  (forward-compatible with pre-migration servers).
- `agentbus phonebook` SHALL display a persona column when at least one
  agent has a persona, and SHALL NOT display the column when nobody does
  (byte-identical to pre-persona output on old servers).
- The watcher's wake handler SHALL inject a `lane` field into a shallow
  copy of every message and envelope dict before sub-handlers see it.
  The field is absent when the acting agent has no persona.
- `notify_command` SHALL substitute `{lane}` in the --exec template.
  Empty string when no persona — never a KeyError.
- `agentbus-hook inject` SHALL accept `--lane <lane>` and, when set,
  append one reminder line to the injected body:
  "Your lane is: <lane>. This message may touch other lanes — if it
  does, HAND IT OFF (agentbus send tag:persona=<other> ...) rather than
  act outside your lane."
- The lane reminder SHALL appear at most ONCE per wake (one per
  envelope), never once per message — the coalescer and the `with_lane`
  wrapper ensure this.
- `AsyncAgentBus.register` SHALL accept `persona=` with parity to the
  sync client.

## Vocabulary

18-entry starter (workspace-configurable, regex `[a-z][a-z0-9-]{0,31}`):

  legal, privacy, security, audit, compliance,
  frontend, backend, database, mobile,
  data-engineering, data-quality, ml,
  infra, ops, docs, product,
  orchestrator, generic

## Admin enforcement

Backend (commit e8f9cf7, migration 01M0982RBKD6M7J9ESTGYWA5RK):
  - `agents.persona VARCHAR(32)` nullable + CHECK regex
  - `deliveries.handoff_from VARCHAR(32)` nullable
  - register accepts persona, admin-only enforcement
  - whoami, phonebook, agent_health return persona
  - messages accept handoff_from, persisted per delivery

Client: `--persona` on register/setup passes it through. Non-admin
callers get silent drop (documented in --help). The primary persona
assignment path is admin-side (dashboard or admin-scope key).

## Forward compatibility

Every client surface is forward-compatible:
  - `--persona` on register: ignored by old servers (unknown field)
  - whoami: persona line absent when server returns no persona field
  - phonebook: persona column absent when no agent has one
  - lane in hook: absent when agent has no persona
  - inject --lane: absent when plugin template doesn't pass {lane}

Nothing changes for existing users until the server and plugin are
updated. Everything lights up automatically when they are.