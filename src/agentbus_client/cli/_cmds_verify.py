from __future__ import annotations

import click

from . import _verify
from ._app import argument, verb


@verb(
    "verify",
    _verify.cmd_verify,
    help="inspect a claim; with --run, execute it opt-in and record your verdict",
    common=True,
)
@argument("delivery_id")
@click.option(
    "--run",
    "run",
    is_flag=True,
    help=(
        "execute the repro on this host (never automatic; scrubbed of this "
        "session's bus credentials)"
    ),
)
@click.option(
    "--with-creds",
    "with_creds",
    is_flag=True,
    help=(
        "explicit override: let the repro inherit the bus credential (read the "
        "claim fully before this)"
    ),
)
@click.option(
    "--timeout", "timeout", default=60.0, type=float, help="repro timeout in seconds (default 60)"
)
def cmd_verify_cli() -> None: ...


@verb(
    "verify-sender",
    _verify.cmd_verify_signature,
    help="check a message's signature yourself, without trusting the bus",
    common=True,
)
@argument("delivery_id")
def cmd_verify_sender_cli() -> None: ...
