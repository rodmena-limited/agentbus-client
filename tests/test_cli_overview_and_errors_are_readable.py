from __future__ import annotations

import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

import click
import pytest

from agentbus_client import __version__
from agentbus_client.cli import _help
from agentbus_client.cli._app import BusArgument
from agentbus_client.cli._parser import build, parse_args, verbs

SRC = str(Path(__file__).resolve().parents[1] / "src")
ANSI = re.compile(r"\x1b\[")
ROW = re.compile(r"^  (\S+)\s{2,}\S")


def _cli(*argv: str, **env: str) -> subprocess.CompletedProcess[str]:
    environment = {k: v for k, v in os.environ.items() if k != "NO_COLOR"}
    environment["PYTHONPATH"] = SRC + os.pathsep + environment.get("PYTHONPATH", "")
    environment.update(env)
    return subprocess.run(
        [sys.executable, "-m", "agentbus_client.cli", *argv],
        capture_output=True,
        text=True,
        env=environment,
        timeout=60,
    )


def _context(verb: str | None = None) -> tuple[click.Command, click.Context]:
    root = build()
    parent = root.make_context("agentbus", [], resilient_parsing=True)
    if verb is None:
        return root, parent
    command = root.commands[verb]
    return command, command.make_context(verb, [], parent=parent, resilient_parsing=True)


def test_bare_agentbus_prints_the_overview_and_exits_0():
    result = _cli()
    assert result.returncode == 0, result.stderr
    assert "Send mail:" in result.stdout
    assert "{invite,join" not in result.stdout + result.stderr
    assert result.stderr == ""


def test_every_verb_is_in_the_overview_exactly_once():
    out = _cli().stdout
    rows = Counter(m.group(1) for line in out.splitlines() if (m := ROW.match(line)))
    assert set(rows) == set(verbs()) - set(_help.DEPRECATED)
    assert all(count == 1 for count in rows.values())
    footer = next(line for line in out.splitlines() if line.startswith("Deprecated:"))
    assert all(name in footer for name in _help.DEPRECATED)


def test_the_overview_fits_its_budget():
    out = _cli().stdout
    assert len(out.splitlines()) <= 85
    assert len(_help.SECTIONS) <= 9
    assert [(v, s) for _, rows in _help.SECTIONS for v, s in rows if len(s) > 60] == []


def test_piped_output_carries_no_ansi():
    result = _cli()
    assert result.stdout
    assert not ANSI.search(result.stdout)
    error = _cli("send")
    assert error.stderr
    assert not ANSI.search(error.stderr)


def test_a_terminal_does_get_colour(monkeypatch):
    monkeypatch.setattr(_help, "styled", lambda stream: True)
    root, ctx = _context()
    text = _help.overview(root, ctx)
    assert ANSI.search(text)
    assert "send" in text


def test_no_color_turns_styling_off_even_on_a_terminal(monkeypatch):
    class Tty:
        def isatty(self) -> bool:
            return True

    monkeypatch.delenv("NO_COLOR", raising=False)
    assert _help.styled(Tty()) is True
    monkeypatch.setenv("NO_COLOR", "1")
    assert _help.styled(Tty()) is False


def test_a_missing_argument_is_a_short_error_not_a_wall():
    result = _cli("send")
    lines = result.stderr.strip().splitlines()
    assert result.returncode == 2
    assert 1 <= len(lines) <= 6, result.stderr
    assert "agentbus send --help" in result.stderr
    assert "invite" not in result.stderr
    assert result.stdout == ""


def test_an_unknown_option_is_named_in_a_short_error():
    result = _cli("inbox", "--bogus")
    assert result.returncode == 2
    assert "--bogus" in result.stderr
    assert len(result.stderr.strip().splitlines()) <= 6


def test_a_bad_value_exits_2_and_names_the_option():
    result = _cli("inbox", "--limit", "notanumber")
    assert result.returncode == 2
    assert "--limit" in result.stderr


def test_keys_without_a_subcommand_is_a_usage_error():
    assert _cli("keys").returncode == 2


def test_verb_help_shows_argument_help_and_options():
    found = None
    for name, command in sorted(build().commands.items()):
        for param in command.params:
            if isinstance(param, BusArgument) and param.help:
                found = (name, param.help)
                break
        if found:
            break
    assert found is not None
    name, text = found
    result = _cli(name, "--help")
    assert result.returncode == 0
    assert "".join(text.split())[:40] in "".join(result.stdout.split())
    assert "Options" in result.stdout


def test_version_prints_the_client_version():
    result = _cli("--version")
    assert result.returncode == 0
    assert result.stdout.strip() == f"agentbus {__version__}"


def test_a_terminal_render_has_no_trailing_padding(monkeypatch):
    monkeypatch.setattr(_help, "styled", lambda stream: True)
    command, ctx = _context("send")
    text = _help.verb_help(command, ctx)
    assert ANSI.search(text)
    padded = [line for line in text.split("\n") if re.search(r"[ \t]+(?:\x1b\[[0-9;]*m)*$", line)]
    assert padded == []


def test_rendering_help_in_process_leaves_h_working():
    command, ctx = _context("send")
    _help.verb_help(command, ctx)
    with pytest.raises(SystemExit) as exit_info:
        parse_args(["send", "-h"])
    assert exit_info.value.code == 0
