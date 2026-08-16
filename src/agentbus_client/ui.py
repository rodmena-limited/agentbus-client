"""Styled CLI output for the onboarding surface (issuedb #119).

One place, so every command looks like the same product. Built on `rich`, and
DEGRADES HONESTLY: when stdout is not a TTY, or NO_COLOR is set, everything
falls back to the exact plain lines the old `_say` printed — scripts, probes
and transcripts parse the same text they always did. Style is additive, never
load-bearing: no assertion anywhere may depend on ANSI codes.

The wordmark is AGENT/BUS on the operator's instruction — the product name
must be unmissable at the top of every onboarding interaction.
"""

from __future__ import annotations

import os
import sys
from typing import Any

_FORCE_PLAIN = bool(os.environ.get("NO_COLOR")) or not sys.stdout.isatty()


def _console() -> Any | None:
    if _FORCE_PLAIN:
        return None
    try:
        from rich.console import Console

        return Console(highlight=False)
    except ImportError:
        return None


_CON = _console()

#: The wordmark. Slash on purpose: AGENT/BUS reads as two words at a glance.
WORDMARK = "AGENT/BUS"


def banner(subtitle: str) -> None:
    """The product header every onboarding command opens with."""
    if _CON is None:
        print(f"== {WORDMARK} — {subtitle}")
        return
    from rich.panel import Panel
    from rich.text import Text

    mark = Text()
    mark.append("AGENT", style="bold white on blue")
    mark.append("/", style="bold blue")
    mark.append("BUS", style="bold blue")
    mark.append(f"  {subtitle}", style="dim")
    _CON.print(Panel(mark, border_style="blue", expand=False))


def ok(message: str) -> None:
    """Plain mode prints the bare message — legacy scripts grep for exact
    phrases like 'key VERIFIED against', not for an 'ok' prefix."""
    if _CON is None:
        print(message)
    else:
        _CON.print(f"  [green]✓[/green] {message}")


def item(label: str, value: str) -> None:
    """An aligned key/value line inside a section.

    PLAIN MODE KEEPS THE LEGACY `label: value` SHAPE EXACTLY. The probes and
    the container harness parse `registered: <name>` and friends from stdout;
    the first cut of this module printed aligned columns in plain mode too and
    broke 18 harness assertions in one run. Style may only change what a HUMAN
    sees on a TTY — piped output is an API.
    """
    if _CON is None:
        print(f"  {label}: {value}")
    else:
        _CON.print(f"  [bold cyan]{label:<12}[/bold cyan] {value}")


def warn(message: str) -> None:
    if _CON is None:
        print(f"  !    {message}")
    else:
        _CON.print(f"  [yellow]![/yellow] {message}")


def fail(message: str) -> None:
    if _CON is None:
        print(f"  FAIL {message}")
    else:
        _CON.print(f"  [red]✗[/red] {message}")


def next_steps(*commands: str) -> None:
    """The 'what now' block — always the last thing a command prints."""
    if _CON is None:
        print("Next:")
        for c in commands:
            print(f"  {c}")
        return
    from rich.panel import Panel
    from rich.text import Text

    body = Text()
    for i, c in enumerate(commands):
        if i:
            body.append("\n")
        body.append("  $ ", style="dim")
        body.append(c, style="bold")
    _CON.print(Panel(body, title="Next", border_style="green", expand=False))


def prompt_secret(label: str) -> str:
    """Ask for a secret without echoing it. TTY only — callers must check."""
    import getpass

    return getpass.getpass(f"{label}: ")
