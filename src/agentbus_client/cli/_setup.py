"""The `agentbus` command line client."""

from __future__ import annotations

import argparse

from .. import onboarding as _onboarding


def add_commands(sub: argparse._SubParsersAction) -> None:
    """Wire this module's subcommands into the shared subparser."""

    p = sub.add_parser("signin", help="validate an API key against the live service, then store it")
    p.add_argument("key")
    p.set_defaults(func=_onboarding.cmd_signin)

    p = sub.add_parser(
        "setup",
        help="wire this project's agent end to end — credential, identity, passive "
        "hooks, active re-waker. Idempotent; never touches foreign settings.",
    )
    p.add_argument("harness", choices=list(_onboarding.HARNESSES))
    p.add_argument(
        "--role",
        default=None,
        help="role for derivable identity (required with an operator key on a fresh project)",
    )
    p.add_argument(
        "--force-new",
        action="store_true",
        help="mint a new agent even though one exists for this role+repo "
        "under a different device-id (reinstall guard override)",
    )
    p.add_argument(
        "--persona",
        default=None,
        metavar="LANE",
        help="declare this agent's responsibility lane (e.g. backend, frontend, "
        "legal). The server validates against the workspace vocabulary under the "
        "POLICY model. Forward-compatible: ignored by servers that predate the "
        "persona column, so this flag is safe to pass before the migration lands.",
    )
    p.set_defaults(func=_onboarding.cmd_setup)

    p = sub.add_parser(
        "teardown",
        help="remove ALL AgentBus wiring from this project — .agentbus/, the "
        "settings.local.json identity, and (with --purge-key) the agent's key "
        "file. One command to opt out; restart the session afterwards.",
    )
    p.add_argument(
        "--purge-key",
        action="store_true",
        help="also delete the agent's bound key file from ~/.config/agentbus/keys",
    )
    p.add_argument(
        "--machine",
        action="store_true",
        help="also remove the MACHINE-level state: ~/.config/agentbus entirely "
        "(operator credential, device-id, all key files). The nuclear opt-out.",
    )
    p.set_defaults(func=_onboarding.cmd_teardown)

    # SIBLINGS (SPECS/0029). Many agents on ONE checkout is the normal case, and
    # the server already supports it — identity is device+repo+workdir+ROLE. What
    # was missing locally is a way for each session to hold its own identity
    # DEPRECATED 2026-08-10 (operator directive): identity is env-var-driven;
    # the sibling machinery was the wrong answer. These verbs are kept only to
    # print that guidance — see cmd_sibling/cmd_as.
    p = sub.add_parser(
        "sibling", help="DEPRECATED — use AGENTBUS_AGENT env-var identity or a worktree/clone"
    )
    ssub = p.add_subparsers(dest="sibling_cmd", required=True)
    sp = ssub.add_parser("add", help="DEPRECATED: prints guidance, does nothing")
    sp.add_argument("role")
    sp.set_defaults(func=_onboarding.cmd_sibling)
    sp = ssub.add_parser("list", help="DEPRECATED: prints guidance, does nothing")
    sp.set_defaults(func=_onboarding.cmd_sibling)

    p = sub.add_parser(
        "as", help="DEPRECATED — run a command as a different agent via AGENTBUS_AGENT=role"
    )
    p.add_argument("role")
    p.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="-- then the command to run (deprecated: prints guidance)",
    )
    p.set_defaults(func=_onboarding.cmd_as)
