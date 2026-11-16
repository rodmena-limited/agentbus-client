# #101: watch-check --json: holder kinds as fields

Ticket: issuedb #101 (open)

EARS SPEC (draft):
- Where --json is given, agentbus watch-check shall print {agent, free, holders: [{kind, pid, path|state_key, session}]} where kind is one of omo, session_monitor, watch.
- The client shall classify a registration as session_monitor when its state-file name carries a session uuid, and as watch otherwise.
- The text output and the exit codes (0 free, 4 held) shall not change.

Requested by agentbus-8dc08d (thread 01M7K0R7V99SD3YQ72KQ0K9JFE, message 01M7K6J657BQ4C724GDVNVZSKS): plugin 0.6.33 greps watch-check text for '^  omo session' and the session-uuid label, because exit 4 alone also counts a daemon watch, which wakes no Claude session.
