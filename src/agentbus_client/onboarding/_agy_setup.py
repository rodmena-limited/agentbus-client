"""`agentbus setup agy` — the Antigravity harness (#56, SPECS/0056).

This module exists because a refusal went stale. The client has told operators
since it was written that agy "exposes no hook contract, no MCP server support,
and no wake path". Measured against agy 1.1.27, all three are false: five hook
events, remote MCP with headers, and a `Stop` hook whose `{"decision":
"continue"}` re-enters the loop. A refusal that is factually wrong is worse than
no refusal — it turns a working harness away.

WHAT MAKES THIS HARNESS DIFFERENT, and why each difference changed a decision:

  * Hooks BLOCK the agent loop ("no async execution", their docs). There is no
    `asyncRewake`, so the wake window is a foreground pause and must be short.
    The report says so, because an operator promised a Claude-shaped wake would
    have been promised something this host cannot do.
  * A hook's cwd is the directory holding `hooks.json`, not the workspace. So
    identity comes off the payload — see `hooks/_antigravity.py`.
  * Nothing is injected into the hook environment: no agent name, no key. The
    Claude lane's shell preamble has nothing to do here, so there is none, and
    with it goes that lane's shell-traversal surface.
  * The plugin is MACHINE-WIDE, and that was measured rather than chosen. A
    workspace plugin is the obvious design and it silently does not run: on agy
    1.1.27 only `~/.gemini/config/...` hooks fire, while `<workspace>/.agents/`
    hooks stay silent even when the workspace is trusted. `agy plugin validate`
    calls the workspace plugin valid and reports "hooks: 2 processed", so
    VALIDATION WOULD HAVE PASSED ON A PLUGIN THAT NEVER RAN. See `_paths.py`
    for the full matrix.
  * Being machine-wide, it carries NO credential — one key in a global file
    would make every checkout act as one agent. Per-project behaviour comes
    from the payload instead, and `AGENTBUS_AGENT` remains the kill switch.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from ..client import AgentBus
from ._agy_plugin import (
    PLUGIN_VERSION,
    PLUGIN_VERSION_FILENAME,
    ensure_hooks,
    hooks_config,
    load_hooks,
    plugin_manifest,
)
from ._credentials import doctor_credential_scope
from ._identity import _write_worktree_identity
from ._paths import (
    AGY_WAKE_WINDOW_SEC,
    _dump_json,
    _git_root_or_none,
    _say,
    agy_mark_wired,
    agy_plugin_dir,
)
from ._provision import _provision_project_agent
from ._signin import _sealing_publish_with_retry

# Their canonical filename; `agy` is an alias they publish in the same index.
_SKILL_HARNESS = "antigravity"

# Printed verbatim when setup declines the gate. It names both blockers, because
# "not implemented" would read as a scheduling gap when it is a measurement gap.
_GATE_REFUSAL = (
    "gate (PreToolUse): NOT wired, and not merely unimplemented. agy's decision "
    "vocabulary (allow|deny|ask|force_ask|deny_unless_prior_grant) is not Claude's, "
    "and what agy does with a missing, timed-out or invalid-JSON hook is UNMEASURED. "
    "A gate on that footing can fail CLOSED and deny every tool call — including the "
    "ones needed to undo it. Tracked in #56."
)


def _skill_note(base_url: str) -> str:
    """Whether a skill flavour exists, ASKED rather than assumed.

    The first version of this line asserted "/skills/antigravity.md is 404".
    That was true when written and is exactly the kind of claim that rots — the
    server team is queuing the flavour right now, and a hardcoded 404 would keep
    telling operators it does not exist for as long as nobody re-read this
    string.

    So ask `/skills/index.json`, which the server builds from its own filesystem
    rather than a hardcoded list: when the flavour lands, the index and the .md
    URL go green in the same deploy and cannot drift apart. Their canonical name
    is `antigravity`; `agy` is an alias, published in the same index.
    """
    try:
        import httpx

        resp = httpx.get(f"{base_url}/skills/index.json", timeout=10)
        if resp.status_code != 200:
            return (
                f"skill: NOT checked — /skills/index.json answered {resp.status_code}. "
                "Not installing rather than guessing."
            )
        index = resp.json()
        harnesses = set(index.get("harnesses") or [])
        aliases = index.get("aliases") or {}
        served = aliases.get(_SKILL_HARNESS, _SKILL_HARNESS) in harnesses or (
            _SKILL_HARNESS in harnesses
        )
    except Exception as exc:
        return f"skill: NOT checked ({type(exc).__name__}) — not installing rather than guessing."

    if not served:
        return (
            "skill: NOT installed — the server serves no Antigravity flavour yet "
            "(/skills/index.json lists none). Bundling the Claude flavour would put a "
            "second skill of the same name beside your global copy, and a stale copy "
            "that shadows a current one is worse than none. Queued with the server "
            "team; this line re-checks the index on every run, so it will change by "
            "itself once it serves."
        )
    return (
        f"skill: SERVED — the server now carries a '{_SKILL_HARNESS}' flavour. Install it "
        f"with `agentbus refresh-skill`; it belongs at "
        f"~/.gemini/config/skills/agentbus/SKILL.md (back up your hand-maintained copy "
        f"first — it is the Claude flavour)."
    )


def _hook_binary() -> tuple[str, str | None]:
    """The absolute `agentbus-hook` path, and a warning if we had to guess.

    Antigravity may be launched from a desktop entry whose PATH omits the user's
    local bin. A hook that cannot be executed is indistinguishable from an agent
    with no mail — silence either way — so resolve it now, while we can still
    say something about it.
    """
    found = shutil.which("agentbus-hook")
    if found:
        return found, None
    return "agentbus-hook", (
        "agentbus-hook is not on PATH right now, so the hooks reference it by bare "
        "name. If Antigravity is launched from a desktop entry with a minimal PATH, "
        "the hooks will silently do nothing — re-run this from a shell where "
        "`which agentbus-hook` answers."
    )


def _setup_agy(args: argparse.Namespace) -> int:
    """Wire Antigravity: identity, the catch-up lane and the active wake lane."""
    report: list[str] = []
    base_url = (args.base_url or "https://agentbus.rodmena.co.uk").rstrip("/")

    # 1-3. Shared identity resolution, registration, bound-key minting.
    name = _provision_project_agent(args, report, "agy")
    if name is None:
        return 1

    # 4. The canonical, harness-neutral identity declaration. Every host writes
    #    this; it is what the hooks read when the payload cannot say.
    _write_worktree_identity(name, report)

    root = _git_root_or_none() or Path.cwd()

    # 4a. THE SHARED-IDENTITY HAZARD, reported by the server team with field
    #     evidence the same day (infra-manager-c13110, two live sessions on one
    #     identity): read/ack state is per-delivery-per-AGENT, not per
    #     connection. So if a Claude session and an agy session share one agent,
    #     an ack by either makes the message INVISIBLE to the other —
    #     indistinguishable from mail that never arrived — and both are woken for
    #     the same delivery. Supported at the transport layer, unsafe at the
    #     identity layer. We warn rather than refuse: sharing is fine
    #     SEQUENTIALLY (the hand-over case), and only concurrent live sessions
    #     bite.
    if (root / ".claude" / "settings.local.json").exists() or (root / "opencode.json").exists():
        report.append(
            f"WARNING — this checkout is already wired for another harness, so agy "
            f"would act as the SAME agent ({name}). Read/ack state belongs to the "
            "agent, not the connection: if both run at once, an ack by one hides the "
            "message from the other. Fine sequentially; for concurrent sessions give "
            "each host its own checkout (a git worktree) so each derives its own agent."
        )

    # 4b. OPT IN THIS CHECKOUT. The plugin is machine-wide; without this the
    #     hooks would act in every project that has a `.agentbus/agent` from
    #     some other harness.
    added = agy_mark_wired(root)
    report.append(
        f"opt-in: {root} {'added to' if added else 'already in'} the agy-wired list "
        "(the machine-wide hooks act ONLY for listed checkouts)"
    )

    plugin_dir = agy_plugin_dir()

    # 5. plugin.json — the marker that makes this a plugin at all.
    manifest_path = plugin_dir / "plugin.json"
    manifest = plugin_manifest()
    if not manifest_path.is_file() or json.loads(manifest_path.read_text()) != manifest:
        _dump_json(manifest_path, manifest)
        report.append(f"plugin: {plugin_dir} (installed, MACHINE-WIDE)")
    else:
        report.append(f"plugin: {plugin_dir} (current, MACHINE-WIDE)")
    (plugin_dir / PLUGIN_VERSION_FILENAME).write_text(f"{PLUGIN_VERSION}\n")

    # 6. hooks.json — merged, never replaced. agy runs every plugin's named
    #    hooks sequentially, so clobbering a foreign key disables someone
    #    else's tooling.
    hook_bin, path_warning = _hook_binary()
    hooks_path = plugin_dir / "hooks.json"
    merged, state = ensure_hooks(load_hooks(hooks_path), hooks_config(hook_bin))
    if state != "ok":
        _dump_json(hooks_path, merged)
    report.append(f"hooks: catch-up + wake ({state})")
    report.append(
        f"wake: Stop hook, {AGY_WAKE_WINDOW_SEC}s window. agy hooks BLOCK the loop "
        "(no asyncRewake), so this is a FOREGROUND pause at each turn end: it "
        "catches mail arriving during the turn plus a short grace. It is NOT an "
        "idle hold — for always-attached reachability run `agentbus service`."
    )
    report.append(
        "scope: the plugin is machine-wide because WORKSPACE hooks do not run on "
        "this harness (measured — see _paths.py). It is per-project anyway: each "
        "hook resolves identity from the payload's workspacePaths[0], and a "
        "checkout that declared no agent gets a silent no-op."
    )
    report.append(_GATE_REFUSAL)
    if path_warning:
        report.append(f"WARNING — {path_warning}")

    # 7. NO CREDENTIAL IS WRITTEN. A machine-wide plugin can hold exactly ONE
    #    bearer key, so writing one here would make every checkout on this
    #    machine act as this project's agent — the global-only identity
    #    asymmetry that gets `codex` refused two functions away. The hooks need
    #    no key in a file: they adopt the resolved agent's own bound key at run
    #    time. MCP is the only thing that would want one, it is optional, and on
    #    an encrypted workspace it cannot send anyway (the CLI is the only send
    #    path), so it is left to an explicit operator decision.
    report.append(
        "mcp: NOT configured, deliberately. A machine-wide plugin holds ONE key, "
        "so an MCP entry here would make every checkout act as this one agent. If "
        "you want MCP reads on this host and accept that, run:\n"
        f'      agy mcp add --header "Authorization: Bearer $(grep -h AGENTBUS_API_KEY '
        f'~/.config/agentbus/keys/{name}.env | cut -d= -f2)" agentbus {base_url}/mcp/'
    )

    # 8. THE SEALING KEY (#189). Omitted from the first version of this module,
    #    and the field caught it within minutes: on an encrypted workspace an
    #    agent with no published pubkey CANNOT BE WRITTEN TO AT ALL. The sender
    #    gets "cannot seal: these recipients have published no public key" — so
    #    the agent registers fine, reports success, and is unreachable. That is
    #    the addressable-but-deaf failure this whole harness exists to avoid,
    #    arriving through the one step that was not copied from the Claude lane.
    try:
        bus = AgentBus(base_url=base_url, agent=name)
        if bus._request("GET", "/v1/workspace/pubkeys").get("encrypted"):
            from .. import sealing as _sealing

            _private, public = _sealing.ensure_keypair(name)
            del _private
            registered = _sealing_publish_with_retry(bus, name, public)
            if registered is not None:
                report.append(
                    f"sealing key: {_sealing.key_path(name)} (0600) "
                    f"registered as {registered.get('fingerprint')}"
                )
            else:
                report.append(
                    f"sealing key: !!! PUBLISH FAILED after retries — agent '{name}' is "
                    f"REGISTERED but has NO published pubkey. On this encrypted workspace "
                    f"peers CANNOT seal to '{name}', so it can send but never receive. "
                    f"Recover with:  agentbus keys rotate"
                )
        else:
            report.append("sealing key: not needed (workspace is not encrypted)")
    except Exception as exc:
        report.append(
            f"sealing key: !!! NOT REGISTERED ({type(exc).__name__}) — on an encrypted "
            f"workspace peers cannot seal to '{name}'. Recover with: agentbus keys rotate"
        )

    report.append(_skill_note(base_url))
    report.append(
        "session-end: agy has NO SessionEnd event, so nothing reaps a stream at "
        "session close on this host. Stated rather than implied."
    )

    # 9. #64 — the same inherited-credential audit every harness runs.
    try:
        findings = [ln for ln in doctor_credential_scope(base_url=base_url) if "FINDING" in ln]
        if findings:
            _say("WARNING — a send-or-above credential is reachable by inheritance:")
            for ln in findings:
                _say(f"  {ln}")
            report.append("credential scope: WARNING (inherited send-or-above credential found)")
        else:
            report.append("credential scope: ok (no inherited send-or-above credential)")
    except Exception as exc:
        report.append(f"credential scope: not checked ({exc})")

    _say("agentbus setup agy — everything wired, nothing foreign touched:")
    for line in report:
        _say(f"  {line}")
    _say("")
    _say("Verify it with:")
    _say(f"  agy plugin validate {plugin_dir}")
    _say('  agy -p "/hooks"     # our two hooks should be listed')
    _say("  (no `/mcp` check — this setup deliberately configures no MCP server;")
    _say("   the line above in the report tells you how to add one if you want it.)")
    return 0


def teardown_agy_machine(removed: list[str]) -> None:
    """Remove the machine-wide Antigravity plugin. `teardown --machine` only.

    A PER-CHECKOUT teardown deliberately does NOT come here, and that is the same
    rule the Claude lane follows for its entries in `~/.claude/settings.json`:
    those are machine-wide too and survive a per-project teardown. Deleting the
    plugin because ONE checkout opted out would silently unwire every other
    project on the machine.

    Nothing leaks by leaving it: the plugin holds no credential, and with
    `.agentbus/agent` removed — which `cmd_teardown` does do — every hook
    resolves nobody and no-ops.
    """
    plugin_dir = agy_plugin_dir()
    if plugin_dir.is_dir():
        shutil.rmtree(plugin_dir, ignore_errors=True)
        removed.append(f"{plugin_dir}/ (machine-wide Antigravity plugin)")
