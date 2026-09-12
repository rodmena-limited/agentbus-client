from __future__ import annotations

import click

from . import _block
from ._app import argument, verb


@verb(
    "block",
    _block.cmd_block,
    help="stop a peer's mail reaching you — even one trusted by the workspace",
    common=True,
)
@argument("name", help="the agent to block")
@click.option("--reason", "reason", help="why, for your own later reference")
@click.option(
    "--for",
    "for_",
    metavar="DURATION",
    help=(
        "expire the block automatically (2h, 3d). RECOMMENDED for a zombie: the "
        "process gets restarted and a permanent block then silently drops "
        "legitimate mail from the same name"
    ),
)
def cmd_block_cli() -> None: ...


@verb("unblock", _block.cmd_unblock, help="resume delivery from a blocked peer", common=True)
@argument("name", help="the agent to unblock")
def cmd_unblock_cli() -> None: ...


@verb(
    "blocks",
    _block.cmd_blocks,
    help="who you are blocking, and what it has suppressed",
    common=True,
)
def cmd_blocks_cli() -> None: ...
