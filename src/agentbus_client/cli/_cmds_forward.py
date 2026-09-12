from __future__ import annotations

import click

from . import _forward
from ._app import VERBATIM, argument, verb


@verb(
    "forward",
    _forward.cmd_forward,
    help="forward a conversation to a third party, RE-SEALED to their keys",
    common=True,
    none_when_empty=("cc",),
)
@argument("delivery_id")
@argument("to", nargs=-1, required=True, help="new recipients")
@click.option("-c", "--cc", "cc", multiple=True)
@click.option("-b", "--body", "body", help="a note to put above the forwarded text")
@click.option("-p", "--priority", "priority", type=click.Choice(["urgent", "normal", "background"]))
def cmd_forward_cli() -> None: ...


@verb("draft", _forward.cmd_draft, help="save a draft without sending it", common=True)
@argument("to", nargs=-1, required=True)
@click.option("-s", "--subject", "subject")
@click.option("-b", "--body", "body")
def cmd_draft_cli() -> None: ...


@verb("draft-send", _forward.cmd_draft_send, help="send a stored draft", common=True)
@argument("draft_id")
def cmd_draft_send_cli() -> None: ...


@verb(
    "undeliverable",
    _forward.cmd_undeliverable,
    help="external mail that could not be routed",
    common=True,
)
@click.option("--limit", "limit", default=20, type=VERBATIM)
def cmd_undeliverable_cli() -> None: ...


@verb("drafts", _forward.cmd_drafts, help="list drafts", common=True)
def cmd_drafts_cli() -> None: ...


@verb("approve", _forward.cmd_approve, help="ask a human to approve something", common=True)
@argument("title")
@click.option("--kind", "kind", default="generic")
@click.option("--summary", "summary")
@click.option(
    "--wait",
    "wait",
    default=0,
    type=int,
    metavar="SECONDS",
    help=(
        "BLOCK until a human decides, and exit on the outcome. Without it this "
        "returns while the approval is still open — which means you have raised a"
        " gate and not waited at it."
    ),
)
def cmd_approve_cli() -> None: ...


@verb(
    "approval",
    _forward.cmd_approval,
    help=(
        "check an approval by id (the CLI twin of MCP's bus_approval_status)\n\n"
        "Report the status of an approval you already have an id for — one raised"
        " by an earlier session, or handed to you by a peer. `agentbus approve "
        "--wait` can only wait on an approval it just created; this works on any "
        "id. Exit codes: 0 approved, 1 DENIED "
        "(rejected/cancelled/timed_out/changes_requested), 7 nobody has decided "
        "yet. 1 and 7 are different answers and must not be treated alike."
    ),
    common=True,
)
@argument("approval_id")
@click.option(
    "--wait",
    "wait",
    default=0,
    type=int,
    metavar="SECONDS",
    help=(
        "block until it is decided (server caps the wait; exit 7 if the wait "
        "elapses with no decision)"
    ),
)
def cmd_approval_cli() -> None: ...
