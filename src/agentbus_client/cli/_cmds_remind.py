from __future__ import annotations

import click

from . import _remind
from ._app import verb


@verb(
    "remind",
    _remind.cmd_remind,
    help=(
        "schedule a message into an agent's inbox (yours, unless --target)\n\n"
        "Schedule a reminder. With no --target this is a NOTE TO YOURSELF, which "
        "is the common case. The body is sealed on this machine before it is "
        "uploaded, so it sits encrypted until it is due."
    ),
    common=True,
)
@click.option("-m", "--message", "message", help="text, @file, or @- for stdin")
@click.option(
    "--target",
    "target",
    help=(
        "who to remind (default: YOU). Naming someone else schedules a message "
        "into their inbox, so say something they will understand out of context."
    ),
)
@click.option("-s", "--subject", "subject")
@click.option(
    "--delay",
    "delay",
    metavar="DURATION",
    help="fire after this long: 90m, 2h, 3d, or bare seconds",
)
@click.option(
    "--at",
    "at",
    metavar="WHEN",
    help="fire at an absolute time (ISO-8601). Mutually exclusive with --delay",
)
@click.option(
    "--expire",
    "expire",
    metavar="DURATION",
    help=(
        "the END DATE. On a one-shot: do not deliver if it would fire later than "
        "this — a stale reminder is worse than none. ON A RECURRENCE it is the "
        "stop date: after it passes the schedule is cancelled upstream and stops "
        "firing entirely. Same duration format as --delay."
    ),
)
@click.option(
    "--repeat",
    "repeat",
    metavar="RULE",
    help="recurring: daily, weekly, monthly, or a 5-field cron expression",
)
@click.option(
    "--repeat-until",
    "repeat_until",
    metavar="WHEN",
    help=(
        "DOES NOT EXIST — use --expire instead, which IS the end date for a "
        "recurrence. Kept only to redirect: a recurrence with no end is a "
        "commitment nobody remembers making, and --expire is how you avoid it."
    ),
)
@click.option(
    "--timezone",
    "timezone",
    metavar="IANA",
    help=(
        "zone for --repeat (e.g. Europe/London). Needed so 'daily at 9' means 9 "
        "where you are; a UTC offset like +01:00 is not accepted and would be "
        "wrong across a DST boundary anyway."
    ),
)
@click.option("--cancel", "cancel", metavar="ID", help="cancel a scheduled reminder")
def cmd_remind_cli() -> None: ...


@verb(
    "reminds",
    _remind.cmd_reminds,
    help=(
        "list scheduled reminders (agent tags are `tag`; ack-chasing is "
        "`reminders`)\n\nReminders not yet delivered. NOT `agentbus reminders`, "
        "which is ack-tracking — that chases messages already sent; this lists "
        "messages not yet sent."
    ),
    common=True,
)
@click.option(
    "--all",
    "all",
    is_flag=True,
    help=(
        "include FINISHED reminders (fired and cancelled). The default shows only"
        " live ones, because this command exists to find something to cancel and "
        "dead rows crowd out the ones you can still act on."
    ),
)
def cmd_reminds_cli() -> None: ...
