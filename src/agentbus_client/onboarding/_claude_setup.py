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
import subprocess
from pathlib import Path

from ..client import AgentBus
from ._credentials import doctor_credential_scope
from ._identity import _agent_key, _project_claude_dir, _write_worktree_identity
from ._paths import (
    _MARKER_HOOK,
    _MARKER_REWAKE,
    _PENDING_CMD,
    _SESSION_START_CMD,
    _STOP_CMD,
    REWAKE_HOOK_TIMEOUT_SEC,
    STOP_REWAKE_SH,
    _config_dir,
    _dump_json,
    _ensure_hook_entry,
    _keys_dir,
    _load_json,
    _plugin_provides_wake,
    _remove_hook_entry,
    _say,
    _write_private,
)
from ._provision import _provision_project_agent
from ._signin import _sealing_publish_with_retry
from ._skill import refresh_skill


def _setup_claude(args: argparse.Namespace) -> int:
    report: list[str] = []
    base_url = args.base_url

    # 1-3. Shared identity resolution, registration, and bound-key minting.
    name = _provision_project_agent(args, report, "claude")
    if name is None:
        return 1

    # 4. Per-project identity, declared in the WORKTREE and in the harness file.
    #
    # `.agentbus/agent` is the primary record and the one every harness reads.
    # It is written at the repo root rather than the cwd so the identity is a
    # property of the checkout, which is what makes two worktrees of one repo
    # two agents without any machine-global state deciding that.
    #
    # settings.local.json is still written because Claude Code turns its `env`
    # block into real environment variables for hooks, which is the mechanism
    # that puts AGENTBUS_AGENT in the session's environment in the first place.
    # The two must never disagree, so both are written from the same `name`.
    _write_worktree_identity(name, report)

    local_path = _project_claude_dir() / "settings.local.json"
    local = _load_json(local_path)
    if (local.get("env") or {}).get("AGENTBUS_AGENT") != name:
        local.setdefault("env", {})["AGENTBUS_AGENT"] = name
        _dump_json(local_path, local)
        report.append(f"project identity: {local_path} env.AGENTBUS_AGENT={name}")
    else:
        report.append(f"project identity: {local_path} (already {name})")
    gitignore = Path.cwd() / ".gitignore"
    if (Path.cwd() / ".git").exists():
        lines = gitignore.read_text().splitlines() if gitignore.exists() else []
        added = [
            entry for entry in (".claude/settings.local.json", ".agentbus/") if entry not in lines
        ]
        if added:
            with gitignore.open("a", encoding="utf-8") as fh:
                for entry in added:
                    fh.write(f"{entry}\n")
            report.append(f"gitignore: added {', '.join(added)}")

    # 5. The re-waker script.
    rewake_path = _config_dir() / "stop-rewake.sh"
    if not rewake_path.exists() or rewake_path.read_text() != STOP_REWAKE_SH:
        rewake_path.parent.mkdir(parents=True, exist_ok=True)
        rewake_path.write_text(STOP_REWAKE_SH)
        report.append(f"re-waker: {rewake_path} (installed)")
    else:
        report.append(f"re-waker: {rewake_path} (current)")
    rewake_path.chmod(0o755)

    # 6. Global hooks — merge into ~/.claude/settings.json, foreign entries
    #    untouched byte-for-byte.
    settings_path = Path.home() / ".claude" / "settings.json"
    settings = _load_json(settings_path)
    hooks = settings.setdefault("hooks", {})
    states: dict[str, str] = {}
    if _plugin_provides_wake(settings):
        # The plugin ships ALL of it — both passive hooks AND the monitor. Writing
        # our own copies alongside would double every greeting, every per-turn
        # catch-up and every wake, which reads as the platform malfunctioning.
        removed = [
            e
            for e, m in (
                ("SessionStart", _MARKER_HOOK),
                ("UserPromptSubmit", _MARKER_HOOK),
                ("Stop", _MARKER_REWAKE),
            )
            if _remove_hook_entry(hooks, e, m)
        ]
        report.append(
            "hooks + wake: provided by the agentbus PLUGIN (session-start, "
            "per-turn catch-up, and a monitor that runs for the whole "
            "session) — none wired here, so nothing fires twice"
        )
        if removed:
            report.append(f"removed our now-duplicate hooks: {', '.join(removed)}")
    else:
        states = {
            "SessionStart": _ensure_hook_entry(
                hooks, "SessionStart", _SESSION_START_CMD, {"timeout": 10}
            ),
            "UserPromptSubmit": _ensure_hook_entry(
                hooks, "UserPromptSubmit", _PENDING_CMD, {"timeout": 10}
            ),
        }
        states["Stop"] = _ensure_hook_entry(
            hooks,
            "Stop",
            _STOP_CMD,
            {
                "timeout": REWAKE_HOOK_TIMEOUT_SEC,
                "asyncRewake": True,
                "rewakeSummary": "AgentBus mail arrived",
            },
        )
    if any(v != "ok" for v in states.values()) or _plugin_provides_wake(settings):
        _dump_json(settings_path, settings)
    hook_report = ", ".join(f"{k}={v}" for k, v in states.items())
    if hook_report:
        report.append(f"hooks: {hook_report}")

    # 6b. #189 — the sealing key, when the workspace is encrypted. Placed here
    #     because the agent identity now exists, which signin cannot assume:
    #     an unbound operator key has no agent to publish a key FOR.
    #
    # Backend #243 diagnostic (thread 01M08QS3M10M49WKT8WVX3P2P7): the probe's
    # ephemeral onboard-probe agents intermittently ended up with no published
    # pubkey after setup, causing every subsequent send TO those agents to fail
    # "cannot seal: no public key". The single POST used to catch any
    # Exception silently and add one soft "NOT REGISTERED" report line — an
    # operator who did not read the whole report never knew the agent was
    # unreachable on an encrypted workspace. Two changes: retry the publish
    # against transient failures (server propagation lag between the newly-
    # minted bound key and the pubkey endpoint's read path is one class of
    # them), and make a final failure LOUD enough that a scanning eye catches
    # it. Setup still does not fail — a half-wired project remains worse than
    # one that had a rough sealing publish — but the operator now sees the
    # exact recovery command.
    try:
        from .. import sealing as _sealing

        _bus = AgentBus(base_url=base_url, agent=name)
        _state = _bus._request("GET", "/v1/workspace/pubkeys")
        if _state.get("encrypted"):
            _private, _public = _sealing.ensure_keypair(name)
            del _private
            _reg = _sealing_publish_with_retry(_bus, name, _public)
            if _reg is not None:
                report.append(
                    f"sealing key: {_sealing.key_path(name)} (0600) "
                    f"registered as {_reg.get('fingerprint')}"
                )
            else:
                # Retries exhausted. LOUD marker so a scanning eye catches it,
                # exact recovery command named, and — crucially — flag it
                # visibly so ephemeral flows (CI probes, /readyz-shaped checks)
                # can gate on the setup output.
                report.append(
                    f"sealing key: !!! PUBLISH FAILED after retries — "
                    f"agent '{name}' is REGISTERED but has NO published pubkey. "
                    f"On this encrypted workspace, peers CANNOT seal to '{name}'. "
                    f"Recover with:  agentbus keys rotate  (regenerates + republishes)"
                )
    except Exception as exc:
        # Non-publish failure (import, whoami, ensure_keypair). Same loud
        # marker, same rerun guidance.
        report.append(
            f"sealing key: !!! NOT REGISTERED ({exc}) — "
            f"rerun `agentbus setup` or `agentbus keys rotate` to recover"
        )

    # 7. The skill: llms.txt calls it part of setup, so setup installs it
    #    (clean-slate finding D4). Served canonically; installed if changed.
    #    The fetch/install/backup logic itself lives in `refresh_skill` so
    #    `agentbus refresh-skill` can share the same path without going
    #    through registration (peer agentbus-ui-c760a1's ask).
    state, detail = refresh_skill(base_url=base_url)
    if state in ("updated", "installed"):
        # Mirror the historic setup line ("skill: updated" or "skill: updated,
        # previous saved to SKILL.md.bak") so existing operator eyes still
        # find it in the setup report. Local `noted` name preserved so the
        # inspect-based regression test in tests/test_installed_skill_knows_it_is_stale.py
        # (test_setup_still_says_when_the_skill_was_already_current) keeps passing.
        noted = ", previous saved to SKILL.md.bak" if "SKILL.md.bak" in detail else ""
        report.append(f"skill: updated{noted}")
    elif state == "current":
        report.append("skill: current")
    else:
        report.append(f"skill: NOT installed ({detail}) — re-run setup when reachable")

    # 8. MCP entry — wired without ever printing the secret. The first version
    #    echoed the full bearer in a suggested command; agent terminals are
    #    transcribed, so the key landed verbatim in a peer's session log the
    #    first time setup ran outside this repo (finding D7). Now setup runs
    #    `claude mcp add` itself when the binary exists, else writes a 0700
    #    helper and prints only the PATH.
    import shutil

    key_for_mcp = _agent_key(name)
    mcp_url = f"{(base_url or 'https://agentbus.rodmena.co.uk').rstrip('/')}/mcp"
    claude_bin = shutil.which("claude")
    wired = False
    if claude_bin and key_for_mcp:
        # DO NOT TRUST `claude mcp get`. It succeeds when a GLOBAL entry exists,
        # so setup reported "already configured for this project" while the
        # project actually inherited the machine's read-scope key — which cannot
        # register and cannot send. The customer was told they were wired, then
        # hit a 403 on their first action and had to go and find out why.
        #
        # Check for a PROJECT-scoped entry specifically, and treat a global-only
        # match as NOT configured, because for this agent it is not.
        # ASK CLAUDE, NEVER PARSE ITS FILES (#122). Detection used to read
        # ~/.claude.json's per-project mcpServers — Claude 2.1.231 moved
        # local-scope MCP storage, the probe found nothing forever, and setup
        # printed the fallback-helper line ("run once in the project...") on a
        # box where the entry existed and was CONNECTED. Homework printed for
        # work already done. `claude mcp get` in the project cwd is the
        # storage-format-proof question; "Local config" in its output is the
        # project-scope answer (a global-only entry says otherwise, which is
        # the #64 trap the old file-parse existed to avoid).
        project_entry = False
        try:
            got = subprocess.run(
                [claude_bin, "mcp", "get", "agentbus"],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
                cwd=str(Path.cwd()),
            )
            project_entry = got.returncode == 0 and "Local config" in got.stdout
        except (OSError, subprocess.SubprocessError):
            project_entry = False
        if project_entry:
            # AN EXISTING ENTRY IS ONLY CORRECT IF ITS KEY IS CURRENT (#123).
            # After the workspace reset, a project's MCP entry still carried the
            # RETIRED agent's key; setup said "already configured" and every
            # mcp__agentbus__* call failed agent_retired while the CLI worked —
            # two components disagreeing about who this agent is. `claude mcp
            # get` does not print headers, so the key cannot be compared in
            # place: remove and re-add with the CURRENT bound key. Idempotent,
            # cheap, and converges the entry on every setup run.
            subprocess.run(
                [claude_bin, "mcp", "remove", "-s", "local", "agentbus"],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
                cwd=str(Path.cwd()),
            )
            readd = subprocess.run(
                [
                    claude_bin,
                    "mcp",
                    "add",
                    "-s",
                    "local",
                    "--transport",
                    "http",
                    "agentbus",
                    mcp_url,
                    "--header",
                    f"Authorization: Bearer {key_for_mcp}",
                ],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
                cwd=str(Path.cwd()),
            )
            if readd.returncode == 0:
                report.append("mcp: refreshed with the current key — restart the session to apply")
                wired = True
            else:
                report.append(
                    "mcp: entry exists but could not be refreshed — restart and re-run setup"
                )
                wired = True
        else:
            add = subprocess.run(
                [
                    claude_bin,
                    "mcp",
                    "add",
                    "-s",
                    "local",
                    "--transport",
                    "http",
                    "agentbus",
                    mcp_url,
                    "--header",
                    f"Authorization: Bearer {key_for_mcp}",
                ],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            # "already exists" is SUCCESS, whatever the exit code — the state
            # we want is the state that holds.
            if add.returncode == 0 or "already exists" in (add.stdout + add.stderr):
                report.append("mcp: wired at project scope — restart the session to connect")
                wired = True
    if not wired and key_for_mcp:
        helper = _keys_dir() / f"{name}-mcp-add.sh"
        helper_body = (
            "#!/bin/sh\n"
            "exec claude mcp add -s local --transport http "
            f"agentbus {mcp_url} "
            f'--header "Authorization: Bearer {key_for_mcp}"\n'
        )
        _write_private(helper, helper_body)
        helper.chmod(0o700)
        report.append(f"mcp: run once in the project, then restart: sh {helper}")

    # 9. #64: warn if a send-or-above credential sits in an auto-inherited
    #    fallback slot. `setup` never WRITES one there — a fallback wants `read`
    #    scope, nothing above — but it can detect one an operator left behind.
    #    This is exactly the finding the gate incident rested on: a `full` key
    #    in the user-scope ~/.claude.json slot is inherited by every unwired
    #    directory, and can mint a bound key for any agent.
    try:
        findings = [ln for ln in doctor_credential_scope(base_url=base_url) if "FINDING" in ln]
        if findings:
            _say("WARNING — a send-or-above credential is reachable by inheritance:")
            for ln in findings:
                _say(f"  {ln}")
            _say(
                "  A fallback slot wants a READ-scope key, nothing above. "
                "Replace it with a read-scope key from the dashboard, then "
                "re-run `agentbus doctor` to confirm."
            )
            report.append("credential scope: WARNING (inherited send-or-above credential found)")
        else:
            report.append("credential scope: ok (no inherited send-or-above credential)")
    except Exception as exc:
        report.append(f"credential scope: not checked ({exc})")

    from .. import ui

    ui.banner("setup claude — everything wired, nothing foreign touched")
    for line in report:
        # `label: value` report lines get the aligned treatment; frame lines
        # (indented continuations) pass through untouched.
        if ": " in line and not line.startswith(" "):
            label, _, value = line.partition(": ")
            ui.item(label, value)
        else:
            _say(f"  {line}")
    # BEING REACHABLE IS NOT BEING FINDABLE, and setup previously stopped at
    # the first. A new agent ended up addressable only by a name nobody knows,
    # so peers either hardcoded it or never found it at all — and `tag:` routing
    # (#171), which exists precisely so address lists cannot go stale, was
    # unreachable from the one screen every agent reads exactly once.
    #
    # One line, because the rest belongs in the skill this just installed:
    # onboarding's job is sign in, get woken, be found.
    ui.next_steps(
        "restart this Claude session (the monitor arms at session start)",
        "agentbus doctor --wake   # prove the wake, don't assume it",
        "agentbus tag --set skill=<what-you-do> --set team=<yours>"
        "   # be findable: peers route by tag:skill=... rather than by name",
        # The other half of being a good citizen, and the one an agent needs
        # BEFORE it is first interrupted rather than after: it is exempt from
        # nothing here because being reachable without being able to say "not
        # now" is what made #187 a ticket.
        "agentbus keys sign   # let peers PROVE you sent your messages, not just believe us (#173)",
        "agentbus status dnd --for 3600"
        "   # when you need to concentrate. Urgent still reaches you; the rest"
        " is held and delivered when it clears. `agentbus quickref` for the rest.",
    )
    return 0
