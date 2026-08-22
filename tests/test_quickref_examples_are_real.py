"""Every command the quickref shows must actually parse.

A doc naming a flag the binary lacks is worse than no doc: the reader concludes
their install is broken. This repo has already shipped that failure twice — a
withdrawal section describing a command that was never implemented, and a
served llms.txt telling readers to use `agentbus show --raw` months before the
flag existed, and again telling them it did not exist an hour after it shipped.

So the quickref is checked against the PARSER rather than proof-read.
"""

from __future__ import annotations

import re
import shlex

import pytest

from agentbus_client.cli._diag import QUICKREF
from agentbus_client.cli._parser import build_parser


def _example_commands() -> list[str]:
    """Every `agentbus ...` line in the quickref, trimmed of its comment column."""
    found = []
    for raw in QUICKREF.splitlines():
        line = raw.strip()
        if not line.startswith("agentbus "):
            continue
        # the right-hand column is prose, separated by 2+ spaces
        cmd = re.split(r"\s{2,}", line)[0].strip()
        if cmd:
            found.append(cmd)
    return found


def test_the_quickref_actually_contains_examples():
    """KNOWN-POSITIVE. Without this, a scraper that silently matched nothing
    would make every assertion below vacuously true — the exact shape of a
    check that cannot go red."""
    assert len(_example_commands()) > 10


@pytest.mark.parametrize("cmd", _example_commands())
def test_each_example_parses(cmd: str):
    parser = build_parser()
    # Shell redirection is not part of the command the parser sees.
    cmd = cmd.split("<")[0].strip() if " < " in f" {cmd} " else cmd
    argv = [a for a in shlex.split(cmd)[1:] if a != "\\"]
    # `[--flag]` marks an OPTIONAL flag in the quickref's notation; the command
    # must parse both with and without it, and the shape is what is under test.
    argv = [a.strip("[]") for a in argv]
    # Placeholders stand in for real values; the parser only needs the shape.
    argv = [re.sub(r"^<.*>$", "PLACEHOLDER", a) for a in argv]
    try:
        parser.parse_args(argv)
    except SystemExit as exc:  # argparse exits 2 on an unknown flag
        pytest.fail(f"quickref shows a command the parser rejects: {cmd!r} ({exc})")
