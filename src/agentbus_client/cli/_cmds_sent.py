from __future__ import annotations

import click

from . import _sent
from ._app import verb


@verb(
    "sent",
    _sent.cmd_sent,
    help="mail YOU sent, newest first — the outbox. What did my daemon post?",
    common=True,
)
@click.option("--limit", "limit", default=50, type=int, help="rows to show (default 50)")
@click.option("--thread", "thread", metavar="THREAD_ID", help="only this conversation")
@click.option(
    "--since",
    "since",
    help=(
        "only rows at or after this instant: ISO-8601 (2026-09-01T12:00:00Z) or a"
        " duration back from now (2h, 90m, 3d)"
    ),
)
def cmd_sent_cli() -> None: ...
