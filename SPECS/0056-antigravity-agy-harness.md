# 0056 — Antigravity (`agy`): replacing a refusal that went stale

Ticket #56. The client refused `agentbus setup agy` from the day the verb
existed, naming three blockers:

> "agy's CLI exposes no hook contract, no MCP server support, and no wake path
> (no daemon, stream, or socket)"

Measured against `agy` 1.1.27 (`~/.local/bin/agy`), all three are false. A
refusal that is factually wrong is worse than no refusal — it turns away an
operator whose harness works. This is the `claims-about-other-repos-expire`
failure: a dated negative about someone else's product, still asserted in the
present tense.

## What was measured, and how

Facts came from the installed binary's own embedded documentation and from
running probes, NOT from <https://antigravity.google/docs/plugins/>, which is a
stub. Where the two disagree, the binary won.

### Hook execution matrix — the finding that changed the design

A probe plugin whose handlers append to a file, then a real `agy` turn:

| Location | Hooks fire? |
|---|---|
| `~/.gemini/config/hooks.json` | **yes** |
| `~/.gemini/config/plugins/<name>/hooks.json` | **yes** |
| `<workspace>/.agents/hooks.json` | no |
| `<workspace>/.agents/plugins/<name>/hooks.json` | no — even with the workspace listed in `settings.json` `trustedWorkspaces` |

A workspace `mcp_config.json` was never initialised either; nothing about the
server appears in the CLI log.

**`agy plugin validate` reports a workspace plugin as `[ok]` with "hooks: 2
processed".** Valid and loaded are different things here, so validation alone
would have shipped a plugin that never ran. The plugin is therefore MACHINE-WIDE,
which was measured rather than chosen.

### Other measured facts

- `Stop` → `{"decision":"continue","reason":…}` re-enters the loop; `reason` is
  injected as a system message. `PreInvocation` → `{"injectSteps":[{"ephemeralMessage":…}]}`.
- Hooks run with cwd = the directory containing `hooks.json`, and NOTHING is put
  in their environment — no agent name, no key.
- **"Hooks run synchronously and block the agent loop (no async execution)"** —
  their own *Current Limitations*. There is no `asyncRewake` analogue.
- No `SessionStart` and no `SessionEnd` event.
- `agy plugin validate` exits 0 on a good plugin and 1 on a bad one.
- `mcp_config.json` shape, derived by running `agy mcp add --header` into a
  throwaway `HOME`: `{"disabled": false, "headers": {...}, "serverUrl": "..."}`.
  The embedded MCP schema omits `headers` entirely.
- `/skills/antigravity.md` and `/skills/agy.md` are 404; `claude-code.md` (74373 b)
  and `opencode.md` (17249 b) are 200.

## EARS spec

- The CLI SHALL accept `setup agy`, SHALL accept `antigravity` as an alias
  normalized to `agy` before dispatch, and SHALL name the canonical form.
- `setup agy` SHALL write a plugin at `~/.gemini/config/plugins/agentbus/`
  containing `plugin.json` and `hooks.json`, and SHALL leave foreign entries in
  that `hooks.json` byte-identical.
- The plugin SHALL contain NO credential. A machine-wide plugin holds exactly one
  bearer key, so writing one would make every checkout act as one agent — the
  global-only identity asymmetry that gets `codex` refused.
- `setup agy` SHALL record the checkout in an opt-in list, and the hooks SHALL act
  ONLY for listed checkouts. Without this the machine-wide hooks would act in
  every project that carries a `.agentbus/agent` from another harness.
- `setup agy` SHALL wire an ACTIVE wake lane: a `Stop` handler emitting
  `{"decision":"continue","reason":<mail>}` on genuinely new mail.
- The Stop handler SHALL claim through the SAME flock'd ledger as the Claude
  re-waker. Because `{"decision":"continue"}` re-enters the loop, an unclaimed
  re-wake is an unbounded model loop, not a duplicate notification.
- `setup agy` SHALL wire a PASSIVE lane: `PreInvocation` emitting
  `injectSteps[].ephemeralMessage` — never `userMessage`, which reads as a human
  prompt.
- Both handlers SHALL claim through the same ledger, so a Stop-forced turn does
  not re-inject the message that caused it.
- Both handlers SHALL resolve identity from `workspacePaths[0]`, never from cwd;
  `AGENTBUS_AGENT` SHALL still outrank it.
- Both handlers SHALL resolve the credential through `sealing.bound_env_filename`.
- With no identity, both handlers SHALL emit a valid no-op JSON object, touch no
  network, and exit 0.
- No internal failure SHALL hold the loop or exit non-zero.
- The window and the declared hook `timeout` SHALL derive from one constant pair
  with `timeout > window`, and the window SHALL stay strictly below agy's
  documented 30 s default until the true ceiling is measured.
- Because hooks block the loop, the report SHALL state that the window is a
  foreground pause and that always-attached reachability comes from
  `agentbus service`.
- `setup agy` SHALL NOT wire a `PreToolUse` gate, and SHALL say why.
- `setup agy` SHALL install no skill and SHALL report the 404.
- `setup agy` SHALL be idempotent.
- `teardown --machine` SHALL remove the plugin; a per-checkout `teardown` SHALL
  NOT, because removing machine-wide wiring for one project would unwire the rest.

## Verified end to end, against agy 1.1.27

- `agy plugin validate` on the installed plugin: exit 0, "hooks: 2 processed".
  Known-negative: a corrupted `plugin.json` gives exit 1.
- **A real `agy` turn quoted a marker string it could only have learned from our
  `PreInvocation` injection** (`WAKEPROBE-1788722782`), with the delivery
  recorded in the probe ledger. An observed turn, not an asserted one.
- Stop lane, live: `{"decision":"continue","reason":"…"}` on fresh mail; `{}` on
  the second call for the same delivery.
- Opt-in guard, both directions: the wired checkout is served; this repo — which
  IS Claude-wired, same agent, same unread mail — stays silent.
- No key material anywhere under the plugin directory.

## Not verified, deliberately

- **The Stop-hook timeout ceiling.** `AGY_WAKE_WINDOW_SEC = 20` is chosen to sit
  under agy's documented 30 s default, so the lane never depends on a raised
  timeout being honoured. The true ceiling is unmeasured; measure before raising.
- **Hook failure semantics** (missing binary, timeout, invalid JSON). The gate is
  not shipped precisely because its fail-open doctrine depends on this.
- **Interactive `agy`.** Every hook observation above is from print mode (`-p`).

## Alternatives

- Placement: machine-wide `~/.gemini/config/plugins/agentbus/` **[CHOSEN — the
  only location whose hooks actually run]** vs workspace `.agents/plugins/`
  [REJECTED: measured silent, though `plugin validate` calls it valid] vs
  `plugins.json` [REJECTED: second source of truth, loses to workspace discovery].
- Machine-wide safety: an explicit opt-in list **[CHOSEN]** vs acting on
  `.agentbus/agent` alone [REJECTED: that file exists in every Claude-wired repo,
  so agy would poll and pause projects that never asked for it].
- Gate: omitted with a named refusal **[CHOSEN]** vs `enabled: false` [REJECTED: a
  disabled block pointing at an unimplemented subcommand is a trap] vs shipped
  [REJECTED: fail-open unproven on this host].
- Skill: install nothing **[CHOSEN]** vs bundle the Claude flavour [REJECTED: a
  stale second copy of the same skill name beside the operator's current one].

FILES: `onboarding/_agy_setup.py`, `onboarding/_agy_plugin.py`,
`hooks/_antigravity.py`, `onboarding/_paths.py`, `rewake.py` (extracted
`poll_for_fresh_mail`); tests `test_agy_setup_wires_a_real_wake_lane.py`,
`test_agy_hook_speaks_antigravity.py`, `test_agy_identity_comes_from_the_payload.py`.
