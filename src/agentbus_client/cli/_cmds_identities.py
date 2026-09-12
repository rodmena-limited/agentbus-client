from __future__ import annotations

import click

from . import _identities
from ._app import argument, verb


@verb(
    "identities",
    _identities.cmd_identities,
    help=(
        "every agent identity credentialled on THIS machine, which one this "
        "directory would act as, and (with --remote) whether each is live "
        "somewhere else. Prints no key material."
    ),
    common=True,
)
@click.option(
    "--remote",
    "remote",
    is_flag=True,
    help=(
        "also query each identity's health + registration device. Shows whether "
        "each is live, and flags any that last REGISTERED from another device. "
        "Does NOT detect a stolen key reused in place — see SPECS/0020."
    ),
)
def cmd_identities_cli() -> None: ...


@verb(
    "health",
    _identities.cmd_health,
    help=(
        "canary heartbeat for an agent — is their watcher actually alive right "
        "now? Consumes GET /v1/agents/{name}/health. wake_channel_state 'stale' "
        "or 'none' means a send would be stored into a queue nothing drains, even"
        " if presence reads 'responsive'."
    ),
    common=True,
)
@argument(
    "target_agent",
    required=False,
    help="the agent to check (default: acting agent from --agent / $AGENTBUS_AGENT)",
)
def cmd_health_cli() -> None: ...
