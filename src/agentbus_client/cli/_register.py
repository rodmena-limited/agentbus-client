"""The `agentbus` command line client."""

from __future__ import annotations

import argparse
import contextlib
import os
import sys

from . import _common
from ._common import _accept_common_flags_after_subcommand, _git_remote, _print, _print_qr
from ._identity_cmd import cmd_identity


def cmd_device_id(_args: argparse.Namespace) -> int:
    from .. import identity

    print(identity.device_id())
    return 0


def cmd_invite(args: argparse.Namespace) -> int:
    """Mint a one-time join token for an agent that does not exist yet.

    THE OPERATOR HALF OF `join`, AND IT WAS MISSING. The platform could consume
    an enrolment token and gave no ergonomic way to produce one: `agentbus join`
    existed, the dashboard had no invite screen, and the only route was a raw
    curl to POST /v1/invites. So the documented least-privilege path — issue a
    token instead of handing over a workspace key — was one an operator could
    not actually take, and the reasonable thing to do instead was hand over the
    key. A control nobody can reach is not a control.

    Prints the exact command the recipient runs, because the failure this
    prevents is the operator pasting a bare token into chat with no context and
    the recipient guessing at the verb.
    """
    from ..client import AgentBusError

    try:
        result = _common._bus(args).create_invite(role=args.role, ttl_seconds=args.ttl)
    except AgentBusError as exc:
        # NAME THE SCOPE, DO NOT ECHO THE HTTP ERROR. Issuing enrolment needs an
        # UNBOUND key of `full` or `admin` — and the operator most likely to hit
        # this is one running from a session wired with its own bound agent key,
        # for whom "403" alone is indistinguishable from a broken bus.
        print(f"could not create a join token: {exc}", file=sys.stderr)
        print(
            "\nIssuing a join token needs an UNBOUND key with scope 'full' or "
            "'admin'.\nA key bound to an agent cannot issue one, by design: a "
            "bounded credential\nmust not be able to enrol new identities. Check "
            "with:  agentbus whoami",
            file=sys.stderr,
        )
        return 1

    if args.json:
        _print(result, True)
        return 0

    token = result.get("token", "")
    ttl = int(result.get("expires_in_seconds") or 0)
    hours, remainder = divmod(ttl, 3600)
    human = f"{hours}h" if hours and not remainder // 60 else f"{hours}h {remainder // 60}m"
    if not hours:
        human = f"{remainder // 60}m"

    print("join token minted — it creates ONE agent, then stops existing\n")
    print(f"  token:   {token}")
    print(f"  expires: in {human}")
    if result.get("role"):
        print(f"  role:    {result['role']}")
    print("\nSend the recipient this command (the NAME is theirs to choose):\n")
    role_flag = f" --role {result['role']}" if result.get("role") else ""
    print(f"    agentbus join {token} <their-agent-name>{role_flag}\n")
    print("They need no key on their machine — the token IS the credential, and")
    print("it can only create a NEW agent. It can never act as an existing one.")
    print("\nShown once. It is not recoverable — if it is lost, mint another.")
    return 0


def cmd_join(args: argparse.Namespace) -> int:
    """Register THIS session as a NEW agent using a one-time join token.

    The whole point: it needs no pre-existing credential on the machine. The
    operator issues a token, the agent redeems it once, and the key it gets back
    is bound to itself and can act as nothing else. A host provisioned with bound
    keys only — which is what our own guidance recommends — can now onboard.
    """
    import json as _json
    import urllib.error
    import urllib.request

    from ..onboarding import _keys_dir, _write_private

    body = _json.dumps(
        {
            "token": args.token,
            "name": args.name,
            "role": args.role,
            "repo_remote": args.repo_remote,
            "capabilities": args.capability or [],
        }
    ).encode()
    # Resolve the base URL the same way the client does — --base-url defaults to
    # None, and joining is the one command that runs before any client exists.
    from ..client import DEFAULT_BASE_URL

    base = (args.base_url or os.environ.get("AGENTBUS_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    request = urllib.request.Request(
        f"{base}/v1/agents/join",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = _json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = ""
        with contextlib.suppress(Exception):
            detail = _json.loads(exc.read()).get("detail", "")
        print(f"could not join: {detail or exc}", file=sys.stderr)
        return 1

    secret = result.get("api_key")
    agent = (result.get("agent") or {}).get("name") or args.name
    keys_dir = _keys_dir()
    keys_dir.mkdir(parents=True, exist_ok=True)
    # REG-8b (round-3.5 re-audit): sanitize the filename. `agent` here is
    # `args.name` (unvalidated CLI arg) when the server does not echo a name
    # back — a hostile `agentbus join <token> --name "../operator"` used to
    # WRITE to keys/../operator.env and CLOBBER the operator credential.
    # bound_env_filename collapses '..' and '/' to '_', so the write lands
    # inside keys/ regardless.
    from .. import sealing as _sealing

    key_path = keys_dir / _sealing.bound_env_filename(agent)
    if secret:
        # Written 0600 in the same shape `register` uses, so the monitor and the
        # shell hooks already know how to source it. The secret is returned once
        # and is unrecoverable, so failing to persist it here would strand the
        # agent with an identity it cannot authenticate as.
        _write_private(key_path, f"export AGENTBUS_API_KEY={secret}\n")

    print(f"joined as {agent}")
    print(f"  address:  {result.get('address')}")
    print(f"  key:      {key_path} (0600, bound to {agent} — cannot act as anything else)")
    print("  token:    spent; it cannot be used again")
    print()
    print("  ** RESTART THIS SESSION NOW **")
    print("     The monitor picks up its identity when it starts, and this one started")
    print("     before you existed — so it is watching nothing. Until you restart, mail")
    print("     sent to you ARRIVES AND WAITS, and no session is woken by it.")
    return 0


def _print_persona_outcome(result: dict, requested: str | None) -> None:
    """Say when a requested persona did NOT take, instead of reporting success.

    A WRITE THAT REPORTS SUCCESS AND DOES NOTHING IS INDISTINGUISHABLE FROM ONE
    THAT WORKED, and this one had a victim: a peer set `--persona`, read it back
    as null, could not tell "not wired" from "wired and dropped" — those are
    identical from outside — and reasonably concluded the CLI was broken. They
    filed a defect against this repo and went looking in the wrong layer. The
    flag was fine; the server was discarding it.

    Persona is admin-only POLICY (backend #264): a non-admin write is DROPPED
    rather than rejected, deliberately, so an old client passing the field does
    not break. Authorization is checked BEFORE validation, which is why a valid
    lane and a nonsense one behave identically — you never reach the validator.
    That is what made it look unwired rather than unauthorized.

    The server now returns `persona_ignored` explaining the drop (backend
    601008d, verified live). This surfaces it. Printed ONLY when the server says
    so, so a caller who passed no persona sees nothing and it never becomes
    noise — the same rule that keeps `secret_warning` readable.

    The fallback matters as much as the happy path: against a server that
    predates 601008d there is no `persona_ignored` field, so a silently
    dropped persona would still print nothing. When we ASKED for a lane and the
    agent came back without one, say so regardless of whether the server
    explained itself.
    """
    if not requested:
        return
    advisory = result.get("persona_ignored")
    if advisory:
        print(f"\n  PERSONA NOT SET: {advisory}")
        return
    if not (result.get("agent") or {}).get("persona"):
        # Forward-compatible branch: old server, no advisory, same silent drop.
        print(
            f"\n  PERSONA NOT SET: asked for '{requested}' and the agent came "
            f"back without one.\n"
            f"    Setting a persona needs an ADMIN-scope key (it is policy, not "
            f"self-service).\n"
            f"    Check your scope with `agentbus whoami`, or ask an operator."
        )


def cmd_register(args: argparse.Namespace) -> int:
    bus = _common._bus(args)
    # #149: --label k[=v] at register time. The SDK accepted labels all along;
    # this flag was simply never wired, so tags were unreachable from the CLI.
    labels: dict[str, str] = {}
    for item in getattr(args, "label", None) or []:
        key, _, value = item.partition("=")
        labels[key] = value
    result = bus.register(
        args.name,
        role=getattr(args, "role", None),
        workdir=getattr(args, "workdir", None),
        repo_remote=args.repo_remote or _git_remote(),
        capabilities=args.capability,
        labels=labels or None,
        unlisted=args.unlisted,
        ephemeral=True if getattr(args, "ephemeral", False) else None,
        persona=getattr(args, "persona", None),
    )
    agent_name = result["agent"]["name"]

    # WIRE THE SESSION WE JUST REGISTERED, or the wake never starts.
    #
    # `register` created an identity and left the session unwired: the monitor
    # resolves its agent from AGENTBUS_AGENT or .claude/settings.local.json, and
    # `register` wrote neither. So the plugin monitor sat idle beside a real
    # inbox, mail arrived, and NOBODY WAS WOKEN.
    #
    # The operator hit exactly this: registered `james`, emailed him, and the
    # message landed in his inbox with nothing watching. It was silent because
    # the machine-global fallback had (rightly) been removed as a cross-agent
    # leak, and because the no-agent branch had just been made quiet.
    #
    # Registering an agent for a session that then cannot hear it is not a
    # halfway state worth preserving — it is the whole feature failing while
    # reporting success. Writing the project identity is exactly what `setup`
    # does, merged rather than replaced, so nothing else in the file is touched.
    # AND MINT ITS KEY, or the wiring above only gets us a better error.
    #
    # With the identity written but no key file, the monitor resolves the agent
    # and then stops: "no credential for '<name>'". Honest, and still no wake.
    # `setup` mints a bound `send` key for exactly this reason; `register` left
    # it out, so registering by NAME could never produce a working inbox no
    # matter how correct everything else was.
    key_note = ""
    try:
        from .. import sealing as _sealing
        from ..onboarding import (
            _keys_dir,
            _mint_bound_key,
            _operator_key,
            _write_private,
        )

        # REG-8b (round-3.5): sanitize agent_name. This is the setup path;
        # agent_name derives from a user-supplied `--name` or a hostile
        # checkout's identity resolution, so a traversal payload MUST NOT
        # write into <config>/operator.env. bound_env_filename ensures the
        # path always resolves inside keys/.
        key_path = _keys_dir() / _sealing.bound_env_filename(agent_name)
        if key_path.exists():
            key_note = f"  key:      {key_path} (existing)"
        else:
            secret = _mint_bound_key(agent_name, _operator_key(), args.base_url)
            if secret:
                # Same 0600 shape setup writes, so both paths produce a key file
                # the monitor and the shell hooks already know how to source.
                _write_private(
                    key_path,
                    f"export AGENTBUS_API_KEY={secret}\nexport AGENTBUS_AGENT={agent_name}\n",
                )
                key_note = f"  key:      minted bound send key -> {key_path} (0600)"
            else:
                key_note = (
                    "  NO KEY: the wake cannot start. Sign in once: agentbus signin <workspace key>"
                )
    except Exception as exc:
        key_note = f"  NO KEY ({exc}); the wake cannot start."

    wired_note = ""
    try:
        from ..onboarding import _dump_json, _load_json, _project_claude_dir

        local_path = _project_claude_dir() / "settings.local.json"
        local = _load_json(local_path)
        existing = (local.get("env") or {}).get("AGENTBUS_AGENT")

        # NEVER STEAL A PROJECT'S IDENTITY. Two guards, both learned the hard way
        # within an hour of shipping the wiring above.
        #
        # 1. A THROWAWAY AGENT MUST NOT CLAIM THE PROJECT. probe_monitor_sigterm
        #    registers `sigterm-probe-$$ --ephemeral --unlisted` and runs from the
        #    repo directory, so every probe run silently rewrote this repo's
        #    identity from `agentbus-dev` to a fixture. Messages then went out AS
        #    the fixture, and a peer's reply landed in the fixture's inbox —
        #    `not_found` for the agent that sent it. Retiring the fixture would
        #    have severed the thread permanently.
        #
        # 2. AN EXISTING DIFFERENT IDENTITY IS NOT OURS TO REPLACE. Registering a
        #    second agent in a wired project is a legitimate thing to do; taking
        #    over the session on the way is not. Say so and leave it alone.
        throwaway = bool(getattr(args, "ephemeral", False) or getattr(args, "unlisted", False))
        if throwaway:
            wired_note = (
                "  not wired: --ephemeral/--unlisted agents never claim the "
                "project identity (they are throwaway)"
            )
        elif existing and existing != agent_name:
            wired_note = (
                f"  NOT WIRED: this project already belongs to '{existing}'. "
                f"Refusing to reassign it to '{agent_name}'.\n"
                f"             To hand the project over deliberately, edit "
                f"{local_path}"
            )
        elif existing == agent_name:
            wired_note = f"  wired:    {local_path} (already {agent_name})"
        else:
            local.setdefault("env", {})["AGENTBUS_AGENT"] = agent_name
            _dump_json(local_path, local)
            # Both records or neither. The worktree file is what the monitor and
            # every non-Claude harness read; writing only settings.local.json
            # would wire Claude Code and leave opencode and codex deaf in the
            # same checkout, which is the #90 split-identity shape again.
            from ..onboarding import _write_worktree_identity

            notes: list[str] = []
            _write_worktree_identity(agent_name, notes)
            wired_note = f"  wired:    {local_path} env.AGENTBUS_AGENT={agent_name}"
            for note in notes:
                wired_note += f"\n             {note}"
    except Exception as exc:
        wired_note = (
            f"  NOT WIRED ({exc}). The wake will not start; "
            f"run: agentbus setup claude --role <role>"
        )

    if args.json:
        result["wired"] = wired_note
        result["key"] = key_note
        _print(result, True)
    else:
        agent = result["agent"]
        print(f"registered as {agent['name']}")
        print(f"  address:  {result['address']}")
        print(f"  rooms:    {', '.join(result['rooms']) or '(none)'}")
        print(key_note)
        print(wired_note)
        _print_persona_outcome(result, getattr(args, "persona", None))
        # THE RESTART IS THE DIFFERENCE BETWEEN WORKING AND NOT, so it is not a
        # footnote. The monitor reads its identity when it STARTS; this session's
        # monitor started before the identity existed and is watching nothing.
        # Mail sent before the restart still arrives and still waits — it just
        # wakes nobody. The operator lost a message to exactly this.
        if "NO KEY" in key_note or "NOT WIRED" in wired_note:
            print()
            print(
                "  ** NOT READY TO RECEIVE **  see the line(s) above; the wake "
                "cannot start until that is fixed."
            )
        else:
            print()
            print("  ** RESTART THIS SESSION NOW **")
            print(
                "     The monitor picks up its identity when it starts, and this one started before"
            )
            print(
                "     you existed — so it is watching nothing. Until you restart, mail sent to you"
            )
            print("     ARRIVES AND WAITS, and no session is woken by it.")
    return 0


def cmd_qr(args: argparse.Namespace) -> int:
    """`agentbus qr` — the address, as something a phone can scan."""
    result = _common._bus(args).whoami()
    address = result.get("address")
    if not address:
        print(
            "no address: this key is not acting as an agent "
            "(register first: agentbus setup claude --role <role>)",
            file=sys.stderr,
        )
        return 1
    agent = (result.get("agent") or {}).get("name", "this agent")
    print(f"{agent}\n{address}\n")
    if _print_qr(f"mailto:{address}"):
        print(f"  scan to mail {agent} directly")
    return 0


def add_commands(sub: argparse._SubParsersAction) -> None:
    """Wire this module's subcommands into the shared subparser."""
    p = sub.add_parser(
        "invite",
        help="mint a one-time join token so a NEW agent can register itself "
        "(operator; needs an unbound full/admin key)",
    )
    p.add_argument("--role", default=None, help="role recorded on the agent it creates")
    p.add_argument(
        "--ttl",
        type=int,
        default=3600,
        metavar="SECONDS",
        help="how long the token stays usable: 60 to 604800 (7 days), default 3600",
    )
    p.set_defaults(func=cmd_invite)

    p = sub.add_parser(
        "join",
        help="register as a NEW agent using a one-time join token "
        "(no existing key needed on this machine)",
    )
    p.add_argument("token", help="the ab_jt_… token your operator issued")
    p.add_argument("name", help="the agent name to create (lowercase)")
    p.add_argument("--role", default=None)
    p.add_argument("--repo-remote", default=None, help="defaults to this repo's git origin")
    p.add_argument("--capability", action="append", default=[])
    p.set_defaults(func=cmd_join)

    p = sub.add_parser("register", help="register this session as an agent")
    p.add_argument("name", nargs="?", default=None)
    p.add_argument(
        "--label",
        action="append",
        default=None,
        metavar="KEY[=VALUE]",
        help="tag this agent at registration (repeatable): team:frontend, skill:playwright=...",
    )
    p.add_argument(
        "--role",
        default=None,
        help="derive identity from this machine+repo+directory (preferred "
        "over a name: a reopened session recomputes the same agent)",
    )
    p.add_argument("--workdir", default=None, help="defaults to the current directory")
    p.add_argument(
        "--ephemeral",
        action="store_true",
        help="throwaway environment; reclaimed in hours not days (auto-detected in CI)",
    )
    p.add_argument("--repo-remote", default=None, help="defaults to this repo's git origin")
    p.add_argument("--capability", action="append", default=[])
    p.add_argument("--unlisted", action="store_true")
    p.add_argument(
        "--persona",
        default=None,
        metavar="LANE",
        help="declare this agent's responsibility lane (policy: the server validates "
        "against the workspace vocabulary and an admin can override). Starter "
        "vocabulary: legal, privacy, security, audit, compliance, frontend, "
        "backend, database, mobile, data-engineering, data-quality, ml, infra, "
        "ops, docs, product, orchestrator, generic. Workspaces can extend.",
    )
    _accept_common_flags_after_subcommand(p)
    p.set_defaults(func=cmd_register)

    p = sub.add_parser("identity", help="show this session's derived identity")
    p.add_argument("--workdir", default=None)
    p.set_defaults(func=cmd_identity)

    p = sub.add_parser("device-id", help="print this machine's stable device id")
    p.set_defaults(func=cmd_device_id)

    # `qr` EXISTS AS ITS OWN SUBCOMMAND because a flag is not discoverable. A
    # session asked for a QR, ran `agentbus --help`, saw no `qr`, concluded the
    # feature did not exist, and pip-installed segno to build one by hand — for a
    # feature shipped the same day. Top-level help is where people look.
    p = sub.add_parser("qr", help="print a scannable QR of this agent's address")
    p.add_argument("--agent", help="acting agent (may also precede the subcommand)")
    p.set_defaults(func=cmd_qr)
