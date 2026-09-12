from __future__ import annotations

import click

from . import _directory
from ._app import argument, verb


@verb("whoami", _directory.cmd_whoami, help="show the acting identity", common=True)
@click.option(
    "-qr", "--qr", "qr", is_flag=True, help="also print a scannable QR of this agent's address"
)
def cmd_whoami_cli() -> None: ...


@verb(
    "phonebook",
    _directory.cmd_phonebook,
    help="discover agents",
    common=True,
    none_when_empty=("label",),
)
@argument("query", required=False)
@click.option("--capability", "capability")
@click.option(
    "--label",
    "label",
    multiple=True,
    help="filter by tag: `team:frontend` (key exists) or `env=prod` (exact); repeat to AND",
)
def cmd_phonebook_cli() -> None: ...


@verb(
    "tag",
    _directory.cmd_tag,
    help="this agent's discovery tags (teams/skills/projects — delivery mail labels are `labels`)",
    common=True,
)
@argument(
    "set",
    nargs=-1,
    metavar="KEY[=VALUE]",
    help=(
        "tags to set — TWO GRAMMARS, both legal, they mean different things: "
        "`skill:playwright` = wear the NAMESPACED KEY 'skill:playwright' (no "
        "value); `skill=playwright` = wear the KEY 'skill' with the VALUE "
        "'playwright'; `skill:playwright=takes shots` = namespaced key WITH a "
        "value. Split rule: everything before the FIRST `=` is the key (colons "
        "are part of it), everything after is the value. Matching filters follow "
        "the same rule (see `agentbus phonebook --label`)."
    ),
)
@click.option("--remove", "remove", multiple=True, metavar="KEY")
def cmd_tag_cli() -> None: ...


@verb(
    "busy",
    _directory.cmd_busy,
    help="tell senders you cannot take new work for N seconds (0 clears it)",
    common=True,
)
@argument("seconds", type=int, help="how long; 0 clears. Expires on its own.")
@click.option("--reason", "reason", help="shown to senders, e.g. 'deep in a repro'")
def cmd_busy_cli() -> None: ...


@verb(
    "status",
    _directory.cmd_status,
    help="read or declare availability: online|busy|away|dnd|offline",
    common=True,
)
@argument(
    "state",
    required=False,
    type=click.Choice(["online", "busy", "away", "dnd", "offline"]),
    help="omit to READ. dnd and offline WITHHOLD mail; busy and away only tell senders",
)
@click.option(
    "--for",
    "seconds",
    type=int,
    metavar="SECONDS",
    help="how long (capped server-side; every state but online expires)",
)
@click.option("--reason", "reason")
@click.option(
    "--hold-below",
    "hold_below",
    type=click.Choice(["urgent", "normal", "background"]),
    help="override what is withheld (default: dnd holds below urgent)",
)
def cmd_status_cli() -> None: ...


@verb(
    "liveness", _directory.cmd_liveness, help="who is responsive, not merely reachable", common=True
)
def cmd_liveness_cli() -> None: ...
