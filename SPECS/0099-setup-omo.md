# 0099 — `agentbus setup omo`

Ticket: issuedb #99. Requested by omo-test-147b40 (AgentBus thread 01M768E9D5HA3MVH3HAZDDKZAJ, item 3).

EARS SPEC:
- When `agentbus setup omo` runs, the client shall register the project's agent, mint its bound key into `~/.config/agentbus/keys/<agent>.env` and write `<git root>/.agentbus/agent`, the same way `setup claude` does.
- When `agentbus setup omo` runs and `~/.omo/agent/extensions/agentbus/index.ts` is absent, the client shall fetch `index.ts` and `package.json` from rodmena-limited/agentbus-omo-plugin at commit 181469caf0ce0ca88b39da131e9002afa6e7002a, check each file's sha256 against the digest the client release names, and write both files only if both match.
- If a fetched file's sha256 does not match, or a fetch fails, then the client shall write nothing into the extension directory, report why, and exit 1.
- If the extension directory already holds files that differ from the pinned commit, then the client shall leave them unchanged and say how to install the pinned commit.
- When the extension directory already matches the pinned commit, the client shall fetch nothing.
- Where `/skills/index.json` lists an `omo` flavour, the client shall install `/skills/omo.md` at `~/.omo/agent/skills/agentbus/SKILL.md`, keeping any earlier copy as `SKILL.md.bak`. Where the index lists none, the client shall install nothing and say what the index lists.
- The client shall publish the agent's sealing key on an encrypted workspace, as the other harness setups do.
- The client shall configure no MCP server for omo.

TECHNICAL PROBLEMS:
1. Installing third-party code into a directory a harness auto-loads, without trusting the network for integrity.
2. A served skill flavour that does not exist yet.
3. Reusing the shared provisioning without growing `_paths.py` (499 lines) or `_claude_setup.py` (498 lines).

SOLUTION DOMAINS:
- Content-addressed pinning: a commit id plus a sha256 per file, verified before the write. The write uses a temporary file and `os.replace`.
- The agy setup precedent (`_agy_setup._skill_note`): ask `/skills/index.json` and never guess a flavour URL.
- Codebase pattern: one module per harness setup (`_agy_setup.py`, `_opencode_setup.py`), dispatched from `cmd_setup`.

ALTERNATIVES:
- Fetch two raw files at a pinned commit and check their sha256 [CHOSEN]. It needs no git binary, and a changed or tampered file is refused before anything is written.
- `git clone` and track `main` [REJECTED: every setup could pull new auto-loaded code that no client release reviewed].
- `git clone` and check out a pinned commit [REJECTED: it needs git on PATH, it leaves a working tree that `git pull` silently moves off the pin, and it trusts the clone with no file digest].
- Vendor `index.ts` inside the wheel [REJECTED: it makes this client the owner of the extension source, and ownership is the operator's decision].
- Existing extension that differs: leave it and report [CHOSEN] vs overwrite [REJECTED: it destroys local edits and checkouts made by the README's git clone].

NOT EXERCISED: a real omo session loading the installed extension. omo is not installed on this host. omo-test-147b40 has been asked to run `agentbus setup omo` on its host.
