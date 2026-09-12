from __future__ import annotations

import os
import re
import shutil
import sys
from collections.abc import Callable
from io import StringIO
from typing import IO, Any

import click

from ._app import BusArgument, Root, UnknownVerb

TAGLINE = "a real inbox for every agent"
WIDTH = 100
DEPRECATED = ("as", "sibling")
GLOBAL_OPTIONS = "--agent NAME  --json  --api-key KEY  --base-url URL  --version"
ROOT_USAGE = "agentbus [--agent NAME] [--json] COMMAND [ARGS]..."
_TAIL = re.compile(r"(?:[ \t]|\x1b\[[0-9;]*m)+$")
_CODE = re.compile(r"\x1b\[[0-9;]*m")

SECTIONS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (
        "Start here",
        (
            ("setup", "Wire this project's agent end to end"),
            ("signin", "Check an API key and store it on this machine"),
            ("whoami", "Who you are, and whether mail is waiting"),
            ("doctor", "Prove connectivity, quota and the email loop"),
            ("quickref", "The everyday verbs and rules on one screen"),
            ("teardown", "Remove AgentBus wiring from this project"),
        ),
    ),
    (
        "Read mail",
        (
            ("inbox", "List your messages"),
            ("show", "Read one delivery in full"),
            ("thread", "Show a whole conversation"),
            ("ack", "Mark deliveries as done"),
            ("attachment", "Save an attachment from a delivery to disk"),
            ("labels", "File a delivery under labels"),
            ("history", "What a room said before you joined"),
        ),
    ),
    (
        "Send mail",
        (
            ("send", "Send a message"),
            ("reply", "Reply to a message in its thread"),
            ("forward", "Forward a conversation, re-sealed for the new reader"),
            ("send-batch", "Send many messages from JSON lines on stdin"),
            ("draft", "Save a draft without sending it"),
            ("drafts", "List your drafts"),
            ("draft-send", "Send a saved draft"),
            ("sent", "Mail you sent, newest first"),
            ("undeliverable", "External mail that could not be routed"),
        ),
    ),
    (
        "Follow-ups and notes",
        (
            ("remind", "Schedule a message for later, once or on repeat"),
            ("reminds", "List scheduled reminders"),
            ("reminders", "Messages still waiting on an answer"),
            ("approve", "Ask a human to approve something"),
            ("approval", "Check an approval by id"),
            ("memory", "Your own notebook: remember, then read back"),
        ),
    ),
    (
        "People and presence",
        (
            ("phonebook", "Find agents"),
            ("status", "Read or set your availability"),
            ("busy", "Tell senders you are busy for N seconds"),
            ("liveness", "Who is responsive, not merely reachable"),
            ("health", "Is an agent's watcher actually alive?"),
            ("tag", "Your discovery tags: teams, skills, projects"),
            ("qr", "Your address as a scannable QR code"),
        ),
    ),
    (
        "Stay reachable",
        (
            ("watch", "Stay connected and act on arriving mail"),
            ("watch-status", "Is a watcher running for this agent?"),
            ("watch-stop", "Stop the detached watcher"),
            ("service", "Emit a systemd or launchd unit for the watcher"),
        ),
    ),
    (
        "Identity and keys",
        (
            ("register", "Register this session as an agent"),
            ("join", "Register a new agent with a one-time join token"),
            ("invite", "Mint a one-time join token for a new agent"),
            ("identity", "This session's derived identity"),
            ("identities", "Every agent credentialled on this machine"),
            ("device-id", "This machine's stable device id"),
            ("keys", "See, rotate and revoke sealing keys"),
            ("retire", "Stand an agent down (reversible)"),
            ("refresh-skill", "Re-download the served SKILL.md"),
        ),
    ),
    (
        "Trust, rooms and quota",
        (
            ("block", "Stop a peer's mail reaching you"),
            ("unblock", "Resume mail from a blocked peer"),
            ("blocks", "Who you block, and what it suppressed"),
            ("verify", "Inspect a claim; with --run, execute it"),
            ("verify-sender", "Check a message's signature yourself"),
            ("schema", "Read, declare or clear a room's payload contract"),
            ("usage", "Show quota usage"),
        ),
    ),
)


def styled(stream: Any) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    isatty = getattr(stream, "isatty", None)
    try:
        return bool(isatty is not None and isatty())
    except ValueError:
        return False


def _strip_trailing(line: str) -> str:
    match = _TAIL.search(line)
    if match is None:
        return line
    return line[: match.start()] + "".join(_CODE.findall(match.group(0)))


def _render(draw: Callable[[Any], None]) -> str:
    from rich.console import Console

    buffer = StringIO()
    width = max(40, min(shutil.get_terminal_size((WIDTH, 24)).columns, WIDTH))
    console = Console(file=buffer, force_terminal=True, width=width, highlight=False)
    draw(console)
    text = buffer.getvalue().rstrip("\n")
    return "\n".join(_strip_trailing(line) for line in text.split("\n"))


def _wordmark() -> Any:
    from rich.panel import Panel
    from rich.text import Text

    mark = Text()
    mark.append("AGENT", style="bold white on blue")
    mark.append("/", style="bold blue")
    mark.append("BUS", style="bold blue")
    mark.append(f"  {TAGLINE}", style="dim")
    return Panel(mark, border_style="blue", expand=False)


def _sections_for(root: Root) -> list[tuple[str, list[tuple[str, str]]]]:
    registered = set(root.commands)
    sections = [(title, [(v, s) for v, s in rows if v in registered]) for title, rows in SECTIONS]
    listed = {v for _, rows in sections for v, _ in rows}
    extra = sorted(registered - listed - set(DEPRECATED))
    if extra:
        sections.append(("Other", [(v, root.commands[v].get_short_help_str(60)) for v in extra]))
    return [(title, rows) for title, rows in sections if rows]


def overview(root: Root, ctx: click.Context) -> str:
    sections = _sections_for(root)
    deprecated = [v for v in DEPRECATED if v in root.commands]
    pad = max(len(v) for _, rows in sections for v, _ in rows) + 2
    if not styled(sys.stdout):
        out = [f"AGENT/BUS — {TAGLINE}", "", f"Usage: {ROOT_USAGE}"]
        for title, rows in sections:
            out += ["", f"{title}:"]
            out += [f"  {v:<{pad}}{s}" for v, s in rows]
        out.append("")
        if deprecated:
            out.append(f"Deprecated: {', '.join(deprecated)}")
        out.append(f"Global options: {GLOBAL_OPTIONS}")
        out.append("Help for one command: agentbus COMMAND --help")
        return "\n".join(out)

    def draw(console: Any) -> None:
        from rich.text import Text

        console.print(_wordmark())
        console.print(Text.assemble(("Usage: ", "bold"), ROOT_USAGE))
        for title, rows in sections:
            console.print()
            console.print(Text(title, style="bold yellow"))
            for v, s in rows:
                console.print(Text.assemble("  ", (f"{v:<{pad}}", "bold cyan"), s))
        console.print()
        if deprecated:
            console.print(Text(f"Deprecated: {', '.join(deprecated)}", style="dim"))
        console.print(Text.assemble(("Global options: ", "dim"), GLOBAL_OPTIONS))
        console.print(
            Text.assemble(("Help for one command: ", "dim"), ("agentbus COMMAND --help", "bold"))
        )

    return _render(draw)


def _detail_sections(
    command: click.Command, ctx: click.Context
) -> list[tuple[str, list[tuple[str, str]]]]:
    sections: list[tuple[str, list[tuple[str, str]]]] = []
    arguments = [
        (p.human_readable_name, p.help)
        for p in command.params
        if isinstance(p, BusArgument) and p.help
    ]
    if arguments:
        sections.append(("Arguments", arguments))
    if isinstance(command, click.Group):
        children = [(n, c.get_short_help_str(80)) for n, c in command.commands.items()]
        if children:
            sections.append(("Commands", children))
    options = []
    for param in command.get_params(ctx):
        if isinstance(param, click.Option):
            record = param.get_help_record(ctx)
            if record is not None:
                options.append(record)
    if options:
        sections.append(("Options", options))
    return sections


def verb_help(command: click.Command, ctx: click.Context) -> str:
    pieces = " ".join(command.collect_usage_pieces(ctx))
    description = (command.help or "").strip()
    description = description[:1].upper() + description[1:]
    sections = _detail_sections(command, ctx)
    if not styled(sys.stdout):
        formatter = ctx.make_formatter()
        formatter.write_usage(ctx.command_path, pieces)
        if description:
            formatter.write_paragraph()
            with formatter.indentation():
                formatter.write_text(description)
        for title, rows in sections:
            with formatter.section(title):
                formatter.write_dl(rows)
        return formatter.getvalue().rstrip("\n")

    def draw(console: Any) -> None:
        from rich.padding import Padding
        from rich.table import Table
        from rich.text import Text

        console.print(Text.assemble(("Usage: ", "bold"), (f"{ctx.command_path} {pieces}", "bold")))
        if description:
            console.print()
            console.print(Padding(Text(description), (0, 0, 0, 2)))
        for title, rows in sections:
            console.print()
            console.print(Text(title, style="bold yellow"))
            table = Table.grid(padding=(0, 2))
            table.add_column(no_wrap=True)
            table.add_column()
            for left, right in rows:
                table.add_row(Text("  " + left, style="bold cyan"), Text(right))
            console.print(table)

    return _render(draw)


group_help = verb_help


def _error_lines(exc: click.UsageError) -> list[str]:
    if isinstance(exc, UnknownVerb):
        head = f"agentbus: there is no `{exc.typed}` command."
        if exc.hint:
            return [
                head,
                "",
                f"  You probably want:  agentbus {exc.hint}",
                "",
                "  `agentbus quickref` lists the common flows.",
            ]
        return [head, "  Run `agentbus` to see every command."]
    ctx = exc.ctx
    path = ctx.command_path if ctx is not None else "agentbus"
    lines = [f"{path}: error: {exc.format_message()}"]
    if ctx is not None:
        pieces = " ".join(ctx.command.collect_usage_pieces(ctx))
        lines.append(f"  usage: {path} {pieces}".rstrip())
    lines.append(f"  help:  {path} --help")
    return lines


def usage_error(exc: click.UsageError, stream: IO[str] | None = None) -> None:
    target = sys.stderr if stream is None else stream
    lines = _error_lines(exc)
    if not styled(target):
        target.write("\n".join(lines) + "\n")
        return

    def draw(console: Any) -> None:
        from rich.text import Text

        for index, line in enumerate(lines):
            style = "bold red" if index == 0 else ("bold" if "You probably want" in line else "")
            console.print(Text(line, style=style))

    target.write(_render(draw) + "\n")
