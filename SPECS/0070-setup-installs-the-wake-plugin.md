# 0070 — `setup claude` installs the wake plugin, or says loudly that it did not

Ticket: issuedb #70
Reported by: infra-manager-c13110 (apidays demo box, rodmena-agentbus 0.9.99), threads
01M3QG03T1RRESHRVHZAYV54PA / 01M3QG0K9ZEPF7J4N2S1QXZDHV. The director asked for the root cause to
be fixed rather than worked around.

## EARS SPEC

- When `agentbus setup claude` runs and the agentbus plugin is not enabled, setup shall install and
  enable it through Claude Code's own plugin CLI (marketplace add; install agentbus@rodmena
  --scope user).
- When setup reports the wake as provided by the plugin, it shall have verified that by re-reading
  Claude Code's settings after the install, not from the installer's exit status.
- If the plugin cannot be installed or verified, then setup shall say so, shall not print "the
  monitor arms at session start", shall name the exact commands, and shall exit non-zero.
- Setup and doctor shall use `$CLAUDE_CONFIG_DIR/settings.json` when set, else
  `~/.claude/settings.json`.
- If the agentbus plugin is not enabled for Claude Code, then `doctor --wake` shall fail.
- While the plugin is already enabled, setup shall not reinstall it.
- Each plugin CLI call shall be bounded to 120 s.

## ROOT CAUSE

The only mechanism that wakes a session nobody has typed into is a Claude Code plugin monitor
(`experimental.monitors` in the plugin manifest), started by Claude Code at session start and
kept for the session. `setup claude` had two branches:

- plugin enabled: the plugin owns the wake; no hooks written (SPECS/0022).
- plugin not enabled: passive SessionStart and UserPromptSubmit hooks plus a Stop re-waker.
  Stop fires only after a first turn, so a fresh session is deaf.

Setup detected the plugin and never installed it, then printed the plugin branch's promise on
both branches. `install.sh` does install it; `pip install rodmena-agentbus`, which the README
gives, does not.

Infra first retracted this as their own error (they skipped install.sh), then asked for the fix
to ship anyway: anyone who installs through pip or uv reaches the deaf branch.

## ALTERNATIVES

- CHOSEN: setup installs the plugin through the `claude` CLI and verifies settings afterwards.
  Claude Code owns the download, caching and archive sha256 check; this is what install.sh does.
- REJECTED: the SessionStart hook spawning `agentbus watch --daemon`. That watcher is not tied to
  the session socket's lifetime, and it would be a second wake path waking the session twice.
- REJECTED: writing `enabledPlugins` and `extraKnownMarketplaces` into settings.json by hand. It
  bypasses Claude Code's fetch and verification, and it is the hand-assembled-pieces failure that
  caused the incident.
- REJECTED: correcting only the message. Honest, but the agent stays deaf.

## ORDERING, WHICH THE TESTS PIN

The install runs BEFORE setup loads settings.json. The CLI rewrites that file; had setup loaded
it first, writing its hooks back would overwrite `enabledPlugins` and silently undo the install
it had just reported. `test_setup_does_not_overwrite_the_plugin_it_just_installed`.

## VERIFICATION

- `tests/test_setup_installs_the_wake_plugin.py`: 10 tests driving the real `_setup_claude` and
  `doctor_wake` against a fake `claude` on PATH. Modes: ok, fail, a "liar" that reports ok and
  changes nothing, no claude at all, and already enabled. Doctor is tested both ways.
- Mutation: reverting setup to never install (the 0.9.99 behaviour), trusting the installer's
  return, exiting 0 on a deaf agent, and silencing doctor each fail the suite. 4 of 4.
- REAL CLI, fresh config: `agentbus setup claude` in a scratch project with an isolated
  `CLAUDE_CONFIG_DIR`. `claude plugin list` before: `[]`. After: `agentbus@rodmena 0.6.32
  enabled`. Setup exit 0, no duplicate hooks.
- The CLI's own contract was probed before building against it: marketplace add exits 0 when
  already present; install returns `"outcome":"ok"` both fresh and already installed; a failure
  exits 1 with `"outcome":"failed"` and a message.

NOT EXERCISED HERE: a session actually started with nothing typed and woken by an outside
message on a machine where setup did the install. That is the plugin monitor's existing
behaviour, unchanged by this ticket, and infra's acceptance test on a new Linux user on the demo
box is the real verdict.
