from __future__ import annotations

import click

from . import _service
from ._app import argument, verb


@verb("retire", _service.cmd_retire, help="stand an agent down (reversible)", common=True)
@argument("name", required=False)
def cmd_retire_cli() -> None: ...


@verb(
    "service",
    _service.cmd_service,
    help=(
        "emit a systemd unit (Linux) or launchd plist (macOS) so the watcher is "
        "supervised, not merely detached"
    ),
    common=True,
)
@click.option(
    "--manager",
    "manager",
    type=click.Choice(["systemd", "launchd", "rc.d"]),
    help="override the auto-detected service manager",
)
@click.option(
    "--env-file",
    "env_file",
    help=(
        "reference this env file for credentials instead of inlining the key into"
        " the unit (recommended)"
    ),
)
def cmd_service_cli() -> None: ...
