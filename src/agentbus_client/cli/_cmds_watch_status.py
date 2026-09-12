from __future__ import annotations

import click

from . import _watch_status
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
