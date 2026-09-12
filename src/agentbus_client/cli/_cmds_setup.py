from __future__ import annotations

import click

from .. import onboarding as _onboarding
from ._app import VerbGroup, argument, verb


@verb(
    "signin",
    _onboarding.cmd_signin,
    help="validate an API key against the live service, then store it",
)
@argument("key")
def cmd_signin_cli() -> None: ...


@verb(
    "setup",
    _onboarding.cmd_setup,
    help=(
        "wire this project's agent end to end — credential, identity, passive "
        "hooks, active re-waker. Idempotent; never touches foreign settings."
    ),
)
@argument("harness", type=click.Choice(["claude", "opencode", "codex", "agy", "antigravity"]))
@click.option(
    "--role",
    "role",
    help="role for derivable identity (required with an operator key on a fresh project)",
)
@click.option(
    "--force-new",
    "force_new",
    is_flag=True,
    help=(
        "mint a new agent even though one exists for this role+repo under a "
        "different device-id (reinstall guard override)"
    ),
)
@click.option(
    "--persona",
    "persona",
    metavar="LANE",
    help=(
        "declare this agent's responsibility lane (e.g. backend, frontend, "
        "legal). The server validates against the workspace vocabulary under the "
        "POLICY model. Forward-compatible: ignored by servers that predate the "
        "persona column, so this flag is safe to pass before the migration lands."
    ),
)
def cmd_setup_cli() -> None: ...


@verb(
    "teardown",
    _onboarding.cmd_teardown,
    help=(
        "remove ALL AgentBus wiring from this project —.agentbus/, the "
        "settings.local.json identity, and (with --purge-key) the agent's key "
        "file. One command to opt out; restart the session afterwards."
    ),
)
@click.option(
    "--purge-key",
    "purge_key",
    is_flag=True,
    help="also delete the agent's bound key file from ~/.config/agentbus/keys",
)
@click.option(
    "--machine",
    "machine",
    is_flag=True,
    help=(
        "also remove the MACHINE-level state: ~/.config/agentbus entirely "
        "(operator credential, device-id, all key files). The nuclear opt-out."
    ),
)
def cmd_teardown_cli() -> None: ...


cmd_sibling_cli = VerbGroup(
    "sibling",
    dest="sibling_cmd",
    help="DEPRECATED — use AGENTBUS_AGENT env-var identity or a worktree/clone",
)


@verb(
    "add",
    _onboarding.cmd_sibling,
    parent=cmd_sibling_cli,
    help="DEPRECATED: prints guidance, does nothing",
)
@argument("role")
def cmd_sibling_cli_add() -> None: ...


@verb(
    "list",
    _onboarding.cmd_sibling,
    parent=cmd_sibling_cli,
    help="DEPRECATED: prints guidance, does nothing",
)
def cmd_sibling_cli_list() -> None: ...


@verb(
    "as",
    _onboarding.cmd_as,
    help="DEPRECATED — run a command as a different agent via AGENTBUS_AGENT=role",
    remainder=True,
)
@argument("role")
@argument(
    "command",
    nargs=-1,
    type=click.UNPROCESSED,
    help="-- then the command to run (deprecated: prints guidance)",
)
def cmd_as_cli() -> None: ...
