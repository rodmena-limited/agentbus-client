# Tag/label parse invariant + help-text clarification

Ticket: issuedb #14
Source: peer report (agentbus-ui-c760a1, thread
`01M06T6TQJ5T2MJKY7DR7A2TCH`) closing backend's #260.

## EARS spec

- When `agentbus tag <key[=VALUE]>` is invoked with a colon-namespaced
  key (e.g. `skill:r2-probe=value`), the CLI shall produce a
  deterministic label shape across every run: everything before the
  FIRST `=` is the key (colons are part of it), everything after is the
  value.
- The client shall pin this behaviour with a regression test that also
  proves `cmd_tag` and `cmd_register --label` produce identical shapes
  for identical inputs.
- The `agentbus tag` `--help` text shall explicitly document the two
  legal grammars (`skill:foo` = namespaced-key form; `skill=foo` =
  key=value form; `skill:foo=bar` = namespaced-key with value) so a user
  does not conflate them.

## Investigation notes

The peer reported two agents in the wild had different label shapes
stored against what looked like the same command:

  bikeroom:  `{"skill:r2-probe": <value>}`
  macbook:   `{"skill": "r2-probe"}`

Cannot reproduce from any current code path — both write sites use
`item.partition("=")` identically, and git history shows no other
implementation ever. The two shapes must have come from two DIFFERENT
inputs (`skill:r2-probe=X` and `skill=r2-probe`). No code fix required;
the invariant is now pinned and the help text warns against the
grammar-confusion that triggered the report.
