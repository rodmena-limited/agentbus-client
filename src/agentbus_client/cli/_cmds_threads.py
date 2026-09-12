from __future__ import annotations

import click

from . import _threads
from ._app import argument, verb


@verb("thread", _threads.cmd_thread, help="show a whole conversation", common=True)
@argument("thread_id", help="the thread id (a delivery id from your inbox also works)")
def cmd_thread_cli() -> None: ...


@verb(
    "history", _threads.cmd_history, help="what was said in a room before you joined", common=True
)
@argument("room", help="room name, without the room: prefix")
@click.option("--limit", "limit", type=int, help="how many earlier messages to show")
@click.option("--since", "since", metavar="ISO8601", help="only messages after this point")
def cmd_history_cli() -> None: ...


@verb(
    "schema",
    _threads.cmd_schema,
    help="read, declare or clear a room's payload contract",
    common=True,
)
@argument("room", help="the room name")
@click.option("--set", "set", metavar="JSON", help="literal JSON, @file, or @- for stdin")
@click.option("--clear", "clear", is_flag=True, help="remove the contract")
def cmd_schema_cli() -> None: ...


@verb("usage", _threads.cmd_usage, help="show quota usage", common=True)
def cmd_usage_cli() -> None: ...


@verb(
    "reminders",
    _threads.cmd_reminders,
    help=(
        "ack-tracking visibility. Defaults to --owing: what you sent and are "
        "still waiting on. --owed shows what was sent TO you that you owe an ack "
        "on. Reads only, scoped to your own agent."
    ),
    common=True,
    exclusive=[("owed", "owing")],
)
@click.option(
    "--owed",
    "owed",
    is_flag=True,
    help="show messages TO me that I owe an ack on (the recipient view)",
)
@click.option(
    "--owing",
    "owing",
    is_flag=True,
    help="show messages I sent that I'm still waiting to be acked (the sender view; the default)",
)
def cmd_reminders_cli() -> None: ...
