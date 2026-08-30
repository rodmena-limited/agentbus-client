"""#50: an unknown verb must point at the real one, not print 52 choices.

REPORTED BY crypto-trader-manager-6a3048, with the consequence measured rather
than imagined. Told to check in with a peer every 20 minutes, they reached for
`agentbus cron`, got argparse's wall of 52 choices, concluded the bus had no
self-scheduling, and wired a SESSION-LOCAL timer instead. It died with the
session and the follow-ups stopped silently — the wrong setup is
indistinguishable from the right one until the session ends.

`remind` was in the list they had just been shown. A wall of choices is
something a reader SKIMS AND CONCLUDES FROM, which makes it worse than useless
when the right answer is in it.

NOT SOLVED WITH ALIASES, deliberately. `agentbus cron` as a second spelling of
`remind --repeat` would be one concept with two names — the one-fact-two-places
trap behind this repo's split-identity bugs (#40, #44). A suggestion teaches the
verb; an alias hides it, and the reader never discovers `--expire` or `reminds`.
"""

from __future__ import annotations

import pytest

from agentbus_client.cli._parser import build_parser


def _suggestion_line(err: str) -> str:
    """ONLY the suggestion line.

    Asserting against the whole of stderr is vacuous here, and a mutation
    proved it: with the intent map removed, argparse prints its full list of 52
    choices — which CONTAINS the word "remind" — so `"remind" in err` passed on
    the very wall the suggestion exists to replace. 7 of 8 cases went green
    against broken code.
    """
    for line in err.splitlines():
        if "You probably want" in line:
            return line
    return ""


@pytest.mark.parametrize(
    "typed",
    ["cron", "crontab", "schedule", "timer", "wake", "snooze", "poke", "followup"],
)
def test_scheduling_intents_point_at_remind(typed, capsys):
    """Every word an operator reaches for when they mean 'do this later'."""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([typed])
    line = _suggestion_line(capsys.readouterr().err)
    assert line, f"{typed!r} produced no suggestion at all"
    assert "remind" in line, f"{typed!r} did not point at remind: {line!r}"


def test_the_suggestion_names_repeat_for_recurring_intents(capsys):
    """`cron` means RECURRING. Pointing at bare `remind` would send them to a
    one-shot and the 20-minute cadence would fire once."""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["cron"])
    assert "--repeat" in _suggestion_line(capsys.readouterr().err)


def test_a_plain_typo_gets_the_close_match(capsys):
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["inbx"])
    assert "inbox" in _suggestion_line(capsys.readouterr().err)


def test_an_unguessable_verb_still_falls_back_to_argparse(capsys):
    """KNOWN-NEGATIVE. If every unknown word produced a confident suggestion,
    the suggestion would carry no information — and a wrong one is worse than
    the list, because it will be followed."""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["zzzzzzqqqq"])
    err = capsys.readouterr().err
    assert "You probably want" not in err
    assert "invalid choice" in err


@pytest.mark.parametrize("verb", ["inbox", "remind", "reminds", "whoami", "show"])
def test_real_verbs_are_not_intercepted(verb):
    """KNOWN-POSITIVE control: the handler must only fire on UNKNOWN verbs.

    Without this, 'suggests correctly' would also pass in a world where the
    parser had stopped accepting real commands entirely.
    """
    parser = build_parser()
    try:
        parser.parse_args([verb, "--help"])
    except SystemExit as exc:
        # --help exits 0; an invalid-choice interception would exit 2
        assert exc.code == 0, f"{verb} was intercepted as unknown"


# --- #52: the fuzzy fallback must not invent a confident wrong answer --------
#
# Shipped 0.9.63 with cutoff=0.6 and immediately found `agentbus nudge`
# suggesting `agentbus usage` — a quota command, to somebody who meant remind.
# difflib scored that pair at exactly 0.60, the cutoff itself.
#
# Real typos measured against the actual verb list score 0.75-0.91, so the
# threshold is chosen from that gap rather than by feel.


@pytest.mark.parametrize(
    "typed,want",
    [
        ("inbx", "inbox"),
        ("sned", "send"),
        ("statu", "status"),
        ("remnid", "remind"),
        ("wathc", "watch"),
    ],
)
def test_real_typos_still_resolve(typed, want, capsys):
    """KNOWN-POSITIVE for the threshold: tightening it must not break typos."""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([typed])
    assert want in _suggestion_line(capsys.readouterr().err)


@pytest.mark.parametrize("typed", ["nudge", "ping", "chase", "deploy", "commit"])
def test_a_semantic_near_miss_gets_no_confident_suggestion(typed, capsys):
    """THE 0.9.63 BUG. `nudge` is not a typo of `usage`; it is a different word.

    Falling back to argparse costs the reader a list. A wrong suggestion costs
    them the wrong command, because it will be followed — which is the reason
    this handler exists, not a reason to loosen it.
    """
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([typed])
    assert _suggestion_line(capsys.readouterr().err) == "", f"{typed!r} got a guess"


# --- #48: the words someone reaches for when a peer will not stop ------------


@pytest.mark.parametrize("typed", ["mute", "ignore", "silence", "spam", "blocklist", "unmute"])
def test_suppression_intents_point_at_the_block_verbs(typed, capsys):
    """The `remind` incident was a feature that existed with nothing pointing at
    it, and an agent built a session-local timer instead. `block` is a feature
    somebody reaches for while ALREADY ANNOYED, which is the worst moment to be
    handed 52 choices."""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([typed])
    line = _suggestion_line(capsys.readouterr().err)
    assert line, f"{typed!r} produced no suggestion"
    assert "block" in line, f"{typed!r} did not point at block: {line!r}"


def test_unmute_points_at_unblock_not_block(capsys):
    """Pointing `unmute` at `block` would do the opposite of what was asked."""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["unmute"])
    assert "unblock" in _suggestion_line(capsys.readouterr().err)
