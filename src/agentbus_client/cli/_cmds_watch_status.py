from __future__ import annotations

import click

from . import _watch_omo, _watch_status
from ._app import verb


@verb(
    "watch-status",
    _watch_status.cmd_watch_status,
    help="is a watcher running for this agent?",
    common=True,
)
@click.option(
    "--state", "state", help="scope to one registration by state-file name (default: all)"
)
def cmd_watch_status_cli() -> None: ...


@verb(
    "watch-stop",
    _watch_status.cmd_watch_stop,
    help="stop the detached watcher for this agent",
    common=True,
)
@click.option(
    "--state",
    "state",
    help=(
        "stop exactly the registration with this state-file name (default: every "
        "live watcher for the agent)"
    ),
)
def cmd_watch_stop_cli() -> None: ...


@verb(
    "watch-check",
    _watch_omo.cmd_watch_check,
    help=(
        "may a new watcher start for this agent? exit 0 free, 4 held\n\n"
        "Reports every live watcher for the agent: `agentbus watch` processes and an "
        "omo session holding its lock. Harnesses call this before they start a stream."
    ),
    common=True,
)
@click.option(
    "--harness",
    "harness",
    type=click.Choice(["claude", "omo"]),
    default="claude",
    help="who is asking; omo skips its own lock, claude checks it",
)
@click.option(
    "--session",
    "session",
    metavar="ID",
    help="ignore watcher registrations whose state file name contains this session id",
)
def cmd_watch_check_cli() -> None: ...
