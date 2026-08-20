"""`agentbus signin`, `agentbus setup <harness>`, `agentbus doctor --wake`.

SPECS/0021: onboarding is three commands, and everything a host needs — the
credential, the per-project identity, both passive hooks, and the ACTIVE
Stop re-waker — is generated, verified, and idempotent. Nothing is inlined,
nothing is guessed, and setup never touches configuration it did not write:
our entries are recognized by their own content (commands that invoke
agentbus tooling), never by position.

Every rule encoded here was a real failure first, on the platform's own
hosts, in one night:

  * a key inlined into a hook command outlived its rotation;
  * an agent name guessed from a directory acted as an agent that did not
    exist, silently;
  * a key file sourced without `set -a` left the credential unexported and
    the hook looked wired while printing nothing;
  * a by-the-book install with only passive hooks was structurally deaf; and
  * the re-waker that fixes that loops forever unless it dedupes on delivery
    ids, because unread-but-unacked mail is a permanent wake source.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import stat
from pathlib import Path

from ._claude_setup import _setup_claude
from ._credentials import doctor_credential_scope
from ._identity import _agent_key
from ._paths import (
    OPENCODE_PLUGIN_NPM,
    _config_dir,
    _dump_json,
    _git_root_or_none,
    _keys_dir,
    _load_json,
    _say,
)
from ._provision import _provision_project_agent


def _setup_opencode(args: argparse.Namespace) -> int:
    """Wire opencode: project-level identity, the plugin, and the wake.

    THE HARNESS-SPECIFIC ACTIVE-TRIGGER FACT: opencode has NO Stop+asyncRewake
    hook (that is Claude Code's mechanism). Its wake is the PLUGIN consuming
    the agent's SSE stream itself and calling promptAsync — the plugin IS the
    active trigger, so there is no re-waker script to install here. Everything
    setup writes is the identity the plugin reads.

    Per-project identity is a PROJECT-LEVEL opencode.json with an mcp.agentbus
    entry carrying this agent's own bound key. opencode merges it over the
    global config (verified), and the plugin reads the project level before
    the global (agentbus.ts resolveKey) — so a wired project is served by ITS
    OWN credential, which is the no-bypass leg #83 calls for. A session with
    no project-level entry falls back to the global config, which is the
    historical behaviour for unwired directories.
    """
    report: list[str] = []
    base_url = args.base_url

    # 1-3. Shared identity resolution, registration, and bound-key minting.
    name = _provision_project_agent(args, report, "opencode")
    if name is None:
        return 1

    # 4. Project-level opencode.json — merge, never replace. A foreign project
    #    config (MCP servers, provider blocks) must survive byte-for-byte, the
    #    same idempotency law that governs the claude harness. opencode itself
    #    is written as opencode.json; both spellings are read, .jsonc first.
    oc_path = Path.cwd() / "opencode.json"
    oc_data = _load_json(oc_path) if oc_path.exists() else {}
    key_for_mcp = _agent_key(name)
    mcp_url = f"{(base_url or 'https://agentbus.rodmena.co.uk').rstrip('/')}/mcp"

    existing = oc_data.get("mcp", {}).get("agentbus")
    changed = False
    if key_for_mcp:
        want_auth = f"Bearer {key_for_mcp}"
        current_auth = None
        if isinstance(existing, dict):
            headers = existing.get("headers") or {}
            current_auth = headers.get("Authorization") if isinstance(headers, dict) else None
        if current_auth != want_auth:
            oc_data.setdefault("mcp", {})["agentbus"] = {
                "type": "remote",
                "url": mcp_url,
                "enabled": True,
                "headers": {"Authorization": want_auth},
                "oauth": False,
            }
            changed = True
    if changed:
        # It carries the bound bearer key: 0600, never the umask default (issuedb #32).
        _dump_json(oc_path, oc_data, private=True)
        report.append(f"project identity: {oc_path} mcp.agentbus (bound key for {name})")
    else:
        report.append(f"project identity: {oc_path} (already {name})")
    if key_for_mcp and oc_path.exists():
        with contextlib.suppress(OSError):
            os.chmod(oc_path, stat.S_IRUSR | stat.S_IWUSR)

    # 5. Plugin reference in the project's plugin array. The project file is
    #    merged over the global one, so a project-level plugin array ADDS to the
    #    global plugins — it does not replace them. Dedupe so re-runs converge.
    plugins = oc_data.setdefault("plugin", [])
    if not isinstance(plugins, list):
        plugins = [plugins] if isinstance(plugins, str) else []
        oc_data["plugin"] = plugins
    if OPENCODE_PLUGIN_NPM not in plugins:
        plugins.append(OPENCODE_PLUGIN_NPM)
        _dump_json(oc_path, oc_data, private=bool(key_for_mcp))
        report.append(
            f"plugin: {oc_path} -> {OPENCODE_PLUGIN_NPM} "
            "(installed via `opencode plugin <name>` the first time)"
        )
    else:
        report.append(f"plugin: {OPENCODE_PLUGIN_NPM} (already referenced)")

    gitignore = Path.cwd() / ".gitignore"
    if (Path.cwd() / ".git").exists():
        lines = gitignore.read_text().splitlines() if gitignore.exists() else []
        if "opencode.json" not in lines and "opencode.local.json" not in lines:
            # The project identity carries a SECRET (the bound key bearer), so
            # it must never be committed. opencode reads opencode.json; a
            # project that already tracks one for non-secret settings should
            # keep it — but this one is ours, and it holds a key.
            with gitignore.open("a", encoding="utf-8") as fh:
                fh.write("opencode.json\n")
            report.append("gitignore: added opencode.json (holds the bound key)")

    # 6. The plugin binary/client. opencode plugins run in Bun and shell out to
    #    the shared `agentbus`/`agentbus-hook` client, so those must be on PATH
    #    (the same install `signin` and the claude harness rely on). Nothing to
    #    write here — the plugin is the package above; the client is the pip
    #    package already installed.
    report.append(
        "wake: provided by the opencode PLUGIN (SSE stream + "
        "promptAsync) — opencode has no Stop hook, so no re-waker"
    )

    # 7. The skill, served canonically — same managed-artifact rule as claude.
    skill_path = Path.home() / ".claude" / "skills" / "agentbus" / "SKILL.md"
    try:
        import httpx

        resp = httpx.get(
            f"{(base_url or 'https://agentbus.rodmena.co.uk').rstrip('/')}/skills/opencode.md",
            timeout=15,
        )
        if resp.status_code == 200 and len(resp.text) > 500:
            if not skill_path.exists() or skill_path.read_text() != resp.text:
                skill_path.parent.mkdir(parents=True, exist_ok=True)
                if skill_path.exists():
                    bak = skill_path.with_suffix(".md.bak")
                    bak.write_text(skill_path.read_text())
                    report.append(f"skill: previous copy saved to {bak}")
                skill_path.write_text(resp.text)
                report.append(f"skill: {skill_path} (installed — MANAGED artifact)")
            else:
                report.append(f"skill: {skill_path} (current)")
        else:
            report.append(f"skill: NOT installed (served {resp.status_code})")
    except Exception as exc:
        report.append(f"skill: NOT installed ({exc})")

    # 8. #64 credential-scope warning — same finding as claude: an inherited
    #    send-or-above fallback is exactly what this project's own identity is
    #    meant to stop relying on.
    try:
        findings = [ln for ln in doctor_credential_scope(base_url=base_url) if "FINDING" in ln]
        if findings:
            _say("WARNING — a send-or-above credential is reachable by inheritance:")
            for ln in findings:
                _say(f"  {ln}")
            _say(
                "  A fallback slot wants a READ-scope key, nothing above. Replace it "
                "with a read-scope key from the dashboard, then re-run `agentbus doctor`."
            )
            report.append("credential scope: WARNING (inherited send-or-above credential found)")
        else:
            report.append("credential scope: ok (no inherited send-or-above credential)")
    except Exception as exc:
        report.append(f"credential scope: not checked ({exc})")

    _say("agentbus setup opencode — everything wired, nothing foreign touched:")
    for line in report:
        _say(f"  {line}")
    _say("")
    _say(
        "Next: `opencode plugin @rodmena/agentbus-opencode` once (the "
        "package), then restart the opencode session."
    )
    return 0


def cmd_teardown(args: argparse.Namespace) -> int:
    """Remove ALL AgentBus wiring from this project, in one command (#118).

    Opting out used to require knowing three paths — `.agentbus/`, the
    settings.local.json identity, and the key file — and every partial deletion
    left a half-wired state whose symptoms (a monitor resolving a keyless
    identity, a guard failing on a dead credential) read as AgentBus taking the
    session hostage. Three separate incidents in one day, each a different
    missed file. Leaving must be as easy as joining: one command, everything,
    and it says exactly what it removed so a restarted session's silence is
    expected rather than suspicious.

    Local-only, deliberately: the server-side agent is NOT retired here. The
    row keeps the inbox and history for a re-wire (`agentbus setup` restores
    the same derived identity), and retiring is a separate, explicit act
    (`agentbus retire`) because it affects peers who hold the address.
    """
    removed: list[str] = []
    root = _git_root_or_none() or Path.cwd()

    agent: str | None = None
    agentbus_dir = root / ".agentbus"
    agent_file = agentbus_dir / "agent"
    if agent_file.is_file():
        with contextlib.suppress(OSError):
            agent = agent_file.read_text().strip() or None

    local_path = root / ".claude" / "settings.local.json"
    local = _load_json(local_path)
    if agent is None:
        agent = (local.get("env") or {}).get("AGENTBUS_AGENT")

    if agentbus_dir.is_dir():
        import shutil as _shutil

        _shutil.rmtree(agentbus_dir, ignore_errors=True)
        removed.append(f"{agentbus_dir}/")
    if (local.get("env") or {}).get("AGENTBUS_AGENT"):
        # Surgical: drop OUR key, keep every foreign setting in the file.
        del local["env"]["AGENTBUS_AGENT"]
        if not local["env"]:
            del local["env"]
        if local:
            _dump_json(local_path, local)
            removed.append(f"{local_path} (AGENTBUS_AGENT entry only)")
        else:
            with contextlib.suppress(OSError):
                local_path.unlink()
            removed.append(str(local_path))

    if getattr(args, "purge_key", False) and agent:
        for suffix in (".env", ".env.dead"):
            kf = _keys_dir() / f"{agent}{suffix}"
            if kf.is_file():
                with contextlib.suppress(OSError):
                    kf.unlink()
                removed.append(str(kf))

    if getattr(args, "machine", False):
        import shutil as _shutil

        cfg = _config_dir()
        if cfg.is_dir():
            _shutil.rmtree(cfg, ignore_errors=True)
            removed.append(f"{cfg}/ (machine-level: operator credential, device-id, all keys)")

    if not removed:
        _say("nothing to remove — this project carries no AgentBus wiring.")
        return 0
    _say("AgentBus wiring removed:")
    for item in removed:
        _say(f"  - {item}")
    _say("Restart the session; it will be silent — no monitor, no hooks, no bus.")
    if agent and not getattr(args, "machine", False):
        _say(f"(server-side agent '{agent}' is untouched; `agentbus setup` re-wires it,")
        _say(" `agentbus retire` stands it down for peers.)")
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    harness = args.harness
    if harness == "claude":
        return _setup_claude(args)
    if harness == "opencode":
        return _setup_opencode(args)

    # SPECS/0021: an unimplemented harness SHALL say so and name the ticket,
    # NEVER half-wire. The blocker is named per harness, because a refusal that
    # says only "not implemented" reads as a scheduling gap when it is a
    # harness-contract gap — and wiring half of the contract would be worse than
    # refusing, since it would present an AgentBus session that cannot be woken.
    if harness == "codex":
        _say("agentbus setup codex: refused, and not merely 'not implemented yet'.")
        _say("  codex's hook contract supports the GATE (PreToolUse) and the passive")
        _say("  hooks, but has no wake mechanism: the hook docs state that nothing")
        _say("  reactivates an idle session and SessionEnd is advisory-only. A")
        _say("  wired-but-undeaf agent is worse than an unwired one, because it")
        _say("  presents as reachable while nothing can ever start a turn for it.")
        _say("  There is also no per-project codex settings file for a declared")
        _say("  identity — config is global-only, which re-opens the #82/#83 gate")
        _say("  asymmetry. Tracked in #31. Nothing was changed.")
        return 1
    if harness == "agy":
        _say("agentbus setup agy: refused, and not merely 'not implemented yet'.")
        _say("  agy's CLI exposes no hook contract, no MCP server support, and no")
        _say("  wake path (no daemon, stream, or socket) — there is nothing setup")
        _say("  could wire that would give a session an inbox it can act on.")
        _say("  Tracked in #31. Nothing was changed.")
        return 1
    _say(f"unknown harness '{harness}' (choose from claude, opencode, codex, agy).")
    return 1


def cmd_sibling(args: argparse.Namespace) -> int:
    # DEPRECATED (operator directive, 2026-08-10): the sibling machinery is the
    # wrong answer. Identity is env-var-driven — AGENTBUS_AGENT in the project's
    # .env, or exported for one command — and a customer who wants two agents on
    # one checkout should use a git worktree or a clone, which gives each its own
    # directory and therefore its own identity for free. This verb is kept so old
    # muscle memory does not silently become a different command, but it says no.
    _say(
        "DEPRECATED: `agentbus sibling` is being retired. Give the agent its "
        "identity with an env var instead:"
    )
    _say("  AGENTBUS_AGENT=myrole agentbus setup claude --role myrole")
    _say("  # or put `AGENTBUS_AGENT=myrole` in the project's .env")
    _say(
        "For two agents on one checkout, use a git worktree or a separate "
        "clone — each directory has its own identity."
    )
    return 2


def cmd_as(args: argparse.Namespace) -> int:
    """Run a command AS a sibling, so every descendant inherits one identity.

    DEPRECATED (operator directive, 2026-08-10) with `sibling`: identity is
    env-var-driven. `AGENTBUS_AGENT=role agentbus ...` is the supported way to
    act as a different agent; a worktree or clone gives that role its own
    directory identity. Kept so old invocations get guidance, not a silent
    change of meaning.
    """
    if not args.command:
        _say("nothing to run. usage: agentbus as <role> -- <command> [args...]")
        return 2
    # DEPRECATED: do not execute. The env-var identity is the supported way —
    # `AGENTBUS_AGENT=role agentbus ...` — and a worktree/clone gives that role
    # its own directory identity. Running the command here would keep the old
    # machinery alive; guidance is the point of keeping this verb at all.
    _say(
        "DEPRECATED: `agentbus as` is being retired. Act as a different agent "
        "with an env var instead:"
    )
    _say(f"  AGENTBUS_AGENT={args.role} agentbus {args.command[0]} {' '.join(args.command[1:])}")
    _say("For a separate identity on this checkout, use a git worktree or clone.")
    return 2
