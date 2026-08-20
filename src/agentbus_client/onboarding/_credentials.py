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

import os
from pathlib import Path

from ..client import AgentBusError
from ._identity import _agent_key, _operator_key, _project_claude_dir, _session_identity
from ._paths import _config_dir, _keys_dir, _load_json


def resolve_credentials(preferred_agent: str | None = None) -> tuple[str | None, str | None]:
    """(api_key, agent) for ordinary CLI verbs when nothing was passed or
    exported. Signin promised "once per machine"; before this fallback the
    promise held only for the two onboarding commands, and every plain verb
    still demanded a hand-exported secret — the exact friction the flow
    exists to delete (clean-slate finding D2).

    Order: this session's OWN identity key file (project setting, else the
    signin default), then the operator credential. Asking to act as a DIFFERENT
    agent never loads that agent's key file — see below.
    """
    # WHO THIS SESSION IS, decided WITHOUT reference to what was asked for.
    # `--agent NAME` used to feed straight into the key lookup, so asking to act
    # as a peer silently loaded THAT PEER'S PRIVATE KEY from keys/NAME.env and
    # acted as them. On a shared host `agentbus --agent mailapi whoami` returned
    # mailapi's identity to a caller holding no credential of their own.
    #
    # `--agent` is an ASSERTION OF IDENTITY, the same as the X-AgentBus-Agent
    # header — it says which agent to act as, not "go find that agent's
    # credential". Conflating the two made cross-agent action the path of least
    # effort rather than a deliberate act. Reported by `runflow`, read-only,
    # with a reproduction.
    #
    # The file permissions were never the control here: every key file is 0600
    # under one UID, so anyone who can run the CLI could already read them. What
    # changed is that you now have to mean it.
    identity = _session_identity()
    # SPECS/0038 + stabilize's container finding: the operator exports
    # AGENTBUS_AGENT as this session's identity ("for one command"). In a fresh
    # container or host with NO project settings.local.json, NO signin, and NO
    # operator.env, that env var is the ONLY record of who this session is — and
    # without it the bound key mounted alongside could never be auto-loaded,
    # forcing a hand source (`set -a; . keys/<agent>.env`). The env var names the
    # session's OWN identity, so loading ITS bound key is exactly the #67
    # guarantee: `--agent PEER` (a DIFFERENT identity) still never loads PEER's
    # key — that requires the operator credential.
    if identity is None:
        env_identity = os.environ.get("AGENTBUS_AGENT")
        if env_identity:
            identity = str(env_identity)
    agent = preferred_agent or identity

    # Auto-load a bound key ONLY for the identity this session actually holds.
    if identity and (not preferred_agent or preferred_agent == identity):
        key = _agent_key(str(identity))
        if key:
            return key, str(identity)

    # Asking to act as somebody else is legitimate — with an OPERATOR key, which
    # may act as any agent by design. It is not legitimate by borrowing their
    # bound credential. Fall through to the operator key, or to nothing.
    op = _operator_key()
    if op:
        return op, agent if agent else None
    return None, None


def doctor_credential_scope(base_url: str | None = None) -> list[str]:
    """#64: what credentials are reachable from the CURRENT directory, at what
    scope — the finding the incident rested on.

    The auto-inherited fallback (user-scope ~/.claude.json / opencode.json MCP
    entry) is exactly the slot that held an UNBOUND OPERATOR key: anything
    running in an unwired directory inherits it, and a `full` key can MINT a
    bound key for any agent and send at platform_attested (forging the
    attestation the whole bus rests on). `agentbus setup` neither discourages
    nor detects a `full` key sitting in that slot.

    Report, naming which config supplied it and at what scope:
      1. this directory's OWN wiring   — .claude/settings.local.json env
      2. the user-scope fallback       — ~/.claude.json projects-less MCP entry
      3. the opencode fallback         — ~/.config/opencode/opencode.json(.c)
      4. the operator credential       — ~/.config/agentbus/operator.env

    The scope of each credential is resolved against the LIVE bus via /whoami
    (the server's own read-only answer, never guessed from the key string — a
    bearer token does not encode scope). A credential reachable by inheritance
    with scope send-or-above is a finding. This is a REPORT (no mutation); the
    scope is always the credential's own, and a bound key is never `full`.
    """
    lines: list[str] = []
    try:
        # 1. THIS DIRECTORY's own wiring.
        local = _load_json(_project_claude_dir() / "settings.local.json")
        proj_agent = (local.get("env") or {}).get("AGENTBUS_AGENT")
        if proj_agent:
            lines.append(
                f"project ({_project_claude_dir()}/settings.local.json): agent {proj_agent}"
            )
    except Exception:
        pass

    # 2. User-scope fallback in ~/.claude.json (Claude). A projects-less MCP
    #    entry is inherited by every directory that has no its-own block.
    try:
        import json as _json

        cj = Path.home() / ".claude.json"
        if cj.exists():
            data = _json.loads(cj.read_text())
            # {global,project} less entry for 'agentbus' -> auto-inherited.
            entry = (data.get("mcpServers") or {}).get("agentbus")
            if entry:
                header = (entry.get("headers") or {}).get("Authorization") or ""
                scope = _scope_of_bearer(header, base_url)
                lines.append(
                    f"user-scope ~/.claude.json agentbus MCP: {scope}" + _inherited_flag(scope)
                )
    except Exception:
        pass

    # 3. opencode fallback config.
    try:
        import json as _json
        import re as _re

        for name in ("opencode.jsonc", "opencode.json"):
            p = Path.home() / ".config" / "opencode" / name
            if not p.exists():
                continue
            text = p.read_text()
            text = _re.sub(r"/\*.*?\*/", "", text, flags=_re.DOTALL)
            text = _re.sub(r"//.*", "", text)
            data = _json.loads(_re.sub(r",(\s*[}\]])", r"\1", text))
            entry = (data.get("mcp") or {}).get("agentbus")
            if entry:
                header = (entry.get("headers") or {}).get("Authorization") or ""
                scope = _scope_of_bearer(header, base_url)
                lines.append(f"opencode {name} agentbus MCP: {scope}" + _inherited_flag(scope))
            break
    except Exception:
        pass

    # 4. The operator credential on disk (deliberate, 0600, not inherited).
    op_path = _config_dir() / "operator.env"
    if op_path.exists():
        scope = "full (operator.env: can MINT — never auto-inherit it)"
        lines.append(f"operator: {op_path} — {scope}")
    return lines


def _inherited_flag(scope: str) -> str:
    """#64: a send-or-above credential reachable by inheritance is a finding —
    and the finding must NAME the escalation, not just label the slot. A `full`
    key in an auto-inherited slot can mint a bound key for any agent and then
    send at platform_attested, forging the attestation the whole bus rests on.
    """
    if scope in ("send", "full", "admin"):
        return (
            "  — FINDING: reachable by inheritance; a "
            + {
                "send": "send-scope key in an inherited slot can act as any "
                "agent it is not bound to — this slot wants `read`, "
                "nothing above",
                "full": "`full` key here can MINT a bound key for any agent "
                "and send at platform_attested — this slot wants "
                "`read`, nothing above",
                "admin": "`admin` key here is worse than full: it can revoke "
                "and purge, and is inherited by every unwired "
                "directory",
            }[scope]
        )
    return ""


def _scope_of_bearer(bearer: str, base_url: str | None = None) -> str:
    """Resolve the scope of a bearer credential against the LIVE bus via
    /whoami — the server's own read-only answer, since a `ab_sk_...` token does
    not encode scope. Failures report the state honestly (unusable,
    unreachable) rather than guessing.
    """
    import re as _re

    m = _re.search(r"ab_sk_[A-Za-z0-9_]+", bearer or "")
    if not m:
        return "unknown"
    try:
        from ..client import AgentBus

        who = AgentBus(api_key=m.group(0), base_url=base_url).whoami()
        scope = (who.get("key") or {}).get("scope")
        return scope if isinstance(scope, str) else "unknown"
    except AgentBusError as exc:
        # invalid_api_key is the SERVER's code (src/agentbus/errors.py) for a
        # missing, malformed or revoked key. "unusable" — the credential does
        # not work; "unreachable" — the bus could not be asked at all.
        if exc.code in ("invalid_api_key", "forbidden", "not_found"):
            return f"unusable ({exc.code})"
        return f"unreachable ({exc.code})"
    except Exception:
        return "unknown (resolution failed)"


def explain_refusal(preferred_agent: str | None) -> str | None:
    """Why no credential was resolved, when the reason is the cross-agent guard.

    A refusal that surfaces as the generic "no API key: set AGENTBUS_API_KEY"
    teaches the wrong lesson — the operator exports a key, or worse copies the
    peer's key file, and the guard is defeated by the very message meant to
    explain it. So when the reason is specifically "you asked to be somebody
    else", say that, and say what the two legitimate routes are.
    """
    if not preferred_agent:
        return None
    if not (_keys_dir() / f"{preferred_agent}.env").exists():
        return None

    # DO NOT LEAD WITH "GO AND SOURCE A CREDENTIAL".
    #
    # bob found this text giving the opposite advice to the MCP 403 in the SAME
    # release: that message was rewritten to say explicitly not to go hunting for
    # a key to paste, after sessions did exactly that and one was stopped by a
    # permission classifier that was right to stop it. An agent reading THIS
    # would go and do it, and the wording here is the older, worse one.
    #
    # So the route that needs no secret goes first, and sourcing a key file is
    # labelled an OPERATOR action at a terminal rather than offered as the
    # natural next step for whoever hit the refusal.
    export_hint = (
        f"  * simplest, and needs no credential: run from "
        f"{preferred_agent}'s own project directory, or wire this "
        f"one with `agentbus setup claude --role <role>`\n"
        f"  * OPERATOR ONLY (a human at a terminal — not an agent, "
        f"and not something to go hunting for): source that agent's "
        f"own key file, {_keys_dir()}/{preferred_agent}.env"
    )

    # TWO DIFFERENT REFUSALS, because they have two different fixes and saying
    # the wrong one sends the operator somewhere useless.
    #
    # `futex` reported, and `runflow` reproduced, that running from a directory
    # with no project identity and no signin.json gave "it is not this session's
    # identity" for the caller's OWN agent name. True of the resolver's state
    # and false as a human reads it: they ARE that agent, and the message tells
    # them they are not. The real problem there is that this session has no
    # recorded identity at all, and the fix is to record one — not to go
    # exporting credentials, which is what the other message steers you toward.
    if not _session_identity():
        return (
            f"cannot act as '{preferred_agent}': this session has NO recorded "
            f"identity — no AGENTBUS_AGENT in this project's "
            f".claude/settings.local.json and no signin default — so there is "
            f"nothing to check '{preferred_agent}' against.\n"
            f"This is NOT a claim that you are not {preferred_agent}. It is that "
            f"nothing here says who you are.\n"
            f"  * if this is your project: run `agentbus setup claude` in it\n"
            f"  * if you have signed in on this machine: run from the project "
            f"directory, or re-run `agentbus signin <key>`\n"
            f"{export_hint}"
        )
    return (
        f"refusing to act as '{preferred_agent}': that agent has a bound key on "
        f"this machine, but it is not this session's identity "
        f"(this session is '{_session_identity()}'), and --agent says WHICH "
        f"AGENT TO ACT AS, not 'load that agent's credential'.\n"
        f"{export_hint}"
    )
