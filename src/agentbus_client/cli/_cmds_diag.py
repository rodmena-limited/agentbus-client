from __future__ import annotations

import click

from . import _diag
from ._app import verb


@verb(
    "refresh-skill",
    _diag.cmd_refresh_skill,
    help=(
        "re-download the served SKILL.md into ~/.claude/skills/agentbus/, no "
        "registration flow. Use this when `agentbus doctor` says the skill is "
        "stale but `agentbus setup claude` refuses because your cwd's repo "
        "differs from the one this agent was registered from."
    ),
    common=True,
)
def cmd_refresh_skill_cli() -> None: ...


@verb(
    "quickref", _diag.cmd_quickref, help="the six verbs and three rules, on one screen", common=True
)
@click.option(
    "--verbs",
    "verbs",
    is_flag=True,
    help=(
        "print every verb, one per line, for scripts and doc guards (a stable "
        "contract; do NOT parse --help, which wraps)"
    ),
)
def cmd_quickref_cli() -> None: ...


@verb("doctor", _diag.cmd_doctor, help="prove connectivity, quota and the SMTP loop", common=True)
@click.option(
    "--wake",
    "wake",
    is_flag=True,
    help=(
        "prove the WAKE chain instead: a self-probe must surface through the Stop"
        " re-waker exactly once"
    ),
)
def cmd_doctor_cli() -> None: ...
