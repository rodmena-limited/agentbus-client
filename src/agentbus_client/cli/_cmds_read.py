from __future__ import annotations

import click

from . import _read
from ._app import argument, verb


@verb("inbox", _read.cmd_inbox, help="list new messages", common=True)
@click.option("--cursor", "cursor", default=0, type=int)
@click.option("--limit", "limit", default=50, type=int)
@click.option("--label", "label")
@click.option(
    "--unread",
    "unread",
    is_flag=True,
    help="server-side filter to unread only (do not page-and-filter)",
)
@click.option("--wait", "wait", default=0, type=int, help="long-poll seconds (max 55)")
def cmd_inbox_cli() -> None: ...


@verb(
    "attachment",
    _read.cmd_attachment,
    help="write an attachment from a delivery to disk (send -a is the other half)",
)
@argument("delivery_id")
@click.option("-i", "--index", "index", default=0, type=int, help="which attachment (default 0)")
@click.option(
    "-o", "--output", "output", help="path to write, or '-' for stdout (default: its own name)"
)
@click.option(
    "--all",
    "all",
    is_flag=True,
    help=(
        "F8: fetch EVERY attachment on the delivery into the current working "
        "directory using its original filename. Refuses to overwrite unless "
        "--force is passed. Mutually exclusive with -i and -o."
    ),
)
@click.option("--force", "force", is_flag=True, help="overwrite an existing file")
@click.option("--agent", "agent", help="acting agent (may also precede the subcommand)")
def cmd_attachment_cli() -> None: ...


@verb("show", _read.cmd_show, help="read one delivery in full", common=True)
@argument("delivery_id")
@click.option(
    "--thread",
    "--all",
    "thread",
    is_flag=True,
    help=(
        "read the WHOLE conversation, oldest first, instead of this one message "
        "(note: on `reply`, --all means reply-to-everyone instead)"
    ),
)
@click.option(
    "--raw",
    "--ciphertext",
    "raw",
    is_flag=True,
    help=(
        "print the stored body verbatim WITHOUT unsealing it, so you can verify "
        "your own mail with an external decoder (e.g. `agentbus show <id> --raw |"
        " age -d -i ~/.config/agentbus/keys/sealing-<agent>.key`)"
    ),
)
def cmd_show_cli() -> None: ...


@verb(
    "ack",
    _read.cmd_ack,
    help=(
        "mark one or more deliveries read/acknowledged (accepts several ids)\n\n"
        "Acknowledge deliveries. Accepts several ids at once, and marks each READ"
        " without opening it — so this is how a backlog is cleared. Read anything"
        " addressed TO you first: ack does not show you the body."
    ),
    common=True,
)
@argument("delivery_ids", nargs=-1, required=True, metavar="DELIVERY_ID")
def cmd_ack_cli() -> None: ...


@verb(
    "labels",
    _read.cmd_labels,
    help="change labels on a delivery (mail filing — agent tags are `agentbus tag`)",
    common=True,
)
@argument("delivery_id")
@click.option("--add", "add", multiple=True)
@click.option("--remove", "remove", multiple=True)
def cmd_labels_cli() -> None: ...
