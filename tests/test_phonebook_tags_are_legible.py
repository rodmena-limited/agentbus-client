"""The tag display must name a key, and line up as a column.

#182 and #183, both measured on the served build with Farshid.

    agentbus-279ca7  responsive  agentbus+...  [+3 more]
    alice-b3394d     idle        agentbus+...  [demo:agentbus,film:the-hive +4 more]

Two separate defects in two lines of output:

  * `[+3 more]` names NOTHING. One long value — `duty:bus-core=owns the
    AgentBus server and deploys` — consumed the whole budget alphabetically
    before any key was printed, so the agent with the most descriptive tags was
    the one whose tags could not be seen. A value is never what you scan a
    roster for; a key is.
  * the tag cell was appended after the address and capability list, both
    variable-width, so no two rows lined up and whoever had the longest address
    decided where everyone else's tags began.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from agentbus_client.cli import _format_tags

# The exact labels from the measured row, which is what makes this a regression
# test rather than an invented case.
REPORTED = {
    "duty:bus-core": "owns the AgentBus server and deploys",
    "team:hive": "",
    "film:the-hive": "",
    "demo:agentbus": "",
}


def test_a_long_value_no_longer_hides_every_key() -> None:
    out = _format_tags(REPORTED, limit=40)
    assert out != "+4 more"
    assert ":" in out, f"named no key at all: {out!r}"
    assert "more" in out, "the elision must still be declared"


def test_the_key_survives_when_its_value_cannot() -> None:
    """A single tag whose value is enormous degrades to the bare key rather
    than vanishing into a count."""
    out = _format_tags({"duty:everything": "x" * 200}, limit=40)
    assert out == "duty:everything"


@pytest.mark.parametrize("limit", [12, 20, 40, 60, 200])
def test_the_rendering_never_overruns_its_budget(limit: int) -> None:
    """Including the width of its own ' +N more' marker — the finished string is
    what has to fit, not the part before the suffix."""
    assert len(_format_tags(REPORTED, limit=limit)) <= max(limit, len("+4 more"))


def test_everything_fitting_is_rendered_whole() -> None:
    """The known-positive. Without it, a formatter that always returned '+N
    more' would pass every assertion above."""
    out = _format_tags({"team:a": "", "skill:b": ""}, limit=60)
    assert out == "skill:b,team:a"
    assert "more" not in out


def test_no_tags_renders_nothing_rather_than_an_empty_bracket() -> None:
    assert _format_tags({}) == ""
    assert _format_tags(None) == ""


def test_tags_are_a_fixed_column_not_a_suffix() -> None:
    """#183. Read off the source: the tag cell is padded to a computed width and
    printed BEFORE the address, so rows align regardless of address length.

    A rendering test without a terminal is weak evidence on its own; what it
    pins is that the cell cannot go back to being appended to a variable-width
    line, which is the defect.
    """
    source = (REPO / "src" / "agentbus_client" / "cli" / "_directory.py").read_text()
    assert "tag_width = min(" in source
    assert "{cell:<{tag_width + 2}}  {agent['address']}" in source


def test_every_column_lines_up_across_rows(capsys, monkeypatch) -> None:
    """THE TEST THE UNIT TESTS COULD NOT BE.

    Feeding _format_tags directly proves a string is short enough; it never
    lays two rows beside each other, and both remaining defects lived exactly
    there. The live roster showed `presence:<7` while "responsive" is TEN
    characters, so every responsive row shoved the rest three columns right —
    invisible to any assertion about one row.

    So this renders a roster whose rows disagree on every width and asserts the
    address column starts at one offset.
    """
    import argparse

    from agentbus_client import cli

    roster = [
        {"name": "a", "presence": "responsive", "address": "agentbus+a@x", "labels": {}},
        {
            "name": "much-longer-name",
            "presence": "idle",
            "address": "agentbus+b@x",
            "labels": {"team:frontend": "", "skill:playwright": "takes the screenshots"},
        },
        {"name": "mid", "presence": "idle", "address": "agentbus+c@x", "labels": {"solo:tag": ""}},
    ]

    class _Bus:
        def phonebook(self, *_a, **_k):
            return roster

    monkeypatch.setattr(cli._common, "_bus", lambda _args: _Bus())
    cli.cmd_phonebook(argparse.Namespace(query=None, capability=None, label=None, json=False))
    lines = [ln for ln in capsys.readouterr().out.splitlines() if "agentbus+" in ln]
    assert len(lines) == len(roster)
    offsets = {ln.index("agentbus+") for ln in lines}
    assert len(offsets) == 1, f"address column starts at {sorted(offsets)} — rows do not align"


def test_history_renders_the_fields_the_endpoint_actually_returns(capsys, monkeypatch) -> None:
    """A blank cell where a value belongs is a silent display failure.

    My first `agentbus history` guessed `sent_at`/`sender_display` from the
    INBOX payload; the room-history endpoint returns `created_at`/`sender`. Every
    row printed a blank timestamp and nothing failed — the same shape as a column
    that is written and never selected, one layer further out.

    This fixture uses the field names the deployed endpoint returned, so it goes
    red if the CLI drifts back onto the inbox's names.
    """
    import argparse

    from agentbus_client import cli

    class _Bus:
        def room_history(self, *_a, **_k):
            return {
                "room": "ops",
                "messages": [
                    {
                        "message_id": "01M0",
                        "thread_id": "01M1",
                        "subject": "deploy",
                        "text_body": "x",
                        "sender": "builder",
                        "sender_internal": True,
                        "priority": "normal",
                        "created_at": "2026-08-15T13:02:17.000000+00:00",
                    }
                ],
            }

    monkeypatch.setattr(cli._common, "_bus", lambda _args: _Bus())
    cli.cmd_history(argparse.Namespace(room="ops", limit=None, since=None, json=False))
    out = capsys.readouterr().out
    assert "2026-08-15 13:02:17" in out, f"timestamp missing: {out!r}"
    assert "builder" in out, f"sender missing: {out!r}"
