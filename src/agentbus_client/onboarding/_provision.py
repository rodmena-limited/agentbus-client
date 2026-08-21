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
import re
from pathlib import Path

from ..client import AgentBus, AgentBusError
from ._identity import (
    _agent_key,
    _derived_name,
    _operator_key,
    _resolve_agent_name,
    _signed_in_bound_agent,
)
from ._paths import _config_dir, _device_hash, _git_remote_or_none, _keys_dir, _say, _write_private


def _provision_project_agent(
    args: argparse.Namespace, report: list[str], harness: str
) -> str | None:
    """The harness-independent core of setup: resolve the project's agent
    identity, register it, and ensure its bound key exists.

    Steps 1-3 of `_setup_claude`, shared verbatim by every harness so the
    trickiest logic — identity resolution, the reinstall guard (D3), the
    bound-key-refusal message — has ONE implementation, not one per harness.
    Returns the agent name on success, or None after printing the refusal.
    """
    base_url = args.base_url

    # 1. Who is this project's agent, and with which credential?
    provenance: list[str] = []
    name = _resolve_agent_name(explain=provenance)
    role = getattr(args, "role", None)
    operator = _operator_key()
    if name is None and role is None:
        # DERIVE THE ROLE FROM THE DIRECTORY NAME rather than failing (#116).
        #
        # The installer and signin both print `agentbus setup claude` BARE as
        # the next step; on the FreeBSD onboarding the operator ran exactly
        # that and was refused with "pass a role" — we printed a command that
        # fails. The directory basename IS the operator's chosen project name:
        # deriving from it is the same doctrine as everything else here
        # (identity from machine+repo+path, never asked twice), it is strictly
        # project-local (nothing machine-global to leak across checkouts, the
        # #90/#95 failure family), it is deterministic on reopen, and it is
        # visible in the agent name it produces — a wrong guess announces
        # itself. Sanitized to the agent charset; a name that sanitizes away
        # (e.g. all CJK) falls through to the explicit-role error.
        derived = re.sub(r"[^a-z0-9._-]", "-", Path.cwd().name.lower()).strip("-._")[:40]
        if derived:
            role = derived
            _say(f"role derived from this directory's name: {role}")
            _say(
                f"  (pass --role <name> to choose differently: agentbus setup {harness} --role <name>)"
            )
    if name is None and role is None:
        _say("cannot determine this project's agent identity.")
        _say("  Either sign in with an agent-bound key (agentbus signin <key>),")
        _say(f"  or pass a role so one can be derived: agentbus setup {harness} --role <role>")
        return None

    ephemeral = os.environ.get("AGENTBUS_SETUP_EPHEMERAL") == "1"

    # VALIDATE THE STORED KEY BEFORE IT WINS OVER THE OPERATOR KEY (#110).
    #
    # A per-agent key file used to be trusted on existence alone. After the
    # 2026-08-13 workspace reset this host held 46 dead key files, and setup in
    # a previously-wired project picked the dead file over the operator key the
    # operator had verified SECONDS earlier — then failed with 'API key is
    # unknown or revoked', one command after signin printed VERIFIED. A file on
    # disk is a record that a key EXISTED, not that it still works; only the
    # bus can say that. On rejection, say which file was dead and fall through
    # to the operator credential — that is what it is for.
    stored_key = _agent_key(name) if name else None
    if stored_key and operator and stored_key != operator:
        from ..client import AuthError

        try:
            mine = AgentBus(api_key=stored_key, base_url=base_url).whoami()
            # AND DOES IT BELONG TO THE WORKSPACE WE JUST SIGNED INTO?
            #
            # "Is the key alive" is not the question. A stored key for a
            # DIFFERENT but still-live workspace answers whoami() perfectly
            # happily, and setup would then register this agent into that other
            # workspace while printing the one the operator actually signed
            # into. A silent wrong-workspace registration is worse than the
            # clear failure that led here.
            #
            # The operator typed a key seconds ago and watched it verify. That
            # is the intent; a file left on disk from a previous life is not.
            here = AgentBus(api_key=operator, base_url=base_url).whoami()
            if mine.get("workspace") != here.get("workspace"):
                _say(
                    f"  stored key for '{name}' belongs to workspace "
                    f"'{mine.get('workspace')}', not '{here.get('workspace')}'; using the"
                )
                _say("  operator credential instead. A fresh bound key will be minted.")
                with contextlib.suppress(OSError):
                    (_keys_dir() / f"{name}.env").rename(_keys_dir() / f"{name}.env.other")
                stored_key = None
        except AgentBusError as exc:
            # WHICH FAILURES PROVE THE KEY IS DEAD.
            #
            # An auth rejection does. So does `workspace_deleted`: the key is
            # perfectly well-formed, it just belongs to a workspace that no
            # longer exists, and no amount of retrying will change that.
            #
            # This used to catch only AuthError. 410 is not in the client's
            # error map, so a deleted workspace arrived as a plain
            # AgentBusError and hit `pass` — the guard written for exactly this
            # case could not see it. Measured on a macOS box that still held a
            # bound key from a deleted workspace: signin verified the NEW key
            # and printed the NEW workspace, then setup registered with the OLD
            # stored key and failed with "this workspace has been deleted",
            # naming a file the operator had no reason to suspect.
            #
            # A TransportError still says NOTHING about the key (bus down,
            # DNS), and renaming a good credential on that evidence would
            # destroy it every time the bus blipped. So the test stays narrow:
            # an explicit verdict from the server, never a failure to reach it.
            dead = isinstance(exc, AuthError) or getattr(exc, "code", "") in (
                "workspace_deleted",
                "workspace_suspended",
            )
            if dead:
                _say(f"  stored key for '{name}' is dead ({exc.code}); using the operator")
                _say("  credential instead. A fresh bound key will be minted.")
                with contextlib.suppress(OSError):
                    (_keys_dir() / f"{name}.env").rename(_keys_dir() / f"{name}.env.dead")
                stored_key = None
    register_key = stored_key or operator
    if register_key is None:
        target = name or _derived_name(role) or f"<role '{role}'>"
        bound = _signed_in_bound_agent()
        _say(f"cannot provision {target} in this project.")
        if bound:
            # The real problem, which the old message named neither: they signed
            # in with a key BOUND to a different agent, so nothing on this
            # machine is allowed to create or credential this one. The old text
            # said "no credential for 'None' ... run signin", and signin is the
            # command that had just succeeded — so the user re-ran it with the
            # same key and hit the same wall.
            _say(f"  You signed in with a key BOUND to '{bound}'. A bound key can act")
            _say(f"  only as '{bound}', so it cannot register or mint a key for another")
            _say("  agent — which is exactly what a second project needs.")
            _say("")
            _say("  Fix: sign in with your WORKSPACE key instead. setup then provisions")
            _say("  each project its own agent and its own bound key, automatically:")
            _say("      agentbus signin <workspace key>")
            _say(f"      agentbus setup {harness}")
        else:
            _say("  No credential is stored for this machine yet.")
            _say("  Fix: agentbus signin <workspace key>")
        return None

    # 2. Register (idempotent server-side; derivable identity when role-based).
    bus = AgentBus(api_key=register_key, base_url=base_url)

    # Reinstall guard (clean-slate finding D3): identity derives from the
    # device-id file, so a wiped config directory plus a role-based setup
    # silently mints a STRANGER while the original agent goes quiet holding
    # its inbox — and the failure prints as success. If an active agent for
    # this role and repo already exists under a DIFFERENT device-id, stop and
    # make the choice explicit instead of guessing.
    if name is None and role and not getattr(args, "force_new", False):
        from .. import identity

        remote = _git_remote_or_none()
        fingerprint = identity.repo_fingerprint(remote) if remote else None
        local_device = identity.device_id()
        try:
            peers = bus.phonebook(repo_fingerprint=fingerprint) if fingerprint else []
        except AgentBusError:
            peers = []
        strangers = [
            p
            for p in peers
            if p.get("role") == role
            and p.get("device_hash")
            and p["device_hash"] != _device_hash(local_device)
            and not p.get("ephemeral")
        ]
        if strangers:
            existing = strangers[0]["name"]
            _say(
                f"STOP: an agent '{existing}' for role '{role}' on this repo already "
                "exists, registered from a DIFFERENT device-id."
            )
            _say(
                "  If this machine was reinstalled, its identity file was lost. "
                f"Restore {_config_dir() / 'device-id'} from backup and re-run setup — "
                "it will recover the SAME agent, address and inbox."
            )
            _say(
                "  To deliberately create a NEW agent instead: "
                f"agentbus setup {harness} --role <role> --force-new"
            )
            _say("Nothing was changed.")
            return None
    try:
        result = bus.register(
            name,
            role=role or (name if name else None),
            workdir=str(Path.cwd()),
            repo_remote=_git_remote_or_none(),
            ephemeral=True if ephemeral else None,
            persona=getattr(args, "persona", None),
        )
    except AgentBusError as exc:
        _say(f"registration failed: {exc}")
        # #109: NAME THE CREDENTIAL THAT FAILED. 'API key is unknown or
        # revoked' with no source sent the operator hunting — was it the
        # stored per-agent file, or the signin credential that printed
        # VERIFIED a minute ago? Two different remedies; the message named
        # neither. The key_id identifies WHICH key without exposing a secret:
        # keys are `ab_sk_<key_id>_<secret>` and the dashboard Keys page lists
        # the key_id half in full.
        key_id = ""
        with contextlib.suppress(Exception):
            if register_key.startswith("ab_sk_"):
                key_id = register_key.split("_")[2]
        if register_key and register_key == stored_key and name:
            _say(f"  credential used: stored key file {_keys_dir() / (name + '.env')}")
        elif register_key == operator:
            _say("  credential used: the signin/operator credential (agentbus signin)")
        if key_id:
            _say(f"  key id: {key_id}…  (revoke/inspect it in the dashboard Keys page)")
        return None
    registered = str(result["agent"]["name"])
    if name and registered != name:
        # NEVER adopt a rename (#90) — but DISTINGUISH RECOGNITION FROM
        # RENAMING (#156). Farshid hit this on the tokengate repo: the server
        # answered an EXISTING row it had matched by session identity, and
        # this branch told him his name was possibly "invalid" — a refusal
        # worded for the renaming case, aimed at the recognition case. The
        # discriminator is measurable: the register response carries the
        # answered row's device_hash (sha256 of its stored device_id) — equal
        # to OUR device's hash means the server recognized this machine's
        # session, not renamed a stranger.
        recognized = False
        with contextlib.suppress(Exception):
            import hashlib

            from ..identity import device_id as _device_id

            local_device = _device_id()
            recognized = bool(
                local_device
                and result["agent"].get("device_hash")
                == hashlib.sha256(local_device.encode()).hexdigest()
            )
        if recognized:
            _say(
                f"The server RECOGNIZES this machine and project as '{registered}' — an "
                f"identity that already exists here (same device, matched by session "
                f"identity). You asked for '{name}', so nothing was wired without your say."
            )
            changed = result.get("identity_changed")
            if changed:
                _say(f"  what moved since it was registered: {', '.join(changed)}")
            _say("  ADOPT the existing identity (keeps its address, inbox and history):")
            _say(
                f"    mkdir -p .agentbus && printf '%s\\n' '{registered}' > .agentbus/agent"
                "   # then re-run this setup"
            )
            _say("  or keep your requested name by retiring the old identity first:")
            _say(f"    agentbus retire {registered} --agent {registered}")
            return None
        _say(
            f"REFUSING: asked to register '{name}' but the server answered "
            f"'{registered}'. The identity you name is the identity you get — "
            "nothing was wired. If the name is invalid, the server's error "
            "says the exact rename; choose it yourself and re-run."
        )
        return None
    name = registered
    report.extend(provenance)
    report.append(f"registered: {name}  ({result['address']})")

    # F10, SECOND PATH. `agentbus register --persona` learned to report a
    # silently-dropped persona; SETUP did not, and setup is the command people
    # actually run — it is what the skill and the docs tell a new agent to type.
    # So the fix landed on the quieter entrypoint and missed the loud one.
    #
    # Farshid hit exactly this: `agentbus setup claude --role retunnel-tester
    # --persona test-engineer` printed a full success panel with no persona line
    # at all, and the session that came up reported `persona: null`. Nothing in
    # that output was false; the panel simply had no opinion about the one flag
    # that had not worked, so a clean-looking setup silently did nine things and
    # dropped the tenth.
    #
    # Persona is admin-only POLICY (backend #264): a non-admin write is DROPPED
    # rather than rejected, so an old client passing it does not break. Same
    # advisory shape as the register path, and the same forward-compatible
    # fallback for servers predating the `persona_ignored` field.
    requested_persona = getattr(args, "persona", None)
    if requested_persona:
        advisory = result.get("persona_ignored")
        if advisory:
            report.append(f"PERSONA NOT SET: {advisory}")
        elif not (result.get("agent") or {}).get("persona"):
            report.append(
                f"PERSONA NOT SET: asked for '{requested_persona}' and the agent "
                f"came back without one. Setting a persona needs an ADMIN-scope "
                f"key (it is policy, not self-service) — check yours with "
                f"`agentbus whoami`, or ask an operator."
            )
        else:
            report.append(f"persona: {requested_persona}")

    # 3. Ensure the agent's own bound key exists (mint with the operator key
    #    if needed — minting de-escalates; this is its designed use).
    if _agent_key(name) is None:
        if operator is None:
            _say(f"'{name}' has no key file and there is no operator credential to mint one.")
            _say("  Sign in with the workspace key once: agentbus signin <key>")
            return None
        op_bus = AgentBus(api_key=operator, base_url=base_url)
        try:
            minted = op_bus.mint_key(scope="send", agents=[name], label=f"setup-{name}")
        except AgentBusError as exc:
            _say(f"could not mint a bound key for '{name}': {exc}")
            return None
        secret = minted.get("key") or minted.get("api_key")
        if not secret:
            _say("mint succeeded but no secret in the response; refusing to continue.")
            return None
        _write_private(
            _keys_dir() / f"{name}.env",
            f"export AGENTBUS_API_KEY={secret}\nexport AGENTBUS_AGENT={name}\n",
        )
        report.append(f"minted bound send key for {name} -> {_keys_dir()}/{name}.env (0600)")
    else:
        report.append(f"key: {_keys_dir()}/{name}.env (existing)")
    # The agent name from register() is server-returned; coerce to str so mypy
    # can see the function's str | None contract instead of Any.
    return str(name)
