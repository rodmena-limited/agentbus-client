# Re-anchor `notify_command` placeholder substitution invariant

Ticket: issuedb #12
Source: backend agent report (agentbus-8dc08d, thread
`01M06KND89GDQ7W9MEVJ97JRNK`, build 6874df2).

## EARS spec

- When `notify_command` formats a template, every DOCUMENTED placeholder
  (`{subject}`, `{sender}`, `{delivery_id}`, `{message_id}`,
  `{thread_id}`, `{agent_seq}`, `{direction}`, `{inbound_source}`,
  `{envelope_count}`, `{envelope_kind}`) shall substitute without
  KeyError.
- The client test suite shall include a regression test that pins these
  substitutions, so a future refactor cannot remove one silently.

## Rationale

Before the client extraction (#234, commit 58ec21d), the backend repo's
`audit/evaluations/probe_onboarding.py` checked "`{agent_seq}` is a real
substitution" by reading `sdk/agentbus_client/watch.py`. After the
extraction that path no longer exists in the backend repo — the check
was commented out. The invariant belongs here, in the client that owns
the placeholder set.
