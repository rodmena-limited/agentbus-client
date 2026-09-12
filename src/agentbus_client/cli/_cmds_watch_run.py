from __future__ import annotations

import click

from . import _watch_run
from ._app import verb


@verb(
    "watch", _watch_run.cmd_watch, help="stay connected and act on arriving messages", common=True
)
@click.option(
    "--exec",
    "exec",
    help=(
        "shell command per message; {subject} {sender} {delivery_id} {message_id}"
        " {thread_id} {agent_seq} are substituted and shell-quoted"
    ),
)
@click.option("--append", "append", help="append JSON lines to this file")
@click.option("--state", "state", help="cursor checkpoint file")
@click.option("--cursor", "cursor", default=0, type=int, help="start from this cursor")
@click.option("--once", "once", is_flag=True, help="drain and exit; do not stream")
@click.option(
    "--daemon",
    "daemon",
    is_flag=True,
    help=(
        "detach and keep running after this session ends (the wake channel is "
        "outbound SSE, so this works behind a strict inbound firewall)"
    ),
)
@click.option(
    "--coalesce-window",
    "coalesce_window",
    default=2500,
    type=int,
    metavar="MS",
    help=(
        "max milliseconds the trailing envelope can accumulate (default 2500). "
        "Cap on how long the tail of a burst can hold; overrides quiet."
    ),
)
@click.option(
    "--coalesce-quiet",
    "coalesce_quiet",
    default=800,
    type=int,
    metavar="MS",
    help=(
        "close the envelope after this many ms of silence (default 800). Bounded "
        "above by --coalesce-window."
    ),
)
@click.option(
    "--no-coalesce",
    "no_coalesce",
    is_flag=True,
    help=(
        "disable envelope coalescing entirely; fire the wake hook once per "
        "message. Only useful if a downstream hook is not envelope-aware."
    ),
)
def cmd_watch_cli() -> None: ...
