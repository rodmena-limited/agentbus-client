from __future__ import annotations

import click

from . import _keys
from ._app import VerbGroup, argument, verb

cmd_keys_cli = VerbGroup(
    "keys",
    dest="keys_action",
    help="see, rotate and revoke this agent's SEALING keys (encrypted workspaces)",
)


@verb(
    "list",
    _keys.cmd_keys,
    parent=cmd_keys_cli,
    help="every published key, marking this machine's",
    common=True,
)
def cmd_keys_cli_list() -> None: ...


@verb(
    "rotate",
    _keys.cmd_keys,
    parent=cmd_keys_cli,
    help="new local key, published; the old one stays valid",
    common=True,
)
@click.option("--label", "label", help="how this machine appears in the list (default: hostname)")
@click.option("--yes", "yes", is_flag=True, help="proceed past the old-mail warning")
def cmd_keys_cli_rotate() -> None: ...


@verb(
    "sign",
    _keys.cmd_keys,
    parent=cmd_keys_cli,
    help="publish this machine's SIGNING key so peers can verify you",
    common=True,
)
@click.option("--label", "label", help="how this machine appears in the list (default: hostname)")
def cmd_keys_cli_sign() -> None: ...


@verb(
    "revoke",
    _keys.cmd_keys,
    parent=cmd_keys_cli,
    help="retire one key — forward only, never retroactive",
    common=True,
)
@argument("fingerprint", help="fingerprint of the key to revoke (see `agentbus keys list`)")
@click.option(
    "--yes",
    "yes",
    is_flag=True,
    help="proceed past the warning (required — the warning comes first)",
)
@click.option(
    "--reason",
    "reason",
    help="why, recorded against the key: a rotation and a compromise want different follow-up",
)
def cmd_keys_cli_revoke() -> None: ...
