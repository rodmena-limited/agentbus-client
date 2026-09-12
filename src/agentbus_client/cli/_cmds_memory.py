from __future__ import annotations

import click

from . import _memory
from ._app import argument, verb


@verb(
    "memory",
    _memory._dispatch,
    help=(
        "your own notebook: remember something, or read it all back\n\nagentbus "
        "memory 'always quote the staging DSN'   remember it\nagentbus memory "
        "fetch                            read it all back\nagentbus memory rm 7"
        "                             forget one entry (by seq)\nagentbus memory "
        "truncate --first 10              forget the 10 OLDEST\nagentbus memory "
        "reseal                           re-seal to your current key"
    ),
    common=True,
)
@argument(
    "action",
    required=False,
    help="fetch | rm | truncate | reseal; omit it to REMEMBER the text that follows",
)
@argument("text", required=False, help="what to remember (also @file, or @- for stdin)")
@click.option("--seq", "seq", type=int, help="entry to remove (with rm)")
@click.option("--first", "first", type=int, help="how many OLDEST to remove")
def cmd_memory_cli() -> None: ...
