"""`agentbus approval <id>` — the CLI twin of MCP's bus_approval_status.

issuedb #37, SPECS/0025-mcp-cli-surface-parity.md.

THE GAP: `agentbus approve --wait` can only wait on an approval it just minted
(it passes its own create-response id straight through), so a session that
restarted — or was handed an id by a peer — had no CLI route to that approval.
MCP has had `bus_approval_status` throughout; this was surface drift.

THE SHARP EDGE these tests exist for: a denial and "nobody has answered yet"
must NEVER share an exit code. `timed_out` is the human's window closing, which
is a denial. Our own `--wait` elapsing is not a decision at all. A script that
conflates them either abandons work a reviewer was still considering, or
proceeds unauthorised.
"""

from __future__ import annotations

import pytest

from agentbus_client.cli._forward import _print_reviewer_reasoning, _report_approval

# The exact outcome payload the live server returned for a real rejected
# approval (01M0GKM6R5QZRQ874P4FQXM1T1), trimmed to the fields that matter.
# Captured from the wire, NOT hand-written to match the parser: two earlier
# drafts of the printer passed their own invented shape and printed nothing at
# all against this one.
LIVE_REJECTED_OUTCOME = {
    "status": "rejected",
    "feedback_for_agent": {
        "message": "i rejected, but regardles, the goal is to see if you will wake up without user interaction.",
        "reason_codes": ["OTHER"],
        "change_requests": [],
    },
    "outcome": {
        "result": "rejected",
        "reasons": [
            {
                "code": "OTHER",
                "text": "i rejected, but regardles, the goal is to see if you will wake up without user interaction.",
                "actor_id": "farshid@rodmena.co.uk",
            }
        ],
        "justifications": [{"text": "test", "actor_id": "farshid@rodmena.co.uk"}],
    },
}


def test_approved_is_the_only_zero():
    assert _report_approval({"id": "a", "status": "approved"}) == 0


@pytest.mark.parametrize("status", ["rejected", "cancelled", "timed_out", "changes_requested"])
def test_every_other_terminal_status_is_a_denial(status, capsys):
    """All four non-approved terminal states exit 1, not 0 and not 7.

    `cancelled` is here deliberately: the backend delivered NOTHING to the
    requester on cancellation until build e34edad, so a client waiting on a
    cancelled approval used to hang on a decision that had already been made.
    """
    assert _report_approval({"id": "a", "status": status}) == 1
    assert "DO NOT PROCEED" in capsys.readouterr().out


def test_waited_out_is_not_a_denial_and_not_an_approval(capsys):
    """The distinction the old `0 if approved else 1` could not express."""
    code = _report_approval({"id": "a", "status": "pending", "waited_out": True})
    assert code == 7, "a wait that elapsed is not a decision"
    out = capsys.readouterr().out
    assert "NOT a decision" in out


def test_timed_out_and_waited_out_do_not_collapse():
    """The single most dangerous confusion in this command.

    `timed_out` = the human's window closed -> a denial (1).
    `waited_out` = our --wait elapsed      -> no answer yet (7).
    """
    denial = _report_approval({"id": "a", "status": "timed_out"})
    no_answer = _report_approval({"id": "a", "status": "pending", "waited_out": True})
    assert denial == 1 and no_answer == 7
    assert denial != no_answer


def test_pending_without_waiting_is_not_success(capsys):
    """Checking without --wait must not read as a go."""
    assert _report_approval({"id": "abc", "status": "pending"}) == 7
    assert "agentbus approval abc --wait" in capsys.readouterr().out


def test_unknown_status_is_never_silently_approved():
    """A status we have never heard of is not a go and not a denial."""
    assert _report_approval({"id": "a", "status": "some_new_state"}) == 7


# --------------------------------------------------------------- the printer


def test_reviewer_reasoning_prints_from_the_real_payload(capsys):
    """THE KNOWN-POSITIVE. If this cannot go green, the absence checks below
    are worthless — a printer that renders nothing passes every 'no output'
    assertion with full confidence. Both earlier drafts failed exactly here."""
    _print_reviewer_reasoning(LIVE_REJECTED_OUTCOME)
    out = capsys.readouterr().out
    assert "i rejected, but regardles" in out, "the reviewer's own words must appear"
    assert "OTHER" in out
    assert "justification: test" in out
    assert "farshid@rodmena.co.uk" in out


def test_flat_keys_are_not_where_the_reasoning_lives(capsys):
    """NEGATIVE CONTROL for draft 1, which guessed flat `reason`/`feedback`."""
    _print_reviewer_reasoning({"reason": "x", "feedback": "y"})
    assert "no reviewer reasoning recorded" in capsys.readouterr().out


def test_top_level_reasons_are_not_where_the_reasoning_lives(capsys):
    """NEGATIVE CONTROL for draft 2, which looked one level too shallow."""
    _print_reviewer_reasoning({"reasons": [{"text": "not here"}]})
    out = capsys.readouterr().out
    assert "not here" not in out
    assert "no reviewer reasoning recorded" in out


def test_absent_reasoning_says_so_rather_than_printing_nothing(capsys):
    """Silence is indistinguishable from the bug this printer has had twice."""
    _print_reviewer_reasoning({"status": "timed_out"})
    assert "no reviewer reasoning recorded" in capsys.readouterr().out


def test_printer_tolerates_junk_without_raising(capsys):
    for junk in (
        None,
        "a string",
        42,
        [],
        {"outcome": "not-a-dict"},
        {"outcome": {"reasons": ["not-a-dict"]}},
    ):
        _print_reviewer_reasoning(junk)
    assert capsys.readouterr().out  # said "none recorded" rather than crashing


def test_duplicate_feedback_and_reason_print_once(capsys):
    """The live payload repeats the same string in both fields; printing it
    twice reads as two separate objections."""
    _print_reviewer_reasoning(LIVE_REJECTED_OUTCOME)
    body = capsys.readouterr().out
    assert body.count("i rejected, but regardles") == 1


# --------------------------------------------------------------- wiring


def test_the_verb_is_registered_and_takes_an_id():
    """The whole point: reach an approval THIS process did not create."""
    from agentbus_client.cli._parser import build_parser

    sub = next(a for a in build_parser()._actions if a.dest == "command")
    assert "approval" in sub.choices
    args = build_parser().parse_args(["approval", "01M0X", "--wait", "30"])
    assert args.approval_id == "01M0X" and args.wait == 30


def test_wait_is_optional():
    from agentbus_client.cli._parser import build_parser

    assert build_parser().parse_args(["approval", "01M0X"]).wait == 0
