from __future__ import annotations

import click

from . import _identity_cmd, _register
from ._app import argument, verb


@verb(
    "invite",
    _register.cmd_invite,
    help=(
        "mint a one-time join token so a NEW agent can register itself (operator;"
        " needs an unbound full/admin key)"
    ),
)
@click.option("--role", "role", help="role recorded on the agent it creates")
@click.option(
    "--ttl",
    "ttl",
    default=3600,
    type=int,
    metavar="SECONDS",
    help="how long the token stays usable: 60 to 604800 (7 days), default 3600",
)
def cmd_invite_cli() -> None: ...


@verb(
    "join",
    _register.cmd_join,
    help="register as a NEW agent using a one-time join token (no existing key needed on this machine)",
)
@argument("token", help="the ab_jt_… token your operator issued")
@argument("name", help="the agent name to create (lowercase)")
@click.option("--role", "role")
@click.option("--repo-remote", "repo_remote", help="defaults to this repo's git origin")
@click.option("--capability", "capability", multiple=True)
def cmd_join_cli() -> None: ...


@verb(
    "register",
    _register.cmd_register,
    help="register this session as an agent",
    common=True,
    none_when_empty=("label",),
)
@argument("name", required=False)
@click.option(
    "--label",
    "label",
    multiple=True,
    metavar="KEY[=VALUE]",
    help="tag this agent at registration (repeatable): team:frontend, skill:playwright=...",
)
@click.option(
    "--role",
    "role",
    help=(
        "derive identity from this machine+repo+directory (preferred over a name:"
        " a reopened session recomputes the same agent)"
    ),
)
@click.option("--workdir", "workdir", help="defaults to the current directory")
@click.option(
    "--ephemeral",
    "ephemeral",
    is_flag=True,
    help="throwaway environment; reclaimed in hours not days (auto-detected in CI)",
)
@click.option("--repo-remote", "repo_remote", help="defaults to this repo's git origin")
@click.option("--capability", "capability", multiple=True)
@click.option("--unlisted", "unlisted", is_flag=True)
@click.option(
    "--persona",
    "persona",
    metavar="LANE",
    help=(
        "declare this agent's responsibility lane (policy: the server validates "
        "against the workspace vocabulary and an admin can override). Starter "
        "vocabulary: legal, privacy, security, audit, compliance, frontend, "
        "backend, database, mobile, data-engineering, data-quality, ml, infra, "
        "ops, docs, product, orchestrator, generic. Workspaces can extend."
    ),
)
def cmd_register_cli() -> None: ...


@verb("identity", _identity_cmd.cmd_identity, help="show this session's derived identity")
@click.option("--workdir", "workdir")
def cmd_identity_cli() -> None: ...


@verb("device-id", _register.cmd_device_id, help="print this machine's stable device id")
def cmd_device_id_cli() -> None: ...


@verb("qr", _register.cmd_qr, help="print a scannable QR of this agent's address")
@click.option("--agent", "agent", help="acting agent (may also precede the subcommand)")
def cmd_qr_cli() -> None: ...
