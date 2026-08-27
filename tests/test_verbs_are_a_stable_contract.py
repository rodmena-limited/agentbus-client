"""#53: `quickref --verbs` is the verb list as a CONTRACT, not a scrape.

agentbus-8dc08d's doc guard diffs their skill against this client's verb list,
and obtained it by regexing `agentbus --help`. argparse WRAPS that usage line, so
a line-oriented regex can return a partial list: they measured 46 where the
authoritative count is 52, and were one step from lowering a ratchet budget on
the strength of it.

I could not reproduce their truncation at any terminal width, so this is not a
claim about their mechanism. It does not need to be. `--help` is a HUMAN surface
whose line breaks are a rendering detail; a downstream guard should never depend
on one, and this is the surface that cannot wrap.

The list must therefore be COMPLETE and MATCH THE PARSER — if it drifted, every
consumer's guard would drift with it and none of them could tell.
"""

from __future__ import annotations

import argparse
import io
import json
from contextlib import redirect_stdout

from agentbus_client.cli._diag import cmd_quickref
from agentbus_client.cli._parser import build_parser


def _parser_verbs() -> list[str]:
    parser = build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return sorted(action.choices)
    raise AssertionError("no subparsers found")


def _run(**flags) -> str:
    args = argparse.Namespace(json=flags.get("json", False), verbs=flags.get("verbs", False))
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert cmd_quickref(args) == 0
    return buf.getvalue()


def test_the_list_matches_the_parser_exactly():
    """THE CONTRACT. Not 'contains some verbs' — the same set."""
    printed = [ln for ln in _run(verbs=True).splitlines() if ln.strip()]
    assert printed == _parser_verbs()


def test_there_are_verbs_to_check():
    """KNOWN-POSITIVE: an empty list would satisfy `printed == parser_verbs`
    if the parser were also broken, and satisfies nothing useful."""
    assert len(_parser_verbs()) > 40


def test_one_verb_per_line_so_wc_l_is_the_count():
    """The whole point is that a script can count it without a regex."""
    out = _run(verbs=True)
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert len(lines) == len(_parser_verbs())
    assert all(" " not in ln.strip() for ln in lines), "a verb line must be one token"


def test_json_form_is_a_list_of_strings():
    data = json.loads(_run(verbs=True, json=True))
    assert data == _parser_verbs()


def test_without_the_flag_it_is_still_the_human_quickref():
    """Known-negative: the flag must not have replaced the default output."""
    out = _run()
    assert "AgentBus quick reference" in out
    assert "agentbus whoami" in out


def test_known_verbs_from_the_reported_gap_are_present():
    """The verbs that started this thread must be in the contract, since a
    consumer's doc guard is what will look for them."""
    verbs = _parser_verbs()
    for v in ("remind", "reminds", "whoami", "ack", "approve", "approval"):
        assert v in verbs
